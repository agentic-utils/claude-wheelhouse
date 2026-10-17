"""Is a session alive? Decided by its Claude process, never by the heartbeat. And
stopping one (stop), guarded the same way.

Sleep and hibernate keep the process, so the session stays live. A reboot or a
WSL shutdown changes the boot id, so it is dead. The process start time guards
against pid reuse. The heartbeat only feeds the 'stalled' hint. Linux reads both from
/proc; macOS, which has none, from sysctl and ps.
"""

import json
import ctypes
import ctypes.util
import os
import shutil
import signal
import struct
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

PROC = Path("/proc")
libc = ctypes.CDLL(ctypes.util.find_library("c"))   # macOS's sysctl and libproc, where there's no /proc
SESSIONS = Path.home() / ".claude/sessions"   # Claude Code's record of each running session
STARTING_GRACE = 90      # seconds a launched session may take to register
STALLED_AFTER = 120      # heartbeat age that suggests a stalled session
CLOCK_JUMP = 30          # wall clock ahead of monotonic by this much means we slept
WAKE_GRACE = 60          # hold off the stalled hint this long after waking
KILL_AFTER = 10          # seconds a stopped Claude Code has to exit before its process group is killed
# the process-group calls stop makes, here so a test can stub them for liveness alone
getpgid, killpg = os.getpgid, os.killpg
# Starts a command detached, init's child rather than ours, so it is reaped when it exits rather
# than left a zombie that still looks alive. setsid --fork where there is one (util-linux), else
# the same in Python (macOS)
DETACH = ["setsid", "--fork"] if shutil.which("setsid") else [
    sys.executable, "-c", "import os, sys; os.setsid(); os.fork() and os._exit(0); os.execvp(sys.argv[1], sys.argv[1:])"]


def boot_id(proc: Path = PROC) -> str:
    if not proc.exists():   # macOS
        buf, size = ctypes.create_string_buffer(64), ctypes.c_size_t(64)
        libc.sysctlbyname(b"kern.bootsessionuuid", buf, ctypes.byref(size), None, 0)
        return buf.value.decode()
    return (proc / "sys/kernel/random/boot_id").read_text().strip()


def lstart(text: str) -> int:
    """Seconds since the epoch from ps's lstart in UTC and the C locale ("Sat Oct 17 03:53:50
    2026"), as Claude Code records a process's start (procStart) on macOS."""
    return int(datetime.strptime(" ".join(text.split()), "%a %b %d %H:%M:%S %Y").replace(tzinfo=timezone.utc).timestamp())


def start_time(pid: int, proc: Path = PROC) -> int | None:
    """Field 22 of /proc/<pid>/stat: start time in clock ticks since boot. None for a
    process that has gone, or exited and not been reaped yet (state Z): it keeps its stat.
    Without /proc (macOS), its start in seconds since the epoch, from libproc."""
    if not proc.exists():
        # libproc's struct proc_bsdinfo (PROC_PIDTBSDINFO, 3): pbi_status at 4, pbi_start_tvsec at 120
        info = ctypes.create_string_buffer(136)
        if libc.proc_pidinfo(pid, 3, 0, info, 136) != 136:
            return None
        status, = struct.unpack_from("I", info, 4)
        return None if status == 5 else struct.unpack_from("Q", info, 120)[0]   # 5: SZOMB
    try:
        stat = (proc / str(pid) / "stat").read_text()
    except OSError:
        return None
    # comm (field 2) may contain spaces and parens; split after the last ')'
    fields = stat.rsplit(")", 1)[1].split()
    return None if fields[0] in ("Z", "X") else int(fields[19])


def is_alive(pid, start, boot, proc: Path = PROC) -> bool:
    if not pid or start is None or boot != boot_id(proc):
        return False
    return start_time(pid, proc) == start


def stop(pid, start, boot, kill_after: float | None = KILL_AFTER, proc: Path = PROC) -> bool:
    """Stop a session's process, only if it is still the one registered (pid, start time,
    boot id): SIGTERM, Claude Code's own clean exit (about a second, idle or mid-tool, its
    transcript intact for --resume), then, if it's still alive kill_after seconds later
    (None: never), SIGKILL to its process group. Whether it was signalled: not if it had
    gone. Another OSError (not ours to signal) is raised."""
    if not is_alive(pid, start, boot, proc):
        return False
    group = None
    if kill_after is not None:
        try:
            group = getpgid(pid)
        except OSError:
            pass   # gone already: the signal below says so
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    if group is not None:
        timer = threading.Timer(kill_after, kill_group, (pid, start, boot, group, proc))
        timer.daemon = True
        timer.start()
    return True


def kill_group(pid, start, boot, group: int, proc: Path = PROC) -> None:
    """SIGKILL the process group of a stopped process that is still alive, as registered."""
    if is_alive(pid, start, boot, proc):
        try:
            killpg(group, signal.SIGKILL)
        except OSError:
            pass   # gone meanwhile


def registered_this_launch(session) -> bool:
    """A Claude Code registered since the session's last launch. claude_pid alone can be the
    last launch's: a launch doesn't clear it, so register_if_free still sees a live one. So
    the launch notes the registered process's start time (launched_over), and only another
    process's registration counts. Not the heartbeat: it has second resolution, and the last
    run's final beat can share the launch's second."""
    return bool(session["claude_pid"]) and session["claude_start"] != session["launched_over"]


def status(session, *, now: datetime | None = None, waking: bool = False, proc: Path = PROC) -> str:
    """One of: live, stalled, starting, dead. Always from the process: being parked is
    a separate flag and never hides whether a session is running."""
    now = now or datetime.now(timezone.utc)
    if is_alive(session["claude_pid"], session["claude_start"], session["boot_id"], proc):
        beat = session["heartbeat_at"]
        if not waking and beat and (now - datetime.fromisoformat(beat)).total_seconds() > STALLED_AFTER:
            return "stalled"
        return "live"
    launched = session["launched_at"]
    if launched and not registered_this_launch(session) \
            and (now - datetime.fromisoformat(launched)).total_seconds() < STARTING_GRACE:
        return "starting"
    return "dead"


def running_sessions(sessions: Path = SESSIONS, proc: Path = PROC) -> dict[str, int]:
    """Session id -> pid for every Claude process alive now, launched by the wheelhouse or not.
    Claude Code writes ~/.claude/sessions/<pid>.json per running session; records of dead
    sessions linger, so one counts only while /proc agrees on the process start time."""
    alive = {}
    for f in sessions.glob("*.json"):
        try:
            rec = json.loads(f.read_text())
            pid, start = int(rec["pid"]), rec["procStart"]
            start = lstart(start) if isinstance(start, str) and not start.isdigit() else int(start)
            sid = rec["sessionId"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if start_time(pid, proc) == start:
            alive[sid] = pid
    return alive


def running_pid(sid: str, sessions: Path = SESSIONS, proc: Path = PROC) -> int | None:
    return running_sessions(sessions, proc).get(sid)


class WakeDetector:
    """Notices the machine waking from sleep: wall time jumps, monotonic time does not."""

    def __init__(self, clock=time.time, mono=time.monotonic):
        self.clock, self.mono = clock, mono
        self.last = (clock(), mono())
        self.grace_until = 0.0

    def tick(self) -> bool:
        """Returns True while inside the post-wake grace period."""
        wall, mono = self.clock(), self.mono()
        if (wall - self.last[0]) - (mono - self.last[1]) > CLOCK_JUMP:
            self.grace_until = mono + WAKE_GRACE
        self.last = (wall, mono)
        return mono < self.grace_until
