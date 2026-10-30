# ruff: noqa: F811 (the fixtures host and streamed, imported from test_host, are the tests' parameters)
"""The compaction cycle as its host drives it (#244, .plan/244-compaction-cycle.md, section 7)."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from claude_wheelhouse import host as host_mod
from claude_wheelhouse.host import Host
from claude_wheelhouse.store import NOTES_WAIT, Busy, compacting_now

from test_host import FakeClient, StreamClient, feed, finish_at, host, result, run, streamed, text  # noqa: F401

pytestmark = pytest.mark.usefixtures("sdk_on")

QUIET = 8 * 3600


def end(h, said="", error=False, compacted=False):
    """The turns sent all end together, saying said (an error's text, with error)."""
    async def usage():
        return {"totalTokens": 40_000, "maxTokens": 1_000_000}
    h.client.get_context_usage = usage
    h.sent.clear()
    h.compacted_from = 300_000 if compacted else None
    run(h.finished(SimpleNamespace(result=said, is_error=error, subtype="error_during_execution" if error else "success")))


def notes(h):
    """The notes turn ends carrying its block."""
    end(h, f'<keep id="{h.cycle.nonce}">\nkeep Q1\n</keep>')


def quiet(h, store):
    """The quiet mark due: big, waiting on the person, quiet KEEPALIVE_HOURS, warm."""
    h.keepalive_for, h.tokens = QUIET, 200_000
    t = time.monotonic()
    h.last_turn, h.quiet_since = t - 50 * 60, t - QUIET


def parked_first(h, store):
    """Park with Compact first: the park's turn ends, and the session has parked."""
    store.request(h.sid, "park", True)
    run(h.poll())
    store.set_parked(h.sid, True)
    end(h)
    h.client.queries.clear()


STARTS = {
    "person": lambda h, s: s.command(h.sid, "compact"),
    "model": lambda h, s: s.request_compact(h.sid, "keep the plan"),
    "relaunch": lambda h, s: s.command(h.sid, "compact", origin="relaunch"),
    "park": parked_first,
    "quiet": quiet,
    "auto": lambda h, s: run(h.pre_compact({"trigger": "auto"}, None, None)),
}
STEPS = {"poll": lambda h, s: run(h.poll()), "notes": lambda h, s: notes(h),
         "compacted": lambda h, s: end(h, compacted=True)}


@pytest.mark.parametrize("origin, steps, states, sent, desc", [
    ("person", ["poll", "notes", "compacted"], ["requested", "preparing", "compacting", "completed"],
     ["The person pressed Compact", "/compact keep Q1"], "Compact pressed: asked, notes, /compact, completed"),
    ("model", ["poll", "compacted"], [None, "compacting", "completed"], ["/compact keep the plan"],
     "request_compact: promoted once the turn has ended, its keep text the instructions"),
    ("relaunch", ["poll", "notes", "compacted"], ["requested", "preparing", "compacting", "completed"],
     ["The person pressed Compact", "/compact keep Q1"], "Relaunch compacting first: as the person's"),
    ("park", ["poll", "compacted"], ["requested", "compacting", "completed"], ["/compact"],
     "Park's Compact first: requested from the press (round 4), /compact once parked, then nothing"),
    ("quiet", ["poll", "notes", "compacted"], [None, "preparing", "compacting", "completed"],
     ["This session has been quiet 8 hours", "/compact keep Q1"], "the quiet mark: asked, then compacted"),
    ("auto", ["compacted"], ["compacting", "completed"], [], "Claude Code's own: blue from PreCompact, nothing sent"),
])
def test_each_start_route_runs_the_whole_cycle(host, store, sid, origin, steps, states, sent, desc):
    store.set_runner(sid, "sdk")
    store.post_item(sid, "question", "which db?")   # waits on the person (the quiet mark's condition)
    STARTS[origin](host, store)
    seen = [(store.compaction(sid)["state"], compacting_now(store.session(sid)))]
    for step in steps:
        STEPS[step](host, store)
        seen.append((store.compaction(sid)["state"], compacting_now(store.session(sid))))
    assert [st for st, _ in seen] == states, f"{desc}: {seen}"
    assert all(blue == (st in ("requested", "preparing", "compacting")) for st, blue in seen), f"{desc}: the eye {seen}"
    assert store.compaction(sid)["origin"] == origin and host.cycle is None, desc
    queries = host.client.queries
    assert len(queries) == len(sent) and all(q.startswith(w) or w in q for q, w in zip(queries, sent)), \
        f"{desc}: {queries}"


def interrupt(h, s):
    s.command(h.sid, "interrupt")
    run(h.poll())


def compact_timeout(h, s):
    h.cycle.waited = time.monotonic() - host_mod.COMPACT_TIMEOUT - 1   # taken up that long ago
    run(h.poll())


def host_exit(h, s):
    h.end_cycle("failed", "the host stopped")   # as run's finally


def host_restart(h, s):
    Host(s, h.sid, client_factory=FakeClient).lost_cycle()


@pytest.mark.parametrize("to, ending, state, desc", [
    ("requested", interrupt, "cancelled", "Interrupt before the host took Compact"),
    ("preparing", interrupt, "cancelled", "Interrupt during the notes turn"),
    ("compacting", interrupt, "cancelled", "Interrupt during /compact"),
    ("preparing", lambda h, s: end(h, "overloaded", error=True), "failed", "the notes turn errors"),
    ("compacting", lambda h, s: end(h, "overloaded", error=True), "failed", "/compact errors"),
    ("compacting", lambda h, s: end(h, "Not enough messages to compact."), "failed", "/compact without a boundary"),
    ("compacting", compact_timeout, "failed", "/compact past COMPACT_TIMEOUT"),
    ("preparing", host_exit, "failed", "the host exits mid-cycle"),
    ("compacting", host_restart, "failed", "a new host finds a cycle the last one was driving"),
])
def test_each_end_is_terminal_and_keeps_the_drafts(host, store, sid, to, ending, state, desc):
    store.queue(sid, "use Postgres", store.post_item(sid, "question", "which db?"))
    store.command(sid, "compact", then="send")
    if to != "requested":
        run(host.poll())
    if to == "compacting":
        notes(host)
    assert store.compaction(sid)["state"] == to, desc
    ending(host, store)
    assert store.compaction(sid)["state"] == state, desc
    assert not compacting_now(store.session(sid)), f"{desc}: the eye no longer blue"
    assert [m["body"] for m in store.drafts(sid)] == ["use Postgres"], f"{desc}: the queue left queued"
    run(host.poll())
    assert not any("use Postgres" in q for q in host.client.queries), f"{desc}: and not sent"


def test_notes_bare_when_the_turn_ends_without_its_block(host, store, sid):
    store.command(sid, "compact")
    run(host.poll())
    end(host, "Nothing to keep.")
    assert host.client.queries[-1] == "/compact" and store.compaction(sid)["state"] == "compacting"
    assert store.session(sid)["activity"] == "compacting: no notes came back, so without them"


@pytest.mark.parametrize("then, sent, desc", [
    ("send", True, "completed with then send: the queue goes in the After Compact prompt"),
    ("compact", False, "completed with then compact: the queue waits for Send"),
])
def test_the_queue_after_completion(host, store, sid, then, sent, desc):
    store.queue(sid, "use Postgres", store.post_item(sid, "question", "which db?"))
    store.command(sid, "compact", "keep Q1", then)
    run(host.poll())
    end(host, compacted=True)
    run(host.poll())
    assert any("use Postgres" in q for q in host.client.queries) is sent, f"{desc}: {host.client.queries}"


@pytest.mark.parametrize("ending, desc", [
    (lambda h: end(h, compacted=True), "completed"),
    (lambda h: end(h, "overloaded", error=True), "failed"),
])
def test_a_message_held_during_the_cycle_goes_after_it(host, store, sid, ending, desc):
    store.command(sid, "compact", "keep Q1", "compact")
    run(host.poll())
    store.notice(sid, "from before")   # undelivered when the cycle began: held, not refused
    run(host.poll())
    assert host.client.queries == ["/compact keep Q1"], f"{desc}: held"
    ending(host)
    run(host.poll())
    assert "from before" in host.client.queries[-1], f"{desc}: {host.client.queries}"


def test_notes_running_long_asks_keep_waiting_or_abort(streamed, store, sid, monkeypatch):
    """Q3: past NOTES_TIMEOUT the person is asked, never cancelled unasked; Keep waiting asks
    again after another NOTES_TIMEOUT; the question closes itself when the notes come back."""
    h, offset, real = streamed, [0.0], time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real() + offset[0])

    def waits():
        return sorted((i for i in store.items(sid) if i["answer"] == NOTES_WAIT), key=lambda i: i["id"])

    async def go():
        reader = asyncio.create_task(h.read())
        store.command(sid, "compact")
        await h.poll()
        await feed(h, h.client.replay())   # the notes turn taken up: its clock starts
        await h.poll()
        assert waits() == [], "not yet"
        offset[0] += host_mod.NOTES_TIMEOUT + 1
        await h.poll()
        await h.poll()
        assert [i["status"] for i in waits()] == ["open"], "asked once"
        store.update_item(sid, waits()[0]["ref"], status="closed")   # Keep waiting
        await h.poll()
        offset[0] += host_mod.NOTES_TIMEOUT - 5
        await h.poll()
        assert len(waits()) == 1, "Keep waiting: the clock starts again"
        offset[0] += 10
        await h.poll()
        assert [i["status"] for i in waits()] == ["closed", "open"], "asked again"
        await feed(h, text(f'<keep id="{h.cycle.nonce}">\nkeep\n</keep>'), result())
        await feed(h, None)
        await reader
    run(go())
    assert [i["status"] for i in waits()] == ["closed", "closed"], "closed itself once the notes came"
    assert h.client.queries[-1] == "/compact keep" and store.compaction(sid)["state"] == "compacting"


