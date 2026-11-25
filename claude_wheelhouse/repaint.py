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
the same widget, front to back. Textual is pinned (pyproject), so these track its internals."""
from typing import Callable, Iterable

from textual._compositor import ChopsUpdate, Compositor
from textual.geometry import Region
from textual.strip import Strip

from rich.console import Console
from rich.control import Control

TEXTUAL = Compositor.render_partial_update, ChopsUpdate.render_segments   # Textual's own, which these stand in for


def _touched(compositor: Compositor, regions: Iterable[Region]) -> Iterable[tuple[Region, Region, list[Strip]]]:
    """As `Compositor._get_renders`, front to back, but only the widgets a region touches, each
    rendered across its visible width on the rows from the first to the last region touching it.
    A region inside one widget is that widget's alone: the widgets behind it (its containers,
    the screen) would draw nothing that shows there, so they are not asked for it."""
    left = list(regions)
    for widget, (region, clip) in compositor.visible_widgets.items():
        if not left:
            return
        visible = region.intersection(clip)
        if not visible:
            continue
        rows = [part for r in left if (part := visible.intersection(r))]
        if not rows:
            continue
        top, bottom = min(p.y for p in rows), max(p.bottom for p in rows)
        shown = Region(visible.x, top, visible.width, bottom - top)
        yield region, shown, widget.render_lines(shown.translate(-region.offset))
        left = [r for r in left if not visible.contains_region(r)]


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


def install() -> None:
    """Swap in the partial update above (idempotent)."""
    Compositor.render_partial_update = render_partial_update
    ChopsUpdate.render_segments = render_segments
