"""claude-wheelhouse: the TUI by default, plus the per-session processes it launches."""

import argparse
import os
import sys


def truecolour(environ=os.environ) -> None:
    """Windows Terminal draws 24-bit colour but doesn't tell WSL so: no COLORTERM, and TERM
    is xterm-256color. Rich then picks 256 colours, which turns the stats shimmer's gentle
    fades into a few hard jumps (#50). Textual reads this once, on import: call it first."""
    if environ.get("WT_SESSION") and not environ.get("COLORTERM") and "TEXTUAL_COLOR_SYSTEM" not in environ:
        environ["TEXTUAL_COLOR_SYSTEM"] = "truecolor"


def main() -> None:
    p = argparse.ArgumentParser(prog="claude-wheelhouse", description="Sidecar for parallel Claude Code sessions")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("tui", help="the wheelhouse app (default)")
    sub.add_parser("tutorial", help="start the tutorial afresh, then open the wheelhouse app")
    run = sub.add_parser("run", help="inside a launched tab: exec Claude for a session")
    run.add_argument("session_id")
    host = sub.add_parser("host", help="run a session through the Agent SDK (started by the wheelhouse)")
    host.add_argument("session_id")
    sub.add_parser("mcp", help="the per-session MCP server (stdio)")
    sub.add_parser("monitor", help="the per-session plugin monitor")
    sub.add_parser("protocol", help="print everything wheelhouse puts into a launched session")
    args = p.parse_args()

    if args.cmd == "run":
        from .launch import run as run_session
        run_session(args.session_id)
    elif args.cmd == "host":
        from .host import main as host_main
        host_main(args.session_id)
    elif args.cmd == "mcp":
        from .mcp_server import main as mcp_main
        mcp_main()
    elif args.cmd == "protocol":
        from .launch import injected
        print(injected())
    elif args.cmd == "tutorial":
        truecolour()
        from .store import Store
        from .tui import WheelhouseApp
        from .tutorial import start
        store = Store()
        try:
            start(store)
        except Exception as e:
            sys.exit(f"couldn't start the tutorial: {e}")
        WheelhouseApp(store).run()
    elif args.cmd == "monitor":
        from .monitor import main as monitor_main
        monitor_main()
    else:
        truecolour()
        from .tui import WheelhouseApp
        WheelhouseApp().run()
