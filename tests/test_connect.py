"""The TUI finding its daemon (connect.py), with real daemon processes: started when none
runs, joined when one does, restarted when it runs older code from this package, and a
lock left by one that died, one that hangs, or a port taken, each said as it is."""

import contextlib
import fcntl
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from claude_wheelhouse import auth, connect, features, fingerprint
import conftest


needs_proc = pytest.mark.skipif(not Path("/proc").exists(), reason="reads another process's /proc")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def state(db_file, monkeypatch):
    """A state dir of the test's own, its daemon (any the test starts) stopped at the end."""
    state_dir = db_file.parent
    port = free_port()
    monkeypatch.setenv("WHEELHOUSE_PORT", str(port))
    yield state_dir, port
    # every daemon it started stopped: one asked for late, on a loaded machine, can take the
    # lock after the first has gone, so until none has held it for a while
    deadline, free_since = time.monotonic() + 20, time.monotonic()
    while time.monotonic() < deadline and time.monotonic() - free_since < 3:
        held = connect.holder(state_dir)
        if held and (pid := held.split()[1]).isdigit():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pid), signal.SIGTERM)
            free_since = time.monotonic()
        time.sleep(0.05)


def daemon_pid(state_dir) -> int:
    return int(connect.holder(state_dir).split()[1])


def test_none_running_starts_one_and_the_next_joins_it(state):
    state_dir, port = state
    assert connect.holder(state_dir) is None
    url, token, said = connect.local(state_dir, port)
    assert url == f"http://127.0.0.1:{port}" and said == []
    assert token == auth.read(auth.path(state_dir)) and oct(auth.path(state_dir).stat().st_mode)[-3:] == "600"
    pid = daemon_pid(state_dir)
    assert connect.local(state_dir, port) == (url, token, []), "the next joins it"
    assert daemon_pid(state_dir) == pid
    log = (state_dir / connect.LOG).read_text()
    assert f"pid {os.getpid()} asked for a daemon" in log and f"wheelhouse daemon (pid {pid}) on {url}" in log


@needs_proc
def test_the_daemon_keeps_its_start_pipe_from_what_it_starts(state):
    """The pipe's write end, named on its command line, stays open in the daemon (its
    closing says it has exited) but is close-on-exec, so no host or tab it starts holds it."""
    state_dir, port = state
    connect.local(state_dir, port)
    pid = daemon_pid(state_dir)
    argv = open(f"/proc/{pid}/cmdline").read().split("\0")
    assert "--start-pipe" in argv, argv
    fd = argv[argv.index("--start-pipe") + 1]
    assert os.readlink(f"/proc/{pid}/fd/{fd}").startswith("pipe:"), "kept open"
    flags = next(int(line.split()[1], 8) for line in open(f"/proc/{pid}/fdinfo/{fd}") if line.startswith("flags:"))
    assert flags & os.O_CLOEXEC, f"inheritable: flags {flags:o}"


def test_the_first_run_starts_a_daemon_with_no_state_dir_yet(tmp_path, monkeypatch):
    """A new install: nothing has made the state dir, which daemon.log goes in, before the app starts a daemon."""
    state_dir = tmp_path / "fresh" / "claude-wheelhouse"
    monkeypatch.setenv("WHEELHOUSE_DB", str(state_dir / "wheelhouse.db"))
    port = free_port()
    try:
        url, _, said = connect.local(state_dir, port)
        assert url == f"http://127.0.0.1:{port}" and said == []
        assert f"pid {os.getpid()} asked for a daemon" in (state_dir / connect.LOG).read_text()
    finally:
        connect.halt(state_dir)


@pytest.mark.parametrize("on", [False, True])
def test_a_daemon_started_by_an_app_in_sdk_mode_is_in_it_too(tmp_path, monkeypatch, on):
    """It runs in the state dir, where the starting directory's .env isn't: so it is told."""
    monkeypatch.setattr(features, "_flag", on)
    ran = []
    monkeypatch.setattr(connect.subprocess, "run", lambda argv, **k: ran.append(argv))
    os.close(connect.start(tmp_path, 7428))
    assert (features.FLAG in ran[0]) is on, ran


