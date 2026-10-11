"""The hub on its own, with no Textual: what it keeps, decides and says."""

import json
import sqlite3
import time
from datetime import datetime, timezone

import pytest

from claude_wheelhouse import host as host_mod, hub as hub_mod, launch, liveness, models, stats, subagents, transcript, tutorial
from claude_wheelhouse.hub import Hub
from claude_wheelhouse.store import PROTOCOL_VERSION, SessionGone

real_open_session = launch.open_session   # before any test stubs it


@pytest.fixture
def hub(store):
    return Hub(store)


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
    monkeypatch.setattr(launch, "open_session", lambda store, sid, may_tab=True: calls.append("open"))
    hub.refresh()
    stopping = told(hub.stop_host(sid, asked))
    hub.refresh()
    hub.finish_relaunches()
    assert calls == launched, desc
    assert any(said in text for text, _ in stopping + heard), f"{desc}: {stopping} {heard}"
    assert sid not in hub.relaunching or asked != (4242, 1), f"{desc}: done with, either way"


def test_relaunch_of_a_dead_session_unparks_it(hub, store, sid, heard, monkeypatch):
    monkeypatch.setattr(launch, "restore_session", lambda store, sid, sdk=None, may_tab=True: None)
    store.set_parked(sid, True)
    done = hub.relaunch(sid)
    assert done.ok and not store.session(sid)["parked"]
    assert (told(done), heard) == ([("relaunching", "information")], [])


def test_a_launch_that_fails_is_said_as_an_error(hub, sid, heard, monkeypatch):
    def refuse(store, sid, may_tab=True):
        raise RuntimeError("already running")
    monkeypatch.setattr(launch, "open_session", refuse)
    done = hub.open_session(sid)
    assert not done.ok
    assert (told(done), heard) == ([("already running", "error")], [])


def test_a_relaunch_the_hub_finishes_is_said_to_every_listener(hub, store, sid, heard, monkeypatch):
    host(store, sid)
    monkeypatch.setattr(liveness, "is_alive", lambda *a: False)
    monkeypatch.setattr(launch, "open_session", lambda store, sid, may_tab=True: None)
    hub.refresh()
    hub.relaunching[sid] = (float("inf"), (4242, 1))
    hub.finish_relaunches()
    assert heard == [("relaunched demo", "information")]


@pytest.mark.parametrize("stops, wait, launched, said, desc", [
    (True, 30, ["open"], "relaunched demo", "its host gone meanwhile: started again"),
    (False, 0, [], "didn't stop within 0s", "still up by its deadline: given up on, and said so"),
])
def test_a_relaunch_under_way_is_carried_on_by_a_hub_started_again(store, sid, monkeypatch, stops, wait, launched,
                                                                   said, desc):
    host(store, sid)
    alive, calls = {4242}, []
    monkeypatch.setattr(hub_mod.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(liveness, "is_alive", lambda pid, *a: pid in alive)
    monkeypatch.setattr(liveness, "status", lambda s, **kw: "live" if s["claude_pid"] in alive else "dead")
    monkeypatch.setattr(hub_mod, "RELAUNCH_WAIT", wait)
    monkeypatch.setattr(launch, "open_session", lambda store, sid, may_tab=True: calls.append("open"))
    first = Hub(store)
    first.refresh()
    assert first.stop_host(sid, (4242, 1)).ok
    if stops:
        alive.clear()
    again, heard = Hub(store), []   # the first one gone with it under way: a daemon restarted
    again.listen(lambda e: heard.append(e.text))
    assert sid in again.relaunching, desc
    again.refresh()
    again.finish_relaunches()
    assert calls == launched, desc
    assert any(said in text for text in heard), f"{desc}: {heard}"
    assert sid not in again.relaunching and store.relaunches() == {}, f"{desc}: done with, here and in the store"


def test_a_relaunch_found_past_its_deadline_is_let_go_and_said(store, sid, monkeypatch):
    host(store, sid)
    store.set_parked(sid, True)
    store.set_relaunch(sid, (4242, 1), time.time() - 86400)   # a day ago: the machine was off
    calls, heard = [], []
    monkeypatch.setattr(liveness, "is_alive", lambda *a: False)
    monkeypatch.setattr(launch, "open_session", lambda store, sid, may_tab=True: calls.append("open"))
    monkeypatch.setattr(launch, "restore_session", lambda store, sid, sdk=None, may_tab=True: calls.append("restore"))
    hub = Hub(store)
    hub.listen(lambda e: heard.append((e.text, e.severity)))
    hub.refresh()
    hub.finish_relaunches()
    assert calls == [], "never started again by itself"
    assert heard == [("demo wasn't relaunched: Restore it", "warning")]
    assert sid not in hub.relaunching and store.relaunches() == {} and store.session(sid)["parked"]


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
    decided = {"why": "over Postgres: local; undo: migrate"} if kind == "decision" else {}
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


# a subagent's own conversation (#69)

AGENT_RECS = [
    {"type": "user", "isSidechain": True, "timestamp": "2026-10-10T22:25:07Z",
     "message": {"content": "find the VAT rounding bug"}},
    {"type": "user", "isSidechain": True, "isMeta": True, "message": {"content": "<system-reminder>x</system-reminder>"}},
    {"type": "assistant", "isSidechain": True, "timestamp": "2026-10-10T22:25:30Z", "message": {"content": [
        {"type": "thinking", "thinking": "hmm"},
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "grep -rn round"}}]}},
    {"type": "user", "isSidechain": True, "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                                                   "content": "invoice.py:12"}]}},
    {"type": "assistant", "isSidechain": True, "timestamp": "2026-10-10T22:26:00Z", "message": {"content": [
        {"type": "text", "text": "It rounds per line."}]}},
    {"type": "assistant", "isSidechain": True, "timestamp": "2026-10-10T22:26:10Z", "message": {"content": [
        {"type": "tool_use", "id": "t2", "name": "SubagentHandback", "input": {"message": "Fixed in invoice.py."}}]}},
]


