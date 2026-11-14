"""GitLab merge requests in the PRs module (.plan/gitlab.md): `glab api`'s answers read into the
rows GitHub's give, their status and light shared; what goes wrong said as a warning; the
detail; and glab's own process. glab is never run: a runner stands in for it."""

import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from claude_wheelhouse import api, features

prs = pytest.importorskip("claude_wheelhouse_prs", reason="installed without the prs module")
from claude_wheelhouse_prs import fetch, pane  # noqa: E402

HOST = "gitlab.tufu.internal"
URL = f"https://{HOST}/autonomous-piloting/av/-/merge_requests/{{}}"
ENDPOINT = "projects/autonomous-piloting%2Fav/merge_requests/{}"
GITHUB = "https://github.com/two-inc/wheelhouse/pull/{}"
TOKEN = "glpat-" + "abcdEFGH1234567890xy"   # split, or secret scanners take the fake for a real one


@pytest.fixture(autouse=True)
def hosts(monkeypatch):
    monkeypatch.setenv("WHEELHOUSE_GITLAB_HOSTS", HOST)
    monkeypatch.setattr(features, "env_files", lambda: [])


def mr(n=12, **kw):
    """GitLab's answer for one merge request, as `glab api projects/<path>/merge_requests/<n>` prints it."""
    base = {"iid": n, "title": f"MR number {n}", "state": "opened", "draft": False, "updated_at": "2026-11-12T10:00:00.000Z",
            "source_branch": f"doug/{n}", "has_conflicts": False, "detailed_merge_status": "mergeable",
            "head_pipeline": {"id": 77, "status": "success", "web_url": f"https://{HOST}/autonomous-piloting/av/-/pipelines/77"},
            "references": {"full": f"autonomous-piloting/av!{n}"}}
    return {**base, **kw}


def as_row(**kw):
    return fetch.mr_row(URL.format(12), mr(**kw))


@pytest.mark.parametrize("kw, row, desc", [
    ({}, {"state": "OPEN", "draft": False, "checks": "SUCCESS", "mergeable": "MERGEABLE", "review": None}, "open and ready"),
    ({"state": "merged"}, {"state": "MERGED"}, "merged"),
    ({"state": "closed"}, {"state": "CLOSED"}, "closed"),
    ({"state": "locked"}, {"state": "OPEN"}, "locked, being merged: still open"),
    ({"draft": True, "title": "Draft: Pins"}, {"draft": True, "title": "Pins"}, "a draft, its prefix cut from the title"),
    ({"draft": True, "title": "[Draft] Pins"}, {"title": "Pins"}, "the bracketed prefix"),
    ({"draft": False, "title": "Draft: not a draft"}, {"title": "Draft: not a draft"}, "a prefix GitLab doesn't call a draft stays"),
    ({"draft": None, "work_in_progress": True}, {"draft": True}, "an older GitLab's work_in_progress"),
    ({"head_pipeline": {"status": "failed"}}, {"checks": "FAILURE"}, "a pipeline failed"),
    ({"head_pipeline": {"status": "canceled"}}, {"checks": "FAILURE"}, "a pipeline cancelled blocks the merge too"),
    ({"head_pipeline": {"status": "running"}}, {"checks": "PENDING"}, "running"),
    ({"head_pipeline": {"status": "waiting_for_resource"}}, {"checks": "PENDING"}, "waiting for a runner"),
    ({"head_pipeline": {"status": "manual"}}, {"checks": "SUCCESS"}, "waiting on a manual job: not running"),
    ({"head_pipeline": None}, {"checks": None}, "no pipeline: no checks"),
    ({"has_conflicts": True, "detailed_merge_status": "conflict"}, {"mergeable": "CONFLICTING"}, "a conflict"),
    ({"has_conflicts": True}, {"mergeable": "CONFLICTING"}, "has_conflicts alone"),
    ({"detailed_merge_status": "need_rebase"}, {"mergeable": "UNKNOWN"}, "needs a rebase: not ready"),
    ({"detailed_merge_status": "checking"}, {"mergeable": "UNKNOWN"}, "still checking"),
    ({"detailed_merge_status": "not_approved"}, {"review": "REVIEW_REQUIRED", "mergeable": "UNKNOWN"}, "approval required"),
    ({"detailed_merge_status": "requested_changes"}, {"review": "CHANGES_REQUESTED"}, "changes requested"),
    ({"detailed_merge_status": None, "merge_status": "can_be_merged"}, {"mergeable": "MERGEABLE"}, "an older GitLab's merge_status"),
    ({"detailed_merge_status": None, "merge_status": "cannot_be_merged"}, {"mergeable": "CONFLICTING"}, "and its conflict"),
    ({"detailed_merge_status": None, "merge_status": "checking"}, {"mergeable": "UNKNOWN"}, "and its unknown"),
    ({"updated_at": "2026-11-13T09:00:00Z"}, {"updated": "2026-11-13T09:00:00Z", "branch": "doug/12", "number": 12}, "the rest"),
    ({"references": {"full": "autonomous-piloting/sim/tools!12"}}, {"repo": "autonomous-piloting/sim/tools"}, "the project's full path"),
    ({"references": None}, {"repo": "autonomous-piloting/av"}, "no references: the URL's"),
])
def test_a_merge_request_as_a_row(kw, row, desc):
    got = as_row(**kw)
    assert {k: got[k] for k in row} == row and got["url"] == URL.format(12), desc


