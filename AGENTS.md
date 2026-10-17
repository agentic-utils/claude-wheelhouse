# AGENTS.md

How to work on claude-wheelhouse. The README describes what the app does; `.plan/` holds
the designs. Read the matching `.plan/` doc before changing a feature it covers.

## Layout

`claude_wheelhouse/`:

- `cli.py`: the `claude-wheelhouse` entry point and its subcommands (`tui`, the default;
  `tutorial`, `notes`, `run`, `host`, `mcp`, `monitor`, `protocol`, `daemon`, `token`,
  `open-items`).
  The app takes `--local`, or `--connect HOST:PORT` with `--token-file`; it and `daemon`
  take `--port`, `--usage-mode`, `--keepalive-hours` and `--enable-sdk` (`features.py`);
  `daemon` adds `--address`, `--name` (repeatable) and `--stop`; `tutorial` takes `--yes`.
- `store.py`: the SQLite store, the only source of truth. Schema, added columns,
  triggers, `PROTOCOL_VERSION`. Every public write is its own committed transaction.
- `hub.py`: what the wheelhouse knows and does, whatever shows it. Owns liveness, the session states
  and their verbs (`VERBS`, `.plan/session-states.md`), and
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
- `features.py`: every setting, read by one helper, `given()`: the flag, else
  `WHEELHOUSE_<KEY>`, else `KEY=` in the install directory's `.env`
  (`features.INSTALL_DIR`, the checkout, never the cwd), else
  `~/.config/claude-wheelhouse/.env`, else the default. Keys are upper case only, flags
  lower case. The settings: SDK mode's switch, `sdk_enabled()` (`ENABLE_SDK`), the plan
  usage mode, `usage_mode()` (`USAGE_MODE`), the daemon's port, `port()` (`PORT`), and how
  long a quiet session is kept warm, `keepalive_hours()` (`KEEPALIVE_HOURS`; 8 by default,
  0 for off), and the app's animation rate, `animation_fps()` (`ANIMATION_FPS`; 10 by
  default, 0 for off; `WheelhouseApp.pace` rests it while unfocused with nothing busy). The README's Settings table lists them.
- `launch.py`: Windows Terminal tabs (`wt.exe` through `cmd.exe`, in the user's own
  window, `window_name`), the `claude` command line, and the protocol text injected into
  a session. Every launch passes `clean_env()`, which drops a parent Claude Code
  session's `CLAUDE*` variables: a claude started with them writes no transcript.
- `mcp_server.py`: the per-session `wheelhouse` MCP server. Each tool's docstring is its
  description to Claude, so it is user-facing text.
- `monitor.py` and `plugin/`: the tab session's plugin monitor and `/wheelhouse` skill.
- `adopt.py`: adopting a session the wheelhouse didn't launch.
- `fork.py`: forking a session (`Hub.fork`): its transcript copied by the Agent SDK's
  `fork_session`, a new worktree when the parent's in a linked one, and what the fork and
  its parent are told.
- `liveness.py`: whether a session is alive, by its Claude process, never the heartbeat;
  and stopping one (`stop`: SIGTERM, guarded by pid, start time and boot id, then SIGKILL to
  its process group after `KILL_AFTER`). Tests never signal a real process: stub `os.kill`
  (conftest stubs `os.getpgid` and `os.killpg`).
- `subagents.py`: A items for subagents, from the files Claude Code writes.
- `transcript.py`: a session's conversation, read from its Claude Code transcript.
- `transcript_usage.py`: a session's token use, read from its transcripts (`parse`,
  `UsageFollower`, `follower`, `Snapshot`, `current`, `carry`), for the session list's
  context bars and cache warmth, and for the stats module through `api.py`'s re-exports.
- `plan_usage.py`: the account's session and weekly usage, from `/usage` asked of
  Claude Code every 5 minutes (a one-shot `claude -p` by default, or one kept running with
  `--usage-mode persistent`), and, in SDK mode only, from SDK sessions' rate_limit_events.