def write_agent(tmp_path, sid, aid="a1", recs=AGENT_RECS):
    """A session's transcript in a fake projects folder, and a subagent's beside it."""
    folder = tmp_path / "projects/-home-u-repo"
    (folder / sid / "subagents").mkdir(parents=True, exist_ok=True)
    folder.joinpath(f"{sid}.jsonl").write_text("")
    path = folder / sid / "subagents" / f"agent-{aid}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in recs))
    return path


@pytest.fixture
def projects(tmp_path, monkeypatch):
    monkeypatch.setattr(transcript, "PROJECTS", tmp_path / "projects")
    return tmp_path


def agent_item(store, sid, aid="a1", title="find the bug"):
    store.track_agent(sid, aid, title, "A `general-purpose` subagent.", datetime.now(timezone.utc))
    return store.agent_links(sid)[aid][0]


@pytest.mark.parametrize("linked, written, shows, desc", [
    (True, True, ["head", "you", "tool", "claude", "claude"], "its prompt, tool calls, reply and result"),
    (True, False, ["head", "note"], "linked, but its file isn't there: said so"),
    (False, True, ["head", "note"], "an A item the session posted, tied to no subagent: said so"),
])
def test_a_subagent_items_conversation(hub, store, sid, projects, linked, written, shows, desc):
    ref = agent_item(store, sid) if linked else store.post_item(sid, "agent", "mine", status="running")
    if written:
        write_agent(projects, sid)
    blocks = hub.agent_conversation(sid, ref)
    assert [who for who, _ in blocks] == shows, desc
    if shows[1] == "you":
        mds = [md for _, md in blocks]
        assert mds[1].startswith("**prompt** · 22:25Z") and "find the VAT rounding bug" in mds[1]
        assert mds[2] == "`⚙ Bash: grep -rn round`"
        assert mds[-1].endswith("Fixed in invoice.py."), "its handed-back result, whole"
    else:
        assert blocks[1][1] == (transcript.NO_AGENT_FILE if linked else transcript.NO_AGENT_LINK), desc


def test_only_a_subagent_item_has_its_conversation(hub, store, sid):
    q = store.post_item(sid, "question", "which db?")
    assert (hub.agent_conversation(sid, q), hub.agent_conversation(sid, "A9")) == (None, None)


def test_a_subagents_conversation_follows_it_live_and_two_at_most(hub, store, sid, projects):
    ref, other, third = (agent_item(store, sid, aid, aid) for aid in ("a1", "a2", "a3"))
    path = write_agent(projects, sid, recs=AGENT_RECS[:1])
    for aid in ("a2", "a3"):
        write_agent(projects, sid, aid)
    assert [who for who, _ in hub.agent_conversation(sid, ref)] == ["head", "you"]
    with open(path, "a") as f:
        f.write(json.dumps(AGENT_RECS[4]) + "\n")
    assert hub.agent_conversation(sid, ref)[-1][1].endswith("It rounds per line."), "read again once it changed"
    following = hub.agent_followers[(sid, ref)]
    for _ in range(2):   # a full screen on one while the inbox shows another, each tick
        hub.agent_conversation(sid, other)
        hub.agent_conversation(sid, ref)
    assert hub.agent_followers[(sid, ref)] is following and len(hub.agent_followers) == 2, \
        "two shown at once are each followed, not read afresh in turn"
    hub.agent_conversation(sid, third)
    assert list(hub.agent_followers) == [(sid, ref), (sid, third)], "a third stops following the one asked for longest ago"
    hub.unfollow_agent()
    assert hub.agent_followers == {}


