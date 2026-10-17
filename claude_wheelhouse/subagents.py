"""A session's subagents, tracked by the wheelhouse itself as A items (#69), the same way
for SDK and tab sessions: from the files Claude Code writes, not from the session.

- Start: `<session id>/subagents/agent-<agent id>.meta.json` appears beside the transcript
  when Claude Code launches one, with its description, agent type and the id of the
  Agent tool call. Its start is the first record's timestamp in `agent-<agent id>.jsonl`.
- Finish, in the main transcript: a foreground subagent's tool_result (an error is a
  failure); a background one's `<task-notification>` (completed, failed, killed when the
  person stops it, stopped when its session ended under it). A background launch's own
  tool_result ("Async agent launched") finishes nothing. SendMessage to a finished
  subagent resumes it, so its item runs again. A record written again changes nothing:
  a compaction through the Agent SDK appends earlier records, with their uuids.
- Its session dying: the TUI says so (sync's alive), and a subagent still running then
  fails, with a note. A notification read later, when the session is resumed, updates it
  as usual.

Each subagent gets one item, keyed by its agent id in the agent_items table, so restarts
(and a second wheelhouse) never post twice; an A item the session posted for it itself is
taken instead (store.agent_match). Only the session's own subagents count, not theirs.
The cut-off is when the session joined the wheelhouse (launched or adopted), or when the
wheelhouse first ran this code (store.AGENTS_SINCE), whichever is later. A subagent
started after it gets an item. One started before it gets one only if it was still running
then: no finish in the transcript, and its own transcript written to within LIVE of the
cut-off. So joining, or upgrading, brings in what's running and not the history.

Reads are incremental: the subagents folder is listed again only when it changes, and the
main transcript is read from where the last read stopped. A first read starts at the
earliest subagent still running (transcript_usage.window_start), or at the end when none is. See
.plan/wheelhouse-sessions.md ("Subagents").
"""

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import transcript, transcript_usage
from .store import AGENT_MATCH_AFTER, AGENTS_SINCE

# seconds after a subagent starts before its item is made: a session that posts its own A
# item for it (as the protocol asked before version 7) has done so by then, and it's taken
GRACE = AGENT_MATCH_AFTER
RELOCATE = 10   # seconds between looks for a transcript not found yet
# seconds: a subagent started before the cut-off whose transcript was last written longer
# before it than this had finished (or died with its session), and is passed over unread
LIVE = 1800
FIRST_LINE = 1 << 20   # read no further than this for a subagent transcript's first record
FINISH = {"completed": "done", "failed": "failed", "killed": "failed", "stopped": "failed"}
FIELD = re.compile(r"<(task-id|tool-use-id|status|summary)>(.*?)</\1>", re.S)
TASK_ID = re.compile(r"<task-id>(.*?)</task-id>", re.S)
RESULT_ID = re.compile(rb'"tool_use_id":\s*"([^"]+)"')
SEND = re.compile(rb'"name":\s*"SendMessage"')
UUID = re.compile(rb'"uuid":"([^"]+)"')   # as Claude Code writes it, compact
SCAN = 1 << 22   # bytes read at a time, collecting the uuids written before the first read
NOTE_MAX = 500
DIED = "the session stopped while this subagent ran"


@dataclass
class Agent:
    id: str
    tool: str | None   # the Agent tool call's id, None for a forked skill
    title: str
    body: str
    started: datetime
    before: bool = False   # started before the cut-off: an item only if still running
    status: str | None = None   # as the transcript last said: None until it says anything
    note: str = ""
    log: Path | None = None   # its own transcript, whose first prompt may name its item (store.agent_parent)


def when(at) -> datetime | None:
    t = transcript_usage.epoch(at) if isinstance(at, str) else None
    return datetime.fromtimestamp(t, timezone.utc) if t is not None else None


PROMPT_READ = 4000   # characters of a subagent's prompt searched for its item's ref


