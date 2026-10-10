"""The wheelhouse's own tracking of a session's subagents (#69), from fixture transcripts
built in the shapes Claude Code writes (meta.json, Agent tool_use and tool_result, task
notifications in their three delivery forms)."""

import builtins
import json
import os
import time
from datetime import datetime, timezone

import pytest

from claude_wheelhouse import subagents, transcript
from claude_wheelhouse.store import AGENTS_SINCE, Store
from claude_wheelhouse.subagents import AgentWatcher

NOW = time.time()
JOINED = NOW - 3600   # the session joined the wheelhouse an hour ago


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


class Session:
    """A session's transcript folder: its main transcript and subagents/ beside it."""

    def __init__(self, projects, sid):
        self.sid = sid
        self.main = projects / "-home-me" / f"{sid}.jsonl"
        self.folder = projects / "-home-me" / sid / "subagents"
        self.folder.mkdir(parents=True)
        self.main.write_text("")

    def agent(self, aid, desc, tool=None, shape="background", started=NOW - 600, active=None, **meta):
        """meta.json as Claude Code writes it at launch, and the subagent's first record."""
        tool = tool or f"toolu_{aid}"
        d = {"agentType": "general-purpose", "description": desc, "toolUseId": tool, "spawnDepth": 1,
             "requestShape": shape, "requestNonInteractive": True, **meta}
        path = self.folder / f"agent-{aid}.meta.json"
        path.write_text(json.dumps({k: v for k, v in d.items() if v is not None}))
        log = self.folder / f"agent-{aid}.jsonl"
        log.write_text(json.dumps({"parentUuid": None, "isSidechain": True, "agentId": aid, "type": "user",
                                   "message": {"role": "user", "content": "the brief"},
                                   "timestamp": iso(started)}) + "\n")
        for p in (path, log):
            os.utime(p, (active or started, active or started))
        return tool

    def write(self, *recs):
        with open(self.main, "a") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")


def tool_use(tool, desc, background=True, at=NOW - 600):
    inp = {"description": desc, "subagent_type": "general-purpose", "prompt": "the brief"}
    if background:
        inp["run_in_background"] = True
    return {"isSidechain": False, "type": "assistant", "timestamp": iso(at),
            "message": {"role": "assistant", "content": [{"type": "tool_use", "id": tool, "name": "Agent", "input": inp}]}}


def launched(tool, aid):
    text = (f"Async agent launched successfully.\nagentId: {aid} (internal ID)\nThe agent is working in the "
            "background. You will be notified automatically when it completes.")
    return {"isSidechain": False, "type": "user", "timestamp": iso(NOW - 599),
            "message": {"role": "user", "content": [{"tool_use_id": tool, "type": "tool_result",
                                                     "content": [{"type": "text", "text": text}]}]},
            "toolUseResult": {"isAsync": True, "status": "async_launched", "agentId": aid, "description": "x"}}


def fg_result(tool, aid, error=None):
    if error:
        return {"isSidechain": False, "type": "user", "timestamp": iso(NOW - 300),
                "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool, "is_error": True,
                                                         "content": error}]},
                "toolUseResult": f"Error: {error}"}
    text = f"This agent's report was delivered to you.\nagentId: {aid}\n<usage>subagent_tokens: 56530</usage>"
    return {"isSidechain": False, "type": "user", "timestamp": iso(NOW - 300),
            "message": {"role": "user", "content": [{"tool_use_id": tool, "type": "tool_result",
                                                     "content": [{"type": "text", "text": text}]}]},
            "toolUseResult": {"status": "completed", "agentId": aid, "agentType": "general-purpose"}}


def note_text(aid, tool, status, summary):
    use = f"<tool-use-id>{tool}</tool-use-id>\n" if tool else ""
    return (f"<task-notification>\n<task-id>{aid}</task-id>\n{use}<output-file>/tmp/{aid}.output</output-file>\n"
            f"<status>{status}</status>\n<summary>{summary}</summary>\n<note>A task-notification fires each time "
            "this agent stops.</note>\n</task-notification>")


