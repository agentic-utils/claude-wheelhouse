import io
import os
import sys
import types

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from claude_wheelhouse import monitor
from claude_wheelhouse.store import PROTOCOL_VERSION


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


def test_mcp_server_round_trip(store, sid, db_file):
    """Start the real stdio server and drive it like Claude Code would."""
    store.register(sid, 999999, 1, "boot-x")   # as `claude_wheelhouse run` would, before exec
    params = StdioServerParameters(command=sys.executable, args=["-m", "claude_wheelhouse", "mcp"],
                                   env=dict(os.environ, WHEELHOUSE_SESSION_ID=sid, WHEELHOUSE_DB=str(db_file)))

    async def drive():
        async with stdio_client(params) as (r, w), ClientSession(r, w) as client:
            await client.initialize()
            names = {t.name for t in (await client.list_tools()).tools}
            assert {"post_item", "update_item", "reply", "set_synopsis", "get_input", "list_items",
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
            return ref, got, refused, replied

    ref, got, refused, replied = anyio.run(drive)
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
    assert text.index("cancelled the end request") < text.index("pressed End"), text


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
