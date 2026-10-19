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

from . import docroots, liveness
from .store import GONE_TEXT, SessionGone, Store, runner

HEARTBEAT_SECONDS = 30
STOP_AFTER = 1   # seconds after park_session or end_session replies before its Claude Code is stopped

server = MCPServer("wheelhouse", instructions="Wheelhouse: post and update tasks and questions; read "
                   "the person's answers. It tracks your subagents itself. See the wheelhouse protocol.")
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
def post_item(kind: str, title: str, body: str = "", status: str | None = None, why: str = "",
              alternative: str = "", reverse: str = "", path: str = "", parent: str = "",
              pinned: bool | None = None) -> str:
    """Create a task, question, agent, decision or doc item in the wheelhouse. kind: task |
    question | agent | decision | doc. Returns the item's ref (T1, Q1, A1, D1, M1...). Write
    the full detail in body, once. A decision (see the protocol) also needs why, one short
    line: the option passed over, the reason, how to undo it. It takes no status. The
    wheelhouse posts agent items for your subagents itself: don't post those. (alternative and
    reverse are an older protocol's: they join why.) Posting a task or question may also list
    what is still open: correct any of it that's finished.
    A doc is a markdown document you wrote for the person to read: a design, plan, report or
    review, wherever it lives, ~/.claude included. Not your scratch, working notes, briefs,
    hand-backs or memory files. Give its absolute path (the file must exist) and a short
    title, and parent, the ref of the task, question or decision it belongs to, if any.
    Post the same path again when you've changed the doc: the person sees it marked updated.
    pinned: kept out of reach of the person clearing their inbox. A doc is pinned unless you
    say pinned=false, for a scratch report not worth keeping once read; anything else is
    unpinned unless you say pinned=true. The person can pin or unpin any item, and a doc
    posted again keeps the pin they set."""
    if kind == "permission":   # the host posts these from Claude Code's own permission check
        raise ValueError("kind must be task, question, agent, decision or doc")
    if kind == "doc":
        if body or status or why or alternative or reverse:
            raise ValueError("a doc takes title, path and parent only: the file is its content")
        return _store.post_doc(_sid, title, path, parent or None, pinned is not False)
    if path or parent:
        raise ValueError("path and parent: only a doc takes these")
    if kind != "decision":   # refused by name: only a decision takes these
        ref = _store.post_item(_sid, kind, title, body, status, bool(pinned), why=why, alternative=alternative,
                               reverse=reverse)
        return ref + still_open() if kind in ("task", "question") else ref
    return _store.post_item(_sid, kind, title, body, status, bool(pinned), why=one_why(why, alternative, reverse))


# a change of work: a task finished or dropped, a question answered. Its reply lists what is
# still open (still_open), as posting a task or question does
FINISHING = {"done", "dropped", "answered"}


def still_open() -> str:
    """The items the wheelhouse doubts are still open (Store.in_doubt), to append to a reply
    that marks a change of work; nothing when none is in doubt."""
    doubted = _store.in_doubt(_sid)
    return f". Still open: {', '.join(doubted)}" if doubted else ""


def one_why(why: str, alternative: str = "", reverse: str = "") -> str:
    """A decision's why, with an older protocol's alternative and reverse folded in, so a
    session still writing three fields loses neither."""
    why, alternative, reverse = why.strip(), alternative.strip(), reverse.strip()
    head = ": ".join(p for p in (f"over {alternative}" if alternative else "", why) if p)
    return "; ".join(p for p in (head, f"undo: {reverse}" if reverse else "") if p)


@wheelhouse_tool
def update_item(ref: str, status: str | None = None, title: str | None = None,
                body: str | None = None, note: str | None = None) -> str:
    """Change an item's status, title or body, and/or append a progress note to its thread.
    In a session the wheelhouse runs itself, this is how you declare a question open or
    answered after the person's message on it (the wheelhouse posts what your turn ends by
    saying as the reply). Finishing a task, dropping one or answering a question may also
    list what is still open: correct any of it that's finished."""
    _store.update_item(_sid, ref, status=status, title=title, body=body, note=note, declares=True)
    return f"{ref} updated" + (still_open() if status in FINISHING else "")


