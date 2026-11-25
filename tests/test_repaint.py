"""The partial screen update (repaint.py, #385): only what a refresh touches is rendered, and
written from its first cell, with the same cells on screen as Textual's own update."""
import re

import pytest
from textual.app import App, ComposeResult
from textual.geometry import Region
from textual.widgets import Static

from claude_wheelhouse import repaint

MOVE = re.compile(r"\x1b\[(\d+);(\d+)H")
SGR = re.compile(r"\x1b\[[0-9;]*m")


class Rows(App):
    """Three full-width lines and a narrow box beside a fourth."""
    CSS = "Static { height: 1; } #left { width: 20; } #right { width: 1fr; } Horizontal { height: 1; }"

    def compose(self) -> ComposeResult:
        from textual.containers import Horizontal
        yield Static("t" * 80, id="top")
        yield Static("m" * 80, id="middle")
        yield Static("b" * 80, id="bottom")
        with Horizontal():
            yield Static("l" * 20, id="left")
            yield Static("r" * 60, id="right")


def cells(data: str) -> dict[tuple[int, int], str]:
    """What an update writes: each cell's character, by (x, y) from 0."""
    out, parts = {}, MOVE.split(data)
    for i in range(1, len(parts), 3):
        y, x = int(parts[i]) - 1, int(parts[i + 1]) - 1
        for ch in SGR.sub("", parts[i + 2]).replace("\n", ""):
            out[(x, y)] = ch
            x += 1
    return out


def update(app, regions, which) -> tuple[str, set[str]]:
    """The update for `regions` by `which` (textual, ours): what it writes, and the widgets it rendered."""
    partial, segments = repaint.TEXTUAL if which == "textual" else (repaint.render_partial_update, repaint.render_segments)
    compositor = app.screen._compositor
    rendered = set()
    for widget in app.screen.query("Static, Horizontal"):
        lines = widget.render_lines
        widget.render_lines = lambda crop, w=widget, lines=lines: (rendered.add(w.id or "row"), lines(crop))[1]
    compositor._dirty_regions.update(regions)
    try:
        data = segments(partial(compositor), app.console)
    finally:
        for widget in app.screen.query("Static, Horizontal"):
            del widget.render_lines
    return data, rendered


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
        ours, drew = update(app, regions, "ours")
        theirs, _ = update(app, regions, "textual")
        wanted = {(x, y) for r in regions for y in r.line_range for x in range(r.x, r.right)}
        assert set(cells(ours)) == wanted, f"{desc}: the refreshed cells, no more"
        assert all(cells(theirs)[at] == ch for at, ch in cells(ours).items()), f"{desc}: as Textual draws them"
        assert drew == rendered, desc
        assert len(ours) <= len(theirs), desc
