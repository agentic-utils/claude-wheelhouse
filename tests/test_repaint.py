"""The partial screen update (repaint.py, #385): only what a refresh touches is rendered, and
written from its first cell, with the same cells on screen as Textual's own update."""
import hashlib
import inspect
import re

import pytest
from textual._compositor import ChopsUpdate, Compositor
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.geometry import Region
from textual.widgets import Static

from claude_wheelhouse import repaint
from claude_wheelhouse.tui import WheelhouseApp

MOVE = re.compile(r"\x1b\[(\d+);(\d+)H")
SGR = re.compile(r"(\x1b\[[0-9;]*m)")


class Rows(App):
    """Three full-width lines and a narrow box beside a fourth."""
    CSS = "Static { height: 1; } #left { width: 20; } #right { width: 1fr; } Horizontal { height: 1; }"

    def compose(self) -> ComposeResult:
        yield Static("t" * 80, id="top")
        yield Static("m" * 80, id="middle")
        yield Static("b" * 80, id="bottom")
        with Horizontal():
            yield Static("l" * 20, id="left")
            yield Static("r" * 60, id="right")


class Layered(App):
    """A bordered, padded box of two lines, and an overlay on a layer above, across the box's
    top border and its first line."""
    CSS = """Screen { layers: base top; }
    #box { border: solid red; padding: 1 2; height: 8; width: 40; background: #202020; }
    #box Static { height: 1; background: #303030; }
    #over { layer: top; offset: 10 0; width: 8; height: 3; background: blue; }"""

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Static("x" * 34, id="a")
            yield Static("y" * 34, id="b")
        yield Static("OOOOOOOO\nOOOOOOOO\nOOOOOOOO", id="over")


def cells(data: str) -> dict[tuple[int, int], tuple[str, str]]:
    """What an update writes: each cell's character and the style it is written in, by (x, y)
    from 0."""
    out, parts = {}, MOVE.split(data)
    for i in range(1, len(parts), 3):
        y, x = int(parts[i]) - 1, int(parts[i + 1]) - 1
        style = ""
        for piece in SGR.split(parts[i + 2].replace("\n", "")):
            if SGR.fullmatch(piece):
                style = "" if piece in ("\x1b[0m", "\x1b[m") else piece
                continue
            for ch in piece:
                out[(x, y)] = (ch, style)
                x += 1
    return out


def update(app, regions, which) -> tuple[str, set[str]]:
    """The update for `regions` by `which` (textual, ours): what it writes, and the widgets it rendered."""
    partial, segments = repaint.TEXTUAL if which == "textual" else (repaint.render_partial_update, repaint.render_segments)
    compositor = app.screen._compositor
    rendered = set()
    widgets = list(app.screen.query("Static, Horizontal, Vertical"))
    for widget in widgets:
        lines = widget.render_lines
        widget.render_lines = lambda crop, w=widget, lines=lines: (rendered.add(w.id or "row"), lines(crop))[1]
    compositor._dirty_regions.update(regions)
    try:
        data = segments(partial(compositor), app.console)
    finally:
        for widget in widgets:
            del widget.render_lines
    return data, rendered


def same_as_textual(app, regions, desc) -> set[str]:
    """Our update for `regions` writes exactly the refreshed cells, each as Textual's own update
    writes it; the widgets it rendered."""
    ours, drew = update(app, regions, "ours")
    theirs, _ = update(app, regions, "textual")
    wanted = {(x, y) for r in regions for y in r.line_range for x in range(r.x, r.right)}
    assert set(cells(ours)) == wanted, f"{desc}: the refreshed cells, no more"
    wrong = {at: (cells(theirs).get(at), got) for at, got in cells(ours).items() if cells(theirs).get(at) != got}
    assert not wrong, f"{desc}: as Textual draws them, (Textual's, ours): {sorted(wrong.items())[:6]}"
    assert len(ours) <= len(theirs), desc
    return drew


