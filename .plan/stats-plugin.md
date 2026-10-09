# Stats as a pluggable module

Issue: #43. Status: built. Doug: "go, just build it - this project is super experimental
anyway!"

## Where it stood

The suite architecture (`.plan/suite-architecture.md`, #2) describes modules that plug
into the hub: a package declares an entry point in the group `claude_wheelhouse.modules`,
the hub finds it at startup, and each module brings panes and gets one `Context` from the
hub. The hub's own panes are meant to be built the same way.

None of that protocol existed in code. The stats pane was hard-wired: its widget was a
class in `tui.py`, the app owned its followers, worker reads and usage fetch, and the
app's ticks drove it directly.

## What stats needed that the blueprint didn't cover

Stats is the first pane that isn't a tab, so it tested the protocol in four places:

1. **Slots.** A pane says where it goes. Stats goes in `inbox.side`, under the item list.
2. **What's in context.** Stats follows the highlighted or followed session.
   `Context.focus()` gives it.
3. **Ticks.** Stats repaints on a clock (the cache countdown) and animates (the shimmer).
4. **Typing.** The shimmer rests while the person types.

Ticks and typing are solved together: the hub calls two optional methods on a pane's
widget, `tick()` each second and when the session in context changes, and
`animate(frame)` five times a second except while an answer box has focus. The hub
keeps its one animation clock, and a module never needs to know what has focus.

## As built

- **`claude_wheelhouse/api.py`**, the one module a plugin imports: `WHEELHOUSE_API`
  (1), `Module`, `Pane` (title, slot, surfaces), `Context` (state dir, `sessions()`,
  `focus()`, config), `HostContext`, `load()` and `panes(slot)`. Each session from
  `sessions()` carries `context`, the SDK host's last recorded `HostContext` (#54): an
  added key, so the API stays at 1.
- **Discovery.** `load()` reads the entry-point group. The hub's own `pyproject.toml`
  declares `stats = "claude_wheelhouse.stats_pane:module"`, the same way a third party
  would. A module that fails to import, isn't a `Module`, or was written against another
  API is reported with the reason, never raised.
- **`claude_wheelhouse/stats_pane.py`**, the stats module: the widget, its followers,
  its worker reads (`run_worker` on the widget, `exit_on_error=False`, results applied
  with `call_from_thread`) and the account usage fetch. `stats.py` stays the pure part:
  parsing, following, layout and drawing.
- **What `tui.py` hosts.** `mount_panes()` mounts each pane's `tui` surface at the end
  of its slot's container once the app's own widgets are up, with the module's id as the
  widget's id. `each_pane(hook)` calls the hooks.
  `focus_sid()` and `module_sessions()` back the `Context`. Nothing in `tui.py` imports
  `stats`.
- **Isolation.** A module that didn't start shows a card with its reason in its slot, as
  does one whose factory raises or makes something other than a widget, and a module
  whose id another module or one of the app's own widgets already has. The hub's own
  stats module missing from the installed entry points (a copy installed before they
  were declared) shows a card saying to reinstall with `make install`. A pane whose hook
  raises is swapped for a card naming the exception; the app and the other panes carry
  on. Only hooks the pane defines are called: Textual's own `Widget.animate` isn't one.
  With no module in a slot, the slot collapses: the items table takes the whole column.

## Not built yet

These stay as the blueprint schedules them: tabs as a slot, badges, services and the
change feed, `claude-wheelhouse modules`, the example module, conformance tests in
`claude_wheelhouse.testing`, and an error card for exceptions raised inside a widget's
own event handlers (Textual has no error boundary per subtree, so that needs a spike).
