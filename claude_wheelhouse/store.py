"""SQLite store: the only source of truth for the wheelhouse.

Every public write runs in its own transaction and is committed (and, with
synchronous=FULL in WAL mode, fsynced) before the call returns, so a crash
never loses a change the caller was told about.
"""

import functools
import json
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import features, models

DEFAULT_DB = Path.home() / ".local/state/claude-wheelhouse/wheelhouse.db"

# permission: a tool call waiting for the person's approval, posted by a session's SDK host
# (never by the session itself). Allow or Deny answer it; a message on it denies with that text.
# doc: a markdown document the session declares for the person to read (post_doc), M for markdown
KINDS = {"task": "T", "question": "Q", "agent": "A", "decision": "D", "permission": "P", "doc": "M"}
STATUSES = {
    "task": {"todo", "running", "blocked", "waiting", "done", "dropped"},
    "question": {"open", "answered", "closed"},
    "agent": {"running", "done", "failed"},
    # the person's state, never the session's: seen once viewed, closed when they close it
    "decision": {"unseen", "seen", "closed"},
    "permission": {"open", "allowed", "denied"},
    # the person's too: new until viewed, read once viewed, updated when posted again after that
    "doc": {"new", "updated", "read"},
}
INITIAL_STATUS = {"task": "todo", "question": "open", "agent": "running", "decision": "unseen",
                  "permission": "open", "doc": "new"}
CLOSED = {"done", "dropped", "closed", "failed", "allowed", "denied"}
# Where an item stands in the inbox (standing), one table for every kind (T71). Active: ranked
# as it has always been. Settled: done with, but kept in sight: dimmed, after the rest. Finished:
# off the inbox until F shows it (closed, a permission answered, or dismissed by the person).
SETTLED = {"task": {"done", "dropped"}, "question": {"answered"}, "decision": {"seen"},
           "agent": {"done", "failed"}, "permission": set(), "doc": {"read"}}
