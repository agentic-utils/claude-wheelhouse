"""A command a session asked the person to run (#257, .plan/257-quick-shell.md): its checks,
the file that carries it to a quick shell's tab, and what runs in that tab.

The app writes the text from the Run dialog to `shell/<key>` beside the database (mode
0600, a random key), and the tab's command line carries only the key. `run(key)` reads and
deletes that file, then runs the text in bash: as typed in the shell itself (an rc that
sources ~/.bashrc and evals it), or, when the person ticked "Send the output back", under
`script` so it keeps a real tty while what it shows is captured, cleaned, capped and sent to
the session on its item. The text travels in the environment, never on a command line, so
nothing passes through cmd.exe or wt.exe quoting. Never imports Textual.
"""

import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

MAX_LINES = 20
MAX_CHARS = 2000
CAP, HEAD, TAIL = 16_000, 4_000, 12_000   # a capture's characters kept: the first HEAD and the last TAIL
KEY = re.compile(r"[0-9a-f]{48}")   # write's token_hex(24)
STALE = 24 * 3600   # seconds after which a shell file left behind (a tab closed mid-run) is deleted


def check_command(text: str) -> None:
    """ValueError unless text can be run as shown (Q239): printable ASCII (0x20 to 0x7E) and
    newlines only, so no escape sequence, CR, tab, zero-width or bidi character or homoglyph
    can make what runs differ from what's read; not empty, at most MAX_LINES lines and
    MAX_CHARS characters."""
    if not text.strip():
        raise ValueError("command is empty: give the command to run")
    for line_no, line in enumerate(text.split("\n"), 1):
        for col, c in enumerate(line, 1):
            if not 0x20 <= ord(c) <= 0x7E:
                raise ValueError(f"command holds U+{ord(c):04X} at line {line_no}, column {col}: "
                                 "only printable ASCII and newlines can be run")
    if (n := len(text.split("\n"))) > MAX_LINES:
        raise ValueError(f"command has {n} lines: at most {MAX_LINES} can be run")
    if len(text) > MAX_CHARS:
        raise ValueError(f"command has {len(text)} characters: at most {MAX_CHARS} can be run")


def check_why(why: str) -> None:
    """ValueError unless why is one line saying what the command does or shows."""
    if not why.strip():
        raise ValueError("why is empty: say in one line what the command does or shows")
    if "\n" in why.strip() or "\r" in why:
        raise ValueError("why must be one line: what the command does or shows")


ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[PX^_][^\x1b]*\x1b\\|[@-Z\\-_])")
SCRIPT_LINE = re.compile(r"^Script (?:started|done) on .*$")


def clean(raw: bytes) -> str:
    """What a capture showed, as text: ANSI escapes stripped, each line overwritten by \\r (a
    progress bar) kept at its last state, script's own header and footer dropped."""
    text = ANSI.sub("", raw.decode("utf-8", "replace"))
    lines = []
    for line in text.split("\n"):
        line = line.rstrip("\r")
        if "\r" in line:
            line = next((part for part in reversed(line.split("\r")) if part), "")
        lines.append(line)
    if lines and SCRIPT_LINE.match(lines[0]):
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and SCRIPT_LINE.match(lines[-1]):
        lines.pop()
    return "\n".join(lines).strip("\n")


def cap(text: str) -> str:
    """At most about CAP characters: the first HEAD and the last TAIL, whole lines, with how
    many lines were cut between, as the end usually holds the error."""
    if len(text) <= CAP:
        return text
    head = text[:HEAD]
    head = head[:head.rfind("\n")] if "\n" in head else head
    tail = text[-TAIL:]
    tail = tail[tail.find("\n") + 1:] if "\n" in tail else tail
    cut = text[len(head):len(text) - len(tail)]
    return f"{head}\n[... {cut.strip(chr(10)).count(chr(10)) + 1} lines cut ...]\n{tail}"


def shell_dir(db: Path) -> Path:
    d = db.parent / "shell"
    d.mkdir(mode=0o700, exist_ok=True)
    return d


def write(db: Path, payload: dict) -> str:
    """The run's file (mode 0600, created new) under a random key: the key."""
    key = secrets.token_hex(24)   # never starting with -, which the tab's argv would read as an option
    fd = os.open(shell_dir(db) / key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f)
    return key


def sweep(db: Path, now: float | None = None) -> None:
    """Delete shell files over a day old: left by a tab closed mid-run, never sent."""
    d = db.parent / "shell"
    if not d.is_dir():
        return
    now = time.time() if now is None else now
    for p in d.iterdir():
        try:
            if now - p.stat().st_mtime > STALE:
                p.unlink()
        except OSError:
            pass


