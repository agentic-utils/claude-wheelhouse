"""A session's SDK host: runs one Claude Code session through the Agent SDK, detached from
the TUI, and talks to everything else through the store, like the monitor and MCP server.

EXPERIMENTAL: SDK mode is not for use by anyone other than the author until Anthropic's
terms for it are confirmed. Nothing creates a hosted session unless SDK mode is on
(--enable-sdk, or ENABLE_SDK=true in a .env: features.py); a session hosted already keeps
its host whatever the flag says.

The host is the session's user. The person's sent messages become turns, End and Park
requests become turns, and Claude Code's permission checks and AskUserQuestion calls
become wheelhouse items that the host waits on. The TUI asks for an interrupt, a hand-over
to a shell tab, or a compaction through sessions.host_command.

The host registers its own pid as the session's process, so liveness, Restore and
relaunch treat it like a tab's Claude. It exits when the session ends or parks, and on a
hand-over to a tab it gives up the session, waits for the tab to exit, then takes it back.
"""

import asyncio
import contextlib
import getpass
import json
import os
import re
import secrets
import shutil
import signal
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import features, liveness, models, plan_usage, steps, transcript, transcript_usage
from .launch import PROTOCOL_SDK, after_compaction, opening_prompt, transcript_exists
from .monitor import REQUEST_TEXT, request_text
from .store import (CLOSED, COMPACT_QUESTION, COMPACT_QUESTION_DONE, HOST_QUESTION, NOTES_WAIT, SessionGone, compacting_now, requested_expired, Store, callback_url, db_path,
                    mcp_named, mcp_question, mcp_server, now, runner, wheelhouse_question)

POLL_SECONDS = 1.0
SHELL_POLL_SECONDS = 2.0
CONFIRM_TRIES = (0.1, 0.5, 2.0)   # waits between tries to mark sent messages delivered
STEADY_TRIES = (0.1, 0.5, 2.0, 5.0)   # waits between tries of a write the shell hand-over can't skip
# the compaction cycle (#244, .plan/244-compaction-cycle.md): how long /compact may run before the
# cycle fails, and how long the notes turn runs before the person is asked to keep waiting or abort
COMPACT_TIMEOUT = 600
NOTES_TIMEOUT = 120
# asked of a session when it's to be compacted, after the Before Compact step (steps.py); the
# notes in its reply, inside the block carrying this request's id, become /compact's instructions
KEEP_ASK = ("Then reply with the notes you want carried through compaction (open threads, decisions, file "
            "paths, refs and next steps, as a terse list), with the line <keep id=\"{nonce}\"> before "
            "them and the line </keep> after them, each tag on a line of its own.")
PRESSED_COMPACT = "The person pressed Compact in the wheelhouse."
QUIET_COMPACT = ("This session has been quiet {hours} hours: the wheelhouse compacts it now, while its prompt "
                 "cache is warm.")


def compact_ask(store: Store, nonce: str, why: str = PRESSED_COMPACT) -> str:
    """The Before Compact step, as switched, then the keep ask."""
    return steps.message(store, "before_compact", KEEP_ASK.format(nonce=nonce), why=why)
CLOSED_UNANSWERED = "The person closed this in the wheelhouse without answering."
LOST_ON_RESTART = "The wheelhouse host restarted while this was waiting: Claude Code will ask again if it still needs it."
# Claude Code withdrew the call it was asking about (an interrupt, or the host letting go for a shell tab)
CANCELLED = "cancelled: Claude Code withdrew this (interrupted, or handed to a shell tab), so nothing is waiting on it"
# how a cycle that didn't complete ends the activity line (Host.end_cycle)
NOT_COMPACTED = "idle · not compacted: {why}"
TIMED_OUT = f"no result in {COMPACT_TIMEOUT // 60} minutes"
# the question when the notes turn passes NOTES_TIMEOUT: Keep waiting asks again after another
# NOTES_TIMEOUT, Abort interrupts the notes turn and cancels the cycle (Hub.notes_wait)
NOTES_WAIT_TITLE = "{name}'s notes for compaction: keep waiting?"
NOTES_WAIT_BODY = (f"The session has been writing what to keep through compaction for over {NOTES_TIMEOUT // 60} "
                   "minutes. Keep waiting, and you're asked again if it is still going in "
                   f"{NOTES_TIMEOUT // 60} minutes; or Abort, which interrupts it and cancels the compaction: "
                   "nothing is compacted, and queued messages stay queued.")
SHELL_OPENING = "opening a shell tab"   # the activity line while Shell hands the session over
COMPACTED = "idle · compacted"   # how the activity line starts once a compaction's turn has ended
# A context past this many tokens gets one question item suggesting Compact, per crossing
# (COMPACT_QUESTION): every turn re-reads the whole context from the prompt cache
NUDGE_TOKENS = 200_000
# how soon after the park request is told the session must park for its Compact first to
# hold: the prompt cache's lifetime, after which compacting saves nothing
PARK_COMPACT_WITHIN = 3600
PARKS_AFTER_TURN = "parks once this turn ends"   # the activity line's note while a park waits for its turn
# Keepalive: the prompt cache lives an hour (1 h cache writes) and a cold one is written
# again whole. A session over KEEPALIVE_TOKENS, quiet and waiting on the person or with an
# item in doubt (Store.in_doubt), gets a tiny turn KEEPALIVE_AFTER seconds after its last, to
# keep it warm, until it has been quiet features.keepalive_hours(): an abandoned session
# doesn't pay for ever. With an item in doubt the turn is the check (CHECK_BODY), which asks
# the session about it. The hub keeps tab sessions warm the same way (Hub.keep_warm)
KEEPALIVE_TOKENS = 150_000
KEEPALIVE_AFTER = 50 * 60   # before Claude Code idle-compacts a tab at about 54 minutes
KEEPALIVE_BODY = ("Keepalive: the person is away, and this turn only keeps your prompt cache "
                  "warm. Reply with the one word ok and do nothing else: no tools, no wheelhouse calls.")
KEEPALIVE_TEXT = f"[wheelhouse] {KEEPALIVE_BODY}"
# MCP servers (#179, .plan/mcp-reauth.md, .plan/mcp-on-demand.md): a hosted session has no
# /mcp, so when it needs a server that wants signing in or has failed, it asks the host
# (request_mcp_signin), which asks the person and reconnects the server on their reply
MCP_ASK = ("needs-auth", "failed")   # the statuses the host posts a question for
MCP_SETTLED = ("connected", "disabled")   # the statuses that close it
# the kinds of server whose failure may be a sign-in one (a rejected token), so a failed one
# is offered a sign-in link too: claude.ai connectors and remote servers
MCP_AUTH_KINDS = ("claudeai-proxy", "http", "sse")
MCP_CONNECTOR = "claudeai-proxy"   # a claude.ai connector's config type
MCP_RECHECK = 30   # seconds between checks while a question about one is open
MCP_FLOW_LIFE = 300   # seconds Claude Code's local OAuth flow waits for the browser before it gives up
MCP_WITHIN = 20   # seconds a reply's sign-in and reconnect may take before the poll moves on
# a claude.ai connector stays needs-auth in this Claude Code until it is reconnected, however
# long ago it was authorised on claude.ai. So while its question is open, the check reconnects
# it: at each recheck for the first MCP_RETRY_FAST seconds, then every MCP_RETRY_SLOW, and not
# after MCP_RETRY_STOP, each try cut off after MCP_RETRY_WITHIN. A reply still reconnects at once
MCP_RETRY_FAST = 10 * 60
MCP_RETRY_SLOW = 5 * 60
MCP_RETRY_STOP = 2 * 3600
MCP_RETRY_WITHIN = 5
# what the MCP servers dialog asks a host to do (Hub.mcp): look again, reconnect a server, start
# its sign-in, or finish one with the address the browser ended on
MCP_DOS = ("check", "reconnect", "signin", "callback")
MCP_GONE = ("The wheelhouse host restarted. MCP servers are now asked about only when a session needs one: "
            "Alt+Shift+M lists them all.")
MCP_SHELL = "Handed to a shell tab: /mcp there manages MCP servers."
MCP_STOPPED = "The wheelhouse host stopped: the session asks again if it still needs this server."
MCP_ELSEWHERE = "Nothing acted on this reply: {ref} is the open question about {name}, so reply there."
MCP_CLOSED = ("This question is closed, so the wheelhouse didn't act on the reply. A new question comes "
              "if the session needs the server again.")


def mcp_item(name: str, server: dict, auth: dict | None, why: str = "") -> tuple[str, str]:
    """The question about an MCP server: its title and body. auth: what mcp_authenticate
    said, for one that needs signing in, or failed in a way signing in may cure. why: what
    the session needs it for, as it said (request_mcp_signin)."""
    failed = server.get("status") == "failed"
    title = f"MCP {name} failed" if failed else f"MCP {name}: sign in"
    head = (f"The MCP server {name} failed to connect: {(server.get('error') or 'no reason given').strip()}"
            if failed else f"The MCP server {name} needs signing in.")
    if why:
        head = f"The session needs it: {why}\n\n{head}"
    url = (auth or {}).get("authUrl")
    retried = not failed and (server.get("config") or {}).get("type") == MCP_CONNECTOR
    if (auth or {}).get("callbackExpected"):
        how = ("Open the link and sign in: Claude Code is waiting for the browser to come back, for "
               f"{MCP_FLOW_LIFE // 60} minutes, and connects it then. If the browser can't reach it, paste "
               "the address the browser ends on here as a reply; once the link has expired, any reply "
               "brings a fresh one. This closes by itself once it's connected.")
    elif retried:
        how = ("Open the link and authorise it. The wheelhouse reconnects it every few minutes for the next "
               f"{MCP_RETRY_STOP // 3600} hours, so this closes by itself once you have; or reply anything here "
               "and it reconnects it now.")
    else:   # authorised elsewhere: nothing here waits for it
        how = "Open the link and authorise it, then reply anything here and the wheelhouse reconnects it."
    if url:
        body = f"{head}\n\n{'If that is a sign-in problem, sign in here: ' if failed else ''}{url}\n\n{how}"
        if failed:
            body += " Otherwise, reply anything and the wheelhouse reconnects it."
    elif failed:
        body = f"{head}\n\nReply anything here and the wheelhouse reconnects it."
    else:
        body = (f"{head} Claude Code gave no sign-in link: Shell (Alt+H) opens this session in a tab, where "
                "/mcp signs in; or reply here to reconnect it.")
    if not url or not ((auth or {}).get("callbackExpected") or retried):   # those say so already
        body += " This closes once it's connected."
    return title, body


CHECK_BODY = ("Check: {items}. Correct any that's finished with update_item or reply, then reply ok. "
              "Leave alone any you don't recognise.")


def waits_on_person(store: Store, sid: str) -> bool:
    """Whether the session waits on the person: an open question, an unseen decision or an
    open permission. The host's keepalive and the hub's (tab sessions) both ask. The host's
    own question suggesting Compact is the wheelhouse's, not the session waiting."""
    return any((i["kind"] == "question" and i["status"] == "open" and not wheelhouse_question(i["answer"]))
               or (i["kind"] == "decision" and i["status"] == "unseen")
               or (i["kind"] == "permission" and i["status"] == "open")
               for i in store.items(sid, include_closed=False))


