import os

import pytest

from claude_wheelhouse import docroots
from claude_wheelhouse.docroots import Outside


@pytest.fixture
def tree(tmp_path):
    """A doc root, root/, with a.md and sub/b.md; outside it, out/c.md and a sibling rootx/
    sharing its prefix; inside it, links out (to a file and a directory) and one staying in."""
    root, out = tmp_path / "root", tmp_path / "out"
    (root / "sub").mkdir(parents=True)
    out.mkdir()
    (tmp_path / "rootx").mkdir()
    for f in (root / "a.md", root / "sub" / "b.md", out / "c.md", tmp_path / "rootx" / "d.md", root / "a.txt"):
        f.write_text(f"# {f.name}\n")
    (root / "out.md").symlink_to(out / "c.md")
    (root / "outdir").symlink_to(out)
    (root / "in.md").symlink_to(root / "a.md")
    (root / "dir.md").mkdir()
    (tmp_path / "rootlink").symlink_to(root)
    return tmp_path


@pytest.mark.parametrize("path, roots, allowed, desc", [
    ("root/a.md", ["root"], "root/a.md", "a doc in the root"),
    ("root/sub/b.md", ["root"], "root/sub/b.md", "one in a directory under it"),
    ("root/sub/../a.md", ["root"], "root/a.md", ".. that stays inside"),
    ("root/new.md", ["root"], "root/new.md", "one not written yet (a new doc, stage 8)"),
    ("root/in.md", ["root"], "root/a.md", "a symlink staying inside, resolved"),
    ("rootlink/a.md", ["root"], "root/a.md", "through a symlink to the root"),
    ("root/a.md", ["rootlink"], "root/a.md", "a root given as a symlink, resolved too"),
    ("root/../out/c.md", ["root"], None, ".. out of the root"),
    ("root/sub/../../out/c.md", ["root"], None, ".. out from deeper"),
    ("root/out.md", ["root"], None, "a symlink out"),
    ("root/outdir/c.md", ["root"], None, "through a symlinked directory out"),
    ("out/c.md", ["root"], None, "absolute, elsewhere"),
    ("rootx/d.md", ["root"], None, "a sibling sharing the root's prefix"),
    ("ROOT/a.md", ["root"], None, "the root spelt in another case"),
    ("root/A.MD", ["root"], None, "a suffix in another case"),
    ("root/a.txt", ["root"], None, "not a .md file"),
    ("root", ["root"], None, "the root itself"),
    ("a.md", ["root"], None, "a relative path"),
    ("root/a.md", [], None, "no roots"),
    ("root/a.md", ["relative"], None, "a relative root is no root"),
    ("out/c.md", ["root", "out"], "out/c.md", "under the second of two roots"),
])
def test_confine(tree, path, roots, allowed, desc):
    given = path if path == "a.md" else str(tree / path)
    roots = [r if r == "relative" else str(tree / r) for r in roots]
    if allowed is None:
        with pytest.raises(Outside):
            docroots.confine(given, roots)
            pytest.fail(desc)
    else:
        assert docroots.confine(given, roots) == (tree / allowed).resolve(), desc


@pytest.mark.parametrize("path, have, expect, desc", [
    ("root/a.md", None, {"text": "# a.md\n", "gone": None}, "its text"),
    ("root/gone.md", None, {"text": "", "gone": "gone", "stamp": None}, "a file not there"),
    ("root/dir.md", None, {"text": "", "gone": "not a file"}, "a directory with a doc's name"),
])
def test_read(tree, path, have, expect, desc):
    doc = docroots.read(str(tree / path), [str(tree / "root")], have)
    assert {k: doc[k] for k in expect} == expect, desc


def test_read_answers_same_while_the_stamp_holds(tree):
    path, roots = str(tree / "root/a.md"), [str(tree / "root")]
    first = docroots.read(path, roots)
    assert first["mtime_ns"] == os.stat(path).st_mtime_ns
    assert docroots.read(path, roots, first["stamp"]) == {"same": True}
    (tree / "root/a.md").write_text("# changed, and longer\n")
    assert docroots.read(path, roots, first["stamp"])["text"] == "# changed, and longer\n"


def test_read_refuses_outside(tree):
    with pytest.raises(Outside):
        docroots.read(str(tree / "root/out.md"), [str(tree / "root")])


