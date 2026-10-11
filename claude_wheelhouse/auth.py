"""The daemon's shared secret (.plan/remote.md, phase 2c): a random token in a file of the
user's own, mode 0600, in the state dir beside the database and the daemon's lock. The
daemon and its clients on this machine read the same file, and every request to the daemon
carries the token as a bearer header, so neither another user on the machine nor another
machine on the VPN can read or drive the wheelhouse without it.

The daemon makes the file the first time it starts, under its lock, and reuses it from then
on. `claude-wheelhouse token` prints it, to give to a client on another machine
(--connect)."""

import hmac
import os
import secrets
from pathlib import Path

TOKEN = "daemon.token"
SCHEME = "Bearer"


class TokenError(RuntimeError):
    """The token file can't be read: said as it is, with what to do."""


def path(state_dir: Path) -> Path:
    return Path(state_dir) / TOKEN


def ensure(state_dir: Path) -> tuple[str, str | None]:
    """The token, made if there's none yet: (token, what to say about it, if anything).
    A new one is written to a file of its own, 0600 whatever the umask, and renamed into
    place, so a client never reads it half written."""
    target = path(state_dir)
    if target.exists():
        return read(target), None
    token = secrets.token_urlsafe(32)
    tmp = target.with_name(f".{TOKEN}.{os.getpid()}.{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, f"{token}\n".encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, target)
    return token, f"made a token for clients in {target}"


def read(file: Path) -> str:
    """A token from its file: its first line."""
    try:
        token = Path(file).read_text().strip()
    except OSError as e:
        raise TokenError(f"can't read the token in {file}: {e.strerror}") from None
    if not token:
        raise TokenError(f"{file} holds no token")
    return token


def header(token: str) -> str:
    return f"{SCHEME} {token}"


def matches(given: str | None, token: str) -> bool:
    """Whether an Authorization header carries the token, compared in constant time. No
    token at all (an empty one) matches nothing."""
    if not token or not given:
        return False
    scheme, _, value = given.partition(" ")
    return scheme.lower() == SCHEME.lower() and hmac.compare_digest(value.strip().encode(), token.encode())