@pytest.mark.parametrize("model, effort, stored, desc", [
    (None, None, ("claude-sonnet-5-5", "high"), "the defaults, stored on the session (Q50)"),
    ("claude-haiku-5-5", None, ("claude-haiku-5-5", "xhigh"), "a model alone takes its tier's effort (Q54)"),
    ("claude-opus-5-5", "max", ("claude-opus-5-5", "max"), "as chosen"),
])
@pytest.mark.usefixtures("sdk_on")
def test_a_new_session_stores_its_model_and_effort(hub, store, tmp_path, monkeypatch, model, effort, stored, desc):
    monkeypatch.setattr(launch, "open_session", lambda store, sid, may_tab=True: None)
    done = hub.new_session(str(tmp_path), "n", "", "", "sdk", model, effort)
    s = store.session(done.sid)
    assert (s["model"], s["effort"]) == stored, desc


@pytest.mark.parametrize("on, runner, ok, desc", [
    (False, "tab", True, "SDK mode off: a tab"),
    (False, "sdk", False, "nor a host, whoever asks: refused before anything is created"),
    (True, "sdk", True, "SDK mode on: a host"),
])
def test_sdk_mode_gates_a_new_hosted_session(store, tmp_path, monkeypatch, on, runner, ok, desc):
    opened = []
    monkeypatch.setattr(launch, "open_session", lambda store, sid, may_tab=True: opened.append(sid))
    done = Hub(store, sdk=on).new_session(str(tmp_path), "n", "", "", runner)
    assert done.ok is ok and len(store.sessions()) == len(opened) == int(ok), desc
    if ok:
        assert store.session(done.sid)["runner"] == runner, desc


@pytest.mark.usefixtures("sdk_on")
@pytest.mark.parametrize("runner, command, value, ok, column, said, desc", [
    ("sdk", "set_model", "claude-opus-5-5", True, "claude-opus-5-5", "demo: model Opus 5.5", "a hosted session's model"),
    ("sdk", "set_effort", "xhigh", True, "xhigh", "demo: effort xhigh", "its effort"),
    ("sdk", "set_effort", "high", False, "high", None, "already that: nothing done"),
    ("tab", "set_model", "claude-opus-5-5", False, "claude-sonnet-5-5", "demo runs in a tab: type /model there",
     "never a tab's (Q52): it changes its own"),
    ("tab", "set_effort", "low", False, "high", "demo runs in a tab: type /effort there", "nor its effort"),
    ("shell", "set_model", "claude-opus-5-5", True, "claude-opus-5-5",
     "demo: model Opus 5.5 when the wheelhouse takes it back from its shell tab, unless /model is typed there after this",
     "handed to a shell tab: stored, and said to apply on taking it back, not live"),
])
def test_model_and_effort_change_only_for_a_hosted_session(hub, store, tmp_path, runner, command, value, ok, column,
                                                             said, desc):
    sid = store.create_session(str(tmp_path), name="demo", runner="sdk" if runner == "shell" else runner,
                               model="claude-sonnet-5-5", effort="high")
    if runner == "shell":
        store.set_shell(sid, "tab")
    done = getattr(hub, command)(sid, value)
    assert done.ok == ok, desc
    assert store.session(sid)["model" if command == "set_model" else "effort"] == column, desc
    assert [e.text for e in done.notices] == ([said] if said else []), desc


@pytest.mark.parametrize("command, value", [("set_model", "claude-opus-5-5"), ("set_effort", "xhigh")])
def test_a_hosts_model_and_effort_stay_as_set_out_of_sdk_mode(store, tmp_path, command, value):
    sid = store.create_session(str(tmp_path), name="demo", runner="sdk", model="claude-sonnet-5-5", effort="high")
    done = getattr(Hub(store, sdk=False), command)(sid, value)
    assert not done.ok and (store.session(sid)["model"], store.session(sid)["effort"]) == ("claude-sonnet-5-5", "high")
    assert [e.text for e in done.notices] == [f"demo's {command.removeprefix('set_')} can't be changed here"]


