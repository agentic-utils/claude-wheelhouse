"""The PRs pane's daemon side, without Textual (.plan/pr-pane.md, part 3): the one GraphQL
query a fetch sends through `gh`, its answer read into rows, each row's light, when a fetch
is due, and the cache in `<state dir>/prs.db` the route answers from.

A fetch is one `gh api graphql` call serving both scopes: each tracked PR (a PR item not yet
merged or closed) as an aliased `repository { pullRequest }`, and the `author:@me` search for
All. The clock and the runner are injectable, so the tests never run `gh`.
"""

import json
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from claude_wheelhouse import api

EVERY = 300        # seconds: a fetch at least this often while a pane asks
SETTLE = 20        # seconds after a push before the fetch it cues (GitHub registers it, starts checks)
GAP = 30           # seconds: no fetch within this of the last, whoever asks (the person's r included)
TIMEOUT = 9        # seconds a fetch may take: a route is waited on 10 (daemon.MODULE_WAIT)
SEARCH = "is:pr is:open author:@me"
LIGHTS = ("red", "orange", "yellow", "green", "grey")   # most urgent first: the default order
FAILED = ("FAILURE", "ERROR")
RUNNING = ("PENDING", "EXPECTED")

FIELDS = """fragment F on PullRequest {
  number title url isDraft state updatedAt headRefName reviewDecision mergeable
  repository { nameWithOwner }
  commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
}"""

# a PR's detail (#145): its description, the newest of its timeline (commits, comments, reviews
# and what happened to it), and its head commit's checks, one by one
DETAIL_EVERY = 120   # seconds a PR's detail is kept before a pick fetches it again
DETAIL_GAP = 10      # seconds: r fetches it again, but not within this of the last
HISTORY = 40         # timeline entries asked for
EVENTS = ("PULL_REQUEST_COMMIT", "ISSUE_COMMENT", "PULL_REQUEST_REVIEW", "REVIEW_REQUESTED_EVENT", "READY_FOR_REVIEW_EVENT",
          "CONVERT_TO_DRAFT_EVENT", "HEAD_REF_FORCE_PUSHED_EVENT", "CLOSED_EVENT", "REOPENED_EVENT", "MERGED_EVENT")
DETAIL = ("query($owner: String!, $name: String!, $number: Int!) {\n"
          "  repository(owner: $owner, name: $name) { pullRequest(number: $number) {\n"
          f"    body\n    timelineItems(last: {HISTORY}, itemTypes: [{', '.join(EVENTS)}]) {{ nodes {{ __typename\n"
          "      ... on PullRequestCommit { commit { abbreviatedOid messageHeadline committedDate } }\n"
          "      ... on IssueComment { author { login } createdAt body url }\n"
          "      ... on PullRequestReview { author { login } submittedAt state body url }\n"
          "      ... on ReviewRequestedEvent { createdAt actor { login } requestedReviewer { ... on User { login } ... on Team { name } } }\n"
          + "".join(f"      ... on {t} {{ createdAt actor {{ login }} }}\n" for t in
                    ("ReadyForReviewEvent", "ConvertToDraftEvent", "HeadRefForcePushedEvent", "ClosedEvent",
                     "ReopenedEvent", "MergedEvent"))
          + "    } }\n"
          "    commits(last: 1) { nodes { commit { statusCheckRollup { contexts(first: 100) { nodes { __typename\n"
          "      ... on CheckRun { name status conclusion detailsUrl checkSuite { workflowRun { workflow { name } } } }\n"
          "      ... on StatusContext { context state targetUrl } } } } } } }\n"
          "  } }\n}\n")
BAD = ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE")   # a check failed
DONE = ("SUCCESS", "NEUTRAL", "SKIPPED")                                                    # a check passed
SAID = {"PullRequestReview": {"APPROVED": "approved", "CHANGES_REQUESTED": "asked for changes",
                              "COMMENTED": "reviewed", "DISMISSED": "review dismissed"},
        "ReadyForReviewEvent": "marked it ready", "ConvertToDraftEvent": "made it a draft",
        "HeadRefForcePushedEvent": "force-pushed", "ClosedEvent": "closed it", "ReopenedEvent": "reopened it",
        "MergedEvent": "merged it", "IssueComment": "commented"}
