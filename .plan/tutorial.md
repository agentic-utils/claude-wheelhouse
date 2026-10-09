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
   runs in the wheelhouse itself, with no tab; "Open in a terminal tab instead" is the
   other choice.
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
- **Layout improvements**: parked, to come back to separately.

## As built

- `make tutorial` runs `claude-wheelhouse tutorial`: it ends any earlier tutorial session
  (its items go with it; a host that hasn't exited after 5 seconds gets SIGTERM), recreates
  `claude-wheelhouse-tutorial/` next to the database (deleted on a restart only if it carries the tutorial's marker file), creates a session there run in the wheelhouse, starts
  its host, then opens the app.
- The tutorial session is simply the one whose directory is `claude-wheelhouse-tutorial/`: no schema column.
  Its `.claude/settings.json` allows the wheelhouse's own tools (so a tester in default
  permission mode isn't asked about each post) and always asks for `touch tutorial-ok`,
  so a permission item appears even in auto mode.
- The brief has the session post a task, two related questions (tone, and a sign-off that
  depends on the tone) and a decision, then wait. On the answers it replies, writes the
  message in chat and runs the `touch`, which is the permission prompt; then it marks the
  task done. It's told to touch nothing else and to skip its own session-end routine (no
  memory or handoff notes) when ended.
- First run: with no sessions and no `tutorial_offer` setting (a new `settings` table), a
  one-line offer sits at the top of the screen. Enter starts the tutorial, Esc dismisses
  it; either way the setting is written and it never comes back.
- The checklist sits at the top of the right-hand pane while the tutorial session exists.
  Each step is worked out from the store on the refresh: both questions answered, the
  answers sent, a reply after they went, the decision closed (not just seen: the app's
  automatic selection marks a decision seen), the permission allowed. The two
  only the screen knows (a question highlighted or opened, the conversation followed) are
  recorded as the person does them, never by the app's own automatic selection, and kept
  in the store (`tutorial_seen`), so a restart of the app keeps them. The next step shows
  how to do it; the rest are one line each.
- Starting refuses before creating anything if `claude` isn't on PATH. The host starts
  detached, so a later failure (Claude Code won't start) arrives as the host's stop reason;
  the checklist shows it, with `make tutorial` to start afresh.
- `?` opens every key, generated from the bindings themselves (a test fails if a binding
  has no description), the mouse, every button and the send-mode rules.
