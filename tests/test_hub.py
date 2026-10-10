"""The hub on its own, with no Textual: what it keeps, decides and says."""

import pytest

from claude_wheelhouse import hub as hub_mod, launch, liveness, subagents, tutorial
from claude_wheelhouse.hub import Hub
from claude_wheelhouse.store import PROTOCOL_VERSION, SessionGone


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
    """What the hub says to every listener: only what it does by itself."""
    said = []
    hub.listen(lambda e: said.append((e.text, e.severity)))
    return said


def told(done):
    """What a command says to the surface that gave it."""
    return [(e.text, e.severity) for e in done.notices]


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
    assert hub.shown_status(sid) == shown, desc


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
    stopping = told(hub.stop_host(sid, asked))
    hub.refresh()
    hub.finish_relaunches()
    assert calls == launched, desc
    assert any(said in text for text, _ in stopping + heard), f"{desc}: {stopping} {heard}"
    assert sid not in hub.relaunching or asked != (4242, 1), f"{desc}: done with, either way"


def test_relaunch_of_a_dead_session_unparks_it(hub, store, sid, heard, monkeypatch):
    monkeypatch.setattr(launch, "restore_session", lambda store, sid: None)
    store.set_parked(sid, True)
    done = hub.relaunch(sid)
    assert done.ok and not store.session(sid)["parked"]
    assert (told(done), heard) == ([("relaunching", "information")], [])


def test_a_launch_that_fails_is_said_as_an_error(hub, sid, heard, monkeypatch):
    def refuse(store, sid):
        raise RuntimeError("already running")
    monkeypatch.setattr(launch, "open_session", refuse)
    done = hub.open_session(sid)
    assert not done.ok
    assert (told(done), heard) == ([("already running", "error")], [])


def test_a_relaunch_the_hub_finishes_is_said_to_every_listener(hub, store, sid, heard, monkeypatch):
    host(store, sid)
    monkeypatch.setattr(liveness, "is_alive", lambda *a: False)
    monkeypatch.setattr(launch, "open_session", lambda store, sid: None)
    hub.refresh()
    hub.relaunching[sid] = (float("inf"), (4242, 1))
    hub.finish_relaunches()
    assert heard == [("relaunched demo", "information")]


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


def queues(store, sid):
    store.db.execute("UPDATE sessions SET code_version = ?", (PROTOCOL_VERSION,))   # on code that queues


@pytest.mark.parametrize("kind, mode, status, queued, sent, said, desc", [
    ("question", "queued", "live", ["yes"], [], "queued for Q1: Ctrl+S", "queued, by default"),
    ("question", "queued", "dead", ["yes"], [], "but the session isn't running", "a dead session's queue waits"),
    ("question", "immediate", "live", [], ["yes"], "sent to Q1", "immediate mode sends at once"),
    (None, "queued", "live", ["yes"], [], "queued for the session", "a general message, to the session"),
    ("permission", "queued", "live", [], [], "denied P1, with your message", "a permission is denied, never queued"),
])
def test_submit_queues_or_sends_by_the_sessions_mode(hub, store, sid, heard, monkeypatch, kind, mode, status,
                                                     queued, sent, said, desc):
    queues(store, sid)
    store.set_mode(sid, mode)
    monkeypatch.setattr(liveness, "status", lambda s, **kw: status)
    ref = kind and store.post_item(sid, kind, "which db?")
    hub.refresh()
    done = hub.submit((sid, ref), "yes")
    assert done.ok, desc
    assert [m["body"] for m in store.drafts(sid)] == queued, desc
    assert [m["body"] for m in store.pending(sid)] == sent, desc
    assert any(said in text for text, _ in told(done)), f"{desc}: {told(done)}"
    assert heard == [], f"{desc}: said to the surface that gave it, not broadcast"
    assert done.denied == (kind == "permission"), desc
    assert kind != "permission" or store.item(sid, ref)["status"] == "denied", desc


def test_a_permission_answered_meanwhile_is_not_submitted(hub, store, sid, heard):
    ref = store.post_item(sid, "permission", "Bash: rm -rf build")
    hub.asking = lambda target: True   # open as the box was submitted, answered by the time it lands
    store.answer_permission(sid, ref, "allow")
    done = hub.submit((sid, ref), "no")
    assert not done.ok and not done.denied
    assert told(done)[-1][1] == "warning"


@pytest.mark.parametrize("status, left, said, desc", [
    ("dead", ["a"], "demo isn't running: Restore it first", "a dead session's queue stays queued (#62)"),
    ("live", [], "sent 1 to demo", "a live one's goes, as one message"),
])
def test_send_queue_leaves_a_dead_sessions_queue(hub, store, sid, heard, monkeypatch, status, left, said, desc):
    monkeypatch.setattr(liveness, "status", lambda s, **kw: status)
    store.queue(sid, "a")
    hub.refresh()
    done = hub.send_queue(sid)
    assert [m["body"] for m in store.drafts(sid)] == left, desc
    assert told(done)[-1][0] == said, desc


