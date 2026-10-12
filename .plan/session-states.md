# Session states (design)

Four states a person can reason about, a verb for each move between them, and a caption
that says what the verb really does. Restore goes; Relaunch covers it. Issue #16.

**Status: for Doug's review. No code yet.** Grounded in `doug/16-beta-1` at d5db021.
Decided: Doug's proposal, Q83 (A), Q84 (B) and the refinements below. Open: one question,
how the wheelhouse stops a tab's Claude Code (see "Open question").

## Where we are

- `liveness.status` gives live, stalled, starting or dead, always from the process
  (`claude_pid`, `claude_start`, `boot_id`). Being parked is a separate `parked` flag that
  "never hides whether a session is running", so a parked session can still be live.
- Park sets the flag (the session is asked first if running). It keeps running in its tab
  ("the person can close this tab"). In SDK mode the host already exits once its row is
  parked (`host.py` serve loop and `shell_wait`).
- End asks the session to do its end steps, then `end_session` deletes the row.
- Restore (`S`) brings back a dead session; Restore all (`Shift+S`) every dead one that
  isn't parked (`hub.restorable`). Relaunch (`L`) stops a running SDK host and starts it
  again; on a tab it says `TAB_RELAUNCH` ("/exit it there, then Relaunch").
- Adopt (`a`) lists transcripts on disk plus tracked sessions whose process has gone, so
  it overlaps Restore. It refuses a running session ("type /exit in its tab").

## States

The wheelhouse stores one thing: whether a row exists, and its `parked` flag. Active and
Available are the same stored row, told apart by liveness.

