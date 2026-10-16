# Drag to reorder, and header sort (T114, design)

The ask, in Doug's words: "drag to reorder sessions and inbox items, and click a column
header to sort."

Decided before this doc:

- Q98 A: a drag reorders within its band (active, settled, finished). Children (agents and
  docs nested under an item, `.plan/docs-in-inbox.md`) move with their parent. The manual
  order is persisted in the DB.
- The inbox Status header cycles `*` (current behaviour), `▲` ascending, `▼` descending.
  Other columns cycle `▲`, `▼`, off. The marker shows only on the active column (D97).
- The session list's status-dot column has one sort state, shown as `*`: active first,
  then the most recent activity. A second click goes back to the manual order.
- Drag is off while a column sort is on.

## What each list's order is

**Inbox.** Today `ranked_keyed` sorts by `store.inbox_rank`, and `paint_items` splits the
result into the three bands by `band(rank)`. The `*` state keeps the bands exactly as they
are and orders within each band by the manual order, which is seeded from today's ranking.
Until the person drags, `*` is identical to today. So `*` is the inbox's manual view, and
drag works in it (Q1 below: the decisions read as if `*` is a sort, which would leave the
inbox with no draggable state at all).

**Sessions.** Today: unparked first, then parked (the Sink keeps those at the foot), each in
creation order. Its bands are those two. The manual order replaces creation order within
each; with no drag yet it is creation order, as today. The `*` sort is a view over it.

## Store

Three columns through `ADDED_COLUMNS`, all nullable, no backfill:

| column | type | meaning |
|---|---|---|
| `items.pos` | REAL | place in the manual order, lower first |
| `items.pos_band` | INTEGER | the band (0, 1, 2) `pos` was set in |
| `sessions.pos` | REAL | place in the manual order, lower first |

NULL means "unplaced": no drag has touched it since it arrived. Writing `pos` never touches
`updated_at` (it drives the ranking and "newest"), so a drag can't reshuffle anything else.
`items` writes fire the `marks` triggers, so every client sees a drag on its next refresh.

Two store calls, each one transaction:

- `set_item_order(band, keys)`: `pos = i`, `pos_band = band` for each (sid, ref) in `keys`.
- `set_session_order(sids)`: `pos = i` for each.

The client always sends the whole band in its new order, renumbered from 0, never a
midpoint between neighbours. Bands are tens of rows, so the write is cheap, there is no
precision to run out of, and an unplaced band is materialised by the same call that moves
the row. The client can build the whole band because `/inbox` returns every item and the
client filters (daemon `inbox`), so a drag in one session's filtered view still sends the
full band: the moved row lands just above (or below) its visible neighbour in the global
order, and other sessions' rows between keep their places.

## How new items slot in

An item is placed in its band only while `pos_band` equals its current band. A NULL `pos`,
or one set in another band (it settled, reopened, finished), counts as unplaced. Within a
band, unplaced items take the place today's ranking gives the newest arrival, which is also
where they would show today:

- active: above every placed item, by `inbox_rank` among themselves (newest, most urgent first)
- settled and finished: below every placed item, by `inbox_rank` (the latest to settle lowest)

So the sort key within a band is `(0, rank)` for unplaced active, `(1, pos)` for placed,
`(2, rank)` for unplaced settled or finished. Nothing writes on a band change: the band is
the client's reading (`standing` takes `busy`), and an MCP server on older code writes
items with no idea of `pos`. Both fall out of the rule for free.

Sessions: a new session (NULL `pos`) goes to the foot of the unparked band, as today. Park
and Unpark keep `pos`, so a session returns to its old place among its band, as creation
order does today. Sort key `(parked, pos is NULL, pos, created_at, rowid)`.

The trade-off (Q2): once a band is placed, an item's status change inside the active band
(running to blocked, a question gaining a reply) no longer moves it. A question that opens
is new (top) and one reopened changes band (top), so the urgent cases still surface.

## Hub, daemon and compatibility

- Hub: `reorder_items(band, keys)` and `reorder_sessions(sids)`, each returning `Done`.
  Daemon routes `POST /order/items` and `POST /order/sessions` (not under `/sessions/`,
  where `{sid}/{action}` already lives). Client calls them through `command`, not
  `quietly`: a refused write must snap the row back and say so.
- `Store.sessions` gains `last_at` (below) as one more subquery. Item rows carry
  `pos` and `pos_band` with no change: the daemon serves `i.*` less `UNLISTED`.
