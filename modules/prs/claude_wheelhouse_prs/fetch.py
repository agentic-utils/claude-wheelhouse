"""The PRs pane's daemon side, without Textual (.plan/pr-pane.md, part 3): the one GraphQL
query a fetch sends through `gh`, its answer read into rows, each row's light, when a fetch
is due, and the cache in `<state dir>/prs.db` the route answers from.

A fetch is one `gh api graphql` call serving both scopes: each tracked PR (a PR item not yet
merged or closed) as an aliased `repository { pullRequest }`, and the `author:@me` search for
All. The clock and the runner are injectable, so the tests never run `gh`.

A GitLab merge request (a PR item whose URL is one: .plan/gitlab.md) is fetched by `glab api`,
one REST call each, its answer read into the same row (`mr_row`), so status, light and the
cache are shared. glab holds the host's login and token (its config, or GITLAB_TOKEN in the
environment, both its own): none is read, passed on or logged here, and errors are scrubbed.
A problem with GitLab is a warning, never the error that blanks the table; All is GitHub's.
Its own runner is injectable too (`glab`).
"""

import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from claude_wheelhouse import api

EVERY = 300        # seconds: a fetch at least this often while a pane asks
SETTLE = 20        # seconds after a push before the fetch it cues (GitHub registers it, starts checks)
GAP = 30           # seconds: no fetch within this of the last, whoever asks (the person's r included)
TIMEOUT = 9        # seconds a fetch may take: a route is waited on 10 (daemon.MODULE_WAIT)
RECHECK = 3600     # seconds: a gone PR is asked about again this often (gone may be the login's view, and pass)
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


# GitLab (.plan/gitlab.md)
GONE = object()      # fetch_gitlab's word for a merge request the forge says is gone
GITLAB_AT_ONCE = 4   # merge requests fetched in parallel
GITLAB_MOST = 30     # merge requests fetched in one go: the rest keep their last row
NOTES = 40           # a merge request's newest notes asked for
CHECKS = {"success": "SUCCESS", "skipped": "SUCCESS", "manual": "SUCCESS", "failed": "FAILURE", "canceled": "FAILURE",
          "canceling": "FAILURE", "running": "PENDING", "pending": "PENDING", "created": "PENDING",
          "preparing": "PENDING", "waiting_for_resource": "PENDING", "scheduled": "PENDING",
          "waiting_for_callback": "PENDING"}   # a pipeline's status as GitHub's rollup says it
STATE = {"opened": "OPEN", "locked": "OPEN", "merged": "MERGED", "closed": "CLOSED"}
DRAFT_MARK = re.compile(r"^\s*(?:\[draft\]|\(draft\)|draft:)\s*", re.IGNORECASE)
TOKENS = re.compile(r"\bgl[a-z]{2,6}-[A-Za-z0-9_.-]{8,}")   # GitLab's token prefixes (glpat-, gldt-, glcbt-...)
SECRETS = ("GITLAB_TOKEN", "GITLAB_ACCESS_TOKEN", "OAUTH_TOKEN")   # glab's token variables
TOKEN_HOST = ("GITLAB_HOST", "GITLAB_URI", "GL_HOST")   # glab's default host, the one its token variables are for
DRAFT_PREFIX = re.compile(r"^\s*(?:\[draft\]|\(draft\)|draft:|\[wip\]|wip:)\s*", re.IGNORECASE)   # as GitLab reads one
# what stands in a merge request's way, by detailed_merge_status: the words a refused merge says
BLOCKED = {"draft_status": "it's a draft", "not_approved": "an approval is required",
           "requested_changes": "changes were requested", "conflict": "it has conflicts with its target branch",
           "ci_must_pass": "its pipeline hasn't passed", "ci_still_running": "its pipeline is still running",
           "discussions_not_resolved": "its discussions aren't all resolved", "need_rebase": "it needs a rebase",
           "jira_association": "it needs a Jira issue", "external_status_checks": "its external checks haven't passed",
           "merge_request_blocked": "another merge request blocks it", "locked_paths": "files it changes are locked",
           "locked_lfs_files": "LFS files it changes are locked", "title_regex": "its title doesn't fit the project's rule",
           "security_policy_violations": "a security policy blocks it", "not_open": "it isn't open"}


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


