"""The helper chat (#198, .plan/helper-chat.md): how-to questions about the wheelhouse,
answered by Haiku through the person's own Claude Code login. No Textual: the app paints it.

One `claude -p` reading stream-json on stdin, started at the first question, never before,
and kept for the chat. It is told the fact sheet (helper_facts.md, kept in step with the code
by facts.py), the README and what sessions are told (protocol.md), in the first message, and
may read the package's and the modules' source with Read, Grep and Glob, nothing else: no
Bash, no edits, no web, no MCP servers (so no `wheelhouse` server and nothing that acts on a
session), none of the person's settings, hooks or plugins, no transcript written, and no
WHEELHOUSE_* variable that could find the daemon. Anything that would need permission is
denied (dontAsk, no one to ask). A chat quiet for IDLE seconds stops its claude, and the next
question starts another, told the chat so far. Each answer streams in as events to the
callback it was given, on the helper's reader thread, or on the asker's own for a claude that
couldn't start: the callback must hand them to its own loop either way."""

import contextlib
import importlib.metadata
import importlib.util
import json
import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import plan_usage
from .launch import clean_env

PACKAGE = Path(__file__).resolve().parent
FACTS = PACKAGE / "helper_facts.md"
INSTALL_DIR = PACKAGE.parent   # README.md, in a checkout
MODEL = "claude-haiku-5-5"
TOOLS = "Read,Grep,Glob"
IDLE = 600.0     # seconds without a question before its claude stops: the prompt cache has gone cold by then
FULL = 90_000    # context tokens past which the drawer offers New chat: past 100k Haiku's long-prompt rate applies
GROUP = "claude_wheelhouse.modules"   # api.GROUP, not imported: api imports half the package
# what nothing in the helper's environment may carry: the wheelhouse's own variables
UNSET_PREFIX = "WHEELHOUSE_"
# every flag that keeps it a read-only helper (the tests check each)
SANDBOX = ("--tools", TOOLS, "--strict-mcp-config", "--setting-sources=", "--restricted",
           "--permission-mode", "dontAsk", "--permission-prompts", "none", "--no-session-persistence")

RULES = """You are the help desk inside claude-wheelhouse, a terminal app that runs beside a
person's Claude Code sessions. You answer how-to questions about the wheelhouse: what a key,
button or pane does, how to do something, why something behaves as it does.

- The first message holds the fact sheet, the README and the protocol sessions are told.
  The fact sheet's generated part comes from the code; prose may lag it. Where they
  disagree, the fact sheet wins, and the source (Read, Grep, Glob in this directory and the
  modules') wins over both.
- Name a key or button only as the fact sheet lists it or as a Binding in the source says.
  Write keys in bold: **Alt+P**.
- When an answer came from the source, say which file or symbol.
- Say "I don't know" rather than guess. Keep answers short: a sentence or a few steps.
- {mode}
- You can't act on anything: you only explain. Questions about anything other than the
  wheelhouse and Claude Code as it runs there get a polite one-line refusal.
- British English."""
TABS = ("The person runs sessions as Windows Terminal tabs: SDK mode is off. Never describe SDK-only "
        "features (host.py, the effort slider, Interrupt, Compact, Model, the permission buttons).")
SDK = ("The person has SDK mode on (features.py). The fact sheet answers for tabs; for sessions run in the "
       "wheelhouse (host.py) the source has the rest: Interrupt, Compact, Model, the permission buttons.")


@dataclass(frozen=True)
class Event:
    """What the helper says as an answer comes: "text" (a piece of the answer, to append),
    "tool" (it is reading: text says what), "done" (the answer is complete: text is all of it,
    tokens the context it now holds) or "error" (text says what went wrong, plainly)."""
    kind: str
    text: str = ""
    tokens: int = 0


def module_dirs() -> list[Path]:
    """The installed modules' package directories, found without importing them."""
    out = []
    for ep in sorted(importlib.metadata.entry_points(group=GROUP), key=lambda ep: ep.name):
        with contextlib.suppress(ImportError, ValueError):
            spec = importlib.util.find_spec(ep.module.partition(".")[0])
            if spec and spec.origin:
                out.append(Path(spec.origin).resolve().parent)
    return out


def command(sdk: bool = False) -> list[str]:
    """The helper's claude: stream-json both ways, Haiku, sandboxed, the modules' source
    readable beside the package's (its cwd). --add-dir last: it takes every argument after it."""
    claude = os.environ.get(plan_usage.CLAUDE_ENV) or plan_usage.CLAUDE
    dirs = [str(d) for d in module_dirs()]
    return [claude, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--include-partial-messages", "--model", MODEL, *SANDBOX,
            "--system-prompt", RULES.format(mode=SDK if sdk else TABS), *(["--add-dir", *dirs] if dirs else [])]


