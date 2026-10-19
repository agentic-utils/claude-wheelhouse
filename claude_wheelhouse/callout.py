"""The tour's callout (.plan/tutorial-overhaul.md, "Painting a callout over its pane"): a box
painted beside the pane a tour dialog describes, that pane's border highlighted. Not a modal:
the screen under it stays usable, so the keys a dialog teaches reach the app.

It finds its pane by id each time it places itself (find), so a pane moved to another slot
takes its callout with it, and it places itself again whenever the screen's layout changes
(a resize, a pane swapped, a splitter dragged). Where it goes is a pure function, place.
"""

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.geometry import Offset, Region, Size
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, Input, Static, TextArea

WIDTH = 46   # a callout's width, on a screen wide enough
MARGIN = 4   # the columns a narrower screen keeps free beside it
COLLAPSED = 3   # a pane fewer rows high than this shows nothing a callout could sit over
EDITABLE = (Input, TextArea)   # what a Next callout leaves focus in
HIGHLIGHT = "tour-target"   # the class on the pane a callout describes: a yellow border

# a tour pane id (whatsnew.Stop.pane): the selector of its widget on the screen and its title.
# The one lookup (find), by the panes' ids in their slots (.plan/modules.md)
PANES = {
    "sessions": ("#sessions-pane", "Sessions"),
    "session-info": ("#session-info-pane", "Session info"),
    "items": ("#items-pane", "Inbox"),
    "detail": ("#detail-pane", "Detail"),
    "stats": ("#stats", "Stats"),
}

SIDES = ("right", "left", "below", "above")   # tried in turn, beside the pane; then inside, then centred
# the border title, pointing from the callout at its pane
POINTERS = {"right": "◀ {}", "left": "{} ▶", "below": "▲ {}", "above": "▼ {}", "inside": "{}", "centred": ""}


def width(screen: Size) -> int:
    return max(1, min(WIDTH, screen.width - MARGIN))


def place(screen: Size, pane: Region | None, height: int, clear: Region | None = None,
          keep: Region | None = None) -> tuple[Region, str]:
    """Where a callout `height` rows high goes for a pane at `pane` (screen coordinates; None:
    absent), and how. One rule (#101): of the candidates in turn (spot's choice, beside the
    pane, inside it or centred; then below every inbox row, `clear`; below the demo's rows,
    `keep`; the screen's foot; its top), the first on screen that covers neither the demo's
    rows nor the pane (but inside it, as spot put it there); with none, the one covering least of the demo's rows, then of the pane."""
    first, side = spot(screen, pane, height)
    spots = candidates(screen, first, clear, keep)
    if side == "inside":   # placed over its pane by design: only the demo's rows to keep clear of
        pane = None

    def covers(r: Region, of: Region | None) -> int:
        return r.intersection(of).area if of is not None else 0

    clear_of = [r for r in spots if not covers(r, keep) and not covers(r, pane)]
    return (clear_of[0] if clear_of else min(spots, key=lambda r: (covers(r, keep), covers(r, pane)))), side


def candidates(screen: Size, first: Region, clear: Region | None, keep: Region | None) -> list[Region]:
    """place's candidates, in order, those on screen: `first`, then the same box below every
    inbox row, below the demo's, at the screen's foot and at its top."""
    tops = [first.y] + [r.bottom for r in (clear, keep) if r is not None] + [screen.height - first.height, 0]
    spots = [Region(first.x, y, first.width, first.height) for y in tops]
    return [r for r in spots if r.y >= 0 and r.bottom <= screen.height] or [first]


