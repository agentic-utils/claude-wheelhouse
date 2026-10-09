"""A session's main conversation, read from its Claude Code transcript for the inbox's
session view.

Claude Code appends one JSON record per line to ~/.claude/projects/<project>/<id>.jsonl as
the conversation happens. The view shows the person's prompts, their general messages sent
from the wheelhouse and Claude's text in full, each tool call as one line, and nothing else:
no tool results, thinking, subagent (sidechain) records or bookkeeping. Only the tail of the
file is read, so a transcript of many megabytes costs the same as a short one.

What the inbox already shows elsewhere is left out too: answers on an item (its thread
holds them), the wheelhouse's own notices and the default opening prompt (a ticket line
before it stays), and Claude's wheelhouse tool calls (posting, replying, synopsis). A
general message keeps its words without the `[wheelhouse] from <person> (general):`
prefix, and one a notification cut short is shown whole, from the wheelhouse's copy.
"""

import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
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
# the opening prompt of a session started without a brief (launch.opening_prompt)
NO_BRIEF = "Session started from the wheelhouse. Wait for instructions."
# Claude's calls to the wheelhouse's own MCP tools, however the server is named
WHEELHOUSE_TOOL = re.compile(r"^mcp__(?:.*_)?wheelhouse__")
# One message from the person as the wheelhouse delivers it: "from <person> on Q1:" or "from
# <person> (general):" heads each one. A host types them as a user turn, a block each; a tab
# session's monitor prints them as one notification line, a batch "from <person>, 3 answers:"
# with blocks split by " ‖ " and each message's newlines flattened to " ⏎ ".
BATCH = re.compile(r"\[wheelhouse\] from \S+, \d+ answers: ")
FROM = re.compile(r"(?:\[wheelhouse\] from \S+ )?(on \S+|\(general\)):\s")
CUT = re.compile(r"\[cut short, full text: get_input\(message_id=(\d+)\)\] ")
CUT_MARK = " [cut short]"   # a message shown as it arrived, cut, when its full text has gone
# the opening prompt of a session started with a ticket and no brief (launch.opening_prompt)
OPENING = re.compile(r"(?:Ticket: [^\n]*\n\n)?" + re.escape(NO_BRIEF))


@dataclass
class Entry:
    who: str     # you | claude | tool | note
    text: str
    at: str = ""  # ISO timestamp, UTC


def project_folder(cwd: str) -> str:
    """How Claude Code names a project's transcript folder."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def locate(sid: str, projects: Path | None = None, cwd: str | None = None) -> Path | None:
    """The session's transcript: in its directory's project folder if cwd is given and it
    is there, else wherever it is."""
    projects = projects or PROJECTS
    if cwd and (path := projects / project_folder(cwd) / f"{sid}.jsonl").is_file():
        return path
    return next(projects.glob(f"*/{sid}.jsonl"), None)


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


def general_text(text: str, full=None) -> str | None:
    """The person's general messages in what the wheelhouse delivered, prefixes stripped and
    newlines restored; None if it carried only answers on items or the wheelhouse's notices.
    One the monitor cut short is shown whole from full(message id), the wheelhouse's copy,
    or as it arrived and marked as cut if that has gone."""
    if batch := BATCH.match(text):   # a monitor line carrying several messages
        parts = text[batch.end():].split(" ‖ ")
    else:   # a host's turn, a block per message, or a monitor line carrying one
        parts = re.split(r"\n\n(?=\[wheelhouse\] )", text)
    kept = []
    for part in parts:
        head = FROM.match(part)
        if head and head.group(1) == "(general)":
            said = part[head.end():].strip()
            if cut := CUT.match(said):
                said = (full and full(int(cut.group(1)))) or said[cut.end():].replace(" ⏎ ", "\n") + CUT_MARK
            else:
                said = said.replace(" ⏎ ", "\n")
            kept.append(said)
    return "\n\n".join(kept) or None


def tool_line(name: str, args) -> str:
    args = args if isinstance(args, dict) else {}
    detail = next((args[k] for k in ("description", "command", "file_path", "pattern", "url", "query", "prompt")
                   if isinstance(args.get(k), str) and args[k].strip()), "")
    if not detail:
        detail = next((v for v in args.values() if isinstance(v, str) and v.strip()), "")
    line = f"{name}: {' '.join(detail.split())}" if detail else name
    return line if len(line) <= TOOL_WIDTH else line[:TOOL_WIDTH - 1] + "…"


def entries(recs: list[dict], full=None) -> list[Entry]:
    """full: the person's message by id, for one a notification cut short (general_text)."""
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
                if event and (said := general_text(event.group(1).strip(), full)):
                    out.append(Entry("you", said, at))
            elif text.startswith("[wheelhouse]"):   # a host's turn: the person's messages, or a notice
                if said := general_text(text, full):
                    out.append(Entry("you", said, at))
            elif text and not text.startswith("<"):
                if OPENING.fullmatch(text):   # its ticket line stays; the default sentence goes
                    text = text[:-len(NO_BRIEF)].strip()
                if text:
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
                elif part.get("type") == "tool_use" and not WHEELHOUSE_TOOL.match(part.get("name", "")):
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


def hard_breaks(text: str) -> str:
    """The person's text as markdown that keeps their line breaks: markdown would join
    lines split by a single newline into one paragraph."""
    return re.sub(r"(?<!\n)\n(?!\n)", "  \n", text)