def environment(base=None) -> dict[str, str]:
    """The helper's environment: a launch's (clean_env), without any WHEELHOUSE_ variable."""
    return {k: v for k, v in clean_env(base).items() if not k.startswith(UNSET_PREFIX)}


def briefing() -> str:
    """What the first message carries before the question: the fact sheet (its stamp left
    off), the README and the protocol."""
    facts = FACTS.read_text()
    parts = [("fact-sheet", facts.partition("\n")[2] if facts.startswith("<!--") else facts)]
    for name, path in (("readme", INSTALL_DIR / "README.md"), ("protocol", PACKAGE / "protocol.md")):
        if path.exists():
            parts.append((name, path.read_text()))
    return "\n\n".join(f"<{name}>\n{text.strip()}\n</{name}>" for name, text in parts)


def opening(question: str, history: list[tuple[str, str]]) -> str:
    """A new claude's first message: the briefing, the chat so far (after an idle stop), then
    the question."""
    earlier = "".join(f"<question>{q}</question>\n<answer>{a}</answer>\n" for q, a in history)
    return (briefing() + (f"\n\n<chat-so-far>\n{earlier}</chat-so-far>" if earlier else "")
            + f"\n\nThe person asks:\n{question}")


def plainly(text: str) -> str:
    """What went wrong, said for the person, from what Claude Code said."""
    low = (text or "").lower()
    if any(s in low for s in ("not logged in", "/login", "invalid api key", "authentication", "oauth")):
        return "Claude Code isn't logged in. Run `claude` in a terminal and /login, then ask again."
    if any(s in low for s in ("rate limit", "usage limit", "limit reached", "429", "overloaded")):
        return "Claude is busy or you've reached a usage limit (the helper runs on your plan). Try again later."
    first = next((line.strip() for line in (text or "").splitlines() if line.strip()), "")
    return f"The helper stopped: {first[:200]}" if first else "The helper stopped without saying why."


