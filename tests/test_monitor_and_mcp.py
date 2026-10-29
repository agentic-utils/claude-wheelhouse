import io
import os
import sys
import types

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from claude_wheelhouse import monitor
from claude_wheelhouse.store import MCP_IN_TAB, PROTOCOL_VERSION


@pytest.mark.parametrize("ref, body, kind, expected, desc", [
    ("Q1", "yes", None, "[wheelhouse] from doug on Q1: yes", "answer to a question"),
    (None, "a\nb", None, "[wheelhouse] from doug (general): a ⏎ b", "general hint, newlines flattened"),
    (None, "you joined", "notice", "[wheelhouse] you joined", "the wheelhouse's own notice names no sender"),
])
def test_format_message(ref, body, kind, expected, desc):
    m = {"id": 1, "item_ref": ref, "body": body, "kind": kind}
    assert monitor.format_message(m, person="doug") == expected, desc


@pytest.mark.parametrize("msgs, expected, desc", [
    ([("Q1", "yes")], "[wheelhouse] from doug on Q1: yes", "one message reads as before"),
    ([("Q3", "use SQLite"), ("Q4", "yes,\nkeep the flag"), (None, "ship it tonight")],
     "[wheelhouse] from doug, 3 answers: on Q3: use SQLite ‖ on Q4: yes, ⏎ keep the flag ‖ (general): ship it tonight",
     "a batch is one line, a block per message in order"),
])
def test_format_batch(msgs, expected, desc):
    got = monitor.format_batch([{"id": i, "item_ref": r, "body": b} for i, (r, b) in enumerate(msgs)], person="doug")
    assert got == (expected, len(msgs)), desc


@pytest.mark.parametrize("n, length, shown, desc", [
    (2, 5000, 2, "two long messages share the limit, each cut short"),
    (15, 5000, 3, "past what fits, the rest follow"),
    (30, 300, 3, "many medium messages: the line stays within the limit"),
    (30, 20, 13, "many short messages: as many as fit whole"),
    (5, 30, 5, "a few short messages all fit"),
])
def test_a_long_batch_stays_within_the_line_limit(n, length, shown, desc):
    msgs = [{"id": 100 + i, "item_ref": f"Q{i}", "body": "x" * length} for i in range(n)]
    line, got = monitor.format_batch(msgs, person="doug")
    assert (got, len(line) <= monitor.LINE_LIMIT) == (shown, True), desc
    assert line[:500] == line, f"{desc}: Claude Code cuts a notification at 500 characters"
    if length > 100:
        assert "get_input(message_id=100)" in line, desc
    assert (f"{n - shown} more follow" in line) == (shown < n), desc


def test_messages_that_do_not_fit_come_in_the_next_notification(store, sid):
    for i in range(20):
        store.send(sid, "x" * 300, f"Q{i}")
    out = io.StringIO()
    counts = [monitor.poll_once(store, sid, out) for _ in range(10)]
    assert sum(counts) == 20 and counts[0] < 20 and counts[-1] == 0, "released, then printed by later polls"
    assert all(len(line) <= monitor.LINE_LIMIT for line in out.getvalue().splitlines())


def test_a_cut_short_message_can_always_be_read_in_full(store, sid):
    """Review: a general message is confirmed delivered as it prints, so get_input() can't find it."""
    from claude_wheelhouse import mcp_server
    store.send(sid, "y" * 5000)
    out = io.StringIO()
    monitor.poll_once(store, sid, out)
    msg_id = store.db.execute("SELECT id FROM messages").fetchone()[0]
    assert f"get_input(message_id={msg_id})" in out.getvalue()
    mcp_server._store, mcp_server._sid = store, sid
    assert mcp_server.get_input(message_id=msg_id) == "[general] " + "y" * 5000
    assert mcp_server.get_input(message_id=msg_id + 1) == f"no message {msg_id + 1}"


def test_a_dispatched_batch_arrives_as_one_notification(store, sid):
    store.queue(sid, "SQLite", "Q3")
    store.queue(sid, "yes", "Q4")
    out = io.StringIO()
    assert monitor.poll_once(store, sid, out) == 0, "drafts wait"
    store.dispatch(sid)
    assert monitor.poll_once(store, sid, out) == 2
    assert out.getvalue().count("\n") == 1 and "2 answers" in out.getvalue()


