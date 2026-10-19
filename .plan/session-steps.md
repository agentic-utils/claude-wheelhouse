# Session steps: compaction on our terms, and informal hooks anyone can opt into

Two things in one design (#122). First, keep sessions warm and compact them on the
wheelhouse's terms rather than Claude Code's (Doug's Q142). Second, turn the "before
compaction" ask the wheelhouse already makes into a concept any user can switch on, not
only one whose CLAUDE.md says what to do. Doug: "let's also think about how we generalise
this for other wheelhouse users who don't have the same long-term memory setup as me.
Basically we've got an informal precompact hook here. How do we create that concept within
wheelhouse that allows a user to opt in easily?"

And on how steps relate to the user's own instructions: "there's no point fighting
CLAUDE.md so essentially every one should offer behaviours in addition to what's in
CLAUDE.md, not instead of. There should be an option, selected by default, for wheelhouse to
declare that the event is about to happen and allow the session to respond, thus enabling
any instructions in CLAUDE.md to take effect."

So a **step** is what the wheelhouse says to a session at a lifecycle event, in two layers:

- **Announce**, on by default for every event: the session is told the event is about to
  happen (or has happened) and is given the chance to respond, so whatever its CLAUDE.md
  says about that event takes effect.
- **Add-ons**, built-in behaviours layered on top, each switched on or off. The main one
  keeps a **handoff** per session: a short markdown file the session rewrites each time,
  shown in the inbox as its doc item.

Add-ons never replace the announce; they ask for something extra in the same message.

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
- Stop & Forget already announces: "If your own instructions describe anything to do when
  a session ends, do it first" (`monitor.REQUEST_TEXT["end"]`, monitor.py:87). Park asks for
  handoff notes in the items. Both are informal hooks already, with fixed wording.

Everything reaches a tab session as a notice through the monitor (the plugin's notification
stream) or as a tool result. The wheelhouse cannot type a slash command into a tab, so it
cannot run `/compact` there.

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
compact a tab at the 8 h mark, because only the person can type `/compact` there. Proposed,
pending Doug's nod (Open 1): tabs keep idle compaction **on**, launched with
`CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS=150000` to match `KEEPALIVE_TOKENS`. The 50 min keepalive
beats the 54 min idle compaction while the window lasts. At the 8 h mark the hub sends the
Before Compact step as a notice (announce, and the handoff if that add-on is on), sends no
more keepalives, and Claude Code compacts about 54 min later with the cache still warm. The
handoff carries what keep notes would have: the SessionStart `compact` hook puts it back in
the context. The cost: a quiet tab not waiting on anyone idle-compacts at 54 min with no
step, as it does today. If Doug keeps Q142 as written, `claude_argv` (launch.py:161) adds
`--settings '{"idleCompaction": false}'` for tabs, and a tab past 8 h gets its step and then
goes cold.

**The test**, before this lands: does a turn started by a monitor notice reset Claude Code's
idle timer? One throwaway tab, from a scratch wheelhouse (`WHEELHOUSE_DB` and
`WHEELHOUSE_PORT` of its own, never the live database), launched with
`CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS=100000`, its context padded past 100k by reading a few
large files. Then a notice from the scratch store at 40 min, and nothing more. The transcript
shows the answer: a compact boundary about 54 min after the notice's turn means the notice
resets the timer; one about 54 min after the padding turn means it doesn't, and the
keepalive can't hold idle compaction off. About 100 min of wall time and one cached 100k
read. A second tab with no notice is the control.

## Steps

### The events

| Event | Fires on | Announce says | Add-ons (default) |
|---|---|---|---|
| Before Compact | the Compact button, the 200k nudge's Compact now, the 8 h mark, Relaunch's compact first, Park's Compact first | which of those it is, then the keep ask | write the handoff (on) |
| Before Forget | Stop & Forget's request | the session is about to end (today's text) | final handoff (on); close or hand over open items (on) |
| On resume | unpark, Relaunch, Restore (`launch.JOINED_TEXT`, launch.py:79) after an hour or more away | the session is resumed, and how long it was away | the handoff inline (on); report what changed and update items (on) |
| After Compact | the session's first turn after any compaction | the session was just compacted, and whether the wheelhouse drove it (`after_compaction` and the SessionStart hook, today's open items included) | the handoff inline (on) |
| PR merged | one of the session's PR items (#111) turns `merged` | which PR merged | clean up its worktree and branch (off) |
| Session start | a new session's opening prompt | none: Claude Code's own start already applies CLAUDE.md | the last handoff of an ended session in the same directory (off) |

