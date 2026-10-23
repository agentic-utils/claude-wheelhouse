# Modules: stats and design docs, pane swapping, editing docs in place

Issue: #79. Status: approved, stages 1 to 6 landed, stage 7 in progress.

The asks, in Doug's words (Q94): "user needs to be able to edit ADR (or any other doc for
that matter)"; "collapse the tab idea and instead keep a single view. To do that, let's
divide the first column into three and move the statistics module in to the middle
location, immediately under the session list; then re-use the space that used to be
statistics for showing the list of ADRs"; "This entire design piece should be a module, not
part of the core build - as should the statistics piece." (Q107): "just don't try to make
the inbox a module at all - it's too basic to the system as a whole." (Q105): "allow the
user to drag windows to different locations. Then I can experiment with a few layouts and
we can settle on a good default."

Decided before this plan: a fixed grid of slots, where dragging a pane by its title onto
another swaps the two, saved to config, with a reset (D116, no free-form docking); the
editor behaviour in Q106 (below); the inbox stays core (Q107).

## The layout

Seven slots in three columns (six until #111 gave the middle column a third, `middle.middle`, for the PRs pane). A column's slots share its rows by one rule, `columns.py`: weights per slot, a splitter between each pair of neighbouring weighted slots. The splitters stay where they are today and size slots, not
panes, so a swap keeps each slot's size. Default panes:

```
+-------------------+-----------------------+-----------------------+
| left.top          | middle.top            | right                 |
| Sessions          | Inbox                 | Detail                |
|                   |                       | (thread, answer box,  |
+-------------------+                       |  or a doc's viewer    |
| left.middle       +-----------------------+  and its editor)      |
| Stats (module)    | middle.middle         |                       |
+-------------------+ PRs (module)          |                       |
| left.bottom       +-----------------------+                       |
| Session info,     | middle.bottom         |                       |
| buttons           | Decisions (module)    |                       |
+-------------------+-----------------------+-----------------------+
```

- **Core panes**: Sessions (the list), Session info (description, activity and both button
  grids, one pane so the buttons stay with what they act on), Inbox (the item list) and
  Detail. Each gets its `id` from today's widget ids and a title in its top border
  (`border_title`), which is also its drag handle.
- **Module panes** declare a default slot. Stats takes `left.middle`, PRs
  `middle.middle`, Decisions `middle.bottom`.
- **An empty slot collapses**: its column's other slots share the room, as the items table
  takes the whole column today when no module is in `inbox.side`. A column with every slot
  empty collapses too. A module that failed to start still shows its error card in its slot.
- A third module pane with no free slot is not shown, and `claude-wheelhouse modules` says
  so. More slots come with a plan of their own.

## Swapping panes (D116)

- Press on a pane's title (its top border row) and drag: the pane under the pointer gets a
  teal border as the target. Release swaps the two panes' slots. Esc during the drag, or a
  release on the pane's own slot or outside the grid, changes nothing. A plain click on a
  title does nothing, so it never swaps by accident.
- The layout is saved per TUI as `layout.slots` (`{"left.top": "sessions", ...}`, by pane
  id) through the existing `/settings`, which already allows `layout.*`, and read at start,
  like `layout.sort.*` (`.plan/drag-and-sort.md`).
- **Reset layout** in the command palette (Ctrl+P) puts back the default slots and the
  stylesheet's splitter sizes, and clears `layout.slots`.
- A saved layout naming a pane that isn't there (a module uninstalled or disabled) leaves
  that slot empty, so it collapses; the module's return puts it back where it was. A pane
  the saved layout doesn't name goes to its default slot if that is empty, else the first
  empty slot.
- Each pane keeps its own minimum size (the session buttons need 39 columns). A swap that
  puts a wide pane in a narrow slot lets the slot grow to that minimum; the splitter's
  `fit()` already shrinks the others. A column too short for its slots' minimums (#132: session
  info, sessions and a third pane in one column at 80x24) takes the rows it lacks a row at a time
  from the slot with the most above a border and a line (`columns.squeezed`).
- Implementation: the slots are fixed containers in `compose`; a swap moves the two pane
  widgets between them (`Widget.move_child` cannot cross parents, so remove and mount,
  keeping the widget instances so their state survives). The app's `query_one("#items")`
  style lookups are unaffected, as ids don't change. As built (stage 3), a removed widget
  can't be mounted again in Textual 8, so `tui.rehome` moves it across parents as
  `move_child` does within one. Each pane's minimum lives with the pane (`PANE_MINIMUM`), not
  its default slot, and its slot and column grow to it. A swap posts
  `WheelhouseApp.LayoutChanged`. Tab's order is by pane, not place: the session list, the
  inbox, the answer box (or doc), then the buttons in reading order where they now are, then
  the stats controls.

