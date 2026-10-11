import functools
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_wheelhouse import launch, store as store_module, tui
from claude_wheelhouse.store import Store

SHIPPED_DWELL = tui.DWELL   # before no_dwell_timer puts it out of reach
NO_DEFAULT_DB = Path("/nonexistent-wheelhouse-test-state/wheelhouse.db")   # unwritable: a reach for it fails


def live_state_dir() -> Path:
    return (Path.home() / ".local/state/claude-wheelhouse").resolve()


def pytest_configure(config):
    """A run never touches the person's own wheelhouse. A session the wheelhouse launched
    hands its tests WHEELHOUSE_DB, pointing at the live database: the run stops before any
    test if the database or its state directory still resolves under the live one."""
    store_module.DEFAULT_DB = NO_DEFAULT_DB
    live = live_state_dir()
    for name, path in [("store.DEFAULT_DB", store_module.DEFAULT_DB), ("launch.db_path()", launch.db_path()),
                       ("state dir", launch.db_path().parent)]:
        if path.resolve().is_relative_to(live):
            raise pytest.UsageError(f"test isolation: {name} is {path}, under the live wheelhouse {live}. "
                                    "Unset WHEELHOUSE_DB (and WHEELHOUSE_SESSION_ID, WHEELHOUSE_TOKEN) and run again.")


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Every test has a database of its own (db_file is this one), wherever the run was started."""
    path = tmp_path / "wheelhouse.db"
    monkeypatch.setenv("WHEELHOUSE_DB", str(path))
    monkeypatch.setattr(store_module, "DEFAULT_DB", path)
    monkeypatch.delenv("WHEELHOUSE_SESSION_ID", raising=False)
    monkeypatch.delenv("WHEELHOUSE_TOKEN", raising=False)
    return path


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
def db_file(isolated_db):
    return isolated_db


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
    HELD = {"tick", "animate"}   # the app's own intervals, which run only when a test calls them

    def __init__(self):
        self.t, self.timers, self.app, self.held = 0.0, [], None, set()

    def bind(self, app) -> None:
        """The app the test is running, whose lists advance draws. A new one starts with none
        of the last one's timers."""
        if app is not self.app:
            self.app, self.timers, self.held = app, [], set()

    def now(self) -> float:
        return self.t

    def after(self, delay, callback):
        timer = SimpleNamespace(due=self.t + delay, callback=callback, stopped=False)
        timer.stop = lambda: setattr(timer, "stopped", True)
        self.timers.append(timer)
        return timer

    def advance(self, by: float) -> None:
        """by seconds on: the timers due by then fire, and both lists draw the frame for then."""
        assert self.app is None or self.held == self.HELD, f"the app's intervals held: {self.held}"
        self.t = round(self.t + by, 9)   # whole nanoseconds: 0.05 s steps add up to 0.40 s exactly
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
    every, once = tui.WheelhouseApp.set_interval, tui.WheelhouseApp.set_timer

    def interval(app, delay, callback, **kw):
        # fail closed on the app's own bound-method timers, which would run on real time unknown to
        # this fixture; lambdas and Textual's own (App, widget) timers pass through on real time
        if getattr(callback, "__self__", None) is app and callback.__name__ in vars(tui.WheelhouseApp):
            if callback.__name__ not in Clock.HELD:
                raise AssertionError(f"the clock fixture doesn't know the app's interval {callback.__name__}")
            clock.bind(app)
            clock.held.add(callback.__name__)
            return SimpleNamespace(stop=lambda: None)
        return every(app, delay, callback, **kw)

    def timer(app, delay, callback=None, **kw):
        if getattr(callback, "__self__", None) is app and callback.__name__ in vars(tui.WheelhouseApp):
            raise AssertionError(f"the clock fixture doesn't know the app's timer {callback.__name__}")
        return once(app, delay, callback, **kw)
    monkeypatch.setattr(tui.WheelhouseApp, "set_interval", interval)
    monkeypatch.setattr(tui.WheelhouseApp, "set_timer", timer)
    return clock


TOKEN = "the-test-token"


