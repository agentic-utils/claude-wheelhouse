"""#249: an operation's row (sessions.op*) for Park and Stop & Forget: blue from the press to the end,
dispatch and every other operation refused, queueing open, Interrupt aborts, and a way out of every
state (.plan/249-ops-blue.md)."""
import pytest

from claude_wheelhouse import liveness
from claude_wheelhouse.hub import Hub
from claude_wheelhouse.store import PARK_WITHIN, Busy, operating

CANCEL = "The person cancelled the {what} request: carry on as before."


@pytest.fixture
def hub(store):
    return Hub(store)


@pytest.fixture
def hosted(store, sid, monkeypatch):
    """A running host's session (SDK), on current code."""
    store.set_runner(sid, "sdk")
    store.db.execute("UPDATE sessions SET claude_pid = 4242, claude_start = 1, boot_id = 'b'")
    store.mark_version(sid)
    status(monkeypatch, "live")
    return sid


def status(monkeypatch, state):
    monkeypatch.setattr(liveness, "status", lambda s, **kw: state)


def into(store, sid, what, state, compact=False):
    """The session's park or end, moved on to state as its host or monitor and the session would."""
    store.request(sid, what, compact and what == "park")
    if state in ("told", "parking"):
        store.tell_request(sid, what, store.session(sid)[f"{what}_requested_at"])
    if state == "parking":
        store.park(sid, compact=compact, keep="")


def overdue(store, sid):
    store.db.execute("UPDATE sessions SET op_deadline = '2000-01-01T00:00:00+00:00' WHERE id = ?", (sid,))


def said(store, sid) -> list[str]:
    return [m["body"] for m in store.messages(sid) if not m["draft"]]


STATES = [("park", "requested"), ("park", "told"), ("park", "parking"), ("end", "requested"), ("end", "told")]
DOING = {"park": "parking", "end": "being stopped and forgotten"}


@pytest.mark.parametrize("steps, desc", [
    ([("request", "park", "requested"), ("tell", "park", "told"), ("park", None, "parking"), ("gone", None, "completed")],
     "Park: asked, told, parked, its process gone"),
    ([("request+compact", "park", "requested"), ("tell", "park", "told"), ("park", None, "parking"),
      ("gone", None, "completed")], "Park with Compact first: the same, its cycle inside"),
    ([("park", None, "parking"), ("gone", None, "completed")], "park_session unasked: parking until it has gone"),
    ([("request", "end", "requested"), ("tell", "end", "told"), ("end", None, None)], "Stop & Forget: the row goes"),
])
def test_an_op_runs_from_the_press_to_its_end(hub, store, hosted, monkeypatch, steps, desc):
    sid = hosted
    for step, what, want in steps:
        if step.startswith("request"):
            store.request(sid, what, step.endswith("compact"))
        elif step == "tell":
            store.tell_request(sid, what, store.session(sid)[f"{what}_requested_at"])
        elif step == "park":
            store.park(sid, compact=False, reason="done")
        elif step == "end":
            store.end_session(sid)
        elif step == "gone":
            status(monkeypatch, "dead")
        hub.refresh()
        s = store.session(sid)
        if want is None:
            assert s is None, f"{desc}: {step}"
            continue
        assert store.op(sid)["state"] == want, f"{desc}: {step}"
        assert bool(operating(s)) == (want != "completed"), f"{desc}: {step}, blue while active"


@pytest.mark.parametrize("what, state", STATES)
@pytest.mark.parametrize("act, action, desc", [
    (lambda st, sid: st.send(sid, "hi"), "send", "an immediate send"),
    (lambda st, sid: st.dispatch(sid), "send", "Ctrl+S, Send"),
    (lambda st, sid: st.dispatch_last(sid, "hi"), "send", "a wake's send"),
    (lambda st, sid: st.command(sid, "compact"), "compact", "Compact"),
    (lambda st, sid: st.command(sid, "shell"), "open a shell tab", "Shell"),
    (lambda st, sid: st.request(sid, "park"), "park", "Park"),
    (lambda st, sid: st.request(sid, "end"), "stop and forget", "Stop & Forget, Let it know"),
    (lambda st, sid: st.check_busy(sid, "relaunch"), "relaunch", "Relaunch, Unpark (the hub's check)"),
])
def test_an_op_refuses_dispatch_and_every_other_op(store, hosted, what, state, act, action, desc):
    sid = hosted
    store.queue(sid, "queued before")
    into(store, sid, what, state)
    before = ([dict(m) for m in store.messages(sid)], dict(store.session(sid)))
    with pytest.raises(Busy) as e:
        act(store, sid)
    assert str(e.value) == f"demo is busy {DOING[what]}: {action} once it's done, or Interrupt to abort it.", desc
    assert ([dict(m) for m in store.messages(sid)], dict(store.session(sid))) == before, f"{desc}: nothing written"