# kinds whose settled items the person dismisses (Delete), recorded in items.dismissed; a
# question or a decision is closed instead
DISMISSABLE = ("task", "agent", "doc")
# what a session may hand to a fork (Store.move_items): an item not under way and the
# person's to answer, never an agent, decision or permission
MOVABLE = {"task": ("todo", "blocked"), "question": ("open",)}
# what a decision records besides its title (what was decided): post_item's keyword name, label.
# One short line: the option passed over, the reason and how to undo it (#16: once three fields)
DECISION_FIELDS = (("why", "Why"),)
# Bump when a session still running older code (its MCP server and monitor keep the code
# they started with) would mishandle the store or miss new behaviour: the wheelhouse then
# shows it as needing a relaunch. 2: queued answers (draft messages) that older code would deliver.
# 3: reply declares a question's status. 4: decisions. 5: SDK-hosted sessions and permission items.
# 6: decisions listed in a report are posted as items. 7: the wheelhouse tracks subagents
# itself (subagents.py), so the protocol no longer asks a session to post them. 8: a hosted
# session's host posts its chat as the reply on the ref answered, so the protocol asks it
# for the status alone. 9: a decision's alternative, why and reverse are one short line, `why`.
# 10: the host posts only what a turn ends by saying, so the protocol asks it to end with the answer.
# 11: replies list the items still open (in_doubt), and the wheelhouse checks on them, so the
# protocol asks the session to correct any that's finished.
# 12: park_session and end_session stop the session's Claude Code once they return, and
# park asks for handoff notes: an older session's tab stays open, parked but running.
# 13: doc items (M), which the protocol asks a session to declare for docs meant for the person,
# and a subagent's description naming its item's ref, which files it under that item.
PROTOCOL_VERSION = 13
DRAFTS_VERSION = 2   # the first that holds a queued answer until it is sent
# How a session's answers go until the person toggles it: "queued" holds them until sent,
# "immediate" sends each as it's submitted. Stored per session; NULL means this default.
DEFAULT_MODE = "queued"
MODES = ("queued", "immediate")
# How a session runs: "sdk" in a wheelhouse host process (claude-wheelhouse host), "tab" as
# interactive Claude Code in a Windows Terminal tab. Stored per session; NULL (rows from before
# hosts existed) means tab. New sessions run DEFAULT_RUNNER, or with SDK mode on (features.py:
# experimental, the author's alone until Anthropic's terms are confirmed) SDK_DEFAULT_RUNNER;
# WHEELHOUSE_RUNNER overrides either, but never turns SDK mode on by itself.
RUNNERS = ("sdk", "tab")
DEFAULT_RUNNER = "tab"
SDK_DEFAULT_RUNNER = "sdk"
# What the person can ask a running host to do, handed over in sessions.host_command
HOST_COMMANDS = ("interrupt", "shell", "compact")
# what a host does once its Compact has compacted the session: tell it its open items, as
# after any compaction; that and send its queued answers, in the same one prompt; or park it
# at once, sending nothing more, so no prompt writes the compacted context to the cache
THEN = ("compact", "send", "park")
DEFAULT_THEN = "send"
# an item's answer column marks the host's question suggesting Compact, past 200k context,
# until the context is back under that; then it is marked done, and the next crossing asks again
COMPACT_QUESTION = '{"from": "context nudge"}'
COMPACT_QUESTION_DONE = '{"from": "context nudge", "done": true}'

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL DEFAULT '',
    ticket       TEXT NOT NULL DEFAULT '',
    brief        TEXT NOT NULL DEFAULT '',
    cwd          TEXT NOT NULL,
    parked       INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    launched_at  TEXT,
    claude_pid   INTEGER,
    claude_start INTEGER,
    boot_id      TEXT,
    heartbeat_at TEXT,
    end_requested_at  TEXT,
    park_requested_at TEXT,
    end_told_at  TEXT,
    park_told_at TEXT,
    adopted      INTEGER NOT NULL DEFAULT 0,
    synopsis     TEXT NOT NULL DEFAULT '',
    code_version INTEGER,
    send_mode    TEXT
);
CREATE TABLE IF NOT EXISTS items (
    id         INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    ref        TEXT NOT NULL,
    kind       TEXT NOT NULL,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    reopened_after INTEGER,   -- unused: kept for MCP servers still running older code
    UNIQUE (session_id, ref)
);
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY,
    session_id   TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    item_ref     TEXT,
    author       TEXT NOT NULL CHECK (author IN ('claude', 'person')),
    body         TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    claimed_at   TEXT,
    delivered_at TEXT,
    draft        INTEGER NOT NULL DEFAULT 0,
    kind         TEXT
);
-- the A items the wheelhouse made for (or matched to) a session's subagents, one per
-- agent id: so it never posts twice, across restarts and between two wheelhouses
CREATE TABLE IF NOT EXISTS agent_items (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    agent_id   TEXT NOT NULL,
    item_ref   TEXT NOT NULL,
    PRIMARY KEY (session_id, agent_id)
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- how many times each of items and messages has been written, by the triggers below: the
-- daemon's mark of each (MARKED), moved by any change a client could see, whoever writes it
CREATE TABLE IF NOT EXISTS marks (
    kind TEXT PRIMARY KEY,
    n    INTEGER NOT NULL DEFAULT 0
);
-- the daemon's answer to each request a client sent with an Idempotency-Key, for a day
-- (KEYS_KEPT): the same request sent again is answered the same and done once
CREATE TABLE IF NOT EXISTS idempotency (
    key         TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    status      INTEGER NOT NULL,
    body        TEXT NOT NULL,
    at          TEXT NOT NULL
);
"""
# run after ADDED_COLUMNS, which may have just added the columns they use
INDEXES = """
DROP INDEX IF EXISTS messages_pending;
CREATE INDEX IF NOT EXISTS messages_unsent
    ON messages (session_id) WHERE delivered_at IS NULL AND draft = 0;
DROP INDEX IF EXISTS items_seq;
-- sessions(), each tick: each session's latest word and item (last_at) and its drafts
DROP INDEX IF EXISTS messages_session_created;
DROP INDEX IF EXISTS items_session_updated;
CREATE INDEX IF NOT EXISTS messages_session_author ON messages (session_id, author, created_at);
CREATE INDEX IF NOT EXISTS items_session_at ON items (session_id, session_at);
CREATE INDEX IF NOT EXISTS items_session_created ON items (session_id, created_at);
CREATE INDEX IF NOT EXISTS messages_drafts ON messages (session_id) WHERE draft = 1;
"""
# Triggers fire for any connection: in the schema, so a writer running older code fires
# them too once any newer one has opened the database. Each is one statement of its own, as
# its body holds a ";". Their names carry TRIGGERS_VERSION, as CREATE TRIGGER IF NOT EXISTS
# never replaces one: a changed body takes the next version, and opening drops any of them
# of an older version (none counts as 0). A newer version's stay, so older code reopening
# never drops them: the two sets then both fire, which is harmless for each below.
# The marks: each table the daemon marks (marks) bumped on every row inserted, updated or
# deleted. The daemon compares marks only for equality, so a write may bump one twice
MARKED = ("items", "messages")
TRIGGERS_VERSION = 1
TRIGGER_BODIES = {f"{table}_marked_{op.lower()}": f"AFTER {op} ON {table} "
                  f"BEGIN UPDATE marks SET n = n + 1 WHERE kind = '{table}'; END"
                  for table in MARKED for op in ("INSERT", "UPDATE", "DELETE")}
# A row the person placed under a Status sort (pos_group status*, status▲, status▼) is placed
# only until its status changes, whoever writes it: from then on it goes by status, until
# dragged again (T121). Not with ordering off (manual), where nothing moves by itself, nor
# under another column's sort, which its status leaves alone
TRIGGER_BODIES["items_unplaced_on_status"] = (
    "AFTER UPDATE OF status ON items WHEN OLD.status IS NOT NEW.status AND NEW.pos_group LIKE 'status%' "
    "BEGIN UPDATE items SET pos = NULL, pos_group = NULL WHERE id = NEW.id; END")
OUR_TRIGGER = re.compile(rf"^(?:{'|'.join(TRIGGER_BODIES)})(?:_v(\d+))?$")
# retired, every version dropped on open: items_seq_* numbered each write of updated_at in
# items.seq (#70, 2bbb99a), with the unique index items_seq (INDEXES drops it). Ties now go
# by id (inbox_rank), so nothing reads seq. 2bbb99a must not run against a database this
# code has opened: items posted here have a NULL seq, and 2bbb99a recreates the triggers
# without renumbering, so its items() fails comparing None with a number. It never ran live
# (the live processes predate it). The column stays, unused: dropping it rewrites the table
RETIRED_TRIGGER = re.compile(r"^items_seq_(?:insert|update)(?:_v\d+)?$")
TRIGGER_NAMES = {f"{name}_v{TRIGGERS_VERSION}": body for name, body in TRIGGER_BODIES.items()}
TRIGGERS = [f"CREATE TRIGGER IF NOT EXISTS {name} {body}" for name, body in TRIGGER_NAMES.items()]

# columns added after the first release: (table, column, type)
ADDED_COLUMNS = [("sessions", "end_requested_at", "TEXT"), ("sessions", "park_requested_at", "TEXT"),
                 ("sessions", "end_told_at", "TEXT"), ("sessions", "park_told_at", "TEXT"),
                 ("messages", "claimed_at", "TEXT"), ("sessions", "adopted", "INTEGER NOT NULL DEFAULT 0"),
                 ("messages", "draft", "INTEGER NOT NULL DEFAULT 0"), ("messages", "kind", "TEXT"),
                 ("sessions", "synopsis", "TEXT NOT NULL DEFAULT ''"), ("sessions", "code_version", "INTEGER"),
                 ("items", "reopened_after", "INTEGER"), ("sessions", "send_mode", "TEXT"),
                 ("sessions", "runner", "TEXT"), ("sessions", "activity", "TEXT NOT NULL DEFAULT ''"),
                 ("sessions", "host_command", "TEXT"), ("sessions", "shell", "TEXT"),
                 ("sessions", "context_tokens", "INTEGER"), ("sessions", "context_max", "INTEGER"),
                 ("items", "answer", "TEXT"), ("sessions", "context_at", "TEXT"),
                 ("items", "dismissed", "TEXT"), ("sessions", "relaunch", "TEXT"),
                 ("sessions", "model", "TEXT"), ("sessions", "effort", "TEXT"),
                 ("sessions", "applied_model", "TEXT"), ("sessions", "applied_effort", "TEXT"),
                 ("sessions", "interrupted_at", "TEXT"), ("sessions", "model_chosen_at", "TEXT"),
                 ("sessions", "effort_chosen_at", "TEXT"), ("sessions", "launched_over", "INTEGER"),
                 ("items", "session_at", "TEXT"), ("items", "moved_to", "TEXT"), ("sessions", "compact_plan", "TEXT"),
                 ("sessions", "park_compact", "INTEGER NOT NULL DEFAULT 0"), ("items", "parent_ref", "TEXT"),
                 # the person's order (ordering.py): a row's place, and the group it was placed in
                 ("items", "pos", "REAL"), ("items", "pos_group", "TEXT"),
                 ("sessions", "pos", "REAL"), ("sessions", "pos_group", "TEXT")]
# older databases may also carry sessions.transcript_title and sessions.renamed_at,
# from a /rename pickup since dropped, and items.seq (RETIRED_TRIGGER): unused, and left in place
DECISIONS_CLOSE = "migrated_decisions_close"   # settings: the one-off migration above has run
# settings: when this database was first opened by code that tracks subagents. Subagents
# that had finished by then (or by when their session joined) get no item (#69)
AGENTS_SINCE = "agents_since"
# seconds: how long before a subagent starts the session's own A item for it may be made
AGENT_MATCH_BEFORE = 600
AGENT_MATCH_AFTER = 15   # subagents.GRACE: by then the wheelhouse makes its own
# an item the session has been silent on this long, in a transient state (in_doubt), is one
# the wheelhouse asks it about: still open, or finished and never closed?
DOUBT_AFTER = timedelta(minutes=15)
TRANSIENT = ("running", "blocked", "waiting")   # a task's statuses that are under way
REQUESTS = ("end", "park")   # what the wheelhouse can ask a running session to do
CLAIM_TIMEOUT = 30   # seconds before a claim from a monitor that died mid-print is retaken
KEYS_KEPT = timedelta(days=1)   # how long the daemon answers a request sent again the same


class SessionGone(LookupError):
    """The session's row has gone: it ended itself, or was force-ended in the wheelhouse."""

    def __init__(self, sid: str = ""):
        super().__init__(f"session {sid} no longer exists")


GONE_TEXT = ("[wheelhouse] This session was force-ended in the wheelhouse (or has ended): its wheelhouse data "
             "is gone. Stop using wheelhouse tools.")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stamp() -> str:
    """A precise timestamp: tells one claim or request from the next within a second."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def mode(session) -> str:
    """The session's send mode: queued or immediate."""
    return session["send_mode"] or DEFAULT_MODE


def runner(session) -> str:
    """How the session runs: sdk (a wheelhouse host) or tab (interactive Claude Code)."""
    return (session["runner"] if "runner" in session.keys() else None) or "tab"


def default_runner(sdk: bool | None = None) -> str:
    """How a new session runs. sdk: whether SDK mode is on, else features.sdk_enabled().
    With it off, a session runs in a tab whatever WHEELHOUSE_RUNNER says."""
    if sdk is None:
        sdk = features.sdk_enabled()
    r = os.environ.get("WHEELHOUSE_RUNNER") or (SDK_DEFAULT_RUNNER if sdk else DEFAULT_RUNNER)
    if r not in RUNNERS:
        raise ValueError(f"WHEELHOUSE_RUNNER must be one of {', '.join(RUNNERS)}, not {r!r}")
    return r if sdk else "tab"


def needs_relaunch(session) -> bool:
    """The session's MCP server and monitor run code older than this store expects (or
    stamp no version at all): a relaunch picks up the new code."""
    return (session["code_version"] or 0) < PROTOCOL_VERSION


def can_queue(session) -> bool:
    """The session's code holds a queued answer until it is sent. Older code (or none
    stamped) would deliver it at once, so the wheelhouse sends instead."""
    return (session["code_version"] or 0) >= DRAFTS_VERSION


def db_path() -> Path:
    path = Path(os.environ.get("WHEELHOUSE_DB") or DEFAULT_DB).expanduser()
    if str(path.resolve()).startswith("/mnt/"):
        raise ValueError(f"refusing {path}: SQLite locking on /mnt/ is unreliable, use the Linux filesystem")
    return path


class Store:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # autocommit mode: transactions are explicit, one per write
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=10, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        # one connection, possibly many threads (the MCP server runs tools in worker threads)
        self.lock = threading.RLock()
        self.mine = threading.local()   # rows each thread's committed transactions changed (written)
        with self.tx() as db:   # one transaction, so processes starting together can't both migrate
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)
            added = set()
            for table, column, kind in ADDED_COLUMNS:
                if column not in {r[1] for r in db.execute(f"PRAGMA table_info({table})")}:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
                    added.add((table, column))
            if ("items", "dismissed") in added:
                # settled tasks and subagents used to be off the inbox: those settled by the
                # upgrade stay off it, dismissed, rather than flood it, dimmed
                for kind in DISMISSABLE:
                    db.execute(f"UPDATE items SET dismissed = status WHERE kind = ? AND status IN "
                               f"({','.join('?' * len(SETTLED[kind]))})", (kind, *SETTLED[kind]))
            if ("items", "session_at") in added:
                # the session's latest word on each item before the column (a note, reply or
                # status, author claude), so an item it updated a minute ago isn't in doubt
                db.execute("""UPDATE items SET session_at = (SELECT max(m.created_at) FROM messages m
                              WHERE m.session_id = items.session_id AND m.item_ref = items.ref
                              AND m.author = 'claude')""")
            # a model or effort set before choices were dated counts as chosen when this code
            # first opened the database: never older than a /model typed weeks before (Q56)
            for column in ("model", "effort"):
                if ("sessions", f"{column}_chosen_at") in added:
                    db.execute(f"UPDATE sessions SET {column}_chosen_at = ? WHERE {column} IS NOT NULL", (stamp(),))
            for statement in INDEXES.split(";"):
                if statement.strip():
                    db.execute(statement)
            db.executemany("INSERT OR IGNORE INTO marks (kind) VALUES (?)", [(t,) for t in MARKED])
            for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall():
                if RETIRED_TRIGGER.match(name) or ((match := OUR_TRIGGER.match(name))
                                                   and int(match[1] or 0) < TRIGGERS_VERSION):
                    db.execute(f'DROP TRIGGER "{name}"')
            for trigger in TRIGGERS:
                db.execute(trigger)
            # a decision used to be finished once seen, and now stays until closed: those
            # seen before then are closed, once, rather than come back to the inbox
            if db.execute("SELECT 1 FROM settings WHERE key = ?", (DECISIONS_CLOSE,)).fetchone() is None:
                db.execute("UPDATE items SET status = 'closed' WHERE kind = 'decision' AND status = 'seen'")
                db.execute("INSERT INTO settings (key, value) VALUES (?, '1')", (DECISIONS_CLOSE,))
            db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (AGENTS_SINCE, now()))

    @contextmanager
    def tx(self):
        with self.lock:
            before = self.db.total_changes
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.execute("COMMIT")
            except BaseException:
                # a COMMIT that raises (SQLITE_BUSY, a disk error) leaves the transaction
                # open, and every later BEGIN would fail on it (#3)
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                raise
            self.mine.n = self.written() + self.db.total_changes - before

    def written(self) -> int:
        """How many rows this thread's transactions have changed, counting only those that
        committed: another thread's writes on the same connection (a subagent sync on a
        worker) never count, nor a transaction rolled back."""
        return getattr(self.mine, "n", 0)

    def _all(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute(sql, params).fetchall()

    def _one(self, sql: str, params=()) -> sqlite3.Row | None:
        with self.lock:
            return self.db.execute(sql, params).fetchone()

    @staticmethod
    def _require(db, sid: str) -> None:
        """Inside a write transaction (BEGIN IMMEDIATE holds the write lock), so the row
        can't vanish between this check and the write that follows."""
        if db.execute("SELECT 1 FROM sessions WHERE id = ?", (sid,)).fetchone() is None:
            raise SessionGone(sid)

    # sessions

    def create_session(self, cwd: str, name: str = "", ticket: str = "", brief: str = "",
                       sid: str | None = None, runner: str | None = None, model: str | None = None,
                       effort: str | None = None) -> str:
        """A new session, or (with sid) an adopted one keeping its Claude session id. Its
        model and effort, if given, are launched with, chosen now; unset (adopted), Claude Code's own."""
        adopted = sid is not None
        sid = sid or str(uuid.uuid4())
        runner = runner or default_runner()
        if runner not in RUNNERS:
            raise ValueError(f"runner must be one of {', '.join(RUNNERS)}, not {runner!r}")
        if model is not None:
            models.check_model(model)
        if effort is not None:
            models.check_effort(effort)
        with self.tx() as db:
            db.execute(
                "INSERT INTO sessions (id, name, ticket, brief, cwd, created_at, adopted, runner, model, effort, "
                "model_chosen_at, effort_chosen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, name, ticket, brief, cwd, now(), int(adopted), runner, model, effort,
                 model and stamp(), effort and stamp()),
            )
        return sid

    def set_model(self, sid: str, model: str) -> None:
        """The model the session runs on, chosen now: its host applies it live, every launch
        passes it."""
        models.check_model(model)
        with self.tx() as db:
            self._require(db, sid)
            db.execute("UPDATE sessions SET model = ?, model_chosen_at = ? WHERE id = ?", (model, stamp(), sid))

    def set_effort(self, sid: str, effort: str) -> None:
        """The effort the session runs at, chosen now: its host applies it live, every launch
        passes it."""
        models.check_effort(effort)
        with self.tx() as db:
            self._require(db, sid)
            db.execute("UPDATE sessions SET effort = ?, effort_chosen_at = ? WHERE id = ?", (effort, stamp(), sid))

    def carry(self, sid: str, column: str, value: str, at: float) -> bool:
        """A model or effort typed in a tab at `at` (epoch seconds), carried to its column
        (Q56): last write wins. Taken where it differs from the column and is newer than the
        column's last choice (model_chosen_at, effort_chosen_at; none counts as older), and
        the choice is then dated `at`. A tie goes to the column. Unchecked, as it is what
        ran. Read and written in one write transaction, so a choice made meanwhile is never
        undone. Whether it was taken."""
        if column not in ("model", "effort"):
            raise ValueError(f"column must be model or effort, not {column!r}")
        with self.tx() as db:
            row = db.execute(f"SELECT {column}, {column}_chosen_at FROM sessions WHERE id = ?", (sid,)).fetchone()
            if row is None or row[0] == value or (row[1] and datetime.fromisoformat(row[1]).timestamp() >= at):
                return False
            db.execute(f"UPDATE sessions SET {column} = ?, {column}_chosen_at = ? WHERE id = ?",
                       (value, datetime.fromtimestamp(at, timezone.utc).isoformat(timespec="microseconds"), sid))
        return True

    def put_back(self, sid: str, sent: dict, **values) -> None:
        """The model or effort column put back to what Claude Code has in force, by a host
        Claude Code refused the change: not a choice, so its time stays. Unchecked, as it is
        what runs. Only where the column
        still holds what was sent (sent): a newer choice made meanwhile stands, and is the
        host's to apply next."""
        with self.tx() as db:
            for column in ("model", "effort"):
                if column in values:
                    db.execute(f"UPDATE sessions SET {column} = ? WHERE id = ? AND {column} IS ?",
                               (values[column], sid, sent.get(column)))

    def set_applied(self, sid: str, model: str | None, effort: str | None) -> None:
        """What the session's host has in force, as Claude Code last confirmed it."""
        with self.tx() as db:
            db.execute("UPDATE sessions SET applied_model = ?, applied_effort = ? WHERE id = ?",
                       (model, effort, sid))

    def set_runner(self, sid: str, runner: str) -> None:
        if runner not in RUNNERS:
            raise ValueError(f"runner must be one of {', '.join(RUNNERS)}, not {runner!r}")
        with self.tx() as db:
            self._require(db, sid)
            db.execute("UPDATE sessions SET runner = ? WHERE id = ?", (runner, sid))

    # a session's SDK host

    def set_activity(self, sid: str, text: str) -> None:
        """What the host's session is doing now, one line: idle, thinking, running Bash..."""
        with self.tx() as db:
            db.execute("UPDATE sessions SET activity = ? WHERE id = ?", (text, sid))

    def set_context(self, sid: str, tokens: int, max_tokens: int) -> None:
        """The context size the host's Claude Code reports, and when: the stats read it after
        a compaction, before the transcript has a response to size it by."""
        with self.tx() as db:
            db.execute("UPDATE sessions SET context_tokens = ?, context_max = ?, context_at = ? WHERE id = ?",
                       (tokens, max_tokens, stamp(), sid))

    def command(self, sid: str, what: str, notes: str | None = None, then: str | None = None) -> None:
        """Ask the session's host to interrupt, hand over to a shell tab, or compact. A newer
        command replaces one the host hasn't taken yet. A Compact may carry the person's
        instructions for it (notes: the session isn't asked what to keep) and what follows it
        (THEN; None, as before: its open items, as after any compaction)."""
        if what not in HOST_COMMANDS:
            raise ValueError(f"command must be one of {', '.join(HOST_COMMANDS)}, not {what!r}")
        if then is not None and then not in THEN:
            raise ValueError(f"then must be one of {', '.join(THEN)}, not {then!r}")
        plan = json.dumps({"notes": notes or None, "then": then}) if what == "compact" else None
        with self.tx() as db:
            self._require(db, sid)
            db.execute("UPDATE sessions SET host_command = ?, compact_plan = ? WHERE id = ?", (what, plan, sid))

    def compact_plan(self, sid: str, take: bool = False) -> tuple[str | None, str | None]:
        """The Compact command's instructions and what follows it (command), as last pressed;
        taken (cleared) by the host as it carries it out (take)."""
        with self.tx() as db:
            row = db.execute("SELECT compact_plan FROM sessions WHERE id = ?", (sid,)).fetchone()
            if take:
                db.execute("UPDATE sessions SET compact_plan = NULL WHERE id = ?", (sid,))
        plan = json.loads(row[0]) if row and row[0] else {}
        return plan.get("notes"), plan.get("then")

    def take_command(self, sid: str, only: str | None = None) -> str | None:
        """The host takes the pending command, once. With `only`, it is taken only if it is
        that one: another, pressed meanwhile, stays for the host."""
        with self.tx() as db:   # read and clear in one write transaction
            row = db.execute("SELECT host_command FROM sessions WHERE id = ?", (sid,)).fetchone()
            if row and row[0] and only in (None, row[0]):
                db.execute("UPDATE sessions SET host_command = NULL WHERE id = ?", (sid,))
            else:
                return None
        return row[0]

    def set_interrupted(self, sid: str) -> None:
        """The host has interrupted the session, now: kept, as its activity line isn't (the
        interrupted turn's end, or the next turn, writes over that)."""
        with self.tx() as db:
            db.execute("UPDATE sessions SET interrupted_at = ? WHERE id = ?", (stamp(), sid))

    def set_answer(self, sid: str, ref: str, answer: str) -> None:
        """An item's answer column: a permission's decision, or the host marking a question
        it asked for Claude Code's AskUserQuestion."""
        with self.tx() as db:
            db.execute("UPDATE items SET answer = ? WHERE session_id = ? AND ref = ?", (answer, sid, ref))

    def set_shell(self, sid: str, state: str | None) -> None:
        """tab: the host has handed the session to an interactive tab and waits for it to exit."""
        with self.tx() as db:
            db.execute("UPDATE sessions SET shell = ? WHERE id = ?", (state, sid))

    def set_relaunch(self, sid: str, host: tuple | None, deadline: float = 0.0, asked: float | None = None,
                     may_tab: bool = True) -> None:
        """A Relaunch under way: the host it stopped (or, compacting first, will stop once
        the session is compacted, the epoch second Compact was asked for in `asked`), (pid,
        start time), and the epoch second it gives up by, so a wheelhouse started again
        carries it on; None once it's done. Compacting first, the row has five values: the
        Hub of older code (from before #16's token savings) unpacks three, and would fail to
        start on one if the code were rolled back while it is under way. Asked for by another
        machine (may_tab false: relaunch_may_tab), a sixth, false: the five-value reader
        (pid, start, deadline, *rest) ignores it, and the three-value one fails on it as it
        does on five."""
        if not host:
            relaunch = None
        else:
            relaunch = [*host, deadline] + ([asked is not None, asked] if asked is not None or not may_tab else [])
            relaunch += [] if may_tab else [False]
        with self.tx() as db:
            db.execute("UPDATE sessions SET relaunch = ? WHERE id = ?",
                       (json.dumps(relaunch) if relaunch else None, sid))

    def relaunches(self) -> dict[str, tuple[float, tuple, float | None]]:
        """Each Relaunch under way, by session: (its epoch deadline, the host it stopped,
        and, compacting first, the epoch second Compact was asked for, else None)."""
        return {r["id"]: (deadline, (pid, start), (rest[1] if len(rest) > 1 else 0.0) if rest and rest[0] else None)
                for r in self._all("SELECT id, relaunch FROM sessions WHERE relaunch IS NOT NULL")
                for pid, start, deadline, *rest in [json.loads(r["relaunch"])]}

    def relaunch_may_tab(self, sid: str) -> bool:
        """Whether the Relaunch under way may bring the session back in a tab: not one another
        machine asked for (set_relaunch), where a tab here would go unseen."""
        s = self.session(sid)
        relaunch = json.loads(s["relaunch"]) if s is not None and s["relaunch"] else []
        return len(relaunch) < 6 or relaunch[5] is not False

    def unregister(self, sid: str) -> None:
        """The registered process is letting go of the session (a host handing over to a tab):
        forget it and the launch, so the next launch isn't refused as running or starting."""
        with self.tx() as db:
            db.execute("UPDATE sessions SET claude_pid = NULL, claude_start = NULL, launched_at = NULL "
                       "WHERE id = ?", (sid,))

    def session(self, sid: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM sessions WHERE id = ?", (sid,))

    def sessions(self) -> list[sqlite3.Row]:
        return self._all(
            f"""SELECT s.*,
                 (SELECT count(*) FROM items i WHERE i.session_id = s.id
                    AND i.kind = 'question' AND i.status = 'open' AND NOT {PROCESSING}) AS open_questions,
                 (SELECT count(*) FROM items i WHERE i.session_id = s.id
                    AND i.status = 'running') AS running,
                 (SELECT count(*) FROM messages m WHERE m.session_id = s.id AND m.draft = 1) AS drafts,
                 (SELECT count(*) FROM items i WHERE i.session_id = s.id
                    AND i.kind = 'decision' AND i.status = 'unseen') AS unseen_decisions,
                 (SELECT count(*) FROM agent_items a JOIN items i ON i.session_id = a.session_id
                    AND i.ref = a.item_ref WHERE a.session_id = s.id AND i.status = 'running') AS running_agents,
                 -- the session's own latest activity (ordering.py): its words, not the person's
                 -- (a decision seen, an answer queued), so their own acts never re-sort the list
                 max(s.created_at, coalesce(s.launched_at, ''),
                     coalesce((SELECT max(m.created_at) FROM messages m WHERE m.session_id = s.id
                               AND m.author = 'claude'), ''),
                     coalesce((SELECT max(i.session_at) FROM items i WHERE i.session_id = s.id), ''),
                     coalesce((SELECT max(i.created_at) FROM items i WHERE i.session_id = s.id), '')) AS last_at
               FROM sessions s ORDER BY s.created_at, s.rowid"""   # created_at ties: to the second
        )

    def rename(self, sid: str, name: str) -> None:
        """A rename in the wheelhouse, the only place a session's name changes."""
        with self.tx() as db:
            self._require(db, sid)
            db.execute("UPDATE sessions SET name = ? WHERE id = ?", (name, sid))

    def set_synopsis(self, sid: str, text: str) -> None:
        with self.tx() as db:
            self._require(db, sid)
            db.execute("UPDATE sessions SET synopsis = ? WHERE id = ?", (text.strip(), sid))

    def mark_launched(self, sid: str) -> None:
        """A (re)launch also drops any request left over from the last run. It keeps the last
        run's pid, which register_if_free compares against, and notes its start time in
        launched_over, so a registration since this launch shows (liveness.registered_this_launch)."""
        with self.tx() as db:
            db.execute("""UPDATE sessions SET launched_at = ?, launched_over = claude_start,
                          end_requested_at = NULL, park_requested_at = NULL, end_told_at = NULL, park_told_at = NULL
                          WHERE id = ?""", (now(), sid))

    def register(self, sid: str, pid: int, start: int, boot_id: str) -> None:
        with self.tx() as db:
            self._register(db, sid, pid, start, boot_id)

    def register_if_free(self, sid: str, pid: int, start: int, boot_id: str, is_alive) -> bool:
        """Compare-and-set: register only if no live Claude holds the session. One
        transaction, so two tabs racing to start the same session can't both win."""
        with self.tx() as db:
            s = db.execute("SELECT claude_pid, claude_start, boot_id FROM sessions WHERE id = ?", (sid,)).fetchone()
            if s is None or is_alive(s["claude_pid"], s["claude_start"], s["boot_id"]):
                return False
            self._register(db, sid, pid, start, boot_id)
        return True

    @staticmethod
    def _register(db, sid, pid, start, boot_id) -> None:
        db.execute(
            "UPDATE sessions SET claude_pid = ?, claude_start = ?, boot_id = ?, heartbeat_at = ?, "
            "code_version = ? WHERE id = ?",
            (pid, start, boot_id, now(), PROTOCOL_VERSION, sid),
        )

    def request_end(self, sid: str) -> None:
        self.request(sid, "end")

    def request_park(self, sid: str) -> None:
        self.request(sid, "park")

    def request(self, sid: str, what: str, compact: bool = False) -> None:
        """The person pressed End or Park on a running session; its monitor passes this on.
        Nothing happens to the data until the session acts, or the person forces it. A Park
        may ask a host to compact the session once it has parked, before it stops (compact)."""
        assert what in REQUESTS
        with self.tx() as db:
            self._require(db, sid)
            db.execute(f"UPDATE sessions SET {what}_requested_at = coalesce({what}_requested_at, ?) "
                       "WHERE id = ?", (stamp(), sid))
            if what == "park":
                db.execute("UPDATE sessions SET park_compact = ? WHERE id = ?", (int(compact), sid))

    def take_park_compact(self, sid: str) -> bool:
        """The host takes the Park's Compact first (request), once: to compact, or to drop it
        (the park not done in time)."""
        with self.tx() as db:
            return db.execute("UPDATE sessions SET park_compact = 0 WHERE id = ? AND park_compact = 1",
                              (sid,)).rowcount == 1

    def tell_request(self, sid: str, what: str, asked: str) -> bool:
        """The monitor is about to pass a request on: record that, only if it is still the
        current request (a cancel may have just cleared it). Returns whether to print it."""
        with self.tx() as db:
            return db.execute(f"UPDATE sessions SET {what}_told_at = ? WHERE id = ? "
                              f"AND {what}_requested_at = ?", (asked, sid, asked)).rowcount == 1

    def untell_request(self, sid: str, what: str) -> None:
        """Printing the request failed: it was never passed on after all."""
        with self.tx() as db:
            db.execute(f"UPDATE sessions SET {what}_told_at = NULL WHERE id = ?", (sid,))

    def cancel_request(self, sid: str, what: str) -> None:
        """Clear a request. Only a session that was told about it hears that it's cancelled."""
        assert what in REQUESTS
        with self.tx() as db:
            self._require(db, sid)
            told = db.execute(f"SELECT {what}_told_at FROM sessions WHERE id = ?", (sid,)).fetchone()[0]
            db.execute(f"UPDATE sessions SET {what}_requested_at = NULL, {what}_told_at = NULL "
                       "WHERE id = ?", (sid,))
            if what == "park":
                db.execute("UPDATE sessions SET park_compact = 0 WHERE id = ?", (sid,))
            if told:
                db.execute(
                    "INSERT INTO messages (session_id, author, body, created_at) VALUES (?, 'person', ?, ?)",
                    (sid, f"The person cancelled the {what} request: carry on as before.", now()),
                )

    def heartbeat(self, sid: str) -> None:
        with self.tx() as db:
            db.execute("UPDATE sessions SET heartbeat_at = ?, code_version = ? WHERE id = ?",
                       (now(), PROTOCOL_VERSION, sid))

    def mark_version(self, sid: str) -> None:
        """Stamp this code's PROTOCOL_VERSION on the session (the monitor, as it starts)."""
        with self.tx() as db:
            db.execute("UPDATE sessions SET code_version = ? WHERE id = ?", (PROTOCOL_VERSION, sid))

    def set_mode(self, sid: str, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}, not {mode!r}")
        with self.tx() as db:
            self._require(db, sid)
            db.execute("UPDATE sessions SET send_mode = ? WHERE id = ?", (mode, sid))

    def set_parked(self, sid: str, parked: bool) -> None:
        """Parking (by the session or by force) also settles any pending park request, all
        but its Compact first, which the host takes once the session has parked; unparking
        drops that too."""
        with self.tx() as db:
            self._require(db, sid)
            db.execute("UPDATE sessions SET parked = ?, park_requested_at = NULL, park_told_at = NULL, "
                       "park_compact = park_compact * ? WHERE id = ?", (int(parked), int(parked), sid))

    def end(self, sid: str) -> None:
        with self.tx() as db:
            db.execute("DELETE FROM sessions WHERE id = ?", (sid,))

    # items

    def post_item(self, sid: str, kind: str, title: str, body: str = "", status: str | None = None,
                  **decision: str) -> str:
        """A decision also takes why (DECISION_FIELDS), required: one short line with the
        option passed over, the reason and how to undo it, which joins its body; its status
        is the person's (unseen until they view it)."""
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {sorted(KINDS)}")
        if kind == "doc":
            raise ValueError("a doc is posted with its path (post_doc)")
        if kind == "decision":
            body = decision_body(body, status, decision)
            status = None
        elif any(decision.values()):
            raise ValueError(f"{', '.join(k for k, v in decision.items() if v)}: only a decision takes these")
        status = status or INITIAL_STATUS[kind]
        self._check_status(kind, status)
        with self.tx() as db:
            self._require(db, sid)
            return self._insert_item(db, sid, kind, title, body, status)

    def post_doc(self, sid: str, title: str, path: str, parent: str | None = None) -> str:
        """A markdown document the session declares for the person to read: its absolute path,
        a file that exists, kept as the item's body; parent, a task, question or decision of
        the session's, to list it under. The same path posted again by the same session is the
        same item: its title (and parent, if given) updated, and once read, marked updated,
        never new again. Returns the item's ref."""
        if not path or not os.path.isabs(path):
            raise ValueError(f"path must be absolute (the full path from /), not {path!r}")
        path = os.path.normpath(path)
        if not os.path.isfile(path):
            raise ValueError(f"no file at {path}: write the doc first, then post it")
        with self.tx() as db:
            self._require(db, sid)
            if parent:
                row = db.execute("SELECT kind FROM items WHERE session_id = ? AND ref = ?", (sid, parent)).fetchone()
                if row is None or row["kind"] not in PARENTS:
                    raise ValueError(f"parent {parent} is not a task, question or decision of this session")
            row = db.execute("SELECT ref FROM items WHERE session_id = ? AND kind = 'doc' AND body = ? ORDER BY id LIMIT 1",
                             (sid, path)).fetchone()
            if row is None:
                ref = self._insert_item(db, sid, "doc", title, path, INITIAL_STATUS["doc"])
                db.execute("UPDATE items SET parent_ref = ? WHERE session_id = ? AND ref = ?", (parent, sid, ref))
                return ref
            db.execute(f"""UPDATE items SET title = ?, parent_ref = coalesce(?, parent_ref), updated_at = ?, session_at = ?,
                          status = CASE WHEN status = 'new' THEN 'new' ELSE 'updated' END, dismissed = {KEEP_DELETED}
                          WHERE session_id = ? AND ref = ?""", (title, parent, now(), now(), sid, row["ref"]))
            return row["ref"]

    def post_compact_question(self, sid: str, title: str, body: str) -> str:
        """The host's question suggesting Compact, marked (COMPACT_QUESTION) as it is posted:
        one transaction, so no reader sees it unmarked."""
        with self.tx() as db:
            self._require(db, sid)
            ref = self._insert_item(db, sid, "question", title, body, INITIAL_STATUS["question"])
            db.execute("UPDATE items SET answer = ? WHERE session_id = ? AND ref = ?", (COMPACT_QUESTION, sid, ref))
        return ref

    @staticmethod
    def _insert_item(db, sid, kind, title, body, status) -> str:
        n = db.execute("SELECT count(*) FROM items WHERE session_id = ? AND kind = ?", (sid, kind)).fetchone()[0]
        ref = f"{KINDS[kind]}{n + 1}"
        db.execute(
            """INSERT INTO items (session_id, ref, kind, title, body, status, created_at, updated_at, session_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (sid, ref, kind, title, body, status, now(), now(), now()),
        )
        return ref

    def move_items(self, sid: str, to: str, refs: list[str], fork: str,
                   tell=None) -> tuple[list[tuple[str, str]], list[str]]:
        """Items handed to another session, fork (fork.py), in one transaction: each row
        re-owned with its thread, under the next ref of its kind there, and its old ref kept
        here by a finished stand-in (a task dropped and dismissed, a question closed) that says
        where it went (moved_to), as refs are numbered by count (_insert_item), in older code
        too. Only those still MOVABLE as the transaction holds the write lock: the session may
        have changed one since it was offered. tell(moved): the notices that go with the move,
        (session, body), written in the same transaction, so the items never move untold.
        (moved, as (old, new) in the order given, each once; the refs left, gone or no longer
        movable)."""
        moved, left = [], []
        with self.tx() as db:
            self._require(db, sid)
            self._require(db, to)
            for ref in dict.fromkeys(refs):
                item = db.execute("SELECT * FROM items WHERE session_id = ? AND ref = ?", (sid, ref)).fetchone()
                if item is None or item["status"] not in MOVABLE.get(item["kind"], ()) or item["moved_to"]:
                    left.append(ref)
                    continue
                n = db.execute("SELECT count(*) FROM items WHERE session_id = ? AND kind = ?",
                               (to, item["kind"])).fetchone()[0]
                new = f"{KINDS[item['kind']]}{n + 1}"
                db.execute("UPDATE items SET session_id = ?, ref = ?, updated_at = ? WHERE id = ?",
                           (to, new, now(), item["id"]))
                db.execute("UPDATE messages SET session_id = ?, item_ref = ? WHERE session_id = ? AND item_ref = ?",
                           (to, new, sid, ref))
                status = "dropped" if item["kind"] == "task" else "closed"
                db.execute("""INSERT INTO items (session_id, ref, kind, title, body, status, created_at, updated_at,
                              dismissed, moved_to) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                           (sid, ref, item["kind"], item["title"], f"Moved to {fork}, a fork of this session, as {new}.",
                            status, item["created_at"], now(), status if item["kind"] in DISMISSABLE else None,
                            f"{fork} as {new}"))
                moved.append((ref, new))
            for session, body in (tell(moved) if tell else []):
                db.execute("INSERT INTO messages (session_id, author, body, created_at, kind) "
                           "VALUES (?, 'person', ?, ?, 'notice')", (session, body, now()))
        return moved, left

    @staticmethod
    def check_not_moved(item) -> None:
        """A session's own word on an item it handed to a fork (move_items) is refused, saying where it went."""
        if item["moved_to"]:
            raise ValueError(f"{item['ref']} moved to {item['moved_to']}: it is no longer this session's")

    # subagents the wheelhouse tracks itself (subagents.py)

    def agent_links(self, sid: str) -> dict[str, tuple[str, str]]:
        """The session's tracked subagents: agent id to (item ref, item status)."""
        rows = self._all("""SELECT a.agent_id, a.item_ref, i.status FROM agent_items a JOIN items i
                            ON i.session_id = a.session_id AND i.ref = a.item_ref WHERE a.session_id = ?""", (sid,))
        return {r["agent_id"]: (r["item_ref"], r["status"]) for r in rows}

    def agent_of(self, sid: str, ref: str) -> str | None:
        """The agent id of the subagent an A item tracks, if the wheelhouse has tied it to one."""
        row = self._one("SELECT agent_id FROM agent_items WHERE session_id = ? AND item_ref = ?", (sid, ref))
        return row["agent_id"] if row else None

    def track_agent(self, sid: str, agent_id: str, title: str, body: str, started: datetime,
                    status: str | None = None, note: str = "", prompt: str = "") -> str:
        """The subagent's A item, made once per agent id, with its status (None: nothing
        said yet, so running when made and left alone when it exists, as another wheelhouse
        may have made it). The session's own A item for it is taken instead when there is
        one (agent_match), so it isn't listed twice. Returns the item's status as stored."""
        with self.tx() as db:
            self._require(db, sid)
            row = db.execute("SELECT item_ref FROM agent_items WHERE session_id = ? AND agent_id = ?",
                             (sid, agent_id)).fetchone()
            if row:
                ref = row["item_ref"]
            else:
                ref = agent_match(db, sid, title, started)
                if ref is None:
                    ref = self._insert_item(db, sid, "agent", title, body, "running")   # then finished, with its note
                db.execute("UPDATE items SET parent_ref = ? WHERE session_id = ? AND ref = ? AND parent_ref IS NULL",
                           (agent_parent(db, sid, started, title, prompt), sid, ref))
                db.execute("INSERT INTO agent_items (session_id, agent_id, item_ref) VALUES (?, ?, ?)",
                           (sid, agent_id, ref))
                status = status or "running"
            if status:
                self._agent_status(db, sid, ref, status, note)
            return db.execute("SELECT status FROM items WHERE session_id = ? AND ref = ?", (sid, ref)).fetchone()["status"]

    def agent_status(self, sid: str, agent_id: str, status: str, note: str = "") -> None:
        """A tracked subagent finished (done or failed), or was resumed (running)."""
        with self.tx() as db:
            row = db.execute("SELECT item_ref FROM agent_items WHERE session_id = ? AND agent_id = ?",
                             (sid, agent_id)).fetchone()
            if row:
                self._agent_status(db, sid, row["item_ref"], status, note)

    def _agent_status(self, db, sid, ref, status, note) -> None:
        """Only a change is written, so a transcript read again moves nothing."""
        self._check_status("agent", status)
        if db.execute(f"UPDATE items SET status = ?, updated_at = ?, dismissed = {KEEP_DELETED} WHERE session_id = ? "
                      "AND ref = ? AND status != ?", (status, now(), sid, ref, status)).rowcount and note:
            self._said(db, sid, ref, note, "note")

    def update_item(self, sid: str, ref: str, *, status=None, title=None, body=None, note=None,
                    declares: bool = False) -> None:
        """declares: the session's own update (the MCP tool), whose status, on an item the
        person's word awaits a reply on, is its word on it: the item no longer awaits it
        (a STATUS message, which AWAITING counts as a reply). It is word from the session
        on the item too (session_at), whatever it changes."""
        if self.session(sid) is None:
            raise SessionGone(sid)
        item = self.item(sid, ref)
        if item is None:
            raise KeyError(f"no item {ref} in this session")
        if declares:
            self.check_not_moved(item)
        if status is not None:
            self._check_session_status(item, status)
        with self.tx() as db:
            self._require(db, sid)
            db.execute(
                f"""UPDATE items SET status = coalesce(?, status), title = coalesce(?, title),
                   body = coalesce(?, body), updated_at = ?, {UNDISMISS},
                   session_at = CASE WHEN ? THEN ? ELSE session_at END WHERE session_id = ? AND ref = ?""",
                (status, title, body, now(), status, declares, now(), sid, ref),
            )
            if declares and status is not None and db.execute(
                    f"SELECT 1 FROM ({AWAITING}) WHERE session_id = ? AND item_ref = ?", (sid, ref)).fetchone():
                self._said(db, sid, ref, f"status: {status}", STATUS)
            if note:
                self._said(db, sid, ref, note, "note")

    def reply(self, sid: str, ref: str, text: str, status: str | None = None, by_session: bool = False) -> None:
        """The session's answer in an item's conversation, declaring where the item stands.
        On a question the status is required: open (still waiting on the person, a
        clarification answered included) or answered (the person's input lets it proceed).
        by_session: the session's own call (the MCP tool), word from it on the item
        (session_at); the host's replies on its behalf are not."""
        item = self.item(sid, ref)
        if self.session(sid) is None:
            raise SessionGone(sid)
        if item is None:
            raise KeyError(f"no item {ref} in this session")
        if by_session:
            self.check_not_moved(item)
        if item["kind"] == "question" and status not in ("open", "answered"):
            raise ValueError(f"{ref} is a question: reply with status open (still waiting on the "
                             "person) or answered (you have what you need)")
        if status is not None:
            self._check_session_status(item, status)
        with self.tx() as db:
            self._require(db, sid)
            self._said(db, sid, ref, text, "reply")
            db.execute(f"UPDATE items SET status = coalesce(?, status), updated_at = ?, {UNDISMISS}, "
                       "session_at = CASE WHEN ? THEN ? ELSE session_at END WHERE session_id = ? AND ref = ?",
                       (status, now(), status, by_session, now(), sid, ref))

    def host_reply(self, sid: str, ref: str, text: str, after: int = 0) -> None:
        """A hosted session's chat, posted by its host as the reply on an item the person's
        message (id `after`) was on, unless the session has replied there itself since. It
        declares nothing: the session sets the status itself."""
        with self.tx() as db:
            self._require(db, sid)
            if db.execute("SELECT 1 FROM messages WHERE session_id = ? AND item_ref = ? AND author = 'claude' "
                          "AND kind = 'reply' AND id > ?", (sid, ref, after)).fetchone():
                return
            if db.execute("UPDATE items SET updated_at = ? WHERE session_id = ? AND ref = ?",
                          (now(), sid, ref)).rowcount:
                self._said(db, sid, ref, text, "reply")

    @staticmethod
    def _said(db, sid, ref, text, kind) -> int:
        return db.execute(
            """INSERT INTO messages (session_id, item_ref, author, body, created_at, delivered_at, kind)
               VALUES (?, ?, 'claude', ?, ?, ?, ?)""",
            (sid, ref, text, now(), now(), kind),
        ).lastrowid

    def item(self, sid: str, ref: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM items WHERE session_id = ? AND ref = ?", (sid, ref))

    def items(self, sid: str | None = None, include_closed: bool = True) -> list[sqlite3.Row]:
        sql = """SELECT i.*, s.name AS session_name FROM items i JOIN sessions s ON s.id = i.session_id
                 WHERE (? IS NULL AND s.parked = 0) OR i.session_id = ?"""
        rows = self._all(sql, (sid, sid))
        if not include_closed:
            rows = [r for r in rows if r["status"] not in CLOSED]
        processing = self.processing()
        return sorted(rows, key=lambda r: inbox_rank(r, processing))

    def all_items(self) -> list[sqlite3.Row]:
        """Every item, parked sessions' too: what the person's order is laid over (ordering.py)."""
        return self._all("SELECT i.*, s.name AS session_name, s.parked AS session_parked FROM items i "
                         "JOIN sessions s ON s.id = i.session_id")

    def set_item_order(self, group: str, keys: list[tuple]) -> None:
        """The person's order for a group of items, (session, ref) in order, or (session, ref,
        its own group) where each has its own (Status *'s tiers, ordering.arrange): each placed.
        updated_at stays: it is what the inbox ranks by."""
        with self.tx() as db:
            db.executemany("UPDATE items SET pos = ?, pos_group = ? WHERE session_id = ? AND ref = ?",
                           [(i, k[2] if len(k) > 2 else group, k[0], k[1]) for i, k in enumerate(keys)])

    def set_session_order(self, group: str, sids: list[str]) -> None:
        """The person's order for a group of sessions, each placed in it."""
        with self.tx() as db:
            db.executemany("UPDATE sessions SET pos = ?, pos_group = ? WHERE id = ?",
                           [(i, group, sid) for i, sid in enumerate(sids)])

    def in_doubt(self, sid: str, at: datetime | None = None) -> list[str]:
        """The items the wheelhouse doubts are still open, oldest first, each as `T1 running`
        or `Q2 reply owed`: a task running, blocked or waiting, or a question whose latest
        word is the person's (awaiting the session's reply), that the session has said
        nothing on (post_item, update_item, reply: session_at, else created_at) for
        DOUBT_AFTER or more; on a question, nor the person since their latest message on it,
        so a fresh answer isn't owed a reply at once. Never a todo, a question waiting on
        the person, an agent, decision or permission, nor anything settled; nothing at all
        for a session on older code (needs_relaunch), whose MCP server never writes
        session_at."""
        session = self.session(sid)
        if session is None or needs_relaunch(session):
            return []
        cutoff = ((at or datetime.now(timezone.utc)) - DOUBT_AFTER).isoformat(timespec="seconds")
        rows = self._all(
            f"""SELECT ref, kind, status FROM items i WHERE session_id = ?
                AND max(coalesce(session_at, created_at), CASE WHEN kind = 'question' THEN coalesce(
                    (SELECT max({REACHED.format(m="m.")}) FROM messages m WHERE m.session_id = i.session_id
                     AND m.item_ref = i.ref AND m.author = 'person' AND m.draft = 0), '') ELSE '' END) <= ?
                AND ((kind = 'task' AND status IN ({','.join('?' * len(TRANSIENT))}))
                     OR (kind = 'question' AND status = 'open'
                         AND (session_id, ref) IN ({AWAITING})))
                ORDER BY id""", (sid, cutoff, *TRANSIENT))
        return [f"{r['ref']} {'reply owed' if r['kind'] == 'question' else r['status']}" for r in rows]

    def heard_at(self, sid: str) -> str | None:
        """When the session was last sent anything: the latest a message of the person's (a
        notice included) was sent, claimed or delivered (REACHED); None if never."""
        row = self._one(f"""SELECT max(CASE WHEN author = 'person' AND draft = 0 THEN {REACHED.format(m="")} END) AS at
                              FROM messages WHERE session_id = ?""", (sid,))
        return row["at"] or None

    def open_items(self, sid: str) -> list[sqlite3.Row]:
        """What a session is told after a compaction: every task not settled and every open
        question, as list_items orders them; no agent, decision or permission items."""
        return [r for r in self.items(sid, include_closed=False)
                if (r["kind"] == "task" and r["status"] not in SETTLED["task"])
                or (r["kind"] == "question" and r["status"] == "open" and r["answer"] != COMPACT_QUESTION)]

    def answer_permission(self, sid: str, ref: str, decision: str, message: str = "") -> None:
        """The person's answer to a permission item: allow, always (allow, and keep the rule
        Claude suggested) or deny (with what to do instead, optionally)."""
        if decision not in ("allow", "always", "deny"):
            raise ValueError(f"decision must be allow, always or deny, not {decision!r}")
        with self.tx() as db:
            self._require(db, sid)
            done = db.execute("UPDATE items SET status = ?, answer = ?, updated_at = ? WHERE session_id = ? "
                              "AND ref = ? AND kind = 'permission' AND status = 'open'",
                              ("denied" if decision == "deny" else "allowed",
                               json.dumps({"decision": decision, "message": message}), now(), sid, ref)).rowcount
        if not done:
            raise KeyError(f"no open permission {ref} in this session")

    def mark_seen(self, sid: str, ref: str) -> bool:
        """The person has viewed a decision or a doc: True if it was unseen (a doc new or
        updated) until now."""
        with self.tx() as db:
            return self._mark_seen(db, sid, ref)

    @staticmethod
    def _mark_seen(db, sid: str, ref: str | None) -> bool:
        """Answering an item is an explicit act: a decision is seen at once, not after the
        dwell. In the answer's own transaction, so a command that fails between the two never
        leaves one done without the other."""
        return ref is not None and db.execute(
            "UPDATE items SET status = CASE kind WHEN 'decision' THEN 'seen' ELSE 'read' END, updated_at = ? "
            "WHERE session_id = ? AND ref = ? AND ((kind = 'decision' AND status = 'unseen') "
            "OR (kind = 'doc' AND status IN ('new', 'updated')))", (now(), sid, ref)).rowcount > 0

    def dismiss(self, sid: str, ref: str, dismissed: bool = True) -> None:
        """The person dismisses a settled task or subagent (Delete), or brings it back (Ctrl+R). Kept as
        the status it was dismissed in: a session that changes it brings it back."""
        with self.tx() as db:
            self._require(db, sid)
            done = db.execute("UPDATE items SET dismissed = CASE WHEN ? THEN status END WHERE session_id = ? "
                              f"AND ref = ? AND kind IN {DISMISSABLE}", (dismissed, sid, ref)).rowcount
        if not done:
            raise KeyError(f"no task or subagent {ref} in this session")

    def delete_children(self, sid: str, ref: str) -> list[str]:
        """The person deleted an item with its children (family): each one not running marked
        deleted (DELETED), finished ones included, a question or decision closed too, so it stays
        off the inbox when its session or subagent changes it. Ctrl+R on one (undelete) brings it
        back. Each one running (spared) stays, orphaned: at the top level, and still there when
        Ctrl+R brings its parent back. Returns the refs deleted."""
        with self.tx() as db:
            self._require(db, sid)
            rows = family(db.execute("SELECT ref, kind, status, dismissed, parent_ref FROM items WHERE session_id = ?",
                                     (sid,)).fetchall(), ref)
            for r in rows:   # a finished one too: one dismissed already would come back on a change
                if spared(r):
                    db.execute("UPDATE items SET parent_ref = NULL WHERE session_id = ? AND ref = ?", (sid, r["ref"]))
                    continue
                closed = "closed" if r["kind"] in ("question", "decision") else r["status"]
                db.execute("UPDATE items SET status = ?, dismissed = ?, updated_at = ? WHERE session_id = ? AND ref = ?",
                           (closed, DELETED, now(), sid, r["ref"]))
        return [r["ref"] for r in rows if not spared(r)]

    def undelete(self, sid: str, ref: str) -> None:
        with self.tx() as db:
            db.execute("UPDATE items SET dismissed = NULL WHERE session_id = ? AND ref = ? AND dismissed = ?",
                       (sid, ref, DELETED))

    def close_decision(self, sid: str, ref: str, closed: bool = True) -> None:
        """The person closes a decision (or reopens it, as seen)."""
        with self.tx() as db:
            self._require(db, sid)
            done = db.execute("UPDATE items SET status = ?, updated_at = ? WHERE session_id = ? AND ref = ? "
                              "AND kind = 'decision'", ("closed" if closed else "seen", now(), sid, ref)).rowcount
        if not done:
            raise KeyError(f"no decision {ref} in this session")

    @classmethod
    def _check_session_status(cls, item, status: str) -> None:
        if item["kind"] == "decision":
            raise ValueError(f"{item['ref']} is a decision: whether it has been seen is the person's, "
                             "set when they view it. Reply without a status.")
        if item["kind"] == "doc":
            raise ValueError(f"{item['ref']} is a doc: whether it has been read is the person's. Post its "
                             "path again to say it changed.")
        cls._check_status(item["kind"], status)

    @staticmethod
    def _check_status(kind: str, status: str) -> None:
        if status not in STATUSES[kind]:
            raise ValueError(f"{kind} status must be one of {sorted(STATUSES[kind])}")

    # settings: the wheelhouse's own, such as whether the first-run tutorial offer was answered

    def setting(self, key: str) -> str | None:
        row = self._one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self.tx() as db:
            db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                       "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))

    def clear_setting(self, key: str) -> None:
        with self.tx() as db:
            db.execute("DELETE FROM settings WHERE key = ?", (key,))

    # the daemon's requests, by Idempotency-Key

    def answered(self, key: str) -> sqlite3.Row | None:
        """The answer given to a request with this key, if within KEYS_KEPT."""
        return self._one("SELECT * FROM idempotency WHERE key = ? AND at > ?",
                         (key, (datetime.now(timezone.utc) - KEYS_KEPT).isoformat(timespec="seconds")))

    def answer(self, key: str, fingerprint: str, status: int, body: str) -> None:
        """The answer given to a request with this key; those older than KEYS_KEPT go."""
        with self.tx() as db:
            db.execute("DELETE FROM idempotency WHERE at <= ?",
                       ((datetime.now(timezone.utc) - KEYS_KEPT).isoformat(timespec="seconds"),))
            db.execute("INSERT OR REPLACE INTO idempotency (key, fingerprint, status, body, at) VALUES (?, ?, ?, ?, ?)",
                       (key, fingerprint, status, body, now()))

    # messages

    def messages(self, sid: str) -> list[sqlite3.Row]:
        """Every message in a session, both voices, drafts included, oldest first."""
        return self._all("SELECT * FROM messages WHERE session_id = ? ORDER BY id", (sid,))

    def send(self, sid: str, body: str, item_ref: str | None = None) -> None:
        """A message from the person, sent now; the session's monitor delivers it. An answer
        to an unseen decision marks it seen, in the same transaction (_mark_seen)."""
        with self.tx() as db:
            self._require(db, sid)
            self._mark_seen(db, sid, item_ref)
            db.execute(
                "INSERT INTO messages (session_id, item_ref, author, body, created_at) VALUES (?, ?, 'person', ?, ?)",
                (sid, item_ref, body, now()),
            )

    def notice(self, sid: str, body: str) -> None:
        """A message from the wheelhouse itself (not the person), sent now."""
        with self.tx() as db:
            self._require(db, sid)
            db.execute("INSERT INTO messages (session_id, author, body, created_at, kind) "
                       "VALUES (?, 'person', ?, ?, 'notice')", (sid, body, now()))

    def queue(self, sid: str, body: str, item_ref: str | None = None) -> None:
        """A message from the person, held as a draft until dispatch() sends it. An answer to
        an unseen decision marks it seen, in the same transaction (_mark_seen)."""
        with self.tx() as db:
            self._require(db, sid)
            self._mark_seen(db, sid, item_ref)
            db.execute("INSERT INTO messages (session_id, item_ref, author, body, created_at, draft) "
                       "VALUES (?, ?, 'person', ?, ?, 1)", (sid, item_ref, body, now()))

    def drafts(self, sid: str | None = None) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM messages WHERE draft = 1 AND (? IS NULL OR session_id = ?) ORDER BY id",
                         (sid, sid))

    def unqueue(self, draft_id: int) -> str | None:
        """Take a draft back (to edit it, or to drop it). Its text, or None if it was sent meanwhile."""
        with self.tx() as db:
            row = db.execute("DELETE FROM messages WHERE id = ? AND draft = 1 RETURNING body", (draft_id,)).fetchone()
        return row["body"] if row else None

    def dispatch(self, sid: str) -> int:
        """Send the session's drafts together, in one transaction: the monitor claims every sent
        message at once, so the session never sees part of a batch. Returns how many went."""
        with self.tx() as db:
            self._require(db, sid)
            sent = db.execute("UPDATE messages SET draft = 0 WHERE session_id = ? AND draft = 1 RETURNING id",
                              (sid,)).fetchall()
        return len(sent)

    def awaiting(self) -> set[tuple[str, str]]:
        """Items whose latest word is the person's: sent to the session, with no reply since.
        The ball is in the session's court; the session's reply says where the item stands."""
        return {(r["session_id"], r["item_ref"]) for r in self._all(AWAITING)}

    def processing(self) -> set[tuple[str, str]]:
        """Unfinished items of any kind the session is working on (D28): the person's word sent,
        awaiting its reply, nothing more queued."""
        return {(r["session_id"], r["ref"]) for r in self._all(f"SELECT session_id, ref, status FROM items i WHERE {PROCESSING}")
                if r["status"] not in CLOSED}

    def pending(self, sid: str) -> list[sqlite3.Row]:
        return self._all(
            "SELECT * FROM messages WHERE session_id = ? AND delivered_at IS NULL AND draft = 0 ORDER BY id", (sid,)
        )

    def claim(self, sid: str, item_ref: str | None = None) -> list[sqlite3.Row]:
        """Claim undelivered messages in one transaction, so the monitor and get_input()
        never both hand Claude the same one. The claimer confirms or releases them; a claim
        left by a monitor that died mid-print is retaken after CLAIM_TIMEOUT."""
        stale = (datetime.now(timezone.utc) - timedelta(seconds=CLAIM_TIMEOUT)).isoformat(timespec="microseconds")
        with self.tx() as db:
            rows = db.execute(
                """UPDATE messages SET claimed_at = ? WHERE session_id = ? AND delivered_at IS NULL
                   AND draft = 0 AND (claimed_at IS NULL OR claimed_at < ?) AND (? IS NULL OR item_ref = ?)
                   RETURNING *""", (stamp(), sid, stale, item_ref, item_ref)
            ).fetchall()
        return sorted(rows, key=lambda m: m["id"])   # RETURNING order is unspecified

    def confirm(self, claimed) -> None:
        """Mark claimed messages delivered. Only touches a message still under this claim:
        if it was retaken as stale, the new claimer confirms it."""
        self._mark(claimed, "delivered_at = ?", (now(),))

    def release(self, claimed) -> None:
        self._mark(claimed, "claimed_at = NULL", ())

    def _mark(self, claimed, assignment: str, params) -> None:
        with self.tx() as db:
            for m in claimed:
                db.execute(f"UPDATE messages SET {assignment} WHERE id = ? AND claimed_at = ?",
                           (*params, m["id"], m["claimed_at"]))

    def message(self, sid: str, msg_id: int) -> sqlite3.Row | None:
        """One of the person's sent messages, delivered or not: the full text of one the
        monitor cut short."""
        return self._one("SELECT * FROM messages WHERE session_id = ? AND id = ? AND author = 'person' "
                         "AND draft = 0", (sid, msg_id))

    def take_pending(self, sid: str, item_ref: str | None = None) -> list[sqlite3.Row]:
        """Claim and confirm in one go, for a caller that can't fail to show them."""
        rows = self.claim(sid, item_ref)
        self.confirm(rows)
        return rows

    def thread(self, sid: str, ref: str, in_flight: bool = True) -> list[sqlite3.Row]:
        """An item's messages, the person's drafts included. in_flight=False is the session's
        view: no drafts, and none another reader has claimed but not yet delivered (the
        monitor is printing those)."""
        return self._all(
            """SELECT * FROM messages WHERE session_id = ? AND item_ref = ?
               AND (? OR ((delivered_at IS NOT NULL OR claimed_at IS NULL) AND draft = 0)) ORDER BY id""",
            (sid, ref, in_flight),
        )


RANK = {"open": 0, "blocked": 1, "waiting": 1, "answered": 2, "processing": 2, "unseen": 2, "seen": 2,
        "running": 3, "todo": 4}
SETTLED_RANK, FINISHED_RANK = 10, 11   # after every active item's rank
# in a write that may set an item's status (the first parameter, or NULL to leave it): a change
# of status brings back an item the person dismissed
DELETED = "deleted"   # items.dismissed: deleted with its parent, off the inbox whatever its status becomes
KEEP_DELETED = f"CASE WHEN dismissed = '{DELETED}' THEN dismissed END"   # a status change clears a dismissal, not this
UNDISMISS = (f"dismissed = CASE WHEN dismissed = '{DELETED}' OR coalesce(?, status) = status THEN dismissed END")


def spared(item) -> bool:
    """Whether Delete on an item's parent leaves it in the list: one running, not deleted already
    (T125, reversing D102). Nothing runs out of sight."""
    return item["status"] == "running" and item["dismissed"] != DELETED


def family(items, ref: str, parent=lambda it: it["parent_ref"]) -> list:
    """What Delete on `ref` acts on, among its session's `items`: its children and, under each
    child that goes, theirs, at any depth. One spared stays, with whatever is under it, so the
    walk stops there."""
    under: dict[str, list] = {}
    for it in items:
        under.setdefault(parent(it), []).append(it)
    found, todo = [], [ref]
    while todo:
        for it in under.get(todo.pop(), ()):
            found.append(it)
            if not spared(it):
                todo.append(it["ref"])
    return found


# when a sent message reached the session, on messages aliased {m}: the latest of when it was
# written, claimed and delivered. Not created_at alone: a draft keeps the time it was written
# when dispatch sends it, maybe long after. claimed_at is to the microsecond (stamp): cut to the
# second, as the others are, so the three compare as strings (all UTC)
REACHED = ("max({m}created_at, coalesce(substr({m}claimed_at, 1, 19) || '+00:00', ''), "
           "coalesce({m}delivered_at, ''))")
# a message's kind for a session's status update on an item awaiting its reply: its word on it
# (update_item, declares), shown as a note
STATUS = "status"
# (session_id, item_ref) of items whose latest word is the person's, sent, with no reply since
# (or status update: STATUS)
AWAITING = f"""SELECT session_id, item_ref FROM messages WHERE item_ref IS NOT NULL AND draft = 0
              GROUP BY session_id, item_ref
              HAVING max(CASE WHEN author = 'person' THEN id END) >
                     coalesce(max(CASE WHEN author = 'claude' AND kind IN ('reply', '{STATUS}') THEN id END), 0)"""
# D28, on items aliased i: answered and sent, not yet replied to, nothing more queued. Shown as
# processing and not counted as awaiting the person; stored as open, so the protocol is unchanged
PROCESSING = f"""((i.session_id, i.ref) IN ({AWAITING}) AND (i.session_id, i.ref) NOT IN
                  (SELECT session_id, item_ref FROM messages WHERE draft = 1 AND item_ref IS NOT NULL))"""


def agent_words(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.casefold()))


