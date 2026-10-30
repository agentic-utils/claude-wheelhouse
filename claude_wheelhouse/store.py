"""SQLite store: the only source of truth for the wheelhouse.

Every public write runs in its own transaction and is committed (and, with
synchronous=FULL in WAL mode, fsynced) before the call returns, so a crash
never loses a change the caller was told about.
"""

import functools
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import features, models, prs, whatsnew

DEFAULT_DB = Path.home() / ".local/state/claude-wheelhouse/wheelhouse.db"

# permission: a tool call waiting for the person's approval, posted by a session's SDK host
# (never by the session itself). Allow or Deny answer it; a message on it denies with that text.
# doc: a markdown document the session declares for the person to read (post_doc), M for markdown
# pr: a pull request the session made (post_pr, prs.py), its ref the repo's PR number (PR108)
KINDS = {"task": "T", "question": "Q", "agent": "A", "decision": "D", "permission": "P", "doc": "M", "pr": "PR"}
STATUSES = {
    "task": {"todo", "running", "blocked", "waiting", "done", "dropped"},
    "question": {"open", "answered", "closed"},
    "agent": {"running", "done", "failed"},
    # the person's state, never the session's: seen once viewed, closed when they close it
    "decision": {"unseen", "seen", "closed"},
    "permission": {"open", "allowed", "denied"},
    # the person's too: new until viewed, read once viewed, updated when posted again after that
    "doc": {"new", "updated", "read"},
    # GitHub's, never the session's or the person's (set_pr_states, from the PRs pane's fetch)
    "pr": {"draft", "open", "merged", "closed"},
}
INITIAL_STATUS = {"task": "todo", "question": "open", "agent": "running", "decision": "unseen",
                  "permission": "open", "doc": "new", "pr": "open"}
CLOSED = {"done", "dropped", "closed", "failed", "allowed", "denied"}
# Where an item stands in the inbox (standing), one table for every kind (T71). Active: ranked
# as it has always been. Settled: done with, but kept in sight: dimmed, after the rest. Finished:
# off the inbox until F shows it (closed, a permission answered, or dismissed by the person).
SETTLED = {"task": {"done", "dropped"}, "question": {"answered"}, "decision": {"seen"},
           "agent": {"done", "failed"}, "permission": set(), "doc": {"read"},
           "pr": {"merged", "closed"}}
# kinds whose settled items the person dismisses (Delete), recorded in items.dismissed; a
# question or a decision is closed instead. A PR is dismissed whatever its state (never on GitHub)
DISMISSABLE = ("task", "agent", "doc", "pr")
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
# 14: set_doc_dirs, the tool a session says where its repo's decisions are with, which the
# design module's Decisions pane now reads (sessions.doc_dirs): an older session has no such tool.
# 15: the person comments on a doc (a message on its ref), which awaits the session's reply
# there as a question's answer does (in_doubt), so the protocol asks it to reply on the doc.
# 16: post_item's pinned (items.pinned): a doc is pinned unless the session says it's a scratch report.
# 17: PR items (kind pr, its ref the PR's number), which post_item(kind="pr", url=...) posts and
# the protocol asks a session to reply on when the person writes on one: an older one knows no pr.
# 18: session steps (steps.py): write_handoff, the tool a session keeps its handoff with, which the
# wheelhouse's messages at compaction, Stop & Forget and resume now ask for: an older one has none.
# 19: request_compact, the tool a session run in the wheelhouse asks to be compacted with once
# its turn ends (sessions.compact_request): an older one has no such tool.
# 20: park_session's compact, keep and reason, with which a session run in the wheelhouse parks
# itself unasked, compacted first unless it says why not (sessions.park_keep): an older one's
# park_session takes no arguments.
# 21: a reply on the host's MCP question (#179) is never passed on: claim skips it, the
# monitor notes it and get_input redacts a pasted sign-in code. An older reader passes it on.
# 22: request_mcp_signin (.plan/mcp-on-demand.md), with which a session run in the wheelhouse asks
# the person to sign in to an MCP server it needs (sessions.mcp_asks): the host no longer asks
# about every server unprompted, and an older host still does.
# 23: a turn's closing text answers only the last message it took up (#243), and a message sent
# mid-turn names the earlier refs to answer with reply. A session on older code (its host runs
# that code) gets no such note and files one closing text on every ref, misfiling answers.
# 24: the compaction cycle (#244, sessions.compaction): from the press to /compact's end the store
# refuses every send, queue, park, relaunch and second Compact, and the host moves the state on.
# An older host never moves it, so its sessions would stay blue and closed: Compact waits for a relaunch.
# 25: the compaction cycle's task item (COMPACTION_TASK), which the session must never see: an older
# MCP server lists it (list_items) and an older reminder after compaction names it; and the tab
# plugin's PreCompact hook (compaction-start), which an older tab doesn't have.
# 26: an operation's row (#249, sessions.op*): from the press of Park, Stop & Forget and the other
# wheelhouse-driven operations to their end the session is blue, its dispatch and other operations are
# refused, and end_session is refused but during its own Stop & Forget. An older MCP server ends a
# session mid-operation, and an older host keeps it warm.
PROTOCOL_VERSION = 26
# the least time between two compactions a session asks for itself (request_compact), so a
# confused one can't loop: seconds since its last accepted request
MODEL_COMPACT_GAP = 30 * 60
PARKED_UNCOMPACTED = "Parked without compacting"   # the decision a session's reason not to compact is posted as
# a compaction's cut-off (Store.compact_cut), in settings by session id, and the activity lines
# that end one: the host's idle and interrupted lines, and its errors and stops
COMPACT_CUTOFF = "compact.cutoff."
COMPACT_ENDS = ("idle", "interrupted", "error", "stopped")
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
# the compaction cycle (#244, .plan/244-compaction-cycle.md): a session's compaction, by any route,
# from the press to /compact's end (sessions.compaction). While it is active every write a person
# makes to the session is refused (Busy), but Interrupt and an answer to a call the notes
# turn waits on; the host makes every move after requested, and ends it
COMPACTION_ACTIVE = ("requested", "preparing", "compacting")
COMPACTION_ENDS = ("completed", "failed", "cancelled")
# who started it: the person's Compact, the session's request_compact, Relaunch or Park compacting
# first, the quiet mark's (KEEPALIVE_HOURS), or Claude Code's own (the PreCompact hook)
COMPACTION_ORIGINS = ("person", "model", "relaunch", "park", "quiet", "auto")
# park_session's refusal during a cycle, said to the session itself
PARK_REFUSED = ("this session is being compacted, and can't park until that has finished: end your turn, and "
                "call park_session again once the compaction is done")
COMPACTING_IN_TAB = "{name} is compacting in its tab: {action} once it's done."
# an operation the wheelhouse drives (#249, .plan/249-ops-blue.md, sessions.op*): its kind, its active
# states and its ends. The row is the lock and the clock, never a command: nothing starts because it exists
OPS = ("park", "end", "relaunch", "start", "handover")
OP_ACTIVE = ("requested", "told", "parking", "stopping", "starting", "opening")
OP_ENDS = ("completed", "failed", "cancelled")
# what a busy session is doing, by its operation's kind (operating), for the refusal and the notices
OP_DOING = {"compact": "compacting", "park": "parking", "end": "being stopped and forgotten",
            "relaunch": "relaunching", "start": "starting", "handover": "handing over to Claude Code in a shell"}
# seconds a Park or Stop & Forget has from its press to the session acting on it (Q243): past it the
# operation fails, and the session carries on as it was
PARK_WITHIN = 15 * 60
# seconds a launched session has to register (liveness.STARTING_GRACE), and each hand-over state has
STARTING_WITHIN = 90
HANDOVER_WITHIN = 60
# seconds past the hub's own Relaunch deadline before the sweep fails a relaunch it lost track of
RELAUNCH_SLACK = 60
# seconds /compact may run before its cycle fails (host.py's clock); a park compacting first has that
# besides PARK_WITHIN, as it parks before it compacts
COMPACT_TIMEOUT = 600
CANCELLED = "The person cancelled the {what} request: carry on as before."   # what a told session hears
BUSY_REFUSED = "{name} is busy {doing}: {action} once it's done, or Interrupt to abort it."
# said to the session itself, refusing its own call mid-operation
SESSION_BUSY = "this session is busy {doing}: call {call} again once that has finished"
NOT_THE_SESSIONS = "{ref} is the wheelhouse's own record of a compaction: there's no session to send it to"
# the cycle's task item (#244), the wheelhouse's own: never in what the session is told or lists
COMPACTION_TASK = '{"from": "compaction"}'
# the task item's status at each state, and its note there ({origin}, {why}: stage notes)
COMPACTION_ITEM = {"requested": ("running", "asked for by {origin}"), "preparing": ("running", "collecting notes"),
                   "compacting": ("running", "compacting"), "completed": ("done", "{why}"),
                   "failed": ("blocked", "failed: {why}"), "cancelled": ("dropped", "{why}")}
ORIGIN_WORDS = {"person": "you (Compact)", "model": "the session (request_compact)", "relaunch": "Relaunch",
                "park": "Park, compact first", "quiet": "the quiet mark", "auto": "Claude Code, at full context"}
# the host's question when a notes turn runs past NOTES_TIMEOUT: Keep waiting or Abort, buttons only
NOTES_WAIT = '{"from": "compaction wait"}'
# an item's answer column marks a question the host asked for Claude Code's AskUserQuestion
HOST_QUESTION = '{"from": "AskUserQuestion"}'
COMPACT_QUESTION_DONE = '{"from": "context nudge", "done": true}'
# an item's answer column marks the host's question about an MCP server that needs signing in
# or has failed (#179, .plan/mcp-reauth.md): the person's replies on it are the host's, never a turn
MCP_FROM = "mcp server"


def mcp_question(server: str) -> str:
    """The answer column's mark on the host's question about this MCP server."""
    return json.dumps({"from": MCP_FROM, "server": server})


def mcp_server(answer: str | None) -> str | None:
    """The MCP server a question so marked is about; None for any other item."""
    if not answer or MCP_FROM not in answer:
        return None
    try:
        mark = json.loads(answer)
    except ValueError:
        return None
    return mark.get("server") if isinstance(mark, dict) and mark.get("from") == MCP_FROM else None


def mcp_named(server: str, servers) -> str | None:
    """The server a name means among servers (names): as given, else the one it names
    without its case or Claude Code's "claude.ai " prefix (the model may write Slack for
    claude.ai Slack), if just one."""
    if server in servers:
        return server
    bare = server.strip().lower().removeprefix("claude.ai ")
    found = [n for n in servers if n.lower().removeprefix("claude.ai ") == bare]
    return found[0] if len(found) == 1 else None


