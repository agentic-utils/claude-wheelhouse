import json

import pytest

from claude_wheelhouse import api

prs = pytest.importorskip("claude_wheelhouse_prs", reason="installed without the prs module")
from claude_wheelhouse_prs import fetch, pane  # noqa: E402

URL = "https://github.com/two-inc/wheelhouse/pull/{}"


def node(n, repo="two-inc/wheelhouse", state="OPEN", draft=False, checks="SUCCESS", review="APPROVED",
         mergeable="MERGEABLE", updated="2026-10-20T10:00:00Z", title=None):
    return {"number": n, "title": title or f"PR number {n}", "url": f"https://github.com/{repo}/pull/{n}",
            "isDraft": draft, "state": state, "updatedAt": updated, "headRefName": f"doug/{n}",
            "reviewDecision": review, "mergeable": mergeable, "repository": {"nameWithOwner": repo},
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": checks} if checks else None}}]}}


def as_row(**kw):
    return fetch.row(node(1, **kw))


@pytest.mark.parametrize("kw, light, desc", [
    ({}, "green", "checks pass, approved, mergeable: ready"),
    ({"review": None}, "green", "no review required"),
    ({"checks": None}, "green", "no checks count as passing"),
    ({"checks": "FAILURE"}, "red", "a check failed"),
    ({"checks": "ERROR"}, "red", "a check errored"),
    ({"review": "CHANGES_REQUESTED"}, "red", "changes requested"),
    ({"mergeable": "CONFLICTING"}, "red", "a conflict"),
    ({"checks": "FAILURE", "draft": True}, "red", "red wins over a draft"),
    ({"checks": "PENDING"}, "orange", "checks running"),
    ({"checks": "EXPECTED", "review": "REVIEW_REQUIRED"}, "orange", "checks expected wins over yellow"),
    ({"checks": "PENDING", "draft": True}, "orange", "orange wins over a draft"),
    ({"draft": True}, "grey", "a draft otherwise ready is grey"),
    ({"draft": True, "review": "REVIEW_REQUIRED"}, "grey", "a draft wins over yellow"),
    ({"review": "REVIEW_REQUIRED"}, "yellow", "a review required"),
    ({"mergeable": "UNKNOWN"}, "yellow", "mergeability not worked out holds at yellow"),
])
def test_light(kw, light, desc):
    assert fetch.light(as_row(**kw)) == light, desc


@pytest.mark.parametrize("kw, status, desc", [
    ({}, "open", "open"),
    ({"draft": True}, "draft", "a draft"),
    ({"state": "MERGED"}, "merged", "merged"),
    ({"state": "CLOSED", "draft": True}, "closed", "closed wins over draft"),
])
def test_status(kw, status, desc):
    assert fetch.status(as_row(**kw)) == status and status in api.PR_STATES, desc


@pytest.mark.parametrize("now, last, activity, fresh, want, desc", [
    (1000, None, None, False, True, "the first ask fetches"),
    (1000, 980, None, True, False, "never within GAP of the last, fresh or not"),
    (1000, 960, None, True, True, "fresh past GAP"),
    (1000, 960, None, False, False, "not fresh, under EVERY: the cache"),
    (1000, 700, None, False, True, "EVERY seconds on"),
    (1000, 900, 990, False, False, "a push under SETTLE old waits"),
    (1000, 900, 975, False, True, "a push SETTLE old fetches"),
    (1000, 900, 850, False, False, "a push before the last fetch is spent"),
])
def test_due(now, last, activity, fresh, want, desc):
    assert fetch.due(now, last, activity, fresh) is want, desc


def test_query_aliases_each_tracked_pr_and_searches():
    q = fetch.query([("two-inc", "wheelhouse", 108), ("doug", "mizpah", 3)])
    assert 'p0: repository(owner: "two-inc", name: "wheelhouse") { pullRequest(number: 108) { ...F } }' in q
    assert 'p1: repository(owner: "doug", name: "mizpah") { pullRequest(number: 3) { ...F } }' in q
    assert 'search(query: "is:pr is:open author:@me", type: ISSUE, first: 100)' in q
    assert "statusCheckRollup { state }" in q and q.count("fragment F on PullRequest") == 1


def test_parse_skips_unresolved_aliases_and_non_prs():
    data = {"p0": {"pullRequest": node(1)}, "p1": None, "search": {"nodes": [node(2), {}]}}
    tracked, found = fetch.parse(data, 2)
    assert [r["number"] for r in tracked] == [1] and [r["number"] for r in found] == [2]
    assert tracked[0]["checks"] == "SUCCESS" and tracked[0]["branch"] == "doug/1"


class Fake:
    """A Context's daemon side, and a gh that answers what it's given."""

    def __init__(self, tmp_path, items=(), answer=None, code=0, err=""):
        self.items, self.states, self.calls, self.activity = list(items), {}, [], None
        self.answer, self.code, self.err, self.now = answer or {"data": {"search": {"nodes": []}}}, code, err, 1000.0
        self.ctx = api.Context(tmp_path, lambda: [], lambda: None, pr_items=lambda: self.items,
                               set_pr_states=self.states.update, pr_activity=lambda: self.activity)

    def run(self, argv):
        self.calls.append(argv)
        return self.code, json.dumps(self.answer) if self.answer is not None else "", self.err

    def fetcher(self):
        return fetch.Fetcher(self.ctx, self.run, lambda: self.now)


def item(n, sid="s1", status="open"):
    return {"url": URL.format(n), "session_id": sid, "ref": f"PR{n}", "status": status, "title": "x"}


def test_a_fetch_serves_both_scopes_and_sets_states(tmp_path):
    f = Fake(tmp_path, [item(1), item(2), item(3, status="merged")], {"data": {
        "p0": {"pullRequest": node(1, checks="FAILURE")}, "p1": {"pullRequest": node(2, state="MERGED")},
        "search": {"nodes": [node(1, checks="FAILURE"), node(4, updated="2026-10-20T11:00:00Z"),
                             node(5, draft=True)]}}})
    got = f.fetcher().answer("mine", False)
    assert len(f.calls) == 1 and "pullRequest(number: 3)" not in f.calls[0][-1], "one call; a merged item isn't tracked"
    assert f.states == {URL.format(1): "open", URL.format(2): "merged"}, "the tracked PRs' states written back"
    assert [(r["number"], r["item"]) for r in got["prs"]] == [(1, {"session_id": "s1", "ref": "PR1"})], \
        "mine: the open items only, merged left out"
    every = fetch.Fetcher(f.ctx, f.run, lambda: f.now).answer("all", False)
    assert len(f.calls) == 1, "a restart keeps the GAP: answered from prs.db"
    assert [(r["number"], r["light"]) for r in every["prs"]] == [(1, "red"), (4, "green"), (5, "grey")], \
        "all: the search's, by urgency then newest"


def test_no_second_fetch_within_the_gap_and_a_push_cues_one(tmp_path):
    f = Fake(tmp_path)
    fetcher = f.fetcher()
    fetcher.answer("all", True)
    f.now += 10
    fetcher.answer("all", True)
    assert len(f.calls) == 1, "fresh within GAP: the cache"
    f.activity = f.now + 30
    f.now += 40
    fetcher.answer("all", False)
    assert len(f.calls) == 1, "a push 10 s old waits for SETTLE"
    f.now += 15
    fetcher.answer("all", False)
    assert len(f.calls) == 2, "then fetches"


def test_a_fetch_in_flight_answers_others_from_the_cache(tmp_path):
    f = Fake(tmp_path)
    fetcher = f.fetcher()
    fetcher.lock.acquire()
    assert fetcher.answer("all", True)["prs"] == [] and f.calls == [], "the lock held: no second fetch"


@pytest.mark.parametrize("answer, code, err, raises, key, desc", [
    (None, 4, "To get started with GitHub CLI, please run:  gh auth login", None, "error", "not logged in"),
    (None, 1, "error connecting to api.github.com", None, "warning", "a network failure keeps the cache"),
    (None, 0, "", fetch.Unavailable("gh isn't installed"), "error", "no gh"),
])
def test_gh_failures(tmp_path, answer, code, err, raises, key, desc):
    f = Fake(tmp_path, answer=answer, code=code, err=err)
    f.answer = answer

    def run(argv):
        if raises:
            raise raises
        return f.run(argv)
    got = fetch.Fetcher(f.ctx, run, lambda: f.now).answer("all", True)
    assert got.get(key) and got["prs"] == [], desc


def test_a_partial_answer_still_counts(tmp_path):
    f = Fake(tmp_path, [item(1)], {"data": {"p0": None, "search": {"nodes": [node(2)]}},
                                   "errors": [{"message": "Could not resolve"}]}, code=1)
    got = f.fetcher().answer("all", False)
    assert [r["number"] for r in got["prs"]] == [2] and not got.get("warning"), "the data GitHub gave"


def test_the_route_refuses_an_unknown_scope(tmp_path, monkeypatch):
    f = Fake(tmp_path)
    monkeypatch.setattr(pane, "RUNNER", f.run)
    (route,) = pane.routes(f.ctx)
    assert route.handler(api.Call({}, {"scope": "all"}))["prs"] == []
    with pytest.raises(api.Refused):
        route.handler(api.Call({}, {"scope": "everyone"}))


@pytest.mark.parametrize("spec, column, want, desc", [
    ("light*", "light", "light▲", "the light cycles *, ▲"),
    ("light▲", "light", "light▼", "then ▼"),
    ("light▼", "light", "light*", "then back to *"),
    ("light*", "title", "title▲", "another column starts ▲"),
    ("title▲", "title", "title▼", "then ▼"),
    ("title▼", "title", "light*", "then back to the light"),
])
def test_next_sort(spec, column, want, desc):
    assert pane.next_sort(spec, column) == want, desc
