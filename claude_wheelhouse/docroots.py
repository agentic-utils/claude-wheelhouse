"""The docs a client may read through the hub, and write (.plan/modules.md, "The module
boundary"): only a .md file under a directory a module declares (api.Module.doc_roots), and
for a save also a doc item's file (permitted, write). Each
path is resolved first, its symlinks followed and its .. taken out, so none reaches outside a
root; the roots are resolved the same way. And where a repo's decisions are found (Q114):
docs/adr under its git root, unless a session there declared others (set_doc_dirs), which
may be in another repo kept beside it. Never imports Textual."""

import contextlib
import json
import os
import stat
import tempfile
from pathlib import Path

DEFAULT = ("docs/adr",)   # a repo's decisions, unless a session there declares others (Q114)
SUFFIX = ".md"
MOST = 20                 # directories a session may declare
DOC_CAP = 1 << 20   # bytes of a doc read at most: the rest is left out, and said to be


class Outside(ValueError):
    """A path no doc root holds, or one that isn't a doc's."""


class Conflict(Exception):
    """A save refused (409): the file has changed since the editor's base. Its mtime and
    text now, for the editor's Reload."""

    def __init__(self, path: Path, st: os.stat_result, text: str):
        super().__init__(f"{path} has changed on disk since you started editing")
        self.mtime_ns, self.stamp, self.text = st.st_mtime_ns, f"{st.st_mtime_ns}:{st.st_size}", text


class Gone(Exception):
    """A save refused (404): the file has gone since the editor's base."""


class Unwritable(Exception):
    """A save that couldn't be made: the daemon's user may not write there, the disk is full,
    or the path is no longer a regular file. Says the path and why."""


def confine(path, roots) -> Path:
    """path resolved, if it names a .md file under one of roots (each resolved too); else
    Outside. Only an absolute path: a relative one would resolve against the daemon's own
    directory. A relative root is no root. Compared as the filesystem gives the names, so on
    a case-insensitive one a path spelt in another case than its root's is refused, never
    let in. The file need not exist (a doc written anew, write)."""
    if not isinstance(path, str) or not os.path.isabs(path) or "\0" in path:
        raise Outside(f"{path!r} is not an absolute path")
    real = Path(os.path.realpath(path))
    if real.suffix != SUFFIX:
        raise Outside(f"{path} is not a {SUFFIX} file")
    for root in roots:
        if os.path.isabs(root) and real.is_relative_to(Path(os.path.realpath(root))):
            return real
    raise Outside(f"{path} is not under any doc root")