def mcp_servers(session) -> dict[str, dict] | None:
    """A session's MCP servers as its host last saw them (sessions.mcp_status), by name;
    None before its host has looked."""
    raw = session["mcp_status"] if session is not None and "mcp_status" in session.keys() else None
    if not raw:
        return None
    try:
        return {s["name"]: s for s in json.loads(raw)["servers"]}
    except (ValueError, KeyError, TypeError):
        return None


# a message that is the person's reply on an MCP question (in SQL, on a messages row): claim
# skips these unless asked (Store.claim), so no reader but the host can pass one on
MCP_REPLY = f"""EXISTS (SELECT 1 FROM items mi WHERE mi.session_id = messages.session_id AND mi.ref = messages.item_ref
                AND mi.kind = 'question'
                AND CASE WHEN json_valid(mi.answer) THEN json_extract(mi.answer, '$.from') END = '{MCP_FROM}')"""
# what a reply on an MCP question gets in a terminal tab (Shell), where nothing acts on it
MCP_IN_TAB = ("This session is in a terminal tab, so nothing acted on the reply: /mcp in the tab manages "
              "MCP servers.")


def callback_url(text: str) -> str | None:
    """An OAuth redirect URL pasted as a reply: one carrying a code or an error."""
    m = re.search(r"https?://\S+[?&](?:code|error)=\S*", text or "")
    return m.group(0) if m else None


def redacted(text: str) -> str:
    """A reply with any OAuth redirect URL's query (its one-time code) taken out, as kept."""
    return re.sub(r"(https?://[^\s?]+)\?\S*(?:code|error)=\S*", r"\1?[redacted]", text)


def compaction_title(s, tokens: int | None) -> str:
    """A compaction's task item's title: "Compact {name}: {k}k context"."""
    return f"Compact {session_name(s)}" + (f": {tokens // 1000}k context" if tokens else "")


def compacted_note(before: int | None, after: int | None) -> str:
    """A completed compaction's note: "{before}k → {after}k"."""
    k = lambda n: f"{n // 1000}k" if n is not None else "?"   # noqa: E731
    return f"{k(before)} → {k(after)}"


def wheelhouse_item(answer: str | None) -> bool:
    """An item the wheelhouse posted for the person, never the session's: a question of its own
    (wheelhouse_question) or a compaction's task (COMPACTION_TASK)."""
    return answer == COMPACTION_TASK or wheelhouse_question(answer)


def wheelhouse_question(answer: str | None) -> bool:
    """A question the wheelhouse asks the person itself (Compact, an MCP server, a compaction's
    wait for its notes), not the session: it never means the session waits on the person, nor
    is the session told it."""
    return answer in (COMPACT_QUESTION, NOTES_WAIT) or mcp_server(answer) is not None

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
CREATE TABLE IF NOT EXISTS compaction_stages (
    session_id TEXT NOT NULL,
    cycle_id   TEXT NOT NULL,
    state      TEXT NOT NULL,
    at         TEXT NOT NULL,
    origin     TEXT,
    note       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS compaction_stages_session ON compaction_stages (session_id, at);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- the tour's features (whatsnew.FEATURES) the person has been shown, or skipped by
-- answering Never: a feature with no row is pending (whatsnew.pending)
CREATE TABLE IF NOT EXISTS tour_seen (
    feature_id TEXT PRIMARY KEY,
    seen_at    TEXT NOT NULL,
    how        TEXT NOT NULL
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
# A row inserted without items.pinned (by a session's own process on code from before pins, which
# never writes it) takes its kind's default: a doc pinned, anything else not. This code always writes it
TRIGGER_BODIES["items_pinned_by_kind"] = (
    "AFTER INSERT ON items WHEN NEW.pinned IS NULL "
    "BEGIN UPDATE items SET pinned = (NEW.kind = 'doc') WHERE id = NEW.id; END")
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
                 ("items", "new_replies", "INTEGER NOT NULL DEFAULT 0"),
                 ("items", "session_at", "TEXT"), ("items", "moved_to", "TEXT"), ("sessions", "compact_plan", "TEXT"),
                 ("sessions", "park_compact", "INTEGER NOT NULL DEFAULT 0"), ("items", "parent_ref", "TEXT"),
                 # the person's order (ordering.py): a row's place, and the group it was placed in
                 ("items", "pos", "REAL"), ("items", "pos_group", "TEXT"),
                 ("sessions", "pos", "REAL"), ("sessions", "pos_group", "TEXT"),
                 # where its repo's decisions are, as it declared them (set_doc_dirs): JSON, NULL for docs/adr
                 ("sessions", "doc_dirs", "TEXT"),
                 # kept out of reach of clearing the inbox (pinned): a doc by default, anything once the person pins it.
                 # No default: a row older code inserts takes its kind's (items_pinned_by_kind)
                 ("items", "pinned", "INTEGER"),
                 # a compaction the session asked for itself (request_compact): its keep text while
                 # pending, and when it last asked, for MODEL_COMPACT_GAP
                 ("sessions", "compact_request", "TEXT"), ("sessions", "compact_requested_at", "TEXT"),
                 # a park the session asked to be compacted first itself (park_session): /compact's
                 # instructions, '' for none; NULL when it didn't ask
                 ("sessions", "park_keep", "TEXT"),
                 # its host's last look at its MCP servers (JSON: when, and each server's name, status,
                 # kind, scope and error), and what it is asked to do about them (JSON list, taken
                 # whole): a sign-in the session asked for (request_mcp_signin), .plan/mcp-on-demand.md
                 ("sessions", "mcp_status", "TEXT"), ("sessions", "mcp_asks", "TEXT"),
                 # its wheelhouse MCP server as it started (mark_mcp): its pid, the code it loaded
                 # (fingerprint.code) and when, so the session info shows one stale after a pull (#242)
                 ("sessions", "mcp_pid", "INTEGER"), ("sessions", "mcp_code", "TEXT"),
                 ("sessions", "mcp_started_at", "TEXT"),
                 # the compaction cycle (#244): its state (COMPACTION_ACTIVE, COMPACTION_ENDS), id, origin
                 # (COMPACTION_ORIGINS), what follows it (THEN), when the state was entered (UTC) and its task item
                 ("sessions", "compaction", "TEXT"), ("sessions", "compaction_id", "TEXT"),
                 ("sessions", "compaction_origin", "TEXT"), ("sessions", "compaction_then", "TEXT"),
                 ("sessions", "compaction_at", "TEXT"), ("sessions", "compaction_item", "TEXT"),
                 # the operation under way (#249, OPS): its kind, state (OP_ACTIVE, OP_ENDS), id, who began it,
                 # when its last active state was entered and its absolute deadline (both UTC). The last one stays
                 # till the next
                 ("sessions", "op", "TEXT"), ("sessions", "op_state", "TEXT"), ("sessions", "op_id", "TEXT"),
                 ("sessions", "op_origin", "TEXT"), ("sessions", "op_at", "TEXT"), ("sessions", "op_deadline", "TEXT"),
                 # when its host last ended a turn (set_activity, UTC): a call from the turn an op was told in, after
                 # that op was called off, is late (refuse_late)
                 ("sessions", "idle_at", "TEXT")]
# older databases may also carry sessions.transcript_title and sessions.renamed_at,
# from a /rename pickup since dropped, and items.seq (RETIRED_TRIGGER): unused, and left in place
DECISIONS_CLOSE = "migrated_decisions_close"   # settings: the one-off migration above has run
TUTORIAL_OFFER = "tutorial_offer"   # settings: the tour's invite as answered (tutorial.OFFER_KEY)
NEVER = "never"   # TUTORIAL_OFFER answered Never: every feature registered then is skipped
TOUR_HOW = ("shown", "skipped")   # tour_seen.how
NOTES_SEEN = "notes_seen"   # settings: the last version whose release notes the tour showed
TOUR_AT = "tour_at"   # settings: where a tour under way, or paused, resumes ("tour:answer", tour.Tour.where)
# settings: the one-off move to the tour's record (.plan/tutorial-overhaul.md, Migration) has run
TOUR_MIGRATED = "migrated_tour"
RETIRED_TUTORIAL_SEEN = "tutorial_seen"   # settings: the old checklist's record, deleted on open
# settings: when this database was first opened by code that tracks subagents. Subagents
# that had finished by then (or by when their session joined) get no item (#69)
AGENTS_SINCE = "agents_since"
# settings: when this database was first opened by code that finds PRs in transcripts (prs.py).
# A PR made before then (or before its session joined) is never found (#111)
PRS_SINCE = "prs_since"
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


class Refused(ValueError):
    """A person's write the store refuses, said as a warning (Hub.refusable)."""


class Busy(Refused):
    """Refused: the session is in an operation (operating), its kind in `kind`. A compaction in a
    tab, which has no Interrupt, is waited out (tab)."""

    def __init__(self, name: str, action: str, kind: str = "compact", tab: bool = False):
        self.kind = kind
        super().__init__(COMPACTING_IN_TAB.format(name=name, action=action) if tab and kind == "compact"
                         else BUSY_REFUSED.format(name=name, doing=OP_DOING[kind], action=action))


def session_name(s) -> str:
    """A session's name as the app shows it: its own, else its directory's, else its id's start."""
    return s["name"] or os.path.basename(s["cwd"] or "") or s["id"][:8]


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


def compacting_now(s) -> bool:
    """Whether the session is in a compaction cycle (#244): from Compact pressed, or any other
    start, to /compact's end, whatever the activity line says. Not liveness: a host that died
    mid-way leaves the state until the hub's sweep ends it, so a surface asks whether it's
    running too."""
    return (s["compaction"] if "compaction" in s.keys() else None) in COMPACTION_ACTIVE


def operating(s) -> tuple[str, str] | None:
    """The session's operation under way (#249), as (kind, state): its op row if active, else its
    compaction cycle (kind compact); None for neither. One answer for blue, for every refusal and for
    the keepalives. An outer op (Park or Relaunch compacting first) owns the lock over its inner cycle."""
    if models_field(s, "op") in OPS and models_field(s, "op_state") in OP_ACTIVE:
        return s["op"], s["op_state"]
    return ("compact", s["compaction"]) if compacting_now(s) else None


def op_overdue(s) -> bool:
    """Whether the session's active op is past its deadline (op_deadline, UTC). Read only."""
    return (models_field(s, "op_state") in OP_ACTIVE and bool(models_field(s, "op_deadline"))
            and datetime.now(timezone.utc) > datetime.fromisoformat(s["op_deadline"]))


# how long a cycle may stay requested before a host claims it (moves it on, at its next poll:
# the claim, never the notes turn starting, so a session in a long turn never fails); past it the
# cycle fails (a host that can't drive it: older code, wedged). Park's Compact first waits for
# its park instead, PARK_WITHIN, passed in as park_within; while its park's op is active, the op's
# deadline owns it (Hub.sweep_ops), so the two never race
CLAIM_WITHIN = 60


def requested_expired(s, park_within: float) -> float | None:
    """A session row's cycle, still requested past its deadline: that deadline (seconds), to
    expire it with (Store.expire_requested); None otherwise. Read only: no write lock taken."""
    if models_field(s, "compaction") != "requested" or not models_field(s, "compaction_at"):
        return None
    if s["compaction_origin"] == "park" and (operating(s) or ("", ""))[0] == "park":
        return None
    within = park_within if s["compaction_origin"] == "park" else CLAIM_WITHIN
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(s["compaction_at"])).total_seconds()
    return within if age > within else None


