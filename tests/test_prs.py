"""PR items (#111, .plan/pr-pane.md part 2): posted or found in transcripts, their ref the PR's
number, their status GitHub's; and the inbox's colour by kind and hover text (part 1)."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from claude_wheelhouse import mcp_server, prs
from claude_wheelhouse.hub import Hub
from claude_wheelhouse.store import PRS_SINCE, Store, inbox_rank, standing
from claude_wheelhouse.tui import closable, family_prompt, item_tip, ref_text, titled

URL = "https://github.com/agentic-utils/claude-wheelhouse/pull/108"
OTHER = "https://github.com/two-inc/checkout-api/pull/108"
EPOCH = datetime.fromtimestamp(0, timezone.utc)


# reading a URL

@pytest.mark.parametrize("text, expected, desc", [
    (URL, URL, "a canonical URL as it is"),
    (URL + "/files#diff", URL, "its files tab cut back to the PR"),
    (f"Creating pull request...\n{URL}\n", URL, "gh's output, the URL last"),
    (f"a pull request for branch \"x\" into branch \"main\" already exists:\n{URL}", URL, "gh's already exists"),
    ("https://github.com/a/b/issues/3", None, "an issue is no PR"),
    ("", None, "nothing"),
])
def test_canonical(text, expected, desc):
    assert prs.canonical(text) == expected, desc


@pytest.mark.parametrize("command, expected, desc", [
    ('gh pr create --title "Inbox pins" --body x', "Inbox pins", "--title"),
    ("gh pr create -t 'Inbox pins' -b x", "Inbox pins", "-t"),
    ("gh pr create --title=Pins --fill", "Pins", "--title="),
    ("gh pr create --fill", "", "none given"),
    ('gh pr create --title "unclosed', "", "a command that doesn't parse"),
])
def test_title_in(command, expected, desc):
    assert prs.title_in(command) == expected, desc


# the store

def test_post_pr_numbers_its_ref_by_the_pr(store, sid):
    assert store.post_pr(sid, URL, "Inbox pins") == "PR108"
    item = store.item(sid, "PR108")
    assert (item["kind"], item["status"], item["body"], item["pinned"]) == ("pr", "open", URL, 0)


def test_same_number_in_another_repo_takes_a_suffix(store, sid):
    refs = [store.post_pr(sid, u, "t") for u in (URL, OTHER, "https://github.com/x/y/pull/108")]
    assert refs == ["PR108", "PR108b", "PR108c"]


def test_same_url_again_is_the_same_item(store, sid):
    store.post_item(sid, "task", "the work")
    store.post_pr(sid, URL, "first")
    assert store.post_pr(sid, URL, "second", parent="T1") == "PR108"
    item = store.item(sid, "PR108")
    assert (item["title"], item["parent_ref"]) == ("second", "T1")
    assert [r["ref"] for r in store.items(sid) if r["kind"] == "pr"] == ["PR108"]


@pytest.mark.parametrize("url, parent, error, desc", [
    ("https://github.com/a/b/issues/3", None, "url must be a GitHub pull request", "an issue"),
    (URL + "/files", None, "url must be a GitHub pull request", "not canonical"),
    (URL, "T9", "parent T9 is not a task", "a parent that isn't there"),
])
def test_post_pr_refuses(store, sid, url, parent, error, desc):
    with pytest.raises(ValueError, match=error):
        store.post_pr(sid, url, "t", parent=parent)


def test_detected_pr_already_an_item_anywhere_is_passed_over(store, sid, tmp_path):
    other = store.create_session(str(tmp_path), name="other")
    store.post_pr(other, URL, "posted by hand")
    assert store.post_pr(sid, URL, "PR 108", detected=True) is None
    assert store.post_pr(sid, URL, "posted here too") == "PR108"   # posting explicitly makes its own


def test_status_is_githubs(store, sid):
    store.post_pr(sid, URL, "t")
    with pytest.raises(ValueError, match="its status comes from GitHub"):
        store.update_item(sid, "PR108", status="merged", declares=True)
    with pytest.raises(ValueError, match="posted with its url"):
        store.post_item(sid, "pr", "t")
    store.update_item(sid, "PR108", title="renamed", declares=True)
    assert store.item(sid, "PR108")["title"] == "renamed"


@pytest.mark.parametrize("dismissed, status, expected, desc", [
    (False, "merged", "settled", "merged settles, dimmed"),
    (False, "closed", "settled", "closed settles too"),
    (False, "draft", "active", "a draft is active"),
    (True, "merged", "finished", "one the person dismissed stays dismissed once merged"),
])
def test_set_pr_states(store, sid, tmp_path, dismissed, status, expected, desc):
    other = store.create_session(str(tmp_path), name="other")
    store.post_pr(sid, URL, "t")
    store.post_pr(other, URL, "t")
    if dismissed:
        store.dismiss(sid, "PR108")
    assert store.set_pr_states({URL: status}) == 2, desc
    assert store.set_pr_states({URL: status}) == 0, "only a change is written"
    item = store.item(sid, "PR108")
    assert (item["status"], standing(item)) == (status, expected), desc
    assert store.item(other, "PR108")["status"] == status, "every session's item of it"


def test_pr_items(store, sid):
    store.post_pr(sid, URL, "t")
    assert store.pr_items() == [{"url": URL, "session_id": sid, "ref": "PR108", "status": "open", "title": "t"}]


def test_open_pr_ranks_as_a_running_task(store, sid):
    store.post_item(sid, "task", "t", status="running")
    store.post_pr(sid, URL, "t")
    task, pr = store.item(sid, "T1"), store.item(sid, "PR108")
    assert inbox_rank(task)[0] == inbox_rank(pr)[0]


def test_prs_since_is_written_once(db_file):
    first = Store(db_file).setting(PRS_SINCE)
    assert first and Store(db_file).setting(PRS_SINCE) == first


# the MCP fallback

def call(name, **kw):
    """An MCP tool as written (mcp_server.TOOLS), its refusals raised as ValueError."""
    return next(t for t in mcp_server.TOOLS if t.__name__ == name)(**kw)


@pytest.mark.parametrize("kw, error, desc", [
    ({"url": "https://example.com/x"}, "url must be a GitHub pull request", "not GitHub"),
    ({"url": URL, "status": "merged"}, "its status comes from GitHub", "a status"),
    ({"url": URL, "body": "x"}, "a pr takes title, url, parent and pinned only", "a body"),
])
def test_mcp_post_pr_refuses(store, sid, kw, error, desc):
    mcp_server._store, mcp_server._sid = store, sid
    with pytest.raises(ValueError, match=error):
        call("post_item", kind="pr", title="t", **kw)


def test_mcp_post_pr(store, sid):
    mcp_server._store, mcp_server._sid = store, sid
    assert call("post_item", kind="pr", title="Inbox pins", url=URL, pinned=True) == "PR108"
    assert store.item(sid, "PR108")["pinned"] == 1
    with pytest.raises(ValueError, match="only a pr takes this"):
        call("post_item", kind="task", title="t", url=URL)


# finding PRs in transcripts

def rec(kind, ago, *blocks):
    return {"type": kind, "timestamp": datetime.fromtimestamp(1_700_000_000 - ago, timezone.utc).isoformat(),
            "message": {"role": kind, "content": list(blocks)}}


def bash(tid, command):
    return {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": command}}


def result(tid, text, error=False):
    return {"type": "tool_result", "tool_use_id": tid, "content": text, "is_error": error}


def write(path, *recs):
    with open(path, "a") as f:
        f.writelines(json.dumps(r) + "\n" for r in recs)


@pytest.mark.parametrize("records, expected, desc", [
    ([rec("assistant", 9, bash("a", 'gh pr create --title "Pins"')), rec("user", 8, result("a", f"...\n{URL}\n"))],
     [("PR108", "Pins")], "gh pr create, its title from --title"),
    ([rec("assistant", 9, bash("a", "gh pr create --fill")), rec("user", 8, result("a", URL))],
     [("PR108", "PR 108")], "no title given: PR <n> until the pane's refresh"),
    ([rec("assistant", 9, {"type": "tool_use", "id": "m", "name": "mcp__github__create_pull_request",
                           "input": {"title": "Via MCP"}}),
      rec("user", 8, result("m", json.dumps({"html_url": URL})))],
     [("PR108", "Via MCP")], "the GitHub MCP server's tool"),
    ([rec("assistant", 9, bash("a", "gh pr create -t x")), rec("user", 8, result("a", "error: no commits", True))],
     [], "an error with no URL makes nothing"),
    ([rec("assistant", 9, bash("a", "gh pr view 108")), rec("user", 8, result("a", URL))],
     [], "a URL from another command makes nothing"),
])
def test_watcher_finds_prs(store, sid, tmp_path, records, expected, desc):
    main = tmp_path / "main.jsonl"
    write(main, *records)
    prs.PrWatcher(sid).sync(store, main, {}, EPOCH)
    assert [(r["ref"], r["title"]) for r in store.items(sid) if r["kind"] == "pr"] == expected, desc


def test_watcher_reads_incrementally_and_files_a_subagents_pr_under_its_task(store, sid, tmp_path):
    store.post_item(sid, "task", "the work")
    main, sub = tmp_path / "main.jsonl", tmp_path / "agent-x.jsonl"
    main.write_text("")
    write(sub, rec("assistant", 9, bash("a", "gh pr create -t Sub")))
    watcher = prs.PrWatcher(sid)
    watcher.sync(store, main, {sub: "T1"}, EPOCH)
    assert store.item(sid, "PR108") is None, "the call alone makes nothing"
    write(sub, rec("user", 8, result("a", URL)))
    watcher.sync(store, main, {sub: "T1"}, EPOCH)
    item = store.item(sid, "PR108")
    assert (item["title"], item["parent_ref"]) == ("Sub", "T1")


@pytest.mark.parametrize("command, acts, desc", [
    ("git push -u origin HEAD", True, "a push"),
    ("gh pr merge 108 --squash", True, "a merge"),
    ("gh pr ready 108", True, "ready"),
    ("git status", False, "anything else"),
])
def test_watcher_notes_activity(store, sid, tmp_path, command, acts, desc):
    main = tmp_path / "main.jsonl"
    write(main, rec("assistant", 5, bash("a", command)))
    watcher = prs.PrWatcher(sid)
    watcher.sync(store, main, {}, EPOCH)
    assert (watcher.activity == 1_700_000_000 - 5) if acts else watcher.activity is None, desc


def test_hub_sync_prs_takes_the_later_cutoff_and_activity(store, sid, tmp_path):
    main = tmp_path / "main.jsonl"
    write(main, rec("assistant", 9, bash("a", "gh pr create -t x && git push")), rec("user", 8, result("a", URL)))
    hub = SimpleNamespace(store=store, pr_watchers={}, pr_activity_at=None)
    watcher = SimpleNamespace(sid=sid, main=main, cutoff=EPOCH, agents={})
    store.set_setting(PRS_SINCE, "2000-01-01T00:00:00+00:00")
    Hub.sync_prs(hub, watcher)
    assert store.item(sid, "PR108") is not None
    assert hub.pr_activity_at == 1_700_000_000 - 9


# the inbox's rows

@pytest.mark.parametrize("kind, child, dim, style, desc", [
    ("task", False, False, "green3", "a task green"),
    ("pr", False, False, "steel_blue1", "a PR light blue"),
    ("question", False, True, "dim gold3", "settled: dimmed, keeping its colour"),
    ("agent", True, False, "dim grey58", "a child under its parent"),
    ("doc", False, False, "", "a doc the theme's foreground"),
])
def test_ref_colour(kind, child, dim, style, desc):
    cell = ref_text({"kind": kind, "ref": "X1"}, child=child, dim=dim)
    assert str(cell.style) == style, desc


def test_unknown_kind_draws_as_before():
    assert ref_text({"kind": "future", "ref": "Z1"}) == "Z1"


def test_pr_title_and_tip_carry_its_repo():
    item = {"kind": "pr", "ref": "PR108", "title": "Inbox pins", "body": URL}
    assert titled(item).plain == "claude-wheelhouse · Inbox pins"
    assert item_tip(item) == "PR108  claude-wheelhouse · Inbox pins"
    assert titled({"kind": "task", "title": "t", "body": URL}) == "t"


def test_delete_on_prs():
    pr = {"kind": "pr", "status": "open", "ref": "PR108", "title": "t", "dismissed": None,
          "session_id": "s", "updated_at": "2026-01-01T00:00:00+00:00", "id": 1}
    assert closable(pr), "a PR is deleted whatever its state"
    parent = {"ref": "T1", "title": "work"}
    assert "1 still open on GitHub" in family_prompt([parent], [pr])


def test_a_subagents_pr_from_before_the_cutoff_is_not_found(store, sid, tmp_path):
    """A subagent transcript is read from its start: its records before the cut-off
    (store.PRS_SINCE) make nothing, so an upgrade never back-fills old PRs (#111 review 1)."""
    main, sub = tmp_path / "main.jsonl", tmp_path / "agent-old.jsonl"
    main.write_text("")
    write(sub, rec("assistant", 9, bash("a", "gh pr create -t Old")), rec("user", 8, result("a", URL)),
          rec("assistant", 1, bash("b", "gh pr create -t New")), rec("user", 0, result("b", OTHER)))
    cutoff = datetime.fromtimestamp(1_700_000_000 - 5, timezone.utc)
    prs.PrWatcher(sid).sync(store, main, {sub: None}, cutoff)
    assert [(r["ref"], r["title"]) for r in store.items(sid) if r["kind"] == "pr"] == [("PR108", "New")]
    assert store.item(sid, "PR108")["body"] == OTHER, "only the one made after it"
