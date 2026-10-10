"""SQLite store: the only source of truth for the wheelhouse.

Every public write runs in its own transaction and is committed (and, with
synchronous=FULL in WAL mode, fsynced) before the call returns, so a crash
never loses a change the caller was told about.
"""

import json
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_DB = Path.home() / ".local/state/claude-wheelhouse/wheelhouse.db"

# permission: a tool call waiting for the person's approval, posted by a session's SDK host
# (never by the session itself). Allow or Deny answer it; a message on it denies with that text.
KINDS = {"task": "T", "question": "Q", "agent": "A", "decision": "D", "permission": "P"}
STATUSES = {
    "task": {"todo", "running", "blocked", "waiting", "done", "dropped"},
    "question": {"open", "answered", "closed"},
    "agent": {"running", "done", "failed"},
    # the person's state, never the session's: seen once viewed, closed when they close it
    "decision": {"unseen", "seen", "closed"},
    "permission": {"open", "allowed", "denied"},
}
INITIAL_STATUS = {"task": "todo", "question": "open", "agent": "running", "decision": "unseen",
                  "permission": "open"}
CLOSED = {"done", "dropped", "closed", "failed", "allowed", "denied"}
# Where an item stands in the inbox (standing), one table for every kind (T71). Active: ranked
# as it has always been. Settled: done with, but kept in sight: dimmed, after the rest. Finished:
# off the inbox until F shows it (closed, a permission answered, or dismissed by the person).
SETTLED = {"task": {"done", "dropped"}, "question": {"answered"}, "decision": {"seen"},
           "agent": {"done", "failed"}, "permission": set()}
# kinds whose settled items the person dismisses (Delete), recorded in items.dismissed; a
# question or a decision is closed instead
DISMISSABLE = ("task", "agent")
# what a decision records besides its title (what was decided): post_item's keyword name, label
DECISION_FIELDS = (("alternative", "Alternative"), ("why", "Why"), ("reverse", "To reverse"))
# Bump when a session still running older code (its MCP server and monitor keep the code
# they started with) would mishandle the store or miss new behaviour: the wheelhouse then
# shows it as needing a relaunch. 2: queued answers (draft messages) that older code would deliver.
# 3: reply declares a question's status. 4: decisions. 5: SDK-hosted sessions and permission items.
# 6: decisions listed in a report are posted as items. 7: the wheelhouse tracks subagents
# itself (subagents.py), so the protocol no longer asks a session to post them.
PROTOCOL_VERSION = 7
DRAFTS_VERSION = 2   # the first that holds a queued answer until it is sent
# How a session's answers go until the person toggles it: "queued" holds them until sent,
# "immediate" sends each as it's submitted. Stored per session; NULL means this default.
DEFAULT_MODE = "queued"
MODES = ("queued", "immediate")
# How a session runs: "sdk" in a wheelhouse host process (claude-wheelhouse host), "tab" as
# interactive Claude Code in a Windows Terminal tab. Stored per session; NULL (rows from before
# hosts existed) means tab. New sessions take WHEELHOUSE_RUNNER, else DEFAULT_RUNNER.
RUNNERS = ("sdk", "tab")
DEFAULT_RUNNER = "sdk"
# What the person can ask a running host to do, handed over in sessions.host_command
HOST_COMMANDS = ("interrupt", "shell", "compact")

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
"""
# run after ADDED_COLUMNS, which may have just added the columns they use
INDEXES = """
DROP INDEX IF EXISTS messages_pending;
CREATE INDEX IF NOT EXISTS messages_unsent
    ON messages (session_id) WHERE delivered_at IS NULL AND draft = 0;