def test_format_message_names_the_os_user(monkeypatch):
    monkeypatch.setattr(monitor.getpass, "getuser", lambda: "ada")
    assert monitor.format_message({"id": 1, "item_ref": "Q1", "body": "yes"}).startswith("[wheelhouse] from ada on Q1")


@pytest.mark.parametrize("ref, kind, desc", [
    ("Q2", None, "an answer on an item"),
    (None, None, "a general message (Doug's 676-character message 25 lost its tail and pointer)"),
    (None, "notice", "the wheelhouse's own notice"),
])
def test_a_long_message_shows_its_pointer_before_the_cut(ref, kind, desc):
    line = monitor.format_message({"id": 7, "item_ref": ref, "kind": kind, "body": "x" * 676}, person="doug")
    assert len(line) <= monitor.LINE_LIMIT < 500, desc
    assert line.index("get_input(message_id=7)") < 100, f"{desc}: the pointer comes before the text"


def test_poll_once_delivers_each_message_once(store, sid):
    store.send(sid, "first")
    out = io.StringIO()
    assert monitor.poll_once(store, sid, out) == 1
    assert monitor.poll_once(store, sid, out) == 0
    assert out.getvalue().count("first") == 1


@pytest.mark.parametrize("status, desc", [
    ("closed", "closed by the host as it handed over to the tab"),
    ("open", "reopened by the person meanwhile"),
])
def test_poll_once_never_passes_on_a_reply_on_an_mcp_question(store, sid, status, desc):
    """After Shell the tab's monitor reads the person's messages: a reply on the host's MCP
    question, a pasted sign-in code most of all, never reaches the session."""
    from claude_wheelhouse.store import mcp_question
    ref = store.post_marked_question(sid, "MCP local-oauth: sign in", "body", mcp_question("local-oauth"))
    if status == "closed":
        store.update_item(sid, ref, status="closed")
    store.send(sid, "http://localhost:4321/callback?code=SECRET&state=x", ref)
    store.send(sid, "general")
    out = io.StringIO()
    monitor.poll_once(store, sid, out)
    assert "SECRET" not in out.getvalue() and "general" in out.getvalue(), desc
    thread = store.thread(sid, ref)
    assert all("SECRET" not in m["body"] for m in thread), f"{desc}: no code kept"
    assert thread[-1]["body"] == MCP_IN_TAB, desc
    assert monitor.poll_once(store, sid, out) == 0, f"{desc}: taken, not left for later"


def test_mcp_server_round_trip(store, sid, db_file, tmp_path):
    """Start the real stdio server and drive it like Claude Code would."""
    (tmp_path / ".git").mkdir()   # the session's directory a repo, for set_doc_dirs
    store.register(sid, 999999, 1, "boot-x")   # as `claude_wheelhouse run` would, before exec
    params = StdioServerParameters(command=sys.executable, args=["-m", "claude_wheelhouse", "mcp"],
                                   env=dict(os.environ, WHEELHOUSE_SESSION_ID=sid, WHEELHOUSE_DB=str(db_file)))

    async def drive():
        async with stdio_client(params) as (r, w), ClientSession(r, w) as client:
            await client.initialize()
            names = {t.name for t in (await client.list_tools()).tools}
            assert {"post_item", "update_item", "reply", "set_synopsis", "set_doc_dirs", "get_input", "list_items",
                    "park_session", "end_session"} <= names
            ref = (await client.call_tool("post_item", {"kind": "question", "title": "db?",
                                                         "body": "detail"})).content[0].text
            store.send(sid, "postgres", ref)
            got = (await client.call_tool("get_input", {})).content[0].text
            refused = [await client.call_tool("update_item", args) for args in (
                {"ref": ref, "status": "done"}, {"ref": "Q9", "status": "closed"})]
            replied = (await client.call_tool("reply", {"ref": ref, "text": "which version?",
                                                        "status": "open"})).content[0].text
            await client.call_tool("set_synopsis", {"text": "Choosing a database."})
            dirs = (await client.call_tool("set_doc_dirs", {"paths": ["docs/decisions"]})).content[0].text
            return ref, got, refused, replied, dirs

    ref, got, refused, replied, dirs = anyio.run(drive)
    assert dirs == "decisions are looked for in docs/decisions"
    assert store.session(sid)["doc_dirs"] == '["docs/decisions"]', "a list, through MCP's schema"
    assert replied == "replied on Q1, now open"
    assert store.item(sid, ref)["status"] == "open"
    assert store.session(sid)["synopsis"] == "Choosing a database."
    assert [(r.is_error, r.content[0].text) for r in refused] == [
        (True, "Error executing tool update_item: question status must be one of ['answered', 'closed', 'open']"),
        (True, "Error executing tool update_item: no item Q9 in this session"),
    ], "a refused call tells the session why"
    assert ref == "Q1"
    assert got == "[Q1] postgres"
    row = store.session(sid)
    assert row["claude_pid"] == 999999, "server leaves the pid registered by run() alone (review #4)"
    assert row["heartbeat_at"], "server beats once on start"
    assert row["code_version"] == PROTOCOL_VERSION, "and stamps its code version"
    from claude_wheelhouse import fingerprint
    assert row["mcp_pid"] and row["mcp_code"] == fingerprint.code(), "and records its pid and code (#242)"


