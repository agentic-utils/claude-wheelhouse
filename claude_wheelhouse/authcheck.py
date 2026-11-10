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
    or_else: str = "be offline"     # what else a failed check may mean


# one row per CLI. Unverified against an installed CLI's --help: aws, az, vercel
CLIS = (
    Cli(("gcloud", "bq", "gsutil"), "gcloud auth print-access-token --quiet", "gcloud auth login --update-adc"),
    Cli(("gh",), "gh auth status", "gh auth login"),
    Cli(("aws",), "aws sts get-caller-identity", "aws sso login", hint="or aws configure, for access keys"),
    Cli(("az",), "az account get-access-token --output none", "az login"),
    # assumes GKE: another cluster has its own login
    Cli(("kubectl", "helm"), "kubectl version --request-timeout=5s", "gcloud auth login --update-adc",
        hint="assuming a GKE cluster", or_else="be offline, or the kube context is missing or unreachable"),
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


# where a program may start: after a separator, $(, a backtick, ( or an opening quote
STARTS = re.compile(r"&&|\|\||\$\(|[;|&\n`()'\"]")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
WRAPPERS = {"sudo", "env", "time", "nice", "nohup", "command", "exec", "timeout", "xargs", "doas"}
BY_NAME = {n: cli for cli in CLIS for n in cli.names}


def programs(command: str) -> list[tuple[str, list[str]]]:
    """Each program the command runs, with the words after it up to the next separator:
    past env assignments and wrappers with their flags and values. A path's last part is
    the name (/usr/bin/gh is gh)."""
    found = []
    for piece in STARTS.split(command):
        words = piece.split()
        while words and ASSIGNMENT.match(words[0]):
            words.pop(0)
        if words and words[0] in WRAPPERS:
            words.pop(0)
            while words and (words[0].startswith("-") or words[0].isdigit() or ASSIGNMENT.match(words[0])):
                flag = words.pop(0)
                # a short flag's value (sudo -u root), unless it's the program (sudo -E gh)
                if len(flag) == 2 and flag[0] == "-" and words and words[0] not in BY_NAME:
                    words.pop(0)
        if words:
            found.append((words[0].rsplit("/", 1)[-1], words[1:]))
    return found


def clis(command: str) -> list[Cli]:
    """The listed CLIs the command runs, once each, in table order. A CLI whose login the
    command runs is left out: it doesn't need running before itself."""
    used = {BY_NAME[name] for name, args in programs(command) if name in BY_NAME
            and (not BY_NAME[name].subcommands or set(args) & set(BY_NAME[name].subcommands))}
    return [cli for cli in CLIS if cli in used and cli.login not in command]


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
            lines.append(f"{name}'s sign-in check failed ({cli.check}): it may need signing in, or {cli.or_else}. "
                         f'Suggest "{cli.login}"{hint} as its own command, and ask the person to run it before {ref}.')
        elif s == UNKNOWN:
            lines.append(f"couldn't confirm {name} is signed in (its check timed out): if {ref} stops at a login "
                         f'prompt, suggest "{cli.login}"{hint}.')
    return lines
