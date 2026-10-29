# ruff: noqa: F811 (the fixtures host and streamed, imported from test_host, are the tests' parameters)
"""The compaction cycle as its host drives it (#244, .plan/244-compaction-cycle.md, section 7)."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from claude_wheelhouse import host as host_mod
from claude_wheelhouse.host import Host
from claude_wheelhouse.store import NOTES_WAIT, Compacting, compacting_now

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
    ("park", ["poll", "compacted"], [None, "compacting", "completed"], ["/compact"],
     "Park's Compact first: /compact once parked, then nothing"),
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
    h.cycle.since -= host_mod.COMPACT_TIMEOUT + 1
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
    ref = store.post_item(sid, "question", "which db?")
    store.queue(sid, "use Postgres", ref)
    store.command(sid, "compact")
    run(host.poll())
    assert store.compaction(sid)["state"] == "preparing"
    with pytest.raises(Compacting, match="is compacting: send once it's done"):
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
    with pytest.raises(Compacting, match="compact once it's done"):
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
    with pytest.raises(Compacting):
        store.send(sid, "I'm back")
    with pytest.raises(Compacting):
        store.queue(sid, "I'm back")
    assert store.compaction(sid)["state"] == "compacting" and not store.messages(sid)
