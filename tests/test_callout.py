"""The tour's callout (callout.py): where it goes, and a test app of fake panes it follows."""

import pytest
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.geometry import Region, Size
from textual.screen import ModalScreen
from textual.widgets import Static

from claude_wheelhouse.callout import POINTERS, covers, facing, HIGHLIGHT, TourCallout, place, widths

S = Size(80, 24)


@pytest.mark.parametrize("screen, pane, height, region, side, desc", [
    (S, Region(0, 0, 30, 24), 6, Region(30, 0, 46, 6), "right", "right of a pane on the left"),
    (S, Region(50, 0, 30, 24), 6, Region(4, 0, 46, 6), "left", "left of a pane on the right"),
    (S, Region(20, 0, 40, 10), 6, Region(20, 10, 46, 6), "below", "below a wide pane at the top"),
    (S, Region(20, 14, 40, 10), 6, Region(20, 8, 46, 6), "above", "above a wide pane at the bottom"),
    (S, Region(0, 20, 30, 4), 6, Region(30, 18, 46, 6), "right", "level with the pane, kept on screen"),
    (S, Region(30, 0, 50, 24), 6, Region(32, 1, 46, 6), "inside", "inside a pane with no room beside it"),
    (S, Region(20, 0, 40, 24), 6, Region(17, 8, 46, 7), "centred", "centred: the pane narrower than the callout"),
    (S, Region(30, 10, 50, 2), 20, Region(17, 1, 46, 21), "centred", "centred: the pane collapsed"),
    (S, None, 6, Region(17, 8, 46, 7), "centred", "centred: no pane"),
    (S, Region(0, 1, 32, 21), 6, Region(32, 1, 46, 6), "right", "80 by 24: the sessions column"),
    (S, Region(32, 1, 48, 21), 6, Region(33, 2, 46, 6), "inside", "80 by 24: the right-hand column"),
    (Size(40, 24), None, 6, Region(2, 8, 36, 7), "centred", "a narrow screen keeps four columns free"),
])
def test_placement(screen, pane, height, region, side, desc):
    assert place(screen, pane, height) == (region, side), desc


@pytest.mark.parametrize("screen, expected, desc", [
    (Size(160, 40), [46, 156], "46 wide, else the screen's width less the margin"),
    (S, [46, 76], "80 by 24: 46, else 76"),
    (Size(48, 20), [44], "a screen narrower than 46 and the margin: its width less the margin alone"),
])
def test_widths(screen, expected, desc):
    """#133: the widths a callout tries, narrowest first; a wider one wraps its text to fewer rows."""
    assert widths(screen) == expected, desc


@pytest.mark.parametrize("w, height, clear, desc", [
    (46, 23, False, "46 wide, 23 rows: nowhere on screen clear of the row at y 4"),
    (76, 12, True, "76 wide, 12 rows: clear of it"),
])
def test_a_wider_box_clears_the_row(w, height, clear, desc):
    """#133: the same text at 80x24 over a narrow pane, centred: at 46 wide it is too tall to
    keep clear of the step's row; wider, and so shorter, it is."""
    keep = Region(41, 4, 10, 1)
    region, _ = place(S, Region(40, 1, 12, 11), height, keep, keep, centre=True, w=w)
    assert region.width == w and (not covers(region, keep)) is clear, f"{desc}: {region}"


ROWS = Region(33, 2, 12, 6)   # the demo's rows in the inbox (callout.rows), left of a pane on the right
EVERY = Region(33, 2, 12, 20)   # every row, too many to go below
RIGHT = Region(50, 0, 30, 24)


