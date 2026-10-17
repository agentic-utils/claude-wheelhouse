"""The stats pane's drawing: two small charts of a session's last two hours (how its context
was assembled and the output it made), the gauges, the effort slider and the zoom. What it
draws from, a session's token use, is the wheelhouse's (transcript_usage.py, through api.py).

The charts and their look are claude-dashboard's, ported: `build_column`, palette, shimmer,
gauges and panel border. See .plan/session-stats.md.
"""

import bisect
import functools
import itertools
import math
import re
import textwrap
import time
from dataclasses import dataclass, field
from datetime import date

from rich.color import Color
from rich.style import Style
from rich.text import Text

from claude_wheelhouse.api import (AMBER, BUCKET, CHARTS, DAY, EFFORTS, HOT, HOUR, OK, PARTIAL, SERIES, SPAN,
                                   USAGE_EVERY, WARN, WEEK, AccountUsage, History, Snapshot, Turn, clock,
                                   grade, hexc, model_label, tokens, window_for)

# the charts' zoom levels, finest first; past a week each doubles (wider), as far as the history goes
SPANS = (900, 1800, HOUR, SPAN, 4 * HOUR, 8 * HOUR, 12 * HOUR, DAY, 2 * DAY, 4 * DAY, WEEK)
# the axis ticks' steps, on the local clock: each span's finest is TICK's (else an eighth of
# it), and a coarser one where the labels would crowd (axis_ticks)
TICK = {900: 300, 1800: 600, HOUR: 900, SPAN: HOUR}
STEPS = (300, 600, 900, 1800, HOUR, 2 * HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR, DAY, 2 * DAY, WEEK)
WRITE_PRICE = {"1h": 2.0, "5m": 1.25}   # cache write, times the base input price
MARGIN = 6                         # the charts' Y-axis labels
LABEL = 8                          # the gauges' and stats rows' labels
# the dashboard's truecolour palette
CO = {"read": (52, 224, 150), "new": (84, 160, 255), "miss": (255, 88, 96),   # green, blue, red
      "output": (255, 205, 82)}                                               # yellow
ACCENT, TEXT, DIM, DIM2 = (90, 232, 232), (216, 220, 240), (124, 128, 158), (72, 74, 102)
CHART_SHARE = 0.4                  # each chart's height, against the one chart the pane had before (#43)
CHART_MIN = 2                      # rows of bars, below which a chart is left out


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
    """The pane with no session in context: every session's, added up."""
    sessions: int
    context: int
    warm: int
    next_cold: tuple[float, str] | None   # when, and whose
    turns: list[Turn]
    estimate: bool = False                # some session's context is the host's estimate
    histories: list[History] = field(default_factory=list)   # each session's older history


def combine(snaps: dict[str, Snapshot], names: dict[str, str], now: float) -> Totals:
    warm = {sid: s for sid, s in snaps.items() if s.warm(now)}
    soonest = min(warm, key=lambda sid: warm[sid].expires, default=None)
    return Totals(len(snaps), sum(s.context for s in snaps.values()), len(warm),
                  (warm[soonest].expires, names.get(soonest, soonest)) if soonest else None,
                  sorted((t for s in snaps.values() for t in s.turns), key=lambda t: t.at),
                  any(s.estimate for s in snaps.values()), [s.history for s in snaps.values() if s.history])


