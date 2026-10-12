"""The TUI finding its daemon (.plan/remote.md, phase 2c): the local one, started if none
runs and restarted if it runs older code from the same package, or another machine's with
--connect, which is never started or stopped from here.

The state dir's daemon.lock says whether one runs: held (flock) by the daemon while it
runs, with "pid N at URL" in it. The kernel lets go of a flock when its holder dies, so a
lock file left by a daemon that crashed is only text: not held, none runs."""

import contextlib
import errno
import fcntl
import os
import re
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import auth, features, fingerprint
from .client import RemoteClient

START_WAIT = 10   # seconds a daemon started has to take its lock and answer
HELLO_WAIT = 3    # seconds another machine's daemon has to answer (--connect)
STOP_WAIT = 10    # seconds a daemon asked to stop has to let go of its lock
REVIVES = 3       # daemons started again after one went, within REVIVE_SPAN, before giving up
REVIVE_SPAN = 300
LOCK, LOG = "daemon.lock", "daemon.log"
STOP = "claude-wheelhouse daemon --stop"


class NoDaemon(RuntimeError):
    """The daemon can't be had: why, for the person, with what to do."""


class TokenRefused(NoDaemon):
    """The daemon answers, but refuses the token given (401)."""


class Older(NoDaemon):
    """The daemon answers, but has no /hello: it runs code from before 2c, which took no token."""


def holder(state_dir: Path) -> str | None:
    """What the lock file says, if a daemon holds it; None if none runs."""
    file = Path(state_dir) / LOCK
    try:
        f = open(file)
    except FileNotFoundError:
        return None
    with f:
        try:
            fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return f.read().strip() or "pid unknown"
        fcntl.flock(f, fcntl.LOCK_UN)
        return None


def url_of(held: str) -> str | None:
    """The address a lock names, once its daemon is listening."""
    match = re.search(r"at (http://\S+)", held)
    return match[1] if match else None


def pid_of(held: str | None) -> int | None:
    match = re.match(r"pid (\d+)", held or "")
    return int(match[1]) if match else None


def start(state_dir: Path, port: int) -> int:
    """A daemon, on this interpreter and environment (WHEELHOUSE_DB and the rest go with
    it, but for WHEELHOUSE_TOKEN, which nothing it starts should see), init's child rather
    than ours (setsid --fork, as hosts are), its output appended to daemon.log. On the
    package this process runs: its folder first on the path, and the current directory
    (the state dir) left off it, so `-m claude_wheelhouse` never finds another checkout's,
    nor the installed one when this one is a copy run from its folder. One that finds
    another holding the lock stops at once, and says so in the log. In SDK mode if this
    process is (features.py): it reads the same .env files, but not the app's flags, so it
    is told with --enable-sdk, with --usage-mode how to read plan usage, with
    --keepalive-hours how long to keep quiet sessions warm, and its port with --port.

    Returns the read end of a pipe whose write end only the daemon holds (its number given
    as --start-pipe, which the daemon keeps from anything it starts): it reads as ended the
    moment the daemon exits (listening), so one that stops as it starts (a token file it
    refuses, say) is said at once, not after START_WAIT. The caller closes it."""
    path = os.pathsep.join(filter(None, [str(fingerprint.PACKAGE.parent), os.environ.get("PYTHONPATH")]))
    env = {k: v for k, v in os.environ.items() if k != "WHEELHOUSE_TOKEN"}
    ended, held = os.pipe()
    try:
        with open(Path(state_dir) / LOG, "a") as log:
            print(time.strftime("%Y-%m-%dT%H:%M:%S%z"), f"pid {os.getpid()} asked for a daemon", file=log, flush=True)
            argv = ["setsid", "--fork", sys.executable, "-m", "claude_wheelhouse", "daemon", "--port", str(port),
                    "--start-pipe", str(held)]
            argv += [features.USAGE_FLAG, features.usage_mode(), features.KEEPALIVE_FLAG,
                     f"{features.keepalive_hours():g}"] + ([features.FLAG] if features.sdk_enabled() else [])
            subprocess.run(argv, cwd=state_dir,
                           stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                           env=dict(env, PYTHONSAFEPATH="1", PYTHONPATH=path), pass_fds=(held,), check=True)
    except BaseException:
        os.close(ended)
        raise
    finally:
        os.close(held)
    return ended


def exited(ended: int | None) -> bool:
    """Whether the daemon start() started has exited: its pipe reads as ended."""
    return ended is not None and bool(select.select([ended], [], [], 0)[0])


def last_line(state_dir: Path) -> str:
    try:
        lines = (Path(state_dir) / LOG).read_text().strip().splitlines()
    except OSError:
        return ""
    return lines[-1] if lines else ""


