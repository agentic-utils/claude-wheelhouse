"""The account's plan usage: /usage's answer read, a background Claude Code asked for it
(a fake one: no test runs the real claude), and SDK sessions' readings merged in."""

import json
import os
import sys
import textwrap
import threading
import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from claude_wheelhouse import features, host, plan_usage
from claude_wheelhouse.hub import Hub

REAL_POLL = plan_usage.AccountUsage.poll   # before conftest stubs it
LONDON = ZoneInfo("Europe/London")


def london(*args) -> float:
    return datetime(*args, tzinfo=LONDON).timestamp()


def utc(*args) -> float:
    return datetime(*args, tzinfo=ZoneInfo("UTC")).timestamp()


ANSWER = """You are currently using your subscription to power your Claude Code usage

Current session: 10% used · resets Oct 11, 7:20pm (Europe/London)
Current week (all models): 67% used · resets Oct 15, 1am (Europe/London)
Current week (Fable): 3% used · resets Oct 15, 1am (Europe/London)

What's contributing to your limits usage?
Last 24h · 9090 requests · 48 sessions"""

NOW = london(2026, 10, 11, 17, 30)   # BST, UTC+1


@pytest.mark.parametrize("text, now, expected, desc", [
    (ANSWER, NOW, {"session": (10.0, utc(2026, 10, 11, 18, 20)), "weekly_all": (67.0, utc(2026, 10, 15, 0, 0)),
                   "weekly_fable": (3.0, utc(2026, 10, 15, 0, 0))}, "/usage as Claude Code 2.1 words it, BST to UTC"),
    ("Current session: 5% used · resets 1am (Europe/London)", NOW,
     {"session": (5.0, utc(2026, 10, 12, 0, 0))}, "a time alone, earlier than now: tomorrow's"),
    ("Current session: 5% used · resets 9pm (Europe/London)", NOW,
     {"session": (5.0, utc(2026, 10, 11, 20, 0))}, "a time alone, later than now: today's"),
    ("Current week (all models): 67% used · resets Oct 15, 1am (Europe/London)", NOW,
     {"weekly_all": (67.0, utc(2026, 10, 15, 0, 0))}, "the session's line missing: the week's alone"),
    ("Current session: 99% used · resets Jan 1, 4am (Europe/London)", london(2026, 12, 31, 23, 0),
     {"session": (99.0, utc(2027, 1, 1, 4, 0))}, "Dec 31's Jan 1 is next year's"),
    ("Current week (all models): 1% used · resets Dec 31, 11pm (Europe/London)", london(2027, 1, 1, 0, 30),
     {"weekly_all": (1.0, utc(2026, 12, 31, 23, 0))}, "Jan 1's Dec 31 is last year's"),
    ("Current session: 10% used · resets Nov 1, 7:20pm (America/New_York)", london(2026, 10, 31, 12, 0),
     {"session": (10.0, utc(2026, 11, 2, 0, 20))}, "another zone, after its clocks change: EST"),
    ("Current session: 42% used", NOW, {"session": (42.0, None)}, "no reset said: the bar without it"),
    ("Current session: 42% used · resets in 3 hours", NOW, {"session": (42.0, None)}, "a reset worded otherwise: None"),
    ("Current session: 42% used · resets Oct 12, 1am (Nowhere/Atlantis)", NOW,
     {"session": (42.0, datetime(2026, 10, 12, 1, 0).astimezone().timestamp())}, "a zone unknown: the machine's"),
    ("Current session: 7.5% used · resets 12pm (Europe/London)", NOW,
     {"session": (7.5, utc(2026, 10, 12, 11, 0))}, "12pm is noon, a fraction kept"),
])
def test_parse(text, now, expected, desc):
    assert plan_usage.parse(text, now) == expected, desc


@pytest.mark.parametrize("text, desc", [
    ("", "nothing"),
    ("Session usage: 10%\nWeekly: 5%", "worded otherwise"),
    ("Current week (Fable): 3% used · resets Oct 15, 1am (Europe/London)", "Fable's week alone: neither limit shown"),
])
def test_parse_refuses_what_it_cant_read(text, desc):
    with pytest.raises(ValueError):
        plan_usage.parse(text, NOW)


