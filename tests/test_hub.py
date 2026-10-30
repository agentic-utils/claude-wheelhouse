"""The hub on its own, with no Textual: what it keeps, decides and says."""

import json
import os
import sqlite3
import time
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest

from claude_wheelhouse import fork as fork_mod, host as host_mod, hub as hub_mod, launch, liveness, models, plan_usage, subagents, transcript, transcript_usage, tutorial, whatsnew
from claude_wheelhouse.hub import Hub
from claude_wheelhouse.store import PROTOCOL_VERSION, SessionGone, compacting_now, mode as store_mode

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
    store.mark_version(sid)   # on current code: an older host can't compact (#244)


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
    assert heard == [("demo wasn't relaunched: Relaunch it", "warning")]
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


def test_a_general_message_shows_from_queued_to_taken(hub, store, sid, projects):
    """Queued, then sending from Ctrl+S until the session takes it up, through Claude Code
    holding it, then the person's words in the conversation: never gone in between."""
    path = projects / "projects" / "demo" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("")

    def write(*recs):
        with path.open("a") as f:
            f.writelines(json.dumps(r) + "\n" for r in recs)

    def mine():
        return [md.split("\n\n", 1) for who, md in hub.conversation(sid) if who == "you"]

    store.queue(sid, "ship it")
    assert mine() == [["**you · queued**", "ship it"]]
    store.dispatch(sid)
    assert mine() == [["**you · sending**", "ship it"]], "sent, not yet passed on"
    store.confirm(store.claim(sid))
    turn = "[wheelhouse] from doug (general):\nship it"
    write({"type": "queue-operation", "operation": "enqueue", "content": turn})
    assert mine() == [["**you · sending**", "ship it"]], "passed on, Claude Code holding it"
    write({"type": "attachment", "attachment": {"type": "queued_command", "prompt": turn, "commandMode": "prompt"}},
          {"type": "queue-operation", "operation": "remove", "content": turn})
    assert mine() == [["**you**", "ship it"]], "taken up mid-turn: the person's words"


def test_usage_is_asked_every_five_minutes(hub, monkeypatch):
    now, groups = [1000.0], []
    monkeypatch.setattr(hub_mod.time, "time", lambda: now[0])
    hub.spawn = lambda work, group: groups.append(group)
    for at in (1000.0, 1001.0, 1030.0, 1001.0 + plan_usage.EVERY):   # the first tick only sets the clock
        now[0] = at
        hub.fetch_usage()
    assert groups == ["usage", "usage"]


def queues(store, sid):
    store.db.execute("UPDATE sessions SET code_version = ?", (PROTOCOL_VERSION,))   # on code that queues


@pytest.mark.parametrize("kind, mode, status, now, queued, sent, said, desc", [
    ("question", "queued", "live", False, ["yes"], [], "queued for Q1: Ctrl+S or Send sends the session's queue",
     "queued, by default"),
    ("question", "queued", "dead", False, ["yes"], [], "queued for Q1, but the session isn't running: Ctrl+S "
     "relaunches it and sends its queue", "a dead session's queue waits"),
    ("question", "immediate", "live", False, [], ["yes"], "sent to Q1", "immediate mode sends at once"),
    (None, "queued", "live", False, ["yes"], [], "queued for the session: Ctrl+S or Send sends the session's queue",
     "a general message, to the session"),
    ("permission", "queued", "live", False, [], [], "denied P1, with your message", "a permission is denied, never queued"),
    ("question", "queued", "live", True, [], ["yes"], "sent to Q1", "Alt+S sends at once in queued mode (#124)"),
    ("question", "queued", "dead", True, [], ["yes"], "sent to Q1 (not running: delivered when it's relaunched)",
     "Alt+S to a dead session: it says the answer waits for a relaunch"),
    ("question", "immediate", "dead", False, [], ["yes"], "sent to Q1 (not running: delivered when it's relaunched)",
     "immediate mode to a dead session says the same"),
    ("question", "immediate", "live", True, [], ["yes"], "sent to Q1", "Alt+S in immediate mode is Ctrl+Enter"),
    ("permission", "queued", "live", True, [], [], "denied P1, with your message", "Alt+S on a permission denies too"),
])
def test_submit_queues_or_sends_by_the_sessions_mode(hub, store, sid, heard, monkeypatch, kind, mode, status, now,
                                                     queued, sent, said, desc):
    queues(store, sid)
    store.set_mode(sid, mode)
    monkeypatch.setattr(liveness, "status", lambda s, **kw: status)
    ref = kind and store.post_item(sid, kind, "which db?")
    hub.refresh()
    done = hub.submit((sid, ref), "yes", now)
    assert done.ok, desc
    assert [m["body"] for m in store.drafts(sid)] == queued, desc
    assert [m["body"] for m in store.pending(sid)] == sent, desc
    assert [text for text, _ in told(done)] == [said], desc
    assert heard == [], f"{desc}: said to the surface that gave it, not broadcast"
    assert done.denied == (kind == "permission"), desc
    assert kind != "permission" or store.item(sid, ref)["status"] == "denied", desc


# (mode, what Alt+S answers and the earlier drafts' targets: "Q" a question, "T" another item,
# None the session; what's said after "sent to ...", desc)
@pytest.mark.parametrize("mode, to, earlier, said, desc", [
    ("queued", "Q", ["T"], ["1 answer still queued for demo: Ctrl+S or Send sends it"],
     "queued: another item's draft stays, said in the singular"),
    ("queued", "Q", ["T", None], ["2 answers still queued for demo: Ctrl+S or Send sends them"],
     "queued: two drafts, in the plural"),
    ("queued", "Q", ["Q", "T"], ["2 answers still queued for demo (1 for Q1, which this one has overtaken): "
                                 "Ctrl+S or Send sends them"], "queued: a draft for the same item is overtaken, and said"),
    ("queued", None, [None, "T"], ["2 answers still queued for demo (1 general, which this one has overtaken): "
                                   "Ctrl+S or Send sends them"], "queued: a general message overtakes a general draft"),
    ("queued", "Q", [], [], "queued: nothing else queued, nothing more to say"),
    ("immediate", "Q", ["T"], [], "immediate: as Ctrl+Enter, nothing more to say"),
])
def test_send_now_leaves_the_mode_and_the_rest_of_the_queue(hub, store, sid, heard, monkeypatch, mode, to, earlier,
                                                           said, desc):
    queues(store, sid)
    store.set_mode(sid, mode)
    monkeypatch.setattr(liveness, "status", lambda s, **kw: "live")
    refs = {"Q": store.post_item(sid, "question", "which db?"), "T": store.post_item(sid, "task", "build"), None: None}
    hub.refresh()
    for i, at in enumerate(earlier):
        store.queue(sid, f"earlier {i}", refs[at])
    done = hub.submit((sid, refs[to]), "now", now=True)
    assert [m["body"] for m in store.drafts(sid)] == [f"earlier {i}" for i in range(len(earlier))], desc
    assert [m["body"] for m in store.pending(sid)] == ["now"], desc
    assert store_mode(store.session(sid)) == mode, desc
    assert [text for text, _ in told(done)] == [f"sent to {refs[to] or 'the session'}", *said], desc


def test_a_permission_answered_meanwhile_is_not_submitted(hub, store, sid, heard):
    ref = store.post_item(sid, "permission", "Bash: rm -rf build")
    hub.asking = lambda target: True   # open as the box was submitted, answered by the time it lands
    store.answer_permission(sid, ref, "allow")
    done = hub.submit((sid, ref), "no")
    assert not done.ok and not done.denied
    assert told(done)[-1][1] == "warning"


