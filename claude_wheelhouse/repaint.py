"""Textual's partial screen update, made to cost what it changes (#385).

A refresh of a few cells reaches the terminal through `Compositor.render_partial_update` and
`ChopsUpdate.render_segments`. In Textual 8.2 both do far more than the cells ask for:

- The compositor renders every widget inside the bounding box of all the regions refreshed in
  one frame, and the containers behind them. The title's shimmer (row 0, the full width) and the
  session list's eyes (rows 3-22) share a frame, so each frame rendered every widget on rows
  0-22, some 900 lines and 50 widgets, to keep 20 cells of each line.
- The update writes each piece of a line from the left edge of its widget, cropped only on the
  right. An eye in the session list's last column wrote its whole row, 670 bytes, for 8 cells.

`install` swaps in versions that render only the front widgets a refreshed region touches, on
the lines it touches, and write from the first refreshed cell. What reaches the screen is the
same: a line nobody refreshed is never written, and of a line that is, the same cells come from
the same widget, front to back. Textual is pinned (pyproject), so these track its internals.

It also lets a widget be drawn without being pointed at: one with `pass_through` set is passed
over when the pointer's widget is looked up (mouse events, hover, tooltips, a click's cell), so
the widget beneath gets them, as if it weren't there. The session list's eyes are drawn by such
windows over its cells (tui.RowCells)."""
from typing import Callable, Iterable

from textual._compositor import ChopsUpdate, Compositor
from textual.errors import NoWidget
from textual.geometry import Region
from textual.strip import Strip

from rich.console import Console
from rich.control import Control

TEXTUAL = Compositor.render_partial_update, ChopsUpdate.render_segments   # Textual's own, which these stand in for
WIDGET_AT, WIDGETS_AT = Compositor.get_widget_at, Compositor.get_widgets_at   # Textual's own, which these stand in for


def _touched(compositor: Compositor, regions: Iterable[Region]) -> Iterable[tuple[Region, Region, list[Strip]]]:
    """As `Compositor._get_renders`, front to back, but only the widgets a region touches, each
    rendered across its visible width on the rows from the first to the last region touching it.
    A region inside the front widget at its corner, with no widget in front of that one (a
    child, an overlay) reaching into it, is that widget's alone: the widgets behind it (its
    containers, the screen) would draw nothing that shows there, so they are not asked."""
    visible_widgets = compositor.visible_widgets
    order = {widget: i for i, widget in enumerate(visible_widgets)}   # front to back
    shown_at = [region.intersection(clip) for region, clip in visible_widgets.values()]
    owned: dict = {}   # each front widget's regions
    shared = []   # the rest, which every widget they touch draws
    clear: dict[int, bool] = {}   # whether no widget in front reaches into a front widget at all
    for r in regions:
        try:
            widget, _ = WIDGET_AT(compositor, r.x, r.y)   # what is drawn there, pass_through or not
        except NoWidget:
            shared.append(r)
            continue
        front = order.get(widget)
        if front is None or not shown_at[front].contains_region(r):
            shared.append(r)
            continue
        # asked once a widget, not once a region: the eyes' windows take dozens of regions a frame (#385)
        if front not in clear:
            clear[front] = not any(shown.overlaps(shown_at[front]) for shown in shown_at[:front])
        if clear[front] or not any(shown.overlaps(r) for shown in shown_at[:front]):
            owned.setdefault(widget, []).append(r)
        else:
            shared.append(r)
    for widget, (region, clip) in visible_widgets.items():
        mine = owned.get(widget, [])
        if not mine and not shared:
            continue
        visible = region.intersection(clip)
        rows = mine + [part for r in shared if (part := visible.intersection(r))]
        if not rows:
            continue
        top, bottom = min(p.y for p in rows), max(p.bottom for p in rows)
        shown = Region(visible.x, top, visible.width, bottom - top)
        yield region, shown, widget.render_lines(shown.translate(-region.offset))


def _render_chops(compositor: Compositor, regions: Iterable[Region], is_rendered_line: Callable[[int], bool]) -> list[dict]:
    """`Compositor._render_chops` over the touched widgets alone (_touched)."""
    cuts = compositor.cuts
    chops: list[dict[int, Strip | None]] = [dict.fromkeys(cut_set[:-1]) for cut_set in cuts]
    for region, shown, strips in _touched(compositor, regions):
        render_x = shown.x
        first_cut, last_cut = shown.column_span
        for y, strip in zip(shown.line_range, strips):
            if not is_rendered_line(y):
                continue
            line = chops[y]
            final_cuts = [cut for cut in cuts[y] if last_cut >= cut >= first_cut]
            for cut, piece in zip(final_cuts, strip.divide([cut - render_x for cut in final_cuts[1:]])):
                if line.get(cut) is None:   # painted front to back: the first piece at a cut wins
                    line[cut] = piece
    return chops


def render_partial_update(self: Compositor) -> ChopsUpdate | None:
    """`Compositor.render_partial_update`, rendering only what the refreshed regions touch."""
    regions = self._dirty_regions.copy()
    self._dirty_regions.clear()
    if not regions:
        return None
    screen = self.size.region
    regions = [clipped for r in regions if (clipped := r.intersection(screen))]
    if not regions:
        return None
    spans = list(self._regions_to_spans(regions))
    chops = _render_chops(self, regions, {y for y, _, _ in spans}.__contains__)
    return ChopsUpdate(chops, spans, [cut_set[1:] for cut_set in self.cuts])


def render_segments(self: ChopsUpdate, console: Console) -> str:
    """`ChopsUpdate.render_segments`, each piece written from the first cell of its span, not the
    left edge of its widget."""
    sequences: list[str] = []
    append = sequences.append
    move_to = Control.move_to
    last_y = self.spans[-1][0]
    for y, x1, x2 in self.spans:
        for end, (x, strip) in zip(self.chop_ends[y], self.chops[y].items()):
            if strip is None or x > x2 or end <= x1:
                continue
            if x2 > x >= x1 and end <= x2:
                append(move_to(x, y).segment.text)
                append(strip.render(console))
                continue
            left = max(x, x1)
            append(move_to(left, y).segment.text)
            append(strip.crop(left - x, min(end, x2) - x).render(console))
        if y != last_y:
            append("\n")
    return "".join(sequences)


def get_widgets_at(self: Compositor, x: int, y: int) -> Iterable:
    """`Compositor.get_widgets_at`, front to back, less the widgets drawn to be passed over (pass_through)."""
    return ((widget, region) for widget, region in WIDGETS_AT(self, x, int(y)) if not getattr(widget, "pass_through", False))


def get_widget_at(self: Compositor, x: int, y: int):
    """`Compositor.get_widget_at`: the front widget at the cell the pointer can reach, past any
    drawn to be passed over (pass_through)."""
    for found in get_widgets_at(self, x, y):
        return found
    raise NoWidget(f"No widget under screen coordinate ({x}, {y})")


def install() -> None:
    """Swap in the partial update and the pointer's lookup above (idempotent)."""
    Compositor.render_partial_update = render_partial_update
    ChopsUpdate.render_segments = render_segments
    Compositor.get_widget_at = get_widget_at
    Compositor.get_widgets_at = get_widgets_at