def test_abort_on_the_notes_question_cancels(host, store, sid):
    store.queue(sid, "use Postgres")
    store.command(sid, "compact")
    run(host.poll())
    host.cycle.waited = time.monotonic() - host_mod.NOTES_TIMEOUT - 1
    run(host.poll())
    ref = next(i["ref"] for i in store.items(sid) if i["answer"] == NOTES_WAIT)
    store.command(sid, "interrupt")   # Abort (Hub.notes_wait)
    run(host.poll())
    assert host.client.interrupts == 1 and store.compaction(sid)["state"] == "cancelled"
    assert store.item(sid, ref)["status"] == "closed" and len(store.drafts(sid)) == 1


# the 29 Oct repros (design section 1), as regression tests

def test_ctrl_s_mid_prep_is_refused(host, store, sid):
    store.set_runner(sid, "sdk")
    ref = store.post_item(sid, "question", "which db?")
    store.queue(sid, "use Postgres", ref)
    store.command(sid, "compact")
    run(host.poll())
    assert store.compaction(sid)["state"] == "preparing"
    with pytest.raises(Busy, match="is busy compacting: send once it's done"):
        store.dispatch(sid)
    run(host.poll())
    assert len(host.client.queries) == 1, f"nothing went into the notes turn: {host.client.queries}"
    assert [m["body"] for m in store.drafts(sid)] == ["use Postgres"]


