import contextlib
import json
import threading
from datetime import datetime, timezone
from unittest import mock

import pytest

from claude_wheelhouse.store import TRIGGER_NAMES, SessionGone, Store


def test_pragmas_make_every_commit_durable(store):
    assert store.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store.db.execute("PRAGMA synchronous").fetchone()[0] == 2   # FULL


def test_refuses_windows_mount(monkeypatch):
    monkeypatch.setenv("WHEELHOUSE_DB", "/mnt/c/wheelhouse.db")
    with pytest.raises(ValueError, match="/mnt/"):
        Store()


DECIDED = {"why": "over Postgres: no server to run; undo: swap the DSN"}


@pytest.mark.parametrize("posts, expected, desc", [
    ([("task", None)], [("T1", "todo")], "task starts todo"),
    ([("question", None)], [("Q1", "open")], "question starts open"),
    ([("agent", None)], [("A1", "running")], "agent starts running"),
    ([("task", None), ("task", "running"), ("question", None)],
     [("T1", "todo"), ("T2", "running"), ("Q1", "open")], "refs count per kind"),
])
def test_post_item_refs_and_statuses(store, sid, posts, expected, desc):
    refs = [store.post_item(sid, kind, "title", "body", status) for kind, status in posts]
    got = [(r, store.item(sid, r)["status"]) for r in refs]
    assert got == expected, desc


@pytest.mark.parametrize("call, error, desc", [
    (lambda s, sid: s.post_item(sid, "bug", "x"), ValueError, "unknown kind"),
    (lambda s, sid: s.post_item(sid, "task", "x", status="answered"), ValueError, "question status on a task"),
    (lambda s, sid: s.update_item(sid, "T9", status="done"), KeyError, "missing ref"),
    (lambda s, sid: s.reply(sid, "T9", "hi"), KeyError, "reply to a missing ref"),
    (lambda s, sid: s.reply(sid, s.post_item(sid, "question", "x"), "which?"), ValueError,
     "a reply on a question must say where it stands"),
    (lambda s, sid: s.reply(sid, s.post_item(sid, "question", "x"), "ok", "closed"), ValueError,
     "closing is the person's call"),
    (lambda s, sid: s.reply(sid, s.post_item(sid, "task", "x"), "ok", "answered"), ValueError,
     "a question status on a task"),
    (lambda s, sid: s.post_item(sid, "decision", "used SQLite"), ValueError, "a decision needs its why line"),
    (lambda s, sid: s.post_item(sid, "decision", "used SQLite", why="local", reverse="migrate"), ValueError,
     "the three fields are one line now: reverse isn't one"),
    (lambda s, sid: s.post_item(sid, "decision", "x", status="seen", **DECIDED), ValueError,
     "a decision's status is the person's"),
    (lambda s, sid: s.post_item(sid, "task", "x", why="because"), ValueError, "only a decision takes why"),
    (lambda s, sid: s.update_item(sid, s.post_item(sid, "decision", "x", **DECIDED), status="seen"),
     ValueError, "the session can't mark its decision seen"),
    (lambda s, sid: s.reply(sid, s.post_item(sid, "decision", "x", **DECIDED), "ok", "seen"), ValueError,
     "a reply on a decision takes no status"),
    (lambda s, sid: s.update_item(sid, s.post_item(sid, "decision", "x", **DECIDED), status="closed"),
     ValueError, "the session can't close its decision"),
    (lambda s, sid: s.close_decision(sid, s.post_item(sid, "question", "x")), KeyError,
     "close_decision is for decisions only"),
])
def test_rejects_bad_writes(store, sid, call, error, desc):
    with pytest.raises(error):
        call(store, sid)


def test_an_answer_waits_for_delivery_and_leaves_the_status_to_the_session(store, sid):
    q = store.post_item(sid, "question", "which db?", "full detail")
    store.send(sid, "postgres", q)
    assert store.item(sid, q)["status"] == "open", "the session's reply says whether it's answered"
    assert [m["body"] for m in store.pending(sid)] == ["postgres"]
    assert [m["body"] for m in store.take_pending(sid)] == ["postgres"]
    assert store.pending(sid) == [] and store.take_pending(sid) == []
    assert [m["body"] for m in store.thread(sid, q)] == ["postgres"]


