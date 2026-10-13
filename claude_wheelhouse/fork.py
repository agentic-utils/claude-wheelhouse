"""Fork a session: a new session in the wheelhouse that carries on from the same
conversation, the parent left as it was (Hub.fork).

The conversation is copied by the Agent SDK's fork_session, which writes the copy, under a
new session id, beside the parent's transcript and runs nothing: the wheelhouse knows the
id before anything launches, so the fork is launched as any session is, with --resume.
Claude Code finds a transcript by its directory (~/.claude/projects/<cwd slug>/), so a fork
in another directory has its copy moved there (place).

Where the parent works in a linked git worktree, the fork may work in a new one, on a new
branch made from the parent's HEAD (git refuses one branch in two worktrees): nothing
uncommitted is stashed, copied or committed, so the fork doesn't see it, and is told so.
Items move rather than copy: those ticked are re-owned by the fork (Store.move_items)."""

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .store import MOVABLE   # what the Fork dialog offers to move


@dataclass
class Tree:
    """A session's directory as git sees it. linked: inside a linked worktree (its git dir
    isn't the common one), whose top level is top, the main checkout main, the branch
    branch ("" when detached). dirty: uncommitted changes there (git status --porcelain)."""
    linked: bool = False
    top: str = ""
    main: str = ""
    branch: str = ""
    dirty: bool = False

    @property
    def can_branch(self) -> bool:
        """Whether a fork can have a new worktree: in a linked one whose main worktree is known."""
        return self.linked and bool(self.main)


@dataclass
class Plan:
    """What the Fork dialog offers for a session (Hub.fork_plan): its fork's name, its
    directory, the worktree choice (tree.linked) with the new branch's name, and the items
    that may move, each (ref, kind, status, title)."""
    name: str
    cwd: str
    tree: Tree
    branch: str = ""
    items: list[tuple[str, str, str, str]] = field(default_factory=list)


def git(cwd: str, *args: str) -> str | None:
    """git's output, stripped, or None if it failed (not a repository, git missing)."""
    try:
        r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def tree(cwd: str) -> Tree:
    """cwd as git sees it: a linked worktree when its git dir differs from the common one,
    whose main worktree is the first `git worktree list` gives (a bare repository's own
    directory; "" when git can't say, as for --separate-git-dir). Paths git gives
    relative are cwd's. Raises ValueError on output it doesn't expect, rather than call a
    worktree a plain directory."""
    dirs = git(cwd, "rev-parse", "--git-dir", "--git-common-dir", "--show-toplevel")
    if dirs is None:   # not a repository (or not a work tree): nothing to offer
        return Tree()
    lines = dirs.splitlines()
    if len(lines) != 3:
        raise ValueError(f"git rev-parse gave {len(lines)} lines in {cwd}, not 3: {dirs!r}")
    git_dir, common, top = ((Path(cwd) / line).resolve() for line in lines)
    if git_dir == common:
        return Tree()
    listed = (git(cwd, "worktree", "list", "--porcelain") or "").split("\n\n")[0].splitlines()
    if not listed or not listed[0].startswith("worktree "):
        raise ValueError(f"git worktree list gave no main worktree in {cwd}: {listed!r}")
    main = Path(listed[0].removeprefix("worktree ")).resolve()
    # git names the git dir itself for a --separate-git-dir checkout, which nothing points back
    # to: main stays unknown, and only this worktree is offered
    known = "bare" in listed or main != common
    branch = git(cwd, "symbolic-ref", "--quiet", "--short", "HEAD") or ""
    return Tree(True, str(top), str(main) if known else "", branch, bool(git(cwd, "status", "--porcelain")))


def worktree_path(t: Tree, branch: str) -> Path:
    """Where a new worktree for branch goes: the main worktree's .worktrees, one level, a /
    in the branch's name a -."""
    return Path(t.main) / ".worktrees" / branch.replace("/", "-")


def branch_taken(t: Tree, name: str) -> bool:
    return git(t.main, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}") is not None


def free_branch(t: Tree, base: str) -> str:
    """base, else base-2, base-3...: the first that is neither a branch nor a worktree's directory."""
    n = 1
    while True:
        name = base if n == 1 else f"{base}-{n}"
        if not branch_taken(t, name) and not worktree_path(t, name).exists():
            return name
        n += 1


def default_branch(t: Tree, cwd: str) -> str:
    """The new branch's name as offered: the parent's with -fork, de-duplicated (detached:
    its worktree's directory name's); "" with no main worktree known to put it under."""
    if not t.main:
        return ""
    return free_branch(t, f"{t.branch or Path(t.top or cwd).name}-fork")


