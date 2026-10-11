"""The daemon: the wheelhouse's one hub, served over HTTP to its clients (.plan/remote.md,
phase 2). `claude-wheelhouse daemon` runs it, one per user, held by a lock file in the
state dir. The TUI starts it, or joins it, and acts through it (phase 2c); `--local`
still runs a hub of the TUI's own.

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
hostile page can't drive it or read it by DNS rebinding. Every request, the event stream's
too, must carry the shared secret in the state dir (auth.py) as a bearer token, else 401,
so neither another user on the machine nor another machine on the VPN can read or drive
it without being given it. The
Idempotency-Key every command needs is the guard against cross-site requests too: a custom
header makes a page's request a CORS preflight, which the daemon never grants, so a page
can't send a command without it. It must never become optional."""

import asyncio
import contextlib
import fcntl
import functools
import hashlib
import ipaddress
import json
import os
import signal
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
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import auth, fingerprint, liveness, models, stats, tutorial
from .hub import Done, Event, Hub, host_context
from .store import SessionGone, Store, inbox_rank, mode, runner, standing

ADDRESS, PORT = "127.0.0.1", 7428
TICK = 1.0           # seconds between the hub's refreshes, as the TUI's tick
RING = 1024          # events kept for a client that reconnects
PING = 15            # seconds between keep-alive comments on a quiet stream
READ_WAIT = 1.0      # seconds a conversation waits on a read of it under way before 503
LOCK = "daemon.lock"
LOCK_WAIT = 0.5      # seconds it waits on the lock: a client's glance at it is shorter
# what a change in the database may have moved: every session's row, the inbox, the threads,
# and the conversations, which show what's queued
STORE_KINDS = ("sessions", "items", "thread", "conversation")
# each table the queries read besides the sessions (store.MARKED) is marked by how many times
# it has been written, kept by triggers in the schema, so any change a client could see moves
# it, whoever writes it, two edits in the same second and a draft taken back and queued again
# under its id too. A read of two rows, whatever the history: a host's heartbeat, every
# second, writes only the sessions, which carry no mark
# what each mark moving moves: "sessions" is the rows but for heartbeat_at, "listed" what the
# inbox shows of each session (its name, and whether it's parked, which leaves its items
# out), "named" what a conversation shows of it (its name, its folder's name for a tab
# with none, and whether it runs in a tab)
MOVES = {"sessions": {"sessions"}, "listed": {"items"}, "named": {"conversation"}, "items": {"items", "thread"},
         "messages": {"items", "thread", "conversation"}}
HOW = {"ask": Hub.ask, "now": Hub.act_on_dead, "cancel": Hub.cancel_request, "force": Hub.force}
LAYOUT = "layout."   # the only settings a client reads and writes: the TUI's splitter sizes
# what the inbox leaves out of each item, as the item list shows none of it: the body and
# answer come with its thread, and seq is retired (store.RETIRED_TRIGGER)
UNLISTED = ("body", "answer", "seq")


class AlreadyRunning(RuntimeError):
    pass


class Refused(Exception):
    """A request answered with an error status and its reason."""

    def __init__(self, status: int, error: str, headers: dict | None = None):
        super().__init__(error)
        self.status, self.error, self.headers = status, error, headers


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
    on the loopback, and any other names it's reached by (a VPN's name for the machine).
