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
  or "Ctrl+Enter to send". Ctrl+T switches the session's mode. A bar above the footer always
  shows the mode and two buttons: Send (this session's queue) and Send all (every
  session's), each greyed out while its queue is empty. Send all has no key: Windows
  Terminal sends Ctrl+Shift+S and Ctrl+Alt+S as plain Ctrl+S. Ctrl+R takes a queued answer
  back to edit or drop. Emoji codes work as in chat apps: `:tada:` turns into 🎉, and while
  you type `:gri` the hint line suggests matches (Tab or Enter takes the first). Text you haven't sent stays with the item (or session) you typed it
  for: moving to another clears the box, and coming back restores it. Each session shows
  its queued count (`✉ 3`). Sending doesn't change a question's status: the session's
  reply declares it, `open` while it still needs you or `answered`. Until it replies the
  question shows as `⏳` (dimmed), awaiting the session. Answers reach the session
  as a notification, even when it's idle. A running session started from an older
  wheelhouse shows "needs relaunch" (`⟳` in the inbox list): it can't hold queued answers,
  so Ctrl+Enter sends to it straight away, whatever its mode, until you `/exit` it and
  restore or adopt it again.
- **Decisions.** If your own instructions let a session decide some things without asking
  you, it reports each one as a decision (`D1`): what it decided, the alternative, why, and
  how to reverse it. Decisions block nothing. Each session counts its unseen ones (the `D`
  column); viewing one marks it seen, and it stays in the inbox until you close it with
  `X`, as with a question. To push back, answer in its thread ("reverse that").
- **Follow a session.** Click a session in the inbox's list (or Enter on it): its items
  list gains a pinned first row, 💬 Conversation, highlighted, and the right-hand pane
  follows its conversation, read live from its transcript: your prompts and general
  messages (in green, line breaks kept, without the `[wheelhouse] from …` prefix) and
  Claude's replies (in white), in full, each tool call as one line. Tool output and
  subagents are left out, and so is what the inbox already shows: answers on an item (its
  thread has them), the wheelhouse's own notices, the default opening prompt and Claude's
  wheelhouse tool calls. The box underneath sends the session a general message,
  queued and sent like any answer. The pane names the session's tab: permission prompts,
  questions asked with Claude's question dialog, and slash commands still need that tab,
  and the pane flags when the session is waiting for you there (a question or a plan;
  permission prompts can't be seen from the transcript). Going to the item list switches
  the pane back to the highlighted item.
- **Stats.** Under the item list, the session in context's numbers, read from its
  transcripts, in claude-dashboard's colours. A bordered panel, titled with the session,
  model and window, holds three gauges: context size against its window (green to
  flashing red), and the account's session and weekly usage with their reset times, as
  `/usage` shows them, fetched once a minute. Under them, whether the prompt cache is warm
  and when it goes cold (once cold, what the next turn re-pays), and its compactions.
  Below the panel, two charts of the last two hours, subagents included: how each turn's
  context was assembled (read from cache, new, cache miss) and the output tokens it made.
  A short pane drops the output chart first, then both. With no session in context, the
  running sessions' totals. The charts' shimmer rests while you type in an answer box.
  Transcripts are read off the UI thread, so a big one says "reading …" for a moment
  rather than freezing the app.
- **Sessions.** Select a session to read its synopsis, which the session keeps up to
  date itself. New session (directory, optional name, optional ticket, opening brief)
  runs Claude in the wheelhouse (below), or in a Windows Terminal tab if you tick the box.
  Browse… picks the directory from a tree (Enter opens a folder, Backspace goes up,
  Ctrl+Enter or Choose takes the highlighted one). Rename changes a session's name; a
  `/rename` in Claude Code is picked up too, within a few seconds while the wheelhouse is
  open, and before a restore relaunches it.
  Restore brings back sessions that died
  (reboot, crash), one at a time or all at once; selecting a dead (red) session also
  offers to relaunch it. Nothing restarts on its own, and a resumed session is asked to
  post the questions and tasks it already had open. Park
  hides a session until you restore it. End deletes its wheelhouse data. On a running
  session, Park and End only ask the session to do it (press again to cancel or force);
  the wheelhouse never deletes anything by itself.
- **Run in the wheelhouse.** New sessions run through the Claude Agent SDK in a small
  host process of their own (`claude-wheelhouse host`), with no tab: you read them in the
  conversation pane and answer from the inbox, where your messages arrive as ordinary
  turns. Claude Code still loads your CLAUDE.md, settings, permission mode, hooks,
  plugins, skills and MCP servers. A tool call your permission settings would have asked
  about becomes a permission item (`P1`): Allow, Always (keeps the rule Claude Code
  suggests) or Deny, from the bar; a message typed on it denies the call with your text as
  what to do instead. A question Claude asks with its question dialog becomes a question
  item. The bar also has Interrupt, Compact (the session is asked what to keep, then
  compacted with that, and the bar reports the token drop) and Shell, which hands the
  session to the real Claude Code in a tab and takes it back when you `/exit` there, plus
  a line saying what the session is doing. Closing the wheelhouse leaves the hosts
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

Keys, shown in upper case as usual (X means the x key, not Shift+X): `1` inbox, `2`
sessions, `Enter` open an item's thread (or follow a session's conversation, in the
session list), `F` show or hide finished items, `X` close the highlighted question or
decision (or reopen a closed one: a question as answered, a decision as seen), `N` new session, `A` adopt, `Esc` all sessions (or back
from a thread), `Ctrl+Enter` submit an answer (queued or sent, by the session's mode),
`Ctrl+S` send the session's queue, `Ctrl+T` switch the session between Queued and
Immediate, `Ctrl+R` take a queued answer back, `?` list every key and button, `Q` quit. Send all is a button, in the bar
above the footer.

Closing several questions or decisions at once: in the item list, `Ctrl`+click marks or unmarks a row
and `Shift`+click marks the range from the last one marked; from the keyboard, `Space`
marks or unmarks the highlighted row and `Shift+Up`/`Shift+Down` extend the range (Windows
Terminal keeps `Shift`+click for its own text selection). `X` then closes the marked
questions and decisions, or reopens them if they're all closed, `Esc` clears the marks, and a plain click
starts afresh.

Text: drag the mouse over the conversation or a thread to select part of it, `Ctrl+A`
selects all of the focused pane or answer box, and `Ctrl+C` copies the selection (through
the terminal, which Windows Terminal supports). Hold `Shift` to drag with the terminal's
own selection instead.

## Test

```
make test
```

## Licence

MIT, see [LICENSE](LICENSE).