@pytest.mark.parametrize("kw, status, light, desc", [
    ({}, "open", "green", "ready to merge"),
    ({"state": "merged"}, "merged", "green", "merged"),
    ({"state": "closed"}, "closed", "green", "closed"),
    ({"draft": True}, "draft", "grey", "a draft"),
    ({"head_pipeline": {"status": "failed"}}, "open", "red", "a failed pipeline"),
    ({"has_conflicts": True}, "open", "red", "a conflict"),
    ({"detailed_merge_status": "requested_changes"}, "open", "red", "changes requested"),
    ({"head_pipeline": {"status": "running"}, "detailed_merge_status": "ci_still_running"}, "open", "orange", "pipeline running"),
    ({"detailed_merge_status": "not_approved"}, "open", "yellow", "approval required"),
    ({"detailed_merge_status": "discussions_not_resolved"}, "open", "yellow", "threads unresolved: not ready"),
    ({"detailed_merge_status": "need_rebase"}, "open", "yellow", "needs a rebase"),
    ({"head_pipeline": {"status": "failed"}, "draft": True}, "draft", "red", "red wins over a draft, as on GitHub"),
])
def test_a_merge_requests_status_and_light_are_the_shared_ones(kw, status, light, desc):
    row = as_row(**kw)
    assert (fetch.status(row), fetch.light(row)) == (status, light) and fetch.status(row) in api.PR_STATES, desc


@pytest.mark.parametrize("data, desc", [(None, "nothing"), ([], "a list"), ({}, "no iid"), ({"message": "404 Not found"}, "an error body")])
def test_no_merge_request_no_row(data, desc):
    assert fetch.mr_row(URL.format(12), data) is None, desc


# what going wrong says

@pytest.mark.parametrize("code, out, err, kind, says, desc", [
    (1, "", "glab: 401 Unauthorized (HTTP 401)", fetch.Unavailable, f"glab auth login --hostname {HOST}", "a bad or no token"),
    (1, "", "ERROR: no token found for host", fetch.Unavailable, "isn't logged in", "no login"),
    (1, "", "Get \"https://gitlab.tufu.internal/api/v4/x\": tls: failed to verify certificate: x509: certificate signed by unknown authority",
     RuntimeError, "GITLAB_CA_CERT", "a CA glab doesn't know: told how to give it one"),
    (1, '{"message":"404 Project Not Found"}', "glab: 404 Not Found (HTTP 404)", RuntimeError, "404 Not Found", "not found: glab's last line"),
    (1, "", "", RuntimeError, "glab exited 1", "silence"),
    (1, "", f"bad token {TOKEN} sent", RuntimeError, "[token]", "a token in the output never reaches the warning"),
])
def test_mr_failures(code, out, err, kind, says, desc):
    got = fetch.mr_failure(HOST, code, out, err)
    assert type(got) is kind and says in str(got) and str(got).startswith(HOST) and TOKEN not in str(got), desc