class ServedDaemon:
    """A daemon serving a database over a real socket, on a thread of its own, for the TUI
    to reach as it reaches the real one (client.RemoteClient). It ticks only when told
    (tick): a test's refresh ticks it first, as the hub in process read the store then."""

    def __init__(self, path, port: int = 0):
        import socket
        import threading

        import uvicorn

        from sse_starlette.sse import AppStatus

        from claude_wheelhouse import daemon as daemon_mod
        AppStatus.should_exit = False   # sse-starlette's, for the whole process: set when the last one stopped
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)   # a daemon started again on its port
        sock.bind(("127.0.0.1", port))
        self.port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.store = Store(path)
        self.daemon = daemon_mod.Daemon(self.store, daemon_mod.hosts("127.0.0.1", self.port), TOKEN, inline=True,
                                        ticks=False)
        self.server = daemon_mod.Server(self.daemon, uvicorn.Config(self.daemon.app, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [sock]}, daemon=True)
        self.thread.start()
        for _ in range(500):
            if self.server.started:
                break
            time.sleep(0.01)
        assert self.server.started, "the test daemon didn't start"
        self.tick()

    def tick(self) -> None:
        import asyncio
        asyncio.run_coroutine_threadsafe(self.daemon.on_hub(self.daemon.tick), self.daemon.loop).result(30)

    def client(self, **kw):
        from claude_wheelhouse.client import RemoteClient
        return RemoteClient(self.url, TOKEN, **kw)

    def stop(self) -> None:
        if self.thread.is_alive():
            import signal
            self.server.handle_exit(signal.SIGTERM, None)   # as systemd stops it: the streams end first
            self.thread.join(10)
            self.daemon.watch.close()


@pytest.fixture
def served():
    """served(path): a ServedDaemon on that database, stopped at the test's end."""
    running = []

    def serve(path, port: int = 0):
        running.append(ServedDaemon(path, port))
        return running[-1]
    yield serve
    for d in running:
        d.stop()


def pytest_generate_tests(metafunc):
    """A test marked both runs twice: through a hub in process (--local), and through the
    daemon (via)."""
    marker = metafunc.definition.get_closest_marker("both")
    if marker:   # threaded=False: a test whose change is a transcript file a dead session never reads again
        threaded = marker.kwargs.get("threaded", True)
        metafunc.parametrize("via", ["local", "daemon", *(["threaded"] if threaded else [])], indirect=True)


def caught_up(client, served, refresh) -> None:
    """The daemon's tick, then the client's threads given time to hear all it published and
    fetch what it moved, then the refresh that swaps it in: as a refresh tick a second after
    a change, but on the test's clock."""
    served.tick()
    feed, deadline = served.daemon.feed, time.monotonic() + 15
    while True:
        with client.lock:
            settled = (client.last_event is not None and int(client.last_event) >= feed.seq and not client.dirty
                       and not client.in_flight)
        if settled or time.monotonic() > deadline:
            break
        time.sleep(0.005)
    refresh()


@pytest.fixture(autouse=True)
def via(request, monkeypatch):
    """How WheelhouseApp(store) reaches the hub: in process, or (marked both) through a
    daemon served on that store's database. "daemon": a client that fetches at each refresh,
    after the daemon's tick. "threaded": the client as it runs, its stream and fetching on
    threads of their own, what they bring swapped in at the refresh; each refresh ticks the
    daemon first and waits for the client to have heard and fetched all it published. app.store
    is still the test's store, to write as hosts do and read what the app did; app.hub the
    daemon's."""
    how = getattr(request, "param", "local")
    if how == "local":
        yield how
        return
    daemons = {}
    made = []
    init = tui.WheelhouseApp.__init__

    def through_daemon(self, store=None, client=None):
        if client is not None or store is None:
            return init(self, store, client)
        served = daemons.get(store.path)
        if served is None:
            served = daemons[store.path] = ServedDaemon(store.path)
        if how == "daemon":
            client = served.client(synchronous=True, before_fetch=served.tick)
        else:
            client = served.client()
            client.refresh = functools.partial(caught_up, client, served, client.refresh)
            start = client.start
            # the stream's notices posted without waiting on the app: a refresh waiting here on
            # the stream would otherwise wait on itself
            def start_threads(post=None, start=start):
                import asyncio
                loop = asyncio.get_running_loop()
                start(lambda callback: loop.call_soon_threadsafe(callback))
            client.start = start_threads
        made.append(client)
        init(self, client=client)
        self.store, self.hub = store, served.daemon.hub
    monkeypatch.setattr(tui.WheelhouseApp, "__init__", through_daemon)
    yield how
    for client in made:
        client.disconnect()
    for served in daemons.values():
        served.stop()
