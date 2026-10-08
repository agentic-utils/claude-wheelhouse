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
  goes as you submit it. Ctrl+T switches the session's mode. A bar above the footer always
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
  column); viewing one marks it seen, and it moves to the finished items (F) once you move
  on. To push back, answer in its thread ("reverse that").
- **Follow a session.** Click a session in the inbox's list (or Enter on it): its items
  list gains a pinned first row, 💬 Conversation, highlighted, and the right-hand pane
  follows its conversation, read live from its transcript: your prompts
  (in green) and wheelhouse notifications, and Claude's replies (in white), in full, each tool call as one line, tool
  output and subagents left out. The box underneath sends the session a general message,
  queued and sent like any answer. The pane names the session's tab: permission prompts,
  questions asked with Claude's question dialog, and slash commands still need that tab,
  and the pane flags when the session is waiting for you there (a question or a plan;
  permission prompts can't be seen from the transcript). Going to the item list switches
  the pane back to the highlighted item.
- **Stats.** Under the item list, the session in context's numbers, read from its
  transcripts: context size against its window (green to flashing red), whether its
  prompt cache is warm and when it goes cold (once cold, what the next turn re-pays), its
  compactions, and a chart of how each turn's context was assembled over the last two
  hours (read from cache, new, cache miss), subagents included. With no session in
  context, the running sessions' totals.
- **Sessions.** Select a session to read its synopsis, which the session keeps up to
  date itself. New session (directory, optional name, optional ticket, opening brief)
  opens a Windows Terminal tab running Claude. Restore brings back sessions that died
  (reboot, crash), one at a time or all at once; selecting a dead (red) session also
  offers to relaunch it. Nothing restarts on its own, and a resumed session is asked to
  post the questions and tasks it already had open. Park
  hides a session until you restore it. End deletes its wheelhouse data. On a running
  session, Park and End only ask the session to do it (press again to cancel or force);
  the wheelhouse never deletes anything by itself.
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
  followed by [`protocol_decisions.md`](claude_wheelhouse/protocol_decisions.md), which
  has the session report the choices it makes without asking.
  `WHEELHOUSE_DECISIONS=0` launches sessions without that section.
- [The `/wheelhouse` skill](claude_wheelhouse/plugin/skills/wheelhouse/SKILL.md):
  `/wheelhouse park` and `/wheelhouse end`.
- [The `wheelhouse` MCP server](claude_wheelhouse/mcp_server.py): the tools the session
  posts and reads through. Each tool's description is its docstring.
- [The monitor](claude_wheelhouse/monitor.py) ([plugin entry](claude_wheelhouse/plugin/monitors/monitors.json)):
  delivers your answers, the End and Park requests, and the note an adopted session gets
  on joining, as `[wheelhouse]` notifications.

`claude-wheelhouse protocol` prints all of it in one go, exactly as a session receives it,
along with the launch command.

## Run

```
make install
make run
```

`make` on its own lists every command.

Keys, shown in upper case as usual (X means the x key, not Shift+X): `1` inbox, `2`
sessions, `Enter` open an item's thread (or follow a session's conversation, in the
session list), `F` show or hide finished items, `X` close the highlighted question (or
reopen a closed one as answered), `N` new session, `A` adopt, `Esc` all sessions (or back
from a thread), `Ctrl+Enter` submit an answer (queued or sent, by the session's mode),
`Ctrl+S` send the session's queue, `Ctrl+T` switch the session between Queued and
Immediate, `Ctrl+R` take a queued answer back, `Q` quit. Send all is a button, in the bar
above the footer.

Closing several questions at once: in the item list, `Ctrl`+click marks or unmarks a row
and `Shift`+click marks the range from the last one marked; from the keyboard, `Space`
marks or unmarks the highlighted row and `Shift+Up`/`Shift+Down` extend the range (Windows
Terminal keeps `Shift`+click for its own text selection). `X` then closes the marked
questions, or reopens them if they're all closed, `Esc` clears the marks, and a plain click
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
