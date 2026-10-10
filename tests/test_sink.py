"""Settled items and parked sessions sink to the foot of their list, and rise again (T66, T71):
one status table for every kind (store.standing), and one animation for both lists (Sink)."""

import pytest

from claude_wheelhouse.store import Store, standing
from claude_wheelhouse.tui import BLANK, Sink, WheelhouseApp
from test_tui import assert_one_current, one_second, raw_keys

DECIDED = {"alternative": "Postgres", "why": "no server", "reverse": "swap the DSN"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.parametrize("kind, status, busy, dismissed, expected, desc", [
    ("task", "todo", False, None, "active", "a task to do"),
    ("task", "running", False, None, "active", "a running task"),
    ("task", "blocked", False, None, "active", "a blocked task"),
    ("task", "waiting", False, None, "active", "a waiting task"),
    ("task", "done", False, None, "settled", "a done task settles"),
    ("task", "dropped", False, None, "settled", "a dropped task settles"),
    ("task", "done", True, None, "active", "a done task with a word queued or awaiting a reply doesn't"),
    ("task", "done", False, "done", "finished", "a done task dismissed"),
    ("task", "running", False, "done", "active", "a dismissed task the session took up again"),
    ("task", "done", True, "done", "active", "a dismissed task with a word queued or awaiting a reply stays in sight"),
    ("agent", "failed", True, "failed", "active", "so does a dismissed subagent"),
    ("question", "open", False, None, "active", "an open question"),
    ("question", "answered", False, None, "settled", "an answered question settles"),
    ("question", "answered", True, None, "active", "one with a further word queued or awaiting doesn't"),
    ("question", "closed", False, None, "finished", "a closed question"),
    ("decision", "unseen", False, None, "active", "an unseen decision"),
    ("decision", "seen", False, None, "settled", "a seen decision settles"),
    ("decision", "seen", True, None, "active", "a seen decision the person pushed back on doesn't"),
    ("decision", "closed", False, None, "finished", "a closed decision"),
    ("agent", "running", False, None, "active", "a running subagent"),
    ("agent", "done", False, None, "settled", "a finished subagent settles"),
    ("agent", "failed", False, None, "settled", "so does a failed one"),
    ("agent", "failed", False, "failed", "finished", "a failed subagent dismissed"),
    ("permission", "open", False, None, "active", "an open permission"),
    ("permission", "allowed", False, None, "finished", "an allowed permission goes at once, as before"),
    ("permission", "denied", False, None, "finished", "so does a denied one"),
])
def test_where_each_status_stands(kind, status, busy, dismissed, expected, desc):
    item = {"kind": kind, "status": status, "dismissed": dismissed, "session_id": "s", "ref": "X1"}
    assert standing(item, {("s", "X1")} if busy else set()) == expected, desc


@pytest.mark.parametrize("kind, status, dismissed, desc", [
    ("task", "done", "done", "a task done before the upgrade stays off the inbox, as it was"),
    ("task", "dropped", "dropped", "so does a dropped one"),
    ("agent", "done", "done", "and a finished subagent"),
    ("agent", "failed", "failed", "and a failed one"),
    ("task", "running", None, "a running task is left as it is"),
    ("agent", "running", None, "so is a running subagent"),
    ("question", "answered", None, "an answered question was in sight before, and stays so"),
    ("decision", "seen", None, "so was a seen decision"),
])
def test_the_upgrade_keeps_what_was_off_the_inbox_off_it(db_file, store, sid, kind, status, dismissed, desc):
    ref = store.post_item(sid, kind, "old", **(DECIDED if kind == "decision" else {}))
    store.db.execute("UPDATE items SET status = ? WHERE ref = ?", (status, ref))
    store.db.execute("ALTER TABLE items DROP COLUMN dismissed")   # as before the upgrade
    assert Store(db_file).item(sid, ref)["dismissed"] == dismissed, desc
    fresh = store.post_item(sid, kind, "new", **(DECIDED if kind == "decision" else {}))
    store.db.execute("UPDATE items SET status = ? WHERE ref = ?", (status, fresh))
    assert Store(db_file).item(sid, fresh)["dismissed"] is None, f"{desc}: once, at the upgrade"


@pytest.mark.parametrize("kind, change, back, desc", [
    ("task", lambda st, s, r: st.update_item(s, r, status="running"), True, "the session takes the task up again"),
    ("task", lambda st, s, r: st.update_item(s, r, note="shipped"), False, "a note alone leaves it dismissed"),
    ("task", lambda st, s, r: st.dismiss(s, r, False), True, "Delete on it again (F shows it)"),
    ("agent", lambda st, s, r: st.agent_status(s, "a1", "running"), True, "the subagent is resumed"),
])
def test_a_dismissed_item_comes_back(store, sid, kind, change, back, desc):
    if kind == "agent":
        from datetime import datetime, timezone
        store.track_agent(sid, "a1", "Job", "", datetime.now(timezone.utc), "done")
        ref = "A1"
    else:
        ref = store.post_item(sid, kind, "ship", status="done")
    store.dismiss(sid, ref)
    assert standing(store.item(sid, ref)) == "finished", desc
    change(store, sid, ref)
    assert (standing(store.item(sid, ref)) != "finished") == back, desc


async def moved(app, pilot):
    """Until both lists have stopped moving."""
    for _ in range(100):
        if app.session_sink.move is None and app.item_sink.move is None:
            return
        await pilot.pause(0.05)
    raise AssertionError("still moving")


def refs(app) -> list[str]:
    return [k.split("|")[1] for k in app.items_table.keys()]


SETTLE = {
    "task": lambda st, s, r: st.update_item(s, r, status="done"),
    "question": lambda st, s, r: st.reply(s, r, "got it", status="answered"),
    "decision": lambda st, s, r: None,   # seen as it's selected
    "agent": lambda st, s, r: st.update_item(s, r, status="failed"),
}


@pytest.mark.anyio
@pytest.mark.parametrize("kind, desc", [
    ("task", "a task done while selected"),
    ("question", "a question answered while selected"),
    ("decision", "a decision seen as it's selected: it falls below an older settled item, never jumps up"),
    ("agent", "a subagent failed while selected"),
])
async def test_a_settled_item_holds_while_selected_and_sinks_after(store, sid, kind, desc):
    """Doug: "only move items when not selected (so eg the falling animation for completed
    tasks ... should only take place after I move focus to something else)"."""
    start = {"task": "running", "question": "open", "agent": "running"}.get(kind)
    with one_second():
        old = store.post_item(sid, "task", "old one", status="done")
        top = store.post_item(sid, kind, "subject", status=start, **(DECIDED if kind == "decision" else {}))
        q = store.post_item(sid, "question", "another")
    for ref, at in ((old, "09:00"), (top, "10:02"), (q, "10:01")):   # the subject first in its rank
        store.db.execute("UPDATE items SET updated_at = ? WHERE ref = ?", (f"2026-10-09T{at}:00+00:00", ref))
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        table = app.items_table
        before = refs(app)
        assert before[-1] == old, f"{desc}: the settled item at the foot"
        table.move_cursor(row=table.get_row_index(f"{sid}|{top}"))
        await pilot.pause()
        SETTLE[kind](store, sid, top)
        app.refresh_data()
        await pilot.pause()
        assert standing(store.item(sid, top)) == "settled", desc
        assert refs(app) == before and app.item_sink.move is None, f"{desc}: held while selected"
        assert "dim" in str(table.get_row(f"{sid}|{top}")[-1].style), f"{desc}: dimmed at once"
        table.move_cursor(row=table.get_row_index(f"{sid}|{q}"))
        await pilot.pause()
        app.refresh_data()
        assert app.item_sink.move is not None, f"{desc}: sinking once the selection moved on"
        while app.item_sink.move is not None:   # the cursor stays on its item throughout
            assert_one_current(app, f"{desc}: mid-move")
            await pilot.pause(0.03)
        assert refs(app)[-2:] == [old, top], f"{desc}: at the foot, below the older settled item"
        assert app.selected == (sid, q), desc


@pytest.mark.anyio
async def test_delete_moves_at_once(store, sid):
    """D33: closing with Delete is an explicit act, so it moves at once, unanimated."""
    with one_second():
        q = store.post_item(sid, "question", "which db?")
        store.post_item(sid, "question", "which cache?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.items_table.focus()
        app.items_table.move_cursor(row=app.items_table.get_row_index(f"{sid}|{q}"))
        await pilot.pause()
        await pilot.press("f")   # finished items shown: the closed one goes to the foot
        await pilot.press("delete")
        await pilot.pause()
        assert app.item_sink.move is None and refs(app)[-1] == q, "at the foot, at once"


@pytest.mark.anyio
@pytest.mark.parametrize("subject, presses, desc", [
    ("one", 1, "Delete closes an open question: at the foot, at once"),
    ("one", 2, "and again reopens it as answered: with the settled, at once, not held at the foot"),
    ("three", 1, "Delete reopens a closed question: with the settled, at once"),
    ("three", 2, "and again closes it: back at the foot, at once"),
])
async def test_delete_moves_at_once_both_ways(store, sid, subject, presses, desc):
    """D33: closing with Delete moves the item at once, and so does reopening it."""
    from claude_wheelhouse.store import inbox_rank
    with one_second():
        names = {store.post_item(sid, "question", t): t for t in ("one", "two", "three")}
    ref = {t: r for r, t in names.items()}
    store.reply(sid, ref["two"], "x", status="answered")
    store.update_item(sid, ref["three"], status="closed")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.items_table.focus()
        await pilot.press("f")
        await pilot.pause()
        app.items_table.move_cursor(row=app.items_table.get_row_index(f"{sid}|{ref[subject]}"))
        await pilot.pause()
        for _ in range(presses):
            await pilot.press("delete")
            await pilot.pause()
        expected = [names[it["ref"]] for it in sorted(store.items(sid), key=inbox_rank)]
        assert app.item_sink.move is None, f"{desc}: not sinking"
        assert [names[r] for r in refs(app)] == expected, desc
        assert app.selected == (sid, ref[subject]), f"{desc}: still selected"


@pytest.mark.anyio
@pytest.mark.parametrize("kind, status, said, desc", [
    ("task", "done", "dismissed T1", "Delete dismisses a done task"),
    ("agent", "failed", "dismissed A1", "and a failed subagent"),
    ("task", "running", "T1 is a task: its status is the session's to set", "not a running task"),
    ("agent", "running", "A1 is a running subagent: its status is the session's to set", "nor a running subagent"),
])
async def test_delete_dismisses_settled_tasks_and_subagents(store, sid, monkeypatch, kind, status, said, desc):
    ref = store.post_item(sid, kind, "ship", status=status)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        notes = []
        monkeypatch.setattr(app, "notify", lambda text, **kw: notes.append(text))
        app.items_table.focus()
        app.items_table.move_cursor(row=app.items_table.get_row_index(f"{sid}|{ref}"))
        await pilot.pause()
        await pilot.press("delete")
        await pilot.pause()
        assert notes[0].startswith(said), f"{desc}: {notes}"
        gone = said.startswith("dismissed")
        assert (f"{sid}|{ref}" not in app.items_table.rows) == gone, f"{desc}: off the inbox"
        if gone:
            await pilot.press("f")
            await pilot.pause()
            app.items_table.move_cursor(row=app.items_table.get_row_index(f"{sid}|{ref}"))
            await pilot.pause()
            await pilot.press("delete")
            await pilot.pause()
            assert standing(store.item(sid, ref)) == "settled", f"{desc}: Delete on it again brings it back"


@pytest.mark.anyio
@pytest.mark.parametrize("filtered, columns, desc", [
    (False, ["session", "ref", "status", "title"], "every session's items: whose each is"),
    (True, ["ref", "status", "title"], "the inbox following one session: no session column (D31)"),
])
async def test_the_session_column_only_with_every_session(store, sid, filtered, columns, desc):
    store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        if filtered:
            app.follow(sid)
            await pilot.pause()
        table = app.items_table
        assert [str(c.label) for c in table.ordered_columns] == columns, desc
        assert [len(table.get_row(k)) for k in table.keys()] == [len(columns)] * table.row_count, desc
        assert_one_current(app, desc)
        app.action_clear_filter()
        await pilot.pause()
        assert len(table.ordered_columns) == 4, f"{desc}: back with Esc"


def slots(app) -> dict[str, int]:
    return {r.key.value: i for i, r in enumerate(app.session_list.ordered_rows) if not r.key.value.startswith(BLANK)}


@pytest.mark.anyio
async def test_a_parked_session_falls_to_the_foot_and_rises_back(store, sid, tmp_path, monkeypatch):
    """T66: parked sessions sit at the foot of the list's room, falling there as they park,
    rising back into the list as they're unparked; the cursor stays on its session."""
    from claude_wheelhouse import liveness
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "live")
    other = store.create_session(str(tmp_path), name="other")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.refresh_data()   # laid out: the list knows its room
        await pilot.pause()
        table = app.session_list
        room = table.size.height - table.header_height
        assert slots(app) == {sid: 0, other: 1}, "no gap with nothing parked"
        for parked, end, desc in ((True, room - 1, "falls to the foot"), (False, 0, "rises back to the top")):
            store.set_parked(sid, parked)
            app.refresh_data()
            seen = []
            while app.session_sink.move is not None:
                seen.append(slots(app)[sid])
                assert app.current_session() == sid, f"{desc}: the cursor rides its row"
                assert_one_current(app, f"{desc}: mid-move")
                await pilot.pause(0.02)
            seen.append(slots(app)[sid])
            assert len(seen) > 3 and seen == sorted(seen, reverse=not parked), f"{desc}: step by step, {seen}"
            assert seen[-1] == end and table.row_count == (room if parked else 2), desc
        await pilot.press("down")   # never onto a blank row
        assert app.current_session() == other


async def moving(tmp_path, where, keys, typed, count=3, start=None) -> dict:
    """Sessions alpha, bravo and charlie, live, with questions one, two and three (or count of
    one to four), alpha's. A move starts (bravo parks, or question one is answered and sinks),
    then the keys, from the session list on alpha (or start) or the items on two (or start):
    raw as a terminal sends a burst, slowly, both during the move, or after it has ended.
    What they left, by name."""
    from unittest import mock
    from claude_wheelhouse import liveness
    store = Store(tmp_path / "w.db")
    ids = {store.create_session(str(tmp_path), name=n): n for n in ("alpha", "bravo", "charlie")}
    alpha, bravo, _ = ids
    with one_second():
        refs_ = {store.post_item(alpha, "question", t): t for t in ("one", "two", "three", "four")[:count]}
    one = list(refs_)[0]
    # a long move, so the keys land inside it however slowly they're typed
    with mock.patch.object(liveness, "status", lambda s, waking=False: "live"), mock.patch.object(Sink, "SECONDS", 3):
        app = WheelhouseApp(store)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            app.refresh_data()
            await pilot.pause()
            if where == "sessions":
                app.session_list.focus()
                app.session_list.move_cursor(row=[*ids.values()].index(start or "alpha"))
                await pilot.pause()
                store.set_parked(bravo, True)
            else:
                app.items_table.focus()
                ref = next(r for r, t in refs_.items() if t == (start or "two"))
                app.items_table.move_cursor(row=app.items_table.get_row_index(f"{alpha}|{ref}"))
                await pilot.pause()
                store.reply(alpha, one, "got it", status="answered")
            app.refresh_data()
            sink = app.session_sink if where == "sessions" else app.item_sink
            assert sink.move, "under way"
            if typed == "after":
                await moved(app, pilot)
            if typed == "raw":
                raw_keys(app, *keys)
            else:
                for key in keys:
                    await pilot.press(key)
            for _ in range(3):
                await pilot.pause()
            early = sink.move is not None
            assert_one_current(app, f"{where} {keys}: as they land")
            await moved(app, pilot)
            assert_one_current(app, f"{where} {keys}: after the move")
            return {"current": ids.get(app.current_session()), "early": early,
                    "selected": app.selected and refs_.get(app.selected[1]),
                    "marked": sorted(refs_[k.split("|")[1]] for k in app.items_table.marked)}


@pytest.mark.anyio
@pytest.mark.parametrize("where, keys, current, selected, desc", [
    ("sessions", ("down",), "charlie", None, "Down on alpha as bravo falls: charlie, its next in the final order"),
    ("sessions", ("down", "down"), "bravo", None, "Down, Down: bravo, at the foot"),
    ("sessions", ("down", "down", "up"), "charlie", None, "and back up: charlie, never a blank row"),
    ("items", ("down",), "alpha", "three", "Down on two as one sinks: three"),
    ("items", ("up",), "alpha", "two", "Up on two as one sinks: two is first now, so it stays"),
    ("items", ("down", "down"), "alpha", "one", "Down, Down: one, at the foot"),
])
async def test_keys_typed_during_a_move_end_as_typed_slowly(tmp_path, where, keys, current, selected, desc):
    """Arrows step through the final order, never a frame's, so a burst typed during a move
    ends as the same keys typed slowly do (and D22 holds while it moves)."""
    raw = await moving(tmp_path / "raw", where, keys, "raw")
    slow = await moving(tmp_path / "slow", where, keys, "slow")
    assert raw.pop("early") and slow.pop("early"), f"{desc}: the keys landed mid-move"
    assert raw == slow == {"current": current, "selected": selected, "marked": []}, f"{desc}: raw {raw}, slow {slow}"


@pytest.mark.anyio
@pytest.mark.parametrize("where, start, keys, current, selected, marked, desc", [
    ("items", "two", ("shift+up",), "alpha", "two", ["two"], "Shift+Up on two as one sinks: two is first, so only it"),
    ("items", "four", ("shift+down",), "alpha", "one", ["four", "one"], "Shift+Down on four: one, below it now"),
    ("items", "four", ("down", "shift+up"), "alpha", "four", ["four", "one"], "Down then Shift+Up: back to four"),
    ("items", "three", ("pageup",), "alpha", "two", [], "PageUp on three: two, the top now"),
    ("items", "two", ("pagedown",), "alpha", "one", [], "PageDown on two: one, the foot now"),
    ("items", "three", ("ctrl+home",), "alpha", "two", [], "Ctrl+Home: two, the top now"),
    ("items", "two", ("ctrl+end",), "alpha", "one", [], "Ctrl+End: one, the foot now"),
    ("items", "three", ("home", "end"), "alpha", "three", [], "Home and End scroll sideways, the cursor stays"),
    ("sessions", "alpha", ("ctrl+end",), "bravo", None, [], "Ctrl+End as bravo falls: bravo, at the foot"),
    ("sessions", "alpha", ("pagedown",), "bravo", None, [], "PageDown: bravo, at the foot"),
    ("sessions", "alpha", ("pagedown", "pageup"), "alpha", None, [], "and PageUp: alpha, never a blank row"),
    ("sessions", "charlie", ("ctrl+home",), "alpha", None, [], "Ctrl+Home from charlie: alpha"),
])
async def test_every_cursor_key_typed_during_a_move_ends_as_typed_after_it(
        tmp_path, where, start, keys, current, selected, marked, desc):
    """Every key that moves a list's cursor acts in the final order, never a frame's: a burst
    typed during a move ends as the same keys typed once it has ended, so Delete on what they
    marked closes what the person meant."""
    out = {typed: await moving(tmp_path / typed, where, keys, typed, count=4, start=start)
           for typed in ("raw", "slow", "after")}
    assert out["raw"].pop("early"), f"{desc}: the burst landed mid-move"
    out["slow"].pop("early"), out["after"].pop("early")
    expected = {"current": current, "selected": selected, "marked": marked}
    assert out["raw"] == out["slow"] == out["after"] == expected, f"{desc}: {out}"
