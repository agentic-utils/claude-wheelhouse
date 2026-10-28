<!-- fact sheet stamp: version 32, 2026-10-28T12:33:28Z, source 5239cc8d923b62d4 (make facts restamps it: AGENTS.md) -->
# Wheelhouse fact sheet

What the helper chat knows before any question. The guide below is written by hand; the
rest is generated from the code. Where this sheet, the README or the code disagree, the code
wins.

## What the wheelhouse is

A terminal app (Textual) that sits beside the person's Claude Code sessions. Each session
posts tasks, questions and decisions to it through the `wheelhouse` MCP server; the person
answers in the inbox, and the answers go back to the session. Sessions run as Windows
Terminal tabs (WSL), each a real Claude Code. The app talks to a daemon, one per user on
127.0.0.1:7428, which keeps the SQLite store; quitting the app leaves the sessions and the
daemon running.

## The screen

- Left column: the session list (each session's state, context bar and cache warmth), and
  under it the buttons that act on the highlighted session.
- Middle: the inbox (the item list: tasks, questions, decisions, subagents, PRs, docs), the
  conversation pane and the answer box with its send bar (Mode, Send).
- Right and the other slots: the modules' panes (stats, PRs, Decisions) when installed. A
  pane dragged by its title swaps places with another; Reset layout puts them back.
- The footer lists the main keys; F1 lists every key and button.

## Words the app uses

- Item: anything a session posts. Kinds: task, question, decision, subagent (A), PR, doc.
- Queued and Immediate: a session's send mode. Queued holds answers until Send (Ctrl+S).
- Park: a session writes its handoff and stops, kept dimmed until Unpark.
- Forget: the session leaves the wheelhouse (its data deleted); Adopt brings one back.
- Relaunch: start a session again where it left off.
- Fork: a new session carrying on from another's conversation.
- Pin: an item kept through Delete and Ctrl+A.
- Session steps: what the wheelhouse tells a session at compaction, ending, resuming or a
  merged PR (command palette, Session steps).
- Tour: the guided tour, resumed with Alt+T while the title bar shows its badge.

## Where to look in the source

Paths are relative to the package directory, the working directory. The modules are their
own packages beside it.

- Keys, buttons, dialogs and every pane's behaviour: `tui.py` (`BINDINGS`, `DESCRIBE`,
  `BUTTONS`, the `Dialog` subclasses, `WheelhouseApp`).
- Session states and what each verb does (Relaunch, Park, Forget, Fork): `hub.py`
  (`VERBS`), `fork.py`, `adopt.py`.
- Launching a session and its command line: `launch.py`.
- Settings and how each is given (flag, `WHEELHOUSE_<KEY>`, `.env`): `features.py`.
- What sessions are told: `protocol.md`, `protocol_tab.md`, `protocol_decisions.md`,
  `steps.py`, and the MCP tools' docstrings in `mcp_server.py`.
- The store and what it keeps: `store.py`. The daemon: `daemon.py`, `connect.py`.
- Plan usage gauges: `plan_usage.py`; context and cache: `transcript_usage.py`.
- The tour and release notes: `tour.py`, `whatsnew.py`, `tutorial.py`.
- The stats, PRs and Decisions panes: the `claude_wheelhouse_stats`,
  `claude_wheelhouse_prs` and `claude_wheelhouse_design` packages, each a `pane.py`.

## What the helper is not told

SDK mode (`features.py`, `host.py`) is experimental and off for everyone but the author:
answer for sessions run as tabs. The design notes (`.plan/`) are not available: they hold
rejected options, not features.

<!-- generated: everything below is written by make facts from the code. Edit the code, not this -->

## Version

This fact sheet describes claude-wheelhouse 0.2.0.dev0.

## Keys, from the bindings (F1 lists the same)

### Everywhere

- **Ctrl+Enter or Ctrl+J**: Submit what's typed: queued, or sent at once, by the session's mode

- **Alt+S**: Send what's typed at once, whatever the mode, leaving the mode and the rest of the queue as they are (one not running: Unpark or Relaunch it and send it with the queue, asked first)

- **Ctrl+S**: Send the current session's queue (one not running: Unpark or Relaunch it and send, asked first)

- **Ctrl+T**: Switch the session between Queued and Immediate

- **Ctrl+R**: Take this item's latest queued answer back into the box; with none queued, in the item list, reopen the highlighted finished item (or every marked one)

