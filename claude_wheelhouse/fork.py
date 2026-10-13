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

# what the Fork dialog offers to move: those not under way (no running task) and the
# person's to answer, never an agent, decision or permission
MOVABLE = {"task": ("todo", "blocked"), "question": ("open",)}
# the parent's stand-in for an item moved (Store.move_items), {ref} its ref in the fork
MOVED_NOTE = "Moved to {fork}, a fork of this session, as {ref}."


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
    """cwd as git sees it: a linked worktree when its git dir differs from the common one."""
    dirs = git(cwd, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir", "--show-toplevel")
    if dirs is None or len(dirs.splitlines()) != 3:
        return Tree()
    git_dir, common, top = dirs.splitlines()
    if Path(git_dir).resolve() == Path(common).resolve():
        return Tree()
    branch = git(cwd, "symbolic-ref", "--quiet", "--short", "HEAD") or ""
    return Tree(True, top, str(Path(common).parent), branch, bool(git(cwd, "status", "--porcelain")))


def worktree_path(t: Tree, branch: str) -> Path:
    """Where a new worktree for branch goes: the main checkout's .worktrees, one level, a /
    in the branch's name a -."""
    return Path(t.main) / ".worktrees" / branch.replace("/", "-")


def free_branch(t: Tree, base: str) -> str:
    """base, else base-2, base-3...: the first that is neither a branch nor a worktree's directory."""
    n = 1
    while True:
        name = base if n == 1 else f"{base}-{n}"
        if git(t.main, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}") is None \
                and not worktree_path(t, name).exists():
            return name
        n += 1


def default_branch(t: Tree, cwd: str) -> str:
    """The new branch's name as offered: the parent's with -fork, de-duplicated (detached:
    its worktree's directory name's)."""
    return free_branch(t, f"{t.branch or Path(t.top or cwd).name}-fork")


def new_worktree(cwd: str, t: Tree, branch: str) -> str:
    """A worktree on a new branch made from cwd's HEAD, at worktree_path; the fork's
    directory in it, cwd's own place under the top level. Raises ValueError with git's words."""
    path = worktree_path(t, branch)
    r = subprocess.run(["git", "-C", cwd, "worktree", "add", "-b", branch, str(path), "HEAD"],
                       capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise ValueError(f"couldn't make a worktree on {branch}: {(r.stderr or r.stdout).strip()}")
    return str(path / Path(cwd).resolve().relative_to(Path(t.top).resolve()))


def drop_worktree(t: Tree, branch: str) -> None:
    """A worktree new_worktree made, and its branch, taken away again: the fork failed after it."""
    git(t.main, "worktree", "remove", "--force", str(worktree_path(t, branch)))
    git(t.main, "branch", "-D", branch)


def copy(sid: str, cwd: str, title: str, to_cwd: str) -> str:
    """The parent's transcript copied under a new session id (the Agent SDK's fork_session),
    in to_cwd's project directory (place). The new id."""
    from claude_agent_sdk import fork_session   # the SDK is heavy: only loaded to fork
    new = fork_session(sid, directory=cwd, title=title).session_id
    if to_cwd != cwd:
        place(new, to_cwd)
    return new


def place(sid: str, cwd: str) -> Path:
    """A transcript moved into the project directory Claude Code reads for cwd, as the SDK
    names it (realpath, sanitised, hashed if long)."""
    from claude_agent_sdk import project_key_for_directory
    from claude_agent_sdk._internal.sessions import _get_projects_dir
    projects = _get_projects_dir()
    src = next(projects.glob(f"*/{sid}.jsonl"))
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
