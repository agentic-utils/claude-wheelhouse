"""#249 PR 2: Relaunch, a launch's start (New session, Adopt, a fork's child) and the handover to
Claude Code in a shell, as operations: blue from the press until the new process registers or the
session is handed over, refused around, aborted by Interrupt, and failed past their deadlines."""
import time

import pytest

from claude_wheelhouse import launch, liveness
from claude_wheelhouse.hub import Hub
from claude_wheelhouse.store import Busy, operating


@pytest.fixture
def hub(store):
    return Hub(store)


def status(monkeypatch, state):
    monkeypatch.setattr(liveness, "status", lambda s, **kw: state)


def hosted(store, sid, pid=4242):
    store.set_runner(sid, "sdk")
    store.db.execute("UPDATE sessions SET claude_pid = ?, claude_start = 1, boot_id = 'b', heartbeat_at = '2026-01-01'",
                     (pid,))
    store.mark_version(sid)


@pytest.fixture
def launches(store, monkeypatch):
    """Every launch marked, as the real one does, and nothing opened."""
    opened = []

    def opening(store_, sid, *a, **kw):
        store_.mark_launched(sid)
        opened.append(sid)
    for name in ("open_session", "open_tab", "open_host"):
        monkeypatch.setattr(launch, name, opening)
    monkeypatch.setattr(launch, "restore_session", lambda store_, sid, **kw: opening(store_, sid))
    return opened


def registered(store, sid, pid=5151):
    store.register(sid, pid, 2, "b")


def overdue(store, sid):
    store.db.execute("UPDATE sessions SET op_deadline = '2000-01-01T00:00:00+00:00' WHERE id = ?", (sid,))


def kind_state(store, sid):
    op = store.op(sid)
    return op["kind"], op["state"]


@pytest.mark.parametrize("how, steps, desc", [
    ("stop", ["press:relaunch:stopping", "gone", "launch:relaunch:starting", "register:relaunch:completed"],
     "Relaunch of a running host: stopping, starting once gone, completed at registration"),
    ("dead", ["launch:relaunch:starting", "register:relaunch:completed"], "Relaunch of a stopped session"),
    ("wake", ["launch:relaunch:starting", "register:relaunch:completed"], "Ctrl+S on a stopped session: its send goes"),
    ("new", ["launch:start:starting", "register:start:completed"], "New session, Adopt or a fork's child: start"),
])
def test_a_launch_is_an_op_from_the_press_to_registration(hub, store, sid, monkeypatch, launches, how, steps, desc):
    if how != "new":
        hosted(store, sid)
    alive = {4242}
    monkeypatch.setattr(liveness, "is_alive", lambda pid, *a: pid in alive)
    monkeypatch.setattr(Hub, "stop_process", staticmethod(lambda s: True))
    store.queue(sid, "queued")
    for step in steps:
        if step.startswith("press"):
            assert hub.stop_host(sid, (4242, 1)).ok
        elif step == "gone":
            alive.clear()
        elif step.startswith("launch"):
            if how == "stop":
                hub.refresh()
                hub.finish_relaunches()
            elif how == "dead":
                alive.clear()
                assert hub.relaunch(sid).ok
            elif how == "wake":
                alive.clear()
                assert hub.wake_and_send(sid).ok
                assert not store.drafts(sid), f"{desc}: the wake's own send is its payload, never refused"
            else:
                store.mark_launched(sid)
        elif step.startswith("register"):
            registered(store, sid)
        want = tuple(step.split(":")[1:]) if ":" in step else ("relaunch", "stopping")
        assert kind_state(store, sid) == want, f"{desc}: {step}"
        assert bool(operating(store.session(sid))) == (want[1] != "completed"), f"{desc}: {step}, blue while active"


def test_the_handover_runs_from_the_press_to_the_tab(store, sid):
    hosted(store, sid)
    store.command(sid, "shell")
    assert kind_state(store, sid) == ("handover", "requested")
    assert store.take_command(sid) == "shell"
    assert kind_state(store, sid) == ("handover", "opening")
    store.set_shell(sid, "tab")
    assert kind_state(store, sid) == ("handover", "completed") and not operating(store.session(sid))
    store.mark_launched(sid)   # the host opens the tab: no launch op of its own
    assert kind_state(store, sid) == ("handover", "completed")


@pytest.mark.parametrize("state", ["starting", "stopping", "handover"])
@pytest.mark.parametrize("act, action, desc", [
    (lambda st, sid: st.send(sid, "hi"), "send", "an immediate send"),
    (lambda st, sid: st.dispatch(sid), "send", "Ctrl+S, Send"),
    (lambda st, sid: st.command(sid, "compact"), "compact", "Compact"),
    (lambda st, sid: st.request(sid, "park"), "park", "Park"),
    (lambda st, sid: st.request(sid, "end"), "stop and forget", "Stop & Forget"),
])
def test_a_launch_refuses_dispatch_and_every_other_op(store, sid, state, act, action, desc):
    hosted(store, sid)
    if state == "starting":
        store.mark_launched(sid)
    elif state == "stopping":
        store.set_relaunch(sid, (4242, 1), time.time() + 30)
    else:
        store.command(sid, "shell")
    store.queue(sid, "queued")   # Q244: queueing stays open
    doing = {"starting": "relaunching", "stopping": "relaunching", "handover": "handing over to Claude Code in a shell"}
    with pytest.raises(Busy, match=f"^demo is busy {doing[state]}: {action} once it's done, or Interrupt to abort it.$"):
        act(store, sid)
    assert len(store.drafts(sid)) == 1, desc


