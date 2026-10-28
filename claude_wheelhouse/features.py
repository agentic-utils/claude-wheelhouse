"""SDK mode's switch, sdk_enabled(), how plan usage is read, usage_mode(), the local
daemon's port, port(), and how long a quiet session is kept warm, keepalive_hours().

EXPERIMENTAL: SDK mode (sessions run in a wheelhouse host through the Claude Agent SDK:
host.py, protocol_sdk.md, launch.open_host, and the plan usage readings SDK sessions give,
plan_usage.from_rate_limit and SDK_SETTING) is not for use by anyone other than the
author until Anthropic's terms for it are confirmed. It is deliberately undocumented: the
README never mentions it, and a new user gets tab mode.

It is off unless the app is started with --enable-sdk, or a .env file holds
`ENABLE_SDK=true`: the one in the install directory (the checkout the package runs from,
INSTALL_DIR, never the directory it is started in), or ~/.config/claude-wheelhouse/.env.
Every setting is read by given(): its flag, else the environment variable WHEELHOUSE_<KEY>,
else KEY= in the install directory's .env, else the user's, else its default. .env keys
are upper case only (no other spelling is read) and flags lower case. Off, nothing creates an SDK session (New session, the
tutorial, Adopt, Restore and the daemon all run tabs) and the SDK-only controls hide.
Sessions a host already runs keep working, so nobody's work is stranded: they show and
end as before, with the controls that manage them (store.runner says which they are), and
Relaunch of a running one starts its host again. Once one has stopped, Restore (and
Relaunch of a dead session) brings it back as a tab, as it does any session, the way new
sessions run now (launch.restore_runner, Doug's Q27 option A). The flag gates creating new ones, and the controls that only choose how one
runs (Model, the effort slider). Through the daemon the mode is the daemon's (connect.start
passes the flag on; its inbox tells the app).

The stats pane's plan usage runs a one-shot `claude -p "/usage"` every 5 minutes, or with
--usage-mode persistent (or WHEELHOUSE_USAGE_MODE, or USAGE_MODE= in either .env) keeps
one Claude Code running and asks it (plan_usage.Probe, Doug's Q69), in given()'s order; a value that is neither
mode is said once and taken as oneshot. connect.start passes the mode to the daemon.

The local daemon's port is 7428 unless --port, WHEELHOUSE_PORT or PORT= in either .env
says otherwise, in that order (Doug's Q74), so a second user on the machine can run a
wheelhouse of their own beside the first. A value that isn't a port from 1024 to 65535 is
refused (BadPort), naming where it was given. The app resolves it, and connect.start
passes it to the daemon it starts; a daemon started by hand resolves it the same way.

A session quiet over 150k tokens of context gets a keepalive every 55 minutes until it has
been quiet keepalive_hours() hours: --keepalive-hours, else WHEELHOUSE_KEEPALIVE_HOURS, else
KEEPALIVE_HOURS= in either .env, in that order (Doug's Q80), else 8. Fractions are allowed, 0 turns the keepalive
off, and anything else is said once and taken as 8. The hub reads it for tab sessions;
launch.open_host passes it to each SDK host, and connect.start to the daemon.

The app's animation (the title's shimmer, the Cylon eyes, the stats bars) runs at
animation_fps() frames a second: --animation-fps, else WHEELHOUSE_ANIMATION_FPS, else
ANIMATION_FPS= in either .env, else 10 (Doug's Q82, D85). Fractions are allowed up to 30,
0 turns animation off (the title and eyes drawn still), more than 30 is said once and
taken as 30, and anything else is said once and taken as 10. Whatever the rate, everything
moves at the same speed: frames are counted from the time elapsed (WheelhouseApp.animate).

The app shows what happened while the person was away (away.py) after away_hours() hours
with no key press, click or scroll: --away-hours, else WHEELHOUSE_AWAY_HOURS, else
AWAY_HOURS= in either .env, else 2 (#223). Fractions are allowed, 0 turns the card off, and
anything else is said once and taken as 2.

The New session dialog starts in session_dir(): WHEELHOUSE_SESSION_DIR, else SESSION_DIR=
in either .env, else the parent of the install directory (Doug's T123, Q115), as the
wheelhouse's own clone is rarely where a session works. ~ is expanded and a relative path
is taken from the home directory; a directory that
doesn't exist is said once and the default taken."""