def histories(view) -> list[History]:
    if isinstance(view, Totals):
        return view.histories
    return [view.history] if view.history else []


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
    every session there is no context to show: each has its own window, and a sum of them
    means nothing, so the totals show the account's gauges alone."""
    bars = []   # (name, percent, colour, tail)
    if isinstance(view, Snapshot):
        bars.append(("context", view.context * 100 / view.window, grade(view.context),
                     f" {approx(view)}{tokens(view.context)}/{tokens(view.window)}"))
    shown = [kind for kind, _ in LIMITS
             if kind in usage.limits and not (kind in OPTIONAL and usage.limits[kind][0] <= 0)]
    for kind, name in LIMITS:
        if kind in shown:
            pct, at = usage.limits[kind]
            colour, flashing = usage_grade(pct)
            bars.append((name, pct, flash(colour, flashing, now), f" {resets(at, now)}" if at else ""))
    width = max(4, width - LABEL - 5 - max((len(b[3]) for b in bars), default=0))
    out = [meter(name, pct, colour, width, tail) for name, pct, colour, tail in bars]
    age = usage.age(now, shown)
    if age is None:
        out.append(Text("usage unavailable" if usage.failed else "usage loading…", style=hexc(DIM)))
    elif age > STALE:
        out.append(label("") + Text(f"usage read {until(age)} ago", style=hexc(DIM)))
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
        return f"all sessions · {view.sessions}", False
    setting = setting or {}
    model = setting.get("model") or view.requested or view.model
    effort = effort_of(view, setting)
    window = tokens(view.max or window_for(model, view.peak))
    shown = model_label(model) or model_name(model)
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
LEVELS = " ".join(EFFORTS)   # the slider in full
WIDEST = max(map(len, EFFORTS))
SHORT = {"medium": "med", "xhigh": "xhi"}   # the levels where a pane has no room for them whole
SHORTEST = max(len(SHORT.get(level, level)) for level in EFFORTS)
KEYS, KEYS_LABEL = "  ←/→", "eff ←/→ "   # the slider's keys, after the levels or, short of room, in the label's place


def slider_parts(current: str | None, width: int) -> list[tuple[str, str | None]]:
    """The slider's line, as (text, the level a click on it picks): every level where the
    line has room for them all, else the current one between arrows that step it (◂ high ▸),
    padded to the widest level so the arrows stay put, the label shortened, then dropped,
    then the level shortened (SHORT), so both arrows always show. Starting from high where
    nothing is known, as the keys do."""
    levels = EFFORTS
    if width >= LABEL + len(LEVELS):
        out = [("effort".ljust(LABEL), None)]
        for i, level in enumerate(levels):
            out += [(" ", None)] * bool(i) + [(level, level)]
        return out
    at = levels.index(current if current in levels else "high")
    shown = current if current in levels else "?"
    widest, head = WIDEST, ("effort".ljust(LABEL) if width >= LABEL + WIDEST + 4 else
                            "eff " if width >= 4 + WIDEST + 4 else "")
    if width < WIDEST + 4:
        shown, widest = SHORT.get(shown, shown), SHORTEST
    left = (widest - len(shown)) // 2
    right = widest - len(shown) - left
    return [(head, None)] * bool(head) + [
        ("◂", levels[at - 1] if at else None), (" " * (left + 1), None),
        (shown, current if current in levels else None), (" " * (right + 1), None),
        ("▸", levels[at + 1] if at + 1 < len(levels) else None)]


def slider(current: str | None, focused: bool, width: int = LABEL + len(LEVELS)) -> Text:
    """The effort slider (slider_parts) at a width, the current level lit (brighter while
    the pane has focus); an arrow with nowhere to go dim. In full, with focus, the keys
    too: after the levels where there's room, else in the label's place."""
    lit = f"bold #000000 on {hexc(ACCENT if focused else DIM)}"
    full = width >= LABEL + len(LEVELS)
    keys = focused and full and width >= LABEL + len(LEVELS) + len(KEYS)
    out = Text()
    for i, (text, level) in enumerate(slider_parts(current, width)):
        if i == 0 and level is None and text.strip() in ("effort", "eff"):
            text = KEYS_LABEL if focused and full and not keys else text   # the keys where the label was
            style = hexc(DIM)
        elif text in ("◂", "▸"):
            style = hexc(TEXT if level else DIM2)
        elif level is not None and level == current:
            style = lit
        else:
            style = hexc(TEXT) if text.strip() else ""
        out += Text(text, style=style)
    return out + Text(KEYS if keys else "", style=hexc(DIM))


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
           height: int, setting: dict | None = None, focused: bool = False,
           span: int = SPAN, loading: bool = False, more: bool = True) -> tuple[list[Text], list["Chart"]]:
    """The pane at a size: a bordered panel of the gauges (a blank line above them and
    under each) and the cache and compaction rows, then as many of the charts as fit under it.
    A session run by a host (setting: Hub.model_state) has the effort slider above the
    gauges, on SLIDER_ROW. The charts cover `span` (zoom), its older part from the history,
    `loading` while that's still being read, and `more` if there's older history to zoom out to."""
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
    return rows, charts(view.turns, now, width, height - len(rows), height, span, histories(view), loading, more)


