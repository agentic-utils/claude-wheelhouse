"""The hub: what the wheelhouse knows and does, whatever shows it (.plan/remote.md, phase 1).
The TUI owns one, in process, and reads and acts through it; phase 2 puts it in a daemon,
behind HTTP. It never imports Textual: what a surface should know, it says as events
(Event) to whoever listens.

It owns the sessions' liveness, and Relaunch, from the stop to the start again. Like the
app before it, it never acts on a session by itself: a host that dies stays dead until
the person restores it. And it reads the sessions' transcripts: each conversation, each
context size and each session's subagents, and fetches the account's usage, the reading
and fetching on the owner's worker threads (spawn), never on the thread that calls it.

Its commands are the person's acts on sessions and items: each checks what it needs,
writes the store and says what it did. One on a session that has gone raises SessionGone
(row), which a surface reports; the dialogs that ask first, and their wording, are the
surface's."""

import functools
import os
import signal
import time
from dataclasses import dataclass
from typing import Callable

from . import adopt, launch, liveness, stats, subagents, transcript, tutorial
from .store import DISMISSABLE, SessionGone, Store, can_queue, mode, needs_relaunch, runner, standing

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


def aimed(target) -> str:
    """What a message goes to: an item's ref, or the session itself (a general message)."""
    return target[1] or "the session"


@dataclass(frozen=True)
class Event:
    """What a surface should know. "notice": text to show the person, with its severity
    (information, warning or error). "landed": a read of a session's transcript (sid)
    brought something, so what shows its context size can repaint."""
    kind: str
    text: str = ""
    severity: str = "information"
    sid: str | None = None


