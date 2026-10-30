# #249 Blue for every wheelhouse-driven operation

From the press of any operation that isn't the model at work (Park, Stop & Forget, Relaunch, Unpark, Fork, Shell, Compact) to its real end, the session's eye is blue and nothing can be sent or queued to it. Blue always means sends are locked (Q231 a). Fork blues the child, not the parent.

This generalises #244's compaction cycle (`.plan/244-compaction-cycle.md`). Everything decided there about compaction stands.

## 1. The operations on main today

| Kind | Started by | Its state today | Who acts | Ends today when |
|---|---|---|---|---|
| `compact` | Compact, `request_compact`, quiet mark, Claude Code's own | `sessions.compaction*` (#244) | host | `/compact`'s result |
| `park` | Park (`Hub.ask` → `Store.request`), with or without Compact first | `park_requested_at`, `park_told_at`, `park_compact`, `park_keep` | host or tab monitor tells it; the session calls `park_session`; the host stops after the turn | the host exits with `parked = 1`. No deadline without Compact first |
| `end` | Stop & Forget, "Let it know" | `end_requested_at`, `end_told_at` | as Park; the session calls `end_session` | the row is deleted. No deadline |
| `relaunch` | Relaunch (`stop_host`, `compact_then_relaunch`), Unpark, Relaunch all, Ctrl+S on a stopped session (`wake_and_send`) | `sessions.relaunch` (JSON) plus hub memory (`relaunching`, `compacting`, `lapsed`) | hub (`finish_relaunches`) | the new process registers; `RELAUNCH_WAIT` 30 s to stop, `COMPACT_RELAUNCH_WAIT` 30 min compacting first |
| `start` | New session, Adopt, Fork (the child) | none: `launched_at` and liveness `starting` | hub (`launch`, `Hub.forked`) | the process registers, `STARTING_GRACE` 90 s |
| `shell` | Shell (`Store.command("shell")`), and the take-back when the tab exits | `host_command`, activity `SHELL_OPENING`, `shell` | host | `shell = 'tab'` (out) or `NULL` (back) |

Not operations: model and effort changes (applied live, one write), MCP reconnect and sign-in (no turn, nothing to merge into), keepalives (a turn: red), Park now and "Just stop it" (the hub stops the process and writes the end in one call: nothing to wait on).

## 2. Recommendation: an `op` row beside the compaction cycle

**Compaction stays as it is, and the other kinds join it in a new set of columns.** AGENTS.md forbids renaming a column older code still reads, and every v25 host reads and writes `compaction*` mid-flight (it starts quiet-mark and auto cycles itself). Folding compaction into the new row would also rewrite #244's most-reviewed code for no change in behaviour. The cost is two places to read, so nothing reads them directly: one predicate and one guard cover both.

New `sessions` columns (`ADDED_COLUMNS`, PROTOCOL_VERSION 26): `op` (kind), `op_state`, `op_id`, `op_origin`, `op_at` (state entered, UTC) and `op_deadline` (absolute, UTC). An absolute deadline, written by whoever moves the state, lets the hub's sweep fail any kind without knowing its clocks.

- `operating(s) -> (kind, state) | None`: the outer op if active, else the compaction cycle if active. It replaces `compacting_now` at every caller that means "locked" or "blue". `compacting_now` stays for the activity line and compaction's own code.
- `Store.refuse_busy(db, sid, action, person)` replaces `refuse_compacting` in the same writes (`send`, `queue`, `dispatch`, `dispatch_last`, `command`, `request`, `park`, `request_compact`, `end`), and `check_busy` replaces `check_compacting` in the hub's multi-write actions. It keeps the quiet-mark call-off and the waiting-permission exemption unchanged.
- **Nesting.** Park with Compact first and Relaunch compacting first are the outer op (`park`, `relaunch`) with a compaction cycle inside (origin `park`, `relaunch`), as #244 already models. The outer op owns the lock and the eye; the inner cycle keeps its task item, stages and clocks. An inner cycle ending never ends the outer op; the outer op's end or cancel ends the inner one in the same transaction.
- **The row is the lock and the clock, never a command.** The request columns (`park_requested_at` and the rest) and `relaunch` remain the payload that tells the session or carries the stopped host's pid. They are written and cleared only in the same transaction as the op row, by the store methods that already own them (`request`, `cancel_request`, `set_parked`, `park`, `end`, `mark_launched`, `set_relaunch`). Nothing starts an action because an op row exists, except the hub re-driving its own relaunch after a restart.
- **Ends are observed by the hub wherever possible**, so they work whatever code the host runs: a park completes when the row says `parked` and the process is gone, an end when the row is gone, a start or relaunch when the new process registers, a shell hand-over when `shell` changes. Only compaction needs the host.

