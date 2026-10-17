"""The docs a client may read through the hub, and later write (.plan/modules.md, "The module
boundary"): only a .md file under a directory a module declares (api.Module.doc_roots). Each
path is resolved first, its symlinks followed and its .. taken out, so none reaches outside a
root; the roots are resolved the same way. And where a repo's decisions are found (Q114):
docs/adr under its git root, unless a session there declared others (set_doc_dirs). Never
imports Textual."""

import json
import os
import stat
from pathlib import Path

DEFAULT = ("docs/adr",)   # a repo's decisions, unless a session there declares others (Q114)
SUFFIX = ".md"
MOST = 20                 # directories a session may declare
DOC_CAP = 1 << 20   # bytes of a doc read at most: the rest is left out, and said to be


class Outside(ValueError):
    """A path no doc root holds, or one that isn't a doc's."""


def confine(path, roots) -> Path:
    """path resolved, if it names a .md file under one of roots (each resolved too); else
    Outside. Only an absolute path: a relative one would resolve against the daemon's own
    directory. A relative root is no root. Compared as the filesystem gives the names, so on
    a case-insensitive one a path spelt in another case than its root's is refused, never
    let in. The file need not exist (a doc written anew, stage 8)."""
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


def git_root(cwd) -> Path | None:
    """The git root of a directory: the nearest holding .git (a directory, or a linked
    worktree's file); None outside any repo, which then has no doc dirs (never the directory
    itself: a session in ~ would otherwise reach all of it)."""
    here = Path(os.path.realpath(cwd))
    for d in (here, *here.parents):
        if (d / ".git").exists():
            return d
    return None


def checked(paths, cwd) -> list[str]:
    """Directories a session declares (set_doc_dirs), each relative to its git root and
    staying in it once resolved, normalised; ValueError for any that isn't."""
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise ValueError("paths must be a list of directory paths")
    if len(paths) > MOST:
        raise ValueError(f"at most {MOST} directories")
    root = git_root(cwd)
    if root is None:
        raise ValueError(f"{cwd} is in no git repo: doc dirs are only looked for in one")
    out = []
    for p in paths:
        if not p.strip() or os.path.isabs(p) or "\0" in p:
            raise ValueError(f"{p!r}: give a directory relative to the git root, {root}")
        real = Path(os.path.realpath(root / p))
        if not real.is_relative_to(root):
            raise ValueError(f"{p!r} is outside the git root, {root}")
        if real == root:
            raise ValueError(f"{p!r} is the git root itself: name the directory the decisions are in")
        if (norm := os.path.normpath(p)) not in out:
            out.append(norm)
    return out


def declared(raw: str | None) -> list[str] | None:
    """A session's declared directories as the store keeps them (JSON), None for none."""
    return json.loads(raw) if raw else None


def doc_dirs(sessions) -> list[Path]:
    """Where decisions are found for every repo a session works in (each a dict with "cwd"
    and "doc_dirs", declared or None): per git root, every directory the sessions there
    declared, else DEFAULT. Each resolved, and left out if it isn't a directory, is the git
    root itself or resolves outside it. A session in no git repo adds none."""
    repos: dict[Path, set[str]] = {}
    for s in sessions:
        if (root := git_root(s["cwd"])) is None:
            continue
        repos.setdefault(root, set()).update(s.get("doc_dirs") or ())
    out = set()
    for root, dirs in repos.items():
        for d in dirs or DEFAULT:
            real = Path(os.path.realpath(root / d))
            if real != root and real.is_relative_to(root) and real.is_dir():
                out.add(real)
    return sorted(out)
