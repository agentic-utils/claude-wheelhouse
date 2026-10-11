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
    before or after tui or tutorial: a subcommand's defaults are left out (SUPPRESS), so it
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
    except (NoDaemon, TokenError, OSError) as e:
        sys.exit(f"claude-wheelhouse: {e}")
    app = WheelhouseApp(client=client)   # which lets go of the client as it closes
    app.said += [(text, "information") for text in said]
    app.run()


def main() -> None:
    # --enable-sdk: experimental SDK mode (features.py), deliberately left out of --help.
    # Taken before or after the command, for the commands that create sessions
    sdk = argparse.ArgumentParser(add_help=False)
    sdk.add_argument(features.FLAG, dest="enable_sdk", action="store_true", default=argparse.SUPPRESS,
                     help=argparse.SUPPRESS)
    p = argparse.ArgumentParser(prog="claude-wheelhouse", description="Sidecar for parallel Claude Code sessions",
                                parents=[sdk])
    client_args(p)
    sub = p.add_subparsers(dest="cmd")
    client_args(sub.add_parser("tui", help="the wheelhouse app (default)", parents=[sdk]), sub=True)
    client_args(sub.add_parser("tutorial", help="start the tutorial afresh, then open the wheelhouse app",
                               parents=[sdk]), sub=True)
    run = sub.add_parser("run", help="inside a launched tab: exec Claude for a session")
    run.add_argument("session_id")
    host = sub.add_parser("host", help="inside the wheelhouse: a session's host process (started by the wheelhouse)")
    host.add_argument("session_id")
    sub.add_parser("mcp", help="the per-session MCP server (stdio)")
    sub.add_parser("monitor", help="the per-session plugin monitor")
    sub.add_parser("protocol", help="print everything wheelhouse puts into a launched session")
    daemon = sub.add_parser("daemon", help="serve the wheelhouse over HTTP (the app starts it, or joins it)",
                            parents=[sdk])
    daemon.add_argument("--address", default=None, help="the IP address to listen on (default 127.0.0.1; "
                        "never 0.0.0.0): the VPN interface's, to reach it from another machine")
    daemon.add_argument("--port", type=int, default=None, help="the port to listen on (default 7428)")
    daemon.add_argument("--name", action="append", default=[], help="another name clients reach this machine by "
                        "(a VPN's name for it), so their requests are accepted: repeat for more")
    daemon.add_argument("--stop", action="store_true", help="stop the daemon running, by its pid (no token "
                        "needed): the next start reads the token file again, or makes one if there's none")
    daemon.add_argument("--start-pipe", type=int, default=None, help=argparse.SUPPRESS)   # connect.start's
    sub.add_parser("token", help="print the daemon's token, for a client on another machine (--connect)")
    args = p.parse_args()
    if getattr(args, "enable_sdk", False):
        features.enable_sdk()

    if args.cmd == "run":
        from .launch import run as run_session
        run_session(args.session_id)
    elif args.cmd == "host":
        from .host import main as host_main
        host_main(args.session_id)
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
        from .daemon import ADDRESS, PORT, AlreadyRunning, main as daemon_main
        try:
            daemon_main(args.address or ADDRESS, args.port or PORT, args.name, args.start_pipe)
        except (AlreadyRunning, TokenError, ValueError) as e:
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
        from .store import Store
        from .tutorial import start
        store = Store()
        try:
            start(store, sdk=features.sdk_enabled())
        except Exception as e:
            sys.exit(f"couldn't start the tutorial: {e}")
        run_app(args, store)
    elif args.cmd == "monitor":
        from .monitor import main as monitor_main
        monitor_main()
    else:
        run_app(args)