class BrokenOut:
    def write(self, s):
        raise BrokenPipeError

    def flush(self):
        raise BrokenPipeError


def test_a_failed_print_leaves_the_message_for_redelivery(store, sid):
    """Round-2 #2: stdout closing must not lose a claimed message."""
    store.send(sid, "keep me", "Q1")
    with pytest.raises(BrokenPipeError):
        monitor.poll_once(store, sid, BrokenOut())
    out = io.StringIO()
    assert monitor.poll_once(store, sid, out) == 1
    assert "keep me" in out.getvalue()


def test_a_claim_abandoned_by_a_killed_monitor_is_retaken(store, sid):
    """A monitor killed between claim and print leaves a stale claim behind."""
    store.send(sid, "orphan")
    assert [m["body"] for m in store.claim(sid)] == ["orphan"]
    assert store.claim(sid) == [], "a fresh claim is not taken twice"
    store.db.execute("UPDATE messages SET claimed_at = '2000-01-01T00:00:00+00:00'")
    assert [m["body"] for m in store.claim(sid)] == ["orphan"]


def test_reading_a_thread_marks_its_messages_delivered(store, sid):
    """Round-2 #6: get_input(ref) shows the thread, so the monitor must not repeat it."""
    from claude_wheelhouse import mcp_server
    q = store.post_item(sid, "question", "db?")
    store.send(sid, "postgres", q)
    store.send(sid, "unrelated")
    mcp_server._store, mcp_server._sid = store, sid
    assert "postgres" in mcp_server.get_input(q)
    out = io.StringIO()
    assert monitor.poll_once(store, sid, out) == 1
    assert "unrelated" in out.getvalue() and "postgres" not in out.getvalue()


def test_a_cancel_then_a_fresh_request_arrive_in_order(store, sid):
    """R5 #2: within one poll, the cancel of the old request must come before the new one."""
    store.request(sid, "end")
    monitor.poll_once(store, sid, io.StringIO())
    store.cancel_request(sid, "end")
    store.request(sid, "end")
    out = io.StringIO()
    monitor.poll_once(store, sid, out)
    text = out.getvalue()
    assert text.index("cancelled the end request") < text.index("pressed Stop & Forget"), text


def test_the_monitor_survives_a_locked_database(sid, monkeypatch):
    """R5 #3: one "database is locked" must not end delivery for the rest of the session."""
    import sqlite3
    calls = []

    def poll(store, sid):
        calls.append(sid)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return None if len(calls) == 3 else 0

    monkeypatch.setattr(monitor, "poll_once", poll)
    monkeypatch.setattr(monitor.time, "sleep", lambda s: None)
    monitor.main(sid)
    assert len(calls) == 3


