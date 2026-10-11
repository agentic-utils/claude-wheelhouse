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
   all in the inbox (`1`), open questions first. `F` shows finished items.
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

Shape of B:

- `make tutorial` (and a hint on first run when the database is empty) launches the demo
  session in `~/.local/state/claude-wheelhouse/claude-wheelhouse-tutorial/`, a scratch directory it owns.
- A short checklist in the side pane advances as the user does each step: open a question,
  queue two answers, send with Ctrl+S, see the reply, read the decision and close it with Delete, follow the
  conversation, end the session. Steps are detected from store state, not timers.
- The brief tells the session to explain nothing in chat that the checklist covers, so
  the lesson happens in the wheelhouse.
- Ending the tutorial ends the session and deletes its items, leaving a clean inbox.

The demo session runs on whatever model the tester's Claude Code defaults to, with no
pin: a few cents of their tokens per run, and it behaves like the sessions they will
really use.

## Resolved

- **Approach** (Q16): B with the `?` overlay. "Note we'll need a way for me to re-run the
  tutorial even if I've taken it before": `make tutorial` always starts afresh.
- **First-run trigger** (Q17): offer it on first install, "as long as there's an easy way
  to dismiss it with a single keypress": Enter takes it, Esc dismisses it.
- **Checklist placement** (Q18): the top of the right-hand pane.
- **How the demo runs** (Q26): in the wheelhouse, like every new session, testers included.
  Superseded by Q66 below.
- **The tutorial is tab mode's** (Q66, option B): "Tutorial has to be tab". The demo opens
  in a Windows Terminal tab, and the steps cover what tab mode offers: questions and
  answers, decisions, items, End.
- **Always a tab** (D66): the tutorial is a tab session even with SDK mode on. The
  permission and conversation steps come back as SDK-only top-ups (below), offered only
  when SDK mode is on, once the top-up machinery is built.
- **Layout improvements**: parked, to come back to separately.

## As built

- `make tutorial` runs `claude-wheelhouse tutorial`: it ends any earlier tutorial session
  (its items go with it; a host that hasn't exited after 5 seconds gets SIGTERM, a tab's
  Claude Code gets it at once, since it never exits by itself), recreates
  `claude-wheelhouse-tutorial/` next to the database (deleted on a restart only if it
  carries the tutorial's marker file), creates a session there, opens it in a Windows
  Terminal tab, then opens the app. It is a tab whether SDK mode is on or not.
- The steps: find the demo in its tab (it ticks once the session has posted, so past any
  trust prompt), open a question, answer both, send them, see the reply, read and close
  the decision, end it.
- The tutorial session is simply the one whose directory is `claude-wheelhouse-tutorial/`: no schema column.
  Its `.claude/settings.json` allows the wheelhouse's own tools (so a tester in default
  permission mode isn't asked about each post).
- The brief has the session post a task, two related questions (tone, and a sign-off that
  depends on the tone) and a decision, then wait. On the answers it replies, writes the
  message in chat, then marks the task done. It's told to touch nothing else and to skip its own session-end routine (no
  memory or handoff notes) when ended.
- First run: with no sessions and no `tutorial_offer` setting (a new `settings` table), a
  one-line offer sits at the top of the screen. Enter starts the tutorial, Esc dismisses
  it; either way the setting is written and it never comes back.
- The checklist sits at the top of the right-hand pane while the tutorial session exists.
  Each step is worked out from the store on the refresh: both questions answered, the
  answers sent, a reply after they went, the decision closed (not just seen: the app's
  automatic selection marks a decision seen once it has rested there a second). The one
  only the screen knows (a question highlighted or opened) is recorded as the person does
  it (a highlight once they've rested on it a second, the dwell; Enter at once), never by
  the app's own automatic selection, and kept in the store (`tutorial_seen`), so a restart
  of the app keeps it. The next step shows how to do it; the rest are one line each.
- Starting refuses before creating anything if `claude` isn't on PATH. The tab starts
  detached, so a later failure comes afterwards: once it isn't running, "its tab didn't
  start Claude Code" if it never registered (wt.exe failed, or Claude Code did), else "its
  tab has closed". A tutorial an earlier version ran in the wheelhouse shows its host's
  stop reason. The checklist shows it, with the command to start afresh (`tutorial.command()`).
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

- `STEPS` in `tutorial.py` is one fixed list of eight `(key, what, how)` tuples, all
  taught by one scripted demo session (`BRIEF`).
- Nothing records a step as done. `steps()` works each one out from the store on every
  refresh, so the checklist exists only while the tutorial session does: once it ends, its
  items go with it and there is nothing left to work from. `end` is never done for that
  reason.
- Two steps only the screen sees (`open`, `follow`) are kept in the `tutorial_seen`
  setting, a JSON blob tied to one tutorial session id.
- `tutorial_offer` (unset, `taken`, `dismissed`) gates the one-line modal offer
  (`TutorialOffer`), shown only when it is unset and there are no sessions. Esc dismisses
  it for good.

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
- `make tutorial` still starts afresh: it runs every step, done or not, and records any
  it ticks. Nothing is cleared.

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
  The current eight checks move over as they are.
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
| `sdk` | SDK mode is on (`--enable-sdk`, or `enable-sdk=true` in a `.env`): the step is offered only then. The permission and conversation steps, which the tab tutorial leaves out, come back this way |
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

`end` becomes checkable: ending the demo session from the wheelhouse records it before the
session's rows go.

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
`migrated_decisions_close`. It writes rows for the eight steps that exist today, named in
the migration, not picked by `new_in`:

| Today | Becomes |
|---|---|
| `tutorial_offer = taken`, no tutorial session left | the eight steps done |
| `tutorial_offer = taken`, tutorial session still running | nothing: its checks record steps as the person goes |
| `tutorial_offer = dismissed` | the eight steps dismissed |
| unset, sessions exist (installed before the tutorial) | the eight steps dismissed |
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
  tab (D66), and the permission and conversation steps wait for the `tutorial_steps` table
  and the badge.

## Open questions

1. **Later on first run.** Read here as: the modal never shows again, and the badge carries
   the offer from then on. The other reading is the modal returning at each start until
   Start or Never. Which?
2. **Installs from before the tutorial** (sessions, no `tutorial_offer`, which is your own)
   get the eight core steps dismissed. Or should they be offered once, through the badge?