def notified(aid, tool, status, summary="Agent finished", form="enqueue"):
    text = note_text(aid, tool, status, summary)
    if form == "enqueue":
        return {"type": "queue-operation", "operation": "enqueue", "timestamp": iso(NOW - 120), "content": text}
    if form == "attachment":
        return {"isSidechain": False, "type": "attachment", "timestamp": iso(NOW - 120),
                "attachment": {"type": "queued_command", "prompt": text, "commandMode": "task-notification"}}
    return {"isSidechain": False, "type": "user", "timestamp": iso(NOW - 120), "message": {"role": "user", "content": text}}


def quoted(aid, tool):
    """A notification's text in a message and in tool output: finishes nothing."""
    text = note_text(aid, tool, "completed", "Agent finished")
    return [{"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}},
            {"type": "user", "message": {"role": "user", "content": [
                {"tool_use_id": "toolu_bash", "type": "tool_result", "content": text}]}}]


def send(aid):
    return {"isSidechain": False, "type": "assistant", "timestamp": iso(NOW - 60),
            "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_send", "name": "SendMessage",
                                                          "input": {"to": aid, "summary": "more", "message": "and"}}]}}


@pytest.fixture
def projects(tmp_path, monkeypatch):
    monkeypatch.setattr(transcript, "PROJECTS", tmp_path / "projects")
    return tmp_path / "projects"


@pytest.fixture
def joined(store, sid, projects):
    """The session, joined the wheelhouse an hour ago, after the wheelhouse first ran this code."""
    store.db.execute("UPDATE sessions SET created_at = ?", (iso(JOINED).replace("Z", "+00:00"),))
    store.set_setting(AGENTS_SINCE, iso(JOINED - 86400).replace("Z", "+00:00"))
    return Session(projects, sid)


def agents(store, sid):
    return {r["title"]: r for r in store.items(sid) if r["kind"] == "agent"}


def notes(store, sid, ref):
    return [m["body"] for m in store.messages(sid) if m["item_ref"] == ref and m["kind"] == "note"]


def fg_ok(s):
    t = s.agent("a1", "Check pages", shape="foreground")
    s.write(tool_use(t, "Check pages", background=False), fg_result(t, "a1"))


def fg_failed(s):
    t = s.agent("a1", "Check pages", shape="foreground")
    s.write(tool_use(t, "Check pages", background=False), fg_result(t, "a1", error="API Error: overloaded"))


def bg_running(s):
    t = s.agent("a1", "Check pages")
    s.write(tool_use(t, "Check pages"), launched(t, "a1"))


def bg_running_no_result(s):
    t = s.agent("a1", "Check pages", shape=None)
    rec = launched(t, "a1")
    del rec["toolUseResult"]
    s.write(tool_use(t, "Check pages"), rec)


def bg_done(s):
    t = s.agent("a1", "Check pages")
    s.write(tool_use(t, "Check pages"), launched(t, "a1"), notified("a1", t, "completed"))


def bg_failed(s):
    t = s.agent("a1", "Check pages")
    s.write(tool_use(t, "Check pages"), launched(t, "a1"),
            notified("a1", t, "failed", 'Agent "Check pages" failed: API Error (EAI_AGAIN)', form="user"))


def bg_killed(s):
    t = s.agent("a1", "Check pages", stoppedByUser=True)
    s.write(tool_use(t, "Check pages"), launched(t, "a1"),
            notified("a1", t, "killed", 'Agent "Check pages" was stopped by user', form="attachment"))


def bg_stopped(s):
    t = s.agent("a1", "Check pages")
    s.write(tool_use(t, "Check pages"), launched(t, "a1"),
            notified("a1", None, "stopped", "Background agent \"Check pages\" didn't finish before the previous "
                     "session ended", form="user"))


def bg_resumed(s):
    t = s.agent("a1", "Check pages")
    s.write(tool_use(t, "Check pages"), launched(t, "a1"), notified("a1", t, "completed"), send("a1"))