@pytest.mark.anyio
@pytest.mark.parametrize("regions, rendered, desc", [
    ([Region(30, 0, 4, 1)], {"top"}, "a few cells of one line"),
    ([Region(30, 0, 4, 1), Region(5, 2, 2, 1)], {"top", "bottom"},
     "two lines apart: the line between is not rendered"),
    ([Region(25, 3, 3, 1)], {"right"}, "cells inside one widget: that widget alone, not its container"),
    ([Region(0, 0, 80, 1), Region(18, 3, 4, 1)], {"top", "left", "right", "row"}, "a whole line, and cells across two widgets"),
])
async def test_a_partial_update_renders_and_writes_only_what_it_touches(regions, rendered, desc):
    app = Rows()
    async with app.run_test(size=(80, 6)) as pilot:
        await pilot.pause()
        assert same_as_textual(app, regions, desc) == rendered, desc


@pytest.mark.anyio
@pytest.mark.parametrize("regions, desc", [
    ([Region(0, 0, 40, 8)], "the bordered box refreshed whole: its children and the overlay over it drawn"),
    ([Region(1, 1, 38, 6)], "inside the box's border: its padding and children, not the box's background over them"),
    ([Region(4, 2, 30, 1)], "a child's line under the overlay: the overlay's cells kept"),
    ([Region(5, 0, 10, 1)], "the box's top border, the overlay over part of it"),
    ([Region(2, 1, 6, 1)], "the box's padding alone: the box's own"),
    ([Region(20, 3, 4, 1)], "inside a child, nothing over it: the child's own"),
    ([Region(12, 1, 2, 1), Region(30, 6, 3, 1)], "the overlay's cells, and the box's padding far below"),
])
async def test_a_partial_update_over_containers_and_layers_writes_what_textual_does(regions, desc):
    app = Layered()
    async with app.run_test(size=(50, 10)) as pilot:
        await pilot.pause()
        same_as_textual(app, regions, desc)


@pytest.mark.anyio
async def test_focus_moves_in_the_app_update_the_screen_as_textual_does(store, sid, monkeypatch):
    """Tab and Shift+Tab mark the focused panel (a bordered container refreshed whole): every
    partial update matches Textual's own, cell for cell."""
    updates, wrong = [], []

    def checked(self):
        regions = set(self._dirty_regions)
        theirs = repaint.TEXTUAL[0](self)
        self._dirty_regions.update(regions)
        ours = repaint.render_partial_update(self)
        if theirs and ours:
            got, want = cells(repaint.render_segments(ours, app.console)), cells(repaint.TEXTUAL[1](theirs, app.console))
            updates.append(regions)
            if diff := [(at, want.get(at), cell) for at, cell in got.items() if want.get(at) != cell]:
                wrong.append((sorted(regions)[:3], diff[:6], len(diff)))
        return ours

    monkeypatch.setattr(Compositor, "render_partial_update", checked)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        for key in ["tab", "tab", "tab", "shift+tab", "tab", "shift+tab"]:
            await pilot.press(key)
            await pilot.pause()
    assert updates, "the focus moves drew partial updates"
    assert not wrong, f"partial updates unlike Textual's: {wrong[:4]}"


@pytest.mark.parametrize("original, digest, desc", [
    (repaint.TEXTUAL[0], "888dccc892d8ecc2", "Compositor.render_partial_update, replaced"),
    (repaint.TEXTUAL[1], "4c1cdb567e0b8354", "ChopsUpdate.render_segments, replaced"),
    (Compositor._render_chops, "ca8c3359d7afd017", "Compositor._render_chops, which repaint._render_chops follows"),
    (Compositor._get_renders, "8ac93299aa5d3bef", "Compositor._get_renders, which repaint._touched follows"),
])
def test_the_textual_code_repaint_stands_in_for_is_unchanged(original, digest, desc):
    """repaint.py replaces two of Textual's methods and leans on its internals (Compositor's
    _dirty_regions, _regions_to_spans, cuts, visible_widgets, get_widget_at; ChopsUpdate's
    chops, spans, chop_ends). Textual is pinned; when it moves, this fails until repaint.py is
    checked against the new code."""
    got = hashlib.sha256(inspect.getsource(original).encode()).hexdigest()[:16]
    assert got == digest, f"{desc} changed in this Textual: re-check claude_wheelhouse/repaint.py against it, then update the digest to {got}"
    assert ChopsUpdate.render_segments is repaint.render_segments, "repaint installed"