@pytest.mark.parametrize("raw, expected, desc", [
    ({"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.25, "resetsAt": 1791750000},
                                              "seven_day": {"utilization": 0.7, "resetsAt": 1792000000}}},
     {"session": (25.0, 1791750000.0), "weekly_all": (70.0, 1792000000.0)}, "both windows"),
    ({"unifiedWindows": {"five_hour": {"utilization": 0.5}}}, {"session": (50.0, None)}, "no reset"),
    ({"unifiedWindows": {"five_hour": {"resetsAt": 1}, "seven_day": "junk"}}, {}, "malformed windows skipped"),
    ({"status": "allowed"}, {}, "no windows"),
    (None, {}, "no info"),
])
def test_from_rate_limit(raw, expected, desc):
    assert plan_usage.from_rate_limit(raw) == expected, desc


@pytest.mark.parametrize("first, second, expected, desc", [
    (({"session": (10.0, None)}, 100), ({"session": (20.0, None)}, 200), {"session": (20.0, 200)},
     "a newer reading wins"),
    (({"session": (10.0, None)}, 200), ({"session": (20.0, None)}, 100), {"session": (10.0, 200)},
     "an older one, an SDK session's late, doesn't"),
    (({"session": (10.0, None), "weekly_fable": (3.0, None)}, 100), ({"session": (20.0, None)}, 200),
     {"session": (20.0, 200), "weekly_fable": (3.0, 100)}, "a kind the newer lacks stays"),
])
def test_newest_wins(first, second, expected, desc):
    u = plan_usage.AccountUsage()
    u.take(*first)
    u.take(*second)
    assert {k: (pct, u.read[k]) for k, (pct, _) in u.limits.items()} == expected, desc


class FakeProbe:
    stopped = False

    def __init__(self, answer):
        self.answer = answer

    def ask(self):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.mark.parametrize("answer, kept, desc", [
    (ANSWER, True, "an answer read: the new numbers"),
    (plan_usage.Unavailable("no answer in 60s"), True, "no answer: the last numbers stay up, failed"),
    ("Your usage: plenty", False, "an answer it can't read: unavailable, as its wording has changed"),
])
def test_a_poll(answer, kept, desc, capsys):
    u = plan_usage.AccountUsage()
    u.take({"session": (1.0, None)}, 100)
    REAL_POLL(u, FakeProbe(answer))
    REAL_POLL(u, FakeProbe(answer))
    assert bool(u.limits) == kept, desc
    assert u.failed == (answer is not ANSWER), desc
    if answer is ANSWER:
        assert u.limits["session"][0] == 10.0, desc
    else:
        assert capsys.readouterr().err.count("plan usage") == 1, f"{desc}: said once"


def test_an_unreadable_answer_keeps_an_sdk_sessions_reading():
    u = plan_usage.AccountUsage()
    u.take({"session": (10.0, None), "weekly_fable": (3.0, None)}, 100)
    u.take({"session": (30.0, None)}, 200, sdk=True)
    REAL_POLL(u, FakeProbe("Your usage: plenty"))
    assert u.limits == {"session": (30.0, None)} and u.read == {"session": 200} and u.failed


def test_a_poll_is_quiet_once_the_probe_has_stopped(tmp_path, capsys):
    p = plan_usage.Probe(tmp_path)
    p.stop()
    u = plan_usage.AccountUsage()
    REAL_POLL(u, p)
    assert capsys.readouterr().err == "" and not u.failed, "the hub is stopping: nothing said, nothing run"


def test_the_hub_takes_an_sdk_sessions_newer_reading(store, sdk_on):
    hub = Hub(store)
    hub.usage.take({"session": (10.0, None), "weekly_all": (60.0, None)}, time.time() - 120)
    host.Host.rate_limited(SimpleNamespace(store=store), {"unifiedWindows": {"five_hour": {"utilization": 0.3}}})
    hub.fetch_usage()
    assert hub.usage.limits["session"][0] == 30.0 and hub.usage.limits["weekly_all"][0] == 60.0, \
        "the SDK session's session limit, newer, wins; the week /usage read stays"
    store.set_setting(plan_usage.SDK_SETTING, json.dumps({"at": 1, "limits": {"session": [5.0, None]}}))
    hub.sdk_usage_due = 0
    hub.fetch_usage()
    assert hub.usage.limits["session"][0] == 30.0, "an older reading doesn't"