Not proposed: Fork (the fork carries the conversation; its own id gets its own handoff on its
first step). Plain Park keeps its current text: parking with Compact first runs Before
Compact, and the next life gets the handoff On resume.

**PR merged and a parked session.** Telling a parked or stopped session means starting it,
which writes its whole context to the cache cold: a 300k session costs about a full cold
read for the one notice. So a parked session isn't started for it. The announcement waits,
kept on the session, and joins its On resume message ("while you were away, PR108 merged").
A live session is told at once, by notice or the host's turn. The worktree add-on asks the
session to remove the PR's worktree and delete its branch if both are its own and nothing
in them is uncommitted, and to say so on the PR item; it's off by default because removing a
worktree is not something to do unasked.

### Prompts

One table in a new module, `steps.py`: `ANNOUNCE[event]` and `ADDONS[event][name]`, joined
in that order, then any mechanics the event needs (the keep ask, `end_session`). Every
message starts `[wheelhouse] ` and is complete on its own, so it needs no protocol line to be
followed. Drafts:

- Before Compact, announce: "The person pressed Compact" (or "This session has been quiet
  8 hours: the wheelhouse compacts it now, while its cache is warm", and so on), then today's
  "If your own instructions say to do anything before compaction, do it now." Handoff: "Also
  call write_handoff with a terse handoff: what was done, decisions and why, open threads,
  and the next step." Then the keep ask.
- Before Forget, announce: today's `REQUEST_TEXT["end"]` first sentence. Final handoff: as
  above, final. Items: "Close each open item with update_item, or say in its body who or what
  it's handed to." Then "Then call end_session."
- On resume, announce: JOINED_TEXT, then "You were away <duration>. If your own instructions
  say what to do when a session resumes, do it now." Handoff: "Your handoff, written <time
  UTC>:" and the file inline. What changed: "Check what changed while you were away (git
  status, new commits on your branch and its base, PRs merged), update your items to match,
  and call write_handoff if anything moved." Held PR merged announcements join here.
- After Compact, announce: today's open-items reminder, plus for a compaction the wheelhouse
  didn't drive: "This compaction was automatic: the conversation before it is in <transcript
  path>." Handoff: the file inline with its age.
- PR merged, announce: "Your PR <repo>#<n> (<ref>) has merged. If your own instructions say
  what to do once a PR merges, do it now."
- Session start, last handoff: "The last session in this directory, <name>, ended <time
  UTC> with this handoff:" and the file inline, newest only.

The handoff is inlined, not left as a path, so a session never needs permission to read the
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
times out, the event goes ahead as it would have without it, and the activity line says so.

| Step | Done when | Timeout | Then |
|---|---|---|---|
| Before Compact, button or nudge | the keep block arrives (as now) | `COMPACT_TIMEOUT` quiet | nothing compacted, `NO_NOTES` (the person is there to try again) |
| Before Compact, 8 h mark (host) | the keep block | `COMPACT_TIMEOUT` quiet | a bare `/compact`: the alternative is a cold cache |
| Before Compact, Park's Compact first | `park_session` (the step rides in the park request) | as Park today | the bare `/compact`, as now |
| Before Forget | `end_session` | as today | as today: the person can force it |
| On resume, After Compact, PR merged, Session start | the turn ends | none | nothing waits on it |

Whether a handoff add-on was done is a separate check: `write_handoff` called since the
step was sent. A keep block without one still compacts; the activity line ends "(no handoff
written)", so the person sees a session that ignores the add-on. Interrupt cancels a step as
it cancels Compact now.

