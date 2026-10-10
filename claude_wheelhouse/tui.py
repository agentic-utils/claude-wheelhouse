"""The wheelhouse app: a Textual app. It only reads and writes the database."""

import asyncio
import functools
import math
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import time

from rich.markdown import Markdown as RichMarkdown
from rich.segment import Segment
from rich.style import Style
from rich.cells import cell_len
from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.screen import ModalScreen, Screen
from textual.scrollbar import ScrollBar
from textual.strip import Strip
from textual.widget import Widget
from textual.worker import get_current_worker
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    DirectoryTree,
    Footer,
    Input,
    Label,
    Static,
    TextArea,
)

from . import adopt, api, emoji, launch, liveness, stats, subagents, transcript, tutorial
from .knurl import KnurlRender
from .splitter import Splitter, fit
from .store import (DISMISSABLE, FINISHED_RANK, SETTLED, SETTLED_RANK, SessionGone, Store, can_queue,
                    default_runner, inbox_rank, mode, needs_relaunch, runner, standing)

MATRIX = "#00ff41"
SHIMMER = ["#ff2a6d", "#ff7b00", "#ffd300", "#05d9e8", "#7b61ff", "#d300c5"]
# a hosted session's activity when nothing is under way: errored and stopped count as resting
RESTING = ("idle", "interrupted", "stopped", "in a shell tab", "error")
MARKED = Style(bgcolor="#3a1060")   # rows picked to close together
DECISION = "bold #b967ff"   # an unseen decision: noticeable, not urgent
STATUS_STYLE = {"live": "bold #00ff41", "stalled": "bold #ffd300", "starting": "#05d9e8",
                "dead": "bold #ff2a6d", "ending": "bold #d300c5", "parking": "bold #d300c5",
                "relaunching": "bold #05d9e8"}
TITLE = " ▓▒░ CLAUDE·WHEELHOUSE ░▒▓ "
RUNNING = ("live", "stalled", "starting")
# seconds the person must stay on an item, without a break, before it counts as looked at:
# an unseen decision becomes seen, a tutorial step ticks (dwelt). Passing over it does neither
DWELL = 1.0
RELAUNCH_WAIT = 30   # seconds a host has to stop for Relaunch before it gives up and says so
NOTHING_SELECTED = "Select an item, or a session to follow its conversation."
TAB_RELAUNCH = "a session in a tab relaunches once it has exited: /exit it there, then Relaunch"
# the person's words in terminal green, Claude's in white as in the Claude app
VOICE = {"you": MATRIX, "claude": "#e8e8e8", "head": "#05d9e8",
         "warn": "bold #ffd300", "note": "#777777", "tool": "#777777"}
PENDING = {"end": "ending", "park": "parking"}
CLOSABLE = {"question": "answered", "decision": "seen"}   # what Delete closes, and what reopening makes it


def closable(item) -> bool:
    """What Delete acts on: a question or decision whatever its status, a task or subagent
    once it has settled (store.SETTLED), dismissed or not."""
    return item["kind"] in CLOSABLE or (item["kind"] in DISMISSABLE and item["status"] in SETTLED[item["kind"]])
# where each module slot (api.SLOTS) is mounted: at the end of this container
SLOT_PARENTS = {"inbox.side": "#items-pane"}
LAYOUT = "layout."   # settings: each splitter's size, by its key, as a share of its parent


def cylon(frame: int, width: int = 8) -> Text:
    """A red eye sweeping back and forth with a fading tail."""
    pos = round((math.sin(frame / 4) + 1) / 2 * (width - 1))
    t = Text()
    for i in range(width):
        d = abs(i - pos)
        t.append("█" if d == 0 else "▓" if d == 1 else "░" if d == 2 else " ",
                 style="#ff0000" if d == 0 else "#aa0000" if d == 1 else "#550000")
    return t


def shimmer(frame: int) -> Text:
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
            if self.selected and self.store.session(self.selected[0]) is None:
                self.selected = None
            self.refresh_data()
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


def short(sid: str) -> str:
    return sid[:6]


def host_context(s) -> api.HostContext | None:
    """The context size an SDK session's host last recorded, for the stats to show after a
    compaction. A tab session's sizes come from its transcript alone."""
    at = stats.epoch(s["context_at"] or "")
    if runner(s) != "sdk" or at is None or not s["context_tokens"] or not s["context_max"]:
        return None
    return api.HostContext(s["context_tokens"], s["context_max"], at)


def aimed(target) -> str:
    """What a message goes to: an item's ref, or the session itself (a general message)."""
    return target[1] or "the session"


def thread_blocks(store: Store, sid: str, ref: str, session_name: str = "") -> list[tuple[str, str]]:
    """An item's detail and conversation, oldest first, as (who, markdown) blocks: the
    person's messages (queued ones marked), the session's replies, and its progress notes
    as quieter quotes."""
    item = store.item(sid, ref)
    if item is None:
        return [("note", "_gone_")]
    msgs = store.thread(sid, ref)
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
    return "\n\n".join(md for _, md in thread_blocks(store, sid, ref, session_name))


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


class Transcript(Widget, can_focus=True):
    """A conversation or a thread: one widget drawing cached lines, as Blocks does, and
    selectable. Drag selects, Ctrl+A selects it all, Ctrl+C copies (Textual's screen binding,
    which copies through the terminal: OSC 52, which Windows Terminal supports)."""
    ALLOW_SELECT = True
    DEFAULT_CSS = "Transcript { height: auto; }"
    BINDINGS = [Binding("ctrl+a", "select_all", "Select all", show=False)]

    def __init__(self, placeholder: str = "", **kwargs):
        super().__init__(**kwargs)
        self.blocks: list[tuple[str, str]] = [("note", placeholder)] if placeholder else []
        self._laid: tuple | None = None

    def update(self, content: "Blocks | list[tuple[str, str]]") -> None:
        self.blocks = content.blocks if isinstance(content, Blocks) else list(content)
        self._laid = None
        self.refresh(layout=True)

    def _layout(self, width: int) -> tuple[list[Strip], list[str]]:
        if self._laid is None or self._laid[0] != width:
            strips, texts = [], []
            for who, md in self.blocks:
                s, t = _block_strips(who, md, width, self.app.console)
                strips.extend(s)
                texts.extend(t)
            self._laid = (width, strips, texts)
        return self._laid[1], self._laid[2]

    def get_content_width(self, container, viewport) -> int:
        return container.width

    def get_content_height(self, container, viewport, width: int) -> int:
        return len(self._layout(width)[0])

    def render_line(self, y: int) -> Strip:
        width = self.size.width
        strips, texts = self._layout(width)
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

    def __init__(self, repaint):
        self.repaint = repaint   # the list's paint, which lays it again: each frame
        self.to: dict[str, int] | None = None   # each key's slot, the move's end: what the cursor keys act in (SinkList)
        self.sunk: set[str] = set()
        self.move: tuple[dict[str, float], float] | None = None   # each key's slot at the start, and when
        self.timer = None

    def lay(self, app, keys: list[str], sunk: set[str], room: int = 0) -> list[str | None]:
        top = sum(k not in sunk for k in keys)
        foot = max(top, room - (len(keys) - top))
        to = {k: i if i < top else foot + i - top for i, k in enumerate(keys)}
        moved = self.to is not None and any(k in self.to and (k in sunk) != (k in self.sunk) for k in keys)
        if moved or (self.move and to != self.to):   # from wherever each row shows now
            self.move = (self.at(time.monotonic()), time.monotonic())
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
        now = time.monotonic()
        if self.move and now - self.move[1] >= self.SECONDS:
            self.jump(keep=True)
        slots = self.at(now)
        rows: list[str | None] = []
        # never two in one slot. Whole slots passed, so each row moves one way only, never back
        for k in sorted(slots, key=lambda k: (slots[k], self.to[k])):
            rows += [None] * max(0, int(slots[k]) - len(rows)) + [k]
        return rows

    def jump(self, keep: bool = False) -> None:
        """No move under way, and none for the next lay unless keep: Delete's change lands at once (D33)."""
        self.move = None
        if self.timer is not None:
            self.timer.stop()
            self.timer = None
        if not keep:
            self.to = None