"""

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
                 ("items", "dismissed", "TEXT")]
# older databases may also carry sessions.transcript_title and sessions.renamed_at,
# from a /rename pickup since dropped: unused, and left in place
DECISIONS_CLOSE = "migrated_decisions_close"   # settings: the one-off migration above has run
# settings: when this database was first opened by code that tracks subagents. Subagents
# that had finished by then (or by when their session joined) get no item (#69)
AGENTS_SINCE = "agents_since"
# seconds: how long before a subagent starts the session's own A item for it may be made
AGENT_MATCH_BEFORE = 600
AGENT_MATCH_AFTER = 15   # subagents.GRACE: by then the wheelhouse makes its own
REQUESTS = ("end", "park")   # what the wheelhouse can ask a running session to do
CLAIM_TIMEOUT = 30   # seconds before a claim from a monitor that died mid-print is retaken


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


def default_runner() -> str:
    r = os.environ.get("WHEELHOUSE_RUNNER") or DEFAULT_RUNNER
    if r not in RUNNERS:
        raise ValueError(f"WHEELHOUSE_RUNNER must be one of {', '.join(RUNNERS)}, not {r!r}")
    return r


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
            for statement in INDEXES.split(";"):
                if statement.strip():
                    db.execute(statement)
            # a decision used to be finished once seen, and now stays until closed: those
            # seen before then are closed, once, rather than come back to the inbox
            if db.execute("SELECT 1 FROM settings WHERE key = ?", (DECISIONS_CLOSE,)).fetchone() is None:
                db.execute("UPDATE items SET status = 'closed' WHERE kind = 'decision' AND status = 'seen'")
                db.execute("INSERT INTO settings (key, value) VALUES (?, '1')", (DECISIONS_CLOSE,))
            db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (AGENTS_SINCE, now()))

    @contextmanager
    def tx(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")

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
                       sid: str | None = None, runner: str | None = None) -> str:
        """A new session, or (with sid) an adopted one keeping its Claude session id."""
        adopted = sid is not None
        sid = sid or str(uuid.uuid4())
        runner = runner or default_runner()
        if runner not in RUNNERS:
            raise ValueError(f"runner must be one of {', '.join(RUNNERS)}, not {runner!r}")
        with self.tx() as db:
            db.execute(
                "INSERT INTO sessions (id, name, ticket, brief, cwd, created_at, adopted, runner) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, name, ticket, brief, cwd, now(), int(adopted), runner),
            )
        return sid

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

    def command(self, sid: str, what: str) -> None:
        """Ask the session's host to interrupt, hand over to a shell tab, or compact. A newer
        command replaces one the host hasn't taken yet."""
        if what not in HOST_COMMANDS:
            raise ValueError(f"command must be one of {', '.join(HOST_COMMANDS)}, not {what!r}")
        with self.tx() as db:
            self._require(db, sid)
            db.execute("UPDATE sessions SET host_command = ? WHERE id = ?", (what, sid))

    def take_command(self, sid: str) -> str | None:
        """The host takes the pending command, once."""
        with self.tx() as db:   # read and clear in one write transaction
            row = db.execute("SELECT host_command FROM sessions WHERE id = ?", (sid,)).fetchone()
            if row and row[0]:
                db.execute("UPDATE sessions SET host_command = NULL WHERE id = ?", (sid,))
        return row[0] if row else None

    def set_answer(self, sid: str, ref: str, answer: str) -> None:
        """An item's answer column: a permission's decision, or the host marking a question
        it asked for Claude Code's AskUserQuestion."""
        with self.tx() as db:
            db.execute("UPDATE items SET answer = ? WHERE session_id = ? AND ref = ?", (answer, sid, ref))

    def set_shell(self, sid: str, state: str | None) -> None:
        """tab: the host has handed the session to an interactive tab and waits for it to exit."""
        with self.tx() as db:
            db.execute("UPDATE sessions SET shell = ? WHERE id = ?", (state, sid))

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
                    AND i.ref = a.item_ref WHERE a.session_id = s.id AND i.status = 'running') AS running_agents
               FROM sessions s ORDER BY s.created_at"""
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
        """A (re)launch also drops any request left over from the last run."""
        with self.tx() as db:
            db.execute("""UPDATE sessions SET launched_at = ?, end_requested_at = NULL,
                          park_requested_at = NULL, end_told_at = NULL, park_told_at = NULL
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

    def request(self, sid: str, what: str) -> None:
        """The person pressed End or Park on a running session; its monitor passes this on.
        Nothing happens to the data until the session acts, or the person forces it."""
        assert what in REQUESTS
        with self.tx() as db:
            self._require(db, sid)
            db.execute(f"UPDATE sessions SET {what}_requested_at = coalesce({what}_requested_at, ?) "
                       "WHERE id = ?", (stamp(), sid))

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
        """Parking (by the session or by force) also settles any pending park request."""
        with self.tx() as db:
            self._require(db, sid)
            db.execute("UPDATE sessions SET parked = ?, park_requested_at = NULL, park_told_at = NULL "
                       "WHERE id = ?", (int(parked), sid))

    def end(self, sid: str) -> None:
        with self.tx() as db:
            db.execute("DELETE FROM sessions WHERE id = ?", (sid,))

    # items

    def post_item(self, sid: str, kind: str, title: str, body: str = "", status: str | None = None,
                  **decision: str) -> str:
        """A decision also takes alternative, why and reverse (DECISION_FIELDS), all required,
        which join its body; its status is the person's (unseen until they view it)."""
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {sorted(KINDS)}")
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

    @staticmethod
    def _insert_item(db, sid, kind, title, body, status) -> str:
        n = db.execute("SELECT count(*) FROM items WHERE session_id = ? AND kind = ?", (sid, kind)).fetchone()[0]
        ref = f"{KINDS[kind]}{n + 1}"
        db.execute(
            """INSERT INTO items (session_id, ref, kind, title, body, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (sid, ref, kind, title, body, status, now(), now()),
        )
        return ref

    # subagents the wheelhouse tracks itself (subagents.py)

    def agent_links(self, sid: str) -> dict[str, tuple[str, str]]:
        """The session's tracked subagents: agent id to (item ref, item status)."""
        rows = self._all("""SELECT a.agent_id, a.item_ref, i.status FROM agent_items a JOIN items i
                            ON i.session_id = a.session_id AND i.ref = a.item_ref WHERE a.session_id = ?""", (sid,))
        return {r["agent_id"]: (r["item_ref"], r["status"]) for r in rows}

    def track_agent(self, sid: str, agent_id: str, title: str, body: str, started: datetime,
                    status: str | None = None, note: str = "") -> str:
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
        if db.execute("UPDATE items SET status = ?, updated_at = ?, dismissed = NULL WHERE session_id = ? AND ref = ? "
                      "AND status != ?", (status, now(), sid, ref, status)).rowcount and note:
            self._said(db, sid, ref, note, "note")

    def update_item(self, sid: str, ref: str, *, status=None, title=None, body=None, note=None) -> None:
        if self.session(sid) is None:
            raise SessionGone(sid)
        item = self.item(sid, ref)
        if item is None:
            raise KeyError(f"no item {ref} in this session")
        if status is not None:
            self._check_session_status(item, status)
        with self.tx() as db:
            self._require(db, sid)
            db.execute(
                f"""UPDATE items SET status = coalesce(?, status), title = coalesce(?, title),
                   body = coalesce(?, body), updated_at = ?, {UNDISMISS} WHERE session_id = ? AND ref = ?""",
                (status, title, body, now(), status, sid, ref),
            )
            if note:
                self._said(db, sid, ref, note, "note")

    def reply(self, sid: str, ref: str, text: str, status: str | None = None) -> None:
        """The session's answer in an item's conversation, declaring where the item stands.
        On a question the status is required: open (still waiting on the person, a
        clarification answered included) or answered (the person's input lets it proceed)."""
        item = self.item(sid, ref)
        if self.session(sid) is None:
            raise SessionGone(sid)
        if item is None:
            raise KeyError(f"no item {ref} in this session")
        if item["kind"] == "question" and status not in ("open", "answered"):
            raise ValueError(f"{ref} is a question: reply with status open (still waiting on the "
                             "person) or answered (you have what you need)")
        if status is not None:
            self._check_session_status(item, status)
        with self.tx() as db:
            self._require(db, sid)
            self._said(db, sid, ref, text, "reply")
            db.execute(f"UPDATE items SET status = coalesce(?, status), updated_at = ?, {UNDISMISS} "
                       "WHERE session_id = ? AND ref = ?", (status, now(), status, sid, ref))

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
        """The person has viewed a decision: True if it was unseen until now."""
        with self.tx() as db:
            return db.execute("UPDATE items SET status = 'seen', updated_at = ? WHERE session_id = ? "
                              "AND ref = ? AND kind = 'decision' AND status = 'unseen'",
                              (now(), sid, ref)).rowcount > 0

    def dismiss(self, sid: str, ref: str, dismissed: bool = True) -> None:
        """The person dismisses a settled task or subagent (Delete), or brings it back. Kept as
        the status it was dismissed in: a session that changes it brings it back."""
        with self.tx() as db:
            self._require(db, sid)
            done = db.execute("UPDATE items SET dismissed = CASE WHEN ? THEN status END WHERE session_id = ? "
                              f"AND ref = ? AND kind IN {DISMISSABLE}", (dismissed, sid, ref)).rowcount
        if not done:
            raise KeyError(f"no task or subagent {ref} in this session")

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

    # messages

    def messages(self, sid: str) -> list[sqlite3.Row]:
        """Every message in a session, both voices, drafts included, oldest first."""
        return self._all("SELECT * FROM messages WHERE session_id = ? ORDER BY id", (sid,))

    def send(self, sid: str, body: str, item_ref: str | None = None) -> None:
        """A message from the person, sent now; the session's monitor delivers it."""
        with self.tx() as db:
            self._require(db, sid)
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
        """A message from the person, held as a draft until dispatch() sends it."""
        with self.tx() as db:
            self._require(db, sid)
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
UNDISMISS = "dismissed = CASE WHEN coalesce(?, status) = status THEN dismissed END"

# (session_id, item_ref) of items whose latest word is the person's, sent, with no reply since
AWAITING = """SELECT session_id, item_ref FROM messages WHERE item_ref IS NOT NULL AND draft = 0
              GROUP BY session_id, item_ref
              HAVING max(CASE WHEN author = 'person' THEN id END) >
                     coalesce(max(CASE WHEN author = 'claude' AND kind = 'reply' THEN id END), 0)"""
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
                         "(and body), plus the alternative, why, and how to reverse it")
    parts = [body.strip()] if body.strip() else []
    parts += [f"**{label}:** {fields[k].strip()}" for k, label in DECISION_FIELDS]
    return "\n\n".join(parts)


