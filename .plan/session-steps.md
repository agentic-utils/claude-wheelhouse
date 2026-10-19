# Session steps: compaction on our terms, and informal hooks anyone can opt into

Two things in one design (#122). First, keep sessions warm and compact them on the
wheelhouse's terms rather than Claude Code's (Doug's Q142). Second, turn the "before
compaction" ask the wheelhouse already makes into a concept any user can switch on, not
only one whose CLAUDE.md says what to do. Doug: "let's also think about how we generalise
this for other wheelhouse users who don't have the same long-term memory setup as me.
Basically we've got an informal precompact hook here. How do we create that concept within
wheelhouse that allows a user to opt in easily?"

A **step** is a prompt the wheelhouse gives a session at a lifecycle event (before Compact,
before Forget, on resume), chosen per event from a preset or the user's own instructions.
The default preset keeps one **handoff** per session: a short markdown file the session
rewrites each time, shown in the inbox as its doc item.

## Where we are

- Compact (SDK hosts only: `tui.SDK_BUTTONS`, tui.py:3429) sends `COMPACT_ASK`
  (host.py:46-50): do what your own instructions say before compaction, then reply with
  notes inside `<keep id="nonce">`. `keep_notes` (host.py:125) finds the block, and the turn's
  end sends `/compact <notes>` (`finished`, host.py:854-861). Notes typed by the person skip
  the ask (host.py:595-599). No notes in `COMPACT_TIMEOUT` (600 s, host.py:43) of quiet:
  nothing is compacted, said as `NO_NOTES`.
- `compact_then` is just compact, send the queued answers, or park. Park's Compact first,
  within `PARK_COMPACT_WITHIN` (3600 s, host.py:68), sends a bare `/compact` after the park
  turn (host.py:703): no ask.
- Past `NUDGE_TOKENS` (200k, host.py:65) the host posts one "context 200k: compact?"
  question. Hosts only: tab sessions get no nudge.
- After a compaction the session is told its open items (`launch.after_compaction`,
  launch.py:67): by the host for SDK sessions, and for tabs by the plugin's SessionStart hook
  with matcher `compact` (plugin/hooks/hooks.json), which runs `claude-wheelhouse open-items`.
- Keepalive: a session over `KEEPALIVE_TOKENS` (150k), quiet, and waiting on the person or
  with an item in doubt, gets a one-word turn `KEEPALIVE_AFTER` (55 min, host.py:76) after
  its last, until quiet `features.keepalive_hours()` (8 h). The host sends its own; the hub
  sends tab sessions theirs as a notice (`Hub.keep_warm`, hub.py:1034).
- Stop & Forget already says "If your own instructions describe anything to do when a
  session ends, do it first" (`monitor.REQUEST_TEXT["end"]`, monitor.py:87). Park asks for
  handoff notes in the items. Both are informal hooks already, with fixed wording.

How tab sessions are driven matters below: everything reaches a tab as a notice through the
monitor (the plugin's notification stream) or as a tool result. The wheelhouse cannot type a
slash command into a tab, so it cannot run `/compact` there.

## Keeping warm and compacting on our terms (Q142)

Claude Code 2.1.299 compacts an interactive session idle about 54 min with at least 200k
tokens (`CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS`, at least 100k), unless `"idleCompaction":
false`. It fires PreCompact with trigger `auto` and no custom instructions, so nothing of the
wheelhouse's ask runs. SDK hosts aren't interactive, so it never applies to them.

As decided for SDK hosts:

- `KEEPALIVE_AFTER` goes to 50 min, for hosts and the hub alike (one constant), and
  protocol_tab.md's "about every 55 minutes" with it.
- When the keepalive window ends (quiet `keepalive_hours()`), the host runs its own Compact
  instead of letting the cache go cold: the Before Compact step and the keep ask, as the
  button does. Only for a session the keepalive was keeping warm (over 150k, waiting on the
  person or in doubt); others were never kept and are left as today.

For tab sessions the decision as written has a gap: with idle compaction off, nothing can
compact a tab at the 8 h mark, because only the person can type `/compact` there. This design
proposes, pending Doug's nod (Open 1): tabs keep idle compaction **on**, launched with
`CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS=150000` to match `KEEPALIVE_TOKENS`. The 50 min keepalive
beats the 54 min idle compaction while the window lasts. At the 8 h mark the hub sends the
Before Compact step as a notice (the handoff is written), sends no more keepalives, and
Claude Code compacts about 54 min later with the cache still warm. The handoff carries what
the keep notes would have: the SessionStart `compact` hook puts it back in the context. The
cost: a quiet tab not waiting on anyone idle-compacts at 54 min with no step, as it does
today. That a keepalive turn arriving by notice resets Claude Code's idle timer needs a
short spike before this lands. If Doug keeps Q142 as written, `claude_argv` (launch.py:161)
adds `--settings '{"idleCompaction": false}'` for tabs instead.

## Steps

### The events

| Event | Fires on | Default | Modes |
|---|---|---|---|
| Before Compact | the Compact button, the 200k nudge's Compact now, the 8 h mark, Relaunch's compact first, Park's Compact first | handoff | off, handoff, own |
| Before Forget | Stop & Forget's request | final handoff, then close or hand over open items | off, handoff, own |
| On resume | unpark, Relaunch, Restore (`launch.JOINED_TEXT`, launch.py:79), after an hour or more away | read the handoff, report what changed, update items | off, handoff, own |
| Session start | a new session's opening prompt | off: the last handoff of an ended session in the same directory | off, handoff |
| After Compact | not a step: always on | the reminder adds the handoff | none |

Not proposed: Fork (the fork carries the conversation, and its own id gets its own handoff
on its first step) and PR merged (an action, not a lifecycle event: parked with the PR
pane's ideas). Plain Park keeps its current text: parking with Compact first runs Before
Compact, and the next life reads the handoff On resume. The table is the proposal Doug asked
for ("Session start? Park/unpark? Forget? Post-compact?") and waits on his nod (Open 2).

### Modes and prompts

One table in a new module, `steps.py`: `PRESETS[event][mode] -> text`, plus `run_mode(event)`
reading the setting. Every step's text starts `[wheelhouse] ` and is complete on its own, so a
session needs no protocol line to follow it. Drafts:

- Before Compact, handoff: "Before compaction, call write_handoff with a terse handoff:
  what was done, decisions and why, open threads, and the next step." Then the keep ask, as
  now.
- Before Compact, own: today's sentence, "If your own instructions say to do anything before
  compaction (save a transcript or handoff note, say), do it now." Then the keep ask.
- Before Compact, off: the keep ask alone.
- Before Forget, handoff: "Before this session ends, call write_handoff with a final
  handoff, then close each open item with update_item, or say in its body who or what it's
  handed to. Then call end_session." Own is today's `REQUEST_TEXT["end"]`; off drops the
  first sentence.
- On resume, handoff: JOINED_TEXT, then "Your handoff, written <time UTC>:", the file inline,
  then "Check what changed while you were away (git status, new commits on your branch and
  its base, PRs merged), update your items to match, and call write_handoff if anything
  moved." Own: "If your own instructions say what to do when a session resumes, do it now."
- Session start, handoff: the opening prompt gains "The last session in this directory,
  <name>, ended <time UTC> with this handoff:" and the file inline, newest first, one only.

The handoff is inlined, not left to a path, so a session never needs permission to read the
state directory. A tab's notice past the monitor's limit is cut short and points at
`get_input`, as any long notice is.

### A new tool: write_handoff

`write_handoff(text)` on the wheelhouse MCP server writes the session's handoff file
atomically (a temporary file, then rename) and posts its doc item, or re-posts it, which
marks a read one `updated` (.plan/docs-in-inbox.md). Over 16 KB is refused, saying so. A
tool, not a file write by the session: no permission prompt for a path outside the project,
no path to get wrong, front matter the wheelhouse controls, and a call the wheelhouse can
see, which is how a step's handoff is detected.

### Done, timeouts and fallbacks

A step never holds up its event longer than the event already allows. When a step fails or
times out, the event goes ahead as it would with the step off, and the activity line says so.

| Step | Done when | Timeout | Then |
|---|---|---|---|
| Before Compact, button or nudge | the keep block arrives (as now) | `COMPACT_TIMEOUT` quiet | nothing compacted, `NO_NOTES` (the person is there to try again) |
| Before Compact, 8 h mark (host) | the keep block | `COMPACT_TIMEOUT` quiet | a bare `/compact`: the alternative is a cold cache |
| Before Compact, Park's Compact first | `park_session` (the step rides in the park request) | as Park today | the bare `/compact`, as now |
| Before Forget | `end_session` | as today | as today: the person can force it |
| On resume, Session start | the turn ends | none | nothing waits on it |

Whether the handoff was written is a separate check: `write_handoff` called since the step
was sent. A keep block without one still compacts; the activity line ends "(no handoff
written)" so the person sees a session that ignores the preset. Interrupt cancels a step as
it cancels Compact now.

## SDK hosts and tab sessions

| Event | SDK host | Tab |
|---|---|---|
| Before Compact, button, nudge, Relaunch | the host's turn | none: no Compact button, no nudge |
| Before Compact, 8 h mark | the host's turn, then `/compact` | the hub's notice; Claude Code's idle compaction (Open 1) |
| Before Compact, Park's Compact first | in the park request | in the park request; the tab doesn't compact (as now) |
| Before Forget | the host's turn | the monitor's notice |
| On resume | appended to JOINED_TEXT | appended to JOINED_TEXT |
| Session start | opening prompt | opening prompt |
| After Compact | `after_compaction`, by the host | `after_compaction`, by the SessionStart hook |

A compaction the person types in a tab, or Claude Code's at full context, isn't the
wheelhouse's to drive. PreCompact can block (exit 2) but can't run a model turn, so no step
can run there. The best effort is after it: `after_compaction` already re-posts the open
items, and now adds the handoff inline with its age, plus, when the compaction wasn't the
wheelhouse's (no step sent for it), "this compaction was automatic: the full conversation
before it is in <transcript path>; call write_handoff once you have your bearings." The 200k
nudge stays the real defence for hosts. Blocking a typed `/compact` until a fresh handoff
exists is possible with PreCompact, and parked: it would surprise anyone without the preset.

## The handoff file

- `db_path().parent / "handoffs" / "<session id>.md"`, so `~/.local/state/claude-wheelhouse/
  handoffs/` by default, and a test or second database gets its own directory.
- Named by session id: stable across a rename, distinct for a fork.
- One per session, rewritten each time (Q144). Front matter, written by the tool: `session`,
  `name`, `cwd`, `updated` (UTC). Session start needs it: Stop & Forget deletes the session's
  row (`Store.end`, store.py:796), and the file outlives it.
- Kept after Forget: Session start reads it. Files untouched for 30 days are removed when the
  hub starts, said once in its log.
- The doc item's title is "handoff", pinned by default as every doc is (#110).

## Settings and the first-press dialog

Each event's mode is a setting, resolved as `features.given` resolves the others (Doug's
Q80 order): `--step EVENT=MODE` (repeatable), else `WHEELHOUSE_STEP_<EVENT>`, else
`STEP_<EVENT>=` in either .env; then the choice made in the app, stored in the `settings`
table (store.py:156) as `step.<event>`; then the table's default. `STEPS=own` (and
`--steps own`, `WHEELHOUSE_STEPS`) sets every event at once, for a user like Doug whose
CLAUDE.md says it all. A bad value is said once, naming where it was given, and taken as
the default.