@pytest.mark.parametrize("mode", ["oneshot", "persistent"])
def test_a_daemon_started_by_an_app_reads_plan_usage_as_the_app_would(tmp_path, monkeypatch, mode):
    """Its .env isn't the starting directory's: so it is told the mode."""
    features.set_usage_mode(mode)
    ran = []
    monkeypatch.setattr(connect.subprocess, "run", lambda argv, **k: ran.append(argv))
    os.close(connect.start(tmp_path, 7428))
    argv = ran[0]
    assert argv[argv.index(features.USAGE_FLAG) + 1] == mode, argv


def test_a_lock_file_left_by_a_daemon_that_died_is_only_text(state):
    state_dir, port = state
    (state_dir / connect.LOCK).write_text("pid 999999 at http://127.0.0.1:1\n")
    assert connect.holder(state_dir) is None, "not held: none runs, whatever it says"
    url, _, _ = connect.local(state_dir, port)
    assert url == f"http://127.0.0.1:{port}" and daemon_pid(state_dir) != 999999


def test_a_daemon_on_older_code_from_this_package_is_restarted(state, monkeypatch):
    state_dir, port = state
    connect.local(state_dir, port)
    old = daemon_pid(state_dir)
    monkeypatch.setattr(fingerprint, "code", lambda: "code-after-a-pull")
    url, token, said = connect.local(state_dir, port)
    assert said[0] == "the daemon ran older code: restarted it on this code", said
    assert daemon_pid(state_dir) != old, "a new daemon"
    with pytest.raises(ProcessLookupError):
        os.kill(old, 0)


def test_a_daemon_from_another_package_is_joined_if_its_api_matches(state, monkeypatch):
    state_dir, port = state
    connect.local(state_dir, port)
    pid = daemon_pid(state_dir)
    monkeypatch.setattr(fingerprint, "code", lambda: "another-checkout")
    monkeypatch.setattr(fingerprint, "PACKAGE", fingerprint.PACKAGE.parent / "elsewhere")
    _, _, said = connect.local(state_dir, port)
    assert daemon_pid(state_dir) == pid and said == [f"the daemon (pid {pid}) runs other code, from "
                                                     f"{fingerprint.PACKAGE.parent}/claude_wheelhouse"], said
    monkeypatch.setattr(fingerprint, "API", 3)
    with pytest.raises(connect.NoDaemon, match="speaks API 2, this wheelhouse 3"):
        connect.local(state_dir, port)