class Hub:
    """The wheelhouse's service over the store. Its state lives as long as it does."""

    def __init__(self, store: Store | None = None, spawn=None, post=None):
        self.store = store or Store()
        self.listeners: list[Callable[[Event], None]] = []
        # how its owner runs work on a worker thread, by group, and a callback back on its
        # own thread (a no-op once it's closing): both at once by default, as in a test
        self.spawn: Callable[[Callable[[], None], str], None] = spawn or (lambda work, group: work())
        self.post: Callable[[Callable[[], None]], None] = post or (lambda callback: callback())
        self.wake = liveness.WakeDetector()
        self.waking = False
        self.sessions = []
        self.statuses: dict[str, str] = {}
        # sessions whose host Relaunch has stopped: started again once it has gone. Each has
        # a monotonic deadline and the stopped host, (pid, start time)
        self.relaunching: dict[str, tuple[float, tuple]] = {}
        self.followers: dict[str, transcript.Follower] = {}
        # each session's context size for the session list: read on workers, by a follower
        # shared with the stats pane (stats.follower), so each transcript is read once
        self.contexts: dict[str, stats.UsageFollower] = {}
        # each session's subagents, tracked as A items (#69): read on workers, as contexts are
        self.agent_watchers: dict[str, subagents.AgentWatcher] = {}
        self.agent_errors: dict[str, str] = {}   # each session's watcher error last said
        self.usage = stats.AccountUsage()   # the account's, for every surface: fetched once
        # unsent text typed for each target, (session id, ref or None), kept in memory only
        self.unsent: dict[tuple, str] = {}

    # events

    def listen(self, listener: Callable[[Event], None]) -> None:
        self.listeners.append(listener)

    def emit(self, event: Event) -> None:
        for listener in self.listeners:
            listener(event)

    def say(self, text: str, severity: str = "information") -> None:
        self.emit(Event("notice", text, severity))

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

    def restore(self, sid: str) -> bool:
        """A dead session brought back where it left off, and unparked. Whether it was."""
        if self.statuses.get(sid) != "dead":
            self.say("only a dead session can be restored", "warning")
            return False
        if not self.open_session(sid, restore=True):
            return False
        self.store.set_parked(sid, False)
        return True

    def restorable(self) -> list[str]:
        """What Restore all brings back: every dead session that isn't parked."""
        return [s["id"] for s in self.sessions if self.statuses.get(s["id"]) == "dead" and not s["parked"]]

    def restore_all(self, sids: list[str]) -> None:
        n = sum(self.open_session(sid, restore=True) for sid in sids)
        self.say(f"restoring {n} session(s)")

    def new_session(self, cwd: str, name: str, ticket: str, brief: str, runner: str) -> None:
        sid = self.store.create_session(cwd, name=name, ticket=ticket, brief=brief, runner=runner)
        self.open_session(sid)

    def adoptable(self) -> list[adopt.Candidate]:
        """The Claude Code sessions on disk that aren't in the wheelhouse yet (adopt.candidates)."""
        return adopt.candidates(self.store)

    @staticmethod
    def running_pid(sid: str) -> int | None:
        """The pid of Claude Code running a session outside the wheelhouse, if it is running."""
        return liveness.running_pid(sid)

    def adopt(self, candidate: adopt.Candidate, name: str) -> bool:
        """A session started outside the wheelhouse, brought in (adopt.adopt). Whether it was."""
        try:
            adopt.adopt(self.store, candidate, name)
        except Exception as e:   # came back to life, wt.exe missing...
            self.say(str(e), "error")
            return False
        self.say(f"adopting {name or short(candidate.id)}")
        return True

    def answer_offer(self, take: bool) -> bool:
        """The first-run tutorial offer, answered either way: it never comes back. Whether the
        tutorial started."""
        if not take:
            self.store.set_setting(tutorial.OFFER_KEY, "dismissed")
            self.say("make tutorial runs the tutorial any time; ? lists every key")
            return False
        try:
            tutorial.start(self.store)
        except Exception as e:   # claude missing, say
            self.store.set_setting(tutorial.OFFER_KEY, "dismissed")
            self.say(f"couldn't start the tutorial: {e}", "error")
            return False
        self.say("tutorial started: follow the checklist on the right")
        return True

    # Park and End: a running session is asked; a dead one, or one forced, is acted on

    def ask(self, sid: str, what: str) -> None:
        self.store.request(sid, what)
        self.say(f"asked the session to {what}")

    def cancel_request(self, sid: str, what: str) -> None:
        self.store.cancel_request(sid, what)
        self.say(f"{what} request cancelled")

    def act_on_dead(self, sid: str, what: str) -> bool:
        """Park or End a dead session now. The confirm may have sat open while it came back:
        then it's left as it is, and said so. Whether it was acted on."""
        if liveness.status(self.row(sid), waking=self.waking) in RUNNING:
            self.say(f"the session is running again: press {what.capitalize()} to ask it instead", "warning")
            return False
        self.force(sid, what)
        return True

    def force(self, sid: str, what: str) -> None:
        if what == "end":
            self.store.end(sid)
        else:
            self.store.set_parked(sid, True)

    def unpark(self, sid: str) -> None:
        self.store.set_parked(sid, False)

    def rename(self, sid: str, name: str | None) -> bool:
        """Whether the name changed: not if cancelled (None) or the same."""
        if name is None or name == self.row(sid)["name"]:
            return False
        self.store.rename(sid, name)
        self.say(f"renamed to {name}" if name else "name cleared: it shows its directory")
        return True

    # answers and queues

    def asking(self, target):
        """The open permission item a target is, if it is one."""
        item = target and target[1] and self.store.item(*target)
        return item if item and item["kind"] == "permission" and item["status"] == "open" else None

    def submit(self, target: tuple, text: str) -> bool:
        """What the person submitted for a target: an open permission is denied with it at
        once, never queued (#53); otherwise it's queued or sent at once, by the session's mode.
        Answering an item is an explicit act: a decision is seen at once, not after the dwell.
        Whether it went: not if the permission was answered meanwhile."""
        s = self.row(target[0])
        if self.asking(target):
            try:
                self.answer_permission(*target, "deny", text)
                self.say(f"denied {target[1]}, with your message")
            except KeyError as e:   # answered meanwhile
                self.say(str(e.args[0]), "warning")
                return False
        elif mode(s) == "immediate":
            self.store.send(target[0], text, target[1])
            self.say(f"sent to {aimed(target)}")
        elif self.sends_now(s):   # its old monitor would deliver a draft at once anyway
            self.store.send(target[0], text, target[1])
            self.say(f"sent to {aimed(target)} now: that session runs older wheelhouse code, "
                     "so it can't queue until it's relaunched", "warning")
        else:
            self.store.queue(target[0], text, target[1])
            if self.dead(target[0]):   # aimed names the item, so say whose queue isn't running
                self.say(f"queued for {aimed(target)}, but the session isn't running: "
                         "Restore it, then Ctrl+S or Send sends its queue")
            else:
                self.say(f"queued for {aimed(target)}: Ctrl+S or Send sends the session's queue")
        if target[1]:
            self.store.mark_seen(*target)
        return True

    def answer_permission(self, sid: str, ref: str, decision: str, message: str = "") -> None:
        self.store.answer_permission(sid, ref, decision, message)

    def unqueue(self, msg_id: int) -> str | None:
        """A queued answer taken back: its text, or None if it went meanwhile."""
        return self.store.unqueue(msg_id)

    def sent_note(self, sid: str, n: int) -> str:
        s = self.store.session(sid)
        name = (s["name"] or short(sid)) if s else short(sid)
        late = "" if self.running(sid) else " (not running: delivered when it's restored)"
        return f"{n} to {name}{late}"

    def send_queue(self, sid: str) -> None:
        """A session's queue, sent as one message; a dead one's stays queued (#62)."""
        if self.dead(sid):
            self.say(f"{display_name(self.row(sid))} isn't running: Restore it first", "warning")
            return
        n = self.store.dispatch(sid)
        self.say(f"sent {self.sent_note(sid, n)}" if n else "nothing queued for that session")

    def send_all(self) -> None:
        """Every queue but a dead session's, which stays queued until it's restored (#62)."""
        queued = dict.fromkeys(m["session_id"] for m in self.store.drafts())
        sent = [(sid, n) for sid in queued if not self.dead(sid) and (n := self.store.dispatch(sid))]
        if sent:
            self.say("sent " + "; ".join(self.sent_note(sid, n) for sid, n in sent))
        elif any(self.dead(sid) for sid in queued):
            self.say("nothing queued for a running session: a dead one's queue waits until it's restored",
                     "warning")
        else:
            self.say("nothing queued")

    def toggle_mode(self, sid: str) -> None:
        s = self.row(sid)
        new = "immediate" if mode(s) == "queued" else "queued"
        self.store.set_mode(sid, new)
        name = s["name"] or short(sid)
        self.say(f"{name}: answers now send as you submit them" if new == "immediate" else
                 f"{name}: answers now queue until you send them (Ctrl+S or Send)")
        if new == "immediate" and (n := len(self.store.drafts(sid))):
            self.say(f"{n} answer(s) still queued for {name}: Ctrl+S or Send sends them")

    def host_command(self, sid: str, what: str) -> None:
        """Interrupt, Compact or Shell, for the session's host to carry out."""
        try:
            self.store.command(sid, what)
        except SessionGone:
            return
        self.say({"interrupt": "interrupting", "compact": "compacting: asking what to keep",
                  "shell": "opening a terminal tab"}[what])

    # items

    def mark_seen(self, sid: str, ref: str) -> bool:
        """Whether an unseen decision became seen: a no-op for anything else."""
        return self.store.mark_seen(sid, ref)

    def close(self, item, closed: bool) -> None:
        """Close a question or decision, or reopen it: a question as answered, a decision as
        seen. Dismiss a settled task or subagent, or bring it back. One finished already has
        nothing to close."""
        if closed and standing(item) == "finished":
            return
        if item["kind"] == "decision":
            self.store.close_decision(item["session_id"], item["ref"], closed)
        elif item["kind"] in DISMISSABLE:
            self.store.dismiss(item["session_id"], item["ref"], closed)
        else:
            self.store.update_item(item["session_id"], item["ref"], status="closed" if closed else "answered")

    # the transcripts

    def read_contexts(self) -> None:
        """The context size of each session with a bar, read on a worker thread: every one
        not parked, read once if it isn't running, and every one running, parked or not. A
        read the stats pane has under way counts."""
        listed = {s["id"]: s for s in self.sessions if not s["parked"] or self.running(s["id"])}
        for sid in [sid for sid in self.contexts if sid not in listed]:
            del self.contexts[sid]
        for sid in listed:
            follower = self.contexts.setdefault(sid, stats.follower(sid))
            if follower.reading or (follower.ready and not self.running(sid)):
                continue
            follower.reading = True
            self.spawn(functools.partial(self.read_context, follower), "contexts")

    def read_context(self, follower: stats.UsageFollower) -> None:
        """On a worker thread. A failed read leaves the last size up; the next tick reads
        again. One that brought anything is said at once ("landed"), for the stats pane too
        if it shows that session: the follower is its as well, and it started no read of its
        own while this one was under way."""
        first = not follower.ready
        if stats.read_safely(follower) or first:
            self.post(functools.partial(self.emit, Event("landed", sid=follower.sid)))

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
                self.say(f"{display_name(s)}: {watcher.error}", "warning")
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
        (at most once a minute, stats.AccountUsage)."""
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
