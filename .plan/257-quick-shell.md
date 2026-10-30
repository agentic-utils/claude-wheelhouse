# Quick shell, and commands a session asks Doug to run

Issue: #257. Status: design, revised with Doug's answers (Q238 to Q241), for review.

The ask, in Doug's words (30 Oct): "Add a way for me to launch a shell for a quick command;
then, when a session suggests a Bash excerpt to me, let Wheelhouse display that with a
button that I can press to just invoke it directly rather than having to open a shell and
paste it in myself"

Settled with Doug:

- Q238: "why not give me a modal with an editable text box and a run button? Keep it all in
  one step?"
- Q239: "a": refuse anything outside printable ASCII, newlines allowed.
- Q240: "no, a for me, and it prompts a question: how are you designing this system to
  detect an invokable bash excerpt? I think it should be explicit, requested by the model
  similar to how questions are raised. Don't have wheelhouse regex'ing for expressions,
  have the model respond something like user_tool_call("xxx")"
- Q241: "I actually think, repurpose the existing "Shell" button for this. We don't need a
  button for handing an SDK session over to a tab, it's a rare operation; rename it
  "Handover to Claude Code in a shell" so it is no longer listed as "Shell" in the F1 help
  box, then the name "Shell" can be repurposed for this."

Two parts, landing in this order: the shell, then command items.

## 1. The quick shell