COMMENTS = 10        # the newest comments shown


def login(node: dict | None, key: str = "author") -> str:
    return ((node or {}).get(key) or {}).get("login") or "someone"


def check(node: dict) -> dict | None:
    """A check of the head commit's: {"name", "url", "state"}, state one of failing,
    running or passed; None for none."""
    if node.get("__typename") == "StatusContext":
        state = node.get("state") or ""
        return {"name": node.get("context") or "?", "url": node.get("targetUrl") or "",
                "state": "failing" if state in BAD else "passed" if state in DONE else "running"}
    if node.get("__typename") == "CheckRun":
        flow = ((((node.get("checkSuite") or {}).get("workflowRun") or {}).get("workflow")) or {}).get("name")
        name = node.get("name") or "?"
        conclusion = node.get("conclusion") or ""
        return {"name": f"{flow} / {name}" if flow else name, "url": node.get("detailsUrl") or "",
                "state": "failing" if conclusion in BAD else "passed" if conclusion in DONE
                else "running" if node.get("status") != "COMPLETED" or not conclusion else "passed"}
    return None


def event(node: dict) -> dict | None:
    """A timeline entry as the history shows it: {"at", "what"} (a commit's also "commit");
    None for one it doesn't."""
    kind = node.get("__typename")
    if kind == "PullRequestCommit":
        c = node.get("commit") or {}
        return {"at": c.get("committedDate") or "", "commit": c.get("abbreviatedOid") or "",
                "what": c.get("messageHeadline") or ""}
    if kind == "PullRequestReview":
        return {"at": node.get("submittedAt") or "", "what": f"{login(node)} "
                + SAID[kind].get(node.get("state"), (node.get("state") or "reviewed").lower())}
    if kind == "IssueComment":
        return {"at": node.get("createdAt") or "", "what": f"{login(node)} commented"}
    if kind == "ReviewRequestedEvent":
        who = node.get("requestedReviewer") or {}
        return {"at": node.get("createdAt") or "",
                "what": f"{login(node, 'actor')} asked {who.get('login') or who.get('name') or 'someone'} for a review"}
    if kind in SAID:
        return {"at": node.get("createdAt") or "", "what": f"{login(node, 'actor')} {SAID[kind]}"}
    return None


def detail(data: dict) -> dict:
    """The detail query's data as the pane shows it: {"body", "checks": every check, failing
    first, "history": the timeline, oldest first, "comments": the newest COMMENTS comments and
    reviews that say something, oldest first}."""
    pr = (data.get("repository") or {}).get("pullRequest")
    if not isinstance(pr, dict):
        raise RuntimeError("GitHub has no such pull request")
    nodes = [n for n in ((pr.get("timelineItems") or {}).get("nodes") or []) if isinstance(n, dict)]
    commits = (pr.get("commits") or {}).get("nodes") or []
    rollup = (((commits[0] or {}).get("commit") or {}).get("statusCheckRollup") or {}) if commits else {}
    checks = [c for n in ((rollup.get("contexts") or {}).get("nodes") or []) if isinstance(n, dict) and (c := check(n))]
    said = [{"author": login(n), "at": n.get("createdAt") or n.get("submittedAt") or "", "body": n.get("body") or "",
             "url": n.get("url") or ""}
            for n in nodes if n.get("__typename") in ("IssueComment", "PullRequestReview") and (n.get("body") or "").strip()]
    order = ("failing", "running", "passed")
    return {"body": pr.get("body") or "", "checks": sorted(checks, key=lambda c: order.index(c["state"])),
            "history": [e for n in nodes if (e := event(n))], "comments": said[-COMMENTS:]}


Runner = Callable[[list[str]], tuple[int, str, str]]   # argv -> (exit code, stdout, stderr)


class Unavailable(Exception):
    """No `gh`, or not logged in: the pane says so in place of the table."""


def query(tracked: list[tuple[str, str, int]]) -> str:
    """The one query: each tracked PR (owner, repo, number) as alias p<i>, and the search."""
    parts = [f"p{i}: repository(owner: {json.dumps(o)}, name: {json.dumps(r)}) "
             f"{{ pullRequest(number: {n}) {{ ...F }} }}" for i, (o, r, n) in enumerate(tracked)]
    parts.append(f"search(query: {json.dumps(SEARCH)}, type: ISSUE, first: 100) "
                 "{ nodes { ... on PullRequest { ...F } } }")
    return "query {\n  " + "\n  ".join(parts) + "\n}\n" + FIELDS


