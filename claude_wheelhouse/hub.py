"""The hub: what the wheelhouse knows and does, whatever shows it (.plan/remote.md, phase 1).
The TUI owns one, in process, and reads and acts through it; phase 2 puts it in a daemon,
behind HTTP. It never imports Textual: what a surface should know of what it does by
itself, it says as events (Event) to whoever listens.

It owns the sessions' liveness, and Relaunch, from the stop to the start again. Like the
app before it, it never acts on a session by itself: a host that dies stays dead until
the person restores it. And it reads the sessions' transcripts: each conversation, each
context size and each session's subagents, and fetches the account's usage, the reading
and fetching on the owner's worker threads (spawn), never on the thread that calls it.

Its commands are the person's acts on sessions and items: each checks what it needs,
writes the store and returns what it did (Done), with what to say to the person, for the
surface that gave it alone. One on a session that has gone raises SessionGone (row), which
a surface reports; the dialogs that ask first, and their wording, are the surface's."""

import functools
import os
import signal
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Callable

from . import adopt, api, features, launch, liveness, models, stats, subagents, transcript, tutorial
from .host import AWAITING_NOTES, NO_NOTES
from .store import DISMISSABLE, SessionGone, Store, can_queue, mode, needs_relaunch, runner, standing

RUNNING = ("live", "stalled", "starting")
# a hosted session's activity when nothing is under way: errored and stopped count as resting
RESTING = ("idle", "interrupted", "stopped", "in a shell tab", "error")
PENDING = {"end": "ending", "park": "parking"}
RELAUNCH_WAIT = 30   # seconds a host has to stop for Relaunch before it gives up and says so
AGENTS_FOLLOWED = 2   # subagent conversations followed at once: the inbox's and a full screen's
TAB_RELAUNCH = "a session in a tab relaunches once it has exited: /exit it there, then Relaunch"
# A host's restart writes its whole context to the prompt cache again: past this many
# tokens, Relaunch offers to compact first, and waits up to COMPACT_RELAUNCH_WAIT seconds
# for the compaction before it gives up, and says so
COMPACT_FIRST_TOKENS = 150_000
COMPACT_RELAUNCH_WAIT = 30 * 60
# activity lines of a host that isn't running a turn: one compacted with nothing behind it
# is stopped for its Relaunch then, never in the middle of a turn queued behind /compact
IDLE_LINES = ("idle", "error", "interrupted")


def short(sid: str) -> str:
    return sid[:6]


def display_name(s) -> str:
    return s["name"] or os.path.basename(s["cwd"]) or short(s["id"])


def requested(s) -> str | None:
    """The request (end or park) a session's row says it has been asked to act on, if any."""
    return next((what for what in PENDING if s[f"{what}_requested_at"]), None)


def working(s) -> bool:
    """Whether a session's row says a turn or a tool is under way, whatever its liveness."""
    return bool(s["running"]) or (runner(s) == "sdk" and not (s["activity"] or "idle").startswith(RESTING))


def aimed(target) -> str:
    """What a message goes to: an item's ref, or the session itself (a general message)."""
    return target[1] or "the session"


def host_context(s) -> api.HostContext | None:
    """The context size an SDK session's host last recorded, for the stats to show after a
    compaction. A tab session's sizes come from its transcript alone."""
    at = stats.epoch(s["context_at"] or "")
    if runner(s) != "sdk" or at is None or not s["context_tokens"] or not s["context_max"]:
        return None
    return api.HostContext(s["context_tokens"], s["context_max"], at)


def since(at: str | None, asked: float) -> bool:
    """Whether a stored timestamp is at or after the epoch second `asked`."""
    epoch = stats.epoch(at or "")
    return epoch is not None and epoch >= asked


def compacted_since(s, asked: float) -> bool:
    """Whether the session's host has recorded a context under COMPACT_FIRST_TOKENS since
    the epoch second `asked`: a compaction's end, which no later activity line hides."""
    at = stats.epoch(s["context_at"] or "")
    return at is not None and at >= asked and (s["context_tokens"] or 0) < COMPACT_FIRST_TOKENS


@dataclass(frozen=True)
class Event:
    """What a surface should know. "notice": text to show the person, with its severity
    (information, warning or error): to every listener, of what the hub did by itself (a
    relaunch finishing, a subagent sync failing), or in a command's Done, to its surface
    alone. "landed": a read of a session's transcript (sid) brought something, so what
    shows its context size can repaint."""
    kind: str
    text: str = ""
    severity: str = "information"
    sid: str | None = None


@dataclass
class Done:
    """What a command did, for the surface that gave it: whether it acted (ok), what to say
    to the person (notices), for submit, whether it denied an open permission, and for
    new_session, the session it made."""
    ok: bool = True
    notices: list[Event] = field(default_factory=list)
    denied: bool = False
    sid: str | None = None

    def say(self, text: str, severity: str = "information") -> "Done":
        self.notices.append(Event("notice", text, severity))
        return self


