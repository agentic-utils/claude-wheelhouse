"""The daemon, in process: its routes through httpx2's ASGI transport, never a socket."""

import asyncio
import json
import os
import signal
import socket
import sqlite3
import time
import uuid

import anyio
import httpx2
import pytest
from sse_starlette.sse import AppStatus

from claude_wheelhouse import daemon as daemon_mod, hub as hub_mod, launch, liveness, store as store_mod, transcript
from claude_wheelhouse.daemon import AlreadyRunning, Daemon, Feed, PortTaken, checked_address, hosts, listener, take_lock
from claude_wheelhouse.hub import Event
from claude_wheelhouse.store import COMPACT_QUESTION, SessionGone, Store

pytestmark = pytest.mark.anyio
URL = "http://127.0.0.1:7428"
TOKEN = "the-test-token"


@pytest.fixture
def daemon(store, monkeypatch, sdk_on):
    """A daemon in SDK mode, which a remote client's sessions need (test_sdk_mode_off_refuses_new_sessions)."""
    monkeypatch.setattr(launch, "open_session", lambda store, sid, may_tab=True: None)
    monkeypatch.setattr(launch, "restore_session", lambda store, sid, sdk=None, may_tab=True: None)
    d = Daemon(store, hosts("127.0.0.1", 7428), TOKEN, inline=True)
    d.feed = Feed(start=100)
    yield d
    d.pool.shutdown()
    d.worker.shutdown()
    d.watch.close()


def client(daemon, **headers):
    """Through the ASGI transport, with the token unless the headers say otherwise."""
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=daemon.app), base_url=URL,
                              headers={"Authorization": f"Bearer {TOKEN}", **headers})


async def call(daemon, method, path, body=None, key=None, **headers):
    """One request; a POST with an Idempotency-Key of its own unless given one."""
    if method == "POST" and "idempotency_key" not in headers:
        headers["Idempotency-Key"] = key or str(uuid.uuid4())
    headers = {k.replace("_", "-"): v for k, v in headers.items()}
    async with client(daemon) as c:
        return await c.request(method, path, headers=headers, content=None if body is None else json.dumps(body))


def told(r):
    return [n["text"] for n in r.json()["notices"]]


# each route: (method, path, body, what it did, desc). {sid}, {ref} and {tmp} are filled in;
# the session "demo" has a task T1 and a permission P1, and has never run, so it's dead
ROUTES = [
    ("GET", "/sessions", None, lambda store, sid, r: [(s["id"], s["status"], s["mode"]) for s in r.json()]
     == [(sid, "dead", "queued")], "every session, with its status and mode"),
    ("GET", "/sessions/{sid}/conversation", None, lambda store, sid, r: isinstance(r.json(), list),
     "a session's conversation, as blocks"),
    ("GET", "/items", None, lambda store, sid, r: [i["ref"] for i in r.json()] == ["P1", "T1"],
     "the inbox, ranked: the open permission first"),
    ("GET", "/items?sid={sid}&finished=1", None, lambda store, sid, r: {i["ref"] for i in r.json()} == {"P1", "T1"},
     "one session's, with the finished"),
    ("GET", "/items/{sid}/{ref}/thread", None, lambda store, sid, r: r.json()["item"]["title"] == "do it",
     "an item and its messages"),
    ("GET", "/items/{sid}/{ref}/document", None, lambda store, sid, r: r.json() is None,
     "a doc's file: none for an item that isn't a doc"),
    ("GET", "/fs?path={tmp}", None, lambda store, sid, r: r.json()["dirs"] == ["sub"], "a directory's folders"),
    ("POST", "/sessions", {"cwd": "{tmp}", "name": "new"},
     lambda store, sid, r: store.session(r.json()["sid"])["name"] == "new", "a new session, named in the answer"),
    ("POST", "/sessions/{sid}/rename", {"name": "renamed"},
     lambda store, sid, r: store.session(sid)["name"] == "renamed", "rename"),
    ("POST", "/sessions/{sid}/mode", {"mode": "immediate"},
     lambda store, sid, r: store.session(sid)["send_mode"] == "immediate", "mode, set"),
    ("POST", "/sessions/{sid}/mode", {"mode": "queued"},
     lambda store, sid, r: not r.json()["ok"] and store.session(sid)["send_mode"] is None,
     "mode, last write wins: already that, nothing done"),
    ("POST", "/sessions/{sid}/send", {"text": "hello", "ref": "{ref}"},
     lambda store, sid, r: [m["body"] for m in store.drafts(sid)] == ["hello"], "send, queued by the mode"),
    ("POST", "/sessions/{sid}/dispatch", None, lambda store, sid, r: "isn't running" in told(r)[0],
     "a session's queue: a dead one's waits"),
    ("POST", "/dispatch", None, lambda store, sid, r: told(r) == ["nothing queued"], "Send all"),
    ("POST", "/sessions/{sid}/park", {"how": "now"}, lambda store, sid, r: store.session(sid)["parked"],
     "park a dead session now"),
    ("POST", "/sessions/{sid}/unpark", None, lambda store, sid, r: r.json()["ok"], "unpark"),
    ("POST", "/sessions/{sid}/end", {"how": "force"}, lambda store, sid, r: store.session(sid) is None, "end by force"),
    ("POST", "/sessions/{sid}/interrupt", None, lambda store, sid, r: store.session(sid)["host_command"] == "interrupt",
     "a host command"),
    ("POST", "/sessions/{sid}/relaunch", None, lambda store, sid, r: told(r) == ["relaunching"],
     "relaunch a dead one"),
    ("POST", "/items/{sid}/P1/permission", {"decision": "allow"},
     lambda store, sid, r: store.item(sid, "P1")["status"] == "allowed", "a permission allowed"),
    ("POST", "/items/{sid}/{ref}/close", {"closed": True},
     lambda store, sid, r: store.item(sid, "T1")["dismissed"] == "todo", "an item closed"),
    ("POST", "/items/{sid}/{ref}/delete-family", None,
     lambda store, sid, r: store.item(sid, "T1")["dismissed"] == "todo", "an item deleted with its children"),
    ("GET", "/models", None, lambda store, sid, r: [m["id"] for m in r.json()["models"]] == [
        "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"], "the models offered"),
    ("POST", "/sessions", {"cwd": "{tmp}", "model": "claude-haiku-5-5", "effort": "low"},
     lambda store, sid, r: (store.session(r.json()["sid"])["model"], store.session(r.json()["sid"])["effort"])
     == ("claude-haiku-5-5", "low"), "a new session on a model and effort"),
    ("POST", "/sessions", {"cwd": "{tmp}"},
     lambda store, sid, r: (store.session(r.json()["sid"])["model"], store.session(r.json()["sid"])["effort"])
     == ("claude-sonnet-5-5", "high"), "with neither: the defaults (Q50)"),
    ("POST", "/sessions/{sid}/effort", {"effort": "xhigh"},
     lambda store, sid, r: not r.json()["ok"] and "type /effort there" in told(r)[0] and store.session(sid)["effort"]
     is None, "a tab's effort isn't changed from here (Q52)"),
    ("POST", "/sessions/{sid}/model", {"model": "claude-opus-5-5"},
     lambda store, sid, r: not r.json()["ok"] and store.session(sid)["model"] is None, "nor its model"),
]


def fill(value, sid, tmp_path):
    if isinstance(value, dict):
        return {k: fill(v, sid, tmp_path) for k, v in value.items()}
    if isinstance(value, list):
        return [fill(v, sid, tmp_path) for v in value]
    return value.format(sid=sid, ref="T1", tmp=tmp_path) if isinstance(value, str) else value


@pytest.mark.parametrize("method, path, body, did, desc", ROUTES)
async def test_each_route(daemon, store, sid, tmp_path, method, path, body, did, desc):
    store.post_item(sid, "task", "do it")
    store.post_item(sid, "permission", "Bash: ls")
    (tmp_path / "sub").mkdir()
    daemon.tick()
    r = await call(daemon, method, fill(path, sid, tmp_path), fill(body, sid, tmp_path))
    assert r.status_code == 200, f"{desc}: {r.text}"
    assert did(store, sid, r), f"{desc}: {r.text}"


async def test_a_change_during_a_shell_hand_over_says_it_applies_on_taking_it_back(daemon, store, sid):
    store.set_runner(sid, "sdk")
    store.set_shell(sid, "tab")
    daemon.tick()
    r = await call(daemon, "POST", f"/sessions/{sid}/effort", {"effort": "low"})
    assert r.json()["ok"] and store.session(sid)["effort"] == "low", r.text
    assert told(r) == [f"{store.session(sid)['name'] or sid[:8]}: effort low when the wheelhouse takes it back "
                       "from its shell tab, unless /effort is typed there after this"], r.text