def read(path, roots, have: str | None = None) -> dict:
    """A doc under roots (confine), at most DOC_CAP bytes: {path, text, stamp, gone, mtime_ns},
    path as resolved; {"same": true} if its stamp is still have. gone says why it can't be
    read ("gone", "not a file"). Opened without following a symlink and without blocking, so
    a link or FIFO swapped in since it was resolved is refused or never waited on."""
    real = confine(path, roots)
    try:
        fd = os.open(real, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {"path": str(real), "text": "", "stamp": None, "gone": "gone", "mtime_ns": None}
    except OSError as e:
        raise Outside(f"{path} can't be opened: {e.strerror}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return {"path": str(real), "text": "", "stamp": None, "gone": "not a file", "mtime_ns": None}
        stamp = f"{st.st_mtime_ns}:{st.st_size}"
        if stamp == have:
            return {"same": True}
        with os.fdopen(os.dup(fd), "rb") as f:
            data = f.read(DOC_CAP + 1)
    finally:
        os.close(fd)
    text = data[:DOC_CAP].decode("utf-8", errors="replace")
    if len(data) > DOC_CAP:
        text += f"\n\n_(only the first {DOC_CAP >> 20} MB is shown)_"
    return {"path": str(real), "text": text, "stamp": stamp, "gone": None, "mtime_ns": st.st_mtime_ns}


def permitted(path, roots, files=()) -> Path:
    """path resolved, if a doc item's file (files, as posted) or a .md file under a doc root
    (confine); else Outside. Where a save may write (.plan/modules.md, "Saving and leaving")."""
    if isinstance(path, str) and os.path.isabs(path) and "\0" not in path:
        real = Path(os.path.realpath(path))
        if any(real == Path(os.path.realpath(f)) for f in files if isinstance(f, str) and os.path.isabs(f)):
            return real
    return confine(path, roots)


def write(path, text: str, mtime_ns: int | None, roots, files=(), force: bool = False) -> dict:
    """A doc saved from the editor: {path, mtime_ns, stamp} as written, path resolved
    (permitted, else Outside). mtime_ns is the file's when the editor took its text (None for
    a file that wasn't there): one changed since is Conflict, one gone since Gone, unless
    force (Overwrite, or Write anew). Written atomically, a temporary file beside it renamed
    over it, with the file's mode kept; a new file is made as the daemon's user makes any,
    under its umask. A file over DOC_CAP is refused: the editor holds only that much of it.
    Unwritable, naming the path and the reason, for any write the filesystem refuses."""
    real = permitted(path, roots, files)
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    try:
        st = os.stat(real)
    except FileNotFoundError:
        st = None
    except OSError as e:
        raise Unwritable(f"{real} can't be written: {e.strerror}") from None
    if st is not None and not stat.S_ISREG(st.st_mode):
        raise Unwritable(f"{real} can't be written: it isn't a regular file")
    if st is not None and st.st_size > DOC_CAP:
        raise Unwritable(f"{real} can't be written from here: it's over {DOC_CAP >> 20} MB, more than the editor holds")
    if st is not None and not os.access(real, os.W_OK):   # a rename over it would replace a read-only file
        raise Unwritable(f"{real} can't be written: Permission denied")
    if st is not None and not utf8(real):
        raise Unwritable(f"{real} can't be written from here: it isn't UTF-8 text, so the editor shows some of its "
                         "bytes as \ufffd and a save would change them")
    if not force:
        if st is None and mtime_ns is not None:
            raise Gone(f"{real} has gone from disk since you started editing")
        if st is not None and st.st_mtime_ns != mtime_ns:
            raise Conflict(real, st, current(real))
    data = text.encode()
    try:
        if st is None:
            made(real, data)
        else:
            replaced(real, data, stat.S_IMODE(st.st_mode))
        st = os.stat(real)
    except FileExistsError:   # made meanwhile, between the stat and the write
        if not force:
            st = os.stat(real)
            raise Conflict(real, st, current(real)) from None
        return write(path, text, None, roots, files, force)
    except OSError as e:
        raise Unwritable(f"{real} can't be written: {e.strerror or e}") from None
    return {"path": str(real), "mtime_ns": st.st_mtime_ns, "stamp": f"{st.st_mtime_ns}:{st.st_size}"}


def save(path, text: str, mtime_ns: int | None, roots, files=(), force: bool = False) -> dict:
    """write's outcome as either client answers it (client.write_doc): {path, mtime_ns, stamp}
    saved; {conflict: true, mtime_ns, text, error} for a file changed since; {gone: true,
    error} for one gone; {refused: why} for a path outside or a write refused."""
    try:
        return write(path, text, mtime_ns, roots, files, force)
    except Conflict as e:
        return {"conflict": True, "mtime_ns": e.mtime_ns, "stamp": e.stamp, "text": e.text, "error": str(e)}
    except Gone as e:
        return {"gone": True, "error": str(e)}
    except (ValueError, Unwritable) as e:   # Outside is a ValueError
        return {"refused": str(e)}


def utf8(real: Path) -> bool:
    """Whether a file's bytes are UTF-8, so the editor's text of it is them exactly; True if
    it can't be read (the write then says why)."""
    try:
        real.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return False
    except OSError:
        return True
    return True


def current(real: Path) -> str:
    """A file's text as it is now, for a Conflict's Reload: "" if it can't be read."""
    try:
        with open(real, "rb") as f:
            return f.read(DOC_CAP).decode("utf-8", errors="replace")
    except OSError:
        return ""


def made(real: Path, data: bytes) -> None:
    """A new file, never one there already (FileExistsError), its mode the umask's."""
    fd = os.open(real, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(real)
        raise


def replaced(real: Path, data: bytes, mode: int) -> None:
    """A file's contents swapped whole: a temporary file beside it, its mode set, renamed over
    it, so a reader sees the old text or the new, never half; the temporary file gone on any
    failure."""
    fd, tmp = tempfile.mkstemp(dir=real.parent, prefix=f".{real.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fchmod(f.fileno(), mode)
            os.fsync(f.fileno())
        os.replace(tmp, real)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def git_root(cwd) -> Path | None:
    """The git root of a directory: the nearest holding .git, or for a linked worktree its
    main repository's (main_root), so a repo is one repo whichever worktree a session is in;
    None outside any repo, which then has no doc dirs (never the directory itself: a session
    in ~ would otherwise reach all of it)."""
    tree = work_tree(cwd)
    return main_root(tree) if tree is not None else None


def work_tree(path) -> Path | None:
    """The nearest directory holding .git at or above path (resolved; it need not exist yet),
    None if there is none."""
    here = Path(os.path.realpath(path))
    return next((d for d in (here, *here.parents) if (d / ".git").exists()), None)


def main_root(root: Path) -> Path:
    """A linked worktree's main repository: its .git file names its gitdir, whose commondir
    is the main repository's .git (git rev-parse --git-common-dir, read without running git).
    root itself otherwise: a main checkout, a submodule (a gitdir with no commondir), or a
    worktree of a bare repository, which has no main checkout."""
    dot = root / ".git"
    try:
        if not dot.is_file():
            return root
        text = dot.read_text(errors="replace").strip()
        if not text.startswith("gitdir:"):
            return root
        gitdir = root / text[len("gitdir:"):].strip()   # an absolute one replaces root
        common = Path(os.path.realpath(gitdir / (gitdir / "commondir").read_text(errors="replace").strip()))
    except OSError:
        return root
    return common.parent if common.name == ".git" and common.is_dir() else root


def allowed(real: Path, root: Path | None) -> str | None:
    """Why real, a resolved directory declared from a session whose git root is root (None for
    a session in no git repo), can't hold its decisions, None if it can: under root but not
    root itself, or in another git work tree (a sibling repo kept for docs) but not that
    tree's root. Anything in no git repo is refused, so a session can't point the wheelhouse
    at arbitrary files by mistake: a guard, not a security boundary (a repo at ~ would admit
    everything under it)."""
    if root is not None:
        if real == root:
            return "is the git root itself: name the directory the decisions are in"
        if real.is_relative_to(root):
            return None
    tree = work_tree(real)
    if tree is None:
        return "is in no git repo" if root is None else f"is outside the git root, {root}, and in no other git repo"
    if real == tree:
        return f"is the root of {tree.name}: name the directory the decisions are in"
    return None


def checked(paths, cwd) -> list[str]:
    """Directories a session declares (set_doc_dirs), each relative to its git root or
    absolute, under it or in another git repo (allowed), normalised; ValueError for any that
    isn't. A session in no git repo (#220) may still name one in a git repo, absolute or
    relative to its own directory (a repo under it, or by .. beside it), kept absolute since
    there is no root to resolve it against later."""
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise ValueError("paths must be a list of directory paths")
    if len(paths) > MOST:
        raise ValueError(f"at most {MOST} directories")
    root = git_root(cwd)
    base = root if root is not None else Path(os.path.realpath(cwd))   # from where it really is
    out = []
    for p in paths:
        if not p.strip() or "\0" in p:
            where = f"relative to the git root ({root}) or absolute" if root else "relative to here or absolute"
            raise ValueError(f"{p!r}: give a directory, {where}")
        # with no root to be relative to later, kept absolute, and checked as kept: a link
        # before a .. would otherwise check one directory and keep another
        norm = os.path.normpath(p if root is not None else base / p)
        real = Path(os.path.realpath(base / p if root is not None else norm))
        if why := allowed(real, root):
            raise ValueError(f"{p!r} {why}")
        if norm not in out:
            out.append(norm)
    return out


def declared(raw: str | None) -> list[str] | None:
    """A session's declared directories as the store keeps them (JSON), None for none."""
    return json.loads(raw) if raw else None


def doc_dirs(sessions) -> list[Path]:
    """Where decisions are found for every repo a session works in (decision_dirs), the
    directories alone."""
    return [d for d, _ in decision_dirs(sessions)]


def decision_dirs(sessions) -> list[tuple[Path, Path]]:
    """Where decisions are found for every repo a session works in (each a dict with "cwd"
    and "doc_dirs", declared or None), each with the git root of the repo that looks there:
    per git root, every directory the sessions there declared, else DEFAULT. Each resolved,
    and left out if it isn't a directory or isn't allowed (since it was declared, a link may
    have moved). A directory several repos look in is listed once, with the one whose root sorts first.
    A session in no git repo adds the absolute directories it declared (#220), each with the
    git root of the repo it is in, as it has none of its own."""
    repos: dict[Path, set[str]] = {}
    loose: set[str] = set()
    for s in sessions:
        if (root := git_root(s["cwd"])) is None:
            loose.update(d for d in s.get("doc_dirs") or () if os.path.isabs(d))
            continue
        repos.setdefault(root, set()).update(s.get("doc_dirs") or ())
    out: dict[Path, Path] = {}
    for root in sorted(repos):
        for d in repos[root] or DEFAULT:
            real = Path(os.path.realpath(root / d))
            if real not in out and allowed(real, root) is None and real.is_dir():
                out[real] = root
    for d in loose:
        real = Path(os.path.realpath(d))
        if allowed(real, None) is None and real.is_dir():
            own = git_root(real)
            out[real] = min(out.get(real, own), own)
    return sorted(out.items())
