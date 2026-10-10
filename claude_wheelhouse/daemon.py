"""The daemon: the wheelhouse's one hub, served over HTTP to its clients (.plan/remote.md,
phase 2). `claude-wheelhouse daemon` runs it, one per user, held by a lock file in the
state dir. No client uses it yet: the TUI still owns a hub of its own.

Queries are GETs answering JSON: the store's rows as they are, with what the hub works out
beside them. Commands are POSTs, each with an Idempotency-Key, answering what the hub's
command did (Done): a request sent again with the same key, within a day, gets the same
answer and is done once. What has moved goes out on GET /events, as Server-Sent Events:
invalidations, not data ({seq, kind, sid?, ref?}), each with a monotonic id, kept in a
short ring so a client reconnecting with Last-Event-ID gets what it missed, or "refetch"
when the ring no longer holds it. The hub's own notices (a relaunch finishing, a subagent
sync failing) go out the same way, as "notice".

It never binds 0.0.0.0: 127.0.0.1 by default, or the address given (the VPN interface's;
a LAN address would open it to everyone on that LAN). Every request must name the daemon in
its Host, and a POST from a browser page must come from the daemon's own origin, so a
hostile page can't drive it or read it by DNS rebinding. The Idempotency-Key every command
needs is the guard against cross-site requests too: a custom header makes a page's request
a CORS preflight, which the daemon never grants, so a page can't send a command without
it. It must never become optional."""

import asyncio
import contextlib
import fcntl
import functools
import hashlib
import ipaddress
import json
import os
import sqlite3
import sys
import threading
import time
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import uvicorn
from sse_starlette import EventSourceResponse
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import stats
from .hub import Done, Event, Hub, host_context
from .store import SessionGone, Store, inbox_rank, mode, runner, standing

ADDRESS, PORT = "127.0.0.1", 7428
TICK = 1.0           # seconds between the hub's refreshes, as the TUI's tick
RING = 1024          # events kept for a client that reconnects
PING = 15            # seconds between keep-alive comments on a quiet stream
LOCK = "daemon.lock"
# what a change in the database may have moved: every session's row, the inbox, the threads,
# and the conversations, which show what's queued
STORE_KINDS = ("sessions", "items", "thread", "conversation")
# each table the queries read besides the sessions, marked by a digest of every row as it is,
# so any change a client could see moves it, two edits in the same second and a draft taken
# back and queued again under its id too (a few ms each on a wheelhouse of 400 items and 700
# messages): read only when data_version says something was written, which a host's
# heartbeat does every second
MARKS = {"items": "SELECT * FROM items ORDER BY id", "messages": "SELECT * FROM messages ORDER BY id"}
# what each mark moving moves: "sessions" is the rows but for heartbeat_at, "listed" what the
# inbox shows of each session (its name, and whether it's parked, which leaves its items
# out), "named" what a conversation shows of it (its name, its folder's name for a tab
# with none, and whether it runs in a tab)
MOVES = {"sessions": {"sessions"}, "listed": {"items"}, "named": {"conversation"}, "items": {"items", "thread"},
         "messages": {"items", "thread", "conversation"}}
HOW = {"ask": Hub.ask, "now": Hub.act_on_dead, "cancel": Hub.cancel_request, "force": Hub.force}


class AlreadyRunning(RuntimeError):
    pass


class Refused(Exception):
    """A request answered with an error status and its reason."""

    def __init__(self, status: int, error: str):
        super().__init__(error)
        self.status, self.error = status, error


