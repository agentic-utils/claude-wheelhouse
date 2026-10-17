"""A session's token use, read from its Claude Code transcripts: how big its context is,
whether its prompt cache is still warm, its compactions, and its turns, followed as the
transcript grows. The hub reads it for the session list's context bars and cache warmth,
and the stats module (modules/stats) draws its pane from it, through api.py's re-exports.

The usage parse is claude-dashboard's, ported. The context grades are the wheelhouse's own,
shared with the session list's context bar (#51). See .plan/session-stats.md and
.plan/modules.md ("Stats, carved out").
"""

import dataclasses
import itertools
import json
import math
import os
import re
import threading
import time
import weakref
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from rich.text import Text

from . import models, transcript
from .store import runner

HOUR, DAY, WEEK = 3600, 86400, 7 * 86400
SPAN = 2 * HOUR                    # the charts' default, and what the followers keep turn by turn
BUCKET = 300                       # older history is kept as these sums, not turns (UsageFollower.load)
TTL = {"1h": 3600, "5m": 300}
PARTIAL = " ▁▂▃▄▅▆▇█"              # 0..8 eighths of a cell, bottom up
OK, WARN, AMBER, HOT = (52, 224, 150), (255, 205, 82), (255, 138, 56), (255, 88, 96)
# the charts: title, and the series stacked in each column with their legend labels
CHARTS = {"assembly": ("context assembly", (("read", "cache"), ("new", "new"), ("miss", "miss"))),
          "output": ("output", (("output", "output tokens"),))}
SERIES = tuple(k for _, series in CHARTS.values() for k, _ in series)


def epoch(at: str) -> float | None:
    try:
        return datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


@dataclass
class Turn:
    """One API response: when, what it cost to assemble, and (main thread only) its size."""
    at: float
    read: int
    new: int      # written to cache, or uncached, on top of a cache read
    miss: int     # the same with nothing read: the cache was cold
    context: int
    ttl: str | None   # the cache it wrote, if any
    model: str | None
    main: bool
    output: int = 0
    effort: str | None = None   # the effort it ran at, as Claude Code recorded it
    requested: str | None = None   # the model asked for (a /model's, say), as Claude Code recorded it
    start: float | None = None   # when its turn began (parse): the time its requested model and effort count from
    stop: str | None = None   # why it stopped (stop_reason): tool_use while its turn runs on


@dataclass
class Compaction:
    at: float
    trigger: str
    pre: int
    post: int     # Claude Code's postTokens: the messages kept, without the system prompt and tools
    after: int | None = None   # the next main-thread response's context: the size it left. 0 if unread


@dataclass
class Setting:
    """A /model or /effort typed in Claude Code and taken: what it set, the other None. Or,
    with neither, a prompt: where a main-thread turn began."""
    at: float
    model: str | None = None   # the table's pinned id
    effort: str | None = None


# what Claude Code 2.1 writes as a local command's output when it takes the change: a
# model as it was named (table_model), an effort level
SET_MODEL = re.compile(r"Set model to `([^`]+)`")
SET_EFFORT = re.compile(r"Set effort level to (\w+)")
NAMED = re.compile(r"(.*?)\s*(?:\((.*)\))?")   # a model named, then anything in brackets


def table_model(named: str) -> str | None:
    """A model as Claude Code 2.1 names one it set, as the table's pinned id: by display
    name (Haiku 5.5, Opus 5.5 (1M context), Opus 5.5 (default), Sonnet 5.5[1m]), by id
    (claude-sonnet-5-5, claude-opus-5-5[1m]), or by an alias with its id in brackets (haiku
    (claude-haiku-5-5)). None for one off the table."""
    m = NAMED.fullmatch(named.strip())
    for part in (m[1], m[2]) if m else ():
        part = (part or "").strip()
        if (pinned := models.pinned(part)) in models.BY_ID:
            return pinned
        if found := next((x.id for x in models.MODELS if x.name == part.split("[")[0]), None):
            return found
    return None


