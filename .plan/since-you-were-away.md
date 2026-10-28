# Since you were away (design)

One dismissable card at the top of the inbox, after a long gap, saying what happened across
every session meanwhile. Issue #223.

## The gap

- A gap is `features.away_hours()` (`--away-hours`, `WHEELHOUSE_AWAY_HOURS`, `AWAY_HOURS=`,
  2 by default, 0 for off) with no key press, click or scroll in the app. Pointer movement
  alone doesn't count: a pointer crossing the window on its way elsewhere isn't a return.
- `WheelhouseApp.on_event` notes each input. The first after a gap is a return: the card's
  window is from the input before it to now.
- The last input is kept in the `away.last_input` setting (UTC), written at most once a
  minute and as the app closes. At start, a stored time a gap or more ago is a return too,
  its window from then to now. With none stored (the first start on this code) there's no card.

## What went quiet

Nothing in the store dates a park, an end or a crash: an end deletes the row. So
`Hub.refresh`, which reads every session each second, compares with its last read and keeps
what it sees in the `away.quiet` setting, the latest per session (`{sid: {name, how, at}}`,
how one of parked, ended, stopped), dropped after 7 days. A session parked (`parked` 0 to 1),
gone (its row deleted), or alive then dead and not parked (stopped: crashed, or its tab
closed). It sees older sessions' doings too, as it watches the rows, not the writers. Only
the daemon's hub (or `--local`'s) sees them; a hub's first read compares with the last
hub's, kept in `away.seen`, so what went while neither ran is noted, dated at that start.

## The query

`away.card(items, quiet, running, start, end)`, pure, over `client.all_items()` and that
setting, so it is the same in process and through the daemon (no route added):

| Group | Rows |
|---|---|
| Needs you | questions and permissions `open`, created in the window; tasks `blocked` or `waiting`, updated in it |
| Landed | PRs `merged` and tasks `done`, updated in the window |
| Went quiet | `away.quiet` entries in the window whose session isn't running now |
| Decided without you | decisions created in the window: a count, then titles |

Each group newest first, at most `PER_GROUP` (3) lines then `+N more`; an empty group is
left out, and with every group empty there is no card.

## The card

`AwayCard`, an `OptionList` above the inbox's table in `#items-pane`, hidden until a return.
A title line with the window (`While you were away, 14:05 to 17:30`) and a `✕` close
control; a heading per group, then its lines as options. Enter or a click on a line jumps as
an inbox row does: an item through `show_item`, a session through `follow`. Esc in the card
or `✕` dismisses it, as does Esc anywhere on the main screen but a text box with text in it.
Shown, it takes the focus unless a text box has it (typing is never
redirected), and is a Tab stop before the inbox while it shows. Each refresh drops Needs-you lines dealt with (no longer
open, blocked or waiting); when the last goes, so does the card. Showing it marks nothing seen.
At 80x24 it takes at most 4 groups x (1 + 3 + 1) rows plus its title, capped at half the
inbox's height and scrolling past that.