async def test_the_inbox_and_a_thread_carry_no_seq_from_a_database_that_had_it(daemon, store, sid):
    """#70: items.seq is retired, but a database 2bbb99a opened keeps the column, unused."""
    store.db.execute("ALTER TABLE items ADD COLUMN seq INTEGER")
    store.post_item(sid, "task", "do it")
    r = await call(daemon, "GET", "/items")
    assert [i["ref"] for i in r.json()] == ["T1"] and "seq" not in r.json()[0], r.text
    r = await call(daemon, "GET", f"/items/{sid}/T1/thread")
    assert r.json()["item"]["ref"] == "T1" and "seq" not in r.json()["item"], r.text


@pytest.mark.parametrize("method, path, body, status, desc", [
    ("GET", "/sessions/gone/conversation", None, 404, "a session gone"),
    ("GET", "/items?sid=gone", None, 404, "the inbox of a session gone"),
    ("POST", "/sessions/gone/rename", {"name": "x"}, 404, "a command on a session gone"),
    ("POST", "/sessions/gone/park", {"how": "ask"}, 404, "a request of a session gone"),
    ("GET", "/items/{sid}/T9/thread", None, 404, "an item that isn't there"),
    ("POST", "/items/{sid}/T9/close", None, 404, "closing one that isn't there"),
    ("POST", "/items/{sid}/P1/permission", {"decision": "allow"}, 409, "a permission decided already"),
    ("POST", "/items/{sid}/P1/permission", {"decision": "maybe"}, 400, "a decision that isn't one"),
    ("POST", "/sessions/{sid}/park", {}, 400, "park without how"),
    ("POST", "/sessions/{sid}/relaunch", {"host": "1"}, 400, "a host that isn't [pid, start]"),
    ("POST", "/sessions/{sid}/send", {}, 400, "send without text"),
    ("POST", "/sessions/{sid}/fly", None, 404, "a command that isn't one"),
    ("POST", "/sessions", ["not", "an", "object"], 400, "a body that isn't an object"),
    ("GET", "/fs?path=/no/such/dir", None, 404, "a directory that isn't there"),
    ("POST", "/sessions", {"cwd": "/no/such/dir"}, 400, "a new session where there's no directory"),
    ("POST", "/sessions", {"cwd": "."}, 400, "nor a relative path, which would be the daemon's"),
    ("POST", "/items/{sid}/T1/permission", {"decision": "allow"}, 400, "a permission's answer to a task"),
    ("POST", "/sessions", {"cwd": "/", "model": "opus[1m]"}, 400, "a model that isn't offered"),
    ("POST", "/sessions", {"cwd": "/", "effort": "turbo"}, 400, "an effort that isn't one"),
    ("POST", "/sessions/{sid}/model", {"model": "claude-nonsense-9"}, 400, "a model change to one not offered"),
    ("POST", "/sessions/{sid}/model", {}, 400, "a model change naming none"),
    ("POST", "/sessions/{sid}/effort", {"effort": "bogus"}, 400, "an effort change to a level that isn't one"),
    ("POST", "/sessions/gone/effort", {"effort": "high"}, 404, "an effort change on a session gone"),
])
async def test_errors_map_to_statuses(daemon, store, sid, method, path, body, status, desc):
    store.post_item(sid, "task", "do it")
    ref = store.post_item(sid, "permission", "Bash: ls")
    store.answer_permission(sid, ref, "deny")
    r = await call(daemon, method, path.format(sid=sid), body)
    assert r.status_code == status, f"{desc}: {r.text}"
    assert r.json()["error"], desc


async def test_a_command_without_an_idempotency_key_is_refused(daemon, sid):
    r = await call(daemon, "POST", f"/sessions/{sid}/rename", {"name": "x"}, idempotency_key="")
    assert r.status_code == 400 and "Idempotency-Key" in r.json()["error"]


@pytest.mark.parametrize("second, status, sent, desc", [
    ({"text": "hello"}, 200, 1, "the same request again: answered the same, done once"),
    ({"text": "other"}, 422, 1, "another request under the same key: refused"),
])
async def test_a_request_sent_again_is_done_once(daemon, store, sid, second, status, sent, desc):
    first = await call(daemon, "POST", f"/sessions/{sid}/send", {"text": "hello"}, key="k1")
    again = await call(daemon, "POST", f"/sessions/{sid}/send", second, key="k1")
    assert again.status_code == status, desc
    assert len(store.drafts(sid)) == sent, desc
    if status == 200:
        assert (again.json(), again.headers["idempotent-replayed"]) == (first.json(), "true"), desc


async def test_an_answer_the_store_cant_keep_is_kept_in_memory_till_it_can(daemon, store, sid, monkeypatch, capsys):
    answer = store.answer

    def locked(*a, **kw):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(store, "answer", locked)
    first = await call(daemon, "POST", f"/sessions/{sid}/send", {"text": "hello"}, key="k1")
    again = await call(daemon, "POST", f"/sessions/{sid}/send", {"text": "hello"}, key="k1")
    assert (first.status_code, again.json(), len(store.drafts(sid))) == (200, first.json(), 1), "done once"
    assert "couldn't keep the answer" in capsys.readouterr().err
    daemon.tick()
    assert store.answered("k1") is None and daemon.unkept, "still locked: still in memory"
    monkeypatch.setattr(store, "answer", answer)
    daemon.tick()
    assert store.answered("k1")["body"] == first.text and not daemon.unkept


async def test_the_same_key_sent_at_once_is_done_once(daemon, store, sid):
    sent = await asyncio.gather(*(call(daemon, "POST", f"/sessions/{sid}/send", {"text": "hello"}, key="k1")
                                  for _ in range(4)))
    assert len(store.drafts(sid)) == 1
    assert sorted(r.headers.get("idempotent-replayed", "") for r in sent) == ["", "true", "true", "true"], \
        "one acts, the rest replay its answer"
    assert all((r.status_code, r.json()) == (200, sent[0].json()) for r in sent)


@pytest.mark.parametrize("breaks, acted, desc", [
    ("answered", 0, "the kept answers can't be read: nothing done"),
    ("marks", 1, "what it moved can't be read, after it was done"),
])
async def test_a_command_the_daemon_fails_is_answered_as_json(daemon, store, sid, monkeypatch, capsys, breaks, acted,
                                                              desc):
    def locked(*a, **kw):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(store if breaks == "answered" else daemon, breaks, locked)
    r = await call(daemon, "POST", f"/sessions/{sid}/send", {"text": "hello"})
    assert (r.status_code, r.json()["error"]) == (500, "the daemon failed: OperationalError: database is locked"), desc
    assert len(store.drafts(sid)) == acted, desc
    assert "Traceback" in capsys.readouterr().err, desc


def fails_once(real, after: bool):
    """real, but its first call fails as a locked database would: after acting, or before."""
    calls = []

    def broken(*a, **kw):
        if calls:
            return real(*a, **kw)
        calls.append(1)
        if after:
            real(*a, **kw)
        raise sqlite3.OperationalError("database is locked")
    return broken


# (path, body, the store method that fails once, whether it acted first, the second answer's
# status, whether it's replayed, what's done in the end, desc). The session is dead, so an
# answer is queued; it has a decision D1 and a permission P1
@pytest.mark.parametrize("path, body, breaks, after, status, replayed, done, desc", [
    ("send", {"text": "yes", "ref": "D1"}, "queue", True, 500, True, (1, "seen", "open"),
     "queued, then failed: the 500 kept, so a retry doesn't queue it twice"),
    ("send", {"text": "yes", "ref": "D1"}, "queue", False, 200, False, (1, "seen", "open"),
     "failed before queueing: nothing kept, so a retry queues it"),
    ("send", {"text": "yes", "ref": "D1"}, "_mark_seen", False, 200, False, (1, "seen", "open"),
     "marking the decision seen failed: one transaction with the queue, so a retry does both"),
    ("send", {"text": "no", "ref": "P1"}, "answer_permission", True, 500, True, (0, "unseen", "denied"),
     "a permission denied, then failed: a retry replays, not 'answered meanwhile'"),
    ("P1/permission", {"decision": "allow"}, "answer_permission", True, 500, True, (0, "unseen", "allowed"),
     "a permission allowed, then failed: a retry replays, not 409"),
    ("P1/permission", {"decision": "allow"}, "answer_permission", False, 200, False, (0, "unseen", "allowed"),
     "failed before allowing: a retry allows it"),
])
async def test_a_command_that_fails_is_kept_if_and_only_if_it_wrote(daemon, store, sid, monkeypatch, capsys, path,
                                                                    body, breaks, after, status, replayed, done, desc):
    store.post_item(sid, "decision", "pick one", why="over the other: simpler; undo: pick again")
    store.post_item(sid, "permission", "Bash: ls")
    monkeypatch.setattr(store, breaks, fails_once(getattr(store, breaks), after))
    url = f"/sessions/{sid}/send" if path == "send" else f"/items/{sid}/{path}"
    first = await call(daemon, "POST", url, body, key="k1")
    again = await call(daemon, "POST", url, body, key="k1")
    assert first.status_code == 500 and "database is locked" in first.json()["error"], desc
    assert (again.status_code, again.headers.get("idempotent-replayed") == "true") == (status, replayed), desc
    assert (len(store.drafts(sid)), store.item(sid, "D1")["status"], store.item(sid, "P1")["status"]) == done, desc