class Gone(Exception):
    """The local daemon let go of its lock while it was being asked: stopped, or restarted
    by another app. Gone round again, never fatal on its own."""


def hello(url: str, token: str, wait: float, state_dir: Path | None = None) -> dict | None:
    """The daemon's /hello, once it answers ready, within wait seconds; None if it doesn't.
    NoDaemon if it refuses the token (401). With state_dir, Gone as soon as the local one
    has let go of its lock."""
    import httpx2
    deadline = time.monotonic() + wait
    with httpx2.Client(timeout=httpx2.Timeout(2, connect=1), headers={"Authorization": auth.header(token)}) as http:
        while True:
            try:
                r = http.get(f"{url}/hello")
                if r.status_code == 404:   # every path answers 401 without the token since 2c, /hello with it
                    raise Older(f"the daemon at {url} runs older code, from before the wheelhouse took a token")
                if r.status_code == 401:
                    raise TokenRefused(f"the daemon at {url} refuses this token: claude-wheelhouse token, on "
                                       "its machine, prints the one it takes")
                if r.status_code == 200 and r.json().get("ready"):
                    return r.json()
            except (httpx2.TransportError, ValueError):
                pass
            if state_dir is not None and holder(state_dir) is None:
                raise Gone()
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.1)


def stop(url: str, token: str, state_dir: Path, pid: int) -> bool:
    """Ask the daemon (pid) to stop, and wait for it to let go of its lock: until the lock
    is free or held by another daemon (another app's, started meanwhile), which the caller
    goes round again to join. Whether this request stopped it: not if another app's asked
    first (both found it on older code)."""
    import uuid

    import httpx2
    stopped = False
    with httpx2.Client(timeout=5) as http, contextlib.suppress(httpx2.TransportError, ValueError):
        r = http.post(f"{url}/shutdown", json={}, headers={"Idempotency-Key": str(uuid.uuid4()),
                                                           "Authorization": auth.header(token)})
        stopped = r.status_code == 200 and r.json().get("ok") is True
    deadline = time.monotonic() + STOP_WAIT
    while pid_of(holder(state_dir)) == pid:
        if time.monotonic() >= deadline:
            raise NoDaemon(f"the daemon (pid {pid}) at {url} runs older code and didn't stop when asked: stop "
                           "it, then start the wheelhouse again (claude-wheelhouse --local runs without it)")
        time.sleep(0.1)
    return stopped


def halt(state_dir: Path) -> str:
    """The local daemon stopped (claude-wheelhouse daemon --stop), by the pid its lock
    names, with SIGTERM, as systemd stops it: no token needed, so it works when the token
    file is what's wrong. What was done, for the person."""
    held = holder(state_dir)
    if held is None:
        return "no wheelhouse daemon is running"
    pid = pid_of(held)
    if pid is None:
        raise NoDaemon(f"the daemon's lock ({state_dir / LOCK}) names no pid: {held}")
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + STOP_WAIT
    while pid_of(holder(state_dir)) == pid:
        if time.monotonic() >= deadline:
            raise NoDaemon(f"the daemon (pid {pid}) didn't stop within {STOP_WAIT} s: kill {pid}")
        time.sleep(0.1)
    return f"stopped the daemon (pid {pid})"


def token_trouble(e: auth.TokenError, file: Path, held: str | None) -> str:
    """What's wrong with the token file while a daemon runs (held, its lock), and what to do:
    the daemon took the token when it started and keeps it, so the fix is to stop it, and the
    next start reads the file afresh, or makes one if there's none."""
    pid = pid_of(held)
    daemon = f"the daemon (pid {pid})" if pid else "the daemon"
    if e.errno == errno.ENOENT:
        return (f"the token file {file} has gone, but {daemon} still runs on the token it read when it started: "
                f"stop it ({STOP}), and the next start makes a new token (give that to each --connect machine)")
    if e.errno is not None:
        return (f"{e}. If it's yours, chmod 600 {file}; otherwise delete it and stop {daemon} ({STOP}), and "
                "the next start makes a new token")
    return f"{e}: delete it and stop {daemon} ({STOP}), and the next start makes a new token"


def older_trouble(held: str | None) -> str:
    pid = pid_of(held)
    daemon = f"the daemon (pid {pid})" if pid else "the daemon"
    return (f"{daemon} runs older code, from before the wheelhouse took a token: stop it ({STOP}), and the "
            "next start runs this code")


def predates_token(url: str) -> bool:
    """Whether the daemon at url runs code from before 2c: one since refuses /hello without
    the token (401); one before had no /hello (404)."""
    import httpx2
    try:
        return httpx2.get(f"{url}/hello", timeout=2).status_code == 404
    except httpx2.TransportError:
        return False


