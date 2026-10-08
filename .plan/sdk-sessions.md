# SDK-owned sessions (design)

Run each session through the Claude Agent SDK instead of an interactive Claude Code tab.
The wheelhouse becomes the place you talk to sessions, and a terminal tab opens only when
you ask for one. Fewer tabs, one-click compaction, and adopt, restore and relaunch all
become "start a host with `resume=<id>`".

Evidence is marked **verified** (spike run on 2026-10-08, scripts and log in the session
scratchpad under `sdk-spike/`, total cost $0.36) or **inferred**.

## 1. Billing and policy: the deciding question

**Technically it runs on the subscription login. Verified.** With no `ANTHROPIC_API_KEY`
in the environment, `ClaudeSDKClient` (claude-agent-sdk 0.2.164, bundled CLI 2.1.292)
reported `apiKeySource: none` in its init message and answered normally on the logged-in
Max account. The SDK drives the Claude Code binary, which uses whatever login it finds.

**Policy is a different matter.** The sources:

- Agent SDK overview: "Unless previously approved, Anthropic does not allow third party
  developers to offer claude.ai login or rate limits for their products, including agents
  built on the Claude Agent SDK. Use the API key authentication methods described in the
  Quickstart instead." (code.claude.com/docs/en/agent-sdk/overview)
- Legal and compliance: "Advertised usage limits for Pro and Max plans assume ordinary,
  individual usage of Claude Code and the Agent SDK." And: "Developers building products
  or services that interact with Claude's capabilities, including those using the Agent
  SDK, should use API key authentication ... Anthropic does not permit third-party
  developers to offer Claude.ai login into their own applications, or to route requests
  through Free, Pro, or Max plan credentials on behalf of their users." But also: it does
  not "prevent an end user from signing in to the unmodified Claude Code binary with
  their own Claude subscription". (code.claude.com/docs/en/legal-and-compliance)

Reading (inferred, not legal advice):

- **Doug's own use** reads as "ordinary, individual usage of ... the Agent SDK": his tool,
  his machine, his login. Fine.
- **Beta testers on their own subscriptions** is the grey zone. The wheelhouse never
  touches credentials and runs the unmodified binary, which argues for it; but it is a
  product built on the SDK offering subscription use, which is exactly what the overview
  note names. The interactive-tab design has no such question: that is plainly a user
  running Claude Code.

So this is a go for Doug and an open question for beta. See open question 1.

## 2. Permission prompts and Claude's own questions

Both arrive at the `can_use_tool` callback. Verified: a Bash `touch` in `default` mode
called the callback with `{"command": "touch spike.txt", ...}` and our deny reached Claude.
Docs: "The callback can stay pending indefinitely. Execution remains paused until your
callback returns." (agent-sdk/user-input)

- **Tool approval** becomes a new item kind, permission (`P`): the command or diff in the
  body, buttons Allow, Allow always (echoes `context.suggestions` as `updated_permissions`,
  written to `.claude/settings.local.json`), Deny with an optional message. Deny with a
  message doubles as "do this instead".
- **AskUserQuestion** becomes a native question item with the options as buttons and
  multi-select where `multiSelect` is set. The answer goes back as
  `updated_input={"questions": ..., "answers": {question: label}}`. This finally gives us
  Claude's real questions in the inbox, not the ones it remembered to post.
- **Plan approval** (ExitPlanMode) inferred to arrive the same way; check in phase 1.
- If the host restarts while a prompt is pending, the callback is lost. The documented fix
  is a PreToolUse hook returning `defer`, so the call resumes from the persisted session.

## 3. Escape hatch to a terminal

Verified: a session created by the SDK resumes in the plain CLI. `claude --resume <id>`
on the spike's session answered from its history with the same session id.

Flow: you press Shell on a session. The host interrupts or waits for the turn to end,
disconnects, marks the session `in shell` and exits. The wheelhouse opens a Windows
Terminal tab running `claude --resume <id>` (the existing `open_tab` path). Liveness
already finds that process by its session id; when it exits, the wheelhouse starts a host
with `resume=<id>` again and the session is back in the inbox.