@wheelhouse_tool
def reply(ref: str, text: str, status: str | None = None) -> str:
    """Answer the person in an item's thread, every time a message from them arrives on that
    ref (a note records progress; a reply is part of the conversation). On a question,
    status is required: "open" if you are still waiting on the person (you answered their
    clarification, or need more), "answered" once their input lets you proceed. On a task
    or agent it is optional. In a session the wheelhouse runs itself (no tab), the wheelhouse
    posts what your turn ends by saying as the reply on the ref the person's message was on: don't repeat
    it here, set the status with update_item instead. A reply that finishes a task or
    answers a question may also list what is still open: correct any of it that's finished."""
    _store.reply(_sid, ref, text, status, by_session=True)
    return f"replied on {ref}" + (f", now {status}" if status else "") + (still_open() if status in FINISHING else "")


@wheelhouse_tool
def set_synopsis(text: str) -> str:
    """Two or three sentences on what this session is doing, shown in the wheelhouse's
    description of the session. Set it early; update it when the session's focus shifts."""
    _store.set_synopsis(_sid, text)
    return "synopsis set"


@wheelhouse_tool
def set_doc_dirs(paths: list[str]) -> str:
    """Where this repo keeps its design decisions (ADRs), when that isn't docs/adr: directories
    relative to the git root of your working directory, such as ["docs/decisions"]. The
    wheelhouse then lists the NNNN-*.md files in them instead of docs/adr's, for every
    session in this repo. Only directories you name are looked in (.plan/, say, only if
    named), never the git root itself, and none outside a git repo. An empty list goes back
    to docs/adr."""
    session = _store.session(_sid)
    if session is None:   # gone since the tool's own check: said as it says it (GONE_TEXT)
        raise SessionGone(_sid)
    dirs = docroots.checked(paths, session["cwd"])
    _store.set_doc_dirs(_sid, dirs or None)
    return f"decisions are looked for in {', '.join(dirs)}" if dirs else "decisions are looked for in docs/adr"


HOSTED_TEXT = ("nothing to take: this session runs in the wheelhouse, so the person's messages "
               "arrive as your user turns")


def hosted_here(s) -> bool:
    """The session runs under its SDK host (not handed to a shell tab, whose monitor delivers).
    EXPERIMENTAL (features.py): SDK mode is not for use by anyone other than the author until
    Anthropic's terms are confirmed."""
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


def stop_soon(s) -> bool:
    """Stop this session's Claude Code STOP_AFTER seconds from now, once the tool's result has
    reached its transcript: only if it is the registered process (s, the row read before it
    changed) and this server's parent, as in a tab. A host's session needs nothing: its host
    exits once the row is parked or gone, which stops its Claude Code. Whether it will."""
    if s is None or not s["claude_pid"] or s["claude_pid"] != os.getppid():
        return False
    timer = threading.Timer(STOP_AFTER, liveness.stop, (s["claude_pid"], s["claude_start"], s["boot_id"]))
    timer.daemon = True
    timer.start()
    return True


@wheelhouse_tool
def park_session() -> str:
    """Park this session: it stops a moment after this returns, and shows dimmed at the
    foot of the wheelhouse's list until the person unparks it. Write your handoff notes into
    your items first: this is the last thing you do."""
    s = _store.session(_sid)
    _store.set_parked(_sid, True)
    return "parked; this session stops in a moment" if stop_soon(s) else "parked"


@wheelhouse_tool
def end_session() -> str:
    """Forget this session: deletes all of its wheelhouse data, and it stops a moment after
    this returns. If your own instructions describe anything to do when a
    session ends, do it before calling this: this is the last thing you do."""
    s = _store.session(_sid)
    _store.end(_sid)
    return ("forgotten; wheelhouse data deleted; this session stops in a moment" if stop_soon(s)
            else "forgotten; wheelhouse data deleted")


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