- **Ctrl+Z**: Undo the last delete, pin or reopen in the inbox: the rows come back as they stood, an open question open again (once; a running child Delete left at the top level stays there). In a text box, its own undo

- **Alt+N**: New session

- **Alt+A**: Adopt: pick a Claude Code session that isn't in the wheelhouse yet, from those on disk

- **Alt+Shift+L**: Relaunch all: every Available session (not running, not parked)

- **Alt+Shift+M**: MCP servers: the session in context's, as its host last saw them, each with a button to sign in or reconnect it (a claude.ai connector in every session the wheelhouse hosts); a tab's: /mcp there

- **Esc**: Clear a text selection first, then the marks, else show every session's items again

- **Alt+V**: Show or hide finished items

- **F1**: This list

- **Alt+Q**: Ask the wheelhouse: a chat that answers how-to questions about it, from its help and its source (Haiku, on your Claude Code login). Alt+Q again, or Esc, closes it; the chat is kept until you quit

- **Ctrl+L**: With a doc shown: its thread under it (split), or Doc and Thread as tabs (tabbed); kept

- **Ctrl+PgUp**: With a doc shown tabbed: the tab before

- **Ctrl+PgDn**: With a doc shown tabbed: the tab after

- **Alt+T**: The tour, while the title bar's badge shows: resume a tour quit in the middle where it was, or take what's new (Alt+T, or a click on the badge)

- **Ctrl+Q**: Quit the wheelhouse (sessions carry on without it)

### The buttons

- **Alt+Shift+A**: Send all: every running session's queue (a dead or parked one's waits until it's relaunched or unparked)

### Moving between panes

- **Tab**: The next pane (Tab order below)

- **Shift+Tab**: The previous pane

### Either list

- **Up**: Up a row

- **Down**: Down a row

- **PgUp**: Up a page

- **PgDn**: Down a page

- **Ctrl+Home**: The first row

- **Ctrl+End**: The last row

### The session list

- **Alt+R**: Rename the highlighted session

- **Alt+L**: Relaunch it, or Unpark a parked one

- **Alt+P**: Park it, or Unpark a parked one

- **Alt+K**: Fork it: a new session carrying on from its conversation

