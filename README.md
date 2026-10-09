# claude-wheelhouse

> claude-wheelhouse is an independent open-source productivity tool. It is not
> affiliated with, endorsed by or supported by Anthropic. "Claude" is a trademark of
> Anthropic.

A sidecar for running several Claude Code sessions in parallel, on Windows with WSL and
Windows Terminal. Prototype: see `.plan/wheelhouse-sessions.md` for the design.

- **Inbox.** Every task, question and subagent status from every session in one list,
  open questions first. Pick one to preview its detail and thread; Enter opens it full
  screen as a conversation, with the session's replies. Ctrl+Enter submits an answer, and
  each session has a send mode: in Queued mode (where every session starts) answers wait
  and go out together, Ctrl+S sending the session's as one message; in Immediate mode each
  goes as you submit it. The line under the answer box says which: "Ctrl+Enter to queue"
  or "Ctrl+Enter to send". Ctrl+T switches the session's mode. Under the session list, Mode
  and Send (n) do the same, for the same session, and a bar above the footer
  always shows Send all (n), every session's queue; each Send is greyed out while its
  queue is empty. Send all has no key: Windows Terminal sends Ctrl+Shift+S and Ctrl+Alt+S
  as plain Ctrl+S. Ctrl+R takes a queued answer
  back to edit or drop. Emoji codes work as in chat apps: `:tada:` turns into 🎉, and while
  you type `:gri` the hint line suggests matches (Tab or Enter takes the first). Text you haven't sent stays with the item (or session) you typed it
  for: moving to another clears the box, and coming back restores it. Each session shows
  its queued count (`✉ 3`). Sending doesn't change a question's status: the session's
  reply declares it, `open` while it still needs you or `answered`. Until it replies the
  question shows as `processing` (dimmed), awaiting the session, and the session list's
  question count leaves it out: it counts only questions awaiting you, a queued answer
  not yet sent included. Tasks, decisions and subagents you answer show `queued` and
  `processing` the same way. Answers reach the session
  as a notification, even when it's idle. A running session started from an older
  wheelhouse shows "needs relaunch" (`⟳` in the session list): Relaunch gives it the new wheelhouse
  code (a tab session once you've `/exit`ed it). It still queues answers as usual,
  unless it is old enough to predate queued answers: then it can't hold them, so
  Ctrl+Enter sends to it straight away, whatever its mode, until it's relaunched.
- **Decisions.** If your own instructions let a session decide some things without asking
  you, it reports each one as a decision (`D1`): what it decided, the alternative, why, and
  how to reverse it. Decisions block nothing. Each session counts its unseen ones (the `D`
  column); viewing one marks it seen, and it stays in the inbox until you close it with
  `Delete`, as with a question. To push back, answer in its thread ("reverse that").
- **Follow a session.** Click a session in the inbox's list (or move to it, or Enter on it): its items
  list gains a pinned first row, 💬 Conversation, highlighted, and the right-hand pane
  follows its conversation, read live from its transcript: your prompts and general
  messages (in green, line breaks kept, without the `[wheelhouse] from …` prefix, and in
  full even when the notification that delivered one was cut short) and Claude's replies
  (in white), in full, each tool call as one line. Tool output and subagents are left
  out, and so is what the inbox already shows: answers on an item (its thread has them),
  the wheelhouse's own notices, the default opening prompt (its ticket line stays) and
  Claude's wheelhouse tool calls. The box underneath sends the session a general message,
  queued and sent like any answer. The pane names the session's tab: permission prompts,
  questions asked with Claude's question dialog, and slash commands still need that tab,
  and the pane flags when the session is waiting for you there (a question or a plan;
  permission prompts can't be seen from the transcript). Going to the item list switches
  the pane back to the highlighted item.
- **Stats.** Under the item list, the session in context's numbers, read from its
  transcripts, in claude-dashboard's colours. A bordered panel, titled with the session,
  model and window, holds three gauges: context size against its window (green under
  150k, yellow under 300k, amber under 600k, then red), and the account's session and weekly usage with their reset times, as
  `/usage` shows them, fetched once a minute. Under them, whether the prompt cache is warm
  and when it goes cold (once cold, what the next turn re-pays), and its compactions.
  Straight after a compaction a wheelhouse-hosted session shows the size its Claude Code
  reports, marked `~` as an estimate until the next response sizes it exactly; a tab
  session keeps its pre-compaction size until then.
  Below the panel, two charts of the last two hours, subagents included: how each turn's
  context was assembled (read from cache, new, cache miss) and the output tokens it made.
  Each bar covers a fixed stretch of clock (about 2 minutes at 60 columns), the rightmost
  the current one, and the scale steps 1, 2, 5, 10 so the bars hold still between turns.
  A short pane drops the output chart first, then both. With no session in context, the
  running sessions' totals. The charts' shimmer rests while you type in an answer box. The
  colours need 24-bit colour: Windows Terminal draws it but doesn't tell WSL, so the
  wheelhouse assumes it there (`TEXTUAL_COLOR_SYSTEM` overrides that).
  Transcripts are read off the UI thread, so a big one says "reading …" for a moment
  rather than freezing the app. The inbox's session list shows each session's context
  size the same way, as a one-cell bar in the same colours, filled in eighths on an
  exponential scale: two eighths each for 100k, 200k, 500k and 1M.
- **Sessions.** The left-hand column lists every session (parked ones dimmed, at the
  foot). Under it, a description of the highlighted one: its status, where it runs, its
  ticket, directory and counts, what it's doing, and its synopsis, which the session keeps
  up to date itself (its brief until it sets one), in a box that takes half the column.
  The highlighted session is the current one: highlighting an item moves the list's
  highlight to the item's session, and moving the list's highlight follows that session.
  The buttons under the description act on it, and only those that apply show, each with
  a tooltip saying what it does: its lifecycle (Rename, Relaunch and Park on a running
  session, Restore on a dead one, Unpark on a parked one, End on either), then its
  conversation (Mode, Send on one that isn't dead, and Interrupt, Compact and Shell while it runs in the
  wheelhouse). New (`N`), Adopt (`A`, a list of the sessions on disk you can adopt) and
  Restore all (`Shift+S`) act on no one session, so they're keys in the footer. Ctrl+S and Ctrl+T, the
  hint under the answer box and the activity line act on or describe the same session.
  An item opened full screen has Mode, Send and the rest in its bar, for its own session. New session (directory, optional name, optional ticket, opening brief)
  runs Claude in the wheelhouse (below), or in a Windows Terminal tab if you tick the box.
  Browse… picks the directory from a tree (Enter opens a folder, Backspace goes up,
  Ctrl+Enter or Choose takes the highlighted one). Rename changes a session's name, and
  is the only way to: a `/rename` in Claude Code isn't picked up, and the next launch
  gives Claude Code the wheelhouse's name again.
  Restore brings back sessions that died
  (reboot, crash), one at a time or all at once; selecting a dead (red) session also
  offers to relaunch it, unless it's parked. Relaunch does it in one click for a running session run in the
  wheelhouse too: its host stops (interrupting any turn under way; an open permission or
  question closes, since nothing is left to answer it) and starts again on the same
  conversation once the old process has gone, to pick up new wheelhouse code or settings.
  A tab session relaunches once you've `/exit`ed it. Nothing restarts on its own, and a resumed session is asked to
  post the questions and tasks it already had open. Park
  drops a session's items off the inbox until you unpark or restore it. End deletes its wheelhouse data. On a running
  session, Park and End only ask the session to do it (press again to cancel or force);
  the wheelhouse never deletes anything by itself.
  The buttons stay out of the Tab order, and each has a key: with the list focused, `R` renames, `L` relaunches,
  `S` restores, `Shift+S` restores all, `P` parks or unparks and `E` ends; the rest are under Keys below.
- **Run in the wheelhouse.** New sessions run through the Claude Agent SDK in a small
  host process of their own (`claude-wheelhouse host`), with no tab: you read them in the
  conversation pane and answer from the inbox, where your messages arrive as ordinary
  turns. Claude Code still loads your CLAUDE.md, settings, permission mode, hooks,
  plugins, skills and MCP servers. A tool call your permission settings would have asked
  about becomes a permission item (`P1`), answered with the Allow, Always (keeps the rule
  Claude Code suggests) and Deny buttons over its answer box, at once in either send mode.
  Text typed in the box is optional: Deny or Ctrl+Enter denies the call with it as what to
  do instead, also at once. A question Claude asks with its question dialog becomes a
  question item. Its buttons include Interrupt, Compact (the session is asked what to keep,
  then compacted with that, and the bar reports the token drop) and Shell, which hands the
  session to the real Claude Code in a tab and takes it back when you `/exit` there; the
  bar above the footer has a line saying what the session in context is doing. Closing the wheelhouse leaves the hosts
  running. `WHEELHOUSE_RUNNER=tab` makes tabs the default again. Restore and Adopt bring a
  session back the way new sessions run, so a tab from before hosts existed comes back as
  a host once you `/exit` it; Shell is the way back to a tab. Host logs are under `~/.local/state/claude-wheelhouse/hosts/`.
  This runs on your own Claude login, which Anthropic's terms allow for individual use;
  see `.plan/sdk-sessions.md` before offering it to others.
- **Adopt.** Brings a session the wheelhouse didn't launch into the wheelhouse: pick it
  from the recent sessions in `~/.claude/projects` (one whose adoption failed is offered
  again). If it's still running, type `/exit` in
  its tab first (the wheelhouse never kills it), then press Adopt; it reopens in a new tab
  with `claude --resume` and the wheelhouse's flags. Its first notification asks it to
  post the questions it is already waiting on you for and its running tasks, and to set
  its synopsis.
- **Durable.** State is in SQLite at `~/.local/state/claude-wheelhouse/wheelhouse.db`
  (override with `WHEELHOUSE_DB`), committed to disk on every change.

Only sessions launched or adopted from the wheelhouse are tracked. Nothing is installed
into your Claude Code settings: each launched session gets everything below through its
own launch flags.

## What wheelhouse puts into your sessions

Read these before you launch a session from the wheelhouse. They tell the session how to
report to the wheelhouse, and defer to your own instructions (CLAUDE.md and the like) on
how to work.

- [`protocol.md`](claude_wheelhouse/protocol.md): appended to the session's system prompt,
  followed by how your messages reach it ([`protocol_sdk.md`](claude_wheelhouse/protocol_sdk.md)
  in the wheelhouse, [`protocol_tab.md`](claude_wheelhouse/protocol_tab.md) in a tab) and
  [`protocol_decisions.md`](claude_wheelhouse/protocol_decisions.md), which
  has the session report the choices it makes without asking.
  `WHEELHOUSE_DECISIONS=0` launches sessions without that section.
- In a tab, [the `/wheelhouse` skill](claude_wheelhouse/plugin/skills/wheelhouse/SKILL.md):
  `/wheelhouse park` and `/wheelhouse end`.
- [The `wheelhouse` MCP server](claude_wheelhouse/mcp_server.py): the tools the session
  posts and reads through. Each tool's description is its docstring.
- In a tab, [the monitor](claude_wheelhouse/monitor.py) ([plugin entry](claude_wheelhouse/plugin/monitors/monitors.json))
  delivers your answers, the End and Park requests, and the note an adopted session gets
  on joining, as `[wheelhouse]` notifications. In the wheelhouse, [the host](claude_wheelhouse/host.py)
  sends the same as turns.

`claude-wheelhouse protocol` prints all of it in one go, exactly as a session receives it,
along with the launch command.

## Run

```
make install
make run
```

`make` on its own lists every command.

New to the wheelhouse? `make tutorial` starts a short demo session, run in the wheelhouse,
that posts a task, two related questions and a decision, then asks permission for one
harmless command; a checklist at the top of the right-hand pane walks you through
answering, sending, allowing and ending it. It always starts afresh (any earlier tutorial
is ended), in its own scratch directory, `claude-wheelhouse-tutorial/` next to the
database, and costs a few cents of your usual model's tokens. The first time the wheelhouse opens with no sessions it
offers the tutorial in one line: `Enter` takes it, `Esc` dismisses it for good.

Keys, shown in upper case as usual (F means the f key, not Shift+F): `Enter` open an item's thread (or follow a session's conversation, in the
session list), `F` show or hide finished items (the footer says which it will do), `Delete` or `Backspace` in the item list close the
highlighted question or decision (or reopen a closed one: a question as answered, a decision as seen), `N` new session, `A` adopt, `Shift+S` restore all, `Esc` all sessions (or back
from a thread), `Ctrl+Enter` submit an answer (queued or sent, by the session's mode),
`Ctrl+S` send the session's queue, `Ctrl+T` switch the session between Queued and
Immediate, `Ctrl+R` take a queued answer back, `?` list every key and button, `Q` quit (it asks first
while a relaunch is waiting for a host to stop). Outside a text box, the buttons that have no other key:
`I` Interrupt, `C` Compact, `H` Shell, `1` Allow, `2` Always and `3` Deny on a permission item (numbered
as in Claude Code's own prompt), and `Shift+A` Send all. A key whose button doesn't apply says so.

`Tab` goes round the panes: the session list, the items, the answer box, then the conversation (one
stop, where the arrows, `PgUp`, `PgDn`, `Home` and `End` scroll it), and `Shift+Tab` goes back.
`Ctrl+Enter` in the answer box goes back to the items with the cursor where it was, so answering a run
of items is: type, `Ctrl+Enter`, `Down`, `Tab`, type, `Ctrl+Enter`. An item opened full screen has two
stops, its answer box and its thread. These keys keep their order when typed ahead of the screen.

Closing several questions or decisions at once: in the item list, `Ctrl`+click marks or unmarks a row
and `Shift`+click marks the range from the last one marked; from the keyboard, `Space`
marks or unmarks the highlighted row and `Shift+Up`/`Shift+Down` extend the range (Windows
Terminal keeps `Shift`+click for its own text selection). `Delete` or `Backspace` then closes the marked
questions and decisions, or reopens them if they're all closed, `Esc` clears the marks, and a plain click
starts afresh.

Text: drag the mouse over the conversation or a thread to select part of it (shown black
on white), `Ctrl+A` selects all of the focused pane or answer box, and `Ctrl+C` copies the
selection (through the terminal, which Windows Terminal supports). Right-click does as a
terminal's does: it copies the selection, or with nothing selected pastes the clipboard
into the answer box under the pointer (or the focused one). Under WSL the paste reads the
Windows clipboard through PowerShell, so it takes most of a second; elsewhere it pastes the
wheelhouse's own last copy. Hold `Shift` to drag with the terminal's own selection
instead.

Every boundary between areas can be dragged: the lines between the three columns, between
the session list and its description, between the items and the stats, and above the
answer box. A line turns teal under the pointer and while you drag it, and no pane goes
below its minimum (the session column never gets too narrow for its buttons' captions).
The sizes are kept in the wheelhouse's database, as shares of the space, so they follow a
resized terminal; double-click a line to put its default back. A size that doesn't fit a
smaller terminal shrinks there, and comes back when there's room again. The three columns
fit an 80-column terminal.

Scrollbars are one cell wide: a teal thumb with solid ends and a knurled Braille grip,
on a thin teal track. Drag the thumb, or click the track to page. The thumb brightens
under the pointer and turns white while you drag it. Text boxes and the footer, one row
high, have none.

## Test

```
make test
```

## Licence

MIT, see [LICENSE](LICENSE).
