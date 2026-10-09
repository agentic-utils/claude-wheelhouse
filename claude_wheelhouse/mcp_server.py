"""The wheelhouse MCP server, one per launched session (stdio).

It keeps a heartbeat going, which is only a 'stalled' hint; see liveness.py. The
session's Claude process was registered by `claude_wheelhouse run` before it exec'd Claude.
"""

import functools
import os
import threading
import time

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .store import GONE_TEXT, SessionGone, Store, runner

HEARTBEAT_SECONDS = 30

server = MCPServer("wheelhouse", instructions="Wheelhouse: post and update tasks, questions and "
                   "subagent statuses; read the person's answers. See the wheelhouse protocol.")
TOOLS = []   # registered tool functions, in order: `claude-wheelhouse protocol` lists them
_store: Store | None = None
_sid = ""


def wheelhouse_tool(fn):
    """Register a tool that answers plainly once the session's wheelhouse data has gone,
    instead of failing with a raw database error. A refused call (bad kind, status or ref)
    goes back as a ToolError so the session reads the reason, not a bare failure."""
    @functools.wraps(fn)
    def tool(*args, **kwargs):
        if _store.session(_sid) is None:
            return GONE_TEXT
        try:
            return fn(*args, **kwargs)
        except SessionGone:
            return GONE_TEXT
        except (ValueError, KeyError) as e:
            raise ToolError(e.args[0] if e.args else str(e)) from e
    TOOLS.append(fn)
    return server.tool()(tool)


@wheelhouse_tool
def post_item(kind: str, title: str, body: str = "", status: str | None = None,
              alternative: str = "", why: str = "", reverse: str = "") -> str:
    """Create a task, question, agent or decision item in the wheelhouse. kind: task |
    question | agent | decision. Returns the item's ref (T1, Q1, A1, D1...). Write the full
    detail in body, once. A decision (see the protocol) also needs alternative, why and
    reverse (how to undo it), and takes no status."""
    if kind == "permission":   # the host posts these from Claude Code's own permission check
        raise ValueError("kind must be task, question, agent or decision")
    return _store.post_item(_sid, kind, title, body, status,
                            alternative=alternative, why=why, reverse=reverse)


@wheelhouse_tool
def update_item(ref: str, status: str | None = None, title: str | None = None,
                body: str | None = None, note: str | None = None) -> str:
    """Change an item's status, title or body, and/or append a progress note to its thread."""
    _store.update_item(_sid, ref, status=status, title=title, body=body, note=note)
    return f"{ref} updated"


@wheelhouse_tool
def reply(ref: str, text: str, status: str | None = None) -> str:
    """Answer the person in an item's thread, every time a message from them arrives on that
    ref (a note records progress; a reply is part of the conversation). On a question,
    status is required: "open" if you are still waiting on the person (you answered their
    clarification, or need more), "answered" once their input lets you proceed. On a task
    or agent it is optional."""
    _store.reply(_sid, ref, text, status)
    return f"replied on {ref}" + (f", now {status}" if status else "")


@wheelhouse_tool
def set_synopsis(text: str) -> str:
    """Two or three sentences on what this session is doing, shown in the wheelhouse's
    description of the session. Set it early; update it when the session's focus shifts."""
    _store.set_synopsis(_sid, text)
    return "synopsis set"


HOSTED_TEXT = ("nothing to take: this session runs in the wheelhouse, so the person's messages "
               "arrive as your user turns")


def hosted_here(s) -> bool:
    """The session runs under its SDK host (not handed to a shell tab, whose monitor delivers)."""
    session = s.session(_sid)
    return session is not None and runner(session) == "sdk" and not session["shell"]


@wheelhouse_tool
def get_input(ref: str | None = None, message_id: int | None = None) -> str:
    """Without arguments: the person's undelivered messages (marked delivered).
    With ref: that item's full body and thread (its messages count as delivered).
    With message_id: the full text of one message a notification cut short.
    In a session the wheelhouse runs itself (no tab), the person's messages arrive as your
    user turns: there this only reads an item's thread, and takes nothing."""
    s = _store
    hosted = hosted_here(s)
    if hosted and ref is None and message_id is None:
        return HOSTED_TEXT
    if message_id is not None:
        m = s.message(_sid, message_id)
        return f"[{m['item_ref'] or 'general'}] {m['body']}" if m else f"no message {message_id}"
    if ref:
        item = s.item(_sid, ref)
        if item is None:
            return f"no item {ref}"
        if not hosted:
            s.take_pending(_sid, ref)   # shown in the thread below, so the monitor mustn't repeat them
        lines = [f"{ref} [{item['status']}] {item['title']}", item["body"], ""]
        # a tab session leaves out what the monitor has claimed and is printing right now. A
        # hosted one shows the person's every sent message, saying which reach it as a user
        # turn, so it neither acts on one twice nor misses one the host sent but hasn't marked
        for m in s.thread(_sid, ref, in_flight=hosted):
            if m["draft"]:
                continue
            via = ""
            if hosted and m["author"] == "person" and m["delivered_at"] is None:   # claimed: already passed on
                via = " (passed to you)" if m["claimed_at"] else " (reaches you as a user turn)"
            lines.append(f"{m['author']} @ {m['created_at']}{via}: {m['body']}")
        return "\n".join(lines)
    msgs = s.take_pending(_sid)
    return "\n\n".join(f"[{m['item_ref'] or 'general'}] {m['body']}" for m in msgs) or "nothing new"


@wheelhouse_tool
def list_items(include_closed: bool = False) -> str:
    """This session's items, one per line."""
    rows = _store.items(_sid, include_closed=include_closed)
    return "\n".join(f"{r['ref']} [{r['status']}] {r['title']}" for r in rows) or "no items"


@wheelhouse_tool
def park_session() -> str:
    """Park this session: hidden from the inbox and off the restore list until resumed."""
    _store.set_parked(_sid, True)
    return "parked; the person can close this tab"


@wheelhouse_tool
def end_session() -> str:
    """End this session for good: deletes all of its wheelhouse data. If your own instructions
    describe anything to do when a session ends, do it before calling this."""
    _store.end(_sid)
    return "ended; wheelhouse data deleted; the person can close this tab"


def _heartbeat() -> None:
    store = Store(_store.path)   # own connection: this runs on another thread
    while True:
        time.sleep(HEARTBEAT_SECONDS)
        try:
            store.heartbeat(_sid)
        except Exception:
            pass   # a missed beat only risks a 'stalled' hint


def main() -> None:
    global _store, _sid
    _sid = os.environ["WHEELHOUSE_SESSION_ID"]
    _store = Store()
    _store.heartbeat(_sid)
    threading.Thread(target=_heartbeat, daemon=True).start()
    server.run("stdio")
