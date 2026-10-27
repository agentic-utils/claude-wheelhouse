# Helper chat (#198, T178)

The ask, in Doug's words: "Let's add a helper chat. Since we've got a big meaty LLM within
reach, may as well offer the user a way to ask how to do things, feeding into Haiku with
context including all the help files and full source code of Wheelhouse."

Designed 27 Oct 2026 against main bc081dc; built in three PRs into batch/2026-10-27:
`helper.py` and the fact sheet, the drawer, and the eval.

## Decisions (Doug's answers)

**Q1. How the app calls Haiku: `claude -p` on the person's own Claude Code login.**
"Option a, yes, and of course defer creation until first use." One `claude -p
--input-format stream-json --output-format stream-json` per chat, as `plan_usage`'s
persistent mode runs, started at the first question and never before. On a subscription it
costs plan usage, on an API-key login that key. Passed over: the Messages API through the
`anthropic` package (every user would need a Console key) and the Agent SDK (gated
EXPERIMENTAL in `features.py`).

**Q2. The source behind tools, the help and a fact sheet in the prompt.** Everything in the
prompt would be about 450k tokens on the long-prompt rate card, slower and worse answers.
Instead the fact sheet, the README and `protocol.md` (about 35k tokens) go in the first
message, and the package's and modules' source is readable through Read, Grep and Glob.
Doug, on the fact sheet: "let's add a precommit hook and a CI test to ensure that the fact
sheet has been updated on each commit (hash comparison prob best), then add to AGENTS.md to
direct the coding agent to check and update the fact sheet on every commit, and to hold a
version and timestamp in the fact sheet so that a no-op commit can still be accompanied by a
change to the fact sheet." Built as below (The fact sheet).

**Q3. A drawer**, docked over the right column, Esc to close, the conversation still there
when it opens again. Passed over: a module pane (competes for room) and a pseudo-session
(every verb would need a special case).

**Q4. Alt+G** ("guide"), and a command palette entry. Alt+Q was one key from Ctrl+Q, Quit.

**Q5. History kept for the app's run**, in memory; New chat clears it.

Other decisions: model `claude-haiku-5-5` at its default effort; the helper runs in the app,
not the daemon (it reads no store, so no route and no `fingerprint.API` bump), and with
`--connect` it reads the app machine's install; `.plan/` is left out of the prompt and the
tools (rejected options read as features); SDK mode stays unadvertised (the sheet is
generated with it off and the prompt says the person runs tabs, unless they have it on);
the claude stops after 10 minutes without a question (`helper.IDLE`, the cache cold by then)
and the next question starts another, told the chat so far.

## The fact sheet

`claude_wheelhouse/helper_facts.md`, committed. Above its `generated` line, a guide written
by hand (what the wheelhouse is, the screen, its words, where in the source to look); below,
what `make facts` writes from the code (`facts.generated`): F1's text as `keys_help` builds
it with SDK mode off and the modules' keys, the command palette's commands, `make args`'s
settings and every feature's release note. The source wins where they disagree, and the
prompt says so.

Its first line is the stamp: a version (one past every stamp in the file), a UTC time, and
a hash of the source as git's index holds it: each path and blob id of the package's `.py`
and `.md` files, the modules' `.py` files but their tests, and the README. Not the sheet,
the tests, `.plan/` or AGENTS.md. Taken from the index, not the files, so the hook checks
exactly what the commit holds, and the CI test (a clean checkout) the same.

- `make facts` regenerates and restamps for what is staged, and stages the sheet. It names
  any source changed but not added.
- `.githooks/pre-commit`, installed by `make hooks` (`core.hooksPath`), runs
  `python3 claude_wheelhouse/facts.py check`: a script, standard library only, run in the
  repo it's in. No pre-commit framework: the repo had none, and this needs no install.
- `test_facts.py` checks the stamp against the checkout and the generated part against the
  code, in the suite CI already runs.
- Two branches stamped apart conflict on the stamp line. `make facts` resolves it (a
  version past both, its part regenerated); only a conflict in the guide needs a person. A
  batch merge restamps once its PRs are in (AGENTS.md).

## Privacy and safety

Read-only, with nothing that acts on sessions. The command line (`helper.command`):

- `--model claude-haiku-5-5 --tools Read,Grep,Glob`: no Bash, Edit, Write, WebFetch,
  WebSearch or Task.
- `--strict-mcp-config` with no `--mcp-config`: no MCP servers, so no `wheelhouse` server.
- `--setting-sources=`: none of the person's hooks, plugins or permission allow-lists.
- `--restricted`: Claude Code 2.1.311 documents it as confining the file tools to the
  working directories; added beside the design's flags.
- `--permission-mode dontAsk --permission-prompts none`: anything needing approval is denied.
- cwd the package directory, `--add-dir` each installed module's package directory: the
  install's `.env`, `.venv` and `.git` sit outside them.
- `--no-session-persistence`: no transcript, so never in Adopt, the stats or usage.
- `--system-prompt`: short rules, replacing Claude Code's default prompt. The briefing (the
  sheet, README and protocol) goes in the first user message instead: as one argument it
  would pass Linux's 128 KB limit on a single argument.
- `launch.clean_env()`, and every `WHEELHOUSE_*` variable dropped.
- `--bare` is not usable: it never reads the OAuth login.

What leaves the machine: the questions, the briefing and the source Haiku reads, under the
person's own Claude Code account. No session content, transcripts, items or store data.

## Haiku 5.5 and cost

From the claude-api skill (6 Oct 2026): 1M context, 128K output; $0.10 in, $0.50 out per
million tokens up to 100k tokens of prompt, $0.50 and $2.50 above; cache reads 0.1x. A
question costs about $0.01 cold and $0.004 warm at API rates (about 35k of briefing, four
tool rounds, 20k of source read). Past 90k tokens of context (`helper.FULL`) the drawer
offers New chat, before a request crosses 100k onto the long-prompt card.

## Delivery

1. `helper.py` (no Textual): the command line, the briefing and the stream-json driver
   (start at the first question, ask, stream text and tool events, errors said plainly,
   idle stop, New chat). The fact sheet, `facts.py`, the hook, `make facts`, `make hooks`.
2. The drawer on Alt+G: the palette command, the F1 line, a `whatsnew` feature, a README
   section.
3. `make helper-eval`: how-to questions with the key each answer must name, against real
   Haiku, run by hand, never in the suite; and the sandbox checks.