def test_a_failed_confirm_is_retried_not_left_for_the_claim_timeout(store, sid, monkeypatch):
    """R6: if confirm hits "database is locked" after printing, retry it, so the message
    isn't printed again when its claim goes stale 30 s later."""
    import sqlite3
    store.send(sid, "delete the branch")
    real_confirm, fails = store.confirm, [1]

    def confirm(msgs):
        if fails:
            fails.pop()
            raise sqlite3.OperationalError("database is locked")
        real_confirm(msgs)

    monkeypatch.setattr(store, "confirm", confirm)
    monkeypatch.setattr(monitor.time, "sleep", lambda s: None)
    out = io.StringIO()
    assert monitor.poll_once(store, sid, out) == 1
    assert store.pending(sid) == []
    assert out.getvalue().count("delete the branch") == 1


def test_the_monitor_survives_a_locked_start_and_reports_each_error_once(sid, monkeypatch, capsys):
    """R6: a lock while opening the store must not kill the monitor, and a repeating error
    is reported once on stderr rather than every poll."""
    import sqlite3
    opens, polls = [], []

    def store():
        opens.append(1)
        if len(opens) == 1:
            raise sqlite3.OperationalError("database is locked")
        return types.SimpleNamespace(mark_version=lambda sid: None)

    def poll(store, sid):
        polls.append(1)
        if len(polls) < 3:
            raise sqlite3.OperationalError("database is locked")
        if len(polls) == 3:
            raise sqlite3.OperationalError("disk I/O error")
        return None

    monkeypatch.setattr(monitor, "Store", store)
    monkeypatch.setattr(monitor, "poll_once", poll)
    monkeypatch.setattr(monitor.time, "sleep", lambda s: None)
    monitor.main(sid)
    assert len(opens) == 2 and len(polls) == 4
    err = capsys.readouterr().err
    assert err.count("database is locked") == 1, err
    assert err.count("disk I/O error") == 1, err


@pytest.mark.parametrize("fields, why, desc", [
    ({"why": "over Postgres: no server; undo: swap the DSN"}, "over Postgres: no server; undo: swap the DSN",
     "the one line, as the protocol asks"),
    ({"alternative": "Postgres", "why": "no server", "reverse": "swap the DSN"},
     "over Postgres: no server; undo: swap the DSN", "an older protocol's three fields: folded into why, none lost"),
    ({"alternative": "Postgres", "reverse": "swap the DSN"}, "over Postgres; undo: swap the DSN",
     "no why of its own: what there is"),
])
def test_a_decision_keeps_an_older_protocols_fields(store, sid, fields, why, desc):
    from claude_wheelhouse import mcp_server
    mcp_server._store, mcp_server._sid = store, sid
    ref = mcp_server.post_item("decision", "used SQLite", **fields)
    assert store.item(sid, ref)["body"] == f"**Why:** {why}", desc


def test_the_sessions_status_call_settles_an_awaiting_item(store, sid):
    from claude_wheelhouse import mcp_server
    mcp_server._store, mcp_server._sid = store, sid
    q = store.post_item(sid, "question", "db?")
    store.send(sid, "postgres?", q)
    mcp_server.update_item(q, status="answered")
    assert (sid, q) not in store.awaiting() and store.item(sid, q)["status"] == "answered"


@pytest.mark.parametrize("fields, named, desc", [
    ({"alternative": "Postgres"}, "alternative", "a task with an older decision field: that field is named"),
    ({"reverse": "undo it"}, "reverse", "the other"),
    ({"why": "because"}, "why", "and why itself"),
])
def test_only_a_decision_takes_the_decision_fields(store, sid, fields, named, desc):
    from claude_wheelhouse import mcp_server
    mcp_server._store, mcp_server._sid = store, sid
    with pytest.raises(Exception, match=f"{named}: only a decision"):
        mcp_server.post_item("task", "x", **fields)


