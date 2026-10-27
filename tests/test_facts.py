"""The helper's fact sheet (facts.py): stamped for the source this commit holds, its generated
part the code's, and the stamp, the restamp and the pre-commit hook working as AGENTS.md says."""

import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_wheelhouse import api, facts

AT = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]


def in_git() -> bool:
    return shutil.which("git") is not None and (facts.ROOT / ".git").exists()


@pytest.mark.skipif(not in_git(), reason="not a git checkout: the stamp is of git's index")
def test_the_sheet_is_stamped_for_this_source():
    try:
        facts.check()
    except facts.Stale as e:
        pytest.fail(str(e))


def test_its_generated_part_is_the_codes(every_module):
    if len([m for m in api.load() if m.module]) < len(list((facts.ROOT / "modules").glob("*/pyproject.toml"))):
        pytest.skip("a module isn't installed: the sheet lists every module's keys")
    below = facts.SHEET.read_text().partition(facts.GENERATED)[2]
    assert below.strip() == facts.generated().strip(), "run make facts: the keys, settings or features changed"


@pytest.fixture
def repo(tmp_path):
    """A git repo laid out as this one, with facts.py, a stamped sheet, committed."""
    for rel in ("claude_wheelhouse/__init__.py", "claude_wheelhouse/facts.py"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(facts.ROOT / rel, tmp_path / rel)
    for rel in ("README.md", "claude_wheelhouse/tui.py", "claude_wheelhouse/protocol.md", "AGENTS.md",
                "modules/prs/claude_wheelhouse_prs/pane.py", "modules/prs/tests/test_pane.py", "tests/test_tui.py",
                ".plan/helper-chat.md"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("one\n")
    (tmp_path / facts.SHEET.relative_to(facts.ROOT)).write_text(f"{facts.TITLE}\n\nThe guide.\n")
    run = lambda *a: subprocess.run([*GIT, *a], cwd=tmp_path, check=True, capture_output=True)   # noqa: E731
    run("init", "-q")
    run("add", "-A")
    facts.make(tmp_path, now=AT, regenerate=False)
    run("commit", "-qm", "first")
    return tmp_path


@pytest.mark.parametrize("path, counts, desc", [
    ("claude_wheelhouse/tui.py", True, "the package's code"),
    ("claude_wheelhouse/facts.py", True, "the package's code, the sheet's own maker too"),
    ("claude_wheelhouse/protocol.md", True, "what sessions are told"),
    ("README.md", True, "the README"),
    ("modules/prs/claude_wheelhouse_prs/pane.py", True, "a module's code"),
    ("modules/prs/tests/test_pane.py", False, "a module's tests"),
    ("tests/test_tui.py", False, "the tests"),
    (".plan/helper-chat.md", False, "the design notes"),
    ("AGENTS.md", False, "the agents' guide"),
    ("claude_wheelhouse/helper_facts.md", False, "the sheet itself"),
])
def test_what_the_stamp_covers(repo, path, counts, desc):
    before = facts.source_hash(repo)
    (repo / path).write_text("two\n")
    assert facts.source_hash(repo) == before, f"{desc}: a change not yet added isn't in the next commit"
    subprocess.run(["git", "add", path], cwd=repo, check=True)
    assert (facts.source_hash(repo) != before) == counts, desc


def test_the_stamp_holds_a_version_a_utc_time_and_the_hash(repo):
    first = (repo / "claude_wheelhouse/helper_facts.md").read_text().partition("\n")[0]
    assert first.startswith("<!-- fact sheet stamp: version 1, 2026-10-27T12:00:00Z, source ")
    facts.make(repo, now=AT, regenerate=False)
    again = (repo / "claude_wheelhouse/helper_facts.md").read_text()
    assert facts.read_stamp(again) == (2, facts.source_hash(repo)), "restamped with nothing changed: a new version"
    assert again.count(facts.TITLE) == 1 and "The guide." in again, "the guide kept"


@pytest.mark.parametrize("text, version, desc", [
    (f"{facts.TITLE}\n\nguide\n", 1, "never stamped"),
    (f"{facts.stamp_line(4, AT, 'aaa')}\n{facts.TITLE}\n\nguide\n", 5, "stamped once"),
    (f"<<<<<<< HEAD\n{facts.stamp_line(3, AT, 'aaa')}\n=======\n{facts.stamp_line(5, AT, 'bbb')}\n>>>>>>> b\n"
     f"{facts.TITLE}\n\nguide\n", 6, "two branches' stamps in conflict: past both"),
])
def test_restamp(text, version, desc):
    out = facts.restamp(text, "ccc", AT, generated="the keys")
    assert facts.read_stamp(out) == (version, "ccc"), desc
    assert "<<<<<<<" not in out and out.endswith(f"{facts.GENERATED}\n\nthe keys\n"), desc


def test_a_conflict_in_the_guide_needs_a_person():
    with pytest.raises(ValueError, match="conflict"):
        facts.restamp(f"{facts.TITLE}\n<<<<<<< HEAD\na\n=======\nb\n>>>>>>> x\n", "ccc", AT)


def test_the_hook_refuses_a_commit_with_a_stale_stamp(repo):
    hook = facts.ROOT / ".githooks/pre-commit"
    assert hook.stat().st_mode & 0o111, "executable"
    shutil.copytree(hook.parent, repo / ".githooks")
    subprocess.run(["git", "config", "core.hooksPath", ".githooks"], cwd=repo, check=True)
    (repo / "claude_wheelhouse/tui.py").write_text("two\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    refused = subprocess.run([*GIT, "commit", "-qm", "stale"], cwd=repo, capture_output=True, text=True)
    assert refused.returncode != 0 and "make facts" in refused.stderr, "a source change with no restamp"
    said = facts.make(repo, now=AT, regenerate=False)
    assert "not added" not in said
    subprocess.run([*GIT, "commit", "-qm", "stamped"], cwd=repo, check=True, capture_output=True)
    (repo / "AGENTS.md").write_text("three\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    refused = subprocess.run([*GIT, "commit", "-qm", "no source"], cwd=repo, capture_output=True, text=True)
    assert refused.returncode != 0 and "doesn't" in refused.stderr, "a commit with no source change restamps too"
    facts.make(repo, now=AT, regenerate=False)
    subprocess.run([*GIT, "commit", "-qm", "restamped"], cwd=repo, check=True, capture_output=True)


def test_make_says_what_the_stamp_leaves_out(repo):
    (repo / "README.md").write_text("two\n")
    assert "not added, so not in the stamp: README.md" in facts.make(repo, now=AT, regenerate=False)


def test_the_hook_needs_no_venv():
    banned = [line for line in Path(facts.__file__).read_text().splitlines()
              if line.startswith(("import ", "from ")) and line.split()[1].split(".")[0] not in sys.stdlib_module_names]
    assert banned == [], "the hook runs python3 with no venv: standard library only at the top"
