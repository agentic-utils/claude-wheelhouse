"""A list's rows as the person sees them, shared by the core's lists and the modules' (through
api.py's re-exports): each item kind's colour, and a row's full text on hover (#111,
.plan/pr-pane.md part 1). Imports no Textual, so the daemon's api import stays light.
"""

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


class RowTips:
    """A DataTable mixin: the row under the pointer's full text as the table's tooltip, which
    a narrow column cuts off. A list says what that is for a row in `tips`, by row key, as it
    paints, or overrides `row_tip(row)` (its index; None or '' for none). The tooltip changes
    only when the pointer's row does; tip_again() asks again for the row under it, when the
    rows change under a still pointer."""

    tips: dict = {}   # row key: its full text, set per instance as the list paints

    def row_tip(self, row: int) -> str | None:
        return self.tips.get(self.coordinate_to_cell_key((row, 0)).row_key.value)

    def watch_hover_coordinate(self, old, value) -> None:
        super().watch_hover_coordinate(old, value)
        if old.row != value.row:
            self.tip_again()

    def tip_again(self) -> None:
        row = self.hover_coordinate.row
        try:
            text = self.row_tip(row) if 0 <= row < self.row_count else None
        except (IndexError, KeyError):
            text = None
        self.tooltip = Text(text) if text else None   # plain: a str tooltip is markup (plain)
