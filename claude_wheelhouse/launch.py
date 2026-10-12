"""Open sessions in Windows Terminal tabs, and build the Claude command inside them.

The wt.exe command line carries only the session id: `claude_wheelhouse run <id>` reads
everything else from the database. That keeps the brief away from wt's `;`
command separator and from Windows argument quoting.

wt.exe is a Windows execution alias, which WSL can't run directly, so it goes
through `cmd.exe /c` as Microsoft's docs prescribe. cmd re-parses the line, so
its metacharacters are kept out of the title and refused in the directory.

wsl.exe runs the command with no shell, so the user's profile never puts
~/.local/bin (where claude lives) on the PATH. The tab runs it through the user's
interactive login shell, giving it the environment of an ordinary WSL tab: a login shell
alone stops at ~/.bashrc's interactive guard, which skips anything set below it (brew).
"""

import getpass
import json
import os
import pwd
import re
import shlex
import subprocess
import sys
from pathlib import Path

from . import features, liveness, models, tutorial
from .transcript import NO_BRIEF
from .store import Store, db_path, default_runner, now, runner

PLUGIN_DIR = Path(__file__).parent / "plugin"
# The decisions section is separable so its effect on how sessions behave (priming) can be
# A/B tested: WHEELHOUSE_DECISIONS=0 launches sessions without it. See
# .plan/decisions-priming-test.md.
DECISIONS = (Path(__file__).parent / "protocol_decisions.md").read_text()


def protocol_file(name: str) -> str:
    """One of the protocol's files, less a leading <!-- --> comment: a note for whoever reads
    the file (protocol_sdk.md's), never sent to a session."""
    text = (Path(__file__).parent / name).read_text()
    if text.startswith("<!--"):
        text = text[text.index("-->") + 3:].lstrip("\n")
    return text


def protocol(decisions: bool | None = None, runner: str = "tab") -> str:
    """protocol.md, then what differs by how the session runs (protocol_tab.md or
    protocol_sdk.md: how the person's messages reach it), then the decisions section."""
    if decisions is None:
        decisions = os.environ.get("WHEELHOUSE_DECISIONS", "1") != "0"
    base = protocol_file("protocol.md") + protocol_file(f"protocol_{runner}.md")
    return base + "\n" + DECISIONS if decisions else base


PROTOCOL = protocol()
PROTOCOL_SDK = protocol(runner="sdk")
# Sent whenever a tab resumes a conversation (adopt, Restore, relaunch): it triggers a turn,
# so work the session was already doing shows up in the wheelhouse without waiting for its
# next post. Safe to repeat: the session checks what the wheelhouse already holds.
JOINED_TEXT = ("You have just been opened in the wheelhouse, mid-conversation. Call list_items to see "
               "what it already holds for this session, then post as items the questions you are "
               "already waiting on the person for and the tasks you have running (skip any already "
               "there), and set your synopsis. Then stop: don't resume other work because of this message.")


CMD_META = set('&|<>^%"')


def login_shell() -> str:
    return os.environ.get("SHELL") or pwd.getpwuid(os.getuid()).pw_shell or "/bin/bash"


def tab_title(session) -> str:
    """The session's Windows Terminal tab title, kept clear of cmd and wt metacharacters."""
    title = "".join(c for c in session["name"] or Path(session["cwd"]).name if c not in CMD_META)
    return title.replace(";", ",")


def window_name(user: str) -> str:
    """The Windows Terminal window a user's tabs open in, wheelhouse-<user> (Doug's Q75), so
    each wheelhouse's sessions gather in a window of their own, whoever else runs one: wt's
    -w opens a window by that name the first time and adds to it after. Letters, digits, -
    and _ only, which neither cmd nor wt reads as anything but a name: anything else becomes
    -, so d.lindsay and d-lindsay would share a window (users on one machine that close in
    name aren't expected)."""
    return "wheelhouse-" + re.sub(r"[^A-Za-z0-9_-]+", "-", user)


def wt_argv(session, *, python: str, distro: str, user: str, shell: str) -> list[str]:
    if CMD_META & set(session["cwd"]):
        raise ValueError(f"can't launch in {session['cwd']}: it contains one of {''.join(sorted(CMD_META))}")
    return [
        "cmd.exe", "/c", "wt.exe", "-w", window_name(user), "new-tab", "--title", tab_title(session),
        "wsl.exe", "-d", distro, "-u", user, "--cd", session["cwd"].replace(";", r"\;"),
        "--", shell, "-lic", f"exec {shlex.join([python, '-m', 'claude_wheelhouse', 'run', session['id']])}",
    ]


def transcript_exists(sid: str, projects: Path = Path.home() / ".claude/projects") -> bool:
    return any(projects.glob(f"*/{sid}.jsonl"))