def test_a_callback_that_fails_is_said_with_its_traceback(daemon, capsys):
    daemon.post(lambda: 1 / 0)
    daemon.worker.submit(lambda: None).result()   # the hub's thread has run it
    err = capsys.readouterr().err
    assert "a callback failed: ZeroDivisionError" in err and "Traceback" in err


async def test_a_key_is_forgotten_after_a_day(daemon, store, sid):
    await call(daemon, "POST", f"/sessions/{sid}/send", {"text": "hello"}, key="k1")
    store.db.execute("UPDATE idempotency SET at = '2000-01-01T00:00:00+00:00'")
    await call(daemon, "POST", f"/sessions/{sid}/send", {"text": "hello"}, key="k1")
    assert len(store.drafts(sid)) == 2


async def test_an_error_is_replayed_as_it_was(daemon, store, sid):
    store.end(sid)
    first = await call(daemon, "POST", f"/sessions/{sid}/rename", {"name": "x"}, key="k1")
    again = await call(daemon, "POST", f"/sessions/{sid}/rename", {"name": "x"}, key="k1")
    assert (first.status_code, again.status_code, again.json()) == (404, 404, first.json())


@pytest.mark.parametrize("method, headers, status, desc", [
    ("GET", {}, 200, "a client naming the daemon"),
    ("GET", {"host": "localhost:7428"}, 200, "by localhost, as it's on the loopback"),
    ("GET", {"host": "LocalHost:7428"}, 200, "a host name in any case"),
    ("GET", {"host": "evil.example:7428"}, 421, "a page rebound to the daemon's address can't read it"),
    ("POST", {"host": "evil.example:7428"}, 421, "nor drive it"),
    ("POST", {}, 200, "a client sends no Origin"),
    ("POST", {"origin": URL}, 200, "a page of the daemon's own"),
    ("POST", {"origin": "http://LOCALHOST:7428"}, 200, "named in any case"),
    ("POST", {"origin": "http://evil.example"}, 403, "another site's page is refused"),
    ("POST", {"origin": "null"}, 403, "as is a page with no origin of its own"),
])
async def test_host_and_origin(daemon, sid, method, headers, status, desc):
    path = "/sessions" if method == "GET" else f"/sessions/{sid}/rename"
    r = await call(daemon, method, path, {"name": "x"} if method == "POST" else None, **headers)
    assert r.status_code == status, f"{desc}: {r.text}"


def test_a_name_given_is_accepted_as_a_host():
    assert hosts("100.64.0.1", 7428, ["Home"]) == {"100.64.0.1:7428", "home:7428"}
    assert hosts("::1", 7428) == {"[::1]:7428", "localhost:7428"}


@pytest.mark.parametrize("address, ok, desc", [
    ("127.0.0.1", True, "the loopback, the default"),
    ("100.64.0.1", True, "a VPN interface's address"),
    ("0.0.0.0", False, "never every interface"),
    ("::", False, "nor every IPv6 one"),
    ("home", False, "an address, not a name"),
])
def test_the_address_to_bind(address, ok, desc):
    if ok:
        assert checked_address(address) == address, desc
    else:
        with pytest.raises(ValueError):
            checked_address(address)


@pytest.mark.parametrize("address, taken, error, said, desc", [
    ("127.0.0.1", False, None, None, "a free port: bound"),
    ("127.0.0.1", True, PortTaken, "port {port} is taken by another process (another user's wheelhouse?): set PORT= "
     "in ~/.config/claude-wheelhouse/.env", "another process's: said as the app says it"),
    ("192.0.2.1", False, ValueError, "can't listen on 192.0.2.1 port {port}: Cannot assign requested address",
     "an address not this machine's: another failure, in a line"),
])
def test_the_socket_the_daemon_listens_on(address, taken, error, said, desc):
    with socket.socket() as other:
        other.bind(("127.0.0.1", 0))
        port = other.getsockname()[1]
        if taken:
            other.listen()
        else:
            other.close()
        if error is None:
            with listener(address, port) as sock:
                assert sock.getsockname() == (address, port), desc
            return
        with pytest.raises(error) as e:
            listener(address, port)
        assert str(e.value) == said.format(port=port), desc


def test_two_daemons_starting_together_on_one_port_the_second_is_told_it_is_taken():
    """Each listens as it binds: two SO_REUSEADDR sockets can both bind a port neither listens on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with listener("127.0.0.1", port), pytest.raises(PortTaken, match=f"port {port} is taken"):
        listener("127.0.0.1", port)


def test_main_refuses_every_interface_before_anything_else(monkeypatch):
    monkeypatch.setattr(daemon_mod, "Store", lambda: pytest.fail("opened the store"))
    with pytest.raises(ValueError, match="every interface"):
        daemon_mod.main("0.0.0.0")



def test_main_says_a_start_pipe_that_isnt_open_in_a_line(monkeypatch):
    monkeypatch.setattr(daemon_mod, "Store", lambda: pytest.fail("opened the store"))
    with pytest.raises(ValueError, match="--start-pipe 987654: Bad file descriptor"):
        daemon_mod.main(start_pipe=987654)

def test_one_daemon_per_user(tmp_path):
    first = take_lock(tmp_path)
    with pytest.raises(AlreadyRunning, match=f"pid {os.getpid()}"):
        take_lock(tmp_path)
    first.close()
    take_lock(tmp_path).close()


def received(body: str) -> list[dict]:
    return [json.loads(line[len("data: "):]) for line in body.splitlines() if line.startswith("data: ")]


async def events(daemon, last=None):
    """What a client gets from /events, connecting with Last-Event-ID last: the feed is
    closed once it's connected, so the stream ends."""
    async with client(daemon) as c:
        async with anyio.create_task_group() as tg:
            got = {}

            async def get():
                headers = {} if last is None else {"last-event-id": str(last)}
                got["r"] = await c.get("/events", headers=headers)
            tg.start_soon(get)
            await anyio.sleep(0.05)
            daemon.feed.close()
    return received(got["r"].text)


@pytest.mark.parametrize("ring, published, last, kinds, desc", [
    (8, 3, 101, ["items", "thread"], "resumed after the last seen"),
    (8, 3, 103, [], "nothing missed"),
    (2, 5, 101, ["refetch"], "the ring no longer holds it: refetch"),
    (8, 3, None, ["refetch"], "a fresh connection fetches everything"),
    (8, 3, 999, ["refetch"], "an id this daemon never gave (an earlier one's): refetch"),
])
async def test_resume_and_overflow(daemon, ring, published, last, kinds, desc):
    daemon.feed = Feed(ring=ring, start=100)
    for kind in ["sessions", "items", "thread", "conversation", "stats"][:published]:
        daemon.feed.publish(kind)
    got = await events(daemon, last)
    assert [e["kind"] for e in got] == kinds, desc
    assert all(e["kind"] != "refetch" or e["seq"] == 100 + published for e in got), f"{desc}: at the latest seq"


async def test_an_event_published_while_connected_arrives(daemon, sid):
    async with client(daemon) as c:
        async with anyio.create_task_group() as tg:
            got = {}

            async def get():
                got["r"] = await c.get("/events", headers={"last-event-id": "100"})
            tg.start_soon(get)
            await anyio.sleep(0.05)
            daemon.feed.publish("items", sid, "T1")
            await anyio.sleep(0.05)
            daemon.feed.close()
    assert received(got["r"].text) == [{"seq": 101, "kind": "items", "sid": sid, "ref": "T1"}]


def kinds(daemon):
    return [(e["kind"], e.get("sid")) for e in daemon.feed.ring]


@pytest.mark.parametrize("write, published, desc", [
    (lambda s, sid: None, [], "nothing written, nothing said"),
    (lambda s, sid: s.heartbeat(sid), [], "a host's heartbeat moves nothing a client sees"),
    (lambda s, sid: s.set_activity(sid, "thinking"), ["sessions"], "a session's row"),
    (lambda s, sid: s.rename(sid, "x"), ["sessions", "items", "conversation"],
     "its name, which its items and its conversation show"),
    (lambda s, sid: s.set_parked(sid, True), ["sessions", "items"], "parked, which leaves its items out of the inbox"),
    (lambda s, sid: s.post_item(sid, "task", "from an MCP server"), ["items", "thread"], "an item posted"),
    (lambda s, sid: s.set_answer(sid, "T1", "yes"), ["items", "thread"], "an item's answer, updated_at as it was"),
    (lambda s, sid: s.dismiss(sid, "T1"), ["items", "thread"], "an item dismissed, updated_at as it was"),
    (lambda s, sid: s.queue(sid, "later"), ["sessions", "items", "thread", "conversation"],
     "a message queued, which its session's row counts"),
    (lambda s, sid: s.confirm(s.claim(sid)), ["items", "thread", "conversation"], "a message delivered"),
])
async def test_a_write_by_another_connection_is_published_at_the_next_tick(daemon, store, sid, db_file, monkeypatch,
                                                                           write, published, desc):
    monkeypatch.setattr(liveness, "status", lambda s, **kw: "live")
    store.post_item(sid, "task", "do it")
    store.send(sid, "hello")
    store.heartbeat(sid)
    daemon.tick()
    daemon.feed.ring.clear()
    write(Store(db_file), sid)
    daemon.tick()
    assert kinds(daemon) == [(kind, None) for kind in published], desc


