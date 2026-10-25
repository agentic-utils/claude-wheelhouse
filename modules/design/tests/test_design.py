import os
from pathlib import Path

import pytest

from claude_wheelhouse import api, served

design = pytest.importorskip("claude_wheelhouse_design", reason="installed without the design module")
from claude_wheelhouse_design import pane, scan  # noqa: E402


def repo(root: Path, files: dict[str, str], where="docs/adr") -> Path:
    """A git repo at root with files in `where`."""
    (root / ".git").mkdir(parents=True, exist_ok=True)
    d = root / where
    d.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (d / name).write_text(text)
    return d


@pytest.mark.parametrize("name, text, number, status, title, desc", [
    ("0001-use-sqlite.md", "---\nstatus: Accepted\n---\n# Use SQLite\n", "0001", "ACCEPTED", "Use SQLite",
     "front matter's status, the heading's title"),
    ("0002-x.md", "---\nStatus: proposed\ntitle: \"Drop Redis\"\n---\n# Something else\n", "0002", "PROPOSED",
     "Drop Redis", "front matter's title wins over the heading, its key in any case"),
    ("0042-ACCEPTED-split-the-hub.md", "# 42. Split the hub\n", "0042", "ACCEPTED", "Split the hub",
     "the name's status (tfu), a heading's leading number left out"),
    ("0003-UNRESOLVED-cache.md", "", "0003", "UNRESOLVED", "cache", "no heading: the name's slug"),
    ("0004-retry.md", "# ADR-0004: Retry\n\n## Status\n\nSuperseded by 0009\n", "0004", "SUPERSEDED", "Retry",
     "an adr-tools Status section, its first word"),
    ("0005-pin.md", "# Pin versions\n\n**Status:** Rejected\n", "0005", "REJECTED", "Pin versions",
     "a Status line"),
    ("0006-nothing-said.md", "# Nothing said\n\nJust prose.\n", "0006", "?", "Nothing said", "no status anywhere: ?"),
    ("0007-PROPOSED-x.md", "---\nstatus: Accepted\n---\n", "0007", "ACCEPTED", "x",
     "front matter outranks the name"),
    ("0008-open.md", "---\nstatus: Accepted\n# never closed\n", "0008", "ACCEPTED", "never closed",
     "front matter never closed is read as the doc's body"),
])
def test_parse(tmp_path, name, text, number, status, title, desc):
    d = scan.parse(tmp_path / name, text, "repo")
    assert (d.number, d.status, d.title, d.repo) == (number, status, title, "repo"), desc


@pytest.mark.parametrize("name, desc", [
    ("README.md", "no number"), ("042-short.md", "three digits"), ("0001-x.txt", "not markdown"),
    ("0001-x.md.bak", "a backup"), ("template.md", "a template"), ("0000-template.md", "a numbered template"),
])
def test_not_a_decision(tmp_path, name, desc):
    assert scan.parse(tmp_path / name, "# x\n", "repo") is None, desc


def sessions(*rows):
    return [{"cwd": str(cwd), "doc_dirs": dirs} for cwd, dirs in rows]


def listed(ctx_sessions) -> list[tuple]:
    ctx = api.Context(Path("/tmp"), lambda: ctx_sessions, lambda: None)
    got = served.Served([api.Loaded("design", design.module)], ctx).call("design", "decisions")
    return [(r["repo"], r["number"], r["status"]) for r in got["decisions"]]