@pytest.mark.parametrize("env, text, want, desc", [
    ({}, f"a {TOKEN} b", "a [token] b", "a token's shape"),
    ({}, "glcbt-0123456789abcdef", "[token]", "another of GitLab's prefixes"),
    ({"GITLAB_TOKEN": "hunter2hunter2"}, "x hunter2hunter2 y", "x [token] y", "the value of the token variable"),
    ({"GITLAB_TOKEN": "short"}, "a short one", "a short one", "a value too short to be one is left alone"),
    ({}, "no secrets here: glab: 404 Not Found", "no secrets here: glab: 404 Not Found", "ordinary text"),
])
def test_scrub(monkeypatch, env, text, want, desc):
    for var in fetch.SECRETS:
        monkeypatch.delenv(var, raising=False)
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    assert fetch.scrub(text) == want, desc


# the fetcher

class Fake:
    """A Context's daemon side, a gh that answers what it's given and a glab that answers by endpoint."""

    def __init__(self, tmp_path, items=(), gh=None, merges=None, notes=None):
        self.items, self.states, self.gh_calls, self.glab_calls, self.activity = list(items), {}, [], [], None
        self.gh, self.merges, self.notes, self.now = gh or {"data": {"search": {"nodes": []}}}, merges or {}, notes, 1000.0
        self.ctx = api.Context(tmp_path, lambda: [], lambda: None, pr_items=lambda: self.items,
                               set_pr_states=self.states.update, pr_activity=lambda: self.activity)

    def run(self, argv):
        self.gh_calls.append(argv)
        if isinstance(self.gh, Exception):
            raise self.gh
        return 0, json.dumps(self.gh), ""

    def glab(self, argv):
        self.glab_calls.append(argv)
        assert argv[:4] == ["glab", "api", "--hostname", HOST], "glab asked for the host's API, nothing else"
        endpoint = argv[4]
        if endpoint.endswith("/notes") or "/notes?" in endpoint:
            return 0, json.dumps(self.notes if self.notes is not None else []), ""
        n = int(endpoint.rsplit("/", 1)[1])
        answer = self.merges.get(n)
        if isinstance(answer, tuple):
            return answer
        if isinstance(answer, Exception):
            raise answer
        return 0, json.dumps(answer if answer is not None else mr(n)), ""

    def fetcher(self):
        return fetch.Fetcher(self.ctx, self.run, lambda: self.now, self.glab)


def item(n, status="open", url=URL):
    return {"url": url.format(n), "session_id": "s1", "ref": f"PR{n}", "status": status, "title": "x"}


def test_merge_requests_are_fetched_one_call_each_and_set_their_items_states(tmp_path):
    f = Fake(tmp_path, [item(1), item(2), item(3, status="merged")],
             merges={1: mr(1, head_pipeline={"status": "failed"}), 2: mr(2, state="merged")})
    got = f.fetcher().answer("mine", False)
    assert [c[4] for c in f.glab_calls] == [ENDPOINT.format(1), ENDPOINT.format(2)], "one call each, none for the merged item"
    assert f.states == {URL.format(1): "open", URL.format(2): "merged"}, "their states written back, as GitHub's are"
    assert [(r["number"], r["light"], r["item"]) for r in got["prs"]] == [(1, "red", {"session_id": "s1", "ref": "PR1"})]
    assert got.get("error") is None and not got.get("warning"), "nothing wrong"
    assert len(f.gh_calls) == 1, "GitHub's one call is made as ever (the All scope's search)"