## The module boundary

What exists (`.plan/stats-plugin.md`): entry-point discovery in `claude_wheelhouse.modules`,
`api.Module`, `api.Pane(title, slot, surfaces)`, `api.Context`, the `tick`, `animate` and
`landed` hooks, and error cards. This plan extends it, and bumps `WHEELHOUSE_API` to 2
because slot names change.

A module provides:

| Part | What | Who uses it |
|---|---|---|
| `panes` | `Pane(title, slot, surfaces)`; `slot` is now its default slot, one of the seven | TUI |
| `routes` (new, optional) | `routes(ctx) -> list[Route]`, mounted by the daemon under `/m/<id>/` | daemon |
| `doc_roots` (new, optional) | `doc_roots(ctx) -> list[Path]`: directories whose `.md` files the core viewer may read and write | daemon |
| its own database | `<state dir>/<id>.db` if it needs one, never `wheelhouse.db` (suite architecture) | daemon |
| config | its section of settings, `modules.<id>.*`, which the daemon lets a client write | both |

Core gives a pane, through `Context`, two additions: `call(path, body=None)`, which reaches
the module's own routes through the client (local or remote, as core's queries go), and
`show_doc(path, title)`, which opens a file in the Detail pane's viewer.

**Discovery and switching off.** Installing the package is still all it takes.
`claude-wheelhouse modules` lists what was found (id, version, API, status), and
`claude-wheelhouse modules disable <id>` and `enable <id>` write `modules.disabled` in the
settings table. Daemon and TUI both read it at start: a disabled module mounts no pane and no
routes. A disabled or uninstalled module's slot collapses, as above.

**As built (stage 5).** A route is `api.Route(path, handler, method)`, `{name}` segments as
params, its handler taking an `api.Call(params, query, body)` and answering JSON or raising
`api.Refused(status, error)`; a GET runs as a query, a POST as a command (Idempotency-Key)
on the module's own thread (`daemon.Lane`; GETs on 4 more, `MODULE_GETS`, then 503), never the hub's, each waited on `daemon.MODULE_WAIT` (10 s) and
then answered 504. The fingerprint covers each installed module's entry point, version and
source, so a module reinstalled restarts the daemon. `served.py` holds the routes and doc roots for the daemon and, the same
way, for `LocalClient` with `--local`, so `Context.call` answers alike through either. The
viewer reads a doc root's file through `GET /docs?path=` (403 outside the roots, `have=` as
a doc item's); `docroots.confine` is the allow-list stage 8 writes through. `show_doc`
takes the Detail pane until the selection moves. `fingerprint.API` is 5. `set_doc_dirs`
stores relative directories on the session (`sessions.doc_dirs`, an empty list for
`docs/adr` again), never the git root itself, and none for a session in no git repo; a repo's directories are every one its sessions declared
(`docroots.doc_dirs`, `api.doc_dirs`). `PROTOCOL_VERSION` is not bumped for it yet, as
nothing reads it until the Design module: stage 6 bumps it.

**No module, no harm.** With neither installed, the wheelhouse is sessions, inbox and detail,
as it was before stats. Core never imports a module. Docs the inbox shows (doc items) keep
the viewer and editor, which are core (below).

## Stats, carved out

Stats is already a module in protocol terms, but it ships in the core package. It becomes
its own distribution in this repo:

- `modules/stats/` with its own `pyproject.toml` (`claude-wheelhouse-stats`), declaring
  `stats = "claude_wheelhouse_stats:module"`. Core's `pyproject.toml` drops its entry point.
  `api.OWN` no longer lists core's own modules: it names the `claude_wheelhouse.` prefix of
  core's old entry points, so a stale install's `stats` entry point, which no longer imports,
  gets the "reinstall" card (below).
- **Moves**: `stats_pane.py` whole; from `stats.py` the drawing (`layout`, `render`, `Chart`,
  the gauges, the slider and zoom spans, `cache_lines`, `Totals`, `combine`), with their
  tests.