def take(db: Path, key: str) -> dict:
    """The run's payload, its file deleted as it's read: once only."""
    if not KEY.fullmatch(key):
        raise ValueError(f"not a shell key: {key!r}")
    path = shell_dir(db) / key
    try:
        with open(path) as f:
            payload = json.load(f)
    finally:
        path.unlink(missing_ok=True)
    check_command(payload["text"])
    return payload


# the rc the tab's bash starts with: the person's own, then the command shown, put in history
# and (unless it ran already under script) eval'd, leaving an ordinary shell at the prompt
RC = r"""[ -f ~/.bashrc ] && . ~/.bashrc
rm -f -- "$WH_RC"
printf '\n\033[2m%s\033[0m\n%s\n\n' "$WH_HEAD" "$WH_CMD"
history -s -- "$WH_CMD"
if [ -z "$WH_RAN" ]; then eval "$WH_CMD"; fi
unset WH_CMD WH_HEAD WH_RC WH_RAN
"""


def bash(db: Path, env: dict, cmd: str, head: str, ran: bool = False) -> None:
    """Exec bash with RC, the command in its environment."""
    rc = shell_dir(db) / (secrets.token_hex(24) + ".rc")
    fd = os.open(rc, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(RC)
    env = {**env, "WH_CMD": cmd, "WH_HEAD": head, "WH_RC": str(rc), **({"WH_RAN": "1"} if ran else {})}
    exe = shutil.which("bash") or "/bin/bash"
    os.execve(exe, [exe, "--rcfile", str(rc), "-i"], env)


def message(payload: dict, status: int, output: str) -> str:
    """The message the session gets on its item: what ran, where and how it ended, then the output."""
    fence = "`" * max(3, max((len(m) for m in re.findall(r"`+", output)), default=0) + 1)
    edited = payload.get("edited")
    lines = [f"ran {payload['ref']} in {payload['cwd']}, exit {status}" + (", edited" if edited else "")]
    if edited:
        cmd_fence = "`" * max(3, max((len(m) for m in re.findall(r"`+", payload["text"])), default=0) + 1)
        lines += ["", f"{cmd_fence}bash", payload["text"], cmd_fence]
    return "\n".join(lines + ["", fence, output or "(no output)", fence])


def run(key: str, db: Path) -> None:
    """Inside the quick shell's tab: the run's file taken, the command run, and the person left
    at an ordinary shell. With the output wanted, it's captured and sent on the item first."""
    from . import hub
    from .launch import clean_env, now
    from .store import SessionGone, Store
    payload = take(db, key)
    env = {k: v for k, v in clean_env().items() if not k.startswith("WHEELHOUSE_")}
    cwd, text, name, ref = payload["cwd"], payload["text"], payload["name"], payload["ref"]
    try:
        os.chdir(cwd)
    except OSError:
        sys.exit(f"claude-wheelhouse: {cwd} is gone: nothing run")
    head = f"{ref} from {name}:"
    with open(db.parent / "launch.log", "a") as log:
        print(now(), payload["sid"], "run", ref, cwd, "output sent" if payload["want"] else "output not sent",
              json.dumps(text), file=log)
    if not payload["want"]:
        bash(db, env, text, head)
        return
    out = shell_dir(db) / (key + ".out")
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    print(f"\n\033[2m{head} (its output goes back to {name}; a cd or export in it doesn't stay)\033[0m\n{text}\n")
    # Ctrl+C is the command's, through script's pty: a handler of ours, not SIG_IGN, which the
    # command would inherit across exec and then never stop
    previous = signal.signal(signal.SIGINT, lambda *_: None)
    exe = shutil.which("bash") or "/bin/bash"
    try:
        # an interactive bash, as the unticked run's, so ~/.bashrc's PATH and aliases hold; the
        # text in its environment, never in the command line script hands its shell
        status = subprocess.run(["script", "-qefc", f"{exe} -ic 'eval \"$WH_CMD\"'", str(out)],
                                env={**env, "SHELL": exe, "WH_CMD": text}).returncode
    finally:
        signal.signal(signal.SIGINT, previous)
    try:
        captured = cap(clean(out.read_bytes()))
    finally:
        out.unlink(missing_ok=True)
    store = Store(db)
    print()
    how = None
    try:
        # queued while the session is busy or parked (hub.send_or_queue, #249): nothing to wait out
        how = hub.send_or_queue(store, payload["sid"], ref, message(payload, status, captured))
    except SessionGone:
        print(f"{name} has gone from the wheelhouse: the output wasn't sent")
    if how:
        print(f"output {how} to {name} as a reply on {ref}")
    bash(db, env, text, head, ran=True)
