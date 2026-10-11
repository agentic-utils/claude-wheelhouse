"""The TUI's client of the daemon (client.RemoteClient), against a daemon served over a real
socket: what it shows changes only at refresh() or after a command, the stream's events
fetch in the background, a lost daemon is said and reconnected to, and a command is done
once whatever the link does."""

import threading
import time

import httpx2
import pytest

from claude_wheelhouse import client as client_mod, launch
from claude_wheelhouse.client import LOST, UNREACHABLE, RemoteClient, Unreachable
from claude_wheelhouse.store import SessionGone

from conftest import TOKEN


def until(check, seconds=10.0, desc=""):
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, f"timed out: {desc}"
        time.sleep(0.02)


@pytest.fixture
def no_launch(monkeypatch):
    monkeypatch.setattr(launch, "open_session", lambda store, sid: None)
    monkeypatch.setattr(launch, "restore_session", lambda store, sid: None)


@pytest.fixture
def heard():
    """A client's events, as the TUI hears them, and the client started on them."""
    said = []

    def start(c):
        c.listen(lambda e: said.append((e.kind, e.text)))
        c.start(lambda callback: callback())
        return c
    start.said = said
    return start


def refs(c, sid=None):
    return sorted(it["ref"] for it in c.inbox(sid).items)


def test_a_command_answers_as_the_hub_did(store, sid, served, no_launch):
    d = served(store.path)
    c = d.client()
    try:
        c.refresh()
        done = c.submit((sid, None), "hello")
        assert (done.ok, [n.text for n in done.notices]) == (True, [
            "queued for the session, but the session isn't running: Restore it, then Ctrl+S or Send sends its queue"])
        assert [m["body"] for m in store.drafts(sid)] == ["hello"]
        assert [d["item_ref"] for d in c.drafts(sid)] == [None], "a command's change is read at once, no refresh needed"
        p = store.post_item(sid, "permission", "Bash: ls")
        c.answer_permission(sid, p, "allow")
        with pytest.raises(KeyError, match="decided already"):
            c.answer_permission(sid, p, "deny")
        with pytest.raises(SessionGone):
            c.rename("nosuch", "x")
        assert not c.rename(sid, None).ok, "a rename cancelled sends nothing"
    finally:
        c.disconnect()


def test_what_it_shows_changes_only_at_refresh(store, sid, served, heard):
    """The stream's events fetch in the background; what's shown waits for refresh(), as
    the hub in process read the store only at its tick."""
    d = served(store.path)
    c = heard(d.client())
    try:
        c.refresh()
        until(lambda: ("inbox",) in c.waiting, desc="the stream's first event, refetch, fetched")
        c.refresh()
        assert refs(c) == []
        store.post_item(sid, "task", "from a host")
        d.tick()   # the daemon sees the write and publishes it
        until(lambda: ("inbox",) in c.waiting, desc="fetched in the background")
        assert refs(c) == [], "not shown before the refresh"
        c.refresh()
        assert refs(c) == ["T1"], "shown at the refresh"
    finally:
        c.disconnect()


def test_a_conversation_shown_is_kept_up_to_date_and_one_not_shown_let_go(store, sid, served, heard):
    d = served(store.path)
    c = heard(d.client())
    try:
        c.refresh()
        t = store.post_item(sid, "task", "build")
        c.refresh()
        assert [m["body"] for m in c.thread(sid, t)[1]] == [], "fetched when first shown"
        store.reply(sid, t, "on it")
        d.tick()
        until(lambda: ("thread", sid, t) in c.waiting, desc="a thread shown, fetched again in the background")
        c.refresh()
        assert [m["body"] for m in c.thread(sid, t)[1]] == ["on it"]
        c.refresh()
        c.refresh()   # not shown since: let go
        assert ("thread", sid, t) not in c.cache
    finally:
        c.disconnect()


def test_a_daemon_lost_is_said_and_reconnected_to(store, sid, served, heard, no_launch):
    d = served(store.path)
    c = heard(d.client())
    try:
        c.refresh()
        until(lambda: c.last_event is not None, desc="the stream connected")
        port = d.port
        d.stop()
        until(lambda: not c.connected, desc="the stream ends")
        assert ("link", "daemon unreachable: reconnecting") in heard.said
        done = c.submit((sid, None), "while it's gone")
        assert (done.ok, [n.text for n in done.notices]) == (False, [UNREACHABLE]), "nothing sent, and said"
        with pytest.raises(Unreachable):
            c.unqueue(1)
        c.refresh()   # what it last had stays
        store.post_item(sid, "task", "while it was down")
        d2 = served(store.path, port)   # started again on its port
        until(lambda: c.connected, desc="reconnected")
        assert ("link", "reconnected to the daemon") in heard.said
        until(lambda: ("inbox",) in c.waiting, desc="everything fetched again (a new daemon's ring: refetch)")
        c.refresh()
        assert refs(c) == ["T1"]
        assert c.submit((sid, None), "back").ok and [m["body"] for m in d2.store.drafts(sid)] == ["back"]
    finally:
        c.disconnect()


