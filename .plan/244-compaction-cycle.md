# #244 The compaction cycle

Once a compaction starts, by any route, the session is closed to everything else until the cycle has finished. Nothing can be sent or queued, and it can't be parked, relaunched or compacted a second time. Its eye stays blue from the press to the end of `/compact`. The cycle shows as a task item and as stage lines in the conversation.

## 1. What happened on 29 Oct

The session was `f1e24d78` (projects-82). Its transcript, all times UTC:

| Time | Event |
|---|---|
| 10:39:06 | Compact ask sent (nonce `cb919ae5`) |
| 10:39:26 | Doug's answers to Q3 to Q8 arrive in Claude Code's queue and are taken into the running notes turn |
| 10:39:42 | The session works the answers (update_item on Q3 to Q8, three agents) in the notes turn |
| 10:41:01 | Notes turn ends; `/compact` sent |
| 10:41:36 | A **second** Compact ask is sent (nonce `19ab544a`) while the first `/compact` runs |
| 10:41:56 | First compaction completes |
| 10:41:57 | The host tells the session "This compaction was automatic" |
| 10:42:17 | Second `/compact`; completes 10:42:57 |

There are two defects. Both are reproduced against main in `~/.claude/scratchpad/batch-2026-10-29/design-agent/test_repro_244.py` (two tests, both pass on main).

**Ctrl+S was let through.** `Hub.send_queue` calls `Store.dispatch` with no compaction check, and so do `Hub.submit`, `send_all` and `dispatch_last`. In `Host.poll`, `deliver()` holds messages only when `compact_then == "park"`. The answers therefore went out as a turn while the notes turn was running, and Claude Code merged them into it.

**Why it looked as if /compact never ran.** Compaction did run, twice. At 10:41:36 a second `compact` command reached the host, and `Host.poll` takes one at any time. The `command == "compact"` branch sets `compact_key = None` and sends a new ask. When the first `/compact` ended, `finished()` no longer recognised it as the wheelhouse's own (`driven=False`). It therefore announced an automatic compaction and dropped the plan that should have followed. Then the second cycle ran. `Hub.host_command` doesn't refuse Compact while one is under way: only `compact_then_relaunch` checks `compacting_now`. The transcript doesn't record who sent the second Compact. My inference is that the activity line after the merged turn looked as if compaction had not happened, and it was pressed again.

Underneath both defects, the cycle's state is spread across about ten host fields (`compact_nonce`, `compact_idle`, `compact_notes`, `compact_then`, `compact_key`, `compact_turn`, `compact_line`, `compact_sent`, `cold_nonce`, `park_told`) plus store columns (`host_command`, `compact_plan`, `compact_request`, `park_compact`, `park_keep`). Each entry point checks a different subset.

## 2. One state machine, in the store

**Recommendation: the state lives in the store. The host drives it, and every writer checks it in the same transaction as its write.**

- Every block in section 3 is a store write (`send`, `queue`, `dispatch`, `dispatch_last`, `command`, `request`, `park`, `set_relaunch`). A guard inside that write's transaction can't be bypassed, whichever app, machine or daemon path makes the call. A guard kept in host memory is invisible to the hub, and a guard in the TUI misses other clients.
- The host is the only process that sees turns start and end, so it alone makes the transitions after `requested`. The hub only ever writes `requested`, plus `failed` when it finds the host dead.