The first time anyone starts a Compact (any of the Before Compact triggers a person presses)
with no `step.chosen` setting and nothing given by flag or .env, a small dialog comes first:

> Before compacting, the wheelhouse can ask the session to write a handoff: what was done,
> decisions, open threads and the next step. It shows in the inbox as the session's doc,
> and is read back after compaction, on resume and when the session ends.
> [Write a handoff] [Follow my own instructions] [Nothing extra]

The choice sets every event: handoff gives the table's defaults, own sets own wherever it is
offered (Session start stays off), nothing sets all off. Then the Compact goes ahead. Esc
cancels the Compact and asks again next time. Until it is answered, unattended events (the
8 h mark, Forget, resume) run the table's defaults (Open 3).

Changing it later: a command palette entry, "Session steps", opens a dialog with one row per
event and its three-way choice, showing a value given by flag or .env as fixed, with where
from. The ? overlay lists the palette entry. Through the daemon: `GET /settings/steps` and
`PUT /settings/steps/{event}`.

## Protocol and versions

- protocol.md: one paragraph. "The wheelhouse may send a `[wheelhouse]` step before or after
  a lifecycle event (a handoff before compaction, say). Do it as written. Your handoff is
  yours: write it with write_handoff, never by file, and keep it short enough to read at a
  glance." protocol_tab.md: the keepalive's 50 minutes.
