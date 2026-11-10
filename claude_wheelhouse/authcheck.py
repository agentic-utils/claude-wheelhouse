"""Whether a suggested command's CLIs are signed in (#361), so a session suggests the login
first instead of leaving the person at a password prompt in the Run tab.

Each check runs in a login shell, as the Run tab's command does, with no stdin and no
controlling terminal, so it can never prompt. Its output is discarded unread: some print
tokens. A CLI that isn't installed (exit 127) is skipped; a check that times out is unknown.
"""

import os
import re
import shlex
import signal
import subprocess
import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Cli:
    names: tuple[str, ...]          # the program names it covers
    check: str                      # exits 0 when signed in, never prompts
    login: str                      # what the person runs to sign in
    subcommands: tuple[str, ...] = ()   # only these need auth (none: every use)


# one row per CLI. Unverified against an installed CLI's --help: aws, az, vercel
CLIS = (
    Cli(("gcloud", "bq", "gsutil"), "gcloud auth print-access-token --quiet", "gcloud auth login --update-adc"),
    Cli(("gh",), "gh auth status", "gh auth login"),
    Cli(("aws",), "aws sts get-caller-identity", "aws sso login (or aws configure)"),
    Cli(("az",), "az account show", "az login"),
    Cli(("kubectl", "helm"), "kubectl version --request-timeout=5s",
        "the cluster's own login (GKE: gcloud auth login)"),
    Cli(("npm",), "npm whoami", "npm login", subcommands=("publish", "unpublish", "deprecate")),
    # assumes the Google provider, the one we use
    Cli(("terraform",), "gcloud auth application-default print-access-token --quiet",
        "gcloud auth application-default login"),
    Cli(("supabase",), "supabase projects list", "supabase login"),
    Cli(("vercel",), "vercel whoami", "vercel login"),
)
BY_NAME = {n: cli for cli in CLIS for n in cli.names}

TIMEOUT = 10        # seconds a check may take
CACHE_SECONDS = 60
NOT_INSTALLED = 127
WRAPPERS = {"sudo", "env", "time", "nice", "nohup", "command", "exec", "timeout", "xargs"}
SEPARATORS = re.compile(r"&&|\|\||[;|\n]")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

_cache: dict[str, tuple[float, str]] = {}
_lock = threading.Lock()


def programs(command: str) -> list[tuple[str, list[str]]]:
    """Each segment's program name and its arguments, past env assignments and wrappers
    (and their flags). A segment that won't parse is skipped."""
    found = []
    for segment in SEPARATORS.split(command):
        try:
            words = shlex.split(segment, comments=True)
        except ValueError:
            continue
        while words and (ASSIGNMENT.match(words[0]) or words[0] in WRAPPERS or words[0].startswith("-")
                         or words[0].isdigit()):   # a wrapper's flags and values: nice -n 19, timeout 5
            words.pop(0)
        if words:
            found.append((os.path.basename(words[0]), words[1:]))
    return found


def clis(command: str) -> list[Cli]:
    """The listed CLIs the command uses, once each, in order."""
    used = []
    for name, args in programs(command):
        cli = BY_NAME.get(name)
        if cli is None or cli in used:
            continue
        if not cli.subcommands or set(args) & set(cli.subcommands):   # any word: a flag may take a value
            used.append(cli)
    return used


def shell() -> str:
    return os.environ.get("SHELL") or "/bin/bash"


SIGNED_IN, SIGNED_OUT, UNKNOWN, ABSENT = "signed in", "signed out", "unknown", "absent"


def run_check(cli: Cli) -> str:
    """SIGNED_IN on exit 0, ABSENT on 127 (not installed), UNKNOWN on a timeout, else
    SIGNED_OUT. Output goes to /dev/null, never read."""
    p = subprocess.Popen([shell(), "-lc", cli.check], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
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
    """run_check's result, cached per CLI for CACHE_SECONDS."""
    with _lock:
        hit = _cache.get(cli.check)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
    result = run_check(cli)
    with _lock:
        _cache[cli.check] = (time.monotonic(), result)
    return result


def notes(command: str) -> list[str]:
    """A line for the session per listed CLI that isn't signed in, or couldn't be confirmed."""
    lines = []
    for cli in clis(command):
        s, name = state(cli), cli.names[0]
        if s == SIGNED_OUT:
            lines.append(f'{name} isn\'t signed in ({cli.check} failed): suggest "{cli.login}" first, '
                         "as its own command, and tell the person why.")
        elif s == UNKNOWN:
            lines.append(f"couldn't confirm {name} is signed in ({cli.check} timed out after {TIMEOUT}s).")
    return lines
