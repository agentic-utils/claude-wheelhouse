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
from textual.widgets import Button, Static

WIDTH = 46   # a callout's width, on a screen wide enough
MARGIN = 4   # the columns a narrower screen keeps free beside it
COLLAPSED = 3   # a pane fewer rows high than this shows nothing a callout could sit over
HIGHLIGHT = "tour-target"   # the class on the pane a callout describes: a yellow border

# a tour pane id (whatsnew.Stop.pane): the selector of its widget on the screen and its title.
# The one lookup (find): modules stage 1 points it at its slots' pane ids
PANES = {
    "sessions": ("#sessions-pane", "Sessions"),
    "session-info": ("#session-info", "Session info"),
    "items": ("#items-pane", "Inbox"),
    "detail": ("#detail-pane", "Detail"),
    "stats": ("#stats", "Stats"),
}

SIDES = ("right", "left", "below", "above")   # tried in turn, beside the pane; then inside, then centred
# the border title, pointing from the callout at its pane
POINTERS = {"right": "◀ {}", "left": "{} ▶", "below": "▲ {}", "above": "▼ {}", "inside": "{}", "centred": ""}


def width(screen: Size) -> int:
    return max(1, min(WIDTH, screen.width - MARGIN))


def place(screen: Size, pane: Region | None, height: int) -> tuple[Region, str]:
    """Where a callout `height` rows high goes for a pane at `pane` (screen coordinates; None:
    absent), and how: beside it on the first side where it fits without covering it (right,
    left, below, above), else inside it over its top rows, else, for a pane narrower than the
    callout or collapsed, centred on the screen, a row higher for the line naming the pane."""
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


def find(screen: Widget, pane: str) -> Widget | None:
    """The pane's widget on the screen, wherever it sits, or None while it's absent."""
    selector = PANES.get(pane, (f"#{pane}", pane))[0]
    found = screen.query(selector)
    return found.first() if found else None


class TourCallout(Vertical):
    """One tour dialog painted beside its pane. With `next` it takes focus, on its Next button,
    so Enter is Next (Next posted); without, it never takes focus, so the keys it teaches reach
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

    def __init__(self, pane: str, text: str, next: bool = False, id: str | None = None) -> None:
        super().__init__(id=id)
        self.pane, self.text, self.next = pane, text, next
        self.target: Widget | None = None
        self.placed: tuple | None = None   # what it was last placed against
        self.side = ""

    def compose(self) -> ComposeResult:
        yield Static(self.text, classes="tour-text")
        if self.next:
            yield Button("Next [Enter]", id="tour-next", compact=True, variant="primary")

    def on_mount(self) -> None:
        self.screen.screen_layout_refresh_signal.subscribe(self, lambda _: self.place())
        self.app.screen_change_signal.subscribe(self, lambda _: self.cover(), immediate=True)
        self.place()
        if self.next:
            self.query_one("#tour-next", Button).focus()

    def on_unmount(self) -> None:
        self.highlight(None)

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
        """Beside its pane, as place says, again only once the screen or the pane has moved."""
        screen = self.screen.size
        target = find(self.screen, self.pane)
        region = target.region if target is not None and target.display else None
        if self.placed == (screen, region):
            return
        self.placed = (screen, region)
        self.highlight(target if region is not None else None)
        title = PANES.get(self.pane, ("", self.pane))[1]
        w = width(screen)
        spot, self.side = place(screen, region, self.lines(w, self.text))
        text = self.text
        if self.side == "centred":   # its pane named first, a line that wraps on a narrow screen
            text = f"In the {title} pane:\n{self.text}"
            spot, _ = place(screen, None, self.lines(w, text) - 1)
        self.query_one(".tour-text", Static).update(text)
        self.border_title = POINTERS[self.side].format(title) or None
        self.styles.width, self.styles.height = spot.width, spot.height
        self.absolute_offset = Offset(spot.x, spot.y)
        self.refresh(layout=True)

    @on(Button.Pressed, "#tour-next")
    def pressed(self, event: Button.Pressed) -> None:
        event.stop()   # the app's own buttons answer a press that bubbles up
        self.post_message(self.Next(self))