STDOUT = "<local-command-stdout>"   # how a local command's output starts
SET_MARKS = (b"local_command", f"{STDOUT}Set ".encode())   # in each line setting may take


def may_set(line: bytes) -> bool:
    """A line that may hold a /model or /effort Claude Code took (setting), judged from its
    bytes: for scanning."""
    return any(mark in line for mark in SET_MARKS)


def setting(rec: dict, at: float) -> Setting | None:
    """A local command's record as the change it made, if it was a /model or /effort Claude
    Code took: one it refused, or a model off the table, is None. Claude Code 2.1 writes
    two shapes: a system record (subtype local_command) naming the command it ran, for one
    typed with its argument; and, for a choice made in /model's picker, a user record with
    no command named, its content only the command's output (local-command-stdout)."""
    if rec.get("type") == "system" and rec.get("subtype") == "local_command":
        command = (rec.get("commandRun") or {}).get("command")
        text = rec.get("content") if isinstance(rec.get("content"), str) else ""
    elif rec.get("type") == "user" and not rec.get("isMeta"):
        text = (rec.get("message") or {}).get("content")
        if not isinstance(text, str) or not text.startswith(STDOUT):
            return None
        command = "model" if text.startswith(f"{STDOUT}Set model to") else \
            "effort" if text.startswith(f"{STDOUT}Set effort level to") else None
    else:
        return None
    if command == "model" and (m := SET_MODEL.search(text)):
        model = table_model(m[1])
        return Setting(at, model=model) if model else None
    if command == "effort" and (m := SET_EFFORT.search(text)) and m[1] in models.EFFORTS:
        return Setting(at, effort=m[1])
    return None


USER = re.compile(rb'"type":\s*"user"')


def is_prompt(rec: dict) -> bool:
    """A main-thread user record that began a turn: Claude Code 2.1 marks each with its
    turnPosition; before that, one typed (text, not a tool's result or a command's record)."""
    if rec.get("type") != "user" or rec.get("isSidechain") or rec.get("isMeta") or rec.get("isCompactSummary"):
        return False
    content = (rec.get("message") or {}).get("content")
    return "turnPosition" in rec or (isinstance(content, str) and not content.startswith("<"))


def parse(lines, seen: set, main: bool, settings: list | None = None,
          start: float | None = None) -> tuple[list[Turn], list[Compaction]]:
    """The turns and compactions in some transcript lines. One response is written as
    several lines sharing a message id: `seen` counts each once, across reads. Given a
    list, the /model and /effort changes in them (setting) are added to it, and each prompt
    as a Setting of neither; each main-thread turn is dated by when it began (Turn.start):
    its prompt, else `start` (a turn already under way), else its first response read."""
    turns, compactions = [], []
    for line in lines:
        if (b'"usage"' not in line and b"compact_boundary" not in line
                and (settings is None or (not may_set(line) and not (
                    b'"turnPosition"' in line or (USER.search(line) and b"tool_result" not in line))))):
            continue   # most lines: no need to parse them
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or (at := epoch(rec.get("timestamp", ""))) is None:
            continue
        if settings is not None and rec.get("type") == "system" and rec.get("subtype") == "local_command":
            if main and not rec.get("isSidechain") and (found := setting(rec, at)):
                settings.append(found)
            continue
        if settings is not None and rec.get("type") == "user":
            if main and not rec.get("isSidechain") and (found := setting(rec, at)):
                settings.append(found)
            elif main and is_prompt(rec):
                start = at
                settings.append(Setting(at))
            continue
        if rec.get("type") == "system" and rec.get("subtype") == "compact_boundary":
            meta = rec.get("compactMetadata") or {}
            compactions.append(Compaction(at, meta.get("trigger", "?"), meta.get("preTokens", 0) or 0,
                                          meta.get("postTokens", 0) or 0))
            continue
        msg = rec.get("message") or {}
        usage = msg.get("usage") if rec.get("type") == "assistant" else None
        if not usage:
            continue
        inp = usage.get("input_tokens", 0) or 0
        made = usage.get("cache_creation_input_tokens", 0) or 0
        read = usage.get("cache_read_input_tokens", 0) or 0
        if msg.get("model") == "<synthetic>" or not inp + made + read:
            continue   # Claude Code's own stand-in replies (errors, interrupts): no request was made
        key = msg.get("id") or rec.get("requestId")
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        cc = usage.get("cache_creation") or {}
        ttl = "1h" if cc.get("ephemeral_1h_input_tokens") else "5m" if cc.get("ephemeral_5m_input_tokens") else None
        fresh = inp + made
        on_main = main and not rec.get("isSidechain")
        if on_main and settings is not None and start is None:
            start = at   # its prompt unread: from its first response read
        turns.append(Turn(at, read, fresh if read else 0, 0 if read else fresh, inp + made + read, ttl,
                          msg.get("model"), on_main, usage.get("output_tokens", 0) or 0,
                          rec.get("effort") if isinstance(rec.get("effort"), str) else None,
                          rec.get("requestedModel") if isinstance(rec.get("requestedModel"), str) else None,
                          start if on_main else None,
                          msg.get("stop_reason") if isinstance(msg.get("stop_reason"), str) else None))
    return turns, compactions


