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
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

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
# a subagent's own tool for handing its result back to the session that started it: its
# message is the result, shown whole as its reply, not cut to a tool line
HANDBACK = "SubagentHandback"
# the opening prompt of a session started with a ticket and no brief (launch.opening_prompt)
OPENING = re.compile(r"(?:Ticket: [^\n]*\n\n)?" + re.escape(NO_BRIEF))
# a fork subagent's prompt: Claude Code's standing rules for forks wrapped round it, then the
# directive its session gave it
FORK = re.compile(r"<fork-boilerplate>.*?</fork-boilerplate>\s*(?:Your directive:\s*)?", re.S)
# a skill run as a subagent: its first turn is the skill itself (meta), what it was asked last
SKILL_ARGS = "\n\nARGUMENTS: "
# and where it lives, its name the folder's: what it shows as when it was asked nothing
SKILL_BASE = re.compile(r"^Base directory for this skill: (.+)$", re.M)
# a prompt that reached the session mid-turn: Claude Code folds it into the running turn as an
# attachment of this type, not a user record of its own (its commandMode is the origin)
QUEUED = "queued_command"
# stands in the tail for a newest record too large to read within it (tail_records)
OVERSIZE = "wheelhouse-oversize"


@dataclass
class Entry:
    who: str     # you | claude | tool | note
    text: str
    at: str = ""  # ISO timestamp, UTC


def project_folder(cwd: str) -> str:
    """How Claude Code names a project's transcript folder."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def locate(sid: str, projects: Path | None = None) -> Path | None:
    return next((projects or PROJECTS).glob(f"*/{sid}.jsonl"), None)


def tail_records(path: Path, limit: int | None = None, sidechain: bool = False) -> list[dict]:
    """The last records of a transcript, in its last limit bytes (TAIL_BYTES). A session's
    leaves out its subagents' (sidechain) records; a subagent's own transcript is nothing
    but, so sidechain keeps them."""
    limit = limit or TAIL_BYTES
    with open(path, "rb") as f:
        size = f.seek(0, 2)
        start = max(0, size - limit)
        f.seek(max(0, start - 1))   # a byte early, to see whether the window starts a record
        data = f.read()
    # the newest record is larger than the window if none starts in it: no newline before its
    # last byte. Decided before the trim, so one still being written that does start in it isn't
    oversize = start and b"\n" not in data[:-1]
    if start:   # drop the record the window starts inside; one starting exactly at it is whole
        data = data[data.find(b"\n") + 1:] if b"\n" in data else b""
    recs = []
    for line in data.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and (sidechain or not rec.get("isSidechain")):
            recs.append(rec)
    if oversize:   # said so
        recs.append({"type": OVERSIZE})
    return recs


def prompt_record(path: Path) -> dict | None:
    """A subagent's prompt: its transcript's first user record, read from its start, past
    any of the wheelhouse's or Claude Code's own (meta) but a skill's. Records before it (a
    fork's reference to its session's history, and that session's own call starting it)
    aren't the subagent's."""
    with open(path, "rb") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("type") == "user" and \
                    (not rec.get("isMeta") or skill_run(rec)):
                return rec
    return None


def skill_run(rec: dict) -> bool:
    text = _text((rec.get("message") or {}).get("content"))
    return SKILL_ARGS in text or SKILL_BASE.search(text) is not None


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


def persons(text: str, origin: str | None, full=None) -> str | None:
    """What the person said in a prompt the session took (origin: how it came), as
    entries shows it; None for what isn't theirs to show here."""
    if origin == "task-notification" or text.startswith("<task-notification>"):
        event = EVENT.search(text)
        return general_text(event.group(1).strip(), full) if event else None
    if text.startswith("[wheelhouse]"):   # a host's turn: the person's messages, or a notice
        return general_text(text, full)
    if text and not text.startswith("<"):
        if OPENING.fullmatch(text):   # its ticket line stays; the default sentence goes
            text = text[:-len(NO_BRIEF)].strip()
        return text or None
    return None


def entries(recs: list[dict], full=None, agent: bool = False) -> list[Entry]:
    """full: the person's message by id, for one a notification cut short (general_text).
    agent: a subagent's transcript, whose fork prompt keeps its directive (FORK)."""
    out = []
    for rec in recs:
        kind, at = rec.get("type"), rec.get("timestamp", "")
        msg = rec.get("message") or {}
        if kind == "system" and rec.get("subtype") == "compact_boundary":
            out.append(Entry("note", "conversation compacted", at))
        elif kind == OVERSIZE:
            out.append(Entry("note", "a turn too large to show here", at))
        elif kind == "user" and not rec.get("isMeta"):
            if rec.get("isCompactSummary"):
                continue
            text = _text(msg.get("content")).strip()
            if agent and text.startswith("<fork-boilerplate>"):
                text = FORK.sub("", text, count=1).strip()
            if said := persons(text, (rec.get("origin") or {}).get("kind"), full):
                out.append(Entry("you", said, at))
        elif kind == "attachment" and (queued := rec.get("attachment") or {}).get("type") == QUEUED:
            if said := persons(_text(queued.get("prompt")).strip(), queued.get("commandMode"), full):
                out.append(Entry("you", said, at))
        elif kind == "assistant":
            for part in msg.get("content") or []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text" and part.get("text", "").strip():
                    if out and out[-1].who == "claude":   # one reply split across records
                        out[-1].text += "\n\n" + part["text"].strip()
                    else:
                        out.append(Entry("claude", part["text"].strip(), at))
                elif part.get("type") == "tool_use" and part.get("name") == HANDBACK \
                        and isinstance(message := (part.get("input") or {}).get("message"), str) and message.strip():
                    out.append(Entry("claude", message.strip(), at))
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
    out += [entry_block(e) for e in entries(recs or [], full)]
    out += [("you", f"**you · queued**\n\n{hard_breaks(body)}") for body in queued]
    return out


