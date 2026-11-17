"""GitLab merge requests as PR items (.plan/gitlab.md): their URLs read and kept canonical on any
host, posted or found in transcripts, shown as PRs are.
The GitHub paths are pinned beside them in tests/test_prs.py, which this leaves as it was."""

import json
from datetime import datetime, timezone

import pytest

from claude_wheelhouse import features, mcp_server, prs
from claude_wheelhouse.store import PRS_SINCE  # noqa: F401  (the cut-off a watcher test sets)
from claude_wheelhouse.tui import family_prompt, item_tip, open_mr, open_pr, pr_body, pr_text, titled

HOST = "gitlab.tufu.internal"
MR = f"https://{HOST}/autonomous-piloting/av/-/merge_requests/12"
NESTED = f"https://{HOST}/autonomous-piloting/sim/tools/-/merge_requests/7"
GITHUB = "https://github.com/agentic-utils/claude-wheelhouse/pull/108"
EPOCH = datetime.fromtimestamp(0, timezone.utc)
ANYWHERE = MR.replace(HOST, "git.elsewhere.example:8443")   # a host nobody configured


# the CA

@pytest.mark.parametrize("given, want, desc", [
    (None, None, "not given"),
    ("{ca}", "{ca}", "a file"),
    ("{dir}/missing.crt", None, "no such file: said, and glab's own configuration left to it"),
    ("  ", None, "blank"),
])
def test_gitlab_ca_cert(monkeypatch, tmp_path, capsys, given, want, desc):
    ca = tmp_path / "ca.crt"
    ca.write_text("pem")
    monkeypatch.setattr(features, "_warned", set())
    monkeypatch.setattr(features, "env_files", lambda: [])
    if given:
        monkeypatch.setenv("WHEELHOUSE_GITLAB_CA_CERT", given.format(ca=ca, dir=tmp_path))
    assert features.gitlab_ca_cert() == (want.format(ca=ca) if want else None), desc
    assert ("not a file" in capsys.readouterr().err) == (given is not None and "missing" in given), desc


# reading a URL

@pytest.mark.parametrize("text, expected, desc", [
    (MR, MR, "a canonical URL as it is"),
    (NESTED, NESTED, "a project in a subgroup"),
    (MR + "/diffs?commit_id=abc#note_5", MR, "its diffs tab cut back to the merge request"),
    (f"Creating merge request for doug/x into main in autonomous-piloting/av\n\n!12 Pins\n{MR}\n", MR, "glab's output"),
    (f'{{"web_url": "{MR}", "iid": 12}}', MR, "a GitLab MCP server's JSON"),
    (MR.replace(HOST, HOST.upper()), MR, "the host in lower case"),
    (ANYWHERE, ANYWHERE, "any host, its port kept"),
    (MR.replace("https", "http"), None, "no TLS, no merge request"),
    (f"https://{HOST}/autonomous-piloting/av/-/issues/12", None, "an issue is no merge request"),
    (f"https://{HOST}/av/-/merge_requests/12", None, "a project has a group"),
    (f"{GITHUB} then {MR}", GITHUB, "the first of either kind"),
    (f"{MR} then {GITHUB}", MR, "and the first, whichever kind"),
    ("", None, "nothing"),
])
def test_canonical(text, expected, desc):
    assert prs.canonical(text) == expected, desc


@pytest.mark.parametrize("url, parsed, mr, repo, forge, desc", [
    (MR, ("autonomous-piloting", "av", 12), (HOST, "autonomous-piloting/av", 12), "av", "GitLab", "a project"),
    (NESTED, ("autonomous-piloting/sim", "tools", 7), (HOST, "autonomous-piloting/sim/tools", 7), "tools", "GitLab",
     "a subgroup: the namespace keeps it"),
    (MR.replace(HOST, "elsewhere.example"), ("autonomous-piloting", "av", 12), ("elsewhere.example", "autonomous-piloting/av", 12),
     "av", "GitLab", "another host"),
    (GITHUB, ("agentic-utils", "claude-wheelhouse", 108), None, "claude-wheelhouse", "GitHub", "a pull request as before"),
    (None, None, None, "", "GitHub", "nothing"),
])
def test_reading_a_stored_url(url, parsed, mr, repo, forge, desc):
    assert (prs.parse(url), prs.parse_mr(url), prs.repo_of(url), prs.forge(url), prs.is_mr(url)) == \
        (parsed, mr, repo, forge, mr is not None), desc