def keepalive_body(doubted: list[str]) -> str:
    """The keepalive's text, without the [wheelhouse] mark: the check on the items in doubt
    if there are any, else the plain keepalive."""
    return CHECK_BODY.format(items=", ".join(doubted)) if doubted else KEEPALIVE_BODY
# an answer's `n:` or `Qn:` markers: at the start, or after a comma, semicolon or line
# break, and never a time (`1:30`)
ANSWER_MARK = re.compile(r"(?:^|[,;\n])\s*[Qq]?(\d+)\s*:(?!\d)")


def stopped(why) -> str:
    """A stop reason for the activity line: one line, short."""
    return " ".join(f"stopped: {why}".split())[:160]


def where(m) -> str:
    return f"on {m['item_ref']}" if m["item_ref"] else "(general)"


def format_turn(msgs, person: str | None = None) -> str:
    """The person's messages sent since the last turn, as one user turn. No length limit
    here: the host types them, it doesn't print notifications."""
    person = person or getpass.getuser()
    parts = []
    for m in msgs:
        if "kind" in m.keys() and m["kind"] == "notice":
            parts.append(f"[wheelhouse] {m['body']}")
        else:
            parts.append(f"[wheelhouse] from {person} {where(m)}:\n{m['body']}")
    return "\n\n".join(parts)


def midturn_note(earlier: list[str], refs: list[str]) -> str:
    """Told with a turn sent while one of ours is running, which Claude Code may take up into
    it: that turn's closing text answers only the last message taken up (#243), so the
    earlier ones' refs need a reply of their own."""
    earlier = list(dict.fromkeys(earlier))
    goes = f"on {', '.join(refs)}" if refs else "on no item (it shows in the conversation only)"
    note = (f"[wheelhouse] Sent while your turn is running. If it reaches you mid-turn, the text you end "
            f"that turn with is posted as the reply {goes}")
    if earlier:
        note += (f"; {', '.join(earlier)} {'gets' if len(earlier) == 1 else 'get'} no automatic reply, "
                 f"so answer {'it' if len(earlier) == 1 else 'each'} with reply(ref)")
    return note + ". If it starts a turn of its own instead, ignore this note."


def keep_notes(text: str | None, nonce: str) -> str | None:
    """The notes in a reply's last complete block for this request: a line `<keep
    id="nonce">`, the notes, and a line `</keep>`, each tag on a line of its own, so prose
    that quotes the tags never counts. None if there is no such block; an empty block is
    notes too (nothing to keep), and compacts without instructions."""
    tag = rf"<keep\s+id=[\"']?{re.escape(nonce)}[\"']?\s*>"
    blocks = re.findall(rf"^[ \t]*{tag}[ \t]*\n(.*?)^[ \t]*</keep>[ \t]*$", text or "", re.DOTALL | re.MULTILINE)
    return blocks[-1].strip() if blocks else None


def nudge_item(tokens: int) -> tuple[str, str]:
    """The question suggesting Compact: its title and body."""
    k = tokens // 1000
    return (f"context {k}k: compact?",
            f"This session's context is {k}k tokens, and every turn re-reads all of it. Compact asks "
            "the session what to keep, then compacts it with that; instructions typed in the box "
            "below are used instead, without asking. Then, as chosen: just compact (the session is "
            "told its open items), send its queued answers in that same prompt, or park it at once, "
            "sending nothing more. Leave it closes this. It closes by itself once the session is "
            f"compacted, and isn't asked again until the context has been under {NUDGE_TOKENS // 1000}k "
            "and passes it once more.")


def tool_summary(name: str, inp: dict) -> str:
    """One line for a tool call: the activity line and a permission item's title."""
    for key in ("command", "file_path", "path", "url", "pattern", "description", "prompt"):
        if isinstance(inp.get(key), str) and inp[key].strip():
            text = " ".join(inp[key].split())
            return f"{name}: {text[:80]}{'…' if len(text) > 80 else ''}"
    return name


def permission_body(name: str, inp: dict, ctx) -> str:
    """What the person needs to approve a tool call: Claude Code's own description, then
    the call's input in full."""
    lines = []
    for attr in ("title", "description", "decision_reason"):
        text = getattr(ctx, attr, None)
        if text:
            lines.append(text)
    if name == "ExitPlanMode" and isinstance(inp.get("plan"), str):
        lines.append(inp["plan"])
    else:
        lines.append(f"```json\n{json.dumps(inp, indent=2, ensure_ascii=False)}\n```")
    lines.append("Allow, Always (keep the rule Claude Code suggests) or Deny, with the buttons over the "
                 "answer box. Deny asks for an optional reason, which the session reads as why, so it can try "
                 "something else.")
    return "\n\n".join(lines)


def question_item(inp: dict) -> tuple[str, str]:
    """AskUserQuestion's questions as one question item: title and body."""
    qs = inp.get("questions") or []
    title = (qs[0].get("header") or qs[0].get("question", "Question"))[:60] if qs else "Question"
    blocks = []
    for i, q in enumerate(qs, 1):
        head = f"**{i}. {q.get('question', '')}**" if len(qs) > 1 else f"**{q.get('question', '')}**"
        if q.get("multiSelect"):
            head += " (pick any)"
        opts = [f"- {o.get('label', '')}" + (f": {o['description']}" if o.get("description") else "")
                for o in q.get("options") or []]
        blocks.append("\n".join([head, *opts]))
    if len(qs) > 1:
        blocks.append("Answer each by number, e.g. `1: <option>, 2: <option>`, or in your own words.")
    else:
        blocks.append("Answer with an option's label, or in your own words.")
    return title, "\n\n".join(blocks)


def question_answers(inp: dict, reply: str) -> dict:
    """The person's reply as AskUserQuestion's answers. One question takes the reply whole;
    several take `n: answer` parts where given, else the whole reply each. A part runs to
    the next marker, so commas inside it stay (`1: red, blue, 2: large`); a number given
    twice keeps both parts, in order."""
    qs = inp.get("questions") or []
    if len(qs) <= 1:
        return {q.get("question", ""): reply.strip() for q in qs}
    marks = [m for m in ANSWER_MARK.finditer(reply) if 1 <= int(m.group(1)) <= len(qs)]
    parts = {}
    for m, nxt in zip(marks, marks[1:] + [None]):
        part = reply[m.end():nxt.start() if nxt else len(reply)].strip(" ,;\n\t")
        n = int(m.group(1))
        parts[n] = f"{parts[n]}, {part}" if n in parts else part
    return {q.get("question", ""): parts.get(i, reply.strip()) for i, q in enumerate(qs, 1)}


@dataclass
class Sent:
    """A turn sent: the refs it answers (ref: the person's newest message on it), whether
    it is a keepalive, and whether it carries the person's messages (person), as against the
    wheelhouse's own (a keepalive, a request, /compact)."""
    refs: dict[str, int]
    keepalive: bool = False
    person: bool = False


async def user_message(text: str, key: str):
    """A user turn as the SDK client sends one, with the uuid Claude Code replays it under."""
    yield {"type": "user", "message": {"role": "user", "content": text}, "parent_tool_use_id": None,
           "session_id": "default", "uuid": key}


@dataclass
class Cycle:
    """The compaction cycle this host drives (#244), as the store holds it (Store.compaction):
    its id, who started it, what follows it, its state and since when (monotonic)."""
    id: str
    origin: str
    then: str | None
    state: str = "requested"
    line: str = "compacting"   # its activity line, kept in front while its turns run (show)
    nonce: str | None = None   # the notes ask's id, which its block carries
    notes: str | None = None   # seen in a reply: /compact's instructions once the notes turn ends
    ask: str | None = None   # the notes ask's turn key
    key: str | None = None   # the /compact turn's key
    since: float = field(default_factory=time.monotonic)
    waited: float | None = None   # when its turn (the notes ask, then /compact) was taken up, or Keep waiting pressed
    question: str | None = None   # the open Keep waiting or Abort question's ref

    def turn(self) -> str | None:
        """The cycle's own turn now: the notes ask, then /compact."""
        return self.key or self.ask