def bg_quoted(s):
    t = s.agent("a1", "Check pages")
    s.write(tool_use(t, "Check pages"), launched(t, "a1"), *quoted("a1", t))


def nested(s):
    t = s.agent("a1", "Check pages", parentAgentId="a0", spawnDepth=2)
    s.write(notified("a1", t, "completed"))


def forked_skill(s):
    s.agent("a1", "/two-slack Read replies", tool=None, name="two-slack")
    s.write(notified("a1", "toolu_skill", "completed"))


def just_started(s):
    t = s.agent("a1", "Check pages", started=NOW - 5)
    s.write(tool_use(t, "Check pages", at=NOW - 5), launched(t, "a1"))


@pytest.mark.parametrize("build, status, note, desc", [
    (fg_ok, "done", None, "a foreground subagent's tool_result finishes it done"),
    (fg_failed, "failed", "API Error: overloaded", "a foreground tool_result with is_error fails it, saying why"),
    (bg_running, "running", None, "a background launch's own tool_result finishes nothing"),
    (bg_running_no_result, "running", None, "a launch known only by its text finishes nothing either"),
    (bg_done, "done", None, "a background subagent's completed notification (queued) finishes it done"),
    (bg_failed, "failed", "EAI_AGAIN", "a failed notification (delivered as a user turn) fails it, with its summary"),
    (bg_killed, "failed", "stopped by user", "a killed notification (the person stopped it) fails it"),
    (bg_stopped, "failed", "didn't finish", "a stopped notification with only the task id fails it"),
    (bg_resumed, "running", None, "SendMessage to a finished subagent resumes it"),
    (bg_quoted, "running", None, "a notification quoted in a message or tool output finishes nothing"),
    (nested, None, None, "a subagent's own subagents get no item"),
    (forked_skill, "done", None, "a forked skill (no tool call id) finishes by its task id"),
    (just_started, None, None, "no item within GRACE of the start, for the session's own to arrive"),
])
def test_a_subagents_item_follows_its_transcript(store, sid, joined, build, status, note, desc):
    build(joined)
    AgentWatcher(sid).sync(store, NOW)
    items = list(agents(store, sid).values())
    assert [i["status"] for i in items] == ([status] if status else []), desc
    if status:
        assert items[0]["title"].endswith("Check pages") or items[0]["title"].startswith("/two-slack"), desc
        said = notes(store, sid, items[0]["ref"])
        assert (note in said[0] if note else said == []), f"{desc}: {said}"


def test_a_finish_appended_later_is_read_from_where_the_last_read_stopped(store, sid, joined):
    t = joined.agent("a1", "Check pages")
    joined.write(tool_use(t, "Check pages"), launched(t, "a1"))
    w = AgentWatcher(sid)
    assert w.sync(store, NOW), "the item is made"
    assert not w.sync(store, NOW), "nothing new: nothing changes"
    joined.write(notified("a1", t, "completed"))
    assert w.sync(store, NOW)
    assert agents(store, sid)["Check pages"]["status"] == "done"


