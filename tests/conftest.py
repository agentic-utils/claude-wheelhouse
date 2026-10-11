from types import SimpleNamespace

import pytest

from claude_wheelhouse import tui
from claude_wheelhouse.store import Store

SHIPPED_DWELL = tui.DWELL   # before no_dwell_timer puts it out of reach


@pytest.fixture(autouse=True)
def tab_runner(monkeypatch):
    """Sessions made in tests run as tabs (launch is stubbed per test) unless a test picks
    sdk: a real host would start Claude Code."""
    monkeypatch.setenv("WHEELHOUSE_RUNNER", "tab")


@pytest.fixture(autouse=True)
def no_usage_fetch(monkeypatch):
    """The stats pane's account usage comes from a live endpoint: never called in tests."""
    monkeypatch.setattr("claude_wheelhouse.stats.AccountUsage.fetch", lambda self: None)


@pytest.fixture(autouse=True)
def no_tutorial_offer(monkeypatch):
    """The first-run tutorial offer would sit over every test that starts from an empty
    wheelhouse: tests of the offer itself put it back."""
    monkeypatch.setattr("claude_wheelhouse.tutorial.should_offer", lambda store: False)


@pytest.fixture(autouse=True)
def no_dwell_timer(monkeypatch):
    """The dwell (tui.DWELL) never lapses on its own in a test, wherever a slow run puts its
    second: a test rests on an item with test_tui.dwell, or runs on the clock fixture, where
    it lapses at its shipped length as the test moves the clock on."""
    monkeypatch.setattr("claude_wheelhouse.tui.DWELL", 3600)


@pytest.fixture
def db_file(tmp_path, monkeypatch):
    path = tmp_path / "wheelhouse.db"
    monkeypatch.setenv("WHEELHOUSE_DB", str(path))
    return path


@pytest.fixture
def store(db_file):
    return Store(db_file)


@pytest.fixture
def sid(store, tmp_path):
    return store.create_session(str(tmp_path), name="demo", ticket="#7", brief="do the thing")


class Clock:
    """The app's timers on a clock of the test's own, so what a test asserts of a move or a
    dwell holds however slowly a loaded run goes: a Sink's moves (Sink.now), the dwell's
    one-shot timer (WheelhouseApp.dwell_after), and the refresh tick and animation, which
    run only when a test calls tick or animate. Time moves only by advance. A key's land
    keeps to real time: it bounds a wait on the app's own queues."""

    def __init__(self):
        self.t, self.timers, self.app = 0.0, [], None

    def now(self) -> float:
        return self.t

    def after(self, delay, callback):
        timer = SimpleNamespace(due=self.t + delay, callback=callback, stopped=False)
        timer.stop = lambda: setattr(timer, "stopped", True)
        self.timers.append(timer)
        return timer

    def advance(self, by: float) -> None:
        """by seconds on: the timers due by then fire, and both lists draw the frame for then."""
        self.t += by
        for timer in [t for t in self.timers if t.due <= self.t and not t.stopped]:
            timer.stop()
            timer.callback()
        if self.app is not None and self.app.screen_stack:
            self.app.session_sink.repaint()
            self.app.item_sink.repaint()

    @staticmethod
    def tick(app) -> None:
        """The refresh tick firing."""
        app.tick()

    @staticmethod
    def animate(app) -> None:
        """An animation frame firing."""
        app.animate()

    async def finish_moves(self, pilot, app) -> None:
        """Both lists' moves run to their end, a frame at a time."""
        for _ in range(20):
            if app.session_sink.move is None and app.item_sink.move is None:
                return
            self.advance(tui.Sink.SECONDS / 4)
            await pilot.pause()
        raise AssertionError("still moving")


@pytest.fixture
def clock(monkeypatch):
    """Clock: the app's timers on the test's own time. The dwell lapses at its shipped length
    (tui.DWELL), so a test resting on an item rests that long."""
    clock = Clock()
    monkeypatch.setattr(tui.Sink, "now", staticmethod(clock.now))
    monkeypatch.setattr(tui.WheelhouseApp, "dwell_after", lambda self, delay, callback: clock.after(delay, callback))
    monkeypatch.setattr(tui, "DWELL", SHIPPED_DWELL)
    every = tui.WheelhouseApp.set_interval

    def interval(app, delay, callback, **kw):
        if getattr(callback, "__self__", None) is app and callback.__name__ in ("tick", "animate"):
            clock.app = app   # the app the test is running, whose lists advance draws
            return SimpleNamespace(stop=lambda: None)
        return every(app, delay, callback, **kw)
    monkeypatch.setattr(tui.WheelhouseApp, "set_interval", interval)
    return clock
