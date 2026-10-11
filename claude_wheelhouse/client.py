"""The TUI's way to the hub (.plan/remote.md, phase 2c): one interface, two ways through it.

LocalClient is the hub in process, as the TUI had it before 2c (`--local`): it reads the
store where the TUI did. RemoteClient goes through the daemon, over HTTP and SSE, with its
token. Both answer the same calls with the same values: the sessions' rows and what the
hub works out beside them, the inbox's rows and sets, an item's thread, a conversation;
and the person's commands, each answering what it did (hub.Done), or raising what the hub
raises (SessionGone, KeyError for an item gone or a permission decided already).

What a RemoteClient shows changes only where the hub in process used to read the store:
at refresh(), which the TUI calls at its tick (held off while a key lands) and right after
the person's own command. The daemon's events, and what they make it fetch, wait beside
what's shown until then, so nothing moves a row under a key: a burst of keys ends as the
same keys typed slowly do. A command blocks until it's answered, as the hub's call did,
and the refresh after it fetches what it changed at once."""

import contextlib
import socket
import threading
import time
import uuid
from typing import Callable, NamedTuple

from . import adopt, auth, stats, tutorial
from .hub import RUNNING, Done, Event, Hub, host_context, requested
from .store import SessionGone

TIMEOUT = 10        # seconds a request may take to be answered (a launch, Restore all)
CONNECT = 2         # seconds to connect: the daemon is on this machine, or one hop away
STREAM_READ = 40    # seconds the stream may go quiet: the daemon pings every 15
BACKOFF = (0.5, 1, 2, 5)   # seconds between tries to reconnect, the last repeated
AGENT_POLL = 1.0    # a subagent's conversation shown: fetched again each second, as no event says it grew
USAGE_POLL = 60     # the account's usage, as the hub fetches it
UNREACHABLE = "not connected to the daemon: nothing was sent"
LOST = "lost the daemon mid-command: it may or may not have been done, so check before trying again"


class Inbox(NamedTuple):
    """What the item list is painted from: the items (the store's rows, ranked as it ranks
    them), and by (session, ref) what's processing, what's queued (ref None for a general
    message) and what's awaiting a reply."""
    items: list
    processing: set
    queued: set
    awaiting: set


class LocalClient(Hub):
    """The hub in process (--local): the TUI's own, reading the store as it always has."""
    remote = False
    elsewhere = False   # the sessions are on this machine
    connected = True

    def background(self) -> None:
        """The hub's reading at each tick: context sizes, subagents, the account's usage."""
        self.read_contexts()
        self.watch_agents()
        self.fetch_usage()

    def status(self, sid: str) -> str | None:
        return self.statuses.get(sid)

    def context_size(self, s) -> int | None:
        follower = self.contexts.get(s["id"])
        if not (follower and follower.ready):
            return None
        return stats.current(follower.snap, host_context(s)).context or None

    def session(self, sid: str):
        return self.store.session(sid)

    def inbox(self, sid: str | None) -> Inbox:
        processing = self.store.processing()
        queued = {(m["session_id"], m["item_ref"]) for m in self.store.drafts()}
        return Inbox(self.store.items(sid), processing, queued, self.store.awaiting())

    def item(self, sid: str, ref: str):
        return self.store.item(sid, ref)

    def drafts(self, sid: str | None = None) -> list:
        return self.store.drafts(sid)

    def thread(self, sid: str, ref: str):
        """An item and its messages, oldest first, or None if it has gone."""
        item = self.store.item(sid, ref)
        return None if item is None else (item, self.store.thread(sid, ref))

    def setting(self, key: str) -> str | None:
        return self.store.setting(key)

    def set_setting(self, key: str, value: str | None) -> None:
        if value is None:
            self.store.clear_setting(key)
        else:
            self.store.set_setting(key, value)

    def tutorial(self) -> tuple[str, list] | None:
        """The tutorial's session, if one is listed, and its steps."""
        sid = next((s["id"] for s in self.sessions if tutorial.is_tutorial(self.store, s)), None)
        return None if sid is None else (sid, tutorial.steps(self.store, sid, tutorial.seen(self.store, sid)))

    def should_offer(self) -> bool:
        return tutorial.should_offer(self.store)

    def see(self, sid: str, step: str) -> None:
        tutorial.see(self.store, sid, step)

    def start(self, post=None) -> None:
        pass   # its work runs on the app's own workers (spawn, post)

    def disconnect(self) -> None:
        pass