def claude_argv(session, *, python: str, resume: bool) -> list[str]:
    mcp = {"mcpServers": {"wheelhouse": {
        "command": python, "args": ["-m", "claude_wheelhouse", "mcp"],
        "env": {"WHEELHOUSE_SESSION_ID": session["id"], "WHEELHOUSE_DB": str(db_path())},
    }}}
    argv = ["claude", "--resume" if resume else "--session-id", session["id"],
            "--plugin-dir", str(PLUGIN_DIR),
            "--mcp-config", json.dumps(mcp),
            "--append-system-prompt", PROTOCOL]
    if session["adopted"] or runner(session) == "sdk":
        # Claude records the system prompt at a conversation's first request and replays it
        # on every resume, so an adopted session would never see the protocol. "off" renders
        # it fresh each request; it has to stay off, since the old record outlives one launch.
        # A host's session opened in a shell tab is the same case: it first ran the SDK protocol.
        argv += ["--system-prompt-snapshot", "off"]
    # every launch passes both when set: a resume keeps neither a live effort change nor,
    # reliably, the model (.plan/model-effort.md)
    for flag in ("model", "effort"):
        if value := models.field(session, flag):
            argv += [f"--{flag}", value]
    if session["name"]:
        argv += ["-n", session["name"]]
    if not resume:
        argv.append(opening_prompt(session))
    return argv


def opening_prompt(session) -> str:
    lines = []
    if session["ticket"]:
        lines.append(f"Ticket: {session['ticket']}")
    lines.append(session["brief"] or NO_BRIEF)
    prompt = "\n\n".join(lines)
    # claude reads a leading "-" as an option ("unknown option"), e.g. a pasted bullet list
    return f"Brief:\n{prompt}" if prompt.startswith("-") else prompt


def injected(sdk: bool | None = None) -> str:
    """Everything a launched session receives, for `claude-wheelhouse protocol`: read it
    before letting the wheelhouse put standing instructions into your sessions. With SDK
    mode on (sdk, else features.sdk_enabled()), a hosted session's part too."""
    if sdk is None:
        sdk = features.sdk_enabled()
    from . import mcp_server, monitor   # mcp_server pulls in the MCP SDK: only load it here
    from .store import GONE_TEXT
    skill = (PLUGIN_DIR / "skills/wheelhouse/SKILL.md").read_text()
    session = {"id": "<session-id>", "cwd": "<directory>", "name": "<name>", "ticket": None,
               "brief": "<opening brief>", "adopted": False, "model": "<model>", "effort": "<effort>"}
    argv = claude_argv(session, python="<python>", resume=False)
    argv[argv.index(PROTOCOL)] = "<protocol.md, above>"
    tools = "\n\n".join(f"{t.__name__}\n    {t.__doc__.strip()}" for t in mcp_server.TOOLS)
    notices = "\n".join([monitor.format_message({"body": "<message>", "item_ref": "Q1"}),
                         *monitor.REQUEST_TEXT.values(), GONE_TEXT,
                         monitor.format_message({"body": JOINED_TEXT, "item_ref": None, "kind": "notice"})])
    return "\n\n".join([
        f"== protocol.md, protocol_tab.md and protocol_decisions.md (appended to the system prompt) ==\n\n{PROTOCOL}",
        *(["== protocol_sdk.md (in place of protocol_tab.md for a session run in the wheelhouse) ==\n\n"
            + protocol_file("protocol_sdk.md")] if sdk else []),
        f"== /wheelhouse skill ({PLUGIN_DIR / 'skills/wheelhouse/SKILL.md'}) ==\n\n{skill}",
        f"== MCP server \"wheelhouse\" ==\n\n{mcp_server.server.instructions}\n\n{tools}",
        f"== Notifications from the wheelhouse monitor ==\n\n{notices}",
        "== Launch command (new session; Restore and Adopt use --resume, and Adopt adds "
        "--system-prompt-snapshot off) ==\n\n"
        + "".join(f" \\\n    {a}" if a.startswith("-") else f" {shlex.quote(a)}" for a in argv).lstrip(),
    ])


class NotHere(RuntimeError):
    """A tab asked for by another machine (may_tab false): it would open on this machine's
    screen, where nobody there sees it."""

    def __init__(self, session):
        self.name = session["name"] or os.path.basename(session["cwd"]) or session["id"][:6]
        super().__init__(f"{self.name} opens in a tab: open it from the daemon's machine")


def open_session(store: Store, sid: str, may_tab: bool = True) -> None:
    """Launch (or restore) a session the way it runs: a host for sdk, a tab for tab. The one
    place a wheelhouse opens a session's tab: with may_tab false (a request from another
    machine) it refuses one (NotHere) before anything is done."""
    session = store.session(sid)
    if session is None:
        raise KeyError(sid)
    if runner(session) == "sdk":
        open_host(store, sid)
    elif not may_tab:
        raise NotHere(session)
    else:
        open_tab(store, sid)


def restore_runner(store: Store, cwd: str, sdk: bool | None = None) -> str:
    """How Restore, Relaunch of a dead session and Adopt bring a session back: the way new
    sessions run now (store.default_runner, with SDK mode on per sdk, else
    features.sdk_enabled()), whatever it ran as before (Doug's Q27, option A,
    .plan/sdk-sessions.md). So with SDK mode off a host's session comes back as a tab. The
    tutorial is always a tab (tutorial.py)."""
    return "tab" if cwd == str(tutorial.tutorial_dir(store)) else default_runner(sdk)