def has_slider(view, setting: dict | None) -> bool:
    """Whether the pane shows the effort slider: for one session, run by a host, with SDK mode
    on (Hub.model_state's effort_live). EXPERIMENTAL (features.py): SDK mode is not for use by
    anyone other than the author until Anthropic's terms are confirmed."""
    return isinstance(view, Snapshot) and bool(setting and setting.get("effort_live"))


def render(rows: list[Text], shown: list["Chart"], frame: int) -> Text:
    lines = list(rows)
    for i, c in enumerate(shown):
        lines += chart_lines(c, frame, ticks=i == len(shown) - 1)
    return Text("\n", no_wrap=True, overflow="crop").join(lines)


# the account's usage limits: Claude Code's /usage, read by the hub (plan_usage)

# the gauges, by plan_usage's kinds; an optional one only once it's above 0%
LIMITS = (("session", "session"), ("weekly_all", "weekly"), ("weekly_fable", "fable"))
OPTIONAL = {"weekly_fable"}
STALE = USAGE_EVERY + 30   # seconds after which a reading says how old it is: a poll has failed


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


class Series:
    """A list of turns, as the charts read it each second: when each was, in order, and each
    series' running totals in that order, so the turns in a stretch of time are found by
    bisection rather than by going through them all. A turn never changes once made (parse),
    so this holds as long as the list has the same turns."""

    def __init__(self, turns: list[Turn]):
        self.turns = list(turns)   # held: the key's ids stay theirs while this is cached
        ordered = sorted(self.turns, key=lambda t: t.at)
        self.ats = [t.at for t in ordered]
        self.totals = {k: [0, *itertools.accumulate(getattr(t, k) for t in ordered)] for k in SERIES}
        self.drawn: dict[tuple, object] = {}   # what was made of them for a window (buckets, chart)

    def upto(self, now: float) -> int:
        """How many of the turns are at or before `now`: with the window, all that a chart of
        them depends on, whatever the second."""
        return bisect.bisect_right(self.ats, now)

    def total(self, key: str, since: float, now: float) -> int:
        """The series `key` over the turns from `since` to `now`, both included."""
        sums = self.totals[key]
        return sums[bisect.bisect_right(self.ats, now)] - sums[bisect.bisect_left(self.ats, since)]

    def kept(self, key: tuple, make):
        """What `make` makes of the turns for `key`, made once. The window steps on every
        minute or so, and each step's is wanted only until the next."""
        if key not in self.drawn:
            if len(self.drawn) >= 16:
                self.drawn.clear()
            self.drawn[key] = make()
        return self.drawn[key]


SERIES_KEPT: dict[tuple, Series] = {}   # the last few lists of turns charted, by their turns' ids


def series(turns: list[Turn]) -> Series:
    """The Series for these turns, built once while they stay the same: a snapshot's turns
    change only with a read that brought some, and running sessions' combined turns are a
    new list of the same turns each second (combine)."""
    key = tuple(map(id, turns))
    found = SERIES_KEPT.pop(key, None) or Series(turns)
    SERIES_KEPT[key] = found   # the newest last: the oldest goes first
    while len(SERIES_KEPT) > 8:
        del SERIES_KEPT[next(iter(SERIES_KEPT))]
    return found


