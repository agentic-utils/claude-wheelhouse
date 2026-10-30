# #249 Blue for every wheelhouse-driven operation

From the press of any operation that isn't the model at work (Compact, Park, Stop & Forget, Relaunch, Unpark, Fork, Shell, New session, Adopt) to its real end, the session's eye is blue. While it is blue:

- **Dispatch is refused**: an immediate send, Ctrl+S, Send, Send all. Drafting and adding to the queue are always allowed (Q244).
- **Every other operation is refused**: Park, Park now, Stop & Forget ("Let it know" and "Just stop it"), Relaunch, Unpark, Fork, Shell and Compact (Q245).
- **Interrupt aborts the operation**, whatever its state, compaction included, and leaves nothing behind (Q245).

Fork blues the child, not the parent (Q231). New session and Adopt blue the new session while it starts (Q247).

This generalises #244's compaction cycle (`.plan/244-compaction-cycle.md`). Section 6 lists what changes there.

## 1. The operations on main today

| Kind | Started by | Its state today | Who acts | Ends today when |
|---|---|---|---|---|
| `compact` | Compact, `request_compact`, quiet mark, Claude Code's own | `sessions.compaction*` (#244) | host | `/compact`'s result |
| `park` | Park (`Hub.ask` → `Store.request`), with or without Compact first | `park_requested_at`, `park_told_at`, `park_compact`, `park_keep` | host or tab monitor tells it; the session calls `park_session`; the host stops after the turn | the host exits with `parked = 1`. No deadline without Compact first |
| `end` | Stop & Forget, "Let it know" | `end_requested_at`, `end_told_at` | as Park; the session calls `end_session` | the row is deleted. No deadline |
| `relaunch` | Relaunch (`stop_host`, `compact_then_relaunch`), Unpark, Relaunch all, Ctrl+S on a stopped session (`wake_and_send`) | `sessions.relaunch` (JSON) plus hub memory (`relaunching`, `compacting`, `lapsed`) | hub (`finish_relaunches`) | the new process registers |
| `start` | New session, Adopt, Fork (the child) | none: `launched_at` and liveness `starting` | hub (`launch`, `Hub.forked`) | the process registers, `STARTING_GRACE` 90 s |
| `shell` | Shell (`Store.command("shell")`) | `host_command`, activity `SHELL_OPENING`, `shell` | host | `shell = 'tab'` |

Not operations: model and effort changes (applied live, one write), MCP reconnect and sign-in (no turn), keepalives (a turn: red), and Shell's take-back as the tab exits (a few seconds, and nothing to abort). Park now and "Just stop it" have no op of their own: one hub call stops the process and writes the end.

## 2. Recommendation: an `op` row beside the compaction cycle

**Compaction stays as it is, and the other kinds join it in a new set of columns.** AGENTS.md forbids renaming a column older code still reads, and every v25 host reads and writes `compaction*` mid-flight (it starts quiet-mark and auto cycles itself). Folding compaction in would also rewrite #244's most-reviewed code for no change in behaviour. The cost is two places to read, so nothing reads them directly: one predicate and one guard cover both.

New `sessions` columns (`ADDED_COLUMNS`, PROTOCOL_VERSION 26): `op` (kind), `op_state`, `op_id`, `op_origin`, `op_at` (state entered, UTC) and `op_deadline` (absolute, UTC). An absolute deadline, written by whoever moves the state, lets the hub's sweep fail any kind without knowing its clocks.