- **Delete or Backspace**: Forget it (Stop & Forget while it's running)

### The item list

- **Delete or Backspace**: Close the highlighted question or decision, or dismiss a done task or subagent, or a doc (or every marked one but the pinned); it never reopens one: Ctrl+R does, and Ctrl+Z undoes it. On an open question, a pinned item or one with children it asks first (once for marked rows), saying how many are still open or unread, then takes them all but the pinned

- **Ctrl+Space**: Mark or unmark the highlighted row

- **Ctrl+A**: Mark every finished (dimmed) row shown but the pinned, for Delete to clear them together

- **Alt+P**: Pin the highlighted item (📌), or clear its pin: Ctrl+A and Delete leave a pinned item

- **Shift+Up**: Extend the marks up

- **Shift+Down**: Extend the marks down

### An answer box

- **Ctrl+Enter or Ctrl+J**: Submit what's typed: queued, or sent at once, by the session's mode

- **Ctrl+A**: Select all of it, to copy

- **PgUp**: Scroll the conversation (or the thread) up a page

- **PgDn**: Scroll it down a page

- **Up**: While an emoji code is typed, choose the suggestion before (Tab or Enter takes it); else up a line

- **Down**: While an emoji code is typed, choose the next suggestion; else down a line

### The conversation pane

- **Ctrl+A**: Select all of it, to copy

### An item opened full screen

- **Esc**: Back to the inbox; with the doc's editor open, close it first

### A doc opened full screen

- **Esc**: Back to the inbox; with the doc's editor open, close it first

### A doc's editor

- **Ctrl+Enter or Ctrl+J**: Show the edits rendered in the doc above

- **Ctrl+S**: Says writing the doc comes next; never sends the session's queue

- **Esc**: Close the editor, asking first with unsaved edits: Close and keep them, Discard or Keep editing

### The helper (Alt+Q)

- **Alt+Q**: Close the helper, keeping the chat

- **Alt+N**: New chat: the helper forgets this one

### The PRs pane

- **Alt+O**: Mine or all

- **Alt+R**: Fetch now

### Tab order

Tab goes round the session list, the items, the answer box, a tour box's Next while one shows, then every button shown, in reading order, then the stats pane's zoom's - and + (Enter or Space presses the one with the focus), and Shift+Tab back. A click in the conversation puts focus in the answer box, and Ctrl+Enter leaves it there; PgUp and PgDn in the box scroll the conversation. A click on a button leaves focus where it was, and each has a key. An item opened full screen goes the same way: its answer box, then its buttons; a click in its thread puts focus in the box, where PgUp and PgDn scroll the thread. A doc shown in the inbox takes the answer box's place, and a click on it focuses it: Up, Down, PgUp, PgDn, Home and End scroll it. A focused button that is disabled or hidden (Send, once sent) gives focus to the answer box. Send from a list leaves focus in the list.

### Mouse

Click selects a row; in the session list, a click on the selected session or the list's title past its columns unselects it, for all sessions' items, as Esc does. A click on a column header sorts by it. Ctrl+click marks rows and Shift+click a range (Windows Terminal may keep Shift+click for itself: Ctrl+Space and Shift+Up/Down do the same), and Ctrl+A every finished row; then Delete or Backspace closes them together, the highlight going to the row above the topmost one taken, and Ctrl+R reopens the finished ones (Ctrl+Z undoes either). An item with children (its subagents and docs, under it) has a - at the right-hand end of its row: a click there hides them, and the + it becomes shows them again. A click on a doc opens its editor below it there, as a click on it moves it while it's open. A click on a row's first column pins it (📌) or clears its pin, as Alt+P does. A click on an emoji suggestion under the answer box takes it in place of the code typed. Double-click selects a word. Right-click on a session in the session list selects it and opens a menu of its buttons; elsewhere right-click copies the selection, or with none pastes into the answer box, as a terminal does. Drag the lines between the panes to resize them (they light up under the pointer); the sizes are kept for next time, and a double-click on a line puts its default back. Drag a pane by its title onto another to swap the two (Esc cancels); Reset layout, in the command palette (Ctrl+P), puts every pane and line back; Session steps there has what the wheelhouse tells a session at each event.

### Buttons

The buttons under the session list act on the session highlighted there, and only those that apply show: Rename on a running one, Relaunch (Unpark on a parked one), Forget (Stop & Forget on a running one) and Fork on any, and Park on one that isn't parked; then Mode, and Send on one that isn't dead. Hover over one for what it does, and its key is in brackets below. New, Adopt and Relaunch all are keys in the footer. An item opened full screen has Mode, Send and the rest in its bar, for its own session. Ctrl+S and Ctrl+T act on the same session: highlighting an item highlights its session there.

- Mode (Ctrl+T): the session's send mode, Queued or Immediate

- Send (Ctrl+S): send the session's queued answers as one message; its caption counts them

- Send all (Alt+Shift+A), above the footer: send every running session's queue; a dead or parked one's waits until it's relaunched or unparked

- Relaunch / Unpark (Alt+L in the session list): start the session again where it left off, stopping it first if it's running (asked only if it's mid-turn); on a parked one, Unpark (Alt+P too)

- Rename (Alt+R in the session list): the session's name in the wheelhouse

- Fork (Alt+K in the session list): a new session, "<name> (fork)", carrying on from this one's conversation so far, which carries on too. Ticked items move to it; in a linked git worktree, it works in a new one on a new branch from the last commit (uncommitted changes stay behind), or this one

- Park (Alt+P in the session list): a running session writes its handoff notes into its items, then stops; it stays, dimmed, at the foot of the list, its items off the inbox, until Unpark. Off while it's asked; on again after 30 s, pressed again to cancel or force it

- Stop & Forget, or Forget (Delete or Backspace in the session list): a running session asks first, Let it know (Alt+L: it does its own end steps, then stops) or Just stop it (Alt+J: stopped now, told nothing), the first by default while its prompt cache is warm, the second once cold, as its end steps would re-read its whole context; its wheelhouse data is deleted either way, queued answers with it. One not running is deleted at once, once you confirm. Adopt brings a forgotten one back. Off while it's asked; on again after 30 s, pressed again to cancel or force it

### Sending

**Queued** (the default): Ctrl+Enter holds each answer until Ctrl+S (or Send) sends the session's queue as one message, so related answers arrive together; ✉ counts what's queued. A session not running: Ctrl+S or Alt+S asks to Unpark (a parked one) or Relaunch it and send, its queue and the box's answer its first prompt; Send all leaves its queue. Alt+S sends just the answer in the box at once, leaving the queue and the mode as they are. **Immediate**: Ctrl+Enter (or Alt+S) sends at once. Ctrl+T switches the session's mode; Ctrl+R takes a queued answer back to edit.