# context windows and grades

def max_window(model: str | None) -> int:
    """Opus and Sonnet are graded against 1M: on older ones the 1M context was a request
    header, stripped from the logged model id. Fable, Mythos and Haiku 5.5 have it by
    default; older Haiku has 200k."""
    m = (model or "").lower()
    return 1_000_000 if any(k in m for k in ("opus", "sonnet", "fable", "mythos", "haiku-5")) else 200_000


def window_for(model: str | None, peak: int) -> int:
    return 1_000_000 if peak > 200_000 else max_window(model)


GRADES = ((150_000, OK), (300_000, WARN), (600_000, AMBER))   # below each: its colour; red above (#51)
STEP = 125_000                                                 # the session list's bar: an eighth each, linear


def hexc(c: tuple) -> str:
    return "#%02x%02x%02x" % c


def grade(size: int) -> tuple:
    """A context size's colour, the same in the stats pane and the session list: green
    under 150k, yellow under 300k, amber under 600k, red above."""
    return next((colour for limit, colour in GRADES if size < limit), HOT)


def context_bar(size: int) -> Text:
    """A context size in the session list: one cell in its grade's colour, filled in
    eighths on a linear scale, 125k each (#51). An eighth shows as soon as any of its 125k
    is used, so any context at all shows one, and the last eighth begins past 875k: the cell
    is full from there, however far past 1M it goes."""
    return Text(PARTIAL[min(math.ceil(size / STEP), 8)], style=hexc(grade(size)))


@dataclass
class Snapshot:
    """What the pane shows for one session."""
    model: str | None = None
    context: int = 0
    peak: int = 0
    last_at: float | None = None   # the main thread's latest response
    ttl: str | None = None         # the cache its latest writing response used
    compactions: list[Compaction] = field(default_factory=list)
    turns: list[Turn] = field(default_factory=list)   # the span's, main thread and subagents
    estimate: bool = False         # the context is the host's count since a compaction (current)
    max: int | None = None         # the host's window, with it
    effort: str | None = None      # the main thread's latest response's
    requested: str | None = None   # the model the main thread's latest response asked for
    begun: float | None = None     # when the main thread's latest turn began (parse's start)
    stop: str | None = None        # why the main thread's latest response stopped: tool_use, mid-turn
    # the latest model (the table's pinned id) and effort, each as (when, value): typed, a
    # /model or /effort Claude Code took; asked, what a main-thread turn ran on, from when it began
    typed: dict = field(default_factory=dict)
    asked: dict = field(default_factory=dict)
    history: "History" = field(default_factory=lambda: History())   # older than the span: BUCKET sums (fold)

    @property
    def window(self) -> int:
        return self.max or window_for(self.model, self.peak)

    @property
    def expires(self) -> float | None:
        return self.last_at + TTL[self.ttl] if self.last_at and self.ttl else None

    def warm(self, now: float) -> bool:
        return bool(self.expires and now < self.expires)


