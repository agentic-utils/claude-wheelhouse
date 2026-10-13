"""The wheelhouse app: a Textual app. It reads and acts through a client (client.py): the
daemon's hub, over HTTP and SSE, or with --local a hub of its own, in process."""

import asyncio
import bisect
import functools
import gc
import math
import os
import re
import shutil
import sqlite3
import subprocess
import time
import weakref
from typing import Callable, Self

from rich.markdown import Markdown as RichMarkdown
from rich.segment import Segment
from rich.style import Style
from rich.cells import cell_len
from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.cache import LRUCache
from textual.message import Message
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.geometry import Offset
from textual.screen import ModalScreen, Screen
from textual.scrollbar import ScrollBar
from textual.selection import SELECT_ALL, Selection
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets.text_area import Selection as Span
from textual.worker import get_current_worker
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    DirectoryTree,
    Footer,
    Input,
    Label,
    RadioButton,
    RadioSet,
    SelectionList,
    OptionList,
    Select,
    Static,
    TextArea,
)

from . import api, emoji, features, models, stats, subagents, transcript, tutorial
from .client import LocalClient, Unreachable
from .hub import COMPACT_RELAUNCH_WAIT, VERBS, Done, state as hub_state, Event, aimed, display_name, host_context, short
from .knurl import KnurlRender
from .splitter import Splitter, fit
from .stats_pane import SessionStats
from .store import (DEFAULT_THEN, DISMISSABLE, FINISHED_RANK, SETTLED, SETTLED_RANK, SessionGone, Store, db_path, default_runner,
                    inbox_rank, mode, runner, standing)
from .unsent import Unsent

MATRIX = "#00ff41"
SHIMMER = ["#ff2a6d", "#ff7b00", "#ffd300", "#05d9e8", "#7b61ff", "#d300c5"]
MARKED = Style(bgcolor="#3a1060")   # rows picked to close together
DECISION = "bold #b967ff"   # an unseen decision: noticeable, not urgent
STATUS_STYLE = {"live": "bold #00ff41", "stalled": "bold #ffd300", "starting": "#05d9e8",
                "dead": "bold #ff2a6d", "forgetting": "bold #d300c5", "parking": "bold #d300c5",
                "relaunching": "bold #05d9e8"}
TITLE = " ▓▒░ CLAUDE·WHEELHOUSE ░▒▓ "
FRAMES = 10   # frames a second that the shimmer, the eyes and the bars are counted in, whatever the rate drawn
SHIMMER_PERIOD = 2 * len(SHIMMER)   # the title's frames before its colours come round again (shimmer)
UNREACHABLE = Text(" daemon unreachable: reconnecting… ", style="bold #000000 on #ff2a6d")   # the title, while it is
# seconds the person must stay on an item, without a break, before it counts as looked at:
# an unseen decision becomes seen, a tutorial step ticks (dwelt). Passing over it does neither
DWELL = 1.0
# seconds with no key before a text box's cursor blinks again (hold_cursor). A terminal sends
# no key release: a held key repeats after an initial delay of about half a second, then
# every few tens of ms, so a gap longer than both means every key has been let go
QUIET = 0.6
NOTHING_SELECTED = "Select an item, or a session to follow its conversation."
# the person's words in terminal green, Claude's in white as in the Claude app
VOICE = {"you": MATRIX, "claude": "#e8e8e8", "head": "#05d9e8",
         "warn": "bold #ffd300", "note": "#777777", "tool": "#777777"}
CLOSABLE = {"question": "answered", "decision": "seen"}   # what Delete closes, and what Ctrl+R reopening makes it


def closable(item) -> bool:
    """What Delete acts on: a question or decision whatever its status, a task or subagent
    once it has settled (store.SETTLED), dismissed or not."""
    return item["kind"] in CLOSABLE or (item["kind"] in DISMISSABLE and item["status"] in SETTLED[item["kind"]])
# where each module slot (api.SLOTS) is mounted: at the end of this container
SLOT_PARENTS = {"inbox.side": "#items-pane"}
LAYOUT = "layout."   # settings: each splitter's size, by its key, as a share of its parent


def eye_at(frame: int, width: int = 8) -> int:
    """Where the eye is at `frame`: all that changes from one of its frames to the next (cylon)."""
    return round((math.sin(frame / 4) + 1) / 2 * (width - 1))


def cylon(frame: int, width: int = 8) -> Text:
    """A red eye sweeping back and forth with a fading tail."""
    pos = eye_at(frame, width)
    t = Text()
    for i in range(width):
        d = abs(i - pos)
        t.append("█" if d == 0 else "▓" if d == 1 else "░" if d == 2 else " ",
                 style="#ff0000" if d == 0 else "#aa0000" if d == 1 else "#550000")
    return t


def shimmer_phase(frame: int) -> int:
    """The title's look at `frame`: it comes round every SHIMMER_PERIOD frames (shimmer)."""
    return frame % SHIMMER_PERIOD


def shimmer(frame: int) -> Text:
    """The title at `frame`, one of SHIMMER_PERIOD looks, each built once."""
    return _shimmer(shimmer_phase(frame))


@functools.cache
def _shimmer(frame: int) -> Text:
    t = Text()
    for i, ch in enumerate(TITLE):
        t.append(ch, style=f"bold {SHIMMER[(i + frame) // 2 % len(SHIMMER)]}")
    return t


def system_clipboard() -> str | None:
    """The system clipboard's text, for a right-click paste. No terminal lets an app read
    it, so under WSL it's Windows', through PowerShell (most of a second: call it off the
    UI thread). None where there's no way to read it."""
    exe = shutil.which("powershell.exe")
    if exe is None:
        return None
    try:
        out = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command",
                              "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Clipboard -Raw"],
                             capture_output=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.decode("utf-8", "replace").replace("\r\n", "\n").removesuffix("\n")   # PowerShell's own newline


