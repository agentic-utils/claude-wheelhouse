"""The design module's Decisions pane in the app (modules/design): here, not with the module's own
tests, for the app's fixtures (conftest.py). Skipped when it isn't installed."""

import pytest

from claude_wheelhouse import api
from claude_wheelhouse.tui import LAYOUT, DocView, WheelhouseApp, tab_stops
from test_tui import INBOX_STOPS, assert_one_current, by_name, pane_widget, pick, stop_id

design = pytest.importorskip("claude_wheelhouse_design", reason="installed without the design module")
from claude_wheelhouse_design import pane  # noqa: E402


def repo(root, files, where="docs/adr"):
    """A git repo at root with files in `where`."""
    (root / ".git").mkdir(parents=True, exist_ok=True)
    d = root / where
    d.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (d / name).write_text(text)
    return d


@pytest.fixture
def decisions(store, sid, tmp_path, monkeypatch):
    """The session's directory a repo with three decisions, and the design module the only one."""
    d = repo(tmp_path, {"0001-ACCEPTED-use-sqlite.md": "# Use SQLite\n\nWe chose SQLite.\n",
                        "0002-PROPOSED-drop-redis.md": "# Drop Redis\n\nRedis goes.\n",
                        "0003-ACCEPTED-wal.md": "# WAL\n\nWrite-ahead logging.\n"})
    monkeypatch.setattr(api, "load", lambda: [api.Loaded("design", design.module)])
    return d


async def settled(app, pilot):
    """The pane's asks answered, after a tick: the first one, as it mounts, comes before a
    hub in process has read its sessions, so the tick asks again once it lands."""
    await pilot.pause()
    app.each_pane("tick")
    p = app.query_one("#design")
    for _ in range(50):
        await app.workers.wait_for_complete()
        await pilot.pause()
        if not p.asking:
            break
    await pilot.pause()


def numbers(app) -> list[str]:
    table = app.query_one("#decision-list")
    return [table.get_row_at(i)[0] for i in range(table.row_count)]


@pytest.mark.anyio
@pytest.mark.both
async def test_the_pane_lists_and_shows_decisions(store, sid, decisions):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        p = app.query_one("#design")
        assert p.parent.id == "slot-middle-bottom" and p.border_title == "Decisions", "its slot and title"
        assert numbers(app) == ["0002", "0003", "0001"], "PROPOSED first, then newest first"
        before = app._detail_text
        table = app.query_one("#decision-list")
        table.focus()
        await pilot.pause()
        assert app._detail_text == before, "focus alone shows nothing"
        await pilot.press("down")
        await pilot.pause()
        assert "Write-ahead logging." in app._detail_text and "0003 WAL" in app._detail_text, "the cursor's decision"
        assert not app.query_one("#answer").display, "a doc: no answer box"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DocView) and "Write-ahead logging." in app.screen.text, "Enter: full screen"
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, DocView)


@pytest.mark.anyio
async def test_a_header_sorts_and_a_new_decision_shows(store, sid, decisions):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        table = app.query_one("#decision-list")
        column = table.ordered_columns[0]
        table.post_message(table.HeaderSelected(table, column.key, 0, column.label))
        await pilot.pause()
        assert numbers(app) == ["0001", "0002", "0003"] and str(table.ordered_columns[0].label) == "# ▲", \
            "number ▲, marked in its header"
        (decisions / "0004-UNRESOLVED-x.md").write_text("# X\n")
        p = app.query_one("#design")
        p.tick()
        await settled(app, pilot)
        assert numbers(app) == ["0001", "0002", "0003"], "a tick before EVERY seconds asks nothing"
        p.asked_at -= pane.EVERY
        p.tick()
        await settled(app, pilot)
        assert numbers(app) == ["0001", "0002", "0003", "0004"], "the next ask, EVERY seconds on, finds it"


@pytest.mark.anyio
async def test_tab_stops_at_the_decisions_after_the_inbox(store, sid, decisions):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        ids = [w.id for w, _ in tab_stops(app.screen)]
        assert ids[ids.index("items") + 1] == "decision-list", "after the inbox"
        assert ids.index("decision-list") < ids.index("answer"), "before the answer box"


