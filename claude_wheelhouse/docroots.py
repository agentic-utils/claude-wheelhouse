"""The docs a client may read through the hub, and later write (.plan/modules.md, "The module
boundary"): only a .md file under a directory a module declares (api.Module.doc_roots). Each
path is resolved first, its symlinks followed and its .. taken out, so none reaches outside a
root; the roots are resolved the same way. And where a repo's decisions are found (Q114):
docs/adr under its git root, unless a session there declared others (set_doc_dirs), which
may be in another repo kept beside it. Never imports Textual."""

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


def allowed(real: Path, root: Path) -> str | None:
    """Why real, a resolved directory declared from a session whose git root is root, can't
    hold its decisions, None if it can: under root but not root itself, or in another git
    work tree (a sibling repo kept for docs) but not that tree's root. Anything in no git
    repo is refused, so a session can't point the wheelhouse at arbitrary files."""
    if real == root:
        return "is the git root itself: name the directory the decisions are in"
    if real.is_relative_to(root):
        return None
    tree = work_tree(real)
    if tree is None:
        return f"is outside the git root, {root}, and in no other git repo"
    if real == tree:
        return f"is the root of {tree.name}: name the directory the decisions are in"
    return None


def checked(paths, cwd) -> list[str]:
    """Directories a session declares (set_doc_dirs), each relative to its git root or
    absolute, under it or in another git repo (allowed), normalised; ValueError for any that
    isn't."""
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise ValueError("paths must be a list of directory paths")
    if len(paths) > MOST:
        raise ValueError(f"at most {MOST} directories")
    root = git_root(cwd)
    if root is None:
        raise ValueError(f"{cwd} is in no git repo: doc dirs are only looked for in one")
    out = []
    for p in paths:
        if not p.strip() or "\0" in p:
            raise ValueError(f"{p!r}: give a directory, relative to the git root ({root}) or absolute")
        if why := allowed(Path(os.path.realpath(root / p)), root):
            raise ValueError(f"{p!r} {why}")
        if (norm := os.path.normpath(p)) not in out:
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
    have moved). A directory several repos look in is listed once, with the first of them.
    A session in no git repo adds none."""
    repos: dict[Path, set[str]] = {}
    for s in sessions:
        if (root := git_root(s["cwd"])) is None:
            continue
        repos.setdefault(root, set()).update(s.get("doc_dirs") or ())
    out: dict[Path, Path] = {}
    for root in sorted(repos):
        for d in repos[root] or DEFAULT:
            real = Path(os.path.realpath(root / d))
            if real not in out and allowed(real, root) is None and real.is_dir():
                out[real] = root
    return sorted(out.items())
