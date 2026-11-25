"""#385: the real app (claude-wheelhouse tui) with a sampling profiler of its UI thread.

Run by ptybench.py --prof <out.json> in place of `python -m claude_wheelhouse`. A second thread
looks at the UI thread's stack every 4 ms and counts, per bench phase (ptybench writes the
phase's name to PROF_PHASE), where it is: asleep in the event loop's select (idle), or in which
functions (cumulative: anywhere in the stack; self: the innermost frame). It also reads the UI
thread's own CPU clock, so a phase shows how much of its busy wall time the thread held a CPU
for: the rest it spent runnable but waiting (the core taken by something else, or the GIL by one
of the app's own threads). Other threads' running frames are counted too.

The counts are written to PROF_OUT every 5 s and at each phase change, as the app is ended by a signal."""
import json
import os
import sys
import threading
import time
from collections import Counter, defaultdict

OUT, PHASE = os.environ["PROF_OUT"], os.environ["PROF_PHASE"]
EVERY = 0.004
MAIN = threading.main_thread().ident
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) + os.sep
IDLE = {("selectors.py", "select"), ("threading.py", "wait"), ("threading.py", "_wait_for_tstate_lock"), ("queue.py", "get"),
        ("thread.py", "_worker"), ("socket.py", "readinto"), ("threading.py", "join"), ("selector_events.py", "_read_ready")}


def name(code) -> str:
    f = code.co_filename
    if f.startswith(ROOT):
        f = f[len(ROOT):]
    elif "site-packages" in f:
        f = f.split("site-packages" + os.sep, 1)[1]
    else:
        f = os.path.basename(f)
    return f"{f}:{code.co_firstlineno} {code.co_name}"


class Phase:
    def __init__(self):
        self.samples = self.idle = 0
        self.cum, self.self_, self.ours, self.other = Counter(), Counter(), Counter(), Counter()
        self.wall = self.cpu = 0.0

    def report(self) -> dict:
        busy = max(1, self.samples - self.idle)
        ms = lambda n: round(1000 * self.wall * n / max(1, self.samples))   # noqa: E731
        top = lambda c, k: [[what, ms(n), round(100 * n / busy, 1)] for what, n in c.most_common(k)]   # noqa: E731
        return {"wall_s": round(self.wall, 1), "samples": self.samples, "ui_busy_pct": round(100 * busy / max(1, self.samples), 1),
                "ui_busy_ms": ms(busy), "ui_cpu_ms": round(1000 * self.cpu),
                "cumulative [function, ms, % of busy]": top(self.cum, 60), "self": top(self.self_, 30),
                "innermost wheelhouse line": top(self.ours, 40), "other threads running": top(self.other, 15)}


phases: dict[str, Phase] = defaultdict(Phase)


def sampler() -> None:
    clock = time.pthread_getcpuclockid(MAIN)
    phase, at, cpu, wrote = "startup", time.perf_counter(), time.clock_gettime(clock), time.perf_counter()
    while True:
        time.sleep(EVERY)
        now = time.perf_counter()
        frames = sys._current_frames()
        p = phases[phase]
        p.samples += 1
        frame = frames.get(MAIN)
        if frame is not None:
            leaf = frame.f_code
            if (os.path.basename(leaf.co_filename), leaf.co_name) in IDLE:
                p.idle += 1
            else:
                p.self_[f"{name(leaf)} (line {frame.f_lineno})"] += 1
                seen, ours = set(), None
                f = frame
                while f is not None:
                    code = f.f_code
                    if code not in seen:
                        seen.add(code)
                        p.cum[name(code)] += 1
                    if ours is None and code.co_filename.startswith(ROOT):
                        ours = f"{name(code)} (line {f.f_lineno})"
                    f = f.f_back
                p.ours[ours or "(none: Textual's own)"] += 1
                for ident, fr in frames.items():
                    if ident not in (MAIN, threading.get_ident()) and (os.path.basename(fr.f_code.co_filename), fr.f_code.co_name) not in IDLE:
                        p.other[name(fr.f_code)] += 1
        if now - wrote > 1 or now - at > 5:
            wrote = now
            try:
                new = open(PHASE).read().strip() or phase
            except OSError:
                new = phase
            c = time.clock_gettime(clock)
            p.wall += now - at
            p.cpu += c - cpu
            at, cpu = now, c
            if new != phase or p.wall % 5 < 1.1:
                tmp = OUT + ".tmp"
                with open(tmp, "w") as fh:
                    json.dump({k: v.report() for k, v in phases.items()}, fh, indent=1)
                os.replace(tmp, OUT)
            phase = new


threading.Thread(target=sampler, daemon=True, name="prof385").start()
EXP = os.environ.get("EXP", "").split(",")   # experiments, to size a fix before building it
if "coalesce" in EXP:   # a pointer move waiting behind a newer one is dropped (Textual does this for Resize only)
    from textual import events
    events.MouseMove.can_replace = lambda self, message: isinstance(message, events.MouseMove)
if "switch1ms" in EXP:   # the GIL offered to a waiting thread after 1 ms, not 5
    sys.setswitchinterval(0.001)
sys.argv = ["claude-wheelhouse", *sys.argv[1:]]
from claude_wheelhouse.cli import main   # noqa: E402

main()