@pytest.mark.parametrize("status, parked, queued, left, said, desc", [
    ("dead", False, ["a"], ["a"], "demo isn't running: Relaunch it first", "a dead session's queue stays queued (#62)"),
    ("dead", True, ["a"], ["a"], "demo isn't running: Unpark it first", "a parked one's says Unpark, not Relaunch (#218)"),
    ("dead", False, [], [], "nothing queued for that session", "a dead one with nothing queued: nothing to wake it for"),
    ("live", False, ["a"], [], "sent 1 to demo", "a live one's goes, as one message"),
])
def test_send_queue_leaves_a_dead_sessions_queue(hub, store, sid, heard, monkeypatch, status, parked, queued, left,
                                                 said, desc):
    monkeypatch.setattr(liveness, "status", lambda s, **kw: status)
    store.set_parked(sid, parked)
    for body in queued:
        store.queue(sid, body)
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
    (False, None, False, "never", "declined: never offered again"),
    (None, None, False, "reset", "Later: offered at every start, sessions or not"),
    (True, refuse, False, None, "a start that fails is said, and records nothing: offered again"),
    (True, lambda store: store.set_setting(tutorial.OFFER_KEY, "taken"), True, "taken", "taken: it starts"),
])
def test_the_tutorial_offer_answered(hub, store, heard, monkeypatch, take, start, started, setting, desc):
    if start:
        monkeypatch.setattr(tutorial, "start", start)
    monkeypatch.setattr(whatsnew, "FEATURES", (whatsnew.Feature("f", whatsnew.UNRELEASED, whatsnew.ADVANCED, "n"),))
    done = hub.answer_offer(take)
    assert done.ok == started, desc
    assert store.setting(tutorial.OFFER_KEY) == setting, desc
    assert store.tour_seen() == ({"f": "skipped"} if setting == "never" else {}), desc
    assert (len(told(done)), heard) == (0 if started else 1, []), f"{desc}: the tour itself says what's next"


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
def test_compact_first_is_offered(sdk_on, hub, store, sid, monkeypatch, runner_, shell, status, tokens, offered, desc):
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
    if (c := store.compaction(sid)) is not None and c["state"] == "requested":   # the host ends its cycle
        store.move_compaction(sid, c["id"], "completed")


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
    (lambda st, sid: st.move_compaction(sid, st.compaction(sid)["id"], "failed"), False, "its compaction failed",
     "the cycle failed: given up, and said so"),
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


@pytest.mark.parametrize("state, sent, desc", [
    (None, True, "no cycle: Compact is asked for, as the relaunch's"),
    ("requested", False, "Compact pressed, not yet taken: refused, never a second"),
    ("preparing", False, "its notes being written: refused"),
    ("compacting", False, "/compact running: refused"),
    ("completed", True, "the last cycle over: asked for"),
])
def test_compact_first_never_asks_twice(hub, store, sid, monkeypatch, state, sent, desc):
    host(store, sid)
    monkeypatch.setattr(liveness, "is_alive", lambda pid, *a: True)
    if state:
        store.command(sid, "compact")
        store.take_command(sid)
        if state != "requested":
            store.move_compaction(sid, store.compaction(sid)["id"], state)
    hub.refresh()
    done = hub.compact_then_relaunch(sid, (4242, 1))
    assert done.ok == sent, desc
    assert (sid in hub.compacting) == sent, desc
    if sent:
        assert store.compaction(sid)["origin"] == "relaunch", desc
    else:
        assert "is compacting: relaunch once it's done" in done.notices[0].text, desc


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
        follower = transcript_usage.follower(sid)
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
    follower = transcript_usage.follower(sid)
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
    follower = transcript_usage.follower(sid)
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
    follower = transcript_usage.follower(sid)
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


H, M = 3600, 60


def warm_hub(hub, store, sid, snap, status="live"):
    """The hub's view of one tab session: its transcript read as snap, its liveness status."""
    from types import SimpleNamespace
    store.mark_version(sid)
    hub.refresh()
    hub.statuses[sid] = status
    hub.contexts[sid] = SimpleNamespace(ready=True, snap=snap)


def notices(store, sid):
    return [m["body"] for m in store.pending(sid) if m["kind"] == "notice"]


@pytest.mark.parametrize("state, sent, desc", [
    ({}, True, "big, quiet 55 minutes, waiting on an open question: kept warm"),
    ({"since": 49 * M}, False, "49 minutes: not yet"),
    ({"context": 150_000}, False, "at the line, not past it"),
    ({"status": "dead"}, False, "never to a dead session"),
    ({"parked": True}, False, "never to a parked one"),
    ({"stop": "tool_use"}, False, "never mid-turn"),
    ({"compacted": True}, False, "its last turn compacted: nothing to keep warm"),
    ({"waits": False}, False, "nothing waits on the person, nothing in doubt"),
    ({"hours": 0}, False, "0 hours: off"),
    ({"runner": "sdk"}, False, "an SDK session's host keeps its own"),
    ({"old code": True}, False, "a session on older code, which never records its word on an item"),
    ({"heard": "send"}, False, "the person's message since its last response: its turn may be starting"),
    ({"heard": "deliver"}, False, "an answer queued before it, delivered since: the same"),
    ({"heard": "dispatch"}, False, "an answer drafted before it, sent since and not yet delivered: the same"),
])
def test_a_tab_sessions_keepalive_is_sent_under_its_conditions(hub, store, sid, state, sent, desc):
    t0 = time.time()
    if state.get("waits", True):
        store.post_item(sid, "question", "which?")
    if state.get("parked"):
        store.set_parked(sid, True)
    if state.get("runner"):
        store.set_runner(sid, state["runner"])
    hub.keepalive_for = state.get("hours", 8) * H
    if state.get("heard") == "send":
        store.send(sid, "go on")
    elif state.get("heard") == "deliver":   # sent before the last response, delivered after it
        store.send(sid, "go on")
        store.db.execute("UPDATE messages SET created_at = '2000-01-01T00:00:00+00:00' WHERE session_id = ?", (sid,))
        store.take_pending(sid)
    elif state.get("heard") == "dispatch":   # drafted before the last response, sent and claimed after it
        store.queue(sid, "go on")
        store.db.execute("UPDATE messages SET created_at = '2000-01-01T00:00:00+00:00' WHERE session_id = ?", (sid,))
        store.dispatch(sid)
        store.claim(sid)
    if state.get("heard"):
        t0 = time.time() - 30   # the transcript's last response, before the message
    snap = transcript_usage.Snapshot(context=state.get("context", 200_000), last_at=t0, stop=state.get("stop", "end_turn"),
                          compactions=[transcript_usage.Compaction(t0, "manual", 300_000, 50_000)] if state.get("compacted") else [])
    warm_hub(hub, store, sid, snap, state.get("status", "live"))
    if state.get("old code"):
        store.db.execute("UPDATE sessions SET code_version = 10 WHERE id = ?", (sid,))
        hub.refresh()
        hub.statuses[sid] = "live"
    hub.keep_warm(t0 + state.get("since", 55 * M))
    assert notices(store, sid) == ([host_mod.KEEPALIVE_BODY] if sent else []), desc


def test_a_tab_sessions_keepalive_repeats_every_50_minutes_up_to_the_limit(hub, store, sid):
    """Each keepalive's own turn restarts the 50 minutes but not the quiet time; it is the
    check while an item is in doubt."""
    t0 = time.time()
    store.post_item(sid, "question", "which?")
    hub.keepalive_for = 2 * H
    snap = transcript_usage.Snapshot(context=200_000, last_at=t0, stop="end_turn")
    warm_hub(hub, store, sid, snap)
    sent_at = []
    for minute in range(0, 4 * 60):
        at = t0 + minute * M
        hub.keep_warm(at)
        if len(notices(store, sid)) > len(sent_at):
            sent_at.append(minute)
            snap.last_at = at + 5   # the session's "ok"
    assert sent_at == [50, 101], "50 minutes after each last response (the ok, 5 s on), until quiet 2 hours"


