"""The hub: what the wheelhouse knows and does, whatever shows it (.plan/remote.md, phase 1).
The TUI owns one, in process, and reads and acts through it; phase 2 puts it in a daemon,
behind HTTP. It never imports Textual: what a surface should know, it says as events
(Event) to whoever listens.

It owns the sessions' liveness, and Relaunch, from the stop to the start again. Like the
app before it, it never acts on a session by itself: a host that dies stays dead until
the person restores it."""

import os
import signal
import time
from dataclasses import dataclass
from typing import Callable

from . import launch, liveness
from .store import SessionGone, Store, can_queue, needs_relaunch, runner

RUNNING = ("live", "stalled", "starting")
# a hosted session's activity when nothing is under way: errored and stopped count as resting
RESTING = ("idle", "interrupted", "stopped", "in a shell tab", "error")
PENDING = {"end": "ending", "park": "parking"}
RELAUNCH_WAIT = 30   # seconds a host has to stop for Relaunch before it gives up and says so
TAB_RELAUNCH = "a session in a tab relaunches once it has exited: /exit it there, then Relaunch"


def short(sid: str) -> str:
    return sid[:6]


def display_name(s) -> str:
    return s["name"] or os.path.basename(s["cwd"]) or short(s["id"])


@dataclass(frozen=True)
class Event:
    """What a surface should know. "notice": text to show the person, with its severity
    (information, warning or error)."""
    kind: str
    text: str = ""
    severity: str = "information"