@pytest.mark.parametrize("what, state", STATES)
@pytest.mark.parametrize("act, desc", [
    (lambda h, sid: h.force(sid, "park"), "Park now"),
    (lambda h, sid: h.force(sid, "end"), "Stop & Forget, Just stop it"),
    (lambda h, sid: h.unpark(sid), "Unpark"),
    (lambda h, sid: h.stop_host(sid, (4242, 1)), "Relaunch"),
    (lambda h, sid: h.wake_and_send(sid), "Ctrl+S on a stopped session"),
    (lambda h, sid: h.fork(sid, []), "Fork"),
    (lambda h, sid: h.send_queue(sid), "Send"),
])
def test_the_hubs_operations_are_refused_as_one_warning(hub, store, hosted, monkeypatch, what, state, act, desc):
    sid = hosted
    monkeypatch.setattr(Hub, "stop_process", staticmethod(lambda s: pytest.fail("nothing is stopped")))
    store.queue(sid, "queued")
    into(store, sid, what, state)
    hub.refresh()
    done = act(hub, sid)
    assert not done.ok and done.notices[0].severity == "warning", desc
    assert f"demo is busy {DOING[what]}: " in done.notices[0].text, f"{desc}: {done.notices[0].text}"
    assert len(store.drafts(sid)) == 1 and store.session(sid) is not None, f"{desc}: as it was"


@pytest.mark.parametrize("what, state", STATES)
def test_queueing_stays_open_and_nothing_goes(store, hosted, what, state):
    """Q244: drafting, queueing and unqueueing are always allowed; nothing is dispatched until the op ends."""
    sid = hosted
    into(store, sid, what, state)
    store.queue(sid, "one")
    store.queue(sid, "two")
    assert store.unqueue(store.drafts(sid)[0]["id"]) == "one"
    assert [m["body"] for m in store.drafts(sid)] == ["two"] and not store.pending(sid)


@pytest.mark.parametrize("what, state", STATES)
def test_a_call_the_session_waits_on_stays_answerable(store, hosted, what, state):
    sid = hosted
    ref = store.post_item(sid, "permission", "run rm?")
    into(store, sid, what, state)
    store.send(sid, "allow", ref)
    assert "allow" in said(store, sid)


@pytest.mark.parametrize("what, state, call, goes, desc", [
    ("park", "told", "park_session", True, "the call a park waits for"),
    ("end", "told", "end_session", True, "the call an end waits for"),
    ("park", "told", "end_session", False, "end_session mid-park"),
    ("end", "told", "park_session", False, "park_session mid-end"),
    ("park", "parking", "request_compact", False, "request_compact mid-park"),
])
def test_the_sessions_own_calls_mid_op(store, hosted, what, state, call, goes, desc):
    sid = hosted
    into(store, sid, what, state)
    do = {"park_session": lambda: store.park(sid, compact=False, reason="done"),
          "end_session": lambda: store.end_session(sid),
          "request_compact": lambda: store.request_compact(sid, "keep")}[call]
    if goes:
        do()
        return
    with pytest.raises(ValueError, match=f"this session is busy {DOING[what]}: call {call} again"):
        do()


@pytest.mark.parametrize("what, state, compact, told, command, desc", [
    ("park", "requested", False, False, None, "asked, not told: nothing said, nothing interrupted"),
    ("park", "requested", True, False, None, "asked with Compact first: its cycle cancelled too"),
    ("park", "told", False, True, "interrupt", "told: the turn interrupted, the cancel message heard"),
    ("park", "parking", False, True, "interrupt", "parked, its turn running: unparked, told to carry on"),
    ("park", "parking", True, True, "interrupt", "parked, compacting: /compact cut short, unparked"),
    ("end", "requested", False, False, None, "Stop & Forget asked: nothing said"),
    ("end", "told", False, True, "interrupt", "Stop & Forget told: the cancel message heard"),
])
def test_interrupt_aborts_and_leaves_nothing_behind(hub, store, hosted, what, state, compact, told, command, desc):
    sid = hosted
    store.queue(sid, "draft")
    into(store, sid, what, state, compact)
    hub.refresh()
    done = hub.host_command(sid, "interrupt")
    assert done.ok and "aborted" in done.notices[0].text, desc
    s = store.session(sid)
    assert store.op(sid)["state"] == "cancelled" and not operating(s), desc
    assert not s["parked"] and not s["park_compact"] and s["park_keep"] is None, f"{desc}: as it was"
    assert not any(s[f"{w}_{c}_at"] for w in ("park", "end") for c in ("requested", "told")), f"{desc}: no request"
    assert s["host_command"] == command and s["compact_plan"] is None, f"{desc}: no command but the interrupt"
    assert (CANCEL.format(what=what) in said(store, sid)) == told, f"{desc}: the cancel message"
    assert [m["body"] for m in store.drafts(sid)] == ["draft"], f"{desc}: drafts intact"