def test_github_and_gitlab_share_one_table(tmp_path):
    f = Fake(tmp_path, [item(1, url=GITHUB), item(2)], gh={"data": {
        "p0": {"pullRequest": {"number": 1, "title": "GH", "url": GITHUB.format(1), "isDraft": False, "state": "OPEN",
                               "updatedAt": "2026-11-12T08:00:00Z", "headRefName": "b", "reviewDecision": None,
                               "mergeable": "MERGEABLE", "repository": {"nameWithOwner": "two-inc/wheelhouse"},
                               "commits": {"nodes": []}}}, "search": {"nodes": []}}})
    got = f.fetcher().answer("mine", False)
    assert sorted(r["url"] for r in got["prs"]) == sorted([GITHUB.format(1), URL.format(2)])
    assert "pullRequest(number: 2)" not in f.gh_calls[0][-1], "a merge request is no GraphQL alias"
    assert f.states == {GITHUB.format(1): "open", URL.format(2): "open"}


def test_no_merge_requests_no_glab(tmp_path):
    f = Fake(tmp_path, [item(1, url=GITHUB)])
    f.fetcher().answer("mine", False)
    assert f.glab_calls == [], "GitHub alone never touches glab"


@pytest.mark.parametrize("merges, say, desc", [
    ({1: (1, "", "glab: 401 Unauthorized (HTTP 401)")}, "glab auth login --hostname", "not logged in"),
    ({1: (1, "", "x509: certificate signed by unknown authority")}, "GITLAB_CA_CERT", "a CA it doesn't know"),
    ({1: fetch.Unavailable("glab isn't installed: GitLab merge requests need the GitLab CLI")}, "glab isn't installed", "no glab"),
    ({1: subprocess.TimeoutExpired("glab", 9)}, "timed out", "too slow"),
    ({1: (0, "<html>", "")}, "isn't JSON", "a login page, not the API"),
])
def test_gitlab_trouble_is_a_warning_and_the_last_row_stays(tmp_path, merges, say, desc):
    f = Fake(tmp_path, [item(1), item(2)])
    fetcher = f.fetcher()
    fetcher.answer("mine", True)
    f.merges, f.now = merges, f.now + fetch.EVERY
    got = fetcher.answer("mine", True)
    assert sorted(r["number"] for r in got["prs"]) == [1, 2], f"{desc}: both rows still shown, the failed one as it was"
    assert say in got["warning"] and got.get("error") is None, desc
    assert f.states.get(URL.format(1)) == "open", "its item's status left as it was"


def test_a_failed_merge_request_never_shown_is_just_a_warning(tmp_path):
    f = Fake(tmp_path, [item(1)], merges={1: (1, "", "glab: 500 Internal Server Error (HTTP 500)")})
    got = f.fetcher().answer("mine", False)
    assert got["prs"] == [] and "500 Internal Server Error" in got["warning"] and got.get("error") is None


def test_one_warning_a_way_however_many_merge_requests_fail(tmp_path):
    down = (1, "", "glab: 502 Bad Gateway (HTTP 502)")
    f = Fake(tmp_path, [item(n) for n in (1, 2, 3)], merges={1: down, 2: down, 3: down})
    assert f.fetcher().answer("mine", False)["warning"].count("502 Bad Gateway") == 1


def test_a_gh_that_is_gone_leaves_the_merge_requests_showing(tmp_path):
    f = Fake(tmp_path, [item(2)], gh=fetch.Unavailable("gh isn't installed: the PRs pane needs the GitHub CLI"))
    got = f.fetcher().answer("mine", False)
    assert [r["number"] for r in got["prs"]] == [2] and got.get("error") is None
    assert "gh isn't installed" in got["warning"], "said, but the table stays"
    assert f.states == {URL.format(2): "open"}


def test_a_gh_that_fails_keeps_githubs_last_rows_beside_the_merge_requests(tmp_path):
    ok = {"data": {"p0": {"pullRequest": {"number": 1, "title": "GH", "url": GITHUB.format(1), "isDraft": False,
                                          "state": "OPEN", "updatedAt": "t", "headRefName": "b", "reviewDecision": None,
                                          "mergeable": "MERGEABLE", "repository": {"nameWithOwner": "two-inc/wheelhouse"},
                                          "commits": {"nodes": []}}}, "search": {"nodes": []}}}
    f = Fake(tmp_path, [item(1, url=GITHUB), item(2)], gh=ok)
    fetcher = f.fetcher()
    fetcher.answer("mine", True)
    f.gh, f.now = fetch.Unavailable("gh isn't logged in: run gh auth login"), f.now + fetch.EVERY
    got = fetcher.answer("mine", True)
    assert sorted(r["number"] for r in got["prs"]) == [1, 2] and "gh auth login" in got["warning"]