- **Stays core**, renamed `transcript_usage.py`: the transcript parsing and following
  (`parse`, `UsageFollower`, `follower`, `Snapshot`, `current`, `carry`, `epoch`, `grade`,
  `context_bar`). The hub uses them for the session list's context bars and cache warmth,
  and `plan_usage.py` stays in the hub, as now. `api.py` re-exports what the module needs, so the module imports `api` alone.
  As built (stage 2), that is more than `follower`, `Snapshot` and `current`, because the
  drawing and the pane's workers use them: from `transcript_usage`, `UsageFollower`,
  `follower`, `current`, `read_safely`, `load_safely` (the pane's worker reads), `Snapshot`,
  `Turn`, `History`, `tokens`, `clock`, `grade`, `hexc`, `window_for`, and the constants
  `HOUR`, `DAY`, `WEEK`, `SPAN`, `BUCKET`, `PARTIAL`, `CHARTS`, `SERIES`, `OK`, `WARN`,
  `AMBER`, `HOT`; from `models`, `EFFORTS` and `name` (as `model_label`); from `plan_usage`,
  `AccountUsage` and `EVERY` (as `USAGE_EVERY`).
- `make install` installs core `--with` both modules from the repo, so a normal install
  looks as now. The "reinstall with make install" card for a stale install stays.
- Its slot moves from `inbox.side` to `left.middle`. `stats.render` already lays out to a
  width. Column 1 and the Inbox column both default to 45 wide (Q113), and Detail takes the
  rest; on a narrow terminal the two give way in proportion so Detail keeps 40. At 45,
  Stats compacts a little from its 53-wide layout.

## Design, a new module

`modules/design/` (`claude-wheelhouse-design`), id `design`, one pane: **Decisions**.

- **Where decisions come from (Q114).** By default the daemon side scans `docs/adr/` in the
  git root of each session's working directory, every 10 s and on demand. A session can
  declare other locations instead, with a new MCP tool `set_doc_dirs(paths)`: paths relative
  to its git root, stored on the session and replacing the default for that repo. A file is
  a decision if its name matches `NNNN-*.md`. Its status comes from its frontmatter `Status`,
  else from its filename (`0042-ACCEPTED-slug.md`, the tfu convention), else `?`. These
  directories are its `doc_roots`. Route: `GET /m/design/decisions`. It needs no database,
  because the scan is cheap.
- **The pane.** A table: number, status, title, repo. PROPOSED and UNRESOLVED first, then
  the rest by number, newest first. Header sorts as the inbox's do.
- **Selecting a row** shows the decision in the Detail pane's viewer (`show_doc`), as a doc
  item shows; Enter opens it full screen (`DocView`). The answer box is hidden, as for docs.
- **As built (stage 6).** The pane asks its route every 10 s (its `tick`), and at once when
  the sessions' directories or declared doc dirs change, on a worker thread; the route scans
  on each ask, reading a file again only when its mtime or size has changed, so nothing polls
  the filesystem in between. Besides front matter and the file name, a `Status:` line or a
  `## Status` section (adr-tools' layout) gives the status. The Repo column shows only when
  there is more than one repo, as the 45-column pane is narrow. Moving the cursor shows the
  decision (only while the list has focus, so Tab passing through changes nothing), and
  Enter and a click on the cursor's row open it full screen through a new
  `Context.open_doc`, an optional field, so `WHEELHOUSE_API` stays 2. The list is a Tab
  stop after the inbox's (`TAB_LIST`). The header sort lasts the run: a module's settings
  (`modules.<id>.*`) have no writer yet. `PROTOCOL_VERSION` is 14.
- The rest of the proposal (`~/.claude/scratchpad/t112/proposal.md`: Accept and Reject,
  topics, conformance reports, badges) is out of this plan, and adds to this module later.

## Viewing and editing a doc (Q106)

Core, so it applies to every doc the viewer shows: inbox doc items and decisions, in the
Detail pane and full screen.

**Blocks.** The viewer parses the source with markdown-it (already in the tree through
Rich) and renders each top-level block's source lines (its token `.map`) on its own, at the
viewer's width, into one `Text`, recording for each block its source line range and its
rendered row range. Still one widget, so the speed `doc_blocks` won by rendering as one
block holds; stage 4 measures it on the longest doc in the repo.

**Opening.** A click on the rendered doc finds the block at that row, or the nearest one for a
row between blocks, and opens the editor: a `TextArea` (markdown highlighting) in a lower
split of the viewer, 40% of its height, with a splitter kept as `layout.editor`. The cursor
goes to the block's first source line, and the editor scrolls so that line sits near its
vertical middle. The viewer keeps the clicked block where it was on screen if it still
shows, else scrolls it into view.

**Re-render on Ctrl+Enter, as answers send (Doug, after Q106).** The editor follows the send
modes the answer box already has, and nothing re-renders on a timer. While the editor is open
the viewer renders the editor's buffer, not the file.

- **Queued mode:** Ctrl+Enter re-renders the viewer from the buffer, as it queues an answer.
  Ctrl+S saves, as it sends the queue.
- **Immediate mode:** Ctrl+Enter re-renders and saves in one go. Ctrl+S also saves.
- **Which mode applies:** a doc item uses its session's send mode. A doc opened from the
  Decisions pane belongs to no session, so it behaves as Queued.
- While the editor has focus, Ctrl+S saves the doc and doesn't send the session's queue.
- After each render, the block holding the cursor keeps its row on screen.

**Keeping step, both ways.**

- Viewer to editor: when the viewer scrolls (wheel, keys, scrollbar), the editor scrolls so
  the source of the block at the viewer's vertical middle sits at the editor's middle. Only
  the editor's scroll moves, not its cursor, so typing goes where it was.
- Editor to viewer: when the cursor moves into another block (typing, arrows, a click in
  the editor), the viewer scrolls the least that brings that block into view. Within one
  block nothing moves.
- A click elsewhere in the viewer moves the editor there, as opening does.
- **No feedback loop.** Each programmatic scroll is done with `animate=False` and marked: the
  pane sets `syncing = "editor"` or `"viewer"` before it scrolls and clears it with
  `call_after_refresh`, and each side's scroll handler returns at once while the other side's
  mark is set. The re-render's anchoring scroll is marked the same way. Viewer-to-editor sync
  moves no cursor, so it can never raise a cursor-moved event either.

**Saving and leaving.**

- Ctrl+S saves (and Ctrl+Enter in Immediate mode, above). Esc closes the editor; with unsaved edits it first asks **Save**,
  **Discard** or **Keep editing**. The pane title shows `edited` while there are unsaved
  edits.
- Saving goes through the daemon, which owns the files: `POST /docs/write` with `{path,
  text, mtime_ns}`, `mtime_ns` being the file's when the editor opened (or last saved). The
  daemon writes only a path that is a doc item's file or under a module's `doc_roots`,
  compares the mtime, and writes atomically (temporary file and rename, mode kept). On a
  mismatch it answers 409 with the new mtime, and the TUI asks **Overwrite** (resend with
  `force`), **Reload** (the file's text replaces the buffer, edits lost, said in the
  prompt) or **Cancel**. A save sets the new mtime as the base.
- A file gone at save is a 404: the prompt offers to write it anew or cancel.

## Delivery

Each stage lands, is reviewed and is usable on its own.

1. **Slots.** The six slot containers, pane titles, column 1 in three, `api.SLOTS` renamed,
   `WHEELHOUSE_API` 2, Stats in `left.middle`, `middle.bottom` empty so it collapses.
   Visible: stats under the session list.
2. **Stats out of core.** `transcript_usage.py` split, `modules/stats/` distribution, `make
   install --with`, `api` re-exports. No visible change.
3. **Swapping.** Title drag, target highlight, `layout.slots`, Reset layout, and placement
   for missing and unplaced panes.
4. **Block-mapped viewer.** Per-block rendering with the row and source map, in Detail and
   `DocView`, with a timing test. No visible change.
5. **Module routes, doc roots, `show_doc`, `modules disable`.** Protocol only, tested with a
   test module.
6. **Design module.** The scan, the Decisions pane in `middle.bottom`, viewing in Detail.
7. **Editor.** Open on click, the split, Ctrl+Enter re-render, both syncs and the loop guard,
   Esc. Saving behind it, stage 8, so this stage shows the editor but Ctrl+S says "saving
   comes next".
8. **Saving.** `POST /docs/write`, the path allow-list, the mtime check and its prompts.

Stages 7 and 8 can land as one if review prefers an editor that saves from day one.

## Tests

Pilot tests per stage: the default slots and a collapsing empty slot; a title drag swaps and
survives a restart; Esc cancels; Reset layout. Block map: a click on each kind of block
(heading, list, code fence, table) puts the cursor on its first line; a row between blocks
takes the nearest. Sync: viewer scroll moves the editor and not its cursor; arrowing into the
next block scrolls the viewer; Ctrl+Enter re-renders in Queued and re-renders and saves in Immediate; one sync
causes exactly one scroll on each side. Daemon: write refused outside the allow-list, 409 on a
changed mtime, `force` overwrites, the write is atomic.

## Settled questions

1. **Column widths (Q113).** Column 1 and the Inbox column both default to 45. We'll see how
   that works, and swapping lets either be tried elsewhere.
2. **Where decisions are found (Q114).** `docs/adr/` by default, unless a session declares
   other locations with `set_doc_dirs`. `.plan/` is not scanned unless a session declares
   it.
