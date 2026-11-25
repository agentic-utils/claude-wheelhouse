"""#385: end-to-end input latency of the real app in a pseudo-terminal.

The app runs as a person runs it (python -m claude_wheelhouse tui, Textual's Linux driver, its
input thread and its writer thread), in a 220x60 pty this script owns. Input goes in as the bytes
a terminal sends (keys, SGR mouse reports); the latency is from the write of those bytes to the
arrival, on the pty, of the output that shows the app took them:

  key      a letter typed nowhere else before  -> that letter in the output
  hover    pointer moved onto an inbox row     -> a write addressed to that row
  click    press and release on an inbox row   -> a write addressed to that row (the highlight)
  detail   the same click                      -> the item's body token (bqNNNN) in Detail
  arrow    Down in the inbox                   -> a write in the inbox; arrow_detail as detail
  burst    36 letters at 12 a second           -> each letter's echo
  dialog   Alt+N                               -> the New session dialog's title
  pin      Alt+P on an inbox item              -> a write in the inbox (a command to the daemon)
  close    Esc in that dialog                  -> the next frame over 1500 bytes (the shade lifting)

What it leaves out: the terminal emulator's own parse and paint (Windows Terminal adds its own
latency on top), and the keyboard.

Usage: ptybench.py <layout.json> <out.json> [--load N] [--label NAME] [--env K=V ...]
The environment (HOME, WHEELHOUSE_DB, WHEELHOUSE_PORT, PYTHONPATH) is the caller's: see env.sh.
--load N runs N busy loops (python, un-niced) during each measured phase, never more than 55 s
at a time, and ends them after each phase. --core C pins the app and every loop to core C, so they
compete for it (the daemon is pinned by pin-daemon.sh); this script itself runs unpinned."""
import argparse
import fcntl
import json
import os
import pty
import re
import signal
import struct
import subprocess
import sys
import termios
import threading
import time

perf = time.perf_counter
p = argparse.ArgumentParser()
p.add_argument("layout")
p.add_argument("out")
p.add_argument("--load", type=int, default=0)
p.add_argument("--label", default="")
p.add_argument("--env", action="append", default=[])
p.add_argument("--app-arg", action="append", default=[])
p.add_argument("--wrap", default="", help="a command line put before python, e.g. 'nice -n 5'")
p.add_argument("--n", type=int, default=30, help="events per phase")
p.add_argument("--core", default="", help="pin the app and the load to this core (taskset -c)")
p.add_argument("--daemon-pid", type=int, default=0, help="the scratch daemon, for its CPU time per phase")
p.add_argument("--prof", default="", help="profile the UI thread (profapp.py), written here")
p.add_argument("--load-nice", type=int, default=0, help="the load's niceness (0: as the app's)")
p.add_argument("--phases", default="hover,sweep,click,arrow,dialog,pin,key,burst")
args = p.parse_args()
layout = json.load(open(args.layout))
COLS, ROWS = 220, 60

chunks: list[tuple[float, bytes]] = []
DUMP = open(os.environ['PTY_DUMP'], 'wb') if os.environ.get('PTY_DUMP') else None
lock = threading.Lock()
pid, master = pty.fork()
if pid == 0:
    fcntl.ioctl(1, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
    env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor", **dict(e.split("=", 1) for e in args.env))
    if args.prof:
        env.update(PROF_OUT=args.prof, PROF_PHASE=args.prof + ".phase")
    run = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "profapp.py")] if args.prof else ["-m", "claude_wheelhouse"]
    argv = [*(["taskset", "-c", args.core] if args.core else []), *args.wrap.split(), sys.executable, *run, "tui", *args.app_arg]
    os.execvpe(argv[0], argv, env)


def reader():
    while True:
        try:
            data = os.read(master, 1 << 16)
        except OSError:
            return
        if not data:
            return
        with lock:
            chunks.append((perf(), data))
        if DUMP:
            DUMP.write(data)
        if b"\x1b[?2026$p" in data:   # synchronized output: supported, as Windows Terminal answers
            os.write(master, b"\x1b[?2026;2$y")


threading.Thread(target=reader, daemon=True).start()


