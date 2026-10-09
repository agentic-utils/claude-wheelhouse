"""A session's SDK host: runs one Claude Code session through the Agent SDK, detached from
the TUI, and talks to everything else through the store, like the monitor and MCP server.

The host is the session's user. The person's sent messages become turns, End and Park
requests become turns, and Claude Code's permission checks and AskUserQuestion calls
become wheelhouse items that the host waits on. The TUI asks for an interrupt, a hand-over
to a shell tab, or a compaction through sessions.host_command.

The host registers its own pid as the session's process, so liveness, Restore and
relaunch treat it like a tab's Claude. It exits when the session ends or parks, and on a
hand-over to a tab it gives up the session, waits for the tab to exit, then takes it back.
"""

import asyncio
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

from . import liveness
from .launch import PROTOCOL_SDK, opening_prompt, transcript_exists
from .monitor import REQUEST_TEXT
from .store import SessionGone, Store, db_path, now

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
        # turns sent and not yet finished. Claude Code also starts turns by itself (a
        # background task finishing), so a finished turn never takes this below zero.
        self.pending = 0
        # Compact's request: the id its notes come back under, and since when nothing has
        # been running (None while a turn runs: only quiet time counts towards the timeout)
        self.compact_nonce: str | None = None
        self.compact_idle: float | None = None
        self.compact_notes: str | None = None   # seen in a reply, sent as /compact once the turn ends
        self.turn_text: list[str] = []   # this turn's text so far, where Compact's block may straddle blocks
        self.compacted_from = None   # tokens before the last compaction, until its turn ends
        self.owner = False   # registered as the session's process: only then does it write the activity
        self.stopping = False
        self.stop_reason = ""

    # options and connection

    def options(self, resume: bool):
        from claude_agent_sdk import ClaudeAgentOptions
        python = sys.executable
        mcp = {"wheelhouse": {"type": "stdio", "command": python, "args": ["-m", "claude_wheelhouse", "mcp"],
                              "env": {"WHEELHOUSE_SESSION_ID": self.sid, "WHEELHOUSE_DB": str(self.store.path)}}}
        session = self.store.session(self.sid)
        return ClaudeAgentOptions(
            cwd=session["cwd"],
            cli_path=shutil.which("claude"),   # the user's own Claude Code, not the SDK's bundled copy
            resume=self.sid if resume else None,
            session_id=None if resume else self.sid,
            system_prompt={"type": "preset", "preset": "claude_code", "append": PROTOCOL_SDK},
            setting_sources=["user", "project", "local"],
            mcp_servers=mcp,
            can_use_tool=self.can_use_tool,
            # the snapshot would replay whichever protocol the conversation first ran with
            # (a tab's, or an older version's): render it fresh each request
            extra_args={"system-prompt-snapshot": "off"},
            env={"WHEELHOUSE_SESSION_ID": self.sid, "WHEELHOUSE_DB": str(self.store.path)},
            stderr=lambda line: print(f"[claude] {line}", file=sys.stderr, flush=True),
        )

    async def connect(self) -> None:
        resume = transcript_exists(self.sid)
        self.client = self.client_factory(self.options(resume))
        await self.client.connect()
        self.store.set_activity(self.sid, "starting")
        if not resume:
            await self.turn(opening_prompt(self.store.session(self.sid)))
        else:
            self.store.set_activity(self.sid, "idle")

    async def turn(self, text: str) -> None:
        """Send a user turn; Claude Code queues it behind any turn still running."""
        await self.client.query(text)
        self.pending += 1
        self.compact_idle = None
        try:   # sent: a locked database mustn't make the caller think otherwise
            self.store.set_activity(self.sid, "thinking")
        except sqlite3.OperationalError:
            pass

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
        if session is None or session["parked"]:
            return False
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
        if turn:
            try:
                await self.turn(format_turn(turn, self.person))
            except BaseException:   # not sent: left for the next host to deliver
                self.store.release(turn)
                raise
            await self.confirm(turn)
        for what, text in REQUEST_TEXT.items():
            asked = session[f"{what}_requested_at"]
            if asked and session[f"{what}_told_at"] != asked and self.store.tell_request(self.sid, what, asked):
                await self.turn(text)
        if (self.compact_nonce and not self.pending and self.compact_idle is not None
                and time.monotonic() - self.compact_idle > COMPACT_TIMEOUT):   # quiet all that time: nothing to overwrite
            self.end_compact()
            self.store.set_activity(self.sid, NO_NOTES)
        command = self.store.take_command(self.sid)
        if command == "interrupt":
            await self.client.interrupt()
            self.end_compact()
            self.store.set_activity(self.sid, "interrupted")
        elif command == "compact":
            nonce = secrets.token_hex(4)
            await self.turn(COMPACT_ASK.format(nonce=nonce))
            self.compact_nonce, self.compact_notes = nonce, None
            self.store.set_activity(self.sid, "compacting: collecting notes")
        elif command == "shell":
            await self.shell()
        return not self.stopping

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

    # Claude Code's messages

    async def read(self) -> None:
        from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, UserMessage
        from claude_agent_sdk.types import TextBlock, ThinkingBlock, ToolUseBlock
        async for m in self.client.receive_messages():
            try:
                if isinstance(m, (AssistantMessage, UserMessage)):
                    self.compact_idle = None   # a turn is running, maybe one Claude Code started itself
                if isinstance(m, AssistantMessage):
                    for b in m.content:
                        if isinstance(b, TextBlock) and self.compact_nonce:   # the turn's text, whole: the last block wins
                            self.turn_text.append(b.text)
                            notes = keep_notes("\n".join(self.turn_text), self.compact_nonce)
                            self.compact_notes = notes if notes is not None else self.compact_notes
                        if isinstance(b, ToolUseBlock):
                            self.store.set_activity(self.sid, f"running {tool_summary(b.name, b.input or {})}")
                        elif isinstance(b, ThinkingBlock):
                            self.store.set_activity(self.sid, "thinking")
                        elif isinstance(b, TextBlock):
                            self.store.set_activity(self.sid, "writing")
                elif isinstance(m, UserMessage):
                    self.store.set_activity(self.sid, "thinking")
                elif isinstance(m, SystemMessage) and m.subtype == "compact_boundary":
                    meta = m.data.get("compact_metadata") or m.data.get("compactMetadata") or {}
                    self.compacted_from = meta.get("pre_tokens") or meta.get("preTokens") or 0
                elif isinstance(m, ResultMessage):
                    self.pending = max(0, self.pending - 1)
                    self.turn_text = []
                    await self.finished(m)
            except sqlite3.OperationalError as e:   # database is locked: the activity line can wait
                print(f"{now()} {e}: activity not recorded", file=sys.stderr, flush=True)

    async def finished(self, m) -> None:
        """A turn ended. Once a reply has carried Compact's notes, they go to /compact,
        whichever turn they came in: one queued ahead of the ask, an error in another turn
        or a turn Claude Code started itself doesn't lose them."""
        if self.compact_nonce:
            notes = self.compact_notes
            if notes is None and not m.is_error:
                notes = keep_notes(m.result, self.compact_nonce)
            if notes is not None:
                self.end_compact()
                await self.turn(f"/compact {notes}".rstrip())
                self.store.set_activity(self.sid, "compacting")
                return
        tokens = await self.context()
        if not self.pending:
            if self.compact_nonce:   # quiet from here: Compact's wait for its notes counts down
                self.compact_idle = time.monotonic()
            if m.is_error:
                activity = f"error: {(m.result or m.subtype)[:80]}"
            elif self.compacted_from is not None:
                activity = f"idle · compacted {self.compacted_from // 1000}k → {(tokens or 0) // 1000}k tokens"
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
        self.pending, self.turn_text = 0, []
        self.end_compact()
        self.reader.cancel()
        await self.steady(self.deny_stale)
        await self.connect()
        self.reader = asyncio.create_task(self.read())

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
                self.deny_stale()
                await self.connect()
            except sqlite3.OperationalError as e:   # the store, not Claude Code: say which
                self.stop_reason = stopped(f"the wheelhouse database was busy at start: {e}")
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
    store = Store(db_path())
    if store.session(sid) is None:
        sys.exit(f"claude-wheelhouse host: no session {sid} (ended?)")
    os.chdir(store.session(sid)["cwd"])
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