def test_a_fork_blues_the_child_never_the_parent(store, sid, tmp_path):
    hosted(store, sid)
    child = store.create_session(str(tmp_path), name="demo (fork)")
    store.mark_launched(child)
    assert kind_state(store, child) == ("start", "starting") and operating(store.session(child))
    assert not operating(store.session(sid)), "the parent stays as it is (Q231)"


@pytest.mark.parametrize("state, said, desc", [
    ("stopping", "its host had been asked to stop, so it stays stopped", "stopping: the new process never started"),
    ("starting", "it stops as soon as it has started", "starting: stopped once it registers, its wake's send queued"),
])
def test_interrupt_aborts_a_relaunch(hub, store, sid, monkeypatch, launches, state, said, desc):
    hosted(store, sid)
    alive = {4242}
    monkeypatch.setattr(liveness, "is_alive", lambda pid, *a: pid in alive)
    stopped = []
    monkeypatch.setattr(Hub, "stop_process", staticmethod(lambda s: stopped.append(s["claude_pid"]) or True))
    store.queue(sid, "queued")
    if state == "stopping":
        assert hub.stop_host(sid, (4242, 1)).ok
    else:
        alive.clear()
        assert hub.wake_and_send(sid).ok
    done = hub.host_command(sid, "interrupt")
    assert done.ok and said in done.notices[0].text, desc
    s = store.session(sid)
    assert kind_state(store, sid) == ("relaunch", "cancelled") and not operating(s), desc
    assert s["relaunch"] is None and sid not in hub.relaunching and s["host_command"] is None, f"{desc}: nothing left"
    alive.clear()
    hub.refresh()
    hub.finish_relaunches()
    assert launches == ([] if state == "stopping" else [sid]), f"{desc}: never started again"
    if state == "starting":
        assert [m["body"] for m in store.drafts(sid)] == ["queued"], f"{desc}: the wake's send back in the queue"
        registered(store, sid)
        alive.add(5151)
        status(monkeypatch, "live")
        hub.refresh()
        hub.refresh()
        assert stopped == [5151], f"{desc}: the new process stopped, once"
    else:
        assert stopped == [4242], desc


def test_interrupt_aborts_a_start(hub, store, sid, monkeypatch):
    store.mark_launched(sid)
    done = hub.host_command(sid, "interrupt")
    assert done.ok and "start aborted" in done.notices[0].text
    assert kind_state(store, sid) == ("start", "cancelled")


@pytest.mark.parametrize("state, ok, left, desc", [
    ("requested", True, ("handover", "cancelled"), "untaken: the Shell dropped, the host keeps the session"),
    ("opening", False, ("handover", "opening"), "taken: under way, said it can't be aborted now"),
])
def test_interrupt_and_the_handover(hub, store, sid, monkeypatch, state, ok, left, desc):
    hosted(store, sid)
    status(monkeypatch, "live")
    store.command(sid, "shell")
    if state == "opening":
        store.take_command(sid)
    done = hub.host_command(sid, "interrupt")
    assert done.ok == ok and kind_state(store, sid) == left, desc
    assert store.session(sid)["host_command"] is None, f"{desc}: no Shell, no Interrupt left behind"


@pytest.mark.parametrize("setup, live, want, notice, desc", [
    ("starting", "dead", ("start", "failed"), "demo didn't start within 90 s: Relaunch it.", "never registered"),
    ("handover", "dead", ("handover", "failed"),
     "demo's handover to Claude Code in a shell failed: its host isn't running.", "untaken, its host gone"),
    ("handover-overdue", "live", ("handover", "failed"),
     "demo's handover to Claude Code in a shell didn't finish within 60 s.", "untaken past its deadline"),
    ("stopping-lost", "dead", ("relaunch", "failed"), "demo wasn't relaunched: Relaunch it.",
     "a relaunch no hub tracks, past its deadline and slack"),
    ("starting-fresh", "dead", ("start", "starting"), None, "within its 90 s: still starting"),
])
def test_every_launch_state_has_a_way_out(store, sid, monkeypatch, setup, live, want, notice, desc):
    if setup.startswith("starting"):
        store.mark_launched(sid)
    elif setup.startswith("handover"):
        hosted(store, sid)
        store.command(sid, "shell")
    else:
        hosted(store, sid)
        store.set_relaunch(sid, (4242, 1), time.time() + 30)
    if setup in ("starting", "handover-overdue", "stopping-lost"):
        overdue(store, sid)
    status(monkeypatch, live)
    hub = Hub(store)
    hub.relaunching.clear()   # stopping-lost: no hub drives it
    heard = []
    hub.listen(heard.append)
    hub.refresh()
    assert kind_state(store, sid) == want, desc
    assert [e.text for e in heard if e.kind == "notice"] == ([notice] if notice else []), desc
    s = store.session(sid)
    assert s["relaunch"] is None or want[1] != "failed", f"{desc}: no payload left"
    assert s["host_command"] is None or want[1] != "failed", f"{desc}: no Shell left"


def test_a_launch_that_fails_fails_its_op_at_once(hub, store, sid, monkeypatch):
    def broken(store_, sid_, *a, **kw):
        store_.mark_launched(sid_)
        raise OSError("wt.exe missing")
    monkeypatch.setattr(launch, "open_session", broken)
    monkeypatch.setattr(launch, "restore_session", lambda store_, sid_, **kw: broken(store_, sid_))
    done = hub.relaunch(sid)
    assert not done.ok and kind_state(store, sid) == ("start", "failed")