async def test_a_heartbeat_every_second_publishes_nothing(daemon, store, sid, db_file, monkeypatch):
    monkeypatch.setattr(liveness, "status", lambda s, **kw: "live")
    host = Store(db_file)
    host.heartbeat(sid)
    daemon.tick()
    daemon.feed.ring.clear()
    for _ in range(3):
        host.heartbeat(sid)
        daemon.tick()
    assert kinds(daemon) == []


async def test_a_command_publishes_what_it_moved_at_once(daemon, sid):
    daemon.tick()
    daemon.feed.ring.clear()
    await call(daemon, "POST", f"/sessions/{sid}/rename", {"name": "x"})
    assert kinds(daemon) == [("sessions", None), ("items", None), ("conversation", None)]


@pytest.mark.parametrize("before, write, published, desc", [
    (lambda s, sid: s.update_item(sid, "T1", title="first"), lambda s, sid: s.update_item(sid, "T1", title="second"),
     ["items", "thread"], "an item edited twice in the same second, a tick between"),
    (lambda s, sid: (s.post_item(sid, "task", "two"), s.dismiss(sid, "T1")),
     lambda s, sid: (s.dismiss(sid, "T2"), s.dismiss(sid, "T1", False)),
     ["items", "thread"], "one item dismissed and another brought back, as many dismissed"),
    (lambda s, sid: s.queue(sid, "first"), lambda s, sid: (s.unqueue(s.drafts(sid)[-1]["id"]), s.queue(sid, "second")),
     ["items", "thread", "conversation"], "a draft taken back and queued again, under the same id"),
])
async def test_a_change_that_leaves_the_counts_as_they_were_is_published(daemon, store, sid, db_file, monkeypatch,
                                                                         before, write, published, desc):
    monkeypatch.setattr(store_mod, "now", lambda: "2026-01-01T00:00:00+00:00")   # all in the same second
    store.post_item(sid, "task", "do it")
    before(store, sid)
    daemon.tick()
    daemon.feed.ring.clear()
    write(Store(db_file), sid)
    daemon.tick()
    assert kinds(daemon) == [(kind, None) for kind in published], desc


async def test_parking_a_session_by_another_connection_takes_its_items_out_of_the_inbox(daemon, store, sid, db_file):
    store.post_item(sid, "task", "do it")
    daemon.tick()
    assert [i["ref"] for i in (await call(daemon, "GET", "/items")).json()] == ["T1"]
    daemon.feed.ring.clear()
    Store(db_file).set_parked(sid, True)
    daemon.tick()
    assert ("items", None) in kinds(daemon)
    assert (await call(daemon, "GET", "/items")).json() == []


@pytest.mark.parametrize("method, path, body, seen, desc", [
    ("POST", "/sessions/{sid}/rename", {"name": "renamed"}, lambda s: s["name"] == "renamed", "rename"),
    ("POST", "/sessions", {"cwd": "{tmp}", "name": "fresh"}, lambda s: s["name"] == "fresh", "a new session"),
    ("POST", "/sessions/{sid}/relaunch", None, lambda s: s["name"] == "demo" and not s["parked"],
     "Relaunch of a dead one unparks it"),
])
async def test_a_client_refetching_on_a_commands_event_sees_what_it_did(daemon, store, sid, tmp_path, method,
                                                                         path, body, seen, desc):
    store.set_parked(sid, True)
    daemon.tick()
    daemon.feed.ring.clear()
    await call(daemon, method, fill(path, sid, tmp_path), fill(body, sid, tmp_path))
    assert ("sessions", None) in kinds(daemon), desc
    assert any(seen(s) for s in (await call(daemon, "GET", "/sessions")).json()), desc


async def test_a_write_between_the_refresh_and_the_version_read_is_published_next_tick(daemon, sid, db_file,
                                                                                         monkeypatch):
    daemon.tick()
    refresh = daemon.hub.refresh

    def refresh_then_written():
        refresh()
        Store(db_file).rename(sid, "late")
    monkeypatch.setattr(daemon.hub, "refresh", refresh_then_written)
    daemon.tick()
    monkeypatch.setattr(daemon.hub, "refresh", refresh)
    daemon.feed.ring.clear()
    daemon.tick()
    assert ("sessions", None) in kinds(daemon), "the write after the refresh is published at the next tick"
    assert [s["name"] for s in (await call(daemon, "GET", "/sessions")).json()] == ["late"]


async def test_a_status_changing_with_no_write_is_published(daemon, sid, monkeypatch):
    daemon.tick()
    daemon.feed.ring.clear()
    monkeypatch.setattr(liveness, "status", lambda s, **kw: "live")
    daemon.tick()
    assert kinds(daemon) == [("sessions", sid)]


@pytest.mark.parametrize("event, published, desc", [
    (Event("notice", "relaunched demo", "information"),
     [{"kind": "notice", "text": "relaunched demo", "severity": "information"}], "a notice of the hub's own"),
    (Event("landed", sid="s1"), [{"kind": "conversation", "sid": "s1"}, {"kind": "stats", "sid": "s1"}],
     "a transcript read that brought something"),
])
def test_what_the_hub_says_by_itself_goes_to_every_client(daemon, event, published, desc):
    daemon.hub.emit(event)
    assert [{k: v for k, v in e.items() if k != "seq"} for e in daemon.feed.ring] == published, desc


@pytest.mark.parametrize("error", [sqlite3.OperationalError("database is locked"), SessionGone("s1")])
async def test_a_tick_that_fails_is_said_and_the_next_goes_on(daemon, monkeypatch, capsys, error):
    calls = []

    def refresh():
        calls.append(1)
        if len(calls) == 1:
            raise error
    monkeypatch.setattr(daemon.hub, "refresh", refresh)
    monkeypatch.setattr(daemon_mod, "TICK", 0.01)
    task = asyncio.create_task(daemon.ticking())
    await asyncio.sleep(0.1)
    task.cancel()
    assert len(calls) > 1, "ticking carries on"
    assert "a tick failed" in capsys.readouterr().err


async def test_a_slow_disk_never_holds_up_the_sessions(daemon, tmp_path, monkeypatch):
    scandir = daemon_mod.os.scandir

    def slow(path):
        time.sleep(0.5)   # a mount gone slow
        return scandir(path)
    monkeypatch.setattr(daemon_mod.os, "scandir", slow)
    daemon.tick()
    async with client(daemon) as c:
        listing = asyncio.create_task(c.get("/fs", params={"path": str(tmp_path)}))
        await asyncio.sleep(0.05)
        started = time.monotonic()
        assert (await c.get("/sessions")).status_code == 200
        assert time.monotonic() - started < 0.25, "/sessions answered while the listing waits"
        assert not listing.done()
        assert (await listing).status_code == 200


async def test_a_slow_transcript_never_holds_up_the_hub(daemon, sid, monkeypatch):
    read = daemon_mod.Hub.conversation

    def slow(hub, sid):
        time.sleep(0.5)   # a mount gone slow
        return read(hub, sid)
    monkeypatch.setattr(daemon_mod.Hub, "conversation", slow)
    async with client(daemon) as c:
        reading = asyncio.create_task(c.get(f"/sessions/{sid}/conversation"))
        await asyncio.sleep(0.05)
        started = time.monotonic()
        r = await c.post(f"/sessions/{sid}/rename", json={"name": "x"}, headers={"Idempotency-Key": "k1"})
        assert r.status_code == 200 and time.monotonic() - started < 0.25, "a command answered while it reads"
        assert not reading.done()
        assert (await reading).status_code == 200


async def test_a_signal_to_stop_ends_the_streams_first(daemon, monkeypatch):
    monkeypatch.setattr(AppStatus, "should_exit", False)   # sse-starlette's, for the whole process: put back
    daemon.loop = asyncio.get_running_loop()
    server = daemon_mod.Server(daemon, daemon_mod.uvicorn.Config(daemon.app))
    server.handle_exit(signal.SIGTERM, None)
    await asyncio.sleep(0)
    assert daemon.feed.closed and server.should_exit


