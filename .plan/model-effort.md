# Model and effort per session (design)

Issue #66, task T78. Decided (Q49 to Q54, Doug's answers quoted under "Decisions").

## Intent

Doug: "No control over models and effort - need to be able to set them when creating new
session at least, and would be good to be able to change them for an active one."

Every session states its model and effort. The person picks both when creating a session,
can change them on a session running in the wheelhouse, and the choice survives Relaunch,
Restore and a hand-over to a shell tab.

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

**The 1M window needs no `[1m]`** (checked 11 Oct 2026 for Q51). The context-windows doc:
"For every model with a 1M-token context window, 1M is the default: you don't need a beta
header, and long-context requests are billed at standard pricing, except on Claude Haiku
5.5, where prompts over 100,000 tokens cost more." The pricing doc: "Claude 4.6 and later
models (except Claude Haiku 5.5) ... include the full 1M token context window at standard
pricing." The binary agrees: `claude -p --model claude-opus-5-5` and
`--model 'claude-opus-5-5[1m]'` both report `contextWindow: 1000000` and canonical model
`claude-opus-5-5`; so do Sonnet 5.5, Haiku 5.5 and Fable 5.1 by their plain ids. So
`opus[1m]` is not offered.

## Decisions

- **Q49, the column:** "yes, b". A pinned full id (`claude-opus-5-5`), so a relaunch never
  changes model mid-task. A new model is a table update.
- **Q50, the default:** "actually, let's dial it down to Sonnet/High". Sonnet 5.5 at high,
  written to the session's columns.
- **Q51, which models:** option b: Opus 5.5, Sonnet 5.5, Haiku 5.5 and Fable 5.1. No
  `opus[1m]` (see above).
- **Q52, tabs:** "c, don't offer it for tabs. No value in leading the user to believe we
  have some sort of mechanism that we don't." And: "Changing effort does not invalidate
  cache. Changing model, does. Worth warning the user of an SDK session when they change
  model that they'll lose cache, but, no such warning required for effort. Suggests to me
  that we could provide effort as a slider in the stats pane, which is appropriate for a
  setting that can be adjusted without side effects."
- **Q53, the session list:** "c - because I don't want clutter". Nothing there; the stats
  pane only.
- **Q54, effort levels:** "I think list all available efforts, but set defaults as your
  option b". Every model offers low, medium, high, xhigh and max. Defaults by tier: Opus
  high, Sonnet high, Haiku xhigh, and Fable high.

## The table

`models.py` holds the choices: each model's pinned id, display name and default effort,
and the five effort levels. The dialog, the hub, the store's checks and `GET /models` all
read it, not a live `initialize`: the dialog opens before any process exists.

| Model | Id | Default effort |
|---|---|---|
| Opus 5.5 | `claude-opus-5-5` | high |
| Sonnet 5.5 (the default) | `claude-sonnet-5-5` | high |
| Haiku 5.5 | `claude-haiku-5-5` | xhigh |
| Fable 5.1 | `claude-fable-5-1` | high |

## New session dialog

Two selects on one row under the brief: Model (Sonnet 5.5 first chosen) and Effort (the
model's default). Picking another model puts Effort at that model's default; Effort can
then be set to any level. The tab checkbox stays as it is: a tab takes the same two fields
at launch, as `--model` and `--effort`.

## Persistence and launch

New columns on `sessions`, via `ADDED_COLUMNS`:

- `model`, `effort`: what the person chose. The store refuses a value outside the table.
- `applied_model`, `applied_effort`: what a session's host last confirmed is in force
  (below). Tabs leave them NULL.

Every launch path passes both flags when the columns are set, because a resume drops a
live effort change (probed above): `Host.options()` sets `model=` and `effort=` for a
host, and `launch.claude_argv()` appends `--model` and `--effort` for a tab, so a new
session, Restore, Relaunch, Adopt's tab and the shell hand-over all start as chosen.
Sessions from before this, and adopted ones, have NULL columns and launch with neither
flag, as they always have; setting one from the wheelhouse fills it from then on.

Older code coexists through the editable install: it never names the new columns, so its
inserts and `SELECT *` reads are unaffected, and an older host simply doesn't apply a live
change (the stats header then says it applies on relaunch).

## Changing them on a session run in the wheelhouse

Only for a session run by a host (runner sdk), never a tab. The wheelhouse writes the
columns; the host, which polls the session's row each second, reconciles:

1. `set_model(model)` when the model column differs from what it applied.
2. `apply_flag_settings {"settings": {"effortLevel": effort}}` when the effort column
   differs, the level first checked against the table, since Claude Code accepts an
   unknown level silently. The Python SDK has no method for it: the host sends it through
   the SDK's control-request path, and a test pinned to the SDK's internals fails loudly if
   an upgrade moves them.
3. `get_settings`, whose `applied` model and effort are written to `applied_model` and
   `applied_effort`.

If Claude Code refuses (an unknown model, say) or confirms something else, the columns go
back to what is in force and the activity line says why. At connect the host writes the
values it launched with to the applied columns before it connects (so a starting session
never shows as pending), puts back what was there if the connect fails, then confirms them
with `get_settings`. It takes a change as in force only once those writes have landed: a
locked database leaves it to apply and write the change again on the next poll. A
put-back only replaces the value it sent, never a newer choice made while it was out.
A model change made while a turn runs is left to Claude Code's own handling.

**Model: a "Model" action**, a lifecycle button beside End (key M), shown for a session
running in a host; an item opened full screen has none, and M there says so. It opens a
list of the four models with the current one highlighted and a warning: changing model
drops the prompt cache, so the session's next request (inside the running turn, if one
runs) re-reads the whole conversation at the full input price. Effort carries over a
model switch (probed above), so the action leaves it as it is.