def checked_address(address: str) -> str:
    """The address to bind: an IP address, never the unspecified one (0.0.0.0 or ::)."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        raise ValueError(f"{address!r} isn't an IP address: give 127.0.0.1 or the VPN interface's") from None
    if ip.is_unspecified:
        raise ValueError(f"refusing {address}: it would listen on every interface. Give 127.0.0.1 or the "
                         "VPN interface's address")
    return address


def hosts(address: str, port: int, names=()) -> set[str]:
    """The Host headers that name the daemon: its address and port, localhost too when it's
    on the loopback, and any other names it's reached by (a VPN's name for the machine)."""
    ip = ipaddress.ip_address(address)
    shown = f"[{address}]" if ip.version == 6 else address
    return {f"{h}:{port}" for h in (shown, *names, *(["localhost"] if ip.is_loopback else []))}


def take_lock(state_dir, url: str):
    """The lock that makes this the user's one daemon, held while the returned file is open;
    AlreadyRunning, naming the one that holds it, if another does."""
    f = open(state_dir / LOCK, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.seek(0)
        held = f.read().strip()
        f.close()
        raise AlreadyRunning(f"a wheelhouse daemon is already running: {held or 'pid unknown'}") from None
    f.seek(0)
    f.truncate()
    f.write(f"pid {os.getpid()} at {url}\n")
    f.flush()
    return f


class Feed:
    """The events, each with a seq one past the last. Seqs start from the clock, in
    milliseconds, so they keep rising across a restart: an id from an earlier daemon is
    older than this one's ring, and its client refetches."""

    def __init__(self, ring: int = RING, start: int | None = None):
        self.seq = int(time.time() * 1000) if start is None else start
        self.ring: deque[dict] = deque(maxlen=ring)
        self.moved = asyncio.Event()   # set, and replaced, at each event
        self.closed = False

    def publish(self, kind: str, sid: str | None = None, ref: str | None = None, **more) -> None:
        self.seq += 1
        event = {"seq": self.seq, "kind": kind, **({"sid": sid} if sid else {}), **({"ref": ref} if ref else {}),
                 **more}
        self.ring.append(event)
        self.moved.set()
        self.moved = asyncio.Event()

    def since(self, last: int | None) -> list[dict] | None:
        """The events after last, or None when the ring doesn't hold them all (or last is
        none, or not one of this daemon's): then the client refetches everything."""
        first = self.ring[0]["seq"] if self.ring else self.seq + 1
        if last is None or not first - 1 <= last <= self.seq:
            return None
        return [e for e in self.ring if e["seq"] > last]

    async def stream(self, last: int | None):
        """A client's events, from after last (its Last-Event-ID). A client that connects
        afresh, or falls behind the ring (reconnecting, or reading slower than events come),
        is told to refetch, at the latest seq, and carries on from there. Ends once closed."""
        while True:
            moved = self.moved
            events = self.since(last)
            if events is None:
                events = [{"seq": self.seq, "kind": "refetch"}]
            for e in events:
                yield {"id": str(e["seq"]), "data": json.dumps(e)}
            last = events[-1]["seq"] if events else last
            if self.closed:
                return
            await moved.wait()

    def close(self) -> None:
        """Every stream ends, once it has sent what it has: the daemon is stopping."""
        self.closed = True
        self.moved.set()


def digest(rows) -> bytes:
    """A mark of rows that moves with any change to any of them."""
    h = hashlib.sha1()
    for row in rows:
        h.update(repr(tuple(row)).encode())
    return h.digest()


def done_json(done: Done) -> dict:
    return {"ok": done.ok, "notices": [{"text": e.text, "severity": e.severity} for e in done.notices],
            "denied": done.denied, **({"sid": done.sid} if done.sid else {})}


def text(body: dict, key: str, required: bool = True) -> str | None:
    value = body.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise Refused(400, f"{key} must be a string")
    return value