- `write_handoff`'s docstring, and every step's text, join `claude-wheelhouse protocol`'s
  listing (launch.injected), so a user can read all of it before switching steps on.
- `PROTOCOL_VERSION` (store.py:74) is 16 in the batch, and #111 plans 17 (.plan/pr-pane.md).
  This takes 18; if it lands before #111, it takes 17 and #111 rebases. Whichever lands
  second takes the next number.
- `fingerprint.API` (fingerprint.py:13) is 6 in the batch; #111's doc says 6, written before
  #110 took it, so it takes 7 when it rebases. This takes 8 on the same rule: the new
  settings routes.

## Mixed versions

- A session on protocol 17 or older has no `write_handoff`. It is never sent a handoff step:
  Before Compact sends the own or off text, Forget today's text, and resume and start the
  plain prompts. `needs_relaunch` already flags it.
- A host on older code keeps today's `COMPACT_ASK` and ignores the settings until relaunched.
  New hosts read the mode when the event fires, so a change in the dialog applies at once.
- A tab launched before the change runs as today (no idle tokens setting) until relaunched.
- A remote TUI on API 7 or older is refused by the fingerprint, as with every bump.

## Tour and what's new

One `whatsnew.Feature`, "session-steps", on the session pane's tour stop (where Compact
sits; `when=""`, as tabs get Forget, resume and the 8 h handoff too): "Steps: before
Compact, Forget and on resume, the wheelhouse can have a session write a handoff, shown as its
doc. Choose in Session steps (command palette)." The 50 min keepalive and the 8 h compaction
are ADVANCED: release notes only.