CHUNK = 4 << 20                    # bytes a first read takes at a time
RETRY = 60                         # seconds before a failed load of older history is tried again
REGLOB = 10                        # seconds between re-listing the subagent files (a worker's job, off the UI)
STAMP = re.compile(rb'"timestamp":\s*"([^"]+)"')
MAIN_REPLY = re.compile(rb'"type":\s*"assistant"')


def is_main_reply(line: bytes) -> bool:
    """A main-thread response with real usage, judged from its bytes: for scanning."""
    return (b'"usage"' in line and MAIN_REPLY.search(line) is not None and b"<synthetic>" not in line
            and re.search(rb'"isSidechain":\s*true', line) is None)


def window_start(f, size: int, cutoff: float) -> int:
    """Where a first read of a main transcript starts: the first line at or after `cutoff`,
    scanning back from the end, but never past the latest main-thread response, which the
    pane needs even when it's older than the span."""
    pos, carry, main = size, b"", False
    while pos > 0:
        step = min(CHUNK, pos)
        pos -= step
        f.seek(pos)
        buf = f.read(step) + carry
        end = len(buf)
        while end > 0:
            start = buf.rfind(b"\n", 0, end - 1) + 1
            if start == 0 and pos > 0:
                carry = buf[:end]   # cut by the chunk boundary: finished with the next chunk
                break
            line = buf[start:end]
            m = STAMP.search(line)
            at = epoch(m[1].decode()) if m else None
            if at is not None and at < cutoff:
                if main:
                    return pos + end
                if is_main_reply(line):
                    return pos + start
            main = main or is_main_reply(line)
            end = start
        else:
            carry = b""
    return 0


def last_prompt(f, end: int) -> float | None:
    """When the last main-thread prompt (is_prompt) in the first `end` bytes was typed,
    scanning back from there: where the turn under way at `end` began."""
    pos, carry = end, b""
    while pos > 0:
        step = min(CHUNK, pos)
        pos -= step
        f.seek(pos)
        lines = (f.read(step) + carry).split(b"\n")
        carry = lines.pop(0) if pos > 0 else b""   # cut by the chunk boundary: finished with the next chunk
        for line in reversed(lines):
            if not (b'"turnPosition"' in line or (USER.search(line) and b"tool_result" not in line)):
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and is_prompt(rec) and (at := epoch(rec.get("timestamp", ""))) is not None:
                return at
    return None


def before(f, end: int) -> tuple[list[Compaction], list[Setting], float | None]:
    """The compactions, and each /model and /effort Claude Code took (setting), in the first
    `end` bytes, found without parsing the rest: a tab's /model then /exit long before the
    wheelhouse opened is carried as surely as one typed in the span. And when the last
    prompt among them was typed (last_prompt): a turn begun before the span began then."""
    compactions, settings, pos, carry = [], [], 0, b""
    f.seek(0)
    while pos < end:
        n = min(CHUNK, end - pos)
        buf, pos = carry + f.read(n), pos + n
        cut = buf.rfind(b"\n") + 1   # whole lines only: the rest is carried to the next chunk
        carry, starts = buf[cut:], set()
        for marker in (b"compact_boundary", *SET_MARKS):
            i = buf.find(marker, 0, cut)
            while i >= 0:
                starts.add(buf.rfind(b"\n", 0, i) + 1)
                i = buf.find(marker, buf.find(b"\n", i) + 1, cut)
        for a in sorted(starts):
            compactions += parse([buf[a:buf.find(b"\n", a)]], set(), True, settings)[1]
    return compactions, [x for x in settings if x.model or x.effort], last_prompt(f, end)


