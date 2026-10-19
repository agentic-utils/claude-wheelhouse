# PRs: PR items in the inbox, a PRs pane, and colour by item kind

Issue: #111. Status: design, for Doug's review.

The ask, in Doug's words: "A pane that lists open PRs. The old claude-dashboard
implementation was slow because it simply polled gh for all open PRs; let's be smarter. PR
should appear in the inbox when created by a session. They should be numbered to match the
repository's PR number ... colour to the inbox text: green for a task, yellow for a
question, purple for a decision, light blue for a PR, white for a document, grey for a
task... use your imagination for other entity types."

Settled with Doug: colour the ref only (Q135); a PR reaches the inbox by detection in the
transcript, with `post_item(kind="pr")` as the fallback, and shows as `PR108` with its repo
(Q136); the pane tracks the sessions' PRs with one batched GraphQL call, and toggles to all
open PRs (Q137).

Three parts, which can land separately in this order: colour (core, small), PR items (core),
the PRs pane (a module).

## 1. Colour by kind

The ref cell of each inbox row takes its kind's colour. The title, session and status keep
today's styles, so the row still reads by status and the ref says what it is at a glance.

| Kind | Ref | Colour |
|---|---|---|
| task | T | green |
| question | Q | yellow |
| decision | D | purple |
| pr | PR | light blue |
| doc | M | the theme's foreground (white on a dark theme) |
| agent | A | grey |
| permission | P | orange |

