"""A session's token use, read from its Claude Code transcripts for the inbox's stats pane:
how big its context is, whether its prompt cache is still warm, its compactions, and two
small charts of the last two hours: how its context was assembled and the output it made.

The numbers, the charts and their look are claude-dashboard's, ported: its usage parse,
`build_column`, palette, shimmer, gauges and panel border. The context grades are the
wheelhouse's own, shared with the session list's context bar (#51). See
.plan/session-stats.md.
"""

import dataclasses
import functools
import json
import math
import os
import re
import textwrap
import time
import urllib.request
import weakref
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from rich.color import Color
from rich.style import Style
from rich.text import Text

from . import models, transcript

SPAN = 2 * 3600                    # the charts' last two hours
TTL = {"1h": 3600, "5m": 300}
WRITE_PRICE = {"1h": 2.0, "5m": 1.25}   # cache write, times the base input price
MARGIN = 6                         # the charts' Y-axis labels
LABEL = 8                          # the gauges' and stats rows' labels
PARTIAL = " ▁▂▃▄▅▆▇█"              # 0..8 eighths of a cell, bottom up
# the dashboard's truecolour palette
CO = {"read": (52, 224, 150), "new": (84, 160, 255), "miss": (255, 88, 96),   # green, blue, red
      "output": (255, 205, 82)}                                               # yellow
OK, WARN, AMBER, HOT = (52, 224, 150), (255, 205, 82), (255, 138, 56), (255, 88, 96)
ACCENT, TEXT, DIM, DIM2 = (90, 232, 232), (216, 220, 240), (124, 128, 158), (72, 74, 102)
# the charts: title, and the series stacked in each column with their legend labels
CHARTS = {"assembly": ("context assembly", (("read", "cache"), ("new", "new"), ("miss", "miss"))),
          "output": ("output", (("output", "output tokens"),))}
SERIES = tuple(k for _, series in CHARTS.values() for k, _ in series)
CHART_SHARE = 0.4                  # each chart's height, against the one chart the pane had before (#43)
CHART_MIN = 2                      # rows of bars, below which a chart is left out


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


@dataclass
class Compaction:
    at: float
    trigger: str
    pre: int
    post: int     # Claude Code's postTokens: the messages kept, without the system prompt and tools
    after: int | None = None   # the next main-thread response's context: the size it left. 0 if unread


def parse(lines, seen: set, main: bool) -> tuple[list[Turn], list[Compaction]]:
    """The turns and compactions in some transcript lines. One response is written as
    several lines sharing a message id: `seen` counts each once, across reads."""
    turns, compactions = [], []
    for line in lines:
        if b'"usage"' not in line and b"compact_boundary" not in line:
            continue   # most lines: no need to parse them
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or (at := epoch(rec.get("timestamp", ""))) is None:
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
        turns.append(Turn(at, read, fresh if read else 0, 0 if read else fresh, inp + made + read, ttl,
                          msg.get("model"), main and not rec.get("isSidechain"), usage.get("output_tokens", 0) or 0,
                          rec.get("effort") if isinstance(rec.get("effort"), str) else None,
                          rec.get("requestedModel") if isinstance(rec.get("requestedModel"), str) else None))
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

    @property
    def window(self) -> int:
        return self.max or window_for(self.model, self.peak)

    @property
    def expires(self) -> float | None:
        return self.last_at + TTL[self.ttl] if self.last_at and self.ttl else None

    def warm(self, now: float) -> bool:
        return bool(self.expires and now < self.expires)


CHUNK = 4 << 20                    # bytes a first read takes at a time
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


def compactions_before(f, end: int) -> list[Compaction]:
    """The compactions in the first `end` bytes, found without parsing the rest."""
    out, pos, carry = [], 0, b""
    f.seek(0)
    while pos < end:
        n = min(CHUNK, end - pos)
        buf, pos = carry + f.read(n), pos + n
        cut = buf.rfind(b"\n") + 1
        buf, carry = buf[:cut], buf[cut:]
        i = buf.find(b"compact_boundary")
        while i >= 0:
            a, b = buf.rfind(b"\n", 0, i) + 1, buf.find(b"\n", i)
            out += parse([buf[a:b]], set(), main=True)[1]
            i = buf.find(b"compact_boundary", b + 1)
    return out