| State     | Stored                  | Process            | Shown                              |
|-----------|-------------------------|--------------------|------------------------------------|
| Active    | row, `parked = 0`       | running            | normal; decorations below          |
| Available | row, `parked = 0`       | none               | its own colour (today's red)       |
| Parked    | row, `parked = 1`       | none, by intent    | dimmed, at the foot of the list    |
| Forgotten | no row                  | anything           | not listed; offered by Adopt       |

Health is not a state. These decorate an Active session: starting, stalled, busy,
relaunching, park requested, forget requested, needs relaunch (code older than
`PROTOCOL_VERSION`).

Active becomes Available by itself when the process dies or the machine reboots (the boot
id changes). That is the only move the wheelhouse makes without the person.

Parked means "not running, and not meant to be" (Q83). Keepalive, Relaunch all and the
relaunch offer on following a session all skip it, as they skip the flag today.

## Transitions

Captions follow whether a process runs: Active shows "Stop & Forget"; Available and
Parked show "Forget". It is one action underneath (Q84).

| From      | Verb (key)                  | To        | Caption shown      | Confirm                    |
|-----------|-----------------------------|-----------|--------------------|----------------------------|
| Active    | Park (P)                    | Parked    | Park               | no; asks the session       |
| Active    | Forget (E)                  | Forgotten | Stop & Forget      | no; asks the session       |
| Active    | Relaunch (L)                | Active    | Relaunch           | if busy (kills the turn)   |
| Available | Park (P)                    | Parked    | Park               | no                         |
| Available | Forget (E)                  | Forgotten | Forget             | yes (deletes its data)     |
| Available | Relaunch (L)                | Active    | Relaunch           | no                         |
| Parked    | Unpark (P) = Relaunch (L)   | Active    | Unpark             | no                         |
| Parked    | Forget (E)                  | Forgotten | Forget             | yes (deletes its data)     |
| Forgotten | Adopt (a)                   | Active    | Adopt              | the Adopt dialog           |

On a Parked session the Relaunch button is captioned Unpark, and P and L both press it.
There is no separate Park button there. Rename and Model are unchanged.

A pending request (park or forget) is pressed again to choose, as today: `C` cancels, `F`
forces. Forcing on an Active session stops the process now, without the session's steps.

## What each verb does underneath

**Park on Active.** `store.request(sid, "park")` as today. The request text changes from
"bring your items up to date" to "write your handoff notes into your items, then call
`park_session`". `park_session` sets `parked = 1`; then the process stops:
- SDK: the host already exits once its row is parked. No change.
- Tab: needs a way to stop Claude Code (open question).

**Park on Available.** Sets `parked = 1` at once. There is no process to ask for notes.

**Forget on Active (Stop & Forget).** Today's End steps: the request, the session's own
end steps (memory, handoff), then `end_session` deletes the row. Then the process stops,
the same way as Park. A forced Forget stops the process first, then deletes the row.

**Forget on Available or Parked.** `store.end(sid)` at once, after the confirm. The
confirm keeps today's guard (`act_on_dead`): if the session came back meanwhile, nothing
happens and it says so.

**Relaunch on Active.**
- SDK: today's `stop_host` path: SIGTERM the host (checked by pid and start time), then
  `finish_relaunches` starts it again once gone. The compact-first choice stays for big
  contexts. The confirm becomes "only if busy", where today it always asks.
- Tab: today refused with `TAB_RELAUNCH`. Needs a way to stop Claude Code (open
  question); after that, `finish_relaunches` opens a new tab exactly as for a host.
- SDK session handed to a shell tab: stopping the tab's Claude Code hands it back to its
  waiting host (`host.shell_wait`), which is the relaunch. Nothing else to do.

**Relaunch on Available or Parked.** Today's `hub.relaunch`: `launch.restore_session`
(resume by id, the way new sessions run now) and `set_parked(sid, False)`.

**Relaunch all (Shift+L).** Relaunch on every Available session, as `hub.restorable`
picks them today. Parked and Active sessions are left alone.

**Adopt.** Today's `adopt.adopt`, offered only for sessions with no row: forgotten or
never known. The tracked-but-dead entries leave the list, since Relaunch covers them. The
adopted session gets `JOINED_TEXT`, which asks it to post its open questions and running
tasks and set its synopsis: that is the interrogation that rebuilds its items. Adopting a
running session still needs it stopped first; whichever stop mechanism we pick for tabs
can replace "type /exit in its tab" there too, but that is a follow-up, not this change.

## Open question: stopping a tab's Claude Code

Relaunch on an Active tab and Park on a tab both need the tab's Claude Code to exit. What
the code gives us: the tab runs `exec python -m claude_wheelhouse run <id>`, and `run`
registers its own pid and then `exec`s `claude`, so `claude_pid` is the Claude Code
process itself, with `claude_start` and `boot_id` to guard against pid reuse. The MCP
server is Claude Code's child, so `os.getppid()` there is the same process. Whether a
tab closes when Claude Code exits on a signal depends on Windows Terminal's
`closeOnExit` (inferred: a non-zero exit may leave the tab open showing the exit code).

- **A. Keep waiting for /exit.** Relaunch arms and starts the new tab once the person has
  typed /exit; Park asks them to. No new kill path, but a Parked tab can still be running,
  which breaks "Parked means not running", and both verbs stay two-step on tabs.
- **B. The hub signals the pid.** `stop_host` loses its tab check and SIGTERMs any
  registered Claude Code, guarded as now. One mechanism for Relaunch, Park and Forget.
  Forget deletes the row the hub would read the pid from, so `end_session` would have to
  leave a mark for the hub to stop and delete, which changes the store's end path.
- **C. The session stops itself.** `park_session` and `end_session` SIGTERM their parent
  a second after replying, so the tool result reaches the transcript. Relaunch would need
  a new `exit_session` tool the session is asked to call, which a busy or stuck session
  never reaches, and that is when people relaunch.
- **D. B for Relaunch and forced actions, C for Park and Forget.** (Recommended.) Each
  actor stops the process at the moment it knows is right: the session once its notes are
  written (which also covers `/wheelhouse park` and `/wheelhouse end` typed in the tab),
  the hub when the person overrides. Both send the same signal to the same verified pid.

What each answer changes: A keeps the state model with an exception and adds no code to
stop processes. B adds a store change to Forget. C leaves Relaunch on a tab as today. D
adds about 15 lines to `mcp_server.py` and removes the tab refusal from `hub.stop_host`
and `compact_then_relaunch`.

Before coding B, C or D, a short spike confirms: Claude Code exits cleanly on SIGTERM,
the transcript resumes with `--resume`, and what the tab shows afterwards. If SIGTERM
misbehaves, SIGINT twice (Claude Code's Ctrl+C exit) is the fallback to test.

## Keys

| Key       | Today                  | After                                   |
|-----------|------------------------|-----------------------------------------|
| L         | Relaunch (running)     | Relaunch (Active, Available); Unpark    |
| P         | Park or Unpark         | Park; Unpark on a Parked session        |
| E         | End                    | Stop & Forget, or Forget                |
| a         | Adopt                  | Adopt (sessions with no row only)       |
| S         | Restore                | unbound                                 |
| Shift+S   | Restore all            | unbound                                 |
| Shift+L   | (free)                 | Relaunch all (Available only)           |
| R, M, ... | unchanged              | unchanged                               |

E stays for Forget: F is already Show finished, and the caption carries the meaning.
S and Shift+S stay unbound for a release, so muscle memory does nothing rather than
something new.

## Data migration

No schema change: the states come from the row, `parked` and liveness.
- Dead rows with `parked = 0` become Available. Nothing to do.
- Dead rows with `parked = 1` become Parked. Nothing to do.
- Running rows with `parked = 1` (parked under today's rules, tab left open) are set to
  `parked = 0` once, at the first hub start on the new code, so they show as Active
  rather than being stopped by surprise. Logged per session.
- Pending `park_requested_at` and `end_requested_at` keep their columns. The column names
  stay; "end" becomes "forget" in the UI and messages only.
- The daemon API gains `/relaunch-all` and drops `/sessions/<id>/restore` and
  `/restore-all`. Clients and daemon ship together, as now.

## What leaves the UI and docs

- `tui.py`: the Restore button (`LIFECYCLE`, `lifecycle_buttons`), `BUTTONS["restore"]`,
  the `restore` and `restore_all` help lines and bindings, and the `WHOSE` text.
- `hub.py`: `restore`, `restore_all` (folded into Relaunch and Relaunch all), the
  "Restore it" wording in `finish_relaunches`, and `TAB_RELAUNCH` if the answer is B or D.
- `launch.py` and `adopt.py`: "type /exit ... then restore it" messages; tracked dead
  sessions in `adopt.candidates`. Internal names (`restore_session`, `restore_runner`)
  become `resume_*` in a separate mechanical commit.
- `mcp_server.py`, `monitor.REQUEST_TEXT`, `SKILL.md`, `protocol_tab.md`,
  `protocol_sdk.md`: park asks for handoff notes; "the person can close this tab" becomes
  "this tab closes". End wording becomes Forget where the person reads it.
- `README.md` (11 lines mention restore), and the tutorial checklist.

## Tests

Parameterised, one case per line with a description, in the existing files:
- `test_hub.py`: the transition table as data: (state, verb) to (state, caption,
  confirm), including the Parked Relaunch caption "Unpark".
- `test_tui.py`: `lifecycle_buttons` per state; keys L, P, E, Shift+L; S and Shift+S do
  nothing; the busy confirm on Relaunch and none when idle.
- `test_lifecycle.py`: Forget on Available and Parked deletes at once; on Active it
  requests; the confirm's came-back guard; forced Forget stops then deletes.
- `test_hub.py`: Relaunch all picks Available only (not Parked, not Active, not starting).
- `test_host.py`: keepalive skips Parked (exists; keep).
- `test_adopt.py`: candidates exclude every tracked row.
- Store migration: running and parked becomes Active; dead and parked stays Parked.
- Per the open question: the stop path (fake `/proc`, pid and start time guard, the
  parent signalled only after the tool result returns, the tab case in `stop_host`).
