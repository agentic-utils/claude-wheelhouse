"""A list's rows as the person sees them, shared by the core's lists and the modules' (through
api.py's re-exports): each item kind's colour, and a row's full text on hover (#111,
.plan/pr-pane.md part 1). Imports no Textual, so the daemon's api import stays light.
"""

import functools

from rich.style import Style
from rich.text import Text

# each kind's ref colour in the inbox (and the PRs pane's PR column): the ref cell is its only
# reader. A kind not here (one newer code knows) draws as before. A doc takes the theme's
# foreground, the default, so it reads on a light theme too; the rest are fixed colours that
# read on the dark and light themes alike.
KIND_COLOUR = {
    "task": "green3",
    "question": "gold3",
    "decision": "medium_purple2",
    "pr": "steel_blue1",
    "doc": "",
    "agent": "grey58",
    "permission": "dark_orange",
}


MARKED = Style(bgcolor="#3a1060")   # rows picked to act on together: the inbox's to close, the PRs pane's (#146)


def marked(cells: tuple) -> tuple:
    """A marked row's cells, on a background distinct from the cursor's."""
    out = tuple(c.copy() if isinstance(c, Text) else Text(str(c)) for c in cells)
    for c in out:   # the base style, which the table also pads the cell with
        c.style = (Style.parse(c.style) if isinstance(c.style, str) else c.style) + MARKED
    return out


def plain(cell):
    """A cell as its own text, never markup: a DataTable parses a str cell as markup, so a
    session's title or name holding [@click=...] ran an action when clicked (#139)."""
    return Text(cell) if isinstance(cell, str) else cell


class KeepColours:
    """A DataTable mixin: the mouse's row and the cursor's keep their cells' own colours (a
    session's status dot, an item's status, a PR's light) on the hover's or the cursor's
    background. The hover style takes the table's colour (MATRIX) and the cursor's white, and
    with the foreground's priority on the CSS, DataTable laid it over every cell, so every
    status went green under the mouse and white under the cursor (#217 for the PRs pane). A
    cell with no colour of its own still takes theirs. Cached as DataTable's is, which it
    clears by this name when its looks change: DataTable's own, unwrapped, so there is the one
    cache to clear. A list changes what DataTable is asked through cell_styles (SinkList)."""

    @functools.lru_cache(maxsize=32)
    def _get_styles_to_render_cell(self, *args) -> tuple[Style, Style]:
        style, post = self.cell_styles(*args)
        return style, Style.from_color(bgcolor=post.bgcolor)

    def cell_styles(self, *args) -> tuple[Style, Style]:
        """DataTable's styles for a cell, uncached."""
        return super()._get_styles_to_render_cell.__wrapped__(self, *args)


class RowTips:
    """A DataTable mixin: the row under the pointer's full text as the table's tooltip, which
    a narrow column cuts off. A list says what that is for a row in `tips`, by row key, as it
    paints, or overrides `row_tip(row)` (its index; None or '' for none). The tooltip changes
    only when the pointer's row does; tip_again() asks again for the row under it, when the
    rows change under a still pointer. The pointer's row is its own (tip_row), not the hover
    shade's: DataTable moves that only while the cursor shows, so a table with its cursor
    hidden tipped the first row wherever the pointer was (#184)."""

    tips: dict = {}   # row key: its full text, set per instance as the list paints
    tip_row = -1      # the row under the pointer, -1 for none (the header, or below the rows)

    def row_tip(self, row: int) -> str | None:
        return self.tips.get(self.coordinate_to_cell_key((row, 0)).row_key.value)

    def _on_mouse_move(self, event) -> None:
        # Textual runs every class's _on_mouse_move in turn, DataTable's too: no super() call
        row = event.style.meta.get("row", -1)
        if row != self.tip_row:
            self.tip_row = row
            self.tip_again()

    def tip_again(self) -> None:
        row = self.tip_row
        try:
            text = self.row_tip(row) if 0 <= row < self.row_count else None
        except (IndexError, KeyError):
            text = None
        self.tooltip = Text(text) if text else None   # plain: a str tooltip is markup (plain)