def first_stamp(path: Path) -> datetime | None:
    try:
        with open(path, "rb") as f:
            return when(json.loads(f.readline(FIRST_LINE)).get("timestamp"))
    except (OSError, ValueError, AttributeError):
        return None


def first_prompt(path: Path | None) -> str:
    """A subagent's prompt, as its transcript has it (transcript.prompt_record): '' if it
    can't be read yet."""
    try:
        rec = path and transcript.prompt_record(path)
    except OSError:
        return ""
    return transcript._text((rec.get("message") or {}).get("content"))[:PROMPT_READ] if rec else ""


def notification(rec: dict) -> str | None:
    """The text of a task notification, from the records Claude Code delivers one in: not
    from a message or tool output that merely quotes one."""
    if rec.get("type") == "queue-operation":
        text = rec.get("content")
    elif rec.get("type") == "attachment":
        text = (rec.get("attachment") or {}).get("prompt")
    elif rec.get("type") == "user":
        text = (rec.get("message") or {}).get("content")
    else:
        return None
    return text if isinstance(text, str) and text.lstrip().startswith("<task-notification>") else None


def fields(text: str) -> tuple[list[str], dict[str, str]]:
    """A notification's task ids, and the first of each other field. Only its header counts:
    the subagent's own text in <result>, and the description quoted in <summary>, can hold
    anything. One notification can name several task ids under one status (subagents its
    session's end stopped)."""
    head = text.split("<result>", 1)[0]
    first: dict[str, str] = {}
    for k, v in FIELD.findall(head):
        first.setdefault(k, v)
    return TASK_ID.findall(head.split("<summary>", 1)[0]), first


def result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, list):
        content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return str(content or "").strip()[:NOTE_MAX]


