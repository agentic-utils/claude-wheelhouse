"""claude-wheelhouse: the TUI by default, plus the per-session processes it launches."""

import argparse
import errno
import os
import sys

from . import features


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
        hosted = host.parse_args(rest[1:])
        if hosted.keepalive_hours is not None:
            features.set_keepalive_hours(hosted.keepalive_hours)
        from .host import main as host_main
        host_main(hosted.session_id)
        return
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
                       "5 minutes (oneshot, the default), or one Claude Code kept running (persistent)")
    # --port: the local daemon's (features.port), before or after the command, for the app,
    # which starts the daemon on it or warns if the one it joins listens elsewhere, and the daemon
    port = argparse.ArgumentParser(add_help=False)
    port.add_argument(features.PORT_FLAG, dest="port", metavar="PORT", default=argparse.SUPPRESS,
                      help=f"the local daemon's port (default {features.PORT}, or WHEELHOUSE_PORT, or port= in a "
                      ".env): another for each user on the machine who runs a wheelhouse")
    # --keepalive-hours: how long a quiet session is kept warm (features.keepalive_hours),
    # before or after the command, for the app and the daemon, which keeps tab sessions warm
    keep = argparse.ArgumentParser(add_help=False)
    keep.add_argument(features.KEEPALIVE_FLAG, dest="keepalive_hours", metavar="N", default=argparse.SUPPRESS,
                      help=f"keep a quiet session's prompt cache warm for up to N hours (default "
                      f"{features.KEEPALIVE_HOURS:g}, or keepalive-hours= in a .env; 0 turns it off)")
    p = argparse.ArgumentParser(prog="claude-wheelhouse", description="Sidecar for parallel Claude Code sessions",
                                parents=[sdk, usage, port, keep])
    client_args(p)
    sub = p.add_subparsers(dest="cmd")
    client_args(sub.add_parser("tui", help="the wheelhouse app (default)", parents=[sdk, usage, port, keep]), sub=True)
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
    sub.add_parser("mcp", help="the per-session MCP server (stdio)")
    sub.add_parser("monitor", help="the per-session plugin monitor")
    sub.add_parser("protocol", help="print everything wheelhouse puts into a launched session", parents=[sdk])
    daemon = sub.add_parser("daemon", help="serve the wheelhouse over HTTP (the app starts it, or joins it)",
                            parents=[sdk, usage, port, keep])
    daemon.add_argument("--address", default=None, help="the IP address to listen on (default 127.0.0.1; "
                        "never 0.0.0.0): the VPN interface's, to reach it from another machine")
    daemon.add_argument("--name", action="append", default=[], help="another name clients reach this machine by "
                        "(a VPN's name for it), so their requests are accepted: repeat for more")
    daemon.add_argument("--stop", action="store_true", help="stop the daemon running, by its pid (no token "
                        "needed): the next start reads the token file again, or makes one if there's none")
    daemon.add_argument("--start-pipe", type=int, default=None, help=argparse.SUPPRESS)   # connect.start's
    sub.add_parser("token", help="print the daemon's token, for a client on another machine (--connect)")
    args = p.parse_args()
    if getattr(args, "enable_sdk", False):
        features.enable_sdk()
    if getattr(args, "usage_mode", None):
        features.set_usage_mode(args.usage_mode)
    if getattr(args, "port", None):
        features.set_port(args.port)
    if getattr(args, "keepalive_hours", None) is not None:
        features.set_keepalive_hours(args.keepalive_hours)

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
    elif args.cmd == "monitor":
        from .monitor import main as monitor_main
        monitor_main()
    else:
        run_app(args)