@pytest.mark.anyio
async def test_no_decisions_says_where_it_looked(store, sid, tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(api, "load", lambda: [api.Loaded("design", design.module)])
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        note = app.query_one("#decisions-note")
        assert note.display and "docs/adr" in str(note.render()), "the empty pane says why"


@pytest.mark.anyio
async def test_the_shipped_slots_and_a_swap_into_the_new_one(store, sid, tmp_path, every_module):
    """Every module loaded (#111): PRs under the inbox in the middle column's third slot,
    Decisions under it; a pane dragged onto the new slot swaps with PRs, and is kept so."""
    import json
    from claude_wheelhouse.tui import SLOTS_KEY
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        assert app.layout_now() == {"left.top": "sessions", "left.middle": "stats", "left.bottom": "session-info",
                                    "middle.top": "items", "middle.middle": "prs", "middle.bottom": "design",
                                    "right": "detail"}, "the default seven"
        app.swap(pane_widget(app, "design").parent, pane_widget(app, "prs").parent)
        await pilot.pause()
        assert (app.layout_now()["middle.middle"], app.layout_now()["middle.bottom"]) == ("design", "prs"), "swapped"
        assert json.loads(store.setting(SLOTS_KEY))["middle.middle"] == "design", "and kept"


# the shipped default: every module loaded (conftest.every_module), not the stats pane alone

ADRS = {"0001-ACCEPTED-use-sqlite.md": "# Use SQLite\n", "0002-PROPOSED-drop-redis.md": "# Drop Redis\n"}
PANES = ["sessions", "stats", "session-info", "items", "design", "detail"]


@pytest.fixture
def shipped(store, sid, tmp_path, every_module):
    """Every module installed loaded, as a default install runs, and the session's repo with two decisions."""
    return repo(tmp_path, ADRS)


@pytest.mark.anyio
async def test_the_tab_order_with_every_module(store, sid, shipped, monkeypatch):
    by_name(monkeypatch, {"demo": "live"})
    store.post_item(sid, "question", "one?")
    store.queue(sid, "queued")
    order = ["session-list", "items", "decision-list", "answer", "tour-next", *INBOX_STOPS[3:], "session-list"]
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 60)) as pilot:
        await settled(app, pilot)
        await pick(app, pilot, sid)
        from claude_wheelhouse.tour import Tour
        app.tour = Tour(False)
        app.tour.at = [d.id for d in app.tour.stops].index("stats")
        app.show_tour()
        await pilot.pause()
        app.session_list.focus()
        await pilot.pause()
        seen = [stop_id(app)]
        for _ in order[1:]:
            await pilot.press("tab")
            seen.append(stop_id(app))
        assert seen == order, f"the inbox's lists, the Decisions', the box, Next, the buttons, the stats': {seen}"
        assert_one_current(app, "every module")


@pytest.mark.anyio
async def test_reset_brings_the_decisions_home(store, sid, shipped):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        app.swap(pane_widget(app, "design").parent, pane_widget(app, "stats").parent)
        await pilot.pause()
        assert (app.layout_now()["left.middle"], app.layout_now()["middle.bottom"]) == ("design", "stats"), "swapped"
        app.action_reset_layout()
        await pilot.pause()
        assert (app.layout_now()["left.middle"], app.layout_now()["middle.bottom"]) == ("stats", "design"), "home"


@pytest.mark.anyio
@pytest.mark.parametrize("onto", [None, *(p for p in PANES if p != "design")])
async def test_every_decisions_swap_fits_80_by_24(store, sid, shipped, onto):
    """The default with every module, and the Decisions pane swapped with each other pane:
    each within its slot and on screen, and the Decisions more than its border wherever it
    goes (its PANE_MINIMUM). The stats pane gives way to a row, as in its own slot."""
    app = WheelhouseApp(store)
    async with app.run_test(size=(80, 24)) as pilot:
        await settled(app, pilot)
        if onto:
            app.swap(pane_widget(app, "design").parent, pane_widget(app, onto).parent)
        await pilot.pause()
        await pilot.pause()
        for each in PANES:
            widget = pane_widget(app, each)
            assert widget.parent.region.contains_region(widget.region), f"<->{onto}: {each} within its slot"
            assert widget.region.right <= 80 and widget.region.bottom <= 24, f"<->{onto}: {each} on screen"
            if each != "stats":
                assert widget.region.height >= 3, f"<->{onto}: {each} more than its border"


