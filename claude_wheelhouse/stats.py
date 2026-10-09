"""A session's token use, read from its Claude Code transcripts for the inbox's stats pane:
how big its context is, whether its prompt cache is still warm, its compactions, and two
small charts of the last two hours: how its context was assembled and the output it made.

The numbers, the charts and their look are claude-dashboard's, ported: its usage parse,
`build_column`, palette, shimmer, gauges, panel border and context grades. See
.plan/session-stats.md.
"""

import functools
import json
import math
import os
import re
import textwrap
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from rich.color import Color
from rich.style import Style
from rich.text import Text

from . import transcript

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


@dataclass
class Compaction:
    at: float
    trigger: str
    pre: int
    post: int


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
                          msg.get("model"), main and not rec.get("isSidechain"), usage.get("output_tokens", 0) or 0))
    return turns, compactions


# the dashboard's context grades

def max_window(model: str | None) -> int:
    """The 1M context is a request header, stripped from the logged model id: Opus and
    Sonnet can have it, so they're graded against 1M."""
    m = (model or "").lower()
    return 1_000_000 if "opus" in m or "sonnet" in m else 200_000


def window_for(model: str | None, peak: int) -> int:
    return 1_000_000 if peak > 200_000 else max_window(model)


def grade(size: int, window: int) -> tuple[tuple, bool]:
    """Colour, and whether it flashes: green, yellow, amber, red, flashing red."""
    g, y, a, r = (150_000, 300_000, 450_000, 600_000) if window >= 1_000_000 else (100_000, 125_000, 150_000, 175_000)
    if size > r:
        return HOT, True
    return (HOT if size > a else AMBER if size > y else WARN if size > g else OK), False


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

    @property
    def window(self) -> int:
        return window_for(self.model, self.peak)

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
                        list(self.snap.compactions), list(self.snap.turns))
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
                    early = compactions_before(f, offset)
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


def take(s: Snapshot, turns: list[Turn], compactions: list[Compaction]) -> None:
    s.compactions += compactions
    for t in turns:
        s.turns.append(t)
        if t.main:
            s.model, s.context, s.peak, s.last_at = t.model or s.model, t.context, max(s.peak, t.context), t.at
            s.ttl = t.ttl or s.ttl


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
        return [f"{snap.ttl} · warm · cold at {clock(snap.expires)} (in {until(snap.expires - now)})"]
    return [f"{snap.ttl} · cold since {clock(snap.expires)}", cold_cost(snap)]


@dataclass
class Totals:
    """The pane with no session in context: every running session's, added up."""
    sessions: int
    context: int
    warm: int
    next_cold: tuple[float, str] | None   # when, and whose
    turns: list[Turn]


def combine(snaps: dict[str, Snapshot], names: dict[str, str], now: float) -> Totals:
    warm = {sid: s for sid, s in snaps.items() if s.warm(now)}
    soonest = min(warm, key=lambda sid: warm[sid].expires, default=None)
    return Totals(len(snaps), sum(s.context for s in snaps.values()), len(warm),
                  (warm[soonest].expires, names.get(soonest, soonest)) if soonest else None,
                  sorted((t for s in snaps.values() for t in s.turns), key=lambda t: t.at))


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
        colour, flashing = grade(view.context, view.window)
        bars.append(("context", view.context * 100 / view.window, flash(colour, flashing, now),
                     f" {tokens(view.context)}/{tokens(view.window)}"))
    for kind, name in LIMITS:
        if kind in usage.limits:
            pct, at = usage.limits[kind]
            colour, flashing = usage_grade(pct)
            bars.append((name, pct, flash(colour, flashing, now), f" {resets(at, now)}" if at else ""))
    width = max(4, width - LABEL - 5 - max((len(b[3]) for b in bars), default=0))
    out = [meter(name, pct, colour, width, tail) for name, pct, colour, tail in bars]
    if isinstance(view, Totals):
        out.insert(0, label("context") + Text(f"{tokens(view.context)} across {view.sessions}", style=hexc(TEXT)))
    if not any(kind in usage.limits for kind, _ in LIMITS):
        out.append(Text("usage unavailable" if usage.failed else "usage loading…", style=hexc(DIM)))
    return out