class Daemon:
    """The hub, its routes and its feed. The event loop only serves: the hub runs on a
    thread of its own (on_hub), its tick, its commands and its callbacks one at a time, as
    it runs on the TUI's loop, and the queries that read the store or the disk run on
    starlette's threads. So a slow disk or a locked database never holds up /sessions,
    which serves the view the hub's thread last made, nor the event streams' pings.
    inline: the hub's own background work runs in the call that starts it, as in a test;
    otherwise on worker threads, its callbacks back on the hub's thread."""

    def __init__(self, store: Store, allowed: set[str], inline: bool = False):
        self.hub = Hub(store, **({} if inline else {"spawn": self.spawn, "post": self.post}))
        self.store, self.allowed = store, allowed
        self.feed = Feed()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="wheelhouse")
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wheelhouse-hub")
        self.loop_thread: int | None = None
        # a connection of its own, only to read data_version: it moves with every commit made
        # by any other connection, the hosts', the MCP servers' and the hub's own alike
        self.watch = sqlite3.connect(store.path, isolation_level=None, check_same_thread=False)
        self.read: tuple[int, dict] | None = None   # the marks last read, at that data_version
        self.marked: dict = {}                      # as last published
        self.shown: dict[str, str] = {}   # each session's status as last published
        self.view: list[dict] = []        # /sessions, as the hub's thread last made it
        self.unkept: dict[str, dict] = {}  # answers the store couldn't keep: kept here till a tick can
        self.hub.listen(self.heard)
        self.app = Starlette(routes=[
            Route("/sessions", self.query(self.sessions, run=None)),
            Route("/sessions", self.command(self.new_session), methods=["POST"]),
            Route("/sessions/{sid}/conversation", self.query(self.conversation, run=self.on_hub)),
            Route("/sessions/{sid}/{action}", self.command(self.session_command), methods=["POST"]),
            Route("/dispatch", self.command(lambda request, body: self.hub.send_all()), methods=["POST"]),
            Route("/items", self.query(self.items)),
            Route("/items/{sid}/{ref}/thread", self.query(self.thread)),
            Route("/items/{sid}/{ref}/permission", self.command(self.permission), methods=["POST"]),
            Route("/items/{sid}/{ref}/close", self.command(self.close), methods=["POST"]),
            Route("/fs", self.query(self.folders)),
            Route("/events", self.events),
        ], lifespan=self.lifespan)

    # the hub's work and what it says

    def spawn(self, work, group: str) -> None:
        self.pool.submit(work)

    def post(self, callback) -> None:
        """From a worker thread: the hub's callback, on the hub's thread. One that fails is
        said on stderr, with its traceback, as nothing awaits it."""
        def run():
            try:
                callback()
            except Exception as e:
                print(f"wheelhouse daemon: a callback failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
                traceback.print_exc()
        try:
            self.worker.submit(run)
        except RuntimeError:   # shut down: the daemon is stopping
            pass

    async def on_hub(self, work, *args):
        """work(*args) on the hub's thread, awaited."""
        if self.loop is None:   # a test's loop: the lifespan, which takes it, isn't run
            self.loop, self.loop_thread = asyncio.get_running_loop(), threading.get_ident()
        return await self.loop.run_in_executor(self.worker, functools.partial(work, *args))

    def publish(self, kind: str, sid: str | None = None, ref: str | None = None, **more) -> None:
        """An event, from any thread: the feed is the event loop's."""
        if self.loop is None or threading.get_ident() == self.loop_thread:
            self.feed.publish(kind, sid, ref, **more)
        else:
            with contextlib.suppress(RuntimeError):   # the loop has closed: the daemon is stopping
                self.loop.call_soon_threadsafe(functools.partial(self.feed.publish, kind, sid, ref, **more))

    def heard(self, event: Event) -> None:
        """What the hub says by itself, to every client: a notice as it is, and a transcript
        read that brought something as that session's conversation and stats (its context
        size) moving."""
        if event.kind == "notice":
            self.publish("notice", text=event.text, severity=event.severity)
        elif event.kind == "landed":
            self.view = self.session_views()   # its context size
            self.publish("conversation", event.sid)
            self.publish("stats", event.sid)

    def tick(self) -> None:
        """The hub's refresh, as the TUI's tick has it, then what moved."""
        self.keep_answers()
        hub, seen = self.hub, self.marks()   # before the refresh: see publish_moved
        hub.refresh()
        hub.read_contexts()
        hub.watch_agents()
        hub.fetch_usage()
        hub.finish_relaunches()
        self.publish_moved(seen)

    def keep_answers(self) -> None:
        """The answers the store couldn't keep when given, kept now if it can: until then a
        retry is answered from memory, so only a restart meanwhile could do one twice."""
        for key, answer in list(self.unkept.items()):
            try:
                self.store.answer(key, **answer)
            except sqlite3.Error:
                return
            del self.unkept[key]

    def data_version(self) -> int:
        return self.watch.execute("PRAGMA data_version").fetchone()[0]

    def marks(self) -> dict:
        """Each table's mark (MARKS), read again only if anything has been written since."""
        version = self.data_version()
        if self.read is None or self.read[0] != version:
            self.read = (version, {kind: digest(self.watch.execute(sql)) for kind, sql in MARKS.items()})
        return self.read[1]

    def publish_moved(self, marks: dict) -> None:
        """What has moved since last published: each kind whose mark has, and the status each
        session shows, which liveness and Relaunch change with no write. The sessions' mark
        is the rows the hub's refresh read, as /sessions serves them, but for heartbeat_at, so
        a host's heartbeat moves nothing. The tables' marks are read before that refresh: a
        write after them moves them again, for the next tick, and a client refetching on an
        event never gets the rows from before it."""
        rows = {s["id"]: s for s in self.hub.sessions}
        now = {**marks, "sessions": {sid: {**dict(s), "heartbeat_at": None} for sid, s in rows.items()},
               "listed": {sid: (s["name"], s["parked"]) for sid, s in rows.items()},
               "named": {sid: (s["name"], s["cwd"], runner(s), s["shell"]) for sid, s in rows.items()}}
        moved = set().union(*(MOVES[kind] for kind in now if now[kind] != self.marked.get(kind)))
        self.marked = now
        self.view = self.session_views()
        for kind in STORE_KINDS:
            if kind in moved:
                self.publish(kind)
        shown = {sid: self.hub.shown_status(sid) for sid in self.hub.by_id}
        for sid in shown.keys() & self.shown.keys():
            if shown[sid] != self.shown[sid]:
                self.publish("sessions", sid)
        self.shown = shown

    async def ticking(self) -> None:
        """A tick every TICK seconds. One that fails (the database locked, a session ending
        mid-tick) is said on stderr, its traceback the first time, and the next goes on."""
        last = None
        while True:
            try:
                await self.on_hub(self.tick)
            except Exception as e:
                said = f"{type(e).__name__}: {e}"
                print(f"wheelhouse daemon: a tick failed: {said}", file=sys.stderr, flush=True)
                if said != last:
                    traceback.print_exc()
                last = said
            await asyncio.sleep(TICK)

    @contextlib.asynccontextmanager
    async def lifespan(self, app):
        self.loop, self.loop_thread = asyncio.get_running_loop(), threading.get_ident()
        task = asyncio.create_task(self.ticking())
        try:
            yield
        finally:
            self.feed.close()
            task.cancel()
            self.shutdown()

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.worker.shutdown(wait=False, cancel_futures=True)

    # requests

    def check_host(self, request: Request) -> None:
        if request.headers.get("host") not in self.allowed:
            raise Refused(421, "this daemon isn't reached by that name")

    def query(self, handler, run=run_in_threadpool):
        """A GET: handler(request), run on starlette's threads by default (it reads the store
        or the disk), on the hub's (on_hub), or on the event loop (None: memory only)."""
        async def endpoint(request: Request) -> Response:
            try:
                self.check_host(request)
                return JSONResponse(handler(request) if run is None else await run(handler, request))
            except Exception as e:
                return refusal(e)
        return endpoint

    def command(self, handler):
        """A POST: from a client, not another site's page; with an Idempotency-Key, whose
        answer is kept, so the same request sent again is answered the same and done once.
        What it moved is published at once, not at the next tick, and the rows /sessions
        serves are read again first, so a client refetching on that event sees it."""
        async def endpoint(request: Request) -> Response:
            try:
                self.check_host(request)
                origin = request.headers.get("origin")
                if origin is not None and origin not in {f"http://{h}" for h in self.allowed}:
                    raise Refused(403, "requests from another site's page are refused")
                key = request.headers.get("idempotency-key")
                if not key:
                    raise Refused(400, "every command needs an Idempotency-Key")
                raw = await request.body()
                return await self.on_hub(self.carry_out, handler, request, key, raw)
            except Exception as e:
                return refusal(e)
        return endpoint

    def carry_out(self, handler, request: Request, key: str, raw: bytes) -> Response:
        """A command, on the hub's thread: one at a time, so the same key sent twice at once
        is still done once."""
        fingerprint = hashlib.sha256(f"{request.url.path}\n".encode() + raw).hexdigest()
        if (kept := self.unkept.get(key) or self.store.answered(key)) is not None:
            if kept["fingerprint"] != fingerprint:
                return refusal(Refused(422, "that Idempotency-Key was used for another request"))
            return Response(kept["body"], kept["status"], media_type="application/json",
                            headers={"Idempotent-Replayed": "true"})
        try:
            body = json.loads(raw) if raw else {}
            if not isinstance(body, dict):
                raise Refused(400, "the body must be a JSON object")
            response = JSONResponse(done_json(handler(request, body)))
        except json.JSONDecodeError:
            response = refusal(Refused(400, "the body isn't JSON"))
        except ANSWERED as e:   # anything else isn't kept: it escapes, so a retry tries again
            response = refusal(e)
        answer = {"fingerprint": fingerprint, "status": response.status_code, "body": response.body.decode()}
        try:
            self.store.answer(key, **answer)
        except sqlite3.Error as e:   # done, so kept all the same: a retry mustn't do it again
            print(f"wheelhouse daemon: couldn't keep the answer to {key} ({e}): kept in memory meanwhile",
                  file=sys.stderr, flush=True)
            self.unkept[key] = answer
        seen = self.marks()
        self.hub.refresh()   # what /sessions serves has the command's change before it's published
        self.publish_moved(seen)
        return response

    async def events(self, request: Request) -> Response:
        try:
            self.check_host(request)
        except Refused as e:
            return refusal(e)
        last = request.headers.get("last-event-id")
        return EventSourceResponse(self.feed.stream(int(last) if last and last.isdigit() else None), ping=PING)

    # queries

    def session_view(self, s) -> dict:
        sid = s["id"]
        follower = self.hub.contexts.get(sid)
        snap = stats.current(follower.snap, host_context(s)) if follower and follower.ready else None
        return {**dict(s), "status": self.hub.shown_status(sid), "mode": mode(s), "busy": self.hub.busy(sid),
                "stale": self.hub.stale(sid), "sends_now": self.hub.sends_now(sid),
                "context": {"tokens": snap.context, "window": snap.window, "estimate": snap.estimate}
                if snap and snap.context else None}

    def session_views(self) -> list[dict]:
        """Every session as the hub's last refresh read it: on the hub's thread."""
        return [self.session_view(s) for s in self.hub.sessions]

    def sessions(self, request: Request) -> list[dict]:
        return self.view

    def conversation(self, request: Request) -> list[dict]:
        blocks = self.hub.conversation(request.path_params["sid"])
        if blocks is None:
            raise SessionGone(request.path_params["sid"])
        return [{"who": who, "text": markdown} for who, markdown in blocks]

    def items(self, request: Request) -> list[dict]:
        """The inbox, or a session's (sid), ranked as the TUI ranks it, but for the item a
        client holds where it was (its own): finished ones only if asked for (finished)."""
        sid = request.query_params.get("sid") or None
        if sid:
            self.hub.row(sid)
        finished = request.query_params.get("finished", "").lower() in ("1", "true", "yes")
        processing = self.store.processing()
        queued = {(m["session_id"], m["item_ref"]) for m in self.store.drafts()}
        awaiting = self.store.awaiting()
        busy = queued | awaiting
        out = []
        for it in sorted(self.store.items(sid), key=lambda it: inbox_rank(it, processing, busy)):
            key, where = (it["session_id"], it["ref"]), standing(it, busy)
            if finished or where != "finished":
                out.append({**dict(it), "standing": where, "queued": key in queued,
                            "processing": key in processing, "awaiting": key in awaiting})
        return out

    def item(self, sid: str, ref: str):
        item = self.store.item(sid, ref)
        if item is None:
            self.hub.row(sid)   # SessionGone if that's why
            raise KeyError(f"no item {ref} in this session")
        return item

    def thread(self, request: Request) -> dict:
        sid, ref = request.path_params["sid"], request.path_params["ref"]
        item = self.item(sid, ref)
        return {"item": dict(item), "messages": [dict(m) for m in self.store.thread(sid, ref)]}

    def folders(self, request: Request) -> dict:
        """A directory's folders, for the New session picker: hidden ones left out, but for
        .worktrees, as the TUI's picker has them."""
        path = os.path.abspath(os.path.expanduser(request.query_params.get("path") or "~"))
        if not os.path.isdir(path):
            raise KeyError(f"no directory {path}")
        try:
            names = sorted(e.name for e in os.scandir(path)
                           if e.is_dir() and (not e.name.startswith(".") or e.name == ".worktrees"))
        except OSError as e:
            raise Refused(403, f"can't list {path}: {e.strerror}") from None
        return {"path": path, "parent": os.path.dirname(path), "dirs": names}

    # commands

    def new_session(self, request: Request, body: dict) -> Done:
        """A session in an existing directory, by its absolute path, run by a host: a tab
        would open on the home machine's screen, where nobody remote is."""
        cwd = text(body, "cwd")
        if not os.path.isabs(cwd) or not os.path.isdir(cwd):
            raise Refused(400, f"cwd must be an existing directory's absolute path: {cwd!r} isn't")
        if (text(body, "runner", False) or "sdk") != "sdk":
            raise Refused(400, "a session started remotely runs by a host: runner can only be sdk")
        return self.hub.new_session(cwd, text(body, "name", False) or "", text(body, "ticket", False) or "",
                                    text(body, "brief", False) or "", "sdk")

    def session_command(self, request: Request, body: dict) -> Done:
        """Relaunch with the host a client was shown, (pid, start time), stops that host and
        starts it again; without, it brings back a dead session. Park and End say how (HOW):
        ask a running session, act now on a dead one, cancel the request, or force it."""
        hub, sid, action = self.hub, request.path_params["sid"], request.path_params["action"]
        if action in ("park", "end"):
            how = body.get("how")
            if how not in HOW:
                raise Refused(400, f"how must be one of {', '.join(HOW)}")
            return HOW[how](hub, sid, action)
        if action == "relaunch":
            host = body.get("host")
            if host is None:
                return hub.relaunch(sid)
            if not (isinstance(host, list) and len(host) == 2 and all(isinstance(n, int) for n in host)):
                raise Refused(400, "host must be [pid, start time]")
            return hub.stop_host(sid, tuple(host))
        if action == "mode":
            want = text(body, "mode", False)
            if want is not None and want not in ("queued", "immediate"):
                raise Refused(400, "mode must be queued or immediate")
            if want is not None and want == mode(hub.row(sid)):
                return Done(False)   # last write wins: it's that already
            return hub.toggle_mode(sid)
        if action == "send":
            return hub.submit((sid, text(body, "ref", False)), text(body, "text"))
        if action == "rename":
            return hub.rename(sid, text(body, "name"))
        if action in ("interrupt", "compact", "shell"):
            return hub.host_command(sid, action)
        commands = {"restore": hub.restore, "unpark": hub.unpark, "dispatch": hub.send_queue}
        if action not in commands:
            raise Refused(404, f"no command {action}")
        return commands[action](sid)

    def permission(self, request: Request, body: dict) -> Done:
        """Allow, Always or Deny, once: a permission already decided, by another client or
        an outbox replayed late, is a conflict."""
        sid, ref = request.path_params["sid"], request.path_params["ref"]
        if self.item(sid, ref)["kind"] != "permission":
            raise Refused(400, f"{ref} isn't a permission")
        try:
            return self.hub.answer_permission(sid, ref, text(body, "decision"), text(body, "message", False) or "")
        except KeyError:
            raise Refused(409, f"{ref} has been decided already") from None

    def close(self, request: Request, body: dict) -> Done:
        closed = body.get("closed", True)
        if not isinstance(closed, bool):
            raise Refused(400, "closed must be true or false")
        return self.hub.close(request.path_params["sid"], request.path_params["ref"], closed)


# the errors a request is answered with, each as its status (refusal): a command's is kept
# with its key, as any other (the database locked) may not happen again
ANSWERED = (Refused, SessionGone, KeyError, ValueError)


def refusal(e: Exception) -> JSONResponse:
    """An error as its status: a session gone or anything else not there 404, a bad request
    400, and a Refused as it says. Anything else is the daemon's own failure, 500, said on
    stderr with its traceback."""
    if isinstance(e, Refused):
        status, error = e.status, e.error
    elif isinstance(e, (SessionGone, KeyError)):
        status, error = 404, str(e.args[0]) if isinstance(e, KeyError) and e.args else str(e)
    elif isinstance(e, ValueError):
        status, error = 400, str(e)
    else:
        status, error = 500, f"the daemon failed: {type(e).__name__}: {e}"
        print(f"wheelhouse daemon: {error}", file=sys.stderr, flush=True)
        traceback.print_exception(e)
    return JSONResponse({"error": error}, status)


class Server(uvicorn.Server):
    """uvicorn's, but a signal to stop ends the event streams first, each once it has sent
    what it has, so a client sees its stream end cleanly rather than cut off."""

    def __init__(self, daemon: Daemon, config: uvicorn.Config):
        super().__init__(config)
        self.daemon = daemon

    def handle_exit(self, sig, frame) -> None:
        if self.daemon.loop is not None:
            with contextlib.suppress(RuntimeError):   # the loop has closed already
                self.daemon.loop.call_soon_threadsafe(self.daemon.feed.close)
        super().handle_exit(sig, frame)


def main(address: str = ADDRESS, port: int = PORT, names=()) -> None:
    """Serve until stopped (Ctrl+C, or SIGTERM from systemd): one per user, or AlreadyRunning."""
    address = checked_address(address)
    allowed = hosts(address, port, names)
    store = Store()
    url = f"http://{address if ipaddress.ip_address(address).version == 4 else f'[{address}]'}:{port}"
    lock = take_lock(store.path.parent, url)
    try:
        daemon = Daemon(store, allowed)
        print(f"wheelhouse daemon on {url}, state in {store.path.parent}", flush=True)
        Server(daemon, uvicorn.Config(daemon.app, host=address, port=port, log_level="warning")).run()
    finally:
        lock.close()