@pytest.mark.parametrize("read_for, end, status, at_least, under, desc", [
    (0.3, False, 200, 0, 2.0, "a read under way: waited for, then read again for itself"),
    (0.3, True, 404, 0, 0.7, "a read under way of a session that ends meanwhile: gone, not its last blocks"),
    (0.6, False, 503, 0.4, 1.0, "a read under way past READ_WAIT: try again shortly, not at once"),
])
async def test_a_conversation_waits_on_a_read_under_way_for_a_while(daemon, store, sid, monkeypatch, read_for,
                                                                     end, status, at_least, under, desc):
    read = daemon_mod.Hub.conversation
    assert (await call(daemon, "GET", f"/sessions/{sid}/conversation")).status_code == 200

    def slow(hub, sid):
        time.sleep(read_for)   # a transcript on a mount gone slow
        return read(hub, sid)
    monkeypatch.setattr(daemon_mod.Hub, "conversation", slow)
    monkeypatch.setattr(daemon_mod, "READ_WAIT", 0.4)
    async with client(daemon) as c:
        reading = asyncio.create_task(c.get(f"/sessions/{sid}/conversation"))
        await asyncio.sleep(0.05)
        if end:
            store.end(sid)
        started = time.monotonic()
        r = await c.get(f"/sessions/{sid}/conversation")
        assert r.status_code == status and at_least <= time.monotonic() - started < under, desc
        if status == 503:
            assert r.headers["retry-after"] == "1" and r.json()["error"], desc
        await reading


async def test_two_refetches_overlapping_a_transcript_change_both_get_it(daemon, sid, tmp_path, monkeypatch):
    path = tmp_path / "t.jsonl"

    def say(text):
        with path.open("a") as f:
            f.write(json.dumps({"type": "user", "timestamp": "2026-10-07T21:30:00.000Z",
                                "message": {"role": "user", "content": text}}) + "\n")
    say("before")
    monkeypatch.setattr(transcript.Follower, "find", lambda self: path)
    load = transcript.Follower.load
    assert "before" in (await call(daemon, "GET", f"/sessions/{sid}/conversation")).text
    say("the final reply")   # the change a conversation event says: both clients refetch on it

    def slow(self, path):
        time.sleep(0.3)
        return load(self, path)
    monkeypatch.setattr(transcript.Follower, "load", slow)
    async with client(daemon) as c:
        first = asyncio.create_task(c.get(f"/sessions/{sid}/conversation"))
        await asyncio.sleep(0.05)
        second = await c.get(f"/sessions/{sid}/conversation")
        for r in (await first, second):
            assert r.status_code == 200 and "the final reply" in r.text


async def test_a_client_gone_before_its_body_is_let_go_quietly(daemon, store, sid, capsys):
    scope = {"type": "http", "method": "POST", "path": f"/sessions/{sid}/rename", "raw_path": b"",
             "query_string": b"", "root_path": "", "scheme": "http", "http_version": "1.1",
             "server": ("127.0.0.1", 7428), "client": ("127.0.0.1", 50000),
             "headers": [(b"host", b"127.0.0.1:7428"), (b"idempotency-key", b"k1"),
                         (b"authorization", f"Bearer {TOKEN}".encode())]}
    sent = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
    await daemon.app(scope, receive, send)
    assert sent[0]["status"] == 499 and capsys.readouterr().err == ""
    assert store.session(sid)["name"] == "demo" and store.answered("k1") is None


async def test_a_session_gone_is_no_longer_followed(daemon, store, sid, db_file):
    other = store.create_session("/", name="other")
    daemon.tick()
    for s in (sid, other):
        assert (await call(daemon, "GET", f"/sessions/{s}/conversation")).status_code == 200
    Store(db_file).end(other)
    daemon.tick()
    assert (set(daemon.hub.followers), set(daemon.reading)) == ({sid}, {sid})


async def test_a_session_created_since_the_last_refresh_is_still_followed(daemon, store, sid):
    daemon.tick()
    late = store.create_session("/", name="late")
    assert (await call(daemon, "GET", f"/sessions/{late}/conversation")).status_code == 200
    daemon.forget_gone()
    assert late in daemon.hub.followers and late in daemon.reading


@pytest.mark.parametrize("path, body, desc", [
    ("/sessions/{sid}/model", {"model": "claude-opus-5-5"}, "a model change"),
    ("/sessions/{sid}/effort", {"effort": "max"}, "an effort change"),
    ("/sessions", {"cwd": "/", "model": "claude-opus-5-5", "effort": "max"}, "a new session on a model"),
])
@pytest.mark.parametrize("headers, status, guard", [
    ({"idempotency_key": ""}, 400, "Idempotency-Key"),
    ({"host": "evil.example:7428"}, 421, "isn't reached by that name"),
    ({"origin": "http://evil.example"}, 403, "another site's page"),
])
async def test_the_model_routes_keep_the_guards(daemon, store, tmp_path, path, body, desc, headers, status, guard):
    sid = store.create_session(str(tmp_path), runner="sdk", model="claude-sonnet-5-5", effort="high")
    r = await call(daemon, "POST", path.format(sid=sid), body, **headers)
    assert r.status_code == status and guard in r.json()["error"], f"{desc}: {r.text}"
    s = store.session(sid)
    assert (s["model"], s["effort"], len(store.sessions())) == ("claude-sonnet-5-5", "high", 1), f"{desc}: nothing done"


async def test_a_hosted_sessions_model_and_effort_are_set_and_served(daemon, store, tmp_path):
    sid = store.create_session(str(tmp_path), name="h", runner="sdk", model="claude-sonnet-5-5", effort="high")
    for path, body in ((f"/sessions/{sid}/model", {"model": "claude-opus-5-5"}),
                       (f"/sessions/{sid}/effort", {"effort": "max"})):
        r = await call(daemon, "POST", path, body)
        assert r.status_code == 200 and r.json()["ok"], r.text
    row = next(s for s in (await call(daemon, "GET", "/sessions")).json() if s["id"] == sid)
    assert (row["model"], row["effort"]) == ("claude-opus-5-5", "max")
    assert {"applied_model", "applied_effort", "running_model", "running_effort"} <= row.keys()


@pytest.mark.parametrize("body, command, said, desc", [
    ({"host": [4242, 1], "compact": True}, "compact", "compacting first", "compact first: the host is asked to Compact"),
    ({"host": [4242, 1]}, None, "stopping its host", "without: its host is stopped now"),
])
async def test_relaunch_can_compact_first(daemon, store, sid, monkeypatch, body, command, said, desc):
    store.set_runner(sid, "sdk")
    store.db.execute("UPDATE sessions SET claude_pid = 4242, claude_start = 1, boot_id = 'b', context_tokens = 200000")
    monkeypatch.setattr(liveness, "is_alive", lambda *a: True)
    monkeypatch.setattr(liveness, "status", lambda s, **kw: "live")
    monkeypatch.setattr(hub_mod.os, "kill", lambda *a: None)
    daemon.tick()
    shown = await call(daemon, "GET", "/sessions")
    assert shown.json()[0]["compact_first"] is True, f"{desc}: the client is told to offer it"
    r = await call(daemon, "POST", f"/sessions/{sid}/relaunch", body)
    assert r.status_code == 200 and any(said in t for t in told(r)), f"{desc}: {r.text}"
    assert store.session(sid)["host_command"] == command, desc


@pytest.mark.parametrize("address, peer, status, desc", [
    ("127.0.0.1", "127.0.0.1", 200, "from this machine: the TUI's New session, with its tab box ticked"),
    ("127.0.0.1", "100.64.0.7", 403, "from another machine: a tab would open on a screen nobody there sees"),
    ("100.64.0.1", "100.64.0.1", 200, "a daemon on a VPN address, joined by an app on this machine at that address"),
    ("100.64.0.1", "100.64.0.7", 403, "a daemon on a VPN address, from another machine on the VPN"),
])
async def test_a_tab_session_only_from_this_machine(daemon, store, tmp_path, address, peer, status, desc):
    daemon.allowed = hosts(address, 7428)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=daemon.app, client=(peer, 50000)),
                                  base_url=f"http://{address}:7428", headers={"Authorization": f"Bearer {TOKEN}"}) as c:
        r = await c.post("/sessions", json={"cwd": str(tmp_path), "runner": "tab"},
                         headers={"Idempotency-Key": str(uuid.uuid4())})
    assert r.status_code == status, desc
    assert [s["runner"] for s in store.sessions()] == (["tab"] if status == 200 else []), desc


# the token (phase 2c): every route, the stream's too

def every_route(daemon):
    """(method, path) for each route the daemon serves, its parameters filled in."""
    params = {"sid": "nosuch", "ref": "T1", "action": "rename", "id": "1", "id:int": "1"}
    out = []
    for route in daemon.app.routes:
        path = route.path
        for name, value in params.items():
            path = path.replace("{" + name + "}", value)
        out += [(method, path) for method in sorted(route.methods - {"HEAD"})]
    return out


def test_every_route_is_covered_by_the_token_tests(daemon):
    routes = every_route(daemon)
    assert len(routes) == len(set(routes)) == 33 and not [p for _, p in routes if "{" in p], routes
    assert ("GET", "/events") in routes and ("POST", "/shutdown") in routes


