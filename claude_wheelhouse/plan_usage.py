"""The account's plan usage, as Claude Code's /usage shows it: the session's and the week's
percentages and when each resets. The wheelhouse never reads Claude Code's login nor calls
its endpoints: it asks Claude Code itself (#16).

Every EVERY seconds the hub asks /usage of Claude Code (Probe), with no settings, hooks,
MCP servers or transcript: a local command, answered in a few seconds with no tokens
spent. By default ("oneshot") each ask is a `claude -p "/usage"` of its own, nothing left
running between; "persistent" keeps one `claude -p` reading stream-json on stdin and sends
/usage to it, faster per ask but resident and busier at idle. Its answer is prose (parse).
A session run through the Agent SDK says the same numbers as it goes, in its
rate_limit_event (from_rate_limit): its host keeps the latest in the store (SDK_SETTING)
and the hub takes it too. Whichever reading of a limit is newest is shown (AccountUsage)."""

import contextlib
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

EVERY = 300          # seconds between /usage calls
TIMEOUT = 60         # seconds a call may take: the first, starting Claude Code, takes about 8
BACKOFF = 10         # seconds before a Claude Code that died is started again, doubled per death
BACKOFF_MAX = 1800
SDK_SETTING = "plan_usage.sdk"   # settings: an SDK session's latest reading, {"at", "limits"}
CLAUDE = "claude"
CLAUDE_ENV = "WHEELHOUSE_CLAUDE"   # the claude to run instead: the tests' one that isn't there, which daemons they start inherit
FLAGS = ("--verbose", "--setting-sources=", "--strict-mcp-config", "--no-session-persistence")
MODES = ("oneshot", "persistent")   # features.usage_mode chooses: the first by default
ASK = json.dumps({"type": "user", "message": {"role": "user", "content": "/usage"}}) + "\n"
# the probe isn't one of the wheelhouse's sessions
UNSET = ("WHEELHOUSE_DB", "WHEELHOUSE_SESSION_ID", "WHEELHOUSE_TOKEN")

# /usage's lines, as Claude Code 2.1 words them:
#   Current session: 10% used · resets Oct 11, 7:20pm (Europe/London)
#   Current week (all models): 67% used · resets Oct 15, 1am (Europe/London)
#   Current week (Fable): 0% used · resets Oct 15, 1am (Europe/London)
LINE = re.compile(r"^\s*Current (session|week \(([^)]*)\)):\s*(\d+(?:\.\d+)?)% used(?:\s*·\s*resets (.+?))?\s*$", re.M)
RESET = re.compile(r"^(?:(?P<mon>[A-Z][a-z]{2}) (?P<day>\d{1,2}), )?(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s?(?P<ap>am|pm)"
                   r"(?: \((?P<tz>[^)]+)\))?$", re.I)
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
WINDOWS = {"five_hour": "session", "seven_day": "weekly_all"}   # an SDK event's, by kind

said: set[str] = set()


def say_once(what: str) -> None:
    """On stderr, the daemon's log, once per distinct thing: a poll every five minutes
    would otherwise say the same all day."""
    if what not in said:
        said.add(what)
        print(f"wheelhouse: plan usage: {what}", file=sys.stderr, flush=True)


def kind_of(line: str, week: str | None) -> str:
    if week is None:
        return "session"
    return "weekly_all" if week.lower() == "all models" else "weekly_" + re.sub(r"\W+", "_", week.lower()).strip("_")


def parse(text: str, now: float) -> dict[str, tuple[float, float | None]]:
    """/usage's answer as {kind: (percent, resets at)}: session, weekly_all and weekly_<model>
    (weekly_fable), a reset as UTC epoch seconds, None if its wording isn't known. ValueError
    if it has neither the session's line nor the week's: its wording has changed."""
    out = {}
    for m in LINE.finditer(text or ""):
        out[kind_of(m[1], m[2])] = (float(m[3]), reset_at(m[4], now) if m[4] else None)
    if not {"session", "weekly_all"} & out.keys():
        raise ValueError(f"no session or week line in /usage's answer: {(text or '')[:80]!r}")
    return out


def reset_at(words: str, now: float) -> float | None:
    """A reset as /usage says it, local wall-clock to the minute in the zone it names (the
    machine's if none), with no year: "Oct 15, 1am (Europe/London)" or "7:20pm". As UTC epoch
    seconds: a date in the year that puts it nearest now (Dec 31's "Jan 1" is next year's),
    a time alone the next one at or after now."""
    m = RESET.match(words.strip())
    if not m:
        return None
    try:
        zone = ZoneInfo(m["tz"]) if m["tz"] else None
    except (ValueError, KeyError, OSError):
        zone = None
    here = datetime.fromtimestamp(now, zone) if zone else datetime.fromtimestamp(now).astimezone()
    hour = int(m["h"]) % 12 + (12 if m["ap"].lower() == "pm" else 0)
    minute = int(m["m"] or 0)
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    if m["mon"] is None:
        at = here.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if at < here - timedelta(minutes=1):
            at += timedelta(days=1)   # wall-clock: the same time tomorrow, across a clock change too
        return at.timestamp()
    if m["mon"].lower() not in MONTHS:
        return None
    month = MONTHS.index(m["mon"].lower()) + 1
    candidates = []
    for year in (here.year - 1, here.year, here.year + 1):
        try:
            candidates.append(datetime(year, month, int(m["day"]), hour, minute, tzinfo=here.tzinfo))
        except ValueError:   # Feb 29 in a year without one
            pass
    return min(candidates, key=lambda c: abs(c.timestamp() - now)).timestamp() if candidates else None