### Dialogs

Esc, or a click outside the box, cancels a dialog (not the tutorial offer, which is all but outside). Enter presses the focused button; with focus on a one-line box or a checkbox, or nothing, it takes the dialog's one action (Launch, Rename, Fork, Park and the like), but in a list, a set of choices or a multi-line box it does what it does there. A dialog of several choices answers Enter only on the focused button. Tab and Shift+Tab go round its boxes and buttons.

### Tour

Esc ends the tour at any time, once you confirm, from any of its boxes or the New session it opens. While the title bar shows a badge, Alt+T, or a click on it, resumes a tour the wheelhouse quit in the middle of, or offers what's new since you last looked: a top-up showing only the new tips, pane by pane, with no demo session, then the release notes. With the wheelhouse stopped, claude-wheelhouse tutorial (make tutorial in a source checkout) offers the tutorial again at the next start.

Esc, Enter, F1 or a click outside closes this list.

## The command palette (Ctrl+P)

- **Ask the wheelhouse**: A chat that answers how-to questions about the wheelhouse, from its help and its source (Alt+Q)

- **Reset layout**: Put every pane back in its default place, and the dividers at their default sizes

- **Session steps**: What the wheelhouse tells a session when it compacts, ends, resumes or its PR merges, and the handoff it keeps

## Command line and settings (make args prints the same)

```

The app's command line args (make args). Give them to make run in ARGS, for example
  make run ARGS="--local --animation-fps 0"

--usage-mode {oneshot,persistent}  (env WHEELHOUSE_USAGE_MODE, .env USAGE_MODE=)
    how the usage bar reads /usage: a short claude -p every 5 minutes (oneshot, the
    default), or one Claude Code kept running (persistent); or WHEELHOUSE_USAGE_MODE, or
    USAGE_MODE= in a .env
--port PORT  (env WHEELHOUSE_PORT, .env PORT=)
    the local daemon's port (default 7428, or WHEELHOUSE_PORT, or PORT= in a .env):
    another for each user on the machine who runs a wheelhouse
--keepalive-hours N  (env WHEELHOUSE_KEEPALIVE_HOURS, .env KEEPALIVE_HOURS=)
    keep a quiet session's prompt cache warm for up to N hours (default 8, or
    WHEELHOUSE_KEEPALIVE_HOURS, or KEEPALIVE_HOURS= in a .env; 0 turns it off)
--step EVENT.SWITCH=on|off
    a session step's switch (steps.py), repeatable, such as pr_merged.cleanup=off; or
    WHEELHOUSE_STEP_<EVENT>_<SWITCH>, or STEP_<EVENT>_<SWITCH>= in a .env
--animation-fps N  (env WHEELHOUSE_ANIMATION_FPS, .env ANIMATION_FPS=)
    animate the title, the eyes and the stats bars N frames a second (default 10, or
    WHEELHOUSE_ANIMATION_FPS, or ANIMATION_FPS= in a .env; 0 turns animation off)
--local
    run the hub in this process, without the daemon (the wheelhouse as it was before the
    daemon)
--connect HOST:PORT
    use the daemon on another machine (never started or stopped from here); its token
    from --token-file, else WHEELHOUSE_TOKEN, else asked for
--token-file PATH
    with --connect: a file holding the daemon's token

```

## Features, by release (the release notes)

### Not yet released

