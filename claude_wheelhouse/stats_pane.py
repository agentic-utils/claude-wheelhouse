"""The stats module: the session stats pane under the inbox's item list, plugged in through
the module protocol (api.py) like any other module. What it reads and draws is stats.py's;
this is the widget, its followers and its workers."""

import functools
import importlib.metadata
import time

from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.geometry import Region
from textual.strip import Strip
from textual.style import Style as TextualStyle
from textual.visual import Visual, visualize
from textual.widget import Widget

from . import api, models, stats


def looks(rows: list[Text]) -> list[tuple]:
    """Rows to compare: Rich's Text equality leaves out the base style."""
    return [(t.plain, t.style, t.spans) for t in rows]


class SessionStats(Widget):
    """A bordered panel of the context, session and weekly gauges and the cache and
    compaction rows, then charts of the last two hours' context assembly and output (zoomed
    by the - and + under them, both together, for as long as the app runs: out past two
    hours, the older history is read on a worker, and kept); with
    no session in context, every session's totals. Transcripts are read on worker
    threads, never on the UI thread: a first read of a big one takes a while, and even a
    steady one stats every subagent file. Each session's follower is shared with the
    session list's context bars (stats.follower), so a transcript is read once for both. The panel and the charts' columns are rebuilt on
    each tick; animate only recolours the columns."""

    DEFAULT_CSS = "SessionStats { height: 1fr; background: #000000; border-top: solid #7b61ff; }"
    # the effort slider's, while it shows (a session run by a host in context): only then does
    # the pane take focus, after the inbox's own stops (TAB_ORDER)
    BINDINGS = [Binding("left", "effort(-1)", "Lower effort", show=False),
                Binding("right", "effort(1)", "Higher effort", show=False),
                Binding("home", "effort_end(0)", "Lowest effort", show=False),
                Binding("end", "effort_end(-1)", "Highest effort", show=False)]

    def __init__(self, ctx: api.Context, **kwargs):
        super().__init__(**kwargs)
        self.ctx = ctx
        self.followers: dict[str, stats.UsageFollower] = {}
        self.usage = ctx.usage   # the hub's, fetched once for the whole wheelhouse
        self.view, self.name_, self.now = None, "", 0.0
        self.note: str | None = None   # what shows with nothing to show
        self.setting: dict | None = None   # the session shown's model and effort (Hub.model_state)
        self.can_focus = False
        self.rows: list[Text] = []
        self.charts: list[stats.Chart] = []
        self.span = stats.SPAN   # the charts' zoom (stats.SPANS, stats.wider)
        self.filling = False     # the shown sessions' older history is still being read for it (not Widget.loading, which covers the pane)
        self.more = True         # there's older history than it covers (stats.zoom_parts)
        self.frame = 0
        self.drawn: list[Strip] = []   # the lines as last drawn whole (render_line), and what for
        self.drawn_for: tuple | None = None
        self.bars: dict[int, tuple[stats.Chart, int, int]] = {}   # the charts' bars' lines (bars_at)
        self.bar_runs: list[tuple[int, int]] = []   # those lines, each chart's as (first, how many)
        self.drawn_frame = 0
        self.cell_styles: dict[str, dict] = {}   # a bar line's cell's style as drawn, by its line's and its colour
        self.plans: dict[int, tuple] = {}   # each bar line's frames, its last bar and what follows (plan)
        self.cycles: dict[int, tuple] = {}   # each bar line's frames for a cycle but its last bar, kept through a tick
        self.offsets: dict[tuple[int, int], Style] = {}   # where a cell is in render's Text, for a selection

    # the hub's calls (api.py)

    def tick(self) -> None:
        now = time.time()
        sessions = self.ctx.sessions()
        present = {s["id"] for s in sessions}
        for sid in [sid for sid in self.followers if sid not in present]:   # ended: its follower goes
            del self.followers[sid]
        for sid in self.shown(sessions):
            follower = self.followers.setdefault(sid, stats.follower(sid))
            if not follower.reading:
                follower.reading = True
                self.run_worker(functools.partial(self.read, follower, now), thread=True, group="stats",
                                exit_on_error=False)
        self.fill(now)
        self.repaint(now)

    def landed(self, sid: str) -> None:
        """The hub's read of a session's transcript brought something: shown at once if
        this pane shows that session, from the shared follower, with no read of its own."""
        if sid in self.shown(self.ctx.sessions()):
            self.repaint()

    def animate(self, frame: int) -> None:
        self.frame = frame
        if self.charts and self.display and self.text_selection:   # drawn whole, the selection with it
            self.refresh()
        elif self.charts and self.display:
            # the bars' lines are the content's: refresh puts them under the border itself
            self.refresh(*(Region(0, y, self.size.width, n) for y, n in self.bar_runs))

    # reading

    def shown(self, sessions: list[dict]) -> list[str]:
        """The sessions shown: the one in context, else every one, running or not: the charts
        show all their activity."""
        sid = self.ctx.focus()
        if any(s["id"] == sid for s in sessions):
            return [sid]
        return [s["id"] for s in sessions]

    def read(self, follower: stats.UsageFollower, now: float) -> None:
        """On a worker thread: one read of a session's transcripts, then a repaint if it
        brought anything (or was the first)."""
        first = not follower.ready
        if stats.read_safely(follower, now) or first:
            try:
                self.app.call_from_thread(self.repaint)
            except RuntimeError:   # the app is closing
                pass

    def fill(self, now: float) -> None:
        """Zoomed out past what a shown session's follower holds: its older history read on a
        worker (stats.UsageFollower.load), once at a time. A new session's too, once read."""
        cutoff = now - self.span
        for sid in self.shown(self.ctx.sessions()) if self.span > stats.SPAN else ():
            follower = self.followers.get(sid)
            if follower and follower.short(cutoff) and not follower.loading and not follower.resting(now):
                follower.loading = True
                self.run_worker(functools.partial(self.load, follower, cutoff), thread=True, group="stats",
                                exit_on_error=False)

    def load(self, follower: stats.UsageFollower, cutoff: float) -> None:
        """On a worker thread: a follower's older history, then a repaint."""
        stats.load_safely(follower, cutoff)
        try:
            self.app.call_from_thread(self.repaint)
        except RuntimeError:   # the app is closing
            pass

    def repaint(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        sessions = self.ctx.sessions()
        # the history shown: the session in context's, else every session's
        cutoff, shown = now - self.span, [self.followers[sid] for sid in self.shown(sessions) if sid in self.followers]
        self.filling = self.span > stats.SPAN and any(f.short(cutoff) and not f.resting(now) for f in shown)
        start, _ = stats.window(now, max(self.size.width - stats.MARGIN, 0), self.span)   # the charts' left edge
        self.more = not all(f.done for f in shown) or any(f.oldest is not None and f.oldest < start for f in shown)
        failed = next((f.history_error for f in shown if f.history_error), None) if self.span > stats.SPAN else None
        names = {s["id"]: s["name"] for s in sessions}
        host = {s["id"]: s.get("context") for s in sessions}
        sid = self.ctx.focus()
        if sid in names:
            self.setting = next(s for s in sessions if s["id"] == sid)
            follower = self.followers.get(sid)
            if follower and follower.ready:
                self.show(stats.current(follower.snap, host[sid]), names[sid], now, follower.error or failed)
            else:
                self.show(None, names[sid], now, (follower and follower.error)
                          or f"reading {names[sid]}'s transcript…")
            return
        self.setting = None
        sids = [sid for sid in self.shown(sessions) if sid in self.followers]
        snaps = {sid: stats.current(self.followers[sid].snap, host[sid]) for sid in sids if self.followers[sid].ready}
        self.show(stats.combine(snaps, names, now) if snaps else None, "", now,
                  "reading transcripts…" if len(snaps) < len(sids) else failed)

    # drawing

    def show(self, view, name: str, now: float, note: str | None = None) -> None:
        self.view, self.name_, self.now, self.note = view, name, now, note
        self.rebuild()

    def rebuild(self) -> None:
        self.can_focus = self.slider() is not None and self.ctx.set_effort is not None
        rows, charts = stats.layout(self.view, self.name_, self.note, self.usage, self.now,
                                    self.size.width, self.size.height, self.setting, self.has_focus,
                                    span=self.span, loading=self.filling, more=self.more)
        if (looks(rows), charts) != (looks(self.rows), self.charts):   # each tick builds them again, mostly the same
            self.rows, self.charts = rows, charts
            self.drawn_for = None
            self.refresh()

    def on_resize(self) -> None:
        self.rebuild()

    # the effort slider

    def slider(self) -> str | None:
        """The session whose effort the slider shows, if it shows."""
        return self.setting["id"] if stats.has_slider(self.view, self.setting) else None

    def on_focus(self) -> None:
        # the slider shows its keys: once has_focus says so, which Textual sets only after
        # this handler, so a rebuild now would lay it out unfocused until the next tick
        self.call_later(self.rebuild)

    def on_blur(self) -> None:
        self.call_later(self.rebuild)

    def action_effort(self, step: int) -> None:
        """A level lower or higher, applied live: no warning, as effort has no side effects (Q52)."""
        if self.slider() is None:
            return
        levels = models.EFFORTS
        current = stats.effort_of(self.view, self.setting)   # what the slider lights
        at = levels.index(current) if current in levels else levels.index("high")
        self.pick(levels[max(0, min(len(levels) - 1, at + step))])

    def action_effort_end(self, end: int) -> None:
        if self.slider() is not None:
            self.pick(models.EFFORTS[end])

    def pick(self, level: str) -> None:
        sid = self.slider()
        if sid is not None and level != stats.effort_of(self.view, self.setting) and self.ctx.set_effort is not None:
            self.ctx.set_effort(sid, level)

    def on_click(self, event: events.Click) -> None:
        """A click on a level picks it, or on an arrow the level it steps to: whichever form
        the slider is drawn in at this width (stats.slider_spans). One on the charts' - or +
        zooms them both out or in (stats.zoom_spans)."""
        content = self.content_region
        x, y = event.screen_x - content.x - 1, event.screen_y - content.y   # 1: the panel's left border
        if self.charts and y == self.line_ys(self.size.width)[-1]:   # the hour ticks', under the last chart
            to = next((to for start, end, to in stats.zoom_spans(self.span, self.size.width, self.more)
                       if start <= x + 1 < end), None)
            if to is not None:
                self.span = to
                self.fill(time.time())
                self.repaint()
            return
        if self.slider() is None or y != stats.SLIDER_ROW:
            return
        spans = stats.slider_spans(stats.effort_of(self.view, self.setting), self.size.width - 2)
        level = next((level for start, end, level in spans if start <= x < end), None)
        if level is not None:
            self.pick(level)

    def render(self) -> Text:
        return stats.render(self.rows, self.charts, self.frame)

    def selection_updated(self, selection) -> None:
        self.drawn_for = None
        super().selection_updated(selection)

    def bars_at(self, width: int) -> dict[int, tuple[stats.Chart, int, int]]:
        """Where the charts' bars are drawn, by line (line_ys): (chart, row, render's line)."""
        bars = stats.bar_lines(self.rows, self.charts)
        return {y: (*bar, n) for n, (y, bar) in enumerate(zip(self.line_ys(width), bars)) if bar}

    def line_ys(self, width: int) -> list[int]:
        """Where each of render's lines is drawn. Textual wraps a line too long for the pane
        onto the next (a legend, in a narrow pane); the bars just fit."""
        lines = stats.render(self.rows, self.charts, self.frame).split("\n")
        out, y = [], 0
        for line, bar in zip(lines, stats.bar_lines(self.rows, self.charts)):
            out.append(y)
            y += 1 if bar or line.cell_len <= width else len(
                Visual.to_strips(self, visualize(self, line), width, None, self.visual_style))
        return out

    def render_line(self, y: int) -> Strip:
        """Every line drawn from render, as Widget's are, only for a new panel, size or look;
        a shimmer frame (animate) draws again the charts' bars alone, a line at a time, not the
        whole panel's Text: that was a fifth of a core at 5 Hz. Each line's frames come round
        each cycle of the wave (stats.PERIOD), so they're drawn once and recalled (plan)."""
        width, height = self.size
        key = (self.size, self.visual_style, self.text_selection, self.text_selection and self.frame)
        if self.drawn_for != key:
            self.drawn_for, self.cell_styles, self.plans = key, {}, {}
            self.drawn = Visual.to_strips(self, visualize(self, self.render()), width, height, self.visual_style)
            self.drawn_frame = self.frame
            self.bars = {} if self.text_selection else self.bars_at(width)
            self.cycles = {y: cycle for y, cycle in self.cycles.items() if y in self.bars}
            self.bar_runs = [(min(ys), len(ys)) for ys in ([y for y, bar in self.bars.items() if bar[0] is chart]
                                                         for chart in self.charts) if ys]
        if y >= len(self.drawn):
            return Strip.blank(width, self.visual_style.rich_style)
        if y not in self.bars or self.frame == self.drawn_frame:
            return self.drawn[y]
        cycle, last, join, tail, length = self.plans.get(y) or self.plan(y)
        k = self.frame % stats.PERIOD
        segments = cycle[k] or self.draw(y, k)
        if join:   # the frames' last plain run, which tail carries with the blank after it
            segments = segments[:-1]
        if last:
            (ch, at), cells, f, label = last
            rgb = stats.tints(cells, f, self.frame)[0]
            segments = segments + [Segment(ch, (self.cell_styles[label].get(rgb) or self.look(label, rgb)) + at)]
        return Strip(segments + tail, length)

    def look(self, label: str, rgb: tuple | None) -> Style:
        """A bar line's cell's style as Textual draws it: the pane's, its line's (the label's)
        and its colour's, if any."""
        looks = self.cell_styles.setdefault(label, {})
        look = self.visual_style + TextualStyle.from_rich_style(self.app.console.get_style(label))
        if rgb is not None:
            look += TextualStyle.from_rich_style(stats.style_of(rgb))
        look = looks[rgb] = look.rich_style
        return look

    def offset(self, x: int, n: int) -> Style:
        """Where a cell is in render's Text, (x, n), which a selection by mouse goes by."""
        return self.offsets.get((x, n)) or self.offsets.setdefault((x, n), Style.from_meta({"offset": (x, n)}))

    def plan(self, y: int) -> tuple:
        """A bar line's frames, as Textual draws its Text but for where its segments split
        (draw). Its frames for a cycle of the shimmer (stats.PERIOD) are kept, each drawn once,
        but for its last bar's: the current one, which a turn changes, is drawn every frame.
        They're kept through a tick and drawn again only for a new look of the line but its
        last bar: the bars move on a column, the scale changes or the pane's look does."""
        c, row, n = self.bars[y]
        label = stats.bar_label(c, row)
        cells = stats.bar_cells(c, row)
        styles = self.cell_styles.setdefault(label.style, {})
        plain = styles.get(None) or self.look(label.style, None)
        f = stats.row_shade(c, row)
        x = len(label.plain) + len(c.columns)   # where the line's bars end
        before, end = [col[row] for col in c.columns[:-1]], (c.columns[-1][row] if c.columns else None)
        key = (n, row, label.plain, label.style, tuple(before), f, self.visual_style)
        if self.cycles.get(y, (None,))[0] != key:
            # the line but its last bar, as runs of plain characters, each a segment, and
            # runs of bars' cells, each cell as its place and character
            parts, run = [], None
            for at, (ch, bar) in enumerate([(ch, False) for ch in label.plain] +
                                           [(ch, True) if base else (" ", False) for base, ch in before]):
                if not run or run[0] != bar:
                    run = [bar, []]
                    parts.append(run)
                run[1].append((at, ch))
            parts = [[(ch, self.offset(at, n)) for at, ch in run] if bar
                     else Segment("".join(ch for _, ch in run), plain + self.offset(run[0][0], n)) for bar, run in parts]
            self.cycles[y] = (key, [None] * stats.PERIOD, (parts, cells[:-1] if end and end[0] else cells, f, label.style))
        rest = self.drawn[y].crop(x)
        last, tail, join = None, list(rest), False
        if end and end[0]:
            last = (end[1], self.offset(x - 1, n)), cells[-1:], f, label.style
        elif end:   # no last bar on this line: its blank joins the plain run before it, if one ends the frames
            run = self.cycles[y][2][0][-1] if self.cycles[y][2][0] else None
            join = isinstance(run, Segment)
            tail.insert(0, Segment(run.text + " ", run.style) if join else Segment(" ", plain + self.offset(x - 1, n)))
        plan = self.plans[y] = (self.cycles[y][1], last, join, tail, x + rest.cell_length)
        return plan

    def draw(self, y: int, k: int) -> list[Segment]:
        """Frame `k` of a bar line's cycle (plan), but for its last bar: a segment for each run
        of plain characters, as Textual draws them, and one for each bar's cell, with where it
        is in render's Text."""
        _, frame, (parts, cells, f, label) = self.cycles[y]
        rgbs = iter(stats.tints(cells, f, k))
        styles = self.cell_styles.setdefault(label, {})
        segments = []
        for part in parts:
            if isinstance(part, Segment):
                segments.append(part)
                continue
            for ch, at in part:
                rgb = next(rgbs)
                segments.append(Segment(ch, (styles.get(rgb) or self.look(label, rgb)) + at))
        frame[k] = segments
        return segments

module = api.Module(
    id="stats", title="Stats", version=importlib.metadata.version("claude-wheelhouse"), api=api.WHEELHOUSE_API,
    panes=[api.Pane("Stats", "inbox.side", {"tui": SessionStats})])