The rule is one process per session at a time. The docs don't say what happens if two
attach (inferred: both append to one JSONL and the history forks). The host must be gone
before the tab opens, and the TUI must not start a host while a tab process is alive.
Adopt already refuses a running session (`adopt.StillRunning`); the same check applies.

## 4. Parity with interactive sessions

Verified from the spike's init message, with default `setting_sources`:

| Feature | Under the SDK |
| --- | --- |
| CLAUDE.md, rules, settings | Loaded (user, project, local) |
| Plugins | All 24 of Doug's enabled plugins loaded |
| Skills, agents | 99 skills, 7 agents |
| MCP servers | User servers and claude.ai connectors connected |
| Hooks | Run. Doug's PreCompact hook blocked the first `/compact`, exactly as it would in a tab |
| Slash commands | 141 listed. `/compact <instructions>` sent as a prompt compacted the session: `compact_boundary`, trigger `manual`, 43,380 tokens down to 1,226 |
| Transcript | Written to the normal `~/.claude/projects/<dir>/<id>.jsonl`, so the conversation pane and stats pane keep working |
| Context size | `client.get_context_usage()` returns the `/context` breakdown directly |
| Monitor tool | Present, but no longer needed (below) |

The protocol still goes in through `system_prompt={"type": "preset", "preset":
"claude_code", "append": protocol}`. Model is an option and can change mid-session with
`set_model`. The options also take a `session_id`, so the wheelhouse can pick the id
before launch.

