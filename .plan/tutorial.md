# New-user tutorial (design)

A first run that gets a beta tester from `make run` to having answered a session's
question, with every part of the loop seen working once.

## Who it's for

Beta testers who already use Claude Code daily, on Windows with WSL and Windows Terminal,
and who run more than one session at a time. They know Claude, tabs, `/exit` and CLAUDE.md.
What's new to them is the wheelhouse's model: sessions report items, they answer from one
inbox, and answers go back to the session as its next turn.

## What they must learn

In the order they will meet it:

1. **Launch.** New session (`N`): directory, optional name and ticket, opening brief. It
   opens in a Windows Terminal tab (in SDK mode alone, `.plan/sdk-sessions.md`, it can run
   in the wheelhouse instead).
2. **Items.** Tasks (`T`), questions (`Q`), decisions (`D`) and subagent statuses (`A`),
   all in the inbox, open questions first. `F` shows finished items.
3. **Answering.** Enter opens a thread; Ctrl+Enter submits. The session's reply declares
   the question's state: `processing` while it's the session's move (D28), `open` when it needs them
   again, `answered` once it can proceed. Delete or Backspace in the item list closes a question (#64).
4. **Send mode.** Queued (the default) holds answers until Ctrl+S sends the session's
   queue as one message, or Send all sends every session's. Immediate sends on submit.
   Ctrl+T switches; Ctrl+R takes a queued answer back. This is the least familiar
   part, so it gets the most care.
5. **Follow a session.** Selecting a session pins 💬 Conversation and the pane follows its
   transcript; the box underneath sends it a general message. Permission prompts arrive as
   `P` items (Allow, Always, Deny) and Claude's own question dialog as a question item;
   Shell opens the real Claude Code in a tab for anything else.
6. **Lifecycle.** The session list and its description: synopsis, Restore after a reboot, relaunch of a dead
   (red) session, "needs relaunch" after an upgrade, Park, End, Adopt (`A` in the footer).

## Options

**A. Seeded sandbox.** `make tutorial` runs the TUI against a throwaway database
(`WHEELHOUSE_DB` already allows this) seeded with two sessions and a spread of items.

- Cheap, no tokens, deterministic, works offline.
- Fake sessions have no process, so liveness shows them dead (red) and offers relaunch;
  answers queue but nothing ever replies, so `processing` never resolves. It teaches the screens,
  not the loop, and the loop is the product.

**B. Live demo session.** `make tutorial` launches a real session in a scratch directory
with a scripted opening brief: post a task, ask two related questions, record a decision,
then wait. The user answers from the inbox and watches the session reply, update and
finish.

- Exercises the whole loop for real, including the session's host, Claude Code and the MCP
  server, so it doubles as a setup check: if it works, their install works.
- Costs a few cents of tokens and a minute or so; Claude's wording varies run to run, so
  the steps must not depend on exact text.
- Queued mode lands naturally: two related questions are the case batching exists for.

**C. Docs only.** A README quickstart plus a `?` overlay listing every key and the
send-mode rules.

- Smallest to build and always available, but nobody reads it until they're stuck, and
  it can't show `processing` turning into `answered`.

## Recommendation

B, with the `?` overlay from C. B is the only option that shows the loop working and
proves the install at the same time; A's dead sessions would teach the wrong thing on
day one. The overlay is cheap and is where people will look once the tutorial is done.

Shape of B, as first proposed (superseded: "As built" below is what runs):

- `make tutorial` (and a hint on first run when the database is empty) launches the demo
  session in `~/.local/state/claude-wheelhouse/claude-wheelhouse-tutorial/`, a scratch directory it owns.
- A short checklist in the side pane advances as the user does each step: open a question,
  queue two answers, send with Ctrl+S, see the reply, read the decision and close it with Delete, follow the
  conversation, end the session. Steps are detected from store state, not timers.
- The brief tells the session to explain nothing in chat that the checklist covers, so
  the lesson happens in the wheelhouse.
- Ending the tutorial ends the session and deletes its items, leaving a clean inbox.

The demo session runs on New session's defaults, Sonnet 5.5 at high effort
(`models.chosen`), pinned however the tester's own Claude Code is set: a few cents of
their tokens per run.

## Resolved

- **Approach** (Q16): B with the `?` overlay. "Note we'll need a way for me to re-run the
  tutorial even if I've taken it before": `make tutorial` always starts afresh (since Q72,
  by having the next start offer it, below).
- **First-run trigger** (Q17): offer it on first install, "as long as there's an easy way
  to dismiss it with a single keypress": Enter takes it, Esc dismisses it.
- **Checklist placement** (Q18): the top of the right-hand pane.
- **How the demo runs** (Q26): in the wheelhouse, like every new session, testers included.
  Superseded by Q66 below.
