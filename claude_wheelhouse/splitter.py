"""A draggable divider between two panes: Textual has no splitter of its own."""

from typing import Callable

from rich.text import Text
from textual import events
from textual.css.query import NoMatches
from textual.css.scalar import Unit
from textual.message import Message
from textual.widget import Widget

NUDGE = 0.01   # cells a dragged share is over the cell it lands on (Splitter._on_mouse_move)


class Splitter(Widget):
    """A one-cell divider. Dragging it resizes `sized` (a selector, found under the
    splitter's parent), and `flex`, the pane on its other side that fills what's left
    (1fr), gives or takes the room. The size is set as a percentage of the parent, so the
    layout follows a resized terminal. Neither pane goes under its CSS min-width (or
    min-height): one cell without one. A double-click puts back its default (`default`'s,
    else the stylesheet's). With a neighbour missing (a pane swapped out), it does nothing.
    fit() keeps the sizes within the room there is: a size kept from a wider terminal is shrunk while it doesn't
    fit, the preference itself kept.

    axis "x" sits between columns and sets a width; "y" sits between rows and sets a
    height. It brightens to teal under the pointer and while it's dragged, and posts
    Resized when a drag ends or it's reset, for the app to keep."""

    ALLOW_SELECT = False   # a press here starts a drag, not a text selection
    DEFAULT_CSS = """
    Splitter { background: #0a0a12; }
    Splitter.-x { width: 1; height: 1fr; color: #3a3a5a; }
    Splitter.-y { height: 1; width: 1fr; color: #7b61ff; background: #000000; }
    Splitter:hover, Splitter.-dragging { color: #05d9e8; text-style: bold; }
    """

    class Resized(Message):
        """A drag ended (fraction: the sized pane's share of the parent) or the splitter
        was reset (None)."""

        def __init__(self, splitter: "Splitter", fraction: float | None):
            super().__init__()
            self.splitter, self.fraction = splitter, fraction

    def __init__(self, key: str, sized: str, flex: str, axis: str,
                 default: "Callable[[Splitter], int | None] | None" = None, **kwargs):
        super().__init__(classes=f"-{axis}", **kwargs)
        self.key, self.sized_selector, self.flex_selector, self.axis = key, sized, flex, axis
        self.default = default   # the pane's size in cells with none set, if not the stylesheet's
        self.drag: tuple[int, int, int] | None = None   # the pointer, sized and flex at the press
        self.fraction: float | None = None   # set by a drag or a stored size; None: the stylesheet's
        self.moved = False

    def render(self) -> Text:
        if self.axis == "x":
            return Text("\n".join("┊" * self.size.height))
        return Text("─" * self.size.width)

    # the panes and their sizes along the axis

    def panes(self) -> tuple[Widget, Widget] | None:
        """The two panes, or None with either gone."""
        try:
            return self.parent.query_one(self.sized_selector), self.parent.query_one(self.flex_selector)
        except NoMatches:
            return None

    def extent(self, widget: Widget) -> int:
        return widget.region.width if self.axis == "x" else widget.region.height

    def minimum(self, widget: Widget) -> int:
        scalar = widget.styles.min_width if self.axis == "x" else widget.styles.min_height
        return int(scalar.value) if scalar is not None and scalar.unit == Unit.CELLS else 1

    def pointer(self, event: events.MouseEvent) -> int:
        return event.screen_x if self.axis == "x" else event.screen_y

    def room(self) -> int:
        return self.parent.size.width if self.axis == "x" else self.parent.size.height

    def compact(self) -> tuple[bool, bool]:
        """Whether each pane is compact (class -compact: a slot shrunk to its panes' least
        rows while they have nothing to show, WheelhouseApp.fit_compact). Then nothing here
        is sized (wanted): a compact pane keeps the rows the app gave it, and with the
        flexing pane compact the sized one flexes instead, its share kept for when it isn't,
        so the room goes to it and not to a gap. Nor is it dragged or reset then: the share
        stays as it was, for when neither is."""
        panes = self.panes()
        return (panes[0].has_class("-compact"), panes[1].has_class("-compact")) if panes else (False, False)

    def apply(self, fraction: float | None) -> None:
        """Size the pane as a share of the parent; None puts back its default: `default`'s,
        else the stylesheet's. Compact, it keeps its rows; with the flexing pane compact, it
        flexes (1fr)."""
        self.fraction = fraction
        if panes := self.panes():
            sized, flex = self.compact()
            if sized:
                return
            if flex:
                self.show(panes[0], "1fr")
            elif fraction is not None:
                self.show(panes[0], f"{fraction * 100:.3f}%")
            else:
                self.show(panes[0], self.default(self) if self.default else None)

    def show(self, sized: Widget, value: str | int | None) -> None:
        if self.axis == "x":
            sized.styles.width = value
        else:
            sized.styles.height = value

    def wanted(self, sized: Widget, room: int) -> float | None:
        """The cells the pane would have: its share, else the stylesheet's size. None for a
        stylesheet's fr, which takes what's left, and None while either pane is compact."""
        if any(self.compact()):
            return None
        if self.fraction is not None:
            return self.fraction * room
        if self.default and (cells := self.default(self)) is not None:
            return cells
        base = sized.styles.base.width if self.axis == "x" else sized.styles.base.height
        if base is None or base.unit == Unit.FRACTION:
            return None
        return base.value if base.unit == Unit.CELLS else base.value * room / 100

    # the mouse

    def _on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 1 or (panes := self.panes()) is None:
            return
        event.stop()
        if any(self.compact()):   # nothing here is sized while compact: a drag would move nothing, so none is kept
            return
        sized, flex = panes
        self.drag = (self.pointer(event), self.extent(sized), self.extent(flex))
        self.moved = False
        self.capture_mouse()
        self.add_class("-dragging")

    def _on_mouse_move(self, event: events.MouseMove) -> None:
        if self.drag is None:
            return
        if not event.button:   # the release was lost (outside the terminal, say): it ends here
            self.end_drag()
            return
        if (panes := self.panes()) is None:
            return
        start, sized_at, flex_at = self.drag
        sized, flex = panes
        delta = self.pointer(event) - start
        if self.parent.children.index(sized) > self.parent.children.index(self):
            delta = -delta   # the pane is after the splitter: moving towards it shrinks it
        low, high = self.minimum(sized), sized_at + flex_at - self.minimum(flex)
        room = self.room()
        if high < low or not room or not delta:
            return
        self.moved = True
        # a hair over the cell: Textual draws a pane from floor(top) to floor(top + size), so a
        # share a float's error under it loses a row, and half a cell over gains one where the
        # pane starts past a cell's half (one under a percentage-sized pane)
        self.apply((max(low, min(sized_at + delta, high)) + NUDGE) / room)

    def _on_mouse_up(self, event: events.MouseUp) -> None:
        if self.drag is not None:
            self.end_drag()

    def end_drag(self) -> None:
        self.drag = None
        self.release_mouse()
        self.remove_class("-dragging")
        if self.moved:
            self.post_message(self.Resized(self, self.fraction))

    def _on_click(self, event: events.Click) -> None:
        if event.chain >= 2 and not any(self.compact()):   # as a drag: nothing to see, so nothing kept
            self.apply(None)
            self.post_message(self.Resized(self, None))