class Host:
    """One session's host. `client_factory` builds the SDK client from options (tests pass
    a fake); `open_tab` opens the shell tab (tests pass a stub)."""

    def __init__(self, store: Store, sid: str, client_factory=None, open_tab=None, person: str | None = None):
        self.store, self.sid = store, sid
        self.client_factory = client_factory or default_client
        self.open_tab = open_tab
        self.person = person or getpass.getuser()
        self.client = None
        self.waiting: dict[str, asyncio.Future] = {}   # question refs an AskUserQuestion waits on
        self.asking: set[str] = set()   # permission refs a can_use_tool call waits on
        # the person's message on an item, handed to the call waiting on it: still claimed,
        # confirmed once the call takes it, released (so it goes out as a turn) if withdrawn
        self.handed: dict[str, sqlite3.Row] = {}
        self.delivered: set[int] = set()   # passed on, but not yet marked delivered (a locked database)
        # the turns sent and not yet ended, by the uuid each was sent with: Claude Code
        # replays each as it takes it up (replay-user-messages), so a turn's end is matched to
        # the turns it took up, never counted. A turn Claude Code starts itself (a background
        # task finishing) takes up none of ours; a turn sent while one runs may be taken up
        # by that one, and then ends with it. Each has the refs the person's messages were on,
        # with the newest message's id on each, and whether it is a keepalive.
        self.sent: dict[str, Sent] = {}
        self.taken_up: list[str] = []   # the turns sent that the running turn has taken up so far
        # the compaction cycle under way (#244), if any; and a /compact turn whose cycle ended
        # before it did (timed out): its end is still the wheelhouse's own, never "automatic"
        self.cycle: Cycle | None = None
        # a cycle's end its turn couldn't write (a database locked past steady's tries): (state,
        # why, then), written by the next pass, then its then carried out (cycle_due)
        self.owed: tuple[str, str, str | None] | None = None
        self.owed_move: str | None = None   # likewise a move on (the claim), owed to the next pass
        self.late_key: str | None = None
        self.cold_for: float | None = None   # the quiet the quiet mark's compaction was for (cold_due)
        self.park_told: float | None = None   # when the park request went to the session (monotonic)
        self.parked_it = False   # parked after its compaction: Stop & Forget before it stops still wins
        self.parking_after = False   # parked mid-turn: the activity line says it parks once the turn ends
        self.turn_text: list[str] = []   # this turn's text so far, where Compact's block may straddle blocks
        # this turn's text since its last tool call: what it ends by saying, posted as the
        # reply on the refs it answered (the protocol asks the session for the status alone)
        self.final_text: list[str] = []
        # the activity line a keepalive turn found, shown again once it ends: it is no news
        self.kept_line: str | None = None
        self.compacted_from = None   # tokens before the last compaction, until its turn ends
        # whether the last turn to end compacted the context: no keepalive then, as the
        # compaction left no cache worth keeping warm
        self.compacted_last = False
        # the model and effort in force, as Claude Code last confirmed them (or as launched)
        self.applied: tuple[str | None, str | None] = (None, None)
        # keepalive: the context size the last turn ended at (None until one has, so a host
        # just started never pays to warm a cache it can't know is warm), when the last turn
        # ended, and since when the session has been quiet (None while a turn other than a
        # keepalive runs), all monotonic
        self.tokens: int | None = None
        self.keepalive_for = features.keepalive_hours() * 3600   # seconds; 0: no keepalive
        self.last_turn = time.monotonic()
        self.quiet_since: float | None = time.monotonic()
        self.owner = False   # registered as the session's process: only then does it write the activity
        self.unsaid: str | None = None   # an activity line a locked database kept unwritten (say)
        self.stopping = False
        self.stop_reason = ""
        self.mcp_items: dict[str, str] = {}   # the open MCP questions' refs, by ref: the server each is about
        self.mcp_asked: dict[str, tuple] = {}   # each asked-about server's (status, error) when asked
        # servers whose question the person closed, by the (status, error) it was: asked again
        # only once that changes (a new failure), never for the same one
        self.mcp_quiet: dict[str, tuple] = {}
        self.mcp_flows: dict[str, float] = {}   # when each local OAuth flow started (monotonic)
        # servers signed in by a pasted URL and not yet reconnected (the cap cut in): each check retries
        self.mcp_owed: set[str] = set()
        self.mcp_checked = 0.0   # when the MCP servers were last checked (monotonic)
        self.mcp_seen: dict[str, dict] = {}   # each server as the last check saw it, by name
        self.mcp_shown: list | None = None   # the servers as last written to the store (set_mcp_status)
        # each connector question's server: when it was asked (or taken up again), and when the
        # check last reconnected it unasked (mcp_retry), monotonic
        self.mcp_tries: dict[str, list[float]] = {}
        # what the dialog's last press on each server came to: its sign-in link, whether Claude
        # Code waits for the browser, and a note (the error, or what to do next)
        self.mcp_notes: dict[str, dict] = {}
        self.mcp_lock_for: tuple | None = None   # (event loop, lock): check_mcp one at a time

    # options and connection

    def options(self, resume: bool):
        from claude_agent_sdk import ClaudeAgentOptions, HookMatcher
        python = sys.executable
        mcp = {"wheelhouse": {"type": "stdio", "command": python, "args": ["-m", "claude_wheelhouse", "mcp"],
                              "env": {"WHEELHOUSE_SESSION_ID": self.sid, "WHEELHOUSE_DB": str(self.store.path)}}}
        session = self.store.session(self.sid)
        # launched as chosen, every time: a resume would drop a live effort change
        self.applied = (models.field(session, "model"), models.field(session, "effort"))
        return ClaudeAgentOptions(
            cwd=session["cwd"],
            model=self.applied[0],
            effort=self.applied[1],
            cli_path=shutil.which("claude"),   # the user's own Claude Code, not the SDK's bundled copy
            resume=self.sid if resume else None,
            session_id=None if resume else self.sid,
            system_prompt={"type": "preset", "preset": "claude_code", "append": PROTOCOL_SDK},
            setting_sources=["user", "project", "local"],
            mcp_servers=mcp,
            can_use_tool=self.can_use_tool,
            hooks={"PreCompact": [HookMatcher(hooks=[self.pre_compact])]},
            # the snapshot would replay whichever protocol the conversation first ran with
            # (a tab's, or an older version's): render it fresh each request
            # replay-user-messages: each turn sent is replayed, by its uuid, as Claude Code takes
            # it up, so a turn's end is matched to the turns it answered (read)
            extra_args={"system-prompt-snapshot": "off", "replay-user-messages": None},
            env={"WHEELHOUSE_SESSION_ID": self.sid, "WHEELHOUSE_DB": str(self.store.path)},
            stderr=lambda line: print(f"[claude] {line}", file=sys.stderr, flush=True),
        )

    async def connect(self) -> None:
        resume = transcript_exists(self.sid)
        self.client = self.client_factory(self.options(resume))
        # as launched, until Claude Code says: written first, so the pane never shows the
        # last process's (or none) as pending while this one starts
        session = await self.steady(self.store.session, self.sid)
        before = (models.field(session, "applied_model"), models.field(session, "applied_effort"))
        await self.steady(self.store.set_applied, self.sid, *self.applied)
        try:
            await self.client.connect()
        except BaseException:   # nothing launched: the columns as they were
            with contextlib.suppress(sqlite3.OperationalError):
                self.store.set_applied(self.sid, *before)
            raise
        try:
            self.applied = await self.settings()
            self.store.set_applied(self.sid, *self.applied)
        except Exception as e:   # a nicety at start: never worth the session
            print(f"{now()} get_settings at start failed: {e}", file=sys.stderr, flush=True)
        # Claude Code is connected: a locked database mustn't stop it now. The line is only
        # said, so one not written is said again on the next poll (unsaid)
        await self.say("starting")
        if not resume:
            await self.turn(opening_prompt(session, self.store))
        else:
            await self.say("idle")
        await self.check_mcp()

    async def say(self, activity: str) -> None:
        """The activity line, through a locked database (steady); one still not written is
        left for the next poll to write (unsaid), unless the host says another first."""
        self.unsaid = None
        try:
            await self.steady(self.store.set_activity, self.sid, activity)
        except sqlite3.OperationalError as e:
            self.unsaid = activity
            print(f"{now()} {e}: activity {activity!r} left for the next poll", file=sys.stderr, flush=True)

    @property
    def pending(self) -> int:
        """Turns sent and not yet ended."""
        return len(self.sent)

    async def turn(self, text: str, keepalive: bool = False, refs: dict[str, int] | None = None,
                   compacting: str | None = None) -> str:
        """Send a user turn; Claude Code queues it behind any turn still running, or takes
        it up into that one. Only a keepalive leaves the session quiet, and the activity line
        as it was. What it ends by saying is posted as the reply on refs (ref: the id of the
        person's newest message on it); refs given, even empty, marks the turn as the
        person's messages (Sent.person). A compaction's turn (compacting: its activity line)
        writes that line before it goes, so the session never reads as free between the
        command taken and the turn sent, and keeps it in front until the turn ends (show).
        Returns the uuid it was sent with."""
        key = str(uuid.uuid4())
        if keepalive:
            self.kept_line = self.store.session(self.sid)["activity"]
        if compacting:
            try:
                self.store.set_activity(self.sid, compacting)
            except sqlite3.OperationalError:   # written again once sent
                pass
        await self.client.query(user_message(text, key))
        self.sent[key] = Sent(refs or {}, keepalive, refs is not None)
        if keepalive:
            return key
        self.quiet_since = None
        self.unsaid = None   # this turn's line replaces one left unwritten
        try:   # sent: a locked database mustn't make the caller think otherwise
            self.store.set_activity(self.sid, compacting or "thinking")
        except sqlite3.OperationalError:
            pass
        return key

    def compacting(self) -> str | None:
        """The activity line of the compaction cycle whose turn is to come or running (or
        Claude Code's own, running now), if any."""
        c = self.cycle
        return c.line if c and (c.origin == "auto" or c.turn() in self.sent) else None

    def led(self, activity: str) -> str:
        """activity, led by the line of a compaction whose turn is to come or running."""
        line = self.compacting()
        return f"{line} · {activity}" if line and not activity.startswith(line) else activity

    # model and effort, changed live (.plan/model-effort.md)

    async def control(self, request: dict) -> dict:
        """A control request the SDK has no method for, sent through its own control-request
        path; a refusal raises. test_host pins these internals to the SDK version."""
        return await self.client._query._send_control_request(request)

    async def settings(self) -> tuple[str | None, str | None]:
        """The model and effort in force, as Claude Code reports them."""
        applied = (await self.control({"subtype": "get_settings"})).get("applied") or {}
        return applied.get("model"), applied.get("effort")

    async def reconcile(self, session) -> None:
        """The person changed the session's model or effort (its columns): apply each that
        differs from what is in force, set_model for the model, apply_flag_settings for the
        effort (checked first: Claude Code ignores an unknown level silently), then confirm
        with get_settings. What Claude Code refused or confirmed otherwise is put back in the
        columns, which then match what is in force, so nothing is tried again until the
        person changes them; the activity line says why."""
        want = (models.field(session, "model"), models.field(session, "effort"))
        model = want[0] if want[0] and want[0] != self.applied[0] else None
        effort = want[1] if want[1] and want[1] != self.applied[1] else None
        if not (model or effort):
            return
        why = ""
        try:
            if model:
                await self.client.set_model(model)
            if effort:
                await self.control({"subtype": "apply_flag_settings",
                                    "settings": {"effortLevel": models.check_effort(effort)}})
        except Exception as e:
            why = " ".join(str(e).split())[:120] or type(e).__name__
        try:
            applied = await self.settings()
        except Exception as e:   # unconfirmed: taken as asked, unless it was refused
            applied = self.applied if why else (model or self.applied[0], effort or self.applied[1])
            print(f"{now()} get_settings failed: {e}", file=sys.stderr, flush=True)
        asked = {"model": (model, applied[0]), "effort": (effort, applied[1])}
        refused = {k: got for k, (v, got) in asked.items() if v and v != got}
        # written before it is taken as in force: a locked database raises here, and the next
        # poll, still seeing the change, applies and writes it again
        self.store.set_applied(self.sid, *applied)
        if refused:   # only where the column still holds what was sent: not a newer choice
            self.store.put_back(self.sid, {"model": model, "effort": effort}, **refused)
        self.applied = applied
        if refused:
            kept = ", ".join(str(g) for g in applied)
            text = f"{' and '.join(refused)} change refused: {why or f'Claude Code kept {kept}'}"
        else:
            text = f"now {models.name(applied[0]) or applied[0]} at {applied[1]}"
        print(f"{now()} {text}", file=sys.stderr, flush=True)
        if not self.pending:   # the activity line is the turn's while one runs
            with contextlib.suppress(sqlite3.OperationalError):   # applied: only the line is lost
                self.store.set_activity(self.sid, f"idle · {text}")

    # Claude Code's permission check

    async def can_use_tool(self, name: str, inp: dict, ctx):
        """Claude Code waits on this. It cancels the call when it withdraws the request (an
        interrupt, or the host disconnecting for a shell tab): the item then closes, so it
        never shows buttons nothing is waiting on, and a message the person had sent on it
        goes out as a turn instead of into a denial nobody reads."""
        if name == "AskUserQuestion":
            return await self.ask_question(inp)
        from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny
        ref = self.store.post_item(self.sid, "permission", tool_summary(name, inp), permission_body(name, inp, ctx))
        self.store.set_activity(self.sid, self.led(f"waiting on {ref}"))
        self.asking.add(ref)
        try:
            while True:
                item = self.store.item(self.sid, ref)
                if item is None or item["status"] != "open":
                    break
                await asyncio.sleep(POLL_SECONDS / 2)
        except asyncio.CancelledError:
            self.withdrawn(ref, "permission")
            raise
        finally:
            self.asking.discard(ref)
        await self.taken(ref, "permission")
        self.store.set_activity(self.sid, self.led("thinking"))   # its compaction's, if one is under way
        answer = json.loads(item["answer"] or "{}") if item else {}
        if answer.get("decision") in ("allow", "always"):
            keep = list(ctx.suggestions or []) if answer["decision"] == "always" else None
            return PermissionResultAllow(updated_permissions=keep)
        return PermissionResultDeny(message=answer.get("message") or "The person denied this in the wheelhouse.")

    async def ask_question(self, inp: dict):
        from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny
        title, body = question_item(inp)
        ref = self.store.post_item(self.sid, "question", title, body)
        self.store.set_answer(self.sid, ref, HOST_QUESTION)
        self.store.set_activity(self.sid, self.led(f"waiting on {ref}"))
        fut = asyncio.get_running_loop().create_future()
        self.waiting[ref] = fut
        try:
            reply = await fut
        except asyncio.CancelledError:
            self.withdrawn(ref, "question")
            raise
        finally:
            self.waiting.pop(ref, None)
        await self.taken(ref, "question")
        if reply is None:
            return PermissionResultDeny(message=CLOSED_UNANSWERED)
        self.store.reply(self.sid, ref, "Passed to Claude as the answer.", status="answered")
        self.store.set_activity(self.sid, self.led("thinking"))   # its compaction's, if one is under way
        return PermissionResultAllow(updated_input={**inp, "answers": question_answers(inp, reply)})

    async def taken(self, ref: str, kind: str) -> None:
        """The call waiting on this item has the person's message (if one was handed to it):
        now it is delivered. It stays handed until that's recorded: withdrawn while a locked
        database holds the confirm up, the call never returns, so the message is released to
        go out as a turn rather than marked delivered unread."""
        msg = self.handed.get(ref)
        if msg is None:
            return
        try:
            await self.confirm([msg])
        except asyncio.CancelledError:
            self.delivered.discard(msg["id"])
            self.withdrawn(ref, kind)
            raise
        self.handed.pop(ref, None)

    def withdrawn(self, ref: str, kind: str) -> None:
        """Claude Code withdrew the call waiting on this item: close it if still open, and if
        the person's message had been handed to the call, release it, undelivered, so it goes
        out as the next turn (or, during a shell tab, through the tab's monitor). Released or
        not, it is never lost: a claim left behind is retaken after CLAIM_TIMEOUT."""
        msg = self.handed.pop(ref, None)
        if msg is not None:
            try:
                self.store.release([msg])
            except sqlite3.OperationalError:
                pass
        try:
            item = self.store.item(self.sid, ref)
        except sqlite3.OperationalError:
            item = None
        if item is None or item["status"] == "open" or msg is not None:
            self.close_quietly(ref, kind)

    def close_quietly(self, ref: str, kind: str, why: str = CANCELLED) -> None:
        """Close a permission or host question nothing will answer, saying why. Never raises:
        it runs on the way out of a cancelled call, or as the session ends."""
        try:
            if kind == "permission":
                self.store.answer_permission(self.sid, ref, "deny", why)
            else:
                self.store.update_item(self.sid, ref, status="closed", note=why)
        except (KeyError, SessionGone, sqlite3.OperationalError):
            pass

    # the store, polled

    async def confirm(self, msgs) -> None:
        """Mark messages delivered, retrying a locked database. They are remembered until it
        works, so if every try fails, a later claim of the same messages confirms them, never
        resends."""
        ids = {m["id"] for m in msgs}
        self.delivered |= ids
        for wait in (*CONFIRM_TRIES, None):
            try:
                self.store.confirm(msgs)
                self.delivered -= ids
                return
            except sqlite3.OperationalError as e:
                if wait is None:
                    print(f"{now()} {e}: delivery not recorded, will confirm on the next claim",
                          file=sys.stderr, flush=True)
                    return
                await asyncio.sleep(wait)

    async def poll(self) -> bool:
        """One pass: the person's messages, End and Park requests, a host command. False once
        the session has gone or parked, or the host has let go of it, which ends the host."""
        session = self.store.session(self.sid)
        if session is None:
            return False
        if self.ending_wins(session):
            session = self.store.session(self.sid)
        if session["parked"]:
            if not await self.compact_parked(session):
                return False
            await self.deliver(session, hold=True)   # an answer to a call the park's turn waits on still goes
            return True
        if self.parking_after:   # unparked (Park pressed again, Stop & Forget) while its park waited on the turn
            self.parking_after = False
            self.show((session["activity"] or "").replace(f" · {PARKS_AFTER_TURN}", "") or "thinking")
        if self.park_told is not None and time.monotonic() - self.park_told > PARK_COMPACT_WITHIN:
            self.park_told = None   # the park asked for never came: its Compact first goes
            self.store.give_up_park_compact(self.sid)
        if self.unsaid is not None:   # a locked database kept it from the line: written now
            self.store.set_activity(self.sid, self.unsaid)
            self.unsaid = None
        # during a compaction cycle, turns and requests sent before it began are held until it
        # ends (#244): only what a call waiting on the person takes goes
        await self.cycle_due()
        if (self.cycle is None and (within := requested_expired(session, PARK_COMPACT_WITHIN)) is not None
                and self.store.expire_requested(self.sid, session["compaction_id"], within)):
            self.park_told = None   # its park never came: unlocked, the session as it was
            session = self.store.session(self.sid)
        # held whenever the store says a cycle is active, requested included; Park with Compact
        # first's request still goes to the session, as its cycle's first step
        parking = self.cycle is None and compacting_now(session) and session["compaction_origin"] == "park"
        await self.deliver(session, hold=self.cycle is not None or compacting_now(session), tell=parking or None)
        await self.reconcile(session)
        asks = self.store.take_mcp_asks(self.sid)
        if asks or (self.mcp_items and time.monotonic() - self.mcp_checked > MCP_RECHECK):
            if not await self.check_mcp(asks) and asks:
                self.store.give_back_mcp_asks(self.sid, asks)
        if self.keepalive_due(time.monotonic()):
            await self.turn(f"[wheelhouse] {keepalive_body(self.store.in_doubt(self.sid))}", keepalive=True)
        elif self.cold_due(time.monotonic()):
            self.cold_for = self.quiet_since
            if cid := self.store.start_compaction(self.sid, "quiet"):
                hours = f"{self.keepalive_for / 3600:g}"
                self.cycle = Cycle(cid, "quiet", None, line=f"compacting after {hours} quiet hours: collecting notes")
                await self.ask_notes(QUIET_COMPACT.format(hours=hours))
        if not self.pending and self.cycle is None:
            # the session's own request_compact, held until its turn has ended: never sent into one
            self.store.promote_compact_request(self.sid)
        command = self.store.take_command(self.sid)
        if command is not None:   # the person's command first: a request still pending goes
            self.store.drop_compact_request(self.sid)
        if command == "interrupt":
            await self.client.interrupt()
            if self.cycle is None and session["compaction_origin"] == "park" and compacting_now(session):
                self.store.drop_park_request(self.sid)   # Park's Compact first, not yet parked: all of it goes
            self.end_cycle("cancelled", "cancelled by Interrupt")
            self.store.set_interrupted(self.sid)   # durable: the line below is the next turn's to change
            self.store.set_activity(self.sid, "interrupted")
        elif command == "compact":
            notes, then = self.store.compact_plan(self.sid, take=True)
            # the cycle row is the only trigger (Q233): a Compact command with no requested cycle
            # (one that failed or expired, or one written by a daemon on code before PROTOCOL_VERSION
            # 24, which writes no cycle) starts nothing. So after a pull, until the daemon restarts,
            # Compact through an older daemon does nothing on a host on this code
            row = self.store.compaction(self.sid)
            if (self.cycle is None and row is not None and row["state"] == "requested"
                    and row["origin"] != "park"):   # Park's is claimed once parked (compact_parked)
                self.cycle = Cycle(row["id"], row["origin"], then)
                if notes:   # the person's own instructions: the session isn't asked
                    await self.send_compact(notes)
                else:
                    self.cycle.line = "compacting: collecting notes"
                    await self.ask_notes()
        elif command == "shell":
            await self.shell()
        return not self.stopping

    async def deliver(self, session, hold: bool = False, tell: bool | None = None) -> None:
        """The person's messages, as one turn (or to the call waiting on one), and End and
        Park requests. Held (hold): only what a call waiting on the person takes goes; the
        rest is left unclaimed, and no request is told, unless tell (Park's Compact first,
        whose park request is its cycle's first step)."""
        mcp, msgs = self.store.take_mcp_replies(self.sid, self.store.claim(self.sid, mcp="with"))
        self.forget_delivered()
        handed = {m["id"]: ref for ref, m in self.handed.items()}
        again, turn = [m for m in mcp if m["id"] in self.delivered], []
        mcp = [m for m in mcp if m["id"] not in self.delivered]
        for m in msgs:
            ref = m["item_ref"]
            if m["id"] in self.delivered:   # passed on, but never marked
                again.append(m)
            elif m["id"] in handed:   # still with its call, claimed again as stale: keep the new claim
                self.handed[handed[m["id"]]] = m
            elif ref in self.waiting and not self.waiting[ref].done():
                self.handed[ref] = m
                self.waiting[ref].set_result(m["body"])
                self.person_back(m)
            elif ref in self.asking and ref not in self.handed and self.denied_with(ref, m):
                self.handed[ref] = m
            else:
                turn.append(m)
        await self.confirm(again)
        if mcp:
            await self.confirm(mcp)
            await self.mcp_replies(mcp)
        # closed unanswered: after the messages, so a reply sent just before X still counts
        for ref, fut in list(self.waiting.items()):
            item = self.store.item(self.sid, ref)
            if (item is None or item["status"] == "closed") and not fut.done():
                fut.set_result(None)
        if turn and hold:
            self.store.release(turn)
        elif turn:
            refs = {}
            for m in turn:
                if m["item_ref"]:
                    refs[m["item_ref"]] = max(refs.get(m["item_ref"], 0), m["id"])
            text = format_turn(turn, self.person)
            running = [t for t in self.sent.values() if t.person]
            if running:   # may be taken up into the person's turn running, and only the last is answered
                text += "\n\n" + midturn_note([r for t in running for r in t.refs if r not in refs], list(refs))
            try:
                await self.turn(text, refs=refs)
            except BaseException:   # not sent: left for the next host to deliver
                self.store.release(turn)
                raise
            await self.confirm(turn)
            for m in turn:
                self.person_back(m)
        for what in () if (hold if tell is None else not tell) else REQUEST_TEXT:
            asked = session[f"{what}_requested_at"]
            if asked and session[f"{what}_told_at"] != asked and self.store.tell_request(self.sid, what, asked):
                await self.turn(request_text(self.store, what))
                if what == "park":
                    self.park_told = time.monotonic()

    def ending_wins(self, session) -> bool:
        """Stop & Forget pressed while parking compacts (or is about to, or has just parked
        after one, before the host stops), or while a park waits for its turn to end: the park's plan goes,
        and the end request is told once the compaction's turn ends, as the next turn. A parked
        session is unparked for it. Whether the row changed."""
        asked = session["end_requested_at"]
        # parked mid-turn, with Compact first or not, the host waits for that turn: parking too
        parking = (self.cycle is not None and self.cycle.then == "park") or (session["parked"] and (session["park_compact"] or self.parked_it
                                                                          or self.pending))
        if not (asked and session["end_told_at"] != asked and parking):
            return False
        self.parked_it = self.parking_after = False
        if self.cycle is not None and self.cycle.then == "park":
            self.cycle.then = "end"   # its turn ends with no reminder: the end request is the next prompt
        if session["parked"]:
            self.store.set_parked(self.sid, False)   # its Compact first goes with it
        return True

    async def compact_parked(self, session) -> bool:
        """The session has parked. Parked with Compact first (Store.request, or Store.park
        when it parks itself, with its keep text as the instructions), it is compacted
        once its park's turn has ended, and nothing is sent after: the host stops once that
        ends. Whether the host carries on meanwhile."""
        if self.store.take_command(self.sid, only="interrupt"):   # its turn or its compaction: stopped, not compacted
            if self.pending:
                await self.client.interrupt()
            self.end_cycle("cancelled", "cancelled by Interrupt")
            self.store.take_park_compact(self.sid)
            return False
        if self.cycle is not None:   # its compaction under way
            await self.cycle_due()
            return True
        if self.pending:   # the park's own turn still running: its last words and calls still land
            if not self.parking_after:
                self.parking_after = True
                self.show(session["activity"] or "thinking")
            return True
        self.parking_after = False
        if not session["park_compact"]:   # parked without compacting: Park's cycle, if any, is off
            self.store.cancel_park_cycle(self.sid)
            return False
        # only a park this host asked for, lately, or one park_session settled as compacting
        # (park_keep): a request left unanswered, then a park set some other way (by force,
        # or an older session's), never compacts
        keep = session["park_keep"]
        asked = keep is not None or (self.park_told is not None
                                     and time.monotonic() - self.park_told <= PARK_COMPACT_WITHIN)
        if not (self.store.take_park_compact(self.sid) and asked):
            self.store.cancel_park_cycle(self.sid)
            return False
        row = self.store.compaction(self.sid)
        if row is not None and row["state"] == "requested" and row["origin"] == "park":   # started at the press
            cid = row["id"]
        else:   # the session parked itself (park_session)
            cid = self.store.start_compaction(self.sid, "park", then="park")
        if cid is None:   # one under way already (it can't be, parked): nothing more
            return False
        self.cycle = Cycle(cid, "park", "park")
        await self.send_compact(keep or "", "compacting before it stops")
        return True

    def after_compact(self, driven: bool) -> str:
        """The After Compact step (steps.py), without the [wheelhouse] mark: its open items,
        for one the host didn't drive (Claude Code's own, at full context) where the
        conversation before it is, and the handoff inline, as switched."""
        reminder = after_compaction(self.store, self.sid)
        if not driven:
            path = next(Path.home().glob(f".claude/projects/*/{self.sid}.jsonl"), None)
            auto = "This compaction was automatic" + (f": the conversation before it is in {path}." if path else ".")
            reminder = f"{auto}\n\n{reminder}" if reminder else auto
        return steps.message(self.store, "after_compact", mark=False, reminder=reminder,
                             handoff=steps.handoff_field(self.sid))

    def keepalive_due(self, at: float) -> bool:
        """Whether a keepalive turn is due: nothing running, no Compact under way, the last
        turn not a compaction (there is no cache to preserve), the context past KEEPALIVE_TOKENS,
        the last turn KEEPALIVE_AFTER ago, quiet for less than the keepalive limit
        (features.keepalive_hours; 0, never), not parked, and the session waiting on the
        person (waits_on_person) or with an item in doubt (Store.in_doubt)."""
        if not (not self.pending and self.cycle is None and not self.compacted_last
                and self.quiet_since is not None and self.keepalive_for > 0
                and (self.tokens or 0) > KEEPALIVE_TOKENS and at - self.last_turn >= KEEPALIVE_AFTER
                and at - self.quiet_since < self.keepalive_for):
            return False
        session = self.store.session(self.sid)
        return (session is not None and not session["parked"] and not compacting_now(session)   # a cycle: never (#244)
                and (self.waits_on_person() or bool(self.store.in_doubt(self.sid))))

    def person_back(self, m) -> None:
        """A message delivered: one of the person's (not a notice), an answer to a call the notes
        turn waits on, calls off the quiet mark's compaction while it waits for notes, so its
        /compact never lands on a session in use. A Compact the person pressed is theirs, and stays."""
        if "kind" in m.keys() and m["kind"] == "notice":
            return
        c = self.cycle
        if c is not None and c.origin == "quiet" and c.state == "preparing":
            self.end_cycle("cancelled", "cancelled: the person came back")

    def cold_due(self, at: float) -> bool:
        """Whether the keepalive's window has just ended on a session it was keeping warm:
        quiet features.keepalive_hours(), its cache still warm (a turn within the hour), over
        KEEPALIVE_TOKENS, waiting on the person or with an item in doubt. Compacted once then
        (the Before Compact step and the keep ask), rather than left to go cold."""
        if not (not self.pending and self.cycle is None and not self.compacted_last
                and self.quiet_since is not None and self.cold_for != self.quiet_since and self.keepalive_for > 0
                and (self.tokens or 0) > KEEPALIVE_TOKENS and at - self.quiet_since >= self.keepalive_for
                and at - self.last_turn < PARK_COMPACT_WITHIN):
            return False
        session = self.store.session(self.sid)
        return (session is not None and not session["parked"] and not compacting_now(session)   # a cycle: never (#244)
                and (self.waits_on_person() or bool(self.store.in_doubt(self.sid))))

    def waits_on_person(self) -> bool:
        """Whether the session waits on the person (waits_on_person). A permission or
        AskUserQuestion waits inside a running turn, where no keepalive is sent
        (keepalive_due), so in practice it is the questions and decisions the session posts
        itself that bring one."""
        return waits_on_person(self.store, self.sid)

    def forget_delivered(self) -> None:
        """Drop remembered ids someone else has since recorded as delivered (a shell tab's
        monitor): a claim never returns those, so nothing else would."""
        for i in list(self.delivered):
            m = self.store.message(self.sid, i)
            if m is None or m["delivered_at"] is not None:
                self.delivered.discard(i)

    def denied_with(self, ref: str, m) -> bool:
        """A message on a permission item denies the call with its text. False if a button
        answered it meanwhile: the message is then a turn."""
        try:
            self.store.answer_permission(self.sid, ref, "deny", m["body"])
            return True
        except KeyError:
            return False

    # the compaction cycle (#244, .plan/244-compaction-cycle.md)

    async def move(self, state: str) -> None:
        """The cycle to its next active state, here and in the store, retried through a locked
        database (steady): leaving requested is the host's claim, which CLAIM_WITHIN waits for."""
        c = self.cycle
        c.state, c.since = state, time.monotonic()
        try:
            await self.steady(self.store.move_compaction, self.sid, c.id, state)
            self.owed_move = None
        except sqlite3.OperationalError:   # still locked: owed, retried each pass (cycle_due)
            self.owed_move = state

    async def ask_notes(self, why: str = PRESSED_COMPACT) -> None:
        """The Before Compact step and the keep ask, as the notes turn: preparing."""
        c = self.cycle
        c.nonce, c.notes = secrets.token_hex(4), None
        c.ask = await self.turn(compact_ask(self.store, c.nonce, why=why), compacting=c.line)
        await self.move("preparing")

    async def send_compact(self, notes: str, line: str = "compacting") -> None:
        """/compact, with notes as its instructions if any: compacting."""
        c = self.cycle
        self.close_wait("the notes came back: compacting now")
        c.line, c.waited = line, None   # the clock again, from /compact taken up
        c.key = await self.turn(f"/compact {notes}".rstrip(), compacting=line)
        await self.move("compacting")

    def end_cycle(self, state: str, why: str = "") -> None:
        """The cycle ends: completed, failed or cancelled. Its question closes; a /compact turn
        still running is remembered (late_key), so its end is still the wheelhouse's own."""
        c = self.cycle
        if c is None:   # one the host never took (Interrupt pressed before the host saw Compact)
            row = self.store.compaction(self.sid)
            if row is not None and row["state"] == "requested" and state != "completed":
                self.store.move_compaction(self.sid, row["id"], state)
            return
        # the store first: a locked database leaves the cycle held, for the next pass (cycle_due)
        self.store.move_compaction(self.sid, c.id, state)
        self.cycle = None
        if c.key in self.sent:
            self.late_key = c.key
        with contextlib.suppress(sqlite3.OperationalError):   # a question left open closes at the next start
            self.close_wait(why or state, c)

    async def close_cycle(self, state: str, why: str = "", then: str | None = None) -> bool:
        """end_cycle from a turn's end, retried through a locked database; still locked, it is
        owed to the next pass (cycle_due), with what follows. Whether it was written now."""
        try:
            await self.steady(self.end_cycle, state, why)
            return True
        except sqlite3.OperationalError:
            self.owed = (state, why, then)
            return False

    def follow(self, then: str | None) -> None:
        """What follows a completed cycle that its turn's end couldn't carry out."""
        if then == "send":
            self.store.dispatch(self.sid)
        elif then == "park":
            self.store.set_parked(self.sid, True)
            self.parked_it = True

    async def cycle_due(self) -> None:
        """The cycle's clocks, each poll: the notes turn past NOTES_TIMEOUT asks the person to
        keep waiting or abort (never cancelled unasked); /compact past COMPACT_TIMEOUT fails.
        First, an end owed (close_cycle) is written, and its then carried out."""
        if self.owed_move is not None and self.cycle is not None:   # a move (the claim) a lock kept unwritten
            self.store.move_compaction(self.sid, self.cycle.id, self.owed_move)   # still locked: raised, tried next pass
            self.owed_move = None
        if self.owed is not None and self.cycle is not None:
            state, why, then = self.owed
            self.end_cycle(state, why)   # still locked: raised, and the next pass tries again
            self.owed = None
            self.follow(then)
        c, at = self.cycle, time.monotonic()
        if c is None:
            return
        row = self.store.compaction(self.sid)
        if row is None or row["id"] != c.id or row["state"] not in ("requested", "preparing", "compacting"):
            # ended in the store: the person wrote in on the quiet mark's cycle, or the sweep
            self.cycle = None
            self.close_wait("the compaction was called off", c)
            if c.turn() in self.sent:   # its notes turn, still to come or running: stopped, nothing compacted
                await self.client.interrupt()
                if c.key is not None:
                    self.late_key = c.key
            return
        if c.state == "compacting" and c.waited is None and (c.origin == "auto" or list(self.sent) == [c.key]):
            c.waited = at   # running now: Claude Code's own, or /compact with nothing left ahead of it
        if c.state == "compacting" and c.waited is not None and at - c.waited > COMPACT_TIMEOUT:
            self.end_cycle("failed", TIMED_OUT)
            if c.origin != "auto":   # Claude Code's own runs inside a turn whose line is that turn's
                self.store.set_activity(self.sid, NOT_COMPACTED.format(why=TIMED_OUT))
        elif c.state == "preparing" and c.question is not None:
            item = self.store.item(self.sid, c.question)
            if item is None or item["status"] in CLOSED:   # Keep waiting (Abort interrupts too): the clock again
                c.question, c.waited = None, at
        elif c.state == "preparing" and c.waited is not None and at - c.waited > NOTES_TIMEOUT:   # from taken up
            name = self.store.session(self.sid)["name"] or self.sid[:8]
            c.question = self.store.post_marked_question(self.sid, NOTES_WAIT_TITLE.format(name=name),
                                                         NOTES_WAIT_BODY, NOTES_WAIT)

    def close_wait(self, why: str, c: Cycle | None = None) -> None:
        """The cycle's Keep waiting or Abort question, if open, closes: no longer asked."""
        c = c or self.cycle
        if c is not None and c.question is not None:
            item = self.store.item(self.sid, c.question)
            if item is not None and item["status"] not in CLOSED:
                self.close_quietly(c.question, "question", why)
            c.question = None

    async def pre_compact(self, inp, tool_use_id, context) -> dict:
        """Claude Code's PreCompact hook: its own compaction at full context (trigger auto),
        with no cycle under way, is a cycle too, from compacting: blue, closed, until its turn
        ends. The wheelhouse's own /compact (trigger manual) is in its cycle already."""
        try:
            if (inp or {}).get("trigger") == "auto" and self.cycle is None:
                if cid := self.store.start_compaction(self.sid, "auto", "compacting"):
                    self.cycle = Cycle(cid, "auto", None, state="compacting",
                                       line="compacting: Claude Code's own, at full context")
                    self.store.set_activity(self.sid, self.cycle.line)
        except sqlite3.OperationalError as e:   # a locked database: the compaction goes ahead regardless
            print(f"{now()} {e}: auto-compaction not recorded", file=sys.stderr, flush=True)
        return {}

    # Claude Code's messages

    async def read(self) -> None:
        from claude_agent_sdk import AssistantMessage, RateLimitEvent, ResultMessage, SystemMessage, UserMessage
        from claude_agent_sdk.types import TextBlock, ThinkingBlock, ToolUseBlock
        async for m in self.client.receive_messages():
            try:
                if isinstance(m, UserMessage) and m.uuid in self.sent and m.uuid not in self.taken_up:
                    self.taken_up.append(m.uuid)   # one of ours, replayed as Claude Code takes it up
                    if self.cycle is not None and m.uuid in (self.cycle.ask, self.cycle.key):   # its clock starts
                        self.cycle.waited = time.monotonic()
                if isinstance(m, (AssistantMessage, UserMessage)):
                    if not self.pending:   # one it started itself: the session isn't quiet
                        self.quiet_since = None
                if isinstance(m, AssistantMessage):
                    for b in m.content:
                        if isinstance(b, TextBlock):
                            self.turn_text.append(b.text)
                            if m.parent_tool_use_id is None:   # the session's own, not a subagent's
                                self.final_text.append(b.text)
                        c = self.cycle
                        if isinstance(b, TextBlock) and c is not None and c.state == "preparing":
                            notes = keep_notes("\n".join(self.turn_text), c.nonce)   # the turn's text, whole: the last block wins
                            c.notes = notes if notes is not None else c.notes
                        if isinstance(b, ToolUseBlock):
                            if m.parent_tool_use_id is None:   # what it says after this is its answer
                                self.final_text = []
                            self.show(f"running {tool_summary(b.name, b.input or {})}")
                        elif isinstance(b, ThinkingBlock):
                            self.show("thinking")
                        elif isinstance(b, TextBlock):
                            self.show("writing")
                elif isinstance(m, UserMessage):
                    self.show("thinking")
                elif isinstance(m, RateLimitEvent):
                    self.rate_limited(m.rate_limit_info.raw)
                elif isinstance(m, SystemMessage) and m.subtype == "compact_boundary":
                    meta = m.data.get("compact_metadata") or m.data.get("compactMetadata") or {}
                    self.compacted_from = meta.get("pre_tokens") or meta.get("preTokens") or 0
                    if self.cycle is not None and self.cycle.origin == "auto":   # Claude Code's own, mid-turn: done now
                        self.end_cycle("completed")
                elif isinstance(m, ResultMessage):
                    ended = [self.sent.pop(k) for k in self.taken_up if k in self.sent]
                    if not ended and self.sent and getattr(m, "origin", None) is None:
                        # a turn of ours (Claude Code's own carry an origin) that replayed
                        # none of our uuids: Claude Code has stopped replaying them. The
                        # oldest is taken as this one, unanswered, so the host never wedges
                        oldest = next(iter(self.sent))
                        self.sent.pop(oldest)
                        print(f"{now()} a turn ended with none of our turns replayed: taking {oldest} "
                              "as ended (is replay-user-messages still honoured?)", file=sys.stderr, flush=True)
                    self.taken_up = []
                    # its closing text answers the last of the person's turns it took up
                    # (#243): one taken up mid-turn was told to answer the earlier ones with reply
                    said = [t for t in ended if t.person]
                    self.answer(said[-1].refs if said else {}, m)
                    keepalive = bool(ended) and all(t.keepalive for t in ended)
                    self.turn_text, self.final_text = [], []
                    await self.finished(m, keepalive)
            except sqlite3.OperationalError as e:   # database is locked: the activity line can wait
                print(f"{now()} {e}: activity not recorded", file=sys.stderr, flush=True)

    def rate_limited(self, raw) -> None:
        """Claude Code's plan usage, as this session's stream says it: kept in the store,
        where the hub takes it as its newest reading (plan_usage.SDK_SETTING), in SDK mode
        alone (Hub.take_sdk_usage). Kept whatever this process's own mode: a host is started
        without --enable-sdk, in the session's directory, so it never sees the app's .env.
        EXPERIMENTAL (features.py): SDK mode is not for use by anyone other than the author
        until Anthropic's terms are confirmed."""
        limits = plan_usage.from_rate_limit(raw)
        if limits:
            self.store.set_setting(plan_usage.SDK_SETTING, json.dumps({"at": time.time(), "limits": limits}))

    def show(self, activity: str) -> None:
        """What the running turn is doing, on the activity line: not a keepalive's, which
        leaves the line as it found it. Parked mid-turn, it adds that it parks once the turn ends.
        While a compaction's turn is to come or runs, its line goes first (compacting): the
        turns before it and its own are part of the compaction, never the session free (#233)."""
        if not (self.taken_up and all(k in self.sent and self.sent[k].keepalive for k in self.taken_up)):
            activity = self.led(activity)
            if self.parking_after and PARKS_AFTER_TURN not in activity:
                activity = f"{activity} · {PARKS_AFTER_TURN}"
            self.store.set_activity(self.sid, activity)

    def answer(self, refs: dict[str, int], m) -> None:
        """A turn has ended whose last message from the person taken up was on refs: what it
        ended by saying (its text after its last tool call) goes on each as the session's
        reply, once. Messages taken up before that one get none (#243): the note sent with a
        turn that may be taken up mid-turn (midturn_note) asks the session to reply on them.
        Not on a ref the session has replied on itself since the person's message (a
        session copying the reply calls in its history), nor an error's: the session hasn't
        answered, and the item still awaits it."""
        text = "\n\n".join(t.strip() for t in self.final_text if t.strip())
        if m.is_error or not text:
            return
        for ref, after in refs.items():
            try:
                self.store.host_reply(self.sid, ref, text, after=after)
            except SessionGone:
                return

    async def finished(self, m, keepalive: bool = False) -> None:
        """A turn ended. Once a reply has carried Compact's notes, they go to /compact,
        whichever turn they came in: one queued ahead of the ask, an error in another turn
        or a turn Claude Code started itself doesn't lose them. A keepalive's end shows the
        activity line it found again, unless it failed; an interrupted turn ends as
        "interrupted", not as an error. Interrupted is the result's own word (an aborted
        terminal reason), never the host's memory of an Interrupt: one taken while idle
        ends no turn, and a later error is an error."""
        interrupted = m.is_error and str(getattr(m, "terminal_reason", "") or "").startswith("aborted")
        c, ended = self.cycle, None
        if c is not None and c.state == "preparing":
            notes = c.notes
            if notes is None and not m.is_error:
                notes = keep_notes(m.result, c.nonce)
            if notes is not None or (c.ask not in self.sent and not m.is_error):
                # the notes came back, or their turn ended without them: compacted bare, said
                await self.send_compact(notes or "", "compacting" if notes is not None else
                                        "compacting: no notes came back, so without them")
                return
            if c.ask not in self.sent:   # the notes turn ended in an error, or interrupted
                ended = "cancelled" if interrupted else "failed"
                await self.close_cycle(ended, "cancelled by Interrupt" if interrupted else
                                       f"error: {(m.result or m.subtype)[:80]}")
        self.compacted_last = self.compacted_from is not None
        then, driven = None, False
        if c is not None and c.state == "compacting" and (c.origin == "auto" or c.key not in self.sent):
            driven = c.origin != "auto"   # Claude Code's own is "automatic" to the session
            if self.compacted_last:
                then, ended = c.then, "completed"
                if then == "park":   # the park is the cycle's end: parked first, then completed
                    self.store.set_parked(self.sid, True)
                    self.parked_it, then = True, "end"   # nothing more said or sent
                if not await self.close_cycle("completed", then=None if then == "end" else then):
                    then = "end" if then == "end" else None   # its then waits for the end (owed)
            else:
                ended = "cancelled" if interrupted else "failed"
                await self.close_cycle(ended, "cancelled by Interrupt" if interrupted else
                               f"not compacted: {(m.result or m.subtype or 'no compaction')[:80]}")
        elif self.late_key is not None and self.late_key not in self.sent:   # a /compact its cycle gave up on
            driven, self.late_key = True, None
        tokens = await self.context()
        if tokens is not None:
            self.tokens = tokens
            self.nudge(tokens, compacted=self.compacted_last)   # closed first: the reminder never lists it
        if ended == "failed" and not self.compacted_last and not m.is_error:
            failed = NOT_COMPACTED.format(why=(m.result or "no compaction happened")[:80])
        else:
            failed = None
        if self.compacted_last and then == "park":   # nothing more sent: the host stops at its next poll
            self.store.set_parked(self.sid, True)
            self.parked_it = True
        elif self.compacted_last and then != "end":
            if told := self.after_compact(driven):
                # once per compaction: the next turn carries it, or it is one
                self.store.notice(self.sid, told)
            if then == "send":   # with them, in the same one prompt: the next poll claims both
                self.store.dispatch(self.sid)
        if not self.pending:
            await self.check_mcp()
            self.last_turn = time.monotonic()
            if self.quiet_since is None:
                self.quiet_since = self.last_turn
            if keepalive and self.kept_line is not None and (interrupted or not m.is_error):
                activity = self.kept_line
            elif interrupted:
                activity = "interrupted"
            elif m.is_error:
                activity = f"error: {(m.result or m.subtype)[:80]}"
            elif self.compacted_from is not None:
                activity = f"{COMPACTED} {self.compacted_from // 1000}k → {(tokens or 0) // 1000}k tokens"
            elif failed:
                activity = failed
            elif self.cycle is not None:   # its turn still to come: the cycle's line stays
                activity = self.cycle.line
            else:
                activity = "idle"
            self.store.set_activity(self.sid, activity)
        self.compacted_from = None

    async def context(self) -> int | None:
        """Record the context size Claude Code reports, as /context does. Returns the tokens."""
        try:
            usage = await self.client.get_context_usage()
        except Exception:   # a nicety: never worth the session
            return None
        tokens, top = usage.get("totalTokens"), usage.get("maxTokens")
        if isinstance(tokens, int) and isinstance(top, int):
            self.store.set_context(self.sid, tokens, top)
            return tokens
        return None

    def nudge(self, tokens: int, compacted: bool = False) -> None:
        """Past NUDGE_TOKENS, one question item suggesting Compact, for this crossing: the
        store remembers it (COMPACT_QUESTION), so a host started again, or the person leaving
        it, never brings a second. It closes, saying so, once the session is compacted or back
        under NUDGE_TOKENS; only back under does the next crossing ask again, so a compaction
        that leaves it past NUDGE_TOKENS brings no new question until it has been under.
        No SDK mode check here: a host runs only because SDK mode started it, and is never
        told the mode (open_host passes no flag; features.sdk_enabled() here would read only
        the environment and .env, so a mode switched on by --enable-sdk would read as off).
        SDK mode switched off later can't reach a running host; the app hides the question's
        Compact button then (SDK_BUTTONS), and the host goes at its next relaunch."""
        asked = [i for i in self.store.items(self.sid) if i["answer"] == COMPACT_QUESTION]
        under = tokens < NUDGE_TOKENS
        if not (asked or under or compacted):
            self.store.post_compact_question(self.sid, *nudge_item(tokens))
        for item in asked:
            if (under or compacted) and item["status"] not in CLOSED:
                self.store.update_item(self.sid, item["ref"], status="closed",
                                       note=f"{'Compacted: t' if compacted else 'T'}he context is {tokens // 1000}k now.")
            if under:
                self.store.set_answer(self.sid, item["ref"], COMPACT_QUESTION_DONE)

    # MCP servers (#179, .plan/mcp-reauth.md). No failure here ever stops the host: each
    # entry point logs and carries on, as a check that fails is a nicety lost

    def mcp_lock(self) -> asyncio.Lock:
        """check_mcp's lock, for this event loop: the reader (a turn's end) and the poll both
        check, and a check awaits mcp_authenticate between seeing a server and asking."""
        loop = asyncio.get_running_loop()
        if self.mcp_lock_for is None or self.mcp_lock_for[0] is not loop:
            self.mcp_lock_for = (loop, asyncio.Lock())
        return self.mcp_lock_for[1]

    async def check_mcp(self, asks=()) -> bool:
        """Ask Claude Code how its MCP servers are, and keep what it says for the wheelhouse
        (set_mcp_status). A question is asked only when the session asks for one (asks, from
        request_mcp_signin), carrying a sign-in link where signing in may cure it, and closed
        once its server is connected, disabled or gone. Whether it got as far as Claude Code's
        answer and acted on it."""
        try:
            async with self.mcp_lock():
                await self.check_mcp_now(asks)
            return True
        except Exception as e:
            print(f"{now()} checking MCP servers failed: {e}", file=sys.stderr, flush=True)
            return False

    async def check_mcp_now(self, asks=()) -> None:
        self.mcp_checked = time.monotonic()
        self.mcp_dropped()
        await self.mcp_pay_owed()
        await self.mcp_retry()
        for ask in asks:
            if ask.get("do") in ("reconnect", "signin", "callback"):
                await self.mcp_act(ask["do"], ask.get("server") or "", ask.get("url"))
        servers = {s["name"]: s for s in (await self.client.get_mcp_status()).get("mcpServers", [])}
        self.mcp_seen = servers
        asked = {name: ref for ref, name in self.mcp_items.items()}
        for name, server in servers.items():
            status, ref = server.get("status"), asked.get(name)
            if status not in MCP_ASK:
                self.mcp_quiet.pop(name, None)   # well again: its next failure is a new one
                if ref is not None and status in MCP_SETTLED:
                    self.settle_mcp(ref, f"{name} is {status} now.")
        for name, ref in asked.items():
            if name not in servers:
                self.settle_mcp(ref, f"{name} is no longer configured.")
        for ask in asks:
            if ask.get("do") == "ask":   # the session's request_mcp_signin
                await self.mcp_ask(mcp_named(ask.get("server") or "", servers) or "", ask.get("why") or "")
        self.show_mcp(servers, fresh=bool(asks))

    async def mcp_act(self, do: str, name: str, url: str | None) -> None:
        """A press in the MCP servers dialog: Sign in (mcp_authenticate, its link kept for the
        dialog), Reconnect, or the address a local sign-in's browser ended on, then a reconnect.
        What came of it shows in the server's row; it never touches the session's turns."""
        if do == "signin":
            auth = await self.mcp_auth(name) or {}
            link, waits = auth.get("authUrl"), bool(auth.get("callbackExpected"))
            note = ("Claude Code gave no sign-in link: Shell (Alt+H), then /mcp, signs in" if not link else
                    f"Sign in at the link: Claude Code connects it once the browser comes back, within "
                    f"{MCP_FLOW_LIFE // 60} minutes. If the browser can't reach it, paste the address it ends on"
                    if waits else "Authorise it at the link, then press Reconnect")
            self.mcp_notes[name] = {"link": link, "waits": waits, "note": note}
            return
        kept = self.mcp_notes.get(name, {})
        try:
            await asyncio.wait_for(self.mcp_connect(name, url if do == "callback" else None), MCP_WITHIN)
            note = ""
        except TimeoutError:
            note = "Still reconnecting"
        except Exception as e:
            note = f"{'Sign-in' if do == 'callback' else 'Reconnecting'} failed: {e}"
        self.mcp_notes[name] = {**kept, "note": note}

    async def mcp_ask(self, name: str, why: str) -> None:
        """The question the session asked for about a server: only while it needs the person,
        once at a time, and not again for the same state once the person closed it."""
        server = self.mcp_seen.get(name)
        status = (server or {}).get("status")
        state = (status, (server or {}).get("error"))
        if status not in MCP_ASK or name in self.mcp_items.values() or self.mcp_quiet.get(name) == state:
            return
        self.mcp_quiet.pop(name, None)
        kind = (server.get("config") or {}).get("type")
        auth = await self.mcp_auth(name) if status == "needs-auth" or kind in MCP_AUTH_KINDS else None
        ref = self.store.post_marked_question(self.sid, *mcp_item(name, server, auth, why), mcp_question(name))
        self.mcp_items[ref], self.mcp_asked[name] = name, state
        self.mcp_tries[name] = [time.monotonic()] * 2

    async def mcp_retry(self) -> None:
        """A claude.ai connector a question waits on (needs-auth, or failed, as a reconnect may
        leave it), reconnected unasked as MCP_RETRY_*
        allow: Claude Code reads its sign-in afresh only then. A try that fails is logged and
        left for the next; nothing here ever replies."""
        t = time.monotonic()
        for name in list(self.mcp_items.values()):
            server, tries = self.mcp_seen.get(name) or {}, self.mcp_tries.get(name)
            if (tries is None or server.get("status") not in MCP_ASK
                    or (server.get("config") or {}).get("type") != MCP_CONNECTOR):
                continue
            age, since = t - tries[0], t - tries[1]
            if age > MCP_RETRY_STOP or since < (MCP_RECHECK if age < MCP_RETRY_FAST else MCP_RETRY_SLOW):
                continue
            tries[1] = t
            try:
                await asyncio.wait_for(self.client.reconnect_mcp_server(name), MCP_RETRY_WITHIN)
            except Exception as e:
                print(f"{now()} reconnecting {name} unasked failed: {e!r}", file=sys.stderr, flush=True)

    def show_mcp(self, servers: dict[str, dict], fresh: bool = False) -> None:
        """The servers as the wheelhouse shows them (sessions.mcp_status), written when they
        change, or when asked (fresh: the dialog's look, dated now). A press's link and note
        go once the server is connected."""
        for name in list(self.mcp_notes):
            if (servers.get(name) or {}).get("status") in (None, "connected"):
                self.mcp_notes.pop(name)
        shown = [{"name": name, "status": s.get("status"), "kind": (s.get("config") or {}).get("type"),
                  "scope": s.get("scope"), "error": s.get("error"), **self.mcp_notes.get(name, {})}
                 for name, s in servers.items()]
        if fresh or shown != self.mcp_shown:
            self.store.set_mcp_status(self.sid, shown)
            self.mcp_shown = shown

    def mcp_dropped(self) -> None:
        """Questions the person closed: no longer the host's, and their servers left alone
        until they fail anew (mcp_quiet)."""
        for ref, name in list(self.mcp_items.items()):
            item = self.store.item(self.sid, ref)
            if item is None or item["status"] in CLOSED:
                self.mcp_items.pop(ref)
                self.mcp_quiet[name] = self.mcp_asked.pop(name, None)
                self.mcp_owed.discard(name)   # "not now": no reconnect retried behind its back
                self.mcp_tries.pop(name, None)

    async def mcp_auth(self, name: str) -> dict | None:
        """Claude Code's sign-in for a server (mcp_authenticate): a claude.ai connector's
        authorise link, or a local OAuth flow's, with Claude Code waiting on its callback."""
        try:
            auth = await self.control({"subtype": "mcp_authenticate", "serverName": name})
        except Exception as e:
            print(f"{now()} mcp_authenticate {name} failed: {e}", file=sys.stderr, flush=True)
            return None
        if (auth or {}).get("callbackExpected"):
            self.mcp_flows[name] = time.monotonic()
        return auth

    async def mcp_replies(self, msgs) -> None:
        """The person's replies on MCP questions, acted on once per question, however many
        came in one poll (the store has taken a pasted redirect URL's code out of what is
        kept). One on a question the person reopened is taken up again; one on a closed
        question gets a note saying nothing acted on it."""
        by_ref: dict[str, list] = {}
        for m in msgs:
            by_ref.setdefault(m["item_ref"], []).append(m)
        for ref, ms in by_ref.items():
            url = next((u for u in (callback_url(m["body"]) for m in reversed(ms)) if u), None)
            try:
                if ref not in self.mcp_items:
                    self.mcp_adopt(ref)
                if ref in self.mcp_items:
                    await self.mcp_reply(ref, url)
                else:
                    self.store.update_item(self.sid, ref, note=self.mcp_elsewhere(ref) or MCP_CLOSED)
            except Exception as e:
                print(f"{now()} the reply on MCP question {ref} failed: {e}", file=sys.stderr, flush=True)

    def mcp_elsewhere(self, ref: str) -> str | None:
        """For a reply on an MCP question that isn't the host's while another about the same
        server is: where to reply instead."""
        item = self.store.item(self.sid, ref)
        name = mcp_server(item["answer"]) if item else None
        newer = next((r for r, n in self.mcp_items.items() if n == name), None)
        return MCP_ELSEWHERE.format(ref=newer, name=name) if newer else None

    def mcp_adopt(self, ref: str) -> None:
        """An MCP question open again (the person reopened it): the host's once more. Its
        link may be a dead sign-in's, so a reply that doesn't connect brings a fresh one."""
        item = self.store.item(self.sid, ref)
        name = mcp_server(item["answer"]) if item else None
        if name and item["status"] not in CLOSED and name not in self.mcp_items.values():
            self.mcp_items[ref] = name
            self.mcp_quiet.pop(name, None)
            self.mcp_flows[name] = float("-inf")
            self.mcp_tries[name] = [time.monotonic()] * 2

    async def mcp_reply(self, ref: str, url: str | None) -> None:
        """A reply on an MCP question: a pasted redirect URL finishes the sign-in waiting on
        it, then the server is reconnected (Claude Code leaves that to its caller after a
        pasted URL). A fresh check closes the question; otherwise the reply says how it
        stands, with a fresh link if the local sign-in has expired, and it stays open."""
        name = self.mcp_items.get(ref)
        if name is None:
            return
        try:
            await asyncio.wait_for(self.mcp_connect(name, url), MCP_WITHIN)
        except TimeoutError:
            self.store.reply(self.sid, ref, f"Still reconnecting {name}: this closes by itself once it's connected.",
                             status="open")
            return
        except Exception as e:
            self.store.reply(self.sid, ref, f"{'Sign-in' if url else 'Reconnecting'} {name} failed: {e}", status="open")
            return
        await self.check_mcp()
        if ref not in self.mcp_items:
            return
        started = self.mcp_flows.get(name)
        if started is not None and time.monotonic() - started > MCP_FLOW_LIFE and not url:
            auth = await self.mcp_auth(name)
            if (auth or {}).get("authUrl"):
                self.store.update_item(self.sid, ref, body=mcp_item(name, {"status": "needs-auth"}, auth)[1])
                self.store.reply(self.sid, ref, f"The sign-in link had expired. Here's a fresh one:\n\n{auth['authUrl']}",
                                 status="open")
                return
        self.store.reply(self.sid, ref, f"Tried: {name} isn't connected yet. Sign in at the link above if "
                         "you haven't, then reply again.", status="open")

    async def mcp_connect(self, name: str, url: str | None) -> None:
        if url:
            self.mcp_owed.add(name)   # owed a reconnect from here, even if the cap cuts in
            try:
                await self.control({"subtype": "mcp_oauth_callback_url", "serverName": name, "callbackUrl": url})
            except Exception:
                self.mcp_owed.discard(name)
                raise
            self.mcp_flows.pop(name, None)
        await self.client.reconnect_mcp_server(name)
        self.mcp_owed.discard(name)

    async def mcp_pay_owed(self) -> None:
        """The reconnect a pasted URL's sign-in is owed, when the cap cut in before it went."""
        for name in list(self.mcp_owed):
            try:
                await asyncio.wait_for(self.client.reconnect_mcp_server(name), MCP_WITHIN)
                self.mcp_owed.discard(name)
            except Exception as e:   # the sign-in may still be finishing: the next check tries again
                print(f"{now()} the owed reconnect of {name} failed: {e}", file=sys.stderr, flush=True)

    def settle_mcp(self, ref: str, note: str) -> None:
        name = self.mcp_items.pop(ref, None)
        self.mcp_asked.pop(name, None)
        self.mcp_flows.pop(name, None)
        self.mcp_owed.discard(name)
        self.mcp_tries.pop(name, None)
        self.close_quietly(ref, "question", note)

    def close_mcp(self, why: str) -> None:
        """Close every open MCP question: the Claude Code they were about is going."""
        for ref in list(self.mcp_items):
            self.settle_mcp(ref, why)

    # hand-over to an interactive tab

    async def shell(self) -> None:
        """Give the session to an interactive Claude Code tab, wait for the tab to exit, then
        take it back. One process per session: the client is gone before the tab opens.

        Another host taking the session meanwhile (a Restore once the tab has exited) clears
        the shell flag as it starts, and holds the registration: either way this host lets
        go without a word, since the session's activity line is the new owner's."""
        self.store.set_activity(self.sid, SHELL_OPENING)
        if self.pending:
            await self.client.interrupt()
        self.close_mcp(MCP_SHELL)
        await self.disconnect()
        # once the session is let go, a locked database mustn't strand it half handed over
        await self.steady(self.store.unregister, self.sid)
        self.owner = False
        await self.steady(self.store.set_shell, self.sid, "tab")
        try:
            (self.open_tab or default_open_tab)(self.store, self.sid)
        except Exception as e:
            print(f"{now()} shell tab failed: {e}", file=sys.stderr, flush=True)
        await self.steady(self.store.set_activity, self.sid, "in a shell tab")
        while True:   # the tab registers itself while "starting"; dead once it has exited
            await self.shell_wait()
            try:
                session = self.store.session(self.sid)
            except sqlite3.OperationalError:   # locked: look again next time
                continue
            if session is None or session["parked"] or session["shell"] != "tab":
                self.stopping = True
                return
            if liveness.status(session) == "dead":
                break
        if not await self.steady(self.register):   # something else took the session between those two reads
            self.stopping = True
            return
        await self.steady(self.store.set_shell, self.sid, None)
        if not await self.carry_back():
            return
        self.sent, self.taken_up, self.turn_text, self.final_text = {}, [], [], []
        self.end_cycle("cancelled", "handed to a shell tab")
        self.late_key = None
        self.lost_cycle()   # a park told in the tab and never done there: told again here, or failed
        self.tokens = None   # the tab ran turns this host never saw: its cache is the tab's
        self.reader.cancel()
        await self.steady(self.deny_stale)
        await self.connect()
        self.reader = asyncio.create_task(self.read())

    async def carry_back(self) -> bool:
        """A /model or /effort typed in a shell tab, carried to the session's columns before
        the host launches with them (Q56), by the hub's rule (transcript_usage.carry): last write wins,
        all its state in the database, so any host taking the session (this one back from
        its tab, or a Restore's after this one died) carries the same. Retried through a
        locked database while the session wants a host, saying so on its activity line:
        launching first would lose what was typed. A transcript that can't be read carries
        nothing. False if the host is to stop instead: stopping, or the session parked or
        gone meanwhile. A Park or End pressed meanwhile is acted on by poll once the lock clears."""
        try:   # a whole transcript, maybe hundreds of MB: off the event loop
            typed = await asyncio.to_thread(transcript_usage.typed_in, transcript.locate(self.sid))
        except OSError as e:
            print(f"{now()} couldn't read the transcript for a /model or /effort typed: {e}",
                  file=sys.stderr, flush=True)
            return True
        waiting = False
        while not self.stopping:
            try:
                await self.steady(transcript_usage.carry, self.store, self.sid, typed)
                return True
            except sqlite3.OperationalError as e:
                print(f"{now()} {e}: carrying back a /model or /effort typed, retrying", file=sys.stderr, flush=True)
            try:
                session = self.store.session(self.sid)
            except sqlite3.OperationalError:
                session = {"parked": 0}   # unread: still wanted, as far as anyone can tell
            if session is None or session["parked"]:
                self.stopping = True
            elif not waiting:
                waiting = True
                await self.say("waiting on a locked database")
        return False

    async def shell_wait(self) -> None:
        await asyncio.sleep(SHELL_POLL_SECONDS)

    @staticmethod
    async def steady(write, *args):
        """A store write the hand-over can't skip, retried through a locked database."""
        for wait in (*STEADY_TRIES, None):
            try:
                return write(*args)
            except sqlite3.OperationalError:
                if wait is None:
                    raise
                await asyncio.sleep(wait)

    # lifecycle

    def register(self) -> bool:
        pid = os.getpid()
        self.owner = self.store.register_if_free(self.sid, pid, liveness.start_time(pid), liveness.boot_id(),
                                                 liveness.is_alive)
        return self.owner

    async def disconnect(self) -> None:
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None

    def deny_stale(self) -> None:
        """Permissions and questions left open by a Claude Code that has gone can never be
        answered: close them. Questions the session posted itself stay. MCP asks left waiting
        were for that Claude Code too: a pasted sign-in address in one is dropped, not replayed."""
        self.store.take_mcp_asks(self.sid)
        for item in self.store.items(self.sid, include_closed=False):
            if item["status"] != "open" or item["ref"] in self.asking or item["ref"] in self.waiting:
                continue
            if item["kind"] == "permission":
                self.close_quietly(item["ref"], "permission", LOST_ON_RESTART)
            elif item["kind"] == "question" and item["answer"] == HOST_QUESTION:
                self.close_quietly(item["ref"], "question", LOST_ON_RESTART)
            elif item["kind"] == "question" and mcp_server(item["answer"]) and item["ref"] not in self.mcp_items:
                self.close_quietly(item["ref"], "question", MCP_GONE)

    def lost_cycle(self) -> None:
        """At start: a cycle a previous host was driving can't be carried on, as its turns were
        that Claude Code's: it ends as failed, and its question closes. Only one still requested
        with its Compact command waiting is this host's to take."""
        row = self.store.compaction(self.sid)
        s = self.store.session(self.sid)
        parking = row is not None and row["origin"] == "park" and row["state"] == "requested" and s["park_compact"]
        if parking and not s["parked"] and s["park_requested_at"]:
            # Park's Compact first, its request told to a Claude Code now gone: told again, so this
            # host's clock (park_told) runs, under the cycle's own deadline (expire_requested)
            self.store.untell_request(self.sid, "park")
        elif parking and not s["parked"]:
            parking = False   # neither asked nor parked: nothing would drive it
        taken = s["host_command"] == "compact" or parking
        if row is not None and (row["state"] in ("preparing", "compacting")
                                or row["state"] == "requested" and not taken):   # nothing will take it
            self.store.move_compaction(self.sid, row["id"], "failed")
        for item in self.store.items(self.sid, include_closed=False):
            if item["kind"] == "question" and item["answer"] == NOTES_WAIT and item["status"] not in CLOSED:
                self.close_quietly(item["ref"], "question", LOST_ON_RESTART)

    async def run(self) -> None:
        if not self.register():
            sys.exit(f"claude-wheelhouse host: session {self.sid} is already running")
        self.reader = None
        try:
            try:
                self.store.set_shell(self.sid, None)   # a host killed while its shell tab was open left this
                # what was typed in that tab, or any other since the last host
                if not await self.carry_back():
                    return
                self.deny_stale()
                self.lost_cycle()
                await self.connect()
            except sqlite3.DatabaseError as e:   # the store, not Claude Code: say which
                busy = "was busy" if isinstance(e, sqlite3.OperationalError) else "failed"
                self.stop_reason = stopped(f"the wheelhouse database {busy} at start: {e}")
                return
            except Exception as e:   # Claude Code wouldn't start (or resume): say why, not just "dead"
                self.stop_reason = stopped(f"Claude Code didn't start: {e}")
                return
            self.reader = asyncio.create_task(self.read())
            while not self.stopping:
                if self.reader.done():   # Claude Code exited under us
                    exc = self.reader.exception() if not self.reader.cancelled() else None
                    self.stop_reason = stopped(exc) if exc else "stopped"
                    break
                try:
                    if not await self.poll():
                        break
                    self.store.heartbeat(self.sid)
                except sqlite3.OperationalError as e:   # database is locked: try again next pass
                    print(f"{now()} {e}: retrying", file=sys.stderr, flush=True)
                except SessionGone:   # force-ended mid-pass
                    break
                except Exception as e:   # Claude Code gone mid-send, say: stop, saying why
                    self.stop_reason = stopped(e)
                    break
                await asyncio.sleep(POLL_SECONDS)
        finally:
            if self.reader is not None:
                self.reader.cancel()
            with contextlib.suppress(sqlite3.Error, SessionGone):   # never left closed and blue (#244)
                self.end_cycle("failed", "the host stopped")
            self.close_mcp(MCP_STOPPED)
            await self.disconnect()
            try:
                if self.owner and self.store.session(self.sid) is not None:
                    self.store.set_activity(self.sid, self.stop_reason)
            except sqlite3.OperationalError:
                pass


def default_client(options):
    from claude_agent_sdk import ClaudeSDKClient
    return ClaudeSDKClient(options=options)


def default_open_tab(store: Store, sid: str) -> None:
    from .launch import open_tab
    open_tab(store, sid, notice=False)


def main(sid: str) -> None:
    """The `host` subcommand: host a session recorded as an SDK session, and nothing else.
    EXPERIMENTAL (features.py). Only SDK mode records new ones, so with it off this hosts
    only one that existed already: the app's Relaunch of a running host starts it again this
    way (launch.open_host, which passes no --enable-sdk), so that session isn't stranded.
    Restore brings one back the way new sessions run (launch.restore_runner)."""
    store = Store(db_path())
    session = store.session(sid)
    if session is None:
        sys.exit(f"claude-wheelhouse host: no session {sid} (ended?)")
    if runner(session) != "sdk":
        sys.exit(f"claude-wheelhouse host: session {sid} runs in a tab, not in a host")
    os.chdir(session["cwd"])
    host = Host(store, sid)
    loop = asyncio.new_event_loop()
    task = loop.create_task(host.run())
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    try:
        loop.run_until_complete(task)
    except asyncio.CancelledError:
        pass
    finally:
        loop.close()