@pytest.fixture
def repo(tmp_path):
    """A git repo with docs/adr, docs/decisions and .plan, and a link out of it to a directory
    in no repo; beside it, a sibling repo keeping its decisions (a docs repo, #191)."""
    r = tmp_path / "repo"
    for d in (".git", "docs/adr", "docs/decisions", ".plan", "src", "../sib/.git", "../sib/decisions"):
        (r / d).mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    (r / "linkout").symlink_to(tmp_path / "elsewhere")
    (r / "linksib").symlink_to(tmp_path / "sib" / "decisions")
    return r


@pytest.mark.parametrize("paths, expect, desc", [
    (["docs/decisions"], ["docs/decisions"], "a directory under the git root"),
    (["docs/./decisions/", "docs/decisions"], ["docs/decisions"], "normalised, once each"),
    (["docs/adr", ".plan"], ["docs/adr", ".plan"], "several, .plan among them"),
    (["not/yet"], ["not/yet"], "one not made yet"),
    ([], [], "none: back to the default"),
    (["../sib/decisions"], ["../sib/decisions"], "a sibling repo's, by .."),
    (["SIB/decisions"], ["SIB/decisions"], "a sibling repo's, absolute"),
    (["linksib"], ["linksib"], "a link to a sibling repo's"),
    (["../sib/not/yet"], ["../sib/not/yet"], "one not made yet in a sibling repo"),
    (["/etc"], ValueError, "an absolute path in no git repo"),
    (["../x"], ValueError, ".. out of the repo, into no other"),
    (["../sib"], ValueError, "a sibling repo's root itself"),
    (["SIB"], ValueError, "a sibling repo's root itself, absolute"),
    (["docs/../../x"], ValueError, ".. out from deeper"),
    (["linkout"], ValueError, "a symlink out of the repo"),
    ([""], ValueError, "an empty path"),
    (["."], ValueError, "the git root itself"),
    (["src/.."], ValueError, "the git root, by .."),
    ("docs/adr", ValueError, "a string, not a list"),
    ([f"d{i}" for i in range(docroots.MOST + 1)], ValueError, "too many"),
])
def test_checked(repo, paths, expect, desc):
    sib = str(repo.parent / "sib")
    paths = [p.replace("SIB", sib) for p in paths] if isinstance(paths, list) else paths
    expect = [p.replace("SIB", sib) for p in expect] if isinstance(expect, list) else expect
    if expect is ValueError:
        with pytest.raises(ValueError):
            docroots.checked(paths, str(repo / "src"))
            pytest.fail(desc)
    else:
        assert docroots.checked(paths, str(repo / "src")) == expect, desc


@pytest.mark.parametrize("sessions, expect, desc", [
    ([("src", None)], ["docs/adr"], "docs/adr by default, from a session anywhere in the repo"),
    ([("src", ["docs/decisions"])], ["docs/decisions"], "declared dirs replace the default"),
    ([("src", None), ("", ["docs/decisions"])], ["docs/decisions"], "for every session in the repo"),
    ([("src", [".plan"]), ("", ["docs/decisions"])], [".plan", "docs/decisions"], "two sessions' declared, both"),
    ([("src", ["missing"])], [], "a declared dir that isn't there is left out"),
    ([("src", ["linkout"])], [], "one resolving outside the repo is left out"),
    ([("src", ["."])], [], "the git root itself, kept from before the check, is left out"),
    ([], [], "no sessions, no dirs"),
    ([("src", ["../sib/decisions"])], ["../sib/decisions"], "a sibling repo's, declared"),
    ([("src", ["../sib"])], [], "a sibling repo's root, kept from before the check, is left out"),
    ([("src", ["../elsewhere"])], [], "a directory in no git repo is left out"),
])
def test_doc_dirs(repo, sessions, expect, desc):
    given = [{"cwd": str(repo / cwd), "doc_dirs": dirs} for cwd, dirs in sessions]
    assert docroots.doc_dirs(given) == sorted((repo / d).resolve() for d in expect), desc


def test_decision_dirs_lists_a_shared_directory_once_with_the_repo_that_declared_it(repo):
    """Two repos declaring the sibling's decisions: listed once, under the first of them; the
    sibling's own session (default docs/adr, which it hasn't) adds nothing."""
    other = repo.parent / "other"
    (other / ".git").mkdir(parents=True)
    sib = (repo.parent / "sib" / "decisions").resolve()
    given = [{"cwd": str(other), "doc_dirs": [str(sib)]},
             {"cwd": str(repo / "src"), "doc_dirs": ["../sib/decisions", "docs/adr"]},
             {"cwd": str(repo.parent / "sib"), "doc_dirs": None}]
    assert docroots.decision_dirs(given) == [((repo / "docs/adr").resolve(), repo.resolve()),
                                             (sib, other.resolve())]


