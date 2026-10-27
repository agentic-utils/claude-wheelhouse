# PRs: PR items in the inbox, a PRs pane, and colour by item kind

Issue: #111. Status: approved, built (notes on what changed in building are marked "As built").

The ask, in Doug's words: "A pane that lists open PRs. The old claude-dashboard
implementation was slow because it simply polled gh for all open PRs; let's be smarter. PR
should appear in the inbox when created by a session. They should be numbered to match the
repository's PR number ... colour to the inbox text: green for a task, yellow for a
question, purple for a decision, light blue for a PR, white for a document, grey for a
task... use your imagination for other entity types."

Settled with Doug: colour the ref only (Q135); a PR reaches the inbox by detection in the
transcript, with `post_item(kind="pr")` as the fallback, and shows as `PR108` with its repo
(Q136); the pane tracks the sessions' PRs with one batched GraphQL call, and toggles to all
of the person's open PRs (Q137). After review, Doug chose the slot (middle.bottom), cut the
columns to a traffic light, and asked for full titles on hover across the lists.

Three parts, which can land separately in this order: colour and hover (core, small), PR
items (core), the PRs pane (a module).

## 1. Colour by kind, and full titles on hover

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

- One table, `KIND_COLOUR`, keyed by kind, and the ref cell is the only reader. A kind
  missing from it (one an older TUI doesn't know) draws as today. As built, it lives in
  `rows.py` (no Textual), so `api.py` can re-export it to the PRs pane without the daemon
  importing the TUI.
- A doc takes `$foreground`, not a literal white, so it stays visible on a light theme. The
  others are fixed Rich colours, checked for contrast on the default dark and light themes.
- A settled row is dimmed as now, and its ref keeps its colour under the dim, so a settled
  task still reads as a task.
- A child row (`└ A3`, `└ PR108`) colours its ref the same way.
- The cursor row keeps its colours: Textual's cursor background sits behind the ref's colour.

That covers every kind there is. Nothing else is proposed: a new kind adds its line to the
table.

**Hover.** Doug: "Hover over a row to see full title (and add this same hover feature to
inbox and session list)." Resting the pointer on a row shows a tooltip with its full text,
which a narrow column cuts off:

- Inbox: the item's ref and full title (a PR's with its repo).
- Session list: the session's full name and its synopsis.
- PRs pane: the PR's repo, number and full title.

One core helper sets a `DataTable`'s tooltip from the row under the pointer (Textual's
`tooltip`, updated on mouse move by row, so it only changes when the row does), and each
list supplies the text for a row. The PRs pane reaches it through `api.py`'s re-exports.
As built: `rows.RowTips`, a mixin on `DataTable.watch_hover_coordinate`; a list fills
`tips` (row key to text) as it paints.

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
  and the thread. As built, core's Detail shows the title, `owner/repo#n` and the URL as a
  link, with the thread: the branch, checks and review live in the module's `prs.db`,
  which core never reads, and show in the PRs pane. The answer box stays: a message on a PR goes to its session on the
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

### Where it sits