@pytest.mark.parametrize("url, shown, desc", [
    (MR, f"{HOST}/autonomous-piloting/av/-/merge_requests/12", "a merge request's, without https://"),
    (GITHUB, "agentic-utils/claude-wheelhouse/pull/108", "a pull request's, without https://github.com/"),
])
def test_link_shows_the_url_and_opens_the_whole_of_it(url, shown, desc):
    text = prs.link(url)
    assert text.plain == shown and f"app.open_url({url!r})" in str(text.style.meta), desc


# the store

def test_post_pr_takes_a_merge_request(store, sid):
    assert store.post_pr(sid, MR, "Inbox pins") == "av#PR12"
    item = store.item(sid, "av#PR12")
    assert (item["kind"], item["status"], item["body"]) == ("pr", "open", MR)
    assert store.post_pr(sid, NESTED, "t") == "tools#PR7"
    assert store.post_pr(sid, MR, "renamed") == "av#PR12" and len(store.pr_items()) == 2, "the same URL, the same item"
    assert store.post_pr(sid, ANYWHERE, "t") == "av#PR12b", "a host nobody named: taken, a letter for the same name"


def test_a_merge_request_and_a_pull_request_of_one_repo_name_and_number_get_a_letter(store, sid):
    github = "https://github.com/agentic-utils/av/pull/12"
    assert [store.post_pr(sid, u, "t") for u in (GITHUB, MR, github)] == ["claude-wheelhouse#PR108", "av#PR12", "av#PR12b"]
    assert sorted(r["url"] for r in store.pr_items()) == sorted([GITHUB, MR, github])


@pytest.mark.parametrize("url, desc", [
    (MR + "/diffs", "not canonical"),
    (MR.replace("https", "http"), "no TLS"),
    ("https://github.com/a/b/issues/3", "an issue"),
])
def test_post_pr_refuses(store, sid, url, desc):
    with pytest.raises(ValueError) as refused:
        store.post_pr(sid, url, "t")
    assert str(refused.value) == ("url must be a GitHub pull request, https://github.com/<owner>/<repo>/pull/<n>, or a "
                                  "GitLab merge request, https://<host>/<group>/<project>/-/merge_requests/<n>, "
                                  f"not {url!r}"), desc


def test_a_merge_requests_status_is_gitlabs_to_set_through_the_pane(store, sid):
    store.post_pr(sid, MR, "t")
    assert store.set_pr_states({MR: "merged"}) == 1 and store.item(sid, "av#PR12")["status"] == "merged"
    assert store.pr_items()[0]["url"] == MR


def test_mcp_posts_a_merge_request(store, sid):
    mcp_server._store, mcp_server._sid = store, sid
    tool = next(t for t in mcp_server.TOOLS if t.__name__ == "post_item")
    assert tool(kind="pr", title="Pins", url=MR) == "av#PR12"
    assert "merge request" in tool.__doc__


# finding them in transcripts

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
    ([rec("assistant", 9, bash("a", 'glab mr create --title "Pins" --fill')), rec("user", 8, result("a", f"...\n{MR}\n"))],
     [("av#PR12", "Pins")], "glab mr create, its title from --title"),
    ([rec("assistant", 9, bash("a", "glab mr create -t 'Pins' -d x")), rec("user", 8, result("a", MR))],
     [("av#PR12", "Pins")], "-t"),
    ([rec("assistant", 9, bash("a", "glab mr create --fill")), rec("user", 8, result("a", MR))],
     [("av#PR12", "MR 12")], "no title given: MR <n>"),
    ([rec("assistant", 9, {"type": "tool_use", "id": "m", "name": "mcp__gitlab__create_merge_request",
                           "input": {"title": "Via MCP"}}),
      rec("user", 8, result("m", json.dumps({"web_url": MR})))],
     [("av#PR12", "Via MCP")], "the GitLab MCP server's tool"),
    ([rec("assistant", 9, bash("a", "glab mr create --fill")), rec("user", 8, result("a", ANYWHERE))],
     [("av#PR12", "MR 12")], "any host"),
    ([rec("assistant", 9, bash("a", "glab mr create --fill")), rec("user", 8, result("a", "error: no commits", True))],
     [], "an error with no URL makes nothing"),
    ([rec("assistant", 9, bash("a", "glab mr view 12")), rec("user", 8, result("a", MR))],
     [], "a URL from another command makes nothing"),
    ([rec("assistant", 9, bash("a", f"echo {ANYWHERE}")), rec("user", 8, result("a", ANYWHERE)),
      rec("assistant", 7, {"type": "text", "text": f"See {MR}"})],
     [], "a URL a session merely mentions makes nothing"),
    ([rec("assistant", 9, bash("a", "gh pr create -t Hub")), rec("user", 8, result("a", GITHUB))],
     [("claude-wheelhouse#PR108", "Hub")], "a pull request is found as before"),
])
def test_watcher_finds_merge_requests(store, sid, tmp_path, records, expected, desc):
    main = tmp_path / "main.jsonl"
    write(main, *records)
    prs.PrWatcher(sid).sync(store, main, {}, EPOCH)
    assert [(r["ref"], r["title"]) for r in store.items(sid) if r["kind"] == "pr"] == expected, desc


