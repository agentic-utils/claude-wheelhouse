# claude-wheelhouse

> claude-wheelhouse is an independent open-source productivity tool. It is not
> affiliated with, endorsed by or supported by Anthropic. "Claude" is a trademark of
> Anthropic.

A sidecar for running several Claude Code sessions in parallel, on Windows with WSL and
Windows Terminal. Prototype: see `.plan/wheelhouse-sessions.md` for the design.

## Requirements

- Windows with WSL and Windows Terminal. The wheelhouse runs inside WSL and opens each
  session in a new Windows Terminal tab (through `cmd.exe` and `wt.exe`), all in a window
  of their own named `wheelhouse-<your WSL user>`, which the first launch opens.
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
for a first install), which builds a `.venv` inside the clone: the wheelhouse installs
nothing outside it (uv keeps its own cache of downloads, and any Python it fetched). To
update, `git pull`, `make install` again, then quit and reopen the app. Running sessions
show "needs relaunch" when an update changes what they're told.

The `claude-wheelhouse` command lives in that `.venv`, so from the clone run it as
`uv run claude-wheelhouse …` (this README writes it plain). `make` on its own lists the
`make` targets.

Run the app in a Windows Terminal tab, which draws its colours and carries its copies to
the clipboard.

## First run

```
make run
```

The first time the wheelhouse opens with no sessions it offers the tutorial in a line at
the top (wrapped on a narrow screen): `Enter` takes it, `Esc` dismisses it. Taking it starts a short demo session in a new
Windows Terminal tab that posts a task, two related questions and a decision; a checklist
at the top of the right-hand pane walks you through answering, sending, watching it work in
its conversation, ending it and starting a session of your own (`N`): the checklist stays
for that last step once the demo has ended, until you quit the app. If
Claude Code asks whether you trust the demo's folder, say yes in its tab. It always starts
afresh (any earlier tutorial is ended), in its own scratch directory,
`claude-wheelhouse-tutorial/` next to the database. It is a real Claude Code session on
Sonnet 5.5 at high effort, and costs a few cents' worth of tokens on your Claude Code
account. The tutorial's session is whichever one runs in that directory, so don't start a
session of your own there: starting the tutorial, or `make tutorial`, would end yours too,
items and all.

To be offered it again, quit the app, then:

```
make stop
make tutorial
make run
```