@pytest.mark.parametrize("described, posted, made_ago, matched, desc", [
    ("Check pages", "Check pages", 5, True, "the session's own item, titled with the description, is taken"),
    ("Check pages", "check   PAGES!", 5, True, "case, spaces and punctuation aside"),
    ("Check pages", "Adversarial check pages round", 5, True, "a title holding the description"),
    ("Check all the pages", "the pages", 5, True, "a title the description holds, two words or more"),
    ("Check pages", "Check pages", 900, False, "an item made more than 10 minutes before the start is another's"),
    ("Check pages", "Check pages", -10, True, "an item made within 15 s after the start is taken"),
    ("Check pages", "Check pages", -20, False, "an item made more than 15 s after the start is another's"),
    ("Review the plan", "Review", 5, False, "one word held in the description isn't enough"),
    ("Review", "Review the plan", 5, False, "a one-word description holds only an exact title"),
    ("Review", "review!", 5, True, "a one-word description's exact title is taken"),
    ("Check pages", "Review the plan", 5, False, "a different title is another's"),
])
def test_the_sessions_own_item_is_taken_not_duplicated(store, sid, joined, described, posted, made_ago, matched,
                                                       desc):
    started = NOW - 600
    ref = store.post_item(sid, "agent", posted, status="running")
    store.db.execute("UPDATE items SET created_at = ? WHERE ref = ?",
                     (datetime.fromtimestamp(started - made_ago, timezone.utc).isoformat(timespec="seconds"), ref))
    t = joined.agent("a1", described, started=started)
    joined.write(tool_use(t, described), launched(t, "a1"), notified("a1", t, "completed"))
    AgentWatcher(sid).sync(store, NOW)
    items = [i for i in store.items(sid) if i["kind"] == "agent"]
    assert len(items) == (1 if matched else 2), desc
    assert store.item(sid, ref)["status"] == ("done" if matched else "running"), desc


def test_an_exact_title_wins_over_one_that_holds_it(store, sid, joined):
    near = store.post_item(sid, "agent", "Check pages again later")
    exact = store.post_item(sid, "agent", "Check pages")
    t = joined.agent("a1", "Check pages", started=time.time() - 1)   # the items are made now, within 15 s of it
    joined.write(notified("a1", t, "completed"))
    AgentWatcher(sid).sync(store, NOW)
    assert (store.item(sid, exact)["status"], store.item(sid, near)["status"]) == ("done", "running")


def test_a_restart_or_a_second_wheelhouse_never_posts_twice(store, sid, joined, db_file):
    t = joined.agent("a1", "Check pages")
    joined.write(tool_use(t, "Check pages"), launched(t, "a1"))
    AgentWatcher(sid).sync(store, NOW)
    AgentWatcher(sid).sync(Store(db_file), NOW)   # another wheelhouse, at the same time
    joined.write(notified("a1", t, "completed"))
    AgentWatcher(sid).sync(store, NOW)            # restarted: reads from the subagent's start
    items = [i for i in store.items(sid) if i["kind"] == "agent"]
    assert [(i["ref"], i["status"]) for i in items] == [("A1", "done")], "one item, finished after the restart"


def test_a_restart_leaves_a_finished_item_alone(store, sid, joined):
    t = joined.agent("a1", "Check pages")
    joined.write(tool_use(t, "Check pages"), launched(t, "a1"), notified("a1", t, "completed"))
    AgentWatcher(sid).sync(store, NOW)
    store.update_item(sid, "A1", note="the session's progress note")
    before = store.item(sid, "A1")["updated_at"]
    assert not AgentWatcher(sid).sync(store, NOW), "nothing to write"
    assert store.item(sid, "A1")["updated_at"] == before


@pytest.mark.parametrize("started, active, finished, status, desc", [
    (JOINED + 60, None, True, "done", "started after the session joined: tracked, finished or not"),
    (JOINED - 600, None, True, None, "started before and finished: history, no item"),
    (JOINED - 600, JOINED - 60, False, "running", "started before and still running then: tracked"),
    (JOINED - 7200, JOINED - 3600, False, None, "started before, idle for over LIVE: taken as dead, no item"),
])
def test_the_cut_off(store, sid, joined, started, active, finished, status, desc):
    t = joined.agent("a1", "Check pages", started=started, active=active)
    joined.write(tool_use(t, "Check pages", at=started), launched(t, "a1"),
                 *([notified("a1", t, "completed")] if finished else []))
    AgentWatcher(sid).sync(store, NOW)
    assert [i["status"] for i in agents(store, sid).values()] == ([status] if status else []), desc


