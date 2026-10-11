"""SDK mode's switch, sdk_enabled().

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
passes the flag on; its inbox tells the app)."""

from pathlib import Path

FLAG = "--enable-sdk"
KEYS = ("enable-sdk", "ENABLE_SDK", "enable_sdk")   # the .env key, written any of these ways
_flag = False   # set by the command line (enable_sdk)


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