@pytest.mark.parametrize("layout, declared, expect, desc", [
    ({"docs/adr": ["0001-a.md", "0002-PROPOSED-b.md"]}, None, [("app", "0002", "PROPOSED"), ("app", "0001", "?")],
     "docs/adr by default, PROPOSED first"),
    ({"docs/adr": ["0001-a.md"], "docs/decisions": ["0005-c.md"]}, ["docs/decisions"], [("app", "0005", "?")],
     "declared dirs replace docs/adr"),
    ({"docs/adr": ["0001-a.md"], ".plan": ["0009-p.md"]}, None, [("app", "0001", "?")],
     ".plan isn't scanned unless declared"),
    ({}, None, [], "no docs/adr: nothing, and no error"),
    ({"docs/adr": ["0001-a.md", "notes.md", "0002-b.txt", "0003-c.md/"]}, None, [("app", "0001", "?")],
     "only NNNN-*.md files, not other files or a directory so named"),
])
def test_route_lists_a_repos_decisions(tmp_path, layout, declared, expect, desc):
    root = tmp_path / "app"
    (root / ".git").mkdir(parents=True)
    for where, names in layout.items():
        for n in names:
            if n.endswith("/"):
                (root / where / n).mkdir(parents=True)
            else:
                (root / where).mkdir(parents=True, exist_ok=True)
                (root / where / n).write_text("# t\n")
    assert listed(sessions((root, declared))) == expect, desc


def test_route_lists_every_repo_and_none_outside_one(tmp_path):
    repo(tmp_path / "one", {"0001-a.md": "# A\n", "0003-ACCEPTED-c.md": ""})
    repo(tmp_path / "two", {"0002-UNRESOLVED-b.md": ""})
    (tmp_path / "loose" / "docs" / "adr").mkdir(parents=True)
    (tmp_path / "loose" / "docs" / "adr" / "0009-x.md").write_text("")
    got = listed(sessions((tmp_path / "one", None), (tmp_path / "two" / "docs", None), (tmp_path / "loose", None)))
    assert got == [("two", "0002", "UNRESOLVED"), ("one", "0003", "ACCEPTED"), ("one", "0001", "?")], \
        "UNRESOLVED first, the rest newest first, across repos; a session in no repo adds none"


def worktree(main: Path, path: Path, name: str) -> None:
    """A linked worktree of main at path, as git worktree add lays it out on disk."""
    gitdir = main / ".git" / "worktrees" / name
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n")
    path.mkdir(parents=True)
    (path / ".git").write_text(f"gitdir: {gitdir}\n")


def test_a_worktrees_decisions_are_its_main_repos_once(tmp_path):
    main = tmp_path / "app"
    repo(main, {"0001-ACCEPTED-a.md": "# A\n"})
    worktree(main, main / ".worktrees" / "feature-x", "feature-x")
    (main / ".worktrees" / "feature-x" / "docs" / "adr").mkdir(parents=True)
    (main / ".worktrees" / "feature-x" / "docs" / "adr" / "0001-ACCEPTED-a.md").write_text("# A\n")
    worktree(main, tmp_path / "elsewhere", "elsewhere")
    sub = tmp_path / "sub"   # a submodule: a .git file whose gitdir has no commondir
    (tmp_path / "modules-dir").mkdir()
    repo(sub, {"0002-b.md": "# B\n"})
    (sub / ".git").rmdir()
    (sub / ".git").write_text(f"gitdir: {tmp_path / 'modules-dir'}\n")
    got = listed(sessions((main, None), (main / ".worktrees" / "feature-x", None), (tmp_path / "elsewhere", None),
                          (sub, None)))
    assert got == [("sub", "0002", "?"), ("app", "0001", "ACCEPTED")], \
        "each worktree's sessions list the main repo's decisions, once, under its name; a submodule is its own"
    assert api.git_root(tmp_path / "elsewhere") == main.resolve(), "a worktree outside the repo too"


def test_a_file_is_read_again_only_when_it_changes(tmp_path, monkeypatch):
    d = repo(tmp_path, {"0001-a.md": "---\nstatus: proposed\n---\n"})
    scanner = scan.Scanner()
    reads = []
    real = scan.parse
    monkeypatch.setattr(scan, "parse", lambda *a: reads.append(a[0].name) or real(*a))
    assert [x.status for x in scanner.scan([(d, "r")])] == ["PROPOSED"]
    assert [x.status for x in scanner.scan([(d, "r")])] == ["PROPOSED"] and reads == ["0001-a.md"], \
        "unchanged: not read again"
    f = d / "0001-a.md"
    f.write_text("---\nstatus: accepted\n---\n")
    os.utime(f, ns=(f.stat().st_mtime_ns + 10**9,) * 2)
    assert [x.status for x in scanner.scan([(d, "r")])] == ["ACCEPTED"], "changed: read again"
    f.unlink()
    assert scanner.scan([(d, "r")]) == [] and not scanner.files, "gone: forgotten"