- `PROTOCOL_VERSION` stays 13. It is what sessions' MCP servers speak; nothing here changes
  the protocol, and a bump would flag every session as needing a relaunch for nothing.
- `fingerprint.API` stays 2. The routes and keys are additive. A daemon from this package
  on older code is restarted by `connect.local` anyway. One from another package on the
  same API is joined: its rows lack the keys, so the TUI reads them the way `parent_of`
  reads `parent_ref` (missing means NULL, every row unplaced, today's order), and its 404
  on `/order/*` comes back as `Refused`: the row snaps back with "the daemon runs older
  code: restart it to reorder". `last_at` missing falls back to `created_at`.
- Older code on the same DB (session MCP servers, an older daemon) never reads the new
  columns and inserts leave them NULL, which is the newcomer rule.

## Several TUIs on one DB

The order lives in the DB, once, for everyone: local and remote TUIs through the daemon,
and a `--local` TUI with its own hub on the same file. A drag in one shows in the others on
their next refresh. Two drags on one band at once: the last whole-band write wins, and the
loser's row moves on its next refresh. No locking; it's the person against themselves.

The sort state (which column, which direction) is a view choice, per TUI. Lean (Q3): keep
it under `layout.sort.items` and `layout.sort.sessions` through the existing `/settings`,
which the daemon already restricts to `layout.*`. Each TUI reads layout once at start, so a
sort on the laptop never flips the desk's view live; it only seeds the next start.

## Sort keys and ties

Column sorts keep the bands (active, settled, finished, and unparked, parked) and sort
within each, so a sort never buries an open question under settled rows, and the Sink and
the dimming stay as they are. Children always follow their parent; agents grouped by
session stay at their band's foot (`item_rows`). Every key ends with the `*` key (manual
order), so ties are stable and match what the person had.

| list | column | `▲` key | notes |
|---|---|---|---|
| inbox | status | shown status text, casefolded | `*` is the manual order (above); Q4 |
| inbox | session | session name casefolded, then id | only shown unfiltered (D31) |
| inbox | ref | (prefix letter, number): `T2` before `T10` | |
| inbox | title | casefolded title | |
| sessions | dot | not active first, then `last_at` newest first | one state, `*` |

`▼` reverses the column's part of the key only; the tie-break stays in manual order.
Status `▲` sorts the status as shown (queued, processing and the hourglass included), since
that's what the person reads.

Sessions' `last_at`: the latest of the session's newest message `created_at`, newest item
`updated_at`, `launched_at` and `created_at`. Not `heartbeat_at`, which moves every few
seconds for any live session. "Active" is `hub.state(sid) == "active"` (running). Parked
sessions stay in their band at the foot under `*`, as the buttons below need them there.

## Header clicks and markers

`DataTable` posts `HeaderSelected` for a click on row -1. Each list handles it on the app:

- Inbox: same column cycles its states; another column starts at `▲`, and Status goes
  back to plain. Off (or a click on Status from `▼`) returns to `*` on Status.
- Sessions: the dot column toggles `*` and manual. Today any click on the session list's
  header unselects (`SessionList._on_click`, "as Esc"). That stays for every other column;
  a click on the dot column's header sorts instead.

Columns get keys (`add_column(label, key=...)`) so the marker can live in the label:
`status *`, `title ▲`. `paint_items` today compares labels to decide a rebuild; it compares
keys instead, and a sort change updates the label in place and repaints.

## Drag in a DataTable

Both lists are `SinkList`s; the drag lives there, parameterised by how each list finds its
bands and its draggable units.

1. **Press.** `_on_mouse_down` on a real row (meta `row` >= 0, not `BLANK`, not the `+ / -`
   toggle, no Ctrl or Shift) with no sort on records the row's key and the screen y. No
   capture yet: a plain click must stay a click.
2. **Start.** `_on_mouse_move` with the button held and the pointer one row or more from
   the press starts the drag: `capture_mouse()`, so moves and the release reach the table
   wherever the pointer goes. A unit is a top-level row with its children; a drag that
   starts on a child or a grouped agent row starts nothing (Q5).
3. **Target.** Each move maps `event.y` to a content row (`y - header_height + scroll_y`),
   then to a slot among the band's units. Outside the band, above or below, it is clamped
   to the band's first or last slot: the row stops at the band's edge, which shows where it
   would go. Only a change of slot repaints.
