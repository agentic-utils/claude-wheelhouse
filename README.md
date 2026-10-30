# claude-wheelhouse

> claude-wheelhouse is an independent open-source productivity tool. It is not
> affiliated with, endorsed by or supported by Anthropic. "Claude" is a trademark of
> Anthropic.

A sidecar for running several Claude Code sessions in parallel, on Windows with WSL and
Windows Terminal, or on macOS with iTerm. Prototype: see `.plan/wheelhouse-sessions.md`
for the design.

## Requirements

- Windows with WSL and Windows Terminal. The wheelhouse runs inside WSL and opens each
  session in a new Windows Terminal tab (through `cmd.exe` and `wt.exe`), all in a window
  of their own named `wheelhouse-<your WSL user>`, which the first launch opens.
- Or macOS with [iTerm](https://iterm2.com/). Each session opens in a new tab of iTerm's
  front window (a new window if none is open), and a right-click paste reads the clipboard
  through `pbpaste`. The first launch asks to let your terminal control iTerm: allow it.
- [uv](https://docs.astral.sh/uv/), which installs everything else, Python included.
  `make install` installs it if it's missing, with Astral's installer, into `~/.local/bin`.
- Python 3.12 or later (`requires-python` in `pyproject.toml`). uv fetches 3.12 if your
  system doesn't have it.
- Claude Code, installed in WSL (or on the Mac) and logged in, with `claude` on the
  `PATH` of your login shell: each tab starts `claude` through `$SHELL -lic`. The
  wheelhouse was built and checked against Claude Code 2.1 (2.1.287 to 2.1.296).
- git, to clone the repository.
- make (`sudo apt install make`; on macOS, the Xcode command line tools'). Every `make`
  target is a one-line `uv` command, given beside it below, if you'd rather do without.
- curl, for `make install` to fetch uv if you don't have it.

## Install

In WSL, in your Linux home rather than under `/mnt/c` (a `.venv` on the Windows drive is
slow):

```
git clone https://github.com/agentic-utils/claude-wheelhouse.git
cd claude-wheelhouse
make install
```

`make install` installs uv first if it isn't on your `PATH` or in `~/.local/bin` (Astral's
installer, `curl -LsSf https://astral.sh/uv/install.sh | sh`, which puts it in
`~/.local/bin`: open a new shell to have it on your `PATH`). It then runs
`uv sync --reinstall-package claude-wheelhouse` (plain `uv sync` does for a first install),
with the modules in `modules/` (today, the stats, Decisions and PRs panes), which builds a `.venv` inside the clone: the wheelhouse installs
nothing outside it (uv keeps its own cache of downloads, and any Python it fetched). To
update, `git pull`, `make install` again, then quit and reopen the app straight away
(with `--local` too, and on every machine that opens this database): a session relaunched
on the new code can post items the old app can't show, such as docs, and an old app with
its hub in process breaks on them until it is restarted. Running sessions show "needs
relaunch" when an update changes what they're told.

The `claude-wheelhouse` command lives in that `.venv`, so from the clone run it as
`uv run claude-wheelhouse …` (this README writes it plain). `make` on its own lists the
`make` targets, and `make help args` (or `make args`) the app's command line args, each with
its environment variable and `.env` key where it has them (see [Settings](#settings)).
`make run` passes `ARGS` to the app, for example `make run ARGS="--local --animation-fps 0"`,
and runs `make install` first when the clone has none, or `pyproject.toml` or `uv.lock`
changed since. Without uv, every other target stops with `uv not found: run make install`.

The stats pane, the Decisions pane and the PRs pane are modules, each its own package
(`modules/stats`, `modules/design`, `modules/prs`), which `make install` installs with the wheelhouse. To leave them out,
`make install WITH=""`: the wheelhouse runs without them, and the space each took goes to
the panes beside it. `WITH` names the modules to install, `WITH="stats"` for the stats pane
alone. Leaving a module out
is a `make` choice: every `make` target (`run`, `test`, `stop` and the rest) keeps to what
the last `make install` installed, and a plain `make install` installs them all again. A
plain `uv run` (`uv run claude-wheelhouse`, say) installs every module back.

`claude-wheelhouse modules` lists the modules installed, with their version, the module API
they were written for and whether each is on. `claude-wheelhouse modules disable <id>`
switches one off without uninstalling it, and `enable <id>` back on: the daemon and the app
read this as they start, so quit the app and `make stop` for it to take.

Run the app in a Windows Terminal tab, which draws its colours and carries its copies to
the clipboard.

## First run

```
make run
```

The first time the wheelhouse opens with no sessions it invites you to the tutorial, in a
dialog in the middle of the screen: `Enter` (Start) takes it, `Alt+N` (Never) turns the invite
off, and `Alt+L` (Later), `Esc` or a click outside it leaves it to be offered again at the next
start. The tour opens with what the wheelhouse is for (every session in one place, their
questions, decisions and tasks in one inbox, a row that shows when a session needs you),
then paints a box beside each pane in turn, its border highlighted, saying what it is and
what to do there, with everything else on the screen dimmed. A box that waits on you says
what to do now, and has a button that does it for you, captioned with the action and how to
do it yourself ("Send (Ctrl+S)", "Read D1 (Click)"), off until it can act. One that only
explains has a Next button; one that waits for the demo (starting, or replying) has a Next
that stays off until it's done. It starts with the demo session: press `Alt+N` and New session opens filled
in (its brief begins with a note in capitals saying it's the tutorial's), so just press
Launch, and the demo is selected in the session list. The demo writes a short children's
bedtime story with you: it posts a task, two related questions (the main characters and the
setting) and a decision, and starts one small subagent to suggest titles, whose A item a box
points out once the demo has replied (its Next is always on, so a subagent that failed never
holds the tour up). Each box moves on as you do what it asks: answering (once one
answer is queued, the other question is selected for you), sending, reading its reply,
reading the decision, then closing it (that box keeps clear of the decision in Detail),
opening the story it writes as a doc (rendered), opening its conversation and asking it for
a change, Stats, the Decisions pane's ADRs (architecture
decision records, from each repo's `docs/adr`) while the design module is loaded, and ending
it. A box beside Detail never covers the demo's rows in the inbox: it sits below every row, or
below the demo's, or at the foot of the screen. On a small screen, where a box at its usual
width would cover them or be cut short, it widens to nearly the screen's width, which makes
it shorter.
The last dialog shows this release's notes. `Esc` ends the tour at any time, from any of its
boxes or the New session it opens, once you confirm (`Enter` leaves, `Esc` stays), for good: the demo keeps running until you Stop & Forget
it, and `claude-wheelhouse tutorial` offers the tour again from the start. Quitting mid-tour
keeps your place: the title bar's badge then reads "Tour paused · Alt+T", and `Alt+T` (or a click on
the badge) resumes it at the same box.

After an update that brings something new to a pane, the next start offers a top-up in the
same centred dialog ("Updated to 0.3.0: 3 new things in 2 places"): only the new tips (at most
three a box: the rest are in the release notes), in a
box beside each pane they concern, every one moved on with `Enter` (Next), skipping panes
with nothing new, then the notes of every release since the last ones you saw. It launches
no demo session and costs no tokens. Later leaves the badge ("Tour: 3 new · Alt+T") and offers
it again at the next start; Never skips those features, so only later ones are offered. An
update whose features are all for experienced users shows no dialog, only a "What's new · Alt+T"
badge that opens the release notes. The demo runs as a new session
does: hosted in the wheelhouse with SDK mode on, otherwise in a Windows Terminal tab. If
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

After that, `make run` (`uv run claude-wheelhouse`) opens the wheelhouse, and `Alt+N` starts a
session.

## What it does

- **Inbox.** Every task, question and subagent status from every session in one list,
  open questions first. A task is todo, running, blocked, waiting, done or dropped, as its
  session sets it. Pick one to preview its detail and thread; Enter opens it full
  screen as a conversation, with the session's replies. Ctrl+Enter submits an answer, and
  each session has a send mode: in Queued mode (where every session starts) answers wait
  and go out together, Ctrl+S sending the session's as one message; in Immediate mode each
  goes as you submit it. The line under the answer box says which: "Ctrl+Enter: queue"
  or "Ctrl+Enter: send". Alt+S sends just the answer in the box at once, whatever the
  mode, leaving the mode and the rest of the queue as they are. Ctrl+T switches the
  session's mode. Under the session list, Mode and Send (n) do the same, for the
  same session, and a bar above the footer
  always shows Send all (n), every running session's queue; each Send is greyed out while its
  queue is empty. Send all's key is `Alt+Shift+A`: Windows Terminal sends Ctrl+Shift+S and Ctrl+Alt+S
  as plain Ctrl+S. On a session that isn't running, Ctrl+S or Alt+S asks "Unpark <session> and
  send?" (a parked one) or "Relaunch <session> and send?" (any other): Yes brings it back with its
  queue, and Alt+S's answer after it, as its first prompt, behind what the wheelhouse tells a
  session on resume; No sends nothing and leaves the box as it was. With nothing queued, Ctrl+S
  says so. Send all skips it and doesn't count it. Ctrl+R takes a queued answer
  back to edit or drop. A sent answer shows as `you · sending`, as a queued one shows as
  `you · queued`, until the session takes it up: in an item's thread until the wheelhouse
  has passed it on, and in the conversation until the session has it, which may wait for
  its turn to end. Emoji codes work as in chat apps: `:tada:` turns into 🎉, and while
  you type `:gri` the hint line suggests matches: click one to take it, or choose with Up
  and Down (the first is chosen to start with) and take it with Tab or Enter. Text you
  haven't sent stays with the item (or session) you typed it for: moving to another clears the box, and coming back restores it. Each session shows
  its queued count in the ✉ column. Sending doesn't change a question's status: the session's
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
  the same full screen. If its transcript can't be found, the pane says so. An agent item
  sits under the task, question or decision whose ref its description (or else its prompt)
  names first (`└ A1`), moving with it; the protocol asks sessions to name it. Failing
  that, under the one its session last posted, updated or replied on before the subagent
  started; if the session had said nothing on any, under the session's name at the foot of
  the list, as it did before. An
  item with children has a `-` on a grey background at the right-hand end of its row:
  click it to hide them, and the `+` it turns into to show them again (for as long as the
  app runs). `Delete` on an item with children asks first, saying how many it takes and
  how many of them are still open or unread, and how many running ones stay, then closes
  or dismisses it and deletes the rest, at any depth. They stay deleted, even when a
  subagent finishes; `Ctrl+R` with `Alt+V` showing brings one back. A pinned child is never
  deleted or counted: it stays, at its session's level.
  A running child (a subagent still at work, say) is never deleted: it stays in the list
  with no parent, at the top level, and stays there if `Ctrl+R` brings its parent back.
- **Docs.** A session declares each markdown document it writes for you to read (a design,
  plan, report or review, wherever it lives, `~/.claude` included) as a doc item (`M1`),
  under the task, question or decision it belongs to, or under the session's name if none
  (or if that item has been deleted). The session leaves out its scratch and working
  notes. A doc shows `new` until it has been in sight for a second, then `read`; when the
  session changes it and posts it again it shows `updated`, never new again, and replies
  you haven't seen show as `2 new`. The file is read again whenever it changes, and if it
  has gone (or isn't a regular file) the pane says so.
- **Pull requests.** A PR a session makes with `gh pr create` (or the GitHub MCP server),
  itself or through a subagent, shows in the inbox by itself, found in its transcript: its
  ref is `PR` and the repo's PR number (`PR108`), and its title starts with the repo's
  short name, dimmed. A subagent's PR sits under the task the subagent was made for. A
  session posts one it made another way with `post_item(kind="pr", url=...)`. Its status
  is GitHub's (`draft`, `open`, `merged`, `closed`), which the PRs pane brings in; merged
  and closed ones dim. `Delete` takes one off the inbox, asking first while it's open: it
  stays open on GitHub. A message on a PR goes to its session, as on any item.
- **Colour by kind.** Each inbox row's ref is coloured by its kind: a task green, a
  question yellow, a decision purple, a PR light blue, a doc the theme's text colour, a
  subagent grey and a permission orange. Resting the pointer on a row shows its full title
  (a PR's with its repo), and on a session its full name and synopsis.
- **Since you were away.** Back after a gap (two hours with no key press, click or scroll in
  the app, `AWAY_HOURS`; starting the app counts, measured from its last input), a card at
  the top of the inbox says what happened in every session meanwhile: **Needs you** (open
  questions, approval prompts, tasks blocked or waiting on you), **Landed** (PRs merged,
  tasks done), **Went quiet** (sessions that parked, ended or stopped and haven't come back)
  and **Decided without you** (a count, then the titles). Each group shows three lines, then
  `+N more`; an empty group is left out, and with nothing to say there's no card. `Enter` or
  a click on a line jumps to its item or session as an inbox row does. `Esc` in the card, or
  its `✕`, dismisses it, and it goes by itself once every Needs-you line is dealt with.
  Seeing it marks nothing seen. A session going quiet is noticed by the daemon (or a
  `--local` app) as it happens; one that went while neither ran is noticed at the next start.
- **Pins.** The inbox's first column pins an item (📌): a click there, or `Alt+P` in the item
  list, pins the row or clears its pin. A doc comes pinned (a session can post a scratch
  report unpinned), anything else unpinned, and a doc posted again keeps the pin you set.
  Clearing the inbox never takes a pinned item: `Ctrl+A` never marks one, `Ctrl+Delete` and
  autocleanse leave it, a `Delete` on
  marked rows leaves them (and says so: `1 pinned kept`), a parent's `Delete` leaves its
  pinned children, and `Alt+V` never hides one. `Delete` on a pinned item asks first ("Delete
  pinned M2? The file stays on disk.", No by default), saying so too if a comment of
  yours on it awaits a reply; Yes clears its pin and deletes it.
- **A doc's thread.** Select a doc and Detail shows it with its thread: **split** (the
  default), the doc above and its thread and the answer box below, the line between them
  kept as `layout.doc-thread` (with no comment yet, the box alone, so the doc has the
  room); or **tabbed**, Doc and Thread (with its count of new replies) as tabs, switched
  by `Ctrl+PageUp` and `Ctrl+PageDown` or a click, where typing on the Doc tab moves you
  to Thread with what you typed. `Ctrl+L` switches between the two, kept as
  `layout.doc-view`; `Enter`, or a double-click on the doc, opens it full screen, laid out the same way. A comment
  goes to the session that posted the doc, on its ref, queued or sent at once by the
  session's mode, as an answer does. After a click on a block of the doc, the comment
  quotes that block's first line (shown in the box's title); `Esc` in the empty box drops
  the quote. The session replies in the thread; when it revises the doc and posts it
  again, the doc reloads where you were reading.
- **Editing a doc.** A click on a doc shown, an item's or a decision, in Detail or full
  screen, opens an editor below it (a doc item's in place of its thread and the answer
  box, or as a third tab, Editor, when tabbed), its cursor on the first line of the block clicked
  (dragging the line between them resizes it, kept as `layout.editor`). `Ctrl+Enter`
  shows the edits rendered above, as an answer queues; nothing re-renders as you type.
  The two keep step: scrolling the doc scrolls the editor to the same place, and moving
  the cursor into another block scrolls the doc to show it. `Esc` closes the editor,
  asking first if there are edits (Close keeping them, the default; Discard; or Keep
  editing), and the pane's title says `edited` while there are. Edits not discarded are kept, doc by doc, while the app
  runs: another doc shown closes the editor, and opening it on the first again brings them
  back, a click putting the cursor on its block; until then that doc shows them rendered,
  full screen too. A file changed on disk meanwhile keeps the edits, said once and as
  `file changed` in the title. One file has one buffer, however it's reached. Saving
  comes next: for now `Ctrl+S` (and `Ctrl+Enter` for a session in Immediate mode) says so,
  and the file is never written.
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
  question or decision); `Alt+V` shows the dismissed and closed ones, and `Ctrl+R` on one
  brings it back (`Delete` only ever closes: on one its session finished while you had it
  selected, it lets it go at once). One you've answered and the session hasn't replied to stays up, as
  processing.
- **Follow a session.** Click a session in the inbox's list (or move to it, or Enter on it): its items
  list gains a pinned first row, 💬 Conversation, highlighted, and drops its session column (it shows
  only while the list has every session's items, after `Esc` or a click on the selected session, which
  unselects it; there every unparked session has its 💬 row, named after it, in the session list's order: at the
  top under the inbox's own order (where the highlight starts), under the active items under another sort,
  or at the bottom with ordering off), and the right-hand pane
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
- **Stats.** Under the session list, the session in context's numbers, read from its
  transcripts, in claude-dashboard's colours. A bordered panel, titled with the session,
  model, effort and window, holds three gauges: context size against its window (green under
  150k, yellow under 300k, amber under 600k, then red), and the account's session and weekly usage with their reset times, as
  `/usage` shows them (see [The usage gauges](#the-usage-gauges)). Under them, whether the prompt cache is warm
  and when it goes cold (once cold, what the next turn re-pays), and its compactions.
  Straight after a compaction a session keeps its pre-compaction size until the next
  response sizes it. Below the panel, two charts of the last two hours, subagents included: how each turn's
  context was assembled (read from cache, new, cache miss) and the output tokens it made,
  each with its total over the span shown at the right of its caption (`340k tokens/last 2h`,
  shortened to `340k/2h` or left out in a narrow pane, so the caption never wraps).
  Each bar covers a fixed stretch of clock (about 2 minutes at 60 columns), the rightmost
  the current one, and the scale steps 1, 2, 5, 10 so the bars hold still between turns.
  The `+` and `-` at the bottom right zoom both charts together: in from two hours to one,
  30 or 15 minutes (finer bars, and ticks every 15, 10 or 5 minutes), and out to 4, 8 or 12
  hours, 1, 2 or 4 days, a week, then 2, 4, 8 weeks and on. `-` stays lit until the span
  covers the oldest turn shown (the selected session's, or every session's), then dims.
  Past two hours the ticks spread out to suit the span: at 12 hours they show the weekday
  and hour (`Fri 15:00`), and from a day up the date at each midnight (`Fri 16`, or `16 Oct`
  past a week). The pane reads only the last two hours at start; zoomed out further, it reads
  the older history in the background, and the caption says `loading…` until it's in. What
  it has read, and the zoom, last until the wheelhouse closes.
  With a session selected, the `ⓘ` at the panel's top right (a click, or `Tab` to it and `Enter`)
  opens its session info: its id (`C` or Copy id copies it), the user and machine it runs as,
  its directory and branch, model and effort, mode (terminal tab or SDK host), start (UTC),
  transcript, Claude Code's pid, and its wheelhouse MCP server's pid and the code it started on,
  flagged in yellow when that isn't the wheelhouse's own (a pull since it started: relaunch it),
  and the protocol version it runs against the current one.
  A short pane drops the output chart first, then both. With no session selected, every
  session's totals, with no context gauge (each session has its own). The charts' shimmer, and the session list's Cylon eyes, rest while you type in an answer box. The
  colours need 24-bit colour: Windows Terminal draws it but doesn't tell WSL, so the
  wheelhouse assumes it there (`TEXTUAL_COLOR_SYSTEM` overrides that).
  Transcripts are read off the UI thread, so a big one says "reading …" for a moment
  rather than freezing the app. The inbox's session list shows each session's context
  size the same way, as a one-cell bar in the same colours, filled in eighths on a
  linear scale: an eighth for each 125k begun, so the cell is full past 875k.
- **The PRs pane.** Under the inbox, the open pull requests your sessions made (`PRs ·
  mine`), or with `Alt+O` every open PR you authored in any repo (`PRs · all`). As the inbox
  does, it follows the session selected: its PRs alone (`PRs · mine · <session>`), or every
  one with none selected (`Esc`). Each row is its
  `PR` number, repo (when there's more than one), title and a light: red for a failed
  check, changes requested or a conflict with its base; orange, flashing, while checks
  run; yellow while a review is awaited; green when it's ready to merge; grey for a draft.
  One `gh api graphql` call, made by the daemon with `gh`'s own login, fetches them all
  every 5 minutes, when the pane is shown, 20 seconds after a session pushes or merges,
  and on `Alt+R`; every app shares it, and a merged or closed PR leaves the pane at once.
  Highlighting a session's PR shows it in Detail with its thread; `Enter` opens it in the
  browser. Detail always shows what you picked last, in the inbox or here: the other list
  highlights it too if it lists it, and otherwise hides its highlight until you click, press
  `Enter` or move in it again. A PR no session made isn't an item, so picking it leaves
  nothing selected and Detail says why. Without `gh`, or not logged in, the pane says so.
  Mark PRs to act on several at once, as in the inbox: `Ctrl+click` toggles one,
  `Shift+click` (or `Alt+click`) takes the range, `Ctrl+Space` and `Shift+Up`/`Shift+Down` do
  the same from the keyboard, and a plain click or `Esc` drops the marks; the pane's
  bottom border counts them. Under the table, the PRs marked (or else the one selected)
  have five buttons, which a right-click on a row gives as a menu too (for every PR marked
  if the row is one of them, else for that row alone): **Open** (the browser); **Merge**,
  asked first, with `gh pr merge` the way the repo allows (a merge commit where it can,
  else squash, else rebase); **Mark ready** or **Make draft** (`gh pr ready`, or with
  `--undo`), not asked first: Mark ready while any PR it acts on is a draft, for the drafts
  alone; **Request review**, which asks once for a Slack channel or
  name (`#channel`, `@name`, past ones offered most recent first) and sends each PR's
  session a message asking it to post the request on Slack; and **Close**, asked first,
  with `gh pr close`. A button that can't apply to any of the PRs is greyed, and resting
  the pointer on it says why: Merge for a draft, a review still required or changes
  requested, a conflict, failing checks or checks still running (it stays on while GitHub
  hasn't yet worked out whether there's a conflict); Request review for a draft, a PR already approved or
  one no session made (a PR among several it doesn't apply to is skipped, and said so). Merge
  and Close name every PR in their question (Merge with what blocks each), and say under the table what came of each:
  done, or what stood in its way (a review required, conflicts, failing checks, as `gh`
  says). While Detail shows a PR the pane lists (a session's PR item, or one no session
  made, picked here), it has the same buttons for that PR, and under its thread the PR's
  checks (each failing one by name, a link to its run), description, history (commits,
  a run of them on one line, reviews and events) and comments, all as plain text. That
  detail is fetched in the background and kept for two minutes, fetched again sooner when
  the PR changes, and on `Alt+R` in the PRs pane.
- **The Decisions pane.** Under the inbox, the design decisions (ADRs) of every repo a session works
  in: each `NNNN-*.md` file in the repo's `docs/adr`, or in the directories a session there
  named with `set_doc_dirs` instead (`.plan/`, say, only if named, or a directory in another
  repo, such as a sibling docs repo; listed once, under the repo that
  named it, or the first by path when several did). A session started outside any git repo
  (a projects folder, say) may name one too, absolute or relative to that folder, listed under the repo it
  is in. Read again every 10
  seconds and whenever a session starts in another repo or names its directories. A row is
  its number, status and title, and its repo when the pane lists more than one. The status is the doc's front matter `status:`, else
  a `Status:` line or `## Status` section, else its file name's (`0042-ACCEPTED-slug.md`),
  else `?`. PROPOSED and UNRESOLVED come first, then the rest, newest first; a column's
  header sorts by it, as the inbox's do. Moving the cursor shows the decision in Detail,
  rendered, with no answer box, and `Enter` opens it full screen. With no decisions found
  the pane shrinks to a line saying where it looked, and the other panes in its column share
  the room, each in proportion to its size (the inbox twice the PRs', by default). That
  happens in any column with another pane in it: swapped onto Detail's, alone, it keeps its size. A session
  in a git worktree lists its main repository's decisions, so each is listed once. The pane
  is the `design` module: `make install WITH="stats"` leaves it out.
- **Sessions.** The left-hand column lists every session (parked ones dimmed, at the
  foot of the list's room: they fall there as you park them, and rise back as you unpark
  them). A name too long for the column is cut short with `…` (never its `⟳`), so the
  `?`, `D` and `✉` counts after it stay in sight at whatever width you drag the column to. Under it, a description of the selected one: its status, ticket, directory
  and counts, and its synopsis, which the session keeps up to date itself (its brief until
  it sets one), in the Session info pane at the foot of the column, over the session's buttons. The box's last two rows always show
  what a session run in the wheelhouse is doing now, cropped to fit, so nothing above
  them moves as that changes. The dot before each name, and the
  status in the description, are green when it's live (the dot amber once its context reaches 200k tokens, the size at
  which a session run in the wheelhouse is asked whether to Compact), cyan while it starts or relaunches,
  yellow when stalled (its Claude Code runs, but its wheelhouse MCP server hasn't checked in
  for 2 minutes), magenta while it's been asked to park or forget, red when dead (its Claude
  Code isn't running) and grey when parked. A dead session's rows are dimmed in every pane: its
  row here (the dot kept red), its inbox items and conversation, its PRs (each PR's light kept
  bright) and the decisions in a repo no other session works in, until it runs again. While a session is busy (its Cylon eye sweeping), its dot
  flashes, 0.7 seconds in its colour and 0.7 seconds dimmed; with the animation off
  (`ANIMATION_FPS=0`) it stays steady. A row selected or under the mouse keeps its dot's colour, and an
  item's status its own, on the row's shade.
  The selected session, highlighted, is the current one, and the inbox shows its items
  alone. A click on it or on the list's title, or `Esc`, unselects it: the inbox shows every session's items and conversations, the
  list has no highlight, the description says All sessions and the buttons under it are
  disabled (as are their keys) until you click a session, Enter or an arrow in the list
  (which picks the session last selected, or the first). Highlighting an item never brings
  the highlight back. The buttons under the description act on the selected session, and
  only those that apply show, each with
  a tooltip saying what it does: its lifecycle (Rename on a running session, Relaunch
  (Unpark on a parked one), Forget (Stop & Forget on a running one) and Fork on any, and Park on one
  that isn't parked), then its
  conversation (Mode, and Send on one that isn't dead). A button that's off, whatever the reason, is
  dark grey with black text. One you press stays off until what it started has finished: Interrupt
  until the session is interrupted, Compact until it's compacted (Compact on the question suggesting it,
  until that question closes), Shell until the tab has it, Model until the session runs on the new
  one, Relaunch and Unpark until it's running again, Park until it's parked, Forget until it's gone,
  Fork until the fork is made or fails. With no word from the session in 30 seconds it comes back on
  and says so. New (`Alt+N`), Adopt (`Alt+A`, a list of the sessions on disk you can adopt) and
  Relaunch all (`Alt+Shift+L`) act on no one session, so they're keys in the footer. Ctrl+S, Ctrl+T and the
  hint under the answer box act on or describe the same session (with none selected, the highlighted item's).
  An item opened full screen has Mode, Send and the rest in its bar, for its own session. New session (directory, optional name, optional ticket, opening brief,
  model and effort) opens Claude in a Windows Terminal tab. The new session is selected in the session list
  as soon as its row appears, unless you have picked another session or are typing in the answer box by then;
  a fork is selected the same way once it's made.
  The model is Sonnet 5.5, Opus 5.5, Haiku 5.5 or Fable 5.1 (Sonnet 5.5 unless changed), and the effort
  any level from low to max, starting at the model's default (high; xhigh for Haiku). Every launch of the
  session passes both, Relaunch included; to change them while it runs, use `/model` and
  `/effort` in its tab.
  Browse… picks the directory from a tree (Enter opens a folder, Backspace goes up,
  Ctrl+Enter or Choose takes the highlighted one). Rename changes a session's name, and
  is the only way to: a `/rename` in Claude Code isn't picked up, and the next launch
  gives Claude Code the wheelhouse's name again.
  A session is in one of four states. Active: running (starting, stalled, busy and the rest
  only decorate it). Available: not running and not parked, shown red (its rows dimmed), as after a reboot or a
  crash. Parked: not running, by intent, dimmed at the foot of the list. Forgotten: no longer
  in the wheelhouse at all, which Adopt can bring back. Relaunch starts an Available or Parked
  session again where it left off; on a running one it stops it (a tab's Claude Code or a
  host) and starts it again, asking first only if it's mid-turn. Relaunch all (`Alt+Shift+L`)
  brings back every Available session, and selecting an Available session also offers to
  relaunch it. Nothing restarts on its own, and a resumed session is asked to post the
  questions and tasks it already had open. Park on a running session asks it to write its
  handoff notes into its items, then it parks and stops (its tab closes); on one not running
  it parks at once. On one run in the wheelhouse, Park asks first, offering to compact it once
  parked, before it stops (ticked while its prompt cache is warm: a cold one saves nothing). On a Parked session, Relaunch is captioned Unpark, and `Alt+P` and `Alt+L` both
  press it. Forget deletes a session's wheelhouse data: on a running one, Stop & Forget asks you
  first, Let it know (`Alt+L`: it does its own end steps, then stops) or Just stop it (`Alt+J`: it stops
  now, told nothing, which costs no tokens). The default is to let it know while its prompt cache
  is warm and to just stop it once cold, since its end steps would re-read its whole context: the
  dialog says which, and why. Answers queued for it go with it, and the dialog says how many. On
  one not running it asks you to confirm. Fork (`Alt+K` in the session list)
  makes a new session, "<name> (fork)", that carries on from a copy of the session's conversation
  so far, run as the session is, on its model and effort; the session itself carries on too. The
  dialog lists the session's open questions and its todo and blocked tasks, unticked: the ticked
  ones move to the fork (the session is told they're no longer its own, and keeps a closed
  stand-in for each). In a linked git worktree it offers a new worktree for the fork (the
  default), at `<main checkout>/.worktrees/<branch>`, on a new branch from the session's last
  commit (`<branch>-fork` unless changed), or this one. Uncommitted changes stay where they are:
  the dialog warns when there are any, and the fork is told it won't see them. A
  pending Park or Stop & Forget, its button back on after 30 seconds, is pressed again to choose: `Alt+C` cancels the request, `Esc` leaves it be.
  It is never forced: Park, Park now, Stop & Forget and Relaunch all wait for it, and Interrupt aborts it. In the session's
  tab, `/wheelhouse park` and `/wheelhouse end` do the same from there. A tab closes once its
  session stops, by `/exit` or by the wheelhouse: its shell then exits cleanly, which Windows
  Terminal's default `closeOnExit` takes as the end of the tab. If Claude Code fails instead,
  the tab stays open on its error. If the wheelhouse can't start
  a session in its tab, the tab stays open saying why. The wheelhouse never deletes
  anything by itself.
  `Tab` reaches the buttons after the answer box, and each has a key: with the list focused, `Alt+R` renames, `Alt+L` relaunches
  (or unparks), `Alt+P` parks (or unparks) and `Delete` or `Backspace` forgets; the rest are under Keys below. `E`, `S`
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
  on joining, the notes a fork and its parent get ([`fork.py`](claude_wheelhouse/fork.py)), and the keepalive and check messages, as `[wheelhouse]` notifications.
- [The plugin's hook](claude_wheelhouse/plugin/hooks/hooks.json): after a compaction,
  the session's open tasks and questions.
- [Session steps](claude_wheelhouse/steps.py): what a session is told at Compact and after
  it (in the wheelhouse only), at Stop & Forget, when it resumes after an hour or more away
  (or parked), and when one of its PRs merges. It is told the event is coming or has
  happened, so your own instructions can act, and asked for a handoff (what was done,
  decisions, open threads, next step), which it writes with the `write_handoff` tool. Its
  handoff is read back to it on resume and after compaction. A PR that merges while the
  session isn't running is told when it resumes, never by starting it. Messages after an
  event go at once, without waiting for the session's queued answers to be sent. The first
  press of Compact, Stop & Forget or Relaunch asks once which add-ons you want; **Session
  steps**, in the command palette (`Ctrl+P`), has every switch, one row per event. Each is
  also a setting: `WHEELHOUSE_STEP_<EVENT>_<SWITCH>=on|off` or `STEP_<EVENT>_<SWITCH>=` in a
  `.env` (such as `STEP_PR_MERGED_CLEANUP=off`), and `STEP_ADDONS=off` turns every add-on
  off, for instructions of your own that cover it all. Before and After Compact exist only
  in SDK mode. The handoff shows as the
  session's `handoff` doc, kept in `handoffs/` beside the database; one untouched for 30
  days is removed when the daemon starts.

`claude-wheelhouse protocol` prints all of it in one go, exactly as a session receives it,
along with the launch command.

## Using it

Keys, shown in upper case as usual (Alt+V means Alt and the v key, not Shift+V). Every one has a modifier (or is `F1`), so each works from the answer box too: `Enter` open an item's thread (or follow a session's conversation, in the
session list), `Alt+V` show or hide finished items and those autocleanse hid (the footer says which it will do), `Delete` or `Backspace` in the item list close the
highlighted question or decision or dismiss a done task or subagent, or close a doc (asking first, as it does for an open question), `Ctrl+Delete` delete every completed item at once (`Ctrl+Z` brings them back), `Alt+N` new session, `Alt+A` adopt, `Alt+Shift+L` relaunch all, `Alt+Shift+M` the MCP servers of the session in context, `Alt+K` in the session list fork the highlighted session, `Esc` all sessions, unselecting the session (or clears a text
selection first, or back from a thread), `Ctrl+Enter` submit an answer (queued or sent, by the session's mode), `Alt+S` send this one answer now (the queue and mode left as they are),
`Ctrl+S` send the session's queue (one not running: Unpark or Relaunch it and send, asked first), `Ctrl+T` switch the session between Queued and
Immediate, `Ctrl+R` take a queued answer back (with none queued, in the item list, bring back a finished item: a question
as answered, a decision as seen), `Ctrl+Z` undo the last delete, pin or reopen in the inbox (the rows back as they
stood, an open question open again; in a text box, its own undo), `F1` list every key and button, `Alt+Q` ask the wheelhouse how to do something (the helper, below), `Ctrl+Q` quit, `Alt+T` resume the tour while the title bar's badge shows. `Alt+Shift+A` is Send all (every running session's queue). A key whose button doesn't apply says so.
Every dialog opens centred over the screen (the helper's drawer docks right), which stays in sight behind it. `Esc`, or a click outside the box, cancels it (on the tutorial's invite, that is Later, never Never), though not a click outside in the moment a dialog opens (a double-click's second). `Enter` presses the focused button; with focus on a one-line box or a checkbox it takes the dialog's one action (Launch, Rename, Fork, Park and the like), while in a list, a set of choices or a multi-line box it does what it does there. A dialog of several choices (Stop & Forget's, Relaunch's) answers `Enter` only on the focused button. `Tab` and `Shift+Tab` go round its boxes and buttons. A dialog asking yes or no takes `Alt+Y` or `Alt+N` (`Esc` is no, and so is `Enter`, as No has the focus). The `F1` list closes on `Esc`, `Enter`, `F1` or a click outside.

`Tab` goes round the session list, the items, the Decisions list, the answer box, the tour's Next while a tour box
with one shows, then every button shown, in reading order, then the stats pane's effort slider (while it shows), its zoom's `-` and `+` and its session info `ⓘ` (with a session selected; `Enter` or
`Space` presses the one with the focus), and `Shift+Tab` goes back. Nothing else is a stop. A click in the conversation puts focus in the
answer box, where `PgUp` and `PgDn` scroll the conversation, and `Ctrl+Enter` leaves it there. A click on
a button leaves focus where it was. A focused button that is disabled or hidden (Send, once it has sent)
gives focus to the answer box. Send (its button or `Ctrl+S`) from the answer box or a button leaves focus in
the box; from the session list or the items, focus stays there. An item opened full screen goes the same way: its
answer box, then its buttons, and a click in its thread puts focus in the box. A conversation or an item's thread,
in Detail or full screen, follows what's new to its end (your own answer, a reply) unless you've scrolled more than a
line up from the end, and stays at its end when the view gets shorter (emoji suggestions under the box, the box
made taller); an item just selected shows from its top. A doc shown in Detail
is a stop after the answer box (in its place when the box doesn't show), and focused, the arrows,
`PgUp`, `PgDn`, `Home` and `End` scroll it. Keys typed ahead of the screen end as if typed slowly: each acts once the one
before it has done all it does, so the ones behind a key that opens a thread or a dialog go to it, and the ones behind a
dialog's answer go to the screen it uncovers.

### Asking the wheelhouse

`Alt+Q` (or Ask the wheelhouse, in the command palette) opens the helper: a drawer down the right-hand side
where you ask how to do something ("how do I pin an item?") and get a short answer naming the keys and
buttons. It answers from a fact sheet generated from the code (every key, button, setting and feature), this
README and what sessions are told, and reads the wheelhouse's own source when it needs to, saying which file
an answer came from. It runs Claude Haiku through your own Claude Code login, so it costs a little plan usage
(or API spend, on an API-key login); nothing starts until your first question. What leaves the machine: your
questions, the help and the source it reads. Your sessions, items and transcripts don't, and it can't act on
anything: its Claude Code has only the read-only file tools, no MCP servers, none of your settings or hooks,
and writes no transcript. `Esc`, `Alt+Q` again or a click outside closes the drawer and keeps the chat, an
answer still coming included (a notification says when it lands); `Alt+N` in the drawer starts a new chat,
which it offers once a chat gets long. The chat lasts until you quit. A chat quiet for ten minutes lets its
Claude Code go, and the next question starts another, told the chat so far.

Ordering the lists: click a column header in the inbox to sort by it. Status cycles `*`
(the inbox's own order: open questions first, then blocked, running and the rest, each by
session name, then newest first, settled and finished below; with no session selected, every
session's 💬 conversation row at the top), `▲` (by urgency: blocked, waiting, open, running, todo,
newest first, then the settled and finished as `*` has them) and `▼` (the reverse). The other
columns cycle `▲`, `▼`, then back to Status `*`. The marker shows on the column in force.
The session list starts sorted: running sessions first, then by their latest activity (`*`
on the dot column). The name column cycles `▲`, `▼`, then back to the dot's `*`; the counts
and sizes (context, `?`, `D`, `✉`) cycle `▼` (the most first), `▲`, then back, and a session
whose context isn't known yet sorts last either way. A click on the dot's header puts `*` back. Parked sessions stay at the
foot under every sort. A click on the header past the columns shows every session's items,
as `Esc` does. Drag a row with the mouse, or move the
highlighted one with `Alt+Up` and `Alt+Down`, to reorder. A move among rows that share its
sort key keeps the sort, and your order breaks the tie until the row's key changes. Under
Status `*` that is the whole band (active, or settled): a row dropped anywhere in it stays
where you put it, `*` still on, and goes by status from its next status change until you
drag it again. New rows come in by status: at the head of their status, else after the
last row more urgent than them. A move past them (into another band) turns ordering off: the marker goes, the order
as it shows becomes yours, and nothing moves by itself (not even a task finishing) until you
click a header again; new rows then arrive at the top. A row's children move with it.
`Esc` during a drag puts it back. Each TUI remembers its sort for its next start; the order
itself is in the database, shared by every TUI.

Closing several questions or decisions at once: in the item list, `Ctrl`+click marks or unmarks a row
and `Shift`+click or `Alt`+click marks the range from the last one marked (Windows Terminal keeps
`Shift`+click for its own text selection, so use `Alt` there); from the keyboard, `Ctrl+Space`
marks or unmarks the highlighted row, `Shift+Up`/`Shift+Down` extend the range, and `Ctrl+A`
marks every finished (dimmed) row shown, never a pinned one. `Ctrl+Delete` in the item list deletes every
completed item in the inbox (in the session selected, or every session's) at once, with no question: a task done or
dropped, a subagent done or failed, a PR merged or closed, a question answered, a decision seen, a doc read, those
autocleanse has hidden included, never a pinned one. It says how many went, and `Ctrl+Z` brings them all back. One
with a child still under way stays (and it says so). Autocleanse hides a completed item from the inbox 5 minutes after
it completed (`AUTOCLEANSE_MINUTES`, below; 0 turns it off), timed from its last change; it isn't deleted, and `Alt+V`
shows it again with the finished ones. A `Ctrl`+, `Shift`+ or `Alt`+click never starts a drag. `Delete` or `Backspace` then closes the marked
questions and decisions and dismisses the done tasks and subagents, leaving any pinned one among them, `Ctrl+R` brings back the finished ones, `Esc` clears the marks, and a plain click
starts afresh. Marked rows that include an open question ask once first, naming them. Once `Delete` has taken the highlighted row (one row, or several marked), the
highlight goes to the row immediately above the topmost row it took, or to the new first row
if that was the first.

Text: drag the mouse over the conversation or a thread to select part of it (shown black
on white), double-click to select a word (not in Detail showing an item or a doc, where a
double-click opens it full screen as `Enter` on its inbox row does, nor on a full-screen
item, where it goes back as `Esc` does; a double-click on a link or button there does that
link's or button's job), `Ctrl+A` in the answer box selects its text (in the item list it marks the finished rows; in a doc opened
full screen it selects the whole doc), and `Ctrl+C` copies the selection (through the terminal, which Windows Terminal supports). Right-click on a session in the session list selects it and opens a menu of
its buttons (greyed as they are under the list; Esc or a click outside closes it); on a PR in the PRs pane, a menu of its
actions (of every PR marked, if it is one). Elsewhere right-click does as a
terminal's does: it copies the selection, or with nothing selected pastes the clipboard
into the answer box under the pointer (or the focused one). Under WSL the paste reads the
Windows clipboard through PowerShell, so it takes most of a second; elsewhere it pastes the
wheelhouse's own last copy. Hold `Shift` to drag with the terminal's own selection
instead.

The left column holds Sessions, Stats and Session info, top to bottom; the Inbox and,
under it, PRs and Decisions are in the middle, and Detail on the right. Sessions, Session info,
Inbox, Decisions, PRs and Detail are titled in their top border; the Stats panel's own border names the session it shows. The first two
columns are 45 wide where there's room; on a narrower terminal they give way in proportion,
each down to its minimum, so Detail keeps 40. Every boundary between areas can be
dragged: the lines between the three columns, between the panes in the left and middle
columns, and above the answer box. A line turns teal under the pointer and while you drag it, and no pane goes
below its minimum (the session column never gets too narrow for its buttons' captions).
The sizes are kept in the wheelhouse's database, as shares of the space, so they follow a
resized terminal; double-click a line to put its default back. A size that doesn't fit a
smaller terminal shrinks there, and comes back when there's room again. A column too short
for its panes' minimums (some rearrangements on an 80x24 terminal) takes the rows it lacks
from the pane with the most to spare, Session info first, whose description scrolls. The three columns
fit an 80-column terminal.

To try another layout, drag a pane by its title (its top border) onto another pane: the
pane under the pointer gets a teal border, and letting go swaps the two. `Esc` during the
drag, or letting go on the pane's own place or outside the panes, changes nothing, and a
plain click on a title does nothing. Each place keeps its size, and grows to what a pane
moved into it needs (Session info's buttons, say). Each TUI remembers its layout for its
next start. A module's pane that's gone (uninstalled or disabled) leaves its place empty,
and goes back there when it returns. **Reset layout**, in the command palette (`Ctrl+P`),
puts every pane back in its default place and every line at its default size.

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

To stop the daemon, quit the app (`Ctrl+Q`) first, since an open app starts it again, then run
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
  item in doubt gets a `[wheelhouse]` message 50 minutes after its last turn, and every 50
  minutes after that. It keeps the session's prompt cache warm (a cold one is written again
  whole), and with an item in doubt it is the check, `Check: T1 running, Q2 reply owed`,
  which the session answers by correcting what's finished. It stops once the session has
  been quiet 8 hours. Then a session run in the wheelhouse is compacted while its cache is
  still warm (asked first, as Compact asks, and compacted without notes if its notes turn
  ends without them; writing to it before its `/compact` goes calls that off), and a tab idle-compacts by itself about 54 minutes later (the wheelhouse
  launches tabs to do so past 150k tokens). `--keepalive-hours N` (on the app or `claude-wheelhouse daemon`), or
  `WHEELHOUSE_KEEPALIVE_HOURS=N`, or `KEEPALIVE_HOURS=N` in a `.env` (see
  [Settings](#settings)), sets another limit; fractions are allowed and 0
  turns it off. Each message costs one request, which reads the cached context: for a
  quiet session that is much cheaper than writing the cache again when you come back.
- A session run in the wheelhouse has no `/mcp`. When its work needs an MCP server that
  wants signing in (a claude.ai connector such as Slack, or a server of your own with
  OAuth) or has failed, the session asks for it (`request_mcp_signin`), and you get a
  question saying what it needs the server for, with its sign-in link or its error.
  Servers it doesn't need are never asked about. For a claude.ai connector, open the link
  and authorise it: the wheelhouse reconnects it every few minutes for two hours, so the
  question closes by itself, or reply anything to reconnect it at once. For a server of
  your own, Claude Code waits for the browser to come back and connects it itself; if the
  browser can't reach it, paste the address the browser ends on as your reply. Shell
  closes these questions, as `/mcp` in the tab does the same.
- `Alt+Shift+M` lists the MCP servers of the session in context, as its host last saw
  them (it looks again as the dialog opens), those that need you first, each with its
  error or sign-in link. Sign in starts a server's sign-in and opens its link; for a
  claude.ai connector, press Reconnect once you've authorised it. A server of your own
  connects once the browser comes back, or paste the address the browser ends on into
  the box under it. Reconnect on a claude.ai connector reconnects it in every session run
  in the wheelhouse that has it, as its sign-in is your account's; a local server's is
  this session's alone (a stdio server's process restarts). Disabled servers have no
  button: `/mcp` or the settings turn them on. A session not running shows its last look;
  a tab session says to use `/mcp` there.
- Operations: from the press of Park or Stop & Forget, or the start of a compaction, to its
  end, the session is busy and its eye in the session list sweeps blue, running or not.
  While it is busy, sending (now, Ctrl+S, Send, Send all) and every other operation (Park,
  Park now, Stop & Forget either way, Relaunch, Unpark, Fork, Shell, Compact) are refused
  with one message ("demo is busy parking: send once it's done, or Interrupt to abort it.").
  Drafting and queueing stay open, and an answer to a permission or question the session is
  waiting on still goes. Interrupt aborts the operation and nothing else: a park or Stop &
  Forget not yet passed on is dropped silently; one the session has heard, or a park already
  under way (compacting included, its tokens spent), is called off, and the session is told
  to carry on as before. A park or Stop & Forget the session hasn't acted on within 15
  minutes fails with a warning ("demo couldn't be parked within 15 minutes: it carries on as
  it was."), as does one whose session stops running first.
- After a compaction, the session is told its open tasks and questions, with their titles,
  once. A session run in the wheelhouse has a Compact button, and past 200k of context a
  question suggesting it, answered with buttons (Compact, Leave it); both take optional
  instructions for the compaction (none: the session is asked what to keep) and what follows
  it: just compact, send its queued answers in the same one prompt as that reminder (the
  default), or park it at once, sending nothing more, so nothing writes the compacted context
  to the cache until it is unparked. A compaction is one cycle, however it starts (Compact,
  the question's Compact, Relaunch or Park compacting first, the session's own request, the
  8-hour mark, or Claude Code compacting by itself at full context): from the press until
  `/compact` has ended, or the cycle fails or is cancelled, the session is busy, as during
  any operation (below, Operations). Throughout, Compact is off, the session's inbox items and Detail dim
  and ignore clicks, and its eye in the session list sweeps blue instead of red. If the
  session is still writing its notes after 2 minutes, a question asks whether to keep
  waiting (asked again 2 minutes later) or abort (the notes turn is interrupted, nothing is
  compacted, and queued answers stay queued); `/compact` itself has 10 minutes before the
  cycle fails. Each compaction is also a task in the inbox, "Compact demo: 220k context", that
  moves with it (running, then done with the tokens before and after, blocked with why it
  failed, or dropped) and holds the 2-minute question under it; the session never sees it.
  The conversation shows each stage as a blue line of the detention-level trash compactor
  closing in, and while it runs, the odd panicked exchange between the crew and the droids.
  A session in a terminal tab gets the same, from the plugin's PreCompact hook, whether
  `/compact` was typed there or Claude Code compacted by itself; there the wheelhouse can hold
  only what it sends, never what's typed in the tab. The session can also ask for a compaction itself, with
  the `request_compact` tool and the notes it wants kept, for long autonomous runs: the
  wheelhouse compacts it once its turn ends, never during it, as if you had pressed Compact
  with those instructions (then just compact). One request at a time, none within 30
  minutes of its last, and only in a session run in the wheelhouse; a Compact or Interrupt
  you press first, Park, Stop & Forget or a move to a tab drops a request still waiting, and
  lifts its 30 minutes. Once its useful work is done and it is only waiting on you, a session
  run in the wheelhouse parks itself, unasked, with `park_session`: it is compacted once its
  turn ends, with the notes it wants kept, then stops, as if you had pressed Park with Compact
  first, and Unpark resumes it as usual. A session that thinks compacting undesirable (you're
  likely to Stop & Forget it, say) parks as it is once its turn ends, and posts its reason as
  a decision. A session in a terminal tab, where you may be at the keyboard, isn't asked to
  park itself, and a park there is never compacted. Permission items are answered with buttons: Allow and Always at once, while Deny opens a
  one-line box for an optional reason (the session reads it as why, so it can try something
  else); Enter denies, empty or not, and Esc, wherever focus is, goes back to the buttons. The plugin's `SessionStart` hook does it
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
| `AWAY_HOURS` | `WHEELHOUSE_AWAY_HOURS` | `--away-hours N` | 2 (0 turns the card off) |
| `AUTOCLEANSE_MINUTES` | `WHEELHOUSE_AUTOCLEANSE_MINUTES` | `--autocleanse-minutes N` | 5 (0 turns autocleanse off) |
| `SESSION_DIR` | `WHEELHOUSE_SESSION_DIR` | none | the parent of the wheelhouse's clone |

`SESSION_DIR` is the directory the New session dialog (`Alt+N`) starts in. `~` is allowed, and a
relative path is taken from your home directory (`SESSION_DIR=projects` is `~/projects`).
If it isn't a directory, the dialog uses the default, and a line on the terminal the app
was started from says why.

`ANIMATION_FPS` sets how many frames a second the title's shimmer, the session eyes, busy sessions' flashing dots and
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
- Nothing restarts after a reboot: Relaunch all (`Alt+Shift+L`) brings the sessions back.
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
2. Quit the app (`Ctrl+Q`), then in the clone run `make stop`
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

(`make test` is `uv run --no-sync pytest` on what `make install` installed: the modules'
tests too, and with a module left out, the tests that need it skip. Plain `uv run pytest`
installs every module back first.)

The tests run in parallel, a worker a core (pytest-xdist). `uv run pytest -n 0` runs them
serially, as a single test or a debugger wants.

To work on the wheelhouse, read [AGENTS.md](AGENTS.md) first. From inside a wheelhouse
session, `unset WHEELHOUSE_DB WHEELHOUSE_SESSION_ID WHEELHOUSE_TOKEN` before running the
tests: they refuse to start while they would reach your own wheelhouse's state.

## Licence

MIT, see [LICENSE](LICENSE).
