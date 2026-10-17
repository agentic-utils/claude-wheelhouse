import pathlib
import time

import pytest
from rich.console import Console
from rich.markdown import Markdown
from rich.segment import Segment

from claude_wheelhouse import docblocks

ROOT = pathlib.Path(__file__).parent.parent
STYLE = "#e8e8e8"


def console() -> Console:
    return Console(width=100, color_system="truecolor", force_terminal=True)


def whole(md: str, width: int, c: Console) -> list:
    """The doc rendered by Rich in one go, as the viewer drew it before blocks."""
    return list(Segment.split_lines(c.render(Markdown(md, style=STYLE), c.options.update_width(width))))


KINDS = [
    ("# Title\n\nA paragraph\nover two lines.\n", "heading then paragraph"),
    ("Intro.\n\n- one\n- two\n  - nested\n\nAfter.\n", "a tight list after a paragraph"),
    ("- a\n\n  loose para\n\n- b\n", "a loose list"),
    ("1. one\n2. two\n\n3) three\n", "ordered lists"),
    ("Text.\n\n```python\nx = 1\n```\n\nMore.\n", "a code fence"),
    ("Text.\n\n    indented code\n\nMore.\n", "an indented code block"),
    ("Arms:\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\nAfter.\n", "a table"),
    ("Arms:\n\n| a |  |\n|---|---|\n|  | 2 |\n", "a table with empty cells"),
    ("> quote\n> - in a list\n\nAfter.\n", "a blockquote"),
    ("Above.\n\n---\n***\n\nBelow.\n", "rules, which want no line after them"),
    ("Has ![img](x.png) inline.\n\n![alone](y.png)\n\nAfter.\n", "images"),
    ("Text.\n\n<div>html block</div>\n\nMore.\n", "an html block"),
    ("Setext\n------\n\nText<br>hard  \nbreak\n", "a setext heading and breaks"),
    ("See [the docs][d].\n\n[d]: https://example.com\n", "a reference link, defined elsewhere"),
    ("", "empty"),
]
REPO_DOCS = sorted(p for p in [*ROOT.glob("*.md"), *ROOT.glob(".plan/*.md"), *ROOT.glob("claude_wheelhouse/*.md")])


@pytest.mark.parametrize("md, desc", KINDS + [(p.read_text(), str(p.relative_to(ROOT))) for p in REPO_DOCS])
@pytest.mark.parametrize("width", [40, 100])
def test_rendered_by_block_as_rich_renders_it_whole(md, desc, width):
    c = console()
    lines, _ = docblocks.render(md, STYLE, width, c)
    assert lines == whole(md, width, c), f"{desc} at {width}: no visible change"


MIXED = "\n".join([
    "# Title",          # 0
    "",
    "Some prose",       # 2
    "over two lines.",
    "",
    "- one",            # 5
    "- two",
    "",
    "```py",            # 8
    "x = 1",
    "```",
    "",
    "| a | b |",        # 12
    "|---|---|",
    "| 1 | 2 |",
    "",
])


def test_the_map_gives_each_block_its_source_lines_and_rows():
    lines, m = docblocks.render(MIXED, STYLE, 60, console())
    assert m.lines == ((0, 1), (2, 4), (5, 8), (8, 11), (12, 15)), "heading, paragraph, list (markdown-it gives it the blank line after), fence, table"
    assert all(a < b for a, b in m.rows) and m.rows[-1][1] == len(lines), "every block rows of its own, to the end"
    assert all(m.rows[k][1] < m.rows[k + 1][0] for k in range(len(m.rows) - 1)), "a gap row between blocks"


@pytest.mark.parametrize("block, desc", [
    (0, "heading"), (1, "paragraph"), (2, "list"), (3, "code fence"), (4, "table"),
])
def test_every_row_of_a_block_maps_to_its_first_line(block, desc):
    _, m = docblocks.render(MIXED, STYLE, 60, console())
    first = m.lines[block][0]
    assert {m.line_at(r) for r in range(*m.rows[block])} == {first}, f"{desc}: its first source line"
    assert {m.row_of(line) for line in range(*m.lines[block])} == {m.rows[block][0]}, f"{desc}: back to its first row"


@pytest.mark.parametrize("rows, lines, at, want, desc", [
    (((0, 2), (3, 5)), ((0, 1), (2, 3)), 2, 1, "a one-row gap: the block below"),
    (((0, 2), (5, 7)), ((0, 1), (2, 3)), 2, 0, "a wide gap, nearer the block above"),
    (((0, 2), (5, 7)), ((0, 1), (2, 3)), 4, 1, "a wide gap, nearer the block below"),
    (((3, 5), (6, 8)), ((0, 1), (2, 3)), 0, 0, "above the first block: the first"),
    (((0, 2), (3, 5)), ((0, 1), (2, 3)), 99, 1, "past the end: the last"),
    ((), (), 0, None, "no blocks: none"),
])
def test_a_row_between_blocks_takes_the_nearest(rows, lines, at, want, desc):
    assert docblocks.DocMap(lines, rows).block_at_row(at) == want, desc


@pytest.mark.parametrize("line, want, desc", [
    (1, 2, "the blank line after the heading: the paragraph below, on a tie"),
    (4, 5, "the blank line after the paragraph: the list below"),
    (15, 12, "past the last line: the table"),
])
def test_a_line_between_blocks_takes_the_nearest(line, want, desc):
    _, m = docblocks.render(MIXED, STYLE, 60, console())
    assert m.lines[m.block_at_line(line)][0] == want, desc


def section(i: int) -> str:
    return (f"## Section {i}\n\nProse with **bold**, `code` and a [link](https://example.com).\n"
            f"A second line.\n\n- one\n- two\n  - nested\n\n```python\nx = {i}\ny = x * 2\n```\n\n"
            f"| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n\n> quoted\n")


def test_a_long_doc_renders_by_block_about_as_fast_as_whole():
    """A 2,000-line doc of every kind of block. The machine may be busy, so this compares CPU
    time (process_time, not the wall clock) against the whole render measured here, each the
    best of three runs taken in turn."""
    md = "\n".join(section(i) for i in range(96))
    assert md.count("\n") >= 2000
    c = console()
    old, new = [], []
    for _ in range(3):
        for runs, f in ((old, lambda: whole(md, 120, c)), (new, lambda: docblocks.render(md, STYLE, 120, c))):
            started = time.process_time()
            f()
            runs.append(time.process_time() - started)
    assert min(new) <= min(old) * 1.3 + 0.05, f"by block {min(new):.3f}s, whole {min(old):.3f}s"
