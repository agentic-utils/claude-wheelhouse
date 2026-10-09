"""A draggable divider between two panes: Textual has no splitter of its own."""

from rich.text import Text
from textual import events
from textual.css.scalar import Unit
from textual.message import Message
from textual.widget import Widget


class Splitter(Widget):
    """A one-cell divider. Dragging it resizes `sized` (a selector, found under the
    splitter's parent), and `flex`, the pane on its other side that fills what's left
    (1fr), gives or takes the room. The size is set as a percentage of the parent, so the
    layout follows a resized terminal. Neither pane goes under its CSS min-width (or
    min-height): one cell without one. A double-click puts back the stylesheet's size.

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

    def __init__(self, key: str, sized: str, flex: str, axis: str, **kwargs):
        super().__init__(classes=f"-{axis}", **kwargs)
        self.key, self.sized_selector, self.flex_selector, self.axis = key, sized, flex, axis
        self.drag: tuple[int, int, int] | None = None   # the pointer, sized and flex at the press
        self.fraction: float | None = None   # set by a drag or a stored size; None: the stylesheet's
        self.moved = False

    def render(self) -> Text:
        if self.axis == "x":
            return Text("\n".join("┊" * self.size.height))
        return Text("─" * self.size.width)

    # the panes and their sizes along the axis

    def panes(self) -> tuple[Widget, Widget]:
        return self.parent.query_one(self.sized_selector), self.parent.query_one(self.flex_selector)

    def extent(self, widget: Widget) -> int:
        return widget.region.width if self.axis == "x" else widget.region.height

    def minimum(self, widget: Widget) -> int:
        scalar = widget.styles.min_width if self.axis == "x" else widget.styles.min_height
        return int(scalar.value) if scalar is not None and scalar.unit == Unit.CELLS else 1

    def pointer(self, event: events.MouseEvent) -> int:
        return event.screen_x if self.axis == "x" else event.screen_y

    def apply(self, fraction: float | None) -> None:
        """Size the pane as a share of the parent; None puts back the stylesheet's size."""
        self.fraction = fraction
        sized, _ = self.panes()
        value = None if fraction is None else f"{fraction * 100:.3f}%"
        if self.axis == "x":
            sized.styles.width = value
        else:
            sized.styles.height = value

    # the mouse

    def _on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 1:
            return
        event.stop()
        sized, flex = self.panes()
        self.drag = (self.pointer(event), self.extent(sized), self.extent(flex))
        self.moved = False
        self.capture_mouse()
        self.add_class("-dragging")

    def _on_mouse_move(self, event: events.MouseMove) -> None:
        if self.drag is None:
            return
        start, sized_at, flex_at = self.drag
        sized, flex = self.panes()
        delta = self.pointer(event) - start
        if self.parent.children.index(sized) > self.parent.children.index(self):
            delta = -delta   # the pane is after the splitter: moving towards it shrinks it
        low, high = self.minimum(sized), sized_at + flex_at - self.minimum(flex)
        room = self.parent.size.width if self.axis == "x" else self.parent.size.height
        if high < low or not room or not delta:
            return
        self.moved = True
        # half a cell over: a share resolves to a fraction of a cell, and Textual rounds it down
        self.apply((max(low, min(sized_at + delta, high)) + 0.5) / room)

    def _on_mouse_up(self, event: events.MouseUp) -> None:
        if self.drag is None:
            return
        self.drag = None
        self.release_mouse()
        self.remove_class("-dragging")
        if self.moved:
            self.post_message(self.Resized(self, self.fraction))

    def _on_click(self, event: events.Click) -> None:
        if event.chain >= 2:
            self.apply(None)
            self.post_message(self.Resized(self, None))
