# Wheelhouse sessions (prototype)

Issue: #1

## Intent

Run several Claude Code sessions in parallel and keep track of them from one place.
Every task, question and subagent status lives in a store with a stable id and a full
detail body, so Claude writes it once and refers to it by id (`Q3`), and the person
reads it in one inbox instead of scrolling back through several terminals. Answers and
hints typed into the wheelhouse reach the right session, and wake it if it is idle.

The wheelhouse is a sidecar: conversation still happens in the Claude Code terminal. It is
also standalone: it only tracks sessions it launched, needs no global hooks, settings or
`CLAUDE.md` changes, and leaves every other session alone.

## Shape

```
  claude-wheelhouse (Textual TUI)            one per Linux user
        |  reads / writes
        v
  ~/.local/state/claude-wheelhouse/wheelhouse.db  SQLite, WAL, synchronous=FULL
        ^                     ^
        | MCP tools           | polls every 2 s
  wheelhouse MCP server wheelhouse monitor       both started per session by the
  (per session)         (per session)            wheelhouse plugin / launch flags
        \                     /
         Claude Code session in a Windows Terminal tab
```

- **Store.** SQLite is the only source of truth. Every write is its own committed
  transaction before the call returns. The TUI, MCP servers and monitors are separate
  processes that only talk through the database, so any of them can die without the
  others noticing. The TUI is a window onto the data, nothing more.
- **Location.** `~/.local/state/claude-wheelhouse/wheelhouse.db`, overridable with `WHEELHOUSE_DB`.
  Paths under `/mnt/` are refused: SQLite locking on the Windows drive mount is not
  reliable.
- **One instance per Linux user.** Each user has their own database in their own home.

## Lifecycle

| State | Meaning | How it is derived |
|---|---|---|
| starting | launched, not yet registered | no process recorded for this launch, launched under 90 s ago |
| live | session process running | recorded Claude pid exists in `/proc` with the same start time, same boot id |
| stalled | live, but quiet | live, heartbeat older than 120 s, and the wheelhouse has not just woken from sleep. A hint only |
| dead | process gone | anything else |
| parking / ending | Park or End pressed on a running session | `park_requested_at` or `end_requested_at` set and the session is live, stalled or starting |
| (ended) | `/wheelhouse end`, or End on a dead session, or Force end | rows deleted |

**Parked is a flag, not a state.** It is shown alongside the state ("dead · parked"),
hides the session from the inbox and keeps it off Restore All. It never hides whether
the process is running: a parked session that is still running is live, and Restore
and End treat it as live.
If you park a session and keep working in its tab, its new questions stay out of the
inbox too, by design: parking is your own signal to set it aside.

- **Life and death come from the process, not the heartbeat.** Sleep and hibernate keep
  the process, so the session stays live and the heartbeat resumes on wake. A reboot or
  WSL shutdown changes the boot id, so the session is dead. Start time guards against pid
  reuse.
- **Clock jumps.** The TUI compares wall-clock and monotonic time between ticks. A jump
  over 30 s means the machine slept, and the stalled hint is suppressed for 60 s while
  everything catches up.
- **Never respawn automatically.** The Sessions page has Restore on each dead row and
  Restore All, and selecting a dead session (in either list) asks "Relaunch it?" Both re-check the process immediately before launching and refuse a
  session that is still starting (launched under 90 s ago, not yet registered), so a
  double press opens one tab. The launch wrapper then checks and registers in a single
  compare-and-set transaction before it execs Claude, so two tabs racing for the same
  session can't both start it.
- **The wheelhouse never deletes or hides a running session's data behind its back.** There
  is no automatic clean-up of any kind; every deletion and every park comes from the
  session itself or from a button the person pressed and confirmed.
- **Park and End on a running session are requests.** The button (after a confirm that
  names the session) sets `park_requested_at` or `end_requested_at`. The session's
  monitor passes the request on, and Claude acts on it: for End it does its usual
  session-end memory save, then calls `end_session`; for Park it brings its items up to
  date, then calls `park_session`. The row shows parking or ending meanwhile. Pressing
  the button again offers **Cancel** (clears the request; if the monitor had already
  passed it on, recorded as `end_told_at` / `park_told_at`, the session also gets a
  "carry on" message) or **Force** (a second confirm naming the
  session: Force end deletes the rows without the memory save, Force park sets the
  flag). One request at a time: the other button says to cancel the first.
