"""The daemon, in process: its routes through httpx2's ASGI transport, never a socket."""

import asyncio
import json
import signal
import sqlite3
import time
import uuid

import anyio
import httpx2
import pytest
from sse_starlette.sse import AppStatus

from claude_wheelhouse import daemon as daemon_mod, launch, liveness, store as store_mod, transcript
from claude_wheelhouse.daemon import AlreadyRunning, Daemon, Feed, checked_address, hosts, take_lock
from claude_wheelhouse.hub import Event
from claude_wheelhouse.store import SessionGone, Store

pytestmark = pytest.mark.anyio
URL = "http://127.0.0.1:7428"


@pytest.fixture
def daemon(store, monkeypatch):
    monkeypatch.setattr(launch, "open_session", lambda store, sid: None)
    monkeypatch.setattr(launch, "restore_session", lambda store, sid: None)
    d = Daemon(store, hosts("127.0.0.1", 7428), inline=True)
    d.feed = Feed(start=100)
    yield d
    d.pool.shutdown()
    d.worker.shutdown()
    d.watch.close()


def client(daemon, **headers):
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=daemon.app), base_url=URL, headers=headers)


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
    ("POST", "/sessions/{sid}/restore", None, lambda store, sid, r: r.json()["ok"], "restore a dead session"),
    ("POST", "/sessions/{sid}/relaunch", None, lambda store, sid, r: told(r) == ["relaunching"],
     "relaunch a dead one"),
    ("POST", "/items/{sid}/P1/permission", {"decision": "allow"},
     lambda store, sid, r: store.item(sid, "P1")["status"] == "allowed", "a permission allowed"),
    ("POST", "/items/{sid}/{ref}/close", {"closed": True},
     lambda store, sid, r: store.item(sid, "T1")["dismissed"] == "todo", "an item closed"),
]


def fill(value, sid, tmp_path):
    if isinstance(value, dict):
        return {k: fill(v, sid, tmp_path) for k, v in value.items()}
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
    ("POST", "/sessions", {"cwd": "/", "runner": "tab"}, 400, "a tab, on a screen nobody remote sees"),
    ("POST", "/items/{sid}/T1/permission", {"decision": "allow"}, 400, "a permission's answer to a task"),
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
    store.post_item(sid, "decision", "pick one", alternative="the other", why="simpler", reverse="pick again")
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
    ("GET", {"host": "evil.example:7428"}, 421, "a page rebound to the daemon's address can't read it"),
    ("POST", {"host": "evil.example:7428"}, 421, "nor drive it"),
    ("POST", {}, 200, "a client sends no Origin"),
    ("POST", {"origin": URL}, 200, "a page of the daemon's own"),
    ("POST", {"origin": "http://evil.example"}, 403, "another site's page is refused"),
    ("POST", {"origin": "null"}, 403, "as is a page with no origin of its own"),
])
async def test_host_and_origin(daemon, sid, method, headers, status, desc):
    path = "/sessions" if method == "GET" else f"/sessions/{sid}/rename"
    r = await call(daemon, method, path, {"name": "x"} if method == "POST" else None, **headers)
    assert r.status_code == status, f"{desc}: {r.text}"


def test_a_name_given_is_accepted_as_a_host():
    assert hosts("100.64.0.1", 7428, ["home"]) == {"100.64.0.1:7428", "home:7428"}
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


def test_main_refuses_every_interface_before_anything_else(monkeypatch):
    monkeypatch.setattr(daemon_mod, "Store", lambda: pytest.fail("opened the store"))
    with pytest.raises(ValueError, match="every interface"):
        daemon_mod.main("0.0.0.0")


def test_one_daemon_per_user(tmp_path):
    first = take_lock(tmp_path, "http://127.0.0.1:7428")
    with pytest.raises(AlreadyRunning, match="127.0.0.1:7428"):
        take_lock(tmp_path, "http://127.0.0.1:7429")
    first.close()
    take_lock(tmp_path, "http://127.0.0.1:7429").close()


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
             "headers": [(b"host", b"127.0.0.1:7428"), (b"idempotency-key", b"k1")]}
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