## Tests

Parameterised tables: the text per event, mode and protocol version; the timeout table;
`write_handoff`'s write, re-post and size cap; setting precedence; `after_compaction` with
and without a handoff; the dialog once, Esc again; Session start's newest-handoff pick.

## Decided

- `write_handoff` as an MCP tool rather than the session writing a path: no permission
  prompts, detectable, wheelhouse-owned front matter.
- The handoff is inlined into resume, start and after-compaction prompts, never left as a
  path to read.
- An unattended 8 h Compact without notes falls back to a bare `/compact`; a pressed one
  still compacts nothing.
- For a compaction the wheelhouse didn't drive, the after-compaction reminder names the
  transcript path, instead of a doc item pointing at the `.jsonl` (the doc viewer renders
  markdown, and a transcript reads badly there).
- On resume runs only after an hour or more away (parked, or the last response over an hour
  ago), so a quick relaunch for a version bump costs no extra turn.
- Plain Park keeps its own text; Park with Compact first carries the Before Compact step.
- Handoffs untouched for 30 days are removed at hub start.

## Open for Doug

1. Tabs and the 8 h compaction. The wheelhouse can't run `/compact` in a tab, so Q142 as
   written leaves tabs with no compaction at 8 h. Proposed: tabs keep idle compaction on (with
   its threshold at 150k), the 50 min keepalive beats it during the window, the hub sends the
   handoff step at 8 h, and Claude Code compacts about 54 min later while warm. Cost: a quiet
   tab not waiting on anyone idle-compacts at 54 min with no step, as today. Or: idle off per
   Q142, and at 8 h a tab gets the handoff and its cache goes cold (the next turn pays a full
   cold write).
2. The event table: Before Compact and Before Forget on, On resume on, Session start off,
   After Compact built in; Fork and PR merged not proposed.
3. Before the first-press dialog is answered, the unattended events (8 h mark, Forget,
   resume) run the defaults (handoff on), so a new user's sessions start making handoff
   turns unasked. Or: off until the dialog is answered.