def await_output(pattern: bytes, t0: float, timeout: float = 5.0, accept=lambda m: True):
    """The arrival time of the first chunk, after t0, that completes a match of pattern."""
    rx = re.compile(pattern)
    end = t0 + timeout
    while perf() < end:
        with lock:
            late = [(t, d) for t, d in chunks[-400:] if t >= t0]
        buf = b"".join(d for _, d in late)
        for m in rx.finditer(buf):
            if accept(m):
                upto = 0
                for t, d in late:
                    upto += len(d)
                    if upto >= m.end():
                        return t, m
        time.sleep(0.001)
    return None, None


def big_frame(t0: float, size: int, timeout: float = 5.0):
    end = t0 + timeout
    while perf() < end:
        with lock:
            late = [(t, d) for t, d in chunks[-400:] if t >= t0]
        total = 0
        for t, d in late:
            total += len(d)
            if total >= size:
                return t
        time.sleep(0.001)
    return None


def send(data: bytes) -> float:
    t = perf()
    os.write(master, data)
    return t


def mouse(code: int, x: int, y: int, release: bool = False) -> bytes:
    return f"\x1b[<{code};{x + 1};{y + 1}{'m' if release else 'M'}".encode()


def click(x: int, y: int) -> float:
    send(mouse(35, x, y))
    time.sleep(0.03)
    t = send(mouse(0, x, y))
    send(mouse(0, x, y, release=True))
    return t


loaders: list[subprocess.Popen] = []
LOAD_FOR = 55   # seconds a load burst may last: the machine is shared
started = [0.0]


def load_on():
    """The load: N busy loops, each pinned to --core when one is given (the app's own core), ended by timeout."""
    pin = ["taskset", "-c", args.core] if args.core else []
    for _ in range(args.load):
        loaders.append(subprocess.Popen([*pin, "nice", "-n", str(args.load_nice), "timeout", str(LOAD_FOR), sys.executable, "-c", "while True: pass"]))
    if args.load:
        time.sleep(2)
    started[0] = perf()


def loaded() -> bool:
    """Whether the phase may go on: under load, only while the burst lasts (what's left of a phase is dropped)."""
    return not args.load or perf() - started[0] < LOAD_FOR - 7


def load_cpu() -> float:
    """The loops' CPU seconds so far (each is its timeout's child)."""
    total = 0.0
    for q in loaders:
        kids = subprocess.run(["pgrep", "-P", str(q.pid)], capture_output=True, text=True).stdout.split()
        total += sum(cpu_s(int(k)) for k in kids)
    return total


def load_off():
    for q in loaders:
        q.terminate()   # timeout passes it on to its loop
        q.wait()
    loaders.clear()


def stat():
    """The machine's CPU counters, or the one core's with --core."""
    name = f"cpu{args.core}" if args.core else "cpu"
    return next(list(map(int, line.split()[1:])) for line in open("/proc/stat") if line.split()[0] == name)


TICK = os.sysconf("SC_CLK_TCK")


def cpu_s(of: int) -> float:
    """A process's CPU seconds so far (user and system, all threads)."""
    try:
        f = open(f"/proc/{of}/stat").read().rsplit(") ", 1)[1].split()
        return (int(f[11]) + int(f[12])) / TICK
    except (OSError, IndexError):
        return 0.0


own: dict[str, dict] = {}


def busy(a, b):
    total = sum(b) - sum(a)
    return round(100 * (total - ((b[3] + b[4]) - (a[3] + a[4]))) / total) if total else 0


def mark(name: str) -> None:
    """The phase under way, for profapp.py."""
    if args.prof:
        with open(args.prof + ".phase", "w") as f:
            f.write(name)


results: dict[str, list] = {}
machine: dict[str, int] = {}


def phase(name, body):
    load_on()
    mark(name)
    a = stat()
    t = perf()
    c = cpu_s(pid), cpu_s(args.daemon_pid), load_cpu()
    try:
        body()
    finally:
        machine[name] = busy(a, stat())
        took = perf() - t
        own[name] = {"app_cpu_pct": round(100 * (cpu_s(pid) - c[0]) / took), "daemon_cpu_pct": round(100 * (cpu_s(args.daemon_pid) - c[1]) / took),
                     "load_cpu_pct": round(100 * (load_cpu() - c[2]) / took)}
        own[name]["others_pct"] = max(0, machine[name] - sum(own[name].values()))   # with --core: what else ran on the core
        mark("between")
        time.sleep(1.2 if args.prof else 0)
        load_off()
    print(f"{name}: {perf() - t:.1f}s, cpu {machine[name]}% busy", file=sys.stderr)
    time.sleep(3)


