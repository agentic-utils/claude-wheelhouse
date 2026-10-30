"""claude-wheelhouse: the TUI by default, plus the per-session processes it launches."""

import argparse
import errno
import os
import sys
import textwrap

from . import features, steps


def truecolour(environ=os.environ) -> None:
    """Windows Terminal draws 24-bit colour but doesn't tell WSL so: no COLORTERM, and TERM
    is xterm-256color. Rich then picks 256 colours, which turns the stats shimmer's gentle
    fades into a few hard jumps (#50). Textual reads this once, on import: call it first."""
    if environ.get("WT_SESSION") and not environ.get("COLORTERM") and "TEXTUAL_COLOR_SYSTEM" not in environ:
        environ["TEXTUAL_COLOR_SYSTEM"] = "truecolor"


def client_args(p: argparse.ArgumentParser, sub: bool = False) -> None:
    """How the app reaches the hub: the local daemon by default, started if none runs. Given
    before or after tui: a subcommand's defaults are left out (SUPPRESS), so it
    never undoes what was given before it."""
    unset = {"default": argparse.SUPPRESS} if sub else {}
    how = p.add_mutually_exclusive_group()
    how.add_argument("--local", action="store_true", **unset, help="run the hub in this process, without the "
                     "daemon (the wheelhouse as it was before the daemon)")
    how.add_argument("--connect", metavar="HOST:PORT", **unset, help="use the daemon on another machine (never "
                     "started or stopped from here); its token from --token-file, else WHEELHOUSE_TOKEN, else asked for")
    p.add_argument("--token-file", metavar="PATH", **unset, help="with --connect: a file holding the daemon's token")


def run_app(args, store=None) -> None:
    """The app, through the client asked for: the local daemon's (started, or joined), another
    machine's (--connect), or a hub of its own (--local)."""
    truecolour()
    from .tui import WheelhouseApp
    if args.local:
        app = WheelhouseApp(store)
        from .connect import holder
        if holder(app.store.path.parent) is not None:   # as two apps did
            app.said.append(("a wheelhouse daemon is running too: two hubs are acting on the sessions", "warning"))
        app.run()
        return
    from .auth import TokenError
    from .connect import NoDaemon, given_token, local_client, remote_client
    from .store import db_path
    try:
        if args.connect:
            client, said = remote_client(args.connect, given_token(args.token_file))
        else:
            client, said = local_client(db_path().parent)
    except (NoDaemon, TokenError, OSError, features.BadPort) as e:
        sys.exit(f"claude-wheelhouse: {e}")
    app = WheelhouseApp(client=client)   # which lets go of the client as it closes
    app.said += [(text, "information") for text in said]
    app.run()


def tutorial_again(yes: bool) -> None:
    """claude-wheelhouse tutorial: the tutorial offered at the app's next start (tutorial.reset),
    once the person says yes (or --yes). Refused while a daemon runs: the app goes through
    it, and decides on the offer only as it starts. An app run with --local takes no lock,
    so it can't be seen here: its tutorial session is ended under it (the session's Claude
    Code sent SIGTERM, its row removed), and the offer waits for its next start."""
    from . import tutorial
    from .connect import STOP, holder
    from .store import Store, db_path
    def refuse_while_running() -> None:
        if held := holder(db_path().parent):
            sys.exit(f"claude-wheelhouse: the wheelhouse daemon is running ({held}): quit the app, stop the daemon "
                     f"with {STOP} (make stop in a source checkout), then run this again")
    refuse_while_running()
    if not yes:
        try:
            said = input("This ends the tutorial session, if there is one, and the wheelhouse offers the tutorial "
                         "when it next starts. Your other sessions are kept, except any you started in the tutorial's "
                         "own directory: those end too. Go ahead? [y/N] ")
        except EOFError:
            said = ""
        if said.strip().lower() not in ("y", "yes"):
            print("nothing changed")
            return
        refuse_while_running()   # again: the app may have been started while the question waited
    ended = tutorial.reset(Store())
    print(("ended the earlier tutorial session" if ended else "no tutorial session to end")
          + "; the wheelhouse's next start (make run in a source checkout) offers the tutorial")