New `sessions` columns (schema bump): `compaction` (state), `compaction_id`, `compaction_origin` (`person`, `model`, `park`, `relaunch`, `quiet`, `auto`), `compaction_then`, `compaction_at` (when the state was entered, in UTC) and `compaction_item` (the task item's ref). Stage times go in a small `compaction_stages` table so the conversation can show them (section 6). In the host, the ten fields become one `Cycle` object (`id`, `nonce`, `notes`, `turn_key`, `deadline`) that is rebuilt from the row on restart.

| State | Entered by | Ends when (next state) | Guard against sticking |
|---|---|---|---|
| `requested` | Any start (below), in the same transaction as the request | Host takes it and sends the ask (`preparing`), or sends `/compact` straight away when instructions were given (`compacting`) | Not claimed within `CLAIM_WITHIN` (60 s, from the press to a host moving it on at its next poll, never to the notes turn starting, so a session in a long turn doesn't fail): the hub's sweep sets `failed`, unlocking the session with its drafts still queued. This covers a host on older code or a wedged one; Park's Compact first has its own deadline instead (below). The sweep reads the row first and takes a write only for a stale cycle. A running turn is allowed: the ask queues behind it, and the state moves to `preparing` as it is sent |
| `preparing` | Notes ask sent | Turn carrying the notes block ends (`compacting`). Turn ends with no block: bare `/compact`, said in the thread (`compacting`). Error result (`failed`) | `NOTES_TIMEOUT` (120 s) after Claude Code takes the ask up: nothing is cancelled, the host posts a Keep waiting or Abort question (section 5). Keep waiting asks again after another `NOTES_TIMEOUT`; Abort interrupts (`cancelled`). Unanswered, the cycle still ends with the notes turn, an Interrupt or the session's death |
| `compacting` | `/compact` sent, or Claude Code's PreCompact hook fires (auto) | `/compact`'s result with a `compact_boundary` (`completed`). Result without a boundary, or an error (`failed`) | 10 min (`COMPACT_TIMEOUT`) from `/compact` running: Claude Code taking it up, or nothing left ahead of it (not from queueing it behind a running turn): `failed`. This replaces `hold_expired` |
| `completed` | | The host runs `then` (`send`, `compact` or `park`) | Terminal: the row keeps it until the next cycle |
| `failed` / `cancelled` | Timeouts, errors, host or session death, Interrupt, Abort, Stop & Forget "Just stop it" | Queued drafts stay queued | Terminal: the row keeps it until the next cycle |

Every active state also ends on: **Interrupt** (`cancelled`, the client is interrupted), **host exit** (the host's `finally` sets `failed`; the hub's liveness sweep does the same if the host died hard; a host started again fails a cycle past `requested` that the last one was driving, `lost_cycle`), and **session gone**. Only the host that holds the cycle's id moves it, and only while it is active, so an ended cycle stays ended.

The `/compact` turn's end is matched by `compaction_id` and its `turn_key`, not by "the key is no longer in `sent`". With nothing else allowed in during the cycle, the only turns that can merge are Claude Code's own (agent hand-backs and task notifications, which do appear at 10:39:16), and those carry no key of ours.

**How each route starts the cycle:**

- **[Compact]**: on the question, the session menu or its key. This is `Store.command("compact")`, which sets `requested`, with origin `person`.
- **request_compact** (the model): `promote_compact_request` sets `requested` with origin `model` once the turn has ended. A request made while a cycle is active is refused, as now.
- **Relaunch, compact first**: `requested` with origin `relaunch`. The relaunch is the cycle's `then`, so it isn't blocked.
- **Park, compact first**: `requested` (origin `park`, `then` `park`) is set at the press, in `Store.request`'s transaction, so the eye is blue and sends are refused from then. The park request is the cycle's first step: the host still tells it while it holds everything else, and the park's own turn runs. Once the session has parked, the host takes the cycle and sends `/compact` (`compacting`). Cancelling the park request, or parking without compacting, ends it as `cancelled`. Its deadline is `PARK_COMPACT_WITHIN` (an hour, the prompt cache's life) from the press, checked by the host and by the hub's sweep whoever hosts it: past it the cycle fails, unlocking the session, and its Compact first goes, in one transaction. A host that starts (or takes the session back from a shell tab) and finds this cycle re-drives it, telling the park request again, or compacts it if the session has parked, and otherwise fails it: it is never left waiting with no clock. The park is the cycle's end: once `/compact` has compacted it, the session is parked and then the cycle completes. Park with Compact first is refused for a terminal tab, where compaction isn't offered. A session parking itself unasked (`park_session`) starts its cycle when the host compacts it.
- **The 8 h quiet mark** (`cold_due`): origin `quiet`. It is the wheelhouse's own, not the person's, so the person returning calls it off: while it is `requested` or `preparing`, a send, queue or Ctrl+S ends it as `cancelled` in the same transaction (the host then interrupts the notes turn, and nothing is compacted) and goes through as normal. Once `/compact` has gone (`compacting`) it is refused like any other cycle.
- **Claude Code's own auto-compaction, SDK mode:** the SDK exposes a `PreCompact` hook (`claude_agent_sdk.types.PreCompactHookInput`, with `trigger` set to `auto` or `manual`). The host registers it. An `auto` trigger with no cycle active enters `compacting` directly, with origin `auto`: the eye is blue, sends and queueing are blocked, and stage lines are shown. It ends at that turn's result. It gets the task item too (Q2).
- **Terminal mode:** the wheelhouse starts no compactions there, because Compact is refused for non-SDK sessions. A wheelhouse PreCompact hook in the tab plugin (Q4, PR 2) gives a tab's compaction, typed `/compact` or Claude Code's own, the blue eye, stage lines and the task item. What a tab can enforce is limited to what the wheelhouse sends: the tab itself is where the person types.

## 3. What is blocked, and where

The guard is `Store.refuse_compacting(db, sid, action)`, true in `requested`, `preparing` and `compacting`. It raises `Compacting` inside the transaction, the hub turns that into a `Done(False)` warning (`refusable`), and the TUI shows it as a toast. `compacting_now()` reads the same state.

Park, Unpark, Park now and Relaunch are more than one write (stopping a process, a relaunch's bookkeeping), and the host and the hub's own relaunch flow write `parked` and `relaunch` as a cycle's `then` or after it ends. So these are checked in the hub, first thing, with `Store.check_compacting`, rather than inside `set_parked` and `set_relaunch`. Every person's path to them goes through the hub (the daemon's routes and the in-process client call the same methods). Shell is refused too.

**Older hosts.** A host on code before PROTOCOL_VERSION 24 never moves the state on, so a cycle given to it would stay blue and closed (and refuse the Relaunch that would cure it). The hub therefore refuses Compact for a session that needs a relaunch ("runs older wheelhouse code: Relaunch it, then Compact"), and Relaunch compacting first relaunches it at once, without compacting, and says so. A host started again fails a cycle it can't carry on: one past `requested`, or one still `requested` whose Compact command the dead host had already taken. A turn's end that can't write the cycle's end through a locked database owes it to the next pass, which writes it and carries out its `then`.

| Action | Enforced in | During the cycle |
|---|---|---|
| Send now (Immediate mode, Alt+S) | `Store.send` | Refused |
| Queue a message | `Store.queue` | Refused (Doug: queueing is blocked too) |
| Ctrl+S, Send, Send all | `Store.dispatch`, `dispatch_last` | Refused. `send_all` skips the session and names it |
| Park (asked) | `Store.request` | Refused |
| Park now, Unpark, Relaunch | Hub: `force`, `unpark`, `stop_host`, `compact_then_relaunch` (`Store.check_compacting`) | Refused (the relaunch that the cycle was started for finishes once it ends) |
| Shell | `Store.command("shell")` | Refused |
| Stop & Forget, "Let it know" | `Store.request("end")` | Refused: it would send a turn |
| Stop & Forget, "Just stop it" | Hub | Allowed: ends the cycle as `cancelled` |
| Compact again, request_compact | `Store.command`, `request_compact` | Refused ("already compacting") |
| Interrupt | `Store.command("interrupt")` | Allowed: ends the cycle as `cancelled` |
| Answer to a permission, or an AskUserQuestion the notes turn is waiting on | `Store.send` | Allowed, so the notes turn can't wedge on its own prompt. Only for refs the host has registered as waiting |

Wording (one toast, the same for every refused action): **"{name} is compacting: {action} once it's done, or Interrupt to cancel the compaction."** For example: "projects-82 is compacting: send once it's done, or Interrupt to cancel the compaction."

**Messages queued before the start** stay queued. With `then=send` (the default), the host dispatches them in the same prompt as the After Compact reminder, as it does now. **Messages already sent but not yet delivered** when the cycle starts are held by the host (`deliver(hold=True)` while active) and go out after completion. The compose box keeps its text when a submit is refused.

## 4. The indicator

The eye is blue whenever `compaction` is active. `TUI.compacting()` reads that column instead of `host_command` and the activity prefix. Because the hub writes `requested` in the same transaction as the press, the eye turns blue on the next refresh after the press. It stays blue through the gap before the ask goes, through the notes turn, the hand-over to `/compact` and `/compact` itself, and through any wait for a running turn. It goes back to red or idle only at `completed`, `failed` or `cancelled`. The activity line keeps its words (`compacting: collecting notes`, and so on), but colour no longer depends on them.

## 5. The task item

The wheelhouse posts it itself (`Store.post_marked_task`, marked `{"from": "compaction"}` the way the context nudge is marked), when the cycle enters `requested`, after the cut-off is taken, so the #150 lock never covers it. It is left out of the After Compact reminder, and the model never sees it.

- Title: **"Compact {name}: {k}k context"**.
- `requested`: running, note "asked for by {origin}".
- `preparing`: running, note "collecting notes".
- `compacting`: running, note "compacting".
- `completed`: done, note "{before}k → {after}k".
- `failed`: blocked, note with the reason (for example "no result in 10 minutes", "host stopped", "error: {text}"). It stays in the inbox for the person to dismiss.

**The notes question.** When the notes turn passes `NOTES_TIMEOUT` (120 s, one constant in `host.py`) after Claude Code took the ask up, nothing is cancelled. The host posts a question of its own, marked `NOTES_WAIT` (and, from PR 2, under the compaction task), so the person sees it even away from the app. It has two buttons over the answer box, **Keep waiting** (Alt+Y) and **Abort** (Alt+D); a typed reply is refused like any send. Keep waiting closes it, and the host asks again after another `NOTES_TIMEOUT`. Abort interrupts the notes turn, which ends the cycle as `cancelled` with no `/compact`; the session is unlocked and queued drafts stay queued. Unanswered, the session stays locked and blue, but it can't stick: the cycle still ends when the notes turn ends (the question closes itself as `/compact` goes), on an Interrupt, or when the session dies.
- `cancelled`: closed, note "cancelled by Interrupt".

The context nudge question, if open, closes on `completed` as now.

## 6. Stage lines in the conversation

The stages are taken from `compaction_stages` and placed by time among the transcript's entries (`transcript.blocks`), and in the task item's thread. They need a new block kind, `stage`, rendered as plain Rich `Text` in the compaction blue (`#2f8cff`), never as Markdown. I checked: Markdown turns `*** PREPARING ***` into a horizontal rule or bold, and turns `- --` into a bullet. Each line is 36 cells or fewer (measured with `rich.cells.cell_len`). The token counts and time go on a second, dim line. The conversation pane's width in an 80-column terminal still has to be confirmed in a Textual pilot when this is built; lines are cut with an ellipsis rather than wrapped.

**A. Banner**

```
━━━ ◆ PREPARING FOR COMPACTION ◆ ━━━
━━━━━ ◆ COMPACTION INITIATED ◆ ━━━━━
━━━━━ ◆ COMPACTION COMPLETED ◆ ━━━━━
━━━━━━ ◇ COMPACTION FAILED ◇ ━━━━━━━
```

**B. Trash compactor** (detention level, walls closing in)

```
▐█          █▌ walls closing in
▐████    ████▌ compaction initiated
▐████████████▌ compacted
▐█          █▌ 3PO, shut them down!
```

**C. Ludicrous speed**

```
·  ·  ·  ·  ·  preparing to compact
- -- --- --->> compaction initiated
════════════>> ludicrous speed: done
·  ·  ·  ✕     compaction dropped
```

The `requested` state shows no line of its own. It is usually under a second, and the eye covers it.

## 7. Rework or rewrite, size, tests

It is a **rewrite of the host's compaction driver, and a rework of everything else**. The ten host fields and their scattered checks are replaced by `Cycle` plus the store state. `poll`, `finished`, `compact_parked`, `ending_wins`, `hold_expired` and `cold_due` lose their compaction branches and call the driver instead. The store, hub, TUI and transcript changes are additions. Rough size: host about −200 and +250 lines, store +150, hub +60, TUI +80, transcript +40, tests about +600. It would be two stacked PRs:

1. State, guards, driver, indicator and both root-cause fixes.
2. The task item and stage lines.

Test plan (parameterised tables in `test_host`, `test_store`, `test_hub` and `test_tui`):

- **One sequence per start route** (person, model, relaunch, park, quiet, auto): request, ask, notes, `/compact`, completed, `then`. Assert the state and eye at each step, and that nothing else was sent.
- **Every blocked action × every active state**: refused, toast wording, store unchanged. Allowed actions (Interrupt, "Just stop it", answering a waiting permission or question) succeed.
- **Each end**: notes turn without a block, error result, the 2 min notes question (Keep waiting, Abort) and the 10 min cap, host exit, hub sweep, Interrupt in each state. Assert a terminal state, the eye no longer blue, and drafts intact.
- **Both 29 Oct repros**, made into regression tests: Ctrl+S during `preparing` is refused, and a second Compact during `compacting` is refused with the first cycle completing as `driven`.
- **Queued and undelivered messages** go out after `completed` with `then=send`, and stay after `failed` or `cancelled`.
- **Stage blocks** render as Text at 36 and 76 cells, with no Markdown.

## 8. Decided

- **Q1, art: B, the trash compactor.** While the cycle is preparing or compacting, the conversation also shows short panicked exchanges between the crew (Luke, Leia, Han, Chewie) and the droids (R2-D2, C-3PO), drawn at random from a pool of over 80 original lines (`compactor.py`), with no repeats within a cycle: one every 20 to 30 s, and one at each stage change. Same blue style as the stage lines, each 76 cells or fewer, plain Text, only in the conversation view.
- **Q2: yes,** auto-compaction gets the task item too, for visibility.
- **Q3:** the notes turn's cap is 2 minutes, and at that point the person is asked whether to keep waiting or abort, never cancelled unasked (section 5).
- **Q4: yes,** a wheelhouse PreCompact hook for terminal tabs (PR 2).
- **Q5: refuse** Stop & Forget's "Let it know" during a cycle; "Just stop it" stays allowed and ends the cycle with the session.