class Unreachable(RuntimeError):
    """The daemon can't be reached: nothing was sent (UNREACHABLE)."""


class Refused(RuntimeError):
    """The daemon refused a request: its status and what it said."""

    def __init__(self, status: int, error: str, kind: str = ""):
        super().__init__(error)
        self.status, self.error, self.kind = status, error, kind


def blocks(data) -> list[tuple[str, str]] | None:
    """A conversation as the hub makes it, (who, markdown) pairs, from the daemon's JSON."""
    return None if data is None else [(b["who"], b["text"]) for b in data]


class RemoteClient:
    """The daemon's hub, through its routes (daemon.py), with its token. What the TUI shows
    is what refresh() last swapped in; the stream's events and what they fetch wait beside
    it (waiting) until then.

    synchronous: no stream and no threads of its own; each refresh fetches everything at
    once, after before_fetch (a test's: the daemon's tick), as the hub in process read the
    store. reread: the token read again after a 401 (the local daemon's file, rotated).
    revive: called when the daemon can't be reached, to start the local one again if it
    has gone; its URL, or None. elsewhere: the daemon is another machine's (--connect)."""
    remote = True
    elsewhere = False
    store = None   # the database is the daemon's

    def __init__(self, url: str, token: str, reread: Callable[[], str] | None = None,
                 revive: Callable[[], str | None] | None = None, synchronous: bool = False,
                 before_fetch: Callable[[], None] | None = None, transport=None):
        import httpx2   # here, not at the top: the TUI in process (--local) never loads it
        self.httpx = httpx2
        self.url, self.token, self.reread, self.revive = url, token, reread, revive
        self.synchronous, self.before_fetch = synchronous, before_fetch
        self.transport = transport
        self.http = self.make_http(TIMEOUT)
        self.usage = stats.AccountUsage()   # the stats pane's: its numbers come from the daemon's
        self.listeners: list[Callable[[Event], None]] = []
        self.post: Callable[[Callable[[], None]], None] = lambda callback: callback()
        self.connected = True
        # what the TUI paints from, as of the last refresh
        self.views: list[dict] = []
        self.by_id: dict[str, dict] = {}
        self.statuses: dict[str, str] = {}
        self.relaunching: set[str] = set()
        self.compacting: set[str] = set()   # those of them compacting first, to relaunch once compacted
        self.items: list[dict] = []
        self.by_key: dict[tuple, dict] = {}
        self.sets = Inbox([], set(), set(), set())
        self.draft_rows: list[dict] = []
        self.tutorial_state: dict = {}
        # the threads, conversations and subagents' conversations shown, by key: ("thread",
        # sid, ref), ("conversation", sid), ("agent", sid, ref)
        self.cache: dict[tuple, object] = {}
        self.shown_seq: dict[tuple, int] = {}   # each key's fetch as shown: a later one replaces it
        self.waiting: dict[tuple, tuple[int, object]] = {}   # fetched in the background, for the next refresh
        self.dirty: set[tuple] = set()   # what the events say moved, to fetch
        self.asked: set[tuple] = set()   # what the TUI asked for since the last refresh
        self.wanted: set[tuple] = set()  # ... and before it: what the background keeps up to date
        self.seq = 0
        self.forced = True   # the next refresh fetches at once: the first, and after a command
        self.lock = threading.Condition()
        self.stopping = False
        self.threads: list[threading.Thread] = []
        self.last_event: str | None = None
        self.layout: dict | None = None   # the kept splitter sizes, read once
        self.stream_socket = None   # the stream's connection, while it's open
        self.refusal: str | None = None   # why the daemon refuses this client, said once

    def make_http(self, read: float):
        h = self.httpx
        return h.Client(base_url=self.url, timeout=h.Timeout(read, connect=CONNECT), transport=self.transport,
                        headers={"Authorization": auth.header(self.token)})

    # the hub's events, said on the TUI's thread

    def listen(self, listener: Callable[[Event], None]) -> None:
        self.listeners.append(listener)

    def emit(self, event: Event) -> None:
        for listener in self.listeners:
            listener(event)

    def start(self, post: Callable[[Callable[[], None]], None] | None = None) -> None:
        """The stream and the fetching, on threads of their own: not in synchronous mode."""
        if post is not None:
            self.post = post
        if self.synchronous or self.threads:
            return
        for target, name in ((self.stream, "wheelhouse-stream"), (self.fetching, "wheelhouse-fetch")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self.threads.append(thread)

    def disconnect(self) -> None:
        with self.lock:
            self.stopping = True
            self.lock.notify_all()
        self.http.close()
        stream = self.stream_socket
        if stream is not None:   # its read, waiting on the next event, ends now
            with contextlib.suppress(Exception):
                stream.get_extra_info("socket").shutdown(socket.SHUT_RDWR)

    # requests

    def request(self, method: str, path: str, body=None, key: str | None = None):
        """One request, answered as JSON, or Refused with the daemon's error; a 401 once
        more with the token read again (rotated). Unreachable if it couldn't connect;
        the httpx error as it is if the answer was lost after sending."""
        if not self.connected and self.threads:   # lost: the stream says when it's back, so not tried meanwhile
            raise Unreachable(UNREACHABLE)
        for attempt in range(2):
            headers = {"Idempotency-Key": key} if key else {}
            try:
                r = self.http.request(method, path, json=body, headers=headers)
            except (self.httpx.ConnectError, self.httpx.ConnectTimeout, self.httpx.PoolTimeout) as e:
                raise Unreachable(UNREACHABLE) from e
            if r.status_code == 401 and attempt == 0 and self.reread is not None:
                try:
                    token = self.reread()
                except Exception:   # the file gone or unreadable: the refusal stands
                    token = self.token
                if token != self.token:
                    self.token = token
                    self.http.headers["Authorization"] = auth.header(token)
                    continue
            if r.status_code >= 400:
                try:
                    data = r.json()
                except ValueError:
                    data = {}
                raise Refused(r.status_code, data.get("error") or f"the daemon answered {r.status_code}",
                              data.get("type", ""))
            return r.json()

    def get(self, path: str, params: dict | None = None):
        if params:
            path = str(self.httpx.URL(path, params=params))
        return self.request("GET", path)

    def command(self, path: str, body: dict | None = None):
        """A command, sent with an Idempotency-Key of its own: sent again once with the same
        key if its answer was lost, so it's done once. Raises what the hub would (SessionGone,
        KeyError, ValueError); otherwise the daemon's JSON. The next refresh fetches at once."""
        key = str(uuid.uuid4())
        try:
            for attempt in range(2):
                try:
                    return self.request("POST", path, {} if body is None else body, key)
                except Unreachable:
                    raise
                except self.httpx.TransportError:
                    if attempt:
                        raise Unreachable(LOST) from None
                    time.sleep(0.5)
        except Refused as e:
            raise hub_error(e) from None
        finally:
            with self.lock:
                self.forced = True

    def done(self, path: str, body: dict | None = None) -> Done:
        """A command answered as the hub's Done: one that couldn't reach the daemon, or
        that the daemon failed, did nothing the person can see, and says so."""
        try:
            data = self.command(path, body)
        except Unreachable as e:
            return Done(False).say(str(e), "error")
        except Refused as e:
            return Done(False).say(e.error, "error")
        return Done(data.get("ok", True), [Event("notice", n["text"], n.get("severity", "information"))
                                          for n in data.get("notices", [])], data.get("denied", False), data.get("sid"))

    # the sessions, as of the last refresh

    def refresh(self) -> None:
        """What the TUI shows, up to date where the hub in process would read the store: at
        once after a command, else what the events have fetched meanwhile, swapped in. The
        threads and conversations shown before but not since are let go, so one shown again
        later is fetched again rather than shown as it was."""
        if self.synchronous:
            if self.before_fetch is not None:
                self.before_fetch()
            with self.lock:
                self.forced = True
        with self.lock:
            for key, (seq, data) in self.waiting.items():
                if seq > self.shown_seq.get(key, -1):
                    self.apply(key, data, seq)
            self.waiting.clear()
            self.wanted, self.asked = self.asked, set()
            for key in [k for k in self.cache if k not in self.wanted]:
                self.forget(key)
        self.catch_up()

    def catch_up(self) -> None:
        """After a command, what it changed, fetched before anything is read again: the
        sessions and the inbox at once, and the threads and conversations when next shown.
        As the hub in process, whose next read of the store had it."""
        with self.lock:
            if not self.connected and self.threads:   # lost: fetched once the stream is back
                return
            forced, self.forced = self.forced, False
            if forced:
                for key in list(self.cache):
                    self.forget(key)
        if forced:
            try:
                for key in (("sessions",), ("inbox",)):
                    seq = self.next_seq()
                    data = self.fetch(key)
                    with self.lock:
                        self.apply(key, data, seq)
            except (Unreachable, self.httpx.TransportError):
                with self.lock:
                    self.forced = True   # the next refresh tries again
            except Refused as e:   # busy (503), or the token refused: the next refresh tries again
                with self.lock:
                    self.forced = True
                if e.status != 503:
                    self.refused(e.error)

    def next_seq(self) -> int:
        with self.lock:
            self.seq += 1
            return self.seq

    def forget(self, key: tuple) -> None:
        self.cache.pop(key, None)
        self.shown_seq.pop(key, None)

    def fetch(self, key: tuple):
        kind = key[0]
        if kind == "sessions":
            return self.get("/sessions")
        if kind == "inbox":
            return self.get("/inbox")
        if kind == "usage":
            return self.get("/usage")
        try:
            if kind == "thread":
                return self.get(f"/items/{key[1]}/{key[2]}/thread")
            if kind == "conversation":
                return self.get(f"/sessions/{key[1]}/conversation")
            return self.get(f"/items/{key[1]}/{key[2]}/conversation")
        except Refused as e:
            if e.status == 404:   # gone
                return None
            raise

    def apply(self, key: tuple, data, seq: int) -> None:
        """A fetch's answer, as shown from now: under the lock."""
        kind = key[0]
        self.shown_seq[key] = seq
        if kind == "sessions":
            self.views = data
            self.by_id = {s["id"]: s for s in data}
            self.statuses = {s["id"]: s["liveness"] for s in data}
            self.relaunching = {s["id"] for s in data if s["status"] == "relaunching"}
            self.compacting = {s["id"] for s in data if s.get("compacting")}
        elif kind == "inbox":
            self.items = data["items"]
            self.by_key = {(it["session_id"], it["ref"]): it for it in self.items}
            self.draft_rows = data["drafts"]
            self.sets = Inbox(self.items, {tuple(k) for k in data["processing"]},
                              {(d["session_id"], d["item_ref"]) for d in data["drafts"]},
                              {tuple(k) for k in data["awaiting"]})
            self.tutorial_state = data["tutorial"]
        elif kind == "usage":
            self.usage.limits = {k: tuple(v) for k, v in data["limits"].items()}
            self.usage.failed = data["failed"]
        else:
            self.cache[key] = data

    def shown(self, key: tuple):
        """A thread or conversation as shown: fetched now, the first time it's asked for
        since it was let go, as the hub in process read it; after that kept up to date by
        the events. None if it has gone."""
        self.catch_up()
        with self.lock:
            self.asked.add(key)
            if key in self.cache or (not self.connected and self.threads):   # lost: what it had, if anything
                return self.cache.get(key)
        seq = self.next_seq()
        try:
            data = self.fetch(key)
        except (Unreachable, self.httpx.TransportError):
            return None
        except Refused as e:
            if e.status == 503:
                with self.lock:
                    self.dirty.add(key)   # being read: the background fetches it once it's free
                    self.lock.notify_all()
            else:
                self.refused(e.error)
            return [] if key[0] != "thread" else None
        with self.lock:
            if seq > self.shown_seq.get(key, -1):
                self.apply(key, data, seq)
            return self.cache.get(key)

    @property
    def sessions(self) -> list[dict]:
        self.catch_up()
        return self.views

    def status(self, sid: str) -> str | None:
        return self.statuses.get(sid)

    def view(self, sid: str) -> dict | None:
        self.catch_up()
        return self.by_id.get(sid)

    def running(self, sid: str) -> bool:
        return self.statuses.get(sid) in RUNNING

    def dead(self, sid: str) -> bool:
        return self.statuses.get(sid) == "dead"

    def stale(self, sid: str) -> bool:
        return bool((v := self.view(sid)) and v["stale"])

    def sends_now(self, sid: str) -> bool:
        return bool((v := self.view(sid)) and v["sends_now"])

    def busy(self, sid: str) -> bool:
        return bool((v := self.view(sid)) and v["busy"])

    def shown_status(self, sid: str) -> str:
        v = self.view(sid)
        return v["status"] if v else "dead"

    def pending(self, sid: str) -> str | None:
        return requested(self.row(sid))

    def row(self, sid: str):
        """The session's row as last served, or SessionGone."""
        v = self.view(sid)
        if v is None:
            raise SessionGone(sid)
        return v

    def session(self, sid: str):
        return self.view(sid)

    def context_size(self, s) -> int | None:
        return (s.get("context") or {}).get("tokens") or None

    @staticmethod
    def model_state(s) -> dict:
        return s["model_state"]

    def restorable(self) -> list[str]:
        return [s["id"] for s in self.sessions if self.dead(s["id"]) and not s["parked"]]

    def background(self) -> None:
        pass   # the daemon's hub does its reading

    def finish_relaunches(self) -> None:
        pass   # the daemon carries a Relaunch on, whether or not a client is open

    # the inbox and what it opens

    def inbox(self, sid: str | None) -> Inbox:
        """As store.items has it: every unparked session's items, or one session's."""
        self.catch_up()
        items = [it for it in self.items
                 if (it["session_id"] == sid if sid else not it["session_parked"])]
        return self.sets._replace(items=items)

    def item(self, sid: str, ref: str):
        self.catch_up()
        return self.by_key.get((sid, ref))

    def drafts(self, sid: str | None = None) -> list[dict]:
        self.catch_up()
        return [d for d in self.draft_rows if sid is None or d["session_id"] == sid]

    def thread(self, sid: str, ref: str):
        data = self.shown(("thread", sid, ref))
        return None if data is None else (data["item"], data["messages"])

    def conversation(self, sid: str) -> list[tuple[str, str]] | None:
        return blocks(self.shown(("conversation", sid)))

    def agent_conversation(self, sid: str, ref: str) -> list[tuple[str, str]] | None:
        """A subagent item's own conversation; None, unasked, for any other item."""
        item = self.item(sid, ref)
        if item is None or item["kind"] != "agent":
            return None
        return blocks(self.shown(("agent", sid, ref)))

    def unfollow_agent(self) -> None:
        pass   # the daemon follows the last two asked for, whoever asked

    def asking(self, target):
        item = target and target[1] and self.item(*target)
        return item if item and item["kind"] == "permission" and item["status"] == "open" else None

    def adoptable(self) -> list[adopt.Candidate]:
        try:
            return [adopt.Candidate(**c) for c in self.get("/adoptable")]
        except (Unreachable, Refused, self.httpx.TransportError):
            return []

    def running_pid(self, sid: str) -> int | None:
        try:
            return self.get(f"/adoptable/{sid}/running")["pid"]
        except (Unreachable, Refused, self.httpx.TransportError):
            return None

    def setting(self, key: str) -> str | None:
        """A kept splitter size: read once, at start, for every splitter."""
        if self.layout is None:
            try:
                self.layout = self.get("/settings")
            except (Unreachable, Refused, self.httpx.TransportError):
                self.layout = {}
        return self.layout.get(key)

    def set_setting(self, key: str, value: str | None) -> None:
        self.setting(key)
        self.layout[key] = value
        self.quietly("/settings", {"key": key, "value": value})

    def tutorial(self) -> tuple[str, list] | None:
        sid = self.tutorial_state.get("sid")
        if sid is None or sid not in self.by_id:
            return None
        return sid, [tuple(step) for step in self.tutorial_state["steps"]]

    def should_offer(self) -> bool:
        return bool(self.tutorial_state.get("offer"))

    def see(self, sid: str, step: str) -> None:
        self.quietly("/tutorial/seen", {"sid": sid, "step": step})

    def quietly(self, path: str, body: dict) -> bool:
        """A command nobody waits to hear about (a splitter's size, a tutorial step, a
        decision seen by the dwell): one that can't reach the daemon is let go."""
        try:
            return self.command(path, body).get("ok", True)
        except (Unreachable, Refused, SessionGone, KeyError, ValueError):
            return False

    # the person's commands, as the hub's (hub.Hub)

    def relaunch(self, sid: str) -> Done:
        return self.done(f"/sessions/{sid}/relaunch")

    def stop_host(self, sid: str, host: tuple) -> Done:
        return self.done(f"/sessions/{sid}/relaunch", {"host": list(host)})

    def compact_then_relaunch(self, sid: str, host: tuple) -> Done:
        return self.done(f"/sessions/{sid}/relaunch", {"host": list(host), "compact": True})

    def compact_first(self, sid: str) -> bool:
        """Whether Relaunch should offer to compact first (Hub.compact_first)."""
        return bool((v := self.view(sid)) and v.get("compact_first"))

    def restore(self, sid: str) -> Done:
        return self.done(f"/sessions/{sid}/restore")

    def restore_all(self, sids: list[str]) -> Done:
        return self.done("/restore-all", {"sids": sids})

    def new_session(self, cwd: str, name: str, ticket: str, brief: str, runner: str, model: str | None = None,
                    effort: str | None = None) -> Done:
        return self.done("/sessions", {"cwd": cwd, "name": name, "ticket": ticket, "brief": brief, "runner": runner,
                                       "model": model, "effort": effort})

    def adopt(self, candidate: adopt.Candidate, name: str) -> Done:
        return self.done("/adopt", {"id": candidate.id, "name": name})

    def answer_offer(self, take: bool) -> Done:
        return self.done("/tutorial/offer", {"take": take})

    def ask(self, sid: str, what: str) -> Done:
        return self.done(f"/sessions/{sid}/{what}", {"how": "ask"})

    def cancel_request(self, sid: str, what: str) -> Done:
        return self.done(f"/sessions/{sid}/{what}", {"how": "cancel"})

    def act_on_dead(self, sid: str, what: str) -> Done:
        return self.done(f"/sessions/{sid}/{what}", {"how": "now"})

    def force(self, sid: str, what: str) -> Done:
        return self.done(f"/sessions/{sid}/{what}", {"how": "force"})

    def unpark(self, sid: str) -> Done:
        return self.done(f"/sessions/{sid}/unpark")

    def rename(self, sid: str, name: str | None) -> Done:
        if name is None:   # cancelled
            return Done(False)
        return self.done(f"/sessions/{sid}/rename", {"name": name})

    def submit(self, target: tuple, text: str) -> Done:
        return self.done(f"/sessions/{target[0]}/send", {"text": text, "ref": target[1]})

    def answer_permission(self, sid: str, ref: str, decision: str, message: str = "") -> Done:
        return self.done(f"/items/{sid}/{ref}/permission", {"decision": decision, "message": message})

    def unqueue(self, msg_id: int) -> str | None:
        """A queued answer taken back: its text, or None if it went meanwhile. Unreachable
        if the daemon can't be reached, as the text is the person's to keep."""
        return self.command(f"/messages/{msg_id}/unqueue")["text"]

    def send_queue(self, sid: str) -> Done:
        return self.done(f"/sessions/{sid}/dispatch")

    def send_all(self) -> Done:
        return self.done("/dispatch")

    def toggle_mode(self, sid: str) -> Done:
        return self.done(f"/sessions/{sid}/mode")

    def host_command(self, sid: str, what: str) -> Done:
        return self.done(f"/sessions/{sid}/{what}")

    def set_model(self, sid: str, model: str) -> Done:
        return self.done(f"/sessions/{sid}/model", {"model": model})

    def set_effort(self, sid: str, effort: str) -> Done:
        return self.done(f"/sessions/{sid}/effort", {"effort": effort})

    def mark_seen(self, sid: str, ref: str) -> bool:
        return self.quietly(f"/items/{sid}/{ref}/seen", {})

    def close(self, sid: str, ref: str, closed: bool) -> Done:
        return self.done(f"/items/{sid}/{ref}/close", {"closed": closed})

    # the stream and the fetching, on threads of their own

    def lost(self) -> None:
        """The daemon can't be reached: said once, on the TUI's thread, until it's back. The
        stream's to say, as it's the one that says when it's back: a request that fails while
        the stream holds is only that request's."""
        if self.connected:
            self.connected = False
            self.post(lambda: self.emit(Event("link", "daemon unreachable: reconnecting", "error")))

    def found(self) -> None:
        self.refusal = None
        if not self.connected:
            self.connected = True
            self.post(lambda: self.emit(Event("link", "reconnected to the daemon")))

    def refused(self, error: str) -> None:
        """The daemon answers, but refuses this client (its token, rotated): said once,
        until it's taken again."""
        if self.refusal is None:
            self.refusal = error
            self.post(lambda: self.emit(Event("notice", f"the daemon refused this client: {error}", "error")))

    def stream(self) -> None:
        """The daemon's events, from its stream, for as long as the client runs: each
        marks what it moved to fetch. A stream that ends or can't connect is tried again,
        backing off, from the last event's id; a local daemon gone is started again
        (revive)."""
        tries = 0
        http = self.make_http(STREAM_READ)
        try:
            while not self.stopping:
                try:
                    headers = {"Authorization": auth.header(self.token)}
                    if self.last_event:
                        headers["Last-Event-ID"] = self.last_event
                    with http.sse("/events", headers=headers) as events:
                        if events.response.status_code == 401:
                            try:
                                token = self.reread() if self.reread is not None else self.token
                            except Exception:   # the file gone or unreadable: the refusal stands
                                token = self.token
                            if token == self.token:   # not rotated here: the person's to give it the new one
                                self.refused("its token isn't this daemon's: claude-wheelhouse token prints it")
                            self.token = token
                            self.http.headers["Authorization"] = auth.header(token)
                            raise self.httpx.TransportError("the token was refused")
                        if events.response.status_code != 200:
                            raise self.httpx.TransportError(f"the stream answered {events.response.status_code}")
                        tries = 0
                        self.stream_socket = events.response.extensions.get("network_stream")
                        if self.stopping:
                            return
                        self.found()
                        for event in events:
                            if self.stopping:
                                return
                            self.last_event = event.id or self.last_event
                            self.heard(event.json())
                except (self.httpx.TransportError, self.httpx.SSEError, ValueError, RuntimeError):
                    pass   # RuntimeError: its connection closed under it (disconnect)
                if self.stopping:
                    return
                self.lost()
                if self.revive is not None:
                    url = self.revive()
                    if url and url != self.url:
                        self.url = url
                        http.close()
                        http = self.make_http(STREAM_READ)
                        old, self.http = self.http, self.make_http(TIMEOUT)
                        old.close()
                with self.lock:
                    self.lock.wait(BACKOFF[min(tries, len(BACKOFF) - 1)])
                tries += 1
        finally:
            http.close()

    def heard(self, event: dict) -> None:
        """An event: a notice said; anything else marks what it moved to fetch, and a
        thread or conversation not shown now let go, to be fetched afresh if it's shown again."""
        kind, sid = event["kind"], event.get("sid")
        if kind == "notice":
            self.post(lambda: self.emit(Event("notice", event.get("text", ""), event.get("severity", "information"))))
            return
        with self.lock:
            kept = set(self.cache) | self.wanted | self.asked
            moved: set[tuple] = set()
            if kind in ("sessions", "stats"):
                moved.add(("sessions",))
            if kind == "items":
                moved |= {("inbox",)} | {k for k in kept if k[0] == "thread"}
            if kind == "thread":
                moved |= {k for k in kept if k[0] == "thread"}
            if kind == "conversation":
                moved |= {k for k in kept if k[0] == "conversation" and (sid is None or k[1] == sid)}
            if kind == "refetch":
                moved |= {("sessions",), ("inbox",), ("usage",)} | kept
            for key in moved:
                if key in self.cache and key not in self.wanted | self.asked:
                    self.forget(key)
            self.dirty |= moved
            self.lock.notify_all()

    def fetching(self) -> None:
        """What the events marked, fetched, each into waiting for the next refresh: the
        sessions and the inbox always, a thread or conversation only while it's shown. A
        subagent's conversation shown is fetched each second, and the usage each minute."""
        polled = usage = 0.0
        while True:
            with self.lock:
                while not self.stopping and not self.dirty:
                    now = time.monotonic()
                    if now - polled >= AGENT_POLL or now - usage >= USAGE_POLL:
                        break
                    self.lock.wait(min(AGENT_POLL - (now - polled), USAGE_POLL - (now - usage)))
                if self.stopping:
                    return
                now = time.monotonic()
                if now - polled >= AGENT_POLL:
                    polled = now
                    self.dirty |= {k for k in self.wanted | self.asked if k[0] == "agent"}
                if now - usage >= USAGE_POLL:
                    usage = now
                    self.dirty.add(("usage",))
                todo, self.dirty = self.dirty, set()
                shown = self.wanted | self.asked
            for key in sorted(todo):
                if key[0] in ("thread", "conversation", "agent") and key not in shown:
                    continue
                seq = self.next_seq()
                try:
                    data = self.fetch(key)
                except Refused as e:
                    if e.status == 503:   # being read: tried again next time round
                        with self.lock:
                            self.dirty.add(key)
                    continue
                except (Unreachable, self.httpx.TransportError, RuntimeError):   # RuntimeError: closed (disconnect)
                    with self.lock:   # tried again once the stream is back, which refetches all
                        self.dirty |= {k for k in todo if k >= key}
                        if not self.stopping:
                            self.lock.wait(BACKOFF[-1])
                    break
                with self.lock:
                    if key not in self.waiting or self.waiting[key][0] < seq:
                        self.waiting[key] = (seq, data)


def hub_error(e: Refused) -> Exception:
    """A refusal as the hub in process raised it: a session gone, an item gone or decided
    already (KeyError), a bad value; else the refusal itself."""
    if e.kind == "SessionGone":
        gone = SessionGone()
        gone.args = (e.error,)
        return gone
    if e.status == 409 or e.kind == "KeyError":
        return KeyError(e.error)
    if e.kind == "ValueError":
        return ValueError(e.error)
    return e