def agent_match(db, sid: str, title: str, started: datetime) -> str | None:
    """The session's own A item for a subagent, if it posted one: not yet tied to another
    subagent, made no more than AGENT_MATCH_BEFORE before the subagent started nor
    AGENT_MATCH_AFTER after, and titled with its description, or with words that hold it or
    that it holds, two words at least (case and punctuation aside). An exact title wins, then
    the item made nearest the start."""
    want = agent_words(title)
    since = started - timedelta(seconds=AGENT_MATCH_BEFORE)
    until = started + timedelta(seconds=AGENT_MATCH_AFTER)
    rows = db.execute("""SELECT ref, title, created_at FROM items i WHERE session_id = ? AND kind = 'agent'
                         AND NOT EXISTS (SELECT 1 FROM agent_items a WHERE a.session_id = i.session_id
                                         AND a.item_ref = i.ref)""", (sid,)).fetchall()
    best = None
    for r in rows:
        made, have = datetime.fromisoformat(r["created_at"]), agent_words(r["title"])
        if not since <= made <= until or not want or not have:
            continue
        held, holder = sorted((want, have), key=lambda w: w.count(" "))   # fewer words inside more
        if have != want and (" " not in held or f" {held} " not in f" {holder} "):
            continue
        key = (have != want, abs((made - started).total_seconds()))
        if best is None or key < best[0]:
            best = (key, r["ref"])
    return best and best[1]