class AgentWatcher:
    """One session's subagents, followed from its transcripts. sync() runs off the UI
    thread; an idle one stats two paths and reads nothing."""

    def __init__(self, sid: str, projects: Path | None = None):
        self.sid, self.projects = sid, projects
        self.main: Path | None = None
        self.looked = 0.0          # when the transcript was last looked for
        self.cutoff: datetime | None = None
        self.listed: int | None = None    # the subagents folder's mtime when last listed
        self.offset: int | None = None    # the main transcript, read to here
        self.stamp: tuple | None = None   # its (size, mtime) then
        self.base = 0                      # where the first read started
        self.seen: set[str] = set()        # uuids of the records read that may change an item
        self.before: set[bytes] | None = None   # every uuid written before base, once needed
        self.loads = 0                     # times that was read, for the tests
        self.agents: dict[str, Agent] = {}   # the ones tracked: started after the cut-off
        self.by_tool: dict[str, str] = {}    # Agent tool call id: agent id
        self.skipped: set[str] = set()       # finished before the cut-off, or a subagent's subagent
        self.links: dict[str, str] | None = None   # agent id: its item's status, as last written
        self.syncing = False   # a sync is in flight (set and cleared by the app)
        self.ready = False     # a first sync has finished
        self.error: str | None = None   # the last sync's failure

    def sync(self, store, now: float | None = None, alive: bool = True) -> bool:
        """Reads what's new and brings the session's A items up to date. True if it
        changed any. Not alive: the session has died, so what's still running fails."""
        now = time.time() if now is None else now
        if self.cutoff is None:
            s = store.session(self.sid)
            if s is None:
                return False
            joined = when(s["created_at"])
            self.cutoff = max(joined, when(store.setting(AGENTS_SINCE)) or joined)
        if self.main is None:
            if now - self.looked < RELOCATE:
                return False
            self.looked = now
            self.main = transcript.locate(self.sid, self.projects)
            if self.main is None:
                return False
        if self.links is None:
            self.links = {aid: status for aid, (_, status) in store.agent_links(self.sid).items()}
        self.list_agents()
        self.read_main()
        self.ready = True
        changed = self.apply(store, now, alive)
        if not alive:
            changed = self.died(store) or changed
        return changed

    def unfinished(self) -> bool:
        """An item it made is still running, as last written."""
        return "running" in (self.links or {}).values()

    def died(self, store) -> bool:
        """The session is dead: each subagent item still running fails, saying so."""
        gone = [aid for aid, status in (self.links or {}).items() if status == "running"]
        for aid in gone:
            store.agent_status(self.sid, aid, "failed", DIED)
            self.links[aid] = "failed"
            if aid in self.agents:
                self.agents[aid].status, self.agents[aid].note = "failed", DIED
        return bool(gone)

    def list_agents(self) -> None:
        folder = self.main.parent / self.sid / "subagents"
        try:
            mtime = folder.stat().st_mtime_ns
        except OSError:
            return
        if mtime == self.listed:
            return
        self.listed = mtime
        for meta in folder.glob("agent-*.meta.json"):
            aid = meta.name.removeprefix("agent-").removesuffix(".meta.json")
            if aid not in self.agents and aid not in self.skipped:
                self.consider(meta, aid)

    def consider(self, meta: Path, aid: str) -> None:
        log = meta.with_name(f"agent-{aid}.jsonl")
        try:
            launched = meta.stat().st_mtime   # written at launch, and again if it's stopped
            try:
                active = log.stat().st_mtime
            except OSError:   # not started writing yet
                active = launched
            if max(active, launched) < self.cutoff.timestamp() - LIVE:
                self.skipped.add(aid)
                return
            d = json.loads(meta.read_bytes())
        except (OSError, ValueError):
            self.listed = None   # half-written, say: listed again next time
            return
        if d.get("parentAgentId") or (d.get("spawnDepth") or 1) > 1:
            self.skipped.add(aid)
            return
        started = first_stamp(log) or datetime.fromtimestamp(launched, timezone.utc)
        kind = d.get("agentType") or "subagent"
        how = {"background": ", in the background", "foreground": ", in the foreground"}.get(d.get("requestShape"), "")
        self.agents[aid] = Agent(aid, d.get("toolUseId"), (d.get("description") or kind)[:200],
                                 f"A `{kind}` subagent{how}. The wheelhouse tracks it from the session's transcript.",
                                 started, before=started < self.cutoff, log=log)
        if d.get("toolUseId"):
            self.by_tool[d["toolUseId"]] = aid

    def read_main(self) -> None:
        try:
            st = self.main.stat()
        except OSError:
            return
        if (st.st_size, st.st_mtime_ns) == self.stamp:
            return
        with open(self.main, "rb") as f:
            if self.offset is None or st.st_size < self.offset:
                running = [a.started.timestamp() for aid, a in self.agents.items()
                           if self.links.get(aid, "running") == "running"]
                self.offset = transcript_usage.window_start(f, st.st_size, min(running)) if running else st.st_size
                self.base, self.seen, self.before = self.offset, set(), None
            f.seek(self.offset)
            data = f.read(st.st_size - self.offset)
        end = data.rfind(b"\n") + 1   # a half-written last line waits for the next read
        self.offset, self.stamp = self.offset + end, (st.st_size, st.st_mtime_ns)
        for line in data[:end].splitlines():
            try:
                self.take(line)
            except (ValueError, AttributeError, TypeError):
                continue

    def take(self, line: bytes) -> None:
        """One transcript record: parsed only when its bytes say it may matter."""
        if b"<task-notification>" in line:
            rec = json.loads(line)
            text = notification(rec)
            if text:
                ids, first = fields(text)
                status = FINISH.get(first.get("status"))
                aids = [i for i in ids if i in self.agents] or [self.by_tool.get(first.get("tool-use-id"))]
                aids = [aid for aid in aids if aid and status]
                if not self.replayed(rec, acts=bool(aids)):
                    for aid in aids:
                        self.set(aid, status, "" if status == "done" else first.get("summary", ""))
                return
        if b'"tool_result"' in line and any(i.decode() in self.by_tool for i in RESULT_ID.findall(line)):
            rec = json.loads(line)
            result = rec.get("toolUseResult")
            finished = []
            for block in (rec.get("message") or {}).get("content") or []:
                aid = self.by_tool.get(block.get("tool_use_id")) if isinstance(block, dict) else None
                if aid is None or block.get("type") != "tool_result":
                    continue
                if (result.get("isAsync") or result.get("status") == "async_launched") if isinstance(result, dict) \
                        else result_text(block).startswith("Async agent launched"):
                    continue   # a background launch: it finishes with a notification
                failed = bool(block.get("is_error"))
                finished.append((aid, "failed" if failed else "done", result_text(block) if failed else ""))
            if not self.replayed(rec, acts=bool(finished)):
                for aid, status, note in finished:
                    self.set(aid, status, note)
        elif SEND.search(line):
            rec = json.loads(line)
            if rec.get("type") != "assistant":
                return
            sent = [(block.get("input") or {}).get("to") for block in (rec.get("message") or {}).get("content") or []
                    if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "SendMessage"]
            if not sent:
                return
            # finished as the transcript said, or as the item was written before a restart
            resumed = [aid for aid in sent if isinstance(aid, str) and aid in self.agents
                       and (self.agents[aid].status or self.links.get(aid)) not in (None, "running")]
            if not self.replayed(rec, acts=bool(resumed)):
                for aid in resumed:
                    self.set(aid, "running", "")

    def replayed(self, rec: dict, acts: bool = True) -> bool:
        """A record written again, not new: a compaction run through the Agent SDK resumes
        the session, and Claude Code appends records it had already written, uuid and old
        timestamp included. So a uuid already read is a replay, and so is one written before
        where the first read started: those uuids are read once, the first time a record
        would change an item (`acts`). Not timestamps: a first read can start inside a run of
        replayed records, newer ones after it."""
        uid = rec.get("uuid")
        if not isinstance(uid, str):
            return False
        if uid in self.seen:
            return True
        self.seen.add(uid)
        if not (acts and self.base):
            return False
        if self.before is None:
            try:
                self.before = self.uuids_before()
            except OSError:
                return False   # looked for again next time
        return uid.encode() in self.before

    def uuids_before(self) -> set[bytes]:
        """Every uuid written before where the first read started, read once, in chunks
        cut at a line's end (a uuid never spans lines): about 100 to 140 ms for a 50 MB transcript."""
        self.loads += 1
        found: set[bytes] = set()
        with open(self.main, "rb") as f:
            pos, carry = 0, b""
            while pos < self.base:
                chunk = f.read(min(SCAN, self.base - pos))
                if not chunk:
                    break
                pos += len(chunk)
                buf = carry + chunk
                cut = buf.rfind(b"\n") + 1 if pos < self.base else len(buf)
                found.update(UUID.findall(buf, 0, cut))
                carry = buf[cut:]
        return found

    def set(self, aid: str, status: str, note: str) -> None:
        a = self.agents[aid]
        a.status, a.note = status, (note or "").strip()[:NOTE_MAX]

    def apply(self, store, now: float, alive: bool = True) -> bool:
        """Each subagent's item made (after GRACE, unless it has already finished or its
        session has died), and its status written when the transcript has changed it. One
        from before the cut-off that the transcript says has finished is dropped: the first
        read has seen everything since it started."""
        changed = False
        for aid, a in list(self.agents.items()):
            had = self.links.get(aid)
            if had is None and a.before and a.status not in (None, "running"):
                del self.agents[aid]
                self.skipped.add(aid)
                continue
            if had is None:
                if alive and a.status in (None, "running") and now - a.started.timestamp() < GRACE:
                    continue
                # another wheelhouse may have made it: then its status as now stored
                self.links[aid], changed = store.track_agent(self.sid, aid, a.title, a.body, a.started,
                                                             a.status, a.note, prompt=first_prompt(a.log)), True
            elif a.status is None or a.status == had:
                continue
            else:
                store.agent_status(self.sid, aid, a.status, a.note)
                self.links[aid], changed = a.status, True
        return changed