- A guided tour: a box beside each pane walks you through a demo session.
- Every session list column sorts: click its heading to cycle up, down and back.
- Stats has a pane of its own, under the session list.
- Each pane carries its title in its top border.
- Every dialog answers the same keys: Esc or a click outside cancels, Enter presses its main button.
- Tab goes round the session list, the inbox, the answer box and the buttons.
- make run installs when it needs to, make install fetches uv, and make args lists the app's flags.
- claude-wheelhouse modules lists the modules installed; modules disable and enable switch one off and on.
- A session can tell the wheelhouse where its repo keeps its decisions, when that isn't docs/adr (set_doc_dirs).
- A Decisions pane under the inbox lists every repo's design decisions (docs/adr), shown in Detail.
- Drag a pane by its title onto another to swap the two; Reset layout, in the command palette, puts them back.
- A click on a doc opens an editor below it; Ctrl+Enter shows the edits rendered. Saving comes next.
- Alt+Q asks the wheelhouse how to do something: a chat that answers from its help and its source, through Haiku on your own Claude Code login.
- The helper chat moved from Alt+G, which opens Gemini on some desktops, to Alt+Q (Q for question).
- After an update, the tour shows only what's new, pane by pane, with no demo; Esc ends it at any time, and after quitting mid-tour, Alt+T or the title bar's badge resumes it.
- A pressed button (Compact, Interrupt, Relaunch, Park, Fork...) stays off, dark grey, until what it started has finished, or for 30 seconds with no word from the session.
- Pin any inbox item (📌, a click on its first column or Alt+P) and clearing the inbox leaves it; docs come pinned.
- A doc has a thread of its own: comment on it below the doc (split) or on a Thread tab (tabbed); Ctrl+L switches the two, Ctrl+PgUp and Ctrl+PgDn the tabs.
- An inbox item's ref is coloured by its kind (a task green, a question yellow, a decision purple, a PR light blue), and resting the pointer on a row, there or in the session list, shows it in full.
- A pull request a session makes shows in the inbox as PR and its number, under the task it was made for, with GitHub's status: merged and closed ones dim.
- The PRs pane lists the open PRs your sessions made, or with M every open PR you authored, each with a traffic light for checks, review and conflicts; with a session selected, its PRs alone.
- Right-click a session in the session list for a menu of its buttons, acting on that session.
- Alt+S sends the answer in the box at once, even in Queued mode; the rest of the queue waits.
- Ctrl+S or Alt+S on a session that isn't running asks to Unpark (a parked one) or Relaunch it and send: it wakes with your message and its queue as its first prompt.
- A PR in the PRs pane has Open, Merge and Request review buttons, and a right-click on its row gives them as a menu: Merge asks first and says what stood in its way, and Request review asks the session that made the PR to post the request on Slack.
- Ctrl+Z in the inbox undoes the last delete, pin or reopen, an open question coming back open; Delete on an open question asks first.
- Detail, showing a PR, has its Open, Merge, Request review and Close buttons, and its failing checks, description, history and comments.
- Mark PRs in the PRs pane as in the inbox (Ctrl+click, Shift+click, Ctrl+Space, Shift+Up/Down) and act on them all; Close closes a PR unmerged, asked first, as Merge is.
- A PR's buttons that can't apply are greyed, and say why on hover (Merge on a PR awaiting review, say), and Mark ready or Make draft switches a PR between draft and ready for review.
- The wheelhouse tells a session before it ends or resumes (and, in SDK mode, compacts), so its own instructions can act, and can keep a handoff doc for it. Session steps, in the command palette, has every switch; the first press asks once.
- The keepalive comes every 50 minutes, ahead of Claude Code's idle compaction at about 54.
- A session run in the wheelhouse that was being kept warm is compacted when the keepalive's hours run out, while its cache is warm; a tab idle-compacts then, past 150k tokens.
- Sessions keep a handoff with the write_handoff tool, shown as their handoff doc; Stop & Forget and Compact ask for one.
- After a reboot or a crash, a session that isn't running shows a red dot: Alt+L relaunches it, Alt+Shift+L relaunches every one.
- Alt+A adopts a Claude Code session you started outside the wheelhouse: exit it in its tab first.
- Ctrl+R takes a queued answer back into the box to edit it, before it's sent.
- Selecting a session narrows the inbox to its items: Esc, or a click on the selected session, shows every session's again.
- Ctrl+A in the inbox marks every finished item and Delete clears them together; Alt+V shows finished items again.
- Fork (Alt+K) carries a session on in a copy, in a new worktree, taking the items you choose.
- Stats' - and + zoom its charts out to days, and its cache line says when the next turn costs more.
- Click a block of a doc and your next comment on it quotes that block.
- Alt+Up and Alt+Down reorder rows; drag a splitter to resize panes, and double-click it to reset.
- Every key has a modifier, so a letter typed in the answer box is always just typed, and the app's own keys work from there: Ctrl+Q quits, F1 lists the keys, Alt+N is New session.
- With no session selected, the inbox lists each session's conversation (💬, named after the session) under the items waiting on you.
- Emoji suggestions take a click: type :star and click the one you want, or choose it with Up and Down and take it with Tab or Enter.