# the kinds an item can be the child of (items.parent_ref): an agent or a doc sits under one
PARENTS = ("task", "question", "decision")


REF = re.compile(r"\b([TQD]\d+)\b")   # an item's ref, named in a subagent's description or prompt
LEADING_REF = re.compile(r"\W*(?:[TQD]\d+\b)?")   # the ref a prompt starts with, if any


def agent_parent(db, sid: str, started: datetime, title: str = "", prompt: str = "") -> str | None:
    """The item a subagent was dispatched for. First the session's own word: the first ref
    of one of its tasks, questions or decisions named in the subagent's description, else the
    ref its prompt starts with (the protocol asks for it in the description). Else a guess: the one its session last posted,
    updated or replied on (created_at, session_at, its own messages on it) at or before the
    subagent started, within that second the first posted. None if it had said nothing on
    any: the agent then sits in its session's group. Made once, as the item is, so later
    words move nothing."""
    kinds = ",".join("?" * len(PARENTS))
    # anywhere in the description, but only leading the prompt, where a ref further on may be
    # one it's told to leave alone ("unlike T2")
    for refs in (REF.findall(title or ""), REF.findall(LEADING_REF.match(prompt or "")[0])):
        for ref in dict.fromkeys(refs):
            if db.execute(f"SELECT 1 FROM items WHERE session_id = ? AND ref = ? AND kind IN ({kinds}) "
                          "AND moved_to IS NULL", (sid, ref, *PARENTS)).fetchone():
                return ref
    at = started.astimezone(timezone.utc).isoformat(timespec="seconds")
    row = db.execute(
        f"""SELECT ref FROM (
              SELECT ref, id, created_at AS at FROM items WHERE session_id = ? AND kind IN ({kinds}) AND moved_to IS NULL
              UNION ALL SELECT ref, id, session_at FROM items WHERE session_id = ? AND kind IN ({kinds})
                        AND moved_to IS NULL AND session_at IS NOT NULL
              UNION ALL SELECT i.ref, i.id, m.created_at FROM messages m JOIN items i
                        ON i.session_id = m.session_id AND i.ref = m.item_ref
                        WHERE m.session_id = ? AND m.author = 'claude' AND i.kind IN ({kinds}) AND i.moved_to IS NULL)
            WHERE at <= ? ORDER BY at DESC, id LIMIT 1""",
        (sid, *PARENTS, sid, *PARENTS, sid, *PARENTS, at)).fetchone()
    return row and row["ref"]


