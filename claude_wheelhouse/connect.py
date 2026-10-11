"""The TUI finding its daemon (.plan/remote.md, phase 2c): the local one, started if none
runs and restarted if it runs older code from the same package, or another machine's with
--connect, which is never started or stopped from here.

The state dir's daemon.lock says whether one runs: held (flock) by the daemon while it
runs, with "pid N at URL" in it. The kernel lets go of a flock when its holder dies, so a
lock file left by a daemon that crashed is only text: not held, none runs."""

import fcntl
import ipaddress
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import auth, fingerprint
from .client import Impostor, RemoteClient, Unreachable, prove

PORT = 7428
START_WAIT = 10   # seconds a daemon started has to take its lock and answer
HELLO_WAIT = 3    # seconds a running daemon has to answer before it's called hung
STOP_WAIT = 10    # seconds a daemon asked to stop has to let go of its lock
REVIVES = 3       # daemons started again after one went, within REVIVE_SPAN, before giving up
REVIVE_SPAN = 300
LOCK, LOG = "daemon.lock", "daemon.log"


class NoDaemon(RuntimeError):
    """The daemon can't be had: why, for the person, with what to do."""


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
    """The address a lock names, once its daemon is listening: on this machine (the
    loopback) only. A daemon run by hand on another address (a VPN's) is reached with
    --connect, where the person says where it is."""
    match = re.search(r"at (http://\S+)", held)
    if not match:
        return None
    try:
        host = urlsplit(match[1]).hostname
        return match[1] if host and ipaddress.ip_address(host).is_loopback else None
    except ValueError:
        return None


def port() -> int:
    return int(os.environ.get("WHEELHOUSE_PORT") or PORT)


def start(state_dir: Path, port: int) -> None:
    """A daemon, on this interpreter and environment (WHEELHOUSE_DB and the rest go with
    it, but for WHEELHOUSE_TOKEN, which nothing it starts should see), init's child rather
    than ours (setsid --fork, as hosts are), its output appended to daemon.log. On the
    package this process runs: its folder first on the path, and the current directory
    (the state dir) left off it, so `-m claude_wheelhouse` never finds another checkout's,
    nor the installed one when this one is a copy run from its folder. One that finds
    another holding the lock stops at once, and says so in the log."""
    path = os.pathsep.join(filter(None, [str(fingerprint.PACKAGE.parent), os.environ.get("PYTHONPATH")]))
    env = {k: v for k, v in os.environ.items() if k != "WHEELHOUSE_TOKEN"}
    with open(Path(state_dir) / LOG, "a") as log:
        print(time.strftime("%Y-%m-%dT%H:%M:%S%z"), f"pid {os.getpid()} asked for a daemon", file=log, flush=True)
        subprocess.run(["setsid", "--fork", sys.executable, "-m", "claude_wheelhouse", "daemon", "--port", str(port)],
                       cwd=state_dir, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                       env=dict(env, PYTHONSAFEPATH="1", PYTHONPATH=path), check=True)


def last_line(state_dir: Path) -> str:
    try:
        lines = (Path(state_dir) / LOG).read_text().strip().splitlines()
    except OSError:
        return ""
    return lines[-1] if lines else ""


class Gone(Exception):
    """The local daemon let go of its lock while it was being asked: stopped, or restarted
    by another app. Gone round again, never fatal on its own."""


def hello(url: str, token: str, wait: float, state_dir: Path | None = None, reread=None) -> tuple[dict | None, str]:
    """The daemon's /hello, once it answers ready, within wait seconds, and the token it
    took: (None, token) if it doesn't. Proven first each time (client.prove): the token goes
    nowhere that hasn't shown it holds it, and something else at the address is NoDaemon.
    A 401 reads the token again once (rotated). With state_dir, Gone as soon as the local
    one has let go of its lock."""
    import httpx2
    deadline = time.monotonic() + wait
    reread_once = reread
    with httpx2.Client(timeout=httpx2.Timeout(2, connect=1)) as http:
        while True:
            try:
                token = prove(http, url, token, reread)
                r = http.get(f"{url}/hello", params={"nonce": secrets.token_hex(16)},
                             headers={"Authorization": auth.header(token)})
                if r.status_code == 401 and reread_once is not None:
                    token, reread_once = reread_once(), None
                    continue
                if r.status_code == 401:
                    raise NoDaemon(f"the daemon at {url} refuses this token")
                if r.status_code == 200 and r.json().get("ready"):
                    return r.json(), token
            except Impostor as e:
                raise NoDaemon(f"{e}: is another program on its port?") from None
            except (Unreachable, httpx2.TransportError, ValueError):
                pass
            if state_dir is not None and holder(state_dir) is None:
                raise Gone()
            if time.monotonic() >= deadline:
                return None, token
            time.sleep(0.1)


def stop(url: str, token: str, state_dir: Path) -> None:
    """Ask the daemon to stop, once it has proven itself again, and wait for its lock to go."""
    import uuid

    import httpx2
    with httpx2.Client(timeout=5) as http:
        try:
            token = prove(http, url, token)
            http.post(f"{url}/shutdown", json={}, headers={"Idempotency-Key": str(uuid.uuid4()),
                                                           "Authorization": auth.header(token)})
        except (Unreachable, httpx2.TransportError):
            pass
    deadline = time.monotonic() + STOP_WAIT
    while holder(state_dir) is not None:
        if time.monotonic() >= deadline:
            raise NoDaemon(f"the daemon at {url} runs older code and didn't stop when asked: stop it, then start "
                           "the wheelhouse again (claude-wheelhouse --local runs without it)")
        time.sleep(0.1)