- **Park and End on a dead session act straight away**, after a confirm that names the
  session. Unpark is immediate. Any launch clears leftover requests, so a restored
  session is never told to end or park itself.
- **A session can vanish at any moment** (an in-session `/wheelhouse end`, or Force from another
  wheelhouse), including while one of the wheelhouse's dialogs is open. Every session action in
  the TUI goes through one guard (`session_action`): if the row has gone it says
  "session no longer exists" and repaints. The dead-session path re-checks liveness when
  the confirm is answered: if the session came back, it acts on nothing.
- **In-session `/wheelhouse park` and `/wheelhouse end`** are unchanged: the session saves what it needs, then
  acts on itself. Claude Code's own transcript is untouched either way.

## Launching

**New session** takes a working directory, an optional name, an optional ticket ref
(`#42`, `owner/repo#42`, or a Linear key such as `ABC-123`) and an opening brief. It
writes the session row, then opens a Windows Terminal tab:

```
cmd.exe /c wt.exe -w 0 new-tab --title <name> wsl.exe -d <distro> -u <user> --cd <dir> -- \
    <login shell> -lic "exec <python> -m claude_wheelhouse run <session-id>"
```

wsl.exe runs its command with no shell, so the user's profile never runs and
`~/.local/bin`, where claude is installed, is missing from the `PATH`. Going through the
user's login shell (`$SHELL`, else their passwd entry, else bash) gives the tab the
environment of an ordinary WSL tab, which hooks and MCP servers need too. It runs
interactive (`-i`) as well, because a non-interactive bash stops at the interactive guard
near the top of `~/.bashrc` and misses whatever is set below it, such as Homebrew's PATH.

`wt.exe` is a Windows execution alias that WSL can't execute directly (it resolves on
the `PATH` but does nothing), so it goes through `cmd.exe /c` as Microsoft's docs say.
cmd re-parses the line, so its metacharacters are stripped from the title and refused
in the directory. Each tab start is appended to `launch.log` next to the database,
along with any output from cmd.exe or wt.exe, so a launch that fails leaves a trace.
A brief starting with `-` gets a `Brief:` header, because claude reads a leading `-` as
an option.

A directory Claude Code doesn't trust yet shows its trust prompt in the new tab; answer
it there.

Everything else is read from the database by `claude_wheelhouse run`, which then execs:

```
claude --session-id <id> | --resume <id>
       --plugin-dir <wheelhouse plugin>
       --mcp-config <inline JSON: the wheelhouse MCP server for this session>
       --append-system-prompt <wheelhouse protocol>
       -n <name>
       [opening brief, first launch only]
```

with `WHEELHOUSE_SESSION_ID`, `WHEELHOUSE_DB` and `WHEELHOUSE_PYTHON` in the environment. Keeping the
brief out of the `wt.exe` command line avoids its `;` command separator and Windows
quoting entirely.

`--resume` is used when a transcript for the id already exists under
`~/.claude/projects/`, otherwise `--session-id` and the brief.

## The wheelhouse plugin

A static plugin directory shipped in the package, loaded per session with `--plugin-dir`:

- **Monitor** (`monitors/monitors.json`): runs `claude_wheelhouse monitor` for the whole
  session. It polls the database every 2 s and prints one line per new message from the
  person. Plugin monitors deliver each printed line to Claude as a notification and
  Claude interjects when one arrives, idle or mid-task. This replaces both the
  "listener that exits to wake the session" and the `PostToolUse` hook from the design
  discussion, and it restarts with the session on resume, so no `SessionStart` hook is
  needed either.