- `api.py`: the module protocol (entry point group `claude_wheelhouse.modules`), and all a
  module imports of the wheelhouse: what it may use is re-exported there. Bump
  `WHEELHOUSE_API` on any breaking change to it. `SLOTS` names the six places a pane goes
  (`tui.slot`, `.plan/modules.md`): an empty one collapses (`WheelhouseApp.collapse`). A pane
  dragged by its title swaps slots with another (`tui.Slot`, `WheelhouseApp.swap`), kept as
  `layout.slots` and placed at start by `tui.arrange`.
- `models.py`: the models and effort levels offered. A new model is a row here.
- `tutorial.py`: the new-user tutorial: its session brief and checklist steps.
- `whatsnew.py`: the tour's dialogs (`DIALOGS`) and every feature a user can see
  (`FEATURES`), each registered against a dialog or as advanced: what a top-up offers
  (`pending`, by the store's `tour_seen` record, never by version) and the release notes
  (`notes`, `claude-wheelhouse notes`). `make stamp VERSION=...` gives `UNRELEASED`
  features their release (`.plan/tutorial-overhaul.md`).
- `callout.py`: the tour's callout, a box painted beside the pane a tour dialog describes,
  highlighting it and following it through a resize or a swap (`place`, a pure function;
  `PANES`, the one lookup from a tour pane id to its widget).
- `splitter.py`, `knurl.py`, `emoji.py`, `unsent.py`: TUI pieces (dividers, scrollbars,
  emoji shortcodes, unsent text).
- `protocol.md`, `protocol_sdk.md`, `protocol_tab.md`, `protocol_decisions.md`: what a
  launched session is told. `claude-wheelhouse protocol` prints it as a session gets it.

`modules/` holds the modules (`.plan/modules.md`), each its own distribution, a uv workspace
member and a dependency group of the same name (`pyproject.toml`): `modules/stats` is
`claude-wheelhouse-stats`, the stats pane (`pane.py`) and its drawing, charts and gauges
(`stats.py`), with its tests in `modules/stats/tests`. Core never imports a module: the app
finds them by entry point, and must run with none installed (`test_api.py`).

`tests/` mirrors the modules (`test_<module>.py`); `conftest.py` holds the shared fixtures,
and `transcripts.py` the transcripts the core's and the stats module's tests write.

## Run

- `make install` (installs uv if it's missing, then `uv sync --reinstall-package claude-wheelhouse`
  and each module's package, with every module in `modules/` or those `WITH="..."` names,
  `--no-group` for the rest, and writes the modules installed to
  `.venv/.wheelhouse-installed`), then `make run` (`ARGS="..."` passes args;
  it runs `make install` first, with the stamp's modules, when that stamp is missing or
  older than `pyproject.toml`, `uv.lock` or a module's `pyproject.toml`; `none` in the stamp
  for none, and an empty one, an install from before `modules/`, for all). Every target
  depends on the stamp and runs `uv run --no-sync`, so a module left out stays out; `uv run`
  alone syncs the default groups, every module included. The stats module's tests share
  core's `tests/transcripts.py` (pytest's `pythonpath = ["."]`) and skip without it installed. `make args` (`claude-wheelhouse args`, hidden from `--help`, `cli.args_text`)
  lists the app's flags with their settings' names (`features.SETTINGS`).
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
`WHEELHOUSE_DB`, `WHEELHOUSE_PORT` and the other settings' variables (`features.given`),
`WHEELHOUSE_TOKEN` (`--connect`), `WHEELHOUSE_DECISIONS=0`,
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
- `whatsnew.FEATURES`: every change a user can see adds a `Feature` in the same commit,
  with `version=UNRELEASED`. Set `where` to the tour dialog for the pane it changes, with a
  `tip` of at most two lines, or to `ADVANCED` when it is for experienced users only. Give
  it a one-line `note` for the release notes. Fixes add nothing. Never change or reuse a
  released feature's id. Between releases `pyproject.toml`'s version is a `.dev` one (say
  `0.3.0.dev0` after 0.2.0): a release version fails the tests while any feature is
  `UNRELEASED`, until `make stamp` gives them that version.
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
