"""SDK mode's switch, sdk_enabled(), how plan usage is read, usage_mode(), and the local
daemon's port, port().

EXPERIMENTAL: SDK mode (sessions run in a wheelhouse host through the Claude Agent SDK:
host.py, protocol_sdk.md, launch.open_host, and the plan usage readings SDK sessions give,
plan_usage.from_rate_limit and SDK_SETTING) is not for use by anyone other than the
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
mode is said once and taken as oneshot. connect.start passes the mode to the daemon.

The local daemon's port is 7428 unless --port, WHEELHOUSE_PORT or port= in either .env
says otherwise, in that order (Doug's Q74), so a second user on the machine can run a
wheelhouse of their own beside the first. A value that isn't a port from 1024 to 65535 is
refused (BadPort), naming where it was given. The app resolves it, and connect.start
passes it to the daemon it starts; a daemon started by hand resolves it the same way."""

import os
import sys
from pathlib import Path

FLAG = "--enable-sdk"
KEYS = ("enable-sdk", "ENABLE_SDK", "enable_sdk")   # the .env key, written any of these ways
_flag = False   # set by the command line (enable_sdk)
USAGE_FLAG = "--usage-mode"
USAGE_KEYS = ("usage-mode", "USAGE_MODE", "usage_mode")
USAGE_MODES = ("oneshot", "persistent")   # the first the default
_usage_mode: str | None = None   # set by the command line (set_usage_mode)
PORT_FLAG = "--port"
PORT_KEYS = ("port",)   # lower case only: PORT in a project's .env is usually its own server's
PORT = 7428
PORT_TAKEN = ("port {port} is taken by another process (another user's wheelhouse?): set port= in "
              "~/.config/claude-wheelhouse/.env")
_port: str | None = None   # set by the command line (set_port)
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
    return usage_mode_given() or USAGE_MODES[0]


def usage_mode_given() -> str | None:
    """The usage mode the person chose, by --usage-mode or a .env; None if neither chose
    one, or the .env's value is neither mode (said once, on stderr)."""
    given, where = _usage_mode, USAGE_FLAG
    if given is None:
        given, where = next(((env[name], f"{name} in {path}") for path in env_files()
                             for env in [read_env(path)] for name in USAGE_KEYS if name in env), (None, None))
    if given is None:
        return None
    if given.lower() in USAGE_MODES:
        return given.lower()
    said = f"claude-wheelhouse: {where} is {given!r}, not oneshot or persistent: plan usage runs oneshot"
    if said not in _warned:
        _warned.add(said)
        print(said, file=sys.stderr, flush=True)
    return None


class BadPort(ValueError):
    """A port given that isn't one the daemon can listen on."""


def set_port(port: str) -> None:
    """--port on the command line."""
    global _port
    _port = port


def port_given() -> tuple[str, str] | None:
    """The port the person set, unchecked, and where: --port, else WHEELHOUSE_PORT, else
    port= in the starting directory's .env, else the user's; None if none sets one."""
    if _port is not None:
        return _port, PORT_FLAG
    if os.environ.get("WHEELHOUSE_PORT"):
        return os.environ["WHEELHOUSE_PORT"], "WHEELHOUSE_PORT"
    return next(((env[name], f"{name}= in {path}") for path in env_files()
                 for env in [read_env(path)] for name in PORT_KEYS if name in env), None)


def port() -> int:
    """The local daemon's port: the one given (port_given), else 7428. BadPort, naming
    where, for anything but a whole number from 1024 to 65535."""
    if (setting := port_given()) is None:
        return PORT
    given, where = setting
    if not (given.strip().isdecimal() and 1024 <= int(given) <= 65535):
        raise BadPort(f"{where} is {given!r}: give a port from 1024 to 65535")
    return int(given)