def test_a_second_compact_mid_compact_is_refused_and_the_first_completes_driven(host, store, sid):
    store.post_item(sid, "question", "which db?")
    store.command(sid, "compact")
    run(host.poll())
    notes(host)
    first = store.compaction(sid)["id"]
    with pytest.raises(Busy, match="compact once it's done"):
        store.command(sid, "compact")
    run(host.poll())
    assert host.client.queries[-1] == "/compact keep Q1" and len(host.client.queries) == 2
    end(host, compacted=True)
    row = store.compaction(sid)
    assert (row["id"], row["state"]) == (first, "completed")
    notices = [m["body"] for m in store.messages(sid) if m["kind"] == "notice"]
    assert notices and not any("This compaction was automatic" in b for b in notices), notices


@pytest.mark.parametrize("write, desc", [
    (lambda s, sid: s.send(sid, "I'm back"), "an Immediate send"),
    (lambda s, sid: (s.queue(sid, "I'm back"), s.dispatch(sid)), "queued, then Ctrl+S"),
])
def test_writing_in_calls_off_the_quiet_marks_cycle_before_its_compact(host, store, sid, write, desc):
    """The quiet mark's cycle is the wheelhouse's own: the person writing in while its notes are
    asked for cancels it (the notes turn interrupted, nothing compacted), and the message goes."""
    store.set_runner(sid, "sdk")
    store.post_item(sid, "question", "which db?")
    quiet(host, store)
    run(host.poll())
    assert store.compaction(sid)["state"] == "preparing", desc
    write(store, sid)
    assert store.compaction(sid)["state"] == "cancelled" and not compacting_now(store.session(sid)), desc
    run(host.poll())
    assert host.cycle is None and host.client.interrupts, f"{desc}: the notes turn interrupted"
    assert "I'm back" in host.client.queries[-1] and not any(q.startswith("/compact") for q in host.client.queries), \
        f"{desc}: the message went, and no /compact"