@pytest.mark.parametrize("on, hosted", [(True, True), (False, False)])
def test_the_slider_is_sdk_modes(store, tmp_path, monkeypatch, on, hosted):
    """A running host's session out of SDK mode: no slider (hosted False), its settings still shown."""
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "live")
    sid = store.create_session(str(tmp_path), runner="sdk", model="claude-opus-5-5", effort="high")
    hub = Hub(store, sdk=on)
    hub.refresh()
    got = hub.model_state(hub.row(sid))
    assert (got["model"], got["effort"], got["effort_live"]) == ("claude-opus-5-5", "high", hosted)


@pytest.mark.usefixtures("sdk_on")
@pytest.mark.parametrize("runner, status, shell, columns, applied, state, desc", [
    ("sdk", "live", None, ("claude-opus-5-5", "high"), ("claude-opus-5-5", "high"),
     ("claude-opus-5-5", "high", None, True), "applied: nothing pending"),
    ("sdk", "live", None, ("claude-opus-5-5", "xhigh"), ("claude-opus-5-5", "high"),
     ("claude-opus-5-5", "xhigh", "applying", True), "its host has yet to apply the change"),
    ("sdk", "live", None, ("claude-opus-5-5", "xhigh"), (None, None),
     ("claude-opus-5-5", "xhigh", "on relaunch", True), "a host on older code never will"),
    ("sdk", "live", None, (None, None), ("claude-opus-5-5", "medium"),
     ("claude-opus-5-5", "medium", None, True), "no columns (from before): what its host confirmed"),
    ("sdk", "dead", None, ("claude-opus-5-5", "xhigh"), ("claude-opus-5-5", "high"),
     ("claude-opus-5-5", "xhigh", None, False), "dead: it launches with the columns, nothing pending"),
    ("sdk", "live", "tab", ("claude-opus-5-5", "xhigh"), ("claude-opus-5-5", "high"),
     ("claude-opus-5-5", "xhigh", None, False), "handed to a shell tab: no live change"),
    ("tab", "live", None, (None, None), (None, None), (None, None, None, False),
     "a tab with no columns: the stats pane reads its transcript"),
])
def test_model_state(hub, store, tmp_path, monkeypatch, runner, status, shell, columns, applied, state, desc):
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: status)
    sid = store.create_session(str(tmp_path), runner=runner, model=columns[0], effort=columns[1])
    store.set_applied(sid, *applied)
    store.set_shell(sid, shell)
    hub.refresh()
    got = hub.model_state(hub.row(sid))
    assert (got["model"], got["effort"], got["pending"], got["effort_live"]) == state, desc


@pytest.mark.parametrize("runner_, shell, status, tokens, offered, desc", [
    ("sdk", None, "live", 160_000, True, "a running host past 150k: offered"),
    ("sdk", None, "live", 150_000, False, "at the line, not past it"),
    ("sdk", None, "live", None, False, "no size recorded yet"),
    ("sdk", "tab", "live", 160_000, False, "handed to a shell tab: nothing to ask"),
    ("sdk", None, "dead", 160_000, False, "dead: Relaunch is Restore, no host to compact"),
    ("tab", None, "live", 160_000, False, "a tab: no Compact command"),
])
def test_compact_first_is_offered(hub, store, sid, monkeypatch, runner_, shell, status, tokens, offered, desc):
    monkeypatch.setattr(liveness, "status", lambda s, **kw: status)
    store.set_runner(sid, runner_)
    store.set_shell(sid, shell)
    store.db.execute("UPDATE sessions SET context_tokens = ?", (tokens,))
    hub.refresh()
    assert hub.compact_first(sid) is offered, desc