def trouble(e: auth.TokenError, file: Path, held: str | None) -> str:
    """token_trouble, unless the daemon runs older code, which never read the file."""
    url = url_of(held or "")
    return older_trouble(held) if url and predates_token(url) else token_trouble(e, file, held)


def check(state_dir: Path) -> str:
    """The token file's token, for claude-wheelhouse token: NoDaemon if a daemon runs and
    refuses it, or runs older code, or the file can't be read while one runs, said as the
    app says it; TokenError if it can't be read and none runs. One that doesn't answer
    leaves it printed: its token is still the file's, as far as can be told."""
    file, held = auth.path(state_dir), holder(state_dir)
    try:
        token = auth.read(file)
    except auth.TokenError as e:
        if held is None:
            raise
        raise NoDaemon(trouble(e, file, held)) from None
    url = url_of(held or "")
    if url:
        try:
            hello(url, token, 0)
        except TokenRefused:
            raise NoDaemon(refused_trouble(file, held)) from None
        except Older:
            raise NoDaemon(older_trouble(held)) from None
    return token


def refused_trouble(file: Path, held: str | None) -> str:
    pid = pid_of(held)
    daemon = f"the daemon (pid {pid})" if pid else "the daemon"
    return (f"{daemon} refuses the token in {file}: the file has changed since it started (replaced or "
            f"restored). Stop it ({STOP}), and the next start takes the file's token")


def token_of(state_dir: Path, wait: float = 0) -> str:
    deadline = time.monotonic() + wait
    while True:
        try:
            return auth.read(auth.path(state_dir))
        except auth.TokenError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


def listening(state_dir: Path, wait: float, ended: int | None = None) -> str | None:
    """What the lock says once its daemon is listening (it names its address only then),
    within wait seconds; None if none does by then, or the one that took the lock let go
    of it without listening (it couldn't bind its port, say), or the one started (ended,
    start's pipe) has exited, which it may have done before its lock was ever seen."""
    deadline, seen = time.monotonic() + wait, False
    while time.monotonic() < deadline:
        held = holder(state_dir)
        if held is not None and "at http" in held:
            return held
        if held is None and (seen or exited(ended)):
            return None
        seen = seen or held is not None
        time.sleep(0.05)
    return None


def local(state_dir: Path, port: int, given: bool = False) -> tuple[str, str, list[str]]:
    """The local daemon, joined or started: its URL, its token and what to say about it.
    One from the same package on other code runs older code (the checkout changed under it),
    so it's stopped and one started on this code; one from another package is joined if it
    serves the same API. A lock let go meanwhile (another app restarting it, or its own
    Reviver) is gone round again: only a daemon this app started that never listens, or one
    that holds its lock and never answers, is fatal. given: whether the person set the port
    (features.port_given), for what's said of a daemon on another."""
    said, restarted, failed = [], False, 0
    deadline = time.monotonic() + 3 * START_WAIT
    while True:
        if time.monotonic() >= deadline:
            raise NoDaemon(f"no daemon would stay up long enough to join: see {LOG} (in {state_dir}). "
                           "claude-wheelhouse --local runs without it")
        held = holder(state_dir)
        found, ended = held is not None, None
        if held is None:
            try:
                ended = start(state_dir, port)
            except (OSError, subprocess.CalledProcessError) as e:
                raise NoDaemon(f"couldn't start the daemon: {e}") from None
        try:
            held = listening(state_dir, START_WAIT, ended)
        finally:
            if ended is not None:
                os.close(ended)
        if held is None:
            if holder(state_dir) is not None:
                continue   # another app's daemon took the lock and is still starting
            failed += 1
            if failed >= 2 or not found:
                line = last_line(state_dir)
                if line == features.PORT_TAKEN.format(port=port):   # the daemon's own words (daemon.listener)
                    raise NoDaemon(line)
                raise NoDaemon(f"the daemon didn't start: {line or 'see ' + LOG} "
                               f"(in {state_dir}). claude-wheelhouse --local runs without it")
            continue
        url, file = url_of(held), auth.path(state_dir)
        try:   # written before the daemon listens: waited for only from one this app has just started
            token = token_of(state_dir, 0 if found else START_WAIT)
        except auth.TokenError as e:
            raise NoDaemon(trouble(e, file, held)) from None
        try:
            info = hello(url, token, START_WAIT, state_dir)
        except Gone:
            continue
        except TokenRefused:
            raise NoDaemon(refused_trouble(file, held)) from None
        except Older:
            raise NoDaemon(older_trouble(held)) from None
        exposed = auth.exposed(file) if found else None   # one started on it would have refused it
        if exposed and f"the daemon runs, but {exposed}" not in said:
            said.append(f"the daemon runs, but {exposed}")
        elsewhere = elsewhere_said(url, port, given)   # the lock's address wins over the port asked for
        if elsewhere and elsewhere not in said:
            said.append(elsewhere)
        if info is None:
            raise NoDaemon(f"the daemon ({held}) holds its lock but doesn't answer. It's left running: stop it, "
                           "or run claude-wheelhouse --local without it")
        if info["code"] == fingerprint.code():
            return url, token, said
        if info["package"] == str(fingerprint.PACKAGE) and not restarted:
            restarted = True
            if stop(url, token, state_dir, info["pid"]):   # not another app's doing
                said.append("the daemon ran older code: restarted it on this code")
            continue
        if info["api"] != fingerprint.API:
            raise NoDaemon(f"the daemon (pid {info['pid']}, from {info['package']}) speaks API {info['api']}, this "
                           f"wheelhouse {fingerprint.API}: stop it, or run claude-wheelhouse --local")
        said.append(f"the daemon (pid {info['pid']}) runs other code, from {info['package']}")
        return url, token, said