def run(q: str, runner: Runner, gone: set | None = None, unseen: set | None = None) -> dict:
    """The query sent through `gh api graphql`: its data. A partial answer (one alias GitHub
    couldn't resolve) still counts; the aliases whose repo GitHub found without the PR are added
    to gone, and those whose repo it couldn't resolve to unseen, if given (not_found).
    Unavailable when not logged in; RuntimeError otherwise."""
    code, out, err = runner(["gh", "api", "graphql", "-f", f"query={q}"])
    found_no_pr, no_repo = not_found(out)
    if gone is not None:
        gone.update(found_no_pr)
    if unseen is not None:
        unseen.update(no_repo)
    return answer(code, out, err)


def not_found(out: str) -> tuple[set[str], set[str]]:
    """The aliases a GraphQL answer says it couldn't find, by its NOT_FOUND errors' paths: (those
    whose repo resolved and whose pullRequest is null, a PR that isn't there; those whose repo
    didn't resolve, which a login without access to it is told too, so no word on the PR). Any
    other error, or no error, adds none."""
    try:
        got = json.loads(out) if out.strip() else {}
        errors, data = got.get("errors") or [], got.get("data") or {}
    except (ValueError, AttributeError):
        return set(), set()
    paths = [e["path"] for e in errors if isinstance(e, dict) and e.get("type") == "NOT_FOUND"
             and isinstance(e.get("path"), list) and e["path"] and isinstance(e["path"][0], str)]
    gone = {p[0] for p in paths if p[1:] == ["pullRequest"] and isinstance(data, dict)
            and isinstance(data.get(p[0]), dict) and data[p[0]].get("pullRequest") is None}
    return gone, {p[0] for p in paths if len(p) == 1}


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


# GitLab merge requests (.plan/gitlab.md)

def scrub(text: str) -> str:
    """text without any GitLab token in it: one in glab's output, or the value of a token
    variable in this process's environment."""
    for var in SECRETS:
        if len(secret := os.environ.get(var) or "") >= 8:
            text = text.replace(secret, "[token]")
    return TOKENS.sub("[token]", text)


def token_host() -> str:
    """The host glab's token variables are for: its default host, GITLAB_HOST (else GITLAB_URI,
    else GL_HOST) as host[:port] in lower case, any scheme and path cut; else gitlab.com."""
    value = next((v for var in TOKEN_HOST if (v := (os.environ.get(var) or "").strip())), "")
    return value.split("://", 1)[-1].split("/", 1)[0].lower() or "gitlab.com"


def glab_env(argv: list[str]) -> dict[str, str]:
    """glab's environment for argv: this process's, never asking (GLAB_NO_PROMPT), with the CA the
    person named (SSL_CERT_FILE, which a Go program reads). The token variables go only to the
    host they are for (token_host), as `--hostname` names it: a merge request's URL can name any
    host, and an older glab sends an environment token to whichever it is pointed at."""
    env = {**os.environ, "GLAB_NO_PROMPT": "1"}
    host = argv[argv.index("--hostname") + 1].lower() if "--hostname" in argv[:-1] else None
    if host != token_host():
        for var in SECRETS:
            env.pop(var, None)
    if ca := api.gitlab_ca_cert():
        env["SSL_CERT_FILE"] = ca
    return env


def glab(argv: list[str]) -> tuple[int, str, str]:
    """The real runner for GitLab: `glab` itself, Unavailable if it isn't installed, run in
    glab_env(argv); its token it finds itself."""
    env = glab_env(argv)
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT, env=env, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise Unavailable("glab isn't installed: GitLab merge requests need the GitLab CLI") from None
    return done.returncode, done.stdout, scrub(done.stderr)


def mr_path(path: str, n: int) -> str:
    """A merge request's endpoint, under the API root: the project's path as one encoded segment."""
    return f"projects/{quote(path, safe='')}/merge_requests/{n}"