@pytest.mark.parametrize("before, call, said, desc", [
    (["T running"], ("update_item", "T1", {"status": "done"}), "T1 updated",
     "nothing else in doubt: nothing appended"),
    (["T running", "T waiting", "T running"], ("update_item", "T2", {"status": "done"}),
     "T2 updated. Still open: T1 running, T3 running", "closing one of three lists the other two"),
    (["T running", "T waiting"], ("update_item", "T2", {"note": "halfway"}), "T2 updated",
     "a note only is no change of work"),
    (["T waiting", "Q owed"], ("reply", "Q1", {"text": "thanks", "status": "answered"}),
     "replied on Q1, now answered. Still open: T1 waiting", "a question answered"),
    (["T waiting"], ("post_item", "task", {"title": "follow-on"}),
     "T2. Still open: T1 waiting", "a task posted beside one waiting 17 minutes (T81 and T82)"),
    (["Q owed"], ("post_item", "question", {"title": "next?"}),
     "Q2. Still open: Q1 reply owed", "a question the person answered is marked reply owed (Q72 and Q73)"),
    (["T waiting"], ("post_item", "decision", {"title": "chose", "why": "because"}), "D1",
     "a decision is no change of work"),
])
def test_a_change_of_work_lists_what_is_still_open(store, sid, monkeypatch, before, call, said, desc):
    from datetime import datetime, timedelta, timezone

    from claude_wheelhouse import mcp_server
    from claude_wheelhouse import store as store_mod
    mcp_server._store, mcp_server._sid = store, sid
    store.mark_version(sid)
    t0 = datetime.now(timezone.utc) - timedelta(minutes=17)
    real_now = store_mod.now
    monkeypatch.setattr(store_mod, "now", lambda: t0.isoformat(timespec="seconds"))
    for item in before:
        kind, status = item.split()
        ref = store.post_item(sid, "task" if kind == "T" else "question", item,
                              status=None if status == "owed" else status)
        if status == "owed":
            store.send(sid, "yes", ref)
    monkeypatch.setattr(store_mod, "now", real_now)
    name, first, kwargs = call
    assert getattr(mcp_server, name)(first, **kwargs) == said, desc


@pytest.mark.parametrize("tool, parent, stops, said, gone, desc", [
    ("park_session", 4242, True, "parked; this session stops in a moment", False,
     "a tab's: its Claude Code (this server's parent, as registered) is stopped after the reply"),
    ("end_session", 4242, True, "forgotten; wheelhouse data deleted; this session stops in a moment", True,
     "Forget too, read before the row is deleted"),
    ("park_session", 777, False, "parked", False, "a host's session: its host exits by itself, nothing signalled"),
    ("end_session", 777, False, "forgotten; wheelhouse data deleted", True, "so on Forget"),
])
def test_park_and_forget_stop_the_session_after_replying(store, sid, monkeypatch, tool, parent, stops, said, gone,
                                                        desc):
    from claude_wheelhouse import mcp_server
    store.db.execute("UPDATE sessions SET claude_pid = 4242, claude_start = 1, boot_id = 'b'")
    mcp_server._store, mcp_server._sid = store, sid
    timers, stopped = [], []
    monkeypatch.setattr(mcp_server.os, "getppid", lambda: parent)
    monkeypatch.setattr(mcp_server.liveness, "stop", lambda *a: stopped.append(a))
    monkeypatch.setattr(mcp_server.threading, "Timer",
                        lambda delay, fn, args: types.SimpleNamespace(start=lambda: timers.append((delay, fn, args)),
                                                                      daemon=False))
    assert getattr(mcp_server, tool)() == said, desc
    assert stopped == [], f"{desc}: nothing signalled before the reply has gone"
    for delay, fn, args in timers:
        fn(*args)
    assert stopped == ([(4242, 1, "b")] if stops else []), desc
    assert [d for d, *_ in timers] == ([mcp_server.STOP_AFTER] if stops else []), desc
    assert (store.session(sid) is None) is gone, desc