def test_only_so_many_merge_requests_are_fetched_at_once_and_the_rest_keep_their_rows(tmp_path):
    n = fetch.GITLAB_MOST + 2
    f = Fake(tmp_path, [item(i) for i in range(1, n + 1)])
    fetcher = f.fetcher()
    assert len(fetcher.answer("mine", True)["prs"]) == fetch.GITLAB_MOST and len(f.glab_calls) == fetch.GITLAB_MOST


# acting on one

@pytest.mark.parametrize("act", ["merge", "close", "ready", "draft"])
def test_a_merge_request_isnt_acted_on_from_the_pane_yet(tmp_path, act):
    f = Fake(tmp_path, [item(2)])
    fetcher = f.fetcher()
    got = {"merge": fetcher.merge, "close": fetcher.close, "ready": fetcher.ready,
           "draft": lambda url: fetcher.ready(url, draft=True)}[act](URL.format(2))
    assert got == {"failed": fetch.NO_MR_ACTIONS} and f.gh_calls == [] and f.glab_calls == [], act
    assert "Couldn't" in pane.said(act, {"number": 2}, got) and "gh says" not in pane.said(act, {"number": 2}, got)


def test_an_action_still_takes_only_a_listed_url(tmp_path):
    with pytest.raises(api.Refused):
        Fake(tmp_path).fetcher().close("https://other.internal/a/b/-/merge_requests/1")


# the detail

NOTES = [  # newest first, as GitLab answers with sort=desc
    {"id": 31, "system": True, "body": "merged", "created_at": "2026-11-12T12:00:00Z", "author": {"username": "doug"}},
    {"id": 30, "system": False, "body": "Looks good, ship it", "created_at": "2026-11-12T11:00:00Z", "author": {"username": "sam"}},
    {"id": 29, "system": True, "body": "approved this merge request\n\nmore", "created_at": "2026-11-12T10:30:00Z",
     "author": {"username": "sam"}},
    {"id": 28, "system": False, "body": "  ", "created_at": "2026-11-12T10:20:00Z", "author": {"username": "bot"}},
]


def test_a_merge_requests_detail(tmp_path):
    f = Fake(tmp_path, [item(12)], merges={12: mr(12, description="Why: pins.")}, notes=NOTES)
    fetcher = f.fetcher()
    fetcher.answer("mine", True)
    got = fetcher.detail(URL.format(12), False)
    assert got["body"] == "Why: pins."
    assert got["checks"] == [{"name": "pipeline #77", "url": f"https://{HOST}/autonomous-piloting/av/-/pipelines/77",
                              "state": "passed"}]
    assert [(h["at"], h["what"]) for h in got["history"]] == [
        ("2026-11-12T10:20:00Z", "bot commented"), ("2026-11-12T10:30:00Z", "sam approved this merge request"),
        ("2026-11-12T11:00:00Z", "sam commented"), ("2026-11-12T12:00:00Z", "doug merged")], "oldest first, a system note's first line"
    assert got["comments"] == [{"author": "sam", "at": "2026-11-12T11:00:00Z", "body": "Looks good, ship it",
                                "url": URL.format(12) + "#note_30"}], "the people's that say something"
    notes_call = [c[4] for c in f.glab_calls if "/notes" in c[4]][0]
    assert notes_call == ENDPOINT.format(12) + f"/notes?order_by=created_at&sort=desc&per_page={fetch.NOTES}"
    assert len(f.gh_calls) == 1, "gh was asked for the list alone"