@pytest.mark.parametrize("dirs, desc", [
    (None, "docs/adr by default"),
    (["."], "the directory itself, declared"),
    (["docs/adr"], "a directory in it, declared"),
])
def test_no_doc_dirs_outside_a_git_repo(tmp_path, dirs, desc):
    """A session in ~, say, with no .git above: nothing of it is a doc root, and a declaration
    of anything in it is refused."""
    (tmp_path / "docs/adr").mkdir(parents=True)
    assert docroots.doc_dirs([{"cwd": str(tmp_path), "doc_dirs": dirs}]) == [], desc
    if dirs:
        with pytest.raises(ValueError, match="in no git repo"):
            docroots.checked(dirs, str(tmp_path))


@pytest.mark.parametrize("cwd, path, kept, refused, desc", [
    ("repo/src", "SIB/decisions", "SIB/decisions", None, "in a repo: absolute, into a sibling repo"),
    ("repo/src", "../sib/decisions", "../sib/decisions", None, "in a repo: by .., from its git root"),
    ("repo/src", "docs/decisions", "docs/decisions", None, "in a repo: plain relative, from its git root"),
    ("loose", "SIB/decisions", "SIB/decisions", None, "in no repo: absolute, into a repo (#220)"),
    ("loose", "../sib/decisions", "SIB/decisions", None, "in no repo: by .., from its own directory, kept absolute"),
    (".", "sib/decisions", "SIB/decisions", None, "in no repo: plain relative, into a repo under it, kept absolute"),
    ("loose", "sib/decisions", None, "is in no git repo", "in no repo: plain relative, into no repo"),
    ("loose", "SIB", None, "is the root of sib", "in no repo: a repo's root itself"),
    ("loose", "../elsewhere", None, "is in no git repo", "in no repo: by .., into no repo"),
    ("loose", "", None, "relative to here or absolute", "in no repo: an empty path"),
])
def test_checked_whether_or_not_the_session_is_in_a_repo(repo, cwd, path, kept, refused, desc):
    """#220: a session whose directory is in no git repo (a projects folder, say) may name
    decisions in one, absolute or relative to its own directory (by .., or a repo under it)."""
    tmp = repo.parent
    (tmp / "loose").mkdir()
    sib = str(tmp / "sib")
    path = path.replace("SIB", sib)
    if refused:
        with pytest.raises(ValueError, match=refused):
            docroots.checked([path], str(tmp / cwd))
            pytest.fail(desc)
    else:
        assert docroots.checked([path], str(tmp / cwd)) == [kept.replace("SIB", sib)], desc


def test_checked_keeps_the_resolved_path_for_a_session_in_no_repo_behind_a_link(repo):
    """#220: a cwd reached through a link, .. taken from where it really is: kept resolved, so
    what is kept is what was checked (a textual .. would name a directory that isn't there)."""
    tmp = repo.parent
    (tmp / "real" / "loose").mkdir(parents=True)
    (tmp / "real" / "sib" / ".git").mkdir(parents=True)
    (tmp / "real" / "sib" / "d").mkdir()
    (tmp / "home").mkdir()
    (tmp / "home" / "loose").symlink_to(tmp / "real" / "loose")
    assert docroots.checked(["../sib/d"], str(tmp / "home" / "loose")) == [str((tmp / "real").resolve() / "sib/d")]
    (tmp / "real" / "loose" / "l").symlink_to(tmp / "real" / "sib" / "d")
    with pytest.raises(ValueError, match="is in no git repo"):   # kept as loose/d, so checked as that
        docroots.checked(["l/../d"], str(tmp / "home" / "loose"))


def test_decision_dirs_from_a_session_in_no_repo(repo):
    """#220: a session in no git repo lists the directories it declared (absolute, as checked
    keeps them), under the repo each is in; a declared one it shares with a repo's session
    lists once, under the root that sorts first."""
    loose = repo.parent / "loose"
    loose.mkdir()
    sib = (repo.parent / "sib" / "decisions").resolve()
    alone = [{"cwd": str(loose), "doc_dirs": [str(sib), "stale/relative", str(repo.parent / "elsewhere")]}]
    assert docroots.decision_dirs(alone) == [(sib, sib.parent)], "its own repo's root, the rest left out"
    shared = alone + [{"cwd": str(repo / "src"), "doc_dirs": ["../sib/decisions"]}]
    assert docroots.decision_dirs(shared) == [(sib, repo.resolve())], "once, under repo (sorts before sib)"