def test_update_note_lands_in_thread_already_delivered(store, sid):
    t = store.post_item(sid, "task", "build it")
    store.update_item(sid, t, status="running", note="halfway")
    assert store.item(sid, t)["status"] == "running"
    assert store.pending(sid) == []
    assert [(m["author"], m["body"]) for m in store.thread(sid, t)] == [("claude", "halfway")]


def test_inbox_puts_open_questions_first_and_hides_parked(store, sid, tmp_path):
    store.post_item(sid, "task", "a task", status="running")
    store.post_item(sid, "task", "blocked", status="blocked")
    store.post_item(sid, "question", "a question")
    other = store.create_session(str(tmp_path), name="parked one")
    store.post_item(other, "question", "hidden")
    store.set_parked(other, True)
    assert [i["title"] for i in store.items()] == ["a question", "blocked", "a task"]
    assert [i["title"] for i in store.items(other)] == ["hidden"], "a session still sees its own items"


def test_end_deletes_everything(store, sid):
    q = store.post_item(sid, "question", "q")
    store.send(sid, "a", q)
    store.end(sid)
    assert store.session(sid) is None
    assert store.db.execute("SELECT count(*) FROM items").fetchone()[0] == 0
    assert store.db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_one_store_survives_parallel_writers(store, sid):
    """The MCP server runs tool calls in worker threads on one Store (review #2)."""
    import threading
    errors = []

    def post():
        for _ in range(25):
            try:
                store.post_item(sid, "task", "t")
            except Exception as e:
                errors.append(e)
    threads = [threading.Thread(target=post) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sorted(int(r["ref"][1:]) for r in store.items(sid)) == list(range(1, 151))


def test_take_pending_delivers_each_message_once(db_file, store, sid):
    """The monitor and get_input() may read at the same moment (review #10)."""
    import threading
    for i in range(200):
        store.send(sid, f"m{i}")
    seen = []

    def take():
        reader = Store(db_file)
        while msgs := reader.take_pending(sid):
            seen.extend(m["id"] for m in msgs)
    threads = [threading.Thread(target=take) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(seen) == sorted(set(seen)) and len(seen) == 200


def test_queued_answers_wait_until_dispatched(store, sid):
    q3, q4 = store.post_item(sid, "question", "db?"), store.post_item(sid, "question", "flag?")
    store.queue(sid, "SQLite", q3)
    store.queue(sid, "keep it", q4)
    assert store.pending(sid) == [] and store.claim(sid) == [], "drafts are not delivered"
    assert store.item(sid, q3)["status"] == "open", "a queued answer leaves the question open"
    assert [m["body"] for m in store.thread(sid, q3)] == ["SQLite"], "the person's thread shows it"
    assert store.thread(sid, q3, in_flight=False) == [], "the session's view of the thread doesn't"
    assert store.sessions()[0]["drafts"] == 2
    assert store.dispatch(sid) == 2
    assert [m["body"] for m in store.claim(sid)] == ["SQLite", "keep it"], "one claim takes the whole batch"
    assert {store.item(sid, r)["status"] for r in (q3, q4)} == {"open"}, "sending leaves the status alone"
    assert store.drafts() == [] and store.dispatch(sid) == 0


def test_a_queued_answer_can_be_taken_back(store, sid):
    store.queue(sid, "oops", "Q1")
    draft = store.drafts(sid)[0]
    assert store.unqueue(draft["id"]) == "oops"
    assert store.drafts() == [] and store.unqueue(draft["id"]) is None


@pytest.mark.parametrize("steps, status, awaiting, processing, desc", [
    ([], "open", False, False, "nothing said yet: awaiting the person"),
    (["send"], "open", True, True, "the person asked: the ball is in the session's court"),
    (["send", "reply open"], "open", False, False, "a clarification answered: still waiting on the person"),
    (["send", "reply answered"], "answered", False, False, "the session has what it needs"),
    (["send", "reply answered", "send"], "answered", True, True, "a further word is awaiting a reply again"),
    (["queue"], "open", False, False, "a queued answer isn't with the session yet"),
    (["queue", "dispatch"], "open", True, True, "until it's sent"),
    (["send", "queue"], "open", True, False, "more queued: back with the person until it's sent"),
    (["send", "status answered"], "answered", False, False,
     "a hosted session's status call is its word: no longer awaiting it, even with no reply posted"),
    (["send", "status open"], "open", False, False, "status open: back with the person"),
    (["send", "person closes"], "closed", True, False, "the person's own update says nothing for the session"),
    (["status answered", "send"], "answered", True, True, "a word after the status call awaits a reply again"),
])
def test_the_session_declares_where_a_question_stands(store, sid, steps, status, awaiting, processing, desc):
    q = store.post_item(sid, "question", "db?")
    act = {"send": lambda: store.send(sid, "postgres?", q), "queue": lambda: store.queue(sid, "postgres?", q),
           "dispatch": lambda: store.dispatch(sid),
           "reply open": lambda: store.reply(sid, q, "it means the cache db", "open"),
           "reply answered": lambda: store.reply(sid, q, "postgres it is", "answered"),
           "status answered": lambda: store.update_item(sid, q, status="answered", declares=True),
           "status open": lambda: store.update_item(sid, q, status="open", declares=True),
           "person closes": lambda: store.update_item(sid, q, status="closed")}
    for step in steps:
        act[step]()
    assert store.item(sid, q)["status"] == status, desc
    assert ((sid, q) in store.awaiting()) == awaiting, desc
    assert ((sid, q) in store.processing()) == processing, desc
    counted = status == "open" and not processing
    assert store.sessions()[0]["open_questions"] == counted, f"{desc}: the session list counts it"


@pytest.mark.parametrize("kind, status, steps, processing, desc", [
    ("task", "running", ["send"], True, "a task the person spoke on last"),
    ("task", "running", ["send", "reply"], False, "a task the session replied on"),
    ("task", "running", ["send", "queue"], False, "a task with more queued"),
    ("task", "done", ["send"], False, "a finished task keeps its status"),
    ("decision", None, ["send"], True, "a decision the person spoke on last"),
    ("decision", None, ["send", "reply"], False, "a decision the session replied on"),
    ("agent", "running", ["send"], True, "a subagent the person spoke on last"),
])
def test_any_unfinished_item_can_be_processing(store, sid, kind, status, steps, processing, desc):
    extra = {"why": "over Postgres: no server; undo: swap the DSN"} if kind == "decision" else {}
    ref = store.post_item(sid, kind, "x", status=status, **extra)
    act = {"send": lambda: store.send(sid, "and?", ref), "queue": lambda: store.queue(sid, "more", ref),
           "reply": lambda: store.reply(sid, ref, "done that")}
    for step in steps:
        act[step]()
    assert ((sid, ref) in store.processing()) == processing, desc
    assert store.sessions()[0]["open_questions"] == 0, f"{desc}: only questions are counted"


def test_a_processing_question_ranks_below_one_awaiting_the_person(store, sid):
    sent = store.post_item(sid, "question", "sent")
    store.post_item(sid, "task", "blocked", status="blocked")
    store.post_item(sid, "question", "unanswered")
    store.send(sid, "postgres", sent)
    assert [i["title"] for i in store.items()] == ["unanswered", "blocked", "sent"]


def test_synopsis_is_stored_on_the_session(store, sid):
    assert store.session(sid)["synopsis"] == ""
    store.set_synopsis(sid, "  Building the thread view.  ")
    assert store.session(sid)["synopsis"] == "Building the thread view."


def test_an_older_database_gains_the_new_columns(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""CREATE TABLE sessions (id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
        ticket TEXT NOT NULL DEFAULT '', brief TEXT NOT NULL DEFAULT '', cwd TEXT NOT NULL,
        parked INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, launched_at TEXT, claude_pid INTEGER,
        claude_start INTEGER, boot_id TEXT, heartbeat_at TEXT);
        CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, item_ref TEXT,
        author TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL, delivered_at TEXT);
        CREATE INDEX messages_pending ON messages (session_id) WHERE delivered_at IS NULL;
        INSERT INTO sessions (id, cwd, created_at) VALUES ('s1', '/tmp', '2026-10-01T00:00:00+00:00');
        INSERT INTO messages (session_id, author, body, created_at) VALUES ('s1', 'person', 'hi', '2026-10-01T00:00:00+00:00');""")
    old.commit()
    old.close()
    store = Store(path)
    assert [m["body"] for m in store.pending("s1")] == ["hi"], "existing rows read as sent"
    assert store.session("s1")["synopsis"] == ""
    indexes = {r[1] for r in store.db.execute("PRAGMA index_list(messages)")}
    assert "messages_unsent" in indexes and "messages_pending" not in indexes


@pytest.mark.parametrize("stamp, expected, desc", [
    (None, True, "a session that never stamped runs older code"),
    ("register", False, "registering stamps the current version"),
    ("heartbeat", False, "a heartbeat stamps it"),
    ("mark_version", False, "the monitor stamps it on start"),
])
def test_needs_relaunch(store, sid, stamp, expected, desc):
    from claude_wheelhouse.store import needs_relaunch
    if stamp == "register":
        store.register(sid, 1, 1, "boot")
    elif stamp:
        getattr(store, stamp)(sid)
    assert needs_relaunch(store.session(sid)) is expected, desc


def test_decision_records_its_fields_and_stays_until_closed(store, sid):
    d = store.post_item(sid, "decision", "cache in SQLite", "Picked for the prototype.", **DECIDED)
    item = store.item(sid, d)
    assert (d, item["status"]) == ("D1", "unseen")
    assert item["body"] == "Picked for the prototype.\n\n**Why:** over Postgres: no server to run; undo: swap the DSN"
    assert store.sessions()[0]["unseen_decisions"] == 1
    assert store.sessions()[0]["open_questions"] == 0, "a decision doesn't ask for attention"
    assert store.mark_seen(sid, d) and not store.mark_seen(sid, d), "seen once"
    assert store.item(sid, d)["status"] == "seen"
    assert store.sessions()[0]["unseen_decisions"] == 0
    assert [r["ref"] for r in store.items(sid, include_closed=False)] == [d], "a seen decision stays"
    store.close_decision(sid, d)
    assert (store.item(sid, d)["status"], store.items(sid, include_closed=False)) == ("closed", []), "closed: finished"
    store.close_decision(sid, d, closed=False)
    assert store.item(sid, d)["status"] == "seen", "reopened as seen"
    store.reply(sid, d, "Reversed: back on Postgres.")   # pushing back is a thread reply
    assert store.thread(sid, d)[-1]["kind"] == "reply"


def test_mark_seen_leaves_other_kinds_alone(store, sid):
    q = store.post_item(sid, "question", "which?")
    assert not store.mark_seen(sid, q)
    assert store.item(sid, q)["status"] == "open"


@pytest.mark.parametrize("set_to, expected, desc", [
    (None, "queued", "a new session starts in the default mode, queued"),
    ("immediate", "immediate", "the person switched it"),
    ("queued", "queued", "and back"),
])
def test_send_mode(store, sid, set_to, expected, desc):
    from claude_wheelhouse.store import mode
    if set_to:
        store.set_mode(sid, set_to)
    assert mode(store.session(sid)) == expected, desc


def test_send_mode_refuses_a_made_up_mode(store, sid):
    with pytest.raises(ValueError):
        store.set_mode(sid, "eventually")


def test_decisions_seen_before_they_stayed_are_closed_once(db_file, sid, store):
    seen = store.post_item(sid, "decision", "old", **DECIDED)
    store.db.execute("UPDATE items SET status = 'seen' WHERE ref = ?", (seen,))
    store.db.execute("DELETE FROM settings WHERE key = 'migrated_decisions_close'")   # as before the change
    assert Store(db_file).item(sid, seen)["status"] == "closed", "seen under the old rule: closed"
    fresh = store.post_item(sid, "decision", "new", **DECIDED)
    store.mark_seen(sid, fresh)
    assert Store(db_file).item(sid, fresh)["status"] == "seen", "the migration runs once"


@pytest.mark.parametrize("name, desc", [
    ("Columbo check", "a new name"),
    ("", "cleared: the session shows its directory"),
])
def test_rename(store, sid, name, desc):
    store.rename(sid, name)
    assert store.session(sid)["name"] == name, desc


def test_a_database_with_the_dropped_rename_columns_still_works(db_file):
    old = Store(db_file)
    old.db.execute("ALTER TABLE sessions ADD COLUMN transcript_title TEXT")
    old.db.execute("ALTER TABLE sessions ADD COLUMN renamed_at TEXT")
    old.db.close()
    store = Store(db_file)
    sid = store.create_session("/tmp/x", name="demo")
    store.rename(sid, "Columbo check")
    assert store.session(sid)["name"] == "Columbo check"


def test_renaming_a_session_that_has_gone_says_so(store):
    with pytest.raises(SessionGone):
        store.rename("gone", "x")


def marks(store):
    return dict(store.db.execute("SELECT kind, n FROM marks"))


@pytest.mark.parametrize("write, moved, desc", [
    (lambda s, sid: s.heartbeat(sid), set(), "a heartbeat: the sessions carry no mark"),
    (lambda s, sid: s.post_item(sid, "task", "two"), {"items"}, "an item inserted"),
    (lambda s, sid: s.update_item(sid, "T1", title="again"), {"items"}, "an item updated"),
    (lambda s, sid: s.queue(sid, "later"), {"messages"}, "a message inserted"),
    (lambda s, sid: s.confirm(s.claim(sid)), {"messages"}, "a message updated"),
    (lambda s, sid: s.unqueue(s.drafts(sid)[0]["id"]), {"messages"}, "a message deleted"),
    (lambda s, sid: s.end(sid), {"items", "messages"}, "a session ended: its items and messages go by cascade"),
    (lambda s, sid: s.db.execute("UPDATE items SET body = 'by older code'"), {"items"},
     "a write that knows nothing of the marks, as older code's"),
])
def test_every_write_to_items_and_messages_moves_its_mark(store, sid, db_file, write, moved, desc):
    store.post_item(sid, "task", "do it")
    store.send(sid, "hello")
    store.queue(sid, "draft")
    before = marks(store)
    write(Store(db_file), sid)
    after = marks(store)
    assert {kind for kind in after if after[kind] != before[kind]} == moved, desc


def test_an_older_database_gains_the_marks_on_open(db_file, sid, store):
    for trigger in store.db.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall():
        store.db.execute(f"DROP TRIGGER {trigger[0]}")
    store.db.execute("DROP TABLE marks")
    again = Store(db_file)
    assert len(again.db.execute("SELECT 1 FROM sqlite_master WHERE type = 'trigger'").fetchall()) == len(TRIGGER_NAMES)
    store.post_item(sid, "task", "after")   # by the connection opened before: it sees the triggers too
    assert marks(again) == {"items": 1, "messages": 0}


def test_an_older_versions_marks_triggers_are_replaced_and_a_repeat_open_changes_nothing(db_file, sid, store):
    store.db.execute("DROP TRIGGER items_marked_insert_v1")
    store.db.execute("CREATE TRIGGER items_marked_insert AFTER INSERT ON items BEGIN SELECT 1; END")
    Store(db_file)
    names = {r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert names == set(TRIGGER_NAMES)
    cookie = store.db.execute("PRAGMA schema_version").fetchone()[0]
    Store(db_file)
    assert store.db.execute("PRAGMA schema_version").fetchone()[0] == cookie
    before = marks(store)["items"]
    store.post_item(sid, "task", "after")
    assert marks(store)["items"] == before + 1, "the item counted once, by the current trigger alone"


@pytest.mark.parametrize("name, kept, desc", [
    ("items_marked_insert_v2", True, "a newer version's, made by newer code: kept"),
    ("messages_marked_delete_v0", False, "an older version's: dropped"),
    ("items_marked_update", False, "one with no version, as before versions: dropped"),
    ("items_marked_seen", True, "a trigger not a marks one: kept"),
    ("items_seq_insert_v1", False, "#70's retired seq trigger, at the current version: dropped"),
    ("items_seq_update_v2", False, "a retired seq trigger at any version: dropped"),
])
def test_opening_drops_only_an_older_versions_marks_triggers(db_file, store, name, kept, desc):
    table = name.split("_")[0]
    store.db.execute(f"CREATE TRIGGER {name} AFTER INSERT ON {table} BEGIN SELECT 1; END")
    Store(db_file)
    names = {r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert (name in names) == kept and set(TRIGGER_NAMES) <= names, desc


@pytest.mark.parametrize("write, counted, desc", [
    (lambda s, sid: s.rename(sid, "x"), 1, "a write on this thread"),
    (lambda s, sid: s.rename(sid, s.session(sid)["name"]), 1, "a row rewritten as it was is still written"),
    (lambda s, sid: s.session(sid), 0, "a read"),
    (lambda s, sid: (t := threading.Thread(target=s.rename, args=(sid, "x"))).start() or t.join(), 0,
     "another thread's, on the same connection"),
    (lambda s, sid: s.answer_permission(sid, "P9", "allow"), 0, "a transaction that changed nothing"),
])
def test_written_counts_this_threads_committed_rows(store, sid, write, counted, desc):
    before = store.written()
    with contextlib.suppress(KeyError):
        write(store, sid)
    assert store.written() - before == counted, desc


def test_a_transaction_rolled_back_is_not_written(store, sid):
    before = store.written()
    with pytest.raises(RuntimeError), store.tx() as db:
        db.execute("UPDATE sessions SET name = 'x'")
        raise RuntimeError
    assert store.written() == before and store.session(sid)["name"] == "demo"



# 2bbb99a's items.seq, as a database that code opened carries it
SEQ_SCHEMA = ["ALTER TABLE items ADD COLUMN seq INTEGER",
              "CREATE UNIQUE INDEX items_seq ON items (seq)",
              *(f"CREATE TRIGGER items_seq_{name}_v1 AFTER {op} ON items BEGIN UPDATE items SET seq = "
                f"(SELECT coalesce(max(seq), 0) + 1 FROM items) WHERE id = NEW.id; END"
                for name, op in (("insert", "INSERT"), ("update", "UPDATE OF updated_at")))]


def test_a_database_that_had_seq_loses_its_triggers_and_index_on_open(db_file, sid, store):
    """#70: ties go by id now, so 2bbb99a's seq is retired: its triggers and unique index go
    on open, the column stays unused, and a repeat open changes nothing."""
    for statement in SEQ_SCHEMA:
        store.db.execute(statement)
    Store(db_file)
    schema = {r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type IN ('trigger', 'index')")}
    assert not {n for n in schema if n.startswith("items_seq")}, schema
    cookie = store.db.execute("PRAGMA schema_version").fetchone()[0]
    Store(db_file)
    assert store.db.execute("PRAGMA schema_version").fetchone()[0] == cookie, "a repeat open changes nothing"
    before = marks(store)["items"]
    ref = store.post_item(sid, "task", "after")
    assert store.item(sid, ref)["seq"] is None and marks(store)["items"] == before + 1, "nothing numbers seq"


def test_a_new_database_has_no_seq(store):
    assert "seq" not in {r[1] for r in store.db.execute("PRAGMA table_info(items)")}


TIE, EARLIER = "2026-10-08T10:00:00+00:00", "2026-10-08T09:59:59+00:00"


@pytest.mark.parametrize("posts, order, desc", [
    ([("task", "running")] * 3, "T3 T2 T1", "active: newest first, so of one second the higher id first"),
    ([("task", "done")] * 3, "T1 T2 T3", "settled: oldest first, so of one second the lower id first"),
    ([("task", "done"), ("task", "running"), ("task", "done"), ("task", "running")], "T4 T2 T1 T3",
     "each band in its own direction"),
    ([("question", "open"), ("task", "running"), ("question", "open")], "Q2 Q1 T1", "rank first, then the tie"),
    ([("task", "running"), ("task", "running", EARLIER)], "T1 T2", "active: time beats id, newer first"),
    ([("task", "done"), ("task", "done", EARLIER)], "T2 T1", "settled: time beats id, older first"),
    ([("question", "closed"), ("question", "closed", EARLIER)], "Q2 Q1", "finished: time beats id, older first"),
])
def test_items_that_tie_to_the_second_go_by_id_in_their_bands_direction(store, sid, posts, order, desc):
    """Doug: "Sort on time first, id second, to ensure stable ordering even when items are
    updated in the same second bucket." A post is stamped TIE unless it names its own time."""
    for kind, status, *at in posts:
        with mock.patch("claude_wheelhouse.store.now", lambda: at[0] if at else TIE):
            store.post_item(sid, kind, kind, status=status)
    assert " ".join(r["ref"] for r in store.items(sid)) == order, desc


@pytest.mark.parametrize("model, effort, error, desc", [
    ("claude-sonnet-5-5", "high", None, "the table's"),
    (None, None, None, "neither, as an adopted session"),
    ("sonnet", "high", "model must be one of", "an alias isn't stored: a pinned id is (Q49)"),
    ("claude-sonnet-5-5", "turbo", "effort must be one of", "nor a level Claude Code would ignore"),
])
def test_a_session_keeps_its_model_and_effort(store, tmp_path, model, effort, error, desc):
    if error:
        with pytest.raises(ValueError, match=error):
            store.create_session(str(tmp_path), model=model, effort=effort)
        assert store.sessions() == [], desc
        return
    sid = store.create_session(str(tmp_path), model=model, effort=effort)
    s = store.session(sid)
    assert (s["model"], s["effort"], s["applied_model"], s["applied_effort"]) == (model, effort, None, None), desc


@pytest.mark.parametrize("setter, value, column, error, desc", [
    ("set_model", "claude-fable-5-1", "model", None, "a model from the table"),
    ("set_effort", "max", "effort", None, "any level"),
    ("set_model", "claude-nonsense-9", "model", "model must be one of", "a model that isn't offered"),
    ("set_effort", "bogus", "effort", "effort must be one of", "a level that isn't one"),
])
def test_setting_model_and_effort(store, sid, setter, value, column, error, desc):
    before = store.session(sid)[column]
    if error:
        with pytest.raises(ValueError, match=error):
            getattr(store, setter)(sid, value)
        assert store.session(sid)[column] == before, desc
    else:
        getattr(store, setter)(sid, value)
        assert store.session(sid)[column] == value, desc


def test_setting_one_on_a_session_gone_says_so(store):
    with pytest.raises(SessionGone):
        store.set_effort("gone", "high")


def test_a_host_puts_back_what_claude_code_refused_and_records_what_it_has(store, sid):
    store.set_model(sid, "claude-opus-5-5")
    store.set_effort(sid, "max")
    store.put_back(sid, model="claude-haiku-5-5")
    store.set_applied(sid, "claude-haiku-5-5", "max")
    s = store.session(sid)
    assert (s["model"], s["effort"], s["applied_model"], s["applied_effort"]) == (
        "claude-haiku-5-5", "max", "claude-haiku-5-5", "max"), "only the column named goes back"


def test_older_code_still_writes_a_database_with_the_model_columns(store, tmp_path):
    """Older code (the editable install's other copies) never names the new columns: its
    insert, as it was, still works, and leaves them NULL."""
    store.db.execute("INSERT INTO sessions (id, name, ticket, brief, cwd, created_at, adopted, runner) "
                     "VALUES ('old', '', '', '', ?, '2026-10-01T00:00:00+00:00', 0, 'sdk')", (str(tmp_path),))
    s = store.session("old")
    assert (s["model"], s["effort"], s["applied_model"], s["applied_effort"]) == (None, None, None, None)


@pytest.mark.parametrize("before, posted, desc", [
    ([], True, "nothing said since the person's message: posted"),
    (["reply"], False, "the session replied itself since: not posted twice"),
    (["note"], True, "a progress note isn't a reply"),
    (["status"], True, "a status call isn't a reply: the host's text still goes"),
])
def test_the_host_posts_a_reply_once(store, sid, before, posted, desc):
    q = store.post_item(sid, "question", "db?")
    store.send(sid, "postgres?", q)
    last = store.thread(sid, q)[-1]["id"]
    for step in before:
        {"reply": lambda: store.reply(sid, q, "Replied myself.", "open"),
         "note": lambda: store.update_item(sid, q, note="looking"),
         "status": lambda: store.update_item(sid, q, status="answered", declares=True)}[step]()
    store.host_reply(sid, q, "The host's text.", after=last)
    assert ("The host's text." in [m["body"] for m in store.thread(sid, q)]) == posted, desc


@pytest.mark.parametrize("stored, expected, desc", [
    ([4242, 1, 99.0], (99.0, (4242, 1), None), "a plain relaunch: older code wrote this too"),
    ([4242, 1, 99.0, True, 50.0], (99.0, (4242, 1), 50.0), "compacting first, asked at 50"),
    ([4242, 1, 99.0, True], (99.0, (4242, 1), 0.0), "compacting first, from round 43's code: asked at no time"),
    ([4242, 1, 99.0, False], (99.0, (4242, 1), None), "round 43's plain relaunch"),
])
def test_relaunches_read_every_version(store, sid, stored, expected, desc):
    store.db.execute("UPDATE sessions SET relaunch = ?", (json.dumps(stored),))
    assert store.relaunches() == {sid: expected}, desc


@pytest.mark.parametrize("asked, stored, desc", [
    (None, [4242, 1, 99.0], "a plain relaunch is stored as older code reads it"),
    (50.0, [4242, 1, 99.0, True, 50.0], "compacting first"),
])
def test_set_relaunch(store, sid, asked, stored, desc):
    store.set_relaunch(sid, (4242, 1), 99.0, asked)
    assert json.loads(store.session(sid)["relaunch"]) == stored, desc
