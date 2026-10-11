"""The daemon's bearer token (.plan/remote.md, phase 2c): a random secret in a file of the
user's own, mode 0600, in the state dir beside the database and the daemon's lock. Every
request to the daemon carries it, so neither another user on the machine nor another
machine on the VPN can read or drive the wheelhouse without being given it.

The daemon makes it the first time it starts, under its lock. `claude-wheelhouse token`
prints it, to give to a remote client, and `--rotate` replaces it: the daemon reads the
file again when it changes, and from then on refuses the old one."""

import grp
import hmac
import os
import pwd
import secrets
import stat
from pathlib import Path

TOKEN = "daemon.token"
SCHEME = "Bearer"
# what the daemon's proof of holding the token is made over, beside the client's nonce and
# the address it was reached at (proof): a label of its own, so the proof is never a MAC
# the token is used for anything else
PROOF_LABEL = b"claude-wheelhouse daemon proof v1"


class TokenError(RuntimeError):
    """The token file can't be trusted or read: said as it is, never worked around."""


def path(state_dir: Path) -> Path:
    return Path(state_dir) / TOKEN


def private_group(gid: int) -> bool:
    """Whether a group is the user's own (a user private group): their primary group, of
    their name, with nobody else in it, so its write bit lets in nobody but them."""
    try:
        me, group = pwd.getpwuid(os.geteuid()), grp.getgrgid(gid)
    except KeyError:
        return False
    return gid == me.pw_gid and group.gr_name == me.pw_name and not [m for m in group.gr_mem if m != me.pw_name]


def check_dir(state_dir: Path) -> None:
    """The state dir must be the user's, and writable by nobody else: whoever can write in it
    can swap the token or the lock. Group-writable is fine for the user's private group, as
    on a umask of 002 (Ubuntu's)."""
    st = os.stat(state_dir)
    if st.st_uid != os.geteuid():
        raise TokenError(f"{state_dir} belongs to another user: the daemon's token can't be kept there")
    if st.st_mode & stat.S_IWOTH or (st.st_mode & stat.S_IWGRP and not private_group(st.st_gid)):
        raise TokenError(f"{state_dir} can be written by others: chmod go-w it, then start again")


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
    check_dir(state_dir)
    target = path(state_dir)
    if not os.path.lexists(target):
        return write(state_dir), f"made a token for clients in {target}"
    try:
        return read(target), None
    except Exposed:
        return write(state_dir), (f"{target} was readable by others, so it has been replaced: "
                                  "give remote clients the new one")


class Exposed(TokenError):
    """The token file was readable by group or others: it may have leaked."""


def read(file: Path) -> str:
    """A token from its file, its first line: opened without following a link, and checked
    on what was opened (a regular file, the user's, readable by them alone), so nothing can
    be swapped in between the check and the read."""
    try:
        fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as e:
        raise TokenError(f"can't read the token in {file}: {e.strerror}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise TokenError(f"{file} isn't a regular file")
        if st.st_uid != os.geteuid():
            raise TokenError(f"{file} belongs to another user: remove it, and a new one is made")
        if st.st_mode & 0o077:
            raise Exposed(f"{file} is readable by others: chmod 600 it")
        token = os.read(fd, 4096).decode(errors="replace").strip()
    finally:
        os.close(fd)
    if not token:
        raise TokenError(f"{file} holds no token")
    return token


def proof(token: str, nonce: str, url: str) -> str:
    """The daemon's answer to a client's challenge: an HMAC of its nonce, made with the token,
    over the address the daemon listens at. A client that gets it right from the address it
    connected to knows the listener holds the token, so only then sends it; a listener on
    another address that relays the nonce to the real daemon gets a proof for the real
    daemon's address, which the client refuses."""
    return hmac.new(token.encode(), PROOF_LABEL + b"\0" + url.encode() + b"\0" + nonce.encode(), "sha256").hexdigest()


def proven(answer: str | None, token: str, nonce: str, url: str) -> bool:
    return isinstance(answer, str) and hmac.compare_digest(answer.encode(), proof(token, nonce, url).encode())


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
