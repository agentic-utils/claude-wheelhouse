# Model and effort per session (design)

Issue #66, task T78. Design only.

## Intent

Doug: "No control over models and effort - need to be able to set them when creating new
session at least, and would be good to be able to change them for an active one."

Every session states its model and effort. The person picks both when creating a session,
can change them on a running one, and the choice survives Relaunch, Restore and a
hand-over to a shell tab.

## What Claude Code offers (probed 11 Oct 2026, Claude Code 2.1.296, Python SDK 0.2.164)

Probe script and step files: `~/.claude/scratchpad/t78/probe.py`, `steps*.json`. Each run
was a throwaway `claude -p --input-format stream-json --output-format stream-json --verbose`
in a scratch directory, mostly on Haiku; the whole set cost under $0.30.

**Launch flags.** `claude --help`:

```
--effort <level>   Effort level for the current session (low, medium, high, xhigh, max)
--model <model>    Model for the current session. Provide an alias for the latest model
                   (e.g. 'fable', 'opus', or 'sonnet') or a model's full name.
```

`ClaudeAgentOptions` has matching `model` and `effort` fields, so hosts need no extra args.

**Live model change works: `set_model` control request.** The SDK already wraps it
(`ClaudeSDKClient.set_model`). Launched `--model haiku --effort medium`:

```
>> user 'Reply with one word: hi'
  assistant: model=claude-haiku-5-5
>> control {"subtype": "set_model", "model": "sonnet"}
  user echo: <local-command-stdout>Set model to `sonnet (claude-sonnet-5-5)`</local-command-stdout>
  control_response: {"subtype": "success", "request_id": "r3"}
>> user 'Reply with one word: hi'
  init: model=claude-sonnet-5-5
  assistant: model=claude-sonnet-5-5
  result: models=['claude-haiku-5-5', 'claude-sonnet-5-5']
```

The notice line is written to the transcript, so the conversation view can show it.
`"opus[1m]"` resolves to `claude-opus-5-5[1m]`; `null` returns to the default. An unknown id
is refused and leaves the model alone:

```
>> control {"subtype": "set_model", "model": "claude-nonsense-9"}
  control_response: {"subtype": "error", "error": "Model 'claude-nonsense-9' not found", "error_code": "catalog_unknown"}
```

**Live effort change works: `apply_flag_settings` with `effortLevel`.** Not wrapped by the
Python SDK. `get_settings` reports what is in force under `applied`:

```
>> control {"subtype": "get_settings"}
  applied= {"model": "claude-haiku-5-5", "effort": "low", ...}
>> control {"subtype": "apply_flag_settings", "settings": {"effortLevel": "xhigh"}}
  control_response: {"subtype": "success"}
>> control {"subtype": "get_settings"}
  applied= {"model": "claude-haiku-5-5", "effort": "xhigh", ...}
>> control {"subtype": "apply_flag_settings", "settings": {"model": "haiku", "effortLevel": "medium"}}
  applied= {"model": "claude-haiku-5-5", "effort": "medium", ...}
```

An invalid level is accepted silently and ignored (`{"effortLevel": "bogus"}` returned
success, `applied.effort` stayed `medium`), so the host validates first and confirms with
`get_settings` after. Effort carries over a model switch (xhigh stayed xhigh on
haiku to opus). None of these calls touched `~/.claude/settings.json` (mtime unchanged).

**Slash commands as a user turn also work**, at no cost (synthetic reply, `cost=0`):

```
>> user '/model sonnet'   ->  'Set model to `Sonnet 5.5` for this session only'
>> user '/effort high'    ->  'Set effort level to high (this session only): ...'
```

The control requests are the better route for hosts: no turn queued, a typed error.

**Resume does not reliably keep a live change.** Launched sonnet/medium, switched to
haiku/low, took a turn, then resumed with no flags: `applied = haiku, medium`. The model
came back (from the last assistant record), the effort did not. Resumed with
`--model opus --effort high`: `applied = opus, high`. So every launch passes both flags.

**The transcript records both per turn.** Each assistant record carries
`"requestedModel":"claude-haiku-5-5"` and `"effort":"low"`. That is how a tab's changes
become visible.

**Unset effort is per model.** `--model haiku` with no `--effort` ran at `medium`, though
Doug's user settings say `"effortLevel": "high"`. Leaving effort unset is not "Doug's
default".

**Not probed:** a `set_model` sent while a turn is running. The implementation's first
test settles whether it applies to the running turn's next request or waits.

**Catalogue.** The `initialize` response lists models with `supportedEffortLevels`. Today:
`opus` (Opus 5.5), `fable` (Fable 5.1), `sonnet` (Sonnet 5.5), `haiku` (Haiku 5.5), plus
older pinned ids; all five current ones support `low, medium, high, xhigh, max`.

## New session dialog

Two selects under the brief, on one row:

