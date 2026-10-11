"""The daemon's bearer token (.plan/remote.md, phase 2c): a random secret in a file of the
user's own, mode 0600, in the state dir beside the database and the daemon's lock. Every
request to the daemon carries it, so neither another user on the machine nor another
machine on the VPN can read or drive the wheelhouse without being given it.

The daemon makes it the first time it starts, under its lock. `claude-wheelhouse token`
prints it, to give to a remote client, and `--rotate` replaces it: the daemon reads the
file again when it changes, and from then on refuses the old one."""

import hmac
import os
import secrets
from pathlib import Path

TOKEN = "daemon.token"
SCHEME = "Bearer"


class TokenError(RuntimeError):
    """The token file can't be trusted or read: said as it is, never worked around."""


def path(state_dir: Path) -> Path:
    return Path(state_dir) / TOKEN


def write(state_dir: Path) -> str:
    """A new token, in a new file renamed over the old one: never a moment with the file
    readable by others, nor half written."""
    token = secrets.token_urlsafe(32)
    target = path(state_dir)
    tmp = target.with_name(f".{TOKEN}.{os.getpid()}.{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)   # whatever the umask
        os.write(fd, f"{token}\n".encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, target)
    return token


def ensure(state_dir: Path) -> tuple[str, str | None]:
    """The token, made if there's none: (token, what to say about it, if anything). One
    readable by group or others may have leaked, so it's replaced; one owned by another
    user can't be trusted, nor replaced, so it's refused."""
    target = path(state_dir)
    try:
        st = target.stat()
    except FileNotFoundError:
        return write(state_dir), f"made a token for clients in {target}"
    if st.st_uid != os.geteuid():
        raise TokenError(f"{target} belongs to another user: remove it, and a new one is made")
    if st.st_mode & 0o077:
        return write(state_dir), (f"{target} was readable by others, so it has been replaced: "
                                  "give remote clients the new one")
    return read(target), None


def read(file: Path) -> str:
    """A token from its file: its first line."""
    try:
        token = Path(file).read_text().strip()
    except OSError as e:
        raise TokenError(f"can't read the token in {file}: {e.strerror}") from None
    if not token:
        raise TokenError(f"{file} holds no token")
    return token


class TokenFile:
    """The daemon's token, read again whenever its file changes (rotated), from a cheap
    stat. A file gone or unreadable keeps the last token: a rename over it is never seen
    half done, and a removal is the person's to follow with a restart."""

    def __init__(self, file: Path):
        self.file = Path(file)
        self.stamp = None
        self.token = ""
        self.check()

    def check(self) -> bool:
        """Whether the token changed."""
        try:
            st = self.file.stat()
        except OSError:
            return False
        stamp = (st.st_ino, st.st_mtime_ns, st.st_size)
        if stamp == self.stamp:
            return False
        try:
            token = read(self.file)
        except TokenError:
            return False
        self.stamp, changed, self.token = stamp, token != self.token, token
        return changed


def header(token: str) -> str:
    return f"{SCHEME} {token}"


def matches(given: str | None, token: str) -> bool:
    """Whether an Authorization header carries the token, compared in constant time. No
    token at all (an empty one) matches nothing."""
    if not token or not given:
        return False
    scheme, _, value = given.partition(" ")
    return scheme.lower() == SCHEME.lower() and hmac.compare_digest(value.strip().encode(), token.encode())
