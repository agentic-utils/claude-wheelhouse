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
from dataclasses import dataclass

from . import features, liveness, models, plan_usage, stats, transcript
from .launch import PROTOCOL_SDK, after_compaction, opening_prompt, transcript_exists
from .monitor import REQUEST_TEXT
from .store import CLOSED, COMPACT_QUESTION, COMPACT_QUESTION_DONE, SessionGone, Store, db_path, now, runner

POLL_SECONDS = 1.0
SHELL_POLL_SECONDS = 2.0
CONFIRM_TRIES = (0.1, 0.5, 2.0)   # waits between tries to mark sent messages delivered
STEADY_TRIES = (0.1, 0.5, 2.0, 5.0)   # waits between tries of a write the shell hand-over can't skip
COMPACT_TIMEOUT = 600   # seconds Compact waits for its notes once nothing is running
# asked of a session when the person presses Compact; the notes in its reply, inside the
# block carrying this request's id, become /compact's instructions
COMPACT_ASK = ("[wheelhouse] The person pressed Compact in the wheelhouse. If your own instructions say to "
               "do anything before compaction (save a transcript or handoff note, say), do it now. Then "
               "reply with the notes you want carried through compaction (open threads, decisions, file "
               "paths, refs and next steps, as a terse list), with the line <keep id=\"{nonce}\"> before "
               "them and the line </keep> after them, each tag on a line of its own.")
CLOSED_UNANSWERED = "The person closed this in the wheelhouse without answering."
LOST_ON_RESTART = "The wheelhouse host restarted while this was waiting: Claude Code will ask again if it still needs it."
# Claude Code withdrew the call it was asking about (an interrupt, or the host letting go for a shell tab)
CANCELLED = "cancelled: Claude Code withdrew this (interrupted, or handed to a shell tab), so nothing is waiting on it"
HOST_QUESTION = '{"from": "AskUserQuestion"}'   # an item's answer column marks a question the host asked
NO_NOTES = (f"idle · Compact got no notes back in {COMPACT_TIMEOUT // 60} quiet minutes, "
            "so nothing was compacted")
AWAITING_NOTES = "idle · Compact is waiting for the session's notes (Interrupt cancels it)"
HOLD_EXPIRED = (f"compacting for {COMPACT_TIMEOUT // 60}+ minutes: it won't park after all, and your "
                "messages are no longer held")
COMPACTED = "idle · compacted"   # how the activity line starts once a compaction's turn has ended
# A context past this many tokens gets one question item suggesting Compact, per crossing
# (COMPACT_QUESTION): every turn re-reads the whole context from the prompt cache
NUDGE_TOKENS = 200_000
# how soon after the park request is told the session must park for its Compact first to
# hold: the prompt cache's lifetime, after which compacting saves nothing
PARK_COMPACT_WITHIN = 3600
# Keepalive: the prompt cache lives an hour (1 h cache writes) and a cold one is written
# again whole. A session over KEEPALIVE_TOKENS, quiet and waiting on the person or with an
# item in doubt (Store.in_doubt), gets a tiny turn KEEPALIVE_AFTER seconds after its last, to
# keep it warm, until it has been quiet features.keepalive_hours(): an abandoned session
# doesn't pay for ever. With an item in doubt the turn is the check (CHECK_BODY), which asks
# the session about it. The hub keeps tab sessions warm the same way (Hub.keep_warm)
KEEPALIVE_TOKENS = 150_000
KEEPALIVE_AFTER = 55 * 60
KEEPALIVE_BODY = ("Keepalive: the person is away, and this turn only keeps your prompt cache "
                  "warm. Reply with the one word ok and do nothing else: no tools, no wheelhouse calls.")
KEEPALIVE_TEXT = f"[wheelhouse] {KEEPALIVE_BODY}"
CHECK_BODY = ("Check: {items}. Correct any that's finished with update_item or reply, then reply ok. "
              "Leave alone any you don't recognise.")