def test_writing_in_once_the_quiet_marks_compact_has_gone_is_refused(host, store, sid):
    store.set_runner(sid, "sdk")
    store.post_item(sid, "question", "which db?")
    quiet(host, store)
    run(host.poll())
    notes(host)
    assert store.compaction(sid)["state"] == "compacting"
    with pytest.raises(Busy):
        store.send(sid, "I'm back")
    store.queue(sid, "I'm back")   # queueing is always allowed (Q244): only dispatch waits
    assert store.compaction(sid)["state"] == "compacting"
    assert [m["draft"] for m in store.messages(sid) if m["author"] == "person"] == [1]


def test_claude_codes_own_compaction_ends_its_cycle_at_the_boundary_mid_turn(streamed, store, sid):
    """Review 1: auto-compaction fires mid-turn; the cycle ends at its compact_boundary, not at
    the end of a long turn, so the session isn't closed for the rest of it."""
    from test_host import boundary
    store.set_runner(sid, "sdk")
    h = streamed
    states = []

    async def go():
        await h.turn("work")
        await h.pre_compact({"trigger": "auto"}, None, None)
        states.append(store.compaction(sid)["state"])
        for m in (h.client.replay(0), boundary(), text("carrying on")):
            h.client.feed.put_nowait(m)
        h.client.feed.put_nowait(None)
        await h.read()
        states.append(store.compaction(sid)["state"])
    run(go())
    assert states == ["compacting", "completed"] and h.cycle is None and h.pending == 1, states
    store.queue(sid, "queued while the turn still runs")   # no longer refused


@pytest.mark.parametrize("command, state, desc", [
    ("compact", "requested", "its Compact still waiting: the new host takes it"),
    (None, "failed", "requested, its command taken by the host that died: nothing would take it, so failed"),
])
def test_a_host_started_again_fails_a_cycle_nothing_will_take(host, store, sid, command, state, desc):
    store.command(sid, "compact")
    if command is None:
        store.take_command(sid)
    host.lost_cycle()
    assert store.compaction(sid)["state"] == state, desc