@pytest.mark.parametrize("given, desc", [
    (None, "no token"),
    ("Bearer not-the-token", "a wrong one"),
    (f"Bearer {TOKEN}x", "one with a character more"),
    (f"Basic {TOKEN}", "the token under another scheme"),
    ("Bearer ", "an empty one"),
])
async def test_every_route_refuses_a_request_without_the_token(daemon, sid, given, desc):
    headers = {} if given is None else {"Authorization": given}
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=daemon.app), base_url=URL, headers=headers) as c:
        for method, path in every_route(daemon):
            if path == "/hello":   # the one without the token: below
                continue
            r = await c.request(method, path, headers={"Idempotency-Key": str(uuid.uuid4())}, json={})
            assert (r.status_code, r.headers.get("www-authenticate")) == (401, "Bearer"), f"{desc}: {method} {path}"
            assert r.json()["error"].startswith("this daemon needs its token"), f"{desc}: {method} {path}"
    assert store_mod.Store(daemon.store.path).session(sid)["name"] == "demo", f"{desc}: nothing done"


async def test_every_route_takes_the_right_token(daemon, sid):
    """Answered by the route itself, whatever it says (a session that isn't there, a bad
    body): never 401, nor 421. The stream's own test is below."""
    daemon.feed.close()   # the stream ends once it has sent what it has
    async with client(daemon) as c:
        for method, path in every_route(daemon):
            r = await c.request(method, path, headers={"Idempotency-Key": str(uuid.uuid4())}, json={})
            assert r.status_code not in (401, 421), f"{method} {path}: {r.status_code} {r.text}"


@pytest.mark.parametrize("given, status, desc", [
    (None, 401, "the stream without the token"),
    ("Bearer wrong", 401, "with a wrong one"),
    (f"Bearer {TOKEN}", 200, "with the token: its events"),
])
async def test_the_event_stream_needs_the_token(daemon, given, status, desc):
    headers = {} if given is None else {"Authorization": given}
    daemon.feed.close()
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=daemon.app), base_url=URL, headers=headers) as c:
        r = await c.get("/events")
    assert r.status_code == status, desc
    assert (received(r.text) if status == 200 else r.json()["error"][:4]) == (
        [{"seq": 100, "kind": "refetch"}] if status == 200 else "this"), desc


async def test_the_host_is_checked_before_the_token(daemon):
    """D47 first: a page rebound by DNS learns nothing, not even that a token is wanted."""
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=daemon.app), base_url="http://evil.example:7428") as c:
        r = await c.get("/sessions")
    assert r.status_code == 421 and "www-authenticate" not in r.headers


# what 2c adds for the TUI

NEW_ROUTES = [
    ("GET", "/hello", None, lambda store, sid, r: (r.json()["api"], r.json()["ready"], r.json()["pid"])
     == (2, False, os.getpid()), "with the token, which daemon: its API, its code, not ready before its first tick"),
    ("GET", "/inbox", None, lambda store, sid, r: (sorted(i["ref"] for i in r.json()["items"]),
                                                   "body" in r.json()["items"][0], r.json()["tutorial"]["sid"])
     == (["P1", "T1"], False, None), "every item, without its body, and the tutorial's state"),
    ("GET", "/usage", None, lambda store, sid, r: r.json() == {"limits": {}, "read": {}, "failed": False}, "the account's usage"),
    ("POST", "/settings", {"key": "layout.x", "value": "0.5"}, lambda store, sid, r: store.setting("layout.x") == "0.5",
     "a splitter's size kept"),
    ("GET", "/settings", None, lambda store, sid, r: r.json() == {}, "the kept sizes"),
    ("POST", "/settings", {"key": "agents_since", "value": "x"}, lambda store, sid, r: r.status_code == 400,
     "no other setting"),
    ("GET", "/adoptable", None, lambda store, sid, r: isinstance(r.json(), list), "what Adopt offers"),
    ("GET", "/adoptable/{sid}/running", None, lambda store, sid, r: r.json() == {"pid": None}, "whether it runs"),
    ("POST", "/adopt", {"id": "nosuch"}, lambda store, sid, r: r.status_code == 404, "one Adopt doesn't offer"),
    ("POST", "/relaunch-all", {"sids": ["{sid}"]}, lambda store, sid, r: told(r) == ["relaunching 1 session(s)"],
     "Relaunch all, of those the client showed"),
    ("POST", "/relaunch-all", {"sids": "x"}, lambda store, sid, r: r.status_code == 400, "a list of them"),
    ("POST", "/order/items", {"group": "manual", "keys": [["{sid}", "T1"]]},
     lambda store, sid, r: (store.item(sid, "T1")["pos"], store.item(sid, "T1")["pos_group"]) == (0, "manual"),
     "a group of items in the person's order (T114)"),
    ("POST", "/order/items", {"group": "manual", "keys": ["T1"]}, lambda store, sid, r: r.status_code == 400,
     "keys, [session, ref] pairs"),
    ("POST", "/order/sessions", {"group": "manual", "sids": ["{sid}"]},
     lambda store, sid, r: store.session(sid)["pos_group"] == "manual", "sessions in the person's order"),
    ("POST", "/order/sessions", {"group": 3, "sids": []}, lambda store, sid, r: r.status_code == 400, "group, a string"),
    ("GET", "/sessions/{sid}/fork", None, lambda store, sid, r: (r.json()["name"], r.json()["items"], r.json()["tree"]["linked"])
     == ("demo (fork)", [["T1", "task", "todo", "do it"]], False), "what Fork offers: the items that may move"),
    ("POST", "/sessions/{sid}/fork", {"refs": "T1"}, lambda store, sid, r: r.status_code == 400, "refs, a list"),
    ("POST", "/sessions/{sid}/fork", {"refs": [], "branch": 3}, lambda store, sid, r: r.status_code == 400,
     "branch, a name or null"),
    ("GET", "/items/{sid}/{ref}/conversation", None, lambda store, sid, r: r.json() is None,
     "a subagent's conversation: none for a task"),
    ("POST", "/items/{sid}/{ref}/seen", None, lambda store, sid, r: r.json()["ok"] is False,
     "seen: a no-op but for an unseen decision"),
    ("POST", "/messages/1/unqueue", None, lambda store, sid, r: r.json() == {"text": None}, "a draft taken back: gone"),
    ("POST", "/tutorial/offer", {"take": False}, lambda store, sid, r: store.setting("tutorial_offer") == "dismissed",
     "the tutorial offer turned down"),
    ("POST", "/tutorial/seen", {"sid": "{sid}", "step": "open"},
     lambda store, sid, r: json.loads(store.setting("tutorial_seen"))["seen"] == ["open"], "a tutorial step seen"),
    ("POST", "/tutorial/drop-ended", None, lambda store, sid, r: store.setting("tutorial_seen") is None,
     "an ended tutorial's checklist let go as an app starts"),
    ("POST", "/shutdown", None, lambda store, sid, r: not r.json()["ok"], "stop: a test's daemon can't"),
]


@pytest.mark.parametrize("method, path, body, did, desc", NEW_ROUTES)
async def test_each_new_route(daemon, store, sid, tmp_path, method, path, body, did, desc):
    store.post_item(sid, "task", "do it")
    store.post_item(sid, "permission", "Bash: ls")
    if path == "/relaunch-all":
        await daemon.on_hub(daemon.tick)   # the session dead, as the client showed it
    if path == "/tutorial/drop-ended":
        store.set_setting("tutorial_seen", json.dumps({"sid": "ended", "seen": ["open"], "before": []}))
    r = await call(daemon, method, fill(path, sid, tmp_path), fill(body, sid, tmp_path))
    assert r.status_code < 500 and did(store, sid, r), f"{desc}: {r.status_code} {r.text}"


async def test_sessions_carry_their_liveness_and_model_state(daemon, sid):
    await daemon.on_hub(daemon.tick)
    view = (await call(daemon, "GET", "/sessions")).json()[0]
    assert (view["liveness"], view["model_state"]["effort_live"]) == ("dead", False)


@pytest.mark.parametrize("path, body, kind, desc", [
    ("/sessions/nosuch/rename", {"name": "x"}, "SessionGone", "a session gone, as the hub raised it"),
    ("/items/{sid}/T9/close", {"closed": True}, "KeyError", "an item that isn't there"),
])
async def test_an_error_says_its_type(daemon, sid, tmp_path, path, body, kind, desc):
    r = await call(daemon, "POST", fill(path, sid, tmp_path), body)
    assert (r.status_code, r.json()["type"]) == (404, kind), desc


@pytest.mark.parametrize("given, status, desc", [
    (None, 401, "without the token: refused, nothing about the daemon"),
    ("Bearer wrong", 401, "a wrong token: the same"),
    (f"Bearer {TOKEN}", 200, "the token: which daemon"),
])
async def test_hello_needs_the_token_like_every_route(daemon, given, status, desc):
    headers = {} if given is None else {"Authorization": given}
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=daemon.app), base_url=URL, headers=headers) as c:
        r = await c.get("/hello")
    assert r.status_code == status and ("api" in r.json()) == (status == 200), desc


def test_the_lock_is_the_users_alone_and_names_an_address_only_once_listening(tmp_path):
    lock = take_lock(tmp_path)
    try:
        assert oct((tmp_path / daemon_mod.LOCK).stat().st_mode)[-3:] == "600"
        assert (tmp_path / daemon_mod.LOCK).read_text() == f"pid {os.getpid()}\n", "no address before it listens"
        daemon_mod.announce(lock, "http://127.0.0.1:7428")
        assert (tmp_path / daemon_mod.LOCK).read_text() == f"pid {os.getpid()} at http://127.0.0.1:7428\n"
    finally:
        lock.close()


