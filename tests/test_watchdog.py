"""The subagent watchdog (#328): a running subagent gone quiet is flagged once per spell, to its
session as a notice and to the person as an amber `quiet` status. Times are passed to sync as
`now`, a fake clock: nothing sleeps."""

import json
import os
from types import SimpleNamespace

import pytest

from claude_wheelhouse import watchdog
from claude_wheelhouse.subagents import AgentWatcher
from claude_wheelhouse.tui import QUIET_AGENT, WheelhouseApp
from tests.test_subagents import NOW, Session, iso, joined, launched, projects, tool_use  # noqa: F401 (fixtures)

T = float(int(NOW))   # whole seconds, so a record's time reads back exactly: each case writes at T + an offset


def rec(kind, at, *blocks, **extra):
    return {"isSidechain": True, "agentId": "a1", "type": kind, "timestamp": iso(T + at),
            "message": {"role": kind, "content": list(blocks)}, **extra}


def call(at, name="Bash", tid="toolu_x", **inp):
    return rec("assistant", at, {"type": "tool_use", "id": tid, "name": name, "input": inp})


def result(at, tid="toolu_x"):
    return rec("user", at, {"type": "tool_result", "tool_use_id": tid, "content": "ok"})


def thinking(at):
    return rec("assistant", at, {"type": "thinking", "thinking": "", "signature": "x"})


def said(at, stop="end_turn"):
    r = rec("assistant", at, {"type": "text", "text": "All done: 3 findings."})
    r["message"]["stop_reason"] = stop
    return r


def api_request(at):
    return {"type": "api-request", "agentId": "a1", "timestamp": iso(T + at)}


SIM = {"command": "./run-sim --long", "timeout": 600_000}
# the shape of a real cut-off (agent-a665336dd81e40a4e, 29 Oct): a Bash call answered, the next
# request made, its thinking written, then nothing for 6.8 h
CUT_OFF = [call(-30, command="sed -n 520,535p claude_wheelhouse/daemon.py"), result(-29), api_request(-29), thinking(0)]


class Sub:
    """A session with one background subagent, a1, running, its item made."""

    def __init__(self, store, sid, s):
        self.store, self.sid, self.s = store, sid, s
        t = s.agent("a1", "Check pages", started=T - 60)
        s.write(tool_use(t, "Check pages", at=T - 60), launched(t, "a1"))
        self.log = s.folder / "agent-a1.jsonl"
        self.watcher = AgentWatcher(sid)

    def write(self, *recs):
        with open(self.log, "a") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")

    def at(self, offset, watcher=None) -> int:
        """Synced at T + offset: how many watchdog notices its session has had."""
        (watcher or self.watcher).sync(self.store, now=T + offset)
        return len(self.notices())

    def notices(self):
        return [m["body"] for m in self.store.messages(self.sid) if m["kind"] == "notice"]

    def item(self):
        return next(i for i in self.store.items(self.sid) if i["kind"] == "agent")


@pytest.fixture
def sub(store, sid, joined):
    return Sub(store, sid, joined)


def approval(sub):
    sub.store.post_item(sub.sid, "permission", "Bash: ./run-sim --long",
                        f"Claude wants to run a command\n\n```json\n{json.dumps(SIM, indent=2)}\n```")


def ten_minutes(sub):
    sub.store.set_setting(watchdog.SETTING + sub.sid, "10")


@pytest.mark.parametrize("recs, setup, checks, desc", [
    ([thinking(0)], None, [(299, 0), (300, 1), (400, 1), (3600, 1)],
     "quiet for 5 min with nothing in flight fires once, not once a tick"),
    (CUT_OFF, None, [(299, 0), (300, 1), (24_000, 1)], "a stream cut off after its thinking fires at 5 min"),
    ([call(0, **SIM)], None, [(300, 0), (600, 0), (659, 0)], "a tool call in flight within its timeout doesn't fire"),
    ([call(0, **SIM)], None, [(659, 0), (660, 1)], "a hung tool call fires at its timeout plus 1 min"),
    ([call(0, command="pytest")], None, [(179, 0), (180, 1)], "a Bash call with no timeout gets Bash's 2 min, plus 1"),
    ([call(0, name="Write", file_path="/x")], None, [(359, 0), (360, 1)],
     "a tool with no timeout of its own gets the limit, plus 1 min"),
    ([call(0, **SIM)], approval, [(299, 0), (300, 1)], "a call awaiting approval fires at 5 min whatever the tool"),
    ([call(0, name="mcp__wheelhouse__quiet", tid="toolu_q", minutes=20, why="the sim runs"), result(1, "toolu_q")],
     None, [(300, 0), (1199, 0), (1200, 1)], "a declared quiet period suppresses it until the period ends"),
    ([call(0, name="mcp__wheelhouse__quiet", tid="toolu_q", minutes=20, why="x"), result(1, "toolu_q"), thinking(1300)],
     None, [(1500, 0), (1599, 0), (1600, 1)], "once the period has ended, normal watching resumes"),
    ([thinking(0)], ten_minutes, [(300, 0), (599, 0), (600, 1)], "the coordinator's set_watchdog default applies"),
    ([said(0)], None, [(3600, 0)], "a turn ended (end_turn) waits on nothing: its finish is the session's to read"),
    ([call(0, name="SubagentHandback", message="report"), result(1)], None, [(3600, 0)],
     "a hand-back answered ends its turn too"),
    ([said(0, stop=None)], None, [(300, 1)], "text mid-turn, with no end_turn, is no end"),
    ([call(0, name="mcp__wheelhouse__quiet", tid="toolu_q", minutes=20, why="sim"), result(1, "toolu_q"), call(2, **SIM)],
     approval, [(301, 0), (302, 1)], "a call awaiting approval fires at 5 min inside a declared quiet period too"),
])
def test_fires(sub, recs, setup, checks, desc):
    sub.write(*recs)
    if setup:
        setup(sub)
    assert [(t, sub.at(t)) for t, _ in checks] == checks, desc