@pytest.mark.parametrize("call, refused, desc", [
    (dict(kind="doc", title="plan", path="{doc}", body="text"), "title, path and parent only", "a doc with a body"),
    (dict(kind="task", title="build", path="{doc}"), "only a doc takes", "a task with a path"),
    (dict(kind="question", title="which?", parent="T1"), "only a doc takes", "a question with a parent"),
])
def test_post_item_keeps_path_and_parent_to_docs(store, sid, tmp_path, call, refused, desc):
    from claude_wheelhouse import mcp_server
    (tmp_path / "plan.md").write_text("# Plan\n")
    mcp_server._store, mcp_server._sid = store, sid
    with pytest.raises(Exception, match=refused):   # the tool's ToolError, with the store's words
        mcp_server.post_item(**{k: v.format(doc=tmp_path / "plan.md") for k, v in call.items()})
    assert store.items(sid) == [], desc


def test_post_item_declares_a_doc(store, sid, tmp_path):
    from claude_wheelhouse import mcp_server
    (tmp_path / "plan.md").write_text("# Plan\n")
    mcp_server._store, mcp_server._sid = store, sid
    t = mcp_server.post_item(kind="task", title="design it").split(".")[0]
    assert mcp_server.post_item(kind="doc", title="the plan", path=str(tmp_path / "plan.md"), parent=t) == "M1"
    assert store.item(sid, "M1")["parent_ref"] == t


@pytest.mark.parametrize("given, kept, said, desc", [
    (["docs/decisions"], '["docs/decisions"]', "decisions are looked for in docs/decisions", "declared"),
    (["docs/adr", ".plan/"], '["docs/adr", ".plan"]', "decisions are looked for in docs/adr, .plan", "several, normalised"),
    ([], None, "decisions are looked for in docs/adr", "none: docs/adr again"),
    (["../elsewhere"], "unchanged", "is outside the git root", "outside the repo: refused, nothing kept"),
    (["/abs"], "unchanged", "in no other git repo", "an absolute path in no git repo: refused"),
    (["."], "unchanged", "the git root itself", "the repo's root: refused"),
    (["docs"], "no git", "in no git repo", "a session in no git repo: refused"),
])
def test_set_doc_dirs(store, sid, tmp_path, given, kept, said, desc):
    from claude_wheelhouse import mcp_server
    mcp_server._store, mcp_server._sid = store, sid
    if kept != "no git":
        (tmp_path / ".git").mkdir()
    else:
        kept = "unchanged"
    store.set_doc_dirs(sid, ["before"])
    if kept == "unchanged":
        with pytest.raises(Exception, match=said):
            mcp_server.set_doc_dirs(given)
        assert store.session(sid)["doc_dirs"] == '["before"]', desc
    else:
        assert mcp_server.set_doc_dirs(given) == said, desc
        assert store.session(sid)["doc_dirs"] == kept, desc


@pytest.mark.parametrize("runner, before, keep, said, desc", [
    ("sdk", None, "keep the plan", "compaction requested", "in SDK mode: held for the turn's end"),
    ("tab", None, "keep the plan", "only in a session run inside the wheelhouse", "a tab: refused, nothing held"),
    ("shell", None, "keep the plan", "only in a session run inside the wheelhouse",
     "an SDK session handed to a shell tab: refused"),
    ("sdk", None, "  ", "keep must say", "nothing to keep: refused"),
    ("sdk", "pending", "again", "already pending", "one pending at a time"),
    ("sdk", "compacting", "keep", "being compacted already", "a compaction under way: refused"),
    ("sdk", "recent", "keep", "no sooner than 30 minutes", "within the gap of the last: refused"),
    ("sdk", "long ago", "keep", "compaction requested", "past the gap: accepted"),
])
def test_request_compact(store, sid, runner, before, keep, said, desc):
    """Doug: the model can signal the wheelhouse to compact, in SDK mode only, guarded so a
    confused model can't loop."""
    from datetime import datetime, timedelta, timezone
    from claude_wheelhouse import mcp_server
    mcp_server._store, mcp_server._sid = store, sid
    store.set_runner(sid, "sdk" if runner == "shell" else runner)
    if runner == "shell":
        store.set_shell(sid, "tab")
    if before in ("recent", "long ago", "pending"):
        ago = timedelta(minutes=5 if before != "long ago" else 31)
        with store.tx() as db:
            db.execute("UPDATE sessions SET compact_requested_at = ?, compact_request = ? WHERE id = ?",
                       ((datetime.now(timezone.utc) - ago).isoformat(), "first" if before == "pending" else None, sid))
    elif before == "compacting":
        store.set_activity(sid, "compacting")
    held = store.session(sid)["compact_request"]
    if said.startswith("compaction requested"):
        assert mcp_server.request_compact(keep).startswith(said), desc
        assert store.session(sid)["compact_request"] == keep.strip(), desc
    else:
        with pytest.raises(Exception, match=said):
            mcp_server.request_compact(keep)
        assert store.session(sid)["compact_request"] == held, f"{desc}: nothing changed"


