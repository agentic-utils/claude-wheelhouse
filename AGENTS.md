# AGENTS.md

How to work on claude-wheelhouse. The README describes what the app does; `.plan/` holds
the designs. Read the matching `.plan/` doc before changing a feature it covers.

## Layout

`claude_wheelhouse/`:

- `cli.py`: the `claude-wheelhouse` entry point and its subcommands (`tui`, the default;
  `tutorial`, `run`, `host`, `mcp`, `monitor`, `protocol`, `daemon`).
- `store.py`: the SQLite store, the only source of truth. Schema, added columns,
  triggers, `PROTOCOL_VERSION`. Every public write is its own committed transaction.
- `hub.py`: what the wheelhouse knows and does, whatever shows it. Owns liveness and
  Relaunch. Never imports Textual: it tells surfaces what happened as events.
- `tui.py`: the Textual app. Reads and acts through the hub and the store.
- `daemon.py`: the hub served over HTTP (`.plan/remote.md`); no client uses it yet.
- `host.py`: a session run through the Claude Agent SDK, one detached process per session.
  Experimental, and off unless `features.sdk_enabled()`: read `features.py` first.
- `features.py`: SDK mode's switch, `sdk_enabled()`, and the plan usage mode,
  `usage_mode()` (`--usage-mode`, or `usage-mode` in a `.env`).
- `launch.py`: Windows Terminal tabs (`wt.exe` through `cmd.exe`), the `claude` command
  line, and the protocol text injected into a session.
- `mcp_server.py`: the per-session `wheelhouse` MCP server. Each tool's docstring is its
  description to Claude, so it is user-facing text.
- `monitor.py` and `plugin/`: the tab session's plugin monitor and `/wheelhouse` skill.
- `adopt.py`: adopting a session the wheelhouse didn't launch.
- `liveness.py`: whether a session is alive, by its Claude process, never the heartbeat.
- `subagents.py`: A items for subagents, from the files Claude Code writes.
- `transcript.py`: a session's conversation, read from its Claude Code transcript.
- `stats.py`, `stats_pane.py`: token stats and charts; the pane is a module (`api.py`).
- `plan_usage.py`: the account's session and weekly usage, from `/usage` asked of
  Claude Code every 5 minutes (a one-shot `claude -p` by default, or one kept running with
  `--usage-mode persistent`), and, in SDK mode only, from SDK sessions' rate_limit_events.
- `api.py`: the module protocol (entry point group `claude_wheelhouse.modules`). Bump
  `WHEELHOUSE_API` on any breaking change to it.
- `models.py`: the models and effort levels offered. A new model is a row here.
- `tutorial.py`: the new-user tutorial: its session brief and checklist steps.
- `splitter.py`, `knurl.py`, `emoji.py`, `unsent.py`: TUI pieces (dividers, scrollbars,
  emoji shortcodes, unsent text).
- `protocol.md`, `protocol_sdk.md`, `protocol_tab.md`, `protocol_decisions.md`: what a
  launched session is told. `claude-wheelhouse protocol` prints it as a session gets it.

`tests/` mirrors the modules (`test_<module>.py`); `conftest.py` holds the shared fixtures.

## Run

- `make install` (`uv sync --reinstall-package claude-wheelhouse`), then `make run` or
  `make tutorial` (`claude-wheelhouse tutorial`). `make` lists the targets. Python 3.12, managed by uv.
- The install is editable: `.venv` imports this checkout. In another clone or worktree,
  that venv's `pytest` script imports the main checkout, not yours. Run
  `python -m pytest` from your clone's root, or set `PYTHONPATH=<your clone>`.

## Test

- Run `python -m pytest` (or `make test`). `pyproject.toml` adds `-n auto`; `-n 0` runs
  serially.
- Inside a wheelhouse session, first `unset WHEELHOUSE_DB WHEELHOUSE_SESSION_ID WHEELHOUSE_TOKEN`.
  `conftest.py` refuses to run (`test isolation: ...`) while the database or its state
  directory resolves under `~/.local/state/claude-wheelhouse`. Don't weaken that guard.
- The autouse `isolated_db` fixture gives every test its own database. Other autouse
  fixtures turn SDK mode off (`sdk_off`; a test of it takes `sdk_on`), run sessions as
  tabs, stub the plan usage poll (no test runs the real `claude`: `test_plan_usage.py`
  uses a fake, and `WHEELHOUSE_CLAUDE` points every other probe, a test-started daemon's
  too, at one that isn't there), the tutorial offer and the dwell timer. A test that needs a real host or the shipped behaviour opts in explicitly.
- A test that depends on the app's timers (Sink moves, the dwell, `tick`, `animate`)
  uses the `clock` fixture and moves time with `clock.advance`. Never sleep on wall-clock
  time. The fixture fails on any app timer it doesn't know: add it to the fixture.
- Prefer parametrised tests: one case per line, ending in a description used in the
  assertion message.
- `test_host` pins Agent SDK internals (`Host.control`): rerun it after bumping
  `claude-agent-sdk`.

## The live database

Never open, write, migrate or delete `~/.local/state/claude-wheelhouse/wheelhouse.db`,
or anything else under `~/.local/state/claude-wheelhouse/`. It is the person's running
wheelhouse. Point experiments at a scratch path with `WHEELHOUSE_DB`.

## Compatibility with running sessions

Sessions keep running the code they started with, against the same database.

- Bump `PROTOCOL_VERSION` in `store.py` when a session on older code would mishandle the
  store or miss new behaviour (a protocol text change it must follow, a new item kind,
  changed tool semantics). Add a line to the comment above it saying what the bump is
  for. The wheelhouse then shows older sessions as needing a relaunch.
- Add columns through `ADDED_COLUMNS`; never drop or rename one older code still reads.
- Triggers are versioned: their names carry `TRIGGERS_VERSION`. To change a trigger's
  body, bump `TRIGGERS_VERSION`; opening drops older versions and keeps newer ones. A
  trigger must stay harmless when older and newer versions fire together. To retire one,
  match it in a `RETIRED_*` pattern so every version is dropped on open.

## Docs kept in step

Update these in the same commit as the behaviour they describe:

- `protocol*.md`, the MCP tool docstrings and `launch.JOINED_TEXT`: what sessions are told.
- `tutorial.py`: the tutorial session's `BRIEF` and checklist `STEPS`.
- `README.md`: anything a user sees or does.
- The matching `.plan/` doc when a design decision changes.

## Commits

- Subject: `#<issue>/<type> <description>`, for example
  `#16/fix a task posted with alternative or reverse is refused naming that field, not why`.
  The issue must be a real GitHub issue on agentic-utils/claude-wheelhouse; open one first
  if none fits. Types: feat, fix, docs, test, refactor, perf, chore, build, revert.
- The description is plain lower-case prose saying what now holds, not what was done.
- Branches: `doug/<issue>-<slug>`.

## Writing

No em-dashes in prose, code comments or UI text: use commas, colons, parentheses or a
new sentence. User-facing text is British English.