@pytest.mark.parametrize("taken_up, state, desc", [
    (None, "compacting", "queued behind a long turn, never taken up: no clock yet, still compacting"),
    (host_mod.COMPACT_TIMEOUT - 5, "compacting", "taken up, under COMPACT_TIMEOUT: still compacting"),
    (host_mod.COMPACT_TIMEOUT + 1, "failed", "taken up, past COMPACT_TIMEOUT: failed"),
])
def test_compacts_clock_starts_when_it_is_taken_up(host, store, sid, taken_up, state, desc):
    """Review 2: Compact with notes while a long turn runs queues /compact at once; its 10 minutes
    count from Claude Code taking it up, so the session stays blue until it has really run."""
    store.set_runner(sid, "sdk")
    store.command(sid, "compact", notes="keep Q1")
    run(host.poll())
    host.cycle.since -= host_mod.COMPACT_TIMEOUT * 3
    if taken_up is not None:
        host.cycle.waited = time.monotonic() - taken_up
    run(host.poll())
    assert store.compaction(sid)["state"] == state, desc


@pytest.mark.parametrize("ended, desc", [
    ("expired", "unclaimed past CLAIM_WITHIN: its command goes with it"),
    ("swept", "failed by the hub's dead-host sweep: likewise"),
    (None, "a command with no cycle row (a daemon on code before 24): ignored"),
])
def test_a_compact_command_with_no_requested_cycle_starts_nothing(host, store, sid, ended, desc):
    """Q233: the cycle row is the only trigger. A failed or expired requested cycle leaves no
    command behind, and a stray command starts no cycle, so nothing locks again or compacts."""
    store.set_runner(sid, "sdk")
    if ended:
        store.command(sid, "compact")
        cid = store.compaction(sid)["id"]
        if ended == "expired":
            aged(store, sid, 120)
            assert store.expire_requested(sid, cid, 60), desc
        else:
            store.move_compaction(sid, cid, "failed")
        assert store.session(sid)["host_command"] is None, f"{desc}: the command cleared"
    else:
        store.db.execute("UPDATE sessions SET host_command = 'compact', compact_plan = NULL")
    run(host.poll())
    assert host.cycle is None and not compacting_now(store.session(sid)) and not host.client.queries, desc



