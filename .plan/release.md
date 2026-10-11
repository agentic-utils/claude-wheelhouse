# Releases, the update notice and Update and restart (design)

Issue #16. Status: for review. Chosen: option B, "but check hourly not daily".

Option B: tag releases (v0.2.0 and on) as GitHub releases and publish them to PyPI, so
testers install with `uv tool install claude-wheelhouse`. An update notice, checked at
most hourly, that can be switched off. No silent auto-update: a one-key Update and restart
runs `uv tool upgrade` and restarts the app, offered only when no session is mid-turn.

## Where it stands

- `pyproject.toml`: `claude-wheelhouse` 0.1.0, hatchling, one script
  (`claude-wheelhouse = "claude_wheelhouse.cli:main"`), the stats module's entry point, and
  the Claude Code plugin shipped inside the package (`claude_wheelhouse/plugin/**`).
- The Makefile assumes a checkout: `make install` is `uv sync`, `make run` and
  `make tutorial` are `uv run`. No tags exist, and no release workflow.
- The installed version is already read at run time:
  `importlib.metadata.version("claude-wheelhouse")` in `stats_pane.py`.
- The repo (`agentic-utils/claude-wheelhouse`) is public.
- **PyPI name: free.** `https://pypi.org/pypi/claude-wheelhouse/json` answered 404 on
  11 Oct 2026 (as did `claude_wheelhouse`). Nothing holds it until the first upload.

## Versioning

Semver, staying on 0.y.z through the beta. The version lives in one place, `pyproject.toml`
(and `uv.lock`, which records it); code reads it through `importlib.metadata`.

| Bump | When |
|---|---|
| minor (0.2.0 to 0.3.0) | any new feature the person can see; any `PROTOCOL_VERSION` bump; any store migration |
| patch (0.2.0 to 0.2.1) | fixes only, with the same `PROTOCOL_VERSION` and schema |

`PROTOCOL_VERSION` (store.py, 10 now) stays what it is: the contract between the store and
the code a running session loaded. It moves only when a running session on older code
would mishandle the store or miss new behaviour. The release version is for people. The
one link between them is the rule above: a patch never bumps the protocol, so a patch
release never needs a session relaunched, and the app can say so before installing it.

A new tutorial step means a new feature, so it ships in a minor release with `new_in` set
to that version (`.plan/tutorial.md`).

Downgrades aren't supported: store migrations only go forward. Release notes say so.

## Release steps

1. On `main`, a commit `#<ticket>/chore release 0.2.0` that bumps `pyproject.toml` and
   `uv.lock`.
2. Tag it `v0.2.0` and push the tag.
3. The tag runs `.github/workflows/release.yml`:
   - checks the tag matches the package version, and fails if not;
   - runs the tests;
   - `uv build` (wheel and sdist), then installs the wheel with `uv tool install` into a
     clean environment and runs `claude-wheelhouse protocol` as a smoke test, which proves
     the entry point and the bundled plugin made it into the wheel;
   - publishes to PyPI by trusted publishing (`uv publish`), from a GitHub environment
     named `pypi`;
   - creates the GitHub release with the wheel and sdist attached and notes generated from
     the commits since the last tag. The notes list the release's new tutorial steps and
     say whether `PROTOCOL_VERSION` moved.
4. A `make release VERSION=0.2.0` target does steps 1 and 2.

### What publishing needs from Doug

Publishing is outward-facing and runs under Doug's PyPI account, so it needs him once:

- A PyPI account with two-factor authentication.
- A **pending trusted publisher** on PyPI (Account, Publishing): project
  `claude-wheelhouse`, owner `agentic-utils`, repository `claude-wheelhouse`, workflow
  `release.yml`, environment `pypi`. The first tagged run then creates the project. No API
  token is ever stored. PyPI's docs say a pending publisher does not reserve the name:
  only the first upload does.
- In the GitHub repo, an environment `pypi`, optionally with Doug as a required reviewer
  so each publish waits for his click.
- The workflow's publish job has `permissions: id-token: write`; nothing else needs
  secrets.

### Docs that change with the first release

- README: install with `uv tool install claude-wheelhouse`, run with `claude-wheelhouse`,
  and the tutorial with `claude-wheelhouse tutorial`. The `make` targets stay, for a
  checkout.
- The app's own text named `make tutorial` in four places (`TutorialOffer`, the `?`
  overlay, the checklist, `answer_offer`), wrong for an installed copy. Done: each names
  `tutorial.COMMAND`, the fixed "claude-wheelhouse tutorial (make tutorial in a source
  checkout)". Guessing one from `PATH` (reviews 68 and 69) named a command that fails
  under `make run`, where `uv run` puts the venv's `claude-wheelhouse` on `PATH` but the
  person's own shell can't run it.

## The update check