def note(kind, t0, t1):
    results.setdefault(kind, []).append(None if t1 is None else round((t1 - t0) * 1000, 1))


ix, iy, iw, ih = layout["items"]
ax, ay, aw, ah = layout["answer"]
dx, dy, dw, dh = ix + iw + 2, iy, COLS - (ix + iw + 3), ay - iy - 2   # Detail: right of the inbox, above the answer box
rows = [iy + 2 + (i * 7) % (ih - 4) for i in range(200)]   # rows of the inbox, never the header, in a scattered order


SPAN = re.compile(rb"\x1b\[(\d+);(\d+)H((?:\x1b\[[0-9;:]*m|[^\x1b]){0,120})")
SGR = re.compile(rb"\x1b\[[0-9;:]*m")


def row_writes(data: bytes, box) -> int:
    """How many writes in data draw a row's text inside box (x, y, w, h): Textual draws a changed span by moving
    the cursor to its start. At least 8 cells of text, so an animated cell (an eye, a dot) or a bare border isn't one."""
    x, y, w, h = box
    return sum(1 for m in SPAN.finditer(data)
               if y <= int(m.group(1)) - 1 < y + h and x - 1 <= int(m.group(2)) - 1 < x + w
               and len(SGR.sub(b"", m.group(3)).decode("utf8", "replace").replace("│", "").strip()) >= 8)


def drawn_in(box, t0: float, timeout: float = 3.0, last: bool = False):
    """When the app first (or last) drew a row's text inside box after t0."""
    end = t0 + timeout
    found = None
    while perf() < end:
        with lock:
            late = [(t, d) for t, d in chunks[-600:] if t >= t0]
        for t, d in late:
            if row_writes(d, box):
                if not last:
                    return t
                found = t
        time.sleep(0.001)
    return found


INBOX = (ix, iy + 1, iw, ih - 1)


def hovers():
    for i in range(args.n):
        if not loaded():
            return
        t0 = send(mouse(35, ix + 20, rows[i]))
        note("hover", t0, drawn_in((ix, rows[i], iw, 1), t0, 2))
        time.sleep(0.25)


