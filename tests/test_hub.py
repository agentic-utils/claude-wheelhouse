"""The hub on its own, with no Textual: what it keeps, decides and says."""

import pytest

from claude_wheelhouse import hub as hub_mod, launch, liveness, subagents
from claude_wheelhouse.hub import Hub


@pytest.fixture
def hub(store):
    return Hub(store)


@pytest.mark.parametrize("kept, typed, taken, left, desc", [
    ({}, "half an answer", "half an answer", {}, "what's typed comes back once, then it's gone"),
    ({("s", "Q1"): "old"}, "   ", "", {}, "a blank box forgets what was kept"),
    ({("s", None): "general"}, "for Q1", "for Q1", {("s", None): "general"}, "each target keeps its own"),
])
def test_unsent_text_is_kept_per_target(hub, kept, typed, taken, left, desc):
    hub.unsent.update(kept)
    hub.keep(("s", "Q1"), typed)
    assert hub.take(("s", "Q1")) == taken, desc
    assert hub.unsent == left, desc


def test_the_hub_never_imports_textual():
    import subprocess
    import sys
    code = "import sys, claude_wheelhouse.hub; print(any(m.startswith('textual') for m in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "False"


@pytest.fixture
def heard(hub):
    said = []
    hub.listen(lambda e: said.append((e.text, e.severity)))
    return said


@pytest.mark.parametrize("status, requested, relaunching, shown, desc", [
    ("live", None, False, "live", "as liveness has it"),
    ("live", "park", False, "parking", "a running session asked to park"),
    ("dead", "end", False, "dead", "a dead one's request waits: it shows dead"),
    ("live", None, True, "relaunching", "Relaunch under way outranks the rest"),
])
def test_shown_status(hub, store, sid, monkeypatch, status, requested, relaunching, shown, desc):
    monkeypatch.setattr(liveness, "status", lambda s, **kw: status)
    if requested:
        store.request(sid, requested)
    if relaunching:
        hub.relaunching[sid] = (0.0, (1, 1))
    hub.refresh()
    assert hub.shown_status(store.session(sid)) == shown, desc


def host(store, sid, pid=4242):
    store.set_runner(sid, "sdk")
    store.db.execute("UPDATE sessions SET claude_pid = ?, claude_start = 1, boot_id = 'b'", (pid,))


@pytest.mark.parametrize("asked, stops, wait, launched, said, desc", [
    ((4242, 1), True, 30, ["open"], "relaunched demo", "stopped, then started again once gone"),
    ((4242, 1), False, 0, [], "didn't stop within 0s", "a host that won't stop is given up on, and said so"),
    ((9999, 1), True, 30, [], "started again meanwhile", "only the host the confirm was about is stopped"),
])
def test_relaunch_stops_the_host_then_starts_it_again(hub, store, sid, heard, monkeypatch, asked, stops, wait,
                                                      launched, said, desc):
    host(store, sid)
    alive, calls = {4242}, []
    monkeypatch.setattr(hub_mod.os, "kill", lambda pid, sig: stops and alive.discard(pid))
    monkeypatch.setattr(liveness, "is_alive", lambda pid, *a: pid in alive)
    monkeypatch.setattr(liveness, "status", lambda s, **kw: "live" if s["claude_pid"] in alive else "dead")
    monkeypatch.setattr(hub_mod, "RELAUNCH_WAIT", wait)
    monkeypatch.setattr(launch, "open_session", lambda store, sid: calls.append("open"))
    hub.refresh()
    hub.stop_host(sid, asked)
    hub.refresh()
    hub.finish_relaunches()
    assert calls == launched, desc
    assert any(said in text for text, _ in heard), f"{desc}: {heard}"
    assert sid not in hub.relaunching or asked != (4242, 1), f"{desc}: done with, either way"


def test_relaunch_of_a_dead_session_unparks_it(hub, store, sid, heard, monkeypatch):
    monkeypatch.setattr(launch, "restore_session", lambda store, sid: None)
    store.set_parked(sid, True)
    assert hub.relaunch(sid)
    assert not store.session(sid)["parked"]
    assert heard == [("relaunching", "information")]


def test_a_launch_that_fails_is_said_as_an_error(hub, sid, heard, monkeypatch):
    def refuse(store, sid):
        raise RuntimeError("already running")
    monkeypatch.setattr(launch, "open_session", refuse)
    assert not hub.open_session(sid)
    assert heard == [("already running", "error")]


@pytest.mark.parametrize("parked, status, read, desc", [
    (False, "live", True, "a running session's context is read"),
    (False, "dead", True, "so is a dead one's, once"),
    (True, "live", True, "and a parked one that is running"),
    (True, "dead", False, "not a parked dead one"),
])
def test_which_contexts_are_read_and_said(hub, store, sid, monkeypatch, parked, status, read, desc):
    monkeypatch.setattr(liveness, "status", lambda s, **kw: status)
    store.set_parked(sid, parked)
    groups, landed = [], []
    hub.spawn = lambda work, group: (groups.append(group), work())
    hub.listen(lambda e: e.kind == "landed" and landed.append(e.sid))
    hub.refresh()
    hub.read_contexts()
    assert (sid in hub.contexts, groups, landed) == (read, ["contexts"] * read, [sid] * read), desc


def test_a_failing_subagent_sync_is_said_once_per_error(hub, store, sid, heard, monkeypatch):
    errors = iter(["disk gone", "disk gone", "locked"])

    def failing(self, *a, **k):
        raise OSError(next(errors))
    monkeypatch.setattr(subagents.AgentWatcher, "sync", failing)
    for _ in range(4):
        hub.refresh()
        hub.watch_agents()
    assert heard == [("demo: couldn't track subagents: disk gone", "warning"),
                     ("demo: couldn't track subagents: locked", "warning")]


def test_a_conversation_is_none_once_its_session_has_gone(hub, store, sid):
    assert hub.conversation(sid) is not None
    store.end(sid)
    assert hub.conversation(sid) is None


def test_the_usage_is_fetched_at_most_once_a_minute(hub, monkeypatch):
    now, groups = [1000.0], []
    monkeypatch.setattr(hub_mod.time, "time", lambda: now[0])
    hub.spawn = lambda work, group: groups.append(group)
    for at in (1000.0, 1030.0, 1061.0):
        now[0] = at
        hub.fetch_usage()
    assert groups == ["usage", "usage"]
