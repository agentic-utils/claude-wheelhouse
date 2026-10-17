from datetime import datetime, timedelta, timezone

import pytest

from claude_wheelhouse import liveness

NOW = datetime(2026, 10, 1, 21, 0, tzinfo=timezone.utc)
BOOT = "boot-a"


def ago(seconds):
    return (NOW - timedelta(seconds=seconds)).isoformat(timespec="seconds")


@pytest.fixture
def proc(tmp_path):
    """A fake /proc: pid 4242 is a 'claude' started at tick 1000, its child 4243 is python."""
    (tmp_path / "sys/kernel/random").mkdir(parents=True)
    (tmp_path / "sys/kernel/random/boot_id").write_text(BOOT + "\n")
    for pid, ppid, name, state in ((4242, 1, "claude", "S"), (4243, 4242, "python3", "S"),
                                   (4244, 1, "python3", "Z")):   # 4244: exited, not reaped
        d = tmp_path / str(pid)
        d.mkdir()
        fields = [state, str(ppid)] + ["0"] * 17 + ["1000"]
        d.joinpath("stat").write_text(f"{pid} ({name}) " + " ".join(fields))
        d.joinpath("comm").write_text(name + "\n")
    return tmp_path


def session(**over):
    base = dict(parked=0, claude_pid=4242, claude_start=1000, boot_id=BOOT,
                heartbeat_at=ago(10), launched_at=ago(600), launched_over=None)
    return base | over


@pytest.mark.parametrize("over, waking, expected, desc", [
    ({}, False, "live", "process running, fresh heartbeat"),
    ({"heartbeat_at": ago(600)}, False, "stalled", "running but quiet"),
    ({"heartbeat_at": ago(600)}, True, "live", "quiet just after waking from sleep"),
    ({"parked": 1}, False, "live", "a parked flag never hides a running process"),
    ({"claude_start": 999}, False, "dead", "pid reused by another process"),
    ({"boot_id": "boot-old"}, False, "dead", "machine rebooted"),
    ({"claude_pid": 9999}, False, "dead", "process gone"),
    ({"claude_pid": 4244}, False, "dead", "a zombie: exited, its parent hasn't reaped it"),
    ({"claude_pid": None, "heartbeat_at": None, "launched_at": ago(10)}, False, "starting", "just launched"),
    ({"claude_pid": None, "heartbeat_at": None, "launched_at": ago(300)}, False, "dead", "launch never registered"),
    ({"claude_pid": 9999, "heartbeat_at": ago(900), "launched_at": ago(5), "launched_over": 1000}, False, "starting",
     "restore in progress"),
    ({"claude_pid": 9999, "heartbeat_at": ago(5), "launched_at": ago(5), "launched_over": 1000}, False, "starting",
     "relaunched in the second of the last run's final beat: still that run's registration"),
    ({"claude_pid": 9999, "claude_start": 2000, "heartbeat_at": ago(5), "launched_at": ago(5), "launched_over": 1000},
     False, "dead", "another process registered in the launch's own second, and has gone"),
    ({"claude_pid": None, "heartbeat_at": None, "launched_at": None}, False, "dead", "never launched"),
])
def test_status(proc, over, waking, expected, desc):
    assert liveness.status(session(**over), now=NOW, waking=waking, proc=proc) == expected, desc


@pytest.mark.parametrize("steps, expected, desc", [
    ([(1, 1)], False, "clocks agree"),
    ([(3600, 1)], True, "wall clock jumped an hour: slept"),
    ([(3600, 1), (1, 61)], False, "grace over after a minute"),
])
def test_wake_detector(steps, expected, desc):
    wall, mono = [1000.0], [50.0]
    w = liveness.WakeDetector(clock=lambda: wall[0], mono=lambda: mono[0])
    for dw, dm in steps:
        wall[0] += dw
        mono[0] += dm
        result = w.tick()
    assert result is expected, desc


class Timers:
    """threading.Timer, recorded: run() fires every timer started, as its delay passing would."""

    def __init__(self):
        self.started = []

    def __call__(self, delay, fn, args=()):
        timer = type("T", (), {"daemon": False, "start": lambda t: self.started.append((delay, fn, args))})()
        return timer

    def run(self):
        for _, fn, args in self.started:
            fn(*args)


@pytest.mark.parametrize("start, kill_after, gone_on_term, signalled, termed, group_killed, desc", [
    (1000, 10, True, True, True, False, "SIGTERM, and it exits: its group is left alone"),
    (1000, 10, False, True, True, True, "still alive after the grace: its process group is killed"),
    (1000, None, False, True, True, False, "a host: SIGTERM alone, never the group"),
    (999, 10, True, False, False, False, "the pid reused by another process (start time differs): untouched"),
])
def test_stop(proc, monkeypatch, no_group_kill, start, kill_after, gone_on_term, signalled, termed, group_killed,
              desc):
    terms, timers = [], Timers()

    def kill(pid, sig):
        terms.append((pid, sig))
        if gone_on_term:
            (proc / str(pid) / "stat").unlink()
    monkeypatch.setattr(liveness.os, "kill", kill)
    monkeypatch.setattr(liveness.threading, "Timer", timers)
    assert liveness.stop(4242, start, BOOT, kill_after=kill_after, proc=proc) is signalled, desc
    timers.run()
    assert terms == ([(4242, liveness.signal.SIGTERM)] if termed else []), desc
    assert [d for d, *_ in timers.started] == ([kill_after] if termed and kill_after else []), desc
    assert no_group_kill == ([(-4242, liveness.signal.SIGKILL)] if group_killed else []), desc


@pytest.mark.parametrize("text, expected, desc", [
    ("Sat Oct 17 03:53:50 2026", 1792209230, "Claude Code's procStart on macOS: ps's lstart in UTC"),
    ("Thu Oct  1 03:53:50 2026", 1790826830, "a one-digit day, padded with a space"),
])
def test_lstart(text, expected, desc):
    assert liveness.lstart(text) == expected, desc
