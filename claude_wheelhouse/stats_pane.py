"""The stats module: the session stats pane under the inbox's item list, plugged in through
the module protocol (api.py) like any other module. What it reads and draws is stats.py's;
this is the widget, its followers and its workers."""

import functools
import importlib.metadata
import time

from rich.text import Text
from textual.widget import Widget

from . import api, stats


class SessionStats(Widget):
    """A bordered panel of the context, session and weekly gauges and the cache and
    compaction rows, then charts of the last two hours' context assembly and output; with
    no session in context, the running sessions' totals. Transcripts are read on worker
    threads, never on the UI thread: a first read of a big one takes a while, and even a
    steady one stats every subagent file. The panel and the charts' columns are rebuilt on
    each tick; animate only recolours the columns."""

    DEFAULT_CSS = "SessionStats { height: 1fr; background: #000000; border-top: solid #7b61ff; }"

    def __init__(self, ctx: api.Context, **kwargs):
        super().__init__(**kwargs)
        self.ctx = ctx
        self.followers: dict[str, stats.UsageFollower] = {}
        self.usage = stats.AccountUsage()
        self.view, self.name_, self.now = None, "", 0.0
        self.note: str | None = None   # what shows with nothing to show
        self.rows: list[Text] = []
        self.charts: list[stats.Chart] = []
        self.frame = 0

    # the hub's calls (api.py)

    def tick(self) -> None:
        now = time.time()
        if self.usage.due(now):
            self.run_worker(self.usage.fetch, thread=True, group="usage", exit_on_error=False)
        sessions = self.ctx.sessions()
        present = {s["id"] for s in sessions}
        for sid in [sid for sid in self.followers if sid not in present]:   # ended: its follower goes
            del self.followers[sid]
        for sid in self.shown(sessions):
            follower = self.followers.setdefault(sid, stats.UsageFollower(sid))
            if not follower.reading:
                follower.reading = True
                self.run_worker(functools.partial(self.read, follower, now), thread=True, group="stats",
                                exit_on_error=False)
        self.repaint(now)

    def animate(self, frame: int) -> None:
        self.frame = frame
        if self.charts and self.display:
            self.refresh()

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
        try:
            changed = follower.read(now)
            follower.error = None
        except Exception as e:   # a transcript gone between stat and open, say: the pane says so, the app carries on
            changed, follower.error = True, f"couldn't read the transcript: {e}"[:120]
        finally:
            follower.reading = False
        if changed or first:
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
        self.rows, self.charts = stats.layout(self.view, self.name_, self.note, self.usage, self.now,
                                              self.size.width, self.size.height)
        self.refresh()

    def on_resize(self) -> None:
        self.rebuild()

    def render(self) -> Text:
        return stats.render(self.rows, self.charts, self.frame)


module = api.Module(
    id="stats", title="Stats", version=importlib.metadata.version("claude-wheelhouse"), api=api.WHEELHOUSE_API,
    panes=[api.Pane("Stats", "inbox.side", {"tui": SessionStats})])