- `operating(s) -> (kind, state) | None`: the outer op if active, else the compaction cycle if active. It replaces `compacting_now` wherever that means "locked" or "blue".
- `Store.refuse_busy(db, sid, action)` replaces `refuse_compacting` in the dispatching writes (`send`, `dispatch`, `dispatch_last`) and the operation writes (`command`, `request`, `park`, `request_compact`, `end`); `check_busy` replaces `check_compacting` in the hub's multi-write actions (`force`, `unpark`, `stop_host`, `compact_then_relaunch`, `relaunch`, `wake_and_send`, `fork`). `Store.queue` loses its guard.
- **Nesting.** Park with Compact first and Relaunch compacting first are the outer op with a compaction cycle inside (origin `park`, `relaunch`), as #244 already models. The outer op owns the lock and the eye; the inner cycle keeps its task item, stages and clocks. The outer op's end, failure or abort ends the inner cycle in the same transaction.
- **The row is the lock and the clock, never a command.** The request columns and `relaunch` remain the payload that tells the session or carries the stopped host's pid. They are written and cleared only in the same transaction as the op row, by the store methods that already own them. Nothing starts an action because an op row exists, except the hub re-driving its own relaunch or start after a restart.
- **Ends are observed by the hub wherever possible**, so they work whatever code the host runs: a park completes when the row says `parked` and the process is gone, an end when the row is gone, a start or relaunch when the new process registers, a hand-over when `shell = 'tab'`. Only compaction needs the host.

## 3. The shared state table

Every kind runs `requested` → its active states → `completed`, `failed` or `cancelled` (Interrupt). Terminal states stay in the row until the next op.

| Kind | Active states (next) | Driven by | Completed | Deadline, then `failed` |
|---|---|---|---|---|
| `park` | `requested` (told: `told`), `told` (`park_session`: `parking`), `parking` (process gone). Compact first nests a cycle in `parking` | host or monitor tells; hub observes the end | parked and not running | `PARK_WITHIN` 15 min from the press; once parking compacts, the cycle's own `COMPACT_TIMEOUT` |
| `end` | `requested`, `told` (row deleted) | as park | the row is gone, and the op with it | `PARK_WITHIN` from the press |
| `relaunch` | `requested` (compact first: inner cycle), `stopping` (process gone: `starting`), `starting` (registered) | hub | the new process has registered | compact first `COMPACT_RELAUNCH_WAIT`; `stopping` `RELAUNCH_WAIT`; `starting` `STARTING_GRACE` |
| `start` (new, adopt, fork child) | `starting` | hub | registered | `STARTING_GRACE` |
| `shell` | `requested` (host takes it: `opening`), `opening` (`shell = 'tab'`) | host; hub observes | handed to the tab | 60 s per state |

**`PARK_WITHIN` is 15 minutes (Q243), one constant for park and end, and it replaces `PARK_COMPACT_WITHIN`** (1 h, host.py). Recommended, because the hour was chosen as the prompt cache's life, which only bounds how long a compaction is still worth doing; Doug's 15 minutes is inside it, and one clock for "the park hasn't happened" is simpler than two that differ only by Compact first. Past it, the op fails: the request columns, Compact first and any inner cycle are cleared in one transaction, a session already told hears "The person cancelled the park request: carry on as before" (`cancel_request`'s existing message, so it can't park late), and the person gets a warning notice: **"{name} couldn't be parked within 15 minutes: it carries on as it was."** (or "couldn't be stopped and forgotten").

**Ways out.** Every active state has its deadline (hub sweep: read first, write only for a stale row, compare-and-set on `op_id` and `op_state`). A dead or stalled host fails a host-driven state (`requested`, `told`, `opening`); in `parking` it completes instead when the row says parked. A restarted hub re-drives `relaunch` and `start` from the row and its payload, or fails them past their deadline. A restarted host re-tells a `requested` or `told` park or end, and fails a `shell` state it can't resume. `mark_launched` already clears stale requests: it fails their op in the same transaction.