- One table, `KIND_COLOUR` in `tui.py`, keyed by kind, and the ref cell is the only reader.
  A kind missing from it (one an older TUI doesn't know) draws as today.
- A doc takes `$foreground`, not a literal white, so it stays visible on a light theme. The
  others are fixed Rich colours, checked for contrast on the default dark and light themes.
- A settled row is dimmed as now, and its ref keeps its colour under the dim, so a settled
  task still reads as a task.
- A child row (`└ A3`, `└ PR108`) colours its ref the same way.
- The cursor row keeps its colours: Textual's cursor background sits behind the ref's colour.

That covers every kind there is. Nothing else is proposed: a new kind adds its line to the
table.

## 2. PR items

A PR is an item of kind `pr`. `store.KINDS` gains `"pr": "PR"`. Its ref is not counted per
session as other refs are: it is `PR` and the repository's PR number, `PR108`.

### Identity and uniqueness

- The item's body holds the PR's canonical URL, `https://github.com/<owner>/<repo>/pull/<n>`,
  as a doc's body holds its path. Repo and number are read from it, so no new columns are
  needed for identity.
- Within a session, the URL is the item: detecting or posting the same URL again updates
  that row (title, parent if given), never a second row.
- Refs stay unique per session (`UNIQUE (session_id, ref)`), as today. A session that
  creates PR108 in two repos gets `PR108` for the first and `PR108b` for the second (then
  `c`, and so on). The repo shown on the row tells them apart; the suffix only keeps the
  constraint. This is rare enough not to justify rebuilding the table.
- Across sessions, refs never collide, as now. Detection skips a URL that is already an
  item in any session, so a PR shows once even when a second session pushes to it. A
  session that posts it explicitly gets its own item.

### How a PR arrives

**Detection (core, in the hub).** The hub already reads each listed session's main
transcript for its subagents (`subagents.AgentWatcher.read_main`, incremental from an
offset, a byte filter before any JSON parse). A PR watcher, `prs.PrWatcher`, runs in the
same worker (`Hub.sync_agents`) with its own offset:

- A tool call that creates a PR: a Bash `tool_use` whose command contains `gh pr create`,
  or any tool named `*create_pull_request*` (the GitHub MCP server). Byte filter:
  `pr create` or `create_pull_request`. Its tool_use id is remembered.
- Its tool_result, matched by that id, yields the URL: the first
  `https://github.com/<owner>/<repo>/pull/<n>` in the result text (gh prints it last; gh's
  "a pull request for branch X already exists: <url>" counts too, since the session made
  it). An error with no URL creates nothing.
- The title comes from the command's `--title` when it parses, else `PR <n>` until the
  pane's first refresh fills it in.
- Subagents create many of the PRs, so the watcher also reads each subagent's transcript
  (`agent-<id>.jsonl`, already listed by `AgentWatcher`), incrementally, with the same
  filter. The PR belongs to the session, and its parent is the subagent's parent item, so it
  shows under the task it was made for.
- A PR made by the main session has no parent unless the session posts it with one
  (below), and sits in the session's group, as an orphaned doc does.
- The cut-off follows the subagents' rule: the later of when the session joined and when
  this code first ran (`store.PRS_SINCE`). The first read starts there (via
  `transcript_usage.window_start`), not at the end, so a PR made while the wheelhouse was
  down still arrives. After that, reads are incremental.
- `gh pr create --web` prints no URL and is missed: that is what the fallback is for.

**Fallback (MCP).** `post_item(kind="pr", title, url, parent=None)`: the URL must be a
GitHub pull URL, else it is refused, saying why. The protocol asks a session to post a PR it
made some other way (the web, another tool), and to use it with `parent` to file a detected
PR under its task. Posting a URL already detected in the session updates that item.

### What the row shows

- Ref `PR108` in light blue. The title is prefixed with the repo's short name, dimmed:
  `claude-wheelhouse · Inbox pins`. The owner is left off: it is the same for nearly every
  repo a person works in, and the pane shows it.
- Status words: `draft`, `open`, `merged`, `closed`. GitHub sets them, never the session or
  the person: `update_item` on a PR refuses a status, saying it comes from GitHub. A
  session may still change its title or parent.
- The Detail pane shows the title, URL (a terminal link), branch, checks, review decision
  and the thread. The answer box stays: a message on a PR goes to its session on the
  PR's ref, as on any item ("fix the failing lint"). As with docs (protocol 15), the
  session is asked to reply there.

### Merged and closed

- `draft` and `open` are active, ranked with the urgency of a running task
  (`ordering.URGENCY`).
- `merged` and `closed` are settled: dimmed and kept in sight, as a done task is.
- Delete dismisses a PR item whatever its state (`DISMISSABLE` gains `pr`). It never touches
  the PR on GitHub. Delete on an open PR asks first, saying the PR stays open on GitHub.
- A PR with no session left to refresh it still settles: the pane's refresh covers every
  open PR item, not just live sessions'.

### Where it meets pins (#110)

#110 (in `.worktrees/110-pins`) adds `items.pinned` and `post_item(pinned=)`, pins docs by
default, and keeps a pinned child when its parent is deleted. PR items take part with no
special case:

- A PR item is unpinned by default (disposable, like a task). The person can pin it in the
  pin column, and the fallback `post_item(kind="pr", pinned=true)` takes the flag as any
  kind does.
- A pinned PR stays through a parent's Delete and the multi-select Delete, as #110 has it.
  A merged, pinned PR stays dimmed until its own Delete.
- In a parent's Delete Confirm, an unpinned open PR child counts as open, worded "still
  open on GitHub".
- Shared ground: `store.family` and the Confirm's counts, `DISMISSABLE`, `_insert_item`
  (the PR's ref comes from its number, not the counter), `post_item`'s signature, the
  protocol text, and `PROTOCOL_VERSION`. Whichever lands second rebases onto the other.

## 3. The PRs pane (module)

`modules/prs/` (`claude-wheelhouse-prs`), id `prs`, one pane: **PRs**. Like Decisions, it
follows the module boundary in `.plan/modules.md`: core never imports it, and without it
PR items still arrive and show, but stay at the status they were posted with.

### One call, cached in the daemon

- A route, `GET /m/prs/list?scope=mine|all&fresh=0|1`, answers from a cache in the module's
  own database (`<state dir>/prs.db`: one row per PR, its JSON and when it was fetched), so
  the pane shows at once on start and every TUI shares one fetch.
- A fetch is one `gh api graphql` call. Each tracked PR is an aliased
  `repository(owner, name) { pullRequest(number) { ... } }`; in `all` scope each repo adds
  `pullRequests(states: OPEN, first: 50, orderBy: UPDATED_AT DESC)`. Fields: number, title,
  url, isDraft, state, mergedAt, closedAt, createdAt, updatedAt, headRefName, author,
  reviewDecision, and the last commit's `statusCheckRollup.state`. Any number of repos and
  PRs is one request, against a GraphQL budget of 5,000 points an hour.
- It uses `gh`'s own auth, on the machine the daemon runs on. No `gh`, or not logged in:
  the pane says so in a line in place of the table, and nothing else changes.
- After a fetch, the module sets each PR item's status through a new optional command,
  `Context.set_pr_states({url: status})`. `Context` stays read-only otherwise, and as the
  field is optional, `WHEELHOUSE_API` stays 2 (as `open_doc` did).

### When it fetches

- Every 5 minutes, while any TUI is running.
- When the pane is shown (TUI start, or swapped into a slot), with `fresh=1`.
- On a push: the PR watcher also notes a Bash `git push`, `gh pr merge`, `gh pr ready` or
  `gh pr close` in any session's transcript (main or subagent), and sets
  `Hub.pr_activity_at`, exposed as `Context.pr_activity` (optional). A fetch follows 20
  seconds after the latest one, so GitHub has registered the push and started its checks.
  Pushes within that window share one fetch.
- `r` in the pane fetches at once.
- The pane asks its route every 15 s; the route fetches only when one of the above is due,
  else answers from the cache. A fetch never runs twice at once, and a fetch within 30 s of
  the last is refused (the person's `r` included) and answered from the cache.

### Mine and all

`m` toggles the scope, shown in the pane's title (`PRs · mine`, `PRs · all`), and kept for
the run as the Decisions sort is.

- **Mine**: every PR item in the store, open or draft, plus those merged or closed in the
  last 24 hours, dimmed, so a merge is seen to land.
- **All**: every open PR in the repos the wheelhouse knows: the GitHub remote (`origin`) of
  each session's git root (sessions not ended, live or parked), plus the repos of open PR
  items. Read once per session cwd and cached. Up to 50 per repo, newest update first. PRs
  that are items carry their session; the rest show their author.

### Columns (for Doug to confirm)

| Column | Shows |
|---|---|
| Repo | short name; hidden when every row is in one repo, as Decisions does |
| PR | `PR108`, light blue |
| Title | the rest of the width |
| State | `draft`, `open`, `merged`, `closed` |
| Checks | `✓` passing, `✗` failing, `●` pending, blank for none |
| Review | `approved`, `changes`, `required`, blank for none |
| Age | since last updated on GitHub: `4m`, `3h`, `2d` |
| Session | the session's name; in `all`, the author's login, dimmed, for PRs no session made |

Sorted by Age, newest first; header click sorts, as the inbox does. Selecting a row that is
a PR item shows it in Detail (its thread and answer box). Selecting any other shows a
read-only card: title, URL, branch, checks, review. Enter opens the PR in the browser.

## 4. Versions and schema

- `PROTOCOL_VERSION`: the next number, 17 if #110 (which takes 16) lands first. The
  protocol gains the `post_item(kind="pr", url)` line and asks a session to reply on a PR's
  ref when the person writes there. An older session's MCP server knows no `pr` kind, so it
  is shown as needing a relaunch, as with doc items at 13. Detection needs nothing from the
  session, so its PRs arrive regardless.
- `fingerprint.API`: 6. `/items` now returns a kind an older TUI cannot place, so a TUI and
  daemon from either side of this must not pair.
- `ADDED_COLUMNS`: none for core. The URL lives in `body`; GitHub's state lives in
  `prs.db`.
- `store.PRS_SINCE`: a settings row written the first time this code runs, as
  `AGENTS_SINCE` is.

## Decided

1. A PR's identity is its canonical URL in the item body, matched as a doc's path is.
2. A second same-numbered PR in one session takes `PR108b`; the repo on the row
   distinguishes them.
3. Detection reads subagent transcripts too, and files a subagent's PR under the
   subagent's parent item.
4. Detection's first read goes back to the cut-off, not the end, so restarts miss nothing.
5. Status is GitHub's alone; merged and closed settle (dimmed), Delete dismisses.
6. Live GitHub state is cached in the module's `prs.db`, and only the status word is written
   back into core, through `Context.set_pr_states`.
7. Fetches are deduplicated in the daemon (one in flight, none within 30 s of the last).
8. Repos for "all" come from each session's `origin` remote, plus open PR items' repos.
9. `fingerprint.API` goes to 6; `WHEELHOUSE_API` stays 2.

## Open for Doug

1. **Where the pane goes.** All six slots are taken (Sessions, Stats, Session info, Inbox,
   Decisions, Detail), and a pane with no free slot isn't shown. Options:
   (a) a seventh slot, `right.bottom`, splitting the Detail column (widest, so all eight
   columns fit; Detail loses height); (b) give the PRs pane `middle.bottom` and Decisions
   none until swapped in; (c) tabs within one slot, shared with Decisions. I recommend (a).
2. **The columns** above: keep, cut or add (for example, the head branch, or comment
   count).
3. **Mine keeps merged PRs for 24 hours**, dimmed. Longer, shorter, or drop them at once?
4. **All shows up to 50 open PRs per repo** from everyone, bots included. Filter out bots
   (Dependabot, Renovate) by default?