## SDK hosts and tab sessions

| Event | SDK host | Tab |
|---|---|---|
| Before Compact, button, nudge, Relaunch | the host's turn | none: no Compact button, no nudge |
| Before Compact, 8 h mark | the host's turn, then `/compact` | the hub's notice, then Claude Code's idle compaction (Open 1) |
| Before Compact, Park's Compact first | in the park request | in the park request; the tab doesn't compact (as now) |
| Before Forget | the host's turn | the monitor's notice |
| On resume, Session start | JOINED_TEXT, the opening prompt | the same |
| After Compact | `after_compaction`, by the host | `after_compaction`, by the SessionStart hook |
| PR merged | the host's turn, or held for resume | a notice, or held for resume |

A compaction the person types in a tab, or Claude Code's at full context, isn't the
wheelhouse's to drive: PreCompact can block (exit 2) but can't run a model turn, so no
Before Compact step runs there. After Compact still does, through the SessionStart hook,
and says the compaction was automatic. The 200k nudge stays the real defence for hosts.
Blocking a typed `/compact` until a fresh handoff exists is possible with PreCompact, and
parked: it would surprise anyone not using the handoff.

## The handoff file

- `db_path().parent / "handoffs" / "<session id>.md"`, so `~/.local/state/claude-wheelhouse/
  handoffs/` by default, and a test or second database gets its own directory.
- Named by session id: stable across a rename, distinct for a fork.
- One per session, rewritten each time (Q144). Front matter, written by the tool: `session`,
  `name`, `cwd`, `updated` (UTC). Session start needs it: Stop & Forget deletes the session's
  row (`Store.end`, store.py:796), and the file outlives it.
- Files untouched for 30 days are removed when the hub starts, said once in its log.
- The doc item's title is "handoff", pinned by default as every doc is (#110).

## Settings and the first-press dialog

Each switch is a setting, resolved as `features.given` resolves the others (Doug's Q80
order): `--step EVENT.SWITCH=on|off` (repeatable), else `WHEELHOUSE_STEP_<EVENT>_<SWITCH>`,
else `STEP_<EVENT>_<SWITCH>=` in either .env; then the choice made in the app, in the
`settings` table (store.py:156) as `step.<event>.<switch>`; then the table's default. A
switch is `announce` or an add-on's name (`handoff`, `items`, `changes`, `cleanup`).
`STEP_ADDONS=off` turns every add-on off at once, for a user like Doug whose CLAUDE.md says
it all; announce stays on. A bad value is said once, naming where it was given, and taken as
the default.

The first press of anything that fires a step with add-ons (Compact in any of its forms,
Stop & Forget, Relaunch or unpark), with no `step.chosen` setting and nothing given by flag
or .env, shows a small dialog first:

> When the wheelhouse compacts, ends or resumes a session, it tells the session first, so
> your own instructions (CLAUDE.md) can act. It can also do more:
> [x] keep a handoff for each session (what was done, decisions, open threads, next step),
>     shown as its doc and read back after compaction and on resume
> [x] before Stop & Forget, close or hand over its open items
> [x] on resume, report what changed while it was away
> [OK] (Session steps in the command palette has every switch)

OK stores the ticks and goes on with the press; Esc cancels the press and asks again next
time. Until it is answered, the events nobody pressed (the 8 h mark, PR merged, After
Compact) announce only, with their add-ons off: no one gets handoff turns they haven't seen
offered.

Changing it later: a command palette entry, "Session steps", opens a dialog with one row per
event, its announce switch and its add-ons, showing a value given by flag or .env as fixed,
with where from. The ? overlay lists the palette entry. Through the daemon: `GET
/settings/steps` and `PUT /settings/steps/{event}`.

## Protocol and versions