def modules_command(action: str | None, name: str | None) -> None:
    """claude-wheelhouse modules: the modules installed (id, version, API, status), or with
    disable or enable, one switched off or on (api.DISABLED, by its entry point's name), which
    the daemon and the app read as they start: so said, with how to restart them if they run."""
    import json
    from . import api
    from .connect import STOP, holder
    from .store import Store, db_path
    if not db_path().exists():   # a machine whose app joins another's daemon (--connect): nothing here reads it
        sys.exit(f"claude-wheelhouse: no wheelhouse database on this machine ({db_path()}): run claude-wheelhouse "
                 "modules on the machine whose daemon your app connects to")
    store, loaded = Store(), api.load()
    off = api.disabled(store.setting(api.DISABLED))
    if action is None:
        for item in api.switch_off(loaded, off):
            m = item.module
            status = "disabled" if item.disabled else f"not running: {item.error}" if item.error else "on"
            print(f"{m.id if m else item.name}  {m.version if m else '?'}  API {m.api if m else '?'}  {status}")
        if not loaded:
            print("no modules installed")
        return
    found = next((i.name for i in loaded if name in (i.name, i.module and i.module.id)), None)
    if found is None and name not in off:
        sys.exit(f"claude-wheelhouse: no module {name!r} is installed (claude-wheelhouse modules lists them)")
    found = found or name
    off = off | {found} if action == "disable" else off - {found, name}
    if off:
        store.set_setting(api.DISABLED, json.dumps(sorted(off)))
    else:
        store.clear_setting(api.DISABLED)
    held = holder(db_path().parent)
    print(f"{found} {action}d: from the wheelhouse's next start"
          + (f" (the daemon is running: quit the app, then {STOP}, then start it again)" if held else ""))


def open_items() -> None:
    """claude-wheelhouse open-items: the session's open tasks and questions
    (launch.after_compaction), printed for the plugin's SessionStart hook after a compaction,
    which Claude Code adds to the context. Nothing when there are none, or no session."""
    from .launch import after_compaction
    from .store import Store
    sid = os.environ.get("WHEELHOUSE_SESSION_ID")
    if not sid:
        return
    store = Store()
    tab_compaction(store, sid, end=True)   # first: the reminder lists no compaction task
    if text := after_compaction(store, sid):
        print(f"[wheelhouse] {text}")


def tab_compaction(store, sid: str, end: bool = False, trigger: str = "auto") -> None:
    """A tab's compaction (#244), from the plugin's hooks: Claude Code's PreCompact starts a
    cycle at compacting (auto, its own at full context; manual, /compact typed: the person's),
    and its SessionStart after a compaction (end) completes it. The tab's own typing is the
    tab's: only what the wheelhouse sends it is held. Never fails the hook: a compaction goes
    ahead whatever the wheelhouse records."""
    try:
        row = store.compaction(sid)
        if end:
            if row is not None and row["state"] == "compacting":
                store.move_compaction(sid, row["id"], "completed", "compacted in its tab")
        else:
            store.start_compaction(sid, "auto" if trigger == "auto" else "person", "compacting")
    except Exception as e:   # a locked or missing database: the hook says nothing to Claude Code
        print(f"wheelhouse: compaction not recorded: {e}", file=sys.stderr)


def compaction_hook() -> None:
    """claude-wheelhouse compaction-start: the plugin's PreCompact hook in a launched tab."""
    import json
    sid = os.environ.get("WHEELHOUSE_SESSION_ID")
    try:
        trigger = json.loads(sys.stdin.read() or "{}").get("trigger", "auto")
    except ValueError:
        trigger = "auto"
    if sid:
        from .store import Store
        tab_compaction(Store(), sid, trigger=trigger)


ARGS_HEAD = ("The app's command line args (make args). Give them to make run in ARGS, for example\n"
             '  make run ARGS="--local --animation-fps 0"\n')


