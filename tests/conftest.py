import functools
import itertools
import json
import os
import signal
import subprocess
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_wheelhouse import api, features, launch, liveness, store as store_module, tui
from claude_wheelhouse.store import Store

SHIPPED_DWELL = tui.DWELL   # before no_dwell_timer puts it out of reach
NO_CLAUDE = "/nonexistent/claude-is-never-run-in-tests"
NO_DEFAULT_DB = Path("/nonexistent-wheelhouse-test-state/wheelhouse.db")   # unwritable: a reach for it fails


def live_state_dir() -> Path:
    return (Path.home() / ".local/state/claude-wheelhouse").resolve()


def pytest_collection_modifyitems(config, items):
    """WHEELHOUSE_TEST_SHARD=i/n runs every nth test from the ith: CI runs the suite as
    several processes in turn, as each worker's memory grows test by test (#188)."""
    shard = os.environ.get("WHEELHOUSE_TEST_SHARD")
    if not shard:
        return
    i, n = (int(part) for part in shard.split("/"))
    config.hook.pytest_deselected(items=[item for k, item in enumerate(items) if k % n != i - 1])
    items[:] = items[i - 1::n]


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
def stores_closed(monkeypatch):
    """Every Store a test opens, its own, its app's or its hub's, is closed once the test is
    done: an open SQLite connection keeps its database, WAL and shared memory open for the
    rest of the run. First of the autouse fixtures, so closed last."""
    opened = []
    init = Store.__init__

    def opening(self, *args, **kwargs):
        opened.append(self)
        init(self, *args, **kwargs)
    monkeypatch.setattr(Store, "__init__", opening)
    yield
    for store in opened:
        if hasattr(store, "db"):
            store.db.close()


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Every test has a database of its own (db_file is this one), wherever the run was started."""
    path = tmp_path / "wheelhouse.db"
    monkeypatch.setenv("WHEELHOUSE_DB", str(path))
    monkeypatch.setattr(store_module, "DEFAULT_DB", path)
    monkeypatch.delenv("WHEELHOUSE_SESSION_ID", raising=False)
    monkeypatch.delenv("WHEELHOUSE_TOKEN", raising=False)
    monkeypatch.delenv("WHEELHOUSE_PORT", raising=False)   # a test that wants one sets it (features.port)
    for setting in ("ENABLE_SDK", "USAGE_MODE", "KEEPALIVE_HOURS", "ANIMATION_FPS"):   # features.given's
        monkeypatch.delenv(f"WHEELHOUSE_{setting}", raising=False)
    return path


SHIPPED_LOAD = api.load   # every module installed, as the app loads them


@pytest.fixture(autouse=True)
def without_design(monkeypatch):
    """The modules installed, less the design and PRs modules: the app's layout and Tab order
    are tested with the stats pane alone, as written before them. Their panes' own tests
    (test_decisions_pane.py, test_prs_pane.py) load them themselves, and every_module tests
    the shipped default."""
    monkeypatch.setattr(api, "load", lambda entries=None: [m for m in SHIPPED_LOAD(entries)
                                                           if m.name not in ("design", "prs")])


@pytest.fixture
def with_prs(monkeypatch):
    """The modules installed less the design module, the PRs pane's among them (its gh logged
    out, no_gh): the tour walks its dialog."""
    monkeypatch.setattr(api, "load", lambda entries=None: [m for m in SHIPPED_LOAD(entries) if m.name != "design"])


@pytest.fixture
def middle_of_two(monkeypatch, every_module):
    """Every module but the PRs: the middle column of two slots (the inbox and the Decisions),
    where a pane with nothing to show shrinks to its note (WheelhouseApp.fit_compact)."""
    monkeypatch.setattr(api, "load", lambda entries=None: [m for m in SHIPPED_LOAD(entries) if m.name != "prs"])


@pytest.fixture(autouse=True)
def no_gh(monkeypatch):
    """No test runs the real gh: the PRs pane's route, wherever it's loaded, finds it logged
    out unless a test puts its own stub there (test_prs_pane.py)."""
    try:
        from claude_wheelhouse_prs import pane
    except ImportError:
        return
    monkeypatch.setattr(pane, "RUNNER", lambda argv: (4, "", "To get started with GitHub CLI, please run:  gh auth login"))


@pytest.fixture
def every_module(monkeypatch):
    """Every module installed loaded, as a default install runs (without_design undone)."""
    monkeypatch.setattr(api, "load", SHIPPED_LOAD)


@pytest.fixture(autouse=True)
def sdk_off(tmp_path, monkeypatch):
    """SDK mode off, as a new user has it, whatever .env the run starts beside or the person
    keeps in ~/.config: a test that needs it on takes sdk_on."""
    monkeypatch.setattr(features, "_flag", False)
    monkeypatch.setattr(features, "env_files", lambda: [tmp_path / "no-cwd.env", tmp_path / "no-config.env"])


@pytest.fixture(autouse=True)
def usage_mode_default(monkeypatch):
    """No --usage-mode, --port or --keepalive-hours given, and nothing yet said of a bad
    usage mode or keepalive limit (features.usage_mode, features.keepalive_hours)."""
    monkeypatch.setattr(features, "_usage_mode", None)
    monkeypatch.setattr(features, "_port", None)
    monkeypatch.setattr(features, "_keepalive_hours", None)
    monkeypatch.setattr(features, "_animation_fps", None)
    monkeypatch.setattr(features, "_warned", set())


@pytest.fixture
def sdk_on(monkeypatch):
    """SDK mode on, as --enable-sdk turns it on."""
    monkeypatch.setattr(features, "_flag", True)


@pytest.fixture(autouse=True)
def tab_runner(monkeypatch):
    """Sessions made in tests run as tabs (launch is stubbed per test) unless a test picks
    sdk: a real host would start Claude Code."""
    monkeypatch.setenv("WHEELHOUSE_RUNNER", "tab")


@pytest.fixture(autouse=True)
def no_usage_fetch(monkeypatch):
    """The stats pane's plan usage comes from a real Claude Code: never run in tests. The
    hub's polls in this process do nothing, and a Probe made without a command of its own,
    here or in a daemon a test starts, runs a claude that isn't there (test_plan_usage gives it a fake)."""
    monkeypatch.setattr("claude_wheelhouse.plan_usage.AccountUsage.poll", lambda self, probe: None)
    monkeypatch.setenv("WHEELHOUSE_CLAUDE", NO_CLAUDE)   # in the environment: a daemon a test starts has it too