@pytest.mark.parametrize("what, state, live, past, want, notice, desc", [
    ("park", "requested", "live", True, "failed", "demo couldn't be parked within 15 minutes: it carries on as it was.",
     "PARK_WITHIN passed, untold"),
    ("park", "told", "live", True, "failed", "demo couldn't be parked within 15 minutes: it carries on as it was.",
     "PARK_WITHIN passed, told: it hears the cancel message"),
    ("end", "told", "live", True, "failed",
     "demo couldn't be stopped and forgotten within 15 minutes: it carries on as it was.", "Stop & Forget's too"),
    ("park", "requested", "dead", False, "failed", "demo couldn't be parked: it isn't running.", "its process gone"),
    ("park", "told", "stalled", False, "told", None, "stalled is only quiet: it waits for the deadline"),
    ("park", "parking", "stalled", False, "completed", None, "parked, its host stalled: completed"),
    ("park", "parking", "live", False, "parking", None, "parked, its turn still running: still parking"),
    ("park", "requested", "live", False, "requested", None, "within the deadline: still asked"),
])
def test_every_state_has_a_way_out(hub, store, hosted, monkeypatch, what, state, live, past, want, notice, desc):
    sid = hosted
    store.queue(sid, "draft")
    into(store, sid, what, state, compact=what == "park")
    if past:
        overdue(store, sid)
    status(monkeypatch, live)
    heard = []
    hub.listen(heard.append)
    hub.refresh()
    s = store.session(sid)
    assert store.op(sid)["state"] == want, desc
    assert [e.text for e in heard if e.kind == "notice"] == ([notice] if notice else []), desc
    if want == "failed":
        assert not operating(s) and not s[f"{what}_requested_at"] and not s["park_compact"], f"{desc}: unlocked"
        assert (CANCEL.format(what=what) in said(store, sid)) == (state == "told"), f"{desc}: the cancel message"
    assert [m["body"] for m in store.drafts(sid)] == ["draft"], f"{desc}: drafts intact"


def test_park_within_is_fifteen_minutes():
    assert PARK_WITHIN == 15 * 60, "Q243"


def test_a_sweep_with_nothing_to_move_takes_no_write(hub, store, hosted, monkeypatch):
    into(store, hosted, "park", "told")
    hub.refresh()
    monkeypatch.setattr(store, "fail_op", lambda *a: pytest.fail("no write"))
    monkeypatch.setattr(store, "complete_op", lambda *a: pytest.fail("no write"))
    hub.refresh()


def test_a_relaunch_fails_a_leftover_op(store, hosted):
    into(store, hosted, "park", "told")
    store.mark_launched(hosted)
    assert store.op(hosted)["state"] == "failed" and not operating(store.session(hosted))


def test_a_v25_style_park_completes_by_what_the_hub_sees(hub, store, hosted, monkeypatch):
    """An older host tells and parks as it always has (the request columns); the hub observes the end."""
    sid = hosted
    store.request(sid, "park")
    store.db.execute("UPDATE sessions SET park_told_at = park_requested_at")   # its own write, no op move
    store.park(sid, compact=False, reason="done")
    status(monkeypatch, "dead")
    hub.refresh()
    assert store.op(sid)["state"] == "completed"


@pytest.mark.anyio
async def test_the_eye_is_blue_while_an_op_runs_running_or_not(store, hosted, monkeypatch):
    from claude_wheelhouse.tui import WheelhouseApp
    into(store, hosted, "end", "requested")
    status(monkeypatch, "stalled")   # quiet, no turn running: blue by its op alone
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert app.blue() == {hosted} and app.session_list.blue == {hosted}, "blue at the press"
        app.client.host_command(hosted, "interrupt")
        await pilot.pause()
        assert not operating(store.session(hosted)), "aborted: not blue"
