"""A session's main conversation, read from its Claude Code transcript for the inbox's
session view.

Claude Code appends one JSON record per line to ~/.claude/projects/<project>/<id>.jsonl as
the conversation happens. The view shows the person's prompts, Claude's text and the
wheelhouse's notifications in full, each tool call as one line, and nothing else: no tool
results, thinking, subagent (sidechain) records or bookkeeping. Only the tail of the file
is read, so a transcript of many megabytes costs the same as a short one.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

PROJECTS = Path.home() / ".claude/projects"
TAIL_BYTES = 1024 * 1024   # read from the end; older turns are left out
MAX_ENTRIES = 80
TOOL_WIDTH = 90
# tools that stop the session until the person answers in its tab. Their call is written
# before the answer, so a call with no result yet means the session is waiting. Permission
# prompts leave no trace in the transcript and can't be seen this way.
ASKS_IN_TAB = {"AskUserQuestion": "a question", "ExitPlanMode": "a plan to approve"}
EVENT = re.compile(r"<event>(.*?)</event>", re.S)


@dataclass
class Entry:
    who: str     # you | claude | tool | wheelhouse | note
    text: str
    at: str = ""  # ISO timestamp, UTC


def locate(sid: str, projects: Path | None = None) -> Path | None:
    return next((projects or PROJECTS).glob(f"*/{sid}.jsonl"), None)


def tail_records(path: Path, limit: int = TAIL_BYTES) -> list[dict]:
    with open(path, "rb") as f:
        size = f.seek(0, 2)
        f.seek(max(0, size - limit))
        data = f.read()
    lines = data.splitlines()
    if size > limit:
        lines = lines[1:]   # cut mid-record
    recs = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and not rec.get("isSidechain"):
            recs.append(rec)
    return recs


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def tool_line(name: str, args) -> str:
    args = args if isinstance(args, dict) else {}
    detail = next((args[k] for k in ("description", "command", "file_path", "pattern", "url", "query", "prompt")
                   if isinstance(args.get(k), str) and args[k].strip()), "")
    if not detail:
        detail = next((v for v in args.values() if isinstance(v, str) and v.strip()), "")
    line = f"{name}: {' '.join(detail.split())}" if detail else name
    return line if len(line) <= TOOL_WIDTH else line[:TOOL_WIDTH - 1] + "…"


def entries(recs: list[dict]) -> list[Entry]:
    out = []
    for rec in recs:
        kind, at = rec.get("type"), rec.get("timestamp", "")
        msg = rec.get("message") or {}
        if kind == "system" and rec.get("subtype") == "compact_boundary":
            out.append(Entry("note", "conversation compacted", at))
        elif kind == "user" and not rec.get("isMeta"):
            if rec.get("isCompactSummary"):
                continue
            text = _text(msg.get("content")).strip()
            origin = (rec.get("origin") or {}).get("kind")
            if origin == "task-notification" or text.startswith("<task-notification>"):
                event = EVENT.search(text)
                if event and event.group(1).lstrip().startswith("[wheelhouse]"):
                    out.append(Entry("wheelhouse", event.group(1).strip(), at))
            elif text and not text.startswith("<"):
                out.append(Entry("you", text, at))
        elif kind == "assistant":
            for part in msg.get("content") or []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text" and part.get("text", "").strip():
                    if out and out[-1].who == "claude":   # one reply split across records
                        out[-1].text += "\n\n" + part["text"].strip()
                    else:
                        out.append(Entry("claude", part["text"].strip(), at))
                elif part.get("type") == "tool_use":
                    out.append(Entry("tool", tool_line(part.get("name", "?"), part.get("input")), at))
    return out[-MAX_ENTRIES:]


def waiting_in_tab(recs: list[dict]) -> str | None:
    """What the session is waiting on the person for in its own tab, if the transcript shows it."""
    answered, asked = set(), None
    for rec in recs:
        for part in (rec.get("message") or {}).get("content") or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "tool_result":
                answered.add(part.get("tool_use_id"))
            elif part.get("type") == "tool_use" and part.get("name") in ASKS_IN_TAB:
                asked = part
    return ASKS_IN_TAB[asked["name"]] if asked and asked.get("id") not in answered else None


def _hhmm(at: str) -> str:
    return f" · {at[11:16]}Z" if len(at) >= 16 else ""


def blocks(name: str, tab: str, recs: list[dict] | None, queued=()) -> list[tuple[str, str]]:
    """The conversation as (who, markdown) blocks, then the person's general messages still
    queued for it. who is head, warn, note, tool, you, wheelhouse or claude: the app colours
    the person's words apart from Claude's."""
    out = [("head", f"## {name} · conversation\n\n_Tab: **{tab}**. Permission prompts and slash "
                    "commands need that tab._")]
    if recs is None:
        out.append(("note", "_No transcript found for this session yet._"))
    elif waiting := waiting_in_tab(recs):
        out.append(("warn", f"> ⚠ **Waiting for you in its tab:** {waiting}."))
    for e in entries(recs or []):
        if e.who == "tool":
            out.append(("tool", "`⚙ " + e.text.replace("`", "'") + "`"))
        elif e.who == "note":
            out.append(("note", f"_{e.text}{_hhmm(e.at)}_"))
        else:
            out.append((e.who, f"**{e.who}**{_hhmm(e.at)}\n\n{e.text}"))
    out += [("you", f"**you · queued**\n\n{body}") for body in queued]
    return out


def markdown(name: str, tab: str, recs: list[dict] | None, queued=()) -> str:
    return "\n\n".join(md for _, md in blocks(name, tab, recs, queued))


class Follower:
    """One session's transcript, re-read only when the file has changed."""

    def __init__(self, sid: str, projects: Path | None = None):
        self.sid, self.projects = sid, projects
        self.path: Path | None = None
        self.seen = None
        self.recs: list[dict] | None = None
        self.blocks_key, self.blocks = None, None   # the app's rendering of recs, kept with them

    def read(self) -> list[dict] | None:
        if self.path is None:
            self.path = locate(self.sid, self.projects)
            if self.path is None:
                return None
        try:
            st = self.path.stat()
        except OSError:
            self.path = None
            return None
        if (st.st_size, st.st_mtime_ns) != self.seen:
            self.seen = (st.st_size, st.st_mtime_ns)
            self.recs = tail_records(self.path)
        return self.recs