- **The tutorial is tab mode's** (Q66, option B): "Tutorial has to be tab". The demo opens
  in a Windows Terminal tab, and the steps cover what tab mode offers: questions and
  answers, decisions, items, End.
- **Always a tab** (D66): the tutorial is a tab session even with SDK mode on. The
  permission step comes back as an SDK-only top-up (below), offered only when SDK mode is
  on, once the top-up machinery is built.
- **Two core workflows added** (Q73, option A): watching the session work in its
  conversation, which a tab has too (it is read from the transcript), after the reply; and
  starting a session of your own with `N`, last, after End. Nine steps.
- **Layout improvements**: parked, to come back to separately.
- **Re-running it** (Q72, option A): launching the demo straight from `make tutorial` while
  the app was open just joined it to the running wheelhouse, which confused. `make tutorial`
  asks y/N first, then clears the saved answer to the offer and removes the old tutorial
  session, leaving a one-time marker so the next start shows the offer even though other
  sessions exist. It refuses while the app or daemon is up, because the offer is only
  checked at startup. No tab is launched from the make target, and the person's other
  sessions are never touched.

## As built

- Taking the offer (`tutorial.start`): it ends any earlier tutorial session
  (its items go with it; a host that hasn't exited after 5 seconds gets SIGTERM, a tab's
  Claude Code gets it at once, since it never exits by itself), recreates
  `claude-wheelhouse-tutorial/` next to the database (deleted on a restart only if it
  carries the tutorial's marker file), creates a session there and opens it in a Windows
  Terminal tab. It is a tab whether SDK mode is on or not.
- `make tutorial` runs `claude-wheelhouse tutorial` (`tutorial.reset`): refused while the
  daemon's lock is held (`connect.holder`), with how to stop it; then a y/N (`--yes` skips
  it); then it ends any tutorial session as above and sets `tutorial_offer` to `reset`. It
  never opens the app or a tab. The lock is checked again after the y/N, which can wait
  while the app is started. An app run with `--local` takes no lock, so it can't be
  detected: run then, the command SIGTERMs that app's tutorial tab and removes its row, and
  the offer shows only at the app's next start. Documented, not guarded.
- The nine steps: find the demo in its tab (it ticks once the session has posted, so past
  any trust prompt), open a question, answer both, send them, see the reply, watch it work,
  read and close the decision, end it, start your own.
- Watch it work ticks when a turn of Claude's (a reply or a tool call, not the person's
  own words) arrives in the tutorial's conversation while it is shown, the person's own
  pick (the session followed, or its 💬 Conversation row), never the app's automatic
  selection. The session has finished by then, so the hint has them ask for a change in
  the box under the conversation, and the brief has it ask whether they'd like one.
- Start your own ticks once a session exists that wasn't there as the tutorial started
  (`prepare` records their ids) and isn't in the tutorial's directory: `N`, or an Adopt.
  Its hint also says Shift+S (Restore all) brings sessions back after a reboot.
- The checklist outlives the session for that last step: once the session has ended it
  shows End, done, and Start your own until that is done too, then goes. Only a record
  `prepare` wrote (with the ids) is followed past the end, and `make tutorial` clears it.
- The tutorial session is simply the one whose directory is `claude-wheelhouse-tutorial/`: no schema column.
  So a session of the person's own started in that directory is taken for the tutorial's,
  and ended with it (`stop_earlier`, on taking the offer or on `make tutorial`). The README
  says not to start one there.
  Its `.claude/settings.json` allows the wheelhouse's own tools (so a tester in default
  permission mode isn't asked about each post).
- The brief has the session post a task, two related questions (tone, and a sign-off that
  depends on the tone) and a decision, then wait. On the answers it replies, writes the
  message in chat, then marks the task done. It's told to touch nothing else and to skip its own session-end routine (no
  memory or handoff notes) when ended.
- First run: with no sessions and no `tutorial_offer` setting (a new `settings` table), a
  one-line offer sits at the top of the screen. Enter starts the tutorial, Esc dismisses
  it; either way the setting is written and it never comes back. `tutorial_offer = reset`
  (from `make tutorial`) shows it once more, sessions or not, and the answer overwrites it.
- The checklist sits at the top of the right-hand pane while the tutorial session exists,
  and after it ends while Start your own is still to do (above). Each step is worked out from the store on the refresh: both questions answered, the
  answers sent, a reply after they went, the decision closed (not just seen: the app's
  automatic selection marks a decision seen once it has rested there a second). The ones
  only the screen knows (a question highlighted or opened, a turn watched arriving) are
  recorded as the person does them (a highlight once they've rested on it a second, the
  dwell; Enter at once; a turn as it shows), never by
  the app's own automatic selection, and kept in the store (`tutorial_seen`), so a restart
  of the app keeps it. The next step shows how to do it; the rest are one line each.
