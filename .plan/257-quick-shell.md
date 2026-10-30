# Quick terminal, and run a suggested Bash excerpt from a button

Issue: #257. Status: design, for Doug's review.

The ask, in Doug's words (30 Oct): "Add a way for me to launch a shell for a quick command;
then, when a session suggests a Bash excerpt to me, let Wheelhouse display that with a
button that I can press to just invoke it directly rather than having to open a shell and
paste it in myself"

Two parts, landing in this order: the terminal (small, no risk), then run buttons (the
part with the safety design).

The name: the session bar already has **Shell** (Alt+H), which hands an SDK session to a
Claude Code tab. To keep the two apart, this feature is the **Terminal** everywhere in the UI.

## 1. The quick terminal

**Recommendation: a new Windows Terminal tab (iTerm on macOS), opened the way session
tabs are.** Alt+B (free today; B for bash) opens it from anywhere in the app.

- **Launch**: the hub opens it, as `launch.open_tab` does, so it lands in the same
  `wheelhouse-<user>` window as the sessions, titled `⌁ <session name>`. The wt command
  line is `wsl.exe -d <distro> -u <user> --cd <dir> -- $SHELL -li`: no text of ours in it
  beyond the directory, which `wt_argv`'s `CMD_META` check already guards.
- **Directory**: the selected session's (or the selected item's session's) live directory:
  the `cwd` of the last record in its transcript, since a session that `cd`s into a
  worktree works there, else `sessions.cwd`. With no session selected, or the directory
  gone (a removed worktree), `~`, with a notice naming the missing directory.
- **Environment**: nothing to strip. wt.exe starts `wsl.exe` from Windows, so the tab gets
  a fresh login environment: no `WHEELHOUSE_*`, `VIRTUAL_ENV` or `CLAUDE*` from the daemon
  reaches it. On macOS iTerm builds its own environment the same way. The launch still
  passes `clean_env()` minus every `WHEELHOUSE_*`, so a future runner that does inherit
  stays clean (a test pins it).
- **Closing**: `exit` or Ctrl+D; Windows Terminal's default `closeOnExit` closes the tab.
  The wheelhouse doesn't track terminals: no row, no liveness.
- **With `--connect` to another machine** the tab would open on that machine, out of
  sight: Alt+B says so in a notice and opens nothing.

Passed over:

- **An embedded terminal widget.** Textual has none. `textual-terminal` (pyte under it) is
  unmaintained since 2023 and predates Textual 8; a pty in the TUI fights the app for keys
  (Ctrl+C, Alt+ chords, Escape), runs in the app's process (so it dies with the app, and
  under the daemon it would run on the wrong side), and redraws slowly. A lot of new
  surface for a worse terminal than the one next door.
- **A one-line prompt with output in a drawer.** No pty, so nothing interactive works:
  `sudo`, `git push` asking for credentials, `less`, a confirm prompt. Output would stream
  through the daemon's SSE. Doug asked for a shell, and half of quick commands are
  interactive.

## 2. Run buttons on Bash excerpts

**Which blocks**: fenced blocks whose info string is `bash`, `sh`, `shell`, `zsh` or
`console`, in a session's replies (its `claude` blocks in the conversation) and in item
bodies and replies from the session (Detail, the thread view). Not in: unlabelled fences
(could be anything), the session's own tool lines, Doug's own messages, PR item bodies
(GitHub text), docs (examples, often quoted from elsewhere; revisit if missed).

**What runs**, derived once per block into `cmd`, the one string both the confirm and the
run use:

- `bash`/`sh`/`shell`/`zsh`: the block's text as written, `#` comment lines included.
- `console`: only the lines starting `$ `, without it; the rest is output and dropped. No
  `$ ` lines, no button.
- Placeholders (`<pr>`, `<ticket>`, matched as `<[a-z][a-z0-9_-]*>`) don't stop the button:
  the run is edited before Enter (below), and the tab says which placeholders are left.

**Refused** (no button, a dim `can't run: <reason>` line instead): any character outside
printable ASCII (0x20 to 0x7E) and newline. That one rule shuts out escape sequences, CR,
tabs, zero-width and bidi characters (Trojan Source), non-breaking spaces and homoglyphs:
whatever is left is exactly what the terminal draws. Also refused: more than 20 lines or
2000 characters (too long to read before running).

**The button**: one line under the block, inside the conversation's text, so it scrolls
with it:

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ claude · 14:02                                                               │
│                                                                              │
│ The PR is green. To check the deployed ref yourself:                         │
│                                                                              │
│  gh pr view 263 --json mergeStateStatus,headRefOid                           │
│                                                                              │
│  ▶ Run in a terminal                                           bash · 1 line │
│                                                                              │
│ Or the full log:                                                             │
│                                                                              │
│  gh run view --log | grep -n "error"                                         │
│                                                                              │
│  ▶ Run in a terminal                                           bash · 1 line │
└──────────────────────────────────────────────────────────────────────────────┘
```

It is a segment with `Style.from_meta({"@click": "app.run_excerpt('<key>')"})`, as PR links
are (`prs.link`). The key is a random id the app holds against `(session, cmd)`; no text
from the block ever goes into markup or meta (#139's lesson). Keyboard: Alt+Shift+B lists
the excerpts in the conversation shown, newest first, and Enter runs the chosen one.

**A press**: opens a fresh terminal tab (as part 1, same directory rule), titled
`▶ <session name>`, with the command **at the prompt, not run**. Doug reads it, edits a
placeholder if there is one, and presses Enter; Ctrl+C drops it. The tab stays a normal
shell afterwards. A button never reuses an open terminal: there's no way to type into
another Windows Terminal tab.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ From wheelhouse-ui, 14:02. Read it, then Enter runs it; Ctrl+C drops it.     │
│ Fill in before running: <pr>                                                 │
│ ~/projects/claude-wheelhouse/.worktrees/257-quick-shell                      │
│ $ gh pr checks <pr> --watch█                                                 │
└──────────────────────────────────────────────────────────────────────────────┘
```