- **Model:** Opus 5.5 (default), Sonnet 5.5, Haiku 5.5, and Fable 5.1 if Q3 says so.
- **Effort:** medium, high (default), xhigh. Low and max are never offered for Opus 5.5
  (Doug's tiers: low benchmarks poorly, max overthinks).

The choices come from a small table in code (alias, display name, effort levels), not from
a live `initialize`: the dialog opens before any process exists. A new model is a one-line
change. The tab checkbox stays as is; both runners take the same fields.

## Persistence

Two new columns, `sessions.model` and `sessions.effort` (TEXT, via `ADDED_COLUMNS`). They
hold what the person chose, not what Claude Code reports. Every launch path passes them:
`Host.options()` sets `model=` and `effort=`; `launch.claude_argv()` appends `--model` and
`--effort` for tabs, adopts, Restore and the shell hand-over. So Relaunch and Restore come
back on the chosen model and effort (the probe shows they would not otherwise).

Sessions created before this, and adopted ones, have NULL columns. On first sight the hub
fills them from the transcript's last `requestedModel` and `effort`, so a relaunch keeps
what the session was running.

## Changing them on a live hosted session

A "Model" action on the session (key and button beside Interrupt and Compact) opens the
same two selects, prefilled. On OK the hub writes the columns. The host already polls the
session row each tick; it keeps `applied = (model, effort)` and, when the columns differ:

1. `set_model(model)` if the model changed; on `catalog_unknown`, put the column back and
   raise a notice with Claude Code's error.
2. `apply_flag_settings {"effortLevel": effort}` if the effort changed. The Python SDK has no
   method for it, so the host sends it through the SDK's control-request path, with a test
   pinned to the SDK version so an upgrade that moves the internals fails loudly.
3. `get_settings`, and record `applied` from its answer.

The person sees the activity line read "model: Sonnet 5.5, high", the
`Set model to ...` line in the conversation, and the stats header change. Nothing else is
interrupted: no relaunch, no lost cache beyond what a model switch costs anyway (a new
model has a cold cache).

This rides on a new path, not `host_command`: that column holds one bare verb, and a
desired-state pair is easier to reconcile than a queued command.

## Changing them on a tab

The wheelhouse cannot type into a tab. Two parts:

- **In the tab:** the person types `/model sonnet` or `/effort xhigh`. The wheelhouse reads
  the next assistant record's `requestedModel` and `effort`, shows them, and copies them to
  the columns so the next launch keeps them. Prefer `/model <name>` to the bare `/model`
  picker: its help text says "Your pick becomes the default for new sessions", which would
  change Doug's global default. (From the binary's strings; interactive mode not probed.)
- **In the wheelhouse:** the same Model action writes the columns and says "applies when
  the tab next opens; type `/model sonnet` in the tab to switch now". See Q4.

## What the list and stats show

- **Stats pane header:** already "name · model · window" from the transcript; it gains the
  effort: "wheelhouse · Sonnet 5.5 · high · 200k window". From the transcript, so tabs and
  hosts show the same truth.
- **Session list:** a short tag only when the session is off the default, e.g. `son·xh`,
  so the usual Opus/high sessions stay quiet. See Q5.
- **Mismatch:** if the transcript's model or effort differs from the columns for a hosted
  session after a turn, the stats header shows both ("Sonnet 5.5 (asked Haiku 5.5)").

## Daemon API

- `POST /sessions` accepts optional `model` and `effort`; unknown values are a 400 naming
  the allowed ones.
- `POST /sessions/{sid}/model {model?, effort?}`: writes the columns; for a host, applied
  live; for a tab, stored for the next launch. 400 on values outside the table.
- `GET /sessions` rows gain `model`, `effort` (the columns) and `running_model`,
  `running_effort` (the transcript's latest).
- `GET /models`: the table, so a remote client builds the same selects.

`.plan/remote.md`'s API sketch gets the same lines in the implementation PR.

## Open questions for Doug

**Q1. Alias or pinned id in the column.**
(a) Alias (`opus`): sessions move to the newest Opus when Claude Code ships one, including
on Relaunch of an old session. (b) Pinned id (`claude-opus-5-5`): a session keeps its model
until changed by hand; new models need a table update. Proposal: (b) for the column, so a
relaunch never silently changes model mid-task, with (a)'s convenience kept by the dialog
always offering the current table.

**Q2. What "default" means.**
(a) Explicit Opus 5.5 at high, written to the columns. (b) Unset, so Claude Code's user
settings decide (today `opus[1m]` and `high`, but Haiku's unset effort ran at medium).
Proposal: (a); it is the only one that survives a relaunch unchanged.

**Q3. Which models to offer.**
(a) Opus, Sonnet, Haiku. (b) Plus Fable 5.1 (your escalation tier: work projects only,
can refuse silently with HTTP 200). (c) Plus the 1M-context Opus (`opus[1m]`, your current
`settings.json` default). Each added option is one table row.

**Q4. Model change on a tab from the wheelhouse.**
(a) Store for the next launch and tell the person to type the command. (b) Offer to close
and reopen the tab on the new model (a relaunch: loses the tab's scrollback, keeps the
conversation). (c) Not offered for tabs; they change it themselves.

**Q5. Session list.**
(a) Tag only when off the default. (b) A column for every session. (c) Nothing; stats pane
only.

**Q6. Effort levels for models other than Opus 5.5.**
Your rule bars low and max for Opus 5.5. (a) Same three levels for every model. (b) Per
your tiers: Haiku gets xhigh as its default (your agent tier runs it at xhigh), others
medium, high, xhigh.