def test_a_subagents_merge_request_is_filed_under_its_task(store, sid, tmp_path):
    store.post_item(sid, "task", "the work")
    main, sub = tmp_path / "main.jsonl", tmp_path / "agent-x.jsonl"
    main.write_text("")
    write(sub, rec("assistant", 9, bash("a", "glab mr create -t Sub")), rec("user", 8, result("a", MR)))
    prs.PrWatcher(sid).sync(store, main, {sub: "T1"}, EPOCH)
    item = store.item(sid, "av#PR12")
    assert (item["title"], item["parent_ref"], item["body"]) == ("Sub", "T1", MR)


@pytest.mark.parametrize("command, acts, desc", [
    ("glab mr merge 12 --squash", True, "a merge"),
    ("glab mr close 12", True, "a close"),
    ("glab mr update 12 --ready", True, "ready"),
    ("glab mr approve 12", True, "an approval"),
    ("glab mr view 12", False, "a look"),
    ("glab mr list", False, "a list"),
])
def test_watcher_notes_glab_activity(store, sid, tmp_path, command, acts, desc):
    main = tmp_path / "main.jsonl"
    write(main, rec("assistant", 5, bash("a", command)))
    watcher = prs.PrWatcher(sid)
    watcher.sync(store, main, {}, EPOCH)
    assert (watcher.activity == 1_700_000_000 - 5) if acts else watcher.activity is None, desc


# the inbox's rows

@pytest.mark.parametrize("ref, title, tip, desc", [
    ("av#PR12", "Pins", "av#PR12  Pins", "a ref that names its repo: the title doesn't repeat it"),
    ("PR12", "av · Pins", "PR12  av · Pins", "an old ref (an older daemon's): the title carries the repo as before"),
])
def test_a_merge_requests_row(ref, title, tip, desc):
    item = {"kind": "pr", "ref": ref, "title": "Pins", "body": MR}
    assert (str(titled(item)), item_tip(item)) == (title, tip), desc


def test_the_detail_names_a_merge_request_by_its_bang():
    assert pr_body({"body": MR}) == f"Merge request **autonomous-piloting/av!12**: [{MR}]({MR})"
    assert pr_body({"body": NESTED}).startswith("Merge request **autonomous-piloting/sim/tools!7**")
    assert pr_body({"body": GITHUB}).startswith("Pull request **agentic-utils/claude-wheelhouse#108**")
    assert pr_text(pr_body({"body": MR})).plain == f"Pull request: {HOST}/autonomous-piloting/av/-/merge_requests/12"


@pytest.mark.parametrize("body, github, gitlab, desc", [
    (MR, False, True, "a merge request is GitLab's"),
    (GITHUB, True, False, "a pull request is GitHub's"),
])
def test_open_pr_and_open_mr_split_by_forge(body, github, gitlab, desc):
    item = {"kind": "pr", "status": "open", "body": body}
    assert (open_pr(item), open_mr(item)) == (github, gitlab), desc
    assert (open_pr({**item, "status": "merged"}), open_mr({**item, "status": "merged"})) == (False, False), desc


def test_deleting_a_family_says_where_each_stays_open():
    def pr(n, body):
        return {"kind": "pr", "status": "open", "ref": f"PR{n}", "title": "t", "dismissed": None, "body": body,
                "session_id": "s", "updated_at": "2026-01-01T00:00:00+00:00", "id": n}
    said = family_prompt([{"ref": "T1", "title": "work"}], [pr(1, MR), pr(2, GITHUB)])
    assert "1 still open on GitHub" in said and "1 still open on GitLab" in said