import os
import sys
from pathlib import Path

FLAG = "--enable-sdk"
KEY = "ENABLE_SDK"   # the .env key: every key is upper case, every flag lower case
_flag = False   # set by the command line (enable_sdk)
USAGE_FLAG = "--usage-mode"
USAGE_KEY = "USAGE_MODE"
USAGE_MODES = ("oneshot", "persistent")   # the first the default
_usage_mode: str | None = None   # set by the command line (set_usage_mode)
PORT_FLAG = "--port"
PORT_KEY = "PORT"
PORT = 7428
PORT_TAKEN = "port {port} is taken by another process (another user's wheelhouse?)"   # then port_taken's advice
_port: str | None = None   # set by the command line (set_port)
KEEPALIVE_FLAG = "--keepalive-hours"
KEEPALIVE_KEY = "KEEPALIVE_HOURS"
KEEPALIVE_HOURS = 8.0
_keepalive_hours: str | None = None   # set by the command line (set_keepalive_hours)
ANIMATION_FLAG = "--animation-fps"
ANIMATION_KEY = "ANIMATION_FPS"
ANIMATION_FPS = 10.0
MAX_FPS = 30.0   # above it, said once and taken as it
_animation_fps: str | None = None   # set by the command line (set_animation_fps)
AWAY_FLAG = "--away-hours"
AWAY_KEY = "AWAY_HOURS"
AWAY_HOURS = 2.0
_away_hours: str | None = None   # set by the command line (set_away_hours)
AUTOCLEANSE_FLAG = "--autocleanse-minutes"
AUTOCLEANSE_KEY = "AUTOCLEANSE_MINUTES"
AUTOCLEANSE_MINUTES = 5.0
_autocleanse_minutes: str | None = None   # set by the command line (set_autocleanse_minutes)
SESSION_DIR_KEY = "SESSION_DIR"
# each flag's .env key (its environment variable is WHEELHOUSE_<key>), for cli.args_text
SETTINGS = {FLAG: KEY, USAGE_FLAG: USAGE_KEY, PORT_FLAG: PORT_KEY, KEEPALIVE_FLAG: KEEPALIVE_KEY,
            ANIMATION_FLAG: ANIMATION_KEY, AWAY_FLAG: AWAY_KEY, AUTOCLEANSE_FLAG: AUTOCLEANSE_KEY}
_warned: set[str] = set()
# the directory the package runs from: the checkout, as uv sync installs it (editable), so
# the same .env whatever directory the app, the daemon or a tab is started in
INSTALL_DIR = Path(__file__).resolve().parent.parent


def env_files() -> list[Path]:
    """The .env files read, in order: the install directory's, then the user's config."""
    return [INSTALL_DIR / ".env", Path.home() / ".config/claude-wheelhouse/.env"]


def given(flag_value: str | None, flag: str, key: str) -> tuple[str, str] | None:
    """A setting as the person gave it, unchecked, and where: the flag, else the environment
    variable WHEELHOUSE_<key>, else key= in the install directory's .env, else the user's;
    None if none gives it. Keys are upper case only, as flags are lower case."""
    if flag_value is not None:
        return flag_value, flag
    if os.environ.get(var := f"WHEELHOUSE_{key}"):
        return os.environ[var], var
    return next(((env[key], f"{key}= in {path}") for path in env_files()
                 for env in [read_env(path)] if key in env), None)