def compacting(hub, store, sid, monkeypatch, alive):
    """A Relaunch compacting first, under way: (the pids killed, the opens)."""
    host(store, sid)
    killed, opened = [], []
    monkeypatch.setattr(hub_mod.os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(liveness, "is_alive", lambda pid, *a: pid in alive)
    monkeypatch.setattr(liveness, "status", lambda s, **kw: "live" if s["claude_pid"] in alive else "dead")
    monkeypatch.setattr(launch, "open_session", lambda store, sid, may_tab=True: opened.append(sid))
    store.set_context(sid, 220_000, 1_000_000)
    hub.refresh()
    done = hub.compact_then_relaunch(sid, (4242, 1))
    assert done.ok and told(done) == [("relaunching demo: compacting first (it's asked what to keep)",
                                        "information")]
    return killed, opened


def compacted_to(store, sid, tokens, activity):
    """The host records a context since Compact was asked for, and an activity line."""
    store.set_context(sid, tokens, 1_000_000)
    store.set_activity(sid, activity)


@pytest.mark.parametrize("then, stopped, said, desc", [
    (lambda st, sid: compacted_to(st, sid, 40_000, host_mod.COMPACTED + " 220k → 40k tokens"), True,
     "stopping its host", "compacted: its host is stopped"),
    (lambda st, sid: compacted_to(st, sid, 40_000, "idle"), True, "stopping its host",
     "compacted, then a turn queued behind /compact ended: the line says idle, the context says compacted"),
    (lambda st, sid: compacted_to(st, sid, 40_000, "running Bash: ls"), False, None,
     "compacted, with a turn queued behind it running: it waits for that turn"),
    (lambda st, sid: compacted_to(st, sid, 230_000, "idle"), False, None,
     "a turn ended, not compacted: it waits"),
    (lambda st, sid: st.set_activity(sid, "idle · compacted 300k → 40k tokens"), False, None,
     "a compaction from before Relaunch: its line isn't this one's"),
    (lambda st, sid: st.set_activity(sid, "thinking"), False, None, "still collecting notes: it waits"),
    (lambda st, sid: st.set_activity(sid, "interrupted"), False, "Compact was interrupted",
     "interrupted: given up, and said so"),
    (lambda st, sid: st.set_activity(sid, host_mod.NO_NOTES), False, "Compact got no notes back",
     "no notes: given up, and said so"),
    (lambda st, sid: (st.set_interrupted(sid), st.set_activity(sid, "error: error_during_execution")), False,
     "Compact was interrupted", "interrupted by an older wheelhouse, its line since written over: given up all the same"),
    ("timeout", False, "wasn't compacted within 30 minutes", "too long: given up, and said so"),
    ("late", False, "wasn't compacted within 30 minutes",
     "compacted, but only seen past the deadline: given up, never a restart hours later"),
    ("shell", False, "it was handed to a shell tab", "handed to a shell tab: given up, not 'relaunched'"),
    ("other", False, "its host was started again meanwhile", "another host took over: given up"),
])
def test_compact_then_relaunch(hub, store, sid, heard, monkeypatch, then, stopped, said, desc):
    alive = {4242, 5151}
    killed, opened = compacting(hub, store, sid, monkeypatch, alive)
    s = store.session(sid)
    assert (s["host_command"], s["activity"]) == ("compact", ""), f"{desc}: the activity line stays the host's"
    assert sid in hub.compacting and store.relaunches()[sid][2] is not None, f"{desc}: kept in the store"
    if then == "timeout":
        hub.relaunching[sid] = (0.0, (4242, 1))
    elif then == "late":
        compacted_to(store, sid, 40_000, "idle")
        hub.relaunching[sid] = (0.0, (4242, 1))
    elif then == "shell":
        store.set_shell(sid, "tab")
        store.db.execute("UPDATE sessions SET claude_pid = 5151")
    elif then == "other":
        store.db.execute("UPDATE sessions SET claude_pid = 5151")
    else:
        then(store, sid)
    hub.refresh()
    hub.finish_relaunches()
    assert killed == ([4242] if stopped else []), desc
    if said:
        assert any(said in text for text, _ in heard), f"{desc}: {heard}"
    assert not any("relaunched demo" == text for text, _ in heard), desc
    under_way = sid in hub.relaunching
    assert under_way == (stopped or not said), f"{desc}: given up only when said"
    assert (sid in hub.compacting) == (under_way and not stopped), f"{desc}: compacting until stopped"
    assert not opened, desc
    if stopped:
        assert store.session(sid)["host_command"] is None, f"{desc}: no Compact left for the new host"
        assert store.relaunches()[sid][2] is None, f"{desc}: now waiting for it to stop"


def test_a_compacting_relaunch_whose_host_died_is_relaunched(hub, store, sid, heard, monkeypatch):
    alive = {4242}
    killed, opened = compacting(hub, store, sid, monkeypatch, alive)
    alive.clear()
    for _ in range(2):   # let go of compacting, then started again as any stopped host
        hub.refresh()
        hub.finish_relaunches()
    assert (killed, opened) == ([], [sid])


def test_another_machines_compacting_relaunch_never_comes_back_as_a_tab(hub, store, sid, heard, monkeypatch):
    """Review 75: asked for by another machine, a Relaunch compacting first carries that on
    (store.relaunch_may_tab), so a host that dies just as it is to be stopped isn't
    brought back in a tab here, where nobody there sees it."""
    alive = {4242}
    killed, opened = compacting(hub, store, sid, monkeypatch, alive)
    hub.relaunch_under_way(sid, (4242, 1), asked=hub.compacting[sid], may_tab=False)   # as the daemon asks
    assert not store.relaunch_may_tab(sid)
    tabs = []
    monkeypatch.setattr(launch, "open_tab", lambda store, sid, notice=True: tabs.append(sid))
    monkeypatch.setattr(launch, "open_session", real_open_session)
    compacted_to(store, sid, 40_000, "idle")
    take = store.take_command
    monkeypatch.setattr(store, "take_command", lambda *a: alive.clear() or take(*a))   # it dies just then
    hub.refresh()
    hub.finish_relaunches()
    assert (killed, tabs) == ([], []), heard
    assert any("host has stopped: Relaunch it from the daemon's machine" in text for text, _ in heard), heard


@pytest.mark.parametrize("activity, command, sent, desc", [
    ("idle", None, True, "idle: Compact is asked for"),
    ("compacting: collecting notes", None, False, "a Compact under way: not a second one"),
    (host_mod.AWAITING_NOTES, None, False, "a Compact waiting for its notes: not a second one"),
    ("compacting", None, False, "/compact running: not a second one"),
    ("thinking", "compact", False, "Compact pressed, not yet taken: not a second one"),
])
def test_compact_first_never_asks_twice(hub, store, sid, monkeypatch, activity, command, sent, desc):
    host(store, sid)
    monkeypatch.setattr(liveness, "is_alive", lambda pid, *a: True)
    store.set_activity(sid, activity)
    if command:
        store.command(sid, command)
    hub.refresh()
    asked = []
    real = store.command
    monkeypatch.setattr(store, "command", lambda s, what: (asked.append(what), real(s, what)))
    assert hub.compact_then_relaunch(sid, (4242, 1)).ok, desc
    assert asked == (["compact"] if sent else []), desc
    assert sid in hub.compacting, desc


def test_interrupt_cancels_compact_first_in_every_wheelhouse(store, sid, monkeypatch):
    """Interrupt cancels a Relaunch waiting on Compact, from the wheelhouse that began it
    or another (the daemon's); the one that began it lets it go quietly."""
    host(store, sid)
    monkeypatch.setattr(liveness, "is_alive", lambda pid, *a: True)
    monkeypatch.setattr(liveness, "status", lambda s, **kw: "live")
    began, other = Hub(store), Hub(store)
    said = []
    began.listen(lambda e: said.append(e.text))
    began.refresh()
    assert began.compact_then_relaunch(sid, (4242, 1)).ok
    done = other.host_command(sid, "interrupt")
    assert any("cancelled" in text for text, _ in told(done)), told(done)
    assert store.relaunches() == {} and store.session(sid)["host_command"] == "interrupt"
    store.set_context(sid, 40_000, 1_000_000)   # compacted after all: still not relaunched
    store.set_activity(sid, "idle")
    began.refresh()
    began.finish_relaunches()
    assert sid not in began.relaunching and sid not in began.compacting and said == []


def test_a_compacting_relaunch_is_carried_on_by_a_hub_started_again(store, sid, monkeypatch):
    host(store, sid)
    monkeypatch.setattr(liveness, "is_alive", lambda pid, *a: True)
    first = Hub(store)
    first.refresh()
    assert first.compact_then_relaunch(sid, (4242, 1)).ok
    again = Hub(store)
    assert sid in again.relaunching and sid in again.compacting


def test_a_command_pressed_as_compact_first_stops_the_host_stays_for_the_next(hub, store, sid, monkeypatch):
    """Only the Compact it asked for is taken back: a Shell pressed meanwhile is the new host's."""
    alive = {4242}
    killed, _ = compacting(hub, store, sid, monkeypatch, alive)
    compacted_to(store, sid, 40_000, "idle")
    store.command(sid, "shell")
    hub.refresh()
    hub.finish_relaunches()
    assert killed == [4242] and store.session(sid)["host_command"] == "shell"


@pytest.mark.parametrize("pending, left, desc", [
    ("interrupt", None, "an Interrupt pressed in the gap: not left to greet the new host as interrupted"),
    ("compact", None, "the Compact it asked for, untaken: not run again"),
    ("shell", "shell", "a Shell: the person's, left for the new host"),
])
def test_stopping_for_the_relaunch_clears_only_what_would_misgreet_the_new_host(hub, store, sid, monkeypatch,
                                                                                pending, left, desc):
    alive = {4242}
    killed, _ = compacting(hub, store, sid, monkeypatch, alive)
    compacted_to(store, sid, 40_000, "idle")
    store.command(sid, pending)
    hub.refresh()
    hub.finish_relaunches()
    assert killed == [4242] and store.session(sid)["host_command"] == left, desc


@pytest.mark.parametrize("ran, state, desc", [
    (("claude-opus-5-5[1m]", "max"), ("claude-opus-5-5[1m]", "max"),
     "created on Sonnet, its transcript says Opus (a /model typed there): what runs"),
    ((None, None), ("claude-sonnet-5-5", "high"), "the transcript has nothing yet: its columns"),
])
def test_a_tabs_model_state_is_what_its_transcript_says(hub, store, tmp_path, monkeypatch, ran, state, desc):
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "live")
    sid = store.create_session(str(tmp_path), runner="tab", model="claude-sonnet-5-5", effort="high")
    hub.refresh()
    if ran[0]:
        follower = stats.follower(sid)
        follower.ready = True
        follower.snap.model, follower.snap.requested, follower.snap.effort = "claude-opus-5-5", *ran
        hub.contexts[sid] = follower
    got = hub.model_state(hub.row(sid))
    assert (got["model"], got["effort"], got["pending"], got["effort_live"]) == (*state, None, False), desc