def buckets(turns: list[Turn], now: float, columns: int, span: int = SPAN) -> list[dict]:
    """The window's buckets, oldest first, each summing its turns. Made once for each window
    and its turns (Series): the list is shared, so don't change it."""
    s = series(turns)
    start, _ = window(now, columns, span)
    return s.kept(("buckets", columns, span, start, s.upto(now)), lambda: _buckets(turns, now, columns, span))


def _buckets(turns: list[Turn], now: float, columns: int, span: int) -> list[dict]:
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
    recent: int = 0   # its series' tokens over its span, on its legend's line
    loading: bool = False   # its older history is still being read
    more: bool = True   # there's older history to zoom out to
    zoom_focus: int | None = None   # the zoom's button with the focus, 0 for - and 1 for + (SessionStats.part)


def chart(turns: list[Turn], now: float, width: int, height: int, kind: str = "assembly", span: int = SPAN,
          gap: bool = False, history: list[History] = (), loading: bool = False, more: bool = True) -> Chart:
    """A chart of the turns over `span`, and wider than SPAN of the history (older) too."""
    keys = [k for k, _ in CHARTS[kind][1]]
    s, columns = series(turns), max(width - MARGIN, 0)
    start, step = window(now, columns, span)
    history = list(history) if span > SPAN else []
    serials = tuple(h.serial for h in history)

    def bars() -> tuple[list, float]:
        bs = buckets(turns, now, columns, span)
        if history:
            bs = [{k: b[k] + o[i] for i, k in enumerate(SERIES)} for b, o in zip(bs, older(history, start, step, columns))]
        totals = [sum(b[k] for k in keys) for b in bs]
        maxt = nice(max(totals, default=0))
        return [build_column([(CO[k], b[k]) for k in keys], t, maxt, height) for b, t in zip(bs, totals)], maxt
    # the columns, made once for each window, its turns (Series) and history: only the span's total moves between
    built, maxt = s.kept(("chart", kind, columns, span, height, start, s.upto(now), serials), bars)
    return Chart(built, maxt, height, start, span, kind, gap,
                 recent(turns, now, keys, span) + older_total(history, start, step, columns, keys), loading, more)


def recent(turns: list[Turn], now: float, keys: list[str], span: int = SPAN) -> int:
    """The tokens of the series `keys`, the bars' own, in turns from `span` before `now` to
    `now`, both included: by timestamp, not the chart's buckets, whose oldest starts earlier."""
    s = series(turns)
    return sum(s.total(k, now - span, now) for k in keys)


HISTORY_KEPT: dict[tuple, object] = {}   # what was made of the histories charted lately (older, older_total)


def kept_history(key: tuple, make):
    if key not in HISTORY_KEPT:
        while len(HISTORY_KEPT) >= 16:
            del HISTORY_KEPT[next(iter(HISTORY_KEPT))]
        HISTORY_KEPT[key] = make()
    return HISTORY_KEPT[key]


def older(history: list[History], start: float, step: float, columns: int) -> list[list[float]]:
    """The histories' SERIES sums in each of the window's columns, oldest first: a BUCKET
    shared among the columns it overlaps, by how much of it each covers."""
    width = BUCKET / step   # a bucket's width in columns

    def make():
        out = [[0.0] * columns for _ in SERIES]   # by series, then column
        for h in history:
            for b, sums in h.items():
                lo = (b - start) / step
                i, hi = max(math.floor(lo), 0), min(lo + width, columns)
                while i < hi:
                    share = (min(hi, i + 1) - max(lo, i)) / width
                    for k, y in enumerate(sums):
                        out[k][i] += share * y
                    i += 1
        return list(zip(*out))
    return kept_history(("older", tuple(h.serial for h in history), start, step, columns), make)