def local_ip() -> str | None:
    """One of this machine's own addresses off the loopback, if it has one: the one a socket
    would send from to an outside address (UDP: nothing is sent)."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))
            ip = s.getsockname()[0]
        except OSError:
            return None
    return None if ip.startswith("127.") or ip == "0.0.0.0" else ip


@pytest.mark.parametrize("host, own, desc", [
    ("127.0.0.1", True, "the loopback"),
    ("::1", True, "IPv6's loopback"),
    ("192.0.2.1", False, "an address of another machine's (TEST-NET-1)"),
    ("nonsense", False, "not an address"),
    ("own", True, "an address of this machine's off the loopback"),
    ("0.0.0.0", False, "unspecified, which binds"),
    ("::", False, "IPv6's unspecified"),
    ("255.255.255.255", False, "the broadcast address, which binds"),
    ("subnet broadcast", False, "a subnet's broadcast address, which binds"),
    ("224.0.0.1", False, "multicast, which binds"),
    ("ff02::1", False, "IPv6's multicast"),
])
def test_own_address(host, own, desc):
    if host == "own":
        host = local_ip()
        if host is None:
            pytest.skip("this machine has no address off the loopback")
    if host == "subnet broadcast":
        host = next((str(ip) for ip in daemon_mod.broadcasts() if not ip.is_unspecified), None)
        if host is None:
            pytest.skip("this machine has no subnet broadcast address")
    assert daemon_mod.own_address(host) is own, desc


@pytest.mark.parametrize("ipv4, ipv6, desc", [
    ("1", "0", "IPv4's set"),
    ("0", "1", "IPv6's set"),
])
def test_where_any_address_binds_only_the_loopback_is_own(tmp_path, monkeypatch, ipv4, ipv6, desc):
    host = local_ip()
    if host is None:
        pytest.skip("this machine has no address off the loopback")
    files = (tmp_path / "ipv4", tmp_path / "ipv6")
    files[0].write_text(ipv4 + "\n")
    files[1].write_text(ipv6 + "\n")
    monkeypatch.setattr(daemon_mod, "NONLOCAL_BIND", files)
    assert (daemon_mod.own_address(host), daemon_mod.own_address("127.0.0.1")) == (False, True), desc


@pytest.mark.parametrize("method, path, token, status, desc", [
    ("GET", "/nosuch", None, 401, "a path it doesn't serve, without the token"),
    ("POST", "/nosuch/deeper", None, 401, "nor a command there"),
    ("GET", "/dispatch", None, 401, "a method a route doesn't take, without the token"),
    ("GET", "/nosuch", TOKEN, 404, "with the token: not there"),
    ("GET", "/dispatch", TOKEN, 405, "with the token: not that method"),
])
async def test_what_isnt_served_is_refused_without_the_token_first(daemon, method, path, token, status, desc):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=daemon.app), base_url=URL, headers=headers) as c:
        r = await c.request(method, path)
    assert r.status_code == status, desc
    assert status != 401 or r.headers.get("www-authenticate") == "Bearer", desc


async def test_a_conversation_asked_for_before_its_transcript_exists_is_announced_once_it_does(
        daemon, store, sid, tmp_path, monkeypatch):
    """A new session's conversation opened at once: its follower has no path yet."""
    monkeypatch.setattr(transcript, "PROJECTS", tmp_path / "projects")
    assert (await call(daemon, "GET", f"/sessions/{sid}/conversation")).status_code == 200
    daemon.publish_grown()
    assert ("conversation", sid) not in kinds(daemon), "no transcript yet: nothing to say"
    folder = tmp_path / "projects" / transcript.project_folder(str(tmp_path))
    folder.mkdir(parents=True)
    (folder / f"{sid}.jsonl").write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
    daemon.publish_grown()
    assert kinds(daemon).count(("conversation", sid)) == 1, kinds(daemon)


async def test_a_conversation_one_client_fetched_before_the_tick_is_still_announced_to_another(
        store, sid, tmp_path, monkeypatch):
    """Two apps show one session: A fetches the grown transcript before the tick, and B still
    hears of it, from the tick and from the context read that lands after it (as in
    production: the read runs on the pool and is said on the hub's thread)."""
    monkeypatch.setattr(transcript, "PROJECTS", tmp_path / "projects")
    folder = tmp_path / "projects" / transcript.project_folder(str(tmp_path))
    folder.mkdir(parents=True)
    file = folder / f"{sid}.jsonl"
    file.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
    d = Daemon(store, hosts("127.0.0.1", 7428), TOKEN)   # not inline: the pool and the hub's thread
    d.feed = Feed(start=100)
    try:
        assert (await call(d, "GET", f"/sessions/{sid}/conversation")).status_code == 200
        d.worker.submit(d.publish_grown).result()   # the transcript as it was, checked
        with file.open("a") as f:
            f.write(json.dumps({"type": "user", "message": {"content": "more"}}) + "\n")
        assert (await call(d, "GET", f"/sessions/{sid}/conversation")).status_code == 200   # A
        start = len(d.feed.ring)
        d.worker.submit(d.tick).result()
        d.pool.shutdown(wait=True)   # the context read done, and its "landed" posted to the hub's thread
        d.worker.submit(lambda: None).result()   # and heard there
        said = kinds(d)[start:]
        assert ("stats", sid) in said, said
        assert said.count(("conversation", sid)) == 2, f"B hears it from the tick and the read: {said}"
    finally:
        d.pool.shutdown()
        d.worker.shutdown()
        d.watch.close()


async def test_only_the_first_shutdown_stops_the_daemon(daemon):
    """Two apps found it on older code: only the one that stopped it says it restarted it."""
    stopped = []
    daemon.stop = lambda: stopped.append(1)
    first = await call(daemon, "POST", "/shutdown")
    second = await call(daemon, "POST", "/shutdown")
    await asyncio.sleep(0)
    assert (first.json()["ok"], told(first)) == (True, ["the daemon is stopping"])
    assert (second.json()["ok"], told(second)) == (False, ["the daemon is stopping already"])
    assert stopped == [1]


@pytest.mark.parametrize("body, peer, status, desc", [
    ({"cwd": "/", "runner": "sdk"}, "127.0.0.1", 403, "a host, asked for by name"),
    ({"cwd": "/"}, "127.0.0.1", 403, "nor by default"),
    ({"cwd": "/", "runner": "sdk"}, "100.64.0.7", 403, "nor from another machine"),
    ({"cwd": "/", "runner": "tab"}, "100.64.0.7", 403, "nor a tab from another machine, on a screen nobody there sees"),
    ({"cwd": "/", "runner": "tab"}, "127.0.0.1", 200, "a tab from this machine: the app's New session"),
])
async def test_sdk_mode_off_refuses_hosted_sessions(store, monkeypatch, body, peer, status, desc):
    """Out of SDK mode (features.py) the daemon creates no host's session, so a remote
    client can't create a session at all: a host is SDK mode's, and a tab is this machine's."""
    opened = []
    monkeypatch.setattr(launch, "open_session", lambda store, sid, may_tab=True: opened.append(sid))
    d = Daemon(store, hosts("127.0.0.1", 7428), TOKEN, inline=True, sdk=False)
    try:
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=d.app, client=(peer, 50000)),
                                      base_url="http://127.0.0.1:7428",
                                      headers={"Authorization": f"Bearer {TOKEN}"}) as c:
            r = await c.post("/sessions", json=body, headers={"Idempotency-Key": str(uuid.uuid4())})
    finally:
        d.pool.shutdown()
        d.worker.shutdown()
        d.watch.close()
    assert r.status_code == status, f"{desc}: {r.text}"
    if status == 403:
        assert "SDK mode is off" in r.json()["error"], desc
    assert [s["runner"] for s in store.sessions()] == (["tab"] if status == 200 else []), desc


REMOTE = "100.64.0.7"
HERE = "127.0.0.1"
GONE_HOST = {"host": [999999, 1]}   # the host the client was shown, gone meanwhile
OFF = "SDK mode is off: this wheelhouse doesn't start sessions remotely"
HELD = "opens in a tab: open it from the daemon's machine"
GONE = "host has stopped: Relaunch it from the daemon's machine"