@pytest.mark.parametrize("pane, height, clear, keep, region, desc", [
    (RIGHT, 6, None, None, Region(4, 0, 46, 6), "nothing to keep clear of: spot's choice"),
    (RIGHT, 6, ROWS, ROWS, Region(4, 8, 46, 6), "over the demo's rows: below them"),
    (RIGHT, 6, EVERY, ROWS, Region(4, 8, 46, 6), "below every row doesn't fit: below the demo's"),
    (RIGHT, 6, None, Region(0, 12, 4, 4), Region(4, 0, 46, 6), "clear of them already: unmoved"),
    (RIGHT, 20, EVERY, ROWS, Region(4, 4, 46, 20), "nowhere clear: the least over them, the foot"),
    (Region(20, 14, 40, 10), 6, None, Region(20, 2, 10, 10), Region(20, 12, 46, 6),
     "none clear of both: off the demo's rows, the least over its pane"),
    (Region(30, 0, 50, 24), 6, None, Region(32, 1, 10, 4), Region(32, 5, 46, 6), "inside its pane: below the rows there"),
    (None, 6, None, Region(17, 6, 10, 4), Region(17, 10, 46, 7), "centred too: the same search"),
])
def test_placement_keeps_clear(pane, height, clear, keep, region, desc):
    assert place(S, pane, height, clear, keep)[0] == region, f"{desc} (#101 6)"


P = Region(20, 8, 20, 6)   # a pane in the middle


@pytest.mark.parametrize("box, side, desc", [
    (Region(40, 8, 10, 4), "right", "level, to its right"),
    (Region(5, 9, 15, 4), "left", "level, to its left"),
    (Region(22, 14, 10, 4), "below", "under it"),
    (Region(22, 0, 10, 8), "above", "over it"),
    (Region(40, 16, 10, 4), "below-right", "rows below and to the right: not just right (Stats, #101)"),
    (Region(0, 0, 10, 4), "above-left", "up and to the left"),
    (Region(22, 9, 10, 2), "inside", "over it"),
])
def test_facing(box, side, desc):
    assert facing(box, P) == side, desc
    assert POINTERS[side].format("x"), desc


def test_the_nearest_clear_candidate_wins():
    """#101: below the demo's rows is nearer the pane than the screen's foot."""
    pane = Region(50, 0, 30, 24)
    region, side = place(S, pane, 6, None, Region(4, 0, 46, 3))
    assert (region, side) == (Region(4, 3, 46, 6), "left"), "the first clear spot, touching the pane"
    assert place(S, Region(0, 0, 30, 6), 6, None, Region(30, 0, 46, 8)) == (Region(30, 8, 46, 6), "below-right"), \
        "moved down off the rows: pointing up and back at the pane"


def test_shunning_a_pane_beside_it_puts_the_box_right_below():
    """Doug (#183): with no room right of the inbox clear of Detail, the box goes right below
    the inbox, not to the screen's foot."""
    inbox, detail = Region(46, 1, 45, 22), Region(91, 1, 69, 39)
    region, side = place(Size(160, 40), inbox, 11, Region(47, 2, 43, 8), Region(47, 6, 43, 1), shun=detail)
    assert (region.y, side) == (inbox.bottom, "below"), f"{region} under {inbox}"


class Panes(App):
    """Three fake panes, by the main screen's ids, side by side."""
    CSS = "Horizontal { height: 1fr; } .pane { height: 1fr; border: round #7b61ff; } " \
          "#sessions-pane { width: 30; } #items-pane { width: 1fr; } #detail-pane { width: 30; }"

    def __init__(self, callout: TourCallout):
        super().__init__()
        self.callout = callout
        self.nexts = []

    def compose(self) -> ComposeResult:
        with Horizontal(id="columns"):
            for pane in ("sessions-pane", "items-pane", "detail-pane"):
                yield Static(pane, id=pane, classes="pane")
        yield self.callout

    def on_tour_callout_next(self, message: TourCallout.Next) -> None:
        self.nexts.append(message.callout.pane)


def pane_of(app: App, pane: str) -> Region:
    return app.query_one(f"#{pane}").region