**The monitor goes away.** The host owns the session's input, so an answer is a real user
turn, sent with `client.query()`. Streaming mode queues messages sent mid-turn and
processes them in order ("Queued messages: send multiple messages that process
sequentially, with ability to interrupt", agent-sdk/streaming-vs-single-mode). No
480-character notification limit, no `get_input` pointer, no join notice. The MCP server
stays: sessions still post items through it.

What doesn't come across (inferred): Claude Code's own screen. Interactive pickers
(`/model` menu, `/resume` list, `/config`), the status line, the todo panel and Esc Esc
rewind need their own wheelhouse equivalents or the Shell escape hatch. The init message
lists `terminal_slash_commands` separately, which looks like exactly that list.

## 5. Process model

One small host process per session, `claude-wheelhouse host <sid>`, started detached
(`setsid`) so closing or restarting the TUI never kills a session. Each host owns one
`ClaudeSDKClient` and talks to the rest only through SQLite, like everything else today:

- it reads that session's dispatched messages and sends them as turns
- it writes permission and question items from `can_use_tool` and waits for the answer row
- it records state changes (running, waiting, compacting, in shell, exited)

All sessions inside the TUI process is simpler but fails the "TUI restart must not kill
sessions" test, so no.

- **Restore after a reboot:** start a host per session with `resume=<id>`.
- **Adopt:** the same, for any session id found on disk, once its interactive process has
  exited.
- **Relaunch after an upgrade:** stop the host, start a new one. The code-version stamp
  stays.

## 6. What changes for the user

- No tab per session. The conversation pane is where you read and type; it already
  renders the transcript. With `include_partial_messages` it can stream text as it's
  written. Whether to stream is open question 4.
- The send box sends real turns. Queued and Immediate modes keep their meaning: Immediate
  goes straight to `client.query()`; Queued holds until Ctrl+S.
- An Interrupt button (and Esc in the conversation) calls `client.interrupt()`.
- Compact becomes one click. The session is asked what to keep (which also satisfies a
  PreCompact hook like Doug's that wants a transcript saved first), then the host sends
  `/compact <its notes>` and reports the token drop from `compact_boundary`.
- Shell opens the real Claude Code in a tab for anything the wheelhouse can't do.
- `protocol.md` shrinks: the parts about notifications, pointers, `get_input` and
  answering in the tab go. Items, refs, `reply` and decisions stay.

## 7. Migration

1. **Host behind a flag.** `claude-wheelhouse host`, the launch option "Run in wheelhouse"
   alongside "Open a tab" (default stays tab). Permission (`P`) items, AskUserQuestion as
   question items, interrupt, send box wired to turns. Tests use a fake client.
2. **Shell and back, and Compact.** The hand-off in section 3, the Compact button,
   context usage from `get_context_usage()`.
3. **Default to SDK.** Restore and Adopt start hosts; monitor and join notice kept only
   for tab sessions; protocol trimmed for SDK sessions.
4. **Tutorial** on the new flow. It gets simpler: no tab to explain.

Risks:

- Policy (section 1) for anyone but Doug.
- Two Claude Code binaries: the SDK bundles its own CLI (2.1.292 against 2.1.294
  installed today). Setting `cli_path` to the user's `claude` keeps one version.
- A lost pending prompt if a host dies mid-approval, until `defer` is in place.
- Feature gaps against the interactive screen will show up in use; Shell covers them.
- Cost of the rewrite: `launch.py` and `monitor.py` (335 lines) give way to a host of
  similar size, plus the permission item kind and the send path in `tui.py`.

## Open questions for Doug

1. **Beta testers and subscription login.** The SDK route is fine for Doug's own use, but
   Anthropic's docs say third-party products built on the SDK must not offer claude.ai
   login without approval. Options: (a) ask Anthropic (Two Inc has a commercial
   relationship) before beta; (b) beta testers run SDK mode on an API key and pay per
   token; (c) ship beta in tab mode and keep SDK mode for Doug until (a) is answered.
   This decides whether SDK mode can ever be the default for others.
2. **Default launch mode.** Once built, should new sessions run in the wheelhouse by
   default with Shell as the escape hatch, or should launch ask each time? Default
   changes what a new user sees first.
3. **Permission prompt volume.** In `default` permission mode every unapproved tool call
   becomes a `P` item, which could flood the inbox. Options: start SDK sessions in `auto`
   mode (the classifier approves routine calls, only escalations reach the inbox), or
   `default` with Allow always learning rules as you go. `auto` means fewer interruptions
   and more trust in the classifier.
4. **Live streaming.** Show Claude's text as it's generated (`include_partial_messages`,
   more repaints, the pane we only just made fast) or only completed messages (what the
   pane does now from the transcript)?

## As built (2026-10-08, issue #40)

Phases 1 and 2 are built and run on PR #17. Doug's call on building it: "nah it's GO BIG OR
GO HOME time! Let's go straight to build out. This is a prototype and the bar for
experimentation is low".

**Verified live** on Doug's Max login, with Sonnet for the test sessions:

- A new session runs in a host and answers its brief. The init records `permissionMode:
  auto`, the same as Doug's tab sessions, which get it from `defaultMode` in
  `~/.claude/settings.json` (the tab launch passes no mode flag either).
- AskUserQuestion became question item Q1 with its options. A message on Q1 went back as
  the answer, and the session carried on and posted a task through the MCP server.
- A project `ask` rule (`Bash(touch:*)`) forced a prompt in auto mode. It became P1,
  Allow ran the command, and a message on P2 denied the next one with that text.
- Compact: the session was asked for its notes, and then `/compact <notes>`. 48k down to 8k
  on one run (the compact_boundary's tokens), and "compacted 51k → 39k" on another (the
  /context total, system prompt included).
- Restore of a dead host resumed the session, and the join turn had it post its open
  questions.
- Shell: the host let go, a tab opened with `claude --resume`, and when the tab's Claude
  exited the host took the session back under its own pid. A message afterwards was
  answered.
- End through the request path: the session called `end_session`, its row went, and the
  host exited. No stray processes.

**Decisions made while Doug was away:**

| Decision | Alternative | Why |
| --- | --- | --- |
| New sessions run in a host by default (`DEFAULT_RUNNER = "sdk"`); a box in New session opens a tab instead; `WHEELHOUSE_RUNNER=tab` restores the old default | Keep tabs the default behind a flag | The whole point of the change, and it works end to end; one env var reverts it |
| Sessions created before this stay tabs (runner NULL means tab) | Migrate them | Their running tabs would otherwise be misread |
| The host passes no permission mode | Pass `auto` explicitly | Never override the user's settings: SDK sessions already start in the user's `defaultMode`, verified |
| `cli_path` is the user's `claude` | The SDK's bundled CLI | One Claude Code version for tabs and hosts |
| The host always renders the system prompt fresh (`--system-prompt-snapshot off`), and a hosted session opened in a shell tab does too | Only for adopted sessions | Its recorded prompt would be the other runner's protocol |
| No wheelhouse plugin in a host (no monitor, no `/wheelhouse` skill) | Load it | The monitor would deliver the person's messages a second time; End and Park come as turns, which `end_session` and `park_session` answer |
| Permission items are their own kind (`P`, open / allowed / denied); sessions can't post them | Reuse questions | They have their own answers (Allow, Always, Deny) and must never be posted by a session |
| A message typed on a permission item denies the call with that text | Ignore it | It's the natural "do this instead" |
| One AskUserQuestion call is one question item; several questions take `n: answer` parts | One item per question | One tool call waits on one answer |
| Closing an AskUserQuestion item unanswered denies the call | Leave it waiting | The callback would otherwise wait forever |
| A permission left open by a host that died is denied when the host restarts, with a note that Claude Code will ask again | The PreToolUse `defer` hook from section 2 | Simpler for a prototype; `defer` is the follow-up if lost approvals bite |
| A permission or dialog question Claude Code withdraws (Interrupt, Shell) closes with a note; a message typed on it afterwards becomes an ordinary turn | Leave it open | Its buttons would do nothing, and the message would be lost |
| The host starts through `setsid --fork`, so it is init's child, and a zombie (state Z) counts as dead | Reap it from the TUI | An exited host the TUI never waited on stayed a zombie that still read as alive, so Restore and Relaunch refused it |
| In a hosted session `get_input()` takes nothing and says why | Leave it | The person's messages must arrive as user turns, never as a tool result |
| Completed messages in the pane, plus a live activity line in the bar; no partial-text streaming | Stream partial text | The pane already renders the transcript; streaming is a later nicety |
| Compact asks for notes inside `<keep id="<nonce>">…</keep>`, a fresh id per press, and sends `/compact` once a reply has carried that block, whichever turn it arrives in; only Interrupt, the host stopping or 10 minutes without notes abandon it (the last says so) | Count turns between the ask and the reply | Counting broke twice in review: Claude Code starts turns by itself, and an error in an earlier turn ended the wait. Matching on the id needs no count |
| On a shell hand-back the host lets go silently once the shell flag is cleared (a Restore's host clears it as it starts) or another process holds the registration; only the registered owner writes the activity line | Take the session back whenever it reads dead | A Restore in the 2-second gap left the old host lurking, and its exit wrote "stopped" over the new owner's line |
| A message the host has passed on is remembered by id, so a claim retaken after a failed confirm is confirmed, never sent again; an answer handed to a call Claude Code then withdrew goes out as the next turn | Rely on the claim timeout | A locked database at the wrong moment would otherwise duplicate a turn or lose an answer |
| The bar carries Allow, Always, Deny, Interrupt, Compact and Shell, shown only when they apply; Compact and Shell confirm first, Interrupt doesn't | Keys, or a new pane | The bar is on every screen and already context-aware; no new key clashes |
| The monitor and join notice stay for tab sessions | Remove them | Tabs remain supported, and a shell tab uses them |

**Not done or not verified:**

- Beta testers on their own logins: open question 1 still stands. Nothing here changes that.
- ExitPlanMode arrives as a permission item with the plan as its body (the code expects it,
  but it hasn't been seen live).
- Whether Windows Terminal closes the shell tab when Claude is killed rather than `/exit`ed:
  the test killed it and couldn't see the tab.
- Interrupt reached Claude Code (the turn stopped), but the test's tool call had been moved
  to the background by Doug's own hooks, so the interrupt of a running foreground tool is
  unseen.
- `get_context_usage()` is stored on the session (`context_tokens`, `context_max`) but the
  stats pane still reads the transcript.
- Live partial-text streaming, a key for Interrupt, and a model picker are left for later.