4. **Indicator.** A live preview: the list repaints in the provisional order (a
   `drag_order` the paint uses in place of the stored one), the unit moving with the
   pointer in the cursor's colour. One-line rows leave no gap to draw a line in, and an
   underline on the row above is easy to miss. Refresh ticks during the drag paint the new
   data in the provisional order, so nothing jumps under the pointer.
5. **Scrolling.** With the pointer on the first or last visible row, or past it (capture
   still reports it), a 20 Hz timer scrolls one row per tick and retargets. It stops when
   the pointer leaves the edge, or on release.
6. **Drop.** `_on_mouse_up` releases the capture, sends the band's order, and keeps the
   preview until the next paint reads the stored order. A drop in its own slot sends
   nothing. Textual sends a `Click` after the release; the next `_on_click` sees the
   drag's flag and does nothing, so a drag never selects or opens a row.
7. **Cancel.** Esc during a drag, or a refresh in which the dragged item went, drops the
   preview and sends nothing.

A press with a sort on starts nothing; past the one-row threshold it says once: "Sorted by
title: click its header until the sort is off to drag."

The selected item's pin (`ranked_keyed`) carries on: it holds the item's full sort key as
shown, so its own status change still doesn't move it under the person. A drag is the
person's explicit act, so the moved item joins `repin`, as Delete's does.

## Keyboard

Cheap, as it reuses the drop: Alt+Up and Alt+Down on either list move the highlighted unit
one slot within its band (nothing at the band's edge), with the same write. No binding
uses Alt today. The header sorts get no keys in this slice.

## Test plan

Store (`tests/test_store.py`):
- the new columns arrive on an old DB (`ADDED_COLUMNS`), NULL, and old rows still read
- `set_item_order` and `set_session_order` renumber and leave `updated_at` alone
- `last_at` takes the latest of its four sources

Pure ordering (no Textual), one parameterised table, one case per line:
- unplaced active above placed, unplaced settled and finished below
- `pos_band` from another band counts as unplaced
- children follow their parent after a move; grouped agents stay at the foot
- a move in a filtered view lands next to its visible neighbour in the global order
- each column sort, both directions, ties in manual order; ref natural order
- sessions `*`: running first, then `last_at`, parked still at the foot

Hub and daemon (`test_hub.py`, `test_daemon.py`): both routes write through; a client
against a daemon without them gets `Refused`, and rows without the keys read as unplaced.

TUI (`test_tui.py`, Pilot, posting `MouseDown`, `MouseMove` with the button, `MouseUp`):
- a drag of one row moves it, and the store holds the band in the new order
- a drag past the band's edge clamps; Esc cancels and writes nothing
- the click after a drag selects nothing; a plain click still selects
- dragging near the foot scrolls (a list taller than the pane)
- a refresh tick mid-drag keeps the preview
- a press with a sort on starts nothing
- header clicks cycle the markers (Status `*`, `▲`, `▼`; title `▲`, `▼`, off back to
  Status `*`); the dot column's `*` toggles; another session header still unselects
- Alt+Up and Alt+Down move within the band and stop at its edge
- two TUIs on one DB: a drag in one shows in the other after its refresh

## Open questions, with my lean

- **Q1. Is the inbox's `*` the manual order?** The decisions make `*` the Status column's
  default and say drag is off under a sort. Read literally, the inbox is always sorted and
  never draggable. Lean: `*` is today's bands with the manual order inside each, seeded
  from today's ranking, and drag works in it; `▲`, `▼` and the other columns turn it off.
- **Q2. Status changes inside a placed active band.** Once placed, a task going from
  running to blocked doesn't rise. Lean: accept it; new and reopened items still land at
  the top. Alternative: only rows the person dragged hold their place and the rest keep
  ranking around them, which is harder to predict.
- **Q3. Is the sort remembered?** Lean: yes, per `layout.sort.*`, read at start only.
- **Q4. What does Status `▲` sort by?** Lean: the status text as shown, alphabetically,
  since `*` already gives urgency. Alternative: urgency (`RANK`) without the manual order.
- **Q5. Can a child or a grouped agent be dragged?** Lean: not in this slice. Children move
  with their parent; reordering siblings under a parent can follow if wanted.
- **Q6. Do the session list's other columns sort?** Only the dot column is decided. Lean:
  not in this slice: their header click keeps meaning "all sessions", as Esc.