class Gone(RuntimeError):
    """The forge says a PR or merge request, or its repo or project, no longer exists (a 404 that
    isn't the login's doing): never a network error, a 5xx or a refused login."""


def mr_failure(host: str, code: int, out: str, err: str) -> Exception:
    """What `glab api` failing says, as Unavailable (not logged in), Gone (a 404: the caller
    confirms the login is good, mr_json) or RuntimeError (any other)."""
    text = scrub(err or out)
    if re.search(r"\b404\b", text) and not re.search(r"\b401\b|unauthori[sz]ed", text, re.IGNORECASE):
        return Gone(f"{host}: this merge request no longer exists (GitLab answers 404)")
    if re.search(r"\b401\b|unauthori[sz]ed|not logged in|no token|authenticat", text, re.IGNORECASE):
        return Unavailable(f"{host}: glab isn't logged in: run glab auth login --hostname {host}")
    line = last_line(text) or f"glab exited {code}"
    hint = " (the host's CA: set GITLAB_CA_CERT, or glab's ca_cert)" if "x509" in line or "certificate" in line else ""
    return RuntimeError(f"{host}: {line}{hint}")


def mr_json(host: str, endpoint: str, runner: Runner, *options: str):
    """What `glab api --hostname <host> [options] <endpoint>` answers, read as JSON (options such as
    `-X PUT -f title=x`); Unavailable or RuntimeError as mr_failure says."""
    code, out, err = runner(["glab", "api", "--hostname", host, *options, endpoint])
    if code != 0:
        failure = mr_failure(host, code, out, err)
        if isinstance(failure, Gone):   # GitLab also answers 404 to a login that can't see it
            mr_login(host, runner)
        raise failure
    try:
        return json.loads(out)
    except ValueError:
        raise RuntimeError(f"{host}: GitLab's answer isn't JSON") from None


def mr_login(host: str, runner: Runner) -> None:
    """Nothing if `glab api user` answers (the login is good); else what its failure says, but never
    Gone: a host whose API answers 404 even for the login proves nothing about a merge request."""
    code, out, err = runner(["glab", "api", "--hostname", host, "user"])
    if code != 0:
        failure = mr_failure(host, code, out, err)
        raise RuntimeError(f"{host}: GitLab answers 404 even for the login: is it a GitLab host?") \
            if isinstance(failure, Gone) else failure


def mr_blocker(data) -> str | None:
    """Why a merge request can't merge as it stands, in words; None if GitLab would take it (or may:
    still checking). Read from the MR GitLab answered."""
    if not isinstance(data, dict) or not data.get("iid"):
        return "GitLab has no such merge request"
    if data.get("state") not in ("opened", "locked"):
        return f"it is {data.get('state') or 'not open'}"
    status = data.get("detailed_merge_status")
    if status in BLOCKED:
        return BLOCKED[status]
    if status is None and (data.get("draft") or data.get("work_in_progress")):
        return BLOCKED["draft_status"]
    if status is None and (data.get("has_conflicts") or data.get("merge_status") == "cannot_be_merged"):
        return BLOCKED["conflict"]
    return None


def mr_title(title: str, draft: bool) -> str:
    """A merge request's title as a draft (a `Draft: ` prefix) or as ready (any such prefix cut)."""
    plain = DRAFT_PREFIX.sub("", title)
    return f"Draft: {plain}" if draft else plain


def mr_mergeable(data: dict) -> str:
    """GitHub's word for whether a merge request can merge now: MERGEABLE, CONFLICTING or UNKNOWN."""
    detailed = data.get("detailed_merge_status")
    if data.get("has_conflicts") or detailed == "conflict":
        return "CONFLICTING"
    if detailed is None:   # a GitLab before detailed_merge_status
        return {"can_be_merged": "MERGEABLE", "cannot_be_merged": "CONFLICTING"}.get(data.get("merge_status"), "UNKNOWN")
    return "MERGEABLE" if detailed == "mergeable" else "UNKNOWN"