def ran_now(snap):
    """A turn begun now, as the snapshot's requested (or served) model and effort say."""
    for key, value in (("model", models.ran_as(snap.requested, snap.model)), ("effort", snap.effort)):
        if value:
            snap.asked[key] = (time.time(), value)


def ran_in(hub, sid, model, effort, served=None):
    """The hub reads the session's transcript, which shows it ran on model at effort (and
    was answered by served, where given)."""
    follower = stats.follower(sid)
    hub.contexts[sid] = follower
    follower.read = lambda now=None: True
    follower.ready = True
    follower.snap.model = served or (model or "").split("[")[0] or None
    follower.snap.requested, follower.snap.effort = model, effort
    ran_now(follower.snap)
    hub.post = lambda work: None
    hub.read_context(follower)


@pytest.mark.parametrize("runner_, shell, steps, columns, desc", [
    ("tab", None, [("claude-opus-5-5[1m]", "max")], ("claude-opus-5-5", "max"),
     "a tab's /model opus[1m] and /effort max: stored as the pinned id a launch passes"),
    ("tab", None, [(None, None)], ("claude-sonnet-5-5", "high"), "nothing in the transcript yet: the columns stand"),
    ("tab", None, [("claude-legacy-4", "low")], ("claude-legacy-4", "low"), "an id off the table: kept as it ran"),
    ("tab", None, [(None, "high", "claude-opus-5-5-20261001")], ("claude-opus-5-5", "high"),
     "no requested model: the dated id that answered, as the table's pinned id"),
    ("tab", None, [(None, "high", "claude-legacy-4-20250101")], ("claude-sonnet-5-5", "high"),
     "no requested model, and one off the table answered: nothing to launch with, not copied"),
    ("sdk", None, [("claude-opus-5-5", "max")], ("claude-sonnet-5-5", "high"),
     "a hosted session: its host applies the columns, nothing copied"),
    ("sdk", "tab", [("claude-opus-5-5", "low")], ("claude-sonnet-5-5", "high"),
     "in a shell tab: its turns run what the wheelhouse chose, only a /model or /effort typed is carried"),
])
def test_a_tabs_model_and_effort_are_copied_back(hub, store, tmp_path, monkeypatch, runner_, shell, steps, columns, desc):
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "live")
    sid = store.create_session(str(tmp_path), runner=runner_, model="claude-sonnet-5-5", effort="high")
    if shell:
        store.set_shell(sid, shell)
    hub.refresh()
    for step in steps:
        ran_in(hub, sid, *step)
    s = store.session(sid)
    assert (s["model"], s["effort"]) == columns, desc