def session_action(method):
    """Every action on a session goes through here. A session can end itself (or be ended
    from elsewhere) at any moment, including while one of our dialogs is open: then say
    so and repaint, rather than crash."""
    @functools.wraps(method)
    def guarded(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except (SessionGone, sqlite3.IntegrityError):
            self.notify("session no longer exists", severity="warning")
            if self.selected and self.client.session(self.selected[0]) is None:
                self.selected = None
            self.refresh_data()
        except Unreachable as e:   # the daemon: nothing was sent, and what's typed stays
            self.notify(str(e), severity="error")
    return guarded


def item_rows(items, names) -> list[tuple]:
    """Tasks and questions in inbox order, then each session's subagents grouped beneath
    its name. Pairs of (item, None) for inbox rows, (item, position in its group) for agents."""
    inbox = [(it, None) for it in items if it["kind"] != "agent"]
    # sorted is stable, so each group keeps inbox order
    agents = sorted((it for it in items if it["kind"] == "agent"),
                    key=lambda it: (names.get(it["session_id"], ""), it["session_id"]))
    grouped, last, n = [], None, 0
    for it in agents:
        n = n + 1 if it["session_id"] == last else 0
        last = it["session_id"]
        grouped.append((it, n))
    return inbox + grouped


def item_key(item) -> str:
    """An item's row key in the item list."""
    return f"{item['session_id']}|{item['ref']}"


def band(rank: tuple) -> int:
    """Where an inbox rank (store.inbox_rank) puts an item: 0 active, 1 settled, 2 finished."""
    return 2 if rank[0] >= FINISHED_RANK else 1 if rank[0] >= SETTLED_RANK else 0


def share(stored: str | None) -> float | None:
    """A kept splitter size, a share of its parent: None if it's missing, unreadable or not
    finite, else within 0 to 1 (splitter.fit takes it from there)."""
    try:
        value = float(stored)
    except (TypeError, ValueError):
        return None
    return min(max(value, 0.0), 1.0) if math.isfinite(value) else None


def thread_blocks(found, session_name: str = "") -> list[tuple[str, str]]:
    """An item's detail and conversation, oldest first, as (who, markdown) blocks, from the
    item and its messages (client.thread): the person's messages (queued ones marked), the
    session's replies, and its progress notes as quieter quotes."""
    if found is None:
        return [("note", "_gone_")]
    item, msgs = found
    ref = item["ref"]
    queued = any(m["draft"] for m in msgs)
    where = f"{session_name} · " if session_name else ""
    out = [("head", f"## {ref} · {item['title']}\n\n{where}`{item['kind']}` · **{item['status']}**"
                    f"{' · answer queued' if queued else ''} · updated {item['updated_at']}"),
           ("claude", item["body"] or "_no detail_")]
    for m in msgs:
        if m["author"] == "person":
            out.append(("you", f"**you{' · queued' if m['draft'] else ''}** · {m['created_at']}\n\n"
                               f"{transcript.hard_breaks(m['body'])}"))
        elif m["kind"] == "reply":
            out.append(("claude", f"**claude** · {m['created_at']}\n\n{m['body']}"))
        else:   # a progress note (rows from before replies existed read as notes)
            out.append(("note", "\n".join([f"> _note · {m['created_at']}_", ">"]
                                          + [f"> {line}" for line in m["body"].splitlines()])))
    return out


def thread_markdown(store: Store, sid: str, ref: str, session_name: str = "") -> str:
    item = store.item(sid, ref)
    found = None if item is None else (item, store.thread(sid, ref))
    return "\n\n".join(md for _, md in thread_blocks(found, session_name))


@functools.lru_cache(maxsize=2048)
def _block_lines(who: str, md: str, width: int, console) -> list:
    """A block's rendered segments at one width: rendered once, though Textual measures a
    widget's height and then draws it, and a new turn leaves every older block as it was."""
    r = Text(md.strip("`"), style=VOICE[who]) if who == "tool" else RichMarkdown(md, style=VOICE[who])
    return list(console.render(r, console.options.update_width(width)))


class Blocks:
    """A conversation as one renderable for one widget. A Markdown widget makes a child per
    paragraph, and a long conversation's thousand children made every layout pass (each
    keypress, each refresh) take a quarter of a second."""

    def __init__(self, blocks: list[tuple[str, str]]):
        self.blocks = blocks

    def __rich_console__(self, console, options):
        for who, md in self.blocks:
            yield from _block_lines(who, md, options.max_width, console)
            yield Segment.line()


def render(blocks: list[tuple[str, str]]) -> Blocks:
    return Blocks(blocks)


def same(a, b) -> bool:
    """Whether a cell is unchanged. Rich's Text equality ignores the base style, so a cell
    that only changed colour (a context bar crossing a grade, a row marked) counts too."""
    return a == b and getattr(a, "style", None) == getattr(b, "style", None)


def fill(table: DataTable, rows: list[tuple[str, tuple]]) -> bool:
    """Show rows, (key, cells), in a table. With the same keys in the same order only the
    changed cells are updated, so the cursor stays put. Otherwise the table is rebuilt
    without posting RowHighlighted: clear() puts the cursor on row 0 and the first new row
    highlights it, which made every refresh look like the person picking row 0 and back.
    Returns whether it rebuilt; the caller then puts the cursor back."""
    if [r.key.value for r in table.ordered_rows] == [k for k, _ in rows]:
        columns = list(table.columns)
        for key, cells in rows:
            for col, new, old in zip(columns, cells, table.get_row(key)):
                if not same(new, old):
                    table.update_cell(key, col, new)
        return False
    with table.prevent(DataTable.RowHighlighted):
        table.clear()
        for key, cells in rows:
            table.add_row(*cells, key=key)
    return True


@functools.lru_cache(maxsize=2048)
def _block_strips(who: str, md: str, width: int, console) -> tuple[tuple[Strip, ...], tuple[str, ...]]:
    """A block's lines as strips and as plain text (for selection), plus the gap after it."""
    lines = list(Segment.split_lines(_block_lines(who, md, width, console))) + [[]]
    return (tuple(Strip(line).crop_extend(0, width, None) for line in lines),
            tuple("".join(seg.text for seg in line).rstrip() for line in lines))


def agreed(a: list, b: list) -> int:
    """How many items a and b have in common from their starts."""
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


class Transcript(Widget, can_focus=True):
    """A conversation or a thread: one widget drawing cached lines, as Blocks does, and
    selectable. Drag selects, a double click a word (select_word), Ctrl+A selects it all,
    Ctrl+C copies (Textual's screen binding, which copies through the terminal: OSC 52,
    which Windows Terminal supports)."""
    ALLOW_SELECT = True
    DEFAULT_CSS = "Transcript { height: auto; }"
    BINDINGS = [Binding("ctrl+a", "select_all", "Select all", show=False)]

    def __init__(self, placeholder: str = "", **kwargs):
        super().__init__(**kwargs)
        self.blocks: list[tuple[str, str]] = [("note", placeholder)] if placeholder else []
        self._laid: tuple | None = None

    def update(self, content: "Blocks | list[tuple[str, str]]") -> None:
        """New blocks. Scrolled up to read (not at the end, which follows), the block at the top
        of the view keeps its place on screen, whatever changed above it; if it changed itself
        (a reply growing as it streams, a queued answer sent), the nearest unchanged block above
        it does, so the reader's line stays put and what grew shows below it; with none above,
        the first unchanged block below it in the view. Found more than once (a block said
        twice, a run of the same tool line), the one followed by most of what followed it,
        then preceded by most of what preceded it, then the nearest."""
        old, laid, scroll = self.blocks, self._laid, self.parent
        self.blocks = content.blocks if isinstance(content, Blocks) else list(content)
        self._laid = None
        self.refresh(layout=True)
        if not laid or not isinstance(scroll, VerticalScroll) or scroll.scroll_y >= scroll.max_scroll_y - 1:
            return
        width, starts, new = laid[0], laid[3], self.blocks
        top = bisect.bisect_right(starts, scroll.scroll_y) - 1
        bottom = bisect.bisect_left(starts, scroll.scroll_y + scroll.scrollable_content_region.height)
        for k in [top, *range(top - 1, -1, -1), *range(top + 1, bottom)]:
            found = [i for i, b in enumerate(new) if b == old[k]]
            if found:
                at = max(found, key=lambda i: (agreed(old[k:], new[i:]), agreed(old[k::-1], new[i::-1]),
                                               -abs(i - k)))
                y = self._layout(width)[2][at] + scroll.scroll_y - starts[k]
                scroll.scroll_to(y=y, animate=False, immediate=y <= scroll.max_scroll_y)   # or once laid out
                return

    def _layout(self, width: int) -> tuple[list[Strip], list[str], list[int]]:
        """Each line as a strip and as text, and the line each block starts on."""
        if self._laid is None or self._laid[0] != width:
            strips, texts, starts = [], [], []
            for who, md in self.blocks:
                s, t = _block_strips(who, md, width, self.app.console)
                starts.append(len(strips))
                strips.extend(s)
                texts.extend(t)
            self._laid = (width, strips, texts, starts)
        return self._laid[1:]

    def get_content_width(self, container, viewport) -> int:
        return container.width

    def get_content_height(self, container, viewport, width: int) -> int:
        return len(self._layout(width)[0])

    def render_line(self, y: int) -> Strip:
        width = self.size.width
        strips, texts, _ = self._layout(width)
        if y >= len(strips):
            return Strip.blank(width, self.rich_style)
        strip = strips[y]
        selection = self.text_selection
        if selection is not None and (span := selection.get_span(y)) is not None:
            text = texts[y]
            start, end = span
            end = len(text) if end == -1 else min(end, len(text))
            a, b = cell_len(text[:start]), cell_len(text[:end])
            if b > a:
                left, mid, right = strip.divide([a, b, strip.cell_length])
                style = self.screen.get_component_rich_style("screen--selection")
                mid = Strip(Segment.apply_style(list(mid), post_style=style), mid.cell_length)
                strip = Strip.join([left, mid, right])
        return strip.apply_offsets(0, y)

    def get_selection(self, selection) -> tuple[str, str] | None:
        width = self.size.width
        if not width:
            return None
        return selection.extract("\n".join(self._layout(width)[1])), "\n"

    def selection_updated(self, selection) -> None:
        self.refresh()

    def action_select_all(self) -> None:
        self.text_select_all()


WORD = re.compile(r"\w+|\s+|.")   # what a double click selects: a word, a run of spaces, or one other character


def select_word(screen: Screen, x: int, y: int) -> None:
    """A double click at (x, y) on the screen: the word under it selected, as in an editor or
    a terminal. Textual's own double click selects the whole widget, already done when this
    runs (it's the widget's, this the app's), so this narrows it, and only then: a widget it
    left alone (not selectable) stays so. Past a line's end, nothing is selected. An answer
    box keeps a selection of its own, in its text, about where the click put its cursor."""
    widget, at = screen.get_widget_and_offset_at(x, y)
    if widget is None or widget not in screen.selections:
        return
    if isinstance(widget, TextArea):   # no offsets in what it draws: its cursor is where the click was
        row, col = widget.cursor_location
        found = next((m for m in WORD.finditer(widget.document.get_line(row)) if m.start() <= col < m.end()), None)
        screen.clear_selection()
        if found:
            widget.selection = Span((row, found.start()), (row, found.end()))
        return
    if at is None:
        return
    got = widget.get_selection(SELECT_ALL)
    lines = got[0].splitlines() if got else []
    line = lines[at.y] if at.y < len(lines) else ""
    found = next((m for m in WORD.finditer(line) if m.start() <= at.x < m.end()), None)
    screen.selections = {widget: Selection(Offset(found.start(), at.y), Offset(found.end(), at.y))} if found else {}


BLANK = "\x00blank"   # a blank row's key, with its place after it: no session id or item key starts so


class Sink:
    """A list's rows that change sides, from its top to its foot or back (an item settling, a
    session parked), fall or rise there over SECONDS rather than jump (T66, T71); anything
    else jumps, as it always has. lay takes the list's final order, its rows on top first,
    then those sunk to the foot, and gives the rows to show now, None for a blank row. With
    room, the visible rows, the sunk ones sit at the foot of those, below a gap. Its timer
    runs only while a move does: nothing runs while nothing moves."""
    SECONDS = 0.4
    FRAME = 1 / 30
    now = staticmethod(time.monotonic)   # the clock a move is timed by: a test runs it on its own

    def __init__(self, repaint):
        self.repaint = repaint   # the list's paint, which lays it again: each frame
        self.to: dict[str, int] | None = None   # each key's slot, the move's end: what the cursor keys act in (SinkList)
        self.sunk: set[str] | None = None   # those sunk at the last lay; None: the next lay jumps
        self.move: tuple[dict[str, float], float] | None = None   # each key's slot at the start, and when
        self.timer = None

    def lay(self, app, keys: list[str], sunk: set[str], room: int = 0) -> list[str | None]:
        top = sum(k not in sunk for k in keys)
        foot = max(top, room - (len(keys) - top))
        to = {k: i if i < top else foot + i - top for i, k in enumerate(keys)}
        moved = self.sunk is not None and any(k in self.to and (k in sunk) != (k in self.sunk) for k in keys)
        if moved or (self.move and to != self.to):   # from wherever each row shows now
            now = self.now()
            self.move = (self.at(now), now)
            if self.timer is None:
                self.timer = app.set_interval(self.FRAME, self.repaint)
        self.to, self.sunk = to, set(sunk)
        return self.frame()

    def at(self, now: float) -> dict[str, float]:
        """Each key's slot now: falling with gravity, rising as if thrown up."""
        if self.move is None:
            return dict(self.to or {})
        start, began = self.move
        t = min(1.0, (now - began) / self.SECONDS)
        out = {}
        for k, end in self.to.items():
            a = start.get(k, end)
            out[k] = a + (end - a) * (t * t if end > a else 1 - (1 - t) ** 2)
        return out

    def frame(self) -> list[str | None]:
        now = self.now()
        if self.move and now - self.move[1] >= self.SECONDS:
            self.jump(keep=True)
        slots = self.at(now)
        rows: list[str | None] = []
        # never two in one slot. Whole slots passed, so each row moves one way only, never back
        for k in sorted(slots, key=lambda k: (slots[k], self.to[k])):
            rows += [None] * max(0, int(slots[k]) - len(rows)) + [k]
        return rows

    def jump(self, keep: bool = False) -> None:
        """No move under way, and none for the next lay unless keep: Delete's change lands at once
        (D33). The final order stays until then: a row Delete takes goes from its place there."""
        self.move = None
        if self.timer is not None:
            self.timer.stop()
            self.timer = None
        if not keep:
            self.sunk = None


class SinkList(DataTable):
    """A list a Sink lays. Every key that moves its cursor (Up, Down, PageUp, PageDown,
    Ctrl+Home, Ctrl+End, the item list's Shift+Up and Shift+Down) acts in its final order
    (final), never a moving frame's, so keys typed during a move act as they would after it;
    the cursor steps off a blank row the way it was going. Home and End scroll sideways."""

    def __init__(self, **kwargs):
        self.blank_lines: LRUCache[tuple, Strip] = LRUCache(1000)   # _render_line
        super().__init__(**kwargs)
        self.sink: Sink | None = None

    def keys(self) -> list[str]:
        return [r.key.value for r in self.ordered_rows]

    def cursor_key(self) -> str | None:
        return self.keys()[self.cursor_row] if self.row_count else None

    def final(self) -> list[str | None]:
        """The rows as they stand once any move has ended, a key or None for a blank: the
        frame shown, but for a move under way."""
        to = self.sink and self.sink.to
        if not to or any(k not in self.rows for k in to):
            return [None if self.blank(i) else k for i, k in enumerate(self.keys())]
        rows: list[str | None] = [None] * (max(to.values()) + 1)
        for k, slot in to.items():
            rows[slot] = k
        return rows

    def final_row(self) -> int:
        """The cursor's row in the final order: where its row goes once any move has ended."""
        rows, here = self.final(), self.cursor_key()
        return rows.index(here) if here in rows else self.cursor_row

    def go(self, where, page: int = 0) -> None:
        """The cursor to where(row, rows) in the final order, from its own row there, on that
        key's row in the frame; scrolled a page first for PageUp and PageDown, as DataTable's."""
        rows = self.final()
        if not any(rows):
            return
        here = self.cursor_key()
        i = rows.index(here) if here in rows else min(self.cursor_row, len(rows) - 1)
        j = max(0, min(where(i, len(rows)), len(rows) - 1))
        down, up = range(j, len(rows)), range(j, -1, -1)
        j = next(n for n in (*(down if j >= i else up), *(up if j >= i else down)) if rows[n])
        self._set_hover_cursor(False)
        if page:
            self.scroll_relative(y=page, animate=False, force=True)
        self.cursor_coordinate = Coordinate(self.get_row_index(rows[j]), self.cursor_column)

    def page(self) -> int:
        return self.scrollable_content_region.height - (self.header_height if self.show_header else 0)

    def action_cursor_up(self) -> None:
        self.go(lambda i, n: i - 1)

    def action_cursor_down(self) -> None:
        self.go(lambda i, n: i + 1)

    def action_page_up(self) -> None:
        self.go(lambda i, n: i - self.page(), -self.page())

    def action_page_down(self) -> None:
        self.go(lambda i, n: i + self.page(), self.page())

    def action_scroll_top(self) -> None:
        self.go(lambda i, n: 0)

    def action_scroll_bottom(self) -> None:
        self.go(lambda i, n: n - 1)

    @functools.lru_cache(maxsize=32)
    def _get_styles_to_render_cell(self, is_header_cell: bool, is_row_label_cell: bool, is_fixed_style_cell: bool,
                                   hover: bool, cursor: bool, *rest) -> tuple[Style, Style]:
        """DataTable's, but the mouse's row keeps its cells' own colours (a session's status
        dot, an item's status): the hover style takes the table's colour (MATRIX) with its
        background, and with the foreground's priority on the CSS, DataTable laid it over
        every cell, so every status went green under the mouse. The cursor's row still takes
        the cursor's colour. Cached as DataTable's is, which it clears by this name when its
        looks change: DataTable's own, unwrapped, so there is the one cache to clear. With the
        cursor hidden (the session list with none selected, #16) no row is the cursor's, but the
        mouse's is still shaded, where DataTable would shade none."""
        if not rest[0]:   # show_cursor
            cursor, rest = False, (True, *rest[1:])
        style, post = DataTable._get_styles_to_render_cell.__wrapped__(
            self, is_header_cell, is_row_label_cell, is_fixed_style_cell, hover, cursor, *rest)
        if hover and not cursor:
            post = Style.from_color(bgcolor=post.bgcolor)
        return style, post

    def blank(self, row: int) -> bool:
        return 0 <= row < self.row_count and self.ordered_rows[row].key.value.startswith(BLANK)

    def _render_line(self, y: int, x1: int, x2: int, base_style: Style) -> Strip:
        """DataTable's line, kept by whether the cursor and the mouse are on its row, not by
        where they are, as DataTable keeps it: a cursor move draws again the two rows it
        leaves and lands on, not every row. In DataTable's own line cache, which it clears
        whenever its looks or its data change. A blank row (Sink), most of the session
        list's, is kept apart, by its place and the columns' widths, as a change to the
        data leaves it as it was: a rebuild draws again the real rows alone."""
        try:
            row_key, _ = self._get_offsets(y)
        except LookupError:
            return super()._render_line(y, x1, x2, base_style)
        if self.cursor_type != "row":
            return super()._render_line(y, x1, x2, base_style)
        row = -1 if row_key is self._header_row_key else self._row_locations.get(row_key)
        state = (x1, x2, self.size.width, self.cursor_coordinate.row == row, self.hover_coordinate.row == row,
                 base_style, self.show_cursor, self._show_hover_cursor, self._pseudo_class_state)
        if row >= 0 and row_key.value.startswith(BLANK):
            cache, key = self.blank_lines, (row, tuple(c.get_render_width(self) for c in self.ordered_columns), *state)
        else:
            cache, key = self._line_cache, ("row", y, self._update_count, *state)
        strip = cache.get(key)
        if strip is None:
            strip = cache[key] = super()._render_line(y, x1, x2, base_style)
        return strip

    def notify_style_update(self) -> None:
        self.blank_lines.clear()
        super().notify_style_update()

    def _clear_caches(self) -> None:
        """DataTable's caches and the blank rows' with them, for a look their key leaves out
        (zebra stripes, fixed rows)."""
        self.blank_lines.clear()
        super()._clear_caches()

    def clear(self, columns: bool = False) -> Self:
        """The blank rows' lines are kept: they go by their place and the columns' widths, not
        the rows, so a rebuild (fill) draws again the real rows alone. The session list is
        rebuilt at a cursor move to a session whose info is another height (on_resize)."""
        kept, self.blank_lines = self.blank_lines, LRUCache(1000)   # what _clear_caches clears
        super().clear(columns)
        self.blank_lines = kept
        return self

    def validate_cursor_coordinate(self, value: Coordinate) -> Coordinate:
        value = super().validate_cursor_coordinate(value)
        if not self.blank(value.row):
            return value
        ahead = value.row >= self.cursor_coordinate.row
        down, up = range(value.row, self.row_count), range(value.row, -1, -1)
        row = next((i for i in (*(down if ahead else up), *(up if ahead else down)) if not self.blank(i)), value.row)
        return Coordinate(row, value.column)


async def select_now(table: DataTable) -> None:
    """Enter on a list: its row is selected as the key arrives (take_key), not posted, where
    the keys typed behind Enter would act before what it opens."""
    if table.row_count:
        row = table.cursor_row
        await table.app._dispatch_message(DataTable.RowSelected(table, row, table.coordinate_to_cell_key((row, 0)).row_key))


class SessionList(SinkList):
    """The inbox's sessions. One click selects a row: a plain table selects only on a second
    click, the first just moving the cursor there. A single click on the selected row, or on
    the list's title, unselects it (the inbox shows every session's items, as Esc), and with none selected the
    highlight is hidden (show_cursor) until a click, Enter or a key that moves it picks one
    (reveal). The buttons under it act on the highlighted row and never take focus, so Tab
    goes straight on to the items; these keys press them instead."""

    # E, S and Shift+S (End, Restore and Restore all before Forget and Relaunch took them
    # over) are bound to nothing for a release, so muscle memory does nothing rather than
    # something new. Delete and Backspace forget, as they close an item in the item list
    BINDINGS = [Binding("r", "app.press('rename')", "Rename", show=False),
                Binding("l", "app.press('relaunch')", "Relaunch", show=False),
                Binding("p", "app.press('park')", "Park", show=False),
                Binding("k", "app.press('fork')", "Fork", show=False),
                Binding("delete", "app.press('end')", "Forget", show=False),
                Binding("backspace", "app.press('end')", "Forget", show=False)]

    EYE = 8   # the Cylon eye's width (cylon)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.eyes: set[str] = set()   # the busy sessions' rows, whose eye cell paint_sessions leaves blank
        self.eye_frame = 0
        self.swept: tuple | None = None   # the busy rows and the eye's place as last drawn again (sweep)

    def sweep(self, frame: int) -> None:
        """The busy rows' eyes to `frame`: drawn over their blank cell as each line is drawn
        (_render_line), and those rows alone drawn again. Never in the table's data, where
        each change would draw every row again: DataTable's caches key on its count of them.
        Not if the eyes are where they were last drawn, on the same rows: a frame can leave them there."""
        self.eye_frame = frame
        swept = (frozenset(self.eyes), eye_at(frame, self.EYE))   # a row the table draws anew, it draws as it is
        if swept == self.swept:
            return
        self.swept = swept
        for sid in self.eyes:
            if sid in self.rows:
                self.refresh_row(self.get_row_index(sid))

    def _render_line(self, y: int, x1: int, x2: int, base_style: Style) -> Strip:
        """The line as DataTable draws it, with a busy row's eye over its blank cell (sweep)."""
        strip = super()._render_line(y, x1, x2, base_style)
        if not self.eyes or not self.columns:
            return strip
        try:
            row_key, line = self._get_offsets(y)
        except LookupError:
            return strip
        if line or row_key.value not in self.eyes:
            return strip
        *before, _ = self.ordered_columns
        x = sum(c.get_render_width(self) for c in before) + self.cell_padding - x1
        lo, hi = max(0, x), min(strip.cell_length, x + self.EYE)
        if lo >= hi:   # scrolled out of sight
            return strip
        # as DataTable draws a cell: its row's style, the eye's own, then the cursor's or the
        # mouse's colours where they come first (the blank cell under it has the first and last)
        row = self._row_locations.get(row_key)
        _, post = self._get_styles_to_render_cell(
            False, False, False, self.hover_coordinate.row == row, self.cursor_coordinate.row == row,
            self.show_cursor, self._show_hover_cursor, self.cursor_foreground_priority == "css",
            self.cursor_background_priority == "css")
        under = next(iter(strip.crop(lo, lo + 1)), None)
        base = under.style if under is not None and under.style else Style()
        eye = Strip([Segment(seg.text, base + seg.style + post) for seg in cylon(self.eye_frame).render(self.app.console)])
        return Strip.join([strip.crop(0, lo), eye.crop(lo - x, hi - x), strip.crop(hi)])

    def reveal(self) -> str | None:
        """With none selected, the session a key picks: the one last followed if it's still
        listed, else the first. The cursor moved there unseen, so nothing settles it as the
        person's move; the caller follows it."""
        rows = self.final()
        last = self.app.followed
        key = last if last and last in rows else next((k for k in rows if k), None)   # a blank row is None
        if key is not None:
            with self.prevent(DataTable.RowHighlighted):
                self.move_cursor(row=self.get_row_index(key), animate=False)
        return key

    def go(self, where, page: int = 0) -> None:
        if self.show_cursor:
            super().go(where, page)
        elif key := self.reveal():   # none selected: a key that moves the highlight brings it back, followed
            self.app.follow(key)

    async def action_select_cursor(self) -> None:
        if self.show_cursor or self.reveal():   # none selected: Enter picks as a moving key does
            await select_now(self)

    def on_resize(self) -> None:
        if getattr(self.app, "session_list", None) is self:   # its room: the parked sessions keep to its foot
            self.app.paint_sessions()

    def watch_show_horizontal_scrollbar(self, shown: bool) -> None:
        self.on_resize()   # a long name's scrollbar takes the room's last line

    def _on_mouse_move(self, event) -> None:
        # as _on_click, DataTable's runs after this one: it moves the mouse's shade only while
        # the cursor shows, so with none selected it is moved here, a hint that a click selects
        meta = event.style.meta
        if not self.show_cursor and "row" in meta and "column" in meta:
            self.hover_coordinate = Coordinate(meta["row"], meta["column"])

    async def _on_click(self, event) -> None:
        # Textual runs every class's _on_click in turn, DataTable's after this one, so don't
        # call it here too: that made one click select twice and open two relaunch prompts
        row = event.style.meta.get("row", -1)
        if self.blank(row):   # the gap above the parked sessions
            event.prevent_default()
            return
        self.app.settle()   # first what the keys before it moved, as a key's land does
        if not 0 <= row < self.row_count:   # the header, its title: all sessions, as Esc
            if "row" in event.style.meta and self.show_cursor:   # not the room below the rows
                event.prevent_default()
                self.app.unfilter()
            return
        if not self.show_cursor:   # none selected: the click selects its row, where DataTable would do nothing
            event.prevent_default()
            self._set_hover_cursor(True)
            with self.prevent(DataTable.RowHighlighted):
                self.move_cursor(row=row, animate=False)
            self._post_selected_message()
        elif event.chain == 1 and row == self.cursor_row:   # the selected one: unselected, as Esc. Not on
            event.prevent_default()   # a double-click's second click, which selects it again
            self.app.unfilter()
        else:
            self.call_next(self._after_click, self.cursor_coordinate)

    def _after_click(self, before) -> None:
        if self.cursor_coordinate.row != before.row:   # the table selects it itself if unmoved
            self._post_selected_message()

    def _post_selected_message(self) -> None:
        # a click's selection, as Enter's (select_now): handled as the app's next callback,
        # which land runs before the next key acts, not posted to reach the app behind them
        self.app.call_next(select_now, self)


class ItemList(SinkList):
    """The inbox's items, with a multi-selection to close questions in one go, by row key
    so it survives refreshes. Ctrl+click toggles a row, Shift+click takes the range from the
    last one toggled; Space and Shift+Up/Down do the same from the keyboard, for terminals
    that keep Shift+click for their own text selection."""

    # here only, not the app's: in an answer box Backspace and Delete edit the text (#64)
    BINDINGS = [Binding("delete", "app.close_question", "Close", key_display="Del"),
                Binding("backspace", "app.close_question", "Close", show=False),
                Binding("space", "toggle_mark", "Mark", show=False),
                Binding("shift+up", "extend(-1)", "Extend up", show=False),
                Binding("shift+down", "extend(1)", "Extend down", show=False)]

    class MarksChanged(Message):
        pass

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.marked: set[str] = set()
        self.anchor: str | None = None

    def set_marks(self, marks: set[str], anchor: str | None = None) -> None:
        self.marked = {k for k in marks if not k.endswith("|")}   # not the conversation row
        self.anchor = anchor
        self.post_message(self.MarksChanged())

    def toggle(self, key: str) -> None:
        # the first toggle starts from the highlighted row, as file managers do
        marks = set(self.marked) or {self.cursor_key()} - {None, key}
        self.set_marks(marks ^ {key}, key)

    def extend_to(self, key: str) -> None:
        keys = [k for k in self.final() if k]   # the range as it will stand, not a moving frame's
        anchor = self.anchor if self.anchor in keys else self.cursor_key()
        lo, hi = sorted((keys.index(anchor), keys.index(key)))
        self.set_marks(set(keys[lo:hi + 1]), anchor)

    async def _on_click(self, event) -> None:
        self.app.settle()   # first what the keys before it moved: the rows they leave are the ones clicked
        meta = event.style.meta
        if "row" not in meta or meta["row"] < 0:
            return
        if meta["row"] >= self.row_count:   # the settle refilled the list, shorter: below its rows, as typed slowly
            event.prevent_default()   # not DataTable's, which would clamp the cursor onto the last row
            return
        key = self.keys()[meta["row"]]
        if event.ctrl or event.shift:
            event.prevent_default()   # not DataTable's: a click on the highlighted row opens it
            self.toggle(key) if event.ctrl else self.extend_to(key)
            self.move_cursor(row=meta["row"], animate=False)
        elif self.marked or self.anchor:   # a plain click starts afresh
            self.set_marks(set(), key)

    async def action_select_cursor(self) -> None:
        await select_now(self)

    def _post_selected_message(self) -> None:
        # a double-click's selection, which opens the item, as the session list's click
        self.app.call_next(select_now, self)

    def action_toggle_mark(self) -> None:
        if self.cursor_key():
            self.toggle(self.cursor_key())

    def action_extend(self, step: int) -> None:
        if not self.row_count:
            return
        if self.anchor not in self.keys():
            self.anchor = self.cursor_key()
        self.go(lambda i, n: i + step)
        self.extend_to(self.cursor_key())


def marked(cells: tuple) -> tuple:
    """A marked row's cells, on a background distinct from the cursor's."""
    out = tuple(c.copy() if isinstance(c, Text) else Text(str(c)) for c in cells)
    for c in out:   # the base style, which the table also pads the cell with
        c.style = (Style.parse(c.style) if isinstance(c.style, str) else c.style) + MARKED
    return out


class Compose(TextArea):
    """An answer box. Ctrl+A selects all of it, as in other editors (TextArea's own is line
    start). Ctrl+Enter submits what's typed: queued or sent at once, by the session's mode.
    Most terminals (Windows Terminal among them) send Ctrl+Enter as a line feed, which
    arrives as ctrl+j, so both are bound.
    Emoji: a complete :code: turns into its emoji as it's typed, and a code being typed
    shows suggestions in the hint line, the first taken with Tab or Enter."""
    BINDINGS = [Binding("ctrl+enter", "app.submit", "Submit", show=False),   # the app's shows in the footer
                Binding("ctrl+j", "app.submit", "Submit", show=False),
                Binding("ctrl+a", "select_all", "Select all", show=False)]   # not line start

    def _before_cursor(self) -> tuple[int, int, str]:
        row, col = self.cursor_location
        return row, col, self.document.get_line(row)[:col]

    def suggestions(self) -> tuple[str | None, list[tuple[str, str]]]:
        code = emoji.partial(self._before_cursor()[2])
        return code, emoji.suggest(code) if code else []

    def on_text_area_changed(self, event) -> None:
        row, col, before = self._before_cursor()
        done = re.search(r":([a-z0-9_+\-]+):$", before)
        if done and done.group(1) in emoji.EMOJI:   # a code just closed: swap it in place
            self.replace(emoji.EMOJI[done.group(1)], (row, col - len(done.group(0))), (row, col))
            return
        hint = next(iter(self.parent.query(Hint)), None)
        if hint is not None:
            _, found = self.suggestions()
            hint.suggest(Text("  ".join(f"{g} :{c}:" for c, g in found) + "   ⇥ Tab takes the first")
                         if found else None)

    def take_suggestion(self) -> bool:
        """The first suggestion for a code being typed, if there is one, in its place."""
        code, found = self.suggestions()
        if found:
            row, col, _ = self._before_cursor()
            self.replace(found[0][1], (row, col - len(code) - 1), (row, col))
        return bool(found)

    async def _on_key(self, event) -> None:
        # Enter here; Tab is a binding, which acts before the box sees its key (take_key),
        # so cycle_focus takes the suggestion there
        if event.key == "enter" and self.take_suggestion():
            event.prevent_default()
            event.stop()


# the current session's lifecycle buttons, (caption, id), under the session list: each
# shown only where it applies (#62), and with none selected all but Model shown disabled (D20)
LIFECYCLE = (("Rename", "rename"), ("Relaunch", "relaunch"), ("Park", "park"), ("Forget", "end"), ("Model", "model"),
             ("Fork", "fork"))
# a session's conversation buttons, (caption, id, variant): under those, for the current
# session, highlighted there (D20; none while unfiltered); in a full-screen item's bar, for its session
CONVERSATION = (("Mode", "mode", "default"), ("Send", "send", "primary"), ("Interrupt", "interrupt", "error"),
                ("Compact", "compact", "default"), ("Shell", "shell", "default"))


# each root's buttons by id, as show_button found them: they live as long as it does, and
# finding them again each second was a good part of the refresh
HELD_BUTTONS: weakref.WeakKeyDictionary[Widget, dict[str, Button]] = weakref.WeakKeyDictionary()


WIDE_CAPTION = 12   # cells a lifecycle button's caption has in the session pane's three-column grid


def show_button(root: Widget, button_id: str, label: str, disabled: bool, shown: bool = True) -> None:
    """A button's caption and state, wherever it lives under root."""
    held = HELD_BUTTONS.setdefault(root, {})
    button = held.get(button_id)
    if button is None:
        button = next(iter(root.query(f"#{button_id}")), None)
        if button is None:   # the app's first refresh can come before the buttons mount
            return
        held[button_id] = button
    if str(button.label) != label:
        button.label = label
        # a caption past a grid cell's room (Stop & Forget) spans two, as Mode does
        button.set_class(len(label) > WIDE_CAPTION, "wide")
    button.disabled = disabled
    button.display = shown


class Columns(Horizontal):
    """The three columns. Every pane sits in them, so their resize, the terminal's (or the
    first layout), is when the splitters' sizes are fitted to the room again."""

    def on_resize(self) -> None:
        self.app.call_after_refresh(self.app.fit_layout)


class SendBar(Horizontal):
    """Always on screen above the footer: Send all, every session's queue, and a line
    saying what the session in context is doing. Send all's key is Shift+A (BUTTON_KEYS):
    Windows Terminal sends Ctrl+Shift+S and Ctrl+Alt+S as plain Ctrl+S. The buttons never
    take focus, so clicking one leaves the answer box (and what's typed in it) where it was.

    On the inbox the session's own buttons (CONVERSATION) sit under the session list. A
    full-screen item has no session list, so its bar carries them too (conversation=True),
    for the item's session: Mode, Send, and for a session run by a wheelhouse host what its
    terminal would have given, Interrupt, Compact and Shell (the real Claude Code, in a
    tab). Allow, Always and Deny sit with the permission item (PermissionButtons)."""
    DEFAULT_CSS = """
    SendBar { height: 1; background: #12122a; }
    SendBar Button { min-width: 12; margin: 0 1 0 0; padding: 0 1; }
    SendBar #activity { width: 1fr; height: 1; color: #05d9e8; text-style: italic; padding: 0 1; }
    """

    def __init__(self, conversation: bool = False, **kwargs):
        super().__init__(**kwargs)
        self.conversation = conversation

    def compose(self) -> ComposeResult:
        # compact: a variant's own border (tall, top and bottom) outranks the bar's
        # "border: none", and made each button two rows high in a one-row bar, its caption
        # on the row the footer covers
        buttons = CONVERSATION if self.conversation else ()
        for label, id_, variant in (*buttons[:2], ("Send all", "send-all", "warning"), *buttons[2:]):
            yield Button(label, variant, id=id_, compact=True, tooltip=BUTTONS[id_])
        yield Label("", id="activity")

    def on_mount(self) -> None:
        for button in self.query(Button):
            button.can_focus = False

    def activity(self, text: str) -> None:
        label = getattr(self, "activity_label", None) or next(iter(self.query("#activity")), None)
        self.activity_label = label   # held once found: it lives as long as the bar
        if label is not None and str(label.content) != text:
            label.update(text)


class PermissionButtons(Horizontal):
    """In place of the answer box while the item in context is an open permission (P) item:
    the answer is a button, applied at once whatever the session's send mode (#53), and the
    box doesn't show. Only a host's session posts permission items. EXPERIMENTAL (features.py): SDK mode is not for
    use by anyone other than the author until Anthropic's terms are confirmed."""
    DEFAULT_CSS = """
    PermissionButtons { height: 1; display: none; background: #12122a; }
    PermissionButtons Button { min-width: 12; margin: 0 1 0 0; padding: 0 1; }
    """

    def compose(self) -> ComposeResult:
        for label, id_, variant in (("Allow", "allow", "success"), ("Always", "always", "primary"),
                                    ("Deny", "deny", "error")):
            # compact: one row, as in the send bar
            yield Button(label, variant, id=id_, compact=True, tooltip=BUTTONS[id_])

    def on_mount(self) -> None:
        for button in self.query(Button):
            button.can_focus = False   # a click leaves the answer box, and what's typed, where it was


# what follows a Compact the wheelhouse asks for (store.THEN), as its question and dialog offer it
THEN_CHOICES = (("compact", "Just compact"), ("send", "Then send the queued answers"), ("park", "Then park it"))


def then_choice(radios: "RadioSet") -> str:
    """The THEN a set of THEN_CHOICES' radio buttons has pressed: its ids end with it."""
    pressed = radios.pressed_button
    return pressed.id.rsplit("-", 1)[1] if pressed is not None and pressed.id else DEFAULT_THEN


def then_radios(id_: str) -> "RadioSet":
    """THEN_CHOICES as radio buttons, DEFAULT_THEN pressed."""
    return RadioSet(*[RadioButton(label, value=then == DEFAULT_THEN, id=f"{id_}-{then}") for then, label in THEN_CHOICES],
                    id=id_)


class CompactButtons(Horizontal):
    """Over the answer box while the item in context is the host's open question suggesting
    Compact (host.nudge): Compact and Leave it, and what follows the compaction (THEN_CHOICES).
    Text typed in the box below is optional: the compaction's instructions, used instead of
    asking the session what to keep; Ctrl+Enter compacts with it too. Only a host's session
    is asked. EXPERIMENTAL (features.py), as PermissionButtons."""
    DEFAULT_CSS = """
    CompactButtons { height: 1; display: none; background: #12122a; }
    CompactButtons Button { min-width: 12; margin: 0 1 0 0; padding: 0 1; }
    CompactButtons RadioSet { layout: horizontal; width: auto; height: 1; border: none; padding: 0;
                              background: #12122a; }
    CompactButtons RadioButton { height: 1; padding: 0 1 0 0; background: #12122a; }
    """

    def compose(self) -> ComposeResult:
        yield Button("Compact", "success", id="compact-now", compact=True, tooltip=BUTTONS["compact-now"])
        yield Button("Leave it", id="leave-it", compact=True, tooltip=BUTTONS["leave-it"])
        yield then_radios("compact-then")

    def on_mount(self) -> None:
        for widget in (*self.query(Button), *self.query(RadioSet)):
            widget.can_focus = False   # a click leaves the answer box, and what's typed, where it was

    @property
    def then(self) -> str:
        return then_choice(self.query_one(RadioSet))


class ThreadView(Screen):
    """One item full screen: its detail, the whole conversation and a compose box. The app's
    refresh tick repaints it, so a reply shows up while it's open; typed text is untouched."""
    BINDINGS = [Binding("escape", "leave", "Back", key_display="Esc")]

    def __init__(self, sid: str, ref: str):
        super().__init__()
        self.sid, self.ref = sid, ref
        self.text = None

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="thread-scroll", classes="panel"):
            yield Transcript(id="thread")
        yield PermissionButtons()
        yield CompactButtons()
        yield Compose(id="thread-answer")
        yield Hint(classes="answer-hint")
        yield SendBar(conversation=True)
        yield Footer()

    def on_mount(self) -> None:
        self.box = self.query_one(Compose)
        app, me = self.app, (self.sid, self.ref)
        app.keep_unsent(app.answer, app.box_target, None)   # the inbox box's text, this item's included
        app.box_target = None
        app.keep_unsent(self.box, None, me)
        self.paint()
        app.paint_sendbar()   # its hint and bar now, not at the next refresh
        # one Tab stop for the thread, the transcript: its scroll keys reach the scroll as an ancestor's
        self.query_one("#thread-scroll").can_focus = False
        # at once, not after a refresh as focus() does: keys behind Enter go there. Not on a
        # permission, which shows no box (paint_sendbar gives it back once answered)
        self.set_focus(self.box if self.box.display else None)

    def action_leave(self) -> None:
        self.app.keep_unsent(self.box, (self.sid, self.ref), None)
        self.app.pop_screen()
        # the inbox box takes back its target's text, the bar its session: now, not after a
        # refresh, where text typed into the box behind Esc would be swapped out (take_key)
        self.app.retarget()

    def paint(self) -> None:
        s = self.app.client.session(self.sid)
        blocks, _ = self.app.item_blocks(self.sid, self.ref, s and (s["name"] or short(self.sid)))
        text = "\n\n".join(md for _, md in blocks)
        if text != self.text:
            scroll = self.query_one("#thread-scroll")
            following = scroll.scroll_y >= scroll.max_scroll_y - 1
            self.text = text
            self.query_one("#thread", Transcript).update(render(blocks))
            if following:   # at the newest reply, or opening: scrolled up to read, it keeps its place
                scroll.scroll_end(animate=False)