def entry_block(e: Entry, label: str | None = None) -> tuple[str, str]:
    """One entry as a (who, markdown) block, headed by who it is or by label."""
    if e.who == "tool":
        return "tool", "`⚙ " + e.text.replace("`", "'") + "`"
    if e.who == "note":
        return "note", f"_{e.text}{_hhmm(e.at)}_"
    text = hard_breaks(e.text) if e.who == "you" else e.text
    return e.who, f"**{label or e.who}**{_hhmm(e.at)}\n\n{text}"


AGENT_HEAD = "### Its conversation"
NO_AGENT_LINK = ("_No subagent transcript is linked to this item: the session posted it itself, and the "
                 "wheelhouse hasn't matched it to one of the session's subagents._")
NO_AGENT_FILE = "_Its transcript isn't found beside its session's yet, so only what the session's transcript says of it shows._"


def prompt_entry(rec: dict | None) -> Entry | None:
    """A subagent's prompt, from its prompt_record: a skill's is what the skill was asked, or
    its name if it was asked nothing."""
    if rec is None:
        return None
    if rec.get("isMeta"):
        text = _text((rec.get("message") or {}).get("content"))
        cut = text.rfind(SKILL_ARGS)
        asked = text[cut + len(SKILL_ARGS):].strip() if cut >= 0 else ""
        if not asked and (base := SKILL_BASE.search(text)):   # asked nothing: the skill's name
            asked = PurePosixPath(base.group(1).strip()).name
        return Entry("you", asked, rec.get("timestamp", "")) if asked else None
    return next((e for e in entries([rec], agent=True) if e.who == "you"), None)


def agent_blocks(recs: list[dict] | None, linked: bool = True, prompt: dict | None = None) -> list[tuple[str, str]]:
    """A subagent's own conversation, from its transcript, as (who, markdown) blocks: the
    prompt its session gave it (prompt, its prompt_record, read apart from recs, the
    tail after it, so it shows however long the subagent runs), its tool calls and
    replies, and its result, the last of them. What the session sends it later
    (SendMessage) is the session's. None for recs: its transcript isn't found; linked
    False: the item isn't tied to a subagent at all."""
    out = [("head", AGENT_HEAD)]
    if not linked:
        return out + [("note", NO_AGENT_LINK)]
    if recs is None:
        return out + [("note", NO_AGENT_FILE)]
    first, said = prompt_entry(prompt), entries(recs, agent=True)
    if not (first or said):
        return out + [("note", "_Nothing in its transcript yet._")]
    return out + ([entry_block(first, "prompt")] if first else []) \
        + [entry_block(e, "session" if e.who == "you" else None) for e in said]


def agent_path(main: Path, sid: str, agent_id: str) -> Path:
    """Where Claude Code writes a subagent's own transcript, beside its session's (subagents.py)."""
    return main.parent / sid / "subagents" / f"agent-{agent_id}.jsonl"


def markdown(name: str, tab: str, recs: list[dict] | None, queued=()) -> str:
    return "\n\n".join(md for _, md in blocks(name, tab, recs, queued))


class Follower:
    """One session's transcript, re-read only when the file has changed."""
    sidechain = False   # a session's: its subagents' records are left out (tail_records)

    def __init__(self, sid: str, projects: Path | None = None):
        self.sid, self.projects = sid, projects
        self.path: Path | None = None
        self.seen = None
        self.recs: list[dict] | None = None
        self.blocks_key, self.blocks = None, None   # the app's rendering of recs, kept with them

    def find(self) -> Path | None:
        return locate(self.sid, self.projects)

    def read(self) -> list[dict] | None:
        if self.path is None:
            self.path = self.find()
            if self.path is None:
                return None
        try:
            st = self.path.stat()
        except OSError:   # gone: said so, not its last turns left standing
            self.forget()
            return None
        if (st.st_size, st.st_mtime_ns) != self.seen:
            self.seen = (st.st_size, st.st_mtime_ns)
            self.recs = self.load(self.path)
        return self.recs

    def load(self, path: Path) -> list[dict]:
        return tail_records(path, sidechain=self.sidechain)

    def forget(self) -> None:
        self.path = self.seen = self.recs = None
        self.blocks_key = self.blocks = None


class AgentFollower(Follower):
    """One subagent's own transcript (agent_path), followed as a session's is: re-read only
    when it has changed, its tail only, and its prompt once from its start. main: its
    session's transcript, if already known."""
    sidechain = True

    def __init__(self, sid: str, agent_id: str, main: Path | None = None, projects: Path | None = None):
        super().__init__(sid, projects)
        self.agent_id, self.main = agent_id, main
        # its prompt (prompt_record, agent_blocks): read once, as the file only grows
        self.prompt: dict | None = None

    def load(self, path: Path) -> list[dict]:
        """The tail after its prompt: what comes before that isn't the subagent's."""
        if self.prompt is None:
            self.prompt = prompt_record(path)
            if self.prompt is None:
                return []
        recs, uuid = tail_records(path, sidechain=True), self.prompt.get("uuid")
        if prompt_entry(self.prompt) is None:   # no prompt in it at all (it starts mid-way): kept whole
            return recs
        at = next((i for i, r in enumerate(recs) if (r.get("uuid") == uuid if uuid else r == self.prompt)), None)
        return recs if at is None else recs[at + 1:]

    def forget(self) -> None:
        super().forget()
        self.prompt = None

    def find(self) -> Path | None:
        main = self.main or locate(self.sid, self.projects)
        path = main and agent_path(main, self.sid, self.agent_id)
        return path if path and path.exists() else None