@pytest.mark.parametrize("pane, change, side, desc", [
    ("sessions", None, "right", "beside its pane"),
    ("detail", None, "left", "the other side of a pane on the right"),
    ("sessions", "swap", "left", "a pane swapped into another slot takes its callout with it"),
    ("sessions", "resize", "right", "placed again on a resize"),
    ("sessions", "drag", "right", "placed again when a splitter drag moves its pane"),
])
@pytest.mark.anyio
async def test_a_callout_follows_its_pane(pane, change, side, desc):
    selector = {"sessions": "sessions-pane", "detail": "detail-pane"}[pane]
    app = Panes(TourCallout(pane, "Start a session: press N."))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        if change == "swap":
            app.query_one("#columns").move_child(app.query_one("#sessions-pane"), after=app.query_one("#detail-pane"))
        elif change == "resize":
            await pilot.resize_terminal(140, 40)
        elif change == "drag":
            app.query_one("#sessions-pane").styles.width = 50
        await pilot.pause()
        await pilot.pause()
        target, callout = pane_of(app, selector), app.callout.region
        expect, how = place(app.screen.size, target, callout.height)
        assert (callout, app.callout.side) == (expect, how) and how == side, f"{desc}: {callout} by {target}"
        assert app.query_one(f"#{selector}").has_class(HIGHLIGHT), f"{desc}: its pane highlighted"
        assert [w.id for w in app.query(f".{HIGHLIGHT}")] == [selector], f"{desc}: and no other"


@pytest.mark.parametrize("next, focused, desc", [
    (True, "tour-next", "a Next callout takes focus, so Enter is Next"),
    (False, None, "an action callout never takes focus, so its keys reach the app"),
])
@pytest.mark.anyio
async def test_focus_and_next(next, focused, desc):
    app = Panes(TourCallout("items", "Highlight Q1.", next=next))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        assert getattr(app.focused, "id", None) == focused, desc
        await pilot.press("enter")
        await pilot.pause()
        assert app.nexts == (["items"] if next else []), desc


@pytest.mark.parametrize("width, desc", [(120, "one line naming the pane"), (24, "that line wrapped on a narrow screen")])
@pytest.mark.anyio
async def test_a_centred_callout_holds_its_whole_text(width, desc):
    """Review of #92: the line naming the pane, counted as one row, wrapped and cut the last."""
    text = "Questions, decisions and tasks land here. Highlight Q1."
    app = Panes(TourCallout("stats", text))   # no stats pane: centred
    async with app.run_test(size=(width, 30)) as pilot:
        await pilot.pause()
        c = app.callout
        assert c.side == "centred", desc
        assert c.region.height == c.lines(c.region.width, f"In the Stats pane:\n{text}"), desc
        shown = " ".join("".join(s.text for s in app.screen._compositor.render_strips()).split())
        assert "Highlight" in shown and "Q1." in shown, f"{desc}: {shown}"


@pytest.mark.anyio
async def test_hidden_under_another_screen_and_unhighlighted_once_gone():
    app = Panes(TourCallout("items", "Highlight Q1."))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.push_screen(ModalScreen())
        await pilot.pause()
        assert not app.callout.display, "hidden while a dialog covers its screen"
        app.pop_screen()
        await pilot.pause()
        assert app.callout.display, "shown again once it's uncovered"
        await app.callout.remove()
        await pilot.pause()
        assert not app.query(f".{HIGHLIGHT}"), "its pane's highlight goes with it"


@pytest.mark.anyio
async def test_the_next_steps_callout_keeps_the_highlight():
    """Review of #104: the decision step's callout leaving after the docs step's, on the same
    pane, mounted, leaves that pane highlighted."""
    app = Panes(TourCallout("items", "Decisions."))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await app.mount(TourCallout("items", "Documents."))
        await pilot.pause()
        await app.callout.remove()
        await pilot.pause()
        assert HIGHLIGHT in app.query_one("#items-pane").classes, "kept: the next step's callout holds it"

@pytest.mark.anyio
async def test_every_pane_resolves_in_the_real_app(store, every_module):   # design: its module loaded
    """Review of #86: a PANES selector left at an id the six slots changed finds no pane, or
    the wrong one, and the callout sits centred, describing nothing."""
    from claude_wheelhouse.callout import PANES
    from claude_wheelhouse.tui import WheelhouseApp

    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        for pane, (selector, _) in PANES.items():
            assert app.screen.query_one(selector).parent.has_class("slot"), f"{pane}: {selector} is the pane in its slot"