PERMISSION_HINT = "Allow, Always or Deny above (1, 2, 3), at once"
COMPACT_HINT = ("1 Compact or 2 Leave it, then 3 just compact, 4 send the queue or 5 park (outside the box) · "
                "type instructions for the compaction here if you like (none: the session is asked what to keep): "
                "Ctrl+Enter compacts with them")


def hint(sends: str, dead: bool = False) -> str:
    """The line under an answer box. sends: what Ctrl+Enter does for the session in context
    by its mode, queue or send. dead: it isn't running, so Ctrl+S refuses (#62) and goes
    unmentioned."""
    ctrl_s = "" if dead else " · Ctrl+S sends its queue"
    return (f"Ctrl+Enter to {sends}{ctrl_s} · Ctrl+T switches mode"
            " · Ctrl+R takes a queued answer back")


class TitleBar(Static):
    """The title: one of a few looks, its shimmer's phases and the daemon out of reach. A
    look already shown is not shown again, and each look's line is drawn once and recalled:
    the shimmer moves ten times a second, and drawing it afresh each time was a good part of
    what the app cost while idle."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.look: object = None   # what shows, as show was given it
        self.lines: dict[tuple, Strip] = {}   # each look's lines as drawn, for one size, style and selection
        self.lines_for: tuple | None = None

    def show(self, look: object, content: Text) -> None:
        """Show `content`, which `look` names: nothing to do if it already shows."""
        if look != self.look:
            self.look = look
            self.update(content, layout=False)

    def render_line(self, y: int) -> Strip:
        drawn_for = (self.size, self.visual_style, self.text_selection)
        if drawn_for != self.lines_for:
            self.lines, self.lines_for = {}, drawn_for
        key = (self.look, y)
        line = self.lines.get(key)
        if line is None:
            line = self.lines[key] = super().render_line(y)
        return line


class Hint(Label):
    """Under an answer box: what the keys do, or emoji suggestions while a :code: is typed."""

    def __init__(self, **kwargs):
        super().__init__(hint("queue"), **kwargs)
        self.base, self.suggesting = hint("queue"), None

    def set_base(self, text: str) -> None:
        if text != self.base:
            self.base = text
            if self.suggesting is None:
                self.update(text)

    def suggest(self, text: Text | None) -> None:
        # called on every key typed: most change nothing, and an update lays out the screen
        if text != self.suggesting:
            self.suggesting = text
            self.update(self.base if text is None else text)


class Dialog(ModalScreen):
    """A dialog. Every answer, by key, button or Enter in a box, is action_answer. A key acts
    once the one before it has landed (take_key), so a key typed behind the one that answers
    goes to the screen that answer uncovers, in order. Answered once: a second click finds it
    closed already."""

    def action_answer(self, result=None) -> None:
        if self.is_active:
            self.dismiss(result)


class NewSession(Dialog):
    """Name and ticket are optional; the brief becomes the first prompt. Model and effort
    start at the defaults (models.py), and go to every launch of the session. It opens in a
    tab; in SDK mode alone (features.py) a box offers the wheelhouse's host instead, ticked
    for a tab by default only under WHEELHOUSE_RUNNER=tab."""
    BINDINGS = [Binding("escape", "answer", show=False)]

    def compose(self) -> ComposeResult:
        # on another machine's daemon (--connect): its directories aren't this machine's, to
        # check or browse here (the daemon checks), and a tab would open on its screen
        elsewhere = self.app.client.elsewhere
        with Vertical(id="dialog"):
            yield Label("NEW SESSION", classes="dialog-title")
            with Horizontal(id="cwd-row"):
                yield Input(value="" if elsewhere else os.getcwd(), placeholder="working directory", id="cwd")
                if not elsewhere:
                    yield Button("Browse…", id="browse")
            yield Input(placeholder="name (optional)", id="name")
            yield Input(placeholder="ticket: #42, owner/repo#42 or ABC-123 (optional)", id="ticket")
            yield TextArea(id="brief")
            with Horizontal(id="model-row"):   # Sonnet 5.5 at high unless changed (Q50)
                yield Select([(m.name, m.id) for m in models.MODELS], value=models.DEFAULT_MODEL,
                             allow_blank=False, id="model")
                yield Select([(e, e) for e in models.EFFORTS], value=models.default_effort(models.DEFAULT_MODEL),
                             allow_blank=False, id="effort")
            if self.app.client.sdk:   # the runner choice: SDK mode only (features.py, EXPERIMENTAL)
                yield Checkbox("Open in a terminal tab instead of the wheelhouse",
                               default_runner(True) == "tab" and not elsewhere, id="tab", disabled=elsewhere)
            with Horizontal(classes="buttons"):
                yield Button("Launch", variant="success", id="launch")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed, "#launch")
    def launch(self) -> None:
        cwd = self.query_one("#cwd", Input).value.strip()
        if not self.app.client.elsewhere and not os.path.isdir(cwd):
            self.notify(f"no such directory: {cwd}", severity="error")
            return
        self.action_answer({"cwd": cwd, "name": self.query_one("#name", Input).value.strip(),
                            "ticket": self.query_one("#ticket", Input).value.strip(),
                            "brief": self.query_one("#brief", TextArea).text.strip(),
                            "runner": "tab" if not self.app.client.sdk or self.query_one("#tab", Checkbox).value
                            else "sdk",
                            "model": self.query_one("#model", Select).value,
                            "effort": self.query_one("#effort", Select).value})

    @on(Select.Changed, "#model")
    def model_picked(self, event: Select.Changed) -> None:
        """Another model: Effort goes to that model's default (Q54), and can be set again."""
        self.query_one("#effort", Select).value = models.default_effort(event.value)

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.action_answer(None)

    @on(Button.Pressed, "#browse")
    def browse(self) -> None:
        box = self.query_one("#cwd", Input)
        self.app.push_screen(PickDirectory(box.value.strip()), lambda path: path and setattr(box, "value", path))



class Folders(DirectoryTree):
    """Directories only. Hidden ones are left out, but for .worktrees, where a branch's
    worktree lives."""

    def filter_paths(self, paths):
        return [p for p in paths if self._safe_is_dir(p) and (not p.name.startswith(".") or p.name == ".worktrees")]


class PickDirectory(Dialog):
    """The new session's working directory, picked from a tree: Enter opens a folder,
    Backspace (or Up) goes to the parent, Ctrl+Enter (or Choose) takes the highlighted one."""
    BINDINGS = [Binding("ctrl+enter", "choose", "Choose", show=False),
                Binding("ctrl+j", "choose", "Choose", show=False),
                Binding("backspace", "up", "Up", show=False),
                Binding("escape", "answer", show=False)]

    def __init__(self, start: str = ""):
        super().__init__()
        start = os.path.expanduser(start)
        self.start = start if os.path.isdir(start) else os.path.expanduser("~")

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("WORKING DIRECTORY", classes="dialog-title")
            yield Label(self.start, id="picked")
            yield Folders(self.start, id="folders")
            yield Label("Enter opens · Backspace goes up · Ctrl+Enter chooses · Esc cancels",
                        classes="answer-hint")
            with Horizontal(classes="buttons"):
                yield Button("Choose", variant="success", id="choose")
                yield Button("Up", id="up")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.set_focus(self.query_one(Folders))

    @property
    def picked(self) -> str:
        return str(self.query_one("#picked", Label).content)

    @on(DirectoryTree.NodeHighlighted)
    def highlighted(self, event) -> None:
        if event.node.data is not None:
            self.query_one("#picked", Label).update(str(event.node.data.path))

    @on(Button.Pressed, "#choose")
    def action_choose(self) -> None:
        self.action_answer(self.picked)

    @on(Button.Pressed, "#up")
    def action_up(self) -> None:
        tree = self.query_one(Folders)
        parent = os.path.dirname(os.path.abspath(str(tree.path)))
        tree.path = parent
        self.query_one("#picked", Label).update(parent)

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.action_answer(None)



class RenameSession(Dialog):
    """A session's name: Enter keeps it, Esc leaves it as it was. Empty clears it, and the
    session shows its directory's name."""
    BINDINGS = [Binding("escape", "answer", show=False)]

    def __init__(self, name: str):
        super().__init__()
        self.current = name

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("RENAME SESSION", classes="dialog-title")
            yield Input(value=self.current, placeholder="name (empty: its directory's name)", id="new-name")
            with Horizontal(classes="buttons"):
                yield Button("Rename", variant="success", id="ok")
                yield Button("Cancel", id="cancel")

    @on(Input.Submitted)
    @on(Button.Pressed, "#ok")
    def ok(self) -> None:
        self.action_answer(self.query_one("#new-name", Input).value.strip())

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.action_answer(None)



MODEL_WARNING = ("Changing model drops the prompt cache: the session's next request re-reads the whole "
                 "conversation at the full input price (a change made mid-turn applies inside the running "
                 "turn). Effort stays as it is.")


class ChooseModel(Dialog):
    """A hosted session's model, changed live: Enter takes the highlighted one, Esc leaves it
    as it was. Says first that the cache goes with the old model (Q52)."""
    BINDINGS = [Binding("escape", "answer", show=False)]

    def __init__(self, name: str, current: str | None):
        super().__init__()
        self.name_, self.current = name, current

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"MODEL · {self.name_}", classes="dialog-title")
            yield Label(MODEL_WARNING, classes="dialog-prompt", id="model-warning")
            yield OptionList(*[f"{m.name}{' (now)' if m.id == self.current else ''}" for m in models.MODELS],
                             id="models")
            with Horizontal(classes="buttons"):
                yield Button("Change", variant="warning", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        options = self.query_one("#models", OptionList)
        ids = [m.id for m in models.MODELS]
        options.highlighted = ids.index(self.current) if self.current in ids else 0
        self.set_focus(options)

    @on(OptionList.OptionSelected, "#models")
    @on(Button.Pressed, "#ok")
    def ok(self) -> None:
        at = self.query_one("#models", OptionList).highlighted
        self.action_answer(None if at is None else models.MODELS[at].id)

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.action_answer(None)


def ago(epoch: float, now: float | None = None) -> str:
    mins = int(((now or time.time()) - epoch) // 60)
    return f"{mins}m ago" if mins < 60 else f"{mins // 60}h ago" if mins < 48 * 60 else f"{mins // 1440}d ago"


class AdoptSession(Dialog):
    """Pick a session to bring into the wheelhouse. A running one must be /exit-ed first:
    the wheelhouse never kills it, and only launches once it has gone."""
    BINDINGS = [Binding("escape", "answer", show=False)]

    def __init__(self, candidates: list):
        super().__init__()
        self.candidates = {c.id: c for c in candidates}

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="wide"):
            yield Label("ADOPT A SESSION", classes="dialog-title")
            yield DataTable(id="adopt-list", cursor_type="row")
            yield Input(placeholder="name in the wheelhouse", id="adopt-name")
            yield Label("", id="adopt-hint")
            with Horizontal(classes="buttons"):
                yield Button("Adopt", variant="success", id="adopt")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        table = self.query_one("#adopt-list", DataTable)
        table.add_columns("", "last active", "dir", "title")
        for c in self.candidates.values():
            table.add_row(Text("● running", style="bold #ffd300") if c.running_pid else "",
                          ago(c.active), c.cwd, c.title or short(c.id), key=c.id)
        if not self.candidates:
            self.query_one("#adopt-hint", Label).update("No recent sessions to adopt.")
        self.set_focus(table)

    def chosen(self):
        table = self.query_one("#adopt-list", DataTable)
        if not table.row_count:
            return None
        return self.candidates[table.coordinate_to_cell_key((table.cursor_row, 0)).row_key.value]

    @on(DataTable.RowHighlighted, "#adopt-list")
    def highlighted(self, event: DataTable.RowHighlighted) -> None:
        c = self.candidates[event.row_key.value]
        self.query_one("#adopt-name", Input).value = c.name or c.named or c.title[:40]
        self.query_one("#adopt-hint", Label).update(
            "Still running: type /exit in its tab, then press Adopt." if c.running_pid else "")

    @on(DataTable.RowSelected, "#adopt-list")
    @on(Button.Pressed, "#adopt")
    def go(self) -> None:
        c = self.chosen()
        if c is None:
            return
        pid = self.app.client.running_pid(c.id)   # check again: it may have exited, or come back
        if pid:
            self.query_one("#adopt-hint", Label).update(
                f"Still running (pid {pid}): type /exit in its tab, then press Adopt.")
            return
        self.action_answer({"candidate": c, "name": self.query_one("#adopt-name", Input).value.strip()})

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.action_answer(None)



class ForkSession(Dialog):
    """Fork a session (Hub.fork): which of its items move to the fork, all unticked, and
    where its directory is in a linked git worktree, whether the fork works in a new one on a
    new branch (the default; uncommitted changes, if any, stay behind, and it says so) or the
    same one. Answers {"refs", "branch"}, branch None for the same directory."""
    BINDINGS = [Binding("escape", "answer", show=False)]

    def __init__(self, plan):
        super().__init__()
        self.plan = plan

    def compose(self) -> ComposeResult:
        plan = self.plan
        with Vertical(id="dialog", classes="wide"):
            yield Label(f"FORK AS {plan.name}", classes="dialog-title")
            yield Label("A new session carrying on from this conversation so far; this one carries on too.",
                        classes="dialog-prompt")
            if plan.items:
                yield Label("Tick the items that move to it (they stop being this session's):", classes="dialog-prompt")
                yield SelectionList(*[(f"{ref} [{status}] {title}", ref) for ref, _, status, title in plan.items],
                                    id="fork-items")
            else:
                yield Label("No open question or todo or blocked task to move.", classes="dialog-prompt")
            if plan.tree.linked and plan.branch:   # a new worktree only where git says where the main one is
                with RadioSet(id="fork-where"):
                    yield RadioButton("A new worktree, on a new branch from this one's last commit:", value=True,
                                      id="fork-new")
                    yield RadioButton(f"This worktree, {plan.cwd}", id="fork-same")
                yield Input(value=plan.branch, placeholder="new branch", id="fork-branch")
                if plan.tree.dirty:
                    yield Label("This worktree has uncommitted changes: a fork in a new worktree won't see them.",
                                classes="dialog-prompt", id="fork-warning")
            with Horizontal(classes="buttons"):
                yield Button("Fork", variant="success", id="fork-go")
                yield Button("Cancel", id="cancel")

    @on(RadioSet.Changed, "#fork-where")
    def where_changed(self, event: RadioSet.Changed) -> None:
        new = event.pressed.id == "fork-new"
        self.query_one("#fork-branch", Input).disabled = not new
        for warning in self.query("#fork-warning"):
            warning.display = new

    @on(Input.Submitted, "#fork-branch")
    @on(Button.Pressed, "#fork-go")
    def go(self) -> None:
        refs = list(self.query_one("#fork-items", SelectionList).selected) if self.plan.items else []
        branch = None
        if self.plan.tree.linked and self.plan.branch and self.query_one("#fork-new", RadioButton).value:
            branch = self.query_one("#fork-branch", Input).value.strip()
            if not branch:
                self.notify("name the new branch, or choose this worktree", severity="error")
                return
        self.action_answer({"refs": refs, "branch": branch})

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.action_answer(None)


class CompactSession(Dialog):
    """Compact a session run in the wheelhouse: instructions for the compaction (optional: none,
    and the session is asked what to keep) and what follows it (THEN_CHOICES). Answers
    {"notes", "then"}, or None."""
    BINDINGS = [Binding("escape", "answer", show=False)]

    def __init__(self, name: str):
        super().__init__()
        self.name_ = name

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"COMPACT {self.name_}", classes="dialog-title")
            yield Label("Instructions for the compaction (optional: leave it empty and the session is asked "
                        "what to keep):", classes="dialog-prompt")
            yield TextArea(id="compact-notes")
            yield then_radios("compact-dialog-then")
            with Horizontal(classes="buttons"):
                # ids of their own: the app's host buttons (#compact) answer a press that bubbles up
                yield Button("Compact", variant="success", id="compact-go")
                yield Button("Cancel", id="compact-cancel")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        event.stop()   # answered here: not the app's too
        if event.button.id != "compact-go":
            self.action_answer(None)
            return
        notes = self.query_one("#compact-notes", TextArea).text.strip()
        self.action_answer({"notes": emoji.convert(notes) or None, "then": then_choice(self.query_one(RadioSet))})


class ParkSession(Dialog):
    """Park a session run in the wheelhouse: it writes its handoff notes and parks, then with
    Compact first ticked it is compacted before its host stops, nothing sent after. Ticked by
    default while its prompt cache is warm: a compaction reads the whole context, so a cold
    cache saves nothing. Answers {"compact"}, or None."""
    BINDINGS = [Binding("escape", "answer", show=False)]

    def __init__(self, name: str, warm: bool):
        super().__init__()
        self.name_, self.warm = name, warm

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"Park {self.name_}? It writes its handoff notes into its items, then stops.",
                        classes="dialog-prompt")
            yield Checkbox("Compact first, while its prompt cache is " + ("warm" if self.warm else "cold, which saves nothing"),
                           self.warm, id="park-compact")
            with Horizontal(classes="buttons"):
                yield Button("Park", variant="warning", id="park-go")
                yield Button("Cancel", id="park-cancel")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        event.stop()   # answered here: not the app's Park too
        self.action_answer({"compact": self.query_one("#park-compact", Checkbox).value}
                           if event.button.id == "park-go" else None)


