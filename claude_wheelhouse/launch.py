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
import shlex
import subprocess
import sys
from pathlib import Path

from . import liveness
from .transcript import NO_BRIEF, TitleWatch
from .store import Store, db_path, default_runner, now, renamed_since, runner

PLUGIN_DIR = Path(__file__).parent / "plugin"
# The decisions section is separable so its effect on how sessions behave (priming) can be
# A/B tested: WHEELHOUSE_DECISIONS=0 launches sessions without it. See
# .plan/decisions-priming-test.md.
DECISIONS = (Path(__file__).parent / "protocol_decisions.md").read_text()


def protocol(decisions: bool | None = None, runner: str = "tab") -> str:
    """protocol.md, then what differs by how the session runs (protocol_tab.md or
    protocol_sdk.md: how the person's messages reach it), then the decisions section."""
    if decisions is None:
        decisions = os.environ.get("WHEELHOUSE_DECISIONS", "1") != "0"
    here = Path(__file__).parent
    base = (here / "protocol.md").read_text() + (here / f"protocol_{runner}.md").read_text()
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


def wt_argv(session, *, python: str, distro: str, user: str, shell: str) -> list[str]:
    if CMD_META & set(session["cwd"]):
        raise ValueError(f"can't launch in {session['cwd']}: it contains one of {''.join(sorted(CMD_META))}")
    return [
        "cmd.exe", "/c", "wt.exe", "-w", "0", "new-tab", "--title", tab_title(session),
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


def injected() -> str:
    """Everything a launched session receives, for `claude-wheelhouse protocol`: read it
    before letting the wheelhouse put standing instructions into your sessions."""
    from . import mcp_server, monitor   # mcp_server pulls in the MCP SDK: only load it here
    from .store import GONE_TEXT
    skill = (PLUGIN_DIR / "skills/wheelhouse/SKILL.md").read_text()
    session = {"id": "<session-id>", "cwd": "<directory>", "name": "<name>", "ticket": None,
               "brief": "<opening brief>", "adopted": False}
    argv = claude_argv(session, python="<python>", resume=False)
    argv[argv.index(PROTOCOL)] = "<protocol.md, above>"
    tools = "\n\n".join(f"{t.__name__}\n    {t.__doc__.strip()}" for t in mcp_server.TOOLS)
    notices = "\n".join([monitor.format_message({"body": "<message>", "item_ref": "Q1"}),
                         *monitor.REQUEST_TEXT.values(), GONE_TEXT,
                         monitor.format_message({"body": JOINED_TEXT, "item_ref": None, "kind": "notice"})])
    return "\n\n".join([
        f"== protocol.md, protocol_tab.md and protocol_decisions.md (appended to the system prompt) ==\n\n{PROTOCOL}",
        "== protocol_sdk.md (in place of protocol_tab.md for a session run in the wheelhouse) ==\n\n"
        + (Path(__file__).parent / "protocol_sdk.md").read_text(),
        f"== /wheelhouse skill ({PLUGIN_DIR / 'skills/wheelhouse/SKILL.md'}) ==\n\n{skill}",
        f"== MCP server \"wheelhouse\" ==\n\n{mcp_server.server.instructions}\n\n{tools}",
        f"== Notifications from the wheelhouse monitor ==\n\n{notices}",
        "== Launch command (new session; Restore and Adopt use --resume, and Adopt adds "
        "--system-prompt-snapshot off) ==\n\n"
        + "".join(f" \\\n    {a}" if a.startswith("-") else f" {shlex.quote(a)}" for a in argv).lstrip(),
    ])


def open_session(store: Store, sid: str, watch: TitleWatch | None = None) -> None:
    """Launch (or restore) a session the way it runs: a host for sdk, a tab for tab. A
    /rename made in Claude Code since the session's last rename is taken first, so the
    launch's -n carries it rather than the old name. watch: the app's own for the session,
    which has read the transcript already and only reads what is new."""
    session = store.session(sid)
    if session is None:
        raise KeyError(sid)
    renamed = (watch or TitleWatch(sid, cwd=session["cwd"])).read()
    if renamed and renamed_since(session, renamed.at):
        store.take_title(sid, renamed.at, renamed.title, renamed.inferred)
    (open_host if runner(session) == "sdk" else open_tab)(store, sid)


def restore_session(store: Store, sid: str, watch: TitleWatch | None = None) -> None:
    """Restore (or adopt) a session that isn't running, the way new sessions run now: a tab
    from before hosts existed comes back as a host unless WHEELHOUSE_RUNNER=tab. The check
    comes first, so a running tab is never recorded as hosted."""
    session = store.session(sid)
    if session is None:
        raise KeyError(sid)
    check_free(session)
    store.set_runner(sid, default_runner())
    open_session(store, sid, watch)


def check_free(session) -> None:
    """Refuse to launch a session that is running, or still starting from the last launch."""
    sid = session["id"]
    if liveness.is_alive(session["claude_pid"], session["claude_start"], session["boot_id"]):
        raise RuntimeError(f"session {session['name'] or sid} is already running")
    if liveness.status(session) == "starting":   # opening but hasn't registered yet
        raise RuntimeError(f"session {session['name'] or sid} is still starting")


def open_host(store: Store, sid: str) -> None:
    """Start the session's SDK host, detached so that the TUI closing never takes it down."""
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
