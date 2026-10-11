"""SDK mode's switch, sdk_enabled(), and how plan usage is read, usage_mode().

EXPERIMENTAL: SDK mode (sessions run in a wheelhouse host through the Claude Agent SDK:
host.py, protocol_sdk.md, launch.open_host) is not for use by anyone other than the
author until Anthropic's terms for it are confirmed. It is deliberately undocumented: the
README never mentions it, and a new user gets tab mode.

It is off unless the app is started with --enable-sdk, or a .env file holds
`enable-sdk=true` (ENABLE_SDK or enable_sdk as the key do too): the one in the directory
the app starts in, or ~/.config/claude-wheelhouse/.env. Off, nothing creates an SDK session (New session, the
tutorial, Adopt, Restore and the daemon all run tabs) and the SDK-only controls hide.
Sessions a host already runs keep working, so nobody's work is stranded: they show and
end as before, with the controls that manage them (store.runner says which they are), and
Relaunch of a running one starts its host again. Once one has stopped, Restore (and
Relaunch of a dead session) brings it back as a tab, as it does any session, the way new
sessions run now (launch.restore_runner, Doug's Q27 option A). The flag gates creating new ones, and the controls that only choose how one
runs (Model, the effort slider). Through the daemon the mode is the daemon's (connect.start
passes the flag on; its inbox tells the app).

The stats pane's plan usage runs a one-shot `claude -p "/usage"` every 5 minutes, or with
--usage-mode persistent (or usage-mode=persistent, USAGE_MODE or usage_mode, in either
.env) keeps one Claude Code running and asks it (plan_usage.Probe, Doug's Q69). The flag
wins over .env, the starting directory's .env over the user's; a value that is neither
mode is said once and taken as oneshot. connect.start passes the mode to the daemon."""

import sys
from pathlib import Path

FLAG = "--enable-sdk"
KEYS = ("enable-sdk", "ENABLE_SDK", "enable_sdk")   # the .env key, written any of these ways
_flag = False   # set by the command line (enable_sdk)
USAGE_FLAG = "--usage-mode"
USAGE_KEYS = ("usage-mode", "USAGE_MODE", "usage_mode")
USAGE_MODES = ("oneshot", "persistent")   # the first the default
_usage_mode: str | None = None   # set by the command line (set_usage_mode)
_warned: set[str] = set()


def env_files() -> list[Path]:
    """The .env files read, in order: the starting directory's, then the user's config."""
    return [Path.cwd() / ".env", Path.home() / ".config/claude-wheelhouse/.env"]


def read_env(path: Path) -> dict[str, str]:
    """A .env file's key=value lines: blank lines, # comments and lines without = skipped,
    an `export ` prefix, quotes around a value and a ` # comment` after it allowed, and a
    UTF-8 byte order mark at the start. A byte that isn't UTF-8 never stops it reading. A
    missing or unreadable file is empty."""
    try:
        text = path.read_bytes().decode("utf-8-sig", errors="replace")
    except OSError:
        return {}
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip().removeprefix("export ").strip()
        value = value.strip()
        if value[:1] in ("\"", "'") and value[0] in value[1:]:
            value = value[1:value.index(value[0], 1)]   # quoted: anything after the closing quote goes
        else:
            value = value.split(" #", 1)[0].split("\t#", 1)[0].strip()
        out[name] = value
    return out


def enable_sdk() -> None:
    """--enable-sdk on the command line."""
    global _flag
    _flag = True


def sdk_enabled() -> bool:
    """Whether SDK mode is on: --enable-sdk, or enable-sdk=true in either .env file."""
    return _flag or any(read_env(path).get(name, "").lower() == "true" for path in env_files() for name in KEYS)


def set_usage_mode(mode: str) -> None:
    """--usage-mode on the command line."""
    global _usage_mode
    _usage_mode = mode


def usage_mode() -> str:
    """How plan usage is read: --usage-mode, else usage-mode in the starting directory's
    .env, else the user's, else oneshot. A value that is neither mode is said once, on
    stderr, and taken as oneshot."""
    given, where = _usage_mode, USAGE_FLAG
    if given is None:
        given, where = next(((env[name], f"{name} in {path}") for path in env_files()
                             for env in [read_env(path)] for name in USAGE_KEYS if name in env), (None, None))
    if given is None:
        return USAGE_MODES[0]
    if given.lower() in USAGE_MODES:
        return given.lower()
    said = f"claude-wheelhouse: {where} is {given!r}, not oneshot or persistent: plan usage runs oneshot"
    if said not in _warned:
        _warned.add(said)
        print(said, file=sys.stderr, flush=True)
    return USAGE_MODES[0]