@pytest.mark.anyio
@pytest.mark.parametrize("onto", [None, "items"])
async def test_no_decisions_shrinks_the_pane_to_its_note(store, sid, tmp_path, middle_of_two, onto):
    """No decisions found: the pane keeps its border and the note, and the inbox, the other
    pane in its column, takes the rest; once one appears, its share again. Swapped with the
    inbox too."""
    beside = "items"
    (tmp_path / ".git").mkdir()
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        if onto:
            app.swap(pane_widget(app, "design").parent, pane_widget(app, onto).parent)
        await pilot.pause()
        await pilot.pause()
        p, other = app.query_one("#design"), pane_widget(app, beside)
        column = p.parent.parent
        assert p.compact and p.region.height == 3, f"{onto}: the border and the note"
        assert "docs/adr" in str(app.query_one("#decisions-note").render()), f"{onto}: the note says where it looked"
        shown = [w for w in column.children if w.display]
        assert sum(w.outer_size.height for w in shown) == column.size.height, f"{onto}: no gap in the column"
        roomy = other.region.height
        repo(tmp_path, ADRS)
        p.asked_at -= pane.EVERY
        await settled(app, pilot)
        await pilot.pause()
        assert not p.compact and p.region.height > 3 and numbers(app) == ["0002", "0001"], f"{onto}: its share again"
        assert other.region.height < roomy, f"{onto}: {beside} gives it back"


@pytest.mark.anyio
@pytest.mark.parametrize("adrs", [False, True])
@pytest.mark.parametrize("onto", [None, *(p for p in PANES if p != "design")])
async def test_no_column_has_blank_rows_wherever_the_decisions_go(store, sid, tmp_path, middle_of_two, onto, adrs):
    """Swapped with each other pane, with and without decisions found: every column filled
    by its slots and every slot by its panes. With none found the pane shrinks to its note
    in any column of two slots or more (#111: one rule); alone in its column (Detail's) it
    keeps its size."""
    repo(tmp_path, ADRS if adrs else {})
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        if onto:
            app.swap(pane_widget(app, "design").parent, pane_widget(app, onto).parent)
        await pilot.pause()
        await pilot.pause()
        for column in app.query(".column"):
            if column.display:
                shown = sum(w.outer_size.height for w in column.children if w.display)
                assert shown == column.size.height, f"<->{onto}, adrs {adrs}: no gap in #{column.id}"
        for each in PANES:
            widget = pane_widget(app, each)
            assert widget.outer_size.height == widget.parent.size.height, f"<->{onto}, adrs {adrs}: {each} fills its slot"
        p = app.query_one("#design")
        paired = onto != "detail"   # in any column of two slots or more, not alone (#111)
        assert p.compact is not adrs, f"<->{onto}: compact only with none found"
        assert (p.region.height == 3) is (not adrs and paired), f"<->{onto}, adrs {adrs}: shrunk only beside another slot"
        squeezed = [s.id for s in app.query(".slot") if s.has_class("-compact")]
        assert squeezed == ([p.parent.id] if not adrs and paired else []), f"<->{onto}, adrs {adrs}: {squeezed}"


@pytest.mark.anyio
async def test_a_drag_beside_a_compact_pane_is_not_kept(store, sid, tmp_path, middle_of_two):
    """No splitter lies beside the Decisions shrunk to their note (columns.pairs: a compact
    slot has none), so nothing is dragged or kept; once a decision appears the inbox has the
    share it had."""
    from test_tui import splitter
    repo(tmp_path, {})
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        await pilot.pause()
        p, inbox = app.query_one("#design"), pane_widget(app, "items")
        assert p.compact and p.region.height == 3, "shrunk to its note"
        assert not splitter(app, "middle-bottom").display, "no splitter beside it"
        assert inbox.region.height + 3 == app.query_one("#col-middle").size.height, "the inbox has the rest, no splitter row"
        assert app.client.setting(LAYOUT + "weights") is None, "nothing stored"
        repo(tmp_path, ADRS)
        p.asked_at -= pane.EVERY
        await settled(app, pilot)
        await pilot.pause()
        assert not p.compact and inbox.region.height > 7, f"the inbox its default share: {inbox.region.height}"