class UsageFollower:
    """One session's usage, read incrementally: each file's new bytes, when its size or
    modification time has changed. The main transcript, and its subagents' beside it.

    read() runs off the UI thread and swaps in a new snapshot when it's done, so the pane
    can show `snap` at any time. A first read of a big transcript starts at the span
    (window_start); only its compactions are taken from before that."""

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
        """Takes in what's been appended since the last read. True if anything was."""
        now = time.time() if now is None else now
        snap = Snapshot(self.snap.model, self.snap.context, self.snap.peak, self.snap.last_at, self.snap.ttl,
                        list(self.snap.compactions), list(self.snap.turns), effort=self.snap.effort,
                        requested=self.snap.requested)
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
                continue
            with open(path, "rb") as f:
                if path == self.main and offset == 0:
                    offset = window_start(f, st.st_size, now - SPAN)
                    early = [dataclasses.replace(c, after=0) for c in compactions_before(f, offset)]
                    snap.compactions += early
                    snap.peak = max([snap.peak, *(c.pre for c in early)])
                    changed = changed or bool(early)
                f.seek(offset)
                data = f.read(st.st_size - offset)
            end = data.rfind(b"\n") + 1   # a half-written last line waits for the next read
            self.files[path] = (offset + end, (st.st_size, st.st_mtime_ns))
            turns, compactions = parse(data[:end].splitlines(), self.seen, path == self.main)
            take(snap, turns, compactions)
            changed = changed or bool(turns or compactions)
        snap.turns = [t for t in snap.turns if t.at >= now - SPAN]
        self.snap, self.ready = snap, True
        return changed

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


def take(s: Snapshot, turns: list[Turn], compactions: list[Compaction]) -> None:
    s.compactions += compactions
    for t in turns:
        s.turns.append(t)
        if t.main:
            s.model, s.context, s.peak, s.last_at = t.model or s.model, t.context, max(s.peak, t.context), t.at
            s.effort, s.requested = t.effort or s.effort, t.requested or s.requested
            s.ttl = t.ttl or s.ttl
            c = s.compactions[-1] if s.compactions else None
            if c and c.after is None and c.at <= t.at:
                s.compactions[-1] = dataclasses.replace(c, after=t.context)


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
    return f"{n / 1_000_000:.1f}".removesuffix(".0") + "M" if n >= 1_000_000 else f"{n / 1_000:.0f}k" if n >= 1_000 else str(int(n))


def clock(at: float) -> str:
    return time.strftime("%H:%M %Z", time.localtime(at))