def _say(said: str) -> None:
    """said on stderr, once a process."""
    if said not in _warned:
        _warned.add(said)
        print(said, file=sys.stderr, flush=True)


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
    """Whether SDK mode is on: --enable-sdk, else WHEELHOUSE_ENABLE_SDK, else ENABLE_SDK= in
    the install directory's .env, else the user's, is true (given)."""
    setting = given("true" if _flag else None, FLAG, KEY)
    return setting is not None and setting[0].lower() == "true"


def set_usage_mode(mode: str) -> None:
    """--usage-mode on the command line."""
    global _usage_mode
    _usage_mode = mode


def usage_mode() -> str:
    """How plan usage is read: --usage-mode, else WHEELHOUSE_USAGE_MODE, else USAGE_MODE= in
    the install directory's .env, else the user's, else oneshot. A value that is neither mode is said once, on
    stderr, and taken as oneshot."""
    return usage_mode_given() or USAGE_MODES[0]


def usage_mode_given() -> str | None:
    """The usage mode the person chose, by --usage-mode or a .env; None if neither chose
    one, or the .env's value is neither mode (said once, on stderr)."""
    if (setting := given(_usage_mode, USAGE_FLAG, USAGE_KEY)) is None:
        return None
    mode, where = setting
    if mode.lower() in USAGE_MODES:
        return mode.lower()
    _say(f"claude-wheelhouse: {where} is {mode!r}, not oneshot or persistent: plan usage runs oneshot")
    return None


class BadPort(ValueError):
    """A port given that isn't one the daemon can listen on."""


def set_port(port: str) -> None:
    """--port on the command line."""
    global _port
    _port = port


def port_given() -> tuple[str, str] | None:
    """The port the person set, unchecked, and where: --port, else WHEELHOUSE_PORT, else
    PORT= in the install directory's .env (never a bare PORT variable, other tools' own), else the user's; None if none sets one."""
    return given(_port, PORT_FLAG, PORT_KEY)


def port() -> int:
    """The local daemon's port: the one given (port_given), else 7428. BadPort, naming
    where, for anything but a whole number from 1024 to 65535."""
    if (setting := port_given()) is None:
        return PORT
    value, where = setting
    if not (value.strip().isdecimal() and 1024 <= int(value) <= 65535):
        raise BadPort(f"{where} is {value!r}: give a port from 1024 to 65535")
    return int(value)


def port_taken(port: int) -> str:
    """Port `port` is another process's: said with where it was set (port_given), to choose
    another there, or, the default taken, where to set one."""
    setting = port_given()
    fix = f"it is set by {setting[1]}: give another there" if setting else \
        "set PORT= in ~/.config/claude-wheelhouse/.env"
    return f"{PORT_TAKEN.format(port=port)}: {fix}"


def set_keepalive_hours(hours: str) -> None:
    """--keepalive-hours on the command line."""
    global _keepalive_hours
    _keepalive_hours = hours


def keepalive_hours() -> float:
    """How long a quiet session is kept warm, in hours: --keepalive-hours, else
    WHEELHOUSE_KEEPALIVE_HOURS, else KEEPALIVE_HOURS= in the install directory's .env, else the user's, else 8. A number
    of 0 or more, fractions allowed (0: no keepalive); anything else is said once, on
    stderr, and taken as 8."""
    if (setting := given(_keepalive_hours, KEEPALIVE_FLAG, KEEPALIVE_KEY)) is None:
        return KEEPALIVE_HOURS
    value, where = setting
    try:
        hours = float(value)
    except ValueError:
        hours = -1.0
    if hours >= 0 and hours != float("inf"):
        return hours
    _say(f"claude-wheelhouse: {where} is {value!r}, not a number of hours: the keepalive runs for 8")
    return KEEPALIVE_HOURS


def set_animation_fps(fps: str) -> None:
    """--animation-fps on the command line."""
    global _animation_fps
    _animation_fps = fps