def row(node: dict) -> dict | None:
    """A pull request as the GraphQL answer gives it, as a cached row; None for no PR (an
    alias GitHub couldn't resolve, a search hit that isn't one)."""
    if not isinstance(node, dict) or not node.get("url"):
        return None
    commits = (node.get("commits") or {}).get("nodes") or []
    rollup = ((commits[0] or {}).get("commit") or {}).get("statusCheckRollup") if commits else None
    return {"url": api.pr_url(node["url"]) or node["url"], "number": node.get("number"),
            "title": node.get("title") or "", "draft": bool(node.get("isDraft")),
            "state": node.get("state") or "", "updated": node.get("updatedAt") or "",
            "branch": node.get("headRefName") or "", "review": node.get("reviewDecision"),
            "mergeable": node.get("mergeable"), "checks": (rollup or {}).get("state"),
            "repo": (node.get("repository") or {}).get("nameWithOwner") or ""}


def parse(data: dict, count: int) -> tuple[list[dict], list[dict]]:
    """The answer's data: (the tracked PRs found, the search's), each a row."""
    tracked = [r for i in range(count) if (r := row(((data.get(f"p{i}") or {}).get("pullRequest"))))]
    found = [r for n in ((data.get("search") or {}).get("nodes") or []) if (r := row(n))]
    return tracked, found


def status(r: dict) -> str:
    """A PR's status word for its item: one of api.PR_STATES."""
    return {"MERGED": "merged", "CLOSED": "closed"}.get(r["state"]) or ("draft" if r["draft"] else "open")


def light(r: dict) -> str:
    """The first that applies: red (a check failed, changes requested, a conflict); orange
    (checks running); grey (a draft, which isn't ready whatever else holds); yellow (a review
    still required, or mergeability not yet worked out); green (ready to merge). No checks
    count as passing."""
    if r["checks"] in FAILED or r["review"] == "CHANGES_REQUESTED" or r["mergeable"] == "CONFLICTING":
        return "red"
    if r["checks"] in RUNNING:
        return "orange"
    if r["draft"]:
        return "grey"
    if r["review"] == "REVIEW_REQUIRED" or r["mergeable"] != "MERGEABLE":
        return "yellow"
    return "green"


def ordered(rows: list[dict]) -> list[dict]:
    """By the light's urgency, then most recently updated first."""
    rows = sorted(rows, key=lambda r: r["updated"], reverse=True)
    return sorted(rows, key=lambda r: LIGHTS.index(r["light"]))


def due(now: float, last: float | None, activity: float | None, fresh: bool) -> bool:
    """Whether a fetch is due: never within GAP of the last; else when asked fresh, on the
    first ask, EVERY seconds on, or once a push newer than the last fetch is SETTLE old."""
    if last is not None and now - last < GAP:
        return False
    if fresh or last is None or now - last >= EVERY:
        return True
    return activity is not None and activity > last and now - activity >= SETTLE