def decision_body(body: str, status, fields: dict) -> str:
    """A decision's body: its detail, then each of DECISION_FIELDS under its label."""
    if status not in (None, "unseen"):
        raise ValueError("a decision's status is the person's: post it without one")
    unknown = set(fields) - {k for k, _ in DECISION_FIELDS}
    if unknown:
        raise ValueError(f"unknown decision field(s): {', '.join(sorted(unknown))}")
    missing = [k for k, _ in DECISION_FIELDS if not (fields.get(k) or "").strip()]
    if missing:
        raise ValueError(f"a decision needs {', '.join(missing)}: what you decided goes in title "
                         "(and body), and why is one short line with the option you passed over, your "
                         "reason and how to undo it")
    parts = [body.strip()] if body.strip() else []
    parts += [f"**{label}:** {fields[k].strip()}" for k, label in DECISION_FIELDS]
    return "\n\n".join(parts)




def standing(item, busy=frozenset()) -> str:
    """Where an item stands in the inbox: active, settled or finished (SETTLED). A settled
    item the person has a word queued or awaiting a reply on, `busy` by (session, ref), is
    active, even if they dismissed it: the ball isn't back with them."""
    status, waiting = item["status"], (item["session_id"], item["ref"]) in busy
    if status in CLOSED - SETTLED[item["kind"]] or item["dismissed"] == DELETED or (
            item["dismissed"] == status and not waiting):
        return "finished"
    if status in SETTLED[item["kind"]] and not waiting:
        return "settled"
    return "active"


def inbox_rank(item, processing=frozenset(), busy=frozenset()) -> tuple:
    """Questions waiting on the person first, then blocked, running, the rest; newest first within a rank.
    An open question in `processing` (D28) waits on the session, so it ranks as answered does.
    Then the settled (standing), oldest first, so the latest to settle sinks lowest; then the finished,
    oldest first too. Newest and oldest by updated_at, which is to the second, then by id in the same
    direction (newest first: the higher id first; oldest first: the lower), so items of one second
    keep one order however they were written, and no two share a rank (#70)."""
    where = standing(item, busy)
    if where != "active":
        return (SETTLED_RANK if where == "settled" else FINISHED_RANK, -_neg_time(item["updated_at"]), item["id"])
    status = "processing" if item["status"] == "open" and (item["session_id"], item["ref"]) in processing \
        else item["status"]
    return (RANK.get(status, 9), _neg_time(item["updated_at"]), -item["id"])


@functools.lru_cache(maxsize=8192)   # each item's, every refresh: parsed once, not each second
def _neg_time(iso: str) -> float:
    return -datetime.fromisoformat(iso).timestamp()