def test_a_tab_sessions_check_names_the_items_in_doubt(hub, store, sid, monkeypatch):
    t0 = time.time()
    monkeypatch.setattr(store, "in_doubt", lambda s, at=None: ["T1 waiting"])
    warm_hub(hub, store, sid, transcript_usage.Snapshot(context=200_000, last_at=t0, stop="end_turn"))
    hub.keep_warm(t0 + 55 * M)
    assert notices(store, sid) == [host_mod.keepalive_body(["T1 waiting"])]
    assert notices(store, sid)[0].startswith("Check: T1 waiting.")


def test_a_real_turn_restarts_the_quiet_time(hub, store, sid):
    t0 = time.time()
    store.post_item(sid, "question", "which?")
    hub.keepalive_for = 1 * H
    snap = transcript_usage.Snapshot(context=200_000, last_at=t0, stop="end_turn")
    warm_hub(hub, store, sid, snap)
    hub.keep_warm(t0 + 55 * M)
    snap.last_at = t0 + 56 * M   # the keepalive's ok
    hub.keep_warm(t0 + 57 * M)
    snap.last_at = t0 + 58 * M   # the person's own turn, after the keepalive's ended
    hub.keep_warm(t0 + 59 * M)
    hub.keep_warm(t0 + 113 * M)
    assert len(notices(store, sid)) == 2, "quiet again from 58 minutes: a second keepalive inside the hour"


@pytest.mark.parametrize("what, alive, killed, parked, gone, desc", [
    ("end", True, [4242], 0, True, "forced Forget on a running session: stopped first, then its row deleted"),
    ("park", True, [4242], 1, False, "forced Park: stopped, then parked"),
    ("end", False, [], 0, True, "Forget on one not running: nothing to stop"),
])
def test_force_stops_the_process_then_acts(hub, store, sid, monkeypatch, what, alive, killed, parked, gone, desc):
    store.db.execute("UPDATE sessions SET claude_pid = 4242, claude_start = 1, boot_id = 'b'")
    kills = []
    monkeypatch.setattr(liveness, "is_alive", lambda *a: alive and not kills)
    monkeypatch.setattr(hub_mod.os, "kill", lambda pid, sig: kills.append(pid))
    assert hub.force(sid, what).ok, desc
    assert kills == killed, desc
    s = store.session(sid)
    assert (s is None) is gone and (gone or s["parked"] == parked), desc


@pytest.mark.parametrize("start, verb, to, caption, confirm, desc", [
    ("active", "park", "parked", "Park", False, "Park asks an Active session: it writes its notes, parks and stops"),
    ("active", "end", "forgotten", "Stop & Forget", False, "Stop & Forget asks it: its end steps, then its row goes"),
    ("active", "relaunch", "active", "Relaunch", "busy", "Relaunch stops it and starts it again; asked first if busy"),
    ("available", "park", "parked", "Park", False, "Park on an Available session: parked at once"),
    ("available", "end", "forgotten", "Forget", True, "Forget on an Available session: deleted, once confirmed"),
    ("available", "relaunch", "active", "Relaunch", False, "Relaunch brings an Available session back"),
    ("parked", "end", "forgotten", "Forget", True, "Forget on a Parked session: deleted, once confirmed"),
    ("parked", "relaunch", "active", "Unpark", False, "Unpark is Relaunch, captioned so on a Parked session"),
])
def test_transitions(hub, store, sid, monkeypatch, start, verb, to, caption, confirm, desc):
    assert hub_mod.VERBS[(start, verb)] == (caption, confirm), desc
    store.db.execute("UPDATE sessions SET claude_pid = 4242, claude_start = 1, boot_id = 'b'")
    alive = {"now": start == "active"}

    def set_alive(now):
        return lambda *a, **kw: alive.update(now=now)
    monkeypatch.setattr(liveness, "is_alive", lambda *a: alive["now"])
    monkeypatch.setattr(liveness, "status", lambda s, **kw: "live" if alive["now"] else "dead")
    monkeypatch.setattr(hub_mod.os, "kill", set_alive(False))
    monkeypatch.setattr(launch, "open_session", set_alive(True))
    monkeypatch.setattr(launch, "restore_session", set_alive(True))
    store.set_parked(sid, start == "parked")
    hub.refresh()
    assert hub.state(sid) == start, desc
    if start == "active" and verb != "relaunch":
        assert hub.ask(sid, verb).ok and hub.pending(sid) == verb, desc
        store.end(sid) if verb == "end" else store.set_parked(sid, True)   # the session's own tool call...
        alive["now"] = False                                                # ...which stops it
    elif start == "active":
        assert hub.stop_host(sid, (4242, 1)).ok, desc
        hub.refresh()
        hub.finish_relaunches()
    elif verb == "relaunch":
        assert hub.relaunch(sid).ok, desc
    else:
        assert hub.act_on_dead(sid, verb).ok, desc
    if to == "forgotten":
        assert store.session(sid) is None, desc
    else:
        hub.refresh()
        assert hub.state(sid) == to, desc


@pytest.mark.parametrize("status, parked, want, desc", [
    ("live", 1, 0, "running and parked under the old rules: unparked, so it shows as Active"),
    ("dead", 1, 1, "dead and parked: stays Parked"),
    ("live", 0, 0, "running and not parked: untouched"),
])
def test_the_first_hub_unparks_running_parked_sessions_once(store, sid, monkeypatch, status, parked, want, desc):
    store.set_parked(sid, bool(parked))
    monkeypatch.setattr(liveness, "status", lambda s, **kw: status)
    Hub(store)
    assert store.session(sid)["parked"] == want, desc
    store.set_parked(sid, bool(parked))
    Hub(store)
    assert store.session(sid)["parked"] == parked, f"{desc}: once per database only"


# Fork (fork.py): a new session carrying on from a session's conversation

@pytest.fixture
def opened(monkeypatch):
    """The launches the hub makes, (how, sid, notice), none run."""
    calls = []
    for how in ("tab", "host"):
        monkeypatch.setattr(launch, f"open_{how}", lambda store, sid, notice=True, how=how: calls.append((how, sid, notice)))
    return calls


def forks(store, sid) -> list:
    return [s for s in store.sessions() if s["id"] != sid]


def parent_items(store, sid):
    store.post_item(sid, "task", "todo one")
    store.post_item(sid, "task", "running one", status="running")
    store.post_item(sid, "task", "blocked one", status="blocked")
    store.post_item(sid, "question", "which way?")