def gh(argv: list[str]) -> tuple[int, str, str]:
    """The real runner: `gh` itself, Unavailable if it isn't installed."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT)
    except FileNotFoundError:
        raise Unavailable("gh isn't installed: the PRs pane needs the GitHub CLI") from None
    return done.returncode, done.stdout, done.stderr


def run(q: str, runner: Runner) -> dict:
    """The query sent through `gh api graphql`: its data. A partial answer (one alias GitHub
    couldn't resolve) still counts. Unavailable when not logged in; RuntimeError otherwise."""
    return answer(*runner(["gh", "api", "graphql", "-f", f"query={q}"]))


def answer(code: int, out: str, err: str) -> dict:
    """`gh api graphql`'s answer: its data, as run reads it."""
    try:
        data = json.loads(out).get("data") if out.strip() else None
    except (ValueError, AttributeError):
        data = None
    if isinstance(data, dict):
        return data
    if code == 4 or "gh auth login" in err:
        raise Unavailable("gh isn't logged in: run gh auth login")
    raise RuntimeError((err or out).strip().splitlines()[-1] if (err or out).strip() else f"gh exited {code}")


class Cache:
    """`<state dir>/prs.db`: one row per PR from the last fetch, its JSON and when it was
    fetched. A connection a call, as routes run on worker threads."""

    def __init__(self, path: Path):
        self.path = path
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS prs (url TEXT PRIMARY KEY, json TEXT NOT NULL, "
                       "fetched_at REAL NOT NULL)")

    def db(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def replace(self, rows: list[dict], at: float) -> None:
        with self.db() as db:
            db.execute("DELETE FROM prs")
            db.executemany("INSERT OR REPLACE INTO prs VALUES (?, ?, ?)",
                           [(r["url"], json.dumps(r), at) for r in rows])

    def update(self, row: dict) -> None:
        """One row written again, as fetched when it was: the others left as they are."""
        with self.db() as db:
            db.execute("UPDATE prs SET json = ? WHERE url = ?", (json.dumps(row), row["url"]))

    def rows(self) -> list[dict]:
        with self.db() as db:
            return [json.loads(j) for (j,) in db.execute("SELECT json FROM prs")]

    def fetched_at(self) -> float | None:
        with self.db() as db:
            return db.execute("SELECT MAX(fetched_at) FROM prs").fetchone()[0]


class Fetcher:
    """The route's state, one per daemon: the cache, the last fetch, and the lock that keeps
    a second fetch from starting while one runs (its asker answered from the cache)."""

    def __init__(self, ctx: api.Context, runner: Runner = gh, clock: Callable[[], float] = time.time):
        self.ctx, self.runner, self.clock = ctx, runner, clock
        self.cache = Cache(Path(ctx.state_dir) / "prs.db")
        self.last = self.cache.fetched_at()   # a restart keeps the GAP
        self.lock = threading.Lock()
        self.details: dict[str, tuple[float, dict]] = {}   # each PR's detail as last fetched (detail), and when
        self.error: str | None = None     # gh unavailable: shown in place of the table
        self.warning: str | None = None   # the last fetch failed otherwise: the cache shown, and why

    def items(self) -> dict[str, dict]:
        """Each PR item by its URL (the first, should two sessions share one)."""
        out: dict[str, dict] = {}
        for it in (self.ctx.pr_items() if self.ctx.pr_items else []):
            out.setdefault(it["url"], it)
        return out

    def fetch(self) -> None:
        tracked = [(url, p) for url, it in self.items().items()
                   if it["status"] not in ("merged", "closed") and (p := api.parse_pr(url))]
        try:
            data = run(query([p for _, p in tracked]), self.runner)
        except Unavailable as e:
            self.error, self.last = str(e), self.clock()
            return
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            self.error, self.warning, self.last = None, str(e) or type(e).__name__, self.clock()
            return
        mine, found = parse(data, len(tracked))
        for r in mine:
            r["tracked"] = True
        by_url = {r["url"]: r for r in found}
        by_url.update({r["url"]: {**by_url.get(r["url"], {}), **r} for r in mine})
        for r in found:
            by_url[r["url"]].setdefault("searched", True)
        self.last = self.clock()
        self.cache.replace(list(by_url.values()), self.last)
        self.error = self.warning = None
        if mine and self.ctx.set_pr_states is not None:
            self.ctx.set_pr_states({r["url"]: status(r) for r in mine})

    def answer(self, scope: str, fresh: bool) -> dict:
        """The route's answer: a fetch first if one is due and none is running, then the
        cache for the scope, merged and closed PRs left out, in the light's order."""
        activity = self.ctx.pr_activity() if self.ctx.pr_activity else None
        if due(self.clock(), self.last, activity, fresh) and self.lock.acquire(blocking=False):
            try:
                if due(self.clock(), self.last, activity, fresh):   # another may have fetched meanwhile
                    self.fetch()
            finally:
                self.lock.release()
        if self.error:
            return {"error": self.error, "prs": [], "fetched_at": self.last}
        items = self.items()
        out = []
        for r in self.cache.rows():
            it = items.get(r["url"])
            if status(r) in ("merged", "closed") or (scope == "mine" and it is None):
                continue
            out.append({**r, "light": light(r), "item": {"session_id": it["session_id"], "ref": it["ref"]} if it else None})
        return {"prs": ordered(out), "fetched_at": self.last, "warning": self.warning}

    def merge(self, url: str) -> dict:
        """A PR merged by `gh pr merge` (#138), the way its repo allows: {"method"}, its row
        marked merged so the pane drops it; {"error"} with why not, as gh says it."""
        if not isinstance(url, str) or api.pr_url(url) != url:   # the whole of it, nothing gh could take as a flag
            raise api.Refused(400, f"not a pull request URL: {url!r}")
        owner, repo, _ = api.parse_pr(url)
        try:
            how = method(f"{owner}/{repo}", self.runner)
            code, out, err = self.runner(["gh", "pr", "merge", f"--{how}", "--", url])
        except Unavailable as e:
            return {"error": str(e)}
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            return {"error": str(e) or type(e).__name__}
        if code != 0:
            return {"error": last_line(err or out) or f"gh exited {code}"}
        self.ended(url, "MERGED")
        return {"method": how}

    def close(self, url: str) -> dict:
        """A PR closed unmerged by `gh pr close` (#146): {"closed": True}, its row marked
        closed so the pane drops it; {"error"} with why not, as gh says it."""
        if not isinstance(url, str) or api.pr_url(url) != url:   # the whole of it, nothing gh could take as a flag
            raise api.Refused(400, f"not a pull request URL: {url!r}")
        try:
            code, out, err = self.runner(["gh", "pr", "close", "--", url])
        except Unavailable as e:
            return {"error": str(e)}
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            return {"error": str(e) or type(e).__name__}
        if code != 0:
            return {"error": last_line(err or out) or f"gh exited {code}"}
        self.ended(url, "CLOSED")
        return {"closed": True}

    def ended(self, url: str, state: str) -> None:
        """A PR merged or closed from here: its row so marked, and its item's status with it."""
        rows = {r["url"]: r for r in self.cache.rows()}
        if url in rows:
            self.cache.update({**rows[url], "state": state})
        if self.ctx.set_pr_states is not None and url in self.items():
            self.ctx.set_pr_states({url: state.lower()})

    def detail(self, url: str, fresh: bool) -> dict:
        """A PR's detail for the Detail pane (#145): its description, history, checks and
        comments, from one `gh api graphql` call, kept DETAIL_EVERY seconds (fresh: once
        DETAIL_GAP old). On a failure, {"error"}, with what was fetched before if any."""
        if not isinstance(url, str) or api.pr_url(url) != url:
            raise api.Refused(400, f"not a pull request URL: {url!r}")
        now = self.clock()
        at, kept = self.details.get(url, (None, None))
        if at is not None and now - at < (DETAIL_GAP if fresh else DETAIL_EVERY):
            return kept
        owner, repo, n = api.parse_pr(url)
        try:
            code, out, err = self.runner(["gh", "api", "graphql", "-f", f"query={DETAIL}",
                                          "-f", f"owner={owner}", "-f", f"name={repo}", "-F", f"number={n}"])
            got = detail(answer(code, out, err))
        except Unavailable as e:
            return {**(kept or {}), "error": str(e)}
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            return {**(kept or {}), "error": str(e) or type(e).__name__}
        got["fetched_at"] = now
        self.details[url] = (now, got)
        return got


METHODS = (("mergeCommitAllowed", "merge"), ("squashMergeAllowed", "squash"), ("rebaseMergeAllowed", "rebase"))


def method(repo: str, runner: Runner) -> str:
    """How a repo lets its PRs merge, asked of GitHub: a merge commit where it's allowed,
    else squash, else rebase. RuntimeError if it can't say."""
    code, out, err = runner(["gh", "repo", "view", "--json", ",".join(k for k, _ in METHODS), "--", repo])
    if code == 4 or "gh auth login" in err:
        raise Unavailable("gh isn't logged in: run gh auth login")
    try:
        allowed = json.loads(out) if code == 0 else None
    except ValueError:
        allowed = None
    if not isinstance(allowed, dict):
        raise RuntimeError(last_line(err or out) or f"gh exited {code}")
    how = next((m for key, m in METHODS if allowed.get(key)), None)
    if how is None:
        raise RuntimeError(f"{repo} allows no merge method")
    return how


def last_line(text: str) -> str:
    return text.strip().splitlines()[-1].strip() if text.strip() else ""