- Starting refuses before creating anything if `claude` isn't on PATH. The tab starts
  detached, so a later failure comes afterwards: once it isn't running, "its tab didn't
  start Claude Code" if it never registered (wt.exe failed, or Claude Code did), else "its
  tab has closed". A tutorial an earlier version ran in the wheelhouse shows its host's
  stop reason. The checklist shows it, with the command to start afresh (`tutorial.COMMAND`).
- Every hint shown in a running app (the offer, `?`, that checklist line, the notice on
  dismissing the offer) says `tutorial.AGAIN`: with the wheelhouse stopped, the command
  offers it again. The command is refused while the daemon runs, so a hint that left that
  out sent the person to a refusal.
- `?` opens every key, generated from the bindings themselves (a test fails if a binding
  has no description), the mouse, every button and the send-mode rules.

# Top-up tutorials (design)

Issue #16. Status: for review. Doug: "I want the tutorial to be invoked on first ever run,
then I want the ability to run "top-up" tutorials each time we add a new feature... Also
would be good if the first step in the tutorial allows the user to skip it, and, if that
table in the DB records the features that the user has actually seen, so that a user never
misses the opportunity to see a tutorial step even if they don't have time to look when
they first start up after a version upgrade."

Chosen (option A): "Give each step a stable id and a 'new in' version, and keep a
tutorial_steps table (step_id, done_at, dismissed_at). Pending steps are then just: all
steps minus the done or dismissed ones. A new user and an upgrader follow the same rule...
The version is only a label. Skip on the first screen gives three choices: Start (run it
now); Later (nothing is marked, offered again at the next start); Never (dismisses all
current steps, so only future steps get offered). Keep the offer non-modal after first run:
a '2 new tutorial steps' badge."

## Where it stands

- `STEPS` in `tutorial.py` is one fixed list of nine `(key, what, how)` tuples, all
  taught by one scripted demo session (`BRIEF`).
- Nothing records a step as done. `steps()` works each one out from the store on every
  refresh, so once the tutorial session ends its items go with it and there is nothing
  left to work from: the checklist then keeps only End (done, as the session has gone)
  and Start your own, which need nothing of the session's.
- Two steps only the screen sees (`open`, `follow`) are kept in the `tutorial_seen`
  setting, a JSON blob tied to one tutorial session id.
- `tutorial_offer` (unset, `taken`, `dismissed`, `reset`) gates the one-line modal offer
  (`TutorialOffer`), shown when it is unset and there are no sessions, or when it is
  `reset` (`make tutorial`). Esc dismisses it until the next `make tutorial`.

## The model

A step is the unit that's recorded; the pending rule needs nothing else.

```
tutorial_steps (step_id TEXT PRIMARY KEY, done_at TEXT, dismissed_at TEXT)
pending = [s for s in STEPS if no row for s.id has done_at or dismissed_at]
```

- `done_at` is written the moment a step's check first passes, and never cleared. That
  makes done sticky, so the checklist no longer depends on the demo session's items
  surviving, and `tutorial_seen` retires: a screen-only step writes `done_at` directly.
- `dismissed_at` is written only by Never.
- A step id never changes once released. Renaming one would offer it again; a retired
  step's row is ignored.
- `new_in` is shown in the checklist ("new in 0.2.0") and in release notes. The pending
  rule never reads it.
- Taking the offer `make tutorial` re-arms still starts afresh: it runs every step, done
  or not, and records any it ticks. Nothing is cleared.

## A step

`STEPS` becomes a tuple of `Step` records. Adding a feature's step is one entry plus its
check:

```python
Step(id="effort", new_in="0.2.0",
     what="Change a session's effort",
     how="highlight a session, then ←/→ on the effort slider in the stats pane",
     needs=("session",),
     check=lambda t: t.effort_changed())
```

- `check` takes a small view (`t`) over the store, the demo session's id if one is
  running, and the screen events `saw()` records. It returns whether the step is done now.
  The current nine checks move over as they are.
- `brief`, only for steps that need the demo session: the lines added to the demo's brief
  to set the step up (post a decision, run a command that asks permission). The current
  `BRIEF` splits into a shared preamble plus one fragment per step.

### Prerequisites

`needs` names what must hold before the step can be done, from a short fixed list plus
other step ids:

| Need | Means |
|---|---|
| `demo` | the tutorial's demo session is running; Start launches one |
| `session` | any running session of the person's, hosted in the wheelhouse |
| `sdk` | SDK mode is on (`--enable-sdk`, or `enable-sdk=true` in a `.env`): the step is offered only then. The permission step, which the tab tutorial leaves out, comes back this way |
| a step id | that step is done first (`send` needs `queue`) |