class SinkList(DataTable):
    """A list a Sink lays. Every key that moves its cursor (Up, Down, PageUp, PageDown,
    Ctrl+Home, Ctrl+End, the item list's Shift+Up and Shift+Down) acts in its final order
    (final), never a moving frame's, so keys typed during a move act as they would after it;
    the cursor steps off a blank row the way it was going. Home and End scroll sideways."""

    def __init__(self, **kwargs):
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

    def blank(self, row: int) -> bool:
        return 0 <= row < self.row_count and self.ordered_rows[row].key.value.startswith(BLANK)

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
    click, the first just moving the cursor there. The buttons under it act on the
    highlighted row and never take focus, so Tab goes straight on to the items; these keys
    press them instead."""

    BINDINGS = [Binding("r", "app.press('rename')", "Rename", show=False),
                Binding("l", "app.press('relaunch')", "Relaunch", show=False),
                Binding("s", "app.press('restore')", "Restore", show=False),
                Binding("p", "app.press('park')", "Park", show=False),
                Binding("e", "app.press('end')", "End", show=False)]

    async def action_select_cursor(self) -> None:
        await select_now(self)

    def on_resize(self) -> None:
        if getattr(self.app, "session_list", None) is self:   # its room: the parked sessions keep to its foot
            self.app.paint_sessions()

    def watch_show_horizontal_scrollbar(self, shown: bool) -> None:
        self.on_resize()   # a long name's scrollbar takes the room's last line

    async def _on_click(self, event) -> None:
        # Textual runs every class's _on_click in turn, DataTable's after this one, so don't
        # call it here too: that made one click select twice and open two relaunch prompts
        if self.blank(event.style.meta.get("row", -1)):   # the gap above the parked sessions
            event.prevent_default()
            return
        self.app.settle()   # first what the keys before it moved, as a key's land does
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
# shown only where it applies (#62)
LIFECYCLE = (("Rename", "rename"), ("Relaunch", "relaunch"), ("Restore", "restore"), ("Park", "park"), ("End", "end"))
# a session's conversation buttons, (caption, id, variant): under those, for the current
# session, highlighted there (D20); in a full-screen item's bar, for its session
CONVERSATION = (("Mode", "mode", "default"), ("Send", "send", "primary"), ("Interrupt", "interrupt", "error"),
                ("Compact", "compact", "default"), ("Shell", "shell", "default"))


def show_button(root: Widget, button_id: str, label: str, disabled: bool, shown: bool = True) -> None:
    """A button's caption and state, wherever it lives under root."""
    button = next(iter(root.query(f"#{button_id}")), None)
    if button is None:   # the app's first refresh can come before the buttons mount
        return
    if str(button.label) != label:
        button.label = label
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
        label = next(iter(self.query("#activity")), None)
        if label is not None and str(label.content) != text:
            label.update(text)


class PermissionButtons(Horizontal):
    """Over the answer box while the item in context is an open permission (P) item: the
    answer is a button, applied at once whatever the session's send mode (#53). Text
    typed in the box below is optional: Ctrl+Enter denies with it, also at once."""
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
        self.set_focus(self.box)   # at once, not after a refresh as focus() does: keys behind Enter go there

    def action_leave(self) -> None:
        self.app.keep_unsent(self.box, (self.sid, self.ref), None)
        self.app.pop_screen()
        # the inbox box takes back its target's text, the bar its session: now, not after a
        # refresh, where text typed into the box behind Esc would be swapped out (take_key)
        self.app.retarget()

    def paint(self) -> None:
        s = self.app.store.session(self.sid)
        blocks = thread_blocks(self.app.store, self.sid, self.ref, s and (s["name"] or short(self.sid)))
        text = "\n\n".join(md for _, md in blocks)
        if text != self.text:
            self.text = text
            self.query_one("#thread", Transcript).update(render(blocks))
            self.query_one("#thread-scroll").scroll_end(animate=False)


PERMISSION_HINT = "Allow, Always or Deny above, at once · or type what to do instead: Ctrl+Enter denies with it"


def hint(sends: str, dead: bool = False) -> str:
    """The line under an answer box. sends: what Ctrl+Enter does for the session in context
    by its mode, queue or send. dead: it isn't running, so Ctrl+S refuses (#62) and goes
    unmentioned."""
    ctrl_s = "" if dead else " · Ctrl+S sends its queue"
    return (f"Ctrl+Enter to {sends}{ctrl_s} · Ctrl+T switches mode"
            " · Ctrl+R takes a queued answer back")


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
    """Name and ticket are optional; the brief becomes the first prompt."""
    BINDINGS = [Binding("escape", "answer", show=False)]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("NEW SESSION", classes="dialog-title")
            with Horizontal(id="cwd-row"):
                yield Input(value=os.getcwd(), placeholder="working directory", id="cwd")
                yield Button("Browse…", id="browse")
            yield Input(placeholder="name (optional)", id="name")
            yield Input(placeholder="ticket: #42, owner/repo#42 or ABC-123 (optional)", id="ticket")
            yield TextArea(id="brief")
            yield Checkbox("Open in a terminal tab instead of the wheelhouse", default_runner() == "tab", id="tab")
            with Horizontal(classes="buttons"):
                yield Button("Launch", variant="success", id="launch")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed, "#launch")
    def launch(self) -> None:
        cwd = self.query_one("#cwd", Input).value.strip()
        if not os.path.isdir(cwd):
            self.notify(f"no such directory: {cwd}", severity="error")
            return
        self.action_answer({"cwd": cwd, "name": self.query_one("#name", Input).value.strip(),
                            "ticket": self.query_one("#ticket", Input).value.strip(),
                            "brief": self.query_one("#brief", TextArea).text.strip(),
                            "runner": "tab" if self.query_one("#tab", Checkbox).value else "sdk"})

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
        pid = liveness.running_pid(c.id)   # check again: it may have exited, or come back
        if pid:
            self.query_one("#adopt-hint", Label).update(
                f"Still running (pid {pid}): type /exit in its tab, then press Adopt.")
            return
        self.action_answer({"candidate": c, "name": self.query_one("#adopt-name", Input).value.strip()})

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.action_answer(None)



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


class TutorialOffer(Dialog):
    """The first-run offer, one line: Enter takes the tutorial, Esc dismisses it for good."""
    BINDINGS = [Binding("enter", "answer(True)", show=False), Binding("escape", "answer(False)", show=False)]

    def compose(self) -> ComposeResult:
        yield Label(Text.assemble(("New here? ", "bold #ffd300"), ("Enter", "bold"), ": take the tutorial · ",
                                  ("Esc", "bold"), ": dismiss  (make tutorial runs it any time)"), id="offer")



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
    "send_session": "Send the current session's queue (a dead one's waits: Restore it first)",
    "toggle_mode": "Switch the session between Queued and Immediate",
    "recall": "Take this item's latest queued answer back into the box",
    "new_session": "New session",
    "adopt": "Adopt: pick a Claude Code session that isn't in the wheelhouse yet, from those on disk",
    "restore_all": "Restore all: every dead session that isn't parked",
    "clear_filter": "Clear a text selection first, then the marks, else show every session's items again",
    "show_finished(True)": "Show or hide finished items",
    "show_finished(False)": "Show or hide finished items",
    "app.close_question": ("Close the highlighted question or decision, or dismiss a done task or subagent "
                           "(or every marked one); on a finished one, bring it back"),
    "help": "This list",
    "quit": "Quit the wheelhouse (sessions carry on without it)",
    "toggle_mark": "Mark or unmark the highlighted row",
    "extend(-1)": "Extend the marks up",
    "extend(1)": "Extend the marks down",
    "select_all": "Select all of it, to copy",
    "leave": "Back to the inbox",
    "app.press('rename')": "Rename the highlighted session",
    "app.press('relaunch')": "Relaunch it",
    "app.press('restore')": "Restore it, if it's dead",
    "app.press('park')": "Park or unpark it",
    "app.press('end')": "End it",
    "press('interrupt')": "Interrupt the session's current turn",
    "press('compact')": "Compact the session",
    "press('shell')": "Shell: open the session in a Claude Code tab",
    "press('allow')": "Allow the permission item in context",
    "press('always')": "Always: allow it, and keep the rule",
    "press('deny')": "Deny it, with what's typed in its box as what to do instead",
    "press('send-all')": "Send all: every running session's queue (a dead one's waits until it's restored)",
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
    "send-all": "Send all (Shift+A), above the footer: send every running session's queue; a dead one's waits until it's restored",
    "allow": "Allow (1; over the answer box, on a permission item): let the tool call run, at once",
    "always": "Always (2): allow it, and keep the rule Claude Code suggests",
    "deny": "Deny (3): refuse it (or type what to do instead and press Ctrl+Enter: denied with that, at once)",
    "interrupt": "Interrupt (I): stop the session's current turn, like Esc in Claude Code",
    "compact": "Compact (C): the session says what to keep, then is compacted with that",
    "shell": "Shell (H): open the session in a real Claude Code tab; it comes back when you /exit",
    "relaunch": "Relaunch (L in the session list): stop the session and start it again where it left off (a tab session once it has exited)",
    "restore": "Restore (S in the session list): bring back a dead session where it left off",
    "rename": "Rename (R in the session list): the session's name in the wheelhouse",
    "park": "Park / Unpark (P in the session list): drop a session's items off the inbox (it stays, dimmed, at the foot of the list), or bring them back",
    "end": "End (E in the session list): the session does its own end steps, then its wheelhouse data is deleted",
}
# keys for the buttons that have no other: each presses its button on the screen in front,
# outside a text box (whose letters and digits are typing). 1, 2 and 3 number the
# permission answers as Claude Code's own prompt does. Shift+A twice, as Shift+S
BUTTON_KEYS = [
    Binding("i", "press('interrupt')", "Interrupt", show=False),
    Binding("c", "press('compact')", "Compact", show=False),
    Binding("h", "press('shell')", "Shell", show=False),
    Binding("1", "press('allow')", "Allow", show=False),
    Binding("2", "press('always')", "Always", show=False),
    Binding("3", "press('deny')", "Deny", show=False),
    Binding("A", "press('send-all')", "Send all", show=False),
    Binding("shift+a", "press('send-all')", "Send all", show=False),
]
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
TABS = ("Tab goes round the panes: the session list, the items, the answer box, then the "
        "conversation, and Shift+Tab back. Ctrl+Enter in the answer box goes back to the items with "
        "the cursor where it was, so Down then Tab answers the next one. An item opened full screen "
        "has two stops, its answer box and its thread. In the conversation or a thread, Up, Down, PgUp, "
        "PgDn, Home and End scroll it. The buttons never take focus: each has a key.")
