import contextlib
import threading

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
])
def test_the_session_declares_where_a_question_stands(store, sid, steps, status, awaiting, processing, desc):
    q = store.post_item(sid, "question", "db?")
    act = {"send": lambda: store.send(sid, "postgres?", q), "queue": lambda: store.queue(sid, "postgres?", q),
           "dispatch": lambda: store.dispatch(sid),
           "reply open": lambda: store.reply(sid, q, "it means the cache db", "open"),
           "reply answered": lambda: store.reply(sid, q, "postgres it is", "answered")}
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
    assert len(again.db.execute("SELECT 1 FROM sqlite_master WHERE type = 'trigger'").fetchall()) == 6
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