## 3. The shared state table

Every kind runs `requested` → its active states → `completed`, `failed` or `cancelled`. Terminal states stay in the row until the next op.

| Kind | Active states (next) | Driven by | Completed | Deadline (`op_deadline`), then `failed` | Cancelled by |
|---|---|---|---|---|---|
| `park` | `requested` (told: `told`), `told` (`park_session`: `parking`), `parking` (process gone: completed). Compact first nests a cycle in `parking` | host or monitor tells; hub observes the end | parked and not running | `PARK_WITHIN` 1 h from the press (as `PARK_COMPACT_WITHIN`), then `COMPACT_TIMEOUT` once parking compacts | Cancel request; Interrupt before `parking` |
| `end` | `requested`, `told`, (row deleted) | as park | the row is gone: the op goes with it | `PARK_WITHIN` from the press | Cancel request; Interrupt before the row goes |
| `relaunch` | `requested` (compact first: inner cycle), `stopping` (process gone: `starting`), `starting` (registered: completed) | hub | the new process has registered | `stopping` `RELAUNCH_WAIT`; compact first `COMPACT_RELAUNCH_WAIT`; `starting` `STARTING_GRACE` | Interrupt during compact first only |
| `start` (new, adopt, fork child) | `starting` | hub | registered | `STARTING_GRACE` | none |
| `shell` | `requested` (host takes it: `opening`), `opening` (`shell = 'tab'`: completed); take-back `returning` (`shell` NULL: completed) | host; hub observes | the hand-over has landed | 60 s per state | none |

**Ways out.** Every active state has its deadline (hub sweep: read first, write only for a stale row, compare-and-set on `op_id` and `op_state`). A dead or stalled host fails a host-driven state (`requested`, `told`, `opening`, `returning`); for `parking` it completes instead when the row says parked (the session parked, which is the op). A restarted hub re-drives `relaunch` and `start` from the row and the `relaunch` payload, or fails them past their deadline. A restarted host re-tells a `requested` or `told` park or end, as it does today, and fails a `shell` state it can't resume. `mark_launched` already clears stale requests: it ends their op as `failed` in the same transaction.

**Writes through a locked database.** Claims and ends that can't be skipped (the host's `told`, the hub's `stopping` → `starting` and completions) retry as `Host.steady` and #244's owed moves do. The sweep runs on refresh and reads before it takes a write lock.

**Blue.** The eye is blue while `operating(s)` is set, running or not: `TUI.compacting()` loses its `running` filter, as every active state now has a deadline, and `table.eyes` gains those rows so a stopped session being relaunched shows its blue eye. The activity line keeps its words.