SEND_RULES = (
    "**Queued** (the default): Ctrl+Enter holds each answer until Ctrl+S (or Send) sends the "
    "session's queue as one message, so related answers arrive together; ✉ counts what's queued. "
    "A dead session's queue waits: Ctrl+S and Send all leave it until the session is restored. "
    "**Immediate**: Ctrl+Enter sends at once. Ctrl+T switches the session's mode; Ctrl+R takes a "
    "queued answer back to edit.")
MOUSE = ("Click selects a row. Ctrl+click marks rows and Shift+click a range (Windows Terminal may "
         "keep Shift+click for itself: Space and Shift+Up/Down do the same), then Delete or Backspace closes them together. "
         "Right-click copies the selection, or with none pastes into the answer box, as a terminal does. "
         "Drag the lines between the panes to resize them (they light up under the pointer); the sizes are "
         "kept for next time, and a double-click on a line puts its default back.")
WHOSE = ("The buttons under the session list act on the session highlighted there, and only those that "
         "apply show: Rename, Relaunch and Park on a running one, Restore on a dead one, End on either, "
         "Unpark on a parked one; then Mode, Send on one that isn't dead, and on one run in the wheelhouse "
         "Interrupt, Compact and Shell. Hover over one for what it does, and its key is in brackets below. New, Adopt and Restore all are keys in the footer. An item "
         "opened full screen has Mode, Send and the rest in its bar, for its own session. Ctrl+S and Ctrl+T "
         "act on the same session: highlighting an item highlights its session there.")


def keys_help() -> str:
    """The ? overlay: every key, from the bindings themselves, and every button."""
    everywhere = [b for b in WheelhouseApp.BINDINGS if b not in BUTTON_KEYS]
    sections = [("Everywhere", everywhere), ("Outside a text box", BUTTON_KEYS),
                ("Moving between panes", TAB_KEYS), ("Either list", LIST_KEYS),
                ("The session list", SessionList.BINDINGS),
                ("The item list", ItemList.BINDINGS),
                ("An answer box", Compose.BINDINGS), ("The conversation pane", Transcript.BINDINGS),
                ("An item opened full screen", ThreadView.BINDINGS)]
    out = ["# Keys"]
    for title, bindings in sections:
        out.append(f"## {title}")
        actions: dict[str, list[str]] = {}
        for b in bindings:
            keys = actions.setdefault(b.action, [])
            if key_name(b.key) not in keys:   # Shift+S, bound as both S and shift+s
                keys.append(key_name(b.key))
        # dict.fromkeys: one line for a key bound twice, as F is (its footer label changes)
        out += dict.fromkeys(f"- **{' or '.join(keys)}**: {DESCRIBE[action]}" for action, keys in actions.items())
    out += ["## Tab order", TABS, "## Mouse", MOUSE, "## Buttons", WHOSE, *[f"- {text}" for text in BUTTONS.values()],
            "## Sending", SEND_RULES,
            "## The tutorial", "`make tutorial` starts it afresh any time. Esc or ? closes this list."]
    return "\n\n".join(out)


