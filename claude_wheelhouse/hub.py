"""The hub: what the wheelhouse knows and does, whatever shows it (.plan/remote.md, phase 1).
The TUI owns one, in process, and reads and acts through it; phase 2 puts it in a daemon,
behind HTTP. It never imports Textual: what a surface should know of what it does by
itself, it says as events (Event) to whoever listens.

It owns the sessions' liveness, and Relaunch, from the stop to the start again. Like the
app before it, it never acts on a session by itself: a host that dies stays dead until
the person relaunches it. And it reads the sessions' transcripts: each conversation, each
context size and each session's subagents, and asks the account's plan usage of a Claude
Code it keeps in the background (plan_usage), the reading and asking on the owner's worker
threads (spawn), never on the thread that calls it.

Its commands are the person's acts on sessions and items: each checks what it needs,
writes the store and returns what it did (Done), with what to say to the person, for the
surface that gave it alone. One on a session that has gone raises SessionGone (row), which
a surface reports; the dialogs that ask first, and their wording, are the surface's."""

import functools
import getpass
import json
import os
import platform
import signal
import socket
import stat
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Callable

from . import adopt, api, away, compactor, features, fingerprint, fork, launch, liveness, models, plan_usage, prs, steps, subagents, transcript, transcript_usage, tutorial
from .docroots import DOC_CAP
from .host import (COMPACT_TIMEOUT, PARK_COMPACT_WITHIN, KEEPALIVE_AFTER, KEEPALIVE_TOKENS, MCP_CONNECTOR, MCP_DOS, keepalive_body,
                   waits_on_person)
from .store import (COMPACT_QUESTION, NOTES_WAIT, ORIGIN_WORDS, Refused, wheelhouse_item, Compacting, DISMISSABLE, compacting_now, PRS_SINCE, STATUSES, SessionGone, requested_expired, Store, can_queue,
                    PROTOCOL_VERSION, mcp_servers, mode, needs_relaunch, runner, standing)

RUNNING = ("live", "stalled", "starting")
# a hosted session's activity when nothing is under way: errored and stopped count as resting
RESTING = ("idle", "interrupted", "stopped", "in a shell tab", "error")
PENDING = {"end": "forgetting", "park": "parking"}
# The four states a session is in (.plan/session-states.md): Active (running), Available
# (not running, not parked), Parked (not running, by intent) and Forgotten (no row, so never
# listed: Adopt offers it). Each verb on each state, (state, verb): the caption its button
# shows and whether the person confirms first ("busy": only while it's mid-turn). Park and
# Forget on an Active session ask it; Forget is "end" underneath, the store's word
VERBS = {
    ("active", "park"): ("Park", False),
    ("active", "end"): ("Stop & Forget", False),
    ("active", "relaunch"): ("Relaunch", "busy"),
    ("available", "park"): ("Park", False),
    ("available", "end"): ("Forget", True),
    ("available", "relaunch"): ("Relaunch", False),
    ("parked", "end"): ("Forget", True),
    ("parked", "relaunch"): ("Unpark", False),
}
# set once the hub has unparked the sessions parked under the old rules but still running
UNPARKED_RUNNING = "migrated.unparked-running"
SDK_USAGE_EVERY = 10   # seconds between looks for an SDK session's plan usage reading
RELAUNCH_WAIT = 30   # seconds a host has to stop for Relaunch before it gives up and says so
AGENTS_FOLLOWED = 2   # subagent conversations followed at once: the inbox's and a full screen's
DOCS_KEPT = 4   # docs whose last read the hub keeps (Hub.document), the newest asked for
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


def compaction_entries(stages, active: bool, at: float | None = None) -> list:
    """A session's compaction stages as conversation entries (#244): each stage's lines (art B,
    then dim who asked or what came of it, with its time), and while a cycle is preparing or
    compacting the crew's excerpts (compactor.schedule), those due by now (at, epoch). Placed
    by time among the transcript's entries (transcript.blocks); the task item's thread has the
    stages' notes, never the excerpts."""
    at = time.time() if at is None else at
    out, cycles = [], {}
    for st in stages:
        if (text := compactor.stage_text(st["state"], ORIGIN_WORDS.get(st["origin"]), st["note"])) is not None:
            art, *rest = text.split("\n")
            out.append(transcript.Entry("stage", "\n".join([art, " · ".join([*rest, st["at"][11:16] + "Z"])]), st["at"]))
        cycles.setdefault(st["cycle_id"], []).append(st)
    last = stages[-1]["cycle_id"] if stages else None
    for cid, run in cycles.items():
        windows = []
        for i, st in enumerate(run):
            if st["state"] not in compactor.SHOWN:
                continue
            end = (epoch(run[i + 1]["at"]) if i + 1 < len(run) else at if active and cid == last else epoch(st["at"]))
            windows.append((epoch(st["at"]), min(end, at)))
        for when, excerpt in compactor.schedule(cid, windows):
            if when <= at:
                stamp_ = datetime.fromtimestamp(when, timezone.utc).isoformat(timespec="microseconds")
                out.append(transcript.Entry("crew", "\n".join(excerpt), stamp_))
    return out


