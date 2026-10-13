"""Forking a session's directory and transcript (fork.py), on real git repositories."""

import pytest

from claude_wheelhouse import fork
from conftest import git


@pytest.mark.parametrize("where, dirty, linked, branch, desc", [
    ("main", False, False, "", "the main checkout isn't a linked worktree"),
    ("linked", False, True, "feat/x", "a linked worktree, with its branch"),
    ("linked/sub", False, True, "feat/x", "a directory inside one"),
    ("linked", True, True, "feat/x", "with uncommitted changes: dirty"),
    ("plain", False, False, "", "not a repository at all"),
])
def test_tree(worktree, tmp_path, where, dirty, linked, branch, desc):
    main, wt = worktree
    (wt / "sub").mkdir()
    (tmp_path / "plain").mkdir()
    if dirty:
        (wt / "new.txt").write_text("wip")
    cwd = {"main": main, "linked": wt, "linked/sub": wt / "sub", "plain": tmp_path / "plain"}[where]
    t = fork.tree(str(cwd))
    assert (t.linked, t.branch, t.dirty) == (linked, branch, dirty), desc
    if linked:
        assert (t.main, t.top) == (str(main.resolve()), str(wt.resolve())), desc


@pytest.mark.parametrize("taken, want, desc", [
    ((), "feat/x-fork", "the parent's branch with -fork"),
    (("branch feat/x-fork",), "feat/x-fork-2", "a branch of that name already: de-duplicated"),
    (("dir feat-x-fork",), "feat/x-fork-2", "a directory in .worktrees by that name: de-duplicated"),
    (("branch feat/x-fork", "branch feat/x-fork-2"), "feat/x-fork-3", "on until one is free"),
])
def test_default_branch(worktree, taken, want, desc):
    main, wt = worktree
    for what in taken:
        kind, name = what.split()
        if kind == "branch":
            git(main, "branch", name)
        else:
            (main / ".worktrees" / name).mkdir()
    assert fork.default_branch(fork.tree(str(wt)), str(wt)) == want, desc


