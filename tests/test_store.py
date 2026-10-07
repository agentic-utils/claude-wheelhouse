import pytest

from claude_wheelhouse.store import Store


def test_pragmas_make_every_commit_durable(store):
    assert store.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store.db.execute("PRAGMA synchronous").fetchone()[0] == 2   # FULL


def test_refuses_windows_mount(monkeypatch):
    monkeypatch.setenv("WHEELHOUSE_DB", "/mnt/c/wheelhouse.db")
    with pytest.raises(ValueError, match="/mnt/"):
        Store()


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
    (lambda s, sid: s.reply(sid, s.post_item(sid, "task", "x"), "which?", asks=True), ValueError,
     "asks on a task: only a question reopens"),
])
def test_rejects_bad_writes(store, sid, call, error, desc):
    with pytest.raises(error):
        call(store, sid)


def test_answer_marks_question_answered_and_waits_for_delivery(store, sid):
    q = store.post_item(sid, "question", "which db?", "full detail")
    store.send(sid, "postgres", q)
    assert store.item(sid, q)["status"] == "answered"
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
    assert {store.item(sid, r)["status"] for r in (q3, q4)} == {"answered"}
    assert store.drafts() == [] and store.dispatch(sid) == 0


def test_a_queued_answer_can_be_taken_back(store, sid):
    store.queue(sid, "oops", "Q1")
    draft = store.drafts(sid)[0]
    assert store.unqueue(draft["id"]) == "oops"
    assert store.drafts() == [] and store.unqueue(draft["id"]) is None


@pytest.mark.parametrize("asks, status, desc", [
    (False, "answered", "a plain reply leaves the question as it was"),
    (True, "open", "a reply that asks back reopens it"),
])
def test_reply_joins_the_thread(store, sid, asks, status, desc):
    q = store.post_item(sid, "question", "db?")
    store.send(sid, "postgres?", q)
    store.reply(sid, q, "postgres it is", asks=asks)
    assert store.item(sid, q)["status"] == status, desc
    assert [(m["author"], m["kind"]) for m in store.thread(sid, q)] == [("person", None), ("claude", "reply")]


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


@pytest.mark.parametrize("steps, expected, desc", [
    (["queue", "dispatch"], "answered", "a queued answer marks the question answered when sent"),
    (["queue", "reopen", "dispatch"], "open", "a stale answer queued before the follow-up leaves it open"),
    (["queue", "reopen", "queue", "dispatch"], "answered", "an answer queued after the follow-up answers it"),
    (["reopen", "send"], "answered", "an answer sent now after the follow-up answers it"),
])
def test_a_follow_up_is_answered_only_by_a_later_answer(store, sid, steps, expected, desc):
    q = store.post_item(sid, "question", "db?")
    act = {"queue": lambda: store.queue(sid, "SQLite", q), "dispatch": lambda: store.dispatch(sid),
           "reopen": lambda: store.reply(sid, q, "which version?", asks=True),
           "send": lambda: store.send(sid, "3.45", q)}
    for step in steps:
        act[step]()
    assert store.item(sid, q)["status"] == expected, desc
