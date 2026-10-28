"""Since you were away (#223, away.py): the card's query over seeded rows, what the hub notes
of sessions going quiet, and the card in the app."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from claude_wheelhouse import away
from claude_wheelhouse.hub import Hub
from claude_wheelhouse.tui import AwayCard, KeysHelp, WheelhouseApp

NOW = datetime(2026, 10, 28, 17, 0, tzinfo=timezone.utc)
START, END = NOW - timedelta(hours=3), NOW   # the window
IN, BEFORE, AFTER = NOW - timedelta(hours=1), NOW - timedelta(hours=5), NOW + timedelta(minutes=5)


def seed(store, sid, kind, title, status, created, updated=None):
    """An item posted, then dated: created and updated as given (updated defaults to created)."""
    if kind == "pr":
        ref = store.post_pr(sid, "https://github.com/o/r/pull/108", title)
    else:
        ref = store.post_item(sid, kind, title, **({"why": "it was quicker"} if kind == "decision" else {}))
    store.db.execute("UPDATE items SET status = ?, created_at = ?, updated_at = ? WHERE session_id = ? AND ref = ?",
                     (status, away.stamp(created), away.stamp(updated or created), sid, ref))
    return ref


@pytest.mark.parametrize("kind, status, created, updated, group, desc", [
    ("question", "open", IN, None, away.NEEDS, "an open question asked in the gap needs you"),
    ("permission", "open", IN, None, away.NEEDS, "an approval prompt in the gap needs you"),
    ("task", "blocked", BEFORE, IN, away.NEEDS, "a task blocked in the gap needs you"),
    ("task", "waiting", BEFORE, IN, away.NEEDS, "a task waiting in the gap needs you"),
    ("pr", "merged", BEFORE, IN, away.LANDED, "a PR merged in the gap landed"),
    ("task", "done", BEFORE, IN, away.LANDED, "a task done in the gap landed"),
    ("decision", "unseen", IN, None, "Decided without you (1)", "a decision in the gap, counted"),
    ("question", "open", BEFORE, None, None, "a question asked before the gap: left out"),
    ("question", "open", AFTER, None, None, "a question asked after the return: left out"),
    ("question", "answered", IN, None, None, "a question already answered: nothing waits"),
    ("task", "done", BEFORE, BEFORE, None, "a task done before the gap: left out"),
    ("pr", "open", IN, None, None, "a PR opened, not merged: left out"),
    ("decision", "unseen", BEFORE, IN, None, "a decision made before the gap: left out"),
    ("task", "running", IN, None, None, "a task merely running: left out"),
])
def test_the_card_groups_what_happened_in_the_gap(store, sid, kind, status, created, updated, group, desc):
    ref = seed(store, sid, kind, "the thing", status, created, updated)
    groups = away.card(store.all_items(), {}, lambda s: False, START, END)
    assert [(g.title, [(ln.sid, ln.ref) for ln in g.lines]) for g in groups] == \
        ([(group, [(sid, ref)])] if group else []), desc


@pytest.mark.parametrize("quiet, running, shown, desc", [
    ({"how": "parked", "at": IN}, False, ["demo parked"], "parked in the gap"),
    ({"how": "ended", "at": IN}, False, ["demo ended"], "ended in the gap"),
    ({"how": "stopped", "at": IN}, False, ["demo stopped"], "stopped (crashed) in the gap"),
    ({"how": "stopped", "at": IN}, True, [], "stopped, but running again now: not quiet"),
    ({"how": "parked", "at": BEFORE}, False, [], "parked before the gap: left out"),
])
def test_went_quiet_lists_sessions_quiet_now(quiet, running, shown, desc):
    noted = {"s1": {"name": "demo", "how": quiet["how"], "at": away.stamp(quiet["at"])}}
    groups = away.card([], noted, lambda s: running, START, END)
    assert [ln.text for g in groups for ln in g.lines] == shown, desc
    assert all(g.title == away.QUIET_GROUP and g.lines[0].ref is None for g in groups), desc


def test_each_group_caps_its_lines_newest_first(store, sid):
    for n in range(5):
        seed(store, sid, "question", f"q{n}", "open", IN + timedelta(minutes=n))
    (group,) = away.card(store.all_items(), {}, lambda s: False, START, END)
    assert [ln.text.split(" ")[1] for ln in group.shown()] == ["q4", "q3", "q2"]
    assert group.more() == 2


@pytest.mark.parametrize("before, after, how, desc", [
    ({"s": ("a", False, True)}, {"s": ("a", True, True)}, "parked", "parked while running"),
    ({"s": ("a", False, False)}, {"s": ("a", True, False)}, "parked", "parked while dead"),
    ({"s": ("a", False, True)}, {}, "ended", "its row gone"),
    ({"s": ("a", False, True)}, {"s": ("a", False, False)}, "stopped", "alive, then dead"),
    ({"s": ("a", True, True)}, {"s": ("a", True, False)}, None, "parked, then its process stops: parked already"),
    ({"s": ("a", False, False)}, {"s": ("a", False, True)}, None, "relaunched: nothing went quiet"),
    (None, {}, None, "the first read: nothing to compare"),
])
def test_observe_notes_sessions_going_quiet(before, after, how, desc):
    got = away.observe(before, after, {}, NOW)
    assert (got or {}) == ({"s": {"name": "a", "how": how, "at": away.stamp(NOW)}} if how else {}), desc


def test_observe_drops_entries_past_kept():
    old = {"x": {"name": "x", "how": "ended", "at": away.stamp(NOW - away.KEPT - timedelta(minutes=1))}}
    assert away.observe({"s": ("a", False, True)}, {}, old, NOW) == \
        {"s": {"name": "a", "how": "ended", "at": away.stamp(NOW)}}


def test_the_hub_keeps_what_went_quiet(store, sid, monkeypatch):
    monkeypatch.setattr(away, "now", lambda: NOW)
    other = store.create_session("/tmp", name="other")
    hub = Hub(store)
    hub.refresh()
    store.set_parked(sid, True)
    store.end(other)
    hub.refresh()
    quiet = away.kept(store.setting(away.QUIET))
    assert {k: v["how"] for k, v in quiet.items()} == {sid: "parked", other: "ended"}
    assert hub.away()["quiet"] == quiet


def test_a_new_hub_notes_what_went_quiet_while_none_ran(store, sid, monkeypatch):
    monkeypatch.setattr(away, "now", lambda: NOW)
    Hub(store).refresh()   # the last hub, before the night
    store.end(sid)
    Hub(store).refresh()   # the next start's
    assert {k: v["how"] for k, v in away.kept(store.setting(away.QUIET)).items()} == {sid: "ended"}


def card_lines(app) -> list[str]:
    card = app.query_one("#away", AwayCard)
    return [str(card.get_option_at_index(i).prompt).strip() for i in range(card.option_count)]


@pytest.mark.anyio
@pytest.mark.both
async def test_the_card_shows_after_a_gap_and_esc_dismisses_it(store, sid, monkeypatch):
    t = [NOW]
    monkeypatch.setattr(away, "now", lambda: t[0])
    store.set_setting(away.LAST_INPUT, away.stamp(NOW - timedelta(minutes=10)))
    app = WheelhouseApp(store)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert not app.query_one("#away-card").display, "no gap at start: no card"
        ref = seed(store, sid, "question", "which db?", "open", NOW + timedelta(hours=1))
        t[0] = NOW + timedelta(hours=3)   # three hours with no input, then a key
        await pilot.press("f5")
        await pilot.pause()
        await pilot.pause()
        assert app.query_one("#away-card").display, "the first key after the gap shows the card"
        assert card_lines(app) == ["Needs you", f"{ref} which db? · demo"]
        assert store.item(sid, ref)["status"] == "open", "seeing the card marks nothing seen"
        app.query_one("#answer").focus()
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert not app.query_one("#away-card").display, "Esc dismisses it, from an empty answer box too"


@pytest.mark.anyio
@pytest.mark.both
async def test_an_esc_that_wakes_the_screen_leaves_the_card_up(store, sid, monkeypatch):
    t = [NOW]
    monkeypatch.setattr(away, "now", lambda: t[0])
    store.set_setting(away.LAST_INPUT, away.stamp(NOW - timedelta(minutes=10)))
    app = WheelhouseApp(store)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        seed(store, sid, "question", "which db?", "open", NOW + timedelta(hours=1))
        t[0] = NOW + timedelta(hours=3)
        await pilot.press("escape")
        await pilot.pause()
        await pilot.pause()
        assert app.query_one("#away-card").display, "the Esc that brought the card doesn't dismiss it"
        await pilot.press("escape")
        await pilot.pause()
        assert not app.query_one("#away-card").display, "the next Esc does"
        t[0] = NOW + timedelta(hours=4)   # card dismissed; seen again over a second gap with a dialog up
        await pilot.press("f1")
        await pilot.pause()
        assert isinstance(app.screen, KeysHelp)
        seed(store, sid, "question", "which port?", "open", NOW + timedelta(hours=5))
        t[0] = NOW + timedelta(hours=8)
        await pilot.press("escape")
        await pilot.pause()
        await pilot.pause()
        assert not isinstance(app.screen, KeysHelp), "the waking Esc still closes the dialog it was aimed at"
        assert app.query_one("#away-card").display, "and leaves the card up"
        seed(store, sid, "question", "which host?", "open", NOW + timedelta(hours=9))
        t[0] = NOW + timedelta(hours=12)   # a third gap with the card still up: the waking Esc keeps it
        await pilot.press("escape")
        await pilot.pause()
        await pilot.pause()
        assert app.query_one("#away-card").display, "an Esc over a second gap doesn't dismiss the refreshed card"


@pytest.mark.anyio
@pytest.mark.both
async def test_a_start_after_a_gap_shows_the_card_and_a_line_jumps(store, sid, monkeypatch):
    monkeypatch.setattr(away, "now", lambda: NOW)
    store.set_setting(away.LAST_INPUT, away.stamp(START))
    ref = seed(store, sid, "task", "ship it", "done", IN)
    app = WheelhouseApp(store)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.pause()
        assert card_lines(app) == ["Landed", f"{ref} ship it · demo"], "a start is a return too"
        card = app.query_one("#away", AwayCard)
        assert app.focused is card, "it takes the focus when no text box has it"
        await pilot.press("enter")
        await pilot.pause()
        assert app.selected == (sid, ref), "Enter on a line selects its item, as an inbox row does"
        assert app.query_one("#away-card").display, "jumping leaves the card"
        await pilot.click("#away-close")
        await pilot.pause()
        assert not app.query_one("#away-card").display, "its close control dismisses it"
    assert away.parse(store.setting(away.LAST_INPUT)) == NOW, "the last input kept"


@pytest.mark.anyio
async def test_the_card_goes_once_every_needs_you_line_is_dealt_with(store, sid, monkeypatch):
    monkeypatch.setattr(away, "now", lambda: NOW)
    store.set_setting(away.LAST_INPUT, away.stamp(START))
    ref = seed(store, sid, "question", "which db?", "open", IN)
    seed(store, sid, "task", "ship it", "done", IN)
    app = WheelhouseApp(store)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.query_one("#away-card").display
        store.db.execute("UPDATE items SET status = 'answered' WHERE session_id = ? AND ref = ?", (sid, ref))
        app.refresh_data()
        await pilot.pause()
        assert not app.query_one("#away-card").display, "its only Needs-you line answered: gone"


@pytest.mark.anyio
async def test_the_card_fits_80_by_24(store, sid, monkeypatch):
    monkeypatch.setattr(away, "now", lambda: NOW)
    store.set_setting(away.LAST_INPUT, away.stamp(START))
    for n in range(5):
        seed(store, sid, "question", f"question {n}", "open", IN)
        seed(store, sid, "task", f"task {n}", "done", IN)
        seed(store, sid, "decision", f"decision {n}", "unseen", IN)
    store.set_setting(away.QUIET, json.dumps({"gone": {"name": "old", "how": "ended", "at": away.stamp(IN)}}))
    app = WheelhouseApp(store)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.pause()
        card, items, pane = app.query_one("#away-card"), app.query_one("#items"), app.query_one("#items-pane")
        assert card.display
        assert pane.region.contains_region(card.region), "the card within the inbox pane"
        assert card.region.height <= pane.region.height // 2 + 1, "at most half the inbox"
        assert items.region.height >= 3, "the inbox's rows still show under it"
        assert "+2 more" in card_lines(app), "a group past PER_GROUP says how many more"