def test_a_locked_database_never_leaves_the_store_closed_with_no_cycle_held(host, store, sid, monkeypatch):
    """Review 2: the end is written before the host lets the cycle go, so a locked database
    leaves it held and the next pass ends it."""
    import sqlite3
    store.set_runner(sid, "sdk")
    store.command(sid, "compact")
    run(host.poll())
    real = store.move_compaction
    monkeypatch.setattr(store, "move_compaction", lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    with pytest.raises(sqlite3.OperationalError):
        host.end_cycle("cancelled")
    assert host.cycle is not None and store.compaction(sid)["state"] == "preparing"
    monkeypatch.setattr(store, "move_compaction", real)
    host.end_cycle("cancelled")
    assert host.cycle is None and store.compaction(sid)["state"] == "cancelled"


@pytest.mark.parametrize("ahead, state, desc", [
    (False, "failed", "no replay, nothing ahead of /compact: running, so its clock runs and it fails at the cap"),
    (True, "compacting", "a turn still ahead of /compact: not running yet, no clock"),
])
def test_compacts_clock_never_needs_its_replay(host, store, sid, ahead, state, desc):
    """Review 3: whether or not Claude Code replays /compact, its clock starts once nothing is
    ahead of it, so a hung /compact always fails at COMPACT_TIMEOUT."""
    store.set_runner(sid, "sdk")
    if ahead:
        run(host.turn("a long turn"))
    store.command(sid, "compact", notes="keep Q1")
    run(host.poll())
    run(host.poll())
    if host.cycle.waited is not None:
        host.cycle.waited -= host_mod.COMPACT_TIMEOUT + 1
    run(host.poll())
    assert store.compaction(sid)["state"] == state, desc


def test_park_with_compact_first_closes_the_session_from_the_press(host, store, sid):
    """Review 4: Park with Compact first is a compaction Doug starts: blue and closed from the
    press, while the park request itself still goes to the session."""
    store.set_runner(sid, "sdk")
    store.send(sid, "sent before the press")
    store.request(sid, "park", True)
    assert compacting_now(store.session(sid)), "blue at the press"
    with pytest.raises(Busy):
        store.send(sid, "after the press")
    store.queue(sid, "queued after the press")   # Q244: queueing stays open
    run(host.poll())
    assert len(host.client.queries) == 1 and "sent before the press" not in host.client.queries[0], \
        f"the park request alone goes; the earlier message is held: {host.client.queries}"


@pytest.mark.parametrize("how, desc", [
    (lambda h, s: s.cancel_request(h.sid, "park"), "the park request cancelled"),
    (lambda h, s: (s.set_parked(h.sid, True), s.take_park_compact(h.sid), h.sent.clear(), run(h.poll())),
     "parked, its Compact first gone meanwhile: the host lets the cycle go"),
    (lambda h, s: s.park(h.sid, hosted=False), "parked where nothing compacts"),
])
def test_parks_cycle_is_called_off_with_its_compact_first(host, store, sid, how, desc):
    store.set_runner(sid, "sdk")
    store.request(sid, "park", True)
    run(host.poll())
    how(host, store)
    assert store.compaction(sid)["state"] == "cancelled" and not compacting_now(store.session(sid)), desc


def test_an_end_its_turn_couldnt_write_is_owed_with_its_then(host, store, sid, monkeypatch):
    """Review 4: /compact ends while the database stays locked past steady's tries: the end
    is owed, and the next pass writes it and sends the queue (then send)."""
    import sqlite3
    store.set_runner(sid, "sdk")
    store.queue(sid, "queued")
    store.command(sid, "compact", notes="keep Q1", then="send")
    run(host.poll())
    real = store.move_compaction
    monkeypatch.setattr(host_mod, "STEADY_TRIES", ())
    monkeypatch.setattr(store, "move_compaction", lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    end(host, compacted=True)
    assert host.cycle is not None and host.owed[0::2] == ("completed", "send") and store.drafts(sid)
    monkeypatch.setattr(store, "move_compaction", real)
    run(host.poll())
    assert store.compaction(sid)["state"] == "completed" and host.cycle is None and not store.drafts(sid)


def aged(store, sid, seconds):
    """The cycle's state entered `seconds` ago."""
    from datetime import datetime, timedelta, timezone
    at = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="microseconds")
    store.db.execute("UPDATE sessions SET compaction_at = ? WHERE id = ?", (at, sid))


def test_parks_cycle_is_left_to_its_ops_deadline_by_the_host(host, store, sid):
    """#249: the park's op owns the deadline (Hub.sweep_ops), so the host never fails its cycle itself."""
    store.set_runner(sid, "sdk")
    store.request(sid, "park", True)
    run(host.poll())
    aged(store, sid, host_mod.PARK_WITHIN + 1)
    run(host.poll())
    assert store.compaction(sid)["state"] == "requested" and store.session(sid)["park_compact"]


def test_the_hubs_sweep_fails_an_expired_park_cycle_whoever_hosts_it(store, sid, monkeypatch):
    from claude_wheelhouse import liveness
    from claude_wheelhouse.hub import Hub
    store.set_runner(sid, "sdk")
    store.request(sid, "park", True)
    aged(store, sid, host_mod.PARK_WITHIN + 1)
    store.db.execute("UPDATE sessions SET op_deadline = compaction_at WHERE id = ?", (sid,))
    monkeypatch.setattr(liveness, "status", lambda s, **kw: "live")
    Hub(store).refresh()
    assert store.compaction(sid)["state"] == "failed" and not compacting_now(store.session(sid))


@pytest.mark.parametrize("parked, requested, state, retold, desc", [
    (False, True, "requested", True, "its request told to the last Claude Code: told again, its clock runs"),
    (True, False, "requested", False, "parked already: this host compacts it (compact_parked)"),
    (False, False, "failed", False, "neither asked nor parked: nothing would drive it, so failed"),
])
def test_a_restarted_host_redrives_or_fails_a_park_cycle(host, store, sid, parked, requested, state, retold, desc):
    store.set_runner(sid, "sdk")
    store.request(sid, "park", True)
    store.tell_request(sid, "park", store.session(sid)["park_requested_at"])
    if parked:
        store.set_parked(sid, True)
    if not requested:
        store.db.execute("UPDATE sessions SET park_requested_at = NULL, park_told_at = NULL")
    host.lost_cycle()
    assert store.compaction(sid)["state"] == state, desc
    assert (store.session(sid)["park_told_at"] is None and store.session(sid)["park_requested_at"] is not None) == retold, desc


def test_the_park_is_the_cycles_end(host, store, sid):
    """Doug: the cycle completes when parking completes: parked first, then completed."""
    store.set_runner(sid, "sdk")
    store.request(sid, "park", True)
    run(host.poll())
    store.set_parked(sid, True)
    end(host)
    run(host.poll())
    order = []
    real_park, real_move = store.set_parked, store.move_compaction
    store.set_parked = lambda *a: (order.append("parked"), real_park(*a))[1]
    store.move_compaction = lambda *a: (order.append(a[2]), real_move(*a))[1]
    end(host, compacted=True)
    assert order == ["parked", "completed"] and store.session(sid)["parked"], order


def test_messages_sent_before_the_press_are_held_while_requested(host, store, sid):
    """Round 6 (1): pressed, not yet claimed: what was sent before goes after the cycle, never
    as a turn the notes ask could merge into."""
    store.set_runner(sid, "sdk")
    store.send(sid, "sent before the press")
    store.command(sid, "compact")
    run(host.poll())
    assert len(host.client.queries) == 1 and "pressed Compact" in host.client.queries[0], host.client.queries
    assert store.pending(sid), "held, unclaimed"


def test_interrupt_during_parks_requested_cycle_drops_the_park_too(host, store, sid):
    """Round 6 (5): Interrupt before the session has parked: the request, its Compact first and
    the cycle all go, so it never compacts later under a new cycle."""
    store.set_runner(sid, "sdk")
    store.request(sid, "park", True)
    run(host.poll())
    store.command(sid, "interrupt")
    run(host.poll())
    s = store.session(sid)
    assert store.compaction(sid)["state"] == "cancelled" and not compacting_now(s)
    assert (s["park_requested_at"], s["park_compact"]) == (None, 0)


def test_the_claim_is_retried_through_a_locked_database(host, store, sid, monkeypatch):
    """Round 7: leaving requested is the host's claim; one locked write mustn't leave it
    unclaimed for CLAIM_WITHIN to fail while the notes turn runs."""
    import sqlite3
    store.set_runner(sid, "sdk")
    store.command(sid, "compact")
    real, tries = store.move_compaction, []
    monkeypatch.setattr(host_mod, "STEADY_TRIES", (0,))

    def flaky(*a):
        tries.append(a[2])
        if len(tries) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*a)
    monkeypatch.setattr(store, "move_compaction", flaky)
    run(host.poll())
    assert tries == ["preparing", "preparing"] and store.compaction(sid)["state"] == "preparing"