class Hub:
    """The wheelhouse's service over the store. Its state lives as long as it does."""

    def __init__(self, store: Store | None = None, spawn=None, post=None, sdk: bool | None = None):
        self.store = store or Store()
        # SDK mode (features.py, EXPERIMENTAL: the author's alone until Anthropic's terms are
        # confirmed): off, no new session runs in a host, whoever asks (a surface, a daemon's
        # client), and a host's model and effort aren't changed. A session hosted already keeps
        # working: it shows, relaunches and ends as before
        self.sdk = features.sdk_enabled() if sdk is None else sdk
        self.listeners: list[Callable[[Event], None]] = []
        # how its owner runs work on a worker thread, by group, and a callback back on its
        # own thread (a no-op once it's closing): both at once by default, as in a test
        self.spawn: Callable[[Callable[[], None], str], None] = spawn or (lambda work, group: work())
        self.post: Callable[[Callable[[], None]], None] = post or (lambda callback: callback())
        self.wake = liveness.WakeDetector()
        self.waking = False
        self.sessions = []
        self.by_id: dict = {}   # the sessions by id, as the last refresh read them
        self.statuses: dict[str, str] = {}
        # sessions whose host Relaunch has stopped: started again once it has gone. Each has
        # a monotonic deadline and the stopped host, (pid, start time). Kept in the store too
        # (relaunch_under_way), so a hub started again carries on those an earlier one began
        # in time. One past its deadline (lapsed: the wheelhouse was down, or the machine off)
        # is never started again: the wheelhouse never restores a dead host by itself
        under_way = self.store.relaunches()
        self.relaunching: dict[str, tuple[float, tuple]] = {
            sid: (time.monotonic() + deadline - time.time(), host) for sid, (deadline, host, _) in under_way.items()}
        self.lapsed = {sid for sid, (deadline, host, _) in under_way.items() if deadline < time.time()}
        # of those, the ones compacting before their host is stopped (compact_then_relaunch),
        # with the epoch second Compact was asked for
        self.compacting: dict[str, float] = {sid: asked for sid, (*_, asked) in under_way.items()
                                             if asked is not None}
        self.followers: dict[str, transcript.Follower] = {}
        # each session's context size for the session list: read on workers, by a follower
        # shared with the stats pane (stats.follower), so each transcript is read once
        self.contexts: dict[str, stats.UsageFollower] = {}
        # the sessions not running whose transcript has been read, and copied back, since
        # they stopped: read_contexts reads each once more after it stops, for its last second
        self.settled: set[str] = set()
        # each session's subagents, tracked as A items (#69): read on workers, as contexts are
        self.agent_watchers: dict[str, subagents.AgentWatcher] = {}
        self.agent_errors: dict[str, str] = {}   # each session's watcher error last said
        # the subagents whose own conversation a surface shows (agent_conversation), by their
        # session's id and item's ref: only the last AGENTS_FOLLOWED asked for are followed,
        # of the hundreds a session can have (the inbox's and a full screen's), and none once
        # none is shown. Oldest asked first
        self.agent_followers: dict[tuple[str, str], transcript.AgentFollower] = {}
        self.usage = stats.AccountUsage()   # the account's, for every surface: fetched once

    # events

    def listen(self, listener: Callable[[Event], None]) -> None:
        self.listeners.append(listener)

    def emit(self, event: Event) -> None:
        for listener in self.listeners:
            listener(event)

    def announce(self, text: str, severity: str = "information") -> None:
        """To every listener: only for what the hub does by itself. A command's notices go
        in its Done, to the surface that gave it."""
        self.emit(Event("notice", text, severity))

    # sessions and their liveness

    def refresh(self) -> None:
        """The sessions read again, and each one's liveness: only liveness, as the
        wheelhouse never deletes or parks anything by itself."""
        self.waking = self.wake.tick()
        self.sessions = self.store.sessions()
        self.by_id = {s["id"]: s for s in self.sessions}
        self.statuses = {s["id"]: liveness.status(s, waking=self.waking) for s in self.sessions}

    def row(self, sid: str):
        """The session's row, or SessionGone: it can end at any moment (in-session /wheelhouse end)."""
        s = self.store.session(sid)
        if s is None:
            raise SessionGone(sid)
        return s

    def listed(self, sid: str):
        """The session's row as the last refresh read it, as every session is painted from;
        one it didn't see, read now (row)."""
        s = self.by_id.get(sid)
        return s if s is not None else self.row(sid)

    def running(self, sid: str) -> bool:
        return self.statuses.get(sid) in RUNNING

    def dead(self, sid: str) -> bool:
        """It can't receive (D30): Send, Ctrl+S and Send all leave its queue alone (#62)."""
        return self.statuses.get(sid) == "dead"

    def stale(self, sid: str) -> bool:
        """Running older wheelhouse code: a relaunch picks up the new code."""
        return self.running(sid) and needs_relaunch(self.listed(sid))

    def sends_now(self, sid: str) -> bool:
        """Running code from before queued answers, which would deliver one at once: so
        Ctrl+Enter sends, whatever the mode."""
        return self.sends_at_once(self.listed(sid))

    def sends_at_once(self, s) -> bool:
        """sends_now, on the row given: submit's, read as it is now."""
        return self.running(s["id"]) and not can_queue(s)

    def pending(self, sid: str) -> str | None:
        """The request (end or park) a session has been asked to act on, if any, read now."""
        return requested(self.row(sid))

    def shown_status(self, sid: str) -> str:
        st = self.statuses.get(sid, "dead")
        if sid in self.relaunching:
            return "relaunching"
        what = requested(self.listed(sid))
        return PENDING[what] if what and st in RUNNING else st

    def busy(self, sid: str) -> bool:
        return working(self.listed(sid)) and self.statuses.get(sid) in ("live", "stalled")

    # launching and Relaunch

    def open_session(self, sid: str, restore: bool = False) -> Done:
        """Launch a session the way it runs, a host or a tab; a restore runs it the way new
        sessions run now (launch.restore_session)."""
        try:
            if restore:
                launch.restore_session(self.store, sid, sdk=self.sdk)
            else:
                launch.open_session(self.store, sid)
        except Exception as e:   # already running, wt.exe missing...
            return Done(False).say(str(e), "error")
        return Done()

    def relaunch(self, sid: str) -> Done:
        """A dead session brought back where it left off, and unparked. Whether it was."""
        done = self.open_session(sid, restore=True)
        if done.ok:
            self.store.set_parked(sid, False)
            done.say("relaunching")
        return done

    def stop_host(self, sid: str, host: tuple) -> Done:
        """Stop a session's host, as SIGTERM does (its own clean stop: Claude Code is
        disconnected, and an open permission or question closes as withdrawn, or as lost
        when the new host starts). The confirm may have sat open meanwhile, so only the
        host it was asked about, (pid, start time), is stopped: one handed to a shell tab
        registers the tab's Claude Code instead, the person's to /exit. finish_relaunches
        starts it again once the process has gone. Whether anything changed."""
        s = self.row(sid)
        if runner(s) != "sdk" or s["shell"]:
            return Done(False).say(TAB_RELAUNCH, "warning")
        if not liveness.is_alive(s["claude_pid"], s["claude_start"], s["boot_id"]):
            return self.relaunch(sid)   # gone meanwhile: nothing to stop
        if (s["claude_pid"], s["claude_start"]) != host:
            return Done(False).say(f"{display_name(s)} started again meanwhile: Relaunch again to stop this host",
                                   "warning")
        try:
            os.kill(s["claude_pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass   # exited between the check and the signal: finish_relaunches starts it again
        except OSError as e:
            return Done(False).say(f"couldn't stop its host: {e}", "error")
        self.relaunch_under_way(sid, host)
        return Done().say(f"relaunching {display_name(s)}: stopping its host")

    def compact_first(self, sid: str) -> bool:
        """Whether Relaunch should offer to compact first: a running host whose context, as
        it last recorded it, is past COMPACT_FIRST_TOKENS, whose restart would write all of
        that to the prompt cache again."""
        s = self.listed(sid)
        return (runner(s) == "sdk" and not s["shell"] and self.running(sid)
                and (s["context_tokens"] or 0) > COMPACT_FIRST_TOKENS)

    def compact_then_relaunch(self, sid: str, host: tuple) -> Done:
        """Relaunch, compacting first: the host is asked to Compact (unless a Compact is
        under way already), and once the session is compacted and idle, finish_relaunches
        stops it (stop_host), and starts it again once gone. Only the host the confirm was
        about, (pid, start time), as stop_host."""
        s = self.row(sid)
        if runner(s) != "sdk" or s["shell"]:
            return Done(False).say(TAB_RELAUNCH, "warning")
        if not liveness.is_alive(s["claude_pid"], s["claude_start"], s["boot_id"]):
            return self.relaunch(sid)   # gone meanwhile: nothing to compact
        if (s["claude_pid"], s["claude_start"]) != host:
            return Done(False).say(f"{display_name(s)} started again meanwhile: Relaunch again to stop this host",
                                   "warning")
        activity = s["activity"] or ""
        if not (s["host_command"] == "compact" or activity.startswith("compacting") or activity == AWAITING_NOTES):
            self.store.command(sid, "compact")
        self.relaunch_under_way(sid, host, asked=time.time())
        return Done().say(f"relaunching {display_name(s)}: compacting first (it's asked what to keep)")

    def relaunch_under_way(self, sid: str, host: tuple | None, asked: float | None = None) -> None:
        """A Relaunch that has stopped a session's host, or is compacting before it does
        (Compact asked for at the epoch second `asked`), kept here and in the store; or, with
        no host, done with."""
        if host:
            wait = COMPACT_RELAUNCH_WAIT if asked is not None else RELAUNCH_WAIT
            self.relaunching[sid] = (time.monotonic() + wait, host)
            self.store.set_relaunch(sid, host, time.time() + wait, asked)
            if asked is not None:
                self.compacting[sid] = asked
            else:
                self.compacting.pop(sid, None)
        else:
            self.relaunching.pop(sid, None)
            self.lapsed.discard(sid)
            self.compacting.pop(sid, None)
            self.store.set_relaunch(sid, None)

    def compacting_relaunch(self, sid: str, s, host: tuple, deadline: float) -> None:
        """A Relaunch compacting first, while its host still runs. Once the session is
        compacted (a context recorded since Compact was asked for, under
        COMPACT_FIRST_TOKENS) and idle, its host is stopped (stop_host). Given up, and said
        so, past COMPACT_RELAUNCH_WAIT (checked first, so a wheelhouse started hours later
        never restarts it), once its host has gone to a shell tab or been started again,
        or if Compact got no notes or was interrupted. Cancelled by another wheelhouse (an
        Interrupt there), it is let go quietly. A host that died meanwhile is relaunched as
        any stopped host is."""
        name, activity = display_name(s), s["activity"] or ""
        alive = liveness.is_alive(s["claude_pid"], s["claude_start"], s["boot_id"])
        mine = (s["claude_pid"], s["claude_start"]) == host
        stored = self.store.relaunches().get(sid)
        if stored is None or stored[2] is None:   # cancelled elsewhere
            self.relaunching.pop(sid, None)
            self.lapsed.discard(sid)
            self.compacting.pop(sid, None)
            return
        if sid in self.lapsed or time.monotonic() > deadline:
            why = f"it wasn't compacted within {COMPACT_RELAUNCH_WAIT // 60} minutes"
        elif s["shell"]:
            why = "it was handed to a shell tab"
        elif not mine and alive:
            why = "its host was started again meanwhile"
        elif not alive:   # died: relaunched as any stopped host, from the next tick
            self.relaunch_under_way(sid, host)
            return
        elif since(s["interrupted_at"], self.compacting[sid]):   # by any wheelhouse, older ones too
            why = "Compact was interrupted"
        elif compacted_since(s, self.compacting[sid]) and activity.startswith(IDLE_LINES):
            for what in ("compact", "interrupt"):   # either, left untaken, would greet the new host
                self.store.take_command(sid, what)   # (a Shell stays: the person's, for the new host)
            done = self.stop_host(sid, host)
            for notice in done.notices:
                self.emit(notice)
            if not done.ok:
                self.relaunch_under_way(sid, None)
            return
        elif activity == NO_NOTES:
            why = "Compact got no notes back"
        elif activity == "interrupted":
            why = "Compact was interrupted"
        else:
            return
        self.relaunch_under_way(sid, None)
        self.announce(f"{name} wasn't relaunched: {why}. Relaunch again to try once more, or to relaunch "
                      "without compacting", "warning")

    def finish_relaunches(self) -> None:
        """Start each stopped host again once its process has really gone: launch refuses a
        session whose registered process is alive, and a new host registers only if free.
        One that another wheelhouse has started meanwhile (a newer pid, alive) is relaunched
        already. A relaunch unparks, as one of a dead session does. One a hub started again
        found past its deadline (lapsed) is let go, its host not started: Restore is the
        person's. The hub does this by itself, so what happened goes to every listener."""
        for sid, (deadline, host) in list(self.relaunching.items()):
            s = next((x for x in self.sessions if x["id"] == sid), None)
            if s is None:   # ended meanwhile
                self.relaunch_under_way(sid, None)
            elif sid in self.compacting:
                self.compacting_relaunch(sid, s, host, deadline)
            elif sid in self.lapsed and not liveness.is_alive(s["claude_pid"], s["claude_start"], s["boot_id"]):
                self.relaunch_under_way(sid, None)
                self.announce(f"{display_name(s)} wasn't relaunched: Restore it", "warning")
            elif not liveness.is_alive(s["claude_pid"], s["claude_start"], s["boot_id"]):
                done = self.open_session(sid)   # as it ran: a host again, not WHEELHOUSE_RUNNER's way
                self.relaunch_under_way(sid, None)   # after: a hub stopped between sees the new host
                for notice in done.notices:
                    self.emit(notice)
                if done.ok:
                    self.store.set_parked(sid, False)
                    self.statuses[sid] = liveness.status(self.row(sid), waking=self.waking)   # not dead for a tick
                    self.announce(f"relaunched {display_name(s)}")
            elif (s["claude_pid"], s["claude_start"]) != host:
                self.relaunch_under_way(sid, None)
                self.store.set_parked(sid, False)
                self.announce(f"relaunched {display_name(s)}")
            elif time.monotonic() > deadline:
                self.relaunch_under_way(sid, None)
                self.announce(f"{display_name(s)}'s host didn't stop within {RELAUNCH_WAIT}s, so it wasn't "
                              f"relaunched: see hosts/{sid}.log", "error")

    def restore(self, sid: str) -> Done:
        """Restore: only a dead session, brought back where it left off and unparked; one
        that isn't dead is left as it is, and said so. Whether it was restored."""
        if self.statuses.get(sid) != "dead":
            return Done(False).say("only a dead session can be restored", "warning")
        done = self.open_session(sid, restore=True)
        if done.ok:
            self.store.set_parked(sid, False)
        return done

    def restorable(self) -> list[str]:
        """What Restore all brings back: every dead session that isn't parked."""
        return [s["id"] for s in self.sessions if self.statuses.get(s["id"]) == "dead" and not s["parked"]]

    def restore_all(self, sids: list[str]) -> Done:
        done, n = Done(), 0
        for sid in sids:
            one = self.open_session(sid, restore=True)
            done.notices.extend(one.notices)
            n += one.ok
        return done.say(f"restoring {n} session(s)")

    def new_session(self, cwd: str, name: str, ticket: str, brief: str, runner: str, model: str | None = None,
                    effort: str | None = None) -> Done:
        """A new session, launched. Its model and effort as given, else the defaults
        (models.chosen): stored either way, so every launch of it passes them. A host only in
        SDK mode (features.py): out of it, runner sdk is refused before anything is created."""
        if runner == "sdk" and not self.sdk:
            return Done(False).say("a new session runs in a tab", "error")
        model, effort = models.chosen(model, effort)
        sid = self.store.create_session(cwd, name=name, ticket=ticket, brief=brief, runner=runner, model=model,
                                        effort=effort)
        done = self.open_session(sid)
        done.sid = sid
        return done

    def adoptable(self) -> list[adopt.Candidate]:
        """The Claude Code sessions on disk that aren't in the wheelhouse yet (adopt.candidates)."""
        return adopt.candidates(self.store)

    @staticmethod
    def running_pid(sid: str) -> int | None:
        """The pid of Claude Code running a session outside the wheelhouse, if it is running."""
        return liveness.running_pid(sid)

    def adopt(self, candidate: adopt.Candidate, name: str) -> Done:
        """A session started outside the wheelhouse, brought in (adopt.adopt). Whether it was."""
        try:
            adopt.adopt(self.store, candidate, name, sdk=self.sdk)
        except Exception as e:   # came back to life, wt.exe missing...
            return Done(False).say(str(e), "error")
        return Done().say(f"adopting {name or short(candidate.id)}")

    def answer_offer(self, take: bool) -> Done:
        """The first-run tutorial offer, answered either way: it never comes back. Whether the
        tutorial started."""
        if not take:
            self.store.set_setting(tutorial.OFFER_KEY, "dismissed")
            return Done(False).say("claude-wheelhouse tutorial runs the tutorial any time; ? lists every key")
        try:
            tutorial.start(self.store, sdk=self.sdk)
        except Exception as e:   # claude missing, say
            self.store.set_setting(tutorial.OFFER_KEY, "dismissed")
            return Done(False).say(f"couldn't start the tutorial: {e}", "error")
        return Done().say("tutorial started: follow the checklist on the right")

    # Park and End: a running session is asked; a dead one, or one forced, is acted on

    def ask(self, sid: str, what: str) -> Done:
        self.store.request(sid, what)
        return Done().say(f"asked the session to {what}")

    def cancel_request(self, sid: str, what: str) -> Done:
        self.store.cancel_request(sid, what)
        return Done().say(f"{what} request cancelled")

    def act_on_dead(self, sid: str, what: str) -> Done:
        """Park or End a dead session now. The confirm may have sat open while it came back:
        then it's left as it is, and said so. Whether it was acted on."""
        if liveness.status(self.row(sid), waking=self.waking) in RUNNING:
            return Done(False).say(f"the session is running again: press {what.capitalize()} to ask it instead",
                                   "warning")
        return self.force(sid, what)

    def force(self, sid: str, what: str) -> Done:
        if what == "end":
            self.store.end(sid)
        else:
            self.store.set_parked(sid, True)
        return Done()

    def unpark(self, sid: str) -> Done:
        self.store.set_parked(sid, False)
        return Done()

    def rename(self, sid: str, name: str | None) -> Done:
        """Whether the name changed: not if cancelled (None) or the same."""
        if name is None or name == self.row(sid)["name"]:
            return Done(False)
        self.store.rename(sid, name)
        return Done().say(f"renamed to {name}" if name else "name cleared: it shows its directory")

    # answers and queues

    def asking(self, target):
        """The open permission item a target is, if it is one."""
        item = target and target[1] and self.store.item(*target)
        return item if item and item["kind"] == "permission" and item["status"] == "open" else None

    def submit(self, target: tuple, text: str) -> Done:
        """What the person submitted for a target: an open permission is denied with it at
        once, never queued (#53); otherwise it's queued or sent at once, by the session's mode.
        Answering an item is an explicit act: a decision is seen at once, not after the dwell,
        in the same transaction as the send or the queue (store._mark_seen).
        Whether it went (not if the permission was answered meanwhile), and whether it denied
        a permission."""
        s = self.row(target[0])
        done = Done()
        if self.asking(target):
            try:
                self.answer_permission(*target, "deny", text)
            except KeyError as e:   # answered meanwhile
                return Done(False).say(str(e.args[0]), "warning")
            done.denied = True
            done.say(f"denied {target[1]}, with your message")
        elif mode(s) == "immediate":
            self.store.send(target[0], text, target[1])
            done.say(f"sent to {aimed(target)}")
        # on the row as it is now: its old monitor would deliver a draft at once anyway
        elif self.sends_at_once(s):
            self.store.send(target[0], text, target[1])
            done.say(f"sent to {aimed(target)} now: that session runs older wheelhouse code, "
                     "so it can't queue until it's relaunched", "warning")
        else:
            self.store.queue(target[0], text, target[1])
            if self.dead(target[0]):   # aimed names the item, so say whose queue isn't running
                done.say(f"queued for {aimed(target)}, but the session isn't running: "
                         "Restore it, then Ctrl+S or Send sends its queue")
            else:
                done.say(f"queued for {aimed(target)}: Ctrl+S or Send sends the session's queue")
        return done

    def answer_permission(self, sid: str, ref: str, decision: str, message: str = "") -> Done:
        """Allow, Always or Deny: KeyError if it was answered meanwhile."""
        self.store.answer_permission(sid, ref, decision, message)
        return Done()

    def unqueue(self, msg_id: int) -> str | None:
        """A queued answer taken back: its text, or None if it went meanwhile."""
        return self.store.unqueue(msg_id)

    def sent_note(self, sid: str, n: int) -> str:
        s = self.store.session(sid)
        name = (s["name"] or short(sid)) if s else short(sid)
        late = "" if self.running(sid) else " (not running: delivered when it's restored)"
        return f"{n} to {name}{late}"

    def send_queue(self, sid: str) -> Done:
        """A session's queue, sent as one message; a dead one's stays queued (#62)."""
        if self.dead(sid):
            return Done(False).say(f"{display_name(self.row(sid))} isn't running: Restore it first", "warning")
        n = self.store.dispatch(sid)
        return Done(bool(n)).say(f"sent {self.sent_note(sid, n)}" if n else "nothing queued for that session")

    def send_all(self) -> Done:
        """Every queue but a dead session's, which stays queued until it's restored (#62)."""
        queued = dict.fromkeys(m["session_id"] for m in self.store.drafts())
        sent = [(sid, n) for sid in queued if not self.dead(sid) and (n := self.store.dispatch(sid))]
        if sent:
            return Done().say("sent " + "; ".join(self.sent_note(sid, n) for sid, n in sent))
        if any(self.dead(sid) for sid in queued):
            return Done(False).say("nothing queued for a running session: a dead one's queue waits until "
                                   "it's restored", "warning")
        return Done(False).say("nothing queued")

    def toggle_mode(self, sid: str) -> Done:
        s = self.row(sid)
        new = "immediate" if mode(s) == "queued" else "queued"
        self.store.set_mode(sid, new)
        name = s["name"] or short(sid)
        done = Done().say(f"{name}: answers now send as you submit them" if new == "immediate" else
                          f"{name}: answers now queue until you send them (Ctrl+S or Send)")
        if new == "immediate" and (n := len(self.store.drafts(sid))):
            done.say(f"{n} answer(s) still queued for {name}: Ctrl+S or Send sends them")
        return done

    def host_command(self, sid: str, what: str) -> Done:
        """Interrupt, Compact or Shell, for the session's host to carry out. Interrupt also
        cancels a Relaunch compacting first, whichever wheelhouse began it (the store's)."""
        try:
            self.store.command(sid, what)
        except SessionGone:
            return Done(False)
        done = Done().say({"interrupt": "interrupting", "compact": "compacting: asking what to keep",
                           "shell": "opening a terminal tab"}[what])
        stored = self.store.relaunches().get(sid)
        if what == "interrupt" and stored is not None and stored[2] is not None:
            self.relaunch_under_way(sid, None)
            done.say("its Relaunch, waiting on Compact, is cancelled: it stays as it is", "warning")
        return done

    # model and effort (.plan/model-effort.md)

    def set_model(self, sid: str, model: str) -> Done:
        """A hosted session's model: its host applies it live, and every launch passes it.
        Not a tab's: the wheelhouse can't change one (Q52), it says so. Whether it changed."""
        return self.choose(sid, "model", model)

    def set_effort(self, sid: str, effort: str) -> Done:
        """A hosted session's effort, as set_model: live, with no side effects to warn of."""
        return self.choose(sid, "effort", effort)

    def choose(self, sid: str, what: str, value: str) -> Done:
        s = self.row(sid)
        if runner(s) != "sdk":
            return Done(False).say(f"{display_name(s)} runs in a tab: type /{what} there", "warning")
        if not self.sdk:   # a host's session out of SDK mode runs on as it was set (features.py)
            return Done(False).say(f"{display_name(s)}'s {what} can't be changed here", "warning")
        same = models.field(s, what) == value
        # still a choice, dated now, so a /model or /effort typed in a shell tab before it doesn't override it
        (self.store.set_model if what == "model" else self.store.set_effort)(sid, value)
        if same:
            return Done(False)   # it's that already: nothing to apply or say
        shown = models.name(value) or value
        if s["shell"]:   # its host gave it to a shell tab: it launches with the column on taking it back
            return Done().say(f"{display_name(s)}: {what} {shown} when the wheelhouse takes it back from its shell "
                              f"tab, unless /{what} is typed there after this")
        return Done().say(f"{display_name(s)}: {what} {shown}")

    def model_state(self, s) -> dict:
        """What a session runs on, for the stats pane. A hosted session's is what it is set
        to: its columns, else what its host confirmed, and whether its host has yet to apply
        them (pending: "applying", or "on relaunch" for a host on older code that never
        will). A tab's is what its transcript says it runs (the latest response's requested
        model and effort: a /model typed there counts), else its columns until the
        transcript has any. None where unknown: the stats pane falls back on the transcript."""
        want = (models.field(s, "model"), models.field(s, "effort"))
        got = (models.field(s, "applied_model"), models.field(s, "applied_effort"))
        hosted = runner(s) == "sdk" and self.running(s["id"]) and not s["shell"]
        pending = None
        if hosted and any(w and w != g for w, g in zip(want, got)):
            pending = "applying" if any(got) else "on relaunch"
        if runner(s) == "tab":
            follower = self.contexts.get(s["id"])
            snap = follower.snap if follower and follower.ready else None
            ran = (snap.requested or snap.model, snap.effort) if snap else (None, None)
            return {"model": ran[0] or want[0], "effort": ran[1] or want[1], "pending": None, "hosted": False}
        # hosted says its effort can be changed live, so the slider shows: not out of SDK mode
        return {"model": want[0] or (got[0] if hosted else None), "effort": want[1] or (got[1] if hosted else None),
                "pending": pending, "hosted": hosted and self.sdk}

    # items

    def mark_seen(self, sid: str, ref: str) -> bool:
        """Whether an unseen decision became seen: a no-op for anything else."""
        return self.store.mark_seen(sid, ref)

    def close(self, sid: str, ref: str, closed: bool) -> Done:
        """Close a question or decision, or reopen it: a question as answered, a decision as
        seen. Dismiss a settled task or subagent, or bring it back. One finished already has
        nothing to close."""
        item = self.store.item(sid, ref)
        if item is None:
            self.row(sid)   # SessionGone if that's why
            raise KeyError(f"no item {ref} in this session")
        if closed and standing(item) == "finished":
            return Done(False)
        if item["kind"] == "decision":
            self.store.close_decision(sid, ref, closed)
        elif item["kind"] in DISMISSABLE:
            self.store.dismiss(sid, ref, closed)
        else:
            self.store.update_item(sid, ref, status="closed" if closed else "answered")
        return Done()

    # the transcripts

    def read_contexts(self) -> None:
        """The context size of each session with a bar, read on a worker thread: every one
        not parked, read once if it isn't running (and once more when it stops), and every
        one running, parked or not. A read the stats pane has under way counts."""
        listed = {s["id"]: s for s in self.sessions if not s["parked"] or self.running(s["id"])}
        for sid in [sid for sid in self.contexts if sid not in listed]:
            del self.contexts[sid]
        self.settled &= set(listed)
        for sid in listed:
            follower = self.contexts.setdefault(sid, stats.follower(sid))
            alive = self.running(sid)
            if alive:
                self.settled.discard(sid)
            if follower.reading or (follower.ready and sid in self.settled):
                continue
            if not alive:   # read once more after it stops: what it wrote in its last second
                self.settled.add(sid)
            follower.reading = True
            self.spawn(functools.partial(self.read_context, follower), "contexts")

    def read_context(self, follower: stats.UsageFollower) -> None:
        """On a worker thread. A failed read leaves the last size up; the next tick reads
        again. One that brought anything is said at once ("landed"), for the stats pane too
        if it shows that session: the follower is its as well, and it started no read of its
        own while this one was under way."""
        first = not follower.ready
        changed = stats.read_safely(follower)
        if follower.ready and not follower.error:
            try:
                self.copy_back(follower)
            except sqlite3.OperationalError:   # a locked database: the next tick reads, and copies, again
                self.settled.discard(follower.sid)
        if changed or first:
            self.post(functools.partial(self.emit, Event("landed", sid=follower.sid)))

    def copy_back(self, follower: stats.UsageFollower) -> None:
        """A /model or /effort typed in a tab, carried to the session's columns, so its next
        Relaunch, Restore or return from a shell tab launches with it (Q56), and the stats
        pane shows it: by stats.carry's rule, last write wins. A tab session's on every read;
        a hosted session's while it is out in a shell tab (its host carries once more as it
        takes it back), not while hosted, as its host applies the columns."""
        s = self.store.session(follower.sid)
        if s is not None and (runner(s) == "tab" or s["shell"]):
            stats.carry(self.store, follower.sid, follower.snap)

    def watch_agents(self) -> None:
        """Each session's subagents brought up to date as A items, on a worker thread: every
        one not parked once, every one running each tick, and once more when one dies with
        a subagent item still running, which then fails (parked or not): one parked and dead
        already when the wheelhouse starts too, by the items it tracks (running_agents). The
        items show at the next refresh. A sync's failure is said once, as a warning, until it changes."""
        watchers = self.agent_watchers
        listed = {s["id"]: s for s in self.sessions if not s["parked"] or self.running(s["id"])
                  or (watchers[s["id"]].unfinished() if s["id"] in watchers else s["running_agents"])}
        for sid in [sid for sid in watchers if sid not in listed]:
            del watchers[sid]
            self.agent_errors.pop(sid, None)
        for sid, s in listed.items():
            watcher = watchers.setdefault(sid, subagents.AgentWatcher(sid))
            if watcher.error and watcher.error != self.agent_errors.get(sid):
                self.agent_errors[sid] = watcher.error
                self.announce(f"{display_name(s)}: {watcher.error}", "warning")
            alive = self.running(sid)
            if watcher.syncing or (watcher.ready and not alive and not watcher.unfinished()):
                continue
            watcher.syncing = True
            self.spawn(functools.partial(self.sync_agents, watcher, alive), "agents")

    def sync_agents(self, watcher: subagents.AgentWatcher, alive: bool = True) -> None:
        """On a worker thread. A failed sync (the session ended under it, a transcript gone
        between stat and open) is tried again next tick, from where it got to, and says why
        in the watcher's error."""
        try:
            watcher.sync(self.store, alive=alive)
            watcher.error = None
        except Exception as e:
            watcher.error = f"couldn't track subagents: {e}"[:120]
        finally:
            watcher.syncing = False

    def fetch_usage(self) -> None:
        """The account's session and weekly usage, fetched on a worker thread when it's due
        (at most once a minute, stats.AccountUsage). Fetched whether or not any surface
        shows it (no stats pane, say): one call a minute, and a daemon serves it to clients
        that come and go."""
        if self.usage.due(time.time()):
            self.spawn(self.usage.fetch, "usage")

    def sent(self, sid: str, msg_id: int) -> str | None:
        """The full text of a message the person sent, for one a notification cut short."""
        m = self.store.message(sid, msg_id)
        return m["body"] if m else None

    def conversation(self, sid: str) -> list[tuple[str, str]] | None:
        """A session's conversation as (who, markdown) blocks, from its transcript, with
        what's queued for it as a general message; None once the session has gone."""
        s = self.store.session(sid)
        if s is None:
            return None
        follower = self.followers.setdefault(sid, transcript.Follower(sid))
        recs = follower.read()
        queued = tuple(m["body"] for m in self.store.drafts(sid) if m["item_ref"] is None)
        tab = None if runner(s) == "sdk" and not s["shell"] else launch.tab_title(s)
        key = (s["name"] or short(sid), tab, follower.seen, queued)
        if follower.blocks_key != key:   # parsed once per change, not on every refresh tick
            follower.blocks_key, follower.blocks = key, transcript.blocks(*key[:2], recs, queued, functools.partial(self.sent, sid))
        return follower.blocks

    def agent_conversation(self, sid: str, ref: str) -> list[tuple[str, str]] | None:
        """A subagent item's own conversation as (who, markdown) blocks, from its transcript
        beside its session's (transcript.agent_blocks): read now, as conversation is, and
        again only once the file has changed, so asked each refresh it follows the subagent
        live. Following one stops following the last (unfollow_agent too); saying so when
        its transcript isn't found, or the item isn't tied to a subagent. None if the item
        isn't a subagent's, or has gone."""
        item = self.store.item(sid, ref)
        if item is None or item["kind"] != "agent":
            return None
        follower = self.agent_followers.pop((sid, ref), None)
        if follower is None:
            aid = self.store.agent_of(sid, ref)
            if aid is None:
                return transcript.agent_blocks(None, linked=False)
            watcher = self.agent_watchers.get(sid)
            follower = transcript.AgentFollower(sid, aid, watcher and watcher.main)
        self.agent_followers[(sid, ref)] = follower   # the newest asked for, last
        while len(self.agent_followers) > AGENTS_FOLLOWED:
            del self.agent_followers[next(iter(self.agent_followers))]
        recs = follower.read()
        if follower.blocks_key != follower.seen or follower.blocks is None:   # parsed once per change
            follower.blocks_key, follower.blocks = follower.seen, transcript.agent_blocks(recs, prompt=follower.prompt)
        return follower.blocks

    def unfollow_agent(self) -> None:
        """No surface shows a subagent's conversation now: no transcript of one is read."""
        self.agent_followers.clear()