**Refusal.** One toast for every kind: **"{name} is {doing}: {action} once it's done{, or Interrupt to cancel the {noun}}."** Doing and noun: parking/park, being forgotten/Stop & Forget, relaunching/relaunch, starting/start, opening a shell tab/hand-over, compacting/compaction (#244's text, unchanged). The Interrupt clause appears only where Interrupt cancels in that state.

**What stays allowed during any op.** Answering a permission or question the session is waiting on (registered refs, as #244). Park now and Stop & Forget "Just stop it", which end the op (`completed` or with the row) as the escape from a stuck one (Q-c). Interrupt, as the table below. Each op's own payload: `wake_and_send` dispatches its queue inside the relaunch's own call; a park's `then` still goes.

**The session's own MCP calls mid-op** are refused with a reason, except the call the op is waiting for: `park_session` during `park` and `end_session` during `end` go through. `end_session` gains the guard it lacks today (it deletes the row even mid-compaction). Reasons name the op: "this session has been asked to park: call park_session", "this session is relaunching".

**Keepalives** are skipped while `operating(s)` is set (`keepalive_due`), and the quiet mark does not start a cycle inside another op.

## 4. Interrupt, per state

| Kind and state | Interrupt does | Left behind |
|---|---|---|
| park `requested`, `told` | the turn is interrupted; request, Compact first and cycle go (`drop_park_request`); op `cancelled` | nothing: the request columns are cleared in the same transaction |
| park `parking` | the park's turn or its `/compact` is stopped; the inner cycle `cancelled`; the host stops | op `completed` (it is parked, uncompacted) |
| end `requested`, `told` | the turn is interrupted; end request cleared; op `cancelled` | nothing |
| relaunch, compact first | as today: inner cycle and relaunch cancelled | `relaunch` payload cleared with the op |
| relaunch `stopping`, `starting`; start; shell | refused: there is no turn to stop. Toast: "{name} is relaunching: wait for it" | the deadline is the way out |
| compact | #244 unchanged | |

An Interrupt command left untaken when an op ends is dropped with it (as `drop_compact_command`), so no later host receives it.

## 5. Compatibility

- **v25 host, v26 hub.** Park and end: the old host tells the request as now; the hub observes the end, so the op completes. A v25 host doesn't skip keepalives mid-op, but sends are refused in the store, so it has nothing to deliver; acceptable for the window. Shell: the hub observes `shell`. Compaction is unchanged.
- **v26 host, v25 daemon** (after a pull, before the daemon restarts): no op rows are written, so there is no blue for park and the rest; behaviour is as today. The host never requires an op row to act.
- **Tab monitors** tell requests from the request columns, unchanged.

## 6. Size and PRs

About store +180, hub +120 (relaunch's hub memory moves to the row), host +40, TUI +40, MCP server +20, tests +500. Two stacked PRs: (1) the columns, `operating`, the guards, park, end and the blue eye; (2) relaunch, start and shell.

## 7. Test plan (parametrised)

| Table | Cases (one row each) | Asserts |
|---|---|---|
| sequence per kind and origin | park ± compact, park via `park_session`, end, relaunch ± compact, unpark, wake, relaunch all, new, adopt, fork child, shell out and back | op state and eye at each step; nothing else sent; completed |
| blocked action × kind × active state | send, queue, Ctrl+S, send all (skips and names), park, relaunch, unpark, shell, compact, `request_compact` | refused, toast wording, store unchanged |
| allowed action × kind × state | answer waiting permission, Park now, "Just stop it", `park_session` in park, `end_session` in end | succeeds, op ends as specified |
| way out × kind × state | deadline passed, host dead, host stalled, hub restarted, host restarted, locked database on claim and end | terminal state, eye not blue, drafts intact, payload columns cleared, no command left |
| Interrupt × kind × state | every row of section 4 | as section 4 |
| compatibility | v25 host with park and end, v25 daemon with v26 host | op completes, or no op and today's behaviour |
| fork | child blue from row creation to registration; parent never blue | |

## 8. Open questions for Doug

- **Q-a, park and end deadline.** 1 h from the press, as Park's Compact first has. A session in a long turn stays blue and unsendable until its turn ends and it parks. Shorter fails a park still waiting on a long turn and unlocks it; longer keeps a stuck request blue for longer.
- **Q-b, queueing while not running.** During relaunch `stopping` and `starting`, and a fork child's `starting`, there is no turn a queued draft could merge into. Locked (Q231 a, recommended for one rule) means typing waits up to 90 s; allowing queueing there means blue no longer always means "can't queue".
- **Q-c, Park now and "Just stop it" during any op.** Recommended: allowed, ending the op, as the escape from a stuck one. Today Park now is refused during a compaction; this would let it cut a compaction short by stopping the host.
- **Q-d, how a failure shows.** Recommended: a warning notice, as relaunch does now. The alternative is a blocked task in the inbox (as compaction's item), which is seen when away but adds an item per failed park.
- **Q-e, new session and adopt.** Included as `start` because "basically any operation" covers them. Leaving them out keeps their red or idle eye during the 90 s start.
