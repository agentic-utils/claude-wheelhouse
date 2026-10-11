"""SDK mode's switch, sdk_enabled().

EXPERIMENTAL: SDK mode (sessions run in a wheelhouse host through the Claude Agent SDK:
host.py, protocol_sdk.md, launch.open_host) is not for use by anyone other than the
author until Anthropic's terms for it are confirmed. It is deliberately undocumented: the
README never mentions it, and a new user gets tab mode.

It is off unless the app is started with --enable-sdk, or a .env file holds
`enable-sdk=true`: the one in the directory the app starts in, or
~/.config/claude-wheelhouse/.env. Off, nothing creates an SDK session (New session, the
tutorial, Adopt, Restore and the daemon all run tabs) and the SDK-only controls hide.
Sessions a host already runs keep working, so nobody's work is stranded: they show,
relaunch and end as before, with the controls that manage them (store.runner says which
they are). The flag gates creating new ones, and the controls that only choose how one
runs (Model, the effort slider)."""

from pathlib import Path

FLAG = "--enable-sdk"
KEY = "enable-sdk"
_flag = False   # set by the command line (enable_sdk)


def env_files() -> list[Path]:
    """The .env files read, in order: the starting directory's, then the user's config."""
    return [Path.cwd() / ".env", Path.home() / ".config/claude-wheelhouse/.env"]


def read_env(path: Path) -> dict[str, str]:
    """A .env file's key=value lines: blank lines, # comments and lines without = skipped,
    an `export ` prefix and quotes around a value allowed. A missing or unreadable file is empty."""
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        return {}
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def enable_sdk() -> None:
    """--enable-sdk on the command line."""
    global _flag
    _flag = True


def sdk_enabled() -> bool:
    """Whether SDK mode is on: --enable-sdk, or enable-sdk=true in either .env file."""
    return _flag or any(read_env(path).get(KEY, "").strip().lower() == "true" for path in env_files())