- protocol.md: one paragraph. "The wheelhouse tells you when a lifecycle event is about to
  happen or has happened (compaction, ending, resuming, a PR merging). Do what your own
  instructions say for it, and anything extra the message asks. Your handoff is yours: write
  it with write_handoff, never by file, and keep it short enough to read at a glance."
  protocol_tab.md: the keepalive's 50 minutes.
- `write_handoff`'s docstring and every announce and add-on text join `claude-wheelhouse
  protocol`'s listing (launch.injected), so a user can read all of it before switching
  anything on.
- PR merged needs #111's PR items, so this lands after #111. `PROTOCOL_VERSION`
  (store.py:74) is 16 in the batch and #111 takes 17: this takes 18. `fingerprint.API`
  (fingerprint.py:13) is 6 in the batch; #111's doc says 6, written before #110 took it, so
  it takes 7: this takes 8, for the settings routes. If the order changes, whichever lands
  second takes the next numbers.

## Mixed versions

- A session on protocol 17 or older has no `write_handoff`: it gets the announce alone,
  whatever the add-ons say. `needs_relaunch` already flags it.
- A host on older code keeps today's `COMPACT_ASK` and ignores the settings until relaunched.
  New hosts read the switches when the event fires, so a change in the dialog applies at
  once.
- A tab launched before the change runs as today (no idle tokens setting) until relaunched.
- A remote TUI on API 7 or older is refused by the fingerprint, as with every bump.

## Tour and what's new

One `whatsnew.Feature`, "session-steps", on the session pane's tour stop (where Compact
sits; `when=""`, as tabs get Forget, resume, PR merged and the 8 h step too): "The wheelhouse
tells a session before it compacts, ends or resumes, so its own instructions can act, and
can keep a handoff doc for it. Switches: Session steps (command palette)." The 50 min
keepalive and the 8 h compaction are ADVANCED: release notes only.

## Tests

Parameterised tables: the text per event, switch and protocol version; the timeout table;
`write_handoff`'s write, re-post and size cap; setting precedence; PR merged held for a
parked session and joined to its resume; the dialog once, Esc again, unattended events
announce-only until it is answered; Session start's newest-handoff pick.

## Decided

- Announce on and add-ons layered on it, never instead of it, per Doug. Announce can be
  switched off per event, but isn't by default.
- `write_handoff` as an MCP tool rather than the session writing a path: no permission
  prompts, detectable, wheelhouse-owned front matter.
- The handoff is inlined into resume, start and After Compact messages, never left as a
  path to read.
- Until the first-press dialog is answered, unattended events announce only; add-ons start
  once someone has seen them offered. Pressed events show the dialog first, so they never
  run unseen add-ons.
- Session start has no announce (Claude Code's own start applies CLAUDE.md); its one add-on
  is off.
- PR merged on a parked session is held for its resume, never a cold start for a notice.
  Its cleanup add-on is off.
- An unattended 8 h Compact without notes falls back to a bare `/compact`; a pressed one
  still compacts nothing.
- For a compaction the wheelhouse didn't drive, After Compact names the transcript path,
  instead of a doc item pointing at the `.jsonl` (the doc viewer renders markdown).
- On resume runs only after an hour or more away (parked, or the last response over an hour
  ago), so a quick relaunch for a version bump costs no extra turn.
- Plain Park keeps its own text; Park with Compact first carries Before Compact.
- Handoffs untouched for 30 days are removed at hub start.

## Open for Doug

1. Tabs and the 8 h compaction. The wheelhouse can't run `/compact` in a tab, so Q142 as
   written leaves tabs with no compaction at 8 h. Proposed: tabs keep idle compaction on
   (threshold 150k), the 50 min keepalive beats it during the window, the hub sends the
   Before Compact step at 8 h, and Claude Code compacts about 54 min later while warm. Cost:
   a quiet tab not waiting on anyone idle-compacts at 54 min with no step, as today. Or idle
   off per Q142: at 8 h a tab gets its step and its cache goes cold (the next turn pays a
   full cold write). Either way the test above runs first, since the proposal depends on it.
