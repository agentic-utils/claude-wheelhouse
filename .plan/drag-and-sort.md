# Drag to reorder, and header sort (T114)

The ask, in Doug's words: "drag to reorder sessions and inbox items, and click a column
header to sort."

And on how a drag meets a sort (Q102): "I should be able to drag within a band without
cancelling whatever ordering is in force (so for example I can move items within the
"active" list and that doesn't remove the asterisk from the status column); but I should
also be able to drag an item outside of its band, and if I do that, we should just stop
automatically ordering and just remove the ordering icon from whichever column was
previously driving ordering." And (Q103): "say there are six items, A and B in one status,
C and D in another; E and F in a third; say I drag B above A, then B's status changes to
match C and D: B should move. But if I had dragged B between E and F, then automatic
ordering is disabled and B should *not* move when its status changes to match C."

Decided:

- The inbox Status header cycles `*` (the inbox's own order), `▲`, `▼`. Other columns cycle
  `▲`, `▼`, then back to Status `*`. The marker shows only on the column in force (D97).
- Status `▲` sorts by urgency: blocked, waiting, open, then what waits on the session
  (answered, processing, unseen, seen, and any status not named), running, todo, then the
  settled and finished; `▼` the reverse; ties newest first (Q104), the settled and
  finished as `*` has them (the latest to settle lowest).
- The session list's dot column has one sort, `*`: running first, then the latest
  activity. A second click goes back to the person's order. Its other headers keep their
  click: every session's items, as Esc (D109).
- Children (agents and docs under an item, `.plan/docs-in-inbox.md`) are never dragged on
  their own: they move with their parent (D109).
- The sort is kept per TUI under `layout.sort.*`; the person's order is in the DB (D109).
- Alt+Up and Alt+Down move the highlighted row (D109).

## Groups, and the two kinds of drag

A **group** is the rows that share the sort's key. Under Status `*` that is today's
status tiers (`store.RANK`, by `inbox_rank`'s first element); under a column sort, the rows
with equal values in that column. The active, settled and finished bands are part of
every sort's key, so a group never spans two bands. Each group has a token
(`<spec>|<band>|<value>`), and a row is **placed** while its `pos_group` is its group's token.

- **A drag within its group** (a neighbour where it lands is of its group) keeps the sort.
  Every row of the group (all sessions', not only those in view) is written with the
  group's token and its place in the new order. Inside a group, rows not placed come
  first, in the sort's own order; then the placed, by `pos`. So a row whose key changes
  (B's status now C's) lands at the top of its new group, unless it was placed in that
  group before and the token still matches.
- **A drag across groups** turns ordering off: the marker goes, and every row is written
  with `manual` and its place in the order as it now shows. With ordering off there is one
  band: nothing moves by itself, not a status change, not settling or finishing (a finished
  row still hides until F). Rows not placed (new ones) come first. A header click imposes
  a sort again; the `manual` places then count for nothing until ordering is off again.
- A drop the daemon doesn't write (refused, unreachable) changes nothing: the sort,
  its marker and every row stay as they were. Ordering goes off only once the places are
  written. Every row a drop writes takes its new place at once, the selected one too.

The session list works the same way, and starts with its `*` on (Q102's example assumes
the asterisk shows). Under `*` the key is (parked, not running), then `last_at` newest
first; a drag between a running session and the others turns it off. With it off and
nobody placed yet the order is creation order, unparked then parked.

## Code

`ordering.py` holds the rules, pure (no Textual, no store): the specs and their cycles
(`next_item_sort`, `next_session_sort`, `marker`), each row's key and token (`item_key`,
`session_key`), what a drop writes (`drop`) and what the Sink may lay at the foot
(`sunk_keys`: the settled rows only while they all come last).

The TUI: `ranked_keyed` sorts by `ordering.item_key` in place of `inbox_rank`, and the
selected row's pin holds that key; a sort change lets the pin go, as it was held in the
old sort's terms. `paint_items` fills the bands from the key's first element.
`paint_sessions` sorts by `ordering.session_key`. `drop_row` takes a drop, works out the
whole list's order (all items through `client.all_items`, so a drag in one session's view
lands next to its visible neighbour in the global order), and writes it.

## Store, hub, daemon and compatibility

Through `ADDED_COLUMNS`, all nullable, no backfill: `items.pos` REAL, `items.pos_group`
TEXT, `sessions.pos` REAL, `sessions.pos_group` TEXT. NULL is "not placed".

- `Store.set_item_order(group, keys)` and `Store.set_session_order(group, sids)`: `pos = i`,
  `pos_group = group` for each, one transaction. `updated_at` stays (it drives the ranking).
  Item writes fire the `marks` triggers, so every client repaints.
- `Store.sessions` gains `last_at`, the session's own latest activity: the latest of
  `created_at`, `launched_at`, its newest message of its own (author claude), its newest
  item and its latest word on any item (`session_at`). Not the person's acts (a decision
  seen, an answer queued: `updated_at`), so those never re-sort the list, and not
  `heartbeat_at`, which moves every few seconds. It runs each tick, so `INDEXES` adds
  `messages (session_id, author, created_at)`, `items (session_id, session_at)`, `items
  (session_id, created_at)` and, for the drafts count beside it, `messages (session_id)
  WHERE draft = 1`: 0.6 ms a call with 300k messages, from 1.9 s.
- Under `*` the selected session is pinned (D113), as the selected item is: it keeps the
  key it was shown with while it stays selected, and settles into its place once the
  selection leaves it. Park and Unpark, and a drop, move it at once.
- Hub `reorder_items` and `reorder_sessions` return `Done`; daemon `POST /order/items`
  (`{group, keys: [[sid, ref], ...]}`) and `POST /order/sessions` (`{group, sids}`), not
  under `/sessions/`, where `{sid}/{action}` lives. The remote client holds the new places
  in its cache once written, so the list doesn't flick back until the next fetch.
- `PROTOCOL_VERSION` stays 13: it is what sessions' MCP servers speak, and nothing here
  changes it. `fingerprint.API` stays 2: the routes and keys are additive. A daemon from
  this package on older code is restarted anyway (`connect.local`). One from another
  package on the same API serves rows without the keys, read as not placed
  (`ordering.field`), and answers 404 on `/order/*`: the TUI says the daemon runs older
  code that can't save the order and that a restart fixes it, and changes nothing.
- Older code on the same DB never reads the columns, and its inserts leave them NULL: not
  placed, which is the newcomer rule.

## Several TUIs on one DB

The order is in the DB, once, for every TUI: through the daemon, or a `--local` one on the
same file. A drag in one shows in the others at their next refresh. Two drags at once:
the last write wins. The sort is per TUI, kept under `layout.sort.items` and
`layout.sort.sessions` through the existing `/settings` (the daemon allows `layout.*`
only) and read once at start, so one TUI's sort never flips another's live. With ordering
off in one TUI and a sort in another, both read the same `pos`: `manual` places count in
the first, group places in the second.

## Sort keys and ties

| list | column | key, `▲` | ties |
|---|---|---|---|
| inbox | status `*` | `inbox_rank`'s tier | `inbox_rank` (newest first; settled oldest first) |
| inbox | status `▲` | urgency (above) | newest first; settled and finished as `*` |
| inbox | session | name casefolded, then id | `inbox_rank` |
| inbox | ref | (prefix, number): `T2` before `T10` | `inbox_rank` |
| inbox | title | casefolded | `inbox_rank` |
| sessions | dot `*` | (parked, not running) | `last_at` newest first, then creation order |

`▼` reverses the column's key only. Within a group the person's places come after the
rows not placed, as above. Grouped subagents (no parent) keep to their band's foot, by
session, as before, and are not dragged.

## Header clicks and markers

`DataTable` posts `HeaderSelected` for a click on the header; the app's `sort_by` takes
it. The inbox's columns have keys, and the column in force carries its marker in its
label (`status *`, `title ▲`): a change rebuilds the columns, as a filter does. The
session list's dot column shows `*` or nothing; `SessionList._on_click` lets a click on
that header through, and keeps "every session's items" for the rest.

## The drag

On `SinkList`, both lists:

1. `_on_mouse_down` on a draggable row (an inbox row with no parent, any real session row;
   not a child, a grouped subagent, a gap, the `+ / -` toggle, nor with Ctrl or Shift)
   records the press. A plain click stays a click.
2. `_on_mouse_move` with the button held, off the pressed row, starts the drag and
   captures the mouse. A unit is the row and its children (`followers`).
3. The pointer's row gives the target: the unit there takes the dragged one's place
   (before it when moving up, after it when moving down); a row no drag moves counts as
   the unit above it.
4. The drop line: an underline on the row above the gap, or an overline on the first row.
   One-line rows leave no room for anything between them.
5. With the pointer on the header or above, or on the last line or below, the list
   scrolls a row each 50 ms and retargets.
6. `_on_mouse_up` drops (`WheelhouseApp.drop_row`). The click Textual makes of the release
   selects nothing (`swallowed`). A move with no button held (a release lost outside the
   terminal) ends the drag with nothing moved. Esc during a drag puts it back
   (`take_key`, before any binding).

Alt+Up and Alt+Down move the highlighted unit past its neighbour through the same
`drop_row`, so a move past the edge of its group turns ordering off too.

## Tests

- `tests/test_ordering.py`: the cycles; each sort's order, ties and bands; placed rows
  after newcomers; a place from another group counting for nothing; `drop` within, at a
  group's foot, across and in place; Q103's B, moved within and then changing status;
  sessions under `*` and off; `sunk_keys`.
- `tests/test_store.py`: the order writes leave `updated_at`; `last_at`.
- `tests/test_daemon.py`: both routes write, and refuse bad bodies.
- `tests/test_tui.py` (Pilot, `MouseDown`, `MouseMove` with the button, `MouseUp`): a drag
  within a group keeps `status *` and the click after it selects nothing; one across turns
  ordering off, and a status change then moves nothing (both runners, through the daemon
  too); Esc cancels; Alt+Up and Alt+Down; the header cycles and the sort remembered at the
  next start; the session list's `*`, a drag within its group, and one across.

## The order a drop works from

A drop works from the order as it shows, the selected row where its pin holds it, and
judges within or across by the dragged row's neighbours where it lands, as shown. Working
from the order the pin would give up (where a selected row would go once let go) made a
swap of two rows in one group look like a move across: the watched session starting to
run, or the selected item changing status, sat pinned among its old group but counted in
its new one, so the drop turned ordering off and the list jumped. Rows out of view (other
sessions' items, in one session's view) take their places from the whole order.