class ForgetChoice(Dialog):
    """Stop & Forget on a running session: Let it know (the end request, its end steps as
    any session's) or Just stop it (stopped now and its row deleted, nothing sent to it).
    The default, focused for Enter, is to let it know while its prompt cache is warm, and to
    just stop it once cold: its end steps would re-read the whole context, which a session
    used for a quick look isn't worth. Answers "tell", "stop", or None."""
    BINDINGS = [Binding("l", "answer('tell')", show=False), Binding("j", "answer('stop')", show=False),
                Binding("escape", "answer", show=False)]

    def __init__(self, prompt: str, warm: bool):
        super().__init__()
        self.prompt, self.warm = prompt, warm

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.prompt, classes="dialog-prompt")
            with Horizontal(classes="buttons"):
                yield Button(Text("[L]et it know"), variant="success", id="forget-tell")
                yield Button(Text("[J]ust stop it"), variant="error", id="forget-stop")
                yield Button(Text("Cancel [Esc]"), id="forget-cancel")

    def on_mount(self) -> None:
        self.set_focus(self.query_one("#forget-tell" if self.warm else "#forget-stop", Button))

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        event.stop()   # answered here: not the app's Forget too
        self.action_answer({"forget-tell": "tell", "forget-stop": "stop"}.get(event.button.id))


def forget_prompt(name: str, warm: bool, expires: float | None, context: int, queued: int) -> str:
    """ForgetChoice's question: which choice is the default, and why, from the cache's warmth."""
    if warm:
        why = f"Its prompt cache is warm until {stats.clock(expires)}, so its end steps are cheap: " \
              "let it know (the default)." if expires else "Its prompt cache is warm: let it know (the default)."
    else:
        since = f"cold since {stats.clock(expires)}" if expires else "cold"
        cost = f": its end steps would re-read ~{stats.tokens(context)}" if context else ""
        why = f"Its prompt cache is {since}{cost}, so just stop it (the default)."
    lost = f" {queued} queued answer(s) will be discarded." if queued else ""
    return (f"Stop & Forget {name}? Let it know and it does its end steps (memory, handoff) before it stops; "
            f"just stop it and it stops now, told nothing. Its wheelhouse data is deleted either way.{lost}\n\n{why}")


class Confirm(Dialog):
    BINDINGS = [Binding("y", "answer(True)", show=False), Binding("n", "answer(False)", show=False),
                Binding("escape", "answer(False)", show=False)]

    def __init__(self, prompt: str):
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.prompt, classes="dialog-prompt")
            with Horizontal(classes="buttons"):
                yield Button(Text("[Y]es"), variant="error", id="yes")
                yield Button(Text("[N]o"), id="no")

    def on_mount(self) -> None:
        self.set_focus(self.query_one("#no", Button))   # a reflex Enter must not confirm

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.action_answer(event.button.id == "yes")


class Choice(Dialog):
    """A pending request: cancel it, force it, or leave it be."""
    BINDINGS = [Binding("c", "answer('cancel')", show=False), Binding("f", "answer('force')", show=False),
                Binding("escape", "answer('leave')", show=False)]

    def __init__(self, prompt: str):
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.prompt, classes="dialog-prompt")
            with Horizontal(classes="buttons"):
                yield Button(Text("[C]ancel request"), variant="success", id="cancel")
                yield Button(Text("[F]orce"), variant="error", id="force")
                yield Button(Text("Leave it [Esc]"), id="leave")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.action_answer(event.button.id)


class RelaunchChoice(Dialog):
    """Relaunch of a big session: compact first, relaunch now, or neither."""
    BINDINGS = [Binding("c", "answer('compact')", show=False), Binding("r", "answer('now')", show=False),
                Binding("escape", "answer", show=False)]

    def __init__(self, prompt: str):
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.prompt, classes="dialog-prompt")
            with Horizontal(classes="buttons"):
                # ids of its own: the app's host buttons (#compact) answer a press that bubbles up
                yield Button(Text("[C]ompact first"), variant="success", id="relaunch-compact")
                yield Button(Text("[R]elaunch now"), variant="error", id="relaunch-now")
                yield Button(Text("Cancel [Esc]"), id="relaunch-cancel")

    def on_mount(self) -> None:
        self.set_focus(self.query_one("#relaunch-cancel", Button))   # a reflex Enter must not relaunch

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        event.stop()   # answered here: not the app's too
        self.action_answer({"relaunch-compact": "compact", "relaunch-now": "now"}.get(event.button.id))


class TutorialOffer(Dialog):
    """The first-run offer, a line that wraps on a narrow screen, so the command it names is
    never cut: Enter takes the tutorial, Esc dismisses it for good."""
    BINDINGS = [Binding("enter", "answer(True)", show=False), Binding("escape", "answer(False)", show=False)]

    def compose(self) -> ComposeResult:
        yield Label(Text.assemble(("New here? ", "bold #ffd300"), ("Enter", "bold"), ": take the tutorial · ",
                                  ("Esc", "bold"), f": dismiss · {tutorial.AGAIN}"),
                    id="offer")



def key_name(key: str) -> str:
    """A binding's key as the person reads it: Ctrl+S, Shift+Up, Space, ?."""
    names = {"question_mark": "?", "escape": "Esc", "enter": "Enter", "space": "Space", "pageup": "PgUp",
             "pagedown": "PgDn"}
    if len(key) == 1 and key.isupper():
        return f"Shift+{key}"
    return "+".join(names.get(part, part.capitalize() if len(part) > 1 else part.upper())
                    for part in key.split("+"))


# what each key does, by its action, for the ? overlay; every binding must have one
DESCRIBE = {
    "submit": "Submit what's typed: queued, or sent at once, by the session's mode",
    "app.submit": "Submit what's typed: queued, or sent at once, by the session's mode",
    "send_session": "Send the current session's queue (a dead one's waits: Relaunch it first)",
    "toggle_mode": "Switch the session between Queued and Immediate",
    "recall": ("Take this item's latest queued answer back into the box; with none queued, in the item list, "
               "reopen the highlighted finished item (or every marked one)"),
    "new_session": "New session",
    "adopt": "Adopt: pick a Claude Code session that isn't in the wheelhouse yet, from those on disk",
    "relaunch_all": "Relaunch all: every Available session (not running, not parked)",
    "clear_filter": "Clear a text selection first, then the marks, else show every session's items again",
    "show_finished(True)": "Show or hide finished items",
    "show_finished(False)": "Show or hide finished items",
    "app.close_question": ("Close the highlighted question or decision, or dismiss a done task or subagent "
                           "(or every marked one); it never reopens one: Ctrl+R does"),
    "help": "This list",
    "quit": "Quit the wheelhouse (sessions carry on without it)",
    "toggle_mark": "Mark or unmark the highlighted row",
    "extend(-1)": "Extend the marks up",
    "extend(1)": "Extend the marks down",
    "select_all": "Select all of it, to copy",
    "leave": "Back to the inbox",
    "app.press('rename')": "Rename the highlighted session",
    "app.press('relaunch')": "Relaunch it, or Unpark a parked one",
    "app.press('park')": "Park it, or Unpark a parked one",
    "app.press('end')": "Forget it (Stop & Forget while it's running)",
    "app.press('fork')": "Fork it: a new session carrying on from its conversation",
    "press('interrupt')": "Interrupt the session's current turn",
    "press('compact')": "Compact the session",
    "press('shell')": "Shell: open the session in a Claude Code tab",
    "press('model')": "Model: change the model of a session run in the wheelhouse (its cache goes)",
    "effort(-1)": "A lower effort, applied live",
    "effort(1)": "A higher effort, applied live",
    "effort_end(0)": "The lowest effort",
    "effort_end(-1)": "The highest effort",
    "press('allow')": "Allow the permission item in context; on the question suggesting Compact, Compact",
    "press('always')": "Always: allow it, and keep the rule; on the question suggesting Compact, Leave it",
    "press('deny')": "Deny it; on the question suggesting Compact, just compact after",
    "press('compact-then-send')": "On the question suggesting Compact: send the queued answers after, in the same prompt",
    "press('compact-then-park')": "On the question suggesting Compact: park the session after, sending nothing",
    "press('send-all')": "Send all: every running session's queue (a dead one's waits until it's relaunched)",
    "cursor_up": "Up a row",
    "cursor_down": "Down a row",
    "page_up": "Up a page",
    "page_down": "Down a page",
    "scroll_top": "The first row",
    "scroll_bottom": "The last row",
    "app.focus_next": "The next pane (Tab order below)",
    "app.focus_previous": "The previous pane",
}
# the send bar's buttons and the session area's, by id
BUTTONS = {
    "mode": "Mode (Ctrl+T): the session's send mode, Queued or Immediate",
    "send": "Send (Ctrl+S): send the session's queued answers as one message; its caption counts them",
    "send-all": "Send all (Shift+A), above the footer: send every running session's queue; a dead one's waits until it's relaunched",
    "allow": "Allow (1; over the answer box, on a permission item): let the tool call run, at once",
    "always": "Always (2): allow it, and keep the rule Claude Code suggests",
    "deny": "Deny (3): refuse it",
    "compact-now": ("Compact (1, over the answer box, on the question suggesting it): compact the session, with the "
                    "instructions typed in the box if any, else it's asked what to keep; then as chosen beside it: "
                    "3 just compact, 4 send the queued answers in the same prompt, 5 park it at once"),
    "leave-it": "Leave it (2): close the question; it isn't asked again until the context has been under 200k and passes it again",
    "interrupt": "Interrupt (I): stop the session's current turn, like Esc in Claude Code",
    "compact": ("Compact (C): compact the session with instructions of your own, or with what it says to keep; "
                "then just that, its queued answers sent in the same prompt (the default), or parked at once"),
    "shell": "Shell (H): open the session in a real Claude Code tab; it comes back when you /exit",
    "model": "Model (M): change the model a session run in the wheelhouse runs on, live; its prompt cache goes with the old one",
    "relaunch": ("Relaunch / Unpark (L in the session list): start the session again where it left off, stopping it "
                 "first if it's running (asked only if it's mid-turn); on a parked one, Unpark (P too)"),
    "rename": "Rename (R in the session list): the session's name in the wheelhouse",
    "fork": ("Fork (K in the session list): a new session, \"<name> (fork)\", carrying on from this one's "
             "conversation so far, which carries on too. Ticked items move to it; in a linked git worktree, it "
             "works in a new one on a new branch from the last commit (uncommitted changes stay behind), or this one"),
    "park": ("Park (P in the session list): a running session writes its handoff notes into its items, then stops; "
             "it stays, dimmed, at the foot of the list, its items off the inbox, until Unpark"),
    "end": ("Stop & Forget, or Forget (Delete or Backspace in the session list): a running session asks first, "
            "Let it know (L: it does its own end steps, then stops) or Just stop it (J: stopped now, told nothing), "
            "the first by default while its prompt cache is warm, the second once cold, as its end steps would "
            "re-read its whole context; its wheelhouse data is deleted either way, queued answers with it. One not "
            "running is deleted at once, once you confirm. Adopt brings a forgotten one back"),
}
# keys for the buttons that have no other: each presses its button on the screen in front,
# outside a text box (whose letters and digits are typing). 1, 2 and 3 number the
# permission answers as Claude Code's own prompt does. Shift+A twice, as Shift+L
BUTTON_KEYS = [
    Binding("i", "press('interrupt')", "Interrupt", show=False),
    Binding("c", "press('compact')", "Compact", show=False),
    Binding("h", "press('shell')", "Shell", show=False),
    Binding("m", "press('model')", "Model", show=False),
    Binding("1", "press('allow')", "Allow", show=False),
    Binding("2", "press('always')", "Always", show=False),
    Binding("3", "press('deny')", "Deny", show=False),
    Binding("4", "press('compact-then-send')", "Then send the queued answers", show=False),
    Binding("5", "press('compact-then-park')", "Then park it", show=False),
    Binding("A", "press('send-all')", "Send all", show=False),
    Binding("shift+a", "press('send-all')", "Send all", show=False),
]
# the permission keys, 1 to 3, on the question suggesting Compact: Compact, Leave it and the
# first then-choice; 4 and 5 (BUTTON_KEYS) the other two
NUDGE_KEYS = {"allow": "compact-now", "always": "leave-it", "deny": "compact-then-compact"}
# the inbox's Tab order (WheelhouseApp.cycle_focus): the answer box straight after the
# items, so Ctrl+Enter (which goes back to the items), Down and Tab answer the next one.
# Any other pane that takes focus (a module's) comes after these
TAB_ORDER = ("session-list", "items", "answer", "detail")
# for the ? overlay, keys bound elsewhere: Tab and Shift+Tab are every screen's own
# (app.focus_next, which is cycle_focus), the lists' cursor keys DataTable's
TAB_KEYS = [Binding("tab", "app.focus_next"), Binding("shift+tab", "app.focus_previous")]
LIST_KEYS = [Binding(key, action) for key, action in (
    ("up", "cursor_up"), ("down", "cursor_down"), ("pageup", "page_up"), ("pagedown", "page_down"),
    ("ctrl+home", "scroll_top"), ("ctrl+end", "scroll_bottom"))]
SLIDER_TAB = ", then the stats pane's effort slider while it shows"   # SDK mode's alone (keys_help)
TABS = ("Tab goes round the panes: the session list, the items, the answer box, then the "
        "conversation{slider}, and Shift+Tab back. Ctrl+Enter in the answer box goes back to the items with "
        "the cursor where it was, so Down then Tab answers the next one. An item opened full screen "
        "has two stops, its answer box and its thread. In the conversation or a thread, Up, Down, PgUp, "
        "PgDn, Home and End scroll it. The buttons never take focus: each has a key.")
SEND_RULES = (
    "**Queued** (the default): Ctrl+Enter holds each answer until Ctrl+S (or Send) sends the "
    "session's queue as one message, so related answers arrive together; ✉ counts what's queued. "
    "A dead session's queue waits: Ctrl+S and Send all leave it until the session is relaunched. "
    "**Immediate**: Ctrl+Enter sends at once. Ctrl+T switches the session's mode; Ctrl+R takes a "
    "queued answer back to edit.")
MOUSE = ("Click selects a row; in the session list, a click on the selected session or the list's title "
         "unselects it, for all sessions' items, as Esc does. Ctrl+click marks rows and Shift+click a range (Windows Terminal may "
         "keep Shift+click for itself: Space and Shift+Up/Down do the same), then Delete or Backspace closes them together "
         "and Ctrl+R reopens the finished ones. "
         "Double-click selects a word. Right-click copies the selection, or with none pastes into the answer box, as a terminal does. "
         "Drag the lines between the panes to resize them (they light up under the pointer); the sizes are "
         "kept for next time, and a double-click on a line puts its default back.")
# the SDK-only buttons' keys, and those buttons, which the ? overlay leaves out unless SDK
# mode is on (features.py, EXPERIMENTAL: the author's alone until Anthropic's terms are
# confirmed) or the screen shows them (a host's session, which runs on with it off). Out of
# it, a key of one that doesn't show says it doesn't apply, as any other button's does
SDK_BUTTONS = ("interrupt", "compact", "shell", "model", "allow", "always", "deny", "compact-now", "leave-it",
               "compact-then-send", "compact-then-park")
SDK_WHOSE = (", Model on one run in the wheelhouse; then Mode, Send on one that isn't dead, "
             "and on one run in the wheelhouse Interrupt, Compact and Shell. Its effort is the slider at the top of "
             "the stats pane.")
HOSTED_WHOSE = ("; then Mode, Send on one that isn't dead, and on one run in the wheelhouse Interrupt, Compact "
                "and Shell.")   # SDK mode off, with a host's session in context (SDK_WHOSE without Model or the slider)
WHOSE = ("The buttons under the session list act on the session highlighted there, and only those that "
         "apply show: Rename on a running one, Relaunch (Unpark on a parked one), Forget (Stop & Forget "
         "on a running one) and Fork on any, and Park on one that isn't parked{sdk} Hover over one for what it does, and its "
         "key is in brackets below. New, Adopt and Relaunch all are keys in the footer. An item "
         "opened full screen has Mode, Send and the rest in its bar, for its own session. Ctrl+S and Ctrl+T "
         "act on the same session: highlighting an item highlights its session there.")


def showing(button) -> bool:
    """Whether a button shows: it, and what it sits in (a then-choice: its set and its row)."""
    return bool(button and all(w.display for w in (button, *button.ancestors[:3 if isinstance(button, RadioButton) else 1])))


def keys_help(sdk: bool = False, shown=()) -> str:
    """The ? overlay: every key, from the bindings themselves, and every button. Those of SDK
    mode (SDK_BUTTONS, the effort slider) only with it on (sdk), or those of SDK_BUTTONS the
    screen shows (shown: a host's session's, which run on with it off)."""
    everywhere = [b for b in WheelhouseApp.BINDINGS if b not in BUTTON_KEYS]
    offered = set(SDK_BUTTONS) if sdk else set(shown)
    hidden_keys = {f"press('{b}')" for b in SDK_BUTTONS if b not in offered}
    button_keys = [b for b in BUTTON_KEYS if b.action not in hidden_keys]
    sections = [("Everywhere", everywhere), ("Outside a text box", button_keys),
                ("Moving between panes", TAB_KEYS), ("Either list", LIST_KEYS),
                ("The session list", SessionList.BINDINGS),
                ("The item list", ItemList.BINDINGS),
                ("An answer box", Compose.BINDINGS), ("The conversation pane", Transcript.BINDINGS),
                ("An item opened full screen", ThreadView.BINDINGS),
                *([("The stats pane, with a session run in the wheelhouse in context", SessionStats.BINDINGS)]
                  if sdk else [])]
    out = ["# Keys"]
    for title, bindings in sections:
        out.append(f"## {title}")
        actions: dict[str, list[str]] = {}
        for b in bindings:
            keys = actions.setdefault(b.action, [])
            if key_name(b.key) not in keys:   # Shift+L, bound as both L and shift+l
                keys.append(key_name(b.key))
        # dict.fromkeys: one line for a key bound twice, as F is (its footer label changes)
        out += dict.fromkeys(f"- **{' or '.join(keys)}**: {DESCRIBE[action]}" for action, keys in actions.items())
    whose = WHOSE.format(sdk=SDK_WHOSE if sdk else HOSTED_WHOSE if offered & {"interrupt", "compact", "shell"}
                         else "; then Mode, and Send on one that isn't dead.")
    buttons = [text for id_, text in BUTTONS.items() if id_ not in SDK_BUTTONS or id_ in offered]
    out += ["## Tab order", TABS.format(slider=SLIDER_TAB if sdk else ""), "## Mouse", MOUSE, "## Buttons", whose,
            *[f"- {text}" for text in buttons],
            "## Sending", SEND_RULES,
            "## The tutorial", f"{tutorial.AGAIN} at the next start. Esc or ? closes this list."]
    return "\n\n".join(out)


class KeysHelp(Dialog):
    """? : every key and button, and how sending works. shown: the SDK_BUTTONS the screen
    behind it shows (keys_help)."""
    BINDINGS = [Binding("escape,question_mark,q", "answer", show=False)]

    def __init__(self, shown=()):
        super().__init__()
        self.shown = shown

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="keys-dialog"):
            yield Static(RichMarkdown(keys_help(self.app.client.sdk, self.shown)), id="keys")



