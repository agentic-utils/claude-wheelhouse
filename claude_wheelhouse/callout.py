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
    "design": ("#design", "Decisions"),   # the design module's pane
}

SIDES = ("right", "left", "below", "above")   # tried in turn, beside the pane; then inside, then centred
# the border title, pointing from the callout at its pane, by where the box ends up (facing)
POINTERS = {"right": "◀ {}", "left": "{} ▶", "below": "▲ {}", "above": "▼ {}", "below-right": "◤ {}",
            "below-left": "{} ◥", "above-right": "◣ {}", "above-left": "{} ◢", "inside": "{}", "centred": ""}


def width(screen: Size) -> int:
    return max(1, min(WIDTH, screen.width - MARGIN))


def covers(r: Region, of: Region | None) -> int:
    """How much of `of` the box `r` covers, in cells."""
    return r.intersection(of).area if of is not None else 0


def gap(r: Region, pane: Region | None) -> int:
    """The box's distance from the pane, edge to edge: 0 touching or over it."""
    if pane is None:
        return 0
    return max(0, pane.x - r.right, r.x - pane.right) + max(0, pane.y - r.bottom, r.y - pane.bottom)


def facing(r: Region, pane: Region | None) -> str:
    """Which side of the pane the box sits on, for its pointer (POINTERS)."""
    if pane is None:
        return "centred"
    across = "right" if r.x >= pane.right else "left" if r.right <= pane.x else ""
    down = "below" if r.y >= pane.bottom else "above" if r.bottom <= pane.y else ""
    return "-".join(filter(None, (down, across))) or "inside"


def place(screen: Size, pane: Region | None, height: int, clear: Region | None = None,
          keep: Region | None = None, centre: bool = False) -> tuple[Region, str]:
    """Where a callout `height` rows high goes for a pane at `pane` (screen coordinates; None:
    absent), and the side it then points from (facing; "centred" with `centre`, the box
    centred as spot puts it for a pane too narrow or collapsed). One rule (#101): of the
    candidates (spot's choice; then below every inbox row, `clear`; below the step's own rows
    of the demo, `keep`; the screen's foot; its top), those on screen that cover neither `keep`
    nor the pane (bar a box spot put inside it), the nearest the pane; with none, the one
    covering least of `keep`, then of the pane."""
    first, side = spot(screen, None if centre else pane, height)
    avoid = None if side == "inside" else pane
    spots = candidates(screen, first, clear, keep)
    clear_of = [r for r in spots if not covers(r, keep) and not covers(r, avoid)]
    best = min(clear_of, key=lambda r: gap(r, pane)) if clear_of \
        else min(spots, key=lambda r: (covers(r, keep), covers(r, avoid), gap(r, pane)))
    tall = min(best.height, screen.height)   # a screen too short for it: cut to fit, its text clipped
    best = Region(max(0, min(best.x, screen.width - best.width)), max(0, min(best.y, screen.height - tall)),
                  best.width, tall)
    return best, "centred" if side == "centred" else facing(best, pane)


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