def from_rate_limit(raw) -> dict[str, tuple[float, float | None]]:
    """An SDK rate_limit_event's info (RateLimitInfo.raw) as {kind: (percent, resets at)}:
    its unifiedWindows, five_hour as the session and seven_day as the week, utilization
    0..1, resetsAt UTC epoch seconds. Empty if it carries none.

    unifiedWindows is marked @internal in Claude Code 2.1.296's SDK schema, so it may
    change or go without notice, and its 0..1 utilization scale is taken from one observed
    stream, not from documentation: if the gauges read 100 times too low or high, check here."""
    out = {}
    windows = raw.get("unifiedWindows") if isinstance(raw, dict) else None
    for name, kind in WINDOWS.items():
        w = windows.get(name) if isinstance(windows, dict) else None
        if isinstance(w, dict) and isinstance(w.get("utilization"), (int, float)):
            at = w.get("resetsAt")
            out[kind] = (float(w["utilization"]) * 100, float(at) if isinstance(at, (int, float)) else None)
    return out


class AccountUsage:
    """The latest reading of each limit, {kind: (percent, resets at)}, and when each was
    read, {kind: read at}, all UTC epoch seconds: from /usage or an SDK session, the newest
    of each kind winning. A failed call leaves the last numbers up (age says how old); an
    answer that can't be read clears them, as its wording has changed. Both dicts are
    replaced whole, never changed in place, so another thread reads them as it reads any
    attribute."""

    def __init__(self):
        self.limits: dict[str, tuple] = {}
        self.read: dict[str, float] = {}
        self.sdk: set[str] = set()   # the kinds whose reading is an SDK session's
        self.failed = False
        self.lock = threading.Lock()

    def take(self, limits: dict, at: float, sdk: bool = False) -> None:
        with self.lock:
            merged, read, sdk_kinds = dict(self.limits), dict(self.read), set(self.sdk)
            for kind, (pct, resets) in limits.items():
                if at >= read.get(kind, 0.0):
                    merged[kind], read[kind] = (pct, resets), at
                    (sdk_kinds.add if sdk else sdk_kinds.discard)(kind)
            self.read, self.limits, self.sdk = read, merged, sdk_kinds
            self.failed = False

    def lost(self, unreadable: bool = False) -> None:
        """A failed /usage. One whose answer can't be read drops /usage's readings, not an
        SDK session's."""
        with self.lock:
            if unreadable:
                self.read = {k: v for k, v in self.read.items() if k in self.sdk}
                self.limits = {k: v for k, v in self.limits.items() if k in self.sdk}
            self.failed = True

    def age(self, now: float, kinds) -> float | None:
        """How old the oldest of these kinds' readings is, None if none is held."""
        held = [self.read.get(k, now) for k in kinds if k in self.limits]
        return now - min(held) if held else None

    def poll(self, probe: "Probe") -> None:
        """/usage asked of the probe and its answer taken: on a worker thread. Never raises.
        Says nothing once the probe has stopped: the hub is stopping, and a daemon that
        couldn't start must be left to say why in its own words."""
        try:
            text = probe.ask()
        except Unavailable as e:
            if probe.stopped:
                return
            say_once(f"/usage didn't answer: {e}")
            self.lost()
            return
        try:
            self.take(parse(text, time.time()), time.time())
        except ValueError as e:
            say_once(str(e))
            self.lost(unreadable=True)


class Unavailable(RuntimeError):
    """/usage couldn't be asked, or didn't answer."""


def command(mode: str) -> list[str]:
    """The Claude Code run for a mode: one /usage, or a session reading stream-json."""
    claude = os.environ.get(CLAUDE_ENV) or CLAUDE
    if mode == "persistent":
        return [claude, "-p", "--input-format", "stream-json", "--output-format", "stream-json", *FLAGS]
    return [claude, "-p", "/usage", "--output-format", "json", *FLAGS]