`make tutorial` (`uv run claude-wheelhouse tutorial`) asks first (`--yes` skips the
question), then ends the earlier tutorial session, if there is one, and has the next start
offer the tutorial even though you have other sessions. Your other sessions are kept (but
not one started in the tutorial's directory, as above). It refuses while the daemon runs,
since the app decides on the offer only as it starts. An app started with `--local` runs no
daemon, so it can't be detected: quit it first, or `make tutorial` ends the tutorial
session while that app still shows it (its Claude Code is stopped and its row removed), and
the offer shows only when the app next starts. The offer, and the app's help, say to run
`claude-wheelhouse tutorial (make tutorial in a source checkout)` with the wheelhouse
stopped: in your clone, that is `make tutorial`.

After that, `make run` (`uv run claude-wheelhouse`) opens the wheelhouse, and `N` starts a
session.

## What it does

- **Inbox.** Every task, question and subagent status from every session in one list,
  open questions first. A task is todo, running, blocked, waiting, done or dropped, as its
  session sets it. Pick one to preview its detail and thread; Enter opens it full
  screen as a conversation, with the session's replies. Ctrl+Enter submits an answer, and
  each session has a send mode: in Queued mode (where every session starts) answers wait
  and go out together, Ctrl+S sending the session's as one message; in Immediate mode each
  goes as you submit it. The line under the answer box says which: "Ctrl+Enter to queue"
  or "Ctrl+Enter to send". Ctrl+T switches the session's mode. Under the session list, Mode
  and Send (n) do the same, for the same session, and a bar above the footer
  always shows Send all (n), every running session's queue; each Send is greyed out while its
  queue is empty. Send all's key is `Shift+A`: Windows Terminal sends Ctrl+Shift+S and Ctrl+Alt+S
  as plain Ctrl+S. A dead session's queue waits for it: Ctrl+S on one says to Relaunch it first,
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
  context was assembled (read from cache, new, cache miss) and the output tokens it made,
  each with its total over the last hour at the right of its caption (`340k tokens/last hr`).
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
  it sets one), in a box that takes half the column. The box's last two rows always show
  what a session run in the wheelhouse is doing now, cropped to fit, so nothing above
  them moves as that changes. The dot before each name, and the
  status in the description, are green when it's live, cyan while it starts or relaunches,
  yellow when stalled (its Claude Code runs, but its wheelhouse MCP server hasn't checked in
  for 2 minutes), magenta while it's been asked to park or forget, red when dead (its Claude
  Code isn't running) and grey when parked.
  The highlighted session is the current one: highlighting an item moves the list's
  highlight to the item's session, and moving the list's highlight follows that session.
  The buttons under the description act on it, and only those that apply show, each with
  a tooltip saying what it does: its lifecycle (Rename on a running session, Relaunch
  (Unpark on a parked one) and Forget (Stop & Forget on a running one) on any, and Park on one
  that isn't parked), then its
  conversation (Mode, and Send on one that isn't dead). New (`N`), Adopt (`A`, a list of the sessions on disk you can adopt) and
  Relaunch all (`Shift+L`) act on no one session, so they're keys in the footer. Ctrl+S, Ctrl+T and the
  hint under the answer box act on or describe the same session.
  An item opened full screen has Mode, Send and the rest in its bar, for its own session. New session (directory, optional name, optional ticket, opening brief,
  model and effort) opens Claude in a Windows Terminal tab.
  The model is Sonnet 5.5, Opus 5.5, Haiku 5.5 or Fable 5.1 (Sonnet 5.5 unless changed), and the effort
  any level from low to max, starting at the model's default (high; xhigh for Haiku). Every launch of the
  session passes both, Relaunch included; to change them while it runs, use `/model` and
  `/effort` in its tab.
  Browse… picks the directory from a tree (Enter opens a folder, Backspace goes up,
  Ctrl+Enter or Choose takes the highlighted one). Rename changes a session's name, and
  is the only way to: a `/rename` in Claude Code isn't picked up, and the next launch
  gives Claude Code the wheelhouse's name again.
  A session is in one of four states. Active: running (starting, stalled, busy and the rest
  only decorate it). Available: not running and not parked, shown red, as after a reboot or a
  crash. Parked: not running, by intent, dimmed at the foot of the list. Forgotten: no longer
  in the wheelhouse at all, which Adopt can bring back. Relaunch starts an Available or Parked
  session again where it left off; on a running one it stops it (a tab's Claude Code or a
  host) and starts it again, asking first only if it's mid-turn. Relaunch all (`Shift+L`)
  brings back every Available session, and selecting an Available session also offers to
  relaunch it. Nothing restarts on its own, and a resumed session is asked to post the
  questions and tasks it already had open. Park on a running session asks it to write its
  handoff notes into its items, then it parks and stops (its tab closes); on one not running
  it parks at once. On a Parked session, Relaunch is captioned Unpark, and `P` and `L` both
  press it. Forget deletes a session's wheelhouse data: on a running one, Stop & Forget asks it
  to do its own end steps first, then it stops (it asks you first only if answers are queued for
  it, which go with it); on one not running it asks you to confirm. A
  pending Park or Stop & Forget is pressed again to choose: `C` cancels the request, `F` forces
  it (the session is stopped at once, without its steps), `Esc` leaves it be. In the session's
  tab, `/wheelhouse park` and `/wheelhouse end` do the same from there. A tab closes once its
  session stops, by `/exit` or by the wheelhouse: its shell then exits cleanly, which Windows
  Terminal's default `closeOnExit` takes as the end of the tab. If Claude Code fails instead,
  the tab stays open on its error. If the wheelhouse can't start
  a session in its tab, the tab says why and waits for Enter. The wheelhouse never deletes
  anything by itself.
  The buttons stay out of the Tab order, and each has a key: with the list focused, `R` renames, `L` relaunches
  (or unparks), `P` parks (or unparks) and `Delete` or `Backspace` forgets; the rest are under Keys below. `E`, `S`
  and `Shift+S` (End, Restore and Restore all before) do nothing for now.
- **Adopt.** Brings a session the wheelhouse didn't launch into the wheelhouse: pick it
  from the recent sessions in `~/.claude/projects` that the wheelhouse has no record of
  (never known, or forgotten). If it's still running, type `/exit` in
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
  `WHEELHOUSE_DECISIONS=0`, set where the daemon starts, launches sessions without that
  section (the daemon reads it as it starts, so restart it after a change).
- [The `/wheelhouse` skill](claude_wheelhouse/plugin/skills/wheelhouse/SKILL.md):
  `/wheelhouse park` and `/wheelhouse end`.
- [The `wheelhouse` MCP server](claude_wheelhouse/mcp_server.py): the tools the session
  posts and reads through. Each tool's description is its docstring.
- [The monitor](claude_wheelhouse/monitor.py) ([plugin entry](claude_wheelhouse/plugin/monitors/monitors.json))
  delivers your answers, the Park and Stop & Forget requests, the note an adopted session gets
  on joining, and the keepalive and check messages, as `[wheelhouse]` notifications.
- [The plugin's hook](claude_wheelhouse/plugin/hooks/hooks.json): after a compaction,
  the session's open tasks and questions.

`claude-wheelhouse protocol` prints all of it in one go, exactly as a session receives it,
along with the launch command.

## Using it

Keys, shown in upper case as usual (F means the f key, not Shift+F): `Enter` open an item's thread (or follow a session's conversation, in the
session list), `F` show or hide finished items (the footer says which it will do), `Delete` or `Backspace` in the item list close the
highlighted question or decision or dismiss a done task or subagent, `N` new session, `A` adopt, `Shift+L` relaunch all, `Esc` all sessions (or clears a text
selection first, or back from a thread), `Ctrl+Enter` submit an answer (queued or sent, by the session's mode),
`Ctrl+S` send the session's queue (not a dead one's: Relaunch it first), `Ctrl+T` switch the session between Queued and
Immediate, `Ctrl+R` take a queued answer back (with none queued, in the item list, bring back a finished item: a question
as answered, a decision as seen), `?` list every key and button, `Q` quit. Outside a text box, `Shift+A` is Send all (every running session's queue). A key whose button doesn't apply says so.
A dialog asking yes or no takes `Y` or `N` (`Esc` is no, and so is `Enter`, as No has the focus).

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
(`claude-wheelhouse daemon --address` runs one by hand on another address, never on
`0.0.0.0`), its output in `daemon.log` beside the database. If the
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
a tab would open on the daemon's machine. The daemon accepts only requests addressed to its
own address, so if the other machine reaches it by a name (the VPN's), give that too:

    claude-wheelhouse daemon --stop
    claude-wheelhouse daemon --address 100.64.0.5 --name mybox.tailnet.ts.net

(`--name` repeats for more names; quit the app first, as below, and start that daemon before
the app.) On the other machine, `claude-wheelhouse --connect mybox.tailnet.ts.net:7428`.

`claude-wheelhouse --local` runs the app with a hub of its own, without the daemon, as it
ran before. Don't run it while the daemon runs: two hubs would act on the same sessions (the
app warns if you do).

To stop the daemon, quit the app (`Q`) first, since an open app starts it again, then run
`make stop` in the clone (`claude-wheelhouse daemon --stop`). It stops the daemon by the pid in `daemon.lock`, with no
token needed, and says what it did ("no wheelhouse daemon is running" if there was none).
Running sessions carry on in their tabs, and the next `claude-wheelhouse` starts a fresh
daemon.

The port is 7428 unless you choose another: `--port` (on the app or `claude-wheelhouse
daemon`), else `WHEELHOUSE_PORT`, else `PORT=` in a `.env` file, in that order (see
[Settings](#settings)). It must be from
1024 to 65535; anything else stops the app or daemon with a line naming where it was set.
The app starts its daemon on that port. If the daemon is already running on another, the
app joins it where it is and says so; stop it to move it. If another process holds the
port, the app (or the daemon started by hand) says:

    port 7428 is taken by another process (another user's wheelhouse?): set PORT= in ~/.config/claude-wheelhouse/.env

### Two wheelhouses on one machine

Each WSL user has a wheelhouse of their own: everything it keeps is under their home, and
their tabs open in their own Windows Terminal window (`wheelhouse-doug`,
`wheelhouse-tfu`). Only the port is shared, so give every user after the first another.
For a second user `tfu`, as `tfu`:

    mkdir -p ~/.config/claude-wheelhouse
    echo PORT=7429 >> ~/.config/claude-wheelhouse/.env

Then start each user's app as usual. A daemon started by hand reads the same file.

## The usage gauges

The session and weekly gauges come from Claude Code itself. By default (`oneshot`) the
wheelhouse runs a short `claude -p "/usage"` every 5 minutes in the background and reads
what it prints. That spends no tokens, and the wheelhouse never reads your Claude Code
login. When a reading is late, the gauges say how old their numbers are.

`--usage-mode persistent` keeps one Claude Code running and asks it instead: quicker per
reading, but it stays in memory. `WHEELHOUSE_USAGE_MODE=persistent`, or `USAGE_MODE=persistent` in a `.env` file, does the
same (see [Settings](#settings)). The daemon does the asking, so a change takes effect
when the daemon restarts (quit the app, `claude-wheelhouse daemon --stop`, then open the
app again). If the app is given a mode and joins a daemon already running the other, it
says so once.

## Keeping the item list in step

A session can move on to new work and leave an item it finished showing as still open.
The wheelhouse names the items it doubts, and the session corrects any that are wrong with
its ordinary `update_item` and `reply` calls. An item is in doubt when it is a task
running, blocked or waiting, or a question whose latest word is yours (`reply owed`), and
the session has said nothing on it for 15 minutes. Nothing ever closes by itself.

- When a session finishes a task, drops one, answers a question or posts a new task or
  question, the tool's reply lists what is in doubt, as `Still open: T1 waiting, Q2 reply
  owed`. Nothing is added when nothing is in doubt.
- While you're away, a session over 150k tokens of context that waits on you or has an
  item in doubt gets a `[wheelhouse]` message 55 minutes after its last turn, and every 55
  minutes after that. It keeps the session's prompt cache warm (a cold one is written again
  whole), and with an item in doubt it is the check, `Check: T1 running, Q2 reply owed`,
  which the session answers by correcting what's finished. It stops once the session has
  been quiet 8 hours. `--keepalive-hours N` (on the app or `claude-wheelhouse daemon`), or
  `WHEELHOUSE_KEEPALIVE_HOURS=N`, or `KEEPALIVE_HOURS=N` in a `.env` (see
  [Settings](#settings)), sets another limit; fractions are allowed and 0
  turns it off. Each message costs one request, which reads the cached context: for a
  quiet session that is much cheaper than writing the cache again when you come back.
- After a compaction, the session is told its open tasks and questions, with their titles,
  once. The plugin's `SessionStart` hook does it
  ([`hooks.json`](claude_wheelhouse/plugin/hooks/hooks.json)): it runs
  `"${WHEELHOUSE_PYTHON}" -m claude_wheelhouse open-items` with the wheelhouse's own Python,
  as the monitor is run, and Claude Code adds what it prints to the session's context.
- The check and the still-open lists only start once a session runs this version: one
  started on older code shows as needing a relaunch, and gets neither until relaunched.

## Settings

Each setting can be given four ways. The first found wins: the flag (on the app, or on
`claude-wheelhouse daemon` for the daemon's own), then the environment variable, then the
key in a `.env` file, then the default. Two `.env` files are read, in this order: the one
in the wheelhouse's install directory (the clone, whatever directory you start from), then
`~/.config/claude-wheelhouse/.env`. Keys in a `.env` are upper case and flags lower case;
a key spelt any other way isn't read.

| `.env` key | Environment variable | Flag | Default |
|---|---|---|---|
| `PORT` | `WHEELHOUSE_PORT` | `--port N` | 7428 |
| `USAGE_MODE` | `WHEELHOUSE_USAGE_MODE` | `--usage-mode oneshot\|persistent` | `oneshot` |
| `KEEPALIVE_HOURS` | `WHEELHOUSE_KEEPALIVE_HOURS` | `--keepalive-hours N` | 8 (0 turns it off) |
| `ANIMATION_FPS` | `WHEELHOUSE_ANIMATION_FPS` | `--animation-fps N` | 10 (0 turns animation off) |

`ANIMATION_FPS` sets how many frames a second the title's shimmer, the session eyes and
the stats bars move; fractions are allowed, up to 30. Every rate moves them at the same
speed: a lower one draws fewer steps. While the terminal window is unfocused and no
session is busy, the animation rests, and starts again when you come back or a session
gets busy. The daemon reads only the settings it uses: `PORT`, `USAGE_MODE` and
`KEEPALIVE_HOURS`.

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
  alive. The usage gauges parse the wording `/usage` prints, so a change to it can break
  them too. Only Claude Code 2.1.287 to 2.1.296 have been checked.
- Each user's daemon listens on `127.0.0.1:7428`: a second WSL user on the same machine
  needs another port (see "Two wheelhouses on one machine").
- Nothing restarts after a reboot: Relaunch all (`Shift+L`) brings the sessions back.
- This is a 0.1 prototype with no releases yet: update with `git pull` and `make install`.

## Reporting bugs

Open an issue at <https://github.com/agentic-utils/claude-wheelhouse/issues>. Say what you
did, what you expected and what happened, and attach:

- if the app crashed, the traceback it printed in the terminal (a crash in the app goes
  there, not to the logs);
- `launch.log` and `daemon.log`, which sit next to the database: in
  `~/.local/state/claude-wheelhouse/`, or the directory of `WHEELHOUSE_DB` if you set it;
- the output of `uv run claude-wheelhouse protocol`, run in the clone: what your sessions
  are told and the command they launch with, under the settings in force in the shell you
  run it from (`WHEELHOUSE_DECISIONS`, for one, which sessions take from the daemon's);
- the commit you're on (`git rev-parse --short HEAD` in the clone) and `claude --version`.

The logs hold your directory paths and session ids: look them over before you attach them.

## Uninstall

1. End your sessions: Stop & Forget each one in the wheelhouse (its tab closes), or `/exit`
   it in its tab and close the tab.
2. Quit the app (`Q`), then in the clone run `make stop`
   (`uv run claude-wheelhouse daemon --stop`).
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

(`make test` is `uv run pytest`.)

The tests run in parallel, a worker a core (pytest-xdist). `uv run pytest -n 0` runs them
serially, as a single test or a debugger wants.

To work on the wheelhouse, read [AGENTS.md](AGENTS.md) first. From inside a wheelhouse
session, `unset WHEELHOUSE_DB WHEELHOUSE_SESSION_ID WHEELHOUSE_TOKEN` before running the
tests: they refuse to start while they would reach your own wheelhouse's state.

## Licence

MIT, see [LICENSE](LICENSE).