def token_of(state_dir: Path, wait: float = 0) -> str:
    deadline = time.monotonic() + wait
    while True:
        try:
            return auth.read(auth.path(state_dir))
        except auth.TokenError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


def listening(state_dir: Path, wait: float) -> str | None:
    """What the lock says once its daemon is listening (it names its address only then),
    within wait seconds; None if none does by then, or the one that took the lock let go
    of it without listening (it couldn't bind its port, say)."""
    deadline, seen = time.monotonic() + wait, False
    while time.monotonic() < deadline:
        held = holder(state_dir)
        if held is not None and "at http" in held:
            return held
        if held is None and seen:
            return None
        seen = seen or held is not None
        time.sleep(0.05)
    return None


def local(state_dir: Path, port: int) -> tuple[str, str, list[str]]:
    """The local daemon, joined or started: its URL, its token and what to say about it.
    One from the same package on other code runs older code (the checkout changed under it),
    so it's stopped and one started on this code; one from another package is joined if it
    serves the same API. A lock let go meanwhile (another app restarting it, or its own
    Reviver) is gone round again: only a daemon this app started that never listens, or one
    that holds its lock and never answers, is fatal."""
    auth.check_dir(state_dir)
    said, restarted, failed = [], False, 0
    deadline = time.monotonic() + 3 * START_WAIT
    while True:
        if time.monotonic() >= deadline:
            raise NoDaemon(f"no daemon would stay up long enough to join: see {LOG} (in {state_dir}). "
                           "claude-wheelhouse --local runs without it")
        held = holder(state_dir)
        found = held is not None
        if held is None:
            try:
                start(state_dir, port)
            except (OSError, subprocess.CalledProcessError) as e:
                raise NoDaemon(f"couldn't start the daemon: {e}") from None
        held = listening(state_dir, START_WAIT)
        if held is None:
            if holder(state_dir) is not None:
                continue   # another app's daemon took the lock and is still starting
            failed += 1
            if failed >= 2 or not found:
                raise NoDaemon(f"the daemon didn't start: {last_line(state_dir) or 'see ' + LOG} "
                               f"(in {state_dir}). claude-wheelhouse --local runs without it")
            continue
        url = url_of(held)
        if url is None:
            raise NoDaemon(f"the daemon lock says {held!r}: not an address on this machine. A daemon on "
                           "another address is reached with --connect")
        try:
            info, token = hello(url, token_of(state_dir, START_WAIT), HELLO_WAIT if found else START_WAIT,
                                state_dir, reread=lambda: token_of(state_dir))
        except Gone:
            continue
        if info is None:
            raise NoDaemon(f"the daemon ({held}) holds its lock but doesn't answer. It's left running: stop it, "
                           "or run claude-wheelhouse --local without it")
        if info["code"] == fingerprint.code():
            return url, token, said
        if info["package"] == str(fingerprint.PACKAGE) and not restarted:
            stop(url, token, state_dir)
            restarted = True
            said.append("the daemon ran older code: restarted it on this code")
            continue
        if info["api"] != fingerprint.API:
            raise NoDaemon(f"the daemon (pid {info['pid']}, from {info['package']}) speaks API {info['api']}, this "
                           f"wheelhouse {fingerprint.API}: stop it, or run claude-wheelhouse --local")
        said.append(f"the daemon (pid {info['pid']}) runs other code, from {info['package']}")
        return url, token, said


class Reviver:
    """The local daemon, started again when it has gone (its lock free) under a running
    TUI: up to REVIVES times in REVIVE_SPAN, then left to the person. Its address once it's
    listening; the client proves it before sending it anything."""

    def __init__(self, state_dir: Path, port: int):
        self.state_dir, self.port = state_dir, port
        self.started: list[float] = []

    def __call__(self) -> str | None:
        held = holder(self.state_dir)
        if held is None:
            now = time.monotonic()
            self.started = [t for t in self.started if now - t < REVIVE_SPAN]
            if len(self.started) >= REVIVES:
                return None
            self.started.append(now)
            try:
                start(self.state_dir, self.port)
            except (OSError, subprocess.CalledProcessError):
                return None
        held = listening(self.state_dir, START_WAIT)
        return held and url_of(held)


def local_client(state_dir: Path) -> tuple[RemoteClient, list[str]]:
    url, token, said = local(state_dir, port())
    client = RemoteClient(url, token, reread=lambda: token_of(state_dir), revive=Reviver(state_dir, port()))
    return client, said


def remote_client(address: str, token: str) -> tuple[RemoteClient, list[str]]:
    """Another machine's daemon (--connect host:port): never started or stopped from here.
    Another API won't do; other code is only said."""
    url = address if address.startswith("http://") else f"http://{address}"
    info, token = hello(url, token, HELLO_WAIT)
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
    if file:   # the user's, readable by them alone, as the daemon's own (auth.read)
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