def restore_session(store: Store, sid: str, sdk: bool | None = None, may_tab: bool = True) -> None:
    """Restore (or adopt) a session that isn't running, as restore_runner says. The checks
    come first, so a running tab is never recorded as hosted, and a tab another machine
    asked for (may_tab false) is refused before its runner changes."""
    session = store.session(sid)
    if session is None:
        raise KeyError(sid)
    check_free(session)
    how = restore_runner(store, session["cwd"], sdk)
    if how == "tab" and not may_tab:
        raise NotHere(session)
    store.set_runner(sid, how)
    open_session(store, sid, may_tab)


def check_free(session) -> None:
    """Refuse to launch a session that is running, or still starting from the last launch."""
    sid = session["id"]
    if liveness.is_alive(session["claude_pid"], session["claude_start"], session["boot_id"]):
        raise RuntimeError(f"session {session['name'] or sid} is already running")
    if liveness.status(session) == "starting":   # opening but hasn't registered yet
        raise RuntimeError(f"session {session['name'] or sid} is still starting")


def open_host(store: Store, sid: str) -> None:
    """Start the session's SDK host, detached so that the TUI closing never takes it down.
    EXPERIMENTAL (features.py): SDK mode is not for use by anyone other than the author until
    Anthropic's terms are confirmed. It starts a host for a session already recorded as one;
    only SDK mode creates such sessions."""
    session = store.session(sid)
    if session is None:
        raise KeyError(sid)
    check_free(session)
    resuming = transcript_exists(sid)
    store.mark_launched(sid)
    logs = store.path.parent / "hosts"
    logs.mkdir(exist_ok=True)
    with open(logs / f"{sid}.log", "a") as log:
        print(now(), sid, "resume" if resuming else "new", session["cwd"], file=log, flush=True)
        # setsid --fork: the host is init's child, not ours, so when it exits it is reaped
        # rather than left a zombie of the TUI that still looks alive
        subprocess.run(["setsid", "--fork", sys.executable, "-m", "claude_wheelhouse", "host", sid],
                       cwd=session["cwd"], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                       env=dict(os.environ, WHEELHOUSE_DB=str(store.path)), check=True)
    if resuming:
        store.notice(sid, JOINED_TEXT)


def open_tab(store: Store, sid: str, notice: bool = True) -> None:
    """Launch (or restore) a session in a new tab. Refuses if it is already running.
    notice=False for a host handing its session over: it already knows the wheelhouse."""
    session = store.session(sid)
    if session is None:
        raise KeyError(sid)
    check_free(session)
    argv = wt_argv(session, python=sys.executable,
                   distro=os.environ.get("WSL_DISTRO_NAME", "Ubuntu"), user=getpass.getuser(),
                   shell=login_shell())
    store.mark_launched(sid)
    # cmd.exe or wt.exe failing would otherwise be invisible: keep their output
    with open(store.path.parent / "launch.log", "a") as log:
        print(now(), sid, "launching", " ".join(argv), file=log, flush=True)
        # cmd.exe warns about (and ignores) a \\wsl$ working directory, so start it from C:
        subprocess.Popen(argv, cwd="/mnt/c", stdin=subprocess.DEVNULL, stdout=log,
                         stderr=subprocess.STDOUT, start_new_session=True)
    if notice and transcript_exists(sid):   # resuming: the session takes seconds to start, the notice waits
        store.notice(sid, JOINED_TEXT)


def run(sid: str) -> None:
    """Runs inside the new tab: final liveness check, then exec Claude."""
    store = Store()
    session = store.session(sid)
    if session is None:
        sys.exit(f"claude-wheelhouse: no session {sid} (ended?)")
    env = dict(os.environ, WHEELHOUSE_SESSION_ID=sid, WHEELHOUSE_DB=str(store.path), WHEELHOUSE_PYTHON=sys.executable)
    os.chdir(session["cwd"])
    resume = transcript_exists(sid)
    argv = claude_argv(session, python=sys.executable, resume=resume)
    with open(store.path.parent / "launch.log", "a") as log:   # a tab that dies on start leaves this behind
        print(now(), sid, "resume" if resume else "new", session["cwd"], file=log)
    # exec keeps this pid, so it is Claude's: registering now leaves no window (trust prompt,
    # slow MCP start) in which a live session looks dead and could be restored twice.
    # Check-and-register is one transaction, so two tabs racing can't both get here.
    other = liveness.running_pid(sid)   # e.g. adopted, but its old tab never ran /exit
    if other:
        sys.exit(f"claude-wheelhouse: session {session['name'] or sid} is still running (pid {other}): "
                 "type /exit in its tab, then restore it from the wheelhouse")
    pid = os.getpid()
    if not store.register_if_free(sid, pid, liveness.start_time(pid), liveness.boot_id(), liveness.is_alive):
        sys.exit(f"claude-wheelhouse: session {session['name'] or sid} is already running in another tab")
    os.execvpe("claude", argv, env)