def test_a_daemon_that_holds_its_lock_and_never_answers_is_left_alone(state, monkeypatch):
    state_dir, port = state
    monkeypatch.setattr(connect, "START_WAIT", 0.3)
    auth.ensure(state_dir)
    with open(state_dir / connect.LOCK, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock.write(f"pid {os.getpid()} at http://127.0.0.1:{free_port()}\n")
        lock.flush()
        with pytest.raises(connect.NoDaemon, match="holds its lock but doesn't answer. It's left running"):
            connect.local(state_dir, port)


def test_a_port_taken_says_which_and_the_setting_that_moves_it(state, monkeypatch):
    state_dir, port = state
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", port))
        taken.listen()
        with pytest.raises(connect.NoDaemon) as e:
            connect.local(state_dir, port)
    assert str(e.value) == (f"port {port} is taken by another process (another user's wheelhouse?): it is set by "
                            "WHEELHOUSE_PORT: give another there"), "where the app's port was set (state sets the variable)"
    assert connect.holder(state_dir) is None


def test_a_daemon_on_another_port_is_joined_where_it_is_and_said(state):
    state_dir, port = state
    url, token, _ = connect.local(state_dir, port)
    asked = free_port()
    assert connect.local(state_dir, asked, True) == (url, token, [connect.elsewhere_said(url, asked, True)])


@pytest.mark.parametrize("url, port, given, said, desc", [
    ("http://127.0.0.1:7428", 7428, False, None, "the port asked for: nothing to say"),
    ("http://[::1]:7429", 7429, True, None, "IPv6's, the port asked for"),
    ("http://127.0.0.1:7429", 7428, False, "not the default 7428: joined it there", "another than the default"),
    ("http://100.64.0.7:7428", 7429, True, "not 7429 as set here: joined it there", "a VPN address's, another than set"),
])
def test_elsewhere_said(url, port, given, said, desc):
    text = connect.elsewhere_said(url, port, given)
    if said is None:
        assert text is None, desc
    else:
        assert text.startswith(f"the daemon runs on port {url.rsplit(':', 1)[1]}, {said}"), f"{desc}: {text}"


def test_a_daemon_gone_under_a_running_tui_is_started_again_a_few_times(state, monkeypatch):
    state_dir, port = state
    connect.local(state_dir, port)
    revive = connect.Reviver(state_dir, port)
    token = auth.read(auth.path(state_dir))
    assert revive() == (f"http://127.0.0.1:{port}", token), "running: nothing to do"
    monkeypatch.setattr(connect, "REVIVES", 1)
    for n in range(2):
        pid = daemon_pid(state_dir)
        os.kill(pid, signal.SIGKILL)   # it died
        deadline = time.monotonic() + 10
        while connect.holder(state_dir) is not None and time.monotonic() < deadline:
            time.sleep(0.05)
        url = revive()
        if n == 0:
            assert url == (f"http://127.0.0.1:{port}", token) and daemon_pid(state_dir) != pid, "started again"
        else:
            assert url is None and connect.holder(state_dir) is None, "past its limit: left to the person"


@pytest.mark.parametrize("file, env, asked, want, desc", [
    ("tok", None, None, "from-file", "a file"),
    (None, "from-env", None, "from-env", "the environment"),
    (None, None, "typed", "typed", "asked for, without echo"),
])
def test_the_token_for_connect_is_never_a_flags_value(tmp_path, file, env, asked, want, desc):
    (tmp_path / "tok").write_text("from-file\n")
    (tmp_path / "tok").chmod(0o600)
    environ = {"WHEELHOUSE_TOKEN": env} if env else {}
    got = connect.given_token(file and str(tmp_path / file), environ, ask=(lambda prompt: asked) if asked else None)
    assert got == want, desc


def test_no_token_for_connect_says_where_to_give_it(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(connect.NoDaemon, match="--token-file, or WHEELHOUSE_TOKEN"):
        connect.given_token(None, {})


def test_the_token_command_prints_the_one_the_daemon_made(db_file):
    env = dict(os.environ, PYTHONPATH=str(fingerprint.PACKAGE.parent))
    run = lambda: subprocess.run([sys.executable, "-m", "claude_wheelhouse", "token"], env=env,
                                 capture_output=True, text=True)
    assert "the daemon makes it the first time it starts" in run().stderr
    token, _ = auth.ensure(db_file.parent)
    assert run().stdout.strip() == token
    assert run().stdout.strip() == token, "the same each time: no rotation"


@pytest.mark.parametrize("mode, desc", [
    (0o644, "anyone may read it"),
    (0o640, "the group may"),
])
def test_the_token_command_wont_print_a_token_others_can_read(db_file, mode, desc):
    env = dict(os.environ, PYTHONPATH=str(fingerprint.PACKAGE.parent))
    token, _ = auth.ensure(db_file.parent)
    auth.path(db_file.parent).chmod(mode)
    run = subprocess.run([sys.executable, "-m", "claude_wheelhouse", "token"], env=env, capture_output=True, text=True)
    assert run.returncode != 0 and token not in run.stdout + run.stderr, f"{desc}: {run}"
    assert f"chmod 600 {auth.path(db_file.parent)}" in run.stderr, f"{desc}: {run.stderr}"


def test_a_daemon_that_refuses_the_token_says_so(state):
    state_dir, port = state
    url, token, _ = connect.local(state_dir, port)
    with pytest.raises(connect.NoDaemon, match="refuses this token"):
        connect.hello(url, "not-" + token, 2)


def test_a_daemon_started_again_reuses_the_token(state):
    state_dir, port = state
    _, token, _ = connect.local(state_dir, port)
    os.kill(daemon_pid(state_dir), signal.SIGTERM)
    deadline = time.monotonic() + 10
    while connect.holder(state_dir) is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert connect.local(state_dir, port)[1] == token


@pytest.mark.parametrize("held, url, desc", [
    ("pid 1 at http://127.0.0.1:7428", "http://127.0.0.1:7428", "the loopback"),
    ("pid 1 at http://[::1]:7428", "http://[::1]:7428", "IPv6's"),
    ("pid 1 at http://100.64.0.7:7428", "http://100.64.0.7:7428", "a VPN's"),
    ("pid 1", None, "not listening yet"),
])
def test_a_lock_names_the_address_once_listening(held, url, desc):
    assert connect.url_of(held) == url, desc


@pytest.mark.parametrize("held, pid, desc", [
    ("pid 12 at http://127.0.0.1:7428", 12, "listening"),
    ("pid 12", 12, "not listening yet"),
    (None, None, "none holds it"),
])
def test_pid_of(held, pid, desc):
    assert connect.pid_of(held) == pid, desc


def test_stop_waits_for_its_daemon_not_for_the_lock(tmp_path, monkeypatch):
    """The daemon asked to stop has gone, and another app's has taken the lock already:
    stopped, at once, not a wait for a lock that stays held."""
    monkeypatch.setattr(connect, "holder", lambda d: "pid 2 at http://127.0.0.1:1")
    monkeypatch.setattr(connect, "STOP_WAIT", 0.3)
    connect.stop("http://127.0.0.1:1", "t", tmp_path, 1)
    with pytest.raises(connect.NoDaemon, match="pid 2"):
        connect.stop("http://127.0.0.1:1", "t", tmp_path, 2)


def test_two_apps_starting_at_once_join_one_daemon(state):
    import threading
    state_dir, port = state
    got = []
    threads = [threading.Thread(target=lambda: got.append(connect.local(state_dir, port))) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert len(got) == 2 and got[0][0] == got[1][0] == f"http://127.0.0.1:{port}", got


def test_a_lock_let_go_while_joining_is_gone_round_again(tmp_path, monkeypatch):
    """Another app restarting the daemon (it ran older code) as this one joins: its lock goes
    while this one says hello, then comes back on the new daemon. Joined, not fatal. Scripted,
    with no daemon at all (the real race is above)."""
    state_dir, port = tmp_path, 1
    url = f"http://127.0.0.1:{port}"
    auth.ensure(state_dir)
    script = iter(["old", "gone", "new"])
    now = {"held": f"pid 999999 at {url}"}

    def hello(u, token, wait, state_dir=None):
        step = next(script)
        if step == "gone":
            raise connect.Gone()
        return {"code": fingerprint.code() if step == "new" else "old", "package": "elsewhere", "api": fingerprint.API,
                "pid": 2}
    first = {"done": False}

    def hello_once(*a, **k):
        if not first["done"]:
            first["done"] = True
            next(script)
            raise connect.Gone()
        return hello(*a, **k)
    monkeypatch.setattr(connect, "hello", hello_once)
    monkeypatch.setattr(connect, "holder", lambda d: now["held"])
    monkeypatch.setattr(connect, "start", lambda d, p: pytest.fail("started one while one ran"))
    assert connect.local(state_dir, port)[0] == url


def test_a_daemon_the_tests_start_cant_run_claude(state):
    """conftest's WHEELHOUSE_CLAUDE reaches a daemon started in its own process, past the
    monkeypatches: its /usage poll runs a claude that isn't there, and says so."""
    state_dir, port = state
    connect.local(state_dir, port)
    log, deadline = "", time.monotonic() + 15
    while "/usage didn't answer" not in log and time.monotonic() < deadline:
        time.sleep(0.1)
        log = (state_dir / connect.LOG).read_text()
    assert f"couldn't start Claude Code: [Errno 2] No such file or directory: '{conftest.NO_CLAUDE}'" in log, log


@needs_proc
def test_the_daemon_never_sees_the_apps_token_in_its_environment(state, monkeypatch):
    state_dir, port = state
    monkeypatch.setenv("WHEELHOUSE_TOKEN", "a-connect-token")
    connect.local(state_dir, port)
    environ = open(f"/proc/{daemon_pid(state_dir)}/environ", "rb").read().split(b"\0")
    assert not [e for e in environ if e.startswith(b"WHEELHOUSE_TOKEN=")]


def cli(*args) -> subprocess.CompletedProcess:
    """claude-wheelhouse, as the person runs it, on the test's state dir (WHEELHOUSE_DB)."""
    env = dict(os.environ, PYTHONPATH=str(fingerprint.PACKAGE.parent))
    return subprocess.run([sys.executable, "-m", "claude_wheelhouse", *args], env=env, capture_output=True, text=True,
                          timeout=30)


@pytest.mark.parametrize("change, said, desc", [
    (lambda f: f.unlink(), "has gone, but the daemon (pid {pid}) still runs on the token it read",
     "deleted: no wait, and not that the daemon makes it on its first start"),
    (lambda f: f.chmod(0), "Permission denied. If it's yours, chmod 600", "unreadable: no wait either"),
    (lambda f: f.write_text("another-token\n"), "the daemon (pid {pid}) refuses the token in",
     "replaced or restored: not that the token command prints the one it takes"),
    (lambda f: f.write_text("\n"), "holds no token: delete it and stop the daemon (pid {pid})", "emptied"),
])
def test_a_token_file_that_no_longer_matches_the_daemon_says_what_to_do(state, change, said, desc):
    if os.geteuid() == 0 and "Permission" in said:
        pytest.skip("root reads any file")
    state_dir, port = state
    connect.local(state_dir, port)
    pid = daemon_pid(state_dir)
    change(auth.path(state_dir))
    started = time.monotonic()
    with pytest.raises(connect.NoDaemon) as e:
        connect.local(state_dir, port)
    assert time.monotonic() - started < 3, f"{desc}: not waited for, as it runs already"
    assert said.format(pid=pid) in str(e.value) and connect.STOP in str(e.value), f"{desc}: {e.value}"
    assert "first time it starts" not in str(e.value), desc


def test_daemon_stop_stops_it_by_its_pid_and_the_next_start_makes_a_new_token(state):
    state_dir, port = state
    _, old, _ = connect.local(state_dir, port)
    pid = daemon_pid(state_dir)
    auth.path(state_dir).unlink()
    token = cli("token")
    assert f"the daemon (pid {pid}) still runs" in token.stderr and connect.STOP in token.stderr, token.stderr
    assert "first time it starts" not in token.stderr, "one runs already"
    stopped = cli("daemon", "--stop")
    assert stopped.stdout.strip() == f"stopped the daemon (pid {pid})", stopped
    assert connect.holder(state_dir) is None
    assert cli("daemon", "--stop").stdout.strip() == "no wheelhouse daemon is running"
    _, new, _ = connect.local(state_dir, port)
    assert new != old and new == auth.read(auth.path(state_dir))


@pytest.mark.parametrize("stopped, said, desc", [
    (True, ["the daemon ran older code: restarted it on this code"], "this app stopped it"),
    (False, [], "another app's request stopped it first: not this one's doing"),
])
def test_only_the_app_that_stopped_a_daemon_on_older_code_says_it_restarted_it(tmp_path, monkeypatch, stopped, said,
                                                                               desc):
    url = "http://127.0.0.1:1"
    auth.ensure(tmp_path)
    codes = iter(["old", fingerprint.code()])
    monkeypatch.setattr(connect, "holder", lambda d: f"pid 999999 at {url}")
    monkeypatch.setattr(connect, "hello", lambda u, token, wait, state_dir=None: {
        "code": next(codes), "package": str(fingerprint.PACKAGE), "api": fingerprint.API, "pid": 999999})
    monkeypatch.setattr(connect, "stop", lambda *a: stopped)
    assert connect.local(tmp_path, 1) == (url, auth.read(auth.path(tmp_path)), said), desc


def test_a_daemon_running_has_as_long_to_answer_as_one_just_started(tmp_path, monkeypatch):
    auth.ensure(tmp_path)
    waits = []

    def hello(u, token, wait, state_dir=None):
        waits.append(wait)
        return {"code": fingerprint.code(), "package": "p", "api": fingerprint.API, "pid": 2}
    monkeypatch.setattr(connect, "holder", lambda d: "pid 2 at http://127.0.0.1:1")
    monkeypatch.setattr(connect, "hello", hello)
    connect.local(tmp_path, 1)
    assert waits == [connect.START_WAIT]


def test_the_token_command_says_a_token_file_the_running_daemon_refuses(state):
    """Replaced under a running daemon: not printed for a --connect machine to be refused with."""
    state_dir, port = state
    connect.local(state_dir, port)
    pid = daemon_pid(state_dir)
    auth.path(state_dir).write_text("another-token\n")
    token = cli("token")
    assert token.returncode != 0 and token.stdout == "", token
    assert f"the daemon (pid {pid}) refuses the token in" in token.stderr and connect.STOP in token.stderr, token.stderr


def test_a_token_file_the_started_daemon_refuses_is_said_at_once(state):
    state_dir, port = state
    auth.ensure(state_dir)
    auth.path(state_dir).chmod(0o644)
    started = time.monotonic()
    with pytest.raises(connect.NoDaemon, match="chmod 600") as e:
        connect.local(state_dir, port)
    assert time.monotonic() - started < connect.START_WAIT / 2, f"not waited for: {e.value}"


def test_joining_a_daemon_whose_token_file_others_can_read_says_so(state):
    state_dir, port = state
    connect.local(state_dir, port)
    auth.path(state_dir).chmod(0o640)
    said = connect.local(state_dir, port)[2]
    assert [s for s in said if f"chmod 600 {auth.path(state_dir)}" in s], said


@pytest.fixture
def older(tmp_path):
    """A daemon from before 2c, as a client finds it: its lock held, naming its address, and
    no /hello (404), nor any token asked for."""
    import http.server
    import threading

    class NoHello(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(404)
            self.end_headers()

        def log_message(self, *args):
            pass
    server = http.server.HTTPServer(("127.0.0.1", 0), NoHello)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    lock = open(tmp_path / connect.LOCK, "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    lock.write(f"pid {os.getpid()} at http://127.0.0.1:{server.server_port}\n")
    lock.flush()
    yield tmp_path
    lock.close()
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize("token_file, desc", [
    (False, "none: a pre-2c daemon never made one"),
    (True, "one left by a 2c daemon since stopped"),
])
def test_a_daemon_on_code_from_before_the_token_is_said_as_that(older, token_file, desc):
    if token_file:
        auth.ensure(older)
    for ask in (lambda: connect.local(older, 1), lambda: connect.check(older)):
        with pytest.raises(connect.NoDaemon) as e:
            ask()
        assert f"the daemon (pid {os.getpid()}) runs older code" in str(e.value), f"{desc}: {e.value}"
        assert connect.STOP in str(e.value) and "has gone" not in str(e.value), f"{desc}: {e.value}"


@pytest.mark.parametrize("given, passed, desc", [
    (None, "8", "the default"),
    ("2.5", "2.5", "as set here"),
    ("0", "0", "off"),
])
def test_a_daemon_started_by_an_app_keeps_sessions_warm_as_long_as_the_app_would(tmp_path, monkeypatch,
                                                                                given, passed, desc):
    """Its .env isn't the starting directory's: so it is told the limit."""
    if given:
        features.set_keepalive_hours(given)
    ran = []
    monkeypatch.setattr(connect.subprocess, "run", lambda argv, **k: ran.append(argv))
    os.close(connect.start(tmp_path, 7428))
    argv = ran[0]
    assert argv[argv.index(features.KEEPALIVE_FLAG) + 1] == passed, desc