def earlier(f, end: int, cutoff: float, take) -> int:
    """The lines before `end` (a line's start), read backwards and given to `take` a chunk at
    a time, back to the first older than `cutoff` and on to the first that isn't a response's,
    so a response written as several lines is never split between two loads. Where the lines
    left unread end: 0 once the file's start is reached."""
    pos, carry, older = end, b"", False
    while pos > 0:
        step = min(CHUNK, pos)
        pos -= step
        f.seek(pos)
        buf, lines = f.read(step) + carry, []
        stop = len(buf)
        while stop > 0:
            start = buf.rfind(b"\n", 0, stop - 1) + 1
            if start == 0 and pos > 0:
                carry = buf[:stop]   # cut by the chunk boundary: finished with the next chunk
                break
            line = buf[start:stop]
            if not older and (m := STAMP.search(line)) and (at := epoch(m[1].decode())) is not None:
                older = at < cutoff
            if older and MAIN_REPLY.search(line) is None:
                take(lines)
                return pos + stop
            lines.append(line)
            stop = start
        else:
            carry = b""
        take(lines)
    return 0


class History(dict):
    """Turns older than the span, as sums: each BUCKET's start, to its turns' SERIES in order.
    Never changed once made (fold makes another), so its serial stands for what it holds in
    the charts' caches."""
    serials = itertools.count()

    def __init__(self, *args):
        super().__init__(*args)
        self.serial = next(History.serials)