def test_a_command_whose_answer_is_lost_is_sent_again_and_done_once(store, sid, served):
    """The request reaches the daemon, its answer doesn't: sent again with the same key,
    the daemon replays its answer rather than act twice."""
    d = served(store.path)
    real = httpx2.HTTPTransport()
    lost = []

    class Flaky(httpx2.BaseTransport):
        def handle_request(self, request):
            response = real.handle_request(request)
            if request.method == "POST" and not lost:
                response.read()
                lost.append(request.headers["idempotency-key"])
                raise httpx2.ReadError("the link dropped")
            return response
    c = RemoteClient(d.url, TOKEN, transport=Flaky())
    try:
        c.refresh()
        assert c.submit((sid, None), "once").ok
        assert [m["body"] for m in store.drafts(sid)] == ["once"] and lost, "done once"
    finally:
        c.disconnect()


def test_a_command_lost_twice_says_it_may_have_been_done(store, sid, served, monkeypatch):
    d = served(store.path)

    class Lost(httpx2.BaseTransport):
        def handle_request(self, request):
            if request.method == "POST":
                raise httpx2.ReadError("the link dropped")
            return httpx2.HTTPTransport().handle_request(request)
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)
    c = RemoteClient(d.url, TOKEN, transport=Lost())
    try:
        done = c.submit((sid, None), "maybe")
        assert (done.ok, [n.text for n in done.notices]) == (False, [LOST])
    finally:
        c.disconnect()


def test_a_rotated_token_is_read_again(store, sid, served):
    d = served(store.path)
    c = RemoteClient(d.url, "an-old-token", reread=lambda: TOKEN)
    try:
        c.refresh()
        assert c.rename(sid, "renamed").ok and store.session(sid)["name"] == "renamed"
        assert c.token == TOKEN
    finally:
        c.disconnect()


def test_a_refused_token_is_said_once(store, sid, served, heard):
    d = served(store.path)
    c = heard(RemoteClient(d.url, "not-the-token"))
    try:
        c.refresh()
        until(lambda: any(kind == "notice" and "refused this client" in text for kind, text in heard.said),
              desc="the stream's 401 said")
        c.refresh()
        assert sum("refused this client" in text for _, text in heard.said) == 1, heard.said
    finally:
        c.disconnect()


def test_the_account_usage_comes_from_the_daemon(store, served, monkeypatch):
    d = served(store.path)
    d.daemon.hub.usage.limits = {"five_hour": (12.0, 1700000000.0)}
    monkeypatch.setattr(client_mod, "USAGE_POLL", 0.05)
    c = d.client()
    c.start()
    try:
        until(lambda: ("usage",) in c.waiting, desc="fetched")
        c.refresh()
        assert c.usage.limits == {"five_hour": (12.0, 1700000000.0)} and not c.usage.failed
    finally:
        c.disconnect()


def test_the_stream_and_fetching_stop_with_the_client(store, served):
    d = served(store.path)
    c = d.client()
    c.start()
    until(lambda: c.last_event is not None, desc="connected")
    c.disconnect()
    for thread in c.threads:
        thread.join(15)   # a fetch under way finishes first, on a loaded machine too
    assert not [t.name for t in c.threads if t.is_alive()]


def test_both_clients_answer_every_call_the_tui_makes():
    """One interface: whatever the TUI asks of its client, in process or through the
    daemon, both have."""
    import re
    from pathlib import Path

    from claude_wheelhouse.client import LocalClient
    source = (Path(client_mod.__file__).parent / "tui.py").read_text()
    calls = set(re.findall(r"(?:self|app)\.client\.(\w+)", source))
    assert len(calls) > 40, sorted(calls)
    for cls in (LocalClient, RemoteClient):
        missing = sorted(name for name in calls if not hasattr(cls, name) and name not in vars_of(cls))
        assert missing == [], f"{cls.__name__} lacks {missing}"


def vars_of(cls) -> set[str]:
    """What an instance sets in its __init__, as hasattr on the class doesn't see it."""
    import inspect
    import re
    names = set()
    for klass in cls.__mro__:
        init = vars(klass).get("__init__")
        if inspect.isfunction(init):
            names |= set(re.findall(r"self\.(\w+)\s*(?::[^=]+)?=", inspect.getsource(init)))
    return names