- **Skill**: one `wheelhouse` skill, run as `/wheelhouse park` or `/wheelhouse end`; it
  reads `$ARGUMENTS` and replies with usage for anything else. A plugin skill whose
  frontmatter sets `name` answers to the bare name as well as the namespaced one ("The
  bare `/fancy` also invokes the skill unless another command already uses that name",
  code.claude.com/docs/en/skills). Checked headless on `/wheelhouse` itself (claude
  2.1.287, `disable-model-invocation: true`): the session lists the skill as
  `wheelhouse:wheelhouse`, and `/wheelhouse bogus` returns the usage line. `/wheelhouse:wheelhouse`
  remains as a fallback if another command takes the bare name.

## Data model

```
sessions  id (uuid) PK, name, ticket, brief, cwd, parked (0/1),
          created_at, launched_at,
          claude_pid, claude_start, boot_id, heartbeat_at,
          end_requested_at, park_requested_at
items     id PK, session_id FK, ref ('T3' | 'Q1' | 'A2', unique per session),
          kind (task | question | agent), title, body, status,
          created_at, updated_at
messages  id PK, session_id FK, item_ref (nullable), author (claude | person),
          body, created_at, claimed_at, delivered_at
agent_items  (session_id FK, agent_id) PK, item_ref: the A item for each subagent
          the wheelhouse tracks (see Subagents)
```

Deleting a session cascades. Times are UTC ISO-8601; claim and request stamps carry
microseconds so one claim or request is never mistaken for the next. Columns added after
the first release are added on start, inside one transaction, so processes starting
together against an older database can't both add the same column.

Statuses: task `todo running blocked waiting done dropped`; question
`open answered closed`; agent `running done failed`. A person's message on an open
question marks it answered.

## Subagents

The wheelhouse tracks each session's subagents as A items itself (#69), one mechanism for
SDK and tab sessions, rather than relying on the session to post them
(`claude_wheelhouse/subagents.py`). Protocol version 7 tells sessions not to post their own.

- **Start.** Claude Code writes `<session id>/subagents/agent-<agent id>.meta.json` beside
  the transcript at launch: `description`, `agentType`, `toolUseId` (the Agent tool call;
  absent for a forked skill), `requestShape` (`foreground` or `background`, absent in older
  versions), and `parentAgentId` with `spawnDepth` 2 or more for a subagent's own
  subagents, which are left out. The start is the first record's `timestamp` in
  `agent-<agent id>.jsonl`. The item is made 15 s after the start (GRACE), so that a
  session on older code that posts its own has done so first.