def args_text(p: argparse.ArgumentParser) -> str:
    """The app's options as --help gives them (none it hides), each with its help and, for a
    setting, its environment variable and .env key (features.SETTINGS): what make args prints."""
    fmt = p._get_formatter()
    out = [ARGS_HEAD]
    for action in p._actions:
        if not action.option_strings or action.help == argparse.SUPPRESS or isinstance(action, argparse._HelpAction):
            continue
        flag = fmt._format_action_invocation(action)
        if key := features.SETTINGS.get(action.option_strings[0]):
            flag += f"  (env WHEELHOUSE_{key}, .env {key}=)"
        out += [flag, textwrap.fill(action.help, 88, initial_indent="    ", subsequent_indent="    ")]
    return "\n".join(out)


def parser() -> argparse.ArgumentParser:
    """The command line: the app's options, and the subcommands'."""
    # --enable-sdk: experimental SDK mode (features.py), deliberately left out of --help.
    # Taken before or after the command, for the commands that create sessions, and protocol
    sdk = argparse.ArgumentParser(add_help=False)
    sdk.add_argument(features.FLAG, dest="enable_sdk", action="store_true", default=argparse.SUPPRESS,
                     help=argparse.SUPPRESS)
    # --usage-mode: how the stats pane's plan usage is read (features.usage_mode), before or
    # after the command, for those that run a hub: the app's, and the daemon's
    usage = argparse.ArgumentParser(add_help=False)
    usage.add_argument(features.USAGE_FLAG, dest="usage_mode", choices=features.USAGE_MODES,
                       default=argparse.SUPPRESS, help="how the usage bar reads /usage: a short claude -p every "
                       "5 minutes (oneshot, the default), or one Claude Code kept running (persistent); or "
                       "WHEELHOUSE_USAGE_MODE, or USAGE_MODE= in a .env")
    # --port: the local daemon's (features.port), before or after the command, for the app,
    # which starts the daemon on it or warns if the one it joins listens elsewhere, and the daemon
    port = argparse.ArgumentParser(add_help=False)
    port.add_argument(features.PORT_FLAG, dest="port", metavar="PORT", default=argparse.SUPPRESS,
                      help=f"the local daemon's port (default {features.PORT}, or WHEELHOUSE_PORT, or PORT= in a "
                      ".env): another for each user on the machine who runs a wheelhouse")
    # --keepalive-hours: how long a quiet session is kept warm (features.keepalive_hours),
    # before or after the command, for the app and the daemon, which keeps tab sessions warm
    keep = argparse.ArgumentParser(add_help=False)
    keep.add_argument(features.KEEPALIVE_FLAG, dest="keepalive_hours", metavar="N", default=argparse.SUPPRESS,
                      help=f"keep a quiet session's prompt cache warm for up to N hours (default "
                      f"{features.KEEPALIVE_HOURS:g}, or WHEELHOUSE_KEEPALIVE_HOURS, or KEEPALIVE_HOURS= in a .env; 0 turns it off)")
    keep.add_argument(steps.STEP_FLAG, dest="step", metavar="EVENT.SWITCH=on|off", action="append", default=None,
                      help="a session step's switch (steps.py), repeatable, such as pr_merged.cleanup=off; or "
                      "WHEELHOUSE_STEP_<EVENT>_<SWITCH>, or STEP_<EVENT>_<SWITCH>= in a .env")
    # --animation-fps: the app's animation frame rate (features.animation_fps), before or after tui
    fps = argparse.ArgumentParser(add_help=False)
    fps.add_argument(features.ANIMATION_FLAG, dest="animation_fps", metavar="N", default=argparse.SUPPRESS,
                     help=f"animate the title, the eyes and the stats bars N frames a second (default "
                     f"{features.ANIMATION_FPS:g}, or WHEELHOUSE_ANIMATION_FPS, or ANIMATION_FPS= in a .env; "
                     "0 turns animation off)")
    fps.add_argument(features.AWAY_FLAG, dest="away_hours", metavar="N", default=argparse.SUPPRESS,
                     help=f"after N hours with no key press, click or scroll, show what happened meanwhile "
                     f"(default {features.AWAY_HOURS:g}, or WHEELHOUSE_AWAY_HOURS, or AWAY_HOURS= in a .env; "
                     "0 turns it off)")
    fps.add_argument(features.AUTOCLEANSE_FLAG, dest="autocleanse_minutes", metavar="N", default=argparse.SUPPRESS,
                     help=f"hide a completed inbox item N minutes after it completed, until Alt+V shows it (default "
                     f"{features.AUTOCLEANSE_MINUTES:g}, or WHEELHOUSE_AUTOCLEANSE_MINUTES, or AUTOCLEANSE_MINUTES= "
                     "in a .env; 0 turns it off)")
    p = argparse.ArgumentParser(prog="claude-wheelhouse", description="Sidecar for parallel Claude Code sessions",
                                parents=[sdk, usage, port, keep, fps])
    client_args(p)
    sub = p.add_subparsers(dest="cmd")
    client_args(sub.add_parser("tui", help="the wheelhouse app (default)", parents=[sdk, usage, port, keep, fps]), sub=True)
    again = sub.add_parser("tutorial", help="have the app offer the tutorial at its next start, ending any "
                           "earlier tutorial session (run with the app and the daemon stopped)",
                           description="Has the app offer the tutorial at its next start, ending any earlier "
                           "tutorial session. Your other sessions are kept, except any you started in the "
                           "tutorial's own directory: those end too. "
                           "Run it with the app and the daemon stopped. It refuses while the "
                           "daemon runs, but an app started with --local has no daemon and can't be detected: "
                           "running this then stops that app's tutorial session and removes it, and the offer "
                           "shows only when the app next starts.")
    again.add_argument("--yes", "-y", action="store_true", help="don't ask first")
    run = sub.add_parser("run", help="inside a launched tab: exec Claude for a session")
    run.add_argument("session_id")
    run.add_argument(steps.STEP_FLAG, dest="step", action="append", default=None, help=argparse.SUPPRESS)
    sub.add_parser("mcp", help="the per-session MCP server (stdio)")
    sub.add_parser("monitor", help="the per-session plugin monitor")
    sub.add_parser("open-items", help="inside a launched tab: print the session's open items (the plugin's "
                   "hook, after a compaction)")
    sub.add_parser("compaction-start", help="inside a launched tab: record its compaction starting (the plugin's "
                   "PreCompact hook)")
    sub.add_parser("protocol", help="print everything wheelhouse puts into a launched session", parents=[sdk])
    notes = sub.add_parser("notes", help="print a release's notes as Markdown, from the features it registered "
                           "(whatsnew.py)")
    notes.add_argument("version", nargs="?", default=None, help="the release (default: this one's version)")
    daemon = sub.add_parser("daemon", help="serve the wheelhouse over HTTP (the app starts it, or joins it)",
                            parents=[sdk, usage, port, keep])
    daemon.add_argument("--address", default=None, help="the IP address to listen on (default 127.0.0.1; "
                        "never 0.0.0.0): the VPN interface's, to reach it from another machine")
    daemon.add_argument("--name", action="append", default=[], help="another name clients reach this machine by "
                        "(a VPN's name for it), so their requests are accepted: repeat for more")
    daemon.add_argument("--stop", action="store_true", help="stop the daemon running, by its pid (no token "
                        "needed): the next start reads the token file again, or makes one if there's none")
    daemon.add_argument("--start-pipe", type=int, default=None, help=argparse.SUPPRESS)   # connect.start's
    mods = sub.add_parser("modules", help="list the modules installed, or switch one off or on")
    mods.add_argument("action", nargs="?", choices=("disable", "enable"))
    mods.add_argument("name", nargs="?", help="the module's id, as the list shows it")
    mods.add_argument("--connect", metavar="HOST:PORT", help=argparse.SUPPRESS)   # refused: said why
    sub.add_parser("token", help="print the daemon's token, for a client on another machine (--connect)")
    return p