def fit(splitters) -> None:
    """Size the panes the splitters size to what each would have (wanted) where there's
    room, and where there isn't, shrink them towards their minimums, each by its share of
    the overflow, until the panes that flex keep theirs. Each parent's own: its other
    children keep what they have, or their minimum if they flex. Shrinking sets cells,
    leaving the preference (fraction) as it was, for a terminal with room again."""
    groups: dict[tuple[int, str], list[Splitter]] = {}
    for splitter in splitters:
        if splitter.display and splitter.panes() is not None:   # one beside a collapsed slot sizes nothing
            groups.setdefault((id(splitter.parent), splitter.axis), []).append(splitter)
    for group in groups.values():
        first = group[0]
        parent, room = first.parent, first.room()
        # never under the pane's minimum: CSS holds it there, so that's what it takes
        wants = [(s, sized, max(want, first.minimum(sized))) for s in group
                 if (want := s.wanted(sized := s.panes()[0], room)) is not None]
        if not room or not wants:
            continue
        mine = {sized for _, sized, _ in wants}
        rest = 0
        for child in parent.children:
            if child in mine or not child.display:
                continue
            scalar = child.styles.width if first.axis == "x" else child.styles.height
            rest += first.minimum(child) if scalar is not None and scalar.unit == Unit.FRACTION else first.extent(child)
        spare = room - rest - sum(want for *_, want in wants)
        excess = {sized: max(0.0, want - first.minimum(sized)) for _, sized, want in wants}
        total = sum(excess.values())
        for splitter, sized, want in wants:
            if spare >= 0 or not total:
                splitter.apply(splitter.fraction)   # as preferred: the share, or the stylesheet's
            else:
                splitter.show(sized, max(first.minimum(sized), int(want + spare * excess[sized] / total)))