@pytest.mark.parametrize("start, refused, desc", [
    (lambda s, sid: s.command(sid, "compact"), True, "the person's Compact: park_session refused, said to the session"),
    (lambda s, sid: s.request(sid, "park", True), False, "Park with Compact first: this park is its own step"),
    (lambda s, sid: None, False, "no cycle: parks as ever"),
])
def test_park_session_during_a_cycle(store, sid, start, refused, desc):
    """Round 8 (1): the session can't park itself mid-cycle, or a second /compact would follow."""
    from claude_wheelhouse.store import PARK_REFUSED
    store.set_runner(sid, "sdk")
    start(store, sid)
    if refused:
        with pytest.raises(ValueError, match="can't park until that has finished"):
            store.park(sid, compact=True, keep="notes")
        assert not store.session(sid)["parked"] and PARK_REFUSED, desc
    else:
        store.park(sid, compact=True, keep="notes")
        assert store.session(sid)["parked"], desc


def test_no_keepalive_while_a_cycle_waits_to_be_claimed(host, store, sid):
    """Round 8 (2): pressed, not yet claimed: no keepalive goes ahead of the notes ask."""
    store.set_runner(sid, "sdk")
    store.post_item(sid, "question", "which db?")   # waits on the person
    host.tokens = 200_000
    host.last_turn = time.monotonic() - host_mod.KEEPALIVE_AFTER - 1
    host.quiet_since = time.monotonic() - 60
    assert host.keepalive_due(time.monotonic()), "due with no cycle"
    store.command(sid, "compact")
    assert not host.keepalive_due(time.monotonic()), "never while the store says a cycle is active"


