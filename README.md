# claude-wheelhouse

> claude-wheelhouse is an independent open-source productivity tool. It is not
> affiliated with, endorsed by or supported by Anthropic. "Claude" is a trademark of
> Anthropic.

A sidecar for running several Claude Code sessions in parallel, on Windows with WSL and
Windows Terminal. Prototype: see `.plan/wheelhouse-sessions.md` for the design.

- **Inbox.** Every task, question and subagent status from every session in one list,
  open questions first. Pick one to preview its detail and thread; Enter opens it full
  screen as a conversation, with the session's replies. Ctrl+S queues an answer, and
  queued answers go out together: `s` sends the selected session's as one message, `S`
  sends every session's. Ctrl+X sends one answer now. Ctrl+R takes a queued answer back
  to edit or drop. Each session shows its queued count (`✉ 3`). Answers reach the session
  as a notification, even when it's idle. A running session started from an older
  wheelhouse shows "needs relaunch" (`⟳` in the inbox list): it can't hold queued answers,
  so Ctrl+S sends to it straight away until you `/exit` it and restore or adopt it again.
- **Sessions.** Select a session to read its synopsis, which the session keeps up to
  date itself. New session (directory, optional name, optional ticket, opening brief)
  opens a Windows Terminal tab running Claude. Restore brings back sessions that died
  (reboot, crash), one at a time or all at once; nothing restarts on its own. Park
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

- [`protocol.md`](claude_wheelhouse/protocol.md): appended to the session's system prompt.
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

Keys: `1` (or `i`) inbox, `2` sessions, `Enter` open an item's thread, `f` show or hide
finished items, `n` new session, `a` adopt, `Esc` all sessions (or back from a thread),
`Ctrl+S` queue an answer, `Ctrl+X` send it now, `Ctrl+R` take a queued answer back, `s`
send the selected session's queued answers, `S` send all, `q` quit.

## Test

```
make test
```

## Licence

MIT, see [LICENSE](LICENSE).