def older_total(history: list[History], start: float, step: float, columns: int, keys: list[str]) -> int:
    """The tokens of the series `keys` in the histories, as the bars have them (older): a
    BUCKET the window's start cuts counted for the part of it the bars show."""
    if not history:
        return 0
    at = [SERIES.index(k) for k in keys]
    return round(sum(col[i] for col in older(history, start, step, columns) for i in at))


def charts(turns: list[Turn], now: float, width: int, room: int, height: int, span: int = SPAN,
           history: list[History] = (), loading: bool = False, more: bool = True) -> list[Chart]:
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
            return [chart(turns, now, width, bars, kind, span, bool(i) and gap, history, loading, more)
                    for i, kind in enumerate(kinds)]
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
    # the span's tokens at the right, a five-character number's room kept (tokens() never
    # needs more) so the count's size never changes which keys show. The keys sit a space apart,
    # and those that don't fit beside it are left out whole, the first ones first (D82). Short of
    # room the note shortens, then goes, then the name is cut: the line never wraps
    width = MARGIN + len(c.columns)
    count, since = tokens(c.recent), span_name(c.span)
    full = "loading…" if c.loading else f"{count:>5} tokens/last {since}"   # the total waits for the history
    note = Text(next((n for n in (full, full if c.loading else f"{count}/{since}")
                      if head.cell_len + 2 + len(n) <= width), ""),
                style=hexc(DIM))
    keys = [Text("▆", style=hexc(CO[k])) + Text(" " + legend, style=hexc(DIM)) for k, legend in series
            if note.plain == full]   # beside the full note only, so a shortened one never brings keys back
    while keys and head.cell_len + 2 + sum(k.cell_len + 1 for k in keys) - 1 + 2 + note.cell_len > width:
        keys.pop(0)
    if keys:
        head += Text("  ") + Text(" ").join(keys)
    head += Text(" " * max(width - head.cell_len - note.cell_len, 2 if note else 0)) + note
    head.truncate(width)
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
    buttons = zoom(c.span, c.more, c.zoom_focus) if n >= ZOOM_MIN else Text()   # the zoom's, at the right, room permitting
    room = n - buttons.cell_len - bool(buttons)
    axis, free = [" "] * room, 0
    for tick, lab in axis_ticks(c.start, c.span, n):
        pos = round((tick - c.start) / c.span * n)
        if free <= pos and pos + len(lab) <= room:   # a label that doesn't fit at its tick is left out, not moved off it
            axis[pos:pos + len(lab)], free = lab, pos + len(lab) + 1
    out.append(Text(" " * MARGIN + "".join(axis) + " " * bool(buttons), style=hexc(DIM)) + buttons)
    return out


def tick_label(at: float, span: int) -> str:
    """A tick's label: its time; at 12 hours and up, with its weekday; from a day up, its date
    at midnight (Fri 16, or past a week 16 Oct), else its time."""
    lt = time.localtime(at)
    hm = f"{lt.tm_hour}:{lt.tm_min:02d}"
    if span >= DAY:
        if lt.tm_hour or lt.tm_min:
            return hm
        return time.strftime(f"%a {lt.tm_mday}" if span <= WEEK else f"{lt.tm_mday} %b", lt)
    return time.strftime(f"%a {hm}", lt) if span >= 12 * HOUR else hm