@pytest.mark.parametrize("refs, fork_refs, parent_told, desc", [
    ([], [], False, "nothing ticked: the fork starts with no items, the parent isn't told"),
    (["T1"], ["T1"], True, "a todo task moves"),
    (["T3", "Q1"], ["Q1", "T1"], True, "a blocked task and an open question move, numbered afresh there"),
    (["T1", "T1"], ["T1"], True, "a ref given twice moves once, not its stand-in after it"),
])
def test_fork(hub, store, sid, tmp_path, claude_home, opened, heard, refs, fork_refs, parent_told, desc):
    claude_home(sid, str(tmp_path))
    parent_items(store, sid)
    store.queue(sid, "left", "Q1")
    about = []
    hub.listen(lambda e: about.append(e.sid))
    done = hub.fork(sid, refs)
    [f] = forks(store, sid)
    new = f["id"]
    assert done.ok and (f["name"], f["cwd"], f["adopted"], f["runner"]) == ("demo (fork)", str(tmp_path), 1, "tab"), desc
    assert list(claude_home.projects.glob(f"*/{new}.jsonl")), f"{desc}: the transcript copied"
    assert opened == [("tab", new, False)], f"{desc}: launched, without JOINED_TEXT"
    assert sorted(i["ref"] for i in store.items(new)) == fork_refs, desc
    assert [m["kind"] for m in store.pending(new)] == ["notice"] and "a fork of demo" in store.pending(new)[0]["body"], desc
    told = [m["body"] for m in store.pending(sid)]
    assert bool(told) == parent_told, desc
    for old in dict.fromkeys(refs):
        stand_in = store.item(sid, old)
        assert stand_in["status"] in ("dropped", "closed") and stand_in["moved_to"].startswith("demo (fork) as "), desc
        assert f"{old} (its " in told[0], desc
    if "Q1" in refs:
        assert [m["body"] for m in store.thread(new, "Q1")] == ["left"], f"{desc}: its thread moves with it"
    assert store.post_item(sid, "task", "after") == "T4", f"{desc}: the parent's refs stay numbered"
    assert heard[-1][0].startswith("forked demo as demo (fork)"), desc
    assert about == [sid], f"{desc}: said with the parent's id, so its Fork button comes back (#107)"


