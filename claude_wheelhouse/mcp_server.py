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

from . import docroots, liveness, steps
from .store import GONE_TEXT, SessionGone, Store, redacted, runner

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
              pinned: bool | None = None, url: str = "") -> str:
    """Create a task, question, agent, decision, doc or pr item in the wheelhouse. kind: task |
    question | agent | decision | doc | pr. Returns the item's ref (T1, Q1, A1, D1, M1, PR108...). Write
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
    posted again keeps the pin they set.
    A pr is a pull request you made: the wheelhouse finds those made with `gh pr create` (or
    the GitHub MCP server's create_pull_request) itself. Post one you made some other way (the
    web, another tool) with its url, https://github.com/<owner>/<repo>/pull/<n>, and a title;
    its ref is the PR's number. Post the same url again with parent, the ref of the task it was
    made for, to file it there. It takes no status: GitHub's is shown."""
    if kind == "permission":   # the host posts these from Claude Code's own permission check
        raise ValueError("kind must be task, question, agent, decision, doc or pr")
    if kind == "pr":
        if body or status or why or alternative or reverse or path:
            raise ValueError("a pr takes title, url, parent and pinned only: its status comes from GitHub")
        return _store.post_pr(_sid, url, title, parent or None, bool(pinned))
    if url:
        raise ValueError("url: only a pr takes this")
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
def write_handoff(text: str) -> str:
    """Write this session's handoff: a short markdown note of what was done, decisions and
    why, open threads, and the next step, terse enough to read at a glance (16 KB at most).
    Each call replaces the last. The wheelhouse keeps it, shows it to the person as this
    session's handoff doc, and gives it back to you on resume and after compaction. Write it
    with this tool, never as a file. Call it when a wheelhouse message asks you to, and
    whenever your own instructions say to keep a handoff."""
    path = steps.write_handoff(_store, _sid, text)
    ref = _store.post_doc(_sid, "handoff", str(path), None, True)
    return f"handoff written ({ref})"


@wheelhouse_tool
def set_doc_dirs(paths: list[str]) -> str:
    """Where this repo keeps its design decisions (ADRs), when that isn't docs/adr: directories
    relative to the git root of your working directory, such as ["docs/decisions"], or in
    another git repo, such as a sibling docs repo (["../myproj-docs/decisions"], or an
    absolute path). The wheelhouse then lists the NNNN-*.md files in them instead of
    docs/adr's, for every session in this repo. Only directories you name are looked in
    (.plan/, say, only if named), never a repo's root itself, and none outside a git repo. An
    empty list goes back to docs/adr."""
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
def request_compact(keep: str) -> str:
    """Ask the wheelhouse to compact this session once your current turn ends, never during
    it, with `keep` as the compaction's instructions: what to carry through (open threads,
    decisions, files, the next step). For long autonomous runs whose context has grown large.
    Write your handoff first if your instructions keep one, then end your turn soon. Only in
    a session run inside the wheelhouse (SDK mode), not a terminal tab; one request at a time,
    and not again within 30 minutes of the last."""
    if not keep.strip():
        raise ValueError("keep must say what to carry through the compaction")
    if not hosted_here(_store):
        raise ValueError("request_compact works only in a session run inside the wheelhouse (SDK mode); "
                         "this one runs in a terminal tab, where the person can type /compact")
    _store.request_compact(_sid, keep.strip())
    return "compaction requested: it runs once this turn ends, with your keep text as its instructions"


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
        return f"[{m['item_ref'] or 'general'}] {redacted(m['body'])}" if m else f"no message {message_id}"
    if ref:
        item = s.item(_sid, ref)
        if item is None:
            return f"no item {ref}"
        if not hosted:
            s.take_pending(_sid, ref)   # shown in the thread below, so the monitor mustn't repeat them
            s.settle_mcp_replies(_sid)   # a reply on an MCP question: noted, its code redacted, never acted on
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
            lines.append(f"{m['author']} @ {m['created_at']}{via}: {redacted(m['body'])}")
        return "\n".join(lines)
    msgs = s.take_pending(_sid)   # never a reply on an MCP question (Store.claim): settled instead
    s.settle_mcp_replies(_sid)
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
def park_session(compact: bool = True, keep: str = "", reason: str = "") -> str:
    """Park this session: it stops once this turn ends, and shows dimmed at the foot of the
    wheelhouse's list until the person unparks it. Write your handoff notes into your items
    (and write_handoff, if your instructions keep one) first: this is the last thing you do.
    Call it when the wheelhouse asks you to park. Only in a session run inside the wheelhouse
    (SDK mode), also call it unasked once you have come to the end of the useful work and are
    only waiting on the person; never unasked in a terminal tab, where the person may be at
    the keyboard. In SDK mode, parking unasked compacts it first,
    once this turn ends, with `keep` as the compaction's instructions (what to carry through).
    compact=false parks it as it is, and needs `reason`, shown to the person: why compacting
    is undesirable (for example, you expect them to Stop & Forget it rather than carry on).
    Either way it stops once this turn ends. Answering the wheelhouse's park request, the
    person's choice decides, and these are ignored but for `keep`. In a terminal tab nothing
    compacts, and these are ignored."""
    s = _store.session(_sid)   # for stop_soon: the registered process, before parking
    hosted = hosted_here(_store)
    compacts = _store.park(_sid, compact, keep, reason, hosted=hosted)
    if not hosted:
        return "parked; this session stops in a moment" if stop_soon(s) else "parked"
    if compacts:
        return "parked; it is compacted once this turn ends, then stops: end your turn now"
    return "parked; this session stops once this turn ends: end your turn now"


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