Its default slot is a new one, `middle.middle`, under the inbox: the middle column gains a
third row, as the left has, so there are seven slots (Doug: "just introduce a third row in
the middle column, giving us 7 slots in total, then we have one available for PRs").
Decisions keeps `middle.bottom`, shown as today; nothing is hidden.

- Every column follows one rule (`columns.py`): an empty slot is hidden, a compact one (an
  empty Decisions) keeps its own rows, and the rest share what's left in proportion to
  their weights, each at least its minimum, each line on the row nearest it. A slot with no
  weight kept weighs its default share (the stylesheet's with every slot shown: inbox and
  Decisions 40%, the PRs 20%), so a person who never dragged sees today's layout plus the
  PRs; with a slot compact or hidden, its room goes to the others in proportion (an empty
  Decisions: the inbox twice the PRs).
- A splitter joins each pair of neighbouring weighted slots, sitting just above the lower
  one (so it may lie beside a compact slot between them); a drag sets those two slots'
  weights alone, its line on the pointer's row, every other slot keeping its rows. The
  weights are kept per slot (`layout.weights`); a double-click puts the column's defaults back.
- Upgrading: the splitter shares kept before are deleted, and each column starts from its
  defaults (Doug, review 3: a person who once dragged a splitter drags it again once).
- Weights are in one unit, and being compact or hidden is applied at layout only, never
  written back (review 4, rethought with property tests, `tests/test_columns.py`): a slot
  coming or going changes no weight, the others keep their proportions, and a drag's
  direction holds whatever comes or goes after it.
- A saved `layout.slots` naming six slots loads as it was: PRs take the empty new slot
  (`tui.arrange`'s rule).
- Swapping by a pane's title works across the new slot as across any other.

### One call, cached in the daemon

- A route, `GET /m/prs/list?scope=mine|all&fresh=0|1`, answers from a cache in the module's
  own database (`<state dir>/prs.db`: one row per PR, its JSON and when it was fetched), so
  the pane shows at once on start and every TUI shares one fetch.
- A fetch is one `gh api graphql` call serving both scopes. Each tracked PR (every PR item
  in the store not yet merged or closed) is an aliased
  `repository(owner, name) { pullRequest(number) { ... } }`, and the same query carries
  `search(query: "is:pr is:open author:@me", type: ISSUE, first: 100)` for All. Fields:
  number, title, url, isDraft, state, updatedAt, headRefName, reviewDecision, mergeable,
  and the last commit's `statusCheckRollup.state`. One request, however many repos, against
  a GraphQL budget of 5,000 points an hour.
- It uses `gh`'s own auth, on the machine the daemon runs on, and `@me` is whoever that
  is. No `gh`, or not logged in: the pane says so in a line in place of the table, and
  nothing else changes.
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
  else answers from the cache. A fetch never runs twice at once, and one within 30 s of the
  last is answered from the cache instead (the person's `r` included).

### Mine and All

`m` toggles the scope, shown in the pane's title (`PRs · mine`, `PRs · all`), and kept for
the run as the Decisions sort is.

- **Mine**: the open and draft PRs that are items, the ones the sessions made.
- **All**: every open PR the person authored, in any repo, whether or not a session made it
  (Doug: "all PRs by me, including those that do not relate to any currently-open
  Wheelhouse sessions"). Up to 100, from the search.
- As built (#149): either scope follows the session selected in the session list, as the
  inbox does (Doug: "show items related to selected session, or show all if no session
  selected"). With one selected, its PR items alone (`PRs · mine · <session>`); a PR no
  session made relates to none, so shows only with no session selected, and Request review
  is greyed on it.
- A merged or closed PR leaves the pane at once, in both scopes (Doug: "drop closed PRs
  immediately"). Its inbox item still settles and dims, as in part 2.

### Columns

| Column | Shows |
|---|---|
| PR | `PR108`, light blue |
| Repo | the repo's short name; hidden when every row is in one repo, as Decisions does |
| Title | the rest of the width; the full title on hover (part 1) |
| Light | one `●` in the colour below |

The light, taking the first that applies:

| Light | When |
|---|---|
| red | a check failed, a reviewer requested changes, or the branch conflicts with its base |
| orange, flashing | checks are still running |
| yellow | checks pass, and a review is required and not yet given |
| green | checks pass, approved or no review required, and mergeable: ready to merge |
| grey | a draft |

- No checks at all count as passing. Mergeability GitHub has not yet worked out
  (`UNKNOWN`) holds the light at yellow, so green always means ready; the next fetch
  settles it.
- The orange flash toggles on the module's `animate(frame)` hook, so it rests when the app
  rests its animation (`ANIMATION_FPS`, `WheelhouseApp.pace`), as other animation does.
- Sorted by the light's urgency (red, orange, yellow, green, grey), then most recently
  updated first. A header click sorts, as the inbox's do.
- Selecting a row that is a PR item shows it in Detail (its thread and answer box).
  Selecting any other shows a read-only card: title, URL, branch, checks, review. Enter
  opens the PR in the browser.
- As built (#148): Detail shows the last pick in either list. The inbox and the pane each
  highlight it if they list it, and otherwise hide their highlight (the core's
  `Context.unselect` and the panes' `showing(sid, ref)` hook) until a click, Enter or a
  moving key brings it back on its row. A PR that isn't an item leaves nothing selected,
  Detail saying so, its card under the table as before.

### PR actions in Detail, marks and Close (#145, #146)

As built. Doug: "(a) is a good shout (and we should add more to that detail pane for the
PR: description, history, what's currently failing, comments) but I also want b, and I
want to be able to multiselect PRs and invoke an action for all selected. Also need an
action for Close which should be subject to a confirmation dialog."

- **Detail's parts.** A pane may give the Detail pane widgets of its own (`detail_parts()`,
  api.py): a "body" that scrolls under the thread and a "bar" fixed under that. The PRs
  pane's are the action buttons and the PR's detail, shown while Detail shows a PR it
  lists: a PR item (from `showing`), or a PR no session made, picked in the pane (Detail
  says it isn't an item, and shows its link with the detail). Detail hides them while it
  shows a doc or a session's conversation. No API bump: an older hub never asks.
- **The detail.** One `gh api graphql` call a PR, the route `GET detail?url=`: the
  description, the last 40 timeline entries (commits, comments, reviews, review requests,
  ready, draft, force-push, closed, reopened, merged) and the head commit's checks. The
  daemon keeps it 2 minutes (`DETAIL_EVERY`), `r` fetches again past 10 s (`DETAIL_GAP`);
  the pane asks again when the PR's row changes (pushed, reviewed, checks finished). Shown
  as plain text, control codes stripped, the description cut at 1,500 characters and each
  comment at 600; a failing check is a link only to an http(s) page.
- **Marks.** The inbox's keys and clicks, by URL so they survive a fetch; the pane's
  bottom border counts them. The buttons and a marked row's menu act on every PR marked;
  an unmarked row's menu on that row alone.
- **Close.** `gh pr close -- <url>` through `POST close`, as merge goes: asked first,
  naming each PR. Merge of several asks the same way and merges them one after another.
  Each PR's outcome is a line under the table, and a toast when the table hasn't the
  focus (the action came from Detail).
- **Narrow bars.** Buttons go in a row, then in a row with short captions, then two to a
  line, then one to a line.

### Actions that can't apply, and Draft/Ready (#212)

Doug: "PR details show buttons for actions even when an action isn't sensible (eg merge on
a PR that needs review and doesn't have it yet). Also, no buttons for switching between
draft and ready status."

- **Off, saying why.** The bar under the table, Detail's bar and a row's menu all read
  `Prs.entries`, which gives each action's tooltip too. Merge is off when none of the PRs
  it acts on can merge, by the same `blockers` that word a failed merge: a draft, a review
  required or changes requested, a conflict, failed checks, checks still running.
  Mergeability not yet worked out leaves it on. With several marked and one able to merge
  it stays on, and its question names what blocks each of the rest. Request review is off
  for a draft and for a PR already approved, as well as one no session made; with several
  marked it asks only those it applies to and says which it skipped. Open and Close keep
  their rules. The PR actions have no keys, so there's no key to say it too.
- **Draft/Ready.** One button: Mark ready while any PR it acts on is a draft (acting on the
  drafts only), else Make draft. `gh pr ready` or `gh pr ready --undo` through `POST ready`
  and `POST draft`, not asked first (it's undone as easily), busy while it runs as Merge
  and Close are; the daemon's cached row and the PR item's status change with it.

## 4. Versions and schema

- `PROTOCOL_VERSION`: 17 (#110 took 16). The
  protocol gains the `post_item(kind="pr", url)` line and asks a session to reply on a PR's
  ref when the person writes there. An older session's MCP server knows no `pr` kind, so it
  is shown as needing a relaunch, as with doc items at 13. Detection needs nothing from the
  session, so its PRs arrive regardless.
- `fingerprint.API`: 7 (6 was taken meanwhile). `/items` now returns a kind an older TUI cannot place, so a TUI and
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
5. Status is GitHub's alone; merged and closed settle (dimmed) in the inbox and leave the
   pane at once, and Delete dismisses.
6. Live GitHub state is cached in the module's `prs.db`, and only the status word is written
   back into core, through `Context.set_pr_states`.
7. One fetch serves both scopes: the tracked PRs as aliases plus the `author:@me` search.
   Fetches are deduplicated in the daemon (one in flight, none within 30 s of the last).
8. The light's order: red, then orange (flashing), yellow, green, grey; unknown
   mergeability holds it at yellow.
9. The session list's tooltip shows the full name and the synopsis.
10. `fingerprint.API` goes to 7; `WHEELHOUSE_API` stays 2.

## Open for Doug

Nothing open: the slot, columns, merged PRs and All's scope are settled above.