**Shell** (the session bar's button, and Alt+H from anywhere) opens a new Windows Terminal
tab (iTerm on macOS), the way session tabs open: the hub launches it, in the same
`wheelhouse-<user>` window, titled `⌁ <session name>`. The wt command line is
`wsl.exe -d <distro> -u <user> --cd <dir> -- $SHELL -li`: no text of ours beyond the
directory, which `wt_argv`'s `CMD_META` check already guards.

- **Directory**: the selected session's (or the selected item's session's) live directory:
  the `cwd` of the last record in its transcript, since a session that `cd`s into a
  worktree works there, else `sessions.cwd`. With no session selected, or that directory
  gone, `~`, with a notice naming the missing directory.
- **Environment**: wt.exe starts `wsl.exe` from Windows, so the tab gets a fresh login
  environment: no `WHEELHOUSE_*`, `VIRTUAL_ENV` or `CLAUDE*` reaches it. iTerm builds its
  own the same way. The launch still passes `clean_env()` minus every `WHEELHOUSE_*`, so a
  runner that did inherit stays clean (a test pins it).
- **Closing**: `exit` or Ctrl+D. The wheelhouse doesn't track shells: no row, no liveness.
- **With `--connect` to another machine** the tab would open there, out of sight: Shell
  says so in a notice and opens nothing.
- The button shows for every session now, not only SDK ones, and works for parked and
  ended sessions (their directory is still on disk).

**Handover** (today's Shell: an SDK session handed to a Claude Code tab) becomes "Handover
to Claude Code in a shell", in the session menu (right-click) only, with no key: it's rare,
and a mistaken press costs a relaunch. F1 lists it under the menu's entries, not the keys.
The internal button id becomes `handover`; the hub's verb and the `sessions.shell` column
keep their names, which older hosts read.

Passed over: an embedded terminal widget (Textual has none; `textual-terminal` is
unmaintained and fights the app for keys) and a one-line prompt with output in a drawer
(no pty, so `sudo`, `git push` credentials and `less` don't work).

## 2. Command items

No detection: the session asks, as it asks a question. A new MCP tool:

```
suggest_command(command: str, why: str) -> str   # "C4: shown to the person"
```

**A new kind, `command`, ref `C<n>`** (C is free). Not a task: a task's statuses are the
session's (todo, running, done) and a command's are the person's, as a decision's are.
Not a question: a question waits for an answer the session reads, and a command's point is
that nothing comes back unless Doug sends it.

**Validated at post time**, each refusal an error the session can act on: `command` empty,
holding anything but printable ASCII (0x20 to 0x7E) and newline (`"command holds U+2019 at
line 2, column 14: only printable ASCII and newlines can be run"`), over 20 lines or 2000
characters; `why` empty or over one line. The rule shuts out escape sequences, CR, tabs,
zero-width and bidi characters and homoglyphs: what's shown is exactly what runs.

**Statuses**, the person's: `new` (posted, not run), `run` (run at least once), `closed`
(Doug dismissed it unrun, Delete as for a question). Reposting the same command from the
same session updates the item back to `new` rather than adding one.

**Shown** in the inbox like any item (`C4  Check the deployed ref  wheelhouse-ui  new`),
and in the conversation and Detail as its `why`, the command in a code block, and a Run
button: a real Button under the item's detail, as the question's Answer box is, not a
segment in the text, so no session text ever reaches markup.

**Run opens the modal**, the one step:

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Run C4 from wheelhouse-ui                                                    │
│ Check the deployed ref after the merge                                       │
│ in ~/projects/claude-wheelhouse/.worktrees/257-quick-shell                   │
│ ┌──────────────────────────────────────────────────────────────────────────┐ │
│ │gh pr view 263 --json mergeStateStatus,headRefOid                         │ │
│ │                                                                          │ │
│ └──────────────────────────────────────────────────────────────────────────┘ │
│                                                     [ Run  Ctrl+Enter ]      │
│                                                     [ Cancel  Escape ]       │
└──────────────────────────────────────────────────────────────────────────────┘
```

- The box is a `TextArea` holding the command, editable (fill in `<pr>`, trim a line),
  never soft-wrapped (a horizontal scrollbar instead) so line breaks are the real ones,
  and tall enough for all 20 lines it can hold.
- Run checks the box against the same ASCII rule (a paste can bring in anything) and
  refuses with the position, as the tool does. It then opens a fresh shell tab in the
  directory part 1 picks, refusing (never falling back to `~`) if that directory is gone:
  `git reset` in the wrong repo is the failure to avoid.
- Focus starts in the box, not on Run, so a stray Enter edits rather than runs. Nothing
  runs on posting, selection or a double click.

**How the text reaches the tab**: the app writes the box's text to `shell/<key>` beside
the database (mode 0600, a random key), and the tab's command line carries only the key:
`python -m claude_wheelhouse shell <key>`. That reads and deletes the file and execs bash
with a generated rc that sources `~/.bashrc`, prints `C4 from wheelhouse-ui:` and the
command, puts it in history and `eval`s it, leaving an ordinary shell at the prompt after.
The text travels in that process's environment, never on a command line, so nothing passes
through cmd.exe or wt.exe quoting. (macOS zsh: the same through a `ZDOTDIR` rc.)

**After a run**: the item goes to `run` and its detail gains a dim line `ran 14:05` (with
`, edited` if the box changed), kept as an item note (a `messages` row the session isn't
sent). `launch.log` gets the time, session, ref, directory and the exact text run. The
output stays in the tab: the session never sees it, though `list_items` shows the status.

## 3. Compatibility and the protocol

- **Store**: `command` in `KINDS`, `STATUSES` and `INITIAL_STATUS`; no new column (the
  item note is a `messages` row). Items are per session, so a session on older code never
  meets a command item: only a new session can post one.
- **`PROTOCOL_VERSION` 24**: a new tool and a protocol rule sessions must follow. Older
  sessions show as needing a relaunch; until then they just don't suggest commands.
- **protocol.md** gains one rule: "When you want the person to run a shell command
  themselves (one that needs their credentials, their eyes, or their machine outside your
  sandbox), post it with `suggest_command`, not as a code block to copy: `why` says in one
  line what it does or shows. Plain ASCII only; write a placeholder as `<name>`. They may
  edit it before running. You won't see its output: ask for what you need back."
- The tool's docstring, `launch.JOINED_TEXT` if it lists tools, and the helper facts move
  in step; the daemon gains a `shell` route (additive; the fingerprint restarts an older
  daemon).

## 4. Staging

1. The shell: Shell and Alt+H repurposed, Handover renamed into the session menu, the
   hub's `open_shell`, wt and iTerm argv, the directory rule; README, F1 `DESCRIBE` and
   `BUTTONS`, a `whatsnew.FEATURES` entry.
2. Command items: the kind, `suggest_command` with a table-driven validation test, the
   protocol rule and version bump, the Run button and modal, `shell <key>` and its rc,
   the run note; README, a feature and tour tip.

## Open questions for Doug

**Q1. Tell the session when a command has run?** (Decides whether a run interrupts it.)

- (a) No: the status changes, which `list_items` shows; Doug says more if he wants
  (recommended: a run often has nothing for the session, and a message starts a turn).
- (b) Yes: a short message "ran C4 (edited)" goes to the session, starting a turn.
- (c) A checkbox in the modal, "tell the session", off by default.

**Q2. Handover's place.** (Decides whether SDK sessions keep a visible way back to a tab.)

- (a) Session menu only, no key (recommended).
- (b) Session menu, plus Alt+Shift+H.