@pytest.mark.parametrize("pipeline, checks, desc", [
    ({"id": 1, "status": "failed", "web_url": "u"}, [{"name": "pipeline #1", "url": "u", "state": "failing"}], "failing"),
    ({"id": 1, "status": "running", "web_url": "u"}, [{"name": "pipeline #1", "url": "u", "state": "running"}], "running"),
    (None, [], "no pipeline"),
])
def test_a_merge_requests_pipeline_is_its_one_check(tmp_path, pipeline, checks, desc):
    f = Fake(tmp_path, [item(12)], merges={12: mr(12, head_pipeline=pipeline)})
    fetcher = f.fetcher()
    fetcher.answer("mine", True)
    assert fetcher.detail(URL.format(12), False)["checks"] == checks, desc


def test_a_detail_that_fails_says_so_and_keeps_what_it_had(tmp_path):
    f = Fake(tmp_path, [item(12)], merges={12: mr(12, description="d")})
    fetcher = f.fetcher()
    fetcher.answer("mine", True)
    fetcher.detail(URL.format(12), False)
    f.merges[12] = (1, "", "glab: 503 Service Unavailable (HTTP 503)")
    f.now += fetch.DETAIL_EVERY
    got = fetcher.detail(URL.format(12), True)
    assert "503 Service Unavailable" in got["error"] and got["body"] == "d"


# glab itself

def fake_run(monkeypatch, result=None, raises=None):
    seen = SimpleNamespace(argv=None, kw=None)

    def run(argv, **kw):
        seen.argv, seen.kw = argv, kw
        if raises:
            raise raises
        return result
    monkeypatch.setattr(fetch.subprocess, "run", run)
    return seen


def test_glab_runs_with_no_input_and_the_given_ca_alone(monkeypatch, tmp_path):
    ca = tmp_path / "ca.crt"
    ca.write_text("pem")
    monkeypatch.setenv("WHEELHOUSE_GITLAB_CA_CERT", str(ca))
    monkeypatch.setenv("GITLAB_TOKEN", TOKEN)
    seen = fake_run(monkeypatch, subprocess.CompletedProcess([], 0, "{}", ""))
    assert fetch.glab(["glab", "api", "--hostname", HOST, "version"]) == (0, "{}", "")
    env = seen.kw["env"]
    assert (env["SSL_CERT_FILE"], env["NO_PROMPT"], env["GITLAB_TOKEN"]) == (str(ca), "1", TOKEN), "glab finds its token itself"
    assert seen.kw["stdin"] == subprocess.DEVNULL and seen.kw["timeout"] == fetch.TIMEOUT
    assert TOKEN not in " ".join(seen.argv), "the token is never on the command line"
    assert "SSL_CERT_FILE" not in os.environ, "this process's own environment is left as it was"


def test_glab_leaves_its_own_ca_settings_alone_when_none_is_given(monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    seen = fake_run(monkeypatch, subprocess.CompletedProcess([], 0, "{}", ""))
    fetch.glab(["glab", "api", "version"])
    assert "SSL_CERT_FILE" not in seen.kw["env"]


def test_glab_stderr_is_scrubbed_and_a_missing_glab_is_unavailable(monkeypatch):
    fake_run(monkeypatch, subprocess.CompletedProcess([], 1, "", f"bad {TOKEN}"))
    assert fetch.glab(["glab"]) == (1, "", "bad [token]")
    fake_run(monkeypatch, raises=FileNotFoundError())
    with pytest.raises(fetch.Unavailable, match="glab isn't installed"):
        fetch.glab(["glab"])


def test_the_route_builds_its_fetcher_with_glab(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(pane, "RUNNER", lambda argv: (0, json.dumps({"data": {"search": {"nodes": []}}}), ""))
    monkeypatch.setattr(pane, "GLAB_RUNNER", lambda argv: calls.append(argv) or (0, json.dumps(mr(2)), ""))
    ctx = api.Context(tmp_path, lambda: [], lambda: None, pr_items=lambda: [item(2)], set_pr_states=lambda s: None)
    listed = next(r for r in pane.routes(ctx) if r.path == "list")
    got = listed.handler(api.Call({}, {"scope": "mine"}))
    assert calls and calls[0][:4] == ["glab", "api", "--hostname", HOST] and got["prs"][0]["number"] == 2
