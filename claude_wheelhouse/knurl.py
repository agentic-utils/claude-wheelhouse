"""The wheelhouse's scrollbars, app-wide (#55): a one-cell thumb with solid caps and a
Braille knurl, dark grooves on a solid teal block, on a thin track in the thumb's colour
at 35% over the background. Textual's stock thumb is a solid block in the theme's colour,
which read like a text selection. Hover and drag colour the thumb through the app's
scrollbar CSS; the mouse meta is Textual's, so grabbing and track clicks work as before.
"""

from rich.color import Color
from rich.segment import Segment, Segments
from rich.style import Style
from textual.color import Color as TColor
from textual.scrollbar import ScrollBarRender

TRACK = {True: "│", False: "─"}   # vertical, horizontal
CAP, KNURL = "█", "⣿"
TRACK_SHARE = 0.35                 # the track: the thumb's colour, this much over the background
MIN_THUMB = 3                      # two caps and a knurl


def thumb(size: int, virtual: float, window: float, position: float) -> tuple[int, int]:
    """The thumb's first cell and length, in whole cells: the caps are its ends."""
    length = min(size, max(MIN_THUMB, round(size * window / virtual)))
    travel = virtual - window
    start = round((size - length) * position / travel) if travel > 0 else 0
    return max(0, min(start, size - length)), length


class KnurlRender(ScrollBarRender):
    @classmethod
    def render_bar(cls, size: int = 25, virtual_size: float = 50, window_size: float = 20, position: float = 0,
                   thickness: int = 1, vertical: bool = True, back_color: Color = Color.parse("#000000"),
                   bar_color: Color = Color.parse("#05d9e8")) -> Segments:
        size = int(size)
        track = TColor.from_rich_color(back_color).blend(TColor.from_rich_color(bar_color), TRACK_SHARE).rich_color
        grab = {"@mouse.down": "grab"}
        up = Style(color=track, bgcolor=back_color, meta={"@mouse.down": "scroll_up"})
        down = Style(color=track, bgcolor=back_color, meta={"@mouse.down": "scroll_down"})
        cap = Style(color=bar_color, bgcolor=back_color, meta=grab)
        knurl = Style(color=back_color, bgcolor=bar_color, meta=grab)   # inverted: grooves in a teal block
        if window_size and size and virtual_size and window_size < virtual_size:
            start, length = thumb(size, virtual_size, window_size, position)
        else:
            start, length = size, 0   # nothing to scroll: bare track
        end = start + length - 1
        width = thickness if vertical else 1
        segs = [Segment(TRACK[vertical] * width, up) if i < start
                else Segment(TRACK[vertical] * width, down) if i > end
                else Segment(CAP * width, cap) if i in (start, end)
                else Segment(KNURL * width, knurl) for i in range(size)]
        if vertical:
            return Segments(segs, new_lines=True)
        return Segments((segs + [Segment.line()]) * thickness, new_lines=False)