class Hub:
    """The wheelhouse's service over the store. Its state lives as long as it does."""

    def __init__(self, store: Store | None = None):
        self.store = store or Store()
        self.listeners: list[Callable[[Event], None]] = []
        self.wake = liveness.WakeDetector()
        self.waking = False
        self.sessions = []
        self.statuses: dict[str, str] = {}
        # sessions whose host Relaunch has stopped: started again once it has gone. Each has
        # a monotonic deadline and the stopped host, (pid, start time)
        self.relaunching: dict[str, tuple[float, tuple]] = {}
        # unsent text typed for each target, (session id, ref or None), kept in memory only
        self.unsent: dict[tuple, str] = {}

    # events

    def listen(self, listener: Callable[[Event], None]) -> None:
        self.listeners.append(listener)

    def say(self, text: str, severity: str = "information") -> None:
        for listener in self.listeners:
            listener(Event("notice", text, severity))

    # sessions and their liveness

    def refresh(self) -> None:
        """The sessions read again, and each one's liveness: only liveness, as the
        wheelhouse never deletes or parks anything by itself."""
        self.waking = self.wake.tick()
        self.sessions = self.store.sessions()
        self.statuses = {s["id"]: liveness.status(s, waking=self.waking) for s in self.sessions}

    def row(self, sid: str):
        """The session's row, or SessionGone: it can end at any moment (in-session /wheelhouse end)."""
        s = self.store.session(sid)
        if s is None:
            raise SessionGone(sid)
        return s

    def running(self, sid: str) -> bool:
        return self.statuses.get(sid) in RUNNING

    def dead(self, sid: str) -> bool:
        """It can't receive (D30): Send, Ctrl+S and Send all leave its queue alone (#62)."""
        return self.statuses.get(sid) == "dead"

    def stale(self, s) -> bool:
        """Running older wheelhouse code: a relaunch picks up the new code."""
        return self.running(s["id"]) and needs_relaunch(s)

    def sends_now(self, s) -> bool:
        """Running code from before queued answers, which would deliver one at once: so
        Ctrl+Enter sends, whatever the mode."""
        return self.running(s["id"]) and not can_queue(s)

    @staticmethod
    def pending(s) -> str | None:
        """The request (end or park) a session has been asked to act on, if any."""
        return next((what for what in PENDING if s[f"{what}_requested_at"]), None)

    def shown_status(self, s) -> str:
        st = self.statuses.get(s["id"], "dead")
        if s["id"] in self.relaunching:
            return "relaunching"
        what = self.pending(s)
        return PENDING[what] if what and st in RUNNING else st

    def busy(self, s) -> bool:
        working = bool(s["running"]) or (runner(s) == "sdk" and not (s["activity"] or "idle").startswith(RESTING))
        return working and self.statuses.get(s["id"]) in ("live", "stalled")

    # launching and Relaunch

    def open_session(self, sid: str, restore: bool = False) -> bool:
        """Launch a session the way it runs, a host or a tab; a restore runs it the way new
        sessions run now (launch.restore_session)."""
        try:
            (launch.restore_session if restore else launch.open_session)(self.store, sid)
        except Exception as e:   # already running, wt.exe missing...
            self.say(str(e), "error")
            return False
        return True

    def relaunch(self, sid: str) -> bool:
        """A dead session brought back where it left off, and unparked. Whether it was."""
        if not self.open_session(sid, restore=True):
            return False
        self.store.set_parked(sid, False)
        self.say("relaunching")
        return True

    def stop_host(self, sid: str, host: tuple) -> bool:
        """Stop a session's host, as SIGTERM does (its own clean stop: Claude Code is
        disconnected, and an open permission or question closes as withdrawn, or as lost
        when the new host starts). The confirm may have sat open meanwhile, so only the
        host it was asked about, (pid, start time), is stopped: one handed to a shell tab
        registers the tab's Claude Code instead, the person's to /exit. finish_relaunches
        starts it again once the process has gone. Whether anything changed."""
        s = self.row(sid)
        if runner(s) != "sdk" or s["shell"]:
            self.say(TAB_RELAUNCH, "warning")
            return False
        if not liveness.is_alive(s["claude_pid"], s["claude_start"], s["boot_id"]):
            return self.relaunch(sid)   # gone meanwhile: nothing to stop
        if (s["claude_pid"], s["claude_start"]) != host:
            self.say(f"{display_name(s)} started again meanwhile: Relaunch again to stop this host", "warning")
            return False
        try:
            os.kill(s["claude_pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass   # exited between the check and the signal: finish_relaunches starts it again
        except OSError as e:
            self.say(f"couldn't stop its host: {e}", "error")
            return False
        self.relaunching[sid] = (time.monotonic() + RELAUNCH_WAIT, host)
        self.say(f"relaunching {display_name(s)}: stopping its host")
        return True

    def finish_relaunches(self) -> None:
        """Start each stopped host again once its process has really gone: launch refuses a
        session whose registered process is alive, and a new host registers only if free.
        One that another wheelhouse has started meanwhile (a newer pid, alive) is relaunched
        already. A relaunch unparks, as one of a dead session does."""
        for sid, (deadline, host) in list(self.relaunching.items()):
            s = next((x for x in self.sessions if x["id"] == sid), None)
            if s is None:   # ended meanwhile
                del self.relaunching[sid]
            elif not liveness.is_alive(s["claude_pid"], s["claude_start"], s["boot_id"]):
                del self.relaunching[sid]
                if self.open_session(sid):   # as it ran: a host again, not WHEELHOUSE_RUNNER's way
                    self.store.set_parked(sid, False)
                    self.statuses[sid] = liveness.status(self.row(sid), waking=self.waking)   # not dead for a tick
                    self.say(f"relaunched {display_name(s)}")
            elif (s["claude_pid"], s["claude_start"]) != host:
                del self.relaunching[sid]
                self.store.set_parked(sid, False)
                self.say(f"relaunched {display_name(s)}")
            elif time.monotonic() > deadline:
                del self.relaunching[sid]
                self.say(f"{display_name(s)}'s host didn't stop within {RELAUNCH_WAIT}s, so it wasn't "
                         f"relaunched: see hosts/{sid}.log", "error")

    # text typed but not submitted

    def keep(self, target: tuple, text: str) -> None:
        """What's typed for a target, kept until the box shows it again; blank, forgotten."""
        if text.strip():
            self.unsent[target] = text
        else:
            self.unsent.pop(target, None)

    def take(self, target: tuple) -> str:
        """What was typed for a target, handed back to show: no longer kept here."""
        return self.unsent.pop(target, "")