def _hhmm(at: str) -> str:
    return f" · {at[11:16]}Z" if len(at) >= 16 else ""


def blocks(name: str, tab: str | None, recs: list[dict] | None, queued=(), full=None) -> list[tuple[str, str]]:
    """The conversation as (who, markdown) blocks, then the person's general messages still
    queued for it. who is head, warn, note, tool, you or claude: the app colours
    the person's words apart from Claude's. tab None: the session runs in the wheelhouse,
    where its permission prompts and questions are items, so there's no tab to wait in.
    full: the person's message by id (entries)."""
    where = (f"_Tab: **{tab}**. Permission prompts and slash commands need that tab._" if tab else
             "_Runs in the wheelhouse: its permission prompts and questions come to the inbox. "
             "Shell opens it in a terminal tab._")
    out = [("head", f"## {name} · conversation\n\n{where}")]
    if recs is None:
        out.append(("note", "_No transcript found for this session yet._"))
    elif tab and (waiting := waiting_in_tab(recs)):
        out.append(("warn", f"> ⚠ **Waiting for you in its tab:** {waiting}."))
    for e in entries(recs or [], full):
        if e.who == "tool":
            out.append(("tool", "`⚙ " + e.text.replace("`", "'") + "`"))
        elif e.who == "note":
            out.append(("note", f"_{e.text}{_hhmm(e.at)}_"))
        else:
            text = hard_breaks(e.text) if e.who == "you" else e.text
            out.append((e.who, f"**{e.who}**{_hhmm(e.at)}\n\n{text}"))
    out += [("you", f"**you · queued**\n\n{hard_breaks(body)}") for body in queued]
    return out


def markdown(name: str, tab: str, recs: list[dict] | None, queued=()) -> str:
    return "\n\n".join(md for _, md in blocks(name, tab, recs, queued))


@dataclass
class Rename:
    at: str      # when it was made, ISO, UTC
    title: str
    # no /rename in the tail, only its name: made before the tail begins, so `at` is the
    # tail's first time, and it is taken only by a session never renamed since (take_title)
    inferred: bool = False


RENAMED = "Session renamed to: "   # what Claude Code's /rename prints, recorded with its time


class TitleWatch:
    """The latest /rename made in Claude Code, from the session's transcript. /rename writes
    a custom-title record with the new name, then a local-command record with its output,
    "Session renamed to: …", and the time. A launch's -n writes a custom-title record too,
    and Claude Code writes the name again as the conversation goes on, but neither prints
    that output: so only a /rename counts, and one back to the same name counts again. The
    tail is read once, then only what has been appended since. A /rename older than the tail
    shows only as the custom-title record's name: on a transcript longer than the tail, the
    latest is returned as an inferred rename. Thread-safe: the app's worker and a launch
    share one watch."""

    def __init__(self, sid: str, projects: Path | None = None, cwd: str | None = None):
        self.sid, self.projects, self.cwd = sid, projects, cwd
        self.path: Path | None = None
        self.offset: int | None = None
        self.title: str | None = None   # the latest custom-title record's
        self.renamed: Rename | None = None
        self.lock = threading.Lock()

    def read(self) -> Rename | None:
        with self.lock:
            return self._read()

    def _read(self) -> Rename | None:
        if self.path is None:
            self.path = locate(self.sid, self.projects, self.cwd)
            if self.path is None:
                return None
        try:
            with open(self.path, "rb") as f:
                size = f.seek(0, 2)
                if size == self.offset:
                    return self.renamed
                tail = self.offset is None or size < self.offset
                start = max(0, size - TAIL_BYTES) if tail else self.offset
                f.seek(start)
                data = f.read(size - start)
        except OSError:
            self.path = self.offset = None
            return self.renamed
        data = data[:data.rfind(b"\n") + 1]   # a record still being written waits for the next read
        self.offset = start + len(data)
        for line in data.splitlines():
            if b"custom-title" in line or RENAMED.encode() in line:
                try:
                    rec = json.loads(line)
                except ValueError:   # the tail's first line, cut mid-record
                    continue
                if isinstance(rec, dict):
                    self._take(rec)
        if tail and start and self.renamed is None and self.title and (at := first_time(data)):
            self.renamed = Rename(at, self.title, inferred=True)
        return self.renamed

    def _take(self, rec: dict) -> None:
        if rec.get("type") == "custom-title" and rec.get("customTitle"):
            self.title = rec["customTitle"]
            return
        content = rec.get("content")
        if (rec.get("type"), rec.get("subtype")) != ("system", "local_command") or not isinstance(content, str) \
                or RENAMED not in content:
            return
        if (at := utc(rec)) is None:
            return
        said = content.split(RENAMED, 1)[1].split("</local-command-stdout>", 1)[0].strip()
        if title := self.title or said:   # the custom-title record written just before
            self.renamed = Rename(at, title)


def utc(rec: dict) -> str | None:
    """A record's time, ISO, UTC."""
    try:
        at = datetime.fromisoformat(rec.get("timestamp") or "").astimezone(timezone.utc)
    except ValueError:
        return None
    return at.isoformat(timespec="microseconds")


def first_time(data: bytes) -> str | None:
    """The time of the first record that has one."""
    for line in data.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:   # cut mid-record
            continue
        if isinstance(rec, dict) and (at := utc(rec)):
            return at
    return None


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