def new_worktree(cwd: str, t: Tree, branch: str) -> str:
    """A worktree on a new branch made from cwd's HEAD, at worktree_path; the fork's
    directory in it, cwd's own place under the top level, or the top level where that isn't
    there (nothing committed in it: untracked or ignored). Raises ValueError with git's words."""
    path = worktree_path(t, branch)
    try:
        inside = Path(cwd).resolve().relative_to(Path(t.top).resolve())
    except ValueError:
        inside = Path()
    existed = branch_taken(t, branch)
    r = subprocess.run(["git", "-C", cwd, "worktree", "add", "-b", branch, str(path), "HEAD"],
                       capture_output=True, text=True, timeout=60)
    if r.returncode:
        if not existed and branch_taken(t, branch):   # git makes the branch before it fails on the path
            git(t.main, "branch", "-D", branch)
        raise ValueError(f"couldn't make a worktree on {branch}: {(r.stderr or r.stdout).strip()}")
    return str(path / inside) if (path / inside).is_dir() else str(path)


def drop_worktree(t: Tree, branch: str) -> None:
    """A worktree new_worktree made, and its branch, taken away again: the fork failed after it."""
    git(t.main, "worktree", "remove", "--force", str(worktree_path(t, branch)))
    git(t.main, "branch", "-D", branch)


def undo(new: str, t: Tree, branch: str | None) -> None:
    """A fork's transcript, and its worktree and branch if it had them, taken away again."""
    drop_copy(new)
    if branch:
        drop_worktree(t, branch)


def log(store, text: str) -> None:
    """A line in launch.log, beside the database: what a fork made, so anything a wheelhouse
    stopping half way leaves behind can be found."""
    from datetime import datetime, timezone
    with open(store.path.parent / "launch.log", "a") as f:
        print(datetime.now(timezone.utc).isoformat(timespec="seconds"), text, file=f)


def projects_dir() -> Path:
    from claude_agent_sdk._internal.sessions import _get_projects_dir
    return _get_projects_dir()


def copy(sid: str, cwd: str, title: str, to_cwd: str) -> str:
    """The parent's transcript copied under a new session id (the Agent SDK's fork_session),
    in to_cwd's project directory (place); if it can't be put there, the copy is taken away
    again. The new id."""
    from claude_agent_sdk import fork_session   # the SDK is heavy: only loaded to fork
    new = fork_session(sid, directory=cwd, title=title).session_id
    if to_cwd != cwd:
        try:
            place(new, to_cwd)
        except BaseException:
            drop_copy(new)
            raise
    return new


def drop_copy(sid: str) -> None:
    """A fork's transcript taken away again, wherever it is: the fork failed after it."""
    for path in projects_dir().glob(f"*/{sid}.jsonl"):
        path.unlink(missing_ok=True)


def place(sid: str, cwd: str) -> Path:
    """A transcript moved into the project directory Claude Code reads for cwd, as the SDK
    names it (realpath, sanitised, hashed if long)."""
    from claude_agent_sdk import project_key_for_directory
    projects = projects_dir()
    src = next(projects.glob(f"*/{sid}.jsonl"), None)
    if src is None:
        raise RuntimeError(f"the fork's transcript, {sid}.jsonl, isn't under {projects}")
    dest = projects / project_key_for_directory(cwd) / src.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dest)
    return dest


def listing(moved: list[tuple[str, str]], titles: dict[str, str], new_first: bool) -> str:
    """The moved items, one per line: `T1 (was T3) title` for the fork, `T3 (its T1) title`
    for the parent."""
    return "\n".join(f"{new} (was {old}) {titles[old]}" if new_first else f"{old} (its {new}) {titles[old]}"
                     for old, new in moved)


def opening(parent: str, name: str, moved: list[tuple[str, str]], titles: dict[str, str],
            branch: str = "", path: str = "", dirty: bool = False) -> str:
    """What the fork is told as it starts, in place of launch.JOINED_TEXT."""
    parts = [f"You are {name}, a fork of {parent}: the conversation above is a copy of its own, taken just now. "
             f"From here you are a separate session, and the items in it stay {parent}'s unless listed below as yours."]
    if moved:
        parts.append(f"Moved to you from {parent}, now yours:\n" + listing(moved, titles, True))
    if branch:
        parts.append(f"You run in a new worktree, {path}, on a new branch, {branch}, made from {parent}'s last commit."
                     + (f" {parent}'s uncommitted changes stay in its worktree: you don't see them, so don't read "
                        "missing work in progress as a regression." if dirty else ""))
    parts.append("Call list_items, post as items the questions you are waiting on the person for and the tasks you "
                 "have running (skip any already there), and set your synopsis. Then stop: don't carry on "
                 f"{parent}'s work because of this message.")
    return "\n\n".join(parts)


def told_parent(name: str, moved: list[tuple[str, str]], titles: dict[str, str]) -> str:
    """What the parent is told once items have moved to its fork, name."""
    return (f"The person forked you as {name}. These items moved to it and are no longer yours: leave them to it "
            "(their refs here hold closed stand-ins):\n" + listing(moved, titles, False))