def standing(item, busy=frozenset()) -> str:
    """Where an item stands in the inbox: active, settled or finished (SETTLED). A settled
    item the person has a word queued or awaiting a reply on, `busy` by (session, ref), is
    active, even if they dismissed it: the ball isn't back with them."""
    status, waiting = item["status"], (item["session_id"], item["ref"]) in busy
    if status in CLOSED - SETTLED[item["kind"]] or (item["dismissed"] == status and not waiting):
        return "finished"
    if status in SETTLED[item["kind"]] and not waiting:
        return "settled"
    return "active"


def inbox_rank(item, processing=frozenset(), busy=frozenset()) -> tuple:
    """Questions waiting on the person first, then blocked, running, the rest; newest first within a rank.
    An open question in `processing` (D28) waits on the session, so it ranks as answered does.
    Then the settled (standing), oldest first, so the latest to settle sinks lowest; then the finished."""
    where = standing(item, busy)
    if where != "active":
        return (SETTLED_RANK if where == "settled" else FINISHED_RANK, -_neg_time(item["updated_at"]))
    status = "processing" if item["status"] == "open" and (item["session_id"], item["ref"]) in processing \
        else item["status"]
    return (RANK.get(status, 9), _neg_time(item["updated_at"]))


def _neg_time(iso: str) -> float:
    return -datetime.fromisoformat(iso).timestamp()