def test_the_cut_off_is_the_later_of_joining_and_first_running_this_code(store, sid, joined):
    store.set_setting(AGENTS_SINCE, iso(NOW - 60).replace("Z", "+00:00"))   # upgraded a minute ago
    t = joined.agent("a1", "Check pages", started=NOW - 600, active=NOW - 300)
    joined.write(tool_use(t, "Check pages"), launched(t, "a1"), notified("a1", t, "completed"))
    AgentWatcher(sid).sync(store, NOW)
    assert agents(store, sid) == {}, "a subagent that finished before the upgrade gets no item"


def test_refresh_cost(store, sid, joined, monkeypatch):
    """A first read with nothing running skips the transcript; an idle sync opens nothing."""
    for i in range(50):
        joined.agent(f"old{i}", f"old {i}", started=JOINED - 86400, active=JOINED - 86400)
    joined.write(*[tool_use(f"toolu_x{i}", "filler") for i in range(5000)])
    opened = []
    real = builtins.open
    monkeypatch.setattr(subagents, "open", lambda p, *a, **k: opened.append(p) or real(p, *a, **k), raising=False)
    w = AgentWatcher(sid)
    w.sync(store, NOW)
    assert opened == [joined.main], "only the main transcript, to find its end: no subagent's opened"
    assert w.offset == joined.main.stat().st_size, "the first read starts at the end"
    opened.clear()
    start = time.perf_counter()
    for _ in range(200):
        w.sync(store, NOW)
    each = (time.perf_counter() - start) / 200
    assert opened == [], "an idle sync opens no file"
    assert each < 0.002, f"an idle sync took {each * 1e6:.0f} us"


@pytest.mark.anyio
@pytest.mark.parametrize("state, status, desc", [
    ("live", "running", "the refresh tracks a running session's subagent"),
    ("dead", "failed", "a dead session's running subagent fails"),
])
async def test_the_wheelhouse_posts_a_subagents_item_on_its_refresh(store, sid, joined, monkeypatch,
                                                                    state, status, desc):
    from claude_wheelhouse import liveness
    from claude_wheelhouse.tui import WheelhouseApp

    monkeypatch.setattr(liveness, "status", lambda s, **kw: state)
    t = joined.agent("a1", "Check pages")
    joined.write(tool_use(t, "Check pages"), launched(t, "a1"))
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        for _ in range(30):
            await pilot.pause(0.1)
            if agents(store, sid):
                break
    assert [i["status"] for i in agents(store, sid).values()] == [status], desc


@pytest.mark.anyio
async def test_a_failing_sync_warns_once_per_error(store, sid, joined, monkeypatch):
    from claude_wheelhouse.tui import WheelhouseApp

    errors = iter(["disk gone", "disk gone", "disk gone", "locked"])

    def failing(self, *a, **k):
        raise OSError(next(errors, "locked"))

    monkeypatch.setattr(AgentWatcher, "sync", failing)
    app = WheelhouseApp(store)
    told = []
    monkeypatch.setattr(app, "notify", lambda text, **kw: told.append((text, kw.get("severity"))))
    async with app.run_test(size=(160, 40)) as pilot:
        for _ in range(8):
            app.refresh_data()
            await pilot.pause(0.1)
    assert told == [("demo: couldn't track subagents: disk gone", "warning"),
                    ("demo: couldn't track subagents: locked", "warning")], "each distinct error once"


# review r21's probes (P1 to P5): each builds the transcripts, syncs, and gives the statuses

def p1_one_notification_names_three(store, sid, s, db_file):
    for a in ("a1", "a2", "a3"):
        t = s.agent(a, f"Job {a}")
        s.write(tool_use(t, f"Job {a}"), launched(t, a))
    AgentWatcher(sid).sync(store, NOW)
    text = ("<task-notification>\n<task-id>a1</task-id>\n<task-id>a2</task-id>\n<task-id>a3</task-id>\n"
            "<status>stopped</status>\n<summary>3 background agents didn't finish before the previous session "
            "ended</summary>\n</task-notification>")
    s.write({"type": "user", "timestamp": iso(NOW - 10), "message": {"role": "user", "content": text}})
    AgentWatcher(sid).sync(store, NOW)