def waits_on_person(store: Store, sid: str) -> bool:
    """Whether the session waits on the person: an open question, an unseen decision or an
    open permission. The host's keepalive and the hub's (tab sessions) both ask. The host's
    own question suggesting Compact is the wheelhouse's, not the session waiting."""
    return any((i["kind"] == "question" and i["status"] == "open" and i["answer"] != COMPACT_QUESTION)
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
                 "answer box. Anything typed in the box goes with a denial, as what to do instead.")
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
    """A turn sent: the refs it answers (ref: the person's newest message on it), and
    whether it is a keepalive."""
    refs: dict[str, int]
    keepalive: bool = False


async def user_message(text: str, key: str):
    """A user turn as the SDK client sends one, with the uuid Claude Code replays it under."""
    yield {"type": "user", "message": {"role": "user", "content": text}, "parent_tool_use_id": None,
           "session_id": "default", "uuid": key}


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
        # Compact's request: the id its notes come back under, and since when nothing has
        # been running (None while a turn runs: only quiet time counts towards the timeout)
        self.compact_nonce: str | None = None
        self.compact_idle: float | None = None
        self.compact_notes: str | None = None   # seen in a reply, sent as /compact once the turn ends
        # what follows the wheelhouse's compaction (store.THEN), once its /compact turn (sent
        # with compact_key) ends; None: as after any compaction. park: the person's messages
        # and requests are held meanwhile
        self.compact_then: str | None = None
        self.compact_key: str | None = None
        self.park_told: float | None = None   # when the park request went to the session (monotonic)
        self.parked_it = False   # parked after its compaction: Stop & Forget before it stops still wins
        self.compact_sent = 0.0   # when the /compact turn went (monotonic): a hold lasts COMPACT_TIMEOUT at most
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

    # options and connection

    def options(self, resume: bool):
        from claude_agent_sdk import ClaudeAgentOptions
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
            await self.turn(opening_prompt(session))
        else:
            await self.say("idle")

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

    async def turn(self, text: str, keepalive: bool = False, refs: dict[str, int] | None = None) -> str:
        """Send a user turn; Claude Code queues it behind any turn still running, or takes
        it up into that one. Only a keepalive leaves the session quiet, and the activity line
        as it was. What it ends by saying is posted as the reply on refs (ref: the id of the
        person's newest message on it). Returns the uuid it was sent with."""
        key = str(uuid.uuid4())
        if keepalive:
            self.kept_line = self.store.session(self.sid)["activity"]
        await self.client.query(user_message(text, key))
        self.sent[key] = Sent(refs or {}, keepalive)
        self.compact_idle = None
        if keepalive:
            return key
        self.quiet_since = None
        self.unsaid = None   # this turn's line replaces one left unwritten
        try:   # sent: a locked database mustn't make the caller think otherwise
            self.store.set_activity(self.sid, "thinking")
        except sqlite3.OperationalError:
            pass
        return key

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
        self.store.set_activity(self.sid, f"waiting on {ref}")
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
        self.store.set_activity(self.sid, "thinking")
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
        self.store.set_activity(self.sid, f"waiting on {ref}")
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
        self.store.set_activity(self.sid, "thinking")
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
        self.hold_expired(session)
        if session["parked"]:
            if not await self.compact_parked(session):
                return False
            await self.deliver(session, hold=True)   # an answer to a call the park's turn waits on still goes
            return True
        if self.park_told is not None and time.monotonic() - self.park_told > PARK_COMPACT_WITHIN:
            self.park_told = None   # the park asked for never came: its Compact first goes
            self.store.take_park_compact(self.sid)
        if self.unsaid is not None:   # a locked database kept it from the line: written now
            self.store.set_activity(self.sid, self.unsaid)
            self.unsaid = None
        # while parking compacts, new turns and requests are held: queued for when it's resumed
        await self.deliver(session, hold=self.compact_then == "park")
        if (self.compact_nonce and not self.pending and self.compact_idle is not None
                and time.monotonic() - self.compact_idle > COMPACT_TIMEOUT):   # quiet all that time: nothing to overwrite
            self.end_compact()
            self.drop_plan()
            self.store.set_activity(self.sid, NO_NOTES)
        await self.reconcile(session)
        if self.keepalive_due(time.monotonic()):
            await self.turn(f"[wheelhouse] {keepalive_body(self.store.in_doubt(self.sid))}", keepalive=True)
        command = self.store.take_command(self.sid)
        if command == "interrupt":
            await self.client.interrupt()
            self.end_compact()
            self.drop_plan()
            self.store.set_interrupted(self.sid)   # durable: the line below is the next turn's to change
            self.store.set_activity(self.sid, "interrupted")
        elif command == "compact":
            notes, self.compact_then = self.store.compact_plan(self.sid, take=True)
            self.compact_key = None
            if notes:   # the person's own instructions: the session isn't asked
                self.end_compact()
                self.compact_key = await self.turn(f"/compact {notes}")
                self.compact_sent = time.monotonic()
                self.store.set_activity(self.sid, "compacting")
            else:
                nonce = secrets.token_hex(4)
                await self.turn(COMPACT_ASK.format(nonce=nonce))
                self.compact_nonce, self.compact_notes = nonce, None
                self.store.set_activity(self.sid, "compacting: collecting notes")
        elif command == "shell":
            await self.shell()
        return not self.stopping

    async def deliver(self, session, hold: bool = False) -> None:
        """The person's messages, as one turn (or to the call waiting on one), and End and
        Park requests. Held (hold): only what a call waiting on the person takes goes; the
        rest is left unclaimed, and no request is told."""
        msgs = self.store.claim(self.sid)
        self.forget_delivered()
        handed = {m["id"]: ref for ref, m in self.handed.items()}
        again, turn = [], []
        for m in msgs:
            ref = m["item_ref"]
            if m["id"] in self.delivered:   # passed on, but never marked
                again.append(m)
            elif m["id"] in handed:   # still with its call, claimed again as stale: keep the new claim
                self.handed[handed[m["id"]]] = m
            elif ref in self.waiting and not self.waiting[ref].done():
                self.handed[ref] = m
                self.waiting[ref].set_result(m["body"])
            elif ref in self.asking and ref not in self.handed and self.denied_with(ref, m):
                self.handed[ref] = m
            else:
                turn.append(m)
        await self.confirm(again)
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
            try:
                await self.turn(format_turn(turn, self.person), refs=refs)
            except BaseException:   # not sent: left for the next host to deliver
                self.store.release(turn)
                raise
            await self.confirm(turn)
        for what, text in () if hold else REQUEST_TEXT.items():
            asked = session[f"{what}_requested_at"]
            if asked and session[f"{what}_told_at"] != asked and self.store.tell_request(self.sid, what, asked):
                await self.turn(text)
                if what == "park":
                    self.park_told = time.monotonic()

    def ending_wins(self, session) -> bool:
        """Stop & Forget pressed while parking compacts (or is about to, or has just parked
        after one, before the host stops): the park's plan goes,
        and the end request is told once the compaction's turn ends, as the next turn. A parked
        session is unparked for it. Whether the row changed."""
        asked = session["end_requested_at"]
        parking = self.compact_then == "park" or (session["parked"] and (session["park_compact"] or self.parked_it))
        if not (asked and session["end_told_at"] != asked and parking):
            return False
        self.parked_it = False
        if self.compact_then == "park":
            self.compact_then = "end"   # its turn ends with no reminder: the end request is the next prompt
        if session["parked"]:
            self.store.set_parked(self.sid, False)   # its Compact first goes with it
        return True

    def hold_expired(self, session) -> None:
        """A /compact turn that parks and never ends: after COMPACT_TIMEOUT its hold on the
        person's messages and requests goes, said on the activity line. Not once parked
        (Park's Compact first): the session has parked already, and the host waits it out."""
        if (self.compact_then == "park" and not session["parked"] and self.compact_key in self.sent
                and time.monotonic() - self.compact_sent > COMPACT_TIMEOUT):
            self.compact_then = None
            self.store.set_activity(self.sid, HOLD_EXPIRED)

    async def compact_parked(self, session) -> bool:
        """The session has parked. Parked with Compact first (Store.request), it is compacted
        once its park's turn has ended, and nothing is sent after: the host stops once that
        ends. Whether the host carries on meanwhile."""
        if self.store.take_command(self.sid, only="interrupt"):   # its turn or its compaction: stopped, not compacted
            if self.pending:
                await self.client.interrupt()
            self.drop_plan()
            self.store.take_park_compact(self.sid)
            return False
        if self.compact_then == "park":   # its compaction under way
            return True
        if not session["park_compact"]:
            return False
        if self.pending:   # the park's own turn still running
            return True
        # only a park this host asked for, lately: a request left unanswered, then a park of
        # the session's own (or after a restart), never compacts
        asked = self.park_told is not None and time.monotonic() - self.park_told <= PARK_COMPACT_WITHIN
        if not (self.store.take_park_compact(self.sid) and asked):
            return False
        self.compact_then = "park"
        self.compact_key = await self.turn("/compact")
        self.compact_sent = time.monotonic()
        self.store.set_activity(self.sid, "compacting before it stops")
        return True

    def keepalive_due(self, at: float) -> bool:
        """Whether a keepalive turn is due: nothing running, no Compact under way, the last
        turn not a compaction (there is no cache to preserve), the context past KEEPALIVE_TOKENS,
        the last turn KEEPALIVE_AFTER ago, quiet for less than the keepalive limit
        (features.keepalive_hours; 0, never), not parked, and the session waiting on the
        person (waits_on_person) or with an item in doubt (Store.in_doubt)."""
        if not (not self.pending and self.compact_nonce is None and self.compact_then != "park" and not self.compacted_last
                and self.quiet_since is not None and self.keepalive_for > 0
                and (self.tokens or 0) > KEEPALIVE_TOKENS and at - self.last_turn >= KEEPALIVE_AFTER
                and at - self.quiet_since < self.keepalive_for):
            return False
        session = self.store.session(self.sid)
        return (session is not None and not session["parked"]
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

    def end_compact(self) -> None:
        self.compact_nonce, self.compact_notes, self.compact_idle = None, None, None

    def drop_plan(self) -> None:
        """The wheelhouse's Compact is over without its /compact turn ending: nothing follows."""
        self.compact_then, self.compact_key = None, None

    # Claude Code's messages

    async def read(self) -> None:
        from claude_agent_sdk import AssistantMessage, RateLimitEvent, ResultMessage, SystemMessage, UserMessage
        from claude_agent_sdk.types import TextBlock, ThinkingBlock, ToolUseBlock
        async for m in self.client.receive_messages():
            try:
                if isinstance(m, UserMessage) and m.uuid in self.sent and m.uuid not in self.taken_up:
                    self.taken_up.append(m.uuid)   # one of ours, replayed as Claude Code takes it up
                if isinstance(m, (AssistantMessage, UserMessage)):
                    self.compact_idle = None   # a turn is running, maybe one Claude Code started itself
                    if not self.pending:   # one it started itself: the session isn't quiet
                        self.quiet_since = None
                if isinstance(m, AssistantMessage):
                    for b in m.content:
                        if isinstance(b, TextBlock):
                            self.turn_text.append(b.text)
                            if m.parent_tool_use_id is None:   # the session's own, not a subagent's
                                self.final_text.append(b.text)
                        if isinstance(b, TextBlock) and self.compact_nonce:   # the turn's text, whole: the last block wins
                            notes = keep_notes("\n".join(self.turn_text), self.compact_nonce)
                            self.compact_notes = notes if notes is not None else self.compact_notes
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
                    refs = {}
                    for turn in ended:
                        for ref, after in turn.refs.items():
                            refs[ref] = max(refs.get(ref, 0), after)
                    self.answer(refs, m)
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
        leaves the line as it found it."""
        if not (self.taken_up and all(k in self.sent and self.sent[k].keepalive for k in self.taken_up)):
            self.store.set_activity(self.sid, activity)

    def answer(self, refs: dict[str, int], m) -> None:
        """A turn that took up the person's messages on refs has ended: what it ended by
        saying (its text after its last tool call) goes on each as the session's reply, once.
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
        if self.compact_nonce:
            notes = self.compact_notes
            if notes is None and not m.is_error:
                notes = keep_notes(m.result, self.compact_nonce)
            if notes is not None:
                self.end_compact()
                self.compact_key = await self.turn(f"/compact {notes}".rstrip())
                self.compact_sent = time.monotonic()
                self.store.set_activity(self.sid, "compacting")
                return
        self.compacted_last = self.compacted_from is not None
        then = None
        if self.compact_key is not None and self.compact_key not in self.sent:   # the wheelhouse's /compact turn has ended
            then = self.compact_then
            self.drop_plan()
        tokens = await self.context()
        if tokens is not None:
            self.tokens = tokens
            self.nudge(tokens, compacted=self.compacted_last)   # closed first: the reminder never lists it
        if self.compacted_last and then == "park":   # nothing more sent: the host stops at its next poll
            self.store.set_parked(self.sid, True)
            self.parked_it = True
        elif self.compacted_last and then != "end":
            if reminder := after_compaction(self.store, self.sid):
                # once per compaction, its open items: the next turn carries them, or it is one
                self.store.notice(self.sid, reminder)
            if then == "send":   # with them, in the same one prompt: the next poll claims both
                self.store.dispatch(self.sid)
        if not self.pending:
            self.last_turn = time.monotonic()
            if self.quiet_since is None:
                self.quiet_since = self.last_turn
            if self.compact_nonce:   # quiet from here: Compact's wait for its notes counts down
                self.compact_idle = time.monotonic()
            if keepalive and self.kept_line is not None and (interrupted or not m.is_error):
                activity = self.kept_line
            elif interrupted:
                activity = "interrupted"
            elif m.is_error:
                activity = f"error: {(m.result or m.subtype)[:80]}"
            elif self.compacted_from is not None:
                activity = f"{COMPACTED} {self.compacted_from // 1000}k → {(tokens or 0) // 1000}k tokens"
            elif self.compact_nonce:
                activity = AWAITING_NOTES
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
        that leaves it past NUDGE_TOKENS brings no new question until it has been under."""
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

    # hand-over to an interactive tab

    async def shell(self) -> None:
        """Give the session to an interactive Claude Code tab, wait for the tab to exit, then
        take it back. One process per session: the client is gone before the tab opens.

        Another host taking the session meanwhile (a Restore once the tab has exited) clears
        the shell flag as it starts, and holds the registration: either way this host lets
        go without a word, since the session's activity line is the new owner's."""
        self.store.set_activity(self.sid, "opening a shell tab")
        if self.pending:
            await self.client.interrupt()
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
        self.end_compact()
        self.drop_plan()
        self.tokens = None   # the tab ran turns this host never saw: its cache is the tab's
        self.reader.cancel()
        await self.steady(self.deny_stale)
        await self.connect()
        self.reader = asyncio.create_task(self.read())

    async def carry_back(self) -> bool:
        """A /model or /effort typed in a shell tab, carried to the session's columns before
        the host launches with them (Q56), by the hub's rule (stats.carry): last write wins,
        all its state in the database, so any host taking the session (this one back from
        its tab, or a Restore's after this one died) carries the same. Retried through a
        locked database while the session wants a host, saying so on its activity line:
        launching first would lose what was typed. A transcript that can't be read carries
        nothing. False if the host is to stop instead: stopping, or the session parked or
        gone meanwhile. A Park or End pressed meanwhile is acted on by poll once the lock clears."""
        try:   # a whole transcript, maybe hundreds of MB: off the event loop
            typed = await asyncio.to_thread(stats.typed_in, transcript.locate(self.sid))
        except OSError as e:
            print(f"{now()} couldn't read the transcript for a /model or /effort typed: {e}",
                  file=sys.stderr, flush=True)
            return True
        waiting = False
        while not self.stopping:
            try:
                await self.steady(stats.carry, self.store, self.sid, typed)
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
        answered: close them. Questions the session posted itself stay."""
        for item in self.store.items(self.sid, include_closed=False):
            if item["status"] != "open" or item["ref"] in self.asking or item["ref"] in self.waiting:
                continue
            if item["kind"] == "permission":
                self.close_quietly(item["ref"], "permission", LOST_ON_RESTART)
            elif item["kind"] == "question" and item["answer"] == HOST_QUESTION:
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
