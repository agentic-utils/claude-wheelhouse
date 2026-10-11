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

import pytest

from claude_wheelhouse import auth, connect, fingerprint


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
    monkeypatch.setattr(fingerprint, "API", 2)
    with pytest.raises(connect.NoDaemon, match="speaks API 1, this wheelhouse 2"):
        connect.local(state_dir, port)


def test_a_daemon_that_holds_its_lock_and_never_answers_is_left_alone(state, monkeypatch):
    state_dir, port = state
    monkeypatch.setattr(connect, "HELLO_WAIT", 0.3)
    auth.ensure(state_dir)
    with open(state_dir / connect.LOCK, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock.write(f"pid {os.getpid()} at http://127.0.0.1:{free_port()}\n")
        lock.flush()
        with pytest.raises(connect.NoDaemon, match="holds its lock but doesn't answer. It's left running"):
            connect.local(state_dir, port)


def test_a_port_taken_is_said_with_the_daemons_own_words(state, monkeypatch):
    state_dir, port = state
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", port))
        taken.listen()
        with pytest.raises(connect.NoDaemon) as e:
            connect.local(state_dir, port)
    assert "address already in use" in str(e.value) and "--local runs without it" in str(e.value), str(e.value)
    assert connect.holder(state_dir) is None


def test_a_daemon_gone_under_a_running_tui_is_started_again_a_few_times(state, monkeypatch):
    state_dir, port = state
    connect.local(state_dir, port)
    revive = connect.Reviver(state_dir, port)
    assert revive() == f"http://127.0.0.1:{port}", "running: nothing to do"
    monkeypatch.setattr(connect, "REVIVES", 1)
    for n in range(2):
        pid = daemon_pid(state_dir)
        os.kill(pid, signal.SIGKILL)   # it died
        deadline = time.monotonic() + 10
        while connect.holder(state_dir) is not None and time.monotonic() < deadline:
            time.sleep(0.05)
        url = revive()
        if n == 0:
            assert url == f"http://127.0.0.1:{port}" and daemon_pid(state_dir) != pid, "started again"
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


def test_the_daemon_never_sees_the_apps_token_in_its_environment(state, monkeypatch):
    state_dir, port = state
    monkeypatch.setenv("WHEELHOUSE_TOKEN", "a-connect-token")
    connect.local(state_dir, port)
    environ = open(f"/proc/{daemon_pid(state_dir)}/environ", "rb").read().split(b"\0")
    assert not [e for e in environ if e.startswith(b"WHEELHOUSE_TOKEN=")]