def p2_result_quotes_the_fields(store, sid, s, db_file):
    t1, t2 = s.agent("a1", "Reviewer"), s.agent("a2", "Builder")
    s.write(tool_use(t1, "Reviewer"), launched(t1, "a1"), tool_use(t2, "Builder"), launched(t2, "a2"))
    text = (f"<task-notification>\n<task-id>a1</task-id>\n<tool-use-id>{t1}</tool-use-id>\n"
            "<status>completed</status>\n<summary>Agent \"Reviewer\" finished</summary>\n"
            "<result>The parser reads <task-id>a2</task-id> and <status>failed</status> from the text.</result>\n"
            "</task-notification>")
    s.write({"type": "user", "timestamp": iso(NOW - 10), "message": {"role": "user", "content": text}})
    AgentWatcher(sid).sync(store, NOW)


def p3_resumed_after_a_restart(store, sid, s, db_file):
    t = s.agent("a1", "Job")
    s.write(tool_use(t, "Job"), launched(t, "a1"), notified("a1", t, "completed"))
    AgentWatcher(sid).sync(store, NOW)
    w = AgentWatcher(sid)   # the wheelhouse restarts
    w.sync(store, NOW)
    s.write(send("a1"))     # and the session resumes the finished subagent
    w.sync(store, NOW)


def p4_a_second_wheelhouse_finishes_it(store, sid, s, db_file):
    t = s.agent("a1", "Job", started=NOW - 5)
    s.write(tool_use(t, "Job", at=NOW - 5), launched(t, "a1"))
    b = AgentWatcher(sid)
    b.sync(Store(db_file), NOW)        # within GRACE: makes nothing
    AgentWatcher(sid).sync(store, NOW + 20)   # another wheelhouse makes the item, then quits
    s.write(notified("a1", t, "completed"))
    b.sync(Store(db_file), NOW + 25)   # the first sees the finish
    b.sync(Store(db_file), NOW + 30)


def p5_its_session_died(store, sid, s, db_file):
    t = s.agent("a1", "Job")
    s.write(tool_use(t, "Job"), launched(t, "a1"))
    w = AgentWatcher(sid)
    w.sync(store, NOW)
    w.sync(store, NOW + 86400, alive=False)   # killed: no notification is ever written
    return w


def p5_resumed_and_finished(store, sid, s, db_file):
    w = p5_its_session_died(store, sid, s, db_file)
    s.write(notified("a1", "toolu_a1", "completed"))   # resumed later, and it finished after all
    w.sync(store, NOW + 86500)


@pytest.mark.parametrize("scenario, want, desc", [
    (p1_one_notification_names_three, {"Job a1": "failed", "Job a2": "failed", "Job a3": "failed"},
     "P1: a notification with several task ids under one status finishes every one"),
    (p2_result_quotes_the_fields, {"Reviewer": "done", "Builder": "running"},
     "P2: task ids and statuses quoted in <result> are the subagent's text, not fields"),
    (p3_resumed_after_a_restart, {"Job": "running"},
     "P3: SendMessage after a restart resumes an item the store has as finished"),
    (p4_a_second_wheelhouse_finishes_it, {"Job": "done"},
     "P4: a finish seen by a wheelhouse that didn't make the item is still written"),
    (p5_its_session_died, {"Job": "failed"},
     "P5: a running subagent whose session died fails"),
    (p5_resumed_and_finished, {"Job": "done"},
     "P5: the notification of a session resumed later updates the item as usual"),
])
def test_review_probes(store, sid, joined, db_file, scenario, want, desc):
    scenario(store, sid, joined, db_file)
    got = {k: v["status"] for k, v in agents(store, sid).items()}
    assert got == want, f"{desc}: {got}"


def test_a_dead_sessions_subagent_says_why_it_failed(store, sid, joined, db_file):
    p5_its_session_died(store, sid, joined, db_file)
    assert notes(store, sid, "A1") == ["the session stopped while this subagent ran"]