def test_a_host_keeps_its_reading_but_the_hub_takes_none_with_sdk_mode_off(store, tmp_path, monkeypatch):
    """A host runs without --enable-sdk, in the session's directory, where no .env says SDK
    mode: it keeps the reading whatever its own mode; the hub's mode decides (review 2)."""
    monkeypatch.chdir(tmp_path)   # no .env here, nor (conftest's sdk_off) in the config
    assert not features.sdk_enabled()
    host.Host.rate_limited(SimpleNamespace(store=store), {"unifiedWindows": {"five_hour": {"utilization": 0.3}}})
    assert json.loads(store.setting(plan_usage.SDK_SETTING))["limits"] == {"session": [30.0, None]}, "kept"
    hub = Hub(store)
    hub.fetch_usage()
    assert hub.usage.limits == {}, "the hub out of SDK mode takes none"


def test_an_unreadable_sdk_reading_is_said_not_raised(store, capsys, sdk_on):
    hub = Hub(store)
    store.set_setting(plan_usage.SDK_SETTING, "{not json")
    hub.fetch_usage()
    assert hub.usage.limits == {} and "SDK session" in capsys.readouterr().err


# Claude Code, faked: argv[1] says how it behaves. "oneshot..." runs once and exits, as
# claude -p "/usage" does; the others read stream-json on stdin, as the persistent one does

FAKE = textwrap.dedent('''
    import json, os, sys, time
    mode = sys.argv[1]
    said = {"pid": os.getpid(), "cwd": os.getcwd(), "env": sorted(k for k in os.environ if k.startswith("WHEELHOUSE_"))}
    result = {"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(said)}
    if mode.startswith("oneshot"):
        if mode == "oneshot-stubborn":   # a child that ignores SIGTERM, in its group: SIGKILL ends it
            import subprocess
            child = subprocess.Popen([sys.executable, "-c",
                                      "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"])
            open("child.pid", "w").write(str(child.pid))
            time.sleep(60)
        if mode == "oneshot-hang":
            time.sleep(60)
        if mode == "oneshot-silent":
            sys.exit(1)
        if mode == "oneshot-error":
            result |= {"is_error": True, "result": "Not logged in"}
        print(json.dumps([{"type": "system", "subtype": "init"}, result]))
        sys.exit(0)
    for n, line in enumerate(sys.stdin):
        assert json.loads(line)["message"]["content"] == "/usage"
        if mode == "hang":
            time.sleep(60)
        print(json.dumps({"type": "system", "subtype": "init"}))
        print(json.dumps({"type": "assistant", "message": {"content": []}}))
        print(json.dumps(result), flush=True)
        if mode == "once":
            sys.exit(0)
''')


@pytest.fixture
def fake(tmp_path):
    path = tmp_path / "fake_claude.py"
    path.write_text(FAKE)
    probes = []

    def make(how="serve", timeout=10):
        mode = "oneshot" if how.startswith("oneshot") else "persistent"
        p = plan_usage.Probe(tmp_path, mode=mode, command=[sys.executable, str(path), how], timeout=timeout)
        probes.append(p)
        return p
    yield make
    for p in probes:
        p.stop()


def gone(pid: int) -> bool:
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


@pytest.mark.parametrize("mode, has, lacks, desc", [
    ("oneshot", ["-p", "/usage", "json", "--setting-sources=", "--strict-mcp-config", "--no-session-persistence"],
     ["--input-format"], "one /usage, as its argument"),
    ("persistent", ["--input-format", "stream-json", "--setting-sources=", "--strict-mcp-config",
                    "--no-session-persistence"], ["/usage"], "a session reading stream-json"),
])
def test_each_modes_command(mode, has, lacks, desc, monkeypatch):
    monkeypatch.delenv(plan_usage.CLAUDE_ENV)
    argv = plan_usage.command(mode)
    assert argv[0] == "claude" and all(a in argv for a in has) and not any(a in argv for a in lacks), desc
    assert "--bare" not in argv, f"{desc}: --bare loses the login"


def test_oneshot_is_the_default(tmp_path):
    assert plan_usage.Probe(tmp_path).mode == "oneshot"