def add(sums: dict, turns) -> dict:
    """The turns added into BUCKET sums, in place."""
    for t in turns:
        b = int(t.at // BUCKET * BUCKET)
        old = sums.get(b)
        new = tuple(getattr(t, k) for k in SERIES)
        sums[b] = tuple(map(sum, zip(old, new))) if old else new
    return sums


def fold(history: History, turns, sums: dict | None = None) -> History:
    """A new history: this one with the turns and other sums added."""
    out = add(History(history), turns)
    for b, new in (sums or {}).items():
        old = out.get(b)
        out[b] = tuple(map(sum, zip(old, new))) if old else new
    return out


class UsageFollower:
    """One session's usage, read incrementally: each file's new bytes, when its size or
    modification time has changed. The main transcript, and its subagents' beside it.

    read() runs off the UI thread and swaps in a new snapshot when it's done, so the pane
    can show `snap` at any time. A first read of a big transcript starts at the span
    (window_start); only its compactions and what was typed (/model, /effort) are taken
    from before that, in one scan, once per follower."""

    def __init__(self, sid: str, projects: Path | None = None):
        self.sid, self.projects = sid, projects
        self.main: Path | None = None
        self.files: dict[Path, tuple[int, tuple]] = {}   # path: (offset read to, (size, mtime))
        self.seen: set = set()
        self.snap = Snapshot()
        self.ready = False      # a first read has finished
        self.error: str | None = None   # the last read's failure, shown dimly in the pane
        self.reading = False    # a read is in flight (set and cleared by the app)
        self.subagents: list[Path] = []
        self.listed: tuple | None = None   # (the subagents folder's mtime, when listed)
        self.dormant: set[Path] = set()    # read to the end and older than the span
        # the history before what the first read took, read only when a chart zooms out past
        # it (load): where the main transcript's unread start ends (0 once it's all read), the
        # subagents' files it skipped as finished before the span, and the time from which
        # every turn is in the snapshot, as turns or as history
        self.lock = threading.Lock()       # a read and a load take turns at the snapshot
        self.back = 0
        self.skipped: list[Path] = []
        self.since = -math.inf
        self.oldest: float | None = None   # the oldest turn read, whenever it was
        self.loading = False               # a load is in flight (set and cleared by the app)
        self.failed: float | None = None   # when the last load failed: none is tried again until RETRY after
        self.history_error: str | None = None   # why, shown until a load succeeds

    @property
    def done(self) -> bool:
        """Every turn the transcripts hold has been read."""
        return self.ready and not self.back and not self.skipped

    def short(self, cutoff: float) -> bool:
        """Whether some turns from `cutoff` on are still unread (load): the main transcript's,
        or, with it all read, the subagents' its first read skipped."""
        return self.ready and not self.done and (self.since > cutoff or not self.back)

    def resting(self, now: float) -> bool:
        """A load failed less than RETRY ago: not tried again yet, nor shown as loading."""
        return self.failed is not None and now - self.failed < RETRY

    def paths(self, now: float) -> list[Path]:
        """The main transcript, and the subagents' that may still grow. The folder is
        re-listed when it changes, or after REGLOB; finished files are skipped until then."""
        if self.main is None:
            self.main = transcript.locate(self.sid, self.projects)
            if self.main is None:
                return []
        folder = self.main.parent / self.sid / "subagents"
        try:
            mtime = folder.stat().st_mtime_ns
        except OSError:
            mtime = None
        if self.listed is None or self.listed[0] != mtime or now - self.listed[1] >= REGLOB:
            self.subagents = sorted(folder.glob("agent-*.jsonl")) if mtime is not None else []
            self.listed, self.dormant = (mtime, now), set()
        return [self.main, *(p for p in self.subagents if p not in self.dormant)]

    def read(self, now: float | None = None) -> bool:
        """Takes in what's been appended since the last read. True if anything was. Turns
        older than the span are folded into the history."""
        with self.lock:
            return self._read(time.time() if now is None else now)

    def _read(self, now: float) -> bool:
        snap = Snapshot(self.snap.model, self.snap.context, self.snap.peak, self.snap.last_at, self.snap.ttl,
                        list(self.snap.compactions), list(self.snap.turns), effort=self.snap.effort,
                        requested=self.snap.requested, begun=self.snap.begun, stop=self.snap.stop,
                        typed=dict(self.snap.typed),
                        asked=dict(self.snap.asked), history=self.snap.history)
        changed = False
        for path in self.paths(now):
            try:
                st = path.stat()
            except OSError:
                continue
            offset, stamp = self.files.get(path, (0, None))
            if (st.st_size, st.st_mtime_ns) == stamp:
                if path != self.main and st.st_mtime < now - SPAN:
                    self.dormant.add(path)
                continue
            if path != self.main and offset == 0 and st.st_mtime < now - SPAN:
                self.files[path] = (st.st_size, (st.st_size, st.st_mtime_ns))   # finished before the span
                self.dormant.add(path)
                self.skipped.append(path)
                continue
            with open(path, "rb") as f:
                if path == self.main and offset == 0:
                    offset = window_start(f, st.st_size, now - SPAN)
                    self.back, self.since = offset, now - SPAN if offset else -math.inf
                    early, typed, prompt = before(f, offset)
                    if prompt is not None:   # the turn under way at the span began then, not at its first response read
                        snap.begun = max(snap.begun or prompt, prompt)
                    early = [dataclasses.replace(c, after=0) for c in early]
                    snap.compactions += early
                    snap.peak = max([snap.peak, *(c.pre for c in early)])
                    changed = chose(snap, typed) or changed or bool(early)
                f.seek(offset)
                data = f.read(st.st_size - offset)
            end = data.rfind(b"\n") + 1   # a half-written last line waits for the next read
            self.files[path] = (offset + end, (st.st_size, st.st_mtime_ns))
            settings = []
            turns, compactions = parse(data[:end].splitlines(), self.seen, path == self.main, settings,
                                       snap.begun if path == self.main else None)
            take(snap, turns, compactions)
            self.saw(turns)
            changed = chose(snap, settings) or changed
            changed = changed or bool(turns or compactions)
        if old := [t for t in snap.turns if t.at < now - SPAN]:
            snap.history = fold(snap.history, old)
            snap.turns = [t for t in snap.turns if t.at >= now - SPAN]
        self.snap, self.ready = snap, True
        return changed

    def saw(self, turns: list[Turn]) -> None:
        if turns:
            self.oldest = min(self.oldest or math.inf, min(t.at for t in turns))

    def load(self, cutoff: float) -> bool:
        """On a worker thread, after the first read: the turns from `cutoff` on that it left
        unread, taken into the history as BUCKET sums. The main transcript is read backwards
        from where the first read started (earlier); once it's read to its start, the
        subagents' files the first read skipped are read whole, else those that changed since
        `cutoff`. Reads and the snapshot carry on meanwhile. True if it brought any."""
        with self.lock:
            back, skipped, seen = self.back, list(self.skipped), set(self.seen)
            ends = {p: self.files[p][0] for p in skipped}
        sums: dict = {}
        found: list[float] = []

        def taken(lines: list[bytes], main: bool) -> None:
            turns = parse(lines, seen, main)[0]
            add(sums, turns)
            found.extend([min(t.at for t in turns)] if turns else [])

        if back and self.main is not None:
            with open(self.main, "rb") as f:
                back = earlier(f, back, cutoff, lambda lines: taken(lines, True))
        if not back:
            cutoff = -math.inf   # the main transcript read to its start: every subagent's too
        rest = []
        for path in skipped:
            try:
                if path.stat().st_mtime < cutoff:
                    rest.append(path)
                    continue
                with open(path, "rb") as f:
                    data = f.read(ends[path])
            except OSError:
                continue   # gone: nothing more to read of it
            taken(data[:data.rfind(b"\n") + 1].splitlines(), False)
        with self.lock:
            self.back, self.skipped, self.since = back, rest, min(self.since, cutoff)
            if sums:
                self.snap = dataclasses.replace(self.snap, history=fold(self.snap.history, (), sums))
                self.oldest = min(self.oldest or math.inf, *found)
        return bool(sums)

    def take(self, turns: list[Turn], compactions: list[Compaction]) -> None:
        take(self.snap, turns, compactions)


_FOLLOWERS: "weakref.WeakValueDictionary[str, UsageFollower]" = weakref.WeakValueDictionary()


def follower(sid: str) -> UsageFollower:
    """The session's follower, shared: the session list's context bars and the stats pane
    read each transcript once between them, not once each (#51). Each keeps the ones it
    shows; a follower neither holds any more is dropped. Called on the UI thread only."""
    f = _FOLLOWERS.get(sid)
    if f is None:
        f = _FOLLOWERS[sid] = UsageFollower(sid)
    return f


def read_safely(f: UsageFollower, now: float | None = None) -> bool:
    """On a worker thread: one read, by whichever of the follower's users started it. A
    failed read (a transcript gone between stat and open, say) keeps the last snapshot and
    says why, for the pane to show dimly. True if it brought anything, or failed."""
    try:
        changed = f.read(now)
        f.error = None
    except Exception as e:
        changed, f.error = True, f"couldn't read the transcript: {e}"[:120]
    finally:
        f.reading = False
    return changed


def load_safely(f: UsageFollower, cutoff: float) -> None:
    """On a worker thread: one load of older history (UsageFollower.load), as read_safely."""
    try:
        f.load(cutoff)
        f.failed = f.history_error = None
    except Exception as e:
        f.failed, f.history_error = time.time(), f"couldn't read the transcript's history: {e}"[:120]
    finally:
        f.loading = False


def take(s: Snapshot, turns: list[Turn], compactions: list[Compaction]) -> None:
    s.compactions += compactions
    for t in turns:
        s.turns.append(t)
        if t.main:
            s.model, s.context, s.peak, s.last_at = t.model or s.model, t.context, max(s.peak, t.context), t.at
            s.stop = t.stop
            s.effort, s.requested = t.effort or s.effort, t.requested or s.requested
            begun = t.at if t.start is None else t.start
            s.begun = max(s.begun or begun, begun)
            if model := models.ran_as(t.requested, t.model):
                s.asked["model"] = (begun, model)
            if t.effort:
                s.asked["effort"] = (begun, t.effort)
            s.ttl = t.ttl or s.ttl
            c = s.compactions[-1] if s.compactions else None
            if c and c.after is None and c.at <= t.at:
                s.compactions[-1] = dataclasses.replace(c, after=t.context)


def chose(s: Snapshot, settings: list[Setting]) -> bool:
    """The /model, /effort and prompts read (parse), taken in: each typed is the latest
    typed of its kind, and the pane shows it over a turn that began before it (one typed
    while a turn streams doesn't change that turn: its later responses still say the old,
    and the next turn will say the new). Whether any was typed."""
    for x in settings:
        if x.model is None and x.effort is None:
            s.begun = max(s.begun or x.at, x.at)
        for key in ("model", "effort"):
            if getattr(x, key):
                s.typed[key] = (x.at, getattr(x, key))
    for key, attr in (("model", "requested"), ("effort", "effort")):
        typed, asked = s.typed.get(key), s.asked.get(key)
        if typed and (asked is None or typed[0] >= asked[0]):
            setattr(s, attr, typed[1])
    return any(x.model or x.effort for x in settings)


def latest(snap: Snapshot, key: str, turns: bool = True) -> tuple[float, str] | None:
    """The latest model or effort the transcript shows chosen, as (when, value): typed, or
    (with turns) a turn's, from when that turn began. Typed wins a tie."""
    found = [x for x in (snap.typed.get(key), snap.asked.get(key) if turns else None) if x]
    return max(found, key=lambda x: x[0]) if found else None


def carry(store, sid: str, snap: Snapshot) -> None:
    """A /model or /effort typed in a tab, carried to the session's columns, so its next
    launch keeps it (Q56): last write wins, by time. Where the transcript's latest choice is
    newer than the column's (its *_chosen_at) and differs, the column takes it, dated then
    (Store.carry). A choice made in the wheelhouse later stands, and one made earlier gives
    way. A tab session's turns count as a choice from when each began, as nothing but the
    tab chooses for it; a hosted session's only what was typed, as its turns run what the
    wheelhouse chose (its host, or its shell tab's launch)."""
    s = store.session(sid)
    if s is None:
        return
    tab = runner(s) == "tab"
    for key in ("model", "effort"):
        if found := latest(snap, key, turns=tab):
            store.carry(sid, key, found[1], found[0])


def typed_in(path: Path | None) -> Snapshot:
    """What a whole transcript shows typed (/model, /effort), for a host taking a session
    back: only the records that may say so are parsed (before)."""
    snap = Snapshot()
    if path is None:
        return snap
    with open(path, "rb") as f:
        chose(snap, before(f, os.fstat(f.fileno()).st_size)[1])
    return snap


def current(snap: Snapshot, host) -> Snapshot:
    """The snapshot with the SDK host's context size in it (an api.HostContext, None for a
    tab session) when the transcript can't size it: a compaction since the main thread's
    last response, and the host's count taken after it. Marked as an estimate: Claude
    Code's count runs about 6% under the next response's, which makes it exact again."""
    c = snap.compactions[-1] if snap.compactions else None
    if host is None or c is None or c.at < (snap.last_at or 0) or host.at < c.at:
        return snap
    return dataclasses.replace(snap, context=host.tokens, max=host.window, estimate=True)


# what the pane says

def tokens(n: float) -> str:
    """At most three figures, rolling up a unit where rounding would reach 1000: 999.6k is
    1M, not 1000k, and 123.4M is 123M, so a count stays within five characters."""
    if n >= 99_950_000:
        return f"{n / 1_000_000:.0f}M"
    if n >= 999_500:
        return f"{n / 1_000_000:.1f}".removesuffix(".0") + "M"
    return f"{n / 1_000:.0f}k" if n >= 1_000 else str(int(n))


def clock(at: float) -> str:
    return time.strftime("%H:%M %Z", time.localtime(at))