"""
    ip = ipaddress.ip_address(address)
    shown = f"[{address}]" if ip.version == 6 else address
    return {f"{h}:{port}" for h in (shown, *names, *(["localhost"] if ip.is_loopback else []))}


def take_lock(state_dir):
    """The lock that makes this the user's one daemon, held while the returned file is open;
    AlreadyRunning, naming the one that holds it, if another does. It says only its pid
    until it's listening (announce): a client never goes to an address this daemon hasn't
    bound, where something else may be. The user's alone (0600)."""
    fd = os.open(state_dir / LOCK, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    os.fchmod(fd, 0o600)
    f = os.fdopen(fd, "r+")
    deadline = time.monotonic() + LOCK_WAIT
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() < deadline:   # a client looking at it (connect.holder) lets go at once
                time.sleep(0.02)
                continue
            f.seek(0)
            held = f.read().strip()
            f.close()
            raise AlreadyRunning(f"a wheelhouse daemon is already running: {held or 'pid unknown'}") from None
    say(f, f"pid {os.getpid()}")
    return f


def say(lock, text: str) -> None:
    lock.seek(0)
    lock.truncate()
    lock.write(f"{text}\n")
    lock.flush()


def announce(lock, url: str) -> None:
    """The address, in the lock, once the daemon is listening there."""
    say(lock, f"pid {os.getpid()} at {url}")


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


def loopback(request: Request) -> bool:
    """Whether the request came from this machine."""
    try:
        return request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return False


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

    def __init__(self, store: Store, allowed: set[str], token: str, inline: bool = False,
                 ticks: bool = True):
        fingerprint.code()   # the code as loaded, for /hello: asked before anything can change it
        self.hub = Hub(store, **({} if inline else {"spawn": self.spawn, "post": self.post}))
        self.store, self.allowed = store, allowed
        self.token = token   # the shared secret every request must carry (auth.py)
        self.stop = None   # how /shutdown stops it: the server's, once serving
        self.ticks = ticks   # whether it ticks by itself: not when a test ticks it
        self.agent_lock = threading.Lock()   # the subagent conversations followed, by one request at a time
        self.feed = Feed()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="wheelhouse")
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wheelhouse-hub")
        self.loop_thread: int | None = None
        # a connection of its own, only to read the marks, outside the store's lock: they move
        # with every commit made by any connection, the hosts', the MCP servers' and the hub's
        self.watch = sqlite3.connect(store.path, isolation_level=None, check_same_thread=False)
        self.marked: dict = {}                      # the marks as last published
        self.shown: dict[str, str] = {}   # each session's status as last published
        self.view: list[dict] = []        # /sessions, as the hub's thread last made it
        self.unkept: dict[str, dict] = {}  # answers the store couldn't keep: kept here till a tick can
        self.reading: dict[str, threading.Lock] = {}   # each session's transcript, read by one request at a time
        self.hub.listen(self.heard)
        self.app = Starlette(routes=[
            Route("/sessions", self.query(self.sessions, run=None)),
            Route("/sessions", self.command(self.new_session), methods=["POST"]),
            Route("/sessions/{sid}/conversation", self.query(self.conversation)),
            Route("/sessions/{sid}/{action}", self.command(self.session_command), methods=["POST"]),
            Route("/dispatch", self.command(lambda request, body: self.hub.send_all()), methods=["POST"]),
            Route("/items", self.query(self.items)),
            Route("/items/{sid}/{ref}/thread", self.query(self.thread)),
            Route("/items/{sid}/{ref}/permission", self.command(self.permission), methods=["POST"]),
            Route("/items/{sid}/{ref}/close", self.command(self.close), methods=["POST"]),
            Route("/fs", self.query(self.folders)),
            Route("/models", self.query(lambda request: models.catalogue(), run=None)),
            Route("/events", self.events),
            # what the TUI needs beyond the sketch (phase 2c)
            Route("/hello", self.query(self.hello, run=None)),
            Route("/inbox", self.query(self.inbox)),
            Route("/usage", self.query(self.usage, run=None)),
            Route("/settings", self.query(self.settings)),
            Route("/settings", self.command(self.set_setting), methods=["POST"]),
            Route("/adoptable", self.query(lambda request: [vars(c) for c in self.hub.adoptable()])),
            Route("/adoptable/{id}/running", self.query(
                lambda request: {"pid": liveness.running_pid(request.path_params["id"])})),
            Route("/adopt", self.command(self.adopt), methods=["POST"]),
            Route("/restore-all", self.command(self.restore_all), methods=["POST"]),
            Route("/items/{sid}/{ref}/conversation", self.query(self.agent_conversation)),
            Route("/items/{sid}/{ref}/seen", self.command(
                lambda request, body: Done(self.hub.mark_seen(request.path_params["sid"], request.path_params["ref"]))),
                methods=["POST"]),
            Route("/messages/{id:int}/unqueue", self.command(
                lambda request, body: {"text": self.hub.unqueue(request.path_params["id"])}), methods=["POST"]),
            Route("/tutorial/offer", self.command(self.answer_offer), methods=["POST"]),
            Route("/tutorial/seen", self.command(self.tutorial_seen), methods=["POST"]),
            Route("/shutdown", self.command(self.shutdown_requested), methods=["POST"]),
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

    def marks(self) -> dict:
        """Each marked table's count of writes (store.MARKED), as the triggers keep it."""
        return dict(self.watch.execute("SELECT kind, n FROM marks"))

    def publish_moved(self, marks: dict) -> None:
        """What has moved since last published: each kind whose mark has, and the status each
        session shows, which liveness and Relaunch change with no write. The sessions' mark
        is the rows the hub's refresh read, as /sessions serves them, but for heartbeat_at, so
        a host's heartbeat moves nothing. The tables' marks are read before that refresh: a
        write after them moves them again, for the next tick, and a client refetching on an
        event never gets the rows from before it."""
        rows = {s["id"]: s for s in self.hub.sessions}
        self.forget_gone()
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

    def forget_gone(self) -> None:
        """The transcripts followed for sessions gone, let go: those the last refresh no
        longer found and the store hasn't either, so not one created since that refresh.
        A conversation asked for one again finds it gone before following it again. One
        being read is left to the next refresh, its lock held while it is let go."""
        missing = (self.hub.followers.keys() | self.reading.keys()) - self.hub.by_id.keys()
        for sid in [sid for sid in missing if self.hub.store.session(sid) is None]:
            lock = self.reading.get(sid)
            if lock is None or lock.acquire(blocking=False):
                self.hub.followers.pop(sid, None)
                self.reading.pop(sid, None)
                if lock is not None:
                    lock.release()

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
        task = asyncio.create_task(self.ticking() if self.ticks else asyncio.sleep(0))
        try:
            yield
        finally:
            self.feed.close()
            task.cancel()
            with contextlib.suppress(Exception):   # the answers the store couldn't keep, kept now if it can
                self.worker.submit(self.keep_answers).result(timeout=2)
            self.shutdown()

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.worker.shutdown(wait=False, cancel_futures=True)

    # requests

    def check_host(self, request: Request) -> None:
        """Every request, before anything else: it names the daemon (D47), so a page rebound
        by DNS learns nothing, then it carries the token (a 401 if not)."""
        if request.headers.get("host") not in self.allowed:
            raise Refused(421, "this daemon isn't reached by that name")
        if not auth.matches(request.headers.get("authorization"), self.token):
            raise Refused(401, "this daemon needs its token: claude-wheelhouse token prints it",
                          {"WWW-Authenticate": auth.SCHEME})

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
            except ClientDisconnect:   # gone before it sent the body: nothing done, nobody to tell
                return Response(status_code=499)
            except Exception as e:
                return refusal(e)
        return endpoint

    def carry_out(self, handler, request: Request, key: str, raw: bytes) -> Response:
        """A command, on the hub's thread: one at a time, so the same key sent twice at once
        is still done once. One that fails unexpectedly (the database locked) is kept if and
        only if it wrote anything first (store.written): a retry then gets the same 500 rather
        than act twice; one that wrote nothing escapes unkept, so a retry tries again."""
        fingerprint = hashlib.sha256(f"{request.url.path}\n".encode() + raw).hexdigest()
        if (kept := self.unkept.get(key) or self.store.answered(key)) is not None:
            if kept["fingerprint"] != fingerprint:
                return refusal(Refused(422, "that Idempotency-Key was used for another request"))
            return Response(kept["body"], kept["status"], media_type="application/json",
                            headers={"Idempotent-Replayed": "true"})
        wrote = self.store.written()
        try:
            body = json.loads(raw) if raw else {}
            if not isinstance(body, dict):
                raise Refused(400, "the body must be a JSON object")
            done = handler(request, body)
            response = JSONResponse(done_json(done) if isinstance(done, Done) else done)
        except json.JSONDecodeError:
            response = refusal(Refused(400, "the body isn't JSON"))
        except ANSWERED as e:
            response = refusal(e)
        except Exception as e:
            if self.store.written() == wrote:
                raise
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
        running = follower.snap if follower and follower.ready else None
        return {**dict(s), "status": self.hub.shown_status(sid), "liveness": self.hub.statuses.get(sid, "dead"),
                "model_state": self.hub.model_state(s), "mode": mode(s), "busy": self.hub.busy(sid),
                "running_model": running and running.model, "running_effort": running and running.effort,
                "stale": self.hub.stale(sid), "sends_now": self.hub.sends_now(sid),
                "compact_first": self.hub.compact_first(sid), "compacting": sid in self.hub.compacting,
                "context": {"tokens": snap.context, "window": snap.window, "estimate": snap.estimate}
                if snap and snap.context else None}

    def session_views(self) -> list[dict]:
        """Every session as the hub's last refresh read it: on the hub's thread."""
        return [self.session_view(s) for s in self.hub.sessions]

    def sessions(self, request: Request) -> list[dict]:
        return self.view

    def conversation(self, request: Request) -> list[dict]:
        """On starlette's threads, as it reads the transcript: the hub's followers are only
        this query's, each session's read by one request at a time. One that finds a read
        under way waits for it up to READ_WAIT, then reads for itself, so a client refetching
        on an event never gets the blocks from before it; past that, 503, so a hung
        transcript holds each of starlette's threads for READ_WAIT at most."""
        sid = request.path_params["sid"]
        lock = self.reading.setdefault(sid, threading.Lock())
        if not lock.acquire(timeout=READ_WAIT):
            raise Refused(503, "this conversation is being read: try again shortly", {"Retry-After": "1"})
        try:
            blocks = self.hub.conversation(sid)
        finally:
            lock.release()
        if blocks is None:
            raise SessionGone(sid)
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
                row = {k: v for k, v in dict(it).items() if k != "seq"}   # retired, unused (store.RETIRED_TRIGGER)
                out.append({**row, "standing": where, "queued": key in queued,
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
        row = {k: v for k, v in dict(item).items() if k != "seq"}   # retired, unused (store.RETIRED_TRIGGER)
        return {"item": row, "messages": [dict(m) for m in self.store.thread(sid, ref)]}

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

    def hello(self, request: Request) -> dict:
        """Which daemon this is, for a client deciding whether to join it: its API, its code
        as loaded (another from the same package path is older code: restart it), its pid,
        and whether its first tick has made /sessions."""
        return {"api": fingerprint.API, "code": fingerprint.code(), "package": str(fingerprint.PACKAGE),
                "pid": os.getpid(), "ready": bool(self.marked)}

    def inbox(self, request: Request) -> dict:
        """What the TUI's item list is painted from, all at once so it agrees with itself:
        every item (but its body, which its thread brings), with its session's name and
        whether it's parked, so a client filters it as the store would (store.items); what's
        processing, awaiting and drafted, by session and ref (a draft's ref None for a
        general message); and the tutorial's steps. The ranking is the client's own."""
        rows = self.store._all("SELECT i.*, s.name AS session_name, s.parked AS session_parked FROM items i "
                               "JOIN sessions s ON s.id = i.session_id")
        sid = tutorial.session_id(self.store)
        steps = tutorial.steps(self.store, sid, tutorial.seen(self.store, sid)) if sid else []
        return {"items": [{k: v for k, v in dict(r).items() if k not in UNLISTED} for r in rows],
                "processing": sorted(self.store.processing()), "awaiting": sorted(self.store.awaiting()),
                "drafts": [{"id": m["id"], "session_id": m["session_id"], "item_ref": m["item_ref"]}
                           for m in self.store.drafts()],
                "tutorial": {"sid": sid, "steps": steps, "offer": tutorial.should_offer(self.store)}}

    def usage(self, request: Request) -> dict:
        """The account's usage, as the hub last fetched it: {kind: [percent, resets at]}."""
        return {"limits": {k: list(v) for k, v in self.hub.usage.limits.items()}, "failed": self.hub.usage.failed}

    def settings(self, request: Request) -> dict:
        """The TUI's kept splitter sizes, each a share of its parent: no other setting."""
        return dict(self.store._all("SELECT key, value FROM settings WHERE substr(key, 1, ?) = ?",
                                    (len(LAYOUT), LAYOUT)))

    def agent_conversation(self, request: Request) -> list[dict] | None:
        """A subagent item's own conversation, read now (Hub.agent_conversation), on
        starlette's threads, its followers by one request at a time: None if the item isn't
        a subagent's."""
        sid, ref = request.path_params["sid"], request.path_params["ref"]
        self.item(sid, ref)
        with self.agent_lock:
            blocks = self.hub.agent_conversation(sid, ref)
        return None if blocks is None else [{"who": who, "text": markdown} for who, markdown in blocks]

    # commands

    def set_setting(self, request: Request, body: dict) -> Done:
        """A splitter size kept, or with value null its default put back."""
        key, value = text(body, "key"), text(body, "value", False)
        if not key.startswith(LAYOUT):
            raise Refused(400, f"only {LAYOUT} settings are a client's")
        if value is None:
            self.store.clear_setting(key)
        else:
            self.store.set_setting(key, value)
        return Done()

    def adopt(self, request: Request, body: dict) -> Done:
        """A session on disk brought in, by its id, as Adopt offered it: one gone from the
        offer meanwhile (adopted elsewhere, or too old) isn't there."""
        want = text(body, "id")
        found = next((c for c in self.hub.adoptable() if c.id == want), None)
        if found is None:
            raise KeyError(f"no session {want} to adopt")
        return self.hub.adopt(found, text(body, "name", False) or "")

    def restore_all(self, request: Request, body: dict) -> Done:
        """The dead sessions the client showed the person, brought back: what it confirmed."""
        sids = body.get("sids")
        if not (isinstance(sids, list) and all(isinstance(sid, str) for sid in sids)):
            raise Refused(400, "sids must be a list of session ids")
        return self.hub.restore_all(sids)

    def answer_offer(self, request: Request, body: dict) -> Done:
        take = body.get("take")
        if not isinstance(take, bool):
            raise Refused(400, "take must be true or false")
        return self.hub.answer_offer(take)

    def tutorial_seen(self, request: Request, body: dict) -> Done:
        """A tutorial step only the screen sees (a question opened, the conversation followed)."""
        sid, step = text(body, "sid"), text(body, "step")
        self.hub.row(sid)
        tutorial.see(self.store, sid, step)
        return Done()

    def shutdown_requested(self, request: Request, body: dict) -> Done:
        """Stop, once this answer has gone: a client found it running older code."""
        if self.stop is None:
            return Done(False).say("this daemon can't be stopped from here", "warning")
        self.loop.call_soon_threadsafe(self.stop)
        return Done().say("the daemon is stopping")

    def new_session(self, request: Request, body: dict) -> Done:
        """A session in an existing directory, by its absolute path, run by a host. In a tab
        only for a client on this machine (the loopback): a tab opens on this machine's
        screen, where nobody remote is."""
        cwd = text(body, "cwd")
        if not os.path.isabs(cwd) or not os.path.isdir(cwd):
            raise Refused(400, f"cwd must be an existing directory's absolute path: {cwd!r} isn't")
        how = text(body, "runner", False) or "sdk"
        if how not in ("sdk", "tab"):
            raise Refused(400, "runner must be sdk or tab")
        if how == "tab" and not loopback(request):
            raise Refused(400, "a session started remotely runs by a host: runner can only be sdk")
        return self.hub.new_session(cwd, text(body, "name", False) or "", text(body, "ticket", False) or "",
                                    text(body, "brief", False) or "", how, text(body, "model", False),
                                    text(body, "effort", False))

    def session_command(self, request: Request, body: dict) -> Done:
        """Relaunch with the host a client was shown, (pid, start time), stops that host and
        starts it again, with `compact` true once the session is compacted (the offer
        compact_first says to make); without, it brings back a dead session. Park and End say how (HOW):
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
            if body.get("compact"):
                return hub.compact_then_relaunch(sid, tuple(host))
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
        if action == "model":
            return hub.set_model(sid, models.check_model(text(body, "model")))
        if action == "effort":
            return hub.set_effort(sid, models.check_effort(text(body, "effort")))
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
    headers = None
    if isinstance(e, Refused):
        status, error, headers = e.status, e.error, e.headers
    elif isinstance(e, (SessionGone, KeyError)):
        status, error = 404, str(e.args[0]) if isinstance(e, KeyError) and e.args else str(e)
    elif isinstance(e, ValueError):
        status, error = 400, str(e)
    else:
        status, error = 500, f"the daemon failed: {type(e).__name__}: {e}"
        print(f"wheelhouse daemon: {error}", file=sys.stderr, flush=True)
        traceback.print_exception(e)
    # its type too, so a client says what the hub in process said (a session gone, an item)
    return JSONResponse({"error": error, "type": type(e).__name__}, status, headers)


class Server(uvicorn.Server):
    """uvicorn's, but a signal to stop ends the event streams first, each once it has sent
    what it has, so a client sees its stream end cleanly rather than cut off; and once it's
    listening, it says where (listening), as in the lock (announce)."""

    def __init__(self, daemon: Daemon, config: uvicorn.Config, listening=None):
        super().__init__(config)
        self.daemon, self.listening = daemon, listening

    async def startup(self, sockets=None) -> None:
        await super().startup(sockets)
        if self.started and self.listening is not None:
            self.listening()

    def handle_exit(self, sig, frame) -> None:
        if self.daemon.loop is not None:
            with contextlib.suppress(RuntimeError):   # the loop has closed already
                self.daemon.loop.call_soon_threadsafe(self.daemon.feed.close)
        super().handle_exit(sig, frame)


def main(address: str = ADDRESS, port: int = PORT, names=()) -> None:
    """Serve until stopped (Ctrl+C, SIGTERM from systemd, or POST /shutdown): one per user,
    or AlreadyRunning. Its token is made, under the lock, the first time, and reused after."""
    address = checked_address(address)
    allowed = hosts(address, port, names)
    store = Store()
    url = f"http://{address if ipaddress.ip_address(address).version == 4 else f'[{address}]'}:{port}"
    lock = take_lock(store.path.parent)
    try:
        token, said = auth.ensure(store.path.parent)
        if said:
            print(f"wheelhouse daemon: {said}", flush=True)
        daemon = Daemon(store, allowed, token)
        server = Server(daemon, uvicorn.Config(daemon.app, host=address, port=port, log_level="warning"),
                        listening=functools.partial(announce, lock, url))
        daemon.stop = functools.partial(server.handle_exit, signal.SIGTERM, None)
        print(f"wheelhouse daemon (pid {os.getpid()}) on {url}, state in {store.path.parent}", flush=True)
        server.run()
    finally:
        lock.close()