**Every failure is a warning notice** (Q246), from whichever process writes it (hub sweep, hub driver, host through the hub's next refresh), naming the op and the reason. No inbox item; compaction keeps its #244 task item.

**Writes through a locked database.** Claims and ends that can't be skipped (the host's `told`, the hub's `stopping` → `starting` and completions) retry as `Host.steady` and #244's owed moves do. The sweep runs on refresh and reads before it takes a write lock.

**Blue.** The eye is blue while `operating(s)` is set, running or not: `TUI.compacting()` loses its `running` filter (every active state has a deadline), and `table.eyes` gains those rows, so a stopped session being relaunched and a fork child starting show a blue eye. The activity line keeps its words.

**Refusal toast**, one for every refused action and kind: **"{name} is busy {doing}: {action} once it's done, or Interrupt to abort it."** Doing: compacting, parking, being stopped and forgotten, relaunching, starting, opening a shell tab. For example "projects-82 is busy relaunching: send once it's done, or Interrupt to abort it." A tab's compaction keeps #244's "compacting in its tab" wording, which has no Interrupt.

**What stays allowed during any op.** Drafting, queueing and unqueueing. Answering a permission or question the session is waiting on (registered refs, as #244). Interrupt. Each op's own payload: a park's `then` still goes, and `wake_and_send`'s queue (below).

**The session's own MCP calls mid-op** are refused with a reason naming the op, except the call the op is waiting for: `park_session` during `park` and `end_session` during `end`. `end_session` gains the guard it lacks today (it deletes the row even mid-compaction). **Keepalives** are skipped while `operating(s)` is set, and the quiet mark starts no cycle inside another op.

## 4. Interrupt aborts, per state

Abort means: the session ends as it was before the press, as nearly as the state allows; the op `cancelled`; no request, Compact first, command or `relaunch` payload left; nothing dispatched by the op. Interrupt during a hub-driven op is handled in the hub (`Hub.host_command`), and never leaves an Interrupt command for a later host to take.

| Kind and state | Abort does | The session ends |
|---|---|---|
| park or end, `requested` | request and Compact first cleared; nothing was said to the session | as it was |
| park or end, `told` | the turn is interrupted; request cleared; the session hears the cancel message, so it can't park or end late | live, carrying on |
| park, `parking` (parked, its turn or `/compact` running) | the turn or `/compact` interrupted; inner cycle `cancelled`; unparked; the session hears the cancel message; the host keeps running | live, unparked, uncompacted |
| relaunch, compact first | inner cycle `cancelled`; relaunch dropped (as today) | live on its old host |
| relaunch, `stopping` (old process signalled) | the signal can't be taken back: the new process is never started | stopped: Available, or Parked if it was |
| relaunch, `starting` (new process launched, not registered) | the new process stopped (`liveness.stop`); anything `wake_and_send` dispatched and not yet claimed returns to the queue as drafts | stopped, its queue as before the press |
| start (new, adopt, fork child), `starting` | the launching process stopped; the row stays (a fork's worktree and moved items stay too: undoing them is a side effect of its own) | Available, to Relaunch |
| shell, `requested`, `opening` | the command dropped, or the tab's process stopped; the host keeps the session | live on its host |
| compact, any state, `/compact` included | as #244's Interrupt (cancelled, drafts stay queued), now also cutting `/compact` short, its tokens spent | live, uncompacted |

The new host claims its first batch only after registering, and registration completes the op, so `starting` never has claimed messages to recover.

## 5. Compatibility

- **v25 host, v26 hub.** Park and end: the old host tells the request as now, and the hub observes the end. A v25 host doesn't skip keepalives mid-op; sends are refused in the store, so it has nothing else to deliver. Interrupt reaches it as today's command; the hub clears the op and request columns itself.
- **v26 host, v25 daemon** (after a pull, before the daemon restarts): no op rows, so no blue for the new kinds; behaviour as today. The host never needs an op row to act.
- **Tab monitors** tell requests from the request columns, unchanged.

## 6. Changes to #244

- Adding to the queue during a compaction is now allowed: `Store.queue` refuses it on main (`refuse_compacting(db, sid, "queue", ...)`). Only dispatch is refused.
- Stop & Forget "Just stop it" is refused during a compaction instead of cancelling it; Interrupt is the way out. Park now stays refused.
- The toast's wording becomes the shared one ("is busy compacting ... or Interrupt to abort it").
- `PARK_COMPACT_WITHIN` (1 h) becomes `PARK_WITHIN` (15 min).

## 7. Size and PRs

About store +180, hub +140 (relaunch's hub memory moves to the row), host +40, TUI +40, MCP server +20, tests +550. Two stacked PRs: (1) the columns, `operating`, the guards, park, end, the queue change and the blue eye; (2) relaunch, start and shell, with their aborts.

## 8. Test plan (parametrised)

| Table | Cases (one row each) | Asserts |
|---|---|---|
| sequence per kind and origin | park ± compact, park via `park_session`, end, relaunch ± compact, unpark, wake, relaunch all, new, adopt, fork child, shell | op state and eye at each step; nothing else sent; completed |
| dispatch refused × kind × active state | send now, Ctrl+S, Send, Send all (skips and names) | refused, toast names the op, store unchanged |
| queue allowed × kind × active state | queue, edit a draft, unqueue, compaction included | succeeds; nothing dispatched until the op ends |
| operation refused × kind × active state | Park, Park now, Stop & Forget both ways, Relaunch, Unpark, Fork, Shell, Compact, `request_compact` | refused, toast wording, store unchanged |
| allowed × kind × state | answer a waiting permission or question, `park_session` in park, `end_session` in end | succeeds |
| Interrupt × kind × state | every row of section 4 | `cancelled`; the "session ends" column; no request, command, payload or Compact first left; no message dispatched; drafts intact |
| way out × kind × state | `PARK_WITHIN` passed (told and untold), other deadlines, host dead, host stalled, hub restarted, host restarted, locked database on claim and end | `failed`, eye not blue, warning notice text, told session given the cancel message, drafts intact |
| compatibility | v25 host with park and end, v25 daemon with v26 host | op completes, or no op and today's behaviour |
| fork | child blue from row creation to registration; parent never blue | |

## 9. Open questions for Doug

- **Q-a, the quiet mark's call-off.** On main, a person's send, queue or Ctrl+S cancels the quiet mark's own compaction before its `/compact` goes, and the write goes through (#244: the wheelhouse's own, and the person has come back). This design keeps that exception for dispatch. Should adding to the queue still call it off too? Recommended yes, as now: the person is back. No means queueing during the quiet mark's notes turn leaves it running, and only a send or Interrupt stops it.

## 10. As built (PR 1: park, end, the guards and the eye)

Where the build settled something the design left open, or departed from it:

- **Stalled is only quiet.** A stalled session (no heartbeat for 2 minutes, but its process alive) keeps a `requested` or `told` park or end until `PARK_WITHIN`: the session list already treats a stalled session as a quiet one it asks. Only a session no longer running fails it at once. In `parking`, a stalled or dead host with the row parked completes it, as designed.
- **The pending request's dialog loses Force (Alt+O).** A pending park or end is always an op, so Force would always be refused. Cancel stays: a tab session has no Interrupt, and Cancel ends the op as cancelled.
- **`parking` has its own clock**: `PARK_WITHIN` from entering it, plus `COMPACT_TIMEOUT` when it compacts first. Past it the op fails and the session stays parked (it has parked; its host stops).
- **The inner park cycle's own requested deadline** (`requested_expired`) stands aside while its park's op is active, so one sweep, the op's, fails both.
- **The session's own calls refused mid-op** are the operation calls: `park_session` (but during its park), `end_session` (but during its end) and `request_compact`. Posting items and replying stay open.
- **The quiet mark's cache-life check** (`cold_due`) keeps its hour as `CACHE_LIFE`: it was `PARK_COMPACT_WITHIN`'s second use.
- **A called-off park or end can't be acted on late.** A failed or aborted op doesn't interrupt the running turn (no side effects). Instead, `park_session` and `end_session` from the turn that was told are refused ("the park request was cancelled: carry on as before"). The turn boundary is the host's last turn end, `sessions.idle_at`. With none known (a tab, an older host, no turn ended yet), the boundary is the cancel message's handover. An unprompted call in a later turn goes.