@pytest.mark.parametrize("now, what, acted, desc", [
    ("dead", "park", True, "still dead: parked at once"),
    ("dead", "end", True, "still dead: ended at once"),
    ("live", "park", False, "running again while the confirm sat open: left alone, and said so"),
])
def test_act_on_dead_checks_again(hub, store, sid, heard, monkeypatch, now, what, acted, desc):
    monkeypatch.setattr(liveness, "status", lambda s, **kw: now)
    done = hub.act_on_dead(sid, what)
    assert done.ok == acted, desc
    s = store.session(sid)
    assert (s is None if what == "end" else bool(s["parked"])) == acted, desc
    assert bool(told(done)) != acted, desc


def test_a_command_on_a_session_that_has_gone_raises(hub, store, sid):
    store.end(sid)
    for command in (lambda: hub.toggle_mode(sid), lambda: hub.rename(sid, "x"), lambda: hub.act_on_dead(sid, "end")):
        with pytest.raises(SessionGone):
            command()


@pytest.mark.parametrize("kind, closed, after, desc", [
    ("question", True, "closed", "a question closes"),
    ("question", False, "answered", "and reopens as answered"),
    ("decision", True, "closed", "a decision closes"),
    ("decision", False, "seen", "and reopens as seen"),
])
def test_close_and_reopen(hub, store, sid, kind, closed, after, desc):
    decided = {"alternative": "Postgres", "why": "local", "reverse": "migrate"} if kind == "decision" else {}
    ref = store.post_item(sid, kind, "used SQLite", **decided)
    if not closed:   # closed first, to reopen
        hub.close(sid, ref, True)
    hub.close(sid, ref, closed)
    assert store.item(sid, ref)["status"] == after, desc


def refuse(store):
    raise RuntimeError("claude missing")


@pytest.mark.parametrize("take, start, started, setting, desc", [
    (False, None, False, "dismissed", "declined: never offered again"),
    (True, refuse, False, "dismissed", "a start that fails is said, and not offered again"),
    (True, lambda store: store.set_setting(tutorial.OFFER_KEY, "taken"), True, "taken", "taken: it starts"),
])
def test_the_tutorial_offer_answered(hub, store, heard, monkeypatch, take, start, started, setting, desc):
    if start:
        monkeypatch.setattr(tutorial, "start", start)
    done = hub.answer_offer(take)
    assert done.ok == started, desc
    assert store.setting(tutorial.OFFER_KEY) == setting, desc
    assert (len(told(done)), heard) == (1, []), desc


@pytest.mark.parametrize("command, said, desc", [
    (lambda hub, sid: hub.rename(sid, "x"), [("renamed to x", "information")], "a rename"),
    (lambda hub, sid: hub.ask(sid, "park"), [("asked the session to park", "information")], "Park asked"),
    (lambda hub, sid: (hub.store.queue(sid, "a"), hub.toggle_mode(sid))[1],
     [("demo: answers now send as you submit them", "information"),
      ("1 answer(s) still queued for demo: Ctrl+S or Send sends them", "information")], "both of a mode's notices"),
    (lambda hub, sid: hub.host_command(sid, "interrupt"), [("interrupting", "information")], "a host command"),
    (lambda hub, sid: hub.send_all(), [("nothing queued", "information")], "Send all with nothing queued"),
])
def test_a_commands_notices_go_to_its_surface_alone(hub, sid, heard, command, said, desc):
    hub.refresh()
    assert (told(command(hub, sid)), heard) == (said, []), desc


@pytest.mark.parametrize("gone, raised, desc", [
    ("item", KeyError, "an item that isn't there"),
    ("session", SessionGone, "a session that has ended"),
])
def test_close_by_id_of_what_isnt_there(hub, store, sid, gone, raised, desc):
    if gone == "session":
        ref = store.post_item(sid, "question", "which db?")
        store.end(sid)
    else:
        ref = "Q9"
    with pytest.raises(raised):
        hub.close(sid, ref, True)


@pytest.mark.parametrize("helper, desc", [
    (lambda hub, sid: hub.shown_status(sid), "the status shown"),
    (lambda hub, sid: hub.busy(sid), "busy"),
    (lambda hub, sid: hub.stale(sid), "stale"),
    (lambda hub, sid: hub.sends_now(sid), "sends now"),
])
def test_the_status_helpers_read_the_listed_row_by_id(hub, store, sid, monkeypatch, helper, desc):
    monkeypatch.setattr(liveness, "status", lambda s, **kw: "live")
    hub.refresh()
    reads = []
    monkeypatch.setattr(store, "session", lambda sid: reads.append(sid))
    helper(hub, sid)
    assert reads == [], f"{desc}: painted every tick, so from the refresh's rows, not the store"