A step whose needs aren't met stays pending and shows in the checklist greyed, with what
it waits for ("needs a running session"). It is never marked anything because of it.
A test checks every need is known and the step graph has no cycles.

## How a top-up gets its demo

Start collects the pending steps and looks at their needs.

- **Screen-only** (no `demo` need). No session is launched and no tokens are spent. The
  checklist opens in the right pane and ticks as the person does each step. The effort
  slider is the example: it needs a running session, and the step is about the slider, so
  the person's own session will do. The `how` text says to move it and put it back.
- **Needs the demo.** One demo session is launched, as today (scratch directory, its
  `.claude/settings.json`, the default model and effort), with a brief built from the
  preamble and the fragments of the pending demo steps, in `STEPS` order. Several top-ups
  pending at once share one session, so one launch covers them. The offer line says when a
  demo will run: "starts a short demo session (a few cents of tokens)".
- Mixed: both happen; the screen-only steps can be done while the demo runs.

The checklist shows while a run is under way and any of its steps remain. It goes once
they are all done, or when the demo session is ended. Steps still not done stay pending:
the badge offers them again.

`end` is recorded like any other step, once the demo session has gone.

## The offer

**First run** (`tutorial_offer` unset and no sessions, as today): the one-line modal, with
three choices.

| Key | Choice | Writes |
|---|---|---|
| Enter | Start | `tutorial_offer = taken`; launches the run |
| L or Esc | Later | `tutorial_offer = later`; no step rows |
| N | Never | `tutorial_offer = never`; `dismissed_at` on every current step |

Esc becomes Later rather than a permanent dismissal: the safe default marks nothing. That
is a change from today's Esc.

**After first run**, never modal. While any step is pending, a badge sits at the right-hand
end of the title row: "2 new tutorial steps · L". The footer carries the same binding
(`L Tutorial (2)`), shown only while there are pending steps, and `?` lists it. `L` opens
the same three-choice line, naming the steps, so Never is always one key away.

The title row is the only line every screen of the inbox keeps, and its right-hand end is
free. The update notice (`.plan/release.md`) shares it: one slot for notices, the badge
first.

## Existing installs

A one-off migration on first open by the new code, recorded by a settings marker like
`migrated_decisions_close`. It writes rows for the nine steps that exist today, named in
the migration, not picked by `new_in`:

| Today | Becomes |
|---|---|
| `tutorial_offer = taken`, no tutorial session left | the nine steps done |
| `tutorial_offer = taken`, tutorial session still running | nothing: its checks record steps as the person goes |
| `tutorial_offer = dismissed` | the nine steps dismissed |
| unset, sessions exist (installed before the tutorial) | the nine steps dismissed |
| unset, no sessions | nothing: a fresh install, gets the first-run offer |

So an install that has done the tutorial sees nothing new until a release adds a step,
then sees the badge for that step alone. `tutorial_seen` is deleted by the migration.

## Tests

Table-driven, one case a line, as `test_tutorial.py` does now:

- **The pending rule**: rows in, pending ids out (none, done, dismissed, a retired id, a
  new step after the rest are done).
- **Migration**: the five rows of the table above, plus running it twice changes nothing.
- **The offer**: Start, Later, Never and Esc against the rows and settings they write;
  Never leaves a step added later pending.
- **Each step's check**: the existing `test_checklist_steps_come_from_the_store` cases,
  now asserting `done_at` is written and survives the demo session ending.
- **The step table**: ids unique; `new_in` parses as a version and is no later than the
  package's; every `needs` entry is known; no cycles; every demo step has a brief fragment.
- **The demo brief**: built from the pending demo steps only; no demo launched when none
  are pending.
- **TUI**: the badge and the `L` binding show only while steps are pending and give the
  count; the modal shows on first run only.

## Resolved (top-ups)

- **Screen-only steps on a real session** (Q63): A, "screen-only steps use the person's own
  session". The effort step asks the person to move their own session's slider and put it
  back; no demo is launched for it.
- **SDK-only steps** (Q66): a step that needs SDK mode (`needs=("sdk",)`) is offered only
  when it is on, never pending for a new user. Not built yet: the core tutorial is always a
  tab (D66), and the permission step waits for the `tutorial_steps` table and the badge.
  Following the conversation needed no SDK: it is a core step (Q73).

## Open questions

1. **Later on first run.** Read here as: the modal never shows again, and the badge carries
   the offer from then on. The other reading is the modal returning at each start until
   Start or Never. Which?
2. **Installs from before the tutorial** (sessions, no `tutorial_offer`, which is your own)
   get the nine core steps dismissed. Or should they be offered once, through the badge?