class Helper:
    """One chat. ask() a question; its answer comes to on_event as Events, on a thread of the
    helper's own (a claude that can't start: on the asker's), mostly under its lock: the
    callback hands each to its own loop and returns, never calling back in. One question at a time: ask while busy is refused (False). The claude
    starts at the first ask and stops on stop(), on reset() (New chat, the history
    cleared too) or on idle() past IDLE, the history kept for the next."""

    def __init__(self, on_event, sdk: bool = False, command_=None, clock=time.monotonic):
        self.on_event, self.sdk, self.clock = on_event, sdk, clock
        self.command = list(command_) if command_ else None   # a test's fake, else command(sdk)
        self.history: list[tuple[str, str]] = []
        self.proc: subprocess.Popen | None = None
        self.busy = False
        self.question = ""
        self.tokens = 0       # the context the chat holds, by the last request's usage
        self.last = clock()
        self.lock = threading.Lock()

    @property
    def full(self) -> bool:
        """Past FULL: time to offer New chat."""
        return self.tokens >= FULL

    def ask(self, question: str) -> bool:
        """The question sent, starting a claude first if none runs (told the chat so far).
        False if one is being answered. An error is an Event, never raised."""
        question = question.strip()
        why = None
        with self.lock:
            if self.busy or not question:
                return False
            self.busy, self.question, self.last = True, question, self.clock()
            proc = self.proc
            try:
                text = question
                if proc is None or proc.poll() is not None:
                    proc = self.start()
                    text = opening(question, self.history)
                # on a thread: the first message (about 90 KB) outgrows the pipe, and the caller
                # is the app's loop, which mustn't wait for claude to start reading
                line = json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n"
                threading.Thread(target=self.send, args=(proc, line), daemon=True, name="helper-send").start()
            except FileNotFoundError:
                why = "Claude Code isn't installed, or `claude` isn't on your PATH: the helper runs it."
            except (OSError, ValueError) as e:
                why = plainly(str(e))
            if why:
                self.busy = False
                self.halt()
        if why:
            self.on_event(Event("error", why))
        return True

    @staticmethod
    def send(proc: subprocess.Popen, line: str) -> None:
        """A message written to the claude. One that can't take it is ended, so its reader
        sees it go and says why, from what it said on stderr."""
        try:
            proc.stdin.write(line)
            proc.stdin.flush()
        except (OSError, ValueError):
            plan_usage.kill(proc)

    def start(self) -> subprocess.Popen:
        """Under the lock: the claude, with a thread reading what it says. Raises OSError if
        it can't start."""
        err = tempfile.TemporaryFile("w+")
        try:
            proc = subprocess.Popen(self.command or command(self.sdk), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=err, cwd=PACKAGE, env=environment(), text=True, close_fds=True,
                                    start_new_session=True)
        except OSError:
            err.close()
            raise
        self.proc = proc
        threading.Thread(target=self.read, args=(proc, err), daemon=True, name="helper").start()
        return proc

    def read(self, proc: subprocess.Popen, err) -> None:
        """What the claude says, on its own thread, as Events: until it exits."""
        answer, said = [], False
        try:
            for line in proc.stdout:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                kind = msg.get("type")
                if kind == "stream_event":
                    event = msg.get("event") or {}
                    if event.get("type") == "content_block_start" and (event.get("content_block") or {}).get("type") == "text" and said:
                        answer.append("\n\n")
                        self.emit(proc, Event("text", "\n\n"))
                    delta = event.get("delta") or {}
                    if event.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                        said = True
                        answer.append(delta.get("text", ""))
                        self.emit(proc, Event("text", delta.get("text", "")))
                elif kind == "assistant":
                    message = msg.get("message") or {}
                    usage = message.get("usage") or {}
                    if usage and self.proc is proc:   # not a claude stopped since (New chat)
                        self.tokens = sum(usage.get(k) or 0 for k in
                                          ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
                    for block in message.get("content") or []:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            self.emit(proc, Event("tool", reading(block)))
                elif kind == "result":
                    self.finish(proc, msg, "".join(answer))
                    answer, said = [], False
        except (OSError, ValueError):
            pass
        with contextlib.suppress(OSError, ValueError, subprocess.TimeoutExpired):
            proc.stdout.close()
            proc.wait(5)   # reaped: it has closed its stdout, so it is going or gone
        with self.lock:
            mine = self.proc is proc
            died = mine and self.busy   # mid-answer
            if mine:
                self.proc, self.busy = None, False
        if died:
            err.seek(0)
            self.on_event(Event("error", plainly(err.read()[-2000:])))
        err.close()

    def emit(self, proc: subprocess.Popen, event: Event) -> None:
        """An event from this claude, unless it has been stopped since (New chat)."""
        with self.lock:   # under it, so a New chat between the check and the event can't let it through
            if self.proc is proc:
                self.on_event(event)

    def finish(self, proc: subprocess.Popen, result: dict, streamed: str) -> None:
        """A result: the answer done, kept in the history, or an error said."""
        with self.lock:
            if self.proc is not proc:
                return
            self.busy, self.last = False, self.clock()
            if result.get("is_error") or result.get("subtype") != "success":
                event = Event("error", plainly(str(result.get("result") or result.get("subtype") or "")))
            else:
                text = streamed.strip() or str(result.get("result") or "").strip()
                self.history.append((self.question, text))
                event = Event("done", text, self.tokens)
            self.on_event(event)

    def idle(self, now: float | None = None) -> bool:
        """Its claude stopped if the chat has been quiet IDLE seconds: True if it was."""
        with self.lock:
            if self.proc is None or self.busy or (self.clock() if now is None else now) - self.last < IDLE:
                return False
            self.halt()
            return True

    def reset(self) -> None:
        """New chat: the claude stopped, the history and the context gone."""
        with self.lock:
            self.halt()
            self.history, self.tokens, self.busy = [], 0, False

    def stop(self) -> None:
        """The app is quitting: the claude stopped before it returns, not on a thread the
        interpreter's exit could beat (the claude, in a session of its own, would run on)."""
        with self.lock:
            proc, self.proc, self.busy = self.proc, None, False
        if proc is not None:
            plan_usage.kill(proc)

    def halt(self) -> None:
        """Under the lock: the claude and its children ended, if one runs, off the caller's thread."""
        proc, self.proc = self.proc, None
        if proc is not None:   # on a thread: a kill can take seconds, and the caller is the app's loop
            threading.Thread(target=plan_usage.kill, args=(proc,), daemon=True, name="helper-kill").start()


def reading(block: dict) -> str:
    """A tool call, said in a few words: "Read tui.py", "Grep alt+g"."""
    args = block.get("input") or {}
    what = args.get("file_path") or args.get("pattern") or args.get("path") or ""
    with contextlib.suppress(ValueError):
        what = str(Path(what).relative_to(PACKAGE)) if what.startswith("/") else what
    return f"{block.get('name', 'a tool')} {what}".strip()
