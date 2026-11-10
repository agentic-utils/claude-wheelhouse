"""Whether a suggested command's CLIs are signed in (#361), so a session suggests the login
first instead of leaving the person at a password prompt in the Run tab.

Each check runs in a login shell, as the Run tab's command does, with no stdin and no
controlling terminal, so it can never prompt. Its output is discarded unread: some print
tokens. A CLI that isn't installed (exit 127) is skipped; a check that times out is unknown.
"""

import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Cli:
    names: tuple[str, ...]          # the program names it covers
    check: str                      # exits 0 when signed in, never prompts
    login: str                      # a runnable command that signs the person in
    subcommands: tuple[str, ...] = ()   # only these need auth (none: every use)
    hint: str = ""                  # said beside the login, never inside it


# one row per CLI. Unverified against an installed CLI's --help: aws, az, vercel
CLIS = (
    Cli(("gcloud", "bq", "gsutil"), "gcloud auth print-access-token --quiet", "gcloud auth login --update-adc"),
    Cli(("gh",), "gh auth status", "gh auth login"),
    Cli(("aws",), "aws sts get-caller-identity", "aws sso login", hint="or aws configure, for access keys"),
    Cli(("az",), "az account get-access-token --output none", "az login"),
    # assumes GKE: another cluster has its own login
    Cli(("kubectl", "helm"), "kubectl version --request-timeout=5s", "gcloud auth login --update-adc",
        hint="assuming a GKE cluster"),
    Cli(("npm",), "npm whoami", "npm login", subcommands=("publish", "unpublish", "deprecate")),
    # assumes the Google provider, the one we use
    Cli(("terraform",), "gcloud auth application-default print-access-token --quiet",
        "gcloud auth application-default login", hint="assuming the Google provider"),
    Cli(("supabase",), "supabase projects list", "supabase login"),
    Cli(("vercel",), "vercel whoami", "vercel login"),
)

TIMEOUT = 10        # seconds a check may take
CACHE_SECONDS = 60  # how long a signed-in result is trusted
NOT_INSTALLED = 127
SIGNED_IN, SIGNED_OUT, UNKNOWN, ABSENT = "signed in", "signed out", "unknown", "absent"

_cache: dict[str, float] = {}   # a check that passed: when
_lock = threading.Lock()


def word(name: str) -> re.Pattern:
    """name as a program: not inside a longer word (ghost, my-gh) or a file name (gh.sh),
    but after a path (/usr/bin/gh), $( or a backtick."""
    return re.compile(rf"(?<![\w.-]){re.escape(name)}(?![\w.-])")


def clis(command: str) -> list[Cli]:
    """The listed CLIs the command names anywhere, once each, in table order."""
    return [cli for cli in CLIS
            if any(word(n).search(command) for n in cli.names)
            and (not cli.subcommands or any(word(s).search(command) for s in cli.subcommands))]


def run_check(cli: Cli) -> str:
    """SIGNED_IN on exit 0, ABSENT on 127 (not installed), UNKNOWN on a timeout, else
    SIGNED_OUT. Output goes to /dev/null, never read."""
    p = subprocess.Popen([os.environ.get("SHELL") or "/bin/bash", "-lc", cli.check], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        code = p.wait(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)   # the shell and the CLI under it
        except ProcessLookupError:
            pass
        p.wait()
        return UNKNOWN
    return SIGNED_IN if code == 0 else ABSENT if code == NOT_INSTALLED else SIGNED_OUT


def state(cli: Cli) -> str:
    """run_check's result. Only a pass is cached, for CACHE_SECONDS: anything else is checked
    again next time, so a login the person has just done counts at once."""
    with _lock:
        if time.monotonic() - _cache.get(cli.check, -CACHE_SECONDS) < CACHE_SECONDS:
            return SIGNED_IN
    result = run_check(cli)
    if result == SIGNED_IN:
        with _lock:
            _cache[cli.check] = time.monotonic()
    return result


def notes(command: str, ref: str) -> list[str]:
    """A line for the session per listed CLI that may need signing in, about item ref."""
    lines = []
    for cli in clis(command):
        s, name = state(cli), cli.names[0]
        hint = f" ({cli.hint})" if cli.hint else ""
        if s == SIGNED_OUT:
            lines.append(f"{name}'s sign-in check failed ({cli.check}): it may need signing in, or be offline. "
                         f'Suggest "{cli.login}"{hint} as its own command, and ask the person to run it before {ref}.')
        elif s == UNKNOWN:
            lines.append(f"couldn't confirm {name} is signed in (its check timed out): if {ref} stops at a login "
                         f'prompt, suggest "{cli.login}"{hint}.')
    return lines