@pytest.mark.parametrize("dirty, where, desc", [
    (False, "", "a clean worktree"),
    (True, "", "uncommitted changes: the fork is told it won't see them"),
    (False, "kept", "from a committed subdirectory: the same one in the new worktree"),
    (False, "untracked", "from a subdirectory with nothing committed: the new worktree's top, not a missing one"),
])
def test_fork_into_a_new_worktree(hub, store, worktree, claude_home, opened, dirty, where, desc):
    from conftest import git
    main, wt = worktree
    for sub in ("kept", "untracked"):
        (wt / sub).mkdir()
    (wt / "kept/f").write_text("x")
    git(wt, "add", "kept")
    git(wt, "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "kept")
    (wt / "untracked/f").write_text("x")
    (wt / ".gitignore").write_text("untracked/\n.gitignore\n")   # ignored, so the tree stays clean
    if dirty:
        (wt / "wip.txt").write_text("wip")
    cwd = wt / where if where else wt
    sid = store.create_session(str(cwd), name="feat")
    claude_home(sid, str(cwd))
    plan = hub.fork_plan(sid)
    assert (plan.tree.linked, plan.tree.dirty, plan.branch) == (True, dirty, "feat/x-fork"), desc
    assert hub.fork(sid, [], plan.branch).ok, desc
    [f] = forks(store, sid)
    top = main.resolve() / ".worktrees" / "feat-x-fork"
    assert f["cwd"] == str(top / "kept" if where == "kept" else top), desc
    from claude_agent_sdk import project_key_for_directory
    assert list(claude_home.projects.glob(f"*/{f['id']}.jsonl"))[0].parent.name == project_key_for_directory(f["cwd"]), desc
    body = store.pending(f["id"])[0]["body"]
    assert "on a new branch, feat/x-fork" in body and ("regression" in body) == dirty, desc


@pytest.mark.parametrize("refs, branch, may_tab, said, desc", [
    (["T2"], None, True, "T2 can't move", "a running task stays"),
    (["Q9"], None, True, "Q9 can't move", "an item that isn't there"),
    ([], "x-fork", True, "isn't in a linked git worktree", "a new worktree outside one"),
    ([], None, False, "fork it from the daemon's machine", "a tab, for another machine"),
])
def test_fork_refused(hub, store, sid, tmp_path, claude_home, opened, refs, branch, may_tab, said, desc):
    claude_home(sid, str(tmp_path))
    parent_items(store, sid)
    done = hub.fork(sid, refs, branch, may_tab)
    assert not done.ok and said in done.notices[0].text, f"{desc}: {done.notices}"
    assert not forks(store, sid) and not opened, f"{desc}: nothing made"


@pytest.mark.parametrize("fails, said, desc", [
    ("no transcript", "has no conversation to fork yet", "never ran: nothing to copy"),
    ("branch taken", "there's a branch feat/x-fork already", "the branch named exists: refused at once"),
    ("place", "couldn't fork feat: no room", "the copy can't be put in the new worktree's folder"),
    ("parent forgotten", "couldn't fork feat", "the parent forgotten while the copy was made"),
    ("model gone", "couldn't fork feat", "the parent's model no longer offered: the row refused"),
    ("notice fails", "couldn't fork feat: no ink", "a notice failing: the moves go back with it, nothing lost"),
])
def test_a_fork_that_fails_leaves_nothing_behind(store, worktree, claude_home, opened, monkeypatch, fails, said, desc):
    from conftest import git
    main, wt = worktree
    sid = store.create_session(str(wt), name="feat")
    if fails != "no transcript":
        claude_home(sid, str(wt))
    if fails == "branch taken":
        git(main, "branch", "feat/x-fork")
    if fails == "place":
        monkeypatch.setattr(fork_mod, "place", lambda new, cwd: (_ for _ in ()).throw(OSError("no room")))
    if fails == "notice fails":
        monkeypatch.setattr(fork_mod, "opening", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no ink")))
    if fails == "model gone":
        store.db.execute("UPDATE sessions SET model = 'claude-gone-1' WHERE id = ?", (sid,))
    parent_items(store, sid)
    spawn = (lambda work, group: (store.end(sid), work())) if fails == "parent forgotten" else None
    hub = Hub(store, spawn=spawn)
    said_ = []
    hub.listen(lambda e: said_.append((e.text, e.sid)))
    done = hub.fork(sid, ["T1"], "feat/x-fork")
    texts = [n.text for n in done.notices] + [text for text, _ in said_]
    assert any(said in t for t in texts), f"{desc}: {texts}"
    assert all(about == sid for _, about in said_) and bool(said_) == (fails != "branch taken"), \
        f"{desc}: a failure on the worker said with the parent's id, so its Fork button comes back (#107)"
    assert not forks(store, sid) and not opened, f"{desc}: no row, no launch"
    assert sorted(p.stem for p in claude_home.projects.glob("*/*.jsonl")) == ([] if fails == "no transcript" else [sid]), \
        f"{desc}: no copy left"
    assert not (main / ".worktrees/feat-x-fork").exists(), f"{desc}: no worktree left"
    assert bool(git(main, "branch", "--list", "feat/x-fork")) == (fails == "branch taken"), f"{desc}: the branch as it was"
    if fails != "parent forgotten":
        assert store.item(sid, "T1")["status"] == "todo" and not store.item(sid, "T1")["moved_to"], f"{desc}: T1 stays"


@pytest.mark.parametrize("change, status, desc", [
    ("running", "running", "a todo task started meanwhile stays, said"),
    ("done", "done", "one finished meanwhile stays, said"),
])
def test_an_item_changed_while_forking_stays(store, sid, tmp_path, claude_home, opened, change, status, desc):
    claude_home(sid, str(tmp_path))
    parent_items(store, sid)
    hub = Hub(store, spawn=lambda work, group: (store.update_item(sid, "T1", status=change, declares=True), work()))
    heard_ = []
    hub.listen(lambda e: heard_.append((e.text, e.severity)))
    assert hub.fork(sid, ["T1", "Q1"]).ok, desc
    [f] = forks(store, sid)
    assert [i["ref"] for i in store.items(f["id"])] == ["Q1"], f"{desc}: only the question moved"
    assert store.item(sid, "T1")["status"] == status and not store.item(sid, "T1")["moved_to"], desc
    assert heard_[-1] == ("forked demo as demo (fork), 1 item(s) moved to it; T1 changed meanwhile, so stayed",
                          "warning"), desc


def test_the_slow_part_runs_on_a_worker(store, sid, tmp_path, claude_home, opened):
    """git worktree add and the transcript's copy never hold the hub's own thread up."""
    claude_home(sid, str(tmp_path))
    queued, posted = [], []
    hub = Hub(store, spawn=lambda work, group: queued.append((group, work)), post=posted.append)
    done = hub.fork(sid, [])
    assert done.ok and [g for g, _ in queued] == ["fork"] and not forks(store, sid), "said at once, nothing made yet"
    queued[0][1]()
    assert not forks(store, sid) and len(posted) == 1, "the copy made on the worker, the rest posted back"
    posted[0]()
    assert len(forks(store, sid)) == 1 and opened, "made and launched on the hub's thread"


@pytest.mark.parametrize("call, desc", [
    (lambda store, sid: store.update_item(sid, "T1", status="done", declares=True), "update_item"),
    (lambda store, sid: store.reply(sid, "Q1", "x", "open", by_session=True), "reply"),
])
def test_the_parent_cant_act_on_an_item_it_handed_over(hub, store, sid, tmp_path, claude_home, opened, call, desc):
    claude_home(sid, str(tmp_path))
    parent_items(store, sid)
    hub.fork(sid, ["T1", "Q1"])
    with pytest.raises(ValueError, match=r"moved to demo \(fork\) as (T1|Q1): it is no longer this session's"):
        call(store, sid)


@pytest.mark.parametrize("parent, sdk, how, runs, desc", [
    ("tab", False, "tab", "tab", "a tab's fork is a tab"),
    ("sdk", True, "host", "sdk", "a host's, in SDK mode, a host"),
    ("sdk", False, "tab", "tab", "a host's out of SDK mode a tab, as a restore would be"),
    ("tab", True, "tab", "tab", "a tab's stays a tab in SDK mode"),
])
def test_fork_runs_as_its_parent(store, sid, tmp_path, claude_home, opened, parent, sdk, how, runs, desc):
    store.set_runner(sid, parent)
    store.set_model(sid, "claude-opus-5-5")
    claude_home(sid, str(tmp_path))
    Hub(store, sdk=sdk).fork(sid, [])
    [f] = forks(store, sid)
    assert opened == [(how, f["id"], False)] and f["runner"] == runs, desc
    assert f["model"] == "claude-opus-5-5", f"{desc}: with the parent's model"


def test_fork_plan_offers_open_questions_and_todo_and_blocked_tasks(hub, store, sid):
    for kind, status in [("task", "todo"), ("task", "running"), ("task", "blocked"), ("task", "done"),
                         ("question", "open"), ("question", "answered"), ("decision", None)]:
        kw = {"why": "x"} if kind == "decision" else {}
        store.post_item(sid, kind, f"{kind} {status}", status=status, **kw)
    plan = hub.fork_plan(sid)
    assert sorted((ref, status) for ref, _, status, _ in plan.items) == [("Q1", "open"), ("T1", "todo"), ("T3", "blocked")]
    assert (plan.name, plan.tree.linked, plan.branch) == ("demo (fork)", False, "")


@pytest.mark.parametrize("branch, desc", [
    ("feat/x-fork", "the branch's own worktree directory there already"),
    ("feat-x/fork", "a typed name that maps onto it (/ to -)"),
])
def test_fork_refuses_a_worktree_directory_already_there(store, worktree, claude_home, opened, branch, desc):
    from conftest import git
    main, wt = worktree
    (main / ".worktrees/feat-x-fork").mkdir()
    (main / ".worktrees/feat-x-fork/x").write_text("x")
    sid = store.create_session(str(wt), name="feat")
    claude_home(sid, str(wt))
    done = Hub(store).fork(sid, [], branch)
    assert not done.ok and "is there already" in done.notices[0].text, f"{desc}: {done.notices}"
    assert not git(main, "branch", "--list", branch) and not forks(store, sid), f"{desc}: no branch, no fork"


@pytest.mark.parametrize("delivered, left, said, desc", [
    (True, 1, "made, demo (fork)", "made: the log says so after what it made"),
    (False, 0, "dropped, the wheelhouse closing", "the owner closing before the hub's part: taken away, logged"),
])
def test_what_a_fork_makes_is_logged_and_never_left_unsaid(store, sid, tmp_path, claude_home, opened, delivered, left,
                                                           said, desc):
    claude_home(sid, str(tmp_path))
    hub = Hub(store, post=(lambda cb: cb()) if delivered else (lambda cb: False))
    events = []
    hub.listen(events.append)
    assert hub.fork(sid, []).ok, desc
    copies = [p for p in claude_home.projects.glob("*/*.jsonl") if p.stem != sid]
    assert len(copies) == left and len(forks(store, sid)) == left, desc
    log = (store.path.parent / "launch.log").read_text()
    assert "if no line for" in log and said in log, f"{desc}: {log}"
    if delivered:
        assert events[-1].sid == sid, f"{desc}: the announcement names the parent, whose Fork it ends (#107)"


@pytest.mark.parametrize("closing, desc", [
    ("hub", "the owner closing with forked queued but not run: taken away, and forked then does nothing"),
    ("client", "the app quitting (LocalClient.disconnect) does the same"),
])
def test_a_fork_waiting_for_the_hub_is_taken_away_when_its_owner_closes(store, worktree, claude_home, opened, closing,
                                                                       desc):
    from conftest import git
    from claude_wheelhouse.client import LocalClient
    main, wt = worktree
    sid = store.create_session(str(wt), name="feat")
    claude_home(sid, str(wt))
    queued = []
    owner = (Hub if closing == "hub" else LocalClient)(store, post=lambda cb: queued.append(cb) or True)
    assert owner.fork(sid, [], "feat/x-fork").ok and len(queued) == 1, desc
    owner.drop_pending_forks() if closing == "hub" else owner.disconnect()
    queued[0]()   # had it run after all
    assert not forks(store, sid) and not opened, desc
    assert [p.stem for p in claude_home.projects.glob("*/*.jsonl")] == [sid], f"{desc}: no copy left"
    assert not (main / ".worktrees/feat-x-fork").exists() and not git(main, "branch", "--list", "feat/x-fork"), desc
    assert "dropped, the wheelhouse closing" in (store.path.parent / "launch.log").read_text(), desc


def test_a_doc_is_read_again_only_once_its_file_changes(hub, store, sid, tmp_path, monkeypatch):
    doc = tmp_path / "plan.md"
    doc.write_text("# Plan\n")
    ref = store.post_doc(sid, "the plan", str(doc))
    reads = []
    real = os.open
    monkeypatch.setattr(os, "open", lambda p, *a, **k: (reads.append(p), real(p, *a, **k))[1])
    first = hub.document(sid, ref)
    assert (first["text"], first["gone"], first["path"]) == ("# Plan\n", None, str(doc))
    assert hub.document(sid, ref) == first and len(reads) == 1, "unchanged: not read again"
    doc.write_text("# Plan\n\nmore\n")
    assert hub.document(sid, ref)["text"].endswith("more\n") and len(reads) == 2, "changed: read again"
    doc.unlink()
    gone = hub.document(sid, ref)
    assert gone["gone"] == "gone" and gone["text"].endswith("more\n"), "gone: said, with the last text read"
    assert hub.document(sid, store.post_item(sid, "task", "x")) is None, "not a doc"


def test_a_long_doc_is_cut_at_the_cap(hub, store, sid, tmp_path, monkeypatch):
    monkeypatch.setattr("claude_wheelhouse.hub.DOC_CAP", 10)
    (tmp_path / "long.md").write_text("x" * 50)
    text = hub.document(sid, store.post_doc(sid, "long", str(tmp_path / "long.md")))["text"]
    assert text.startswith("x" * 10 + "\n") and "only the first" in text


def test_a_doc_swapped_for_a_fifo_is_never_opened(hub, store, sid, tmp_path):
    doc = tmp_path / "plan.md"
    doc.write_text("# Plan\n")
    ref = store.post_doc(sid, "the plan", str(doc))
    doc.unlink()
    os.mkfifo(doc)   # opened for reading, it would block until a writer came
    assert hub.document(sid, ref) == {"path": str(doc), "text": "", "stamp": "not a file", "gone": "not a file"}


@pytest.mark.parametrize("do, server, fanned, desc", [
    ("reconnect", "claude.ai Slack", ["b"], "a connector: every other running hosted session that has it"),
    ("signin", "claude.ai Slack", [], "a sign-in: this session alone, its link is its own"),
    ("reconnect", "github", [], "a local server: this session's alone"),
    ("check", "", [], "a fresh look"),
])
def test_mcp_press_goes_to_the_host_and_a_connector_reconnects_everywhere(hub, store, tmp_path, monkeypatch,
                                                                          do, server, fanned, desc):
    seen = [{"name": "claude.ai Slack", "status": "needs-auth", "kind": "claudeai-proxy"},
            {"name": "github", "status": "failed", "kind": "stdio"}]
    sids = {}
    for name, runner_, has in (("a", "sdk", True), ("b", "sdk", True), ("c", "tab", True), ("d", "sdk", False),
                               ("e", "sdk", True)):
        sids[name] = store.create_session(str(tmp_path), name=name, runner=runner_)
        if has:
            store.set_mcp_status(sids[name], seen)
    monkeypatch.setattr(liveness, "status", lambda s, **kw: "dead" if s["name"] == "e" else "live")
    hub.refresh()
    assert hub.mcp(sids["a"], do, server).ok, desc
    assert store.take_mcp_asks(sids["a"]) == [{"do": do, "server": server}], desc
    assert [n for n in "bcde" if store.take_mcp_asks(sids[n])] == fanned, desc


def test_mcp_press_on_a_session_not_hosted_says_so(hub, store, sid, monkeypatch):
    monkeypatch.setattr(liveness, "status", lambda s, **kw: "live")
    hub.refresh()
    done = hub.mcp(sid, "reconnect", "github")
    assert not done.ok and store.take_mcp_asks(sid) == []


def test_a_pasted_sign_in_address_is_never_served(store, sid):
    from claude_wheelhouse.daemon import Daemon
    store.ask_mcp(sid, "callback", "local-oauth", url="http://localhost/cb?code=SECRET")
    hub = Hub(store)
    hub.refresh()
    view = Daemon.session_view(SimpleNamespace(hub=hub), hub.listed(sid))
    assert "mcp_asks" not in view and "SECRET" not in json.dumps(view, default=str)


@pytest.mark.parametrize("parked, alt_s, said, desc", [
    (True, True, "unparking demo: 3 sent with it", "Alt+S on a parked session: unparked, the answer after the queue"),
    (False, True, "relaunching demo: 3 sent with it", "Alt+S on a dead one: relaunched, the same"),
    (True, False, "unparking demo: 2 sent with it", "Ctrl+S on a parked one: its queue alone"),
    (False, False, "relaunching demo: 2 sent with it", "Ctrl+S on a dead one: the same"),
])
def test_wake_and_send_makes_the_queue_its_first_prompt(hub, store, sid, monkeypatch, parked, alt_s, said, desc):
    """#221: the wheelhouse's resume notice first, then the person's words, each once, in the
    one turn the woken session's host types, and the queue emptied."""
    monkeypatch.setattr(liveness, "status", lambda s, **kw: "dead")
    monkeypatch.setattr(launch, "restore_session", lambda st, i, **kw: st.notice(i, "RESUME CONTEXT"))
    store.set_parked(sid, parked)
    q = store.post_item(sid, "question", "which db?")
    store.queue(sid, "first", q)
    store.queue(sid, "second")
    hub.refresh()
    done = hub.wake_and_send(sid, q, "ship it" if alt_s else None)
    assert [t for t, _ in told(done)] == [said], desc
    assert store.drafts(sid) == [], f"{desc}: the queue emptied"
    assert store.session(sid)["parked"] == 0, f"{desc}: unparked"
    turn = host_mod.format_turn(store.claim(sid, mcp="with"), "doug")
    words = ["RESUME CONTEXT", "first", "second"] + (["ship it"] if alt_s else [])
    assert [turn.index(w) for w in words] == sorted(turn.index(w) for w in words), f"{desc}: in order, {turn}"
    assert all(turn.count(w) == 1 for w in words), f"{desc}: each once, {turn}"


def test_wake_and_send_that_fails_sends_nothing(hub, store, sid, monkeypatch):
    def refuse(*a, **kw):
        raise RuntimeError("already running")
    monkeypatch.setattr(liveness, "status", lambda s, **kw: "dead")
    monkeypatch.setattr(launch, "restore_session", refuse)
    store.set_parked(sid, True)
    store.queue(sid, "first")
    hub.refresh()
    done = hub.wake_and_send(sid, None, "ship it")
    assert not done.ok
    assert [m["body"] for m in store.drafts(sid)] == ["first"], "the queue stays queued"
    assert store.pending(sid) == [], "nothing sent, the answer kept in the box"
    assert store.session(sid)["parked"] == 1


# the compaction cycle (#244)

def in_cycle(store, sid, state="preparing"):
    store.command(sid, "compact")
    store.take_command(sid)
    store.move_compaction(sid, store.compaction(sid)["id"], state)


@pytest.mark.parametrize("act, desc", [
    (lambda h, sid: h.submit((sid, None), "hi"), "a queued answer"),
    (lambda h, sid: h.submit((sid, None), "hi", now=True), "Alt+S"),
    (lambda h, sid: h.send_queue(sid), "Ctrl+S"),
    (lambda h, sid: h.host_command(sid, "compact"), "Compact again"),
    (lambda h, sid: h.ask(sid, "park"), "Park"),
    (lambda h, sid: h.ask(sid, "end"), "Stop & Forget, Let it know"),
    (lambda h, sid: h.force(sid, "park"), "Park now"),
    (lambda h, sid: h.unpark(sid), "Unpark"),
    (lambda h, sid: h.stop_host(sid, (4242, 1)), "Relaunch"),
    (lambda h, sid: h.compact_then_relaunch(sid, (4242, 1)), "Relaunch, compact first"),
])
def test_a_refused_action_is_said_as_one_warning(hub, store, sid, monkeypatch, act, desc):
    host(store, sid)
    monkeypatch.setattr(liveness, "is_alive", lambda pid, *a: True)
    monkeypatch.setattr(liveness, "status", lambda s, **kw: "live")
    store.queue(sid, "queued before")
    in_cycle(store, sid)
    hub.refresh()
    done = act(hub, sid)
    assert not done.ok and done.notices[0].severity == "warning", desc
    assert "demo is compacting: " in done.notices[0].text and "or Interrupt to cancel the compaction." in \
        done.notices[0].text, desc
    assert len(store.drafts(sid)) == 1 and not store.pending(sid), f"{desc}: the queue as it was"


def test_interrupt_and_just_stop_it_go_during_a_cycle(hub, store, sid, monkeypatch):
    host(store, sid)
    monkeypatch.setattr(liveness, "is_alive", lambda pid, *a: True)
    monkeypatch.setattr(liveness, "status", lambda s, **kw: "live")
    in_cycle(store, sid)
    hub.refresh()
    assert hub.host_command(sid, "interrupt").ok
    monkeypatch.setattr(Hub, "stop_process", staticmethod(lambda s: True))
    assert hub.force(sid, "end").ok and store.session(sid) is None, "Just stop it: the session, cycle and all"


def test_send_all_skips_a_compacting_session_and_names_it(hub, store, sid, tmp_path, monkeypatch):
    other = store.create_session(str(tmp_path), name="other")
    monkeypatch.setattr(liveness, "status", lambda s, **kw: "live")
    for s in (sid, other):
        store.queue(s, "go")
    in_cycle(store, sid)
    hub.refresh()
    done = hub.send_all()
    assert done.ok and [m["session_id"] for m in store.drafts()] == [sid]
    assert any("demo is compacting" in n.text and n.severity == "warning" for n in done.notices), done.notices


def test_an_answer_to_a_waiting_permission_goes_at_once_whatever_the_mode(hub, store, sid):
    ref = store.post_item(sid, "question", "which?")
    store.set_answer(sid, ref, '{"from": "AskUserQuestion"}')
    in_cycle(store, sid)
    assert hub.submit((sid, ref), "the first").ok
    assert not store.drafts(sid) and store.pending(sid), "sent, never queued: queueing is refused"


@pytest.mark.parametrize("state, status, ended, desc", [
    ("preparing", "dead", True, "its host died hard: failed by the sweep"),
    ("compacting", "dead", True, "dead mid-/compact: failed"),
    ("preparing", "live", False, "running: the host's to end"),
    ("compacting", "stalled", True, "its host wedged (no heartbeat): failed, so Relaunch is offered again"),
])
def test_the_sweep_ends_a_dead_sessions_cycle(hub, store, sid, monkeypatch, state, status, ended, desc):
    in_cycle(store, sid, state)
    monkeypatch.setattr(liveness, "status", lambda s, **kw: status)
    hub.refresh()
    assert store.compaction(sid)["state"] == ("failed" if ended else state), desc


@pytest.mark.parametrize("abort, command, desc", [
    (False, None, "Keep waiting: closed, nothing interrupted"),
    (True, "interrupt", "Abort: the notes turn interrupted, which cancels the cycle"),
])
def test_notes_wait_buttons(hub, store, sid, abort, command, desc):
    from claude_wheelhouse.store import NOTES_WAIT
    in_cycle(store, sid)
    ref = store.post_marked_question(sid, "keep waiting?", "", NOTES_WAIT)
    assert hub.notes_wait(sid, ref, abort).ok, desc
    assert store.item(sid, ref)["status"] == "closed" and store.session(sid)["host_command"] == command, desc
    assert not hub.notes_wait(sid, ref, abort).ok, f"{desc}: once"


@pytest.mark.parametrize("act, desc", [
    (lambda h, sid: h.host_command(sid, "compact"), "Compact: refused, its host would never end the cycle"),
    (lambda h, sid: h.compact_then_relaunch(sid, (4242, 1)), "Relaunch, compact first: relaunched without compacting"),
])
def test_an_older_host_is_never_given_a_cycle(hub, store, sid, monkeypatch, act, desc):
    host(store, sid)
    store.db.execute("UPDATE sessions SET code_version = 23")
    monkeypatch.setattr(liveness, "is_alive", lambda pid, *a: True)
    monkeypatch.setattr(liveness, "status", lambda s, **kw: "live")
    monkeypatch.setattr(hub_mod.os, "kill", lambda *a: None)
    hub.refresh()
    done = act(hub, sid)
    assert store.compaction(sid)["state"] is None and not compacting_now(store.session(sid)), desc
    assert any("older wheelhouse code" in n.text for n in done.notices), f"{desc}: {done.notices}"


@pytest.mark.parametrize("runner_, shell, desc", [("tab", None, "a terminal tab"), ("sdk", "tab", "handed to a shell tab")])
def test_park_with_compact_first_is_refused_in_a_tab(hub, store, sid, monkeypatch, runner_, shell, desc):
    store.set_runner(sid, runner_)
    store.db.execute("UPDATE sessions SET shell = ?", (shell,))
    done = hub.ask(sid, "park", compact=True)
    assert not done.ok and "compaction isn't offered" in done.notices[0].text, desc
    assert store.session(sid)["park_requested_at"] is None and store.compaction(sid)["state"] is None, desc


@pytest.mark.parametrize("origin, age, state, desc", [
    ("person", 30, "requested", "pressed 30 s ago, not yet claimed: within CLAIM_WITHIN"),
    ("person", 61, "failed", "unclaimed past CLAIM_WITHIN (an older or wedged host): failed, unlocked"),
    ("park", 61, "requested", "Park's waits for its park, an hour, not CLAIM_WITHIN"),
    ("park", host_mod.PARK_COMPACT_WITHIN + 1, "failed", "Park's past its own deadline: failed"),
])
def test_an_unclaimed_cycle_has_a_deadline(hub, store, sid, monkeypatch, origin, age, state, desc):
    from datetime import datetime, timedelta, timezone
    host(store, sid)
    monkeypatch.setattr(liveness, "status", lambda s, **kw: "live")
    store.queue(sid, "queued")
    if origin == "park":
        store.request(sid, "park", True)
    else:
        store.command(sid, "compact")
    at = (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat(timespec="microseconds")
    store.db.execute("UPDATE sessions SET compaction_at = ?", (at,))
    hub.refresh()
    assert store.compaction(sid)["state"] == state, desc
    assert len(store.drafts(sid)) == 1, f"{desc}: drafts stay queued"


def test_a_refresh_with_no_stale_cycle_takes_no_write(hub, store, sid, monkeypatch):
    monkeypatch.setattr(liveness, "status", lambda s, **kw: "live")
    store.command(sid, "compact")
    monkeypatch.setattr(store, "expire_requested", lambda *a: pytest.fail("a write taken on a fresh cycle"))
    monkeypatch.setattr(store, "move_compaction", lambda *a: pytest.fail("a write taken on a live session"))
    hub.refresh()


@pytest.mark.parametrize("in_cycle, queued, desc", [
    (False, True, "no cycle: an answer to a waiting question queues in Queued mode, as before"),
    (True, False, "during a cycle: sent at once, as queueing is refused"),
])
def test_a_waiting_calls_answer_goes_at_once_only_during_a_cycle(hub, store, sid, in_cycle, queued, desc):
    ref = store.post_item(sid, "question", "which?")
    store.set_answer(sid, ref, '{"from": "AskUserQuestion"}')
    if in_cycle:
        store.command(sid, "compact")
    assert hub.submit((sid, ref), "the first").ok, desc
    assert bool(store.drafts(sid)) == queued and bool(store.pending(sid)) == (not queued), desc


def test_compaction_entries_place_stage_lines_and_the_crew(store, sid):
    """#244: each stage's art and its dim detail, and while it runs the crew's excerpts, seeded by
    the cycle so every refresh shows the same, one at each stage change."""
    from datetime import datetime, timezone
    from claude_wheelhouse.hub import compaction_entries
    store.command(sid, "compact")
    cid = store.compaction(sid)["id"]
    store.move_compaction(sid, cid, "preparing")
    t0 = datetime.fromisoformat(store.stages(sid)[-1]["at"]).timestamp()
    stages = store.stages(sid)
    later = compaction_entries(stages, True, t0 + 65)
    assert later == compaction_entries(stages, True, t0 + 65), "the same at every refresh"
    whos = [e.who for e in later]
    assert whos.count("stage") == 1 and 3 <= whos.count("crew") <= 4, whos   # requested shows no line
    assert later[0].text.startswith("▐█          █▌ walls closing in\n"), later[0].text
    assert all(e.at <= datetime.fromtimestamp(t0 + 65, timezone.utc).isoformat() for e in later)
    store.move_compaction(sid, cid, "compacting")
    store.move_compaction(sid, cid, "completed", "220k → 40k")
    ended = datetime.fromisoformat(store.stages(sid)[-1]["at"]).timestamp()
    done = compaction_entries(store.stages(sid), False, t0 + 3600)
    last = [e for e in done if e.who == "stage"][-1]
    assert last.text.startswith("▐████████████▌ compacted\n220k → 40k · "), last
    assert all(datetime.fromisoformat(e.at).timestamp() <= ended for e in done if e.who == "crew"), \
        "no crew once it has ended, however long after"


@pytest.mark.parametrize("status, why", [("dead", "its host stopped"), ("stalled", "its host stalled")])
def test_the_sweep_says_why_on_the_task(hub, store, sid, monkeypatch, status, why):
    store.command(sid, "compact")
    store.take_command(sid)
    store.move_compaction(sid, store.compaction(sid)["id"], "preparing")
    monkeypatch.setattr(liveness, "status", lambda s, **kw: status)
    hub.refresh()
    ref = store.compaction(sid)["item"]
    assert store.item(sid, ref)["status"] == "blocked"
    assert store.thread(sid, ref)[-1]["body"] == f"failed: {why}"


def test_nothing_is_sent_on_a_compaction_task_and_fork_never_moves_it(hub, store, sid):
    store.command(sid, "compact")
    ref = store.compaction(sid)["item"]
    store.move_compaction(sid, store.compaction(sid)["id"], "failed", "its host stopped")   # blocked: MOVABLE's
    done = hub.submit((sid, ref), "why?", now=True)
    assert not done.ok and "the wheelhouse's own record of a compaction" in done.notices[0].text
    assert not store.pending(sid) and not store.drafts(sid)
    assert ref not in [r[0] for r in hub.fork_plan(sid).items], "Fork never offers it"


def test_a_wake_never_sends_on_a_compaction_task(hub, store, sid, opened):
    store.command(sid, "compact")
    ref = store.compaction(sid)["item"]
    store.move_compaction(sid, store.compaction(sid)["id"], "failed", "its host stopped")
    done = hub.wake_and_send(sid, ref, "why did it fail?")
    assert not done.ok and "own record of a compaction" in done.notices[0].text
    assert not opened and not store.pending(sid), "not woken, nothing sent"



@pytest.mark.parametrize("records, start_exists, want_dir, said, desc", [
    ([{"type": "user", "cwd": "WT"}], True, "WT", None, "the transcript's newest cwd: a worktree it cd'd into"),
    ([{"type": "user", "cwd": "START"}, {"type": "assistant", "cwd": "WT"}, {"type": "x"}], True, "WT", None,
     "the newest record that has one"),
    (None, True, "START", None, "no transcript: where it started"),
    ([{"type": "user", "cwd": "GONE"}], True, "HOME", "GONE is gone", "its directory gone: home, said"),
    (None, False, "HOME", "START is gone", "where it started gone too"),
    ("none", True, "HOME", None, "no session: home, nothing said"),
])
def test_open_shell_picks_the_sessions_live_directory(store, projects, monkeypatch, records, start_exists, want_dir,
                                                      said, desc):
    """#257: Shell opens where the session works now, else home with a notice naming the one gone."""
    root = projects / "dirs"
    paths = {"START": root / "start", "WT": root / "wt", "GONE": root / "gone", "HOME": root / "home"}
    for name in ("WT", "HOME") + (("START",) if start_exists else ()):
        paths[name].mkdir(parents=True)
    monkeypatch.setenv("HOME", str(paths["HOME"]))
    sid = store.create_session(str(paths["START"]), name="demo")
    if isinstance(records, list):
        folder = projects / "projects" / "p"
        folder.mkdir(parents=True)
        folder.joinpath(f"{sid}.jsonl").write_text(
            "".join(json.dumps({**r, **({"cwd": str(paths[r["cwd"]])} if "cwd" in r else {})}) + "\n" for r in records))
    opened = []
    monkeypatch.setattr(launch, "open_shell", lambda st, cwd, title, **kw: opened.append((cwd, title)))
    done = Hub(store).open_shell(None if records == "none" else sid)
    assert done.ok and opened == [(str(paths[want_dir]), "shell" if records == "none" else "demo")], desc
    warnings = [e.text.replace(str(root) + "/", "") for e in done.notices if e.severity == "warning"]
    assert (warnings[0].upper().startswith(said.split()[0]) if said else not warnings), f"{desc}: {warnings}"


@pytest.mark.parametrize("mode_, parked, compacting, want, desc", [
    ("immediate", False, False, "sent", "Immediate: sent"),
    ("queued", False, False, "queued", "Queued: queued for Send"),
    ("immediate", True, False, "queued", "parked: queued, for the resume"),
    ("immediate", False, True, "Compacting", "compacting: refused, the caller waits"),
])
def test_send_or_queue(store, sid, mode_, parked, compacting, want, desc):
    """#257: a run's output goes as an answer would; the one place #249's busy rule joins."""
    ref = store.post_command(sid, "ls", "List")[0]
    store.db.execute("UPDATE sessions SET send_mode = ?, parked = ?", (mode_, int(parked)))
    if compacting:
        store.db.execute("UPDATE sessions SET compaction = 'compacting', compaction_origin = 'person'")
    if want == "Compacting":
        with pytest.raises(hub_mod.Compacting):
            hub_mod.send_or_queue(store, sid, ref, "out")
        return
    assert hub_mod.send_or_queue(store, sid, ref, "out") == want, desc
    queued = [m["body"] for m in store.drafts(sid)]
    assert queued == (["out"] if want == "queued" else []), desc


@pytest.mark.parametrize("text, send, gone, ok, note, desc", [
    ("ls", False, False, True, "ran", "as suggested, unticked: run, noted"),
    ("ls -l", False, False, True, "ran, edited", "edited: noted so"),
    ("ls", True, False, True, None, "ticked: run, the output its record"),
    ("ls", False, True, False, None, "its directory gone: refused, nothing run"),
])
def test_run_command(store, projects, monkeypatch, text, send, gone, ok, note, desc):
    """#257: Run writes a 0600 file and opens a tab whose command line carries only the
    database's path and the key; refused rather than run elsewhere when the directory's gone."""
    work = projects / "work"
    work.mkdir()
    sid = store.create_session(str(work), name="demo")
    ref = store.post_command(sid, "ls", "List")[0]
    if gone:
        work.rmdir()
    opened = []
    monkeypatch.setattr(launch, "open_shell", lambda st, cwd, title, command=None, note="": opened.append((cwd, command)))
    done = Hub(store).run_command(sid, ref, text, send)
    assert done.ok == ok, desc
    item = store.item(sid, ref)
    assert item["status"] == ("run" if ok else "new"), desc
    notes = [m["body"] for m in store.thread(sid, ref) if m["kind"] == "run"]
    assert [n.split(" ", 2)[0] + (", edited" if n.endswith("edited") else "") for n in notes] == \
        ([note] if note else []), desc
    if not ok:
        assert opened == [] and "gone" in done.notices[0].text, desc
        return
    (cwd, command), = opened
    assert cwd == str(work) and command[2:5] == ["claude_wheelhouse", "shell", "--db"], desc
    assert command[5] == str(store.path), f"{desc}: the database, a scratch one too"
    payload = json.loads((store.path.parent / "shell" / command[6]).read_text())
    assert (payload["text"], payload["want"], payload["edited"]) == (text, send, text != "ls"), desc


def test_close_and_reopen_a_command(store, sid):
    ref = store.post_command(sid, "ls", "List")[0]
    hub = Hub(store)
    hub.close(sid, ref, True)
    assert store.item(sid, ref)["status"] == "closed"
    hub.close(sid, ref, False)
    assert store.item(sid, ref)["status"] == "new"