def due(within: float) -> str:
    """An absolute deadline, `within` seconds from now (UTC), as op_deadline holds it."""
    return (datetime.now(timezone.utc) + timedelta(seconds=within)).isoformat(timespec="microseconds")


def models_field(s, key: str):
    return s[key] if key in s.keys() else None


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
            if ("items", "pinned") in added:
                # docs were durable by kind before pins: those still showing stay so, but not one
                # already dismissed or deleted (finished, standing), which a pin would bring back
                db.execute(f"""UPDATE items SET pinned = (kind = 'doc' AND coalesce(dismissed NOT IN
                               ('{DELETED}', status), 1))""")
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
            db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (PRS_SINCE, now()))
            # the old offer's dismissed becomes Never, skipping every feature registered now;
            # taken, reset and unset are kept, so the registered features are pending for them
            if db.execute("SELECT 1 FROM settings WHERE key = ?", (TOUR_MIGRATED,)).fetchone() is None:
                if db.execute("SELECT 1 FROM settings WHERE key = ? AND value = 'dismissed'",
                              (TUTORIAL_OFFER,)).fetchone():
                    self._never(db)
                db.execute("INSERT INTO settings (key, value) VALUES (?, '1')", (TOUR_MIGRATED,))
            # the old tutorial checklist's record (#78 stage 3): the tour keeps its own steps in the app
            db.execute("DELETE FROM settings WHERE key = ?", (RETIRED_TUTORIAL_SEEN,))

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
            if runner != "sdk":
                self.unrequest_compact(db, sid)

    # a session's SDK host

    def set_activity(self, sid: str, text: str) -> None:
        """What the host's session is doing now, one line: idle, thinking, running Bash...
        A compaction's first line ("compacting...") marks its cut-off, if Compact pressed
        hasn't already; a line that ends one (COMPACT_ENDS) lets it go (compact_cut)."""
        with self.tx() as db:
            db.execute("UPDATE sessions SET activity = ? WHERE id = ?", (text, sid))
            if text.startswith("compacting"):
                self.compact_cut(db, sid, fresh=False)
            elif text.startswith(COMPACT_ENDS):
                db.execute("DELETE FROM settings WHERE key = ?", (COMPACT_CUTOFF + sid,))
                db.execute("UPDATE sessions SET idle_at = ? WHERE id = ?", (stamp(), sid))   # a turn has ended

    @staticmethod
    def compact_cut(db, sid: str, fresh: bool) -> None:
        """Where a compaction starts, once for every app: the newest item id the session has
        then (settings, COMPACT_CUTOFF), served on its row as compact_cutoff. Items at or
        below it are locked while it compacts (#150); one posted after never is. fresh: a
        Compact pressed, over any left behind."""
        db.execute(f"""INSERT INTO settings (key, value) SELECT ?, coalesce(max(id), 0) FROM items
                       WHERE session_id = ? ON CONFLICT (key) DO {"UPDATE SET value = excluded.value" if fresh else "NOTHING"}""",
                   (COMPACT_CUTOFF + sid, sid))

    def set_context(self, sid: str, tokens: int, max_tokens: int) -> None:
        """The context size the host's Claude Code reports, and when: the stats read it after
        a compaction, before the transcript has a response to size it by."""
        with self.tx() as db:
            db.execute("UPDATE sessions SET context_tokens = ?, context_max = ?, context_at = ? WHERE id = ?",
                       (tokens, max_tokens, stamp(), sid))

    def command(self, sid: str, what: str, notes: str | None = None, then: str | None = None,
                origin: str = "person") -> None:
        """Ask the session's host to interrupt, hand over to a shell tab, or compact. A newer
        command replaces one the host hasn't taken yet. A Compact may carry the person's
        instructions for it (notes: the session isn't asked what to keep) and what follows it
        (THEN; None, as before: its open items, as after any compaction). Compact starts the
        compaction cycle (requested, origin), in the same transaction. During a cycle only
        Interrupt goes (Busy): it is the host's to end the cycle as cancelled."""
        if what not in HOST_COMMANDS:
            raise ValueError(f"command must be one of {', '.join(HOST_COMMANDS)}, not {what!r}")
        if then is not None and then not in THEN:
            raise ValueError(f"then must be one of {', '.join(THEN)}, not {then!r}")
        plan = json.dumps({"notes": notes or None, "then": then}) if what == "compact" else None
        with self.tx() as db:
            if what != "interrupt":
                self.refuse_busy(db, sid, "compact" if what == "compact" else "open a shell tab")
            self._require(db, sid)
            db.execute("UPDATE sessions SET host_command = ?, compact_plan = ? WHERE id = ?", (what, plan, sid))
            if what == "shell":   # the hand-over to a Claude Code tab (#249): the host takes it, then hands over
                self.begin_op(db, sid, "handover", "requested", "person", HANDOVER_WITHIN)
            if what == "compact":
                self.begin_cycle(db, sid, origin, "requested", then)

    # the compaction cycle (#244)

    @staticmethod
    def refuse_busy(db, sid: str, action: str, person: bool = False) -> None:
        """Inside a write: Busy if the session is in an operation (operating, #249), SessionGone if
        gone. The one check for every dispatch and every operation. person: a send of the person's,
        which first calls off the quiet mark's cycle (call_off_quiet); the write then goes as normal."""
        s = db.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
        if s is None:
            raise SessionGone(sid)
        if person and Store.call_off_quiet(db, s):
            return
        if (op := operating(s)) is not None:
            raise Busy(session_name(s), action, op[0], tab=runner(s) != "sdk" or bool(s["shell"]))

    @staticmethod
    def call_off_quiet(db, s) -> bool:
        """The person has written in (a send or a queue): the quiet mark's cycle (origin quiet, the
        wheelhouse's own) is called off until its /compact has gone, cancelled here, and its host
        interrupts the notes turn (Host.cycle_due) (#244, D230). Whether it was."""
        if not (s["compaction_origin"] == "quiet" and s["compaction"] in ("requested", "preparing")
                and (operating(s) or ("", ""))[0] == "compact"):
            return False
        db.execute("UPDATE sessions SET compaction = 'cancelled', compaction_at = ? WHERE id = ?", (stamp(), s["id"]))
        db.execute("DELETE FROM settings WHERE key = ?", (COMPACT_CUTOFF + s["id"],))
        Store.staged(db, s["id"], "cancelled", "you wrote in, so it was called off")
        return True

    @staticmethod
    def drop_park_cycle(db, sid: str) -> None:
        """Park's Compact first called off before its /compact (the request cancelled, the park
        never coming, or parked without compacting): its cycle, still requested, is cancelled."""
        if db.execute("UPDATE sessions SET compaction = 'cancelled', compaction_at = ? WHERE id = ? "
                      "AND compaction = 'requested' AND compaction_origin = 'park'", (stamp(), sid)).rowcount:
            db.execute("DELETE FROM settings WHERE key = ?", (COMPACT_CUTOFF + sid,))
            Store.staged(db, sid, "cancelled", "the park was called off")

    def cancel_park_cycle(self, sid: str) -> None:
        with self.tx() as db:
            self.drop_park_cycle(db, sid)

    def give_up_park_compact(self, sid: str) -> bool:
        """The park asked for never came in time: its Compact first and its cycle go together, in
        one transaction, so a locked database can't leave one without the other. Whether it had one."""
        with self.tx() as db:
            took = db.execute("UPDATE sessions SET park_compact = 0, park_keep = NULL "
                              "WHERE id = ? AND park_compact = 1", (sid,)).rowcount == 1
            self.drop_park_cycle(db, sid)
        return took

    def expire_requested(self, sid: str, cid: str, within: float) -> bool:
        """A cycle still requested `within` seconds after its press fails (requested_expired):
        only that cycle, and only if no host has claimed it meanwhile. A Park's Compact first goes
        with it, in the same transaction; queued drafts stay queued. Whether it expired."""
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=within)).isoformat(timespec="microseconds")
        with self.tx() as db:
            expired = db.execute("UPDATE sessions SET compaction = 'failed', compaction_at = ?, park_compact = CASE "
                                 "WHEN compaction_origin = 'park' THEN 0 ELSE park_compact END, park_keep = CASE "
                                 "WHEN compaction_origin = 'park' THEN NULL ELSE park_keep END WHERE id = ? AND "
                                 "compaction_id = ? AND compaction = 'requested' AND compaction_at < ?",
                                 (stamp(), sid, cid, cutoff)).rowcount
            if expired:
                db.execute("DELETE FROM settings WHERE key = ?", (COMPACT_CUTOFF + sid,))
                self.drop_compact_command(db, sid)
                self.staged(db, sid, "failed", f"no host took it up within {within:g} s")
        return bool(expired)

    def drop_park_request(self, sid: str) -> None:
        """Interrupt during Park with Compact first, before it has parked: its request, its
        Compact first and its cycle all go, with nothing said to the session (Interrupt is)."""
        with self.tx() as db:
            db.execute("UPDATE sessions SET park_requested_at = NULL, park_told_at = NULL, park_compact = 0, "
                       "park_keep = NULL WHERE id = ?", (sid,))
            self.drop_park_cycle(db, sid)

    def check_busy(self, sid: str, action: str) -> None:
        """Busy if the session is in an operation (refuse_busy): for the hub's actions that are
        more than one write (Relaunch, Unpark, Park now, Fork), checked before the first."""
        with self.tx() as db:
            self.refuse_busy(db, sid, action)

    # the operation's row (#249): written only by the store methods that own its payload, in their transaction

    @staticmethod
    def begin_op(db, sid: str, kind: str, state: str, origin: str, within: float) -> str:
        """A new operation in state, due to end within `within` seconds: its id."""
        assert kind in OPS and state in OP_ACTIVE
        oid = secrets.token_hex(4)
        db.execute("UPDATE sessions SET op = ?, op_state = ?, op_id = ?, op_origin = ?, op_at = ?, op_deadline = ? "
                   "WHERE id = ?", (kind, state, oid, origin, stamp(), due(within), sid))
        return oid

    @staticmethod
    def move_op(db, sid: str, kind: str, state: str, within: float | None = None, since: tuple = OP_ACTIVE,
                oid: str | None = None) -> bool:
        """The session's active op of this kind (and id, given one), in one of `since`, enters state;
        an active state with a new deadline (within), else the old one stands. Whether it moved."""
        assert state in OP_ACTIVE + OP_ENDS
        # op_at is when its last active state was entered: an end keeps it, so a told op's stays its tell (refuse_late)
        return db.execute(f"UPDATE sessions SET op_state = ?, op_at = CASE WHEN ? IN {OP_ENDS} THEN op_at ELSE ? END, "
                          f"op_deadline = coalesce(?, op_deadline) "
                          f"WHERE id = ? AND op = ? AND op_state IN ({', '.join('?' * len(since))}) "
                          f"AND (? IS NULL OR op_id = ?)",
                          (state, state, stamp(), None if within is None else due(within), sid, kind, *since, oid, oid)
                          ).rowcount == 1

    def op(self, sid: str) -> sqlite3.Row | None:
        """The session's operation as the store holds it: kind, state, id, origin, at, deadline."""
        return self._one("SELECT op AS kind, op_state AS state, op_id AS id, op_origin AS origin, op_at AS at, "
                         "op_deadline AS deadline FROM sessions WHERE id = ?", (sid,))

    @staticmethod
    def unpark_request(db, sid: str, what: str, s, ends: str = "cancelled", why: str = "the park was called off") -> None:
        """A park or end request goes, with Compact first and any inner cycle (ended as `ends`); a session
        that was told hears the cancel message, so it can't park or end late. The op's end is the caller's."""
        db.execute(f"UPDATE sessions SET {what}_requested_at = NULL, {what}_told_at = NULL WHERE id = ?", (sid,))
        if what == "park":
            db.execute("UPDATE sessions SET park_compact = 0, park_keep = NULL WHERE id = ?", (sid,))
            if s["compaction_origin"] == "park" and compacting_now(s):
                db.execute("UPDATE sessions SET compaction = ?, compaction_at = ? WHERE id = ?", (ends, stamp(), sid))
                db.execute("DELETE FROM settings WHERE key = ?", (COMPACT_CUTOFF + sid,))
                Store.drop_compact_command(db, sid)
                Store.staged(db, sid, ends, why)
        if s[f"{what}_told_at"] or (what == "park" and s["op_state"] == "parking"):
            db.execute("INSERT INTO messages (session_id, author, body, created_at) VALUES (?, 'person', ?, ?)",
                       (sid, CANCELLED.format(what=what), now()))

    @staticmethod
    def refuse_late(db, sid: str, what: str, call: str) -> None:
        """A park or end called off (Interrupt, or past its deadline) before the session acted: its own
        park_session or end_session from the turn it was told in is refused, so it can't park or end
        after the person was told it carries on (#249). A later turn's, unprompted, goes. The turn
        boundary is its host's last turn end (idle_at); with none known (a tab, an older host, no turn
        ended yet), until the session has been handed the cancel message."""
        s = db.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
        if s is None or s["op"] != what or s["op_state"] not in ("failed", "cancelled"):
            return
        # only an op the session heard of: its end wrote the cancel message (told or parking, unpark_request),
        # at or after op_at (messages are stamped to the second)
        cancel = db.execute("SELECT delivered_at FROM messages WHERE session_id = ? AND body = ? AND "
                            "substr(created_at, 1, 19) >= substr(?, 1, 19) ORDER BY id DESC",
                            (sid, CANCELLED.format(what=what), s["op_at"])).fetchone()
        if cancel is None:
            return
        if runner(s) == "sdk" and not s["shell"] and s["idle_at"]:
            late = s["idle_at"] < s["op_at"]   # no turn has ended since it was told (or parked)
        else:
            late = cancel["delivered_at"] is None
        if late:
            raise ValueError(f"the {what} request was cancelled: carry on as before, and don't call {call}")

    def fail_op(self, sid: str, oid: str, why: str) -> sqlite3.Row | None:
        """An op past its deadline, or whose session stopped running before it acted, fails
        (Hub.sweep_ops): only that op, only while active, its payload (a park or end's request and
        inner cycle, a relaunch's, a Shell untaken) cleared in the same transaction. A parking
        session stays parked (it has, and its host stops). The row before, if it failed."""
        with self.tx() as db:
            s = db.execute("SELECT * FROM sessions WHERE id = ? AND op_id = ?", (sid, oid)).fetchone()
            if s is None or s["op_state"] not in OP_ACTIVE:
                return None
            if s["op"] == "relaunch" and s["op_state"] != "starting":   # its payload goes with it
                db.execute("UPDATE sessions SET relaunch = NULL WHERE id = ?", (sid,))
            elif s["op"] == "handover":   # a Shell the host never took
                db.execute("UPDATE sessions SET host_command = NULL WHERE id = ? AND host_command = 'shell'", (sid,))
            elif s["op"] not in REQUESTS:
                pass   # starting: the launch has gone as far as it will
            elif s["op_state"] == "parking":
                if s["compaction_origin"] == "park" and compacting_now(s):
                    db.execute("UPDATE sessions SET compaction = 'failed', compaction_at = ? WHERE id = ?", (stamp(), sid))
                    db.execute("DELETE FROM settings WHERE key = ?", (COMPACT_CUTOFF + sid,))
                    self.staged(db, sid, "failed", why)
            else:
                self.unpark_request(db, sid, s["op"], s, "failed", why)
            self.move_op(db, sid, s["op"], "failed", oid=oid)
        return s

    def complete_op(self, sid: str, oid: str, since: tuple = ("parking",)) -> bool:
        """The hub has seen the op's end (a park: parked, its process gone). Whether it completed."""
        with self.tx() as db:
            s = db.execute("SELECT op FROM sessions WHERE id = ? AND op_id = ?", (sid, oid)).fetchone()
            return s is not None and self.move_op(db, sid, s["op"], "completed", since=since, oid=oid)

    def abort(self, sid: str) -> tuple[str, str] | None:
        """Interrupt during a park or end (#249, Q245): the op cancelled and the session left as it
        was before the press, as nearly as its state allows, in one transaction. Requested: nothing
        was said, nothing is. Told: the session hears the cancel message. Parking: unparked, its
        Compact first and inner cycle cancelled, and it hears the cancel message. Told or parking,
        a running host is also asked to interrupt the turn or /compact (the only command left).
        The (kind, state) aborted, or None when no park or end was under way."""
        with self.tx() as db:
            s = db.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
            if s is None or (op := operating(s)) is None or op[0] not in REQUESTS + ("handover",):
                return None
            if op[0] == "handover":   # untaken, the Shell goes; taken, the hand-over is under way (Hub.host_command)
                if op[1] == "requested":
                    db.execute("UPDATE sessions SET host_command = NULL WHERE id = ? AND host_command = 'shell'", (sid,))
                    self.move_op(db, sid, "handover", "cancelled")
                return op
            self.unpark_request(db, sid, op[0], s)
            if op[1] == "parking":
                db.execute("UPDATE sessions SET parked = 0 WHERE id = ?", (sid,))
            self.move_op(db, sid, op[0], "cancelled")
            if op[1] != "requested" and runner(s) == "sdk" and not s["shell"]:
                db.execute("UPDATE sessions SET host_command = 'interrupt', compact_plan = NULL WHERE id = ?", (sid,))
        return op

    def fail_launch(self, sid: str) -> None:
        """The launch itself failed (wt.exe missing, already running...), said already: its op fails now."""
        with self.tx() as db:
            for kind in ("relaunch", "start"):
                self.move_op(db, sid, kind, "failed", since=("starting",))

    def abort_launch(self, sid: str) -> tuple[str, str] | None:
        """Interrupt during a Relaunch stopping the host, or a launch starting (#249, section 4), decided
        and done in one transaction. Stopping: the signal can't be taken back, so its payload goes and
        the new process is never started. Starting: what was sent with it (a wake's send) and not yet
        claimed goes back to the queue as drafts, and the op keeps a deadline, which marks its process
        as one to stop once it registers (Hub.stop_aborted_launch); a Relaunch's payload goes too. The
        (kind, state) aborted, or None for neither."""
        with self.tx() as db:
            s = db.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
            op = None if s is None else (s["op"], s["op_state"])
            if op not in (("relaunch", "stopping"), ("relaunch", "starting"), ("start", "starting")):
                return None
            db.execute("UPDATE sessions SET relaunch = NULL WHERE id = ?", (sid,))
            if op[1] == "starting" and (s["op_origin"] or "").startswith("wake:"):   # only the wake's own send
                first, last = map(int, s["op_origin"].split(":")[1:])
                db.execute("UPDATE messages SET draft = 1 WHERE session_id = ? AND draft = 0 AND author = 'person' "
                           "AND id BETWEEN ? AND ? AND claimed_at IS NULL AND delivered_at IS NULL", (sid, first, last))
            self.move_op(db, sid, op[0], "cancelled")
            if op[1] == "stopping":   # no process to stop: the mark is a deadline kept only by a starting one
                db.execute("UPDATE sessions SET op_deadline = NULL WHERE id = ?", (sid,))
            return op

    @staticmethod
    def begin_cycle(db, sid: str, origin: str, state: str, then: str | None = None, tokens: int | None = None) -> str:
        """A new compaction cycle, its cut-off taken (#150): its id."""
        assert origin in COMPACTION_ORIGINS and state in COMPACTION_ACTIVE
        cid = secrets.token_hex(4)
        db.execute("UPDATE sessions SET compaction = ?, compaction_id = ?, compaction_origin = ?, compaction_then = ?, "
                   "compaction_at = ?, compaction_item = NULL WHERE id = ?", (state, cid, origin, then, stamp(), sid))
        Store.compact_cut(db, sid, fresh=True)   # first: the task item, posted after, is never locked
        s = db.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
        tokens = tokens or s["context_tokens"]   # a tab's from its transcript (tokens), a host's as it reports
        ref = Store._insert_item(db, sid, "task", compaction_title(s, tokens), "", "running")
        db.execute("UPDATE items SET answer = ? WHERE session_id = ? AND ref = ?", (COMPACTION_TASK, sid, ref))
        db.execute("UPDATE sessions SET compaction_item = ? WHERE id = ?", (ref, sid))
        Store.staged(db, sid, state)
        return cid

    @staticmethod
    def staged(db, sid: str, state: str, why: str = "") -> None:
        """The cycle has entered state (begin_cycle, move_compaction, or an end written directly):
        its stage kept for the conversation (compaction_stages), and its task item's status and
        note moved with it, in the same transaction."""
        s = db.execute("SELECT compaction_id, compaction_origin, compaction_item FROM sessions WHERE id = ?",
                       (sid,)).fetchone()
        if s is None or s[0] is None:
            return
        db.execute("INSERT INTO compaction_stages (session_id, cycle_id, state, at, origin, note) VALUES (?, ?, ?, ?, ?, ?)",
                   (sid, s[0], state, stamp(), s[1], why))
        if s[2] is None:
            return
        status, note = COMPACTION_ITEM[state]
        note = note.format(origin=ORIGIN_WORDS.get(s[1], s[1]), why=why or state)
        db.execute("UPDATE items SET status = ?, updated_at = ? WHERE session_id = ? AND ref = ?", (status, now(), sid, s[2]))
        Store._said(db, sid, s[2], note, "note")

    def stages(self, sid: str) -> list[sqlite3.Row]:
        """The session's compaction stages, oldest first: cycle_id, state, at (UTC), origin, note."""
        return self._all("SELECT cycle_id, state, at, origin, note FROM compaction_stages WHERE session_id = ? "
                         "ORDER BY at, rowid", (sid,))

    def start_compaction(self, sid: str, origin: str, state: str = "requested", then: str | None = None,
                         tokens: int | None = None) -> str | None:
        """The host starts a cycle of its own (the quiet mark's, Park's Compact first, Claude Code's
        own): its id, or None if one is active already or the session has gone."""
        with self.tx() as db:
            s = db.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
            if s is None or compacting_now(s):
                return None
            # inside another operation, only its own (a park's) or Claude Code's, which runs anyway (#249)
            if (op := operating(s)) is not None and not (origin == "auto" or (origin == "park" and op[0] == "park")):
                return None
            return self.begin_cycle(db, sid, origin, state, then, tokens)

    def move_compaction(self, sid: str, cid: str, state: str, why: str = "", tokens: int | None = None) -> bool:
        """The host moves its cycle on: only the cycle it holds (cid), and only while active, so a
        cycle the hub's sweep has ended stays ended. Whether it moved. An end lets the cut-off go."""
        assert state in COMPACTION_ACTIVE + COMPACTION_ENDS
        with self.tx() as db:
            was = db.execute("SELECT compaction FROM sessions WHERE id = ? AND compaction_id = ?", (sid, cid)).fetchone()
            moved = db.execute(f"UPDATE sessions SET compaction = ?, compaction_at = ? WHERE id = ? AND compaction_id = ? "
                               f"AND compaction IN {COMPACTION_ACTIVE}", (state, stamp(), sid, cid)).rowcount
            if moved:
                self.staged(db, sid, state, why)
                if tokens:   # the size compaction began at, as its boundary says: the title's
                    s = db.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
                    db.execute("UPDATE items SET title = ? WHERE session_id = ? AND ref = ?",
                               (compaction_title(s, tokens), sid, s["compaction_item"]))
            if moved and state in COMPACTION_ENDS:
                db.execute("DELETE FROM settings WHERE key = ?", (COMPACT_CUTOFF + sid,))
                if was[0] == "requested":
                    self.drop_compact_command(db, sid)
        return bool(moved)

    @staticmethod
    def drop_compact_command(db, sid: str) -> None:
        """A requested cycle has ended unclaimed: its Compact command goes with it, so nothing is
        left behind for a host to take later (the cycle row is the only trigger, Q233)."""
        db.execute("UPDATE sessions SET host_command = NULL, compact_plan = NULL "
                   "WHERE id = ? AND host_command = 'compact'", (sid,))

    def compaction(self, sid: str) -> sqlite3.Row | None:
        """The session's cycle as the store holds it: state, id, origin, then, at, item."""
        return self._one("SELECT compaction AS state, compaction_id AS id, compaction_origin AS origin, "
                         "compaction_then AS then_, compaction_at AS at, compaction_item AS item "
                         "FROM sessions WHERE id = ?", (sid,))

    def compact_plan(self, sid: str, take: bool = False) -> tuple[str | None, str | None]:
        """The Compact command's instructions and what follows it (command), as last pressed;
        taken (cleared) by the host as it carries it out (take)."""
        with self.tx() as db:
            row = db.execute("SELECT compact_plan FROM sessions WHERE id = ?", (sid,)).fetchone()
            if take:
                db.execute("UPDATE sessions SET compact_plan = NULL WHERE id = ?", (sid,))
        plan = json.loads(row[0]) if row and row[0] else {}
        return plan.get("notes"), plan.get("then")

    def request_compact(self, sid: str, keep: str, gap: float = MODEL_COMPACT_GAP) -> None:
        """The session asks to be compacted, with keep as /compact's instructions: held here
        until its host's turn ends (promote_compact_request). Refused (ValueError, the reason)
        while one is pending or a compaction is under way, or within `gap` seconds of its last
        accepted request."""
        with self.tx() as db:
            s = db.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
            if s is None:
                raise SessionGone(sid)
            if s["compact_request"] is not None:
                raise ValueError("a compaction you asked for is already pending: it runs once this turn ends")
            if compacting_now(s):
                raise ValueError("this session is being compacted already")
            if (op := operating(s)) is not None:
                raise ValueError(SESSION_BUSY.format(doing=OP_DOING[op[0]], call="request_compact"))
            last = s["compact_requested_at"]
            ago = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() if last else None
            if ago is not None and ago < gap:
                raise ValueError(f"you asked for a compaction {int(ago // 60)} minutes ago: ask again no sooner "
                                 f"than {int(gap // 60)} minutes after the last")
            db.execute("UPDATE sessions SET compact_request = ?, compact_requested_at = ? WHERE id = ?",
                       (keep, stamp(), sid))

    def promote_compact_request(self, sid: str) -> bool:
        """The host, between turns: a pending request_compact becomes the Compact command,
        with its keep text as the instructions and then compact, as if the person had pressed
        Compact with them. Left alone while another command waits for the host. Whether it did."""
        with self.tx() as db:
            row = db.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
            if not row or row["compact_request"] is None or row["host_command"] is not None or operating(row):
                return False
            db.execute("UPDATE sessions SET compact_request = NULL, host_command = 'compact', compact_plan = ? "
                       "WHERE id = ?", (json.dumps({"notes": row["compact_request"], "then": "compact"}), sid))
            self.begin_cycle(db, sid, "model", "requested", "compact")
        return True

    def drop_compact_request(self, sid: str) -> None:
        """A request_compact still pending goes: the person's own command came first."""
        with self.tx() as db:
            self.unrequest_compact(db, sid)

    @staticmethod
    def unrequest_compact(db, sid: str) -> None:
        """A pending request_compact goes, and its gap with it: one that never ran locks the
        session out of none. Also on Park, Stop & Forget asked, a tab or a shell tab: stale keep
        text mustn't compact a later SDK start. A host restarted keeps it."""
        db.execute("UPDATE sessions SET compact_request = NULL, compact_requested_at = NULL "
                   "WHERE id = ? AND compact_request IS NOT NULL", (sid,))

    def take_command(self, sid: str, only: str | None = None) -> str | None:
        """The host takes the pending command, once. With `only`, it is taken only if it is
        that one: another, pressed meanwhile, stays for the host."""
        with self.tx() as db:   # read and clear in one write transaction
            row = db.execute("SELECT host_command FROM sessions WHERE id = ?", (sid,)).fetchone()
            if row and row[0] and only in (None, row[0]):
                db.execute("UPDATE sessions SET host_command = NULL WHERE id = ?", (sid,))
                if row[0] == "shell":
                    self.move_op(db, sid, "handover", "opening", HANDOVER_WITHIN, since=("requested",))
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
            if state is not None:
                self.unrequest_compact(db, sid)
                self.move_op(db, sid, "handover", "completed", since=("requested", "opening"))

    def set_relaunch(self, sid: str, host: tuple | None, deadline: float = 0.0, asked: float | None = None,
                     may_tab: bool = True, ends: str = "failed") -> None:
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
            # its op (#249): compacting first (requested), or stopping the host, until the launch starts it
            # (launching); done with before then, it ends as `ends` (given up: failed; Interrupt: cancelled)
            if host:
                state, within = "requested" if asked is not None else "stopping", deadline - time.time() + RELAUNCH_SLACK
                if not self.move_op(db, sid, "relaunch", state, within, since=("requested", "stopping")):
                    self.begin_op(db, sid, "relaunch", state, "person", within)
            else:
                self.move_op(db, sid, "relaunch", ends, since=("requested", "stopping"))

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
                 (SELECT value FROM settings WHERE key = '{COMPACT_CUTOFF}' || s.id) AS compact_cutoff,
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

    def set_doc_dirs(self, sid: str, dirs: list[str] | None) -> None:
        """The directories, relative to its git root, where a session's repo keeps its
        decisions (docroots.checked), or None to have docs/adr again."""
        with self.tx() as db:
            self._require(db, sid)
            db.execute("UPDATE sessions SET doc_dirs = ? WHERE id = ?", (json.dumps(dirs) if dirs else None, sid))

    def mark_launched(self, sid: str) -> None:
        """A (re)launch also drops any request left over from the last run. It keeps the last
        run's pid, which register_if_free compares against, and notes its start time in
        launched_over, so a registration since this launch shows (liveness.registered_this_launch)."""
        with self.tx() as db:
            db.execute("""UPDATE sessions SET launched_at = ?, launched_over = claude_start,
                          end_requested_at = NULL, park_requested_at = NULL, end_told_at = NULL, park_told_at = NULL
                          WHERE id = ?""", (now(), sid))
            for what in REQUESTS:   # their op fails with them (#249)
                self.move_op(db, sid, what, "failed")
            self.launching(db, sid)

    @staticmethod
    def launching(db, sid: str) -> None:
        """The launch's op (#249), in mark_launched's transaction: a Relaunch that stopped the host
        starts, and any other launch begins one, start for a session's first (New session, Adopt, a
        fork's child), relaunch after (Relaunch, Unpark, a wake's send), until the new process
        registers (_register). A host handing its session to a shell tab (shell) launches none."""
        s = db.execute("SELECT shell, heartbeat_at IS NULL AND claude_pid IS NULL AS first FROM sessions WHERE id = ?",
                       (sid,)).fetchone()
        if s is None or s["shell"]:
            return
        if not Store.move_op(db, sid, "relaunch", "starting", STARTING_WITHIN, since=("requested", "stopping")):
            Store.begin_op(db, sid, "start" if s["first"] else "relaunch", "starting", "launch", STARTING_WITHIN)

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
        for kind in ("relaunch", "start"):   # the new process: its launch's op has ended (#249)
            Store.move_op(db, sid, kind, "completed", since=("starting",))

    def request_end(self, sid: str) -> None:
        self.request(sid, "end")

    def request_park(self, sid: str) -> None:
        self.request(sid, "park")

    def request(self, sid: str, what: str, compact: bool = False, cycle: bool = True) -> None:
        """The person pressed End or Park on a running session; its monitor passes this on.
        Nothing happens to the data until the session acts, or the person forces it. A Park
        may ask a host to compact the session once it has parked, before it stops (compact)."""
        assert what in REQUESTS
        with self.tx() as db:
            self.refuse_busy(db, sid, "park" if what == "park" else "stop and forget")
            db.execute(f"UPDATE sessions SET {what}_requested_at = coalesce({what}_requested_at, ?) "
                       "WHERE id = ?", (stamp(), sid))
            self.begin_op(db, sid, what, "requested", "person", PARK_WITHIN)
            self.unrequest_compact(db, sid)
            if what == "park":
                db.execute("UPDATE sessions SET park_compact = ?, park_keep = NULL WHERE id = ?",
                           (int(compact), sid))
                if compact and cycle:   # Park with Compact first starts the cycle at the press (#244)
                    self.begin_cycle(db, sid, "park", "requested", "park")

    def take_park_compact(self, sid: str) -> bool:
        """The host takes the Park's Compact first (request, or park), once: to compact, or to
        drop it (the park not done in time). Its keep text goes with it."""
        with self.tx() as db:
            return db.execute("UPDATE sessions SET park_compact = 0, park_keep = NULL "
                              "WHERE id = ? AND park_compact = 1", (sid,)).rowcount == 1

    def tell_request(self, sid: str, what: str, asked: str) -> bool:
        """The monitor is about to pass a request on: record that, only if it is still the
        current request (a cancel may have just cleared it). Returns whether to print it."""
        with self.tx() as db:
            told = db.execute(f"UPDATE sessions SET {what}_told_at = ? WHERE id = ? "
                              f"AND {what}_requested_at = ?", (asked, sid, asked)).rowcount == 1
            if told:
                self.move_op(db, sid, what, "told", since=("requested",))
            return told

    def untell_request(self, sid: str, what: str) -> None:
        """Printing the request failed: it was never passed on after all."""
        with self.tx() as db:
            db.execute(f"UPDATE sessions SET {what}_told_at = NULL WHERE id = ?", (sid,))
            self.move_op(db, sid, what, "requested", since=("told",))

    def cancel_request(self, sid: str, what: str) -> None:
        """Clear a request. Only a session that was told about it hears that it's cancelled."""
        assert what in REQUESTS
        with self.tx() as db:
            self._require(db, sid)
            told = db.execute(f"SELECT {what}_told_at FROM sessions WHERE id = ?", (sid,)).fetchone()[0]
            db.execute(f"UPDATE sessions SET {what}_requested_at = NULL, {what}_told_at = NULL "
                       "WHERE id = ?", (sid,))
            if what == "park":
                db.execute("UPDATE sessions SET park_compact = 0, park_keep = NULL WHERE id = ?", (sid,))
                self.drop_park_cycle(db, sid)
            self.move_op(db, sid, what, "cancelled", since=("requested", "told"))
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

    def mark_mcp(self, sid: str, pid: int, code: str) -> None:
        """The session's wheelhouse MCP server, as it starts: its pid and the code it loaded."""
        with self.tx() as db:
            db.execute("UPDATE sessions SET mcp_pid = ?, mcp_code = ?, mcp_started_at = ? WHERE id = ?",
                       (pid, code, now(), sid))

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
                       "park_compact = park_compact * ?, park_keep = CASE WHEN ? THEN park_keep END "
                       "WHERE id = ?", (int(parked), int(parked), int(parked), sid))
            if parked:
                self.unrequest_compact(db, sid)

    def park(self, sid: str, compact: bool = True, keep: str = "", reason: str = "", hosted: bool = True) -> bool:
        """The session parks itself (park_session), in one transaction with what decides how.
        Answering the person's Park, told yet or not, their choice of Compact first stands,
        with keep as its instructions. Unasked in a host's session (hosted), it is compacted
        first unless compact is false, which needs a reason, posted as a decision. Compacting,
        park_keep holds keep ('' for a bare /compact), which tells the host the park was asked
        to compact. Its host does that once the park's turn has ended, and stops then either
        way. Not hosted (a tab), nothing compacts. Whether it compacts."""
        keep, reason = keep.strip(), reason.strip()
        with self.tx() as db:
            s = db.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
            if s is None:
                raise SessionGone(sid)
            # refused during a compaction cycle (#244), but Park's own with Compact first, whose park this is,
            # and during any other operation (#249) but the person's Park, which this answers
            op = operating(s)
            if op is not None and op[0] == "compact" and not (s["compaction_origin"] == "park" and s["compaction"] == "requested"):
                raise ValueError(PARK_REFUSED)
            if op is not None and op[0] not in ("compact", "park"):
                raise ValueError(SESSION_BUSY.format(doing=OP_DOING[op[0]], call="park_session"))
            if op is None:
                self.refuse_late(db, sid, "park", "park_session")
            asked = s["park_requested_at"] is not None
            if not hosted:
                compact = False
            elif asked:
                compact = bool(s["park_compact"])
            elif not compact:
                if not reason:
                    raise ValueError("compact=false needs a reason, shown to the person: why compacting is undesirable")
                self._insert_item(db, sid, "decision", PARKED_UNCOMPACTED,
                                  decision_body("", None, {"why": reason}), INITIAL_STATUS["decision"], False)
            db.execute("UPDATE sessions SET parked = 1, park_requested_at = NULL, park_told_at = NULL, "
                       "park_compact = ?, park_keep = ? WHERE id = ?", (int(compact), keep if compact else None, sid))
            self.unrequest_compact(db, sid)
            if not compact:
                self.drop_park_cycle(db, sid)
            # parking until its process has gone (Hub.sweep_ops); compacting, under the cycle's own clock
            within = PARK_WITHIN + COMPACT_TIMEOUT if compact else PARK_WITHIN
            if not self.move_op(db, sid, "park", "parking", within, since=("requested", "told")):
                self.begin_op(db, sid, "park", "parking", "session", within)
        return compact

    def end_session(self, sid: str) -> None:
        """The session forgets itself (end_session): refused during any operation (#249) but the
        person's Stop & Forget, which this answers."""
        with self.tx() as db:
            s = db.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
            if s is not None and (op := operating(s)) is not None and op[0] != "end":
                raise ValueError(SESSION_BUSY.format(doing=OP_DOING[op[0]], call="end_session"))
            self.refuse_late(db, sid, "end", "end_session")
        self.end(sid)

    def end(self, sid: str) -> None:
        with self.tx() as db:
            db.execute("DELETE FROM sessions WHERE id = ?", (sid,))
            db.execute("DELETE FROM compaction_stages WHERE session_id = ?", (sid,))
            # its PR merges held for a resume that won't come (steps.held_key), its compaction's cut-off
            db.execute("DELETE FROM settings WHERE key IN (?, ?)", (f"step.held.{sid}", COMPACT_CUTOFF + sid))

    # items

    def post_item(self, sid: str, kind: str, title: str, body: str = "", status: str | None = None,
                  pinned: bool = False, **decision: str) -> str:
        """A decision also takes why (DECISION_FIELDS), required: one short line with the
        option passed over, the reason and how to undo it, which joins its body; its status
        is the person's (unseen until they view it). pinned: kept out of reach of clearing the
        inbox (items.pinned), as the person can pin anything."""
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {sorted(KINDS)}")
        if kind == "doc":
            raise ValueError("a doc is posted with its path (post_doc)")
        if kind == "pr":
            raise ValueError("a PR is posted with its url (post_pr)")
        if kind == "decision":
            body = decision_body(body, status, decision)
            status = None
        elif any(decision.values()):
            raise ValueError(f"{', '.join(k for k, v in decision.items() if v)}: only a decision takes these")
        status = status or INITIAL_STATUS[kind]
        self._check_status(kind, status)
        with self.tx() as db:
            self._require(db, sid)
            return self._insert_item(db, sid, kind, title, body, status, pinned)

    def post_doc(self, sid: str, title: str, path: str, parent: str | None = None, pinned: bool = True) -> str:
        """A markdown document the session declares for the person to read: its absolute path,
        a file that exists, kept as the item's body; parent, a task, question or decision of
        the session's, to list it under. The same path posted again by the same session is the
        same item: its title (and parent, if given) updated, and once read, marked updated,
        never new again; its pin stays as the person last set it. pinned: a new one's (items.pinned),
        unpinned for a scratch report not worth keeping. Returns the item's ref."""
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
                ref = self._insert_item(db, sid, "doc", title, path, INITIAL_STATUS["doc"], pinned)
                db.execute("UPDATE items SET parent_ref = ? WHERE session_id = ? AND ref = ?", (parent, sid, ref))
                return ref
            db.execute(f"""UPDATE items SET title = ?, parent_ref = coalesce(?, parent_ref), updated_at = ?, session_at = ?,
                          status = CASE WHEN status = 'new' THEN 'new' ELSE 'updated' END, dismissed = {KEEP_DELETED}
                          WHERE session_id = ? AND ref = ?""", (title, parent, now(), now(), sid, row["ref"]))
            return row["ref"]

    def post_pr(self, sid: str, url: str, title: str, parent: str | None = None, pinned: bool = False,
                detected: bool = False) -> str | None:
        """A pull request the session made: its GitHub URL, kept canonical as the item's body
        (prs.canonical), and its ref the PR's number, PR108 (then PR108b, c... for another
        repo's same number in this session, so refs stay unique). The same URL posted again
        in the session is the same item: its title (and parent, if given) updated, its status
        left to GitHub. parent: a task, question or decision of the session's to list it under.
        detected: found in a transcript (prs.PrWatcher), not posted by the session: nothing
        is made when any session already has the URL, and an existing item keeps its title.
        Returns the item's ref, or None for a detected PR passed over."""
        canon = prs.canonical(url)
        if canon is None or canon != url.strip().rstrip("/"):
            raise ValueError(f"url must be a GitHub pull request, https://github.com/<owner>/<repo>/pull/<n>, not {url!r}")
        with self.tx() as db:
            self._require(db, sid)
            if parent:
                row = db.execute("SELECT kind FROM items WHERE session_id = ? AND ref = ?", (sid, parent)).fetchone()
                if row is None or row["kind"] not in PARENTS:
                    if not detected:
                        raise ValueError(f"parent {parent} is not a task, question or decision of this session")
                    parent = None
            if detected and db.execute("SELECT 1 FROM items WHERE kind = 'pr' AND body = ?", (canon,)).fetchone():
                return None
            row = db.execute("SELECT ref FROM items WHERE session_id = ? AND kind = 'pr' AND body = ? ORDER BY id LIMIT 1",
                             (sid, canon)).fetchone()
            if row is None:
                base = f"{KINDS['pr']}{prs.parse(canon)[2]}"
                taken = {r["ref"] for r in db.execute("SELECT ref FROM items WHERE session_id = ? AND ref LIKE ?",
                                                      (sid, base + "%"))}
                ref = next(base + s for s in ("", *"bcdefghijklmnopqrstuvwxyz") if base + s not in taken)
                db.execute("""INSERT INTO items (session_id, ref, kind, title, body, status, created_at, updated_at,
                              session_at, pinned, parent_ref) VALUES (?, ?, 'pr', ?, ?, ?, ?, ?, ?, ?, ?)""",
                           (sid, ref, title, canon, INITIAL_STATUS["pr"], now(), now(), now(), int(pinned), parent))
                return ref
            db.execute("UPDATE items SET title = ?, parent_ref = coalesce(?, parent_ref), updated_at = ?, session_at = ? "
                       "WHERE session_id = ? AND ref = ?", (title, parent, now(), now(), sid, row["ref"]))
            return row["ref"]

    def pr_items(self) -> list[dict]:
        """Every PR item in the store, any session's: its url, session, ref, status and title."""
        return [{"url": r["body"], "session_id": r["session_id"], "ref": r["ref"], "status": r["status"],
                 "title": r["title"]} for r in self._all("SELECT * FROM items WHERE kind = 'pr' ORDER BY id")]

    def set_pr_states(self, states: dict[str, str]) -> int:
        """GitHub's state for each PR, {url: draft, open, merged or closed}, on every item of
        it, any session's (the PRs pane's fetch). Only a change is written. One the person
        dismissed or deleted stays so, whatever its new state. Returns the items changed."""
        changed = 0
        with self.tx() as db:
            for url, status in states.items():
                self._check_status("pr", status)
                changed += db.execute(
                    f"""UPDATE items SET status = ?, updated_at = ?, dismissed = CASE WHEN dismissed IS NULL
                        THEN NULL WHEN dismissed = '{DELETED}' THEN dismissed ELSE ? END
                        WHERE kind = 'pr' AND body = ? AND status != ?""",
                    (status, now(), status, url, status)).rowcount
        return changed

    def post_compact_question(self, sid: str, title: str, body: str) -> str:
        """The host's question suggesting Compact, marked (COMPACT_QUESTION) as it is posted."""
        return self.post_marked_question(sid, title, body, COMPACT_QUESTION)

    def take_mcp_replies(self, sid: str, msgs) -> tuple[list, list]:
        """Claimed messages split into the person's replies on MCP questions (marked
        mcp_question) and the rest. Every reader of the person's messages (the host, a shell
        tab's monitor) splits through this, so none passes such a reply on as a turn. The
        replies' kept bodies are redacted (a pasted redirect URL's one-time code); the rows
        returned are as sent, for the host to act on."""
        marks: dict[str, bool] = {}
        mcp, rest = [], []
        for m in msgs:
            ref = m["item_ref"]
            if ref and ref not in marks:
                item = self.item(sid, ref)
                marks[ref] = bool(item and item["kind"] == "question" and mcp_server(item["answer"]))
            (mcp if ref and marks[ref] else rest).append(m)
        for m in mcp:
            if redacted(m["body"]) != m["body"]:
                self.redact_message(sid, m["id"], redacted(m["body"]))
        return mcp, rest

    def settle_mcp_replies(self, sid: str, note: str = MCP_IN_TAB) -> int:
        """Where no host acts on them (a terminal tab): the person's replies on MCP questions
        taken, their sign-in codes redacted, and each question noted. How many."""
        mcp, _ = self.take_mcp_replies(sid, self.claim(sid, mcp="only"))
        self.confirm(mcp)
        for ref in dict.fromkeys(m["item_ref"] for m in mcp):
            try:
                self.update_item(sid, ref, note=note)
            except (KeyError, SessionGone):
                pass
        return len(mcp)

    def redact_message(self, sid: str, msg_id: int, body: str) -> None:
        """A message's body replaced by a redacted copy: a pasted secret, once used, not kept."""
        with self.tx() as db:
            db.execute("UPDATE messages SET body = ? WHERE session_id = ? AND id = ?", (body, sid, msg_id))

    def set_mcp_status(self, sid: str, servers: list[dict]) -> None:
        """The host's last look at the session's MCP servers (mcp_servers reads it)."""
        with self.tx() as db:
            db.execute("UPDATE sessions SET mcp_status = ? WHERE id = ?",
                       (json.dumps({"at": stamp(), "servers": servers}), sid))

    def ask_mcp(self, sid: str, do: str, server: str, **more) -> None:
        """Ask the session's host to act on one of its MCP servers, on its next poll: the
        same ask about the same server, already waiting, is replaced."""
        with self.tx() as db:
            row = db.execute("SELECT mcp_asks FROM sessions WHERE id = ?", (sid,)).fetchone()
            if row is None:
                raise SessionGone(sid)
            asks = [a for a in json.loads(row[0] or "[]") if (a.get("do"), a.get("server")) != (do, server)]
            asks.append({"do": do, "server": server, **more})
            db.execute("UPDATE sessions SET mcp_asks = ? WHERE id = ?", (json.dumps(asks), sid))

    def give_back_mcp_asks(self, sid: str, asks: list[dict], tries: int = 3) -> None:
        """Asks a host took but couldn't carry out (Claude Code didn't answer): back for its
        next poll, each at most `tries` times."""
        with self.tx() as db:
            row = db.execute("SELECT mcp_asks FROM sessions WHERE id = ?", (sid,)).fetchone()
            if row is None:
                return
            waiting = json.loads(row[0] or "[]")
            newer = {(a.get("do"), a.get("server")) for a in waiting}   # asked again meanwhile: the new one stands
            waiting += [{**a, "tries": a.get("tries", 0) + 1} for a in asks
                        if a.get("tries", 0) + 1 < tries and (a.get("do"), a.get("server")) not in newer]
            db.execute("UPDATE sessions SET mcp_asks = ? WHERE id = ?", (json.dumps(waiting) if waiting else None, sid))

    def take_mcp_asks(self, sid: str) -> list[dict]:
        """The host takes what it is asked to do about its MCP servers, once."""
        with self.tx() as db:
            row = db.execute("SELECT mcp_asks FROM sessions WHERE id = ?", (sid,)).fetchone()
            if not row or not row[0]:
                return []
            db.execute("UPDATE sessions SET mcp_asks = NULL WHERE id = ?", (sid,))
        return json.loads(row[0])

    def open_mcp_question(self, sid: str, server: str) -> str | None:
        """The ref of the host's open question about this MCP server, if there is one."""
        return next((i["ref"] for i in self.items(sid, include_closed=False)
                     if i["kind"] == "question" and i["status"] not in CLOSED and mcp_server(i["answer"]) == server),
                    None)

    def post_marked_question(self, sid: str, title: str, body: str, mark: str, parent: str | None = None) -> str:
        """A question of the host's own, marked in its answer column as it is posted: one
        transaction, so no reader sees it unmarked. parent: the item it goes under."""
        with self.tx() as db:
            self._require(db, sid)
            ref = self._insert_item(db, sid, "question", title, body, INITIAL_STATUS["question"])
            db.execute("UPDATE items SET answer = ?, parent_ref = ? WHERE session_id = ? AND ref = ?",
                       (mark, parent, sid, ref))
        return ref

    @staticmethod
    def _insert_item(db, sid, kind, title, body, status, pinned=False) -> str:
        n = db.execute("SELECT count(*) FROM items WHERE session_id = ? AND kind = ?", (sid, kind)).fetchone()[0]
        ref = f"{KINDS[kind]}{n + 1}"
        db.execute(
            """INSERT INTO items (session_id, ref, kind, title, body, status, created_at, updated_at, session_at, pinned)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (sid, ref, kind, title, body, status, now(), now(), now(), int(pinned)),
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
                              dismissed, moved_to, pinned) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",   # a stand-in: never pinned
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
        """The session's word on an item. A reply on a doc is one more the person hasn't seen
        (items.new_replies, until they view it: _mark_seen)."""
        if kind == "reply":
            db.execute("UPDATE items SET new_replies = new_replies + 1 WHERE session_id = ? AND ref = ? AND kind = 'doc'",
                       (sid, ref))
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
        or `Q2 reply owed`: a task running, blocked or waiting, or a question or doc whose latest
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
            f"""SELECT ref, kind, status FROM items i WHERE session_id = ? AND coalesce(answer, '') != '{COMPACTION_TASK}'
                AND max(coalesce(session_at, created_at), CASE WHEN kind IN ('question', 'doc') THEN coalesce(
                    (SELECT max({REACHED.format(m="m.")}) FROM messages m WHERE m.session_id = i.session_id
                     AND m.item_ref = i.ref AND m.author = 'person' AND m.draft = 0), '') ELSE '' END) <= ?
                AND ((kind = 'task' AND status IN ({','.join('?' * len(TRANSIENT))}))
                     OR (((kind = 'question' AND status = 'open') OR kind = 'doc')
                         AND (session_id, ref) IN ({AWAITING})))
                ORDER BY id""", (sid, cutoff, *TRANSIENT))
        return [f"{r['ref']} {'reply owed' if r['kind'] in ('question', 'doc') else r['status']}" for r in rows]

    def heard_at(self, sid: str) -> str | None:
        """When the session was last sent anything: the latest a message of the person's (a
        notice included) was sent, claimed or delivered (REACHED); None if never."""
        row = self._one(f"""SELECT max(CASE WHEN author = 'person' AND draft = 0 THEN {REACHED.format(m="")} END) AS at
                              FROM messages WHERE session_id = ?""", (sid,))
        return row["at"] or None

    def open_items(self, sid: str) -> list[sqlite3.Row]:
        """What a session is told after a compaction: every task not settled and every open
        question, as list_items orders them; no agent, decision or permission items."""
        return [r for r in self.items(sid, include_closed=False) if not wheelhouse_item(r["answer"])
                and ((r["kind"] == "task" and r["status"] not in SETTLED["task"])
                     or (r["kind"] == "question" and r["status"] == "open"))]

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
            "UPDATE items SET status = CASE kind WHEN 'decision' THEN 'seen' ELSE 'read' END, new_replies = 0, "
            "updated_at = CASE WHEN status IN ('unseen', 'new', 'updated') THEN ? ELSE updated_at END "
            "WHERE session_id = ? AND ref = ? AND ((kind = 'decision' AND status = 'unseen') "
            "OR (kind = 'doc' AND (status IN ('new', 'updated') OR new_replies > 0)))", (now(), sid, ref)).rowcount > 0

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
            rows = family(db.execute("SELECT ref, kind, status, dismissed, parent_ref, pinned FROM items WHERE session_id = ?",
                                     (sid,)).fetchall(), ref)
            for r in rows:   # a finished one too: one dismissed already would come back on a change
                if spared(r):
                    db.execute("UPDATE items SET parent_ref = NULL WHERE session_id = ? AND ref = ?", (sid, r["ref"]))
                    continue
                closed = "closed" if r["kind"] in ("question", "decision") else r["status"]
                db.execute("UPDATE items SET status = ?, dismissed = ?, updated_at = ? WHERE session_id = ? AND ref = ?",
                           (closed, DELETED, now(), sid, r["ref"]))
        return [r["ref"] for r in rows if not spared(r)]

    def set_pinned(self, sid: str, ref: str, pinned: bool) -> None:
        """The person pins an item, or clears its pin (the inbox's pin column): pinned, clearing
        the inbox never takes it (family, standing)."""
        with self.tx() as db:
            self._require(db, sid)
            done = db.execute("UPDATE items SET pinned = ? WHERE session_id = ? AND ref = ?",
                              (int(pinned), sid, ref)).rowcount
        if not done:
            raise KeyError(f"no item {ref} in this session")

    def undelete(self, sid: str, ref: str) -> None:
        with self.tx() as db:
            db.execute("UPDATE items SET dismissed = NULL WHERE session_id = ? AND ref = ? AND dismissed = ?",
                       (sid, ref, DELETED))

    def close_decision(self, sid: str, ref: str, closed: bool = True, status: str | None = None) -> None:
        """The person closes a decision (or reopens it, as seen, or as `status`: Ctrl+Z)."""
        with self.tx() as db:
            self._require(db, sid)
            done = db.execute("UPDATE items SET status = ?, updated_at = ? WHERE session_id = ? AND ref = ? "
                              "AND kind = 'decision'", ("closed" if closed else status or "seen", now(), sid, ref)).rowcount
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
        if item["kind"] == "pr":
            raise ValueError(f"{item['ref']} is a PR: its status comes from GitHub. Change its title or "
                             "parent, or reply without a status.")
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

    # the tour (whatsnew): which features the person has been shown or skipped

    def tour_seen(self) -> dict[str, str]:
        """Each feature with a record, and how: shown or skipped."""
        return {r["feature_id"]: r["how"] for r in self._all("SELECT feature_id, how FROM tour_seen")}

    def see_features(self, ids, how: str = "shown") -> None:
        """Records features as seen; one already recorded keeps its first record."""
        if how not in TOUR_HOW:
            raise ValueError(f"how must be one of {TOUR_HOW}")
        with self.tx() as db:
            self._see(db, ids, how)

    def tour_never(self) -> None:
        """The tour's invite answered Never: every feature registered now is skipped, so only
        later ones are offered."""
        with self.tx() as db:
            self._never(db)

    @staticmethod
    def _see(db, ids, how: str) -> None:
        at = now()
        db.executemany("INSERT OR IGNORE INTO tour_seen (feature_id, seen_at, how) VALUES (?, ?, ?)",
                       [(i, at, how) for i in ids])

    @classmethod
    def _never(cls, db) -> None:
        db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                   "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (TUTORIAL_OFFER, NEVER))
        cls._see(db, [f.id for f in whatsnew.FEATURES], "skipped")

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
        to an unseen decision marks it seen, in the same transaction (_mark_seen). Refused
        during a operation (Busy), but an answer to a call the notes turn waits on
        (waited_on), so it can't wedge on its own prompt."""
        with self.tx() as db:
            self.refuse_wheelhouse_item(db, sid, item_ref)
            if not self.waited_on(db, sid, item_ref):
                self.refuse_busy(db, sid, "send", person=True)
            self._require(db, sid)
            self._mark_seen(db, sid, item_ref)
            db.execute(
                "INSERT INTO messages (session_id, item_ref, author, body, created_at) VALUES (?, ?, 'person', ?, ?)",
                (sid, item_ref, body, now()),
            )

    def check_sendable(self, sid: str, ref: str | None) -> None:
        """Refused, before any other step, for a message on a compaction's task item."""
        with self.tx() as db:
            self.refuse_wheelhouse_item(db, sid, ref)

    @staticmethod
    def refuse_wheelhouse_item(db, sid: str, ref: str | None) -> None:
        """Refused: a message on a compaction's task item, which no session ever sees (#244)."""
        if ref and db.execute("SELECT 1 FROM items WHERE session_id = ? AND ref = ? AND answer = ?",
                              (sid, ref, COMPACTION_TASK)).fetchone():
            raise Refused(NOT_THE_SESSIONS.format(ref=ref))

    @staticmethod
    def waited_on(db, sid: str, ref: str | None) -> bool:
        """Whether ref is an open call the session's turn waits on: a permission, or a question
        the host asked for AskUserQuestion."""
        row = ref and db.execute("SELECT kind, status, answer FROM items WHERE session_id = ? AND ref = ?",
                                 (sid, ref)).fetchone()
        return bool(row) and row["status"] == "open" and (row["kind"] == "permission" or row["answer"] == HOST_QUESTION)

    def waits_on(self, sid: str, ref: str | None) -> bool:
        """Whether ref is an open call the session's turn waits on (waited_on)."""
        with self.tx() as db:
            return self.waited_on(db, sid, ref)

    def notice(self, sid: str, body: str) -> None:
        """A message from the wheelhouse itself (not the person), sent now."""
        with self.tx() as db:
            self._require(db, sid)
            db.execute("INSERT INTO messages (session_id, author, body, created_at, kind) "
                       "VALUES (?, 'person', ?, ?, 'notice')", (sid, body, now()))

    def queue(self, sid: str, body: str, item_ref: str | None = None) -> None:
        """A message from the person, held as a draft until dispatch() sends it. An answer to
        an unseen decision marks it seen, in the same transaction (_mark_seen). Allowed during any
        operation (Q244): only dispatch waits. It still calls off the quiet mark's cycle (call_off_quiet)."""
        with self.tx() as db:
            self.refuse_wheelhouse_item(db, sid, item_ref)
            self._require(db, sid)
            self.call_off_quiet(db, db.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone())
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
        message at once, so the session never sees part of a batch. Returns how many went.
        Refused during a operation (Busy): its host sends them once it has
        completed, with then send."""
        with self.tx() as db:
            self.refuse_busy(db, sid, "send", person=True)
            sent = db.execute("UPDATE messages SET draft = 0 WHERE session_id = ? AND draft = 1 RETURNING id",
                              (sid,)).fetchall()
        return len(sent)

    def dispatch_last(self, sid: str, body: str | None = None, item_ref: str | None = None, waking: bool = False) -> int:
        """Send the session's drafts, and body if given, behind every message sent before them,
        in one transaction: a session woken to send them (Hub.wake_and_send) reads the resume
        notice its launch left first, then the person's words, oldest first, in the one batch
        the monitor or host claims. The drafts are sent as new rows, so their order follows the
        notice's; each keeps its text, item and time. Returns how many went. Refused during a
        operation (Busy), and on a compaction's task item (Refused)."""
        with self.tx() as db:
            self.refuse_wheelhouse_item(db, sid, item_ref)
            s = db.execute("SELECT op, op_state FROM sessions WHERE id = ?", (sid,)).fetchone()
            # a wake's own send, as its launch starts (#249): the op's payload, never refused by it
            own = waking and s is not None and s["op"] in ("relaunch", "start") and s["op_state"] == "starting"
            if not own:
                self.refuse_busy(db, sid, "send", person=True)
            first = db.execute("SELECT coalesce(max(id), 0) + 1 FROM messages").fetchone()[0]
            moved = db.execute("INSERT INTO messages (session_id, item_ref, author, body, created_at, kind) "
                               "SELECT session_id, item_ref, author, body, created_at, kind FROM messages "
                               "WHERE session_id = ? AND draft = 1 ORDER BY id", (sid,)).rowcount
            db.execute("DELETE FROM messages WHERE session_id = ? AND draft = 1", (sid,))
            if body is not None:
                self._mark_seen(db, sid, item_ref)
                db.execute("INSERT INTO messages (session_id, item_ref, author, body, created_at) "
                           "VALUES (?, ?, 'person', ?, ?)", (sid, item_ref, body, now()))
            if own:   # the wake's send, first and last id, so an Interrupt queues exactly it again (abort_launch)
                last = db.execute("SELECT coalesce(max(id), 0) FROM messages").fetchone()[0]
                db.execute("UPDATE sessions SET op_origin = ? WHERE id = ?", (f"wake:{first}:{last}", sid))
        return moved + (body is not None)

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

    def claim(self, sid: str, item_ref: str | None = None, mcp: str = "skip") -> list[sqlite3.Row]:
        """Claim undelivered messages in one transaction, so the monitor and get_input()
        never both hand Claude the same one. The claimer confirms or releases them; a claim
        left by a monitor that died mid-print is retaken after CLAIM_TIMEOUT. Every reader
        claims through here, and the person's replies on MCP questions (MCP_REPLY) are left
        out unless asked for: mcp "with" (the host, which acts on them) or "only"
        (settle_mcp_replies)."""
        stale = (datetime.now(timezone.utc) - timedelta(seconds=CLAIM_TIMEOUT)).isoformat(timespec="microseconds")
        which = {"skip": f"AND NOT {MCP_REPLY}", "with": "", "only": f"AND {MCP_REPLY}"}[mcp]
        with self.tx() as db:
            rows = db.execute(
                f"""UPDATE messages SET claimed_at = ? WHERE session_id = ? AND delivered_at IS NULL
                   AND draft = 0 AND (claimed_at IS NULL OR claimed_at < ?) AND (? IS NULL OR item_ref = ?)
                   {which} RETURNING *""", (stamp(), sid, stale, item_ref, item_ref)
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


def ranked_status(item) -> str:
    """The status an item ranks by: a PR's draft or open as a running task's (it waits on no one
    here), everything else its own."""
    return "running" if item["kind"] == "pr" and item["status"] in ("draft", "open") else item["status"]


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


def pinned(item) -> bool:
    """Whether an item is pinned (items.pinned): out of reach of clearing the inbox, and shown
    finished with F off. Not on a row from older code, which has no such column (or NULL)."""
    try:
        return bool(item["pinned"])
    except (KeyError, IndexError):
        return False


def family(items, ref: str, parent=lambda it: it["parent_ref"]) -> list:
    """What Delete on `ref` acts on, among its session's `items`: its children and, under each
    child that goes, theirs, at any depth. One spared stays, with whatever is under it, so the
    walk stops there. Never one pinned: out of reach of clearing the inbox (.plan/docs-section.md),
    it stays, under its parent once Ctrl+R brings that back, with whatever is under it."""
    under: dict[str, list] = {}
    for it in items:
        if not pinned(it):
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
# (or status update: STATUS). A session's message with no kind was written before messages had
# one, when a reply was all a session wrote on an item: it counts as a reply (#76)
AWAITING = f"""SELECT session_id, item_ref FROM messages WHERE item_ref IS NOT NULL AND draft = 0
              GROUP BY session_id, item_ref
              HAVING max(CASE WHEN author = 'person' THEN id END) >
                     coalesce(max(CASE WHEN author = 'claude' AND coalesce(kind, 'reply') IN ('reply', '{STATUS}')
                                  THEN id END), 0)"""
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
    active, even if they dismissed it: the ball isn't back with them. A pin changes none of
    this: a pinned item finished is still finished (the inbox shows it with F off all the same)."""
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
        else ranked_status(item)
    return (RANK.get(status, 9), _neg_time(item["updated_at"]), -item["id"])


@functools.lru_cache(maxsize=8192)   # each item's, every refresh: parsed once, not each second
def _neg_time(iso: str) -> float:
    return -datetime.fromisoformat(iso).timestamp()