def spot(screen: Size, pane: Region | None, height: int) -> tuple[Region, str]:
    """place's first candidate: beside the pane on the first side where it fits without
    covering it (right, left, below, above), else inside it over its top rows, else, for a
    pane narrower than the callout or collapsed, centred on the screen, a row higher for the
    line naming the pane."""
    w = width(screen)

    def clamp(value: int, low: int, high: int) -> int:
        return max(low, min(value, high))

    if pane is not None:
        y = clamp(pane.y, 0, screen.height - height)   # level with the pane's top, kept on screen
        x = clamp(pane.x, 0, screen.width - w)
        tall = height <= screen.height
        beside = {
            "right": (Region(pane.right, y, w, height), tall and pane.right + w <= screen.width),
            "left": (Region(pane.x - w, y, w, height), tall and pane.x - w >= 0),
            "below": (Region(x, pane.bottom, w, height), pane.bottom + height <= screen.height),
            "above": (Region(x, pane.y - height, w, height), pane.y - height >= 0),
        }
        for side in SIDES:
            region, fits = beside[side]
            if fits:
                return region, side
        if pane.width >= w and pane.height >= COLLAPSED:   # below the pane's top border, which names it
            return Region(pane.x + (pane.width - w) // 2, clamp(pane.y + 1, 0, screen.height - height),
                          w, height), "inside"
    height += 1
    return Region((screen.width - w) // 2, max(0, (screen.height - height) // 2), w, height), "centred"


def rows(screen: Widget, sid: str | None = None) -> tuple[Region | None, Region | None]:
    """The inbox's rows on the screen, its header included, and those down to the last of
    session `sid`'s (the demo's): what a callout for another pane keeps clear of (place), so
    the items it asks for stay in sight. None for either that isn't there."""
    found = screen.query("#items")
    if not found or not (table := found.first()).display or not table.region.area:
        return None, None
    region = table.region

    ordered = table.ordered_rows
    header = table.header_height if table.show_header else 0

    def down_to(n: int) -> Region | None:
        """The rows from the header to the nth, as far as they're in sight."""
        bottom = region.y + header + sum(row.height for row in ordered[:n]) - table.scroll_offset.y
        bottom = min(region.bottom, bottom)
        return Region(region.x, region.y, region.width, bottom - region.y) if n and bottom > region.y else None

    mine = [i for i, row in enumerate(ordered) if sid and str(row.key.value).startswith(f"{sid}|")]
    return down_to(len(ordered)), down_to(mine[-1] + 1) if mine else None


def find(screen: Widget, pane: str) -> Widget | None:
    """The pane's widget on the screen, wherever it sits, or None while it's absent."""
    selector = PANES.get(pane, (f"#{pane}", pane))[0]
    found = screen.query(selector)
    return found.first() if found else None


class TourCallout(Vertical):
    """One tour dialog painted beside its pane. With `next` it takes focus, on its Next button,
    so Enter is Next (Next posted), unless a text box has it (a click on Next then); without,
    it never takes focus, so the keys it teaches reach
    the app. Hidden while another screen covers the one it's on."""
    SCOPED_CSS = False   # the highlight is on the pane, outside the callout
    DEFAULT_CSS = f"""
    TourCallout {{ overlay: screen; width: {WIDTH}; height: auto; padding: 0 1; background: #12122a;
                   color: #e8e8e8; border: round #ffd300; border-title-color: #ffd300; }}
    TourCallout .tour-text {{ width: 1fr; height: auto; }}
    TourCallout Button {{ width: auto; min-width: 0; }}
    .{HIGHLIGHT} {{ border: round #ffd300 !important; }}
    """

    class Next(Message):
        def __init__(self, callout: "TourCallout") -> None:
            super().__init__()
            self.callout = callout

    def __init__(self, pane: str, text: str, next: bool = False, id: str | None = None, keep: str | None = None) -> None:
        super().__init__(id=id)
        self.pane, self.text, self.next = pane, text, next
        self.keep = keep   # the session whose rows in the inbox it never covers (place): the demo
        self.target: Widget | None = None
        self.placed: tuple | None = None   # what it was last placed against
        self.side = ""

    def compose(self) -> ComposeResult:
        yield Static(Text(self.text), classes="tour-text")   # plain: [Launch] is a button's name, not markup
        if self.next:
            yield Button("Next [Enter]", id="tour-next", compact=True, variant="primary")

    def on_mount(self) -> None:
        self.screen.screen_layout_refresh_signal.subscribe(self, lambda _: self.place())
        self.app.screen_change_signal.subscribe(self, lambda _: self.cover(), immediate=True)
        self.place()
        found = self.query("#tour-next")   # none in one removed before it was composed
        if found and not isinstance(self.screen.focused, EDITABLE):   # never from a box being typed in
            found.first().focus()

    def on_unmount(self) -> None:
        self.highlight(None)

    def say(self, text: str) -> None:
        """Its text changed: placed again, as its height may have."""
        self.text, self.placed = text, None
        self.place()

    def cover(self) -> None:
        """Shown only while its own screen is the one on top."""
        self.display = self.app.screen is self.screen

    def highlight(self, target: Widget | None) -> None:
        if self.target is not None and self.target is not target:
            self.target.remove_class(HIGHLIGHT)
        if target is not None:
            target.add_class(HIGHLIGHT)
        self.target = target

    def lines(self, w: int, text: str) -> int:
        """Its height at width w: the text wrapped inside border and padding, the Next row, the border."""
        return len(Text(text).wrap(self.app.console, max(1, w - 4))) + self.next + 2

    def place(self) -> None:
        """Beside its pane, as place says, again only once the screen or the pane has moved.
        Nothing while it has no text to place (not composed yet, or being removed)."""
        if not self.query(".tour-text"):
            return
        screen = self.screen.size
        target = find(self.screen, self.pane)
        region = target.region if target is not None and target.display else None
        clear, keep = rows(self.screen, self.keep)
        if self.placed == (screen, region, clear, keep):
            return
        self.placed = (screen, region, clear, keep)
        self.highlight(target if region is not None else None)
        title = PANES.get(self.pane, ("", self.pane))[1]
        w = width(screen)
        at, self.side = place(screen, region, self.lines(w, self.text), clear, keep)
        text = self.text
        if self.side == "centred":   # its pane named first, a line that wraps on a narrow screen
            text = f"In the {title} pane:\n{self.text}"
            at, _ = place(screen, None, self.lines(w, text) - 1, clear, keep)
        self.query_one(".tour-text", Static).update(Text(text))
        self.border_title = POINTERS[self.side].format(title) or None
        self.styles.width, self.styles.height = at.width, at.height
        self.absolute_offset = Offset(at.x, at.y)
        self.refresh(layout=True)

    @on(Button.Pressed, "#tour-next")
    def pressed(self, event: Button.Pressed) -> None:
        event.stop()   # the app's own buttons answer a press that bubbles up
        self.post_message(self.Next(self))