def main() -> None:
    # host: a hosted session's process, started by the wheelhouse in SDK mode alone
    # (launch.open_host runs `host <id>`), so taken before the main parser: neither --help
    # nor an invalid choice's message lists it, like --enable-sdk, which it takes before or
    # after it as every command does
    given = sys.argv[1:]
    rest = [a for a in given if a != features.FLAG]
    if rest[:1] == ["host"]:
        if len(rest) < len(given):
            features.enable_sdk()
        host = argparse.ArgumentParser(prog="claude-wheelhouse host")
        host.add_argument("session_id")
        host.add_argument(features.KEEPALIVE_FLAG, dest="keepalive_hours", default=None)
        host.add_argument(steps.STEP_FLAG, dest="step", action="append", default=None)
        hosted = host.parse_args(rest[1:])
        steps.set_flags(hosted.step)
        if hosted.keepalive_hours is not None:
            features.set_keepalive_hours(hosted.keepalive_hours)
        from .host import main as host_main
        host_main(hosted.session_id)
        return
    if rest[:1] == ["args"]:   # make args: hidden like host, as it's for make's help alone
        print(args_text(parser()))
        return
    args = parser().parse_args()
    if getattr(args, "enable_sdk", False):
        features.enable_sdk()
    if getattr(args, "usage_mode", None):
        features.set_usage_mode(args.usage_mode)
    if getattr(args, "port", None):
        features.set_port(args.port)
    if getattr(args, "keepalive_hours", None) is not None:
        features.set_keepalive_hours(args.keepalive_hours)
    for bad in steps.set_flags(getattr(args, "step", None)):   # said, then ignored
        print(f"claude-wheelhouse: {bad}", file=sys.stderr)
    if getattr(args, "animation_fps", None) is not None:
        features.set_animation_fps(args.animation_fps)
    if getattr(args, "away_hours", None) is not None:
        features.set_away_hours(args.away_hours)
    if getattr(args, "autocleanse_minutes", None) is not None:
        features.set_autocleanse_minutes(args.autocleanse_minutes)

    if args.cmd == "run":
        from .launch import run as run_session
        run_session(args.session_id)
    elif args.cmd == "mcp":
        from .mcp_server import main as mcp_main
        mcp_main()
    elif args.cmd == "daemon" and args.stop:
        from .connect import NoDaemon, halt
        from .store import db_path
        try:
            print(halt(db_path().parent))
        except NoDaemon as e:
            sys.exit(f"claude-wheelhouse: {e}")
    elif args.cmd == "daemon":
        from .auth import TokenError
        from .daemon import ADDRESS, AlreadyRunning, PortTaken, main as daemon_main
        try:
            daemon_main(args.address or ADDRESS, features.port(), args.name, args.start_pipe)
        except (AlreadyRunning, PortTaken, TokenError, ValueError) as e:
            sys.exit(str(e))
    elif args.cmd == "protocol":
        from .launch import injected
        print(injected(features.sdk_enabled()))
    elif args.cmd == "notes":
        from . import whatsnew
        print(whatsnew.notes(args.version or whatsnew.running_version()), end="")
    elif args.cmd == "token":
        from . import auth
        from .connect import NoDaemon, check
        from .store import db_path
        try:   # what the running daemon takes, said as the app says it when it doesn't
            token = check(db_path().parent)
            if exposed := auth.exposed(auth.path(db_path().parent)):   # as the daemon refuses it at start
                sys.exit(f"claude-wheelhouse: {exposed}")
            print(token)
        except NoDaemon as e:
            sys.exit(f"claude-wheelhouse: {e}")
        except auth.TokenError as e:
            first = " (the daemon makes it the first time it starts)" if e.errno == errno.ENOENT else ""
            sys.exit(f"claude-wheelhouse: {e}{first}")
    elif args.cmd == "tutorial":
        tutorial_again(args.yes)
    elif args.cmd == "modules":
        if args.connect:
            sys.exit("claude-wheelhouse: modules acts on this machine's wheelhouse only: run it on the machine "
                     f"whose daemon is at {args.connect}, then restart that daemon")
        if bool(args.action) != bool(args.name):
            sys.exit("claude-wheelhouse: modules disable or enable takes a module's id")
        modules_command(args.action, args.name)
    elif args.cmd == "open-items":
        open_items()
    elif args.cmd == "compaction-start":
        compaction_hook()
    elif args.cmd == "monitor":
        from .monitor import main as monitor_main
        monitor_main()
    else:
        run_app(args)