class Probe:
    """Claude Code, asked /usage: never through a shell, in its own process group (so a
    kill takes any children too), with cwd the state dir. In "oneshot" mode each ask runs
    a Claude Code that answers and exits; in "persistent" mode one is started when first
    asked and kept, reading its stdin, so it exits by itself when its owner dies. Either
    way an ask that fails (a Claude Code that can't start, dies, or says nothing usable)
    holds off the next run for BACKOFF seconds, doubling with each failure in a row, and
    one that doesn't answer within TIMEOUT is killed."""

    def __init__(self, cwd: Path, mode: str | None = None, command=None, timeout: float = TIMEOUT):
        self.cwd, self.timeout = cwd, timeout
        self.mode = mode or MODES[0]
        self.command = list(command) if command else None   # a test's fake, else command(mode)
        self.proc: subprocess.Popen | None = None
        self.answers: queue.Queue = queue.Queue()
        self.lock = threading.Lock()   # one ask at a time
        self.deaths = 0
        self.not_before = 0.0
        self.stopped = False

    def ask(self) -> str:
        """/usage's answer, the text of its result. Unavailable if it can't be had."""
        with self.lock:
            if self.stopped:
                raise Unavailable("stopped")
            answer = self.ask_persistent() if self.mode == "persistent" else self.ask_oneshot()
            if answer is None:
                self.failed()
                raise Unavailable("Claude Code exited without an answer")
            self.deaths = 0
            if answer.get("is_error") or answer.get("subtype") != "success":
                raise Unavailable(f"Claude Code said: {str(answer.get('result') or answer.get('subtype'))[:120]}")
            return answer.get("result") or ""

    def ask_oneshot(self) -> dict | None:
        """A Claude Code that runs /usage and exits: its result, None if it gave none."""
        if time.time() < self.not_before:
            raise Unavailable("Claude Code failed: run again after a pause")
        proc = self.spawn(stdin=subprocess.DEVNULL)
        try:
            out, _ = proc.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            self.died(proc)
            raise Unavailable(f"no answer in {self.timeout:.0f}s") from None
        finally:
            self.proc = None
        if self.stopped:
            raise Unavailable("stopped")
        try:
            said = json.loads(out)
        except ValueError:
            return None
        results = [m for m in (said if isinstance(said, list) else [said])   # a list with --verbose
                   if isinstance(m, dict) and m.get("type") == "result"]
        return results[-1] if results else None

    def ask_persistent(self) -> dict | None:
        """/usage sent to the Claude Code kept running, started first if it isn't: its
        result, None if it exited first."""
        proc = self.proc
        if proc is None or proc.poll() is not None:
            if proc is not None:
                self.died(proc)
            if time.time() < self.not_before:
                raise Unavailable("Claude Code exited: started again after a pause")
            proc = self.start()
        while not self.answers.empty():   # a late answer to an ask that timed out
            self.answers.get_nowait()
        try:
            proc.stdin.write(ASK)
            proc.stdin.flush()
            answer = self.answers.get(timeout=self.timeout)
        except (OSError, ValueError) as e:
            self.died(proc)
            raise Unavailable(f"Claude Code's stdin: {e}") from e
        except queue.Empty:
            self.died(proc)
            raise Unavailable(f"no answer in {self.timeout:.0f}s") from None
        if answer is None:
            kill(proc)
            self.proc = None
        return answer

    def spawn(self, stdin) -> subprocess.Popen:
        """Claude Code started, as self.proc, so stop can kill it."""
        env = {k: v for k, v in os.environ.items() if k not in UNSET}
        try:
            proc = subprocess.Popen(self.command or command(self.mode), stdin=stdin, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, cwd=self.cwd, env=env, text=True,
                                    close_fds=True, start_new_session=True)
        except OSError as e:
            self.failed()
            raise Unavailable(f"couldn't start Claude Code: {e}") from e
        self.proc = proc   # before the check: a stop from now on sees it and kills it
        if self.stopped:   # stopped while it started: not left behind
            kill(proc)
            self.proc = None
            raise Unavailable("stopped")
        return proc

    def start(self) -> subprocess.Popen:
        """The persistent Claude Code started, with a thread reading its answers."""
        proc = self.spawn(stdin=subprocess.PIPE)
        self.answers = queue.Queue()
        threading.Thread(target=self.read, args=(proc, self.answers), daemon=True, name="plan-usage").start()
        return proc

    @staticmethod
    def read(proc: subprocess.Popen, answers: queue.Queue) -> None:
        """Its stdout, on a thread of its own: each result, then None once it has closed."""
        try:
            for line in proc.stdout:
                if '"result"' not in line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("type") == "result":
                    answers.put(msg)
        except (OSError, ValueError):
            pass
        answers.put(None)

    def backoff(self) -> float:
        return min(BACKOFF_MAX, BACKOFF * 2 ** max(self.deaths - 1, 0))

    def failed(self) -> None:
        """Under the lock: no run again for a while."""
        self.deaths += 1
        self.not_before = time.time() + self.backoff()

    def died(self, proc: subprocess.Popen) -> None:
        """Under the lock: it's gone, or made to go, and not run again for a while."""
        kill(proc)
        self.proc = None
        self.failed()

    def stop(self) -> None:
        """Killed, its children too, and never run again: the hub is stopping. Not under
        the lock, which an ask can hold for TIMEOUT."""
        self.stopped = True
        proc = self.proc
        if proc is not None:
            kill(proc)


def kill(proc: subprocess.Popen) -> None:
    """Its process group ended: asked, then made (whether or not its leader went at the
    asking, as children may not have), then reaped. Its stdout is left to the thread
    reading it, which sees it close."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)   # the group, even once its leader has gone: its children
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(2)
    try:
        if proc.stdin is not None:
            proc.stdin.close()
    except (OSError, ValueError):   # its pipe already broken
        pass