@pytest.mark.anyio
@pytest.mark.parametrize("name, moved", [("left-bottom", "session-info")])
async def test_a_drag_in_the_left_column_with_the_empty_decisions_on_top(store, sid, tmp_path, every_module,
                                                                       name, moved):
    """Review r3, and #111's one rule: the Decisions, none found, swapped onto the Sessions'
    slot (left.top) shrink to their note there too, with no splitter beside them; the one
    between the other two drags and is kept."""
    from textual import events
    from test_tui import splitter
    repo(tmp_path, {})
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        app.swap(pane_widget(app, "design").parent, pane_widget(app, "sessions").parent)
        await pilot.pause()
        await pilot.pause()
        p, widget = app.query_one("#design"), pane_widget(app, moved)
        assert p.compact and p.parent.id == "slot-left-top" and p.region.height == 3, "at its note on left.top"
        assert not splitter(app, "left-middle").display, "no splitter beside it"
        sp = splitter(app, name)
        before, height = sp.fraction, widget.region.height
        x, y = sp.region.offset
        await pilot.mouse_down(sp)
        app.post_message(events.MouseMove(None, x, y - 4, 0, -4, button=1, shift=False, meta=False,
                                          ctrl=False, screen_x=x, screen_y=y - 4))
        await pilot.pause()
        await pilot.mouse_up(None, (x, y - 4))
        await pilot.pause()
        assert widget.region.height == height + 4, f"{name}: dragged 4 rows ({height} before)"
        assert app.client.setting(LAYOUT + "weights") is not None, f"{name}: kept"


@pytest.mark.anyio
async def test_the_note_fits_one_row_when_compact_comes_without_a_resize(store, sid, tmp_path, middle_of_two):
    """Review r3: the slot dragged to its least rows while decisions show, then the last one
    goes: no resize comes, and the note still says what fits on its row, with an ellipsis."""
    from claude_wheelhouse.splitter import NUDGE
    from test_tui import splitter
    d = repo(tmp_path, ADRS)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        p, note = app.query_one("#design"), app.query_one("#decisions-note")
        bottom = app.query_one("#slot-middle-bottom").outer_size.height   # dragged down to their border and a row (#111)
        app.drag_column("col-middle", "middle.top", "middle.bottom", bottom - 3, dict(app.weights))
        await pilot.pause()
        await pilot.pause()
        assert not p.compact and p.region.height == 3 and not note.has_class("-one-row"), "dragged small"
        for f in d.iterdir():
            f.unlink()
        p.asked_at -= pane.EVERY
        await settled(app, pilot)
        await pilot.pause()
        assert p.compact and p.region.height == 3, "compact, at the same size"
        assert note.has_class("-one-row"), "the note cut to its row"
        repo(tmp_path, ADRS)
        p.asked_at -= pane.EVERY
        await settled(app, pilot)
        await pilot.pause()
        assert not p.compact and not note.has_class("-one-row"), "and back"


@pytest.mark.anyio
@pytest.mark.parametrize("adrs", [False, True])
async def test_a_failed_ask_keeps_the_pane_as_it_was(store, sid, tmp_path, every_module, adrs):
    """A failed ask neither grows the pane shrunk to its note nor shrinks one with decisions:
    the error shows in the note."""
    repo(tmp_path, ADRS if adrs else {})
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        await pilot.pause()
        p = app.query_one("#design")
        height = p.region.height
        p.got({"error": "boom"})
        await pilot.pause()
        await pilot.pause()
        assert p.compact is not adrs and p.region.height == height, f"adrs {adrs}: as it was"
        assert "boom" in str(app.query_one("#decisions-note").render()), f"adrs {adrs}: the error in the note"


@pytest.mark.anyio
async def test_a_decision_edits_in_detail_as_queued_with_every_module(store, sid, shipped):
    """The shipped default: a decision shown in Detail opens its editor on a click, and as a
    doc of no session's, Ctrl+Enter only re-renders (Queued), never saving."""
    store.set_mode(sid, "immediate")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        app.query_one("#decision-list").focus()
        await pilot.press("down")
        await pilot.pause()
        region = app.detail_scroll.scrollable_content_region
        row = next(y for y, t in enumerate(app.detail._layout(app.detail.size.width)[1]) if "Use SQLite" in t)
        await pilot.click(offset=(region.x + 2, region.y + row))
        await pilot.pause()
        await pilot.pause()
        editor = app.detail_editor
        assert editor.display and editor.has_focus and editor.cursor_location == (0, 0), "open on the heading"
        notes = []
        app.notify = lambda message, **_: notes.append(message)
        editor.insert("Edited. ")
        await pilot.press("ctrl+j")
        await pilot.pause()
        assert "Edited. # Use SQLite" in app._detail_text and notes == [], "re-rendered, not saved: Queued"