@pytest.mark.usefixtures("sdk_on")
def test_a_choice_made_during_a_shell_tab_is_not_undone_by_the_hub(hub, store, tmp_path, monkeypatch):
    """The review's repro: hosted on Opus, Sonnet chosen, the shell tab launched on Sonnet,
    Haiku chosen while it runs; the tab's first turn, on Sonnet, leaves Haiku standing."""
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "live")
    sid = store.create_session(str(tmp_path), runner="sdk", model="claude-opus-5-5", effort="high")
    hub.refresh()
    ran_in(hub, sid, "claude-opus-5-5", "high")   # hosted, a turn run
    hub.set_model(sid, "claude-sonnet-5-5")
    store.set_shell(sid, "tab")
    hub.refresh()
    ran_in(hub, sid, "claude-opus-5-5", "high")   # the tab launched on Sonnet, no turn yet
    assert hub.set_model(sid, "claude-haiku-5-5").ok
    ran_in(hub, sid, "claude-sonnet-5-5", "high")   # the tab's first turn
    assert store.session(sid)["model"] == "claude-haiku-5-5"


@pytest.mark.usefixtures("sdk_on")   # Model and the effort slider are SDK mode's alone
@pytest.mark.parametrize("what, value, typed", [
    ("model", "claude-opus-5-5", "claude-sonnet-5-5"),
    ("effort", "high", "low"),
])
def test_rechoosing_what_a_session_has_is_still_a_choice(hub, store, tmp_path, what, value, typed):
    """Round 66, nit 4: chosen in the wheelhouse again, unchanged, it is dated now, so a /model
    or /effort typed in its shell tab before that doesn't override it."""
    sid = store.create_session(str(tmp_path), runner="sdk", model="claude-opus-5-5", effort="high")
    store.db.execute(f"UPDATE sessions SET {what}_chosen_at = ? WHERE id = ?",
                     (datetime.fromtimestamp(time.time() - 3600, timezone.utc).isoformat(), sid))
    assert not (hub.set_model if what == "model" else hub.set_effort)(sid, value).ok, "it's that already"
    assert not store.carry(sid, what, typed, time.time() - 60), f"a /{what} typed before the re-choice gives way"
    assert store.session(sid)[what] == value