class KeysHelp(Dialog):
    """? : every key and button, and how sending works."""
    BINDINGS = [Binding("escape,question_mark,q", "answer", show=False)]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="keys-dialog"):
            yield Static(RichMarkdown(keys_help()), id="keys")



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
    #conversation-buttons #mode {{ column-span: 2; }}
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
    NewSession, PickDirectory, Confirm, Choice, AdoptSession, KeysHelp {{ align: center middle; }}
    #cwd-row {{ height: auto; }}
    #cwd {{ width: 1fr; }}
    #folders {{ height: 20; background: #000000; }}
    TutorialOffer {{ align: center top; }}
    #offer {{ width: 100%; height: 1; background: #12122a; color: #e8e8e8; padding: 0 1; }}
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
        # New, Adopt and Restore all act on no one session, so they're here, not buttons (#62)
        Binding("n", "new_session", "New", key_display="N"),
        Binding("a", "adopt", "Adopt", key_display="A"),
        # Shift+S twice: some terminals send it as S, others as shift+s
        Binding("S", "restore_all", "Restore all", key_display="Shift+S"),
        Binding("shift+s", "restore_all", "Restore all", show=False),
        Binding("escape", "clear_filter", "All sessions", key_display="Esc"),
        # one key, two bindings: the footer shows the one that applies (check_action)
        Binding("f", "show_finished(True)", "Show finished", key_display="F"),
        Binding("f", "show_finished(False)", "Hide finished", key_display="F"),
        Binding("question_mark", "help", "Keys", key_display="?"),
        Binding("q", "quit", "Quit", key_display="Q"),
        *BUTTON_KEYS,
    ]

    def __init__(self, store: Store | None = None):
        super().__init__()
        ScrollBar.renderer = KnurlRender   # Textual's hook for every scrollbar: a class variable
        self.store = store or Store()
        self.wake = liveness.WakeDetector()
        self.waking = False
        self.frame = 0
        self.filter_sid: str | None = None
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
        self.repin: set[str] = set()   # items the person just closed or reopened (close): pinned where they go
        self.ranks: dict[str, tuple] = {}   # each item's rank as last shown
        self.show_finished = False   # finished items (store.standing), after the rest
        # each list's rows that change sides (an item settling, a session parked) fall or rise
        # there (T66, T71): a frame repaints the list, but not while a key lands (tick)
        self.session_sink = Sink(lambda: self.landing or not self.screen_stack or self.paint_sessions())
        self.item_sink = Sink(lambda: self.landing or not self.screen_stack or self.paint_items())
        # the highlighted row: (session id, item ref), or (session id, None) for the
        # session's conversation, the first row while a session is selected
        self.selected: tuple[str, str | None] | None = None   # its setter restarts the dwell (look)
        self.followers: dict[str, transcript.Follower] = {}
        # each session's context size for the session list: read on workers, by a follower
        # shared with the stats pane (stats.follower), so each transcript is read once
        self.contexts: dict[str, stats.UsageFollower] = {}
        # each session's subagents, tracked as A items (#69): read on workers, as contexts are
        self.agent_watchers: dict[str, subagents.AgentWatcher] = {}
        self.agent_errors: dict[str, str] = {}   # each session's watcher error last shown
        self.modules = api.load()
        self.ctx = api.Context(self.store.path.parent, self.module_sessions, self.focus_sid)
        self.panes: list[Widget] = []   # the modules' widgets, mounted in their slots
        self.module_ids: dict[str, str] = {}   # each pane's widget id: the module that has it
        # unsent text typed for each target, (session id, ref or None), kept in memory only
        self.unsent: dict[tuple, str] = {}
        self.box_target: tuple | None = None
        self.landing = False   # a key's land under way, which the refresh tick waits out (tick)
        self.statuses: dict[str, str] = {}
        self.sessions = []
        # sessions whose host Relaunch has stopped: started again once it has gone. Each has
        # a monotonic deadline and the stopped host, (pid, start time)
        self.relaunching: dict[str, tuple[float, tuple]] = {}
        # the tutorial steps only the screen sees (a question opened, the conversation
        # followed), by tutorial session

    def compose(self) -> ComposeResult:
        yield Static(id="title")
        with Columns(id="main"):
            # the sessions, the highlighted one's description, and what can be done to it
            with Vertical(id="sessions-pane", classes="panel"):
                yield SessionList(id="session-list", cursor_type="row")
                yield Splitter("session-info", "#session-info", "#session-list", "y")
                with VerticalScroll(id="session-info"):
                    yield Static(id="session-info-text")
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
                yield Compose(id="answer")
                yield Hint(classes="answer-hint")
        yield SendBar()
        yield Footer()

    async def on_mount(self) -> None:
        await self.mount_panes()
        # held, not queried: timers fire while a dialog is on top and during shutdown
        self.title_bar = self.query_one("#title", Static)
        self.items_table = self.query_one("#items", ItemList)
        self.detail = self.query_one("#detail", Transcript)
        self.detail_scroll = self.query_one("#detail-scroll", VerticalScroll)
        self.answer = self.query_one("#answer", Compose)
        self.session_list = self.query_one("#session-list", SessionList)
        self.session_list.sink, self.items_table.sink = self.session_sink, self.item_sink
        self.session_info = self.query_one("#session-info-text", Static)
        # out of the Tab order, which goes from the session list straight to the items: the
        # buttons still click, and the list's own keys press them (SessionList)
        self.conversation_buttons = self.query_one("#conversation-buttons", Grid)
        self.session_buttons = self.query_one("#session-buttons", Grid)
        # the conversation's one stop is its transcript, not this scroll too: its keys
        # still scroll it, an ancestor's bindings (TAB_ORDER)
        for widget in (self.query_one("#session-info"), self.query_one("#checklist-scroll"), self.detail_scroll,
                       *self.query("#sessions-pane Grid Button")):
            widget.can_focus = False
        for splitter in self.query(Splitter):   # the sizes the person dragged to, last time
            if (stored := share(self.store.setting(LAYOUT + splitter.key))) is not None:
                splitter.apply(stored)
        self.eye_col = self.session_list.add_columns("", "session", "ctx", "?", "D", "✉", "")[-1]
        self.checklist = self.query_one("#checklist", Static)
        self.set_interval(0.1, self.animate)
        self.set_interval(1.0, self.tick)
        # a screen over the inbox, or a thread opened, is a break in looking: the dwell restarts
        self.screen_change_signal.subscribe(self, lambda _: self.look(), immediate=True)
        self.refresh_data()
        if tutorial.should_offer(self.store):
            self.push_screen(TutorialOffer(), self.offer_answered)

    def fit_layout(self) -> None:
        """The panes' sizes kept within the terminal: one kept from a wider one shrinks."""
        if self.screen_stack:
            fit(self.screen_stack[0].query(Splitter))

    def refit(self) -> None:
        """Fit again once laid out: a pane's content changed height, not the terminal."""
        self.call_after_refresh(self.fit_layout)

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

    def animate(self) -> None:
        self.frame += 1
        self.title_bar.update(shimmer(self.frame))
        if self.frame % 2 == 0:
            self.sweep_eyes()
            # it rests while you type. No screen at all while the app shuts down, when
            # asking what has focus would raise
            if self.screen_stack and not isinstance(self.focused, Compose):
                self.each_pane("animate", self.frame // 2)   # 5 frames a second, as in the dashboard

    def tick(self) -> None:
        """The refresh, each second, skipped while a key lands (land): a refresh settles, and a
        burst's arrows are settled by the keys behind them that act on the cursor, or once the
        burst is done (settle), not wherever a tick falls. Nor once the screens are gone: the
        timer can still fire while the app shuts down, and there is nothing left to paint."""
        if not self.landing and self.screen_stack:
            self.refresh_data()

    def refresh_data(self) -> None:
        self.waking = self.wake.tick()
        self.sessions = self.store.sessions()
        # liveness only: the wheelhouse never deletes or parks anything by itself
        self.statuses = {s["id"]: liveness.status(s, waking=self.waking) for s in self.sessions}
        if self.filter_sid and not any(s["id"] == self.filter_sid for s in self.sessions):
            self.clear_filter()   # the followed session went (ended elsewhere): as Esc, no ghost row
        self.read_contexts()
        self.watch_agents()
        self.paint_sessions()
        self.paint_items()
        self.each_pane("tick")
        self.finish_relaunches()
        self.paint_session_info()
        self.paint_sendbar()
        self.paint_checklist()
        if isinstance(self.screen, ThreadView) and self.screen.is_mounted:   # not before its widgets exist
            self.screen.paint()   # an action here (queue, take back) shows at once

    @property
    def selected(self) -> tuple[str, str | None] | None:
        """The highlighted row: (session id, item ref), or (session id, None) for the session's
        conversation. Setting it restarts the dwell (look)."""
        return self._selected

    @selected.setter
    def selected(self, value: tuple[str, str | None] | None) -> None:
        self._selected = value
        self.look()

    def look(self) -> None:
        """What the person is looking at, the selection on this screen, may have changed: if
        it has, the dwell starts again. One one-shot timer, restarted on each change and none
        while nothing is selected: a pass over an item, or a rest shorter than DWELL, leaves
        it as it was (dwelt)."""
        if not self.is_running or not self.screen_stack:   # before the app runs, or as it shuts down
            return
        at = (self._selected, self.screen)
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
            self.dwell_timer = self.set_timer(DWELL, lambda: self.call_later(self.dwelt, gen))

    def dwelt(self, gen: int) -> None:
        """The person has looked at the selection for DWELL without a break: an unseen decision
        becomes seen, and a tutorial step the person's own selection makes ticks (saw). Only if
        it is still what they look at: the same selection, once the cursors have settled (a
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
        if ref and self.store.mark_seen(sid, ref):   # a no-op unless it's an unseen decision
            self.paint_sessions()   # its D count
            self.paint_items()   # it keeps its place while selected (ranked), and sinks once left
            self.paint_detail()
            if isinstance(screen, ThreadView) and screen.is_mounted:
                screen.paint()

    @property
    def viewing(self) -> str | None:
        """The session whose conversation the right pane follows, if that row is highlighted."""
        return self.selected[0] if self.selected and self.selected[1] is None else None

    def running(self, sid: str) -> bool:
        return self.statuses.get(sid) in RUNNING

    def dead(self, sid: str) -> bool:
        """It can't receive (D30): Send, Ctrl+S and Send all leave its queue alone (#62)."""
        return self.statuses.get(sid) == "dead"

    def stale(self, s) -> bool:
        """Running older wheelhouse code: a relaunch picks up the new code."""
        return self.running(s["id"]) and needs_relaunch(s)

    def sends_now(self, s) -> bool:
        """Running code from before queued answers, which would deliver one at once: so
        Ctrl+Enter sends, whatever the mode."""
        return self.running(s["id"]) and not can_queue(s)

    @staticmethod
    def pending(s) -> str | None:
        """The request (end or park) a session has been asked to act on, if any."""
        return next((what for what in PENDING if s[f"{what}_requested_at"]), None)

    def shown_status(self, s) -> str:
        st = self.statuses.get(s["id"], "dead")
        if s["id"] in self.relaunching:
            return "relaunching"
        what = self.pending(s)
        return PENDING[what] if what and st in RUNNING else st

    def busy(self, s) -> bool:
        working = bool(s["running"]) or (runner(s) == "sdk" and not (s["activity"] or "idle").startswith(RESTING))
        return working and self.statuses.get(s["id"]) in ("live", "stalled")

    def read_contexts(self) -> None:
        """The context size of each session with a bar, read on a worker thread, never the
        UI's: every one not parked, read once if it isn't running, and every one running,
        parked or not. A read the stats pane has under way counts."""
        listed = {s["id"]: s for s in self.sessions if not s["parked"] or self.running(s["id"])}
        for sid in [sid for sid in self.contexts if sid not in listed]:
            del self.contexts[sid]
        for sid in listed:
            follower = self.contexts.setdefault(sid, stats.follower(sid))
            if follower.reading or (follower.ready and not self.running(sid)):
                continue
            follower.reading = True
            self.run_worker(functools.partial(self.read_context, follower), thread=True, group="contexts",
                            exit_on_error=False)

    def watch_agents(self) -> None:
        """Each session's subagents brought up to date as A items, on a worker thread: every
        one not parked once, every one running each tick, and once more when one dies with
        a subagent item still running, which then fails (parked or not): one parked and dead
        already when the wheelhouse starts too, by the items it tracks (running_agents). The
        items show at the next refresh. A sync's failure shows once, as a warning, until it changes."""
        watchers = self.agent_watchers
        listed = {s["id"]: s for s in self.sessions if not s["parked"] or self.running(s["id"])
                  or (watchers[s["id"]].unfinished() if s["id"] in watchers else s["running_agents"])}
        for sid in [sid for sid in watchers if sid not in listed]:
            del watchers[sid]
            self.agent_errors.pop(sid, None)
        for sid, s in listed.items():
            watcher = watchers.setdefault(sid, subagents.AgentWatcher(sid))
            if watcher.error and watcher.error != self.agent_errors.get(sid):
                self.agent_errors[sid] = watcher.error
                self.notify(f"{self.display_name(s)}: {watcher.error}", severity="warning")
            alive = self.running(sid)
            if watcher.syncing or (watcher.ready and not alive and not watcher.unfinished()):
                continue
            watcher.syncing = True
            self.run_worker(functools.partial(self.sync_agents, watcher, alive), thread=True, group="agents",
                            exit_on_error=False)

    def sync_agents(self, watcher: subagents.AgentWatcher, alive: bool = True) -> None:
        """On a worker thread. A failed sync (the session ended under it, a transcript gone
        between stat and open) is tried again next tick, from where it got to, and says why
        in the watcher's error."""
        try:
            watcher.sync(self.store, alive=alive)
            watcher.error = None
        except Exception as e:
            watcher.error = f"couldn't track subagents: {e}"[:120]
        finally:
            watcher.syncing = False

    def read_context(self, follower: stats.UsageFollower) -> None:
        """On a worker thread. A failed read leaves the last size up; the next tick reads
        again. One that brought anything shows at once, in the stats pane too if it shows
        that session: the follower is its as well, and it started no read of its own while
        this one was under way."""
        first = not follower.ready
        if stats.read_safely(follower) or first:
            try:
                self.call_from_thread(self.read_landed, follower.sid)
            except RuntimeError:   # the app is closing
                pass

    def read_landed(self, sid: str) -> None:
        self.paint_sessions()
        self.each_pane("landed", sid)   # a repaint, not a tick: a tick would start another read

    def context_cell(self, s) -> Text | str:
        follower = self.contexts.get(s["id"])
        if not (follower and follower.ready):
            return ""
        size = stats.current(follower.snap, host_context(s)).context
        return stats.context_bar(size) if size else ""

    def sweep_eyes(self) -> None:
        """Move the Cylon eye on busy rows without rebuilding the table."""
        for s in self.sessions:
            if self.busy(s) and s["id"] in self.session_list.rows:
                self.session_list.update_cell(s["id"], self.eye_col, cylon(self.frame))

    def paint_sessions(self) -> None:
        """Every session: parked ones dimmed, at the foot of the list's room (they fall there
        as they park, and rise as they're unparked: Sink), so the buttons below still reach
        them (Unpark, Restore, End)."""
        table = self.session_list
        keep, key = table.cursor_row, self.current_session()
        pending = key != self.sessions_cursor   # the person's move, not yet settled
        rows = []
        for s in sorted(self.sessions, key=lambda s: s["parked"]):   # stable: creation order within each
            st = self.shown_status(s)
            busy = cylon(self.frame) if self.busy(s) else Text("")
            dot = Text("●", style="#777777" if s["parked"] else STATUS_STYLE[st])
            name = Text(self.display_name(s), style="dim" if s["parked"] else "")
            if self.stale(s):
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

    @staticmethod
    def display_name(s) -> str:
        return s["name"] or os.path.basename(s["cwd"]) or short(s["id"])

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
        keep = table.cursor_row
        rows_out = []
        names = {s["id"]: s["name"] or short(s["id"]) for s in self.sessions}
        processing = self.store.processing()
        queued = {(m["session_id"], m["item_ref"]) for m in self.store.drafts()}
        awaiting = self.store.awaiting()
        busy = queued | awaiting
        items = self.ranked(self.store.items(self.filter_sid), processing, busy)
        active, settled, finished = bands = ([], [], [])   # by their ranks as shown: a selected item's held
        for it in items:
            bands[band(self.ranks[item_key(it)])].append(it)
        rows = item_rows(active, names) + item_rows(settled, names) + item_rows(finished if self.show_finished else [], names)
        sunk = {item_key(it) for it in settled + finished}
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
            rows_out.append((item_key(it), cells))
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
        """Items in inbox order (inbox_rank), but for the selected item, pinned: it keeps the
        rank it was shown with when it was selected, so its own change (a decision seen, a
        question answered, a task done) never moves it under the person. It re-sorts once the
        selection moves on, and a settled item then sinks (Sink). The exceptions are the person's
        own explicit acts, Delete closing or reopening (close) and a permission answered
        (answer_permission): it goes at once (D33), and is pinned where it went. self.ranks keeps each item's rank as shown: the next pin's, and what has
        somewhere to go (paint_items)."""
        key = self.selected and self.selected[1] and f"{self.selected[0]}|{self.selected[1]}"
        if self.pin is None or self.pin[0] != key:
            self.pin = (key, self.ranks[key]) if key in self.ranks else None
        ranks = {item_key(it): inbox_rank(it, processing, busy) for it in items}
        if self.pin and self.pin[0] in ranks:
            if self.pin[0] in self.repin:   # closed or reopened with Delete: at once, and held there
                self.pin = (self.pin[0], ranks[self.pin[0]])
            else:
                ranks[self.pin[0]] = self.pin[1]
        self.repin = set()
        self.ranks = ranks
        return sorted(items, key=lambda it: ranks[item_key(it)])

    def keep_unsent(self, box, old, new) -> None:
        """Park what's typed for the old target and bring back what was typed for the new one."""
        if old == new:
            return
        if old is not None:
            if box.text.strip():
                self.unsent[old] = box.text
            else:
                self.unsent.pop(old, None)
        text = self.unsent.pop(new, "") if new is not None else ""
        if box.text != text:
            box.text = text   # which puts the cursor at the start: carry on typing at the end
            box.move_cursor(box.document.end)

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

    def paint_detail(self) -> None:
        if self.viewing:
            blocks = self.conversation(self.viewing)
        elif self.selected:
            blocks = thread_blocks(self.store, *self.selected)
        else:   # what was shown has gone (a followed session ended, say): not left standing
            blocks = [("note", NOTHING_SELECTED)]
        text = "\n\n".join(md for _, md in blocks)
        if text != getattr(self, "_detail_text", None):
            following = self.viewing and self.detail_scroll.scroll_y >= self.detail_scroll.max_scroll_y - 1
            self._detail_text = text
            self.detail.update(render(blocks))
            if following:   # stay at the newest turn, unless the person has scrolled up to read
                self.call_after_refresh(self.detail_scroll.scroll_end, animate=False)

    def sent(self, sid: str, msg_id: int) -> str | None:
        """The full text of a message the person sent, for one a notification cut short."""
        m = self.store.message(sid, msg_id)
        return m["body"] if m else None

    def conversation(self, sid: str) -> list[tuple[str, str]]:
        s = self.store.session(sid)
        if s is None:
            self.selected = None
            return [("note", "_gone_")]
        follower = self.followers.setdefault(sid, transcript.Follower(sid))
        recs = follower.read()
        queued = tuple(m["body"] for m in self.store.drafts(sid) if m["item_ref"] is None)
        tab = None if runner(s) == "sdk" and not s["shell"] else launch.tab_title(s)
        key = (s["name"] or short(sid), tab, follower.seen, queued)
        if follower.blocks_key != key:   # parsed once per change, not on every refresh tick
            follower.blocks_key, follower.blocks = key, transcript.blocks(*key[:2], recs, queued, functools.partial(self.sent, sid))
        return follower.blocks

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
        """The session in context, for the modules: followed, else the highlighted item's
        (the current session then, D20). With neither, None: the stats pane shows every
        running session."""
        sid = self.filter_sid or (self.selected[0] if self.selected else None)
        return sid if any(s["id"] == sid for s in self.sessions) else None

    def module_sessions(self) -> list[dict]:
        return [{"id": s["id"], "name": s["name"] or short(s["id"]), "running": self.running(s["id"]),
                 "context": host_context(s)} for s in self.sessions]

    def lifecycle_buttons(self, s) -> list[tuple[str, str, bool, bool]]:
        """A session's lifecycle buttons (LIFECYCLE), as show_button takes them, each shown
        only where it applies (#62, D26): Rename, Relaunch and Park on a live session,
        Restore on a dead one, Unpark on a parked one live or dead, End on either."""
        dead = s is not None and self.statuses.get(s["id"]) == "dead"
        live = s is not None and not dead
        parked = s is not None and bool(s["parked"])
        return [("rename", "Rename", False, live), ("relaunch", "Relaunch", False, live),
                ("restore", "Restore", False, dead),
                ("park", "Unpark" if parked else "Park", False, live or parked), ("end", "End", False, s is not None)]

    def controls(self, s) -> list[tuple[str, str, bool, bool]]:
        """A session's conversation buttons (CONVERSATION), as show_button takes them: (id,
        caption, disabled, shown). s: the session's row, or None with no session: then none
        shows. Mode applies to any session, its queue kept for it while it's dead; Send only
        to one that isn't dead, which can't receive (D30); Interrupt, Compact and Shell only
        to one running in a wheelhouse host (#62)."""
        if s is None:
            return [(id_, label, True, False) for label, id_, _ in CONVERSATION]
        if self.sends_now(s):   # its old monitor would deliver a draft at once anyway
            mode_ = ("Can't queue: relaunch", True)
        else:
            mode_ = (f"Mode: {mode(s).capitalize()}", False)
        n = s["drafts"]
        hosted = runner(s) == "sdk" and self.running(s["id"]) and not s["shell"]
        dead = self.statuses.get(s["id"]) == "dead"
        return [("mode", *mode_, True), ("send", f"Send ({n})", not n, not dead),
                *[(b, b.capitalize(), False, hosted) for b in ("interrupt", "compact", "shell")]]

    def paint_sendbar(self) -> None:
        """The bar on the screen in front: Send all, and the hint, permission buttons and
        activity of the session in context there; a full-screen item's bar also has its
        session's conversation buttons."""
        bar = next(iter(self.screen.query(SendBar)), None)
        if bar is None:
            return
        asking = self.asking(self.bar_item(self.screen)) is not None   # first: it settles
        sessions = {s["id"]: s for s in self.sessions}
        s = sessions.get(self.bar_session(self.screen))
        if bar.conversation:
            for args in self.controls(s):
                show_button(bar, *args)
        total = sum(x["drafts"] for x in self.sessions if not self.dead(x["id"]))   # Send all skips the dead
        sends = "send" if s is not None and (mode(s) == "immediate" or self.sends_now(s)) else "queue"
        for h in self.screen.query(Hint):
            h.set_base(PERMISSION_HINT if asking else hint(sends, s is not None and self.dead(s["id"])))
        for row in self.screen.query(PermissionButtons):
            if row.display != asking:
                row.display = asking
                self.refit()   # a row more or less over the answer box
        show_button(bar, "send-all", f"Send all ({total})", not total)
        if s is not None and runner(s) == "sdk":
            bar.activity("in a shell tab" if s["shell"] else (s["activity"] or "") if self.running(s["id"]) else "")
        else:
            bar.activity("")

    def paint_checklist(self) -> None:
        """While the tutorial's session exists: its steps, at the top of the right pane."""
        sid = next((s["id"] for s in self.sessions if tutorial.is_tutorial(self.store, s)), None)
        if sid is None:
            if self.checklist.display:
                self.checklist.display = False
                self.refit()
            return
        steps = tutorial.steps(self.store, sid, tutorial.seen(self.store, sid))
        nxt = next((key for key, *_, done in steps if not done), None)
        text = Text.assemble(("TUTORIAL", "bold #ffd300"), ("  ? lists every key", "#777777"))
        session = next(s for s in self.sessions if s["id"] == sid)
        why = tutorial.stopped(session) if not self.running(sid) else None
        if why:
            text.append(f"\n{why}", style="bold #ff2a6d")
            text.append("\nmake tutorial starts it afresh", style="#e8e8e8")
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
        """The person opened something of the tutorial's: a question or its conversation.
        Called only from what the person does, never from automatic selection: Enter at once,
        their own selection once they have looked at it (dwelt)."""
        s = next((s for s in self.sessions if s["id"] == sid), None)
        if not tutorial.is_tutorial(self.store, s):
            return
        if step is None:
            item = ref and self.store.item(sid, ref)
            step = "follow" if ref is None else {"question": "open"}.get(item and item["kind"])
        if step:
            tutorial.see(self.store, sid, step)
            self.paint_checklist()

    def paint_session_info(self) -> None:
        """The current session's description, under the list, and its buttons' captions and
        states: Park's, and the conversation buttons' (D20)."""
        sid = self.current_session()
        s = next((x for x in self.sessions if x["id"] == sid), None)
        text = self.describe(s) if s else Text("Select a session to see its description.", style="#777777")
        if text.plain != getattr(self, "_info_text", None):
            self._info_text = text.plain
            self.session_info.update(text)
        before = [b.display for b in self.query("#sessions-pane Grid, #sessions-pane Grid Button")]
        for grid, buttons in ((self.session_buttons, self.lifecycle_buttons(s)),
                              (self.conversation_buttons, self.controls(s))):
            for args in buttons:
                show_button(grid, *args)
            grid.display = any(shown for *_, shown in buttons)   # none: no blank row for it either
        if [b.display for b in self.query("#sessions-pane Grid, #sessions-pane Grid Button")] != before:
            self.refit()   # rows of buttons more or less under the description

    def describe(self, s) -> Text:
        """A session's description: what the old Sessions tab's row and synopsis showed."""
        st = self.shown_status(s)
        t = Text(self.display_name(s), style="bold #ffd300")
        t.append("\n")
        t.append(st, style=STATUS_STYLE[st])
        if s["parked"]:
            t.append(" · parked", style="#777777")
        if self.stale(s):
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
        if runner(s) == "sdk" and s["activity"] and self.running(s["id"]):
            t.append(f"\n{s['activity']}", style="italic #05d9e8")
        t.append("\n\n")
        if s["synopsis"]:
            t.append(s["synopsis"])
        elif s["brief"]:
            t.append("Brief: ", style="#777777")
            t.append(s["brief"])
        else:
            t.append("No synopsis yet.", style="#777777")
        return t

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
        if self.open_session(sid, restore=True):
            self.store.set_parked(sid, False)
            self.notify("relaunching")
            self.refresh_data()

    @on(Button.Pressed, "#relaunch")
    @session_action
    def relaunch_pressed(self) -> None:
        """Relaunch, in one click: a dead session as Restore brings it back; a running one
        run in the wheelhouse has its host stopped, then started again on the conversation.
        A tab is the person's to /exit: it relaunches once it has."""
        self.settle()
        sid = self.current_session()
        if not sid:
            return
        s = self.row(sid)
        if sid in self.relaunching:
            self.notify("already relaunching: waiting for its host to stop", severity="warning")
        elif self.statuses.get(sid) == "dead":
            self.offer_relaunch(sid)
        elif runner(s) != "sdk" or s["shell"]:
            self.notify(TAB_RELAUNCH, severity="warning")
        elif self.statuses.get(sid) == "starting":
            self.notify("it's still starting: Relaunch once it's running", severity="warning")
        else:
            host = (s["claude_pid"], s["claude_start"])
            self.push_screen(Confirm(f"Relaunch {self.label(sid)}? Its host stops, interrupting any turn "
                                     "under way, and starts again on the same conversation."),
                             lambda yes: yes and self.stop_host(sid, host))

    @session_action
    def stop_host(self, sid: str, host: tuple) -> None:
        """Stop a session's host, as SIGTERM does (its own clean stop: Claude Code is
        disconnected, and an open permission or question closes as withdrawn, or as lost
        when the new host starts). The confirm may have sat open meanwhile, so only the
        host it was asked about, (pid, start time), is stopped: one handed to a shell tab
        registers the tab's Claude Code instead, the person's to /exit. The refresh tick
        starts it again once the process has gone (finish_relaunches)."""
        s = self.row(sid)
        if runner(s) != "sdk" or s["shell"]:
            self.notify(TAB_RELAUNCH, severity="warning")
            return
        if not liveness.is_alive(s["claude_pid"], s["claude_start"], s["boot_id"]):
            self.relaunch(sid)   # gone meanwhile: nothing to stop
            return
        if (s["claude_pid"], s["claude_start"]) != host:
            self.notify(f"{self.display_name(s)} started again meanwhile: Relaunch again to stop this host",
                        severity="warning")
            return
        try:
            os.kill(s["claude_pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass   # exited between the check and the signal: the tick starts it again
        except OSError as e:
            self.notify(f"couldn't stop its host: {e}", severity="error")
            return
        self.relaunching[sid] = (time.monotonic() + RELAUNCH_WAIT, host)
        self.notify(f"relaunching {self.display_name(s)}: stopping its host")
        self.refresh_data()

    def finish_relaunches(self) -> None:
        """Start each stopped host again once its process has really gone: launch refuses a
        session whose registered process is alive, and a new host registers only if free.
        One that another wheelhouse has started meanwhile (a newer pid, alive) is relaunched
        already. A relaunch unparks, as one of a dead session does."""
        for sid, (deadline, host) in list(self.relaunching.items()):
            s = next((x for x in self.sessions if x["id"] == sid), None)
            if s is None:   # ended meanwhile
                del self.relaunching[sid]
            elif not liveness.is_alive(s["claude_pid"], s["claude_start"], s["boot_id"]):
                del self.relaunching[sid]
                if self.open_session(sid):   # as it ran: a host again, not WHEELHOUSE_RUNNER's way
                    self.store.set_parked(sid, False)
                    self.statuses[sid] = liveness.status(self.row(sid), waking=self.waking)   # not dead for a tick
                    self.notify(f"relaunched {self.display_name(s)}")
            elif (s["claude_pid"], s["claude_start"]) != host:
                del self.relaunching[sid]
                self.store.set_parked(sid, False)
                self.notify(f"relaunched {self.display_name(s)}")
            elif time.monotonic() > deadline:
                del self.relaunching[sid]
                self.notify(f"{self.display_name(s)}'s host didn't stop within {RELAUNCH_WAIT}s, so it wasn't "
                            f"relaunched: see hosts/{sid}.log", severity="error")

    async def action_quit(self) -> None:
        """Quit, but not silently out from under a relaunch: a host stopped and not yet
        started again would stay dead. Any whose host has gone is started now; for one still
        stopping, the person chooses."""
        self.refresh_data()
        if not self.relaunching:
            self.exit()
            return
        names = ", ".join(self.display_name(self.row(sid)) for sid in self.relaunching)
        self.push_screen(Confirm(f"Still relaunching {names}: its host hasn't stopped yet. Quit anyway, "
                                 "and leave it stopped? (Restore brings it back.)"),
                         lambda yes: yes and self.exit())

    def follow(self, sid: str) -> None:
        """Filter the items to the session and highlight its conversation row, so the right
        pane follows the conversation rather than whichever question comes first."""
        self.filter_sid = sid
        self.selected = (sid, None)
        self.picked = self.selected   # the conversation followed ticks its step once looked at (dwelt)
        self.retarget()
        self.paint_items()
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
            self.picked = self.selected   # looked at for DWELL, it's seen (dwelt)
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
            if self.viewing:
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
        (D20). The app's move, not the person's: it filters nothing."""
        table = self.session_list
        if sid in table.rows and sid != self.current_session():
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
        self.clear_filter()
        self.paint_items()
        self.retarget()

    def clear_filter(self) -> None:
        self.filter_sid = None
        if self.viewing:   # its row goes with the filter; the highlight lands on an item
            self.selected = None

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
        question or decision, or dismisses a settled task or subagent (store.standing); on a
        finished one (shown with F) it brings it back, a question as answered, a decision as
        seen. It moves at once, not sinking (D33)."""
        self.settle()
        if isinstance(self.focused, (TextArea, Input)):
            return
        if self.items_table.marked and self.screen is self.screen_stack[0]:
            self.close_marked()
            return
        target = self.composing()[1]
        item = target and target[1] and self.store.item(*target)
        if not item:
            return
        if not closable(item):
            what = "a running subagent" if item["kind"] == "agent" else f"a {item['kind']}"
            self.notify(f"{item['ref']} is {what}: its status is the session's to set", severity="warning")
            return
        reopen = standing(item) == "finished"
        self.close(item, not reopen)
        verb = "closed" if item["kind"] in CLOSABLE else "dismissed"
        self.notify(f"reopened {item['ref']} as {CLOSABLE.get(item['kind'], item['status'])}" if reopen else
                    f"{verb} {item['ref']}" + ("" if self.show_finished else ": F shows finished items"))
        self.refresh_data()

    def close(self, item, closed: bool) -> None:
        """Close a question or decision, or reopen it: a question as answered, a decision as
        seen. Dismiss a settled task or subagent, or bring it back. At once: no sinking, and
        selected, it moves (D33), the one change that moves a selected item (ranked)."""
        self.item_sink.jump()
        self.repin.add(item_key(item))
        if item["kind"] == "decision":
            self.store.close_decision(item["session_id"], item["ref"], closed)
        elif item["kind"] in DISMISSABLE:
            self.store.dismiss(item["session_id"], item["ref"], closed)
        else:
            self.store.update_item(item["session_id"], item["ref"], status="closed" if closed else "answered")

    def answer_permission(self, sid: str, ref: str, decision: str, message: str = "") -> None:
        """The person's Allow, Always or Deny: an explicit act, as Delete is, so selected, the
        item moves at once (ranked)."""
        self.store.answer_permission(sid, ref, decision, message)
        self.repin.add(f"{sid}|{ref}")

    def close_marked(self) -> None:
        """Delete on a multi-selection: closes its questions and decisions and dismisses its
        settled tasks and subagents, or brings them back if they're all finished. Anything
        else in it is left alone."""
        picked = [it for key in self.items_table.keys() if key in self.items_table.marked
                  if (it := self.store.item(*key.split("|"))) and closable(it)]
        if not picked:
            self.notify("nothing among the marked rows to close", severity="warning")
            return
        reopen = all(standing(it) == "finished" for it in picked)
        done = []
        for it in picked:
            if reopen or standing(it) != "finished":
                try:
                    self.close(it, not reopen)
                except SessionGone:   # ended meanwhile: the rest still go
                    continue
                done.append(it["ref"])
        self.items_table.set_marks(set())
        self.notify(("reopened " if reopen else "closed ") + ", ".join(done)
                    + ("" if reopen or self.show_finished else ": F shows finished items"))
        self.refresh_data()

    def action_help(self) -> None:
        if not isinstance(self.focused, (TextArea, Input)):
            self.push_screen(KeysHelp())

    def offer_answered(self, take: bool) -> None:
        """The first-run offer, answered either way: it never comes back."""
        if not take:
            self.store.set_setting(tutorial.OFFER_KEY, "dismissed")
            self.notify("make tutorial runs the tutorial any time; ? lists every key")
            return
        try:
            tutorial.start(self.store)
        except Exception as e:   # claude missing, say
            self.store.set_setting(tutorial.OFFER_KEY, "dismissed")
            self.notify(f"couldn't start the tutorial: {e}", severity="error")
            return
        self.notify("tutorial started: follow the checklist on the right")
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
        box, target, text = self.typed()
        if not box:
            return
        s = self.row(target[0])
        if self.asking(target):   # never queued: the session is waiting on it (#53)
            try:
                self.answer_permission(*target, "deny", text)
                self.notify(f"denied {target[1]}, with your message")
            except KeyError as e:   # answered meanwhile
                self.notify(str(e.args[0]), severity="warning")
                return
        elif mode(s) == "immediate":
            self.store.send(target[0], text, target[1])
            self.notify(f"sent to {aimed(target)}")
        elif self.sends_now(s):   # its old monitor would deliver a draft at once anyway
            self.store.send(target[0], text, target[1])
            self.notify(f"sent to {aimed(target)} now: that session runs older wheelhouse code, "
                        "so it can't queue until it's relaunched", severity="warning")
        else:
            self.store.queue(target[0], text, target[1])
            if self.dead(target[0]):   # aimed names the item, so say whose queue isn't running
                self.notify(f"queued for {aimed(target)}, but the session isn't running: "
                            "Restore it, then Ctrl+S or Send sends its queue")
            else:
                self.notify(f"queued for {aimed(target)}: Ctrl+S or Send sends the session's queue")
        if target[1]:   # answering a decision is an explicit act: seen at once, not after the dwell
            self.store.mark_seen(*target)
        box.text = ""
        self.refresh_data()
        if box is self.answer:   # back to the items, cursor where it was: Down, Tab answers the next
            self.screen.set_focus(self.items_table)

    def action_recall(self) -> None:
        """Take this item's latest queued answer back into the box, to edit it or drop it."""
        self.settle()
        box, target = self.composing()
        if box is None or not target:
            return
        if box.text.strip():
            self.notify("the box isn't empty: queue or clear it first", severity="warning")
            return
        drafts = [m for m in self.store.drafts(target[0]) if m["item_ref"] == target[1]]
        body = drafts and self.store.unqueue(drafts[-1]["id"])
        if not body:
            self.notify(f"nothing queued for {aimed(target)}")
            return
        box.text = body
        box.move_cursor(box.document.end)
        self.notify("taken back: submit it again, or clear it to drop it")
        self.refresh_data()

    def bar_session(self, screen) -> str | None:
        """The session in context on a screen, which its keys, buttons, hint and activity line
        all act on or describe: the thread's, else the current session, highlighted in the
        session list (D20), which follows the filter or the selected item's session."""
        if isinstance(screen, ThreadView):
            return screen.sid
        return self.current_session()

    def bar_item(self, screen) -> tuple[str, str] | None:
        """The item in context on a screen: the thread's, else the inbox's selected item."""
        self.settle()
        if isinstance(screen, ThreadView):
            return (screen.sid, screen.ref)
        if self.selected and self.selected[1]:
            return self.selected
        return None

    def asking(self, target):
        """The open permission item a target is, if it is one."""
        item = target and target[1] and self.store.item(*target)
        return item if item and item["kind"] == "permission" and item["status"] == "open" else None

    def sent_note(self, sid: str, n: int) -> str:
        s = self.store.session(sid)
        name = (s["name"] or short(sid)) if s else short(sid)
        late = "" if self.running(sid) else " (not running: delivered when it's restored)"
        return f"{n} to {name}{late}"

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
        if self.dead(sid):   # as its hidden Send button: its drafts stay drafts (#62)
            self.notify(f"{self.display_name(self.row(sid))} isn't running: Restore it first", severity="warning")
            return
        n = self.store.dispatch(sid)
        self.notify(f"sent {self.sent_note(sid, n)}" if n else "nothing queued for that session")
        self.refresh_data()

    @session_action
    def send_all(self) -> None:
        """Every queue but a dead session's, which stays queued until it's restored (#62)."""
        queued = dict.fromkeys(m["session_id"] for m in self.store.drafts())
        sent = [(sid, n) for sid in queued if not self.dead(sid) and (n := self.store.dispatch(sid))]
        if sent:
            self.notify("sent " + "; ".join(self.sent_note(sid, n) for sid, n in sent))
        elif any(self.dead(sid) for sid in queued):
            self.notify("nothing queued for a running session: a dead one's queue waits until it's restored",
                        severity="warning")
        else:
            self.notify("nothing queued")
        self.refresh_data()

    def action_toggle_mode(self) -> None:
        self.settle()
        if sid := self.context_session():
            self.toggle_mode(sid)

    @session_action
    def toggle_mode(self, sid: str) -> None:
        s = self.row(sid)
        new = "immediate" if mode(s) == "queued" else "queued"
        self.store.set_mode(sid, new)
        name = s["name"] or short(sid)
        self.notify(f"{name}: answers now send as you submit them" if new == "immediate" else
                    f"{name}: answers now queue until you send them (Ctrl+S or Send)")
        if new == "immediate" and (n := len(self.store.drafts(sid))):
            self.notify(f"{n} answer(s) still queued for {name}: Ctrl+S or Send sends them")
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
        """Answer a permission item, at once. Deny takes what's typed in the box below, if
        anything, as what to do instead."""
        self.settle()
        target = self.bar_item(event.button.screen)
        if not target:
            return
        box, aimed_at = self.composing()
        reason = emoji.convert(box.text.strip()) if box is not None and aimed_at == target and event.button.id == "deny" else ""
        try:
            self.answer_permission(*target, event.button.id, reason)
        except (KeyError, SessionGone) as e:   # answered meanwhile, or the session went
            self.notify(str(e.args[0] if e.args else e), severity="warning")
        else:
            if reason:
                box.text = ""
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
            return
        ask = {"compact": f"Compact {self.label(sid)}? It's asked for what to keep, then compacted with that.",
               "shell": f"Open {self.label(sid)} in a terminal tab? The wheelhouse hands it over, and takes "
                        "it back when you /exit the tab."}[what]
        self.push_screen(Confirm(ask), lambda yes: yes and self.host_command(sid, what))

    def host_command(self, sid: str, what: str) -> None:
        try:
            self.store.command(sid, what)
        except SessionGone:
            return
        self.notify({"interrupt": "interrupting", "compact": "compacting: asking what to keep",
                     "shell": "opening a terminal tab"}[what])

    @on(Splitter.Resized)
    def keep_layout(self, event: Splitter.Resized) -> None:
        """A splitter dragged: its size is kept for next time. Reset: the default again."""
        key = LAYOUT + event.splitter.key
        if event.fraction is None:
            self.store.clear_setting(key)
        else:
            self.store.set_setting(key, f"{event.fraction:.6f}")
        self.fit_layout()   # the default put back may not fit

    # the session area: the list's highlighted session, and its buttons

    def current_session(self) -> str | None:
        """The current session: the one highlighted in the session list (D20)."""
        table = self.session_list
        if not table.row_count or table.blank(table.cursor_row):   # a blank row only as a rebuild lands
            return None
        return table.coordinate_to_cell_key((table.cursor_row, 0)).row_key.value

    def action_new_session(self) -> None:
        if isinstance(self.focused, (TextArea, Input)):
            return
        self.push_screen(NewSession(), self.launch_new)

    def launch_new(self, form) -> None:
        if not form:
            return
        sid = self.store.create_session(**form)
        self.open_session(sid)

    def action_adopt(self) -> None:
        if isinstance(self.focused, (TextArea, Input)):
            return
        self.push_screen(AdoptSession(adopt.candidates(self.store)), self.launch_adopted)

    def launch_adopted(self, form) -> None:
        if not form:
            return
        try:
            adopt.adopt(self.store, form["candidate"], form["name"])
        except Exception as e:   # came back to life, wt.exe missing...
            self.notify(str(e), severity="error")
            return
        self.notify(f"adopting {form['name'] or short(form['candidate'].id)}")
        self.refresh_data()

    def open_session(self, sid: str, restore: bool = False) -> bool:
        """Launch a session the way it runs, a host or a tab; a restore runs it the way new
        sessions run now (launch.restore_session)."""
        try:
            (launch.restore_session if restore else launch.open_session)(self.store, sid)
        except Exception as e:   # already running, wt.exe missing...
            self.notify(str(e), severity="error")
            return False
        return True

    async def action_press(self, button_id: str) -> None:
        """A key standing in for a button that never takes focus: as a click on it, on the
        screen in front, the inbox or a full-screen item. Not in a text box, whose keys are
        typing, nor under a dialog, whose keys are its own. One that doesn't apply to what's
        in context says so rather than doing nothing."""
        if isinstance(self.focused, (TextArea, Input)) or self.composing()[0] is None:
            return
        button = next(iter(self.screen.query(f"#{button_id}")), None)
        if button is None:
            return
        if not (button.display and button.parent.display):
            whose = "the item in context" if button_id in ("allow", "always", "deny") else "this session"
            self.notify(f"{button.label} doesn't apply to {whose}", severity="warning")
        elif button.disabled:
            self.notify(f"{button.label}: nothing to do", severity="warning")
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
        screen.set_focus(nxt)   # at once, not after a refresh as focus() does: keys behind it go there

    def row(self, sid: str):
        """The session's row, or SessionGone: it can end at any moment (in-session /wheelhouse end)."""
        s = self.store.session(sid)
        if s is None:
            raise SessionGone(sid)
        return s

    def label(self, sid: str) -> str:
        s = self.row(sid)
        return f"{s['name'] or os.path.basename(s['cwd'])} ({short(sid)}, {s['cwd']})"

    @on(Button.Pressed, "#restore")
    @session_action
    def restore_pressed(self) -> None:
        self.settle()
        sid = self.current_session()
        if not sid:
            return
        if self.statuses.get(sid) != "dead":
            self.notify("only a dead session can be restored", severity="warning")
            return
        if self.open_session(sid, restore=True):
            self.store.set_parked(sid, False)

    def action_restore_all(self) -> None:
        if isinstance(self.focused, (TextArea, Input)):
            return
        dead = [s["id"] for s in self.sessions if self.statuses.get(s["id"]) == "dead" and not s["parked"]]
        if not dead:
            self.notify("nothing to restore")
            return

        def go(yes: bool) -> None:
            if yes:
                n = sum(self.open_session(sid, restore=True) for sid in dead)
                self.notify(f"restoring {n} session(s)")
        self.push_screen(Confirm(f"Restore {len(dead)} dead session(s)?"), go)

    @on(Button.Pressed, "#park")
    @session_action
    def park_pressed(self) -> None:
        self.settle()
        sid = self.current_session()
        if not sid:
            return
        if self.row(sid)["parked"]:
            self.store.set_parked(sid, False)
            self.follow(sid)   # as Park: it stays current
            self.refresh_data()
            return
        self.lifecycle(sid, "park",
                       ask=f"Ask {self.label(sid)} to park? It brings its items up to date, then parks.",
                       act=f"Park {self.label(sid)}? It drops off the inbox and Restore all.")

    @on(Button.Pressed, "#rename")
    @session_action
    def rename_pressed(self) -> None:
        self.settle()
        sid = self.current_session()
        if sid:
            self.push_screen(RenameSession(self.row(sid)["name"]), lambda name: self.rename(sid, name))

    @session_action
    def rename(self, sid: str, name: str | None) -> None:
        if name is None or name == self.row(sid)["name"]:   # cancelled, or unchanged
            return
        self.store.rename(sid, name)
        self.notify(f"renamed to {name}" if name else "name cleared: it shows its directory")
        self.refresh_data()

    @on(Button.Pressed, "#end")
    @session_action
    def end_pressed(self) -> None:
        self.settle()
        sid = self.current_session()
        if sid:
            n = len(self.store.drafts(sid))
            lost = f" {n} queued answer(s) will be discarded." if n else ""
            self.lifecycle(sid, "end",
                           ask=f"Ask {self.label(sid)} to end? Claude does its own session-end steps, then deletes its wheelhouse data.{lost}",
                           act=f"End {self.label(sid)} and delete its wheelhouse data?{lost}")

    def lifecycle(self, sid: str, what: str, ask: str, act: str) -> None:
        """Park or End. A running session is only asked; a dead one is acted on at once."""
        if not self.running(sid):
            self.push_screen(Confirm(act), lambda yes: yes and self.act_on_dead(sid, what))
            return
        pending = self.pending(self.row(sid))
        if pending is None:
            self.push_screen(Confirm(ask), lambda yes: yes and self.ask(sid, what))
        elif pending != what:
            self.notify(f"the session has already been asked to {pending}: cancel that first",
                        severity="warning")
        else:
            self.push_screen(Choice(f"{self.label(sid)} has been asked to {what} and hasn't yet. "
                                    "Cancel the request, or force it?"),
                             lambda choice: self.resolve_request(sid, what, choice))

    @session_action
    def ask(self, sid: str, what: str) -> None:
        self.store.request(sid, what)
        self.keep_current(sid, what)
        self.notify(f"asked the session to {what}")
        self.refresh_data()

    @session_action
    def act_on_dead(self, sid: str, what: str) -> None:
        """The confirm may have sat open while the session came back: check again."""
        if liveness.status(self.row(sid), waking=self.waking) in RUNNING:
            self.notify(f"the session is running again: press {what.capitalize()} to ask it instead",
                        severity="warning")
            self.refresh_data()
            return
        self.force(sid, what)

    @session_action
    def resolve_request(self, sid: str, what: str, choice: str) -> None:
        if choice == "cancel":
            self.store.cancel_request(sid, what)
            self.notify(f"{what} request cancelled")
            self.refresh_data()
        elif choice == "force":
            warning = ("Deletes its wheelhouse data now, without its session-end steps." if what == "end"
                       else "Parks it now; it is still running.")
            self.push_screen(Confirm(f"Force {what} {self.label(sid)}? {warning}"),
                             lambda yes: yes and self.force(sid, what))

    @session_action
    def force(self, sid: str, what: str) -> None:
        if what == "end":
            self.store.end(sid)
        else:
            self.store.set_parked(sid, True)
        self.keep_current(sid, what)
        self.refresh_data()

    def keep_current(self, sid: str, what: str) -> None:
        """Parking takes a session's items off the unfiltered inbox: following it keeps it
        current, its pinned conversation row selected, so Unpark is one press away."""
        if what == "park":
            self.follow(sid)