def test_a_restart_says_nothing_twice(sub):
    sub.write(thinking(0))
    assert sub.at(300) == 1
    assert sub.at(400, AgentWatcher(sub.sid)) == 1, "a second watcher (a restart, another wheelhouse) tells it once"
    sub.write(thinking(500))
    assert (sub.at(700), sub.at(800)) == (1, 2), "a new spell after it wrote again is told again"


def test_the_message_names_the_agent_and_its_last_action(sub):
    sub.write(call(0, command="pytest tests/test_x.py"))
    sub.at(181)
    [said] = sub.notices()
    ref = sub.item()["ref"]
    for part, desc in [(f'Subagent {ref} "Check pages"', "its item and title"), ("(agent a1)", "its agent id"),
                       ("Bash: pytest tests/test_x.py (still running)", "its last action, still in flight"),
                       ("SendMessage", "what the session can do")]:
        assert part in said, desc


def test_a_subagent_of_its_own_writing_keeps_it_from_quiet(sub):
    sub.s.agent("a2", "nested review", parentAgentId="a1", spawnDepth=2, started=T)
    child = sub.s.folder / "agent-a2.jsonl"
    sub.write(call(0, name="Agent", tid="toolu_n", description="nested review", prompt="go"))
    for t, flagged, desc in [(400, 0, "past its own limit plus 1 min, but its child wrote at 300"),
                             (659, 0, "still within the limit plus 1 min of the child's last write"),
                             (660, 1, "the child silent too: flagged")]:
        if t == 400:
            os.utime(child, (T + 300, T + 300))
        assert sub.at(t) == flagged, desc


def test_an_old_declaration_is_not_named_again(sub):
    sub.write(call(0, name="mcp__wheelhouse__quiet", tid="toolu_q", minutes=10, why="the sim runs"), result(1, "toolu_q"))
    assert sub.at(600) == 1 and "the sim runs" in sub.notices()[0], "its own spell's notice names the period"
    sub.write(thinking(700))
    assert sub.at(1000) == 2 and "quiet period" not in sub.notices()[1], "a later spell's doesn't"


def cells(item):
    """The inbox row's status cell for an item, as the app draws it."""
    app = SimpleNamespace(cells_kept={})
    return WheelhouseApp.item_cells(app, item, None, {}, set(), set(), set(), frozenset(), False, False)[3]


@pytest.mark.parametrize("then, quiet, desc", [
    (lambda sub: None, True, "flagged, the row shows quiet in amber"),
    (lambda sub: sub.write(thinking(400)), False, "the amber clears once it writes again"),
    (lambda sub: sub.store.agent_status(sub.sid, "a1", "done"), False, "the amber clears when it ends"),
])
def test_amber(sub, then, quiet, desc):
    sub.write(thinking(0))
    sub.at(300)
    then(sub)
    sub.at(401)
    item = sub.item()
    assert bool(item["quiet_at"]) == quiet, desc
    shown = cells(item)
    assert (shown.plain, str(shown.style)) == (("quiet", QUIET_AGENT) if quiet else (item["status"], str(shown.style))), desc


@pytest.mark.parametrize("minutes, ok, desc", [
    (5, True, "a whole number in range"), (0, False, "zero"), (1441, False, "past a day"),
    (2.5, False, "a fraction"), (True, False, "a bool"), ("5", False, "a string"),
])
def test_minutes_checked(minutes, ok, desc):
    if ok:
        assert watchdog.minutes(minutes) == minutes, desc
    else:
        with pytest.raises(ValueError):
            watchdog.minutes(minutes)
