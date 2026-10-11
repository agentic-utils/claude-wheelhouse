# claude-wheelhouse

> claude-wheelhouse is an independent open-source productivity tool. It is not
> affiliated with, endorsed by or supported by Anthropic. "Claude" is a trademark of
> Anthropic.

A sidecar for running several Claude Code sessions in parallel, on Windows with WSL and
Windows Terminal. Prototype: see `.plan/wheelhouse-sessions.md` for the design.

## Requirements

- Windows with WSL and Windows Terminal. The wheelhouse runs inside WSL and opens each
  session in a new Windows Terminal tab (through `cmd.exe` and `wt.exe`).
- [uv](https://docs.astral.sh/uv/), which installs everything else, Python included.
- Python 3.12 or later (`requires-python` in `pyproject.toml`). uv fetches 3.12 if your
  system doesn't have it.
- Claude Code, installed in WSL and logged in, with `claude` on the `PATH` of your login
  shell: each tab starts `claude` through `$SHELL -lic`. The wheelhouse was built and
  checked against Claude Code 2.1 (2.1.287 to 2.1.296).
- git, to clone the repository.
- make (`sudo apt install make`). Every `make` target is a one-line `uv` command, given
  beside it below, if you'd rather do without.

## Install

In WSL, in your Linux home rather than under `/mnt/c` (a `.venv` on the Windows drive is
slow):

```
git clone https://github.com/agentic-utils/claude-wheelhouse.git
cd claude-wheelhouse
make install
```

`make install` runs `uv sync --reinstall-package claude-wheelhouse` (plain `uv sync` does
for a first install), which builds a `.venv`
inside the clone: the wheelhouse installs nothing outside it (uv keeps its own cache
of downloads, and any Python it fetched). To update, `git pull`,
`make install` again, then quit and reopen the app. Running sessions show "needs
relaunch" when an update changes what they're told.

The `claude-wheelhouse` command lives in that `.venv`, so from the clone run it as
`uv run claude-wheelhouse …` (this README writes it plain). `make` on its own lists the
`make` targets.

Run the app in a Windows Terminal tab, which draws its colours and carries its copies to
the clipboard.

## First run

```
make tutorial
```

`make tutorial` (`uv run claude-wheelhouse tutorial`) opens the wheelhouse and starts a
short demo session in a new Windows Terminal tab that posts a task, two related questions
and a decision; a checklist at the top of the right-hand pane walks you through answering,
sending and ending it. If Claude Code asks whether you trust the demo's folder, say yes in
its tab. It always starts afresh (any earlier tutorial is ended), in its own scratch
directory, `claude-wheelhouse-tutorial/` next to the database. It is a real Claude Code
session on Sonnet 5.5 at high effort, and costs a few cents' worth of tokens on your
Claude Code account. The first time the wheelhouse opens with no sessions it offers the
tutorial in one line: `Enter` takes it, `Esc` dismisses it for good. That line, and the
app's help, name `claude-wheelhouse tutorial (make tutorial in a source checkout)`: in your
clone, that is `make tutorial`.

After that, `make run` (`uv run claude-wheelhouse`) opens the wheelhouse, and `N` starts a
session.

## What it does

- **Inbox.** Every task, question and subagent status from every session in one list,
  open questions first. Pick one to preview its detail and thread; Enter opens it full
  screen as a conversation, with the session's replies. Ctrl+Enter submits an answer, and
  each session has a send mode: in Queued mode (where every session starts) answers wait
  and go out together, Ctrl+S sending the session's as one message; in Immediate mode each
  goes as you submit it. The line under the answer box says which: "Ctrl+Enter to queue"
  or "Ctrl+Enter to send". Ctrl+T switches the session's mode. Under the session list, Mode
  and Send (n) do the same, for the same session, and a bar above the footer
  always shows Send all (n), every running session's queue; each Send is greyed out while its
  queue is empty. Send all's key is `Shift+A`: Windows Terminal sends Ctrl+Shift+S and Ctrl+Alt+S
  as plain Ctrl+S. A dead session's queue waits for it: Ctrl+S on one says to Restore it first,
  its hint leaves Ctrl+S out, and Send all skips it and doesn't count it. Ctrl+R takes a queued answer
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
  code (once you've `/exit`ed it in its tab). It still queues answers as usual,
  unless it is old enough to predate queued answers: then it can't hold them, so
  Ctrl+Enter sends to it straight away, whatever its mode, until it's relaunched.
- **Subagents.** The wheelhouse tracks each session's subagents itself, from the files
  Claude Code writes: an agent item (`A1`) appears, titled
  with the subagent's description, about 15 s after one starts, and turns done or failed
  when it ends (failed, with the reason in its thread, when it errors, you stop it, or its
  session stops while it runs). An agent item the
  session posted itself for the same subagent is taken over rather than listed twice. The
  history doesn't flood in: only subagents started after a session joins the wheelhouse,
  or still running then, get an item, and the same goes for sessions already in it when
  you upgrade to this version. Select an agent item and the right-hand pane shows its
  description, then the subagent's own conversation, read live from its transcript: the
  prompt its session gave it, each tool call as one line, its replies and its result,
  following it at the end as it works unless you've scrolled up to read. `Enter` shows
  the same full screen. If its transcript can't be found, the pane says so.
- **Decisions.** If your own instructions let a session decide some things without asking
  you, it reports each one as a decision (`D1`): what it decided, and one short line with
  the alternative, why, and how to reverse it. Decisions block nothing. Each session
  counts its unseen ones (the `D` column); one becomes seen once it has been in sight for a second without moving on
  (highlighted, or open full screen), whether you highlighted it or the wheelhouse did (at
  startup, or once an item goes), so arrowing past it leaves it unseen, as does a second
  spent in another window, where your terminal reports focus. Answering it
  or closing it counts at once. It stays in the inbox until you close it with `Delete`, as
  with a question. To push back, answer in its thread ("reverse that").
- **Done with, not gone.** A task done or dropped, a question answered, a decision seen
  and a subagent done or failed stay in the inbox, dimmed, below everything still
  needing you, the latest at the foot. One you've selected keeps its place, whatever its
  status does; once you move on it falls to the foot. `Delete` dismisses it (closes a
  question or decision); `F` shows the dismissed and closed ones, and `Ctrl+R` on one
  brings it back (`Delete` only ever closes: on one its session finished while you had it
  selected, it lets it go at once). One you've answered and the session hasn't replied to stays up, as
  processing.
- **Follow a session.** Click a session in the inbox's list (or move to it, or Enter on it): its items
  list gains a pinned first row, 💬 Conversation, highlighted, and drops its session column (it shows
  only while the list has every session's items, after `Esc`), and the right-hand pane
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
  model, effort and window, holds three gauges: context size against its window (green under
  150k, yellow under 300k, amber under 600k, then red), and the account's session and weekly usage with their reset times, as
  `/usage` shows them (see [The usage gauges](#the-usage-gauges)). Under them, whether the prompt cache is warm
  and when it goes cold (once cold, what the next turn re-pays), and its compactions.
  Straight after a compaction a session keeps its pre-compaction size until the next
  response sizes it. Below the panel, two charts of the last two hours, subagents included: how each turn's
  context was assembled (read from cache, new, cache miss) and the output tokens it made.
  Each bar covers a fixed stretch of clock (about 2 minutes at 60 columns), the rightmost
  the current one, and the scale steps 1, 2, 5, 10 so the bars hold still between turns.
  A short pane drops the output chart first, then both. With no session in context, the
  running sessions' totals. The charts' shimmer, and the session list's Cylon eyes, rest while you type in an answer box. The
  colours need 24-bit colour: Windows Terminal draws it but doesn't tell WSL, so the
  wheelhouse assumes it there (`TEXTUAL_COLOR_SYSTEM` overrides that).
  Transcripts are read off the UI thread, so a big one says "reading …" for a moment
  rather than freezing the app. The inbox's session list shows each session's context
  size the same way, as a one-cell bar in the same colours, filled in eighths on a
  linear scale: an eighth for each 125k begun, so the cell is full past 875k.
- **Sessions.** The left-hand column lists every session (parked ones dimmed, at the
  foot of the list's room: they fall there as you park them, and rise back as you unpark
  them). Under it, a description of the highlighted one: its status, ticket, directory
  and counts, and its synopsis, which the session keeps up to date itself (its brief until
  it sets one), in a box that takes half the column.
  The highlighted session is the current one: highlighting an item moves the list's
  highlight to the item's session, and moving the list's highlight follows that session.
  The buttons under the description act on it, and only those that apply show, each with
  a tooltip saying what it does: its lifecycle (Rename, Relaunch and Park on a running
  session, Restore on a dead one, Unpark on a parked one, End on either), then its
  conversation (Mode, and Send on one that isn't dead). New (`N`), Adopt (`A`, a list of the sessions on disk you can adopt) and
  Restore all (`Shift+S`) act on no one session, so they're keys in the footer. Ctrl+S, Ctrl+T and the
  hint under the answer box act on or describe the same session.
  An item opened full screen has Mode, Send and the rest in its bar, for its own session. New session (directory, optional name, optional ticket, opening brief,
  model and effort) opens Claude in a Windows Terminal tab.
  The model is Sonnet 5.5, Opus 5.5, Haiku 5.5 or Fable 5.1 (Sonnet 5.5 unless changed), and the effort
  any level from low to max, starting at the model's default (high; xhigh for Haiku). Every launch of the
  session passes both, Restore and Relaunch included; to change them while it runs, use `/model` and
  `/effort` in its tab.
  Browse… picks the directory from a tree (Enter opens a folder, Backspace goes up,
  Ctrl+Enter or Choose takes the highlighted one). Rename changes a session's name, and
  is the only way to: a `/rename` in Claude Code isn't picked up, and the next launch
  gives Claude Code the wheelhouse's name again.
  Restore brings back sessions that died
  (reboot, crash), one at a time or all at once; selecting a dead (red) session also
  offers to relaunch it, unless it's parked. A running session relaunches once you've
  `/exit`ed it in its tab. Nothing restarts on its own, and a resumed session is asked to
  post the questions and tasks it already had open. Park
  drops a session's items off the inbox until you unpark or restore it. End deletes its wheelhouse data. On a running
  session, Park and End only ask the session to do it (press again to cancel or force);
  the wheelhouse never deletes anything by itself.
  The buttons stay out of the Tab order, and each has a key: with the list focused, `R` renames, `L` relaunches,
  `S` restores, `Shift+S` restores all, `P` parks or unparks and `E` ends; the rest are under Keys below.
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
  followed by how your messages reach it ([`protocol_tab.md`](claude_wheelhouse/protocol_tab.md)) and
  [`protocol_decisions.md`](claude_wheelhouse/protocol_decisions.md), which
  has the session report the choices it makes without asking.
  `WHEELHOUSE_DECISIONS=0` launches sessions without that section.
- [The `/wheelhouse` skill](claude_wheelhouse/plugin/skills/wheelhouse/SKILL.md):
  `/wheelhouse park` and `/wheelhouse end`.
- [The `wheelhouse` MCP server](claude_wheelhouse/mcp_server.py): the tools the session
  posts and reads through. Each tool's description is its docstring.
- [The monitor](claude_wheelhouse/monitor.py) ([plugin entry](claude_wheelhouse/plugin/monitors/monitors.json))
  delivers your answers, the End and Park requests, and the note an adopted session gets
  on joining, as `[wheelhouse]` notifications.

`claude-wheelhouse protocol` prints all of it in one go, exactly as a session receives it,
along with the launch command.

## Using it

Keys, shown in upper case as usual (F means the f key, not Shift+F): `Enter` open an item's thread (or follow a session's conversation, in the
session list), `F` show or hide finished items (the footer says which it will do), `Delete` or `Backspace` in the item list close the
highlighted question or decision or dismiss a done task or subagent, `N` new session, `A` adopt, `Shift+S` restore all, `Esc` all sessions (or clears a text
selection first, or back from a thread), `Ctrl+Enter` submit an answer (queued or sent, by the session's mode),
`Ctrl+S` send the session's queue (not a dead one's: Restore it first), `Ctrl+T` switch the session between Queued and
Immediate, `Ctrl+R` take a queued answer back (with none queued, in the item list, bring back a finished item: a question
as answered, a decision as seen), `?` list every key and button, `Q` quit. Outside a text box, `Shift+A` is Send all (every running session's queue). A key whose button doesn't apply says so.

`Tab` goes round the panes: the session list, the items, the answer box, then the conversation (one
stop, where the arrows, `PgUp`, `PgDn`, `Home` and `End` scroll it), and `Shift+Tab` goes back.
`Ctrl+Enter` in the answer box goes back to the items with the cursor where it was, so answering a run
of items is: type, `Ctrl+Enter`, `Down`, `Tab`, type, `Ctrl+Enter`. An item opened full screen has two
stops, its answer box and its thread. Keys typed ahead of the screen end as if typed slowly: each acts once the one
before it has done all it does, so the ones behind a key that opens a thread or a dialog go to it, and the ones behind a
dialog's answer go to the screen it uncovers.

Closing several questions or decisions at once: in the item list, `Ctrl`+click marks or unmarks a row
and `Shift`+click marks the range from the last one marked; from the keyboard, `Space`
marks or unmarks the highlighted row and `Shift+Up`/`Shift+Down` extend the range (Windows
Terminal keeps `Shift`+click for its own text selection). `Delete` or `Backspace` then closes the marked
questions and decisions and dismisses the done tasks and subagents, `Ctrl+R` brings back the finished ones, `Esc` clears the marks, and a plain click
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

## The daemon

The app goes through the wheelhouse daemon (`.plan/remote.md`): `claude-wheelhouse` starts
it if none is running, or joins the one that is, and the daemon carries on when the app
closes (a Relaunch under way too). It runs once per user, on `127.0.0.1:7428`
(`WHEELHOUSE_PORT` changes the port; `claude-wheelhouse daemon --address` and `--port` run
one by hand, never on `0.0.0.0`), its output in `daemon.log` beside the database. If the
daemon stops under the app, the title bar says so in red and the app starts it again;
nothing you do is sent meanwhile, and what you'd typed stays in the box. An app started on
newer code restarts a daemon running older code from the same checkout.

The daemon and the app share a secret: `daemon.token` beside the database, readable by you
alone, made by the daemon the first time it starts and kept from then on. Every request
carries it, and the daemon refuses one without it. `claude-wheelhouse token` prints it, to
give to another machine, and says so instead if the running daemon refuses it or anyone
else can read or write the file. The daemon
won't start if the file can be read by anyone else (`chmod 600` it), and the app says so if
it joins one already running on such a file. To replace the token, delete the file, stop the daemon (`claude-wheelhouse
daemon --stop`), start it again (open the app), then give the new token to each `--connect`
machine. `claude-wheelhouse --connect host:7428` runs
the app on another machine against a daemon listening on an address that machine reaches
(a VPN's): the token comes from `--token-file`, else `WHEELHOUSE_TOKEN`, else it asks for
it, never from the command line. There it answers and manages the sessions, but starts none:
a tab would open on the daemon's machine. `claude-wheelhouse --local` runs the app with a hub of its
own, without the daemon, as it ran before.

To stop the daemon, quit the app (`Q`) first, since an open app starts it again, then run
`claude-wheelhouse daemon --stop`. It stops the daemon by the pid in `daemon.lock`, with no
token needed, and says what it did ("no wheelhouse daemon is running" if there was none).
Running sessions carry on in their tabs, and the next `claude-wheelhouse` starts a fresh
daemon. To reset the token, stop the daemon and delete `daemon.token`: the next start makes
a new one, which each `--connect` machine then needs.

## The usage gauges

<!-- T85 -->
The session and weekly gauges come from Claude Code itself: every 5 minutes by default,
the wheelhouse runs a short `claude -p "/usage"` and reads what it prints. It reads no
credentials. `--usage-mode persistent`, or `usage-mode=persistent` in a `.env` file
(in the directory you start the app from, the clone, or in
`~/.config/claude-wheelhouse/.env`), keeps one Claude Code running for the gauges instead of starting one each time.

## Where state lives

Everything the wheelhouse keeps is in `~/.local/state/claude-wheelhouse/` (or the directory
of `WHEELHOUSE_DB`, if you set it):

- `wheelhouse.db`, with `wheelhouse.db-wal` and `wheelhouse.db-shm` beside it while it's
  open: sessions, items, answers, settings and pane sizes.
- `daemon.token`, `daemon.lock` and `daemon.log`: the daemon's secret, the lock that says
  one is running (and its pid), and its output.
- `launch.log`: each tab launch, with any error from `cmd.exe` or `wt.exe`.
- `claude-wheelhouse-tutorial/`: the tutorial's scratch directory.

The sessions' conversations stay where Claude Code keeps them, in `~/.claude/projects`,
which the wheelhouse reads.

## Known limits

- Windows, WSL and Windows Terminal only.
- Only sessions launched or adopted from the wheelhouse are tracked.
- Permission prompts, Claude's question dialog, slash commands (`/model` and `/effort`
  included) and plan approval still happen in the session's tab.
- The wheelhouse never closes a tab or stops Claude Code: Relaunch and Adopt of a running
  session wait for you to `/exit` it in its tab.
- A `/rename` in Claude Code isn't picked up: use Rename.
- A session's directory can't contain any of `& | < > ^ % "`, since the tab's command line
  goes through `cmd.exe`.
- The database must be on the Linux filesystem: it refuses a path under `/mnt/`, where
  SQLite's locking is unreliable.
- An app on another machine (`--connect`) answers and manages sessions but can't start
  them.
- The wheelhouse reads files Claude Code keeps for itself and doesn't document (its
  transcripts in `~/.claude/projects`, and `~/.claude/sessions`), so a Claude Code update
  can break the stats, the conversations, subagent items or whether a session shows as
  alive. Only Claude Code 2.1.287 to 2.1.296 have been checked.
- One wheelhouse per user, its daemon on port 7428 (`WHEELHOUSE_PORT` changes it).
- Nothing restarts after a reboot: Restore brings the sessions back.
- This is a 0.1 prototype with no releases yet: update with `git pull` and `make install`.

## Reporting bugs

Open an issue at <https://github.com/agentic-utils/claude-wheelhouse/issues>. Say what you
did, what you expected and what happened, and attach:

- if the app crashed, the traceback it printed in the terminal (a crash in the app goes
  there, not to the logs);
- `launch.log` and `daemon.log`, which sit next to the database: in
  `~/.local/state/claude-wheelhouse/`, or the directory of `WHEELHOUSE_DB` if you set it;
- the output of `uv run claude-wheelhouse protocol`, run in the clone: what your sessions
  are told and the command they launch with, under the settings in force
  (`WHEELHOUSE_DECISIONS`, for one);
- the commit you're on (`git rev-parse --short HEAD` in the clone) and `claude --version`.

The logs hold your directory paths and session ids: look them over before you attach them.

## Uninstall

1. End your sessions: End each one in the wheelhouse, or `/exit` it, then close its tab.
2. Quit the app (`Q`), then in the clone run `uv run claude-wheelhouse daemon --stop`.
3. Delete the clone: `make install` put the wheelhouse in the clone's `.venv`, and nowhere
   else.
4. Delete the state directory: `rm -rf ~/.local/state/claude-wheelhouse`. If you made a
   `~/.config/claude-wheelhouse/.env`, delete that too.
5. Optionally, `uv cache clean` clears uv's download cache (every project's, not only
   this one's).

Nothing was added to your Claude Code settings, so there is nothing to undo there. The
sessions' conversations stay in `~/.claude/projects`, as Claude Code's own.

## Test

```
make test
```

The tests run in parallel, a worker a core (pytest-xdist). `uv run pytest -n 0` runs them
serially, as a single test or a debugger wants.

To work on the wheelhouse, read [AGENTS.md](AGENTS.md) first. From inside a wheelhouse
session, `unset WHEELHOUSE_DB WHEELHOUSE_SESSION_ID WHEELHOUSE_TOKEN` before running the
tests: they refuse to start while they would reach your own wheelhouse's state.

## Licence

MIT, see [LICENSE](LICENSE).