def stat_rows(view, now: float) -> list[Text]:
    """The cache and compaction rows under the gauges."""
    if isinstance(view, Totals):
        cache = f"{view.warm} of {view.sessions} warm"
        if view.next_cold:
            at, who = view.next_cold
            cache += f" · next cold {who} at {clock(at)} (in {until(at - now)})"
        return [label("cache") + Text(cache, style=hexc(TEXT))]
    out = [label("cache" if i == 0 else "") + Text(line, style=hexc(TEXT if view.warm(now) or i else HOT))
           for i, line in enumerate(cache_lines(view, now))]
    if view.compactions:
        c = view.compactions[-1]
        compact = f"{len(view.compactions)}× · last {clock(c.at)} · {tokens(c.pre)} → {tokens(c.post)}"
    else:
        compact = "none yet"
    return out + [label("compact") + Text(compact, style=hexc(TEXT if view.compactions else DIM))]


def title(view, name: str) -> str:
    if isinstance(view, Totals):
        return f"all running sessions · {view.sessions}"
    return f"{name} · {model_name(view.model)} · {tokens(view.window)} window"


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
           height: int) -> tuple[list[Text], list["Chart"]]:
    """The pane at a size: a bordered panel of the gauges (a blank line above them and
    under each) and the cache and compaction rows, then as many of the charts as fit under it."""
    if view is None:
        body = said(note or "no sessions running", width - 2) + [Text()] + spaced(gauges(None, usage, now, width - 2))
        return panel("stats", body, width), []
    body = [Text()] + spaced(gauges(view, usage, now, width - 2)) + stat_rows(view, now)
    if note:   # beside a view: something went wrong reading it
        body += said(note, width - 2)
    rows = panel(title(view, name), body, width)
    return rows, charts(view.turns, now, width, height - len(rows), height)


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

def buckets(turns: list[Turn], now: float, columns: int, span: int = SPAN) -> list[dict]:
    """The span cut into `columns` equal buckets, oldest first, each summing its turns."""
    out = [dict.fromkeys(SERIES, 0) for _ in range(max(columns, 0))]
    if not out:
        return out
    start, width = now - span, span / columns
    for t in turns:
        if start <= t.at <= now:
            b = out[min(int((t.at - start) / width), columns - 1)]
            for k in SERIES:
                b[k] += getattr(t, k)
    return out


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
    span: int
    kind: str = "assembly"
    gap: bool = False   # a blank line above it


def chart(turns: list[Turn], now: float, width: int, height: int, kind: str = "assembly", span: int = SPAN,
          gap: bool = False) -> Chart:
    keys = [k for k, _ in CHARTS[kind][1]]
    bs = buckets(turns, now, width - MARGIN, span)
    totals = [sum(b[k] for k in keys) for b in bs]
    maxt = max(totals, default=0)
    return Chart([build_column([(CO[k], b[k]) for k in keys], t, maxt, height) for b, t in zip(bs, totals)],
                 maxt, height, now - span, span, kind, gap)


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


def chart_lines(c: Chart, frame: int, ticks: bool = True) -> list[Text]:
    """A chart's legend, bars and baseline at animation frame `frame`, and with `ticks` the
    hourly ticks under it. The shimmer and gradient are the dashboard's."""
    name, series = CHARTS[c.kind]
    head = Text("▸ ", style=f"bold {hexc(ACCENT)}") + Text(name, style=f"bold {hexc(TEXT)}")
    for k, legend in series:
        head += Text("  ▆", style=hexc(CO[k])) + Text(" " + legend, style=hexc(DIM))
    out = ([Text()] if c.gap else []) + [head]
    for row in range(c.height - 1, -1, -1):
        f = 0.5 + 0.5 * row / (c.height - 1) if c.height > 1 else 1.0
        value = tokens(round(c.maxt * (row + 1) / c.height)) if row % 2 and c.maxt else ""
        line = Text(value.rjust(MARGIN - 2) + "  ", style=hexc(DIM))
        for i, col in enumerate(c.columns):
            base, ch = col[row]
            if base:
                wave = 1.0 + 0.18 * math.sin(0.20 * i + 0.45 * row - 0.11 * frame)
                line.append(ch, style=shade(base, max(0.12, min(1.0, f * wave))))
            else:
                line.append(" ")
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
