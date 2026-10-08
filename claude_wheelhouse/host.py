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
import shutil
import signal
import sqlite3
import sys

from . import liveness
from .launch import PROTOCOL_SDK, opening_prompt, transcript_exists
from .monitor import REQUEST_TEXT
from .store import Store, db_path, now

POLL_SECONDS = 1.0
SHELL_POLL_SECONDS = 2.0
# Claude Code asks this of a session when the person presses Compact; the session's answer
# becomes /compact's instructions
COMPACT_ASK = ("[wheelhouse] The person pressed Compact in the wheelhouse. If your own instructions say to "
               "do anything before compaction (save a transcript or handoff note, say), do it now. Then "
               "reply with only the notes you want carried through compaction: open threads, decisions, "
               "file paths, refs and next steps, as a terse list. Nothing else in the reply.")
CLOSED_UNANSWERED = "The person closed this in the wheelhouse without answering."
LOST_ON_RESTART = "The wheelhouse host restarted while this was waiting: Claude Code will ask again if it still needs it."


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
    lines.append("Allow, Allow always (keep the rule Claude Code suggests) or Deny. "
                 "A message on this item denies the call and tells Claude what to do instead.")
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
    several take `n: answer` parts where given, else the whole reply each."""
    qs = inp.get("questions") or []
    if len(qs) <= 1:
        return {q.get("question", ""): reply.strip() for q in qs}
    parts = {}
    for chunk in reply.replace("\n", ",").split(","):
        n, sep, text = chunk.partition(":")
        if sep and n.strip().isdigit():
            parts[int(n.strip())] = text.strip()
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
        self.sent = 0       # turns sent to the client
        self.results = 0    # turns it has finished
        self.compact_turn = None   # the turn whose reply holds the notes to compact with
        self.compacted_from = None   # tokens before the last compaction, until its turn ends
        self.stopping = False

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

    async def turn(self, text: str) -> int:
        """Send a user turn; Claude Code queues it behind any turn still running."""
        await self.client.query(text)
        self.sent += 1
        if self.results < self.sent:
            self.store.set_activity(self.sid, "thinking")
        return self.sent

    # Claude Code's permission check

    async def can_use_tool(self, name: str, inp: dict, ctx):
        from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny
        if name == "AskUserQuestion":
            title, body = question_item(inp)
            ref = self.store.post_item(self.sid, "question", title, body)
            self.store.set_activity(self.sid, f"waiting on {ref}")
            fut = asyncio.get_running_loop().create_future()
            self.waiting[ref] = fut
            try:
                reply = await fut
            finally:
                self.waiting.pop(ref, None)
            if reply is None:
                return PermissionResultDeny(message=CLOSED_UNANSWERED)
            self.store.reply(self.sid, ref, "Passed to Claude as the answer.", status="answered")
            self.store.set_activity(self.sid, "thinking")
            return PermissionResultAllow(updated_input={**inp, "answers": question_answers(inp, reply)})
        ref = self.store.post_item(self.sid, "permission", tool_summary(name, inp), permission_body(name, inp, ctx))
        self.store.set_activity(self.sid, f"waiting on {ref}")
        while True:
            item = self.store.item(self.sid, ref)
            if item is None or item["status"] != "open":
                break
            await asyncio.sleep(POLL_SECONDS / 2)
        self.store.set_activity(self.sid, "thinking")
        answer = json.loads(item["answer"] or "{}") if item else {}
        if answer.get("decision") in ("allow", "always"):
            keep = list(ctx.suggestions or []) if answer["decision"] == "always" else None
            return PermissionResultAllow(updated_permissions=keep)
        return PermissionResultDeny(message=answer.get("message") or "The person denied this in the wheelhouse.")

    # the store, polled

    async def poll(self) -> bool:
        """One pass: the person's messages, End and Park requests, a host command. False once
        the session has gone or parked, which ends the host."""
        session = self.store.session(self.sid)
        if session is None or session["parked"]:
            return False
        for ref, fut in list(self.waiting.items()):   # an AskUserQuestion closed unanswered
            item = self.store.item(self.sid, ref)
            if (item is None or item["status"] == "closed") and not fut.done():
                fut.set_result(None)
        msgs = self.store.claim(self.sid)
        turn = []
        for m in msgs:
            ref = m["item_ref"]
            item = ref and self.store.item(self.sid, ref)
            if ref in self.waiting and not self.waiting[ref].done():
                self.waiting[ref].set_result(m["body"])
            elif item and item["kind"] == "permission" and item["status"] == "open":
                self.store.answer_permission(self.sid, ref, "deny", m["body"])
            else:
                turn.append(m)
        self.store.confirm(msgs)
        if turn:
            await self.turn(format_turn(turn, self.person))
        for what, text in REQUEST_TEXT.items():
            asked = session[f"{what}_requested_at"]
            if asked and session[f"{what}_told_at"] != asked and self.store.tell_request(self.sid, what, asked):
                await self.turn(text)
        command = self.store.take_command(self.sid)
        if command == "interrupt":
            await self.client.interrupt()
            self.store.set_activity(self.sid, "interrupted")
        elif command == "compact":
            self.compact_turn = await self.turn(COMPACT_ASK)
            self.store.set_activity(self.sid, "compacting: collecting notes")
        elif command == "shell":
            await self.shell()
        return True

    # Claude Code's messages

    async def read(self) -> None:
        from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, UserMessage
        from claude_agent_sdk.types import TextBlock, ThinkingBlock, ToolUseBlock
        async for m in self.client.receive_messages():
            if isinstance(m, AssistantMessage):
                for b in m.content:
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
                self.results += 1
                await self.finished(m)

    async def finished(self, m) -> None:
        """A turn ended. The compact notes turn hands its reply to /compact."""
        if self.compact_turn is not None and self.results >= self.compact_turn:
            self.compact_turn = None
            notes = (m.result or "").strip() if not m.is_error else ""
            await self.turn(f"/compact {notes}".rstrip())
            self.store.set_activity(self.sid, "compacting")
            return
        tokens = await self.context()
        if self.results >= self.sent:
            if m.is_error:
                activity = f"error: {(m.result or m.subtype)[:80]}"
            elif self.compacted_from is not None:
                activity = f"idle · compacted {self.compacted_from // 1000}k → {(tokens or 0) // 1000}k tokens"
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
        take it back. One process per session: the client is gone before the tab opens."""
        self.store.set_activity(self.sid, "opening a shell tab")
        if self.results < self.sent:
            await self.client.interrupt()
        await self.disconnect()
        self.store.unregister(self.sid)
        self.store.set_shell(self.sid, "tab")
        try:
            (self.open_tab or default_open_tab)(self.store, self.sid)
        except Exception as e:
            print(f"{now()} shell tab failed: {e}", file=sys.stderr, flush=True)
        self.store.set_activity(self.sid, "in a shell tab")
        while True:   # the tab registers itself while "starting"; dead once it has exited
            await asyncio.sleep(SHELL_POLL_SECONDS)
            session = self.store.session(self.sid)
            if session is None or session["parked"]:
                self.stopping = True
                return
            if liveness.status(session) == "dead":
                break
        self.store.set_shell(self.sid, None)
        self.register()
        self.sent = self.results = 0
        self.compact_turn = None
        self.reader.cancel()
        await self.connect()
        self.reader = asyncio.create_task(self.read())

    # lifecycle

    def register(self) -> bool:
        pid = os.getpid()
        return self.store.register_if_free(self.sid, pid, liveness.start_time(pid), liveness.boot_id(),
                                           liveness.is_alive)

    async def disconnect(self) -> None:
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None

    def deny_stale(self) -> None:
        """A permission left open by a host that died can never be answered: close it."""
        for item in self.store.items(self.sid, include_closed=False):
            if item["kind"] == "permission" and item["status"] == "open":
                self.store.answer_permission(self.sid, item["ref"], "deny", LOST_ON_RESTART)

    async def run(self) -> None:
        if not self.register():
            sys.exit(f"claude-wheelhouse host: session {self.sid} is already running")
        self.deny_stale()
        await self.connect()
        self.reader = asyncio.create_task(self.read())
        try:
            while not self.stopping:
                if self.reader.done():   # Claude Code exited under us
                    exc = self.reader.exception() if not self.reader.cancelled() else None
                    self.store.set_activity(self.sid, f"stopped: {exc}"[:120] if exc else "stopped")
                    break
                try:
                    if not await self.poll():
                        break
                    self.store.heartbeat(self.sid)
                except sqlite3.OperationalError as e:   # database is locked: try again next pass
                    print(f"{now()} {e}: retrying", file=sys.stderr, flush=True)
                await asyncio.sleep(POLL_SECONDS)
        finally:
            self.reader.cancel()
            await self.disconnect()
            if self.store.session(self.sid) is not None:
                self.store.set_activity(self.sid, "")


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