def test_a_link_out_of_the_directory_is_never_read(tmp_path):
    d = repo(tmp_path / "r", {"0001-ACCEPTED-a.md": "# A\n"})
    secret = tmp_path / "secret.md"
    secret.write_text("---\nstatus: proposed\ntitle: Private\n---\n")
    (d / "0002-out.md").symlink_to(secret)
    (d / "0003-in.md").symlink_to(d / "0001-ACCEPTED-a.md")
    got = scan.Scanner().scan([(d, "r")])
    assert [(x.number, x.title) for x in got] == [("0003", "A"), ("0001", "A")], \
        "a link outside its directory skipped, its title never listed; one inside it read"


def test_route_lists_a_sibling_docs_repos_decisions_under_the_declaring_repo(tmp_path):
    """A project split over sibling repos (#191): the code repo's session declares the docs
    repo's decisions; they list once, under the code repo, and read through /docs."""
    app, docs = tmp_path / "app", tmp_path / "app-docs" / "decisions"
    for d in (app / ".git", tmp_path / "app-docs" / ".git", docs):
        d.mkdir(parents=True)
    (docs / "0001-ACCEPTED-separate-docs-repo.md").write_text("# ADR-0001: Separate\n\n- **Status:** ACCEPTED\n")
    rows = sessions((app, ["../app-docs/decisions"]), (app, [str(docs)]))
    assert listed(rows) == [("app", "0001", "ACCEPTED")]
    s = served.Served([api.Loaded("design", design.module)], api.Context(Path("/tmp"), lambda: rows, lambda: None))
    assert "Separate" in s.read(str(docs / "0001-ACCEPTED-separate-docs-repo.md"))["text"]


def test_doc_roots_are_the_decision_dirs(tmp_path):
    d = repo(tmp_path, {"0001-a.md": "# A\n"})
    ctx = api.Context(Path("/tmp"), lambda: sessions((tmp_path, None)), lambda: None)
    s = served.Served([api.Loaded("design", design.module)], ctx)
    assert s.doc_roots() == [d.resolve()]
    assert "# A" in s.read(str(d / "0001-a.md"))["text"], "a decision reads through /docs"


@pytest.mark.parametrize("spec, column, after, desc", [
    ("status*", "status", "status▲", "status: * then ▲"),
    ("status▲", "status", "status▼", "then ▼"),
    ("status▼", "status", "status*", "then back"),
    ("status*", "number", "number▲", "another column: ▲"),
    ("number▲", "number", "number▼", "then ▼"),
    ("number▼", "number", "status*", "then back to the default"),
    ("title▲", "repo", "repo▲", "a third column starts afresh"),
    ("title▲", "status", "status*", "status from another column: its default"),
])
def test_next_sort(spec, column, after, desc):
    assert pane.next_sort(spec, column) == after, desc


ROWS = [{"number": "0002", "status": "PROPOSED", "title": "b", "repo": "z", "path": "/2"},
        {"number": "0003", "status": "ACCEPTED", "title": "A", "repo": "y", "path": "/3"},
        {"number": "0001", "status": "REJECTED", "title": "c", "repo": "x", "path": "/1"}]


@pytest.mark.parametrize("spec, order, desc", [
    ("status*", ["/2", "/3", "/1"], "the default: as the scan ordered them"),
    ("number▲", ["/1", "/2", "/3"], "number, oldest first"),
    ("number▼", ["/3", "/2", "/1"], "number, newest first"),
    ("title▲", ["/3", "/2", "/1"], "title, ignoring case"),
    ("repo▲", ["/1", "/3", "/2"], "repo"),
    ("status▲", ["/3", "/2", "/1"], "status by name"),
])
def test_sorted_by(spec, order, desc):
    assert [r["path"] for r in pane.sorted_by(ROWS, spec)] == order, desc