def test_a_new_worktree_is_made_from_head_on_a_new_branch_without_the_uncommitted_work(worktree):
    main, wt = worktree
    (wt / "sub").mkdir()
    (wt / "sub/kept.txt").write_text("committed")
    git(wt, "add", ".")
    git(wt, "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "two")
    (wt / "sub/wip.txt").write_text("uncommitted")
    t = fork.tree(str(wt / "sub"))
    cwd = fork.new_worktree(str(wt / "sub"), t, "feat/x-fork")
    new = main / ".worktrees" / "feat-x-fork"
    assert cwd == str(new / "sub"), "the fork's directory is the parent's place in the new worktree"
    assert git(new, "rev-parse", "HEAD") == git(wt, "rev-parse", "HEAD"), "made from the parent's HEAD"
    assert git(new, "symbolic-ref", "--short", "HEAD") == "feat/x-fork", "on the new branch"
    assert (new / "sub/kept.txt").exists() and not (new / "sub/wip.txt").exists(), "uncommitted work stays behind"
    assert (wt / "sub/wip.txt").exists(), "and isn't stashed or moved"
    with pytest.raises(ValueError, match="couldn't make a worktree on feat/x"):
        fork.new_worktree(str(wt), t, "feat/x")   # git refuses a branch checked out elsewhere


@pytest.mark.parametrize("other, desc", [
    (False, "the same directory: beside the parent's"),
    (True, "another directory: moved to its project folder, where claude --resume looks"),
])
def test_copy_places_the_transcript_where_its_directory_is(claude_home, tmp_path, other, desc):
    from claude_agent_sdk import project_key_for_directory
    parent = "11111111-1111-4111-8111-111111111111"
    src = claude_home(parent, str(tmp_path))
    to = tmp_path / "elsewhere"
    to.mkdir()
    new = fork.copy(parent, str(tmp_path), "demo (fork)", str(to if other else tmp_path))
    found = list(claude_home.projects.glob(f"*/{new}.jsonl"))
    want = claude_home.projects / project_key_for_directory(str(to if other else tmp_path)) / f"{new}.jsonl"
    assert found == [want], desc
    assert src.exists() and "hello" in want.read_text(), f"{desc}: the parent's kept, the conversation copied"


@pytest.mark.parametrize("moved, branch, dirty, has, lacks, desc", [
    ([], "", False, ["a fork of demo", "set your synopsis"], ["Moved to you", "worktree"], "nothing moved, same directory"),
    ([("T3", "T1")], "", False, ["Moved to you from demo, now yours:\nT1 (was T3) fix it"], ["worktree"],
     "a moved item, under its new ref"),
    ([], "demo-fork", False, ["new worktree, /w, on a new branch, demo-fork"], ["uncommitted"], "a new worktree, clean"),
    ([], "demo-fork", True, ["new worktree", "don't read missing work in progress as a regression"], [],
     "a new worktree, the parent's uncommitted changes left behind"),
])
def test_opening(moved, branch, dirty, has, lacks, desc):
    text = fork.opening("demo", "demo (fork)", moved, {"T3": "fix it"}, branch, "/w" if branch else "", dirty)
    assert all(h in text for h in has) and not any(x in text for x in lacks), f"{desc}: {text}"
    assert "\u2014" not in text, f"{desc}: no em-dash"


@pytest.mark.parametrize("layout, desc", [
    ("bare", "a bare repository with worktrees: its main worktree is the bare directory, not the .git's parent"),
    ("separate", "--separate-git-dir: git names the git dir, so no main worktree, and no new worktree offered"),
])
def test_tree_finds_the_main_worktree(tmp_path, layout, desc):
    c = ["-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false"]
    if layout == "bare":
        main = tmp_path / "repo.git"
        git(tmp_path, "init", "-q", "--bare", "-b", "main", str(main))
        seed = tmp_path / "seed"
        git(tmp_path, "clone", "-q", str(main), str(seed))
        git(seed, *c, "commit", "-q", "--allow-empty", "-m", "first")
        git(seed, "push", "-q", "origin", "HEAD:main")
    else:
        main = tmp_path / "checkout"
        git(tmp_path, "init", "-q", "-b", "main", "--separate-git-dir", str(tmp_path / "gitdir"), str(main))
        git(main, *c, "commit", "-q", "--allow-empty", "-m", "first")
    wt = tmp_path / "wt"
    git(main, "worktree", "add", "-q", "-b", "feat", str(wt))
    t = fork.tree(str(wt))
    want = str(main.resolve()) if layout == "bare" else ""
    assert (t.linked, t.main, t.branch, t.can_branch) == (True, want, "feat", bool(want)), desc
    assert fork.default_branch(t, str(wt)) == ("feat-fork" if want else ""), f"{desc}: a new worktree offered or not"


@pytest.mark.parametrize("answers, desc", [
    ({"rev-parse": "a\nb\nc\nd"}, "four lines from rev-parse (an older git, say): refused, not called plain"),
    ({"rev-parse": ".git-wt\n/x/.git\n/x/wt", "worktree": "bare\n"}, "worktree list without a main worktree"),
])
def test_tree_refuses_output_it_doesnt_expect(monkeypatch, tmp_path, answers, desc):
    monkeypatch.setattr(fork, "git", lambda cwd, *args: answers.get(args[0]))
    with pytest.raises(ValueError, match="git "):
        fork.tree(str(tmp_path))


def test_place_says_what_is_missing(claude_home):
    with pytest.raises(RuntimeError, match="the fork's transcript, nope.jsonl, isn't under"):
        fork.place("nope", "/x")


def test_copy_takes_its_copy_away_when_it_cant_be_placed(claude_home, tmp_path, monkeypatch):
    parent = "11111111-1111-4111-8111-111111111111"
    claude_home(parent, str(tmp_path))
    monkeypatch.setattr(fork, "place", lambda sid, cwd: (_ for _ in ()).throw(OSError("no room")))
    with pytest.raises(OSError, match="no room"):
        fork.copy(parent, str(tmp_path), "demo (fork)", str(tmp_path / "elsewhere"))
    assert [p.stem for p in claude_home.projects.glob("*/*.jsonl")] == [parent], "only the parent's left"