**Effort: a slider in the stats pane**, on the session in context when it runs in a host:
a row "effort  low · medium · high · xhigh · max" with the current level lit. It applies
live with no warning and no confirm, being free of side effects. Keyboard: Tab reaches the
stats pane (after the inbox's own stops) while it shows the slider; Left and Right move a
level, Home and End go to the ends. A click on a level picks it.

## Tabs

No live change is offered for tabs: the wheelhouse cannot type into one, and an action that
only stores a value for the next launch would suggest a mechanism we don't have. A tab
still gets `--model` and `--effort` at launch from the New session dialog, and the person
can type `/model` or `/effort` in it. The stats header shows what the transcript says it
runs (each assistant record carries `requestedModel` and `effort`).

Carried (Q56, option A): a `/model` or `/effort` typed in a tab is copied back to the
columns (`Hub.copy_back`, on each read of the transcript), so the next Relaunch, Restore or
return from a shell tab launches with it. A tab session's columns take whatever its
transcript's latest values are. In a host's shell hand-over, only a change the transcript
shows while the wheelhouse watches is copied, so a choice made in the wheelhouse meanwhile
stands. A suffixed id (`claude-opus-5-5[1m]`) is stored as its pinned id, the same model
with the same 1M window (`models.pinned`).

## What the stats pane shows

The header gains the effort: "wheelhouse · Sonnet 5.5 · high · 1M window". For a hosted
session the model and effort are the columns' (what it is set to), and while the host has
not confirmed them yet the line under the slider says "applying…"; with an older host that
never will, "on relaunch: old host" (or "on relaunch", where the pane is narrow). For a tab
they are what its transcript says it runs (the latest response's `requestedModel` and
`effort`), else its columns until the transcript has any; where a hosted session's columns
are NULL, the transcript's.

Where the pane is too narrow for every level, the slider shows the current one between
arrows (`effort  ◂  high  ▸`, or `eff ◂ …` narrower still): a click on an arrow steps it,
and the keys work as before. The title gives up the window, then the name, then the effort
before the model; a tab's effort the title can't hold goes on a line of its own.

The window is 1M for every model in the table (Haiku 5.5 and Fable 5.1 included), 200k for
older models such as Haiku 4.5.

The session list is unchanged (Q53).

## Daemon API

Every route below goes through the existing guards: the Host header, the Origin check and
the Idempotency-Key on every POST.

- `POST /sessions` accepts optional `model` and `effort`; omitted, the defaults (Sonnet
  5.5, then the model's default effort). A value outside the table is a 400 naming the
  allowed ones.
- `POST /sessions/{sid}/model {model}` and `POST /sessions/{sid}/effort {effort}`: write
  the column, applied live by the session's host. A tab session is refused (not ok, with
  why), as in the TUI. 400 on a value outside the table.
- `GET /sessions` rows carry the columns (`model`, `effort`, `applied_model`,
  `applied_effort`) and `running_model` and `running_effort`, the transcript's latest.
- `GET /models`: the table, so a remote client builds the same selects.

`.plan/remote.md`'s API sketch gets the same lines.