def test_a_claim_locked_past_steady_is_owed_to_the_next_pass(host, store, sid, monkeypatch):
    """Round 8 (3): a long lock never fails the cycle spuriously: the claim is owed and retried."""
    import sqlite3
    store.set_runner(sid, "sdk")
    store.command(sid, "compact")
    real = store.move_compaction
    monkeypatch.setattr(host_mod, "STEADY_TRIES", ())
    monkeypatch.setattr(store, "move_compaction", lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    run(host.poll())
    assert host.owed_move == ("preparing", "") and store.compaction(sid)["state"] == "requested"
    monkeypatch.setattr(store, "move_compaction", real)
    run(host.poll())
    assert host.owed_move is None and store.compaction(sid)["state"] == "preparing"


def test_interrupt_after_the_notes_came_never_compacts(host, store, sid):
    """Round 10: the notes block written, the turn still running, then Interrupt (or Abort):
    its aborted end, taken while the interrupt is awaited, sends no /compact."""
    store.set_runner(sid, "sdk")
    store.command(sid, "compact")
    run(host.poll())
    host.cycle.notes = "keep Q1"

    async def interrupt():   # Claude Code ends the notes turn as aborted while poll awaits this
        host.client.interrupts += 1
        host.sent.clear()
        await host.finished(SimpleNamespace(result="", is_error=True, subtype="error_during_execution",
                                            terminal_reason="aborted_streaming"))
    host.client.interrupt = interrupt
    store.command(sid, "interrupt")
    run(host.poll())
    assert not any(q.startswith("/compact") for q in host.client.queries), host.client.queries
    assert store.compaction(sid)["state"] == "cancelled" and host.cycle is None and host.late_key is None


def test_claude_codes_own_compaction_says_its_size_on_its_task(streamed, store, sid):
    """PR 2 review: an auto cycle ends at its boundary, its task's note giving the size before."""
    from claude_agent_sdk import SystemMessage
    store.set_runner(sid, "sdk")
    h = streamed
    boundary = SystemMessage(subtype="compact_boundary", data={"compact_metadata": {"preTokens": 950_000,
                                                                                  "postTokens": 31_000}})

    async def go():
        await h.turn("work")
        await h.pre_compact({"trigger": "auto"}, None, None)
        for m in (h.client.replay(0), boundary, None):
            h.client.feed.put_nowait(m)
        await h.read()
    run(go())
    c = store.compaction(sid)
    assert c["state"] == "completed" and store.item(sid, c["item"])["status"] == "done"
    assert store.thread(sid, c["item"])[-1]["body"] == "950k → 31k", "before and after, from its boundary"
    assert store.item(sid, c["item"])["title"] == "Compact demo: 950k context"


def test_a_locked_context_write_never_stops_a_cycle_ending(host, store, sid, monkeypatch):
    """PR 2 review: the context size is a nicety; a lock on it mustn't keep the cycle open."""
    import sqlite3
    store.set_runner(sid, "sdk")
    store.command(sid, "compact", notes="keep Q1")
    run(host.poll())
    monkeypatch.setattr(store, "set_context", lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    end(host, compacted=True)
    assert store.compaction(sid)["state"] == "completed" and host.cycle is None