@pytest.fixture(autouse=True)
def no_tutorial_offer(monkeypatch):
    """The first-run tutorial offer would sit over every test that starts from an empty
    wheelhouse, and a top-up's over every other: tests of the offer itself put them back."""
    monkeypatch.setattr("claude_wheelhouse.tutorial.should_offer", lambda store: False)
    # nor an upgrader's top-up, or its badge: every feature counts as seen (tutorial.state)
    from claude_wheelhouse import tutorial, whatsnew
    real = tutorial.state
    monkeypatch.setattr(tutorial, "state", lambda store: {**real(store), "seen": [f.id for f in whatsnew.FEATURES]})


@pytest.fixture
def first_press():
    """Opts a test into the first-press dialog (tui.FirstSteps), which steps_answered skips."""


@pytest.fixture(autouse=True)
def steps_answered(request, monkeypatch):
    """The session steps' first-press dialog as answered, so a press of Compact, Forget or
    Relaunch goes straight on; a test of the dialog takes first_press."""
    if "first_press" not in request.fixturenames:
        monkeypatch.setattr(tui.WheelhouseApp, "first_press", lambda self, button: False)


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
    dwell holds however slowly a loaded run goes: a Sink's moves (Sink.now), the one-shot
    timers of the dwell and a text box's quiet spell (WheelhouseApp.after), and the refresh
    tick and animation, which run only when a test calls tick or animate. Time moves only by
    advance. A key's land keeps to real time: it bounds a wait on the app's own queues."""
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
def frame_clock(monkeypatch):
    """The animation's clock (WheelhouseApp.now) a frame later each time it is read, as at
    10 fps whatever the real time: each app.animate() moves everything one frame."""
    reads = itertools.count()
    monkeypatch.setattr(tui.WheelhouseApp, "now", staticmethod(lambda: next(reads) / tui.FRAMES))


@pytest.fixture
def clock(monkeypatch, frame_clock):
    """Clock: the app's timers on the test's own time, an animation frame a clock.animate
    (frame_clock). The dwell lapses at its shipped length (tui.DWELL), so a test resting on
    an item rests that long."""
    clock = Clock()
    monkeypatch.setattr(tui.Sink, "now", staticmethod(clock.now))
    monkeypatch.setattr(tui.WheelhouseApp, "after", lambda self, delay, callback: clock.after(delay, callback))
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
            return SimpleNamespace(stop=lambda: None, pause=lambda: None, resume=lambda: None)   # as Timer (pace)
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
        from sse_starlette.sse import AppStatus
        AppStatus.should_exit = False   # left set by the stop, it ended the next test's streams at once (test_daemon)


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
    """A test marked both runs three ways: through a hub in process (--local), and through
    the daemon, fetching at each refresh or on the client's own threads (via)."""
    if metafunc.definition.get_closest_marker("both"):
        metafunc.parametrize("via", ["local", "daemon", "threaded"], indirect=True)