def axis_ticks(start: float, span: int, columns: int) -> list[tuple[float, str]]:
    """The axis ticks over a chart's span, each as (when, its label), on the local clock: at
    the span's finest step (TICK, else an eighth of the span) or, where their labels would
    crowd at this width, the next of STEPS (then weeks doubling) that leaves room for them."""
    end, longest = start + span, 6 if span >= DAY else 9 if span >= 12 * HOUR else 5
    steps = [*STEPS, *(WEEK << k for k in range(1, max(span // WEEK, 1).bit_length() + 1))]
    finest = TICK.get(span, span / 8)
    step = next((x for x in steps if x >= finest and x / span * columns >= longest + 2), steps[-1])
    if step < HOUR:   # from the local hour's start: not UTC's, which is off by half an hour in some zones
        lt = time.localtime(start)
        hour = int(start) - lt.tm_min * 60 - lt.tm_sec
        at = [hour + k * step for k in range((int(start) - hour) // step + 1, int(end - hour) // step + 1)]
    else:   # on local hours or midnights, through any change of the clocks
        day, at = date.fromtimestamp(start), []
        while (midnight := time.mktime((day.year, day.month, day.day, 0, 0, 0, 0, 0, -1))) <= end:
            n = day.toordinal() - 1   # 0 on a Monday
            if step < DAY:
                at += [time.mktime((day.year, day.month, day.day, h, 0, 0, 0, 0, -1)) for h in range(0, 24, step // HOUR)]
            elif n % (step // DAY) == 0 if step < WEEK else n % 7 == 0 and n // 7 % (step // WEEK) == 0:
                at.append(midnight)   # every day, every other day, or Mondays
            day = date.fromordinal(day.toordinal() + 1)
    return [(t, tick_label(t, span)) for t in at if start < t <= end]


# the charts' zoom: - and + at the right of the hour ticks, one level for both charts

def wider(span: int) -> int:
    """The zoom level out from `span`: the next of SPANS, past a week twice as wide."""
    return next((x for x in SPANS if x > span), None) or span * 2


def finer(span: int) -> int | None:
    """The zoom level in from `span`, None from the finest."""
    return span // 2 if span > WEEK else next((x for x in reversed(SPANS) if x < span), None)


def span_name(span: int) -> str:
    for unit, name in ((WEEK, "w"), (DAY, "d"), (HOUR, "h"), (60, "m")):
        if span % unit == 0:
            return f"{span // unit}{name}"
    return f"{span}s"


def zoom_parts(span: int, more: bool = True) -> list[tuple[str, int | None, bool]]:
    """The zoom's pieces, left to right: each one's text, the span a click on it goes to
    (None for none) and whether it's a button. - goes wider while there's `more` history, and
    always back out to the default."""
    return [(span_name(span).rjust(3) + " ", None, False),
            (" - ", wider(span) if more or span < SPAN else None, True),
            (" ", None, False), (" + ", finer(span), True)]


def zoom(span: int, more: bool = True, focused: int | None = None) -> Text:
    """The zoom: the span shown, then - and +, each dimmed where it can go no further, and the
    one with the focus (0 for -, 1 for +) lit as the focused slider's level is."""
    out, n = Text(), 0
    for text, to, button in zoom_parts(span, more):
        if button and n == focused:
            style = f"bold #000000 on {hexc(ACCENT)}"
        else:
            style = (f"bold {hexc(TEXT)} on {hexc(DIM2)}" if to else hexc(DIM2)) if button else hexc(DIM)
        n += button
        out.append(text, style=style)
    return out


def zoom_target(span: int, more: bool, button: int) -> int | None:
    """The span the zoom's - (0) or + (1) goes to, None where it can go no further."""
    return [to for _, to, is_button in zoom_parts(span, more) if is_button][button]


ZOOM_MIN = 2 * zoom(SPAN).cell_len   # the columns below which the ticks keep the line to themselves


def zoom_spans(span: int, width: int, more: bool = True) -> list[tuple[int, int, int]]:
    """Where the zoom's buttons are on the hour ticks' line at a width, from its left: each
    one's (first cell, past its last, the span it goes to), for those that go anywhere."""
    if width - MARGIN < ZOOM_MIN:
        return []
    out, x = [], width - sum(len(text) for text, _, _ in zoom_parts(span, more))
    for text, to, _ in zoom_parts(span, more):
        if to:
            out.append((x, x + len(text), to))
        x += len(text)
    return out