def mr_row(url: str, data: dict) -> dict | None:
    """A merge request as GitLab's REST answer gives it, as a cached row (row's shape, so
    status and light read it as they read a PR's); None for no merge request. url is the
    item's own, the row's key."""
    if not isinstance(data, dict) or not data.get("iid"):
        return None
    draft = bool(data["draft"] if data.get("draft") is not None else data.get("work_in_progress"))   # an older GitLab's name
    title = data.get("title") or ""
    pipeline = data.get("head_pipeline") or {}
    detailed = data.get("detailed_merge_status")
    full = ((data.get("references") or {}).get("full") or "").rpartition("!")[0]
    return {"url": url, "number": data["iid"], "title": DRAFT_MARK.sub("", title) if draft else title, "draft": draft,
            "state": STATE.get(data.get("state"), ""), "updated": data.get("updated_at") or "",
            "branch": data.get("source_branch") or "",
            "review": {"not_approved": "REVIEW_REQUIRED", "requested_changes": "CHANGES_REQUESTED"}.get(detailed),
            "mergeable": mr_mergeable(data), "checks": CHECKS.get(pipeline.get("status")),
            "repo": full or api.parse_mr(url)[1]}


def mr_detail(url: str, runner: Runner) -> dict:
    """A merge request's detail, as `detail` gives a PR's: its description, its head pipeline as
    the one check, its notes as history (the system's: approved, merged, commits added...) and as
    comments (the people's). Unavailable or RuntimeError as mr_failure says."""
    host, path, n = api.parse_mr(url)
    data = mr_json(host, mr_path(path, n), runner)
    if not isinstance(data, dict) or not data.get("iid"):
        raise RuntimeError("GitLab has no such merge request")
    notes = mr_json(host, f"{mr_path(path, n)}/notes?order_by=created_at&sort=desc&per_page={NOTES}", runner)
    notes = [x for x in reversed(notes if isinstance(notes, list) else []) if isinstance(x, dict)]
    who = lambda note: (note.get("author") or {}).get("username") or "someone"   # noqa: E731
    history = [{"at": x.get("created_at") or "", "what": f"{who(x)} {((x.get('body') or '').strip().splitlines() or [''])[0]}".strip()}
               if x.get("system") else {"at": x.get("created_at") or "", "what": f"{who(x)} commented"}
               for x in notes]
    comments = [{"author": who(x), "at": x.get("created_at") or "", "body": x.get("body") or "",
                 "url": f"{url}#note_{x.get('id')}"} for x in notes if not x.get("system") and (x.get("body") or "").strip()]
    pipeline = data.get("head_pipeline") or {}
    state = {"SUCCESS": "passed", "FAILURE": "failing", "PENDING": "running"}.get(CHECKS.get(pipeline.get("status")))
    checks = [{"name": f"pipeline #{pipeline.get('id')}", "url": pipeline.get("web_url") or "", "state": state}] if state else []
    return {"body": data.get("description") or "", "checks": checks, "history": history, "comments": comments[-COMMENTS:]}


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

    def drop(self, url: str) -> None:
        with self.db() as db:
            db.execute("DELETE FROM prs WHERE url = ?", (url,))

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

    def __init__(self, ctx: api.Context, runner: Runner = gh, clock: Callable[[], float] = time.time,
                 glab_runner: Runner = glab):
        self.ctx, self.runner, self.clock, self.glab = ctx, runner, clock, glab_runner
        self.cache = Cache(Path(ctx.state_dir) / "prs.db")
        self.last = self.cache.fetched_at()   # a restart keeps the GAP
        self.lock = threading.Lock()
        self.details: dict[str, tuple[float, dict]] = {}   # each PR's detail as last fetched (detail), and when
        self.results: dict[str, dict] = {}   # what each merge, close, ready or draft came to, until asked (pane's result route)
        self.gone: set[str] = set()       # PRs the last fetch found no longer exist, until it has marked them
        self.rechecked: float | None = None   # when a fetch last asked about the gone PRs too (RECHECK)
        self.error: str | None = None     # gh unavailable: shown in place of the table
        self.warning: str | None = None   # the last fetch failed otherwise: the cache shown, and why

    def items(self) -> dict[str, dict]:
        """Each PR item by its URL (the first, should two sessions share one)."""
        out: dict[str, dict] = {}
        for it in (self.ctx.pr_items() if self.ctx.pr_items else []):
            out.setdefault(it["url"], it)
        return out

    def fetch(self) -> None:
        items = self.items()
        if recheck := self.rechecked is None or self.clock() - self.rechecked >= RECHECK:   # gone ones too, now and then
            self.rechecked = self.clock()
        settled = ("merged", "closed") if recheck else ("merged", "closed", "gone")
        open_urls = [url for url, it in items.items() if it["status"] not in settled]
        self.gone = set()
        tracked = [(url, p) for url in open_urls if not api.is_mr(url) and (p := api.parse_pr(url))]
        merges = sorted((url for url in open_urls if api.is_mr(url)), key=lambda u: items[u]["status"] == "gone")   # live first: GITLAB_MOST
        gitlab, kept, notes = self.fetch_gitlab(merges)
        try:
            missing: set[str] = set()
            unseen: set[str] = set()
            data = run(query([p for _, p in tracked]), self.runner, missing, unseen)
            aliased = lambda aliases: [tracked[int(a[1:])][0] for a in aliases   # noqa: E731
                                       if re.fullmatch(r"p\d+", a) and int(a[1:]) < len(tracked)]
            self.gone |= set(aliased(missing))
            if hidden := [url for url in aliased(unseen) if items[url]["status"] != "gone"]:
                notes.append(f"GitHub can't resolve the repo of {', '.join(items[u]['ref'] for u in hidden)}: "
                             "deleted, renamed, or not visible to gh's login")
        except Unavailable as e:
            if not merges:
                self.error, self.last = str(e), self.clock()
                return
            data, notes = None, [str(e), *notes]   # GitLab's merge requests still show
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            if not merges:
                self.error, self.warning, self.last = None, str(e) or type(e).__name__, self.clock()
                return
            data, notes = None, [str(e) or type(e).__name__, *notes]
        if data is None:   # GitHub's last rows stay
            mine, found, old = gitlab, [], [r for r in self.cache.rows() if not api.is_mr(r["url"])]
        else:
            mine, found = parse(data, len(tracked))
            mine, old = mine + gitlab, []
        for r in mine:
            r["tracked"] = True
        by_url = {r["url"]: r for r in [*old, *found]}
        by_url.update({r["url"]: {**by_url.get(r["url"], {}), **r} for r in mine})
        for r in found:
            by_url[r["url"]].setdefault("searched", True)
        last = {r["url"]: r for r in self.cache.rows()}
        by_url.update({url: last[url] for url in kept if url in last and url not in by_url})   # GitLab didn't answer: its last row
        self.last = self.clock()
        self.cache.replace(list(by_url.values()), self.last)
        self.error, self.warning = None, "; ".join(notes) or None
        if (mine or self.gone) and self.ctx.set_pr_states is not None:
            self.ctx.set_pr_states({**{r["url"]: status(r) for r in mine}, **dict.fromkeys(self.gone, "gone")})

    def mr_act(self, url: str, act: Callable[[str, str], dict]) -> dict:
        """What act(host, endpoint) comes to for a merge request, its failures as {"error"} in the
        words glab gave (scrubbed), as the gh path's are."""
        host, path, n = api.parse_mr(url)
        try:
            return act(host, mr_path(path, n))
        except Gone as e:
            return self.vanished(url, str(e))
        except Unavailable as e:
            return {"error": str(e)}
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            return {"error": scrub(str(e) or type(e).__name__)}

    def lost(self, url: str, said: str) -> dict | None:
        """gh saying the PR isn't there in a repo it found: the PR vanished; else None. A repo it can't
        resolve isn't: a login without access to it is told the same."""
        if "Could not resolve to a PullRequest" in said:
            return self.vanished(url, "this pull request no longer exists (GitHub can't find it)")
        return None

    def vanished(self, url: str, why: str) -> dict:
        """A PR the forge says is gone: its row out of the list, its items marked gone (with a note),
        its detail dropped; {"error"} saying so."""
        self.cache.drop(url)
        if self.ctx.set_pr_states is not None and url in self.items():
            self.ctx.set_pr_states({url: "gone"})
        self.details.pop(url, None)
        return {"error": f"{why}: marked gone"}

    def merge_mr(self, url: str) -> dict:
        """A merge request merged, as `merge` merges a PR: refused in words while GitLab says something stands in
        its way (a draft, an approval, a conflict, a pipeline); squashed where the project always squashes, else
        as the project merges; its source branch left to the project's own rule, as gh leaves it. {"method"}, its
        row marked merged; {"error"} with why not."""
        def act(host: str, endpoint: str) -> dict:
            if why := mr_blocker(mr_json(host, endpoint, self.glab)):
                return {"error": f"{host}: {why}"}
            squash = mr_json(host, endpoint.rsplit("/merge_requests/", 1)[0], self.glab).get("squash_option") == "always"
            done = mr_json(host, f"{endpoint}/merge", self.glab, "-X", "PUT", *(["-F", "squash=true"] if squash else []))
            if (done or {}).get("state") != "merged":
                return {"error": f"{host}: GitLab took the merge but the merge request is {(done or {}).get('state')}: check it"}
            return {"method": "squash" if squash else "merge"}
        got = self.mr_act(url, act)
        if "method" in got:
            self.ended(url, "MERGED")
        return got

    def close_mr(self, url: str) -> dict:
        """A merge request closed unmerged: {"closed": True} (`close` marks its row); {"error"}."""
        def act(host: str, endpoint: str) -> dict:
            done = mr_json(host, endpoint, self.glab, "-X", "PUT", "-f", "state_event=close")
            if (done or {}).get("state") != "closed":
                return {"error": f"{host}: the merge request is {(done or {}).get('state')}, not closed"}
            return {"closed": True}
        return self.mr_act(url, act)

    def draft_mr(self, url: str, draft: bool) -> dict | None:
        """A merge request made a draft, or ready (its title's `Draft: ` prefix put on or cut: the update
        call has no draft field): None once it's done, else {"error"}."""
        def act(host: str, endpoint: str) -> dict:
            now = mr_json(host, endpoint, self.glab)
            if not isinstance(now, dict) or not now.get("iid"):
                return {"error": "GitLab has no such merge request"}
            done = mr_json(host, endpoint, self.glab, "-X", "PUT", "-f", f"title={mr_title(now.get('title') or '', draft)}")
            if bool((done or {}).get("draft")) != draft:
                return {"error": f"{host}: GitLab didn't make it {'a draft' if draft else 'ready'}: check it"}
            return {}
        return self.mr_act(url, act) or None

    def fetch_gitlab(self, urls: list[str]) -> tuple[list[dict], list[str], list[str]]:
        """The open merge requests (urls) fetched through glab, a few at a time: (their rows,
        those that didn't answer, so their last rows are kept, and what went wrong, said once
        a way). Never raises."""
        if not urls:
            return [], [], []
        rows, kept, notes = [], urls[GITLAB_MOST:], []

        def one(url: str):
            host, path, n = api.parse_mr(url)
            try:
                return url, mr_row(url, mr_json(host, mr_path(path, n), self.glab)), None
            except Gone:
                return url, None, GONE
            except Unavailable as e:
                return url, None, str(e)
            except (RuntimeError, OSError, subprocess.SubprocessError) as e:
                return url, None, scrub(str(e) or type(e).__name__)
        with ThreadPoolExecutor(max_workers=GITLAB_AT_ONCE) as pool:
            for url, got, why in pool.map(one, urls[:GITLAB_MOST]):
                if got is not None:
                    rows.append(got)
                elif why is GONE:
                    self.gone.add(url)
                else:
                    kept.append(url)
                    if why and why not in notes:
                        notes.append(why)
        return rows, kept, notes

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
        if api.is_mr(url):
            return self.merge_mr(url)
        owner, repo, _ = api.parse_pr(url)
        try:
            how = method(f"{owner}/{repo}", self.runner)
            code, out, err = self.runner(["gh", "pr", "merge", f"--{how}", "--", url])
        except Unavailable as e:
            return {"error": str(e)}
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            return self.lost(url, str(e)) or {"error": str(e) or type(e).__name__}
        if code != 0:
            return self.lost(url, err or out) or {"error": last_line(err or out) or f"gh exited {code}"}
        self.ended(url, "MERGED")
        return {"method": how}

    def close(self, url: str) -> dict:
        """A PR closed unmerged by `gh pr close` (#146): {"closed": True}, its row marked
        closed so the pane drops it; {"error"} with why not, as gh says it."""
        if (failed := self.pr_command(["close"], url)) is not None:
            return failed
        self.ended(url, "CLOSED")
        return {"closed": True}

    def ready(self, url: str, draft: bool = False) -> dict:
        """A draft marked ready for review by `gh pr ready`, or with draft a PR made a draft
        again by `gh pr ready --undo` (#212): {"draft": bool}, its row and its item's status
        with it; {"error"} with why not, as gh says it."""
        if (failed := self.pr_command(["ready", "--undo"] if draft else ["ready"], url)) is not None:
            return failed
        rows = {r["url"]: r for r in self.cache.rows()}
        if url in rows:
            self.cache.update({**rows[url], "draft": draft})
        if self.ctx.set_pr_states is not None and url in self.items():
            self.ctx.set_pr_states({url: "draft" if draft else "open"})
        return {"draft": draft}

    def pr_command(self, verb: list[str], url: str) -> dict | None:
        """`gh pr <verb> -- <url>` run: None once it's done, else {"error"} with why not."""
        if not isinstance(url, str) or api.pr_url(url) != url:   # the whole of it, nothing gh could take as a flag
            raise api.Refused(400, f"not a pull request URL: {url!r}")
        if api.is_mr(url):
            if verb == ["close"]:
                return None if "closed" in (got := self.close_mr(url)) else got
            return self.draft_mr(url, draft=verb == ["ready", "--undo"])
        try:
            code, out, err = self.runner(["gh", "pr", *verb, "--", url])
        except Unavailable as e:
            return {"error": str(e)}
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            return self.lost(url, str(e)) or {"error": str(e) or type(e).__name__}
        if code != 0:
            return self.lost(url, err or out) or {"error": last_line(err or out) or f"gh exited {code}"}
        return None

    def ended(self, url: str, state: str) -> None:
        """A PR merged or closed from here: its row so marked, and its item's status with it."""
        rows = {r["url"]: r for r in self.cache.rows()}
        if url in rows:
            self.cache.update({**rows[url], "state": state})
        if self.ctx.set_pr_states is not None and url in self.items():
            self.ctx.set_pr_states({url: state.lower()})
        self.details.pop(url, None)   # gone from the pane: its detail with it

    def detail(self, url: str, fresh: bool) -> dict:
        """A PR's detail for the Detail pane (#145): its description, history, checks and
        comments, from one `gh api graphql` call, kept DETAIL_EVERY seconds (fresh: once
        DETAIL_GAP old). On a failure, {"error"}, with what was fetched before if any. Only
        for a PR the pane lists (an open one in the cache); the others' kept past
        DETAIL_EVERY are dropped, so the kept details stay few (#156)."""
        if not isinstance(url, str) or api.pr_url(url) != url:
            raise api.Refused(400, f"not a pull request URL: {url!r}")
        if not any(r["url"] == url and status(r) not in ("merged", "closed") for r in self.cache.rows()):
            raise api.Refused(404, f"not a pull request the PRs pane lists: {url}")
        now = self.clock()
        for old in [u for u, (at, _) in self.details.items() if u != url and now - at >= DETAIL_EVERY]:
            del self.details[old]
        at, kept = self.details.get(url, (None, None))
        if at is not None and now - at < (DETAIL_GAP if fresh else DETAIL_EVERY):
            return kept
        try:
            if api.is_mr(url):
                got = mr_detail(url, self.glab)
            else:
                owner, repo, n = api.parse_pr(url)
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