class WheelhouseApp(App):
    CSS = f"""
    Screen {{ background: #0a0a12; }}
    #title {{ height: 1; background: #12122a; content-align: center middle; }}
    #main {{ height: 1fr; }}   /* leaves room for the send bar and footer: no screen scroll */
    .panel {{ background: #000000; color: {MATRIX}; border: round #7b61ff; }}
    .panel:focus-within {{ border: round #ff2a6d; }}
    DataTable {{ background: #000000; color: {MATRIX}; }}
    DataTable > .datatable--header {{ background: #12122a; color: #05d9e8; text-style: bold; }}
    DataTable > .datatable--cursor {{ background: #003b0f; color: #ffffff; }}
    /* the splitters' defaults, and the least each pane keeps when one is dragged (Splitter) */
    #sessions-pane {{ width: 39; min-width: 39; }}   /* names keep their 16 cells beside the context bar (#51); every button its caption */
    #items-pane {{ width: 1fr; min-width: 12; }}   /* 39 + 12 + 24 + the splitters: three columns on 80 */
    #items {{ height: 1fr; min-height: 3; }}   /* the top half; the stats the bottom */
    .-split {{ border-top: none; min-height: 3; }}   /* the first module pane under the items: the splitter is its line */
    #detail-pane {{ width: 2fr; min-width: 24; }}
    #detail-scroll {{ height: 1fr; min-height: 3; }}
    #answer {{ min-height: 3; }}
    #detail, #thread {{ background: #000000; color: {MATRIX}; }}
    #answer, #thread-answer {{ height: 8; background: #000000; color: {MATRIX}; border: round #05d9e8; }}
    #answer:focus, #thread-answer:focus {{ border: round #ff2a6d; }}
    /* the default cursor is a pale grey cell: a hot block reads as "type here" on black */
    Compose > .text-area--cursor {{ background: #ff2a6d; color: #000000; text-style: bold; }}
    .answer-hint {{ color: #777777; height: 1; }}
    #thread-scroll {{ height: 1fr; }}
    #session-list {{ height: 1fr; min-height: 5; }}   /* on a short terminal the description gives way first */
    #session-info {{ height: 50%; min-height: 3; background: #000000; padding: 0 1; }}
    #session-info-text {{ color: #e8e8e8; }}
    #session-info-scroll {{ height: 1fr; }}
    /* the activity's own two rows under the description, always there and never taller, so
       nothing above moves or rewraps as it appears, changes or clears (D81) */
    #session-activity {{ height: 2; overflow: hidden; color: #05d9e8; text-style: italic; }}
    /* three to a row, one row each, a blank row between rows (#62). Only the buttons that
       apply show, and a hidden one leaves no hole: the grid places the shown ones in turn.
       The lifecycle buttons, then the conversation's: Mode across two columns, for its
       longest caption, then Send; Interrupt, Compact and Shell under. All 30% grey with
       white text, a little lighter under the pointer and pressed, dimmed when disabled */
    #session-buttons, #conversation-buttons {{ grid-size: 3; grid-columns: 1fr 1fr 1fr; grid-rows: 1;
        grid-gutter: 1 1; height: auto; background: #000000; }}
    #conversation-buttons {{ padding-top: 1; }}   /* padding, not margin: the splitters fit by region */
    #sessions-pane Grid Button {{ width: 1fr; min-width: 0; padding: 0; background: #4d4d4d; color: #ffffff; }}
    #sessions-pane Grid Button:hover {{ background: #5e5e5e; }}
    #sessions-pane Grid Button.-active {{ background: #6e6e6e; }}
    #sessions-pane Grid Button:disabled {{ background: #333333; color: #8c8c8c; }}
    #conversation-buttons #mode, #session-buttons Button.wide {{ column-span: 2; }}
    #dialog {{ width: 80; max-width: 100%; height: auto; max-height: 100%; overflow-y: auto; padding: 1 2;
               background: #000000; color: {MATRIX}; border: thick #ff2a6d; }}
    .dialog-title {{ color: #ffd300; text-style: bold; }}
    .dialog-prompt {{ width: 1fr; }}   /* wraps, the dialog growing with it */
    #dialog TextArea {{ height: 8; }}
    .buttons {{ height: 3; }}
    RenameSession {{ align: center middle; }}
    #dialog.wide {{ width: 120; }}
    #adopt-list {{ height: 16; }}
    #adopt-hint {{ color: #ffd300; }}
    #fork-items {{ height: auto; max-height: 12; background: #000000; }}
    #fork-where {{ background: #000000; }}
    #fork-warning {{ color: #ffd300; }}
    NewSession, PickDirectory, Confirm, Choice, AdoptSession, ForkSession, KeysHelp, ChooseModel {{ align: center middle; }}
    #model-row {{ height: auto; }}
    #model-row Select {{ width: 1fr; }}
    #models {{ height: auto; max-height: 8; background: #000000; }}
    #cwd-row {{ height: auto; }}
    #cwd {{ width: 1fr; }}
    #folders {{ height: 20; background: #000000; }}
    TutorialOffer {{ align: center top; }}
    #offer {{ width: 100%; height: auto; background: #12122a; color: #e8e8e8; padding: 0 1; }}
    #keys-dialog {{ width: 100; max-width: 100%; height: 90%; background: #000000; color: {MATRIX};
                    border: thick #ff2a6d; padding: 0 1; }}
    /* selected text black on white: the theme's dark teal hid the characters (#52) */
    Screen > .screen--selection, TextArea > .text-area--selection, Input > .input--selection {{
        background: #ffffff; color: #000000; }}
    #checklist-scroll {{ height: auto; max-height: 50%; }}   /* the answer box stays on screen at 80 by 24 */
    #checklist {{ height: auto; display: none; background: #000000; color: #e8e8e8;
                  border-bottom: solid #7b61ff; padding: 0 1; }}
    /* one-cell knurled scrollbars (KnurlRender, #55): teal, brighter on hover, white in the hand */
    Widget {{ scrollbar-size-vertical: 1; scrollbar-size-horizontal: 1; scrollbar-color: #05d9e8;
         scrollbar-color-hover: #5ff0fa; scrollbar-color-active: #ffffff; scrollbar-background: #000000;
         scrollbar-background-hover: #000000; scrollbar-background-active: #000000;
         scrollbar-corner-color: #000000; }}
    /* the rule above outranks Textual's own zero sizes, so put them back (NO_BARS): no bars
       on code; none in an Input, whose one row a bar would cover; none on the footer */
    MarkdownFence {{ scrollbar-size-vertical: 0; scrollbar-size-horizontal: 0; }}
    Input {{ scrollbar-size-horizontal: 0; }}
    Footer {{ scrollbar-size-vertical: 0; scrollbar-size-horizontal: 0; }}
    """

    # keys shown in upper case, the usual convention: F is the f key, not Shift+F
    BINDINGS = [
        Binding("ctrl+enter", "submit", "Submit", key_display="Ctrl+Enter"),
        Binding("ctrl+j", "submit", "Submit", show=False),   # Ctrl+Enter, as most terminals send it
        Binding("ctrl+s", "send_session", "Send", key_display="Ctrl+S"),
        Binding("ctrl+t", "toggle_mode", "Mode", key_display="Ctrl+T"),
        Binding("ctrl+r", "recall", "Edit queued", show=False, key_display="Ctrl+R"),
        # New, Adopt and Relaunch all act on no one session, so they're here, not buttons (#62)
        Binding("n", "new_session", "New", key_display="N"),
        Binding("a", "adopt", "Adopt", key_display="A"),
        # Shift+L twice: some terminals send it as L, others as shift+l
        Binding("L", "relaunch_all", "Relaunch all", key_display="Shift+L"),
        Binding("shift+l", "relaunch_all", "Relaunch all", show=False),
        Binding("escape", "clear_filter", "All sessions", key_display="Esc"),
        # one key, two bindings: the footer shows the one that applies (check_action)
        Binding("f", "show_finished(True)", "Show finished", key_display="F"),
        Binding("f", "show_finished(False)", "Hide finished", key_display="F"),
        Binding("question_mark", "help", "Keys", key_display="?"),
        Binding("q", "quit", "Quit", key_display="Q"),
        *BUTTON_KEYS,
    ]

    def __init__(self, store: Store | None = None, client=None):
        """client: the daemon's (client.RemoteClient); else a hub of this app's own, in
        process, on store (--local, and the tests)."""
        super().__init__()
        ScrollBar.renderer = KnurlRender   # Textual's hook for every scrollbar: a class variable
        self.client = client or LocalClient(store, spawn=self.spawn, post=self.post)
        # the hub in process and its store, for --local alone: through the daemon, neither is this app's
        self.hub = None if self.client.remote else self.client
        self.store = self.client.store
        self.kept = Unsent()   # text typed but not submitted: this client's, not the hub's
        self.frame = 0
        self.eye_frame = 0   # the frame the busy sessions' eyes last moved on (animate)
        self.animation = None   # animate's timer: none at 0 fps (features.animation_fps)
        self.started = 0.0   # when the animation's time starts (animate)
        self.half_frame = 0   # the frame // 2 the eyes and panes last moved on (animate)
        self.animating = True   # whether it runs: paused while unfocused with nothing busy (pace)
        self.filter_sid: str | None = None
        self.followed: str | None = None   # the session last followed, which a key picks again (SessionList.reveal)
        # the dwell (DWELL): what the person is looking at, (selection, screen), its generation,
        # bumped whenever that changes, and its one-shot timer, running only while they rest
        self.dwelling: tuple | None = None
        self.dwell_gen = 0
        self.dwell_timer = None
        self.picked: tuple | None = None   # the selection the person last made, not the app (saw)
        # where each list's cursor was last left by the app, or settled (settle): a cursor
        # anywhere else is the person's move, not yet acted on
        self.items_cursor: str | None = None
        self.sessions_cursor: str | None = None
        # the selected item's key and its rank as it was shown when selected: it keeps that
        # place while selected, re-sorting once the selection moves on (ranked)
        self.pin: tuple[str, tuple] | None = None
        # items the person just closed or reopened (close), or whose permission they answered
        # (answer_permission): pinned where they go
        self.repin: set[str] = set()
        self.ranks: dict[str, tuple] = {}   # each item's rank as last shown
        self.show_finished = False   # finished items (store.standing), after the rest
        # each list's rows that change sides (an item settling, a session parked) fall or rise
        # there (T66, T71): a frame repaints the list, but not while a key lands (tick)
        self.session_sink = Sink(lambda: self.landing or not self.screen_stack or self.paint_sessions())
        self.item_sink = Sink(lambda: self.landing or not self.screen_stack or self.paint_items())
        # the highlighted row: (session id, item ref), or (session id, None) for the
        # session's conversation, the first row while a session is selected
        self.selected: tuple[str, str | None] | None = None   # its setter restarts the dwell (look)
        self.detail_follows = False   # the detail pane shows a conversation: it follows its newest turn
        self.modules = api.load()
        # the state dir: the database's, for a module that keeps files of its own
        state_dir = self.store.path.parent if self.store else db_path().parent
        self.ctx = api.Context(state_dir, self.module_sessions, self.stats_sid, usage=self.client.usage,
                               set_effort=self.set_effort)
        self.panes: list[Widget] = []   # the modules' widgets, mounted in their slots
        self.module_ids: dict[str, str] = {}   # each pane's widget id: the module that has it
        self.box_target: tuple | None = None
        self.landing = False   # a key's land under way, which the refresh tick waits out (tick)
        self.held_bars: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()   # by screen (bar_widgets)
        self.said: list[tuple[str, str]] = []   # what to say as it opens, (text, severity): how it found the daemon
        self.client.listen(self.heard)
        # the tutorial steps only the screen sees (a question opened, the conversation
        # followed), by tutorial session

    def compose(self) -> ComposeResult:
        yield TitleBar(id="title")
        with Columns(id="main"):
            # the sessions, the highlighted one's description, and what can be done to it
            with Vertical(id="sessions-pane", classes="panel"):
                yield SessionList(id="session-list", cursor_type="row", show_cursor=False)   # none selected (D20)
                yield Splitter("session-info", "#session-info", "#session-list", "y")
                with Vertical(id="session-info"):
                    with VerticalScroll(id="session-info-scroll"):
                        yield Static(id="session-info-text")
                    yield Static(id="session-activity")
                with Grid(id="session-buttons"):
                    for label, id_ in LIFECYCLE:   # compact: one row each
                        yield Button(label, id=id_, compact=True, tooltip=BUTTONS[id_])
                with Grid(id="conversation-buttons"):
                    for label, id_, _ in CONVERSATION:
                        yield Button(label, id=id_, compact=True, tooltip=BUTTONS[id_])
            yield Splitter("sessions-pane", "#sessions-pane", "#items-pane", "x")
            with Vertical(id="items-pane", classes="panel"):
                yield ItemList(id="items", cursor_type="row")   # then a splitter and the inbox.side panes
            yield Splitter("detail-pane", "#detail-pane", "#items-pane", "x")
            with Vertical(id="detail-pane", classes="panel"):
                with VerticalScroll(id="checklist-scroll"):   # scrolls on a short terminal (review 9)
                    yield Static(id="checklist")
                with VerticalScroll(id="detail-scroll"):
                    yield Transcript(NOTHING_SELECTED, id="detail")
                yield Splitter("answer", "#answer", "#detail-scroll", "y")
                yield PermissionButtons()
                yield CompactButtons()
                yield Compose(id="answer")
                yield Hint(classes="answer-hint")
        yield SendBar()
        yield Footer()

    async def on_mount(self) -> None:
        await self.mount_panes()
        # held, not queried: timers fire while a dialog is on top and during shutdown
        self.title_bar = self.query_one("#title", TitleBar)
        self.items_table = self.query_one("#items", ItemList)
        self.detail = self.query_one("#detail", Transcript)
        self.detail_scroll = self.query_one("#detail-scroll", VerticalScroll)
        self.answer = self.query_one("#answer", Compose)
        self.session_list = self.query_one("#session-list", SessionList)
        self.session_list.sink, self.items_table.sink = self.session_sink, self.item_sink
        self.session_info = self.query_one("#session-info-text", Static)
        self.session_activity = self.query_one("#session-activity", Static)
        # out of the Tab order, which goes from the session list straight to the items: the
        # buttons still click, and the list's own keys press them (SessionList)
        self.conversation_buttons = self.query_one("#conversation-buttons", Grid)
        self.session_buttons = self.query_one("#session-buttons", Grid)
        self.session_controls = list(self.query("#sessions-pane Grid, #sessions-pane Grid Button"))   # theirs and they
        # the conversation's one stop is its transcript, not this scroll too: its keys
        # still scroll it, an ancestor's bindings (TAB_ORDER)
        for widget in (self.query_one("#session-info-scroll"), self.query_one("#checklist-scroll"), self.detail_scroll,
                       *self.query("#sessions-pane Grid Button")):
            widget.can_focus = False
        for splitter in self.query(Splitter):   # the sizes the person dragged to, last time
            if (stored := share(self.client.setting(LAYOUT + splitter.key))) is not None:
                splitter.apply(stored)
        self.eye_col = self.session_list.add_columns("", "session", "ctx", "?", "D", "✉", "")[-1]
        self.checklist = self.query_one("#checklist", Static)
        self.client.start(self.post)   # the daemon's stream, now there's an app to tell
        self.client.drop_ended_tutorial()   # an ended tutorial's checklist lasts only the app run it ended in
        self.started = self.now()
        if fps := features.animation_fps():   # 0: no animation, the title and eyes drawn still
            self.animation = self.set_interval(1 / fps, self.animate)
        self.paint_title()
        self.set_interval(1.0, self.tick)
        # a screen over the inbox, or a thread opened, is a break in looking: the dwell restarts
        self.screen_change_signal.subscribe(self, lambda _: self.look(), immediate=True)
        self.refresh_data()
        # what's loaded by the first paint lives as long as the app: kept out of the full
        # collections, which otherwise walk it all, a pause typing can feel, every few seconds
        self.call_after_refresh(lambda: (gc.collect(), gc.freeze()))
        for text, severity in self.said:
            self.notify(text, severity=severity)
        if self.client.should_offer():
            self.push_screen(TutorialOffer(), self.offer_answered)

    def on_unmount(self) -> None:
        """The daemon's stream let go as the app closes: nothing posts to it after."""
        self.client.disconnect()

    def fit_layout(self) -> None:
        """The panes' sizes kept within the terminal: one kept from a wider one shrinks."""
        if self.screen_stack:
            fit(self.screen_stack[0].query(Splitter))

    def refit(self) -> None:
        """Fit again once laid out: a pane's content changed height, not the terminal."""
        self.call_after_refresh(self.fit_layout)

    def on_click(self, event: events.Click) -> None:
        if event.chain == 2:
            select_word(self.screen, event.screen_x, event.screen_y)

    # right-click, as in a terminal

    async def on_event(self, event: events.Event) -> None:
        """Right-click copies the selection, or with none pastes. The press never reaches
        the widgets: an answer box would move its cursor and drop its selection, and the
        screen would take it for a click and clear its own."""
        if isinstance(event, (events.MouseDown, events.MouseUp)) and event.button == 3 and not event.is_forwarded:
            if isinstance(event, events.MouseDown):
                self.right_click(event)
            return
        if isinstance(event, events.Key) and not event.is_forwarded:
            await self.take_key(event)
            return
        await super().on_event(event)

    async def take_key(self, event: events.Key) -> None:
        """Every key, one rule: it acts once what the keys before it set in motion has landed
        (land), on what has focus then, so a burst (type-ahead) ends as the same keys typed
        slowly do. Its binding acts at once, from the focus up as Textual resolves them (a key
        a text box types is no one else's); a key no binding takes goes to the focus.
        Textual's own way sent each key to a widget as it arrived but acted on its binding
        only once it came back up, so a later key could move the focus or a cursor first."""
        self.app_focus = True
        await self.land()
        if self._exit or not self.screen_stack:   # quitting (Q), or quit as it landed: nothing to act on
            return
        if self.focused is not None:
            self.screen._clear_tooltip()
        if not (await self._check_bindings(event.key, priority=True) or await self._check_bindings(event.key)):
            (self.focused or self.screen)._forward_event(event)
        if isinstance(self.focused, (TextArea, Input)):   # queued behind the key, which restarts the blink if sent there
            self.focused.call_later(self.hold_cursor, self.focused)

    def hold_cursor(self, box: TextArea | Input) -> None:
        """A key pressed in a text box: its cursor shows solid until QUIET passes with no key,
        so it can be seen while a key is held. Textual restarts the blink only for a key the
        box itself gets, and a key that moves the cursor is a binding (take_key)."""
        box._pause_blink(visible=True)
        # each box its own spell: a key in another (a dialog's, over this one) leaves it running,
        # as closing the dialog gives this box no Focus to restart its blink
        if (timer := getattr(box, "quiet_timer", None)) is not None:
            timer.stop()
        # a box that has lost focus meanwhile keeps the hidden, paused cursor Textual gave it
        box.quiet_timer = self.after(QUIET, lambda: box.has_focus and box._restart_blink())

    async def _on_key(self, event: events.Key) -> None:
        # a key the widgets let through: its bindings were tried as it arrived (take_key)
        event.prevent_default()

    def right_click(self, event: events.MouseDown) -> None:
        """Copies the selection in a pane, else in the box clicked (or focused); with none,
        pastes into that box."""
        try:
            under, _ = self.screen.get_widget_at(event.screen_x, event.screen_y)
        except Exception:
            under = None
        box = under if isinstance(under, (TextArea, Input)) else self.focused
        box = box if isinstance(box, (TextArea, Input)) else None
        text = self.screen.get_selected_text() or (box and box.selected_text)
        if text:
            self.copy_to_clipboard(text)
            self.screen.clear_selection()
            self.notify("copied")
        elif box is not None and not getattr(box, "read_only", False):
            self.screen.set_focus(box)
            # exclusive: a second right-click before the clipboard answers supersedes the first
            self.run_worker(functools.partial(self.paste_into, box), thread=True, group="paste", exclusive=True,
                            exit_on_error=False)

    def paste_into(self, box) -> None:
        """On a worker thread: the system clipboard, else the wheelhouse's own last copy,
        pasted as the terminal's own paste arrives, into the box (focused by now). Checked
        again on the UI thread: a later right-click can cancel this one, or focus move, while
        the paste waits there."""
        worker = get_current_worker()
        text = system_clipboard()
        text = self.clipboard if text is None else text

        def land() -> None:
            if self.focused is box and not worker.is_cancelled:
                self.post_message(events.Paste(text))
        if text and not worker.is_cancelled:
            self.call_from_thread(land)

    # periodic work

    @staticmethod
    def now() -> float:
        """The animation's clock (animate): a test runs it on one of its own."""
        return time.monotonic()

    def animate(self) -> None:
        """A frame: everything where the time elapsed puts it, counted in FRAMES a second, so
        at any rate (features.animation_fps) the shimmer, the eyes and the bars move at the
        same speed; a lower rate draws fewer of the frames."""
        self.frame = round((self.now() - self.started) * FRAMES)
        self.paint_title()
        # the eyes and panes rest while you type, nothing drawn but what the keys change
        # (D41). No screen at all while the app shuts down, when asking what has focus would raise
        half = self.frame // 2   # FRAMES / 2 a second, as in the dashboard
        if half != self.half_frame and self.screen_stack and not isinstance(self.focused, Compose):
            self.half_frame = half
            self.sweep_eyes()
            self.each_pane("animate", half)

    def tick(self) -> None:
        """The refresh, each second, skipped while a key lands (land): a refresh settles, and a
        burst's arrows are settled by the keys behind them that act on the cursor, or once the
        burst is done (settle), not wherever a tick falls. Nor once the screens are gone: the
        timer can still fire while the app shuts down, and there is nothing left to paint."""
        if not self.landing and self.screen_stack:
            self.paint_title()   # the daemon out of reach, said even while the animation rests
            self.refresh_data()

    def paint_title(self) -> None:
        """The title at this frame: one line, never a new size. The daemon out of reach says so
        there, until it's back. Drawn only when its look changes (TitleBar.show)."""
        if self.client.connected:
            self.title_bar.show(shimmer_phase(self.frame), shimmer(self.frame))
        else:
            self.title_bar.show("unreachable", UNREACHABLE)

    def pace(self) -> None:
        """The animation paused while the terminal is unfocused and no session is busy (Doug's
        Q82, option A), resumed when either changes: a still window costs next to nothing. A
        terminal that never reports focus counts as focused (Textual's app_focus starts True)."""
        run = self.app_focus or bool(self.session_list.eyes)
        if self.animation is not None and run and not self.animating:
            self.animation.resume()
        elif self.animation is not None and not run and self.animating:
            self.animation.pause()
        self.animating = run

    def refresh_data(self) -> None:
        self.client.refresh()
        if self.filter_sid and not any(s["id"] == self.filter_sid for s in self.sessions):
            self.clear_filter()   # the followed session went (ended elsewhere): as Esc, no ghost row
        self.client.background()   # in process, the hub's reading: the daemon's hub does its own
        self.paint_sessions()
        self.paint_items()
        self.each_pane("tick")
        self.client.finish_relaunches()
        self.paint_session_info()
        self.paint_sendbar()
        self.paint_checklist()
        if isinstance(self.screen, ThreadView) and self.screen.is_mounted:   # not before its widgets exist
            self.screen.paint()   # an action here (queue, take back) shows at once

    @property
    def selected(self) -> tuple[str, str | None] | None:
        """The highlighted row: (session id, item ref), or (session id, None) for the session's
        conversation. Setting it restarts the dwell (look), whatever made the selection."""
        return self._selected

    @selected.setter
    def selected(self, value: tuple[str, str | None] | None) -> None:
        self._selected = value
        self.look()

    def look(self) -> None:
        """What the person is looking at, the selection on this screen, may have changed: if
        it has, the dwell starts again. One one-shot timer, restarted on each change and none
        while nothing is selected or the terminal has lost focus: a pass over an item, a rest
        shorter than DWELL, or a look away from the window, leaves it as it was (dwelt)."""
        if not self.is_running or not self.screen_stack:   # before the app runs, or as it shuts down
            return
        at = (self._selected if self.app_focus else None, self.screen)
        if at == self.dwelling:
            return
        self.dwelling = at
        self.dwell_gen += 1
        if self.dwell_timer is not None:
            self.dwell_timer.stop()
            self.dwell_timer = None
        if at[0] is not None:
            gen = self.dwell_gen
            # fired on the timer's own task: it waits its turn in the app's queue, behind any key
            # being handled, as a key does
            self.dwell_timer = self.after(DWELL, lambda: self.call_later(self.dwelt, gen))

    def after(self, delay: float, callback: Callable[[], None]):
        """A one-shot timer with stop(), the dwell's (look) or the quiet spell's (hold_cursor):
        a test runs it on a clock of its own."""
        return self.set_timer(delay, callback)

    def watch_app_focus(self) -> None:
        """The terminal window lost focus or got it back (AppBlur, AppFocus): a break in
        looking, and the dwell starts again on return, and the animation rests or runs again
        (pace). A terminal that doesn't report focus never sends either."""
        self.look()
        self.pace()

    def dwelt(self, gen: int) -> None:
        """The selection has been in sight for DWELL without a break: an unseen decision becomes
        seen, whatever selected it (the app's own selection, at startup or once an item goes,
        included), and a tutorial step the person's own selection makes ticks (saw). Only if it
        is still what they look at: the same selection, once the cursors have settled (a
        burst's arrows may have moved them), on the inbox or in the selected item's thread,
        not under a dialog."""
        self.settle()
        if gen != self.dwell_gen or self.selected is None:
            return
        self.dwell_timer = None
        sid, ref = self.selected
        screen = self.screen
        if not (screen is self.screen_stack[0]
                or isinstance(screen, ThreadView) and (screen.sid, screen.ref) == (sid, ref)):
            return
        if self.selected == self.picked:
            self.saw(sid, ref)
        if ref and self.client.mark_seen(sid, ref):   # a no-op unless it's an unseen decision
            self.paint_sessions()   # its D count
            self.paint_items()   # it keeps its place while selected (ranked), and sinks once left
            self.paint_detail()
            if isinstance(screen, ThreadView) and screen.is_mounted:
                screen.paint()

    @property
    def viewing(self) -> str | None:
        """The session whose conversation the right pane follows, if that row is highlighted."""
        return self.selected[0] if self.selected and self.selected[1] is None else None

    # the hub's sessions and their liveness, as the widgets read them (client.py)

    @property
    def sessions(self) -> list:
        return self.client.sessions

    @property
    def statuses(self) -> dict[str, str]:
        return self.client.statuses

    @property
    def relaunching(self):
        """The sessions a Relaunch is under way for."""
        return self.client.relaunching

    @property
    def contexts(self) -> dict[str, stats.UsageFollower]:
        """The hub's context followers: in process alone (--local)."""
        return self.hub.contexts

    @property
    def agent_watchers(self) -> dict[str, subagents.AgentWatcher]:
        return self.hub.agent_watchers

    def running(self, sid: str) -> bool:
        return self.client.running(sid)

    def dead(self, sid: str) -> bool:
        return self.client.dead(sid)

    def stale(self, sid: str) -> bool:
        return self.client.stale(sid)

    def sends_now(self, sid: str) -> bool:
        return self.client.sends_now(sid)

    def pending(self, sid: str) -> str | None:
        return self.client.pending(sid)

    def shown_status(self, sid: str) -> str:
        return self.client.shown_status(sid)

    def busy(self, sid: str) -> bool:
        return self.client.busy(sid)

    def heard(self, event: Event) -> None:
        """What the hub says: a notice is a toast; a transcript read that landed repaints;
        the daemon lost or found again is said, and the title bar shows it lost."""
        if event.kind in ("notice", "link"):
            self.notify(event.text, severity=event.severity)
        elif event.kind == "landed":
            self.read_landed(event.sid)

    def told(self, done: Done) -> Done:
        """What a command of the hub's did: its notices are toasts here, as this app gave it."""
        for notice in done.notices:
            self.heard(notice)
        return done

    def spawn(self, work, group: str) -> None:
        """The hub's work, on a worker thread, never the UI's."""
        self.run_worker(work, thread=True, group=group, exit_on_error=False)

    def post(self, callback) -> bool:
        """From a worker thread: the hub's callback, on the UI thread. False if the app is
        closing, so it never runs."""
        try:
            self.call_from_thread(callback)
        except RuntimeError:   # the app is closing
            return False
        return True

    def read_landed(self, sid: str) -> None:
        self.paint_sessions()
        self.each_pane("landed", sid)   # a repaint, not a tick: a tick would start another read

    def context_cell(self, s) -> Text | str:
        size = self.client.context_size(s)
        return stats.context_bar(size) if size else ""

    def sweep_eyes(self) -> None:
        """Move the Cylon eye on busy rows without rebuilding the table (SessionList.sweep)."""
        self.eye_frame = self.frame
        self.session_list.sweep(self.eye_frame)

    def paint_sessions(self) -> None:
        """Every session: parked ones dimmed, at the foot of the list's room (they fall there
        as they park, and rise as they're unparked: Sink), so the buttons below still reach
        them (Unpark, Forget)."""
        table = self.session_list
        keep, key = table.final_row(), self.current_session()   # in the final order, as the items' (paint_items)
        pending = key != self.sessions_cursor   # the person's move, not yet settled
        rows, table.eyes = [], {s["id"] for s in self.sessions if self.busy(s["id"])}
        self.pace()   # a session busy, or none now: the animation runs, or may rest
        for s in sorted(self.sessions, key=lambda s: s["parked"]):   # stable: creation order within each
            st = self.shown_status(s["id"])
            busy = Text(" " * SessionList.EYE) if s["id"] in table.eyes else Text("")   # the eye's room (sweep)
            dot = Text("●", style="#777777" if s["parked"] else STATUS_STYLE[st])
            name = Text(self.display_name(s), style="dim" if s["parked"] else "")
            if self.stale(s["id"]):
                name.append(" ⟳", style="bold #ff2a6d")
            queued = Text(f"✉ {s['drafts']}", style="bold #05d9e8") if s["drafts"] else ""
            # decisions inform, they don't block: counted, but not blinking like questions
            unseen = Text(str(s["unseen_decisions"]), style=DECISION) if s["unseen_decisions"] else ""
            q = Text(str(s["open_questions"]), style="bold #ffd300 blink") if s["open_questions"] else ""
            rows.append((s["id"], (dot, name, self.context_cell(s), q, unseen, queued, busy)))
        cells = dict(rows)
        room = table.scrollable_content_region.height - table.header_height   # above any scrollbar
        laid = self.session_sink.lay(self, list(cells), {s["id"] for s in self.sessions if s["parked"]}, room)
        rows = [(k, cells[k]) if k else (f"{BLANK}{i}", ("",) * len(table.columns)) for i, k in enumerate(laid)]
        if fill(table, rows) and table.row_count:
            # by key: Park and Unpark move the row, and the cursor goes with it. Not the person
            # moving it, so it changes nothing else (settle)
            with table.prevent(DataTable.RowHighlighted):
                table.move_cursor(row=table.get_row_index(key) if key in table.rows
                                  else min(keep, table.row_count - 1), animate=False)
        if not pending:   # where the app put it; the person's move stays theirs to settle
            self.sessions_cursor = self.current_session()

    display_name = staticmethod(display_name)

    def paint_items(self) -> None:
        """The inbox: active items in inbox order, then the settled, dimmed, sinking there as
        they settle (Sink) once they aren't selected (ranked), then with F the finished. The
        session column shows only where the inbox has every session's items (D31)."""
        table = self.items_table
        columns = ("ref", "status", "title") if self.filter_sid else ("session", "ref", "status", "title")
        if tuple(str(c.label) for c in table.ordered_columns) != columns:
            with table.prevent(DataTable.RowHighlighted):
                table.clear(columns=True)
                table.add_columns(*columns)
        # in the final order, not the frame's: a burst's keys land between frames, so a row that
        # goes falls to the neighbour it has once moves end, as typed slowly (SinkList)
        keep = table.final_row()
        rows_out = []
        names = {s["id"]: s["name"] or short(s["id"]) for s in self.sessions}
        found, processing, queued, awaiting = self.client.inbox(self.filter_sid)
        busy = queued | awaiting
        keyed = self.ranked_keyed(found, processing, busy)
        key_of = {id(it): key for key, it in keyed}   # each item's row key, made once
        active, settled, finished = bands = ([], [], [])   # by their ranks as shown: a selected item's held
        for key, it in keyed:
            bands[band(self.ranks[key])].append(it)
        rows = item_rows(active, names) + item_rows(settled, names) + item_rows(finished if self.show_finished else [], names)
        sunk = {key_of[id(it)] for it in settled + finished}
        if self.filter_sid:   # the session's own conversation, pinned first
            general = (self.filter_sid, None) in queued
            rows_out.append((f"{self.filter_sid}|", (names.get(self.filter_sid, "")[:14], Text("💬"),
                             Text("queued", style="bold #05d9e8") if general else "",
                             Text("Conversation", style="bold"))))
        for it, nested in rows:
            # an unfinished item of any kind with an answer waiting to be sent shows as queued, and one
            # whose answer went with no reply since as processing (D28); neither is stored
            key = (it["session_id"], it["ref"])
            where = standing(it, busy)
            status = it["status"] if where != "active" else "queued" if key in queued \
                else "processing" if key in processing else it["status"]
            style = "dim" if where != "active" else "bold #05d9e8" if status == "queued" \
                else "bold #ffd300" if status == "open" else DECISION if status == "unseen" \
                else "bold #ff2a6d" if status in ("blocked", "waiting") else MATRIX
            name = names.get(it["session_id"], "")[:14]
            if status == "processing" or key in awaiting and status != "queued":
                # the person spoke last: the ball is in the session's court until it replies. A
                # finished item keeps its status, with the hourglass
                shown = Text(status if status == "processing" else f"⏳ {status}", style="dim")
                title = Text(it["title"], style="dim")
            else:
                shown, title = Text(status, style=style), Text(it["title"], style="dim") if where != "active" else it["title"]
            if nested is None:
                cells = (name, it["ref"], shown, title)
            else:   # a subagent, tucked under its session's name
                cells = (name if nested == 0 else "", Text(f"└ {it['ref']}", style="dim"),
                         shown, Text(it["title"], style="dim"))
            rows_out.append((key_of[id(it)], cells))
        if self.filter_sid:   # the session column goes: the inbox follows the one session
            rows_out = [(k, cells[1:]) for k, cells in rows_out]
        cells = dict(rows_out)
        rows_out = [(k, cells[k]) for k in self.item_sink.lay(self, list(cells), sunk) if k]
        table.marked &= {k for k, _ in rows_out}   # a marked item that went is unmarked
        rows_out = [(k, marked(cells) if k in table.marked else cells) for k, cells in rows_out]
        arrow = table.cursor_key()
        pending = arrow != self.items_cursor   # the person's move, its highlight not yet handled
        rebuilt = fill(table, rows_out)
        pending = pending and arrow in table.rows
        # the cursor goes by key, rebuilt or not: items arriving above must not move it, and a
        # selection made without it (following the session already followed) takes it. Except
        # to the person's arrow, which settle selects
        key = arrow if pending else self.selected and f"{self.selected[0]}|{self.selected[1] or ''}"
        with table.prevent(DataTable.RowHighlighted):
            if key in table.rows:
                if key != table.cursor_key():
                    table.move_cursor(row=table.get_row_index(key), animate=False)
            elif rebuilt and table.row_count:
                table.move_cursor(row=min(keep, table.row_count - 1), animate=False)
        if not pending:   # where the app put it; an arrow pending stays pending
            self.items_cursor = table.cursor_key()
        if rebuilt and key not in table.rows and table.row_count:   # its item went: take the one now
            # under the cursor, the list going with it: one current session (D22)
            self.select_row(table.coordinate_to_cell_key((table.cursor_row, 0)).row_key.value)
        if not table.row_count:   # its item (or followed session) went and none is left: nothing selected
            self.selected = None
        if self.selected is None and self.box_target is not None:   # nor the box aimed at what went
            self.retarget(move_list=False)
        self.paint_detail()

    def ranked(self, items, processing, busy=frozenset()) -> list:
        """Items in inbox order (ranked_keyed)."""
        return [it for _, it in self.ranked_keyed(items, processing, busy)]

    def ranked_keyed(self, items, processing, busy=frozenset()) -> list[tuple[str, dict]]:
        """Items in inbox order (inbox_rank), but for the selected item, pinned: it keeps the
        rank it was shown with when it was selected, so its own change (a decision seen, a
        question answered, a task done) never moves it under the person. It re-sorts once the
        selection moves on, and a settled item then sinks (Sink). The exceptions are the person's
        own explicit acts, Delete closing or Ctrl+R reopening (close) and a permission answered
        (answer_permission): it goes at once (D33), and is pinned where it went. self.ranks keeps each item's rank as shown: the next pin's, and what has
        somewhere to go (paint_items). No two items share a rank (store.inbox_rank, ties by id), so
        the order needs no tie-break of its own (#70)."""
        key = self.selected and self.selected[1] and f"{self.selected[0]}|{self.selected[1]}"
        if self.pin is None or self.pin[0] != key:
            self.pin = (key, self.ranks[key]) if key in self.ranks else None
        keyed = [(item_key(it), it) for it in items]   # (row key, item): each key made once
        ranks = {key: inbox_rank(it, processing, busy) for key, it in keyed}
        if self.pin and self.pin[0] in ranks:
            if self.pin[0] in self.repin:   # Delete, Ctrl+R or a permission answered: at once, and held there
                self.pin = (self.pin[0], ranks[self.pin[0]])
            else:
                ranks[self.pin[0]] = self.pin[1]
        self.repin = set()
        self.ranks = ranks
        return sorted(keyed, key=lambda pair: ranks[pair[0]])

    def keep_unsent(self, box, old, new) -> None:
        """Park what's typed for the old target and bring back what was typed for the new one."""
        if old == new:
            return
        if old is not None:
            self.kept.keep(old, box.text)
        text = self.kept.take(new) if new is not None else ""
        if box.text != text:
            box.text = text   # which puts the cursor at the start: carry on typing at the end
            box.move_cursor(box.document.end)

    @property
    def unsent(self) -> dict[tuple, str]:
        return self.kept.texts

    def retarget(self, move_list: bool = True) -> None:
        """The answer box follows what the pane shows, and the session list's highlight its
        session (unless move_list is False). Called on the person's selections only, never
        from the refresh tick, so text being typed is never swapped under them."""
        if move_list:
            self.highlight_session(self.focus_sid())
        self.paint_sendbar()
        if isinstance(self.screen, ThreadView):   # which holds its item's text itself
            return
        target = self.selected
        self.keep_unsent(self.answer, self.box_target, target)
        self.box_target = target

    def item_blocks(self, sid: str, ref: str, session_name: str = "") -> tuple[list[tuple[str, str]], bool]:
        """An item's thread (thread_blocks), and for a subagent's, under it, the subagent's own
        conversation, followed live (Hub.agent_conversation). Whether it has one."""
        agent = self.client.agent_conversation(sid, ref)
        return thread_blocks(self.client.thread(sid, ref), session_name) + (agent or []), agent is not None

    def paint_detail(self) -> None:
        agent = False
        if self.viewing:
            blocks = self.conversation(self.viewing)
        elif self.selected:
            blocks, agent = self.item_blocks(*self.selected)
        else:   # what was shown has gone (a followed session ended, say): not left standing
            blocks = [("note", NOTHING_SELECTED)]
        if not agent and not isinstance(self.screen, ThreadView):   # which may follow one itself
            self.client.unfollow_agent()
        # a conversation, the session's or a subagent's, shows its newest turn, and follows it
        self.detail_follows = bool(self.viewing or agent)
        # a turn of Claude's arriving in the conversation shown, the person's own pick: the
        # tutorial's Watch it work, if it's the tutorial's (saw). Their own words don't count
        turns = {md for who, md in blocks if who in ("claude", "tool")} if self.viewing else set()
        (was, before), self._shown_turns = getattr(self, "_shown_turns", (None, set())), (self.viewing, turns)
        if self.viewing and was == self.viewing and turns - before and self.selected == self.picked:
            self.saw(self.viewing, None, "follow")
        text = "\n\n".join(md for _, md in blocks)
        if text != getattr(self, "_detail_text", None):
            following = self.detail_follows and self.detail_scroll.scroll_y >= self.detail_scroll.max_scroll_y - 1
            self._detail_text = text
            self.detail.update(render(blocks))
            if following:   # stay at the newest turn, unless the person has scrolled up to read
                self.call_after_refresh(self.detail_scroll.scroll_end, animate=False)

    def conversation(self, sid: str) -> list[tuple[str, str]]:
        blocks = self.client.conversation(sid)
        if blocks is None:   # the session went
            self.selected = None
            return [("note", "_gone_")]
        return blocks

    # the modules' panes (api.py)

    async def mount_panes(self) -> None:
        """The modules' panes, mounted once the app's own widgets are, so a module whose id
        one of them has gets a card rather than shadowing it."""
        taken = {w.id for w in self.screen.query("*") if w.id}
        for slot, parent in SLOT_PARENTS.items():
            widgets = list(self.hosted(slot, taken))
            if widgets:   # the first, under the items, below a splitter that sizes the items
                widgets[0].add_class("-split")
                widgets.insert(0, Splitter("items", "#items", ".-split", "y"))
            await self.query_one(parent).mount_all(widgets)

    def hosted(self, slot: str, taken: set[str]):
        """The widgets for a slot: each module's pane, or a card saying why it isn't running
        (it didn't load, its factory raised or made no widget, or its id is taken by another
        module or by the app's own widgets, `taken`)."""
        for item, pane in api.panes(self.modules, slot):
            try:
                if pane is None:
                    raise RuntimeError(item.error)
                if (other := self.module_ids.get(item.module.id)) is not None:
                    raise RuntimeError(f"its id {item.module.id!r} is taken by {other}")
                if item.module.id in taken:
                    raise RuntimeError(f"its id {item.module.id!r} is taken by the wheelhouse")
                widget = pane.surfaces["tui"](self.ctx)
                if not isinstance(widget, Widget):
                    raise TypeError(f"its tui surface made a {type(widget).__name__}, not a widget")
                widget.id = item.module.id
                self.module_ids[item.module.id] = item.name
                self.panes.append(widget)
            except Exception as e:
                widget = Static(Text(f"{item.name}: {e}", style="dim"), classes="module-error")
            yield widget

    def each_pane(self, hook: str, *args) -> None:
        """Calls a hook on every pane that has it. One that raises is swapped for a card
        saying so; the app and the other panes carry on. Textual's own Widget.animate is
        not the hook: a pane without one isn't called."""
        for widget in list(self.panes):
            try:
                if getattr(type(widget), hook, None) not in (None, getattr(Widget, hook, None)):
                    getattr(widget, hook)(*args)
            except Exception as e:
                self.panes.remove(widget)
                # it keeps -split, so the splitter above still finds the pane under it
                card = Static(Text(f"{widget.id}: {type(e).__name__}: {e}", style="dim"),
                              classes=" ".join(["module-error", *widget.classes & {"-split"}]))
                widget.parent.mount(card, after=widget)
                widget.remove()

    def focus_sid(self) -> str | None:
        """The session in context, for the modules: followed (the current session, D20), else
        the highlighted item's. With neither, None: the stats pane shows every running session."""
        sid = self.filter_sid or (self.selected[0] if self.selected else None)
        return sid if any(s["id"] == sid for s in self.sessions) else None

    def stats_sid(self) -> str | None:
        """The session the stats pane shows: the selected one alone. With none selected, not
        the highlighted item's (focus_sid), whose context would read as every session's: the
        pane shows all sessions' totals then."""
        return self.filter_sid if any(s["id"] == self.filter_sid for s in self.sessions) else None

    def module_sessions(self) -> list[dict]:
        return [{"id": s["id"], "name": s["name"] or short(s["id"]), "running": self.running(s["id"]),
                 "context": host_context(s), **self.client.model_state(s)} for s in self.sessions]

    def set_effort(self, sid: str, effort: str) -> None:
        """The stats pane's slider moved: written now, and the panes painted from it, so the
        next key of a burst moves on from there. Said only if it didn't take."""
        try:
            done = self.client.set_effort(sid, effort)
        except (SessionGone, ValueError) as e:
            self.notify(str(e), severity="warning")
            return
        if done.notices and not done.ok:
            self.told(done)
        self.refresh_data()

    def lifecycle_buttons(self, s) -> list[tuple[str, str, bool, bool]]:
        """A session's lifecycle buttons (LIFECYCLE), as show_button takes them, each shown
        only where it applies (#62, D26), captioned for its state (hub.VERBS): Rename on a
        running (Active) session; Relaunch on any, captioned Unpark on a Parked one; Park on
        any that isn't Parked; Forget on any, captioned Stop & Forget on an Active one; Fork
        on any; and Model on one running in a wheelhouse host (Q52: never a tab's) while SDK mode is on:
        out of it, a host's session runs on as it was set (features.py)."""
        if s is None:   # none selected: shown, disabled, but Model, which few sessions have
            return [(id_, caption, True, id_ != "model") for caption, id_ in LIFECYCLE]
        state = self.state(s)
        hosted = state == "active" and runner(s) == "sdk" and not s["shell"]
        caption = {verb: VERBS.get((state, verb), ("", False))[0] for verb in ("relaunch", "park", "end")}
        return [("rename", "Rename", False, state == "active"),
                ("relaunch", caption["relaunch"], False, True),
                ("park", "Park", False, state != "parked"),
                ("end", caption["end"], False, True),
                ("model", "Model", False, hosted and self.client.sdk),
                ("fork", "Fork", False, True)]

    def state(self, s) -> str:
        """A session's state (hub.VERBS): active, available or parked."""
        return hub_state(self.statuses.get(s["id"], "dead"), s["parked"])

    def controls(self, s) -> list[tuple[str, str, bool, bool]]:
        """A session's conversation buttons (CONVERSATION), as show_button takes them: (id,
        caption, disabled, shown). s: the session's row, or None with no session: then Mode
        and Send show, disabled, and the rest don't. Mode applies to any session, its queue
        kept for it while it's dead; Send only to one that isn't dead, which can't receive
        (D30); Interrupt, Compact and Shell only to one running in a wheelhouse host (#62)."""
        if s is None:
            return [(id_, label, True, id_ in ("mode", "send")) for label, id_, _ in CONVERSATION]
        if self.sends_now(s["id"]):   # its old monitor would deliver a draft at once anyway
            mode_ = ("Can't queue: relaunch", True)
        else:
            mode_ = (f"Mode: {mode(s).capitalize()}", False)
        n = s["drafts"]
        hosted = runner(s) == "sdk" and self.running(s["id"]) and not s["shell"]
        dead = self.statuses.get(s["id"]) == "dead"
        return [("mode", *mode_, True), ("send", f"Send ({n})", not n, not dead),
                *[(b, b.capitalize(), False, hosted) for b in ("interrupt", "compact", "shell")]]

    def bar_widgets(self, screen: Screen) -> tuple:
        """A screen's send bar, answer hints, permission buttons, Compact question buttons and
        answer boxes: held once its bar is there, as they're composed with it and live as long
        as the screen."""
        held = self.held_bars.get(screen)
        if held is None:
            held = (next(iter(screen.query(SendBar)), None), list(screen.query(Hint)), list(screen.query(PermissionButtons)),
                    list(screen.query(CompactButtons)), list(screen.query(Compose)))
            if held[0] is not None:
                self.held_bars[screen] = held
        return held

    def paint_sendbar(self) -> None:
        """The bar on the screen in front: Send all, and the hint, permission buttons (with no
        answer box: a permission is answered by its buttons alone), Compact question buttons
        and activity of the session in context there; a full-screen item's bar also has its
        session's conversation buttons."""
        bar, hints, permission_rows, compact_rows, boxes = self.bar_widgets(self.screen)
        if bar is None:
            return
        asking = self.asking(self.bar_item(self.screen)) is not None   # first: it settles
        nudged = self.nudged(self.bar_item(self.screen))
        sessions = {s["id"]: s for s in self.sessions}
        s = sessions.get(self.bar_session(self.screen))
        if bar.conversation:
            for args in self.controls(s):
                show_button(bar, *args)
        total = sum(x["drafts"] for x in self.sessions if not self.dead(x["id"]))   # Send all skips the dead
        sends = "send" if s is not None and (mode(s) == "immediate" or self.sends_now(s["id"])) else "queue"
        for h in hints:
            h.set_base(PERMISSION_HINT if asking else COMPACT_HINT if nudged
                       else hint(sends, s is not None and self.dead(s["id"])))
        thread = isinstance(self.screen, ThreadView)
        for rows, shown in ((permission_rows, asking), (compact_rows, nudged), (boxes, not asking)):
            for row in rows:
                if row.display != shown:
                    if not shown and row.has_focus:   # the box goes: its keys with it
                        self.screen.set_focus(None if thread else self.items_table)
                    row.display = shown
                    if shown and thread and isinstance(row, Compose):
                        self.screen.set_focus(row)   # the thread's box back: its place to type
                    self.refit()   # a row more or less over the answer box
        show_button(bar, "send-all", f"Send all ({total})", not total)
        if s is not None and runner(s) == "sdk":
            bar.activity("in a shell tab" if s["shell"] else (s["activity"] or "") if self.running(s["id"]) else "")
        else:
            bar.activity("")

    def paint_checklist(self) -> None:
        """While the tutorial's checklist shows (its session exists, or has ended with its last
        step to do): its steps, at the top of the right pane."""
        found = self.client.tutorial()
        if found is None:
            if self.checklist.display:
                self.checklist.display = False
                self.refit()
            return
        sid, steps = found
        nxt = next((key for key, *_, done in steps if not done), None)
        text = Text.assemble(("TUTORIAL", "bold #ffd300"), ("  ? lists every key", "#777777"))
        session = next((s for s in self.sessions if s["id"] == sid), None)   # None once ended
        why = tutorial.stopped(session) if session and not self.running(sid) else None
        if why:
            text.append(f"\n{why}", style="bold #ff2a6d")
            text.append(f"\n{tutorial.AGAIN} at the next start", style="#e8e8e8")
        for key, what, how, done in steps:
            if done:
                text.append(f"\n✔ {what}", style="dim")
            elif key == nxt:
                text.append(f"\n▶ {what}", style="bold #05d9e8")
                text.append(f": {how}", style="#e8e8e8")
            else:
                text.append(f"\n☐ {what}", style="#777777")
        if getattr(self, "_checklist_text", None) != text.plain or not self.checklist.display:
            self._checklist_text = text.plain
            self.checklist.update(text)
            self.checklist.display = True
            self.refit()   # its height may have changed: the answer box below gives or takes

    def saw(self, sid: str, ref: str | None, step: str | None = None) -> None:
        """The person opened something of the tutorial's: a question, or watched a turn arrive
        in its conversation (step "follow", from paint_detail). Called only from what the
        person does, never from automatic selection: Enter at once, their own selection once
        they have looked at it (dwelt), their own pick of the conversation."""
        found = self.client.tutorial()
        if found is None or found[0] != sid:
            return
        if step is None and ref is not None:
            item = self.client.item(sid, ref)
            step = {"question": "open"}.get(item and item["kind"])
        done = {key for key, *_, ok in found[1] if ok}
        if step == "follow" and "reply" not in done:   # a turn watched before the first reply is the tutorial's own run
            return
        if step and step not in done:   # once is enough
            self.client.see(sid, step)
            self.paint_checklist()

    def paint_session_info(self) -> None:
        """The current session's description, under the list, and its buttons' captions and
        states: Park's, and the conversation buttons' (D20)."""
        sid = self.current_session()
        s = next((x for x in self.sessions if x["id"] == sid), None)
        text = self.describe(s) if s else self.describe_all()
        if text.plain != getattr(self, "_info_text", None):
            self._info_text = text.plain
            self.session_info.update(text)
        # only a session run in the wheelhouse has an activity: a tab's would be two blank rows
        self.session_activity.display = bool(s) and runner(s) == "sdk"
        activity = self.activity_line(s) if s else ""
        if activity != getattr(self, "_activity_text", None):
            self._activity_text = activity
            self.session_activity.update(activity)
        before = [b.display for b in self.session_controls]
        for grid, buttons in ((self.session_buttons, self.lifecycle_buttons(s)),
                              (self.conversation_buttons, self.controls(s))):
            for args in buttons:
                show_button(grid, *args)
            grid.display = any(shown for *_, shown in buttons)   # none: no blank row for it either
        if [b.display for b in self.session_controls] != before:
            self.refit()   # rows of buttons more or less under the description

    def describe_all(self) -> Text:
        """With no session selected: what the inbox shows, every session's items, in counts."""
        states = [self.state(s) for s in self.sessions]
        t = Text("All sessions", style="bold #ffd300")
        n = len(states)
        counts = [f"{n} session{'' if n == 1 else 's'}"] + [f"{k} {what}" for what in ("active", "parked")
                                                             if (k := states.count(what))]
        t.append("\n" + " · ".join(counts), style="#777777")
        t.append("\n\nThe inbox shows every session's items. Click a session to filter to it "
                 "and act on it; click it again, or Esc, for all sessions.")
        return t

    def describe(self, s) -> Text:
        """A session's description: what the old Sessions tab's row and synopsis showed."""
        st = self.shown_status(s["id"])
        t = Text(self.display_name(s), style="bold #ffd300")
        t.append("\n")
        t.append(st, style=STATUS_STYLE[st])
        if s["parked"]:
            t.append(" · parked", style="#777777")
        if self.stale(s["id"]):
            t.append(" · needs relaunch", style="bold #ff2a6d")
        where = ("shell tab" if s["shell"] else "in the wheelhouse") if runner(s) == "sdk" else "tab"
        t.append(f" · {where} · {mode(s)}", style="#777777")
        if s["ticket"]:
            t.append(f"\n{s['ticket']}", style="#05d9e8")
        t.append(f"\n{s['cwd']}", style="#777777")
        counts = [f"{n} {what}" for n, what in ((s["open_questions"], "open Q"), (s["running"], "running"),
                                                 (s["unseen_decisions"], "unseen D"), (s["drafts"], "queued")) if n]
        if counts:
            t.append("\n" + " · ".join(counts))
        t.append("\n\n")
        if s["synopsis"]:
            t.append(s["synopsis"])
        elif s["brief"]:
            t.append("Brief: ", style="#777777")
            t.append(s["brief"])
        else:
            t.append("No synopsis yet.", style="#777777")
        return t

    def activity_line(self, s) -> str:
        """What a session run in the wheelhouse is doing now, for the line under its description."""
        return s["activity"] if runner(s) == "sdk" and s["activity"] and self.running(s["id"]) else ""

    # selection

    @on(DataTable.RowSelected, "#session-list")
    def pick_session(self, event: DataTable.RowSelected) -> None:
        """Follow the session; a dead one is offered a relaunch, unless it's parked: a parked
        session is dead as often as not, and selecting it is how to reach its buttons."""
        self.settle()
        sid = event.row_key.value
        if (self.filter_sid, self.selected) != (sid, (sid, None)):   # a click's highlight may have followed it
            self.follow(sid)
        else:   # followed already: as following it, the pane goes to the newest turn
            self.call_after_refresh(self.detail_scroll.scroll_end, animate=False)
        if not any(s["id"] == sid and s["parked"] for s in self.sessions):
            self.offer_relaunch(sid)

    @session_action
    def offer_relaunch(self, sid: str) -> None:
        """Selecting a session that has died (shown red) offers to bring it back."""
        if self.statuses.get(sid) != "dead" or isinstance(self.screen, Confirm):   # a double-click's second
            return   # click selects it again before its first offer shows; done slowly, it's the dialog's
        s = self.row(sid)
        name = s["name"] or os.path.basename(s["cwd"]) or short(sid)
        self.push_screen(Confirm(f"{name} isn't running. Relaunch it?"),
                         lambda yes: yes and self.relaunch(sid))

    @session_action
    def relaunch(self, sid: str) -> None:
        if self.told(self.client.relaunch(sid)).ok:
            self.refresh_data()

    @on(Button.Pressed, "#relaunch")
    @session_action
    def relaunch_pressed(self) -> None:
        """Relaunch (Unpark on a parked session), in one press: one that isn't running comes
        back where it left off, unparked; a running one is stopped, a host or a tab's Claude
        Code, then started again on the conversation, asked first only if it's mid-turn
        (that turn is lost), or offered Compact first if its context is large."""
        self.settle()
        sid = self.current_session()
        if not sid:
            return
        s = self.row(sid)
        host = (s["claude_pid"], s["claude_start"])
        if sid in self.relaunching:
            self.notify("already relaunching: " + ("compacting first" if sid in self.client.compacting
                                                   else "waiting for it to stop"), severity="warning")
        elif self.statuses.get(sid) == "dead":
            self.relaunch(sid)
        elif self.statuses.get(sid) == "starting":
            self.notify("it's still starting: Relaunch once it's running", severity="warning")
        elif self.client.compact_first(sid):
            self.push_screen(RelaunchChoice(
                f"Relaunch {self.label(sid)}? Its context is {s['context_tokens'] // 1000}k tokens, which a "
                "restart writes to the prompt cache again whole. Compact first: it's asked what to keep, "
                "compacted with that, then relaunched. Or relaunch now: its host stops, interrupting any "
                "turn under way, and starts again on the same conversation."),
                lambda how: how and self.stop_host(sid, host, compact=how == "compact"))
        elif self.busy(sid):
            self.push_screen(Confirm(f"Relaunch {self.label(sid)}? It's mid-turn: it stops, losing that turn, "
                                     "and starts again on the same conversation."),
                             lambda yes: yes and self.stop_host(sid, host))
        else:
            self.stop_host(sid, host)

    @session_action
    def stop_host(self, sid: str, host: tuple, compact: bool = False) -> None:
        """Stop the session's host, if it is still the one the confirm was about: the hub
        starts it again once it has gone (hub.Hub.stop_host). Compacting first, the hub
        stops it once the session is compacted (hub.Hub.compact_then_relaunch)."""
        stop = self.client.compact_then_relaunch if compact else self.client.stop_host
        if self.told(stop(sid, host)).ok:
            self.refresh_data()

    async def action_quit(self) -> None:
        """Quit, but not silently out from under a relaunch: a host stopped and not yet
        started again would stay dead. Any whose host has gone is started now; for one still
        stopping, the person chooses. The daemon carries a Relaunch on by itself, so through
        it there's nothing to ask."""
        self.refresh_data()
        if not self.relaunching or self.client.remote:
            self.exit()
            return
        stopping = ", ".join(self.display_name(self.row(sid)) for sid in self.relaunching
                             if sid not in self.client.compacting)
        compacting = ", ".join(self.display_name(self.row(sid)) for sid in self.relaunching
                               if sid in self.client.compacting)
        said = []
        if stopping:
            said.append(f"Still relaunching {stopping}: its host hasn't stopped yet. Quit anyway, and leave it "
                        "stopped? (Relaunch brings it back.)")
        if compacting:
            said.append(f"{'Also c' if stopping else 'C'}ompacting {compacting} first, to relaunch it once "
                        f"compacted. {'' if stopping else 'Quit anyway? '}It keeps running; it is relaunched "
                        f"only if the wheelhouse is open within {COMPACT_RELAUNCH_WAIT // 60} minutes of Relaunch.")
        self.push_screen(Confirm(" ".join(said)), lambda yes: yes and self.exit())

    def follow(self, sid: str) -> None:
        """Filter the items to the session and highlight its conversation row, so the right
        pane follows the conversation rather than whichever question comes first. The
        session list's highlight shows again if none was selected (D20)."""
        self.filter_sid = self.followed = sid
        self.selected = (sid, None)
        self.picked = self.selected   # the person's own: a turn arriving in it ticks the tutorial's step (paint_detail)
        table = self.session_list
        if not table.show_cursor:
            with table.prevent(DataTable.RowHighlighted):   # the app's move, as highlight_session's
                if sid in table.rows:
                    table.move_cursor(row=table.get_row_index(sid), animate=False)
                table.show_cursor = True
            self.sessions_cursor = self.current_session()
            self.paint_session_info()
        self.retarget()
        self.paint_items()
        self.each_pane("tick")   # the stats pane: this session's alone now (stats_sid)
        self.call_after_refresh(self.detail_scroll.scroll_end, animate=False)

    @on(DataTable.RowHighlighted, "#items")
    @on(DataTable.RowHighlighted, "#session-list")
    def highlighted(self) -> None:
        """The person moving a list's highlight, handled late: whatever settle hasn't
        already. A refresh never posts one (see fill), so text being typed is never swapped
        under them."""
        self.settle()

    def settle(self) -> None:
        """One settle step (Q35): the two lists' cursors are the source of truth. An arrow
        moves a cursor at once, but its highlight is handled later, after any key behind it
        in a burst, so every action, and the accessors they read (composing, context_session,
        bar_item), settle first. A cursor where the app didn't leave it is the person's move:
        in the session list that session is followed (as a click does, though only Enter or a
        click offers to relaunch a dead one); in the items that row is selected."""
        if not hasattr(self, "items_table"):   # before the widgets exist
            return
        sid = self.current_session()
        if sid is not None and sid != self.sessions_cursor:
            self.sessions_cursor = sid   # first: following repaints, and may settle again
            if sid != self.filter_sid:
                # an items move pending is in the list following replaces: it's superseded, never
                # selected, here or by a settle within follow
                self.items_cursor = self.items_table.cursor_key()
                self.follow(sid)
            self.paint_session_info()
        key = self.items_table.cursor_key()
        if key is not None and key != self.items_cursor:
            self.items_cursor = key
            self.select_row(key)
            self.picked = self.selected   # the person's own pick: looked at for DWELL, a tutorial step it makes ticks (dwelt)
            # what the selection held re-sorts now, not at the next tick (ranked): a settled
            # item sinks as the key that moved on lands, so the keys behind it act on the same rows
            self.paint_items()

    def select_row(self, key: str) -> None:
        sid, ref = key.split("|")
        if self.selected != (sid, ref or None):
            self.selected = (sid, ref or None)   # an unseen decision is seen once looked at (dwelt)
            self.retarget()
            self.paint_detail()
            self.each_pane("tick")
            if self.detail_follows:
                self.call_after_refresh(self.detail_scroll.scroll_end, animate=False)

    @on(DataTable.RowSelected, "#items")
    def open_thread(self, event: DataTable.RowSelected) -> None:
        self.settle()
        sid, ref = event.row_key.value.split("|")
        self.saw(sid, ref or None)
        if ref:
            self.push_screen(ThreadView(sid, ref))
        else:   # the conversation is already in the pane: Enter goes to its box, at once
            self.screen.set_focus(self.answer)

    def highlight_session(self, sid: str | None) -> None:
        """Keep the session list's highlight on the session in context: one current session
        (D20). The app's move, not the person's: it filters nothing. With none selected
        (unfiltered) there is no highlight to move, whatever item is selected."""
        table = self.session_list
        if table.show_cursor and sid in table.rows and sid != self.current_session():
            with table.prevent(DataTable.RowHighlighted):
                table.move_cursor(row=table.get_row_index(sid), animate=False)
            self.sessions_cursor = sid
            self.paint_session_info()

    @on(ItemList.MarksChanged)
    def marks_changed(self) -> None:
        self.paint_items()

    def action_clear_filter(self) -> None:
        if self.screen.selections:   # a text selection first, as the screen's own Esc does
            self.screen.clear_selection()
            return
        self.settle()
        if self.items_table.marked:   # Esc drops a multi-selection first
            self.items_table.set_marks(set())
            return
        self.unfilter()

    def unfilter(self) -> None:
        """All sessions: Esc, or a click on the selected session or the session list's title (SessionList)."""
        self.clear_filter()
        self.paint_items()
        self.retarget()
        self.each_pane("tick")   # the stats pane: every session's totals now (stats_sid)

    def clear_filter(self) -> None:
        """No session selected: every session's items, and no highlight in the session list,
        so no current session for its buttons and keys (D20) until one is picked again."""
        self.filter_sid = None
        if self.viewing:   # its row goes with the filter; the highlight lands on an item
            self.selected = None
        self.session_list.show_cursor = False
        self.sessions_cursor = None
        self.paint_session_info()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        """F's two bindings: only the one that applies shows in the footer, and runs."""
        if action == "show_finished":
            return parameters[0] != self.show_finished
        return True

    def action_show_finished(self, show: bool) -> None:
        if not isinstance(self.focused, (TextArea, Input)):
            self.show_finished = show
            self.notify("showing finished items" if show else "hiding finished items")
            self.refresh_bindings()
            self.paint_items()

    @session_action
    def action_close_question(self) -> None:
        """Delete or Backspace in the item list: the person's call. It closes the highlighted
        question or decision, or dismisses a settled task or subagent (store.standing). It only
        ever closes: a finished one held in sight as its session finished it (F off) goes now,
        as if closed here, and one shown with F stays, with a word that Ctrl+R reopens it
        (action_recall). It moves at once, not sinking (D33)."""
        self.settle()
        if isinstance(self.focused, (TextArea, Input)):
            return
        if self.items_table.marked and self.screen is self.screen_stack[0]:
            self.close_marked()
            return
        target = self.composing()[1]
        item = target and target[1] and self.client.item(*target)
        if not item:
            return
        if not closable(item):
            what = "a running subagent" if item["kind"] == "agent" else f"a {item['kind']}"
            self.notify(f"{item['ref']} is {what}: its status is the session's to set", severity="warning")
            return
        verb = "closed" if item["kind"] in CLOSABLE else "dismissed"
        if standing(item) == "finished" and self.show_finished:
            self.notify(f"{item['ref']} is {verb} already: Ctrl+R reopens it")
            return
        self.close(item, True)
        self.notify(f"{verb} {item['ref']}" + ("" if self.show_finished else ": F shows finished items"))
        self.refresh_data()

    def close(self, item, closed: bool) -> None:
        """Close a question or decision (Delete), or reopen it (Ctrl+R): a question as answered,
        a decision as seen. Dismiss a settled task or subagent, or bring it back. At once: no
        sinking, and selected, it moves (D33), the one change that moves a selected item
        (ranked). One finished already only goes, from where it was held while selected."""
        self.item_sink.jump()
        self.repin.add(item_key(item))
        self.told(self.client.close(item["session_id"], item["ref"], closed))

    def answer_permission(self, sid: str, ref: str, decision: str, message: str = "") -> None:
        """The person's Allow, Always or Deny: an explicit act, as Delete is, so selected, the
        item moves at once (ranked)."""
        self.told(self.client.answer_permission(sid, ref, decision, message))
        self.repin.add(f"{sid}|{ref}")

    def close_marked(self, closed: bool = True) -> None:
        """Delete on a multi-selection closes its questions and decisions and dismisses its
        settled tasks and subagents (and lets go of a finished one held in sight, as Delete
        does); Ctrl+R brings back its finished ones. Anything else in it is left alone, and
        with nothing to act on the marks stay."""
        def acts(it):
            finished = standing(it) == "finished"
            return (not finished or not self.show_finished) if closed else finished
        picked = [it for key in self.items_table.final() if key in self.items_table.marked   # as they'll stand
                  if (it := self.client.item(*key.split("|"))) and closable(it)]
        if not picked or not any(acts(it) for it in picked):
            self.notify("the marked rows are finished already: Ctrl+R reopens them" if picked and closed else
                        f"nothing among the marked rows to {'close' if closed else 'reopen'}", severity="warning")
            return
        done = []
        for it in filter(acts, picked):
            try:
                self.close(it, closed)
            except SessionGone:   # ended meanwhile: the rest still go
                continue
            done.append(it)
        self.items_table.set_marks(set())
        said = {}   # by verb: a task or subagent is dismissed, not closed
        for it in done:
            verb = ("closed" if it["kind"] in CLOSABLE else "dismissed") if closed else "reopened"
            said.setdefault(verb, []).append(it["ref"])
        self.notify("; ".join(f"{verb} {', '.join(refs)}" for verb, refs in said.items())
                    + ("" if not closed or self.show_finished else ": F shows finished items"))
        self.refresh_data()

    def action_help(self) -> None:
        if not isinstance(self.focused, (TextArea, Input)):
            self.push_screen(KeysHelp([b for b in SDK_BUTTONS
                                       if any(showing(x) for x in self.screen.query(f"#{b}"))]))

    def offer_answered(self, take: bool) -> None:
        """The first-run offer, answered either way: it never comes back."""
        if self.told(self.client.answer_offer(take)).ok:
            self.refresh_data()

    def composing(self):
        """The compose box in use and the item it answers: the thread view's, or the inbox's."""
        self.settle()
        if isinstance(self.screen, ThreadView):
            return self.screen.box, (self.screen.sid, self.screen.ref)
        if self.screen is self.screen_stack[0]:
            # following a conversation, the box sends the session a general message
            return self.answer, self.selected
        return None, None   # a dialog is open: its keys are its own

    def typed(self):
        box, target = self.composing()
        if box is None:
            return None, None, None
        text = emoji.convert(box.text.strip())
        if not text or not target:
            self.notify("pick an item and type something first", severity="warning")
            return None, None, None
        return box, target, text

    @session_action
    def action_submit(self) -> None:
        """Ctrl+Enter: queued or sent at once, by the session's mode."""
        self.settle()
        box, target = self.composing()
        if box is not None and target and self.nudged(target):
            self.compact_now(target)   # the box holds its instructions, if anything
            return
        box, target, text = self.typed()
        if not box:
            return
        done = self.told(self.client.submit(target, text))   # an open permission is denied, never queued (#53)
        if not done.ok:   # a permission answered meanwhile
            return
        if done.denied:   # an explicit act, as Delete is: selected, the item moves at once (ranked)
            self.repin.add(f"{target[0]}|{target[1]}")
        box.text = ""
        self.refresh_data()
        if box is self.answer:   # back to the items, cursor where it was: Down, Tab answers the next
            self.screen.set_focus(self.items_table)
        elif isinstance(self.screen, ThreadView) and box is self.screen.box:   # to the end, where the answer just given shows
            self.screen.query_one("#thread-scroll").scroll_end(animate=False)

    @session_action
    def action_recall(self) -> None:
        """Ctrl+R: take this item's latest queued answer back into the box, to edit it or drop
        it. With nothing queued for it and the item list focused, reopen the finished items
        marked, or else the highlighted one if it's finished (close): what Delete closed comes
        back. In a text box it only ever takes back, so typing there never reopens anything."""
        self.settle()
        box, target = self.composing()
        if box is None or not target:
            return
        drafts = [m for m in self.client.drafts(target[0]) if m["item_ref"] == target[1]]
        if not drafts and self.focused is self.items_table:
            if self.items_table.marked:
                self.close_marked(False)
                return
            item = target[1] and self.client.item(*target)
            if item and closable(item) and standing(item) == "finished":
                self.close(item, False)
                self.notify(f"reopened {item['ref']} as {CLOSABLE.get(item['kind'], item['status'])}")
                self.refresh_data()
                return
        if box.text.strip():
            self.notify("the box isn't empty: queue or clear it first", severity="warning")
            return
        body = drafts and self.client.unqueue(drafts[-1]["id"])
        if not body:
            self.notify(f"nothing queued for {aimed(target)}")
            return
        box.text = body
        box.move_cursor(box.document.end)
        self.notify("taken back: submit it again, or clear it to drop it")
        self.refresh_data()

    def bar_session(self, screen) -> str | None:
        """The session in context on a screen, which its keys, buttons, hint and activity line
        all act on or describe: the thread's, else the followed session, highlighted in the
        session list (D20), or with none selected the selected item's (focus_sid)."""
        if isinstance(screen, ThreadView):
            return screen.sid
        return self.focus_sid()

    def bar_item(self, screen) -> tuple[str, str] | None:
        """The item in context on a screen: the thread's, else the inbox's selected item."""
        self.settle()
        if isinstance(screen, ThreadView):
            return (screen.sid, screen.ref)
        if self.selected and self.selected[1]:
            return self.selected
        return None

    def asking(self, target):
        return self.client.asking(target)

    def context_session(self) -> str | None:
        """The session a key acts on, or None (said so) with no session in context. A dialog
        in front has its own keys."""
        self.settle()
        if self.composing()[0] is None:
            return None
        sid = self.bar_session(self.screen)
        if sid is None:
            self.notify("select a session first", severity="warning")
        return sid

    def action_send_session(self) -> None:
        self.settle()
        if sid := self.context_session():
            self.send_session(sid)

    @session_action
    def send_session(self, sid: str) -> None:
        self.told(self.client.send_queue(sid))   # a dead one's, as its hidden Send button: its drafts stay drafts (#62)
        self.refresh_data()

    @session_action
    def send_all(self) -> None:
        self.told(self.client.send_all())
        self.refresh_data()

    def action_toggle_mode(self) -> None:
        self.settle()
        if sid := self.context_session():
            self.toggle_mode(sid)

    @session_action
    def toggle_mode(self, sid: str) -> None:
        self.told(self.client.toggle_mode(sid))
        self.refresh_data()

    @on(Button.Pressed, "#mode")
    def mode_pressed(self, event: Button.Pressed) -> None:
        self.settle()
        if sid := self.bar_session(event.button.screen):
            self.toggle_mode(sid)

    @on(Button.Pressed, "#send")
    def send_pressed(self, event: Button.Pressed) -> None:
        self.settle()
        if sid := self.bar_session(event.button.screen):
            self.send_session(sid)

    @on(Button.Pressed, "#send-all")
    def send_all_pressed(self) -> None:
        self.send_all()

    @on(Button.Pressed, "#allow, #always, #deny")
    def permission_pressed(self, event: Button.Pressed) -> None:
        """Answer a permission item, at once, by its buttons alone: no answer box shows with it."""
        self.settle()
        target = self.bar_item(event.button.screen)
        if not target:
            return
        try:
            self.answer_permission(*target, event.button.id)
        except (KeyError, SessionGone) as e:   # answered meanwhile, or the session went
            self.notify(str(e.args[0] if e.args else e), severity="warning")
        self.refresh_data()

    @on(Button.Pressed, "#interrupt, #compact, #shell")
    def host_pressed(self, event: Button.Pressed) -> None:
        self.settle()
        sid = self.bar_session(event.button.screen)
        if not sid:
            return
        what = event.button.id
        if what == "interrupt":
            self.host_command(sid, what)
        elif what == "compact":
            self.push_screen(CompactSession(self.label(sid)), lambda plan: plan and self.host_command(sid, what, **plan))
        else:
            self.push_screen(Confirm(f"Open {self.label(sid)} in a terminal tab? The wheelhouse hands it over, "
                                     "and takes it back when you /exit the tab."),
                             lambda yes: yes and self.host_command(sid, what))

    def host_command(self, sid: str, what: str, notes: str | None = None, then: str | None = None) -> None:
        self.told(self.client.host_command(sid, what, notes, then))

    @on(Button.Pressed, "#compact-now, #leave-it")
    def compact_question_pressed(self, event: Button.Pressed) -> None:
        """The question suggesting Compact: Compact, with what's typed in the box below as its
        instructions (compact_now), or Leave it, which closes it."""
        self.settle()
        target = self.bar_item(event.button.screen)
        if not target or not self.nudged(target):
            return
        if event.button.id == "leave-it":
            self.told(self.client.close(*target, True))
            self.refresh_data()
        else:
            self.compact_now(target)

    def nudged(self, target) -> bool:
        """Whether the item in context is the question suggesting Compact, of a session whose
        host is running: the conversation's own Compact button shows only then too."""
        if self.client.compact_question(target) is None:
            return False
        s = next((x for x in self.sessions if x["id"] == target[0]), None)
        return s is not None and runner(s) == "sdk" and self.running(s["id"]) and not s["shell"]

    def compact_now(self, target: tuple[str, str]) -> None:
        """Compact from its question: the box's text, if any, the instructions, and the
        question's choice what follows. The question closes itself once compacted (host.nudge)."""
        box, aimed_at = self.composing()
        notes = emoji.convert(box.text.strip()) if box is not None and aimed_at == target else ""
        rows = self.bar_widgets(self.screen)[3]
        then = rows[0].then if rows else DEFAULT_THEN
        if self.told(self.client.host_command(target[0], "compact", notes or None, then)).ok and notes:
            box.text = ""
        self.refresh_data()

    @on(Button.Pressed, "#model")
    @session_action
    def model_pressed(self) -> None:
        """Model, on the current session: a list of the models, warning that the cache goes."""
        self.settle()
        sid = self.current_session()
        if sid:
            s = self.row(sid)
            current = models.field(s, "model") or models.field(s, "applied_model")
            self.push_screen(ChooseModel(display_name(s), current), lambda model: self.change_model(sid, model))

    @session_action
    def change_model(self, sid: str, model: str | None) -> None:
        if model and self.told(self.client.set_model(sid, model)).ok:   # not if cancelled, or unchanged
            self.refresh_data()

    @on(Splitter.Resized)
    def keep_layout(self, event: Splitter.Resized) -> None:
        """A splitter dragged: its size is kept for next time. Reset: the default again."""
        self.client.set_setting(LAYOUT + event.splitter.key, None if event.fraction is None else f"{event.fraction:.6f}")
        self.fit_layout()   # the default put back may not fit

    # the session area: the list's highlighted session, and its buttons

    def current_session(self) -> str | None:
        """The current session: the one highlighted in the session list (D20). None with
        none selected, the highlight hidden (clear_filter)."""
        table = self.session_list
        if not table.show_cursor or not table.row_count or table.blank(table.cursor_row):   # blank: as a rebuild lands
            return None
        return table.coordinate_to_cell_key((table.cursor_row, 0)).row_key.value

    def action_new_session(self) -> None:
        if isinstance(self.focused, (TextArea, Input)):
            return
        self.push_screen(NewSession(), self.launch_new)

    def launch_new(self, form) -> None:
        if form:
            self.told(self.client.new_session(**form))

    def action_adopt(self) -> None:
        if isinstance(self.focused, (TextArea, Input)):
            return
        self.push_screen(AdoptSession(self.client.adoptable()), self.launch_adopted)

    def launch_adopted(self, form) -> None:
        if form and self.told(self.client.adopt(form["candidate"], form["name"])).ok:
            self.refresh_data()

    async def action_press(self, button_id: str) -> None:
        """A key standing in for a button that never takes focus: as a click on it, on the
        screen in front, the inbox or a full-screen item. Not in a text box, whose keys are
        typing, nor under a dialog, whose keys are its own. One that doesn't apply to what's
        in context says so rather than doing nothing."""
        if isinstance(self.focused, (TextArea, Input)) or self.composing()[0] is None:
            return
        inbox = self.screen is self.screen_stack[0]
        if inbox and button_id in dict(LIFECYCLE).values() and self.current_session() is None:
            return   # none selected: its buttons are disabled, and their keys do nothing (D20)
        sid = self.current_session() if button_id == "park" else None
        if sid and self.state(self.row(sid)) == "parked":   # P presses Unpark, the Relaunch button there
            button_id = "relaunch"
        if button_id in NUDGE_KEYS and any(showing(r) for r in self.screen.query(CompactButtons)):
            button_id = NUDGE_KEYS[button_id]   # 1 to 3 on the question suggesting Compact: its own
        button = next(iter(self.screen.query(f"#{button_id}")), None)
        if button is None:   # not on this screen (Model, in a full-screen item's bar): said all the same
            name = next((b.description for b in BUTTON_KEYS if b.action == f"press('{button_id}')"),
                        button_id.capitalize())
            where = ": it's with the session list's buttons" if button_id in dict(LIFECYCLE).values() else ""
            self.notify(f"{name} doesn't apply here{where}", severity="warning")
            return
        if not showing(button):
            whose = "the item in context" if button_id in ("allow", "always", "deny") else "this session"
            self.notify(f"{button.label} doesn't apply to {whose}", severity="warning")
        elif button.disabled:
            self.notify(f"{button.label}: nothing to do", severity="warning")
        elif isinstance(button, RadioButton):
            button.value = True   # a then-choice: pressed in its set
        else:
            # handled now, as the key arrived (take_key): press() posts it to the button, from
            # where it reaches the app behind the keys typed after it, an arrow moving what it acts on
            button._start_active_affect()
            await self._dispatch_message(Button.Pressed(button))

    async def land(self) -> None:
        """What the keys before this one set in motion, landed before it acts (take_key), as if
        they had been typed slowly: a screen one opened has mounted and taken its focus; what
        was sent to the focus has been handled, and so has each message that bubbles from there
        to its screen, as a button's Pressed, an input's Submitted or a row's Selected; and a
        dialog that closed has given its answer to whatever opened it, which Textual runs once
        the app's current message (this key) is done. A message that reaches the app itself
        waits behind the burst, so what it would do is done here or at once instead: a key that
        presses a button or selects a row acts as it arrives (action_press, select_now), a
        click's selection runs here, as the app's next callback (SessionList), and last, the
        lists' cursors the keys moved are settled (settle), their highlights being such
        messages. It waits only when something is waiting, and the refresh tick waits for it
        (tick), so a burst ends the same wherever a tick falls. Bounded: a widget kept busy, or
        a screen slow to mount, never holds the app up for long, though long enough for the
        inbox to restyle as the screen over it closes (a fifth of a second, under load)."""
        deadline = time.monotonic() + 2
        self.landing = True
        try:
            while time.monotonic() < deadline:
                if self._next_callbacks:
                    await self._flush_next_callbacks()
                    continue
                if not self.screen_stack:   # shutting down: nothing to act on
                    return
                if not self.screen.is_mounted:   # it takes its focus as it mounts
                    await asyncio.wait_for(self.screen._mounted_event.wait(), deadline - time.monotonic())
                    continue
                chain = [w for w in (self.focused or self.screen).ancestors_with_self if w is not self]
                # the focus first, as messages bubble. A message its pump has taken from the queue
                # to look at while it handles the one before isn't in the queue's size
                waiting = next((w for w in chain if w.message_queue_size or w._pending_message is not None), None)
                if waiting is None:
                    break
                landed = asyncio.get_running_loop().create_future()
                # a callback queued behind them runs once they have
                if not waiting.call_later(lambda: landed.done() or landed.set_result(None)):
                    break   # closing: nothing more of it lands
                await asyncio.wait_for(landed, deadline - time.monotonic())
        except asyncio.TimeoutError:
            pass
        finally:
            self.landing = False
        self.settle()   # the cursors those keys moved, followed and selected as if typed slowly

    def action_focus_next(self) -> None:
        self.cycle_focus(1)

    def action_focus_previous(self) -> None:
        self.cycle_focus(-1)

    def cycle_focus(self, step: int) -> None:
        """Tab and Shift+Tab on the inbox go round TAB_ORDER, then any other pane that takes
        focus; elsewhere (a full-screen item, a dialog) in screen order. In an answer box
        with an emoji code being typed, Tab takes the first suggestion instead. Tab is a key,
        so the cursors have settled (land) before it moves on."""
        if step > 0 and isinstance(self.focused, Compose) and self.focused.take_suggestion():
            return
        screen = self.screen
        chain = screen.focus_chain
        if screen is self.screen_stack[0]:
            order = [w for id_ in TAB_ORDER for w in chain if w.id == id_]
            chain = order + [w for w in chain if w not in order]
        if not chain:
            return
        here = chain.index(self.focused) if self.focused in chain else (-1 if step > 0 else 0)
        nxt = chain[(here + step) % len(chain)]
        # at once, not after a refresh as focus() does: keys behind it go there. A conversation
        # or thread stays where it's scrolled to, not taken to its top as Textual would (D43)
        screen.set_focus(nxt, scroll_visible=not isinstance(nxt, Transcript))

    def row(self, sid: str):
        return self.client.row(sid)

    def label(self, sid: str) -> str:
        s = self.row(sid)
        return f"{s['name'] or os.path.basename(s['cwd'])} ({short(sid)}, {s['cwd']})"

    def action_relaunch_all(self) -> None:
        """Relaunch all: every Available session, once confirmed. Parked and Active ones are
        left alone."""
        if isinstance(self.focused, (TextArea, Input)):
            return
        available = self.client.relaunchable()
        if not available:
            self.notify("nothing to relaunch: no session is Available")
            return
        self.push_screen(Confirm(f"Relaunch {len(available)} Available session(s)?"),
                         lambda yes: yes and self.told(self.client.relaunch_all(available)))

    @on(Button.Pressed, "#park")
    @session_action
    def park_pressed(self) -> None:
        """Park: an Active session is asked (it writes its handoff notes, then stops); an
        Available one is parked at once. A Parked one's P is Unpark (action_press)."""
        self.settle()
        sid = self.current_session()
        if not sid:
            return
        s = self.row(sid)
        if self.state(s) == "parked":
            self.relaunch_pressed()
        elif s["parked"]:   # parked under the old rules and still running: only the flag goes
            self.told(self.client.unpark(sid))
            self.follow(sid)
            self.refresh_data()
        elif runner(s) == "sdk" and not s["shell"] and self.running(sid) and self.pending(sid) is None:
            # a host's session: it may be compacted once parked, offered while its cache is warm
            self.push_screen(ParkSession(self.label(sid), self.client.warm(sid)),
                             lambda form: form and self.ask(sid, "park", form["compact"]))
        else:
            self.lifecycle(sid, "park", f"Park {self.label(sid)}?")

    @on(Button.Pressed, "#rename")
    @session_action
    def rename_pressed(self) -> None:
        self.settle()
        sid = self.current_session()
        if sid:
            self.push_screen(RenameSession(self.row(sid)["name"]), lambda name: self.rename(sid, name))

    @session_action
    def rename(self, sid: str, name: str | None) -> None:
        if self.told(self.client.rename(sid, name)).ok:   # not if cancelled, or unchanged
            self.refresh_data()

    @on(Button.Pressed, "#fork")
    @session_action
    def fork_pressed(self) -> None:
        """Fork: the dialog (ForkSession), then the fork, which the list follows."""
        self.settle()
        sid = self.current_session()
        if not sid:
            return
        try:
            plan = self.client.fork_plan(sid)
        except (OSError, RuntimeError, ValueError) as e:   # the daemon unreachable or refusing
            self.notify(f"can't fork: {e}", severity="error")
            return
        self.push_screen(ForkSession(plan), lambda form: form and self.fork(sid, form))

    @session_action
    def fork(self, sid: str, form: dict) -> None:
        done = self.told(self.client.fork(sid, form["refs"], form["branch"]))
        if done.ok:
            self.refresh_data()

    @on(Button.Pressed, "#end")
    @session_action
    def end_pressed(self) -> None:
        """Forget. Stop & Forget on an Active session not yet asked offers ForgetChoice: let
        it know (it does its end steps) or just stop it, by its cache's warmth."""
        self.settle()
        sid = self.current_session()
        if not sid:
            return
        n = len(self.client.drafts(sid))
        if self.running(sid) and self.pending(sid) is None:
            warm = self.client.warm(sid)
            prompt = forget_prompt(self.label(sid), warm, self.client.cache_expires(sid),
                                   self.client.context_tokens(sid), n)
            self.push_screen(ForgetChoice(prompt, warm), lambda choice: choice and self.forget(sid, choice))
        else:
            lost = f" {n} queued answer(s) will be discarded." if n else ""
            self.lifecycle(sid, "end", f"Forget {self.label(sid)}? Its wheelhouse data is deleted.{lost}")

    @session_action
    def forget(self, sid: str, choice: str) -> None:
        """ForgetChoice's answer: the end request, or stopped and forgotten now (force). The
        dialog may have sat open while the session stopped or was asked: lifecycle again."""
        if not self.running(sid) or self.pending(sid) is not None:
            self.lifecycle(sid, "end", f"Forget {self.label(sid)}? Its wheelhouse data is deleted.")
        elif choice == "tell":
            self.ask(sid, "end")
        else:
            self.force(sid, "end")

    def lifecycle(self, sid: str, what: str, act: str) -> None:
        """Park or Forget, as hub.VERBS says for the session's state. A running session is
        asked (Forget's choice is ForgetChoice's, before this); one asked already is pressed
        again to cancel or force. One not running is acted on at once, confirmed first where
        VERBS says (act)."""
        if not self.running(sid):
            confirm = VERBS[(self.state(self.row(sid)), what)][1]
            if confirm:
                self.push_screen(Confirm(act), lambda yes: yes and self.act_on_dead(sid, what))
            else:
                self.act_on_dead(sid, what)
            return
        pending = self.pending(sid)
        verb = VERBS[("active", what)][0]
        if pending is None:
            self.ask(sid, what)
        elif pending != what:
            self.notify(f"the session has already been asked to {VERBS[('active', pending)][0].lower()}: "
                        "cancel that first", severity="warning")
        else:
            self.push_screen(Choice(f"{self.label(sid)} has been asked to {verb.lower()} and hasn't yet. "
                                    "Cancel the request, or force it?"),
                             lambda choice: self.resolve_request(sid, what, choice))

    @session_action
    def ask(self, sid: str, what: str, compact: bool = False) -> None:
        self.told(self.client.ask(sid, what, compact))
        self.keep_current(sid, what)
        self.refresh_data()

    @session_action
    def act_on_dead(self, sid: str, what: str) -> None:
        """The confirm may have sat open while the session came back: the hub checks again."""
        if self.told(self.client.act_on_dead(sid, what)).ok:
            self.keep_current(sid, what)
        self.refresh_data()

    @session_action
    def resolve_request(self, sid: str, what: str, choice: str) -> None:
        if choice == "cancel":
            self.told(self.client.cancel_request(sid, what))
            self.refresh_data()
        elif choice == "force":
            n = len(self.client.drafts(sid)) if what == "end" else 0
            warning = ("Stops it now and deletes its wheelhouse data, without its session-end steps." if what == "end"
                       else "Stops it now and parks it, without its handoff notes.")
            warning += f" {n} queued answer(s) will be discarded." if n else ""
            self.push_screen(Confirm(f"Force {VERBS[('active', what)][0]} {self.label(sid)}? {warning}"),
                             lambda yes: yes and self.force(sid, what))

    @session_action
    def force(self, sid: str, what: str) -> None:
        self.told(self.client.force(sid, what))
        self.keep_current(sid, what)
        self.refresh_data()

    def keep_current(self, sid: str, what: str) -> None:
        """Parking takes a session's items off the unfiltered inbox: following it keeps it
        current, its pinned conversation row selected, so Unpark is one press away."""
        if what == "park":
            self.follow(sid)