@pytest.mark.parametrize("sdk, env, path, body, how, peer, status, said, opened_want, desc", [
    (False, "tab", "/sessions/{sid}/relaunch", {}, None, REMOTE, 403, OFF, [], "Relaunch of a dead session: a tab"),
    (False, "tab", "/relaunch-all", {"sids": ["{sid}"]}, None, REMOTE, 403, OFF, [], "Relaunch all: tabs"),
    (False, "tab", "/adopt", {"id": "adoptee"}, None, REMOTE, 403, OFF, [], "Adopt: a tab"),
    (False, "tab", "/tutorial/offer", {"take": True}, None, REMOTE, 403, OFF, [], "the tutorial: a tab"),
    (False, "tab", "/sessions/{sid}/shell", {}, None, REMOTE, 403, OFF, [], "Shell: the host hands over to a tab"),
    (True, "tab", "/relaunch-all", {"sids": ["{sid}"]}, "stale", REMOTE, 200, "relaunching 0", [],
     "live at the last tick, gone since (review 75): not Available as shown, so left, and no tab"),
    (True, "sdk", "/sessions/{sid}/relaunch", {}, "tut", REMOTE, 200, "tut " + HELD, [],
     "SDK mode on: the tutorial is a tab all the same"),
    (True, "sdk", "/tutorial/offer", {"take": True}, None, REMOTE, 403, "it would open in a tab", [],
     "SDK mode on: the tutorial still"),
    (True, "tab", "/sessions/{sid}/relaunch", {}, None, REMOTE, 200, HELD, [], "SDK mode on, new sessions in tabs"),
    (True, "tab", "/adopt", {"id": "adoptee"}, None, REMOTE, 200, "adopt it from the daemon's machine", [],
     "Adopt, the same"),
    (True, "sdk", "/sessions/{sid}/relaunch", {}, None, REMOTE, 200, "relaunching", ["host"], "back by a host"),
    (True, "sdk", "/adopt", {"id": "adoptee"}, None, REMOTE, 200, "adopting", ["host"], "adopted by a host"),
    (True, "sdk", "/relaunch-all", {"sids": ["{sid}", "{tut}"]}, None, REMOTE, 200, "tut " + HELD, ["host"],
     "the rest relaunched, the tutorial held back"),
    (False, "tab", "/tutorial/offer", {"take": False}, None, REMOTE, 200, "the wheelhouse stopped", [],
     "the offer dismissed: nothing opens"),
    (False, "tab", "/sessions/{sid}/relaunch", GONE_HOST, "host", REMOTE, 200, GONE, [],
     "a host gone meanwhile would come back as a tab"),
    (False, "tab", "/sessions/{sid}/relaunch", {**GONE_HOST, "compact": True}, "host", REMOTE, 200, GONE, [],
     "compacting first, the same"),
    (True, "tab", "/sessions/{sid}/relaunch", GONE_HOST, "host", REMOTE, 200, GONE, [], "SDK mode on, the same"),
    (False, "tab", "/sessions/{sid}/relaunch", {}, None, HERE, 200, "relaunching", ["tab"], "this machine: a tab"),
    (False, "tab", "/relaunch-all", {"sids": ["{sid}"]}, None, HERE, 200, "relaunching 1", ["tab"], "Relaunch all here"),
    (False, "tab", "/adopt", {"id": "adoptee"}, None, HERE, 200, "adopting", ["tab"], "Adopt here"),
    (False, "tab", "/sessions/{sid}/relaunch", GONE_HOST, "host", HERE, 200, "relaunching", ["tab"],
     "a host gone meanwhile, here: back as a tab"),
])
async def test_another_machine_opens_no_tab(store, tmp_path, monkeypatch, sdk, env, path, body, how, peer, status,
                                            said, opened_want, desc):
    """A tab opens on this machine's screen, where nobody on another machine sees it: so no
    request from another machine opens one. What opens is recorded at the bottom, open_tab
    and open_host, so a path that misses the rule shows."""
    from claude_wheelhouse import adopt, tutorial
    monkeypatch.setenv("WHEELHOUSE_RUNNER", env)
    opened = []
    monkeypatch.setattr(launch, "open_tab", lambda store, sid, notice=True: opened.append("tab"))
    monkeypatch.setattr(launch, "open_host", lambda store, sid: opened.append("host"))
    monkeypatch.setattr(tutorial, "start", lambda store: opened.append("tutorial"))
    monkeypatch.setattr(hub_mod.Hub, "host_command", lambda self, sid, what: opened.append(what) or hub_mod.Done())
    monkeypatch.setattr(liveness, "running_pid", lambda *a, **k: None)
    sid = store.create_session(str(tmp_path), name="demo", runner="sdk" if how == "host" else "tab")
    tut_dir = tutorial.tutorial_dir(store)
    tut_dir.mkdir(exist_ok=True)
    tut = store.create_session(str(tut_dir), name="tut", runner="tab")
    if how == "tut":
        sid = tut
    d = Daemon(store, hosts("127.0.0.1", 7428), TOKEN, inline=True, sdk=sdk)
    monkeypatch.setattr(d.hub, "adoptable", lambda: [adopt.Candidate("adoptee", str(tmp_path), "t", 0.0, None)])
    try:
        if how in ("live", "stale"):
            store.register(sid, 4242, 1, "b")
            monkeypatch.setattr(liveness, "is_alive", lambda *a, **k: True)
        d.tick()
        if how == "stale":   # its tab has /exited since the last tick
            monkeypatch.setattr(liveness, "is_alive", lambda *a, **k: False)
        body = json.loads(json.dumps(body).replace("{sid}", sid).replace("{tut}", tut))
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=d.app, client=(peer, 50000)),
                                      base_url=URL, headers={"Authorization": f"Bearer {TOKEN}"}) as c:
            r = await c.post(path.format(sid=sid), json=body, headers={"Idempotency-Key": str(uuid.uuid4())})
    finally:
        d.pool.shutdown()
        d.worker.shutdown()
        d.watch.close()
    assert r.status_code == status, f"{desc}: {r.text}"
    words = [r.json()["error"]] if status == 403 else told(r)
    assert any(said in t for t in words), f"{desc}: {words}"
    assert opened == opened_want, f"{desc}: {opened}"


async def test_fork_through_the_daemon(daemon, store, sid, tmp_path, claude_home, monkeypatch):
    opened = []
    monkeypatch.setattr(launch, "open_tab", lambda store, sid, notice=True: opened.append((sid, notice)))
    claude_home(sid, str(tmp_path))
    store.post_item(sid, "task", "do it")
    r = await call(daemon, "POST", f"/sessions/{sid}/fork", {"refs": ["T1"], "branch": None})
    [new] = [s["id"] for s in store.sessions() if s["id"] != sid]
    assert r.json()["ok"] and opened == [(new, False)], r.text
    assert store.session(new)["name"] == "demo (fork)" and store.item(new, "T1")["title"] == "do it", r.text


def test_shutdown_takes_away_a_fork_whose_last_part_never_runs(daemon, monkeypatch):
    from claude_wheelhouse import fork
    undone = []
    monkeypatch.setattr(fork, "undo", lambda new, t, branch: undone.append(new))
    daemon.hub.pending_forks["new-sid"] = (None, None)
    daemon.shutdown()
    assert undone == ["new-sid"] and not daemon.hub.pending_forks


@pytest.mark.parametrize("path, body, status, plan, park_compact, desc", [
    ("compact", {"notes": "keep Q3", "then": "park"}, 200, ("keep Q3", "park"), 0, "Compact with instructions, then park"),
    ("compact", {}, 200, (None, None), 0, "Compact as before: asked what to keep, nothing chosen after"),
    ("compact", {"then": "later"}, 400, (None, None), 0, "an unknown then is refused"),
    ("park", {"how": "ask", "compact": True}, 200, (None, None), 1, "Park, compacting first"),
    ("park", {"how": "ask"}, 200, (None, None), 0, "Park without"),
])
async def test_compact_and_park_carry_their_options(daemon, store, sid, monkeypatch, path, body, status, plan,
                                                   park_compact, desc):
    store.set_runner(sid, "sdk")
    monkeypatch.setattr(liveness, "status", lambda s, **kw: "live")
    daemon.tick()
    r = await call(daemon, "POST", f"/sessions/{sid}/{path}", body)
    assert r.status_code == status, f"{desc}: {r.text}"
    assert (store.compact_plan(sid), store.session(sid)["park_compact"]) == (plan, park_compact), desc


async def test_the_compact_question_is_marked_in_the_inbox(daemon, store, sid):
    """The inbox serves no answers: the question suggesting Compact is marked instead."""
    store.set_answer(sid, store.post_item(sid, "question", "context 220k: compact?"), COMPACT_QUESTION)
    store.post_item(sid, "question", "which db?")
    items = (await call(daemon, "GET", "/inbox")).json()["items"]
    assert sorted((i["title"], i["compact_question"]) for i in items) == [("context 220k: compact?", True),
                                                                          ("which db?", False)]


@pytest.mark.parametrize("runner_, live, ok, desc", [
    ("sdk", True, True, "a running host: Compact is left for it"),
    ("sdk", False, False, "a host not running: refused, so no later host compacts on a cold cache"),
    ("tab", True, False, "a tab: the wheelhouse can't compact it"),
])
async def test_compact_needs_a_running_host(daemon, store, sid, monkeypatch, runner_, live, ok, desc):
    store.set_runner(sid, runner_)
    monkeypatch.setattr(liveness, "status", lambda s, **kw: "live" if live else "dead")
    daemon.tick()
    r = await call(daemon, "POST", f"/sessions/{sid}/compact", {"notes": None, "then": "send"})
    assert r.json()["ok"] is ok and (store.session(sid)["host_command"] == "compact") is ok, desc