def caught_up(client, served, refresh) -> None:
    """The daemon's tick, then the client's threads given time to hear all it published and
    fetch what it moved, and to poll each subagent's conversation shown (AGENT_POLL) once
    since, then the refresh that swaps it in: as a refresh tick a second after a change, but
    on the test's clock."""
    served.tick()
    feed, deadline = served.daemon.feed, time.monotonic() + 15
    with client.lock:
        since = client.seq
    while True:
        with client.lock:
            agents = {k for k in client.wanted | client.asked if k[0] in ("agent", "doc", "file")}
            polled = all(k in client.waiting and client.waiting[k][0] > since for k in agents)
            settled = (client.last_event is not None and int(client.last_event) >= feed.seq and not client.dirty
                       and not client.in_flight and polled)
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
            monkeypatch.setattr("claude_wheelhouse.client.AGENT_POLL", 0.05)   # caught_up waits for one
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


RUN_STARTED = liveness.start_time(os.getpid())   # this pytest process's start, in clock ticks since boot
REFUSED = []   # every signal refused, as (the test running, the call and why)
running = ["(no test yet)"]   # the test under way, or the last one, for a thread that outlives it
real_kill, real_killpg = os.kill, os.killpg


def foreign(pid: int) -> bool:
    """A process that was running before this pytest process started: none of the run's own."""
    start = liveness.start_time(pid)
    return start is not None and start < RUN_STARTED


def refuse(name: str, target: int, sig: int, members: list[int]) -> None:
    """Raises PermissionError, and records it, for a signal aimed at pid 0 or below (every
    process of ours, or a group), at this pytest process, or at a process or group older
    than the run. Signal 0 (is it alive?) is let through."""
    if sig == 0:
        return
    reason = ("pid 0 or below" if target <= 0 else "this pytest process" if target == os.getpid()
              else "a process older than the run" if any(foreign(p) for p in members) else None)
    if reason is None:
        return
    call = f"{name}({target}, {signal.Signals(sig).name}): {reason}"
    REFUSED.append((running[0], call))
    raise PermissionError(f"test guard: {call}")


def guarded_kill(pid, sig):
    refuse("os.kill", pid, sig, [pid])
    real_kill(pid, sig)


def guarded_killpg(group, sig):
    refuse("os.killpg", group, sig, group_members(group))
    real_killpg(group, sig)


# for the whole run, not per test: a thread that outlives its test is held to it too
os.kill, os.killpg = guarded_kill, guarded_killpg
liveness.killpg = guarded_killpg


@pytest.fixture(autouse=True)
def only_own_processes_signalled(request):
    """No test signals a process it didn't start (refuse): the call is refused, and the test
    fails naming it. Where there is no /proc (macOS) only the pid checks apply."""
    running[0] = request.node.nodeid
    before = len(REFUSED)
    yield
    mine = [call for _, call in REFUSED[before:]]
    assert not mine, f"{request.node.nodeid} signalled processes it didn't start: {mine}"


def group_members(group: int) -> list[int]:
    """The pids in a process group, from /proc (none where there is no /proc)."""
    members = []
    for stat in liveness.PROC.glob("[0-9]*/stat"):
        try:
            fields = stat.read_text().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if fields[2] == str(group):   # field 5, pgrp
            members.append(int(stat.parent.name))
    return members


@pytest.fixture(autouse=True)
def no_group_kill(monkeypatch):
    """No test kills a real process group: liveness.stop's fallback SIGKILL is recorded, and
    its timer never starts a thread. A test of it reads what was recorded."""
    killed = []
    monkeypatch.setattr(liveness, "getpgid", lambda pid: -pid)
    monkeypatch.setattr(liveness, "killpg", lambda group, sig: killed.append((group, sig)))
    return killed


@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    """Claude Code's config directory in the test's own (CLAUDE_CONFIG_DIR, which the Agent
    SDK's fork_session reads), and a way to write a session's transcript there: say(sid, cwd)."""
    home = tmp_path / "claude-home"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    from claude_agent_sdk import project_key_for_directory

    def say(sid: str, cwd: str, text: str = "hello") -> Path:
        path = home / "projects" / project_key_for_directory(cwd) / f"{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"type": "user", "uuid": str(uuid.uuid4()), "parentUuid": None, "sessionId": sid,
                                    "cwd": cwd, "timestamp": "2026-01-01T00:00:00Z",
                                    "message": {"role": "user", "content": text}}) + "\n")
        return path
    say.projects = home / "projects"
    return say


def git(cwd, *args) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def worktree(tmp_path):
    """A real git repository, main, with one commit, and a linked worktree of it on branch
    feat/x at main/.worktrees/feat-x: (main, the worktree)."""
    main = tmp_path / "repo"
    main.mkdir()
    git(main, "init", "-q", "-b", "main")
    git(main, "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "first")
    linked = main / ".worktrees" / "feat-x"
    git(main, "worktree", "add", "-q", "-b", "feat/x", str(linked))
    return main, linked
