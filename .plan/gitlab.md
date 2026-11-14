# GitLab merge requests as PR items

Status: built. Extends `.plan/pr-pane.md`: a GitLab merge request (MR) is a PR item (kind `pr`),
found, posted and polled as a GitHub pull request is. With no GitLab host named, nothing changes.

## Settings

- `GITLAB_HOSTS` (`WHEELHOUSE_GITLAB_HOSTS`, `.env`, no flag): host names (`host` or `host:port`),
  separated by commas or spaces. Default none. `features.gitlab_hosts()`.
- `GITLAB_CA_CERT` (`WHEELHOUSE_GITLAB_CA_CERT`, `.env`): a PEM file given to the `glab` process alone
  as `SSL_CERT_FILE`. Unset, glab's own per-host `ca_cert` applies. `features.gitlab_ca_cert()`.

## URLs

`https://<host>/<group>[/<subgroup>...]/<project>/-/merge_requests/<n>`. `prs.canonical` (what detection
and `post_item` accept) requires a named host and returns the lower-case-host form with no trailing path,
query or fragment. Reading a stored URL (`prs.parse`, `parse_mr`, `repo_of`, `link`, `is_mr`, `forge`) is
structural and takes any host: the body was vetted when stored, and a `--connect` app may not have the
setting. `parse` keeps its `(owner, repo, number)` shape (owner the namespace). The ref stays `PR<n>`.

## Detection and post_item

`PrWatcher` also takes a Bash `glab mr create` and a tool named `*create_merge_request*`; the result's
first URL that `canonical` accepts is the MR. Activity cues gain `glab mr merge|close|reopen|update|approve|rebase`.
`Store.post_pr` accepts either form; its refusal names the GitLab hosts only when some are named.
`PROTOCOL_VERSION` is not bumped: a session on older code refuses a GitLab URL with a plain message.

## Status (PRs module, `fetch.py`)

One `glab api --hostname <host> projects/<path, one encoded segment>/merge_requests/<n>` per open MR
(4 at once, 30 a fetch, the rest keep their last row), read by `mr_row` into the row GitHub's answer gives,
so `status` and `light` are shared:

| GitLab | row |
|---|---|
| `state` opened, locked / merged / closed | OPEN / MERGED / CLOSED |
| `draft` (else `work_in_progress`) | `draft`, a `Draft:` title prefix cut |
| `head_pipeline.status` success, skipped, manual / failed, canceled / the rest | SUCCESS / FAILURE / PENDING; none: no checks |
| `has_conflicts`, `detailed_merge_status` conflict / mergeable / else (older: `merge_status`) | CONFLICTING / MERGEABLE / UNKNOWN |
| `detailed_merge_status` not_approved / requested_changes | REVIEW_REQUIRED / CHANGES_REQUESTED |

glab holds the login and the token. This process never reads, passes on or logs one: glab gets no
input and `NO_PROMPT`, and its errors are scrubbed of token shapes and token variables' values.

Trouble with GitLab (no glab, not logged in, TLS, HTTP errors) is the pane's `warning`, never the
`error` that blanks the table, and the MR's last cached row stays. A missing or logged-out `gh` is a
warning too while any MR is tracked, so a user with GitLab alone still sees them; with none tracked it is
the error it always was.

Detail (`mr_detail`): the description, the head pipeline as the one check, and the newest 40 notes as
history (system notes' first lines) and comments (people's).

## Actions

Through `glab api`, as the status is. Merge: one GET of the MR, refused in words by `detailed_merge_status`
(`BLOCKED`: a draft, approval, conflict, pipeline running or failed, discussions, rebase...), then
`PUT .../merge` (`squash=true` where the project's `squash_option` is `always`); no branch deletion asked for
and no merge-when-pipeline-succeeds, as the GitHub path has neither. Marked merged only if GitLab answers
`state: merged`. Close: `PUT state_event=close`. Draft and ready: the title's `Draft: ` prefix put on or cut
(`PUT title=`): GitLab 19.5's update call refuses a `draft` field. glab gets `GLAB_NO_PROMPT` (`NO_PROMPT` is
deprecated).

## Not done

- The All scope (`author:@me`) is GitHub's.
- `authcheck.py` doesn't know glab.
- The detail's notes are one page (no pagination).