def animation_fps() -> float:
    """The app's animation frames a second: --animation-fps, else WHEELHOUSE_ANIMATION_FPS,
    else ANIMATION_FPS= in the install directory's .env, else the user's, else 10. A number
    of 0 or more, fractions allowed (0: no animation), up to MAX_FPS: more is said once,
    on stderr, and taken as 30; anything else is said once and taken as 10."""
    if (setting := given(_animation_fps, ANIMATION_FLAG, ANIMATION_KEY)) is None:
        return ANIMATION_FPS
    value, where = setting
    try:
        fps = float(value)
    except ValueError:
        fps = -1.0
    if 0 <= fps <= MAX_FPS:
        return fps
    if MAX_FPS < fps < float("inf"):
        _say(f"claude-wheelhouse: {where} is {value!r}, above {MAX_FPS:g} frames a second: the animation runs at "
             f"{MAX_FPS:g}")
        return MAX_FPS
    _say(f"claude-wheelhouse: {where} is {value!r}, not a number of frames a second: the animation runs at 10")
    return ANIMATION_FPS


def set_away_hours(hours: str) -> None:
    """--away-hours on the command line."""
    global _away_hours
    _away_hours = hours


def away_hours() -> float:
    """How long without input in the app is a gap, after which it says what happened
    meanwhile (away.py), in hours: --away-hours, else WHEELHOUSE_AWAY_HOURS, else AWAY_HOURS=
    in the install directory's .env, else the user's, else 2. A number of 0 or more,
    fractions allowed (0: never); anything else is said once, on stderr, and taken as 2."""
    if (setting := given(_away_hours, AWAY_FLAG, AWAY_KEY)) is None:
        return AWAY_HOURS
    value, where = setting
    try:
        hours = float(value)
    except ValueError:
        hours = -1.0
    if hours >= 0 and hours != float("inf"):
        return hours
    _say(f"claude-wheelhouse: {where} is {value!r}, not a number of hours: a gap is 2")
    return AWAY_HOURS


def set_autocleanse_minutes(minutes: str) -> None:
    """--autocleanse-minutes on the command line."""
    global _autocleanse_minutes
    _autocleanse_minutes = minutes


def autocleanse_minutes() -> float:
    """How long a completed item (settled: a task done, a subagent finished, a PR merged) stays
    in the inbox before it is hidden, as finished ones are, until Alt+V shows them (#235), in
    minutes: --autocleanse-minutes, else WHEELHOUSE_AUTOCLEANSE_MINUTES, else
    AUTOCLEANSE_MINUTES= in the install directory's .env, else the user's, else 5. A number of
    0 or more, fractions allowed (0: never hidden); anything else is said once, on stderr, and
    taken as 5."""
    if (setting := given(_autocleanse_minutes, AUTOCLEANSE_FLAG, AUTOCLEANSE_KEY)) is None:
        return AUTOCLEANSE_MINUTES
    value, where = setting
    try:
        minutes = float(value)
    except ValueError:
        minutes = -1.0
    if minutes >= 0 and minutes != float("inf"):
        return minutes
    _say(f"claude-wheelhouse: {where} is {value!r}, not a number of minutes: completed items are hidden after 5")
    return AUTOCLEANSE_MINUTES


def session_dir() -> Path:
    """The New session dialog's starting directory: WHEELHOUSE_SESSION_DIR, else SESSION_DIR=
    in the install directory's .env, else the user's, else the install directory's parent.
    ~ expanded, a relative path taken from the home directory (the app is started from
    anywhere); one that isn't a directory is said once, on stderr, and the default taken."""
    default = INSTALL_DIR.parent
    if (setting := given(None, "", SESSION_DIR_KEY)) is None:
        return default
    value, where = setting
    if (path := Path(os.path.abspath(Path.home() / Path(value).expanduser()))).is_dir():
        return path
    _say(f"claude-wheelhouse: {where} is {value!r}, not a directory: new sessions start in {default}")
    return default