- **Endpoint**: `GET https://pypi.org/pypi/claude-wheelhouse/json`, reading
  `info.version` (PyPI's latest non-prerelease, non-yanked version). PyPI is what
  `uv tool upgrade` installs from, so it can't announce a version uv won't find. Only the
  GET is sent: no identifiers, nothing about the install.
- **Hourly, cached in the DB**: the settings table holds `update_checked_at`,
  `update_latest` and the response's ETag. On start, and on a timer while running, the app
  checks if the last check is an hour old or more, off the UI thread, with a 5 second
  timeout. Every app instance shares the database, so several open copies still make one
  request an hour between them.
- **Offline**: a failed check keeps the last known answer and tries again in an hour. It
  never shows an error; the notice simply isn't there, or stays as it was.
- **Switched off**: a setting `update_check = off`, set from the update line itself (O:
  "stop checking") and listed in `?`, and an environment variable
  `WHEELHOUSE_UPDATE_CHECK=0` that wins over it. Off means no request at all.
- **Checkouts don't check.** The package's install record (PEP 610 `direct_url.json`)
  says editable for a checkout run with `uv run`; that's a developer, who updates with
  `git pull`. An install that isn't a uv tool (pip, pipx) gets the notice but no Update
  key: the line names the command to run instead.

The notice sits at the right-hand end of the title row, in the slot it shares with the
tutorial badge: "0.3.0 available · U". The footer carries `U Update` while it shows.

## Update and restart

`U` opens a short dialog: the versions (0.2.1 to 0.3.0), a link to the GitHub release, and
for a minor release, "sessions will need relaunching". Then:

1. **Gate.** Offered only when no session is mid-turn (`Hub.busy` false for every
   session) and no Relaunch is under way. Otherwise the dialog names the sessions it's
   waiting on and enables the key itself once they're idle. Queued answers are in the store
   and survive; text typed but not submitted is in memory (`unsent.py`), so it's written to
   a file in the state directory before the restart and read back once after.
2. **Upgrade.** `uv tool upgrade claude-wheelhouse`, its output in the dialog. On failure
   the app carries on, on the old code already loaded, and shows the error.
3. **Restart.** The app exits cleanly, so Textual gives the terminal back, then `cli.main`
   execs the newly installed `claude-wheelhouse` with the same arguments, in the same
   terminal. The new code opens the store and runs its migrations.
4. **Sessions.** Hosts are started with `setsid --fork` (launch.py), so they aren't the
   app's children: the restart doesn't touch them, and they carry on with the code they
   loaded. After a patch, nothing more happens. After a release that bumped
   `PROTOCOL_VERSION`, every running session shows "needs relaunch", and the idle ones
   relaunch automatically as part of Update and restart (Q64 B): none was mid-turn at the
   gate, so none loses a turn. Hosted sessions (SDK mode, `.plan/sdk-sessions.md`) go
   through the existing Relaunch (`Hub.stop_host`, with compact first past
   `COMPACT_FIRST_TOKENS`). A session in a tab, or a host's in a shell tab, can't be
   relaunched from the wheelhouse while it runs (`TAB_RELAUNCH`), so the app lists those
   with "/exit, then Relaunch" (inferred: the wheelhouse never closes a tab itself).

### What the restart does to running sessions

- **Hosted sessions** keep running old code until relaunched; that's what
  `needs_relaunch` exists for, and the protocol rule keeps a patch safe to mix.
- **Old code loading new files.** `uv tool upgrade` replaces the package's files in place.
  A running host imports lazily in one place (`from .launch import open_tab`, the shell
  hand-over), and Claude Code starts `python -m claude_wheelhouse mcp` afresh when it
  reconnects its MCP server; both would load new code into an old session. The plugin is
  read from the package directory too. Proposed: the host imports everything at start, and
  a session past the gate is relaunched rather than left mixing code after a minor release.
- **The daemon** (`claude-wheelhouse daemon`) is a separate process no client uses yet; it
  keeps old code until restarted. When the TUI moves onto it (`.plan/remote.md`), Update
  and restart must restart it too.
- **A second app window** on the old code keeps running against the migrated store.
  Migrations are additive (new tables, `ADD COLUMN`), so it mostly copes; it shows the
  notice too, and its own restart fixes it.

## Tutorial top-ups

Nothing to wire: the pending rule in `.plan/tutorial.md` is all steps minus the done or
dismissed ones, so the new code's steps are pending the moment it starts. After Update and
restart the badge reads "2 new tutorial steps · L", alone in the title row, since the update
notice has gone. A tester who presses Later, or ignores it, sees it on every start until
they do them or press Never.

## Tests

- The update check, table-driven: fresh cache, an hour old, PyPI down, 304 with the ETag,
  switched off by setting and by environment, an editable install, a newer and an equal
  version. One request between two stores sharing a database.
- The gate: a busy session, a relaunch under way, all idle.
- The restart: an upgrade that fails leaves the app running; one that succeeds execs the
  entry point with the same arguments (both with the subprocess and exec faked); unsent
  text survives it.
- After a protocol bump, idle hosted sessions relaunch automatically and tab sessions are
  listed with "/exit, then Relaunch".
- The release workflow's version check and wheel smoke test run in CI on every PR, so a
  broken wheel shows before the tag.

## Resolved

- **Relaunch after a protocol-bumping update** (Q64): B, "after a protocol-bumping update,
  idle sessions relaunch automatically" (Update and restart, step 4).
- **A required reviewer on the `pypi` environment, and when to publish** (Q65): "approval
  yes, but no need to publish yet. Let's do so when we're happy to start beta, and when
  we've locked down SDK mode." So the `pypi` environment has Doug as a required reviewer,
  and nothing is published, not even 0.1.0 to hold the name, until the beta starts and SDK
  mode's terms are settled.