def rows(screen: Widget, sid: str | None = None, kinds: tuple[str, ...] = ()) -> tuple[Region | None, Region | None]:
    """The inbox's rows on the screen, its header included, and the span of session `sid`'s
    (the demo's) whose refs are of `kinds` (tour.ROWS: "Q", "D", "M"; "" its conversation
    row), the rows a step needs the person to see: what a callout keeps clear of (place). None
    for either that isn't in sight."""
    found = screen.query("#items")
    if not found or not (table := found.first()).display or not table.region.area:
        return None, None
    region = table.region
    ordered = table.ordered_rows
    top = region.y + (table.header_height if table.show_header else 0) - table.scroll_offset.y
    starts = [top + sum(row.height for row in ordered[:n]) for n in range(len(ordered) + 1)]

    def span(first: int, last: int, start: int | None = None) -> Region | None:
        """Rows first to last, as far as they're in sight."""
        y = max(region.y, starts[first] if start is None else start)
        bottom = min(region.bottom, starts[last + 1])
        return Region(region.x, y, region.width, bottom - y) if bottom > y else None

    def kind(key: str) -> str | None:
        owner, _, ref = key.partition("|")
        return ref.rstrip("0123456789") if sid and owner == sid else None

    mine = [i for i, row in enumerate(ordered) if kind(str(row.key.value)) in kinds]
    every = span(0, len(ordered) - 1, region.y) if ordered else None
    return every, span(mine[0], mine[-1]) if mine else None


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
    /* constrain: kept on screen, and so its offset applies even at (0, 0), which Textual
       otherwise takes for none and lays the box out in the flow */
    TourCallout {{ overlay: screen; constrain: inside inside; width: {WIDTH}; height: auto; padding: 0 1; background: #12122a;
                   color: #e8e8e8; border: round #ffd300; border-title-color: #ffd300; }}
    TourCallout .tour-text {{ width: 1fr; height: auto; }}
    TourCallout Button {{ width: auto; min-width: 0; }}
    .{HIGHLIGHT} {{ border: round #ffd300 !important; }}
    """

    class Next(Message):
        def __init__(self, callout: "TourCallout") -> None:
            super().__init__()
            self.callout = callout

    def __init__(self, pane: str, text: str, next: bool = False, id: str | None = None, keep: str | None = None,
                 kinds: tuple[str, ...] = ()) -> None:
        super().__init__(id=id)
        self.pane, self.text, self.next = pane, text, next
        # the demo session, and the kinds of its inbox rows the step needs in sight (tour.ROWS):
        # what it never covers where it can help it (place)
        self.keep, self.kinds = keep, kinds
        self.at: Region | None = None   # where it was last put
        # put there with nowhere clear (place's fallback): the step's rows it was placed against,
        # left be until they move. False: placed clear
        self.forced: Region | None | bool = False
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
        self.text, self.placed, self.at, self.forced = text, None, None, False
        self.place()

    def cover(self) -> None:
        """Shown only while its own screen is the one on top."""
        self.display = self.app.screen is self.screen

    def highlight(self, target: Widget | None) -> None:
        """Its pane highlighted, and the last one not, unless another callout holds that one
        (the next step's, mounted before this one goes, #101)."""
        old = self.target
        if old is not None and old is not target and not any(   # a pane torn down has no screen: none hold it
                c is not self and c.target is old for c in (old.screen.query(TourCallout) if old.is_attached else ())):
            old.remove_class(HIGHLIGHT)
        if target is not None:
            target.add_class(HIGHLIGHT)
        self.target = target

    def lines(self, w: int, text: str) -> int:
        """Its height at width w: the text wrapped inside border and padding, the Next row, the border."""
        return len(Text(text).wrap(self.app.console, max(1, w - 4))) + self.next + 2

    def place(self) -> None:
        """Placed as place says when it shows, when its text changes (say), and when the screen
        is resized or its pane moves (a swap, a splitter dragged). Otherwise it stays put
        (#101), the inbox growing under it and all, unless it now covers the rows the step
        needs or its own pane. Nothing while it has no text to place (not composed yet, or
        being removed)."""
        if not self.query(".tour-text"):
            return
        screen = self.screen.size
        target = find(self.screen, self.pane)
        region = target.region if target is not None and target.display else None
        clear, keep = rows(self.screen, self.keep, self.kinds)
        beside = self.side not in ("inside", "centred")
        if self.placed == (screen, region) and self.at is not None and (
                self.forced is not False and self.forced == keep
                or not covers(self.at, keep) and not (beside and covers(self.at, region))):
            return   # a fallback stays while its rows do: placing it again would only find the same spot
        self.placed = (screen, region)
        self.highlight(target if region is not None else None)   # held for the step: the link, wherever it sits
        title = PANES.get(self.pane, ("", self.pane))[1]
        w = width(screen)
        at, side = place(screen, region, self.lines(w, self.text), clear, keep)
        text = self.text
        if side == "centred":   # its pane named first, a line that wraps on a narrow screen
            text = f"In the {title} pane:\n{self.text}"
            at, _ = place(screen, region, self.lines(w, text) - 1, clear, keep, centre=True)
        self.forced = keep if covers(at, keep) or side not in ("inside", "centred") and covers(at, region) else False
        if (at, side, text) == (self.at, self.side, getattr(self, "shown", None)):
            return   # unchanged: no refresh, which would only bring it back here (a layout loop)
        self.at, self.side, self.shown = at, side, text
        self.query_one(".tour-text", Static).update(Text(text))
        self.border_title = POINTERS[side].format(title) or None
        self.styles.width, self.styles.height = at.width, at.height
        self.absolute_offset = Offset(at.x, at.y)
        self.refresh(layout=True)

    @on(Button.Pressed, "#tour-next")
    def pressed(self, event: Button.Pressed) -> None:
        event.stop()   # the app's own buttons answer a press that bubbles up
        self.post_message(self.Next(self))
