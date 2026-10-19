"""The tour's callout (callout.py): where it goes, and a test app of fake panes it follows."""

import pytest
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.geometry import Region, Size
from textual.screen import ModalScreen
from textual.widgets import Static

from claude_wheelhouse.callout import HIGHLIGHT, TourCallout, place

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
async def test_every_pane_resolves_in_the_real_app(store):
    """Review of #86: a PANES selector left at an id the six slots changed finds no pane, or
    the wrong one, and the callout sits centred, describing nothing."""
    from claude_wheelhouse.callout import PANES
    from claude_wheelhouse.tui import WheelhouseApp

    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        for pane, (selector, _) in PANES.items():
            assert app.screen.query_one(selector).parent.has_class("slot"), f"{pane}: {selector} is the pane in its slot"