def elsewhere_said(url: str, port: int, given: bool) -> str | None:
    """What to say of a running daemon whose lock names another port than the one asked for
    (one started by hand, or by an app with another setting): joined where it is. given:
    whether the person set this port, or it is the default."""
    match = re.search(r":(\d+)$", url)
    if match is None or int(match[1]) == port:
        return None
    asked = f"{port} as set here" if given else f"the default {port}"
    return (f"the daemon runs on port {match[1]}, not {asked}: joined it there. To move it, quit the app, stop it "
            f"({STOP}) and start again")


class Reviver:
    """The local daemon, started again when it has gone (its lock free) under a running
    TUI: up to REVIVES times in REVIVE_SPAN, then left to the person. Its address once it's
    listening, and the token file's token, read again (None if it can't be): a daemon
    started again, or one that refused the client's, may take another."""

    def __init__(self, state_dir: Path, port: int):
        self.state_dir, self.port = state_dir, port
        self.started: list[float] = []

    def __call__(self) -> tuple[str, str | None] | None:
        held = holder(self.state_dir)
        if held is None:
            now = time.monotonic()
            self.started = [t for t in self.started if now - t < REVIVE_SPAN]
            if len(self.started) >= REVIVES:
                return None
            self.started.append(now)
            try:
                ended = start(self.state_dir, self.port)
            except (OSError, subprocess.CalledProcessError):
                return None
        else:
            ended = None
        try:
            held = listening(self.state_dir, START_WAIT, ended)
        finally:
            if ended is not None:
                os.close(ended)
        if held is None:
            return None
        try:
            token = token_of(self.state_dir)
        except auth.TokenError:
            token = None
        return url_of(held), token


def local_client(state_dir: Path) -> tuple[RemoteClient, list[str]]:
    port = features.port()
    url, token, said = local(state_dir, port, features.port_given() is not None)
    client = RemoteClient(url, token, revive=Reviver(state_dir, port),
                          why_refused=lambda: refused_trouble(auth.path(state_dir), holder(state_dir)))
    return client, said


def remote_client(address: str, token: str) -> tuple[RemoteClient, list[str]]:
    """Another machine's daemon (--connect host:port): never started or stopped from here.
    Another API won't do; other code is only said."""
    url = address if address.startswith("http://") else f"http://{address}"
    info = hello(url, token, HELLO_WAIT)
    if info is None:
        raise NoDaemon(f"no daemon answers at {url}")
    if info["api"] != fingerprint.API:
        raise NoDaemon(f"the daemon at {url} speaks API {info['api']}, this wheelhouse {fingerprint.API}")
    said = [] if info["code"] == fingerprint.code() else [f"the daemon at {url} runs other code than this wheelhouse"]
    client = RemoteClient(url, token)
    client.elsewhere = True
    return client, said


def given_token(file: str | None, environ=os.environ, ask=None) -> str:
    """The token for --connect: from a file, else WHEELHOUSE_TOKEN, else asked for without
    echo. Never a flag's value, which ps and the shell's history would show."""
    if file:
        return auth.read(Path(file).expanduser())
    if environ.get("WHEELHOUSE_TOKEN"):
        return environ["WHEELHOUSE_TOKEN"].strip()
    if ask is None and sys.stdin.isatty():
        import getpass
        ask = getpass.getpass
    if ask is None:
        raise NoDaemon("--connect needs the daemon's token: --token-file, or WHEELHOUSE_TOKEN")
    token = ask("the daemon's token (claude-wheelhouse token prints it there): ").strip()
    if not token:
        raise NoDaemon("no token given")
    return token