@pytest.mark.parametrize("locked, desc", [
    (0, "a tab that has exited: its transcript read once more, for what it wrote in its last second"),
    (1, "the database locked as the hub copies back: the next tick reads, and copies, again"),
])
def test_a_tabs_last_model_is_copied_back_after_it_exits(hub, store, tmp_path, monkeypatch, locked, desc):
    status = {"now": "live"}
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: status["now"])
    sid = store.create_session(str(tmp_path), runner="tab", model="claude-sonnet-5-5", effort="high")
    follower = stats.follower(sid)
    snap = follower.snap
    shown = iter([None, "claude-haiku-5-5"])
    follower.read = lambda now=None: (setattr(snap, "requested", next(shown, snap.requested)) or ran_now(snap)
                                      or setattr(follower, "ready", True) or True)
    hub.spawn = lambda work, group=None: work()
    hub.post = lambda work: None
    carry, fails = store.carry, iter([sqlite3.OperationalError("database is locked")] * locked)

    def flaky(*a, **k):
        if (e := next(fails, None)) is not None:
            raise e
        return carry(*a, **k)
    monkeypatch.setattr(store, "carry", flaky)
    hub.refresh()
    hub.read_contexts()   # live: nothing typed yet
    status["now"] = "dead"   # the /model and /exit land between two reads
    hub.refresh()
    for _ in range(3):
        hub.read_contexts()
    assert store.session(sid)["model"] == "claude-haiku-5-5", desc


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


@pytest.mark.parametrize("runner_, shell, desc", [
    ("tab", None, "a tab session"),
    ("sdk", "tab", "a hosted session out in its shell tab"),
])
def test_a_model_typed_while_a_turn_streams_is_carried(hub, store, tmp_path, monkeypatch, runner_, shell, desc):
    """Round 62, M4: a /model typed mid-turn, then /exit. The turn's later responses still
    ask for the old model, but count from when the turn began, before the /model."""
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "live")
    monkeypatch.setattr(transcript, "PROJECTS", tmp_path / "projects")
    sid = store.create_session(str(tmp_path), runner=runner_, model="claude-sonnet-5-5", effort="high")
    if shell:
        store.set_shell(sid, shell)
    folder = tmp_path / "projects" / "-repo"
    folder.mkdir(parents=True)
    t = time.time() + 1

    def response(mid, at):
        return {"type": "assistant", "timestamp": iso(at), "requestedModel": "claude-sonnet-5-5", "effort": "high",
                "message": {"id": mid, "model": "claude-sonnet-5-5", "usage": {"input_tokens": 5, "output_tokens": 1}}}
    recs = [{"type": "user", "timestamp": iso(t), "message": {"role": "user", "content": "go"}}, response("m1", t + 1),
            {"type": "system", "subtype": "local_command", "timestamp": iso(t + 2), "commandRun": {"command": "model"},
             "content": "<local-command-stdout>Set model to `Haiku 5.5` for this session only</local-command-stdout>"},
            response("m2", t + 3)]
    (folder / f"{sid}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
    follower = stats.follower(sid)
    hub.contexts[sid] = follower
    hub.post = lambda work: None
    hub.read_context(follower)
    assert store.session(sid)["model"] == "claude-haiku-5-5", desc


def test_a_copied_back_model_launches(store, tmp_path):
    """What copy_back stores is what a launch passes, and a pinned id the store accepts."""
    sid = store.create_session(str(tmp_path), runner="tab", model="claude-sonnet-5-5", effort="high")
    store.put_back(sid, {"model": "claude-sonnet-5-5"}, model=models.pinned("claude-opus-5-5[1m]"))
    argv = launch.claude_argv(store.session(sid), python="py", resume=True)
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5"
    store.set_model(sid, store.session(sid)["model"])