How the text gets there: the app writes `cmd` to `shell/<key>` beside the database (mode
0600), and the tab's command line carries only the key, as `run` carries only the session
id: `python -m claude_wheelhouse terminal <key>`. That reads and deletes the file, then
execs `bash --rcfile` on a generated rc that sources `~/.bashrc`, puts `cmd` in history
(`history -s`) and on the line with `read -e -i`, then `eval`s what Doug submits in that
same shell. No quoting through cmd.exe or wt.exe, so nothing to escape and nothing to get
wrong. Multi-line blocks: to verify in building whether `read -e -i` holds newlines; if
not, the tab prints the block numbered and Enter runs it all, with the block in history
(Up recalls it whole) for editing instead. Every run is logged to `launch.log`: time,
session, key, `cmd`.

## 3. Safety

A button runs model-written text on Doug's machine, and that text can come from anywhere a
session read: a PR body, a web page, a doc. Defences, in order:

1. **Never run anything without Enter in the terminal.** Recommended over a confirm modal
   in the TUI, and over one-press run. The prompt is the confirm: it shows the exact bytes
   (the ASCII rule makes the terminal's drawing faithful), in the place Doug would have
   pasted it, and it lets him edit, which a modal doesn't. One-press saves one keystroke
   and loses the last look at what a prompt-injected session wrote. A modal in the TUI as
   well would be two confirms for one action.
2. **What's shown is what runs.** `cmd` is derived once; the prompt shows it; Enter runs
   what's on the prompt. The ASCII rule leaves nothing invisible, and the length cap
   leaves nothing past the fold.
3. **Nothing automatic.** No run on arrival, on selection or on a double click (a click
   with `chain > 1` does nothing).
4. **Untrusted quotes.** The wheelhouse can't tell a command the session wrote from one it
   copied off a web page. The tab's header names the session and time so Doug knows whose
   suggestion it is; beyond that, defence 1 is the answer.

## 4. Where the run goes

- **Directory**: the session's live directory, as part 1. If it's gone, the button refuses
  (a notice naming it), never falls back to `~`: `git reset` in the wrong repo is the
  failure to avoid.
- **Output**: in the tab only. The session never sees it; Doug can paste it back if he
  wants. (Feeding it to the session is a later feature, if ever.)
- **Parked or ended sessions**: the button still works, the directory being on disk; a
  conversation you can read is one whose suggestions you may still want.

## 5. Compatibility and the protocol

No store change: the command goes through a file, the terminal has no row, so no
`ADDED_COLUMNS`, trigger or `PROTOCOL_VERSION` bump. One new daemon route (`terminal`,
with optional session and key) is additive; the fingerprint restarts an older daemon.

Protocol: a line in `protocol.md` telling sessions that a command meant for the person goes
in its own fenced `bash` block, one command per block where it can, no `$ ` prompt,
placeholders written `<name>`. Recommended without a `PROTOCOL_VERSION` bump: an older
session that doesn't follow it only gets fewer buttons, not wrong behaviour, and a bump
would mark every running session for relaunch.

## 6. Staging

1. The terminal: Alt+B, the hub's `open_terminal`, wt and iTerm argv, the directory rule,
   README, F1 `DESCRIBE`, a `whatsnew.FEATURES` entry.
2. Run buttons: excerpt extraction and the ASCII rule (a table-driven test of what passes
   and what's refused), the button segment, Alt+Shift+B, the `terminal <key>` entry point
   and rc, the protocol line, README, a feature and tour tip.

## Open questions for Doug

**Q1. Confirm where?** (Decides the run's last step.)

- (a) At the terminal prompt, editable, Enter runs (recommended).
- (b) A modal in the TUI showing the command, then the tab runs it at once: one place to
  read, but no editing, so placeholders can't be filled.
- (c) Both.

**Q2. Non-ASCII in a command.** (Decides whether `echo "naïve"` or an emoji commit message
gets a button.)

- (a) Refuse anything outside printable ASCII (recommended: no homoglyphs or hidden
  characters, at the cost of the odd harmless command).
- (b) Allow printable non-ASCII letters, refusing only control, format and space
  characters other than the plain space: more buttons, homoglyphs back in play.

**Q3. Docs.** (Decides whether `.plan/` docs and other doc items get buttons.)

- (a) No, only session replies and item bodies (recommended).
- (b) Yes, everywhere a fenced bash block shows.

**Q4. Keys.** Alt+B for the terminal and Alt+Shift+B for the excerpt list, or others you'd
rather keep free?