def sweep():
    """A pointer drawn down the inbox: a report every 8 ms for a second, as a terminal sends them, then a rest.
    The latency is from the last report to the last write in the inbox (the highlight catching up)."""
    for i in range(5):   # sweep_settle also catches the row's tooltip, drawn half a second after the pointer rests:
        if not loaded():   # read the phase for its CPU cost (own_cpu_pct, the profile), not for this latency
            return
        for k in range(120):
            t0 = send(mouse(35, ix + 10 + k % 20, iy + 2 + (k * (ih - 6)) // 120))
            time.sleep(0.008)
        note("sweep_settle", t0, drawn_in(INBOX, t0 - 0.5, 2.5, last=True))


last = [b""]


def new_body(m):
    return m.group() != last[0]


def select(kind, t0, row=None):
    note(kind, t0, drawn_in(INBOX if row is None else (ix, row, iw, 1), t0, 3))
    t2, m = await_output(rb"bq\d{4}", t0, 4, accept=new_body)
    if m:
        last[0] = m.group()
    note(kind + "_detail", t0, t2)


def clicks():
    for i in range(args.n):
        if not loaded():
            return
        select("click", click(ix + 20, rows[50 + i]), rows[50 + i])
        time.sleep(0.4)


def arrows():
    click(ix + 20, iy + 2)
    time.sleep(1)
    for i in range(min(args.n, ih - 5)):
        if not loaded():
            return
        select("arrow", send(b"\x1b[B"), iy + 3 + i)   # the row the cursor lands on
        time.sleep(0.3)


def dialogs():
    for i in range(min(args.n, 15)):
        if not loaded():
            return
        t0 = send(b"\x1bn")   # Alt+N: the New session dialog
        t1, _ = await_output(rb"NEW SESSION", t0, 4)
        note("dialog", t0, t1)
        time.sleep(0.8)
        t0 = send(b"\x1b")
        note("close", t0, big_frame(t0, 1500, 4))
        time.sleep(0.8)


def pins():
    """Alt+P pins the item under the cursor: a command to the daemon, sent from the UI thread."""
    click(ix + 20, rows[5])
    time.sleep(1)
    for i in range(min(args.n, 20)):
        if not loaded():
            return
        t0 = send(b"\x1bp")
        note("pin", t0, drawn_in((ix, rows[5], iw, 1), t0, 4))
        time.sleep(1.0)


LETTERS = [chr(c) for c in list(range(0x430, 0x450)) + list(range(0x3b1, 0x3c9)) + list(range(0x561, 0x587))]


def keys():
    click(ax + 5, ay + 1)
    time.sleep(1)
    for ch in LETTERS[:args.n]:
        if not loaded():
            return
        t0 = send(ch.encode())
        t1, _ = await_output(re.escape(ch.encode()), t0, 3)
        note("key", t0, t1)
        time.sleep(0.15)


def burst():
    """Six words typed at 12 characters a second: the latency of each letter's echo."""
    click(ax + 5, ay + 1)
    time.sleep(0.5)
    pending = []
    for ch in LETTERS[45:81]:
        pending.append((ch, send(ch.encode())))
        time.sleep(0.083)
    time.sleep(3)
    with lock:
        seen = list(chunks[-3000:])
    for ch, t0 in pending:
        t1 = next((t for t, d in seen if t >= t0 and ch.encode() in d), None)
        note("burst_key", t0, t1)


try:
    time.sleep(10 if args.core else 8)   # the app starts, connects and paints
    for _ in range(3):
        send(b"\x1b")
        time.sleep(0.4)
    click(ix + 20, rows[1])
    time.sleep(1)
    for _ in range(3):   # past the sessions' conversation rows, to the items
        send(b"\x1b[6~")
        time.sleep(0.7)
    time.sleep(2)
    mark("rest")
    a = stat()
    t = perf()
    c = cpu_s(pid), cpu_s(args.daemon_pid)
    time.sleep(6)
    mark("between")
    with lock:
        quiet = [d for at, d in chunks if at >= t]
    machine["rest"] = busy(a, stat())
    own["rest"] = {"app_cpu_pct": round(100 * (cpu_s(pid) - c[0]) / 6), "daemon_cpu_pct": round(100 * (cpu_s(args.daemon_pid) - c[1]) / 6)}
    own["rest"]["others_pct"] = max(0, machine["rest"] - sum(own["rest"].values()))
    rest = {"writes_per_s": round(len(quiet) / 6, 1), "bytes_per_s": round(sum(map(len, quiet)) / 6),
            "inbox_row_writes": sum(row_writes(d, INBOX) for d in quiet)}
    for name in args.phases.split(","):
        phase(name, {"hover": hovers, "sweep": sweep, "click": clicks, "arrow": arrows, "dialog": dialogs,
                     "pin": pins, "key": keys, "burst": burst, "rest": lambda: time.sleep(25)}[name])
finally:
    load_off()
    os.kill(pid, signal.SIGTERM)
    time.sleep(0.5)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    os.waitpid(pid, 0)


def summary(xs):
    got = sorted(x for x in xs if x is not None)
    if not got:
        return {"n": len(xs), "missed": len(xs)}
    at = lambda q: got[min(len(got) - 1, int(len(got) * q))]   # noqa: E731
    return {"n": len(xs), "missed": len(xs) - len(got), "p50": at(.5), "p95": at(.95), "max": got[-1]}


out = {"label": args.label, "load": args.load, "cpu_busy_pct": machine, "own_cpu_pct": own, "core": args.core, "loadavg": open("/proc/loadavg").read().split()[:3], "at_rest": rest,
       "summary": {k: summary(v) for k, v in results.items()}, "raw": results}
json.dump(out, open(args.out, "w"), indent=1)
print(f"== {args.label} load={args.load} core={args.core} busy {machine}")
for k, v in own.items():
    print(f"   cpu {k:7s} core busy {machine[k]:3d}% {v}")
print("   at rest", rest)
for k, s in out["summary"].items():
    print(f"   {k:13s} {s}")