- **Finish,** in the main transcript. A foreground subagent: its tool_result (done; failed
  if `is_error`). A background one: its launch's tool_result (`toolUseResult.status:
  "async_launched"`) finishes nothing; a `<task-notification>` with its `<task-id>` (the
  agent id) or `<tool-use-id>` does, delivered as a `queue-operation`, a `queued_command`
  attachment or a user turn. `<status>`: `completed` is done; `failed`, `killed` (the person
  stopped it) and `stopped` (its session ended under it, reported on resume) are failed,
  with the notification's summary as a note. Only the notification's header counts: every
  `<task-id>` before `<summary>` (one can name several under one status), and the first
  `<status>`, `<tool-use-id>` and `<summary>`; the subagent's text in `<result>` is never
  read as fields. A notification quoted anywhere else (a message, tool output) is ignored.
  SendMessage to a finished subagent resumes it: running again, after a restart too.
- **Its session dies.** When the TUI's liveness check has a session dead, each of its
  subagent items still running fails, with the note "the session stopped while this
  subagent ran" (parked or not, on the next tick: no timer of its own). If the session is
  resumed and the subagent's notification arrives, the item follows it as usual.
- **Once.** `agent_items` keys the item by agent id, written in the item's transaction, so
  a restart or a second wheelhouse never posts twice. A status is written only when it
  changes, including a finish seen by a wheelhouse that didn't make the item.
- **The session's own item** is taken instead of making one when there is an agent item
  not yet tied to a subagent, made no more than 10 minutes before the subagent started nor
  15 s after it, whose title equals the description or holds it, or is held by it (as
  words, case and punctuation aside, and two words at least: a one-word description or
  title matches only exactly). An exact title wins, then the one made nearest the start. One the
  session posts after the wheelhouse made its own is a duplicate: the protocol now asks
  sessions not to post.
- **Cut-off.** The later of when the session joined the wheelhouse (`created_at`: launched
  or adopted) and when the database was first opened by this code (`settings.agents_since`).
  A subagent started after it gets an item, finished or not. One started before it gets an
  item only if it was still running then: no finish in the transcript, and its transcript
  written to within 30 minutes (LIVE) of the cut-off. So adopting a session, or upgrading,
  brings in what's running and not the history.
- **Cost.** The TUI syncs each session on a worker thread every tick while it runs (once
  for one that isn't). The subagents folder is listed again only when its mtime changes;
  the main transcript is read from the offset the last read reached. A first read starts at
  the earliest subagent still running (`stats.window_start`), or at the end when none is.
  An idle sync is two `stat`s, about 25 us; a first sync of a 13 MB transcript with 141
  subagents takes 4 ms with nothing running.

## MCP tools (server name `wheelhouse`)

| Tool | Does |
|---|---|
| `post_item(kind, title, body, status?)` | creates T/Q/A item, returns its ref |
| `update_item(ref, status?, title?, body?, note?)` | edits; a note is appended to the item's thread |
| `get_input(ref?)` | undelivered messages from the person, or the full thread for one ref (whose undelivered messages then count as delivered) |
| `list_items(include_closed?)` | this session's items |
| `park_session()` / `end_session()` | lifecycle; `park_session` also settles a pending park request |

Delivery is claim, show, confirm. A claim is one transaction, so the monitor and
`get_input` never take the same message. The monitor confirms a message only after
printing and flushing it; if stdout has closed it releases the rest for redelivery, and
a claim abandoned by a monitor killed mid-print is retaken after 30 s. Confirm and
release touch only messages still under the caller's own claim, so a monitor whose claim
was retaken can't confirm someone else's. A thread shown by `get_input(ref)` leaves out
messages the monitor has claimed and is printing, so they aren't shown twice.

One duplicate window is left, by design: a monitor stuck in `print` for over 30 s (Claude
Code not reading its stdout) has its claim retaken by `get_input`, so when the stuck
print finally completes the message has been shown twice. The old claimer's confirm is
ignored, so the database stays right; the cost is one repeated line, in a case that
needs Claude Code itself to stall.

Writes check the session inside their transaction (`BEGIN IMMEDIATE` holds the write
lock, so the row can't vanish between check and write) and raise `SessionGone` if it
has gone. Wheelhouse tools then answer "this session was force-ended in the wheelhouse (or has
ended): stop using wheelhouse tools", and the monitor prints the same once and exits.

`claude_wheelhouse run` registers the session's pid, start time and boot id just before it
execs Claude, as a compare-and-set that fails if another live Claude holds the session.
exec keeps the pid, so the registered pid is Claude's, and a session at the trust prompt
or with a slow MCP server never looks dead. The server only writes a
heartbeat, on start and every 30 s. Tool calls run in worker threads, so the store
serialises access to its one connection with a lock.

## Adopting a session

Adopt brings a session the wheelhouse didn't launch into the wheelhouse, by handoff. Hot adoption
(attaching to a session without restarting it) is deferred.

- **Candidates.** Transcripts under `~/.claude/projects/*/*.jsonl` touched in the last
  14 days, at most 40, most recently active first. Subagent transcripts sit a level
  deeper and are never offered; headless runs (`entrypoint` other than `cli`) and
  sessions never prompted (only slash commands, e.g. a cancelled `/resume`) are skipped.
  Sessions the wheelhouse already tracks are offered only while not open in a wheelhouse
  tab (status dead), keeping their name: an adoption whose tab failed stays adoptable.
  The title is the session's custom title, else its AI title, else its first typed
  prompt. Only the first and last 256 KB of each file are read.
- **Last active.** The timestamp of the last prompt or reply, else the file's mtime. Not
  the mtime first: an open but idle session keeps appending untimestamped mode and
  permission records, which made it look active.
- **Directory.** The cwd whose encoded form matches the transcript's folder (where
  `--resume` finds it), not the latest cwd, since a session may have moved since.
- **Still running?** Claude Code writes `~/.claude/sessions/<pid>.json` (pid,
  `sessionId`, `procStart`) for each running session. Records of dead sessions linger,
  so one counts only while `/proc/<pid>/stat` agrees on the start time. A running
  session is never launched: the dialog says to type `/exit` in its tab, and Adopt checks
  again when pressed. `claude_wheelhouse run` also refuses inside the new tab if the session
  is running elsewhere, which covers Restore as well.
- **Launch.** The session is registered under its own Claude session id with
  `adopted = 1`, then opened like a restore: `claude --resume <id>` with the wheelhouse's
  plugin, MCP server and protocol. If `wt.exe` fails to start, the row is removed
  again; a tab that starts and then fails can't be seen from the wheelhouse, so its row
  stays and the session is offered again. Adopting a tracked session reuses its row,
  renamed if a new name was given.
- **The protocol needs `--system-prompt-snapshot off`.** Claude records the system
  prompt at a conversation's first request and replays it on every resume, so an
  adopted session never sees `--append-system-prompt`. Checked on 2.1.287: with the
  default the resumed session didn't see an appended instruction, with `off` it did, and
  a later default resume lost it again. So adopted sessions launch with `off` every time.
- **Open work comes with it.** A session adopted mid-conversation may already be waiting
  on the person, but the wheelhouse only sees items posted after adoption. Whenever a tab
  resumes a conversation (Adopt, Restore, Restore All, relaunching a dead session),
  `launch.open_tab` queues a notice (`messages.kind = 'notice'`, printed as `[wheelhouse] ...`
  with no sender) asking the session to post its open questions and running tasks,
  skipping any already listed, and to set its synopsis. The notification triggers a
  turn as soon as the monitor starts. The protocol says the same, for a session that
  joins without the notice.

## TUI

- **Inbox tab.** Sessions on the left (a status dot, the name, the open-question count,
  and a Cylon scanner while anything is running; parked sessions are left out). Items in the centre from
  every session, ordered: open questions, blocked or waiting tasks, running, the rest.
  Selecting a session filters; Esc clears. Detail on the right: body, thread, and an
  answer box. Ctrl+S queues; Ctrl+Enter (ctrl+j) sends now.
- **Session view.** Selecting a session (one click, or Enter) also puts its main
  conversation in the right-hand pane, so the person can follow and talk to a session
  without switching tabs. It is read from the transcript
  (`~/.claude/projects/<project>/<id>.jsonl`), tail only (the last 1 MB, at most 80
  entries), re-read when the file changes. Shown: prompts, Claude's text, `[wheelhouse]`
  notifications; each tool call is one line; tool results, thinking, sidechain
  (subagent) records and bookkeeping are left out. The answer box sends a general
  message (no ref) through the same queue. It stays read-mostly: it never mirrors
  permission prompts or slash commands. The pane names the session's tab, since
  `wt` can only focus a tab by index, not by title, and tabs move. An unanswered
  AskUserQuestion or ExitPlanMode call in the transcript shows as "waiting for you in its
  tab". A permission prompt leaves no record, so it can't be flagged this way; a
  `Notification` hook in the wheelhouse plugin could report it. Selecting a session pins a
  "💬 Conversation" row first in its filtered item list and highlights it, so no question
  is selected while the pane shows the conversation; moving to a question shows that
  question. With no session selected there is no conversation row: the list is in inbox
  order, not grouped by session. The pane and the answer box change target only when the
  person moves the highlight. The one-second refresh restores the highlight by the row's
  key, not its position, so an item arriving above doesn't swap the text being typed. The person's words show in terminal green and Claude's in
  white, here and in item threads. The pane is one widget drawing one Rich renderable, each
  block's lines cached per width: as a Textual Markdown widget it made a child per
  paragraph, and with a long conversation's ~900 children every layout pass took a quarter
  of a second, so typing lagged and opening a session took over 3 seconds.
- **Session area** (#58, which replaced the Sessions tab and the Inbox/Sessions tabs).
  The session list, every session with parked ones dimmed at the foot; under it a
  description of the current session (status, runner, mode, ticket, directory,
  open-question, running, unseen-decision and queued counts, activity, synopsis or brief);
  under that its buttons, only those that apply (#62, D26): Rename, Relaunch and Park on
  a live session; Restore on a dead one, parked or not (unparks only once the launch goes
  through); Unpark on a parked one, live or dead; End on either. Park and End follow the
  lifecycle rules above (a dead session is no longer parked from here: End it, or Restore
  it and park it). Under those the conversation buttons moved from the send bar (#59):
  Mode (across two columns, for "Can't queue: relaunch") and Send (n) on any session,
  Interrupt, Compact and Shell while it runs in the wheelhouse. Three to a row with a
  blank row between rows, all 30% grey (#4d4d4d) with white text, lighter under the
  pointer and pressed, dimmed when disabled, each with a tooltip (the ? overlay's text). A
  hidden button leaves no hole: the grid places the shown ones in turn. New (`N`), Adopt
  (`A`, the dialog listing the sessions on disk that can be adopted) and Restore all
  (`Shift+S`, dead and not parked) act on no one session, so they're footer keys, not
  buttons. The send bar keeps Send all and the activity line.
- **D20: one current session, highlighted in the session list.** The session area's
  buttons, Ctrl+S and Ctrl+T, the hint under the answer box and the activity line all act
  on or describe it. The list's highlight follows the session in context: highlighting an
  item, or following a session, moves it to that session, as the app's own move, which
  filters nothing. The person moving it follows the session they move to, as a click
  always did (filter and 💬 Conversation row), at once on every arrow: no follow waits on a
  timer for a refresh or a key to overtake (review 11 dropped the 0.1 s debounce, and the
  class of bugs in its window). The first visit to a session with a 1.2 MB transcript
  costs about 15 ms to follow and 250 to 350 ms to lay out its conversation; later visits
  a few milliseconds. Only a click or Enter offers to relaunch a dead one. The item list's
  cursor goes to the selection by key whenever they differ, except over an arrow of the
  person's whose highlight hasn't been handled yet. (Review 8 replaced the first D20, where the buttons acted on the highlighted
  session and the keys on the session in context: the two could differ on screen, and the
  tutorial's "Ctrl+S, or the Send button" sent another session's queue.) A full-screen
  item has no session list, so its bar keeps Mode, Send, Interrupt, Compact and Shell, for
  its own session. The stats pane still shows every running session while no session is
  followed and no item selected.
- **Resizable layout** (#60). Every boundary is a `Splitter` (`splitter.py`; Textual has
  none): the two between the columns, the session list and its description (the
  description 50% of the column by default), the items and the module panes under them,
  and the conversation and the answer box. A one-cell line that captures the mouse on a
  press, sizes one neighbour as a percentage of the parent while the other (1fr) takes
  the rest, never past either's CSS minimum, and turns teal under the pointer and while
  dragged. Percentages keep the proportions on a resized terminal. Each size is kept in
  the settings table (`layout.<key>`), and a double-click deletes it, putting the
  stylesheet's default back. The session column's minimum, 39 cells, is what its
  buttons' captions need; the items' 12 and the right pane's 24 leave the three columns
  on an 80-column terminal. The session list keeps 5 rows, the description giving way on a
  short terminal. `splitter.fit` runs on the first layout and every resize: where the
  sizes (a kept share, or the stylesheet's) don't fit, each sized pane shrinks towards its
  minimum by its share of the overflow, in cells, leaving the kept share for a terminal
  with room. A kept size that isn't a finite number is ignored, and one outside 0 to 1
  clamped. A splitter with a neighbour missing does nothing, and a pane swapped for an
  error card keeps its place under one (`-split`). A mouse move with no button held ends
  a drag whose release was lost.
- **Relaunch** (#56). Dead: as Restore. Running in the wheelhouse: SIGTERM to the host's
  registered pid, checked against its start time first (the host's own clean stop:
  Claude Code disconnected, waiting permissions and questions withdrawn, anything left
  closed by the next host's `deny_stale`). The refresh tick starts it again with
  `launch.open_session` (so it stays a host) once the process has gone, and gives up,
  saying so, after 30 s. A tab, or a host's session in a shell tab, relaunches only once
  it has exited. A signal rather than a new host command: every host since the first
  handles SIGTERM, including the stale ones a relaunch is for.
- **Look.** Matrix green inside panels; colour and a shimmering title bar on the chrome.

## Relationship to the cache dashboard

Since #8 the wheelhouse is its own repo; the cache dashboard stays in
[agentic-utils/claude-dashboard](https://github.com/agentic-utils/claude-dashboard).
Before that, in the prototype, `claude_dashboard.py` stayed as it was: single file, stdlib only,
because the Homebrew formula installs it by copying that file. The wheelhouse was a separate
project in `wheelhouse/` with its own dependencies (Textual, the MCP SDK) and its own
`claude-wheelhouse` command.

## Path to a single tool

The goal is one tool: the wheelhouse, with the cache view as one of its tabs.

1. Package the repo as one Python project with both commands (`claude-wheelhouse`, and
   `claude-dashboard` kept as an alias that opens the cache tab), installed with
   `uv tool install` instead of the Homebrew single-file copy. Point the Homebrew formula
   at the package, or retire it with a note in the README.
2. Port the cache view into a Textual tab beside Inbox and Sessions, reusing its data
   code; keep its terminal-only mode for anyone who wants it.
3. Drop the separate single-file script once the tab matches it.

## Verified Claude Code facts (v2.1.287, `claude --help` and code.claude.com docs)

| Fact | Result |
|---|---|
| `--session-id <uuid>` | exists |
| `--mcp-config <configs...>` takes files or JSON strings | exists |
| `--settings` hooks merge with user settings | true: "Hook entries merge across settings levels rather than replacing each other" (not used now) |
| `--append-system-prompt` | exists. With the default `--system-prompt-snapshot on`, a resumed session reuses the prompt recorded at first launch |
| per-session plugin | `--plugin-dir <path>`, "for this session only" |
| plugin skills are namespaced | true: `/<plugin>:<skill>` |
| plugin monitors run for the whole session and their output reaches Claude as notifications | true (docs, components page) |
| `-n, --name` | exists; sets the display name and terminal title |
| `--resume <id>` keeps the session id and its transcript file | true (headless check) |
| `--system-prompt-snapshot off` makes a resumed session see `--append-system-prompt` | true (headless check); the default does not |
| `~/.claude/sessions/<pid>.json` records each running session with `sessionId` and `procStart` (= `/proc/<pid>/stat` field 22) | true |
| `wt.exe new-tab --title`, `-w 0` | true (Microsoft docs) |
| `wt.exe` callable directly from WSL | false: it is a 2-byte execution alias; `cmd.exe /c wt.exe` works |

## Verified end to end

A real session launched through `claude_wheelhouse run` (headless, in a pty) registered its
Claude process, started the monitor, posted `T1` through the MCP server, received a
wheelhouse message through the monitor while idle, acted on it, and deleted its own data
with `end_session`. A second headless run checked the registered pid directly: it was
the forked pid, `/proc/<pid>/comm` was `claude` with the `--session-id` command line,
and it was still the running process (with the wheelhouse's MCP server and monitor as
children) after the trust prompt and the brief. A launch through Windows Terminal reached `claude_wheelhouse run` in the
new tab (confirmed by `launch.log`).

## Open questions

1. Restore All after a reboot also catches sessions that died days ago and were never
   parked. Should Restore All only take sessions that were live in the last boot?
2. Notification size: Claude Code cuts a monitor notification at 500 characters, so the
   monitor keeps each line within 480 and a longer message leads with a
   `get_input(message_id=N)` pointer. Settled by the platform, not a choice.
3. When to take the first step of "Path to a single tool": straight after the prototype
   settles, or once the wheelhouse has been in daily use for a while?
