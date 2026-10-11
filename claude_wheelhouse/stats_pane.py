"""The stats module: the session stats pane under the inbox's item list, plugged in through
the module protocol (api.py) like any other module. What it reads and draws is stats.py's;
this is the widget, its followers and its workers."""

import functools
import importlib.metadata
import time

from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual.geometry import Region
from textual.strip import Strip
from textual.style import Style as TextualStyle
from textual.visual import Visual, visualize
from textual.widget import Widget

from . import api, stats


def looks(rows: list[Text]) -> list[tuple]:
    """Rows to compare: Rich's Text equality leaves out the base style."""
    return [(t.plain, t.style, t.spans) for t in rows]


class SessionStats(Widget):
    """A bordered panel of the context, session and weekly gauges and the cache and
    compaction rows, then charts of the last two hours' context assembly and output; with
    no session in context, the running sessions' totals. Transcripts are read on worker
    threads, never on the UI thread: a first read of a big one takes a while, and even a
    steady one stats every subagent file. Each session's follower is shared with the
    session list's context bars (stats.follower), so a transcript is read once for both. The panel and the charts' columns are rebuilt on
    each tick; animate only recolours the columns."""

    DEFAULT_CSS = "SessionStats { height: 1fr; background: #000000; border-top: solid #7b61ff; }"

    def __init__(self, ctx: api.Context, **kwargs):
        super().__init__(**kwargs)
        self.ctx = ctx
        self.followers: dict[str, stats.UsageFollower] = {}
        self.usage = ctx.usage   # the hub's, fetched once for the whole wheelhouse
        self.view, self.name_, self.now = None, "", 0.0
        self.note: str | None = None   # what shows with nothing to show
        self.rows: list[Text] = []
        self.charts: list[stats.Chart] = []
        self.frame = 0
        self.drawn: list[Strip] = []   # the lines as last drawn whole (render_line), and what for
        self.drawn_for: tuple | None = None
        self.bars: dict[int, tuple[stats.Chart, int, int]] = {}   # the charts' bars' lines (bars_at)
        self.bar_runs: list[tuple[int, int]] = []   # those lines, each chart's as (first, how many)
        self.drawn_frame = 0
        self.cell_styles: dict[tuple, Style] = {}   # a bar cell's style as drawn, by its line's and its own
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
        """The sessions shown: the one in context, else every running one."""
        sid = self.ctx.focus()
        if any(s["id"] == sid for s in sessions):
            return [sid]
        return [s["id"] for s in sessions if s["running"]]

    def read(self, follower: stats.UsageFollower, now: float) -> None:
        """On a worker thread: one read of a session's transcripts, then a repaint if it
        brought anything (or was the first)."""
        first = not follower.ready
        if stats.read_safely(follower, now) or first:
            try:
                self.app.call_from_thread(self.repaint)
            except RuntimeError:   # the app is closing
                pass

    def repaint(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        sessions = self.ctx.sessions()
        names = {s["id"]: s["name"] for s in sessions}
        host = {s["id"]: s.get("context") for s in sessions}
        sid = self.ctx.focus()
        if sid in names:
            follower = self.followers.get(sid)
            if follower and follower.ready:
                self.show(stats.current(follower.snap, host[sid]), names[sid], now, follower.error)
            else:
                self.show(None, names[sid], now, (follower and follower.error)
                          or f"reading {names[sid]}'s transcript…")
            return
        sids = [sid for sid in self.shown(sessions) if sid in self.followers]
        snaps = {sid: stats.current(self.followers[sid].snap, host[sid]) for sid in sids if self.followers[sid].ready}
        self.show(stats.combine(snaps, names, now) if snaps else None, "", now,
                  "reading transcripts…" if len(snaps) < len(sids) else None)

    # drawing

    def show(self, view, name: str, now: float, note: str | None = None) -> None:
        self.view, self.name_, self.now, self.note = view, name, now, note
        self.rebuild()

    def rebuild(self) -> None:
        rows, charts = stats.layout(self.view, self.name_, self.note, self.usage, self.now,
                                    self.size.width, self.size.height)
        if (looks(rows), charts) != (looks(self.rows), self.charts):   # each tick builds them again, mostly the same
            self.rows, self.charts = rows, charts
            self.drawn_for = None
            self.refresh()

    def on_resize(self) -> None:
        self.rebuild()

    def render(self) -> Text:
        return stats.render(self.rows, self.charts, self.frame)

    def selection_updated(self, selection) -> None:
        self.drawn_for = None
        super().selection_updated(selection)

    def bars_at(self, width: int) -> dict[int, tuple[stats.Chart, int, int]]:
        """Where the charts' bars are drawn, by line: (chart, row, render's line).
        Textual wraps a line too long for the pane onto the next (a legend, in a narrow pane);
        the bars just fit."""
        lines = stats.render(self.rows, self.charts, self.frame).split("\n")
        out, y = {}, 0
        for n, (line, bar) in enumerate(zip(lines, stats.bar_lines(self.rows, self.charts))):
            if bar:
                out[y] = (*bar, n)
            y += 1 if bar or line.cell_len <= width else len(
                Visual.to_strips(self, visualize(self, line), width, None, self.visual_style))
        return out

    def render_line(self, y: int) -> Strip:
        """Every line drawn from render, as Widget's are, only for a new panel, size or look;
        a shimmer frame (animate) draws again the charts' bars alone, a line at a time from
        their cells' styles, not the whole panel's Text: that was a fifth of a core at 5 Hz."""
        width, height = self.size
        key = (self.size, self.visual_style, self.text_selection, self.text_selection and self.frame)
        if self.drawn_for != key:
            self.drawn_for, self.cell_styles = key, {}
            self.drawn = Visual.to_strips(self, visualize(self, self.render()), width, height, self.visual_style)
            self.drawn_frame = self.frame
            self.bars = {} if self.text_selection else self.bars_at(width)
            self.bar_runs = [(min(ys), len(ys)) for ys in ([y for y, bar in self.bars.items() if bar[0] is chart]
                                                         for chart in self.charts) if ys]
        if y >= len(self.drawn):
            return Strip.blank(width, self.visual_style.rich_style)
        if y not in self.bars or self.frame == self.drawn_frame:
            return self.drawn[y]
        c, row, n = self.bars[y]
        label = stats.bar_label(c, row)
        # as Textual draws the line's Text: a segment for each styled character and one for
        # each run of plain ones, each with where it starts in the Text (x, n), which a
        # selection by mouse goes by
        segments, plain, x = [], None, 0
        for ch, style in [(ch, None) for ch in label.plain] + stats.bar(c, row, self.frame):
            if style is None and plain is not None:
                plain[0] += ch
            else:
                look = self.cell_styles.get((label.style, style))
                if look is None:
                    look = self.visual_style + TextualStyle.from_rich_style(self.app.console.get_style(label.style))
                    if style is not None:
                        look += TextualStyle.from_rich_style(style)
                    look = self.cell_styles[label.style, style] = look.rich_style
                at = self.offsets.get((x, n)) or self.offsets.setdefault((x, n), Style.from_meta({"offset": (x, n)}))
                plain = [ch, look + at] if style is None else None
                segments.append(plain or Segment(ch, look + at))
            x += 1
        return Strip.join([Strip([Segment(*s) if isinstance(s, list) else s for s in segments]), self.drawn[y].crop(x)])


module = api.Module(
    id="stats", title="Stats", version=importlib.metadata.version("claude-wheelhouse"), api=api.WHEELHOUSE_API,
    panes=[api.Pane("Stats", "inbox.side", {"tui": SessionStats})])
