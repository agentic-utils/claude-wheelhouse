# GitLab merge requests as PR items

Status: built. Extends `.plan/pr-pane.md`: a GitLab merge request (MR) is a PR item (kind `pr`),
found, posted and polled as a GitHub pull request is, on any host: its URL's shape says it is GitLab's,
so there is no list of hosts to keep (#376).

## Settings

- `GITLAB_CA_CERT` (`WHEELHOUSE_GITLAB_CA_CERT`, `.env`): a PEM file given to the `glab` process alone
  as `SSL_CERT_FILE`. Unset, glab's own per-host `ca_cert` applies. `features.gitlab_ca_cert()`.

## URLs

`https://<host>/<group>[/<subgroup>...]/<project>/-/merge_requests/<n>`. `prs.canonical` (what detection
and `post_item` accept) takes any host and returns the lower-case-host form with no trailing path,
query or fragment. Reading a stored URL (`prs.parse`, `parse_mr`, `repo_of`, `link`, `is_mr`, `forge`) is
structural too. `parse` keeps its `(owner, repo, number)` shape (owner the namespace).

## Refs

An item's ref is `<repo>#PR<n>` (`Store._pr_ref`; `av#PR12`), repo the project's last path segment; the
same name and number from another owner or host takes a letter (`av#PR12b`). Identity stays the canonical
URL (host, full path, number), and the PRs module's `prs.db` is keyed by URL, so it needed no migration.
Opening a database renames each old `PR<n>` item once (`_migrate_pr_refs`, a settings mark), with its
messages and children's `parent_ref`. `Store.resolve_ref` (used by `update_item`, `reply`, `get_input`)
finds an item by a bare `PR<n>` where one PR has that number. A ref has `#` in it: `client.py` quotes refs
in daemon paths. `search.ITEM_ID` links a PR only as `<repo>#PR<n>`. The PRs pane's lines say
`<repo>#PR<n>` (`pane.label`); its table keeps `PR<n>` beside the Repo column. `PROTOCOL_VERSION` 31.

## Detection and post_item

`PrWatcher` also takes a Bash `glab mr create` and a tool named `*create_merge_request*`; the result's
first URL that `canonical` accepts is the MR. Activity cues gain `glab mr merge|close|reopen|update|approve|rebase`.
A merge request URL a session merely mentions makes nothing: only a create's result counts.
`Store.post_pr` accepts either form, and its refusal names both. `PROTOCOL_VERSION` 32 (#376): before it,
a session's MCP server refused a merge request on a host the person hadn't named in `GITLAB_HOSTS`.

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

## Gone

A PR the forge says no longer exists leaves the open set: status `gone` (settled, dimmed, a note on its
thread; `Store.set_pr_states`). Gone is the login's view (a login without access is told the same), so a
gone PR is still asked about once every `fetch.RECHECK` (an hour, and at the first fetch after a start)
and takes its real status again if it answers. GitLab: a 404 for the MR (or its project) counts only if
`glab api user` then succeeds (`mr_login`; any failure there, a 404 included, is a plain failure), so a
refused login, a 5xx, a timeout or a TLS error stay warnings with the last row kept (`fetch.Gone`,
`mr_json`). GitHub: a NOT_FOUND error at `<alias>.pullRequest` with the repository resolved
(`fetch.not_found`), or gh's "Could not resolve to a PullRequest" on Close, Merge or Ready; a repository
that doesn't resolve is a warning, never gone. Close, Merge and Draft/Ready on one say "no longer exists"
and mark it, not show the raw 404.

## Not done

- The All scope (`author:@me`) is GitHub's.
- `authcheck.py` doesn't know glab.
- The detail's notes are one page (no pagination).
