import pytest

from claude_wheelhouse.store import SessionGone, Store


def test_pragmas_make_every_commit_durable(store):
    assert store.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store.db.execute("PRAGMA synchronous").fetchone()[0] == 2   # FULL


def test_refuses_windows_mount(monkeypatch):
    monkeypatch.setenv("WHEELHOUSE_DB", "/mnt/c/wheelhouse.db")
    with pytest.raises(ValueError, match="/mnt/"):
        Store()


DECIDED = {"alternative": "Postgres", "why": "no server to run", "reverse": "swap the DSN"}


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
    (lambda s, sid: s.post_item(sid, "decision", "used SQLite", alternative="Postgres", why="local"),
     ValueError, "a decision needs how to reverse it"),
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


@pytest.mark.parametrize("steps, status, awaiting, desc", [
    (["send"], "open", True, "the person asked: the ball is in the session's court"),
    (["send", "reply open"], "open", False, "a clarification answered: still waiting on the person"),
    (["send", "reply answered"], "answered", False, "the session has what it needs"),
    (["send", "reply answered", "send"], "answered", True, "a further word is awaiting a reply again"),
    (["queue"], "open", False, "a queued answer isn't with the session yet"),
    (["queue", "dispatch"], "open", True, "until it's sent"),
])
def test_the_session_declares_where_a_question_stands(store, sid, steps, status, awaiting, desc):
    q = store.post_item(sid, "question", "db?")
    act = {"send": lambda: store.send(sid, "postgres?", q), "queue": lambda: store.queue(sid, "postgres?", q),
           "dispatch": lambda: store.dispatch(sid),
           "reply open": lambda: store.reply(sid, q, "it means the cache db", "open"),
           "reply answered": lambda: store.reply(sid, q, "postgres it is", "answered")}
    for step in steps:
        act[step]()
    assert store.item(sid, q)["status"] == status, desc
    assert ((sid, q) in store.awaiting()) == awaiting, desc


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
    assert item["body"] == ("Picked for the prototype.\n\n**Alternative:** Postgres\n\n"
                            "**Why:** no server to run\n\n**To reverse:** swap the DSN")
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


def minute(m: int) -> str:
    return f"2026-10-09T10:{m:02d}:00.000000+00:00"


@pytest.mark.parametrize("steps, expected, desc", [
    ([(20, "take", "Columbo check")], ("Columbo check", True), "a /rename made since the session was created"),
    ([(5, "take", "old")], ("demo", False), "one from before it was created (adopted under a new name) isn't taken"),
    ([(20, "rename", "mine"), (15, "take", "old")], ("mine", False),
     "a rename in the wheelhouse isn't undone by an older /rename"),
    ([(20, "take", "a"), (30, "rename", "mine"), (40, "take", "b")], ("b", True), "a newer /rename wins"),
    ([(20, "take", "a"), (30, "rename", "mine"), (40, "take", "a")], ("a", True),
     "so does a newer /rename back to the name it had"),
    ([(20, "take", "a"), (20, "take", "a")], ("a", False), "the same /rename read again changes nothing"),
])
def test_the_most_recent_rename_wins(store, tmp_path, monkeypatch, steps, expected, desc):
    monkeypatch.setattr("claude_wheelhouse.store.now", lambda: minute(10))
    sid = store.create_session(str(tmp_path), name="demo")
    took = None
    for at, step, value in steps:
        monkeypatch.setattr("claude_wheelhouse.store.stamp", lambda at=at: minute(at))
        took = store.take_title(sid, minute(at), value) if step == "take" else store.rename(sid, value)
    assert (store.session(sid)["name"], took) == expected, desc


@pytest.mark.parametrize("transcript_title, name, expected, desc", [
    ("A", "B", ("B", False), "renamed in the wheelhouse under the first rename code: stamped, an older /rename stays out"),
    ("A", "", ("", False), "its name cleared there: the same"),
    ("A", "A", ("Columbo check", True), "its name the /rename taken: a /rename that code missed is taken"),
    (None, "demo", ("Columbo check", True), "never renamed: the same"),
    ("no column", "demo", ("Columbo check", True), "a store from before renaming: the same"),
])
def test_upgrading_keeps_renames_made_before_they_were_stamped(db_file, transcript_title, name, expected, desc):
    from claude_wheelhouse import store as store_module
    old = Store(db_file)
    sid = old.create_session("/tmp/x", name="demo")
    old.db.execute("UPDATE sessions SET name = ?, created_at = ? WHERE id = ?", (name, minute(10), sid))
    old.db.execute("ALTER TABLE sessions DROP COLUMN renamed_at")   # as before the change
    if transcript_title != "no column":
        old.db.execute("ALTER TABLE sessions ADD COLUMN transcript_title TEXT")
        old.db.execute("UPDATE sessions SET transcript_title = ?", (transcript_title,))
    old.db.close()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(store_module, "stamp", lambda: minute(40))   # the upgrade
        upgraded = Store(db_file)
    took = upgraded.take_title(sid, minute(20), "Columbo check")   # a /rename made before the upgrade
    assert (upgraded.session(sid)["name"], took) == expected, desc
    assert Store(db_file).session(sid)["renamed_at"] == upgraded.session(sid)["renamed_at"], "the backfill runs once"


@pytest.mark.parametrize("renamed_at, at, title, expected, desc", [
    (None, 20, "Columbo check", ("Columbo check", True), "a /rename older than the tail, never renamed since: taken"),
    (None, 20, "demo", ("demo", False), "its name already: nothing to take, and no rename stamped"),
    (15, 20, "Columbo check", ("demo", False), "renamed since it was created: can't tell which came last, so kept"),
    (None, 5, "Columbo check", ("demo", False), "the tail older than its creation: kept"),
])
def test_an_inferred_rename_is_taken_only_by_a_session_never_renamed(store, tmp_path, monkeypatch, renamed_at, at,
                                                                    title, expected, desc):
    monkeypatch.setattr("claude_wheelhouse.store.now", lambda: minute(10))
    sid = store.create_session(str(tmp_path), name="demo")
    if renamed_at:
        monkeypatch.setattr("claude_wheelhouse.store.stamp", lambda: minute(renamed_at))
        store.rename(sid, "demo")
    took = store.take_title(sid, minute(at), title, inferred=True)
    assert (store.session(sid)["name"], took) == expected, desc
    assert store.session(sid)["renamed_at"] == (minute(at) if took else renamed_at and minute(renamed_at)), desc


def test_renaming_a_session_that_has_gone_says_so(store):
    with pytest.raises(SessionGone):
        store.rename("gone", "x")
