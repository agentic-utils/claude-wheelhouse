"""The subagent watchdog (#328): a running subagent that has gone quiet is flagged, once per
quiet spell, to its session (a [wheelhouse] notice naming it and its last action) and to the
person (its A item's row turns amber, items.quiet_at). The clock is the hub's, which syncs each
session's AgentWatcher every tick; this module is what it reads and decides, from the
subagent's own transcript.

Quiet means `limit` with no new record and no tool call in flight: the session's own minutes,
set with set_watchdog (the `watchdog.<session id>` setting), else LIMIT. A call in flight gets
its own timeout plus AFTER (a `timeout` input in ms, Bash's default without one, at most
MAX_TIMEOUT; a tool with none gets the limit), so a hung call is still caught. A call waiting
on the person's approval is flagged at the limit whatever the tool: known only in an SDK
session, whose host posts an open permission item holding the call's input; a tab's prompt
leaves no trace, so it is held to its timeout. A subagent that will be quiet on purpose calls
the `quiet(minutes, why)` MCP tool: the server can't tell which agent called (they share the
session's connection, and a call carries no agent id), so the declaration is read here, from
the tool call in the calling subagent's own transcript. Once its period ends, normal watching
resumes. See .plan/wheelhouse-sessions.md ("Subagents").
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import transcript_usage

LIMIT = 5             # minutes: the quiet limit, unless the session sets its own
MAX_MINUTES = 1440    # the most set_watchdog or quiet takes
AFTER = 60            # seconds past a call's own timeout before it counts as hung
BASH_TIMEOUT = 120    # seconds: Bash's own when the call gives none
MAX_TIMEOUT = 600     # seconds: the most a call's timeout counts for
SETTING = "watchdog."   # + session id: its limit in minutes, as set_watchdog stored it
QUIET_TOOL = re.compile(r"^mcp__\w*wheelhouse\w*__quiet$")
ACTION_MAX = 160


def minutes(value, what: str = "minutes") -> int:
    """A tool's minutes, checked: a whole number from 1 to MAX_MINUTES."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value) \
            or not 1 <= value <= MAX_MINUTES:
        raise ValueError(f"{what} must be a whole number from 1 to {MAX_MINUTES}")
    return int(value)


def limit(store, sid: str) -> int:
    """The session's quiet limit in seconds."""
    try:
        return minutes(int(store.setting(SETTING + sid) or LIMIT)) * 60
    except ValueError:
        return LIMIT * 60


def iso(t: float) -> str:
    """A record's time as the store keeps quiet_at: fixed width, so two compare as text."""
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds")


def summary(name: str, inp) -> str:
    """A tool call in one line: its name and what it was given that says most."""
    inp = inp if isinstance(inp, dict) else {}
    what = next((inp[k] for k in ("command", "file_path", "path", "description", "pattern", "query", "url", "prompt")
                 if isinstance(inp.get(k), str) and inp[k].strip()), "")
    line = " ".join(what.split())
    return f"{name}: {line}"[:ACTION_MAX] if line else name


@dataclass
class Call:
    id: str
    name: str
    input: dict
    at: float

    def timeout(self, quiet: int) -> float:
        """Seconds it may run before it counts as hung, AFTER aside."""
        given = self.input.get("timeout")
        if isinstance(given, (int, float)) and not isinstance(given, bool) and given > 0:
            return min(given / 1000, MAX_TIMEOUT)
        return BASH_TIMEOUT if self.name == "Bash" else quiet

    def asked(self) -> str:
        """Its input as an SDK host's permission item writes it (host.permission_body)."""
        return json.dumps(self.input, indent=2, ensure_ascii=False)


@dataclass
class Follower:
    """One subagent's transcript, read from where the last read stopped."""
    path: Path
    offset: int = 0
    last_at: float | None = None   # its newest record's time
    calls: dict[str, Call] = field(default_factory=dict)   # tool calls in flight, by id
    action: str = ""               # its last action, in one line
    action_call: str | None = None   # the call that action is, if it was one
    until: float | None = None     # a declared quiet period's end
    why: str = ""                  # and the reason it gave
    told: float | None = None      # the last_at of the spell last flagged

    def read(self) -> bool:
        """Whatever it has written since: True if any record."""
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                data = f.read()
        except OSError:
            return False
        end = data.rfind(b"\n") + 1   # a half-written last line waits for the next read
        self.offset += end
        new = False
        for line in data[:end].splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                new = self.take(rec) or new
        return new

    def take(self, rec: dict) -> bool:
        t = transcript_usage.epoch(rec["timestamp"]) if isinstance(rec.get("timestamp"), str) else None
        if t is None:
            return False
        self.last_at = max(self.last_at or t, t)
        content = (rec.get("message") or {}).get("content")
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                name, inp = str(block.get("name") or "tool"), block.get("input")
                inp = inp if isinstance(inp, dict) else {}
                self.calls[block["id"]] = Call(block["id"], name, inp, t)
                self.action, self.action_call = summary(name, inp), block["id"]
                if QUIET_TOOL.match(name):
                    try:
                        self.until, self.why = t + minutes(inp.get("minutes")) * 60, str(inp.get("why") or "")
                    except ValueError:
                        pass   # refused by the tool too
            elif block.get("type") == "tool_result":
                self.calls.pop(block.get("tool_use_id"), None)
            elif block.get("type") == "text" and rec.get("type") == "assistant" and block.get("text", "").strip():
                self.action, self.action_call = f"wrote: {' '.join(block['text'].split())}"[:ACTION_MAX], None
        return True

    def deadline(self, quiet: int, approving=lambda call: False) -> float | None:
        """When it counts as quiet (epoch seconds), None before its first record."""
        if self.last_at is None:
            return None
        if not self.calls or any(approving(c) for c in self.calls.values()):
            due = self.last_at + quiet
        else:
            due = self.last_at + max(c.timeout(quiet) for c in self.calls.values()) + AFTER
        return max(due, self.until) if self.until else due

    def last(self) -> str:
        """Its last action, saying whether that call is still running."""
        if not self.action:
            return "nothing yet"
        return f"{self.action} (still running)" if self.action_call in self.calls else self.action


def message(ref: str, title: str, aid: str, follower: Follower, now: float, approving: bool) -> str:
    """What the session is told, without the [wheelhouse] mark (a notice gets it delivered)."""
    quiet = max(1, round((now - follower.last_at) / 60))
    waits = " It is waiting on the person's approval." if approving else ""
    declared = f" It had declared a quiet period, now over: {follower.why}." if follower.until and follower.why \
        else " It had declared a quiet period, now over." if follower.until else ""
    return (f"Subagent {ref} \"{title}\" (agent {aid}) has been quiet for {quiet} min. Its last action: "
            f"{follower.last()}.{waits}{declared} Steer it with SendMessage, or stop it and dispatch it again.")
