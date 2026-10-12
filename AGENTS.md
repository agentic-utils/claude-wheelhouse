# AGENTS.md

How to work on claude-wheelhouse. The README describes what the app does; `.plan/` holds
the designs. Read the matching `.plan/` doc before changing a feature it covers.

## Layout

`claude_wheelhouse/`:

- `cli.py`: the `claude-wheelhouse` entry point and its subcommands (`tui`, the default;
  `tutorial`, `run`, `host`, `mcp`, `monitor`, `protocol`, `daemon`, `token`, `open-items`).
  The app takes `--local`, or `--connect HOST:PORT` with `--token-file`; it and `daemon`
  take `--port`, `--usage-mode`, `--keepalive-hours` and `--enable-sdk` (`features.py`);
  `daemon` adds `--address`, `--name` (repeatable) and `--stop`; `tutorial` takes `--yes`.
- `store.py`: the SQLite store, the only source of truth. Schema, added columns,
  triggers, `PROTOCOL_VERSION`. Every public write is its own committed transaction.
- `hub.py`: what the wheelhouse knows and does, whatever shows it. Owns liveness and
  Relaunch. Never imports Textual: it tells surfaces what happened as events.
- `tui.py`: the Textual app. Reads and acts through `self.client` (`client.py`);
  `self.hub` is that same `LocalClient` with `--local`, and None when the app goes
  through a daemon (`client.remote`).
- `daemon.py`: the hub served over HTTP and SSE (`.plan/remote.md`), once per user on
  `127.0.0.1:7428` (or `features.port()`). The app goes through it by default; `--local`
  runs a hub in process.
- `client.py`: the app's way to the hub, `LocalClient` (in process) or `RemoteClient`
  (the daemon). Both answer the same calls with the same values.
- `connect.py`: finding, starting, restarting and stopping (`halt`) the local daemon,
  through `daemon.lock` in the state directory; `--connect` for another machine's.
- `auth.py`: the daemon's token, `daemon.token` (mode 0600) beside the database.
- `fingerprint.py`: a hash of the package's source, so an app on newer code restarts a
  daemon on older code. Bump its `API` when the daemon's routes change incompatibly.
- `host.py`: a session run through the Claude Agent SDK, one detached process per session.
  Experimental, and off unless `features.sdk_enabled()`: read `features.py` first.
- `features.py`: SDK mode's switch, `sdk_enabled()`, the plan usage mode,
  `usage_mode()` (`--usage-mode`, or `usage-mode` in a `.env`), the daemon's port,
  `port()` (`--port`, `WHEELHOUSE_PORT`, or `port` in a `.env`), and how long a quiet
  session is kept warm, `keepalive_hours()` (`--keepalive-hours`, or `keepalive-hours` in a
  `.env`; 8 by default, 0 for off).
- `launch.py`: Windows Terminal tabs (`wt.exe` through `cmd.exe`, in the user's own
  window, `window_name`), the `claude` command line, and the protocol text injected into
  a session.
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

- `make install` (`uv sync --reinstall-package claude-wheelhouse`), then `make run`.
  `make stop` (`claude-wheelhouse daemon --stop`) stops the daemon. `make tutorial` (`claude-wheelhouse tutorial`), with the app and daemon stopped, has the
  next start offer the tutorial again. `make` lists the targets. Python 3.12, managed by uv.
- The install is editable: `.venv` imports this checkout. In another clone or worktree,
  that venv's `pytest` script imports the main checkout, not yours. Run
  `uv run python -m pytest` from your clone's root, or set `PYTHONPATH=<your clone>`.

## Test

- Run `uv run python -m pytest` (or `make test`, which runs the `pytest` script, in the main
  checkout only: see above). `pyproject.toml` adds `-n auto`; `-n 0` runs
  serially.
- Inside a wheelhouse session, first `unset WHEELHOUSE_DB WHEELHOUSE_SESSION_ID WHEELHOUSE_TOKEN`.
  `conftest.py` refuses to run (`test isolation: ...`) while the database or its state
  directory resolves under `~/.local/state/claude-wheelhouse`. Don't weaken that guard.
- The autouse `isolated_db` fixture gives every test its own database. Other autouse
  fixtures turn SDK mode off (`sdk_off`; a test of it takes `sdk_on`), run sessions as
  tabs, stub the plan usage poll (no test runs the real `claude`: `test_plan_usage.py`
  uses a fake, and `WHEELHOUSE_CLAUDE` points every other probe, a test-started daemon's
  too, at one that isn't there), the tutorial offer and the dwell timer. A test that needs a real host or the shipped behaviour opts in explicitly.
- A test marked `both` runs three ways (the `via` fixture): through a hub in process, and
  through a daemon served on the test's database, fetching at each refresh or on the
  client's own threads. Mark a TUI test `both` when its behaviour must hold through the
  daemon too.
- A test that depends on the app's timers (Sink moves, the dwell, `tick`, `animate`)
  uses the `clock` fixture and moves time with `clock.advance`. Never sleep on wall-clock
  time. The fixture fails on any app timer it doesn't know: add it to the fixture.
- Prefer parametrised tests: one case per line, ending in a description used in the
  assertion message.
- `test_host` pins Agent SDK internals (`Host.control`): rerun it after bumping
  `claude-agent-sdk`.

## State

The state directory is the database's: `~/.local/state/claude-wheelhouse/`, or the
directory of `WHEELHOUSE_DB`. It holds `wheelhouse.db` (WAL), `daemon.token`,
`daemon.lock`, `daemon.log`, `launch.log`, the tutorial's scratch directory, and
`hosts/<id>.log`, each SDK host's output (SDK mode, experimental). A module
that keeps files of its own gets it as `api.Context.state_dir`. Environment variables:
`WHEELHOUSE_DB`, `WHEELHOUSE_PORT` (the daemon's port, below `--port` and above `port=` in a
`.env`), `WHEELHOUSE_TOKEN` (`--connect`), `WHEELHOUSE_DECISIONS=0`,
`WHEELHOUSE_SESSION_ID` and `WHEELHOUSE_PYTHON`, which a launched session is given,
`WHEELHOUSE_RUNNER` (`tab` or `sdk`; it never turns SDK mode on by itself), and
`TEXTUAL_COLOR_SYSTEM`, which overrides the truecolour the app assumes under Windows
Terminal (`WT_SESSION` set and `COLORTERM` not: `cli.truecolour`), and
`WHEELHOUSE_CLAUDE`, the `claude` the usage probe runs instead (the tests' missing one).
Read from the system: `SHELL` (each tab runs `claude` through `$SHELL -lic`) and
`WSL_DISTRO_NAME` (the distro a tab opens in).
`make stop` (`claude-wheelhouse daemon --stop`) stops the local daemon; quit the app first,
or it starts another.

## The live database

Never open, write, migrate or delete `~/.local/state/claude-wheelhouse/wheelhouse.db`,
or anything else under `~/.local/state/claude-wheelhouse/`. It is the person's running
wheelhouse. Point experiments at a scratch path with `WHEELHOUSE_DB`, and with it either
`WHEELHOUSE_PORT` (another free port) or `--local`: the scratch app otherwise tries to start
its own daemon on the person's port, which theirs holds. Launching a session from it still
opens a real Windows Terminal tab running Claude Code.

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
- `tui.py`'s `DESCRIBE` (each binding's line in the `?` overlay; a test fails when a binding
  has none), `BUTTONS` (each button's tooltip and overlay line), `SEND_RULES` and `MOUSE`.
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