def epoch(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def refusable(method):
    """A hub action the store may refuse during a compaction cycle (Compacting, #244): said as a
    warning, the one wording for every refused action, and nothing done."""
    @functools.wraps(method)
    def act(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Refused as e:
            return Done(False).say(str(e), "warning")
    return act


def display_name(s) -> str:
    return s["name"] or os.path.basename(s["cwd"]) or short(s["id"])


def state(status: str, parked) -> str:
    """A listed session's state (VERBS) from its liveness and its parked flag: running is
    Active whatever the flag says."""
    if status in RUNNING:
        return "active"
    return "parked" if parked else "available"


def waking(s) -> str:
    """What bringing a session that isn't running back is called: a parked one is unparked,
    any other relaunched (VERBS)."""
    return "unparking" if s["parked"] else "relaunching"


def wake_verb(s) -> str:
    """The button a session that isn't running comes back by: Unpark or Relaunch (VERBS)."""
    return "Unpark" if s["parked"] else "Relaunch"


def requested(s) -> str | None:
    """The request (end or park) a session's row says it has been asked to act on, if any."""
    return next((what for what in PENDING if s[f"{what}_requested_at"]), None)


def working(s) -> bool:
    """Whether a session's row says a turn or a tool is under way, whatever its liveness."""
    return bool(s["running"]) or (runner(s) == "sdk" and not (s["activity"] or "idle").startswith(RESTING))


def aimed(target) -> str:
    """What a message goes to: an item's ref, or the session itself (a general message)."""
    return target[1] or "the session"


def still_queued(s, target, drafts) -> str:
    """After Alt+S (#124): what's still queued for the session, and how much of it the answer
    just sent has overtaken, being for the same item (or, a general message, general too)."""
    n, mine = len(drafts), sum(m["item_ref"] == target[1] for m in drafts)
    out = f"{n} answer{'' if n == 1 else 's'} still queued for {display_name(s)}"
    if mine:
        out += f" ({mine} {f'for {target[1]}' if target[1] else 'general'}, which this one has overtaken)"
    return out + f": Ctrl+S or Send sends {'it' if n == 1 else 'them'}"


def host_context(s) -> api.HostContext | None:
    """The context size an SDK session's host last recorded, for the stats to show after a
    compaction. A tab session's sizes come from its transcript alone."""
    at = transcript_usage.epoch(s["context_at"] or "")
    if runner(s) != "sdk" or at is None or not s["context_tokens"] or not s["context_max"]:
        return None
    return api.HostContext(s["context_tokens"], s["context_max"], at)


def since(at: str | None, asked: float) -> bool:
    """Whether a stored timestamp is at or after the epoch second `asked`."""
    epoch = transcript_usage.epoch(at or "")
    return epoch is not None and epoch >= asked


def compacted_since(s, asked: float) -> bool:
    """Whether the session's host has recorded a context under COMPACT_FIRST_TOKENS since
    the epoch second `asked`: a compaction's end, which no later activity line hides."""
    at = transcript_usage.epoch(s["context_at"] or "")
    return at is not None and at >= asked and (s["context_tokens"] or 0) < COMPACT_FIRST_TOKENS


def machine() -> str:
    """This machine's name, and its WSL distro or OS release where it says."""
    try:
        release = platform.freedesktop_os_release().get("PRETTY_NAME")
    except OSError:
        release = None
    where = os.environ.get("WSL_DISTRO_NAME") or release
    return f"{socket.gethostname()} ({where})" if where else socket.gethostname()


def branch(cwd: str) -> str | None:
    """The branch checked out in cwd, "detached at <sha>" off one, None out of a git repository."""
    name = fork.git(cwd, "symbolic-ref", "--quiet", "--short", "HEAD")
    if name:
        return name
    sha = fork.git(cwd, "rev-parse", "--short", "HEAD")
    return f"detached at {sha}" if sha else None


@dataclass(frozen=True)
class Event:
    """What a surface should know. "notice": text to show the person, with its severity
    (information, warning or error): to every listener, of what the hub did by itself (a
    relaunch finishing, a subagent sync failing), or in a command's Done, to its surface
    alone. A notice's sid is the session whose Fork it ends, made or failed, so the
    surface that pressed it knows (#107), and made, the fork's own when it was made, so that
    surface can select it (#116). "landed": a read of a session's transcript (sid)
    brought something, so what shows its context size can repaint."""
    kind: str
    text: str = ""
    severity: str = "information"
    sid: str | None = None
    made: str | None = None


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


@dataclass
class Warmth:
    """A tab session's keepalive, as the hub keeps it (keep_warm), all epoch seconds: since
    when it has been quiet (its last turn not a keepalive's), the latest response seen, and
    when the keepalive running now was sent (None once its turn has ended)."""
    quiet: float
    seen: float
    sent: float | None = None


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
        # own thread (a no-op once it's closing, which post says by returning False): both at
        # once by default, as in a test
        self.spawn: Callable[[Callable[[], None], str], None] = spawn or (lambda work, group: work())
        self.post: Callable[[Callable[[], None]], bool | None] = post or (lambda callback: callback())
        # forks whose worktree and copy are made, waiting for forked on the hub's thread: new
        # sid to (tree, branch), so an owner closing first takes them away (drop_pending_forks)
        self.pending_forks: dict[str, tuple] = {}
        self.forks_lock = threading.Lock()
        self.wake = liveness.WakeDetector()
        self.waking = False
        self.sessions = []
        self.seen = None   # each session's (name, parked, alive) at the last refresh (note_quiet)
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
        # shared with the stats pane (transcript_usage.follower), so each transcript is read once
        self.contexts: dict[str, transcript_usage.UsageFollower] = {}
        # the sessions not running whose transcript has been read, and copied back, since
        # they stopped: read_contexts reads each once more after it stops, for its last second
        self.settled: set[str] = set()
        # each session's subagents, tracked as A items (#69): read on workers, as contexts are
        self.agent_watchers: dict[str, subagents.AgentWatcher] = {}
        self.agent_errors: dict[str, str] = {}   # each session's watcher error last said
        # each session's PRs, found in the same transcripts on the same workers (prs.py, #111),
        # and when any session last pushed or merged one (the PRs pane's cue to fetch)
        self.pr_watchers: dict[str, prs.PrWatcher] = {}
        self.pr_activity_at: float | None = None
        # the subagents whose own conversation a surface shows (agent_conversation), by their
        # session's id and item's ref: only the last AGENTS_FOLLOWED asked for are followed,
        # of the hundreds a session can have (the inbox's and a full screen's), and none once
        # none is shown. Oldest asked first
        self.agent_followers: dict[tuple[str, str], transcript.AgentFollower] = {}
        # the docs shown (document), by (session, ref): their last read, as (path, stamp, text)
        self.docs: dict[tuple[str, str], tuple[str, str, str]] = {}
        # the account's plan usage, for every surface: asked of one Claude Code, every
        # plan_usage.EVERY seconds, and taken from SDK sessions' readings in the store
        self.usage = plan_usage.AccountUsage()
        self.usage_probe = plan_usage.Probe(self.store.path.parent, features.usage_mode())
        # when /usage is next asked: None until the first tick, which only sets it, so the
        # first ask is a tick later, once a daemon is serving (its lifespan ticks before it
        # binds: one that can't bind never runs Claude Code)
        self.usage_due: float | None = None
        self.sdk_usage_due = 0.0   # when the SDK sessions' reading is next looked at
        self.sdk_usage = None      # the last it took, as stored
        # the tab sessions' keepalive (keep_warm), by session: as the host keeps its own
        self.warmth: dict[str, Warmth] = {}
        self.keepalive_for = features.keepalive_hours() * 3600   # seconds; 0: no keepalive
        self.unpark_running()

    def unpark_running(self) -> None:
        """Once per database, at the first hub on this code: a session parked under the old
        rules, which left it running in its tab, is unparked, so it shows as Active rather
        than being stopped by surprise. Each one is logged."""
        if self.store.setting(UNPARKED_RUNNING):
            return
        for s in self.store.sessions():
            if s["parked"] and liveness.status(s) in RUNNING:
                self.store.set_parked(s["id"], False)
                print(f"wheelhouse: {display_name(s)} ({s['id']}) was parked but running: unparked, so it shows "
                      "as Active", file=sys.stderr, flush=True)
        self.store.set_setting(UNPARKED_RUNNING, "1")
    # events

    def listen(self, listener: Callable[[Event], None]) -> None:
        self.listeners.append(listener)

    def emit(self, event: Event) -> None:
        for listener in self.listeners:
            listener(event)

    def announce(self, text: str, severity: str = "information", sid: str | None = None,
                 made: str | None = None) -> None:
        """To every listener: only for what the hub does by itself. A command's notices go
        in its Done, to the surface that gave it. sid: the session whose Fork it ends, made:
        the fork it made (Event)."""
        self.emit(Event("notice", text, severity, sid, made))

    # sessions and their liveness

    def refresh(self) -> None:
        """The sessions read again, and each one's liveness: only liveness, as the
        wheelhouse never deletes or parks anything by itself."""
        self.waking = self.wake.tick()
        self.sessions = self.store.sessions()
        self.by_id = {s["id"]: s for s in self.sessions}
        self.statuses = {s["id"]: liveness.status(s, waking=self.waking) for s in self.sessions}
        self.sweep_compactions()
        self.note_quiet()

    def sweep_compactions(self) -> None:
        """A compaction cycle whose session isn't running (its host died hard, or stopped before
        its finally could end it), or whose host has stalled (no heartbeat: wedged, so its own
        clocks and an Interrupt go unheard), ends as failed, so nothing stays closed and blue and
        Relaunch, the cure for a stalled host, is offered again (#244). A host that recovers finds
        its cycle ended, and lets it go (cycle_due)."""
        for s in self.sessions:
            if compacting_now(s) and self.statuses[s["id"]] in ("dead", "stalled") and s["compaction_id"]:
                why = "its host stalled" if self.statuses[s["id"]] == "stalled" else "its host stopped"
                if self.store.move_compaction(s["id"], s["compaction_id"], "failed", why):
                    print(f"wheelhouse: {display_name(s)}'s compaction failed: its host "
                          f"{'stalled' if self.statuses[s['id']] == 'stalled' else 'is not running'}",
                          file=sys.stderr, flush=True)
            elif (s["compaction"] == "compacting" and (runner(s) != "sdk" or s["shell"]) and s["compaction_at"]
                  and time.time() - epoch(s["compaction_at"]) > COMPACT_TIMEOUT):   # a tab's, its end never heard
                if self.store.move_compaction(s["id"], s["compaction_id"], "failed",
                                              f"no word of its end in {COMPACT_TIMEOUT // 60} minutes"):
                    print(f"wheelhouse: {display_name(s)}'s compaction in its tab failed: no word of its end",
                          file=sys.stderr, flush=True)
            elif (within := requested_expired(s, PARK_COMPACT_WITHIN)) is not None:   # read first: no lock
                if self.store.expire_requested(s["id"], s["compaction_id"], within):
                    print(f"wheelhouse: {display_name(s)}'s compaction failed: no host took it up within "
                          f"{within:g} s", file=sys.stderr, flush=True)

    def note_quiet(self) -> None:
        """A session parked, ended or stopped since the last refresh (or, at a hub's first,
        since the last hub's), kept for the card of what happened while the person was away
        (away.observe): watched here, as nothing in the store dates them, whoever's code did it."""
        seen = {s["id"]: (display_name(s), bool(s["parked"]), self.statuses[s["id"]] in ("live", "stalled"))
                for s in self.sessions}
        # a hub's first read compares with what the last hub saw (SEEN): what went quiet while
        # none ran is noted at this start, so the card after a night away still lists it
        before = self.seen if self.seen is not None else away.seen(self.store.setting(away.SEEN))
        self.seen = seen
        if before == seen:
            return
        self.store.set_setting(away.SEEN, json.dumps(seen))
        if before is None:
            return
        quiet = away.kept(self.store.setting(away.QUIET))
        if (changed := away.observe(before, seen, quiet, away.now())) is not None:
            self.store.set_setting(away.QUIET, json.dumps(changed))

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

    def state(self, sid: str) -> str:
        """The session's state (VERBS): active, available or parked."""
        return state(self.statuses.get(sid, "dead"), self.listed(sid)["parked"])

    # launching and Relaunch

    def open_session(self, sid: str, restore: bool = False, may_tab: bool = True, elsewhere: str | None = None) -> Done:
        """Launch a session the way it runs, a host or a tab; a restore the way new sessions
        run now (launch.restore_runner). may_tab false: asked for by another machine, so a
        tab is refused (launch.NotHere) and said as a warning, its own words or elsewhere."""
        try:
            if restore:
                launch.restore_session(self.store, sid, sdk=self.sdk, may_tab=may_tab)
            else:
                launch.open_session(self.store, sid, may_tab)
        except launch.NotHere as e:
            return Done(False).say(elsewhere or str(e), "warning")
        except Exception as e:   # already running, wt.exe missing...
            return Done(False).say(str(e), "error")
        return Done()

    def relaunch(self, sid: str, may_tab: bool = True, elsewhere: str | None = None) -> Done:
        """An Available or Parked session brought back where it left off, and unparked (Relaunch,
        Unpark). Whether it was."""
        done = self.open_session(sid, restore=True, may_tab=may_tab, elsewhere=elsewhere)
        if done.ok:
            self.store.set_parked(sid, False)
            done.say("relaunching")
        return done

    @refusable
    def wake_and_send(self, sid: str, item_ref: str | None = None, text: str | None = None,
                      may_tab: bool = True) -> Done:
        """Ctrl+S or Alt+S on a session that isn't running, confirmed (#221): Unpark or Relaunch
        it, and send its queue, with text (Alt+S's) after it, as its first prompt on waking.
        They go behind the resume notice its launch leaves (launch.tell_joined), so the session
        reads the wheelhouse's context first and the person's words after, all in the one
        batch its monitor or host claims at start. The launch returns before the session has
        started (a detached host, a tab's Claude), and the queue follows the notice within the
        same call, long before either can claim anything. Nothing is sent if it doesn't come back:
        the queue stays queued, and the text stays the person's."""
        self.store.check_sendable(sid, item_ref if text is not None else None)   # before waking it
        s = self.row(sid)
        verb = waking(s)
        done = self.relaunch(sid, may_tab)
        if not done.ok:
            return done
        n = self.store.dispatch_last(sid, text, item_ref)
        return Done().say(f"{verb} {display_name(s)}" + (f": {n} sent with it" if n else ""))

    def host_gone(self, sid: str, s, may_tab: bool) -> Done:
        """Relaunch of a host that has gone meanwhile: it comes back as a dead session does
        (relaunch), and for another machine (may_tab false) not in a tab."""
        return self.relaunch(sid, may_tab,
                             f"{display_name(s)}'s host has stopped: Relaunch it from the daemon's machine")

    @refusable
    def stop_host(self, sid: str, host: tuple, may_tab: bool = True) -> Done:
        """Refused during a compaction cycle (#244). Stop a running session for Relaunch: a host as SIGTERM does (its own clean stop:
        Claude Code is disconnected, and an open permission or question closes as withdrawn,
        or as lost when the new host starts); a tab's Claude Code the same way, its process
        group killed if it hasn't gone within liveness.KILL_AFTER (stop_process). The confirm
        may have sat open meanwhile, so only the process it was asked about, (pid, start
        time), is stopped. finish_relaunches starts it again once the process has gone: a
        tab in a new tab. A host's session out in a shell tab goes back to its waiting host
        as that tab stops, which is its relaunch. Whether anything changed."""
        self.store.check_compacting(sid, "relaunch")
        s = self.row(sid)
        if not liveness.is_alive(s["claude_pid"], s["claude_start"], s["boot_id"]):
            return self.host_gone(sid, s, may_tab)   # gone meanwhile: nothing to stop
        if (s["claude_pid"], s["claude_start"]) != host:
            return Done(False).say(f"{display_name(s)} started again meanwhile: Relaunch again to stop "
                                   f"{'this host' if runner(s) == 'sdk' and not s['shell'] else 'it'}", "warning")
        try:
            self.stop_process(s)
        except OSError as e:
            return Done(False).say(f"couldn't stop {display_name(s)}: {e}", "error")
        if runner(s) == "sdk" and s["shell"]:   # its host takes it back as the tab stops
            return Done().say(f"relaunching {display_name(s)}: its shell tab stops, and its host takes it back")
        self.relaunch_under_way(sid, host, may_tab=may_tab)
        return Done().say(f"relaunching {display_name(s)}: stopping its "
                          f"{'host' if runner(s) == 'sdk' else 'Claude Code'}")

    @staticmethod
    def stop_process(s) -> bool:
        """Stop a session's registered process, if it is still that one (liveness.stop): a
        host by SIGTERM alone, its own clean stop; Claude Code in a tab by SIGTERM, then its
        process group killed if it is still alive liveness.KILL_AFTER seconds later. Whether
        it was signalled. An OSError other than its having gone is raised."""
        hosted = runner(s) == "sdk" and not s["shell"]
        return liveness.stop(s["claude_pid"], s["claude_start"], s["boot_id"],
                             kill_after=None if hosted else liveness.KILL_AFTER)

    def compact_first(self, sid: str) -> bool:
        """Whether Relaunch should offer to compact first: a running host whose context, as
        it last recorded it, is past COMPACT_FIRST_TOKENS, whose restart would write all of
        that to the prompt cache again. Never outside SDK mode, where there's no compacting."""
        s = self.listed(sid)
        return (self.sdk and runner(s) == "sdk" and not s["shell"] and self.running(sid)
                and (s["context_tokens"] or 0) > COMPACT_FIRST_TOKENS)

    def warm(self, sid: str) -> bool:
        """Whether the session's prompt cache is warm: its last response within the cache's
        lifetime (transcript_usage.Snapshot.warm), as the stats pane shows it."""
        follower = self.contexts.get(sid)
        return bool(follower and follower.ready and follower.snap.warm(time.time()))

    def cache_expires(self, sid: str) -> float | None:
        """When the session's prompt cache goes (or went) cold: transcript_usage.Snapshot.expires, the
        "warm until" or "cold since" the stats pane shows. None with no cache written yet."""
        follower = self.contexts.get(sid)
        return follower.snap.expires if follower and follower.ready else None

    def context_tokens(self, sid: str) -> int:
        """The session's context as the stats pane sizes it: what a cold cache re-reads."""
        follower = self.contexts.get(sid)
        return transcript_usage.current(follower.snap, host_context(self.listed(sid))).context if follower and follower.ready else 0

    def notes_question(self, target):
        """The item in context, if it is the host's open question whether to wait for a
        compaction's notes (NOTES_WAIT, #244)."""
        item = target and target[1] and self.store.item(*target)
        return item if item and item["status"] == "open" and item["answer"] == NOTES_WAIT else None

    def compact_question(self, target):
        """The item in context, if it is the host's open question suggesting Compact."""
        item = target and target[1] and self.store.item(*target)
        return item if item and item["status"] == "open" and item["answer"] == COMPACT_QUESTION else None

    @refusable
    def notes_wait(self, sid: str, ref: str, abort: bool) -> Done:
        """The host's question when a compaction's notes turn runs long (#244): Keep waiting
        closes it, and the host asks again after another NOTES_TIMEOUT; Abort interrupts the
        notes turn, which cancels the cycle (nothing compacted, queued messages stay queued)."""
        item = self.store.item(sid, ref)
        if item is None or item["answer"] != NOTES_WAIT or item["status"] != "open":
            return Done(False).say("that question has closed: the compaction has moved on", "warning")
        if abort:
            self.store.command(sid, "interrupt")
        self.store.update_item(sid, ref, status="closed",
                               note="Abort: the notes turn is interrupted, and the compaction cancelled" if abort
                               else "Keep waiting: asked again if the notes are still being written")
        return Done().say("aborting the compaction" if abort else "waiting for the notes")

    @refusable
    def compact_then_relaunch(self, sid: str, host: tuple, may_tab: bool = True) -> Done:
        """Relaunch, compacting first: the host is asked to Compact (unless a Compact is
        under way already), and once the session is compacted and idle, finish_relaunches
        stops it (stop_host), and starts it again once gone. Only the host the confirm was
        about, (pid, start time), as stop_host. Refused during a compaction cycle (#244)."""
        s = self.row(sid)
        self.store.check_compacting(sid, "relaunch")
        if runner(s) != "sdk" or s["shell"]:   # nothing to compact it: relaunched at once
            return self.stop_host(sid, host, may_tab)
        if not liveness.is_alive(s["claude_pid"], s["claude_start"], s["boot_id"]):
            return self.host_gone(sid, s, may_tab)   # gone meanwhile: nothing to compact
        if (s["claude_pid"], s["claude_start"]) != host:
            return Done(False).say(f"{display_name(s)} started again meanwhile: Relaunch again to stop this host",
                                   "warning")
        if needs_relaunch(s):   # its host would never end the cycle (#244): relaunched without compacting
            done = self.stop_host(sid, host, may_tab)
            return done.say(f"{display_name(s)} ran older wheelhouse code, so it wasn't compacted first", "warning") \
                if done.ok else done
        self.store.command(sid, "compact", origin="relaunch")
        self.relaunch_under_way(sid, host, asked=time.time(), may_tab=may_tab)
        return Done().say(f"relaunching {display_name(s)}: compacting first (it's asked what to keep)")

    def relaunch_under_way(self, sid: str, host: tuple | None, asked: float | None = None,
                           may_tab: bool = True) -> None:
        """A Relaunch that has stopped a session's host, or is compacting before it does
        (Compact asked for at the epoch second `asked`), kept here and in the store; or, with
        no host, done with. may_tab false: another machine asked, and it carries on so
        (store.relaunch_may_tab)."""
        if host:
            wait = COMPACT_RELAUNCH_WAIT if asked is not None else RELAUNCH_WAIT
            self.relaunching[sid] = (time.monotonic() + wait, host)
            self.store.set_relaunch(sid, host, time.time() + wait, asked, may_tab)
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
            self.relaunch_under_way(sid, host, may_tab=self.store.relaunch_may_tab(sid))
            return
        elif since(s["interrupted_at"], self.compacting[sid]):   # by any wheelhouse, older ones too
            why = "Compact was interrupted"
        elif compacted_since(s, self.compacting[sid]) and activity.startswith(IDLE_LINES):
            for what in ("compact", "interrupt"):   # either, left untaken, would greet the new host
                self.store.take_command(sid, what)   # (a Shell stays: the person's, for the new host)
            row = self.store.compaction(sid)
            if row is not None and row["state"] == "requested":   # its Compact, untaken: the cycle goes with it
                self.store.move_compaction(sid, row["id"], "cancelled", "the relaunch went ahead before it began")
            done = self.stop_host(sid, host, self.store.relaunch_may_tab(sid))
            for notice in done.notices:
                self.emit(notice)
            if not done.ok:
                self.relaunch_under_way(sid, None)
            return
        elif s["compaction"] == "failed":
            why = "its compaction failed"
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
        found past its deadline (lapsed) is let go, its host not started: Relaunch is the
        person's. The hub does this by itself, so what happened goes to every listener."""
        for sid, (deadline, host) in list(self.relaunching.items()):
            s = next((x for x in self.sessions if x["id"] == sid), None)
            if s is None:   # ended meanwhile
                self.relaunch_under_way(sid, None)
            elif sid in self.compacting:
                self.compacting_relaunch(sid, s, host, deadline)
            elif sid in self.lapsed and not liveness.is_alive(s["claude_pid"], s["claude_start"], s["boot_id"]):
                self.relaunch_under_way(sid, None)
                self.announce(f"{display_name(s)} wasn't relaunched: Relaunch it", "warning")
            elif not liveness.is_alive(s["claude_pid"], s["claude_start"], s["boot_id"]):
                # as it ran: a host again, not WHEELHOUSE_RUNNER's way
                done = self.open_session(sid, may_tab=self.store.relaunch_may_tab(sid))
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

    def relaunchable(self) -> list[str]:
        """What Relaunch all brings back: every Available session (not running, not parked)."""
        return [s["id"] for s in self.sessions if self.state(s["id"]) == "available"]

    def relaunch_all(self, sids: list[str], may_tab: bool = True) -> Done:
        """Relaunch each of the sessions the person was shown that is Available still: one that
        has started or been parked meanwhile is left as it is. For another machine (may_tab
        false) not in a tab."""
        done, n = Done(), 0
        for sid in sids:
            try:
                if self.state(sid) != "available":
                    continue
            except SessionGone:
                continue
            one = self.relaunch(sid, may_tab)
            done.notices.extend(e for e in one.notices if e.text != "relaunching")
            n += one.ok
        return done.say(f"relaunching {n} session(s)")

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
        """The Claude Code sessions on disk that have no row: forgotten, or never known (adopt.candidates)."""
        return adopt.candidates(self.store)

    @staticmethod
    def running_pid(sid: str) -> int | None:
        """The pid of Claude Code running a session outside the wheelhouse, if it is running."""
        return liveness.running_pid(sid)

    def adopt(self, candidate: adopt.Candidate, name: str, may_tab: bool = True) -> Done:
        """A session started outside the wheelhouse, brought in (adopt.adopt). Whether it was.
        may_tab false: another machine asked, so not in a tab."""
        try:
            adopt.adopt(self.store, candidate, name, sdk=self.sdk, may_tab=may_tab)
        except launch.NotHere as e:
            return Done(False).say(f"{e.name} opens in a tab: adopt it from the daemon's machine", "warning")
        except Exception as e:   # came back to life, wt.exe missing...
            return Done(False).say(str(e), "error")
        return Done().say(f"adopting {name or short(candidate.id)}")

    def fork_plan(self, sid: str) -> fork.Plan:
        """What Fork offers for a session (fork.Plan): its items that may move, and where its
        directory is a linked git worktree, the new branch's name."""
        s = self.row(sid)
        t = fork.tree(s["cwd"])
        items = [(r["ref"], r["kind"], r["status"], r["title"]) for r in self.store.items(sid, include_closed=False)
                 if r["status"] in fork.MOVABLE.get(r["kind"], ()) and not wheelhouse_item(r["answer"])]
        return fork.Plan(f"{display_name(s)} (fork)", s["cwd"], t, fork.default_branch(t, s["cwd"]) if t.can_branch else "",
                         items)

    def fork(self, sid: str, refs: list[str], branch: str | None = None, may_tab: bool = True) -> Done:
        """A new session carrying on from this one's conversation (fork.py), named
        "<name> (fork)", run as this one is (a tab out of SDK mode), with its model and effort,
        in the same directory or, given branch, a new worktree on that new branch. The items
        refs move to it, and this session is told which. What can be checked at once is; the
        worktree and the transcript's copy, which can take a while, are made on a worker
        thread (forking), and what happened then is announced."""
        s = self.row(sid)
        name = f"{display_name(s)} (fork)"
        how = runner(s) if runner(s) == "tab" or self.sdk else "tab"
        if how == "tab" and not may_tab:   # before anything is made
            return Done(False).say(f"{name} would open in a tab: fork it from the daemon's machine", "warning")
        refs = list(dict.fromkeys(refs))
        bad = [r for r in refs if (i := self.store.item(sid, r)) is None
               or i["status"] not in fork.MOVABLE.get(i["kind"], ()) or wheelhouse_item(i["answer"])]
        if bad:
            return Done(False).say(f"{', '.join(bad)} can't move: only a todo or blocked task, or an open "
                                   "question", "error")
        try:
            t = fork.tree(s["cwd"])
            if branch and not t.linked:
                raise ValueError(f"{s['cwd']} isn't in a linked git worktree")
            if branch and not t.can_branch:
                raise ValueError("git can't say where this repository's main checkout is, to put a new worktree "
                                 "under: fork into this worktree")
            if branch and fork.branch_taken(t, branch):
                raise ValueError(f"there's a branch {branch} already: name another")
            if branch and fork.worktree_path(t, branch).exists():
                raise ValueError(f"{fork.worktree_path(t, branch)} is there already: name another branch")
        except ValueError as e:
            return Done(False).say(str(e), "error")
        self.spawn(functools.partial(self.forking, sid, s, name, how, refs, branch, t, may_tab), "fork")
        return Done().say(f"forking {display_name(s)} as {name}")

    def forking(self, sid: str, s, name: str, how: str, refs: list[str], branch: str | None, t, may_tab: bool) -> None:
        """Fork's slow part, on a worker thread: the worktree, then the transcript's copy,
        each taken away again if what follows fails. The rest is the hub's (forked)."""
        cwd = s["cwd"]
        try:
            if branch:
                cwd = fork.new_worktree(s["cwd"], t, branch)
            try:
                new = fork.copy(sid, s["cwd"], name, cwd)
            except BaseException:
                if branch:
                    fork.drop_worktree(t, branch)
                raise
        except FileNotFoundError:
            self.post(functools.partial(self.announce, f"{display_name(s)} has no conversation to fork yet", "error", sid=sid))
            return
        except Exception as e:   # git refused the branch, the transcript unreadable...
            self.post(functools.partial(self.announce, f"couldn't fork {display_name(s)}: {e}", "error", sid=sid))
            return
        made = (f"transcript {new}.jsonl" + (f", worktree {fork.worktree_path(t, branch)} on branch {branch}"
                                             if branch else ""))
        # kept in launch.log until forked says otherwise: a wheelhouse stopping before then
        # leaves these behind, to clear up by hand
        fork.log(self.store, f"fork of {sid} made {made}: if no line for {new} follows, they're left over")
        with self.forks_lock:
            self.pending_forks[new] = (t, branch)
        if self.post(functools.partial(self.forked, sid, s, name, how, refs, branch, t, cwd, new)) is False:
            self.drop_pending_forks()   # the owner is closing: never to run

    def forked(self, sid: str, s, name: str, how: str, refs: list[str], branch: str | None, t, cwd: str,
               new: str) -> None:
        """Fork's last part, on the hub's thread: the row, the items moved and what both
        sessions are told, all undone (row, transcript, worktree and branch) if any of it
        fails; then the launch, which failing leaves a whole fork to Relaunch."""
        with self.forks_lock:
            if self.pending_forks.pop(new, None) is None:   # taken away already: the owner closing
                return
        titles = {r: i["title"] for r in refs if (i := self.store.item(sid, r)) is not None}

        def tell(moved):
            return [*([(sid, fork.told_parent(name, moved, titles))] if moved else []),
                    (new, fork.opening(display_name(s), name, moved, titles, branch or "", cwd, bool(branch) and t.dirty))]
        try:
            self.store.create_session(cwd, name=name, ticket=s["ticket"], brief=s["brief"], sid=new, runner=how,
                                      model=s["model"], effort=s["effort"])
            # the moves and both notices are one transaction: failing, it leaves the fork owning nothing
            moved, left = self.store.move_items(sid, new, refs, name, tell)
        except Exception as e:   # the parent forgotten meanwhile, a model no longer offered...
            if self.store.session(new) is not None and not self.store.items(new):
                self.store.end(new)
            fork.undo(new, t, branch)
            fork.log(self.store, f"fork {new} failed ({e}): taken away again")
            self.announce(f"couldn't fork {display_name(s)}: {e}", "error", sid=sid)
            return
        fork.log(self.store, f"fork {new} made, {name}, in {cwd}")
        try:
            (launch.open_host if how == "sdk" else launch.open_tab)(self.store, new, notice=False)
        except Exception as e:   # wt.exe missing...: the fork stays, to Relaunch
            self.announce(f"forked {name}, but it didn't start: {e}. Relaunch it", "error", sid=sid)
            return
        self.announce(f"forked {display_name(s)} as {name}"
                      + (f", {len(moved)} item(s) moved to it" if moved else "")
                      + (f"; {', '.join(left)} changed meanwhile, so stayed" if left else ""),
                      "warning" if left else "information", sid=sid, made=new)

    def drop_pending_forks(self) -> None:
        """The owner closing (a daemon stopping, the app quitting): each fork made on a worker
        but not yet taken in by forked, which now never runs, taken away again and logged."""
        with self.forks_lock:
            pending, self.pending_forks = self.pending_forks, {}
        for new, (t, branch) in pending.items():
            fork.undo(new, t, branch)
            fork.log(self.store, f"fork {new} dropped, the wheelhouse closing: taken away again")

    def answer_offer(self, take: bool | None) -> Done:
        """The tour's invite, answered: taken (True) or Never (False), it never comes back;
        Later (None) has every start offer it again, sessions or not, until one of those; a
        start that fails records nothing, so it is offered again. Whether the tour starts."""
        if take is None:
            self.store.set_setting(tutorial.OFFER_KEY, tutorial.RESET)
            return Done(False).say("Later: the next start offers the tour again")
        if not take:
            self.store.tour_never()
            return Done(False).say(f"{tutorial.AGAIN}; ? lists every key")
        try:
            tutorial.start(self.store)   # its directory made ready: the person starts the demo from New session
        except Exception as e:   # claude missing, say: nothing recorded, so offered again
            return Done(False).say(f"couldn't start the tutorial: {e}", "error")
        return Done()

    # Park and Forget ("end"): a running session is asked; one not running, or one forced, is acted on

    @refusable
    def ask(self, sid: str, what: str, compact: bool = False) -> Done:
        """A running session asked to Park or Forget; a Park, to compact once parked (compact)."""
        s = self.row(sid)
        if compact and what == "park" and (runner(s) != "sdk" or s["shell"]):   # compaction isn't offered there
            return Done(False).say(f"{display_name(s)} runs in a terminal tab, where compaction isn't offered: "
                                   "Park it without Compact first", "warning")
        # a host on older code would never end the cycle: its park compacts as it always has
        self.store.request(sid, what, compact and what == "park", cycle=not needs_relaunch(s))
        return Done().say(f"asked the session to {what}" + (", then compact" if compact and what == "park" else ""))

    def cancel_request(self, sid: str, what: str) -> Done:
        self.store.cancel_request(sid, what)
        return Done().say(f"{what} request cancelled")

    def act_on_dead(self, sid: str, what: str) -> Done:
        """Park or Forget a session that isn't running, now. The confirm may have sat open while
        it came back: then it's left as it is, and said so. Whether it was acted on."""
        if liveness.status(self.row(sid), waking=self.waking) in RUNNING:
            verb = "Stop & Forget" if what == "end" else "Park"
            return Done(False).say(f"the session is running again: press {verb} to ask it instead", "warning")
        return self.force(sid, what)

    @refusable
    def force(self, sid: str, what: str) -> Done:
        """Park or Forget now, without the session's own steps: a running one is stopped
        first (stop_process), then parked or its row deleted."""
        s = self.row(sid)
        if what != "end":   # Stop & Forget's "Just stop it" goes, and the cycle with the session
            self.store.check_compacting(sid, "park")
        try:
            self.stop_process(s)
        except OSError as e:
            return Done(False).say(f"couldn't stop {display_name(s)}: {e}", "error")
        if what == "end":
            self.store.end(sid)
        else:
            self.store.set_parked(sid, True)
        return Done()

    @refusable
    def unpark(self, sid: str) -> Done:
        self.store.check_compacting(sid, "unpark")
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

    @refusable
    def submit(self, target: tuple, text: str, now: bool = False) -> Done:
        """What the person submitted for a target: an open permission is denied with it at
        once, never queued (#53); otherwise it's queued or sent at once, by the session's mode.
        `now` (Alt+S, #124) sends this one answer at once, as Immediate mode would, leaving
        the session's mode and the rest of its queue as they are.
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
        elif now or mode(s) == "immediate" or (compacting_now(s) and self.store.waits_on(*target)):
            # during a cycle a call the notes turn waits on is answered at once: queueing is refused (#244)
            self.store.send(target[0], text, target[1])
            done.say(f"sent to {aimed(target)}{self.late(target[0])}")
            if mode(s) == "queued" and (drafts := self.store.drafts(target[0])):
                done.say(still_queued(s, target, drafts))
        # on the row as it is now: its old monitor would deliver a draft at once anyway
        elif self.sends_at_once(s):
            self.store.send(target[0], text, target[1])
            done.say(f"sent to {aimed(target)} now: that session runs older wheelhouse code, "
                     "so it can't queue until it's relaunched", "warning")
        else:
            self.store.queue(target[0], text, target[1])
            if self.dead(target[0]):   # aimed names the item, so say whose queue isn't running
                done.say(f"queued for {aimed(target)}, but the session isn't running: Ctrl+S "
                         f"{'unparks' if s['parked'] else 'relaunches'} it and sends its queue")
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

    def late(self, sid: str) -> str:
        """What a send to a session that isn't running adds: when it will be delivered."""
        if self.running(sid):
            return ""
        s = self.store.session(sid)
        return f" (not running: delivered when it's {'unparked' if s and s['parked'] else 'relaunched'})"

    def sent_note(self, sid: str, n: int) -> str:
        s = self.store.session(sid)
        name = (s["name"] or short(sid)) if s else short(sid)
        return f"{n} to {name}{self.late(sid)}"

    @refusable
    def send_queue(self, sid: str) -> Done:
        """A session's queue, sent as one message; a dead one's stays queued (#62)."""
        if self.dead(sid) and not self.store.drafts(sid):
            return Done(False).say("nothing queued for that session")
        if self.dead(sid):
            s = self.row(sid)
            return Done(False).say(f"{display_name(s)} isn't running: {wake_verb(s)} it first", "warning")
        n = self.store.dispatch(sid)
        return Done(bool(n)).say(f"sent {self.sent_note(sid, n)}" if n else "nothing queued for that session")

    def send_all(self) -> Done:
        """Every queue but a dead or parked session's, which stays queued until it's relaunched
        or unparked (#62)."""
        queued = dict.fromkeys(m["session_id"] for m in self.store.drafts())
        sent, refused = [], []
        for sid in queued:
            if self.dead(sid):
                continue
            try:
                if n := self.store.dispatch(sid):
                    sent.append((sid, n))
            except Compacting as e:   # its queue waits for the cycle's end (#244)
                refused.append(str(e))
        if sent:
            done = Done().say("sent " + "; ".join(self.sent_note(sid, n) for sid, n in sent))
            for why in refused:
                done.say(why, "warning")
            return done
        if refused:
            return Done(False).say("; ".join(refused), "warning")
        if any(self.dead(sid) for sid in queued):
            return Done(False).say("nothing queued for a running session: a dead or parked one's queue waits "
                                   "until it's relaunched or unparked", "warning")
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

    @refusable
    def host_command(self, sid: str, what: str, notes: str | None = None, then: str | None = None) -> Done:
        """Interrupt, Compact or Shell, for the session's host to carry out. A Compact may
        carry instructions and what follows it (Store.command). Interrupt also cancels a
        Relaunch compacting first, whichever wheelhouse began it (the store's). Compact is
        refused for a session whose host isn't running: left in the store, the next host
        would carry it out, on a cold cache."""
        try:
            s = self.listed(sid)
        except SessionGone:
            return Done(False)
        if what == "compact" and (runner(s) != "sdk" or s["shell"] or not self.running(sid)):
            return Done(False).say(f"{display_name(s)} isn't running in the wheelhouse: nothing to {what}", "warning")
        if what == "compact" and needs_relaunch(s):   # its host would never end the cycle (#244)
            return Done(False).say(f"{display_name(s)} runs older wheelhouse code: Relaunch it, then Compact", "warning")
        try:
            self.store.command(sid, what, notes, then)
        except SessionGone:
            return Done(False)
        done = Done().say({"interrupt": "interrupting",
                           "compact": "compacting with your instructions" if notes else "compacting: asking what to keep",
                           "shell": "opening a terminal tab"}[what])
        stored = self.store.relaunches().get(sid)
        if what == "interrupt" and stored is not None and stored[2] is not None:
            self.relaunch_under_way(sid, None)
            done.say("its Relaunch, waiting on Compact, is cancelled: it stays as it is", "warning")
        return done

    # model and effort (.plan/model-effort.md)

    def hosted_now(self, s) -> bool:
        """Run by its host right now: not a tab's, not handed to one, not stopped."""
        return runner(s) == "sdk" and not s["shell"] and self.running(s["id"])

    def mcp(self, sid: str, do: str, server: str = "", url: str | None = None) -> Done:
        """The MCP servers dialog's press (host.MCP_DOS), for the session's host to carry out
        on its next poll (.plan/mcp-on-demand.md). Reconnecting a claude.ai connector, whose
        sign-in is account-wide, reconnects it in every other hosted session that has it."""
        if do not in MCP_DOS:
            raise ValueError(f"do must be one of {', '.join(MCP_DOS)}, not {do!r}")
        try:
            s = self.listed(sid)
        except SessionGone:
            return Done(False)
        if not self.hosted_now(s):
            return Done(False).say(f"{display_name(s)} isn't running in the wheelhouse: nothing to ask", "warning")
        self.store.ask_mcp(sid, do, server, **({"url": url} if url else {}))
        if do == "check":
            return Done()
        done = Done().say({"reconnect": f"reconnecting {server}", "signin": f"starting {server}'s sign-in",
                           "callback": f"finishing {server}'s sign-in"}[do])
        if do == "reconnect" and ((mcp_servers(s) or {}).get(server) or {}).get("kind") == MCP_CONNECTOR:
            others = [o["id"] for o in self.store.sessions()
                      if o["id"] != sid and self.hosted_now(o) and server in (mcp_servers(o) or {})]
            for o in others:
                self.store.ask_mcp(o, "reconnect", server)
            if others:
                done.say(f"and in {len(others)} other session(s): a claude.ai sign-in is the account's")
        return done

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
            return {"model": ran[0] or want[0], "effort": ran[1] or want[1], "pending": None, "effort_live": False}
        # effort_live: its effort can be changed live, so the slider shows. Not out of SDK mode,
        # where a host's session runs on as it was set (features.py)
        return {"model": want[0] or (got[0] if hosted else None), "effort": want[1] or (got[1] if hosted else None),
                "pending": pending, "effort_live": hosted and self.sdk}

    def session_info(self, sid: str) -> dict:
        """What the stats pane's session info shows (#242), all JSON: who and where the session
        runs (the hub's user and machine, which launch it), its directory and branch, model,
        effort and mode, when it started, its transcript and Claude Code's pid, and its
        wheelhouse MCP server: the pid and code it started on (mark_mcp), stale when that
        isn't this hub's (a pull since, with no relaunch), None when it never said (older code).
        And the protocol version it stamped against this store's. SessionGone if it has gone."""
        s = self.row(sid)
        path = transcript.locate(sid)
        code = s["mcp_code"]
        return {"id": sid, "name": display_name(s), "user": getpass.getuser(), "machine": machine(),
                "cwd": s["cwd"], "branch": branch(s["cwd"]), **self.model_state(s),
                "mode": "SDK host" if runner(s) == "sdk" else "terminal tab", "shell": bool(s["shell"]),
                "started": s["launched_at"] or s["created_at"], "transcript": str(path) if path else None,
                "running": self.running(sid), "claude_pid": s["claude_pid"],
                "mcp_pid": s["mcp_pid"], "mcp_code": code, "mcp_started": s["mcp_started_at"],
                "code": fingerprint.code(), "mcp_stale": code is not None and code != fingerprint.code(),
                "protocol": s["code_version"], "protocol_now": PROTOCOL_VERSION, "relaunch": needs_relaunch(s)}

    # items

    def away(self) -> dict:
        """What the card of what happened while the person was away is read from (away.py):
        the app's last input, and each session's latest going quiet."""
        return {"last_input": self.store.setting(away.LAST_INPUT), "quiet": away.kept(self.store.setting(away.QUIET))}

    def note_input(self, at: str) -> None:
        """The app's last key press, click or scroll (away.LAST_INPUT), UTC."""
        self.store.set_setting(away.LAST_INPUT, at)

    def mark_seen(self, sid: str, ref: str) -> bool:
        """Whether an unseen decision became seen: a no-op for anything else."""
        return self.store.mark_seen(sid, ref)

    def close(self, sid: str, ref: str, closed: bool, status: str | None = None) -> Done:
        """Close a question or decision, or reopen it: a question as answered, a decision as
        seen, or as `status` (Ctrl+Z putting back the status it had). Dismiss a settled task or
        subagent, or bring it back. One finished already has nothing to close."""
        item = self.store.item(sid, ref)
        if item is None:
            self.row(sid)   # SessionGone if that's why
            raise KeyError(f"no item {ref} in this session")
        if closed and standing(item) == "finished":
            return Done(False)
        if status is not None and (item["kind"] not in ("question", "decision") or status not in STATUSES[item["kind"]]):
            raise ValueError(f"{ref} can't be reopened as {status}")
        if not closed:   # Ctrl+R: one deleted with its parent comes back too
            self.store.undelete(sid, ref)
        if item["kind"] == "decision":
            self.store.close_decision(sid, ref, closed, status)
        elif item["kind"] in DISMISSABLE:
            self.store.dismiss(sid, ref, closed)
        else:
            self.store.update_item(sid, ref, status="closed" if closed else status or "answered")
        return Done()

    def set_pinned(self, sid: str, ref: str, pinned: bool) -> Done:
        """The person pins an item or clears its pin (Store.set_pinned)."""
        self.store.set_pinned(sid, ref, pinned)
        return Done()

    def all_items(self) -> list:
        """Every item, parked sessions' too, for the person's order (ordering.py)."""
        return self.store.all_items()

    def reorder_items(self, group: str, keys: list[tuple[str, str]]) -> Done:
        """The person's order for a group of items (a drag): each placed in it."""
        self.store.set_item_order(group, keys)
        return Done()

    def reorder_sessions(self, group: str, sids: list[str]) -> Done:
        """The person's order for a group of sessions (a drag), read again at once."""
        self.store.set_session_order(group, sids)
        self.refresh()
        return Done()

    def delete_family(self, sid: str, ref: str) -> Done:
        """Delete on an item with children, confirmed: its children deleted (Store.delete_children),
        so they stay off the inbox when they change, a running one orphaned instead, then the item
        closed as Delete closes it."""
        self.store.delete_children(sid, ref)
        return self.close(sid, ref, True)

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
            follower = self.contexts.setdefault(sid, transcript_usage.follower(sid))
            alive = self.running(sid)
            if alive:
                self.settled.discard(sid)
            if follower.reading or (follower.ready and sid in self.settled):
                continue
            if not alive:   # read once more after it stops: what it wrote in its last second
                self.settled.add(sid)
            follower.reading = True
            self.spawn(functools.partial(self.read_context, follower), "contexts")

    def read_context(self, follower: transcript_usage.UsageFollower) -> None:
        """On a worker thread. A failed read leaves the last size up; the next tick reads
        again. One that brought anything is said at once ("landed"), for the stats pane too
        if it shows that session: the follower is its as well, and it started no read of its
        own while this one was under way."""
        first = not follower.ready
        changed = transcript_usage.read_safely(follower)
        if follower.ready and not follower.error:
            try:
                self.copy_back(follower)
            except sqlite3.OperationalError:   # a locked database: the next tick reads, and copies, again
                self.settled.discard(follower.sid)
        if changed or first:
            self.post(functools.partial(self.emit, Event("landed", sid=follower.sid)))

    def copy_back(self, follower: transcript_usage.UsageFollower) -> None:
        """A /model or /effort typed in a tab, carried to the session's columns, so its next
        Relaunch or return from a shell tab launches with it (Q56), and the stats
        pane shows it: by transcript_usage.carry's rule, last write wins. A tab session's on every read;
        a hosted session's while it is out in a shell tab (its host carries once more as it
        takes it back), not while hosted, as its host applies the columns."""
        s = self.store.session(follower.sid)
        if s is not None and (runner(s) == "tab" or s["shell"]):
            transcript_usage.carry(self.store, follower.sid, follower.snap)

    def steps_table(self) -> dict:
        """The session steps' switches, for the dialogs (steps.table)."""
        return steps.table(self.store, self.sdk)

    def set_step(self, event: str, switch: str, on: bool | None) -> Done:
        """A step's switch as chosen in Session steps; None, its default."""
        steps.set_switch(self.store, event, switch, on)
        return Done()

    def choose_steps(self, ticks: dict[str, bool]) -> Done:
        """The first-press dialog's ticks (steps.choose)."""
        steps.choose(self.store, ticks)
        return Done()

    def set_pr_states(self, states: dict[str, str]) -> int:
        """GitHub's states from the PRs pane's fetch (Store.set_pr_states), and the PR merged
        step for each item that has just turned merged. Returns the items changed."""
        before = {(i["session_id"], i["ref"]): i["status"] for i in self.store.pr_items()}
        changed = self.store.set_pr_states(states)
        if changed:
            for i in self.store.pr_items():
                if i["status"] == "merged" and before.get((i["session_id"], i["ref"]), "merged") != "merged":
                    self.pr_merged(i["session_id"], i["ref"], i["url"])
        return changed

    def pr_merged(self, sid: str, ref: str, url: str) -> None:
        """A session's PR has merged: told at once while it runs (a notice, never held behind
        its queued answers), else held for its On resume message, never a cold start for a
        notice (steps.hold)."""
        s = self.store.session(sid)
        if s is None:
            return
        pr = steps.pr_label(url, ref)
        if self.running(sid) and not s["parked"]:
            if text := steps.message(self.store, "pr_merged", mark=False, pr=pr, ref=ref):
                self.store.notice(sid, text)
        else:
            steps.hold(self.store, sid, pr, ref)

    def keep_warm(self, at: float | None = None) -> None:
        """Each tab session's keepalive, as an SDK host sends its own (host.keepalive_due),
        sent as a notice, timed from its transcript (the stats snapshot): KEEPALIVE_AFTER
        after its last response, then every KEEPALIVE_AFTER while it stays quiet, until it
        has been quiet the keepalive limit (features.keepalive_hours; 0, never). Only to a
        live session on current code (needs_relaunch: an older MCP server never records the
        session's word, so its items would all look in doubt), not parked, mid-turn (its
        last response stopped for a tool, or anything sent to it since: Store.heard_at,
        which the transcript doesn't show yet) or just compacted, over KEEPALIVE_TOKENS, and
        waiting on the person or with an item in doubt; the check (host.keepalive_body) when
        any is. A keepalive's own turn restarts
        the 55 minutes, not the quiet time."""
        at = time.time() if at is None else at
        for s in self.sessions:
            sid = s["id"]
            follower = self.contexts.get(sid)
            snap = follower.snap if follower is not None and follower.ready else None
            if snap is None or snap.last_at is None or runner(s) != "tab" or compacting_now(s):   # never mid-cycle (#244)
                self.warmth.pop(sid, None)
                continue
            w = self.warmth.setdefault(sid, Warmth(snap.last_at, snap.last_at))
            if snap.last_at != w.seen:   # a response since: the keepalive's own, or a real turn's
                w.seen = snap.last_at
                if w.sent is None or snap.last_at < w.sent:
                    w.quiet = snap.last_at
                elif snap.stop != "tool_use":
                    w.sent = None   # the keepalive's turn has ended
            if (self.keepalive_for <= 0 or s["parked"] or needs_relaunch(s)
                    or self.statuses.get(sid) not in ("live", "stalled")
                    or snap.stop == "tool_use" or snap.context <= KEEPALIVE_TOKENS
                    or (snap.compactions and snap.compactions[-1].at >= snap.last_at)
                    or at - max(snap.last_at, w.sent or 0) < KEEPALIVE_AFTER or at - w.quiet >= self.keepalive_for):
                continue
            try:
                if (transcript_usage.epoch(self.store.heard_at(sid) or "") or 0) > snap.last_at:
                    continue   # sent something it hasn't answered yet: its turn may be starting
                doubted = self.store.in_doubt(sid)
                if doubted or waits_on_person(self.store, sid):
                    self.store.notice(sid, keepalive_body(doubted))
                    w.sent = at
            except (SessionGone, sqlite3.OperationalError):   # ended meanwhile, or a locked database: next tick
                continue

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
            self.pr_watchers.pop(sid, None)
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
            self.sync_prs(watcher)
        except Exception as e:
            watcher.error = f"couldn't track subagents: {e}"[:120]
        finally:
            watcher.syncing = False

    def sync_prs(self, watcher: subagents.AgentWatcher) -> None:
        """The session's PRs brought in as items (prs.PrWatcher), from the transcripts the
        subagent watcher has found: the main one's, and each subagent's, a PR made there filed
        under the item the subagent was made for. On the same worker, after the subagents'."""
        if watcher.main is None or watcher.cutoff is None:
            return
        found = self.pr_watchers.setdefault(watcher.sid, prs.PrWatcher(watcher.sid))
        links = self.store.agent_links(watcher.sid)
        logs = {}
        for aid, agent in watcher.agents.items():
            if agent.log is not None:
                item = self.store.item(watcher.sid, links[aid][0]) if aid in links else None
                logs[agent.log] = item["parent_ref"] if item else None
        since = max(watcher.cutoff, subagents.when(self.store.setting(PRS_SINCE)) or watcher.cutoff)
        found.sync(self.store, watcher.main, logs, since)
        if found.activity and found.activity > (self.pr_activity_at or 0):
            self.pr_activity_at = found.activity

    def fetch_usage(self) -> None:
        """The account's plan usage: /usage asked on a worker thread when it's due, every
        plan_usage.EVERY seconds, whether or not any surface shows it (a daemon serves it to
        clients that come and go); and an SDK session's newer reading, which its host keeps
        in the store, looked for every SDK_USAGE_EVERY seconds."""
        now = time.time()
        if self.usage_due is None:
            self.usage_due = now
        elif now >= self.usage_due:
            self.usage_due = now + plan_usage.EVERY   # claimed here, so one ask is in flight at a time
            self.spawn(functools.partial(self.usage.poll, self.usage_probe), "usage")
        if now >= self.sdk_usage_due:
            self.sdk_usage_due = now + SDK_USAGE_EVERY
            self.take_sdk_usage()

    def take_sdk_usage(self) -> None:
        """An SDK session's reading (plan_usage.SDK_SETTING), once each time it changes: only
        in SDK mode (features.py, EXPERIMENTAL: the author's alone until Anthropic's terms are
        confirmed)."""
        if not self.sdk:
            return
        stored = self.store.setting(plan_usage.SDK_SETTING)
        if stored is None or stored == self.sdk_usage:
            return
        self.sdk_usage = stored
        try:
            reading = json.loads(stored)
            self.usage.take({k: tuple(v) for k, v in reading["limits"].items()}, float(reading["at"]), sdk=True)
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            plan_usage.say_once(f"an SDK session's reading couldn't be read: {e}")

    def stop_usage(self) -> None:
        """The Claude Code that answers /usage killed: the hub is stopping."""
        self.usage_probe.stop()

    def sent(self, sid: str, msg_id: int) -> str | None:
        """The full text of a message the person sent, for one a notification cut short."""
        m = self.store.message(sid, msg_id)
        return m["body"] if m else None

    def conversation(self, sid: str) -> list[tuple[str, str]] | None:
        """A session's conversation as (who, markdown) blocks, from its transcript, with
        what's sent and queued for it as a general message; None once the session has gone."""
        s = self.store.session(sid)
        if s is None:
            return None
        follower = self.followers.setdefault(sid, transcript.Follower(sid))
        recs = follower.read()
        queued = tuple(m["body"] for m in self.store.drafts(sid) if m["item_ref"] is None)
        # sent, not yet passed on: the wheelhouse's own notices aren't the person's words
        sending = tuple(m["body"] for m in self.store.pending(sid) if m["item_ref"] is None and m["kind"] != "notice")
        tab = None if runner(s) == "sdk" and not s["shell"] else launch.tab_title(s)
        stages = compaction_entries(self.store.stages(sid), compacting_now(s))
        key = (s["name"] or short(sid), tab, follower.seen, queued, sending, tuple(stages))
        if follower.blocks_key != key:   # parsed once per change, not on every refresh tick
            follower.blocks_key, follower.blocks = key, transcript.blocks(*key[:2], recs, queued, functools.partial(self.sent, sid),
                                                                          sending, tuple(stages))
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

    def document(self, sid: str, ref: str) -> dict | None:
        """A doc item's file, read from the path the session posted (never one a client
        sends), at most DOC_CAP bytes, and only a regular file: {path, text, stamp, gone}. Read
        again only when its mtime or size has changed (stamp), so asked each refresh it follows
        the file. gone says why it can't be read ("gone", "not a file"), with the last text
        read, if any; None while it can. None if the item isn't a doc,
        or has gone."""
        item = self.store.item(sid, ref)
        if item is None or item["kind"] != "doc":
            return None
        path, key = item["body"], (sid, ref)
        kept = self.docs.pop(key, None)
        if kept and kept[0] != path:
            kept = None
        why = None
        try:
            st = os.stat(path)
            stamp = f"{st.st_mtime_ns}:{st.st_size}"
            if not stat.S_ISREG(st.st_mode):
                why = "not a file"
            elif kept is None or kept[1] != stamp:
                # opened without blocking and checked again as opened: a FIFO or device swapped in
                # since the stat never holds the read (agent_lock, in the daemon)
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as f:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        why = "not a file"
                    else:
                        data = f.read(DOC_CAP + 1)
                        text = data[:DOC_CAP].decode("utf-8", errors="replace")
                        if len(data) > DOC_CAP:
                            text += f"\n\n_(only the first {DOC_CAP >> 20} MB is shown)_"
                        kept = (path, stamp, text)
        except OSError:
            why = "gone"
        if why:
            kept = (path, why, kept[2] if kept else "")
        self.docs[key] = kept
        while len(self.docs) > DOCS_KEPT:
            del self.docs[next(iter(self.docs))]
        return {"path": path, "text": kept[2], "stamp": kept[1], "gone": why}

    def unfollow_agent(self) -> None:
        """No surface shows a subagent's conversation now: no transcript of one is read."""
        self.agent_followers.clear()