def until(seconds: float) -> str:
    m = max(0, int(seconds // 60))
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def model_name(model: str | None) -> str:
    m = re.match(r"claude-([a-z]+)-(\d+)(?:-(\d+))?", model or "")
    return f"{m[1].capitalize()} {m[2]}{'.' + m[3] if m[3] else ''}" if m else (model or "model unknown")


def cold_cost(snap: Snapshot) -> str:
    """What the next turn costs to rebuild a cold cache: its context, at the write price."""
    price = WRITE_PRICE.get(snap.ttl or "1h", 2.0)
    return f"next turn re-pays ~{tokens(snap.context)} (~{tokens(snap.context * price)} effective)"


def cache_lines(snap: Snapshot, now: float) -> list[str]:
    if not snap.expires:
        return ["no cache written yet"]
    if snap.warm(now):
        return [f"{snap.ttl} · warm until {clock(snap.expires)} ({until(snap.expires - now)})"]
    return [f"{snap.ttl} · cold since {clock(snap.expires)}", cold_cost(snap)]


@dataclass
class Totals:
    """The pane with no session in context: every running session's, added up."""
    sessions: int
    context: int
    warm: int
    next_cold: tuple[float, str] | None   # when, and whose
    turns: list[Turn]
    estimate: bool = False                # some session's context is the host's estimate


def combine(snaps: dict[str, Snapshot], names: dict[str, str], now: float) -> Totals:
    warm = {sid: s for sid, s in snaps.items() if s.warm(now)}
    soonest = min(warm, key=lambda sid: warm[sid].expires, default=None)
    return Totals(len(snaps), sum(s.context for s in snaps.values()), len(warm),
                  (warm[soonest].expires, names.get(soonest, soonest)) if soonest else None,
                  sorted((t for s in snaps.values() for t in s.turns), key=lambda t: t.at),
                  any(s.estimate for s in snaps.values()))


def hexc(c: tuple) -> str:
    return "#%02x%02x%02x" % c


def label(s: str) -> Text:
    return Text(s.ljust(LABEL), style=hexc(DIM))


def flash(colour: tuple, flashing: bool, now: float) -> tuple:
    """A flashing colour goes dark on alternate seconds, as the dashboard's gauges do."""
    return tuple(int(c * 0.22) for c in colour) if flashing and int(now) % 2 else colour


def meter(name: str, pct: float, colour: tuple, width: int, tail: str) -> Text:
    """One gauge, the dashboard's: a solid bar on a dotted track, then the percentage bold
    in the bar's colour."""
    pct = max(0.0, min(pct, 100.0))
    fill = round(pct / 100 * width)
    return (label(name) + Text("█" * fill, style=hexc(colour)) + Text("░" * (width - fill), style=hexc(DIM2))
            + Text(f" {pct:3.0f}%", style=f"bold {hexc(colour)}") + Text(tail, style=hexc(DIM)))


def gauges(view, usage: "AccountUsage", now: float, width: int) -> list[Text]:
    """The context, session and weekly gauges, at one bar width so they line up. Across
    every running session the context is a sum with no one window: a number, not a gauge."""
    bars = []   # (name, percent, colour, tail)
    if isinstance(view, Snapshot):
        bars.append(("context", view.context * 100 / view.window, grade(view.context),
                     f" {approx(view)}{tokens(view.context)}/{tokens(view.window)}"))
    for kind, name in LIMITS:
        if kind in usage.limits:
            pct, at = usage.limits[kind]
            colour, flashing = usage_grade(pct)
            bars.append((name, pct, flash(colour, flashing, now), f" {resets(at, now)}" if at else ""))
    width = max(4, width - LABEL - 5 - max((len(b[3]) for b in bars), default=0))
    out = [meter(name, pct, colour, width, tail) for name, pct, colour, tail in bars]
    if isinstance(view, Totals):
        out.insert(0, label("context") + Text(f"{approx(view)}{tokens(view.context)} across {view.sessions}",
                                              style=hexc(TEXT)))
    if not any(kind in usage.limits for kind, _ in LIMITS):
        out.append(Text("usage unavailable" if usage.failed else "usage loading…", style=hexc(DIM)))
    return out


def approx(view) -> str:
    return "~" if view.estimate else ""


def compact_row(view: Snapshot) -> str:
    """How many compactions, and the last's size before and after: after as the next response
    sized it, else the host's estimate, else only the messages it kept."""
    c = view.compactions[-1]
    if c.after:
        sizes = f"{tokens(c.pre)} → {tokens(c.after)}"
    elif view.estimate:
        sizes = f"{tokens(c.pre)} → ~{tokens(view.context)}"
    else:
        sizes = f"{tokens(c.pre)} · {tokens(c.post)} msgs kept"
    return f"{len(view.compactions)}× · last {clock(c.at)} · {sizes}"


def stat_rows(view, now: float) -> list[Text]:
    """The cache and compaction rows under the gauges."""
    if isinstance(view, Totals):
        cache = f"{view.warm} of {view.sessions} warm"
        if view.next_cold:
            at, who = view.next_cold
            cache += f" · next cold {who} at {clock(at)} ({until(at - now)})"
        return [label("cache") + Text(cache, style=hexc(TEXT))]
    out = [label("cache" if i == 0 else "") + Text(line, style=hexc(TEXT if view.warm(now) or i else HOT))
           for i, line in enumerate(cache_lines(view, now))]
    compact = compact_row(view) if view.compactions else "none yet"
    return out + [label("compact") + Text(compact, style=hexc(TEXT if view.compactions else DIM))]


def titled(view, name: str, setting: dict | None = None, room: int = 0) -> tuple[str, bool]:
    """The panel's title, and whether it says the effort. For one session: its name, model,
    effort and window, the model and effort as the stats pane has them (effort_of). Where
    the title has no room, it gives up the window's word, then the window, then the name,
    then the effort: the model is the last to go."""
    if isinstance(view, Totals):
        return f"all running sessions · {view.sessions}", False
    setting = setting or {}
    model = setting.get("model") or view.requested or view.model
    effort = effort_of(view, setting)
    window = tokens(view.max or window_for(model, view.peak))
    shown = models.name(model) or model_name(model)
    for parts in ([name, shown, effort, f"{window} window"], [name, shown, effort, window], [name, shown, effort],
                  [shown, effort], [shown]):
        text = " · ".join(p for p in parts if p)
        if not room or len(text) <= room:
            break
    return text, bool(effort) and effort in parts


def title(view, name: str, setting: dict | None = None, room: int = 0) -> str:
    return titled(view, name, setting, room)[0]


def effort_of(view, setting: dict | None) -> str | None:
    """The effort the pane shows, the slider's lit level: what the session is set to
    (setting: Hub.model_state), else what its transcript last ran (a host on older code
    that never recorded either)."""
    return (setting or {}).get("effort") or getattr(view, "effort", None)


SLIDER_ROW = 2   # the effort slider's line in the pane: under the panel's top border and a blank line
PENDING = {"applying": "applying…", "on relaunch": "on relaunch: old host"}   # Hub.model_state's
PENDING_SHORT = {"applying": "applying…", "on relaunch": "on relaunch"}       # where the line has less room
LEVELS = " ".join(models.EFFORTS)   # the slider in full
WIDEST = max(map(len, models.EFFORTS))


def slider_parts(current: str | None, width: int) -> list[tuple[str, str | None]]:
    """The slider's line, as (text, the level a click on it picks): every level where the
    line has room for them all, else the current one between arrows that step it (◂ high ▸),
    padded to the widest level so the arrows stay put. Starting from high where nothing is
    known, as the keys do."""
    levels = models.EFFORTS
    if width >= LABEL + len(LEVELS):
        out = [("effort".ljust(LABEL), None)]
        for i, level in enumerate(levels):
            out += [(" ", None)] * bool(i) + [(level, level)]
        return out
    at = levels.index(current if current in levels else "high")
    shown = current if current in levels else "?"
    left = (WIDEST - len(shown)) // 2
    right = WIDEST - len(shown) - left
    head = "effort".ljust(LABEL) if width >= LABEL + WIDEST + 4 else "eff "
    return [(head, None), ("◂", levels[at - 1] if at else None), (" " * (left + 1), None),
            (shown, current if current in levels else None), (" " * (right + 1), None),
            ("▸", levels[at + 1] if at + 1 < len(levels) else None)]


def slider(current: str | None, focused: bool, width: int = LABEL + len(LEVELS)) -> Text:
    """The effort slider (slider_parts) at a width, the current level lit (brighter while
    the pane has focus); an arrow with nowhere to go dim. In full, with focus, the keys
    too, where there's room."""
    lit = f"bold #000000 on {hexc(ACCENT if focused else DIM)}"
    out = Text()
    for i, (text, level) in enumerate(slider_parts(current, width)):
        if i == 0:
            style = hexc(DIM)
        elif text in ("◂", "▸"):
            style = hexc(TEXT if level else DIM2)
        elif level is not None and level == current:
            style = lit
        else:
            style = hexc(TEXT) if text.strip() else ""
        out += Text(text, style=style)
    full = width >= LABEL + len(LEVELS)
    return out + Text("  ←/→" if focused and full else "", style=hexc(DIM))


def slider_spans(current: str | None = None, width: int = LABEL + len(LEVELS)) -> list[tuple[int, int, str]]:
    """Where a click on the slider's line picks a level, from the line's start: (first
    cell, past the last, the level), for the form slider draws at that width."""
    out, x = [], 0
    for text, level in slider_parts(current, width):
        if level is not None:
            out.append((x, x + len(text), level))
        x += len(text)
    return out


def pending_line(pending: str | None, width: int) -> Text:
    """The line under the slider while a change is pending: under the levels where it fits,
    else from the line's start, else shorter."""
    full = PENDING.get(pending)
    if full is None:
        return Text()
    for text in (" " * LABEL + full, full):
        if len(text) <= width:
            return Text(text, style=hexc(WARN))
    return Text(PENDING_SHORT[pending], style=hexc(WARN))


def panel(head: str, rows: list[Text], width: int) -> list[Text]:
    """The dashboard's panel: a rounded border in its dimmest colour, titled in its accent.
    Rows are cut or padded to fit inside."""
    inner = width - 2
    if inner < 8:
        return rows
    border = hexc(DIM2)
    name = Text(head, style=f"bold {hexc(ACCENT)}")
    name.truncate(inner - 4)
    out = [Text("╭─ ", style=border) + name + Text(" " + "─" * (inner - 3 - name.cell_len) + "╮", style=border)]
    for row in rows:
        row = row.copy()
        row.truncate(inner, pad=True)
        out.append(Text("│", style=border) + row + Text("│", style=border))
    return out + [Text("╰" + "─" * inner + "╯", style=border)]


def said(note: str, width: int) -> list[Text]:
    """A note, dim, wrapped to the panel rather than cut off at its border."""
    return [Text(line, style=hexc(DIM)) for line in textwrap.wrap(note, max(width, 1))]


def spaced(rows: list[Text]) -> list[Text]:
    return [r for row in rows for r in (row, Text())]


def layout(view, name: str, note: str | None, usage: "AccountUsage", now: float, width: int,
           height: int, setting: dict | None = None, focused: bool = False) -> tuple[list[Text], list["Chart"]]:
    """The pane at a size: a bordered panel of the gauges (a blank line above them and
    under each) and the cache and compaction rows, then as many of the charts as fit under it.
    A session run by a host (setting: Hub.model_state) has the effort slider above the
    gauges, on SLIDER_ROW."""
    if view is None:
        body = said(note or "no sessions running", width - 2) + [Text()] + spaced(gauges(None, usage, now, width - 2))
        return panel("stats", body, width), []
    head, says_effort = titled(view, name, setting, width - 6)
    effort = effort_of(view, setting)
    if has_slider(view, setting):   # the line under it says while a change is pending: no jump when it's done
        top = [Text(), slider(effort, focused, width - 2), pending_line(setting.get("pending"), width - 2)]
    elif effort and not says_effort and isinstance(view, Snapshot):   # a narrow pane: the effort, under the title
        top = [Text(), label("effort") + Text(effort, style=hexc(TEXT))]
    else:
        top = [Text()]
    body = top + spaced(gauges(view, usage, now, width - 2)) + stat_rows(view, now)
    if note:   # beside a view: something went wrong reading it
        body += said(note, width - 2)
    rows = panel(head, body, width)
    return rows, charts(view.turns, now, width, height - len(rows), height)


def has_slider(view, setting: dict | None) -> bool:
    """Whether the pane shows the effort slider: for one session, run by a host."""
    return isinstance(view, Snapshot) and bool(setting and setting.get("hosted"))


def render(rows: list[Text], shown: list["Chart"], frame: int) -> Text:
    lines = list(rows)
    for i, c in enumerate(shown):
        lines += chart_lines(c, frame, ticks=i == len(shown) - 1)
    return Text("\n", no_wrap=True, overflow="crop").join(lines)


# the account's usage limits: the same numbers as Claude Code's /usage

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_EVERY = 60                   # seconds between fetches: an external endpoint
LIMITS = (("session", "session"), ("weekly_all", "weekly"))


def limits(data) -> dict[str, tuple[float, float | None]]:
    """The usage endpoint's body as {kind: (percent, resets at)}, skipping what's malformed."""
    out = {}
    for lim in (data or {}).get("limits") or []:
        if isinstance(lim, dict) and isinstance(lim.get("percent"), (int, float)):
            out[lim.get("kind")] = (float(lim["percent"]), epoch(lim.get("resets_at") or ""))
    return out


class NoRedirects(urllib.request.HTTPRedirectHandler):
    """urllib forwards the Authorization header on a redirect, to any host: the usage
    fetch carries the person's login token, so a 30x is an error instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirects)


class AccountUsage:
    """Session and weekly usage for the logged-in account, fetched at most once a minute,
    off the UI thread. The last good numbers stay up while a fetch fails."""

    def __init__(self):
        self.limits: dict = {}
        self.at = 0.0
        self.failed = False

    def due(self, now: float) -> bool:
        if now - self.at < USAGE_EVERY:
            return False
        self.at = now   # claimed here, so one fetch is in flight at a time
        return True

    def fetch(self) -> None:
        home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
        try:
            with open(os.path.join(home, ".credentials.json")) as f:
                token = json.load(f)["claudeAiOauth"]["accessToken"]
            req = urllib.request.Request(USAGE_URL, headers={
                "Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20",
                "anthropic-version": "2023-06-01", "User-Agent": "claude-wheelhouse"})
            with OPENER.open(req, timeout=15) as r:
                self.limits, self.failed = limits(json.load(r)), False
        except Exception:
            self.failed = True


def usage_grade(pct: float) -> tuple[tuple, bool]:
    """claude-dashboard's allowance tiers: green to 70, yellow to 80, amber to 90, red, flashing over 95."""
    if pct > 90:
        return HOT, pct > 95
    return (AMBER if pct > 80 else WARN if pct > 70 else OK), False


def resets(at: float | None, now: float) -> str:
    if not at:
        return ""
    same_day = time.localtime(at)[:3] == time.localtime(now)[:3]
    return "resets " + time.strftime("%H:%M %Z" if same_day else "%a %H:%M %Z", time.localtime(at))


# the charts

def window(now: float, columns: int, span: int = SPAN) -> tuple[float, float]:
    """The charts' (start, bucket width): `columns` buckets of span / columns seconds, on
    epoch multiples of that width so each covers a fixed stretch of clock, the last the
    current one, partly filled. Anchored to now, the edges slid on every repaint (#63)."""
    width = span / max(columns, 1)
    return (math.floor(now / width) - max(columns, 1) + 1) * width, width


def buckets(turns: list[Turn], now: float, columns: int, span: int = SPAN) -> list[dict]:
    """The window's buckets, oldest first, each summing its turns."""
    out = [dict.fromkeys(SERIES, 0) for _ in range(max(columns, 0))]
    if not out:
        return out
    start, width = window(now, columns, span)
    for t in turns:
        i = math.floor((t.at - start) / width)
        if 0 <= i < columns and t.at <= now:
            for k in SERIES:
                out[i][k] += getattr(t, k)
    return out


def nice(n: float) -> float:
    """`n` rounded up to 1, 2 or 5 times a power of ten, so the Y axis only rescales when
    the tallest bar crosses one of them, not on every bar that grows."""
    if n <= 0:
        return 0
    step = 10 ** math.floor(math.log10(n))
    return next(m * step for m in (1, 2, 5, 10) if m * step >= n)


def build_column(vc: list[tuple], total: float, maxt: float, height: int) -> list[tuple]:
    """`height` cells bottom to top as (colour or None, char), in eighths of a cell, each
    non-empty segment at least one eighth. The dashboard's, unchanged."""
    col = [(None, " ")] * height
    if total <= 0 or maxt <= 0:
        return col
    units = height * 8
    sub = min(max(int(round(total / maxt * units)), 1), units)
    nz = [i for i, (_, v) in enumerate(vc) if v > 0]
    alloc = [0] * len(vc)
    if sub >= len(nz):
        for i in nz:
            alloc[i] = 1
        fr = []
        for i in nz:
            e = vc[i][1] / total * sub
            alloc[i] += max(int(e) - 1, 0)
            fr.append((e - int(e), i))
        for _, i in sorted(fr, reverse=True)[:max(sub - sum(alloc), 0)]:
            alloc[i] += 1
    else:
        for i in sorted(nz, key=lambda i: vc[i][1], reverse=True)[:sub]:
            alloc[i] = 1
    contrib = [dict() for _ in range(height)]
    filled = [0] * height
    pos = 0
    for (colour, _), n in zip(vc, alloc):
        for _ in range(n):
            ci = pos // 8
            if ci < height:
                contrib[ci][colour] = contrib[ci].get(colour, 0) + 1
                filled[ci] += 1
            pos += 1
    for ci in range(height):
        if filled[ci]:
            col[ci] = (max(contrib[ci].items(), key=lambda kv: kv[1])[0], PARTIAL[min(filled[ci], 8)])
    return col


@dataclass
class Chart:
    """The columns, built when the data or the size changes; the shimmer only recolours them."""
    columns: list[list[tuple]]
    maxt: float
    height: int
    start: float
    span: float
    kind: str = "assembly"
    gap: bool = False   # a blank line above it


def chart(turns: list[Turn], now: float, width: int, height: int, kind: str = "assembly", span: int = SPAN,
          gap: bool = False) -> Chart:
    keys = [k for k, _ in CHARTS[kind][1]]
    bs = buckets(turns, now, width - MARGIN, span)
    totals = [sum(b[k] for k in keys) for b in bs]
    maxt = nice(max(totals, default=0))
    start, step = window(now, len(bs), span)
    return Chart([build_column([(CO[k], b[k]) for k in keys], t, maxt, height) for b, t in zip(bs, totals)],
                 maxt, height, start, step * len(bs), kind, gap)


def charts(turns: list[Turn], now: float, width: int, room: int, height: int) -> list[Chart]:
    """Both charts if they fit in `room` rows, else the context assembly alone, else none.
    Each is CHART_SHARE of the height the one chart had before #43 (the pane less six text
    rows and its own three), and no taller than fits."""
    target = round(CHART_SHARE * (height - 9))
    for kinds in (("assembly", "output"), ("assembly",)):
        n = len(kinds)
        fixed = 2 * n + 1   # each chart's header and baseline, and the hour ticks under the last
        bars = min(target, (room - fixed) // n)
        if bars >= CHART_MIN:
            gap = room - fixed - n * bars >= n - 1   # a blank line between them, room permitting
            return [chart(turns, now, width, bars, kind, gap=bool(i) and gap) for i, kind in enumerate(kinds)]
    return []


def shade(c: tuple, f: float) -> Style:
    """The dashboard's shade, as a Rich style."""
    return style_of((int(c[0] * f), int(c[1] * f), int(c[2] * f)))


@functools.cache
def style_of(rgb: tuple) -> Style:
    """Built once per colour, not parsed from a string every frame: a few hundred at most."""
    return Style(color=Color.from_rgb(*rgb))


def bar_label(c: Chart, row: int) -> Text:
    """A row of bars' Y-axis label, MARGIN wide: every other row's value. Its style is
    the whole line's, under the bars' own."""
    value = tokens(round(c.maxt * (row + 1) / c.height)) if row % 2 and c.maxt else ""
    return Text(value.rjust(MARGIN - 2) + "  ", style=hexc(DIM))


def bar(c: Chart, row: int, frame: int) -> list[tuple[str, Style | None]]:
    """A row of a chart's bars at animation frame `frame`, from the bottom, each column's
    character and its style. The shimmer and gradient are the dashboard's."""
    out: list[tuple[str, Style | None]] = [(" ", None)] * len(c.columns)
    cells = bar_cells(c, row)
    for (i, _, _, ch), rgb in zip(cells, tints(cells, row_shade(c, row), frame)):
        out[i] = (ch, style_of(rgb))
    return out


def bar_cells(c: Chart, row: int) -> list[tuple[int, float, tuple, str]]:
    """A row of a chart's bars' filled cells, those a shimmer frame recolours: each as its
    column, its place in the wave, its colour and its character."""
    return [(i, 0.20 * i + 0.45 * row, col[row][0], col[row][1]) for i, col in enumerate(c.columns) if col[row][0]]


def row_shade(c: Chart, row: int) -> float:
    """A row of bars' gradient: dimmer towards the bottom."""
    return 0.5 + 0.5 * row / (c.height - 1) if c.height > 1 else 1.0


# the shimmer's wave: the dashboard's 0.11 radians a frame, a cycle of 57.1 frames, rounded to a
# whole 57 so each frame of a cycle comes round again the same, and is drawn once (SessionStats.cycles)
PERIOD = round(2 * math.pi / 0.11)


def phase(frame: int) -> float:
    """Where the shimmer's wave is at animation frame `frame`."""
    return 2 * math.pi * (frame % PERIOD) / PERIOD


def tints(cells: list[tuple[int, float, tuple, str]], f: float, frame: int) -> list[tuple[int, int, int]]:
    """The colour of each of bar_cells at animation frame `frame`, in a row whose gradient is
    `f`: its own colour shaded by the gradient and the wave (shade)."""
    out, at_frame, sin = [], phase(frame), math.sin
    for _, at, (r, g, b), _ in cells:
        k = max(0.12, min(1.0, f * (1.0 + 0.18 * sin(at - at_frame))))
        out.append((int(r * k), int(g * k), int(b * k)))
    return out


def bar_lines(rows: list[Text], shown: list["Chart"]) -> list[tuple["Chart", int] | None]:
    """For each of render's lines, the chart and row of bars it is, the lines a shimmer frame
    changes, or None."""
    out: list[tuple[Chart, int] | None] = [None] * len(rows)
    for c in shown:
        out += [None] * (c.gap + 1)   # a blank line above it, then its legend
        out += [(c, row) for row in range(c.height - 1, -1, -1)]
        out.append(None)   # its baseline
    return out + [None] * bool(shown)   # the hour ticks under the last


def chart_lines(c: Chart, frame: int, ticks: bool = True) -> list[Text]:
    """A chart's legend, bars and baseline at animation frame `frame`, and with `ticks` the
    hourly ticks under it. The shimmer and gradient are the dashboard's."""
    name, series = CHARTS[c.kind]
    head = Text("▸ ", style=f"bold {hexc(ACCENT)}") + Text(name, style=f"bold {hexc(TEXT)}")
    for k, legend in series:
        head += Text("  ▆", style=hexc(CO[k])) + Text(" " + legend, style=hexc(DIM))
    out = ([Text()] if c.gap else []) + [head]
    for row in range(c.height - 1, -1, -1):
        line = bar_label(c, row)
        for ch, style in bar(c, row, frame):
            line.append(ch, style=style)
        out.append(line)
    n = len(c.columns)
    out.append(Text("0".rjust(MARGIN - 1) + " ", style=hexc(DIM)) + Text("└" + "─" * max(n - 1, 0), style=hexc(DIM2)))
    if not ticks:
        return out
    axis = [" "] * n
    lt = time.localtime(c.start)
    tick = int(c.start) - lt.tm_min * 60 - lt.tm_sec + 3600   # the next local hour: not UTC's, which is off by half an hour in some zones
    while tick <= c.start + c.span and n:
        lab = f"{time.localtime(tick).tm_hour}:00"
        pos = round((tick - c.start) / c.span * n)
        if pos + len(lab) <= n:   # a label that doesn't fit at its hour is left out, not moved off it
            axis[pos:pos + len(lab)] = lab
        tick += 3600
    out.append(Text(" " * MARGIN + "".join(axis), style=hexc(DIM)))
    return out