@pytest.mark.parametrize("runner, asked, kwargs, said, compact, keep, decision, desc", [
    ("sdk", None, dict(keep="the plan"), "compacted once this turn ends", 1, "the plan", None,
     "unasked in SDK mode: compacted first, with its keep text"),
    ("sdk", None, {}, "compacted once this turn ends", 1, "", None, "unasked, no keep: a bare /compact"),
    ("sdk", None, dict(compact=False, reason="they will Stop & Forget it"), "stops once this turn ends", 0, None,
     "they will Stop & Forget it", "opting out: parked as it is, its reason a decision"),
    ("sdk", None, dict(compact=False, reason="  "), "needs a reason", None, None, None,
     "opting out with no reason: refused, not parked"),
    ("sdk", False, dict(keep="the plan"), "stops once this turn ends", 0, None, None,
     "answering the person's Park without Compact: their choice stands"),
    ("sdk", True, dict(compact=False), "compacted once this turn ends", 1, "", None,
     "answering the person's Park with Compact, told or not: theirs stands, no reason needed"),
    ("sdk", True, dict(keep="the plan"), "compacted once this turn ends", 1, "the plan", None,
     "answering it with keep: its instructions"),
    ("tab", None, dict(compact=False), "parked", 0, None, None, "a tab: never compacted, nothing asked"),
    ("shell", None, dict(keep="x"), "parked", 0, None, None, "an SDK session in a shell tab: as a tab"),
])
def test_park_session_parks_itself(store, sid, runner, asked, kwargs, said, compact, keep, decision, desc):
    """Doug: a session can park itself; a model-initiated park compacts, unless the model has
    a specific reason why compaction is undesirable."""
    from claude_wheelhouse import mcp_server
    mcp_server._store, mcp_server._sid = store, sid
    store.set_runner(sid, "sdk" if runner == "shell" else runner)
    if runner == "shell":
        store.set_shell(sid, "tab")
    if asked is not None:
        store.request(sid, "park", asked)
    if compact is None:
        with pytest.raises(Exception, match=said):
            mcp_server.park_session(**kwargs)
        assert not store.session(sid)["parked"], f"{desc}: not parked"
        return
    assert said in mcp_server.park_session(**kwargs), desc
    s = store.session(sid)
    assert (s["parked"], s["park_compact"], s["park_keep"]) == (1, compact, keep), desc
    whys = [i["body"] for i in store.items(sid) if i["kind"] == "decision"]
    assert [decision in w for w in whys] == ([True] if decision else []), desc
    store.set_parked(sid, False)
    assert store.session(sid)["park_keep"] is None, f"{desc}: Unpark drops it"


@pytest.mark.parametrize("then, desc", [
    ("request", "a new Park request: a self-park's keep text goes"),
    ("cancel", "a cancelled Park request: so does its keep text"),
])
def test_park_keep_goes_with_the_park(store, sid, then, desc):
    with store.tx() as db:
        db.execute("UPDATE sessions SET park_keep = 'stale', park_requested_at = '2026-10-24T00:00:00Z' WHERE id = ?",
                   (sid,))
    if then == "request":
        store.request(sid, "park", True)
    else:
        store.cancel_request(sid, "park")
    assert store.session(sid)["park_keep"] is None, desc


def test_parking_itself_drops_a_pending_compact_request(store, sid):
    store.set_runner(sid, "sdk")
    store.request_compact(sid, "keep the plan")
    store.park(sid, keep="the plan")
    s = store.session(sid)
    assert (s["compact_request"], s["compact_requested_at"]) == (None, None), "the park's compaction replaces it"
