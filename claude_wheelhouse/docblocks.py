"""A doc rendered block by block, with its map (.plan/modules.md, "Viewing and editing a doc").

The doc is parsed once, as Rich's Markdown parses it, and each top-level block (a heading, a
paragraph, a list, a code fence, a table) is rendered from its own tokens at the viewer's
width. The lines come out as the whole doc rendered at once gives them, so nothing shows
differently, and for each block its source lines (its token's `map`) and its rendered rows
are recorded. The map answers which source line a row shows, and which row shows a line.
"""

import bisect
import copy
from dataclasses import dataclass

from rich.markdown import Markdown, MarkdownElement, UnknownElement
from rich.segment import Segment


@dataclass(frozen=True)
class DocMap:
    """Each block's source lines and rendered rows, both [start, end) and 0-based, in order.
    Rows count from the doc's first rendered row."""
    lines: tuple[tuple[int, int], ...]
    rows: tuple[tuple[int, int], ...]

    def block_at_row(self, row: int) -> int | None:
        """The block a row shows, or for a row between blocks (the gap after one, or past
        the end) the nearest: on a tie, the one below, as a gap row reads as the space above
        the next block. None for a doc with no blocks."""
        return _nearest(self.rows, row)

    def block_at_line(self, line: int) -> int | None:
        """The block a source line is in, or for one between blocks (a blank line) the
        nearest, the one below on a tie."""
        return _nearest(self.lines, line)

    def line_at(self, row: int) -> int | None:
        """The first source line of the block a row shows (block_at_row)."""
        k = self.block_at_row(row)
        return None if k is None else self.lines[k][0]

    def row_of(self, line: int) -> int | None:
        """The first rendered row of the block a source line is in (block_at_line)."""
        k = self.block_at_line(line)
        return None if k is None else self.rows[k][0]


def _nearest(spans: tuple[tuple[int, int], ...], at: int) -> int | None:
    if not spans:
        return None
    k = bisect.bisect_right([start for start, _ in spans], at) - 1
    if k < 0:
        return 0
    if at < spans[k][1] or k == len(spans) - 1:
        return k
    return k if at - (spans[k][1] - 1) < spans[k + 1][0] - at else k + 1


def top_level(tokens: list) -> list[list]:
    """The tokens grouped by top-level block: each group opens at level 0 and runs to the
    next one that does."""
    groups: list[list] = []
    for token in tokens:
        if token.level == 0 and token.nesting >= 0:
            groups.append([])
        groups[-1].append(token)
    return groups


# tokens Rich's render loop handles itself, never as an element (Markdown.__rich_console__)
NOT_ELEMENTS = {"text", "hardbreak", "softbreak", "link_open", "link_close", "html_inline"}


def _element(token) -> type[MarkdownElement] | None:
    """The element class Rich's loop makes of a token, or None for one it handles inline."""
    if token.type in NOT_ELEMENTS or (token.tag in Markdown.inlines and token.type not in ("fence", "code_block")):
        return None
    return Markdown.elements.get(token.type) or UnknownElement


def _gap_before(whole: Markdown, group: list) -> bool:
    """Whether the first thing a block renders is one Rich's loop puts a blank line before,
    given the line before wants one: the block's own close, or a child its parent doesn't
    keep (an image in a paragraph). A child a container keeps (a list's item) renders
    nothing, and the block rendered alone then sets the line itself, as the whole doc does;
    so does an empty inline (an empty table cell), which Rich never puts a line before."""
    stack: list[type[MarkdownElement]] = []
    for token in whole._flatten_tokens(group):
        cls = _element(token)
        if cls is None:
            continue
        if token.nesting == 1:
            stack.append(cls)
            continue
        if token.type == "inline":
            return False
        if token.nesting == -1:
            stack.pop()
        return not stack or stack[-1].on_child_close is MarkdownElement.on_child_close
    return False


def render(md: str, style, width: int, console) -> tuple[list[list[Segment]], DocMap]:
    """A doc's lines, as Segment.split_lines gives the whole doc rendered by Rich at once,
    and its map. Between two blocks goes the blank line Rich's own loop puts there: none
    after a rule, whose element says so, and none from here before a block that makes its
    own (_gap_before)."""
    whole = Markdown(md, style=style)
    options = console.options.update_width(width)
    out: list[list[Segment]] = []
    lines, rows = [], []
    new_line = False
    for group in top_level(whole.parsed):
        if new_line and _gap_before(whole, group):
            out.append([])
        part = copy.copy(whole)
        part.parsed = group
        start = len(out)
        out.extend(Segment.split_lines(console.render(part, options)))
        while start < len(out) and not any(seg.text for seg in out[start]):
            start += 1   # a blank line Rich put before it is the gap, not the block
        lines.append(tuple(group[0].map))
        rows.append((start, len(out)))
        new_line = _element(group[0]).new_line
    return out, DocMap(tuple(lines), tuple(rows))