def test_oneshot_runs_a_claude_per_ask_and_leaves_none(fake, tmp_path):
    p = fake("oneshot")
    first = json.loads(p.ask())
    assert p.proc is None and gone(first["pid"]), "nothing left running between asks"
    second = json.loads(p.ask())
    assert second["pid"] != first["pid"], "a Claude Code of its own each time"
    assert first["cwd"] == str(tmp_path), "run in the state dir"
    assert not set(first["env"]) & set(plan_usage.UNSET), "not taken for one of the wheelhouse's sessions"


@pytest.mark.parametrize("how, match, desc", [
    ("oneshot-silent", "without an answer", "it exits saying nothing"),
    ("oneshot-hang", "no answer", "it doesn't answer in time: killed"),
    ("oneshot-error", "Not logged in", "it answers with an error"),
])
def test_oneshot_failures(fake, how, match, desc):
    p = fake(how, timeout=0.5)
    with pytest.raises(plan_usage.Unavailable, match=match):
        p.ask()
    assert p.proc is None, f"{desc}: nothing left running"
    if how != "oneshot-error":   # an answer, if not a good one: Claude Code works
        assert p.deaths == 1 and p.not_before > time.time(), f"{desc}: backed off"
        with pytest.raises(plan_usage.Unavailable, match="after a pause"):
            p.ask()


def test_a_kill_reaches_a_child_that_ignores_sigterm(fake, tmp_path):
    p = fake("oneshot-stubborn", timeout=1)
    with pytest.raises(plan_usage.Unavailable, match="no answer"):
        p.ask()
    assert gone(int((tmp_path / "child.pid").read_text())), "SIGKILL followed, though the leader went at SIGTERM"


def test_stop_kills_a_oneshot_under_way(fake):
    p = fake("oneshot-hang", timeout=30)
    failed = []
    t = threading.Thread(target=lambda: failed.append(pytest.raises(plan_usage.Unavailable, p.ask)))
    t.start()
    deadline = time.time() + 5
    while p.proc is None and time.time() < deadline:
        time.sleep(0.02)
    pid = p.proc.pid
    p.stop()
    t.join(10)
    assert not t.is_alive() and failed and gone(pid), "the run under way killed, the ask ended"


def test_the_probe_starts_when_asked_and_stays(fake, tmp_path):
    p = fake()
    assert p.proc is None, "nothing runs until asked"
    first, second = json.loads(p.ask()), json.loads(p.ask())
    assert first["pid"] == second["pid"] == p.proc.pid, "one Claude Code, asked again"
    assert first["cwd"] == str(tmp_path), "run in the state dir"
    assert not set(first["env"]) & set(plan_usage.UNSET), "not taken for one of the wheelhouse's sessions"


def test_the_probe_starts_again_after_dying(fake, monkeypatch):
    p = fake("once")
    pid = json.loads(p.ask())["pid"]
    p.proc.wait(5)
    with pytest.raises(plan_usage.Unavailable):
        p.ask()   # found dead: not started again at once
    assert p.deaths == 1 and p.not_before > time.time(), "backed off"
    p.not_before = 0
    assert json.loads(p.ask())["pid"] != pid, "started again once the backoff is over"


def test_backoff_doubles_to_a_ceiling():
    p = plan_usage.Probe("/")
    waits = []
    for deaths in (1, 2, 3, 20):
        p.deaths = deaths
        waits.append(p.backoff())
    assert waits == [plan_usage.BACKOFF, 2 * plan_usage.BACKOFF, 4 * plan_usage.BACKOFF, plan_usage.BACKOFF_MAX]


def test_a_probe_that_doesnt_answer_is_killed(fake):
    p = fake("hang", timeout=0.5)
    with pytest.raises(plan_usage.Unavailable, match="no answer"):
        p.ask()
    assert p.proc is None and p.deaths == 1


def test_the_probe_is_killed_when_the_hub_stops(fake, store):
    hub = Hub(store)
    hub.usage_probe = p = fake()
    pid = json.loads(p.ask())["pid"]
    hub.stop_usage()
    assert gone(pid), "killed"
    with pytest.raises(plan_usage.Unavailable, match="stopped"):
        p.ask()


@pytest.mark.parametrize("mode", plan_usage.MODES)
def test_a_claude_that_isnt_there_is_unavailable(tmp_path, mode):
    p = plan_usage.Probe(tmp_path, mode=mode, command=[str(tmp_path / "no-such-claude")])
    with pytest.raises(plan_usage.Unavailable, match="couldn't start"):
        p.ask()
    assert p.not_before > time.time(), "backed off"
