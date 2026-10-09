"""The wheelhouse app: a Textual app. It only reads and writes the database."""

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

from . import adopt, api, emoji, launch, liveness, stats, transcript, tutorial
from .knurl import KnurlRender
from .store import CLOSED, SessionGone, Store, can_queue, default_runner, mode, needs_relaunch, runner

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
RELAUNCH_WAIT = 30   # seconds a host has to stop for Relaunch before it gives up and says so
TAB_RELAUNCH = "a session in a tab relaunches once it has exited: /exit it there, then Relaunch"
# the person's words in terminal green, Claude's in white as in the Claude app
VOICE = {"you": MATRIX, "claude": "#e8e8e8", "head": "#05d9e8",
         "warn": "bold #ffd300", "note": "#777777", "tool": "#777777"}
PENDING = {"end": "ending", "park": "parking"}
CLOSABLE = {"question": "answered", "decision": "seen"}   # what X closes, and what reopening makes it
# where each module slot (api.SLOTS) is mounted: at the end of this container
SLOT_PARENTS = {"inbox.side": "#items-pane"}


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


class SessionList(DataTable):
    """The inbox's sessions. One click selects a row: a plain table selects only on a second
    click, the first just moving the cursor there. The buttons under it never take focus, so
    Tab goes straight on to the items; these keys press them instead."""

    # Shift+S twice: some terminals send it as S, others as shift+s
    BINDINGS = [Binding("r", "app.press('rename')", "Rename", show=False),
                Binding("l", "app.press('relaunch')", "Relaunch", show=False),
                Binding("s", "app.press('restore')", "Restore", show=False),
                Binding("S", "app.press('restore-all')", "Restore all", show=False),
                Binding("shift+s", "app.press('restore-all')", "Restore all", show=False),
                Binding("p", "app.press('park')", "Park", show=False),
                Binding("e", "app.press('end')", "End", show=False)]

    async def _on_click(self, event) -> None:
        # Textual runs every class's _on_click in turn, DataTable's after this one, so don't
        # call it here too: that made one click select twice and open two relaunch prompts
        self.call_next(self._after_click, self.cursor_coordinate)

    def _after_click(self, before) -> None:
        if self.cursor_coordinate.row != before.row:   # the table selects it itself if unmoved
            self._post_selected_message()


class ItemList(DataTable):
    """The inbox's items, with a multi-selection to close questions in one go, by row key
    so it survives refreshes. Ctrl+click toggles a row, Shift+click takes the range from the
    last one toggled; Space and Shift+Up/Down do the same from the keyboard, for terminals
    that keep Shift+click for their own text selection."""

    BINDINGS = [Binding("space", "toggle_mark", "Mark", show=False),
                Binding("shift+up", "extend(-1)", "Extend up", show=False),
                Binding("shift+down", "extend(1)", "Extend down", show=False)]

    class MarksChanged(Message):
        pass

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.marked: set[str] = set()
        self.anchor: str | None = None

    def keys(self) -> list[str]:
        return [r.key.value for r in self.ordered_rows]

    def cursor_key(self) -> str | None:
        return self.keys()[self.cursor_row] if self.row_count else None

    def set_marks(self, marks: set[str], anchor: str | None = None) -> None:
        self.marked = {k for k in marks if not k.endswith("|")}   # not the conversation row
        self.anchor = anchor
        self.post_message(self.MarksChanged())

    def toggle(self, key: str) -> None:
        # the first toggle starts from the highlighted row, as file managers do
        marks = set(self.marked) or {self.cursor_key()} - {None, key}
        self.set_marks(marks ^ {key}, key)

    def extend_to(self, key: str) -> None:
        keys = self.keys()
        anchor = self.anchor if self.anchor in keys else self.cursor_key()
        lo, hi = sorted((keys.index(anchor), keys.index(key)))
        self.set_marks(set(keys[lo:hi + 1]), anchor)

    async def _on_click(self, event) -> None:
        meta = event.style.meta
        if "row" not in meta or meta["row"] < 0:
            return
        key = self.keys()[meta["row"]]
        if event.ctrl or event.shift:
            event.prevent_default()   # not DataTable's: a click on the highlighted row opens it
            self.toggle(key) if event.ctrl else self.extend_to(key)
            self.move_cursor(row=meta["row"], animate=False)
        elif self.marked or self.anchor:   # a plain click starts afresh
            self.set_marks(set(), key)

    def action_toggle_mark(self) -> None:
        if self.cursor_key():
            self.toggle(self.cursor_key())

    def action_extend(self, step: int) -> None:
        if not self.row_count:
            return
        if self.anchor not in self.keys():
            self.anchor = self.cursor_key()
        self.move_cursor(row=max(0, min(self.cursor_row + step, self.row_count - 1)), animate=False)
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

    async def _on_key(self, event) -> None:
        if event.key in ("tab", "enter"):
            code, found = self.suggestions()
            if found:
                event.prevent_default()
                event.stop()
                row, col, _ = self._before_cursor()
                self.replace(found[0][1], (row, col - len(code) - 1), (row, col))


class SendBar(Horizontal):
    """Always on screen above the footer: the current session's send mode, its queue, and
    every session's. Send all has no key: Windows Terminal sends Ctrl+Shift+S and
    Ctrl+Alt+S as plain Ctrl+S. The buttons never take focus, so clicking one leaves the
    answer box (and what's typed in it) where it was.

    A session run by a wheelhouse host also gets what its terminal would have given:
    Interrupt, Compact and Shell (the real Claude Code, in a tab), and a line saying what
    it's doing now. Allow, Always and Deny sit with the permission item (PermissionButtons)."""
    DEFAULT_CSS = """
    SendBar { height: 1; background: #12122a; }
    SendBar Button { min-width: 12; margin: 0 1 0 0; padding: 0 1; }
    SendBar #activity { width: 1fr; height: 1; color: #05d9e8; text-style: italic; padding: 0 1; }
    """

    def compose(self) -> ComposeResult:
        # compact: a variant's own border (tall, top and bottom) outranks the bar's
        # "border: none", and made each button two rows high in a one-row bar, its caption
        # on the row the footer covers
        for label, id_, variant in (("Mode", "mode", "default"), ("Send", "send", "primary"),
                                    ("Send all", "send-all", "warning"), ("Interrupt", "interrupt", "error"),
                                    ("Compact", "compact", "default"),
                                    ("Shell", "shell", "default")):
            yield Button(label, variant, id=id_, compact=True)
        yield Label("", id="activity")

    def on_mount(self) -> None:
        for button in self.query(Button):
            button.can_focus = False

    def show(self, button_id: str, label: str, disabled: bool, shown: bool = True) -> None:
        button = next(iter(self.query(f"#{button_id}")), None)
        if button is None:   # the app's first refresh can come before the bar's buttons mount
            return
        if str(button.label) != label:
            button.label = label
        button.disabled = disabled
        button.display = shown

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
            yield Button(label, variant, id=id_, compact=True)   # compact: one row, as in the send bar

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
        yield SendBar()
        yield Footer()

    def on_mount(self) -> None:
        self.box = self.query_one(Compose)
        app, me = self.app, (self.sid, self.ref)
        app.keep_unsent(app.answer, app.box_target, None)   # the inbox box's text, this item's included
        app.box_target = None
        app.keep_unsent(self.box, None, me)
        self.paint()
        app.paint_sendbar()   # its hint and bar now, not at the next refresh
        self.box.focus()

    def action_leave(self) -> None:
        self.app.keep_unsent(self.box, (self.sid, self.ref), None)
        self.app.pop_screen()
        self.app.call_after_refresh(self.app.retarget)   # the inbox box takes back its target's text, the bar its session

    def paint(self) -> None:
        s = self.app.store.session(self.sid)
        blocks = thread_blocks(self.app.store, self.sid, self.ref, s and (s["name"] or short(self.sid)))
        text = "\n\n".join(md for _, md in blocks)
        if text != self.text:
            self.text = text
            self.query_one("#thread", Transcript).update(render(blocks))
            self.query_one("#thread-scroll").scroll_end(animate=False)


PERMISSION_HINT = "Allow, Always or Deny above, at once · or type what to do instead: Ctrl+Enter denies with it"


def hint(sends: str) -> str:
    """The line under an answer box. sends: what Ctrl+Enter does for the session in context
    by its mode, queue or send."""
    return (f"Ctrl+Enter to {sends} · Ctrl+S sends its queue · Ctrl+T switches mode"
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


class NewSession(ModalScreen):
    """Name and ticket are optional; the brief becomes the first prompt."""

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
        self.dismiss({"cwd": cwd, "name": self.query_one("#name", Input).value.strip(),
                      "ticket": self.query_one("#ticket", Input).value.strip(),
                      "brief": self.query_one("#brief", TextArea).text.strip(),
                      "runner": "tab" if self.query_one("#tab", Checkbox).value else "sdk"})

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#browse")
    def browse(self) -> None:
        box = self.query_one("#cwd", Input)
        self.app.push_screen(PickDirectory(box.value.strip()), lambda path: path and setattr(box, "value", path))

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(None)


class Folders(DirectoryTree):
    """Directories only. Hidden ones are left out, but for .worktrees, where a branch's
    worktree lives."""

    def filter_paths(self, paths):
        return [p for p in paths if self._safe_is_dir(p) and (not p.name.startswith(".") or p.name == ".worktrees")]


class PickDirectory(ModalScreen):
    """The new session's working directory, picked from a tree: Enter opens a folder,
    Backspace (or Up) goes to the parent, Ctrl+Enter (or Choose) takes the highlighted one."""
    BINDINGS = [Binding("ctrl+enter", "choose", "Choose", show=False),
                Binding("ctrl+j", "choose", "Choose", show=False),
                Binding("backspace", "up", "Up", show=False)]

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
        self.query_one(Folders).focus()

    @property
    def picked(self) -> str:
        return str(self.query_one("#picked", Label).content)

    @on(DirectoryTree.NodeHighlighted)
    def highlighted(self, event) -> None:
        if event.node.data is not None:
            self.query_one("#picked", Label).update(str(event.node.data.path))

    @on(Button.Pressed, "#choose")
    def action_choose(self) -> None:
        self.dismiss(self.picked)

    @on(Button.Pressed, "#up")
    def action_up(self) -> None:
        tree = self.query_one(Folders)
        parent = os.path.dirname(os.path.abspath(str(tree.path)))
        tree.path = parent
        self.query_one("#picked", Label).update(parent)

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(None)

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(None)


class RenameSession(ModalScreen):
    """A session's name: Enter keeps it, Esc leaves it as it was. Empty clears it, and the
    session shows its directory's name."""

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
        self.dismiss(self.query_one("#new-name", Input).value.strip())

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(None)

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(None)


def ago(epoch: float, now: float | None = None) -> str:
    mins = int(((now or time.time()) - epoch) // 60)
    return f"{mins}m ago" if mins < 60 else f"{mins // 60}h ago" if mins < 48 * 60 else f"{mins // 1440}d ago"


class AdoptSession(ModalScreen):
    """Pick a session to bring into the wheelhouse. A running one must be /exit-ed first:
    the wheelhouse never kills it, and only launches once it has gone."""

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
        table.focus()

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
        self.dismiss({"candidate": c, "name": self.query_one("#adopt-name", Input).value.strip()})

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(None)

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(None)


class Confirm(ModalScreen):
    def __init__(self, prompt: str):
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.prompt)
            with Horizontal(classes="buttons"):
                yield Button(Text("[Y]es"), variant="error", id="yes")
                yield Button(Text("[N]o"), id="no")

    def on_mount(self) -> None:
        self.query_one("#no", Button).focus()   # a reflex Enter must not confirm

    def on_key(self, event) -> None:
        if event.key in ("y", "n", "escape"):
            event.stop()
            self.dismiss(event.key == "y")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class Choice(ModalScreen):
    """A pending request: cancel it, force it, or leave it be."""

    def __init__(self, prompt: str):
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.prompt)
            with Horizontal(classes="buttons"):
                yield Button(Text("[C]ancel request"), variant="success", id="cancel")
                yield Button(Text("[F]orce"), variant="error", id="force")
                yield Button(Text("Leave it [Esc]"), id="leave")

    def on_key(self, event) -> None:
        keys = {"c": "cancel", "f": "force", "escape": "leave"}
        if event.key in keys:
            event.stop()
            self.dismiss(keys[event.key])

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id)


class TutorialOffer(ModalScreen):
    """The first-run offer, one line: Enter takes the tutorial, Esc dismisses it for good."""

    def compose(self) -> ComposeResult:
        yield Label(Text.assemble(("New here? ", "bold #ffd300"), ("Enter", "bold"), ": take the tutorial · ",
                                  ("Esc", "bold"), ": dismiss  (make tutorial runs it any time)"), id="offer")

    def on_key(self, event) -> None:
        if event.key in ("enter", "escape"):
            event.stop()
            self.dismiss(event.key == "enter")


def key_name(key: str) -> str:
    """A binding's key as the person reads it: Ctrl+S, Shift+Up, Space, ?."""
    names = {"question_mark": "?", "escape": "Esc", "enter": "Enter", "space": "Space"}
    if len(key) == 1 and key.isupper():
        return f"Shift+{key}"
    return "+".join(names.get(part, part.capitalize() if len(part) > 1 else part.upper())
                    for part in key.split("+"))


# what each key does, by its action, for the ? overlay; every binding must have one
DESCRIBE = {
    "submit": "Submit what's typed: queued, or sent at once, by the session's mode",
    "app.submit": "Submit what's typed: queued, or sent at once, by the session's mode",
    "send_session": "Send the current session's queue",
    "toggle_mode": "Switch the session between Queued and Immediate",
    "recall": "Take this item's latest queued answer back into the box",
    "new_session": "New session",
    "adopt": "Adopt a Claude Code session that isn't in the wheelhouse yet",
    "clear_filter": "Clear the marks, else show every session's items again",
    "show_finished(True)": "Show or hide finished items",
    "show_finished(False)": "Show or hide finished items",
    "close_question": "Close the question or decision (or every marked one); on a closed one, reopen it",
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
    "app.press('restore-all')": "Restore all: every dead session that isn't parked",
    "app.press('park')": "Park or unpark it",
    "app.press('end')": "End it",
}
# the send bar's buttons and the session area's, by id
BUTTONS = {
    "mode": "Mode: the session's send mode, Queued or Immediate (Ctrl+T)",
    "send": "Send (n): send this session's queued answers as one message (Ctrl+S)",
    "send-all": "Send all (n): send every session's queue",
    "allow": "Allow (over the answer box, on a permission item): let the tool call run, at once",
    "always": "Always: allow it, and keep the rule Claude Code suggests",
    "deny": "Deny: refuse it (or type what to do instead and press Ctrl+Enter: denied with that, at once)",
    "interrupt": "Interrupt: stop the session's current turn, like Esc in Claude Code",
    "compact": "Compact: the session says what to keep, then is compacted with that",
    "shell": "Shell: open the session in a real Claude Code tab; it comes back when you /exit",
    "new": "New session",
    "adopt-open": "Adopt: take on a Claude Code session started outside the wheelhouse",
    "relaunch": "Relaunch: stop the session and start it again where it left off (a tab session once it has exited)",
    "restore": "Restore: bring back a dead session where it left off",
    "restore-all": "Restore all: every dead session that isn't parked",
    "rename": "Rename: the session's name in the wheelhouse",
    "park": "Park / Unpark: drop a session's items off the inbox (it stays, dimmed, at the foot of the list), or bring them back",
    "end": "End: the session does its own end steps, then its wheelhouse data is deleted",
}
SEND_RULES = (
    "**Queued** (the default): Ctrl+Enter holds each answer until Ctrl+S (or Send) sends the "
    "session's queue as one message, so related answers arrive together; ✉ counts what's queued. "
    "**Immediate**: Ctrl+Enter sends at once. Ctrl+T switches the session's mode; Ctrl+R takes a "
    "queued answer back to edit.")
MOUSE = ("Click selects a row. Ctrl+click marks rows and Shift+click a range (Windows Terminal may "
         "keep Shift+click for itself: Space and Shift+Up/Down do the same), then X closes them together. "
         "Right-click copies the selection, or with none pastes into the answer box, as a terminal does.")


def keys_help() -> str:
    """The ? overlay: every key, from the bindings themselves, and every button."""
    sections = [("Everywhere", WheelhouseApp.BINDINGS), ("The session list", SessionList.BINDINGS),
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
    out += ["## Mouse", MOUSE, "## Buttons", *[f"- {text}" for text in BUTTONS.values()],
            "## Sending", SEND_RULES,
            "## The tutorial", "`make tutorial` starts it afresh any time. Esc or ? closes this list."]
    return "\n\n".join(out)


class KeysHelp(ModalScreen):
    """? : every key and button, and how sending works."""

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="keys-dialog"):
            yield Static(RichMarkdown(keys_help()), id="keys")

    def on_key(self, event) -> None:
        if event.key in ("escape", "question_mark", "q"):
            event.stop()
            self.dismiss(None)


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
    #sessions-pane {{ width: 39; }}   /* names keep their 16 cells beside the context bar (#51) */
    #items-pane {{ width: 1fr; }}
    #items {{ height: 1fr; }}   /* the top half; the stats the bottom */
    #detail-pane {{ width: 2fr; }}
    #detail-scroll {{ height: 1fr; }}
    #detail, #thread {{ background: #000000; color: {MATRIX}; }}
    #answer, #thread-answer {{ height: 8; background: #000000; color: {MATRIX}; border: round #05d9e8; }}
    #answer:focus, #thread-answer:focus {{ border: round #ff2a6d; }}
    /* the default cursor is a pale grey cell: a hot block reads as "type here" on black */
    Compose > .text-area--cursor {{ background: #ff2a6d; color: #000000; text-style: bold; }}
    .answer-hint {{ color: #777777; height: 1; }}
    #thread-scroll {{ height: 1fr; }}
    #session-list {{ height: 1fr; }}
    #session-info {{ height: 10; background: #000000; border-top: solid #7b61ff; padding: 0 1; }}
    #session-info-text {{ color: #e8e8e8; }}
    /* three to a row, one row each. A caption takes its length and a cell either side:
       the third column fits "Restore all", the others "Relaunch" and "Unpark" */
    #session-buttons {{ height: 3; grid-size: 3; grid-columns: 1fr 1fr 13; grid-gutter: 0 1;
                        background: #000000; }}
    #session-buttons Button {{ width: 1fr; min-width: 0; padding: 0; }}
    #dialog {{ width: 80; height: auto; padding: 1 2; background: #000000; color: {MATRIX};
               border: thick #ff2a6d; }}
    .dialog-title {{ color: #ffd300; text-style: bold; }}
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

    # keys shown in upper case, the usual convention: X is the x key, not Shift+X
    BINDINGS = [
        Binding("ctrl+enter", "submit", "Submit", key_display="Ctrl+Enter"),
        Binding("ctrl+j", "submit", "Submit", show=False),   # Ctrl+Enter, as most terminals send it
        Binding("ctrl+s", "send_session", "Send", key_display="Ctrl+S"),
        Binding("ctrl+t", "toggle_mode", "Mode", key_display="Ctrl+T"),
        Binding("ctrl+r", "recall", "Edit queued", show=False, key_display="Ctrl+R"),
        Binding("n", "new_session", "New session", key_display="N"),
        Binding("a", "adopt", "Adopt", key_display="A"),
        Binding("escape", "clear_filter", "All sessions", key_display="Esc"),
        # one key, two bindings: the footer shows the one that applies (check_action)
        Binding("f", "show_finished(True)", "Show finished", key_display="F"),
        Binding("f", "show_finished(False)", "Hide finished", key_display="F"),
        Binding("x", "close_question", "Close", key_display="X"),
        Binding("question_mark", "help", "Keys", key_display="?"),
        Binding("q", "quit", "Quit", key_display="Q"),
    ]

    def __init__(self, store: Store | None = None):
        super().__init__()
        ScrollBar.renderer = KnurlRender   # Textual's hook for every scrollbar: a class variable
        self.store = store or Store()
        self.wake = liveness.WakeDetector()
        self.waking = False
        self.frame = 0
        self.filter_sid: str | None = None
        self.show_finished = False   # done, dropped, closed and failed items, after the rest
        # the highlighted row: (session id, item ref), or (session id, None) for the
        # session's conversation, the first row while a session is selected
        self.selected: tuple[str, str | None] | None = None
        self.followers: dict[str, transcript.Follower] = {}
        # each session's context size for the session list: read on workers, by a follower
        # shared with the stats pane (stats.follower), so each transcript is read once
        self.contexts: dict[str, stats.UsageFollower] = {}
        self.modules = api.load()
        self.ctx = api.Context(self.store.path.parent, self.module_sessions, self.focus_sid)
        self.panes: list[Widget] = []   # the modules' widgets, mounted in their slots
        self.module_ids: dict[str, str] = {}   # each pane's widget id: the module that has it
        # unsent text typed for each target, (session id, ref or None), kept in memory only
        self.unsent: dict[tuple, str] = {}
        self.box_target: tuple | None = None
        self.statuses: dict[str, str] = {}
        self.sessions = []
        # sessions whose host Relaunch has stopped: started again once it has gone. Each has
        # a monotonic deadline and the stopped host, (pid, start time)
        self.relaunching: dict[str, tuple[float, tuple]] = {}
        # the tutorial steps only the screen sees (a question opened, the conversation
        # followed), by tutorial session

    def compose(self) -> ComposeResult:
        yield Static(id="title")
        with Horizontal(id="main"):
            # the sessions, the highlighted one's description, and what can be done to it
            with Vertical(id="sessions-pane", classes="panel"):
                yield SessionList(id="session-list", cursor_type="row")
                with VerticalScroll(id="session-info"):
                    yield Static(id="session-info-text")
                with Grid(id="session-buttons"):
                    for label, id_, variant in (("New", "new", "success"), ("Adopt", "adopt-open", "default"),
                                                ("Rename", "rename", "default"), ("Relaunch", "relaunch", "primary"),
                                                ("Restore", "restore", "default"),
                                                ("Restore all", "restore-all", "warning"),
                                                ("Park", "park", "default"), ("End", "end", "error")):
                        yield Button(label, variant, id=id_, compact=True)   # compact: one row each
            with Vertical(id="items-pane", classes="panel"):
                yield ItemList(id="items", cursor_type="row")   # then the inbox.side panes
            with Vertical(id="detail-pane", classes="panel"):
                yield Static(id="checklist")
                with VerticalScroll(id="detail-scroll"):
                    yield Transcript("Select an item, or a session to follow its conversation.",
                                     id="detail")
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
        self.session_info = self.query_one("#session-info-text", Static)
        # out of the Tab order, which goes from the session list straight to the items: the
        # buttons still click, and the list's own keys press them (SessionList)
        for widget in (self.query_one("#session-info"), *self.query("#session-buttons Button")):
            widget.can_focus = False
        self.eye_col = self.session_list.add_columns("", "session", "ctx", "?", "D", "✉", "")[-1]
        self.items_table.add_columns("session", "ref", "status", "title")
        self.checklist = self.query_one("#checklist", Static)
        self.set_interval(0.1, self.animate)
        self.set_interval(1.0, self.refresh_data)
        self.refresh_data()
        if tutorial.should_offer(self.store):
            self.push_screen(TutorialOffer(), self.offer_answered)

    # right-click, as in a terminal

    async def on_event(self, event: events.Event) -> None:
        """Right-click copies the selection, or with none pastes. The press never reaches
        the widgets: an answer box would move its cursor and drop its selection, and the
        screen would take it for a click and clear its own."""
        if isinstance(event, (events.MouseDown, events.MouseUp)) and event.button == 3 and not event.is_forwarded:
            if isinstance(event, events.MouseDown):
                self.right_click(event)
            return
        await super().on_event(event)

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
            box.focus()
            # exclusive: a second right-click before the clipboard answers supersedes the first
            self.run_worker(functools.partial(self.paste_into, box), thread=True, group="paste", exclusive=True,
                            exit_on_error=False)

    def paste_into(self, box) -> None:
        """On a worker thread: the system clipboard, else the wheelhouse's own last copy,
        pasted as the terminal's own paste arrives, into the box (focused by now)."""
        text = system_clipboard()
        text = self.clipboard if text is None else text
        if text and self.focused is box and not get_current_worker().is_cancelled:
            self.call_from_thread(self.post_message, events.Paste(text))

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

    def refresh_data(self) -> None:
        self.waking = self.wake.tick()
        self.sessions = self.store.sessions()
        # liveness only: the wheelhouse never deletes or parks anything by itself
        self.statuses = {s["id"]: liveness.status(s, waking=self.waking) for s in self.sessions}
        self.read_contexts()
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
    def viewing(self) -> str | None:
        """The session whose conversation the right pane follows, if that row is highlighted."""
        return self.selected[0] if self.selected and self.selected[1] is None else None

    def running(self, sid: str) -> bool:
        return self.statuses.get(sid) in RUNNING

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
        """Every session: parked ones dimmed, after the rest, so the buttons below still
        reach them (Unpark, Restore, End)."""
        table = self.session_list
        keep, key = table.cursor_row, self.current_session()
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
        if fill(table, rows) and table.row_count:
            # by key: Park and Unpark move the row, and the cursor goes with it
            table.move_cursor(row=table.get_row_index(key) if key in table.rows else min(keep, table.row_count - 1),
                              animate=False)

    @staticmethod
    def display_name(s) -> str:
        return s["name"] or os.path.basename(s["cwd"]) or short(s["id"])

    def paint_items(self) -> None:
        table = self.items_table
        keep = table.cursor_row
        rows_out = []
        names = {s["id"]: s["name"] or short(s["id"]) for s in self.sessions}
        items = self.store.items(self.filter_sid)
        shown = [it for it in items if it["status"] not in CLOSED]
        rows = item_rows(shown, names)
        if self.show_finished:
            rows += item_rows([it for it in items if it not in shown], names)
        queued = {(m["session_id"], m["item_ref"]) for m in self.store.drafts()}
        awaiting = self.store.awaiting()
        if self.filter_sid:   # the session's own conversation, pinned first
            general = (self.filter_sid, None) in queued
            rows_out.append((f"{self.filter_sid}|", (names.get(self.filter_sid, "")[:14], Text("💬"),
                             Text("queued", style="bold #05d9e8") if general else "",
                             Text("Conversation", style="bold"))))
        for it, nested in rows:
            # an open question with an answer waiting to be sent shows as queued; it's stored as open
            status = "queued" if it["status"] == "open" and (it["session_id"], it["ref"]) in queued else it["status"]
            style = "dim" if status in CLOSED else "bold #05d9e8" if status == "queued" \
                else "bold #ffd300" if status == "open" else DECISION if status == "unseen" \
                else "bold #ff2a6d" if status in ("blocked", "waiting") else MATRIX
            name = names.get(it["session_id"], "")[:14]
            if (it["session_id"], it["ref"]) in awaiting and status != "queued":
                # the person spoke last: the ball is in the session's court until it replies
                cells = (name, it["ref"], Text(f"⏳ {status}", style="dim"), Text(it["title"], style="dim"))
            elif nested is None:
                cells = (name, it["ref"], Text(status, style=style), it["title"])
            else:   # a subagent, tucked under its session's name
                cells = (name if nested == 0 else "", Text(f"└ {it['ref']}", style="dim"),
                         Text(status, style=style), Text(it["title"], style="dim"))
            rows_out.append((f"{it['session_id']}|{it['ref']}", cells))
        table.marked &= {k for k, _ in rows_out}   # a marked item that went is unmarked
        rows_out = [(k, marked(cells) if k in table.marked else cells) for k, cells in rows_out]
        if fill(table, rows_out):
            key = self.selected and f"{self.selected[0]}|{self.selected[1] or ''}"
            with table.prevent(DataTable.RowHighlighted):
                if key in table.rows:   # by key: items arriving above must not move the highlight
                    table.move_cursor(row=table.get_row_index(key), animate=False)
                elif table.row_count:
                    table.move_cursor(row=min(keep, table.row_count - 1), animate=False)
            if key not in table.rows and table.row_count:   # its item went: take the one now under the cursor
                self.select_row(table.coordinate_to_cell_key((table.cursor_row, 0)).row_key.value)
        self.paint_detail()

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

    def retarget(self) -> None:
        """The answer box follows what the pane shows. Called on the person's selections
        only, never from the refresh tick, so text being typed is never swapped under them."""
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
        else:
            return
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
            await self.query_one(parent).mount_all(list(self.hosted(slot, taken)))

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
                card = Static(Text(f"{widget.id}: {type(e).__name__}: {e}", style="dim"), classes="module-error")
                widget.parent.mount(card, after=widget)
                widget.remove()

    def focus_sid(self) -> str | None:
        """The session in context, for the modules: followed, else the highlighted item's."""
        sid = self.filter_sid or (self.selected[0] if self.selected else None)
        return sid if any(s["id"] == sid for s in self.sessions) else None

    def module_sessions(self) -> list[dict]:
        return [{"id": s["id"], "name": s["name"] or short(s["id"]), "running": self.running(s["id"]),
                 "context": host_context(s)} for s in self.sessions]

    def paint_sendbar(self) -> None:
        """The bar on the screen in front: the mode and queue of the session in context there."""
        bar = next(iter(self.screen.query(SendBar)), None)
        if bar is None:
            return
        sessions = {s["id"]: s for s in self.sessions}
        s = sessions.get(self.bar_session(self.screen))
        total = sum(x["drafts"] for x in self.sessions)
        if s is None:
            bar.show("mode", "Mode", True)
        elif self.sends_now(s):   # its old monitor would deliver a draft at once anyway
            bar.show("mode", "Sends now: needs relaunch", True)
        else:
            bar.show("mode", f"Mode: {mode(s).capitalize()}", False)
        sends = "send" if s is not None and (mode(s) == "immediate" or self.sends_now(s)) else "queue"
        asking = self.asking(self.bar_item(self.screen)) is not None
        for h in self.screen.query(Hint):
            h.set_base(PERMISSION_HINT if asking else hint(sends))
        for row in self.screen.query(PermissionButtons):
            row.display = asking
        n = s["drafts"] if s else 0
        bar.show("send", f"Send ({n})", not n)
        bar.show("send-all", f"Send all ({total})", not total)
        hosted = s is not None and runner(s) == "sdk" and self.running(s["id"]) and not s["shell"]
        for button in ("interrupt", "compact", "shell"):
            bar.show(button, button.capitalize(), False, hosted)
        if s is not None and runner(s) == "sdk":
            bar.activity("in a shell tab" if s["shell"] else (s["activity"] or "") if self.running(s["id"]) else "")
        else:
            bar.activity("")

    def paint_checklist(self) -> None:
        """While the tutorial's session exists: its steps, at the top of the right pane."""
        sid = next((s["id"] for s in self.sessions if tutorial.is_tutorial(self.store, s)), None)
        if sid is None:
            self.checklist.display = False
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
        if getattr(self, "_checklist_text", None) != text.plain:
            self._checklist_text = text.plain
            self.checklist.update(text)
        self.checklist.display = True

    def saw(self, sid: str, ref: str | None, step: str | None = None) -> None:
        """The person opened something of the tutorial's: a question or its conversation.
        Called only from what the person does, never from automatic selection."""
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
        """The highlighted session's description, under the list, and the Park button's
        caption for it."""
        sid = self.current_session()
        s = next((x for x in self.sessions if x["id"] == sid), None)
        text = self.describe(s) if s else Text("Select a session to see its description.", style="#777777")
        if text.plain != getattr(self, "_info_text", None):
            self._info_text = text.plain
            self.session_info.update(text)
        park = next(iter(self.query("#park")), None)
        caption = "Unpark" if s and s["parked"] else "Park"
        if park is not None and str(park.label) != caption:
            park.label = caption

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
        sid = event.row_key.value
        self.follow(sid)
        if not any(s["id"] == sid and s["parked"] for s in self.sessions):
            self.offer_relaunch(sid)

    @session_action
    def offer_relaunch(self, sid: str) -> None:
        """Selecting a session that has died (shown red) offers to bring it back."""
        if self.statuses.get(sid) != "dead":
            return
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
        self.saw(sid, None)
        self.retarget()
        self.paint_items()
        self.call_after_refresh(self.detail_scroll.scroll_end, animate=False)

    @on(DataTable.RowHighlighted, "#items")
    def pick_item(self, event: DataTable.RowHighlighted) -> None:
        """The person moving the highlight: the pane and the answer box change target. A
        refresh never posts one (see fill), so text being typed is never swapped under them."""
        table = self.items_table
        if not event.row_key.value or not table.row_count:
            return
        if table.coordinate_to_cell_key((table.cursor_row, 0)).row_key != event.row_key:
            return   # stale: a rebuild put the cursor back before this was handled
        self.select_row(event.row_key.value)
        self.saw(*self.selected)

    def select_row(self, key: str) -> None:
        sid, ref = key.split("|")
        if self.selected != (sid, ref or None):
            self.selected = (sid, ref or None)
            if ref:
                self.store.mark_seen(sid, ref)   # a no-op unless it's an unseen decision
            self.retarget()
            self.paint_detail()
            self.each_pane("tick")
            if self.viewing:
                self.call_after_refresh(self.detail_scroll.scroll_end, animate=False)

    @on(DataTable.RowSelected, "#items")
    def open_thread(self, event: DataTable.RowSelected) -> None:
        sid, ref = event.row_key.value.split("|")
        self.saw(sid, ref or None)
        if ref:
            self.push_screen(ThreadView(sid, ref))
        else:   # the conversation is already in the pane: Enter goes to its box
            self.answer.focus()

    @on(DataTable.RowHighlighted, "#session-list")
    def pick_session_row(self) -> None:
        self.paint_session_info()

    @on(ItemList.MarksChanged)
    def marks_changed(self) -> None:
        self.paint_items()

    def action_clear_filter(self) -> None:
        if self.items_table.marked:   # Esc drops a multi-selection first
            self.items_table.set_marks(set())
            return
        self.filter_sid = None
        if self.viewing:   # its row goes with the filter; the highlight lands on an item
            self.selected = None
        self.paint_items()
        self.retarget()

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
        """Closing a question or a decision is the person's call: x closes the highlighted
        (or open) one, and on a closed one (shown with f) reopens it, a question as
        answered and a decision as seen."""
        if isinstance(self.focused, (TextArea, Input)):
            return
        if self.items_table.marked and self.screen is self.screen_stack[0]:
            self.close_marked()
            return
        target = self.composing()[1]
        item = target and target[1] and self.store.item(*target)
        if not item:
            return
        if item["kind"] not in CLOSABLE:
            self.notify(f"{item['ref']} is a {item['kind']}: its status is the session's to set",
                        severity="warning")
            return
        reopen = item["status"] == "closed"
        self.close(item, not reopen)
        self.notify(f"reopened {item['ref']} as {CLOSABLE[item['kind']]}" if reopen else
                    f"closed {item['ref']}" + ("" if self.show_finished else ": F shows finished items"))
        self.refresh_data()

    def close(self, item, closed: bool) -> None:
        """Close a question or decision, or reopen it: a question as answered, a decision as seen."""
        if item["kind"] == "decision":
            self.store.close_decision(item["session_id"], item["ref"], closed)
        else:
            self.store.update_item(item["session_id"], item["ref"], status="closed" if closed else "answered")

    def close_marked(self) -> None:
        """X on a multi-selection: closes its questions and decisions, or reopens them if
        they're all closed. Tasks and subagents in it are left alone."""
        closable = [it for key in self.items_table.keys() if key in self.items_table.marked
                     if (it := self.store.item(*key.split("|"))) and it["kind"] in CLOSABLE]
        if not closable:
            self.notify("no questions or decisions among the marked rows", severity="warning")
            return
        reopen = all(it["status"] == "closed" for it in closable)
        done = []
        for it in closable:
            if reopen or it["status"] != "closed":
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
        box, target, text = self.typed()
        if not box:
            return
        s = self.row(target[0])
        if self.asking(target):   # never queued: the session is waiting on it (#53)
            try:
                self.store.answer_permission(*target, "deny", text)
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
            self.notify(f"queued for {aimed(target)}: Ctrl+S or Send sends the session's queue")
        box.text = ""
        self.refresh_data()

    def action_recall(self) -> None:
        """Take this item's latest queued answer back into the box, to edit it or drop it."""
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
        """The session in context on a screen: the thread's, else the inbox's filter or the
        selected item's session."""
        if isinstance(screen, ThreadView):
            return screen.sid
        return self.filter_sid or (self.selected[0] if self.selected else None)

    def bar_item(self, screen) -> tuple[str, str] | None:
        """The item in context on a screen: the thread's, else the inbox's selected item."""
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
        if self.composing()[0] is None:
            return None
        sid = self.bar_session(self.screen)
        if sid is None:
            self.notify("select a session first", severity="warning")
        return sid

    def action_send_session(self) -> None:
        if sid := self.context_session():
            self.send_session(sid)

    @session_action
    def send_session(self, sid: str) -> None:
        n = self.store.dispatch(sid)
        self.notify(f"sent {self.sent_note(sid, n)}" if n else "nothing queued for that session")
        self.refresh_data()

    @session_action
    def send_all(self) -> None:
        sent = [(sid, n) for sid in dict.fromkeys(m["session_id"] for m in self.store.drafts())
                if (n := self.store.dispatch(sid))]
        self.notify("sent " + "; ".join(self.sent_note(sid, n) for sid, n in sent) if sent else "nothing queued")
        self.refresh_data()

    def action_toggle_mode(self) -> None:
        if sid := self.context_session():
            self.toggle_mode(sid)

    @session_action
    def toggle_mode(self, sid: str) -> None:
        s = self.row(sid)
        new = "immediate" if mode(s) == "queued" else "queued"
        self.store.set_mode(sid, new)
        name = s["name"] or short(sid)
        self.notify(f"{name}: answers now send as you submit them" if new == "immediate" else
                    f"{name}: answers now queue until you send them (Ctrl+S)")
        if new == "immediate" and (n := len(self.store.drafts(sid))):
            self.notify(f"{n} answer(s) still queued for {name}: Ctrl+S sends them")
        self.refresh_data()

    @on(Button.Pressed, "#mode")
    def mode_pressed(self, event: Button.Pressed) -> None:
        if sid := self.bar_session(event.button.screen):
            self.toggle_mode(sid)

    @on(Button.Pressed, "#send")
    def send_pressed(self, event: Button.Pressed) -> None:
        if sid := self.bar_session(event.button.screen):
            self.send_session(sid)

    @on(Button.Pressed, "#send-all")
    def send_all_pressed(self) -> None:
        self.send_all()

    @on(Button.Pressed, "#allow, #always, #deny")
    def permission_pressed(self, event: Button.Pressed) -> None:
        """Answer a permission item, at once. Deny takes what's typed in the box below, if
        anything, as what to do instead."""
        target = self.bar_item(event.button.screen)
        if not target:
            return
        box, aimed_at = self.composing()
        reason = emoji.convert(box.text.strip()) if box is not None and aimed_at == target and event.button.id == "deny" else ""
        try:
            self.store.answer_permission(*target, event.button.id, reason)
        except (KeyError, SessionGone) as e:   # answered meanwhile, or the session went
            self.notify(str(e.args[0] if e.args else e), severity="warning")
        else:
            if reason:
                box.text = ""
        self.refresh_data()

    @on(Button.Pressed, "#interrupt, #compact, #shell")
    def host_pressed(self, event: Button.Pressed) -> None:
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

    # the session area: the list's highlighted session, and its buttons

    def current_session(self) -> str | None:
        table = self.session_list
        if not table.row_count:
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

    @on(Button.Pressed, "#adopt-open")
    def adopt_pressed(self) -> None:
        self.action_adopt()

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

    def action_press(self, button_id: str) -> None:
        """A key standing in for a button that never takes focus: as a click on it."""
        self.query_one(f"#{button_id}", Button).press()

    @on(Button.Pressed, "#new")
    def new_pressed(self) -> None:
        self.push_screen(NewSession(), self.launch_new)

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
        sid = self.current_session()
        if not sid:
            return
        if self.statuses.get(sid) != "dead":
            self.notify("only a dead session can be restored", severity="warning")
            return
        if self.open_session(sid, restore=True):
            self.store.set_parked(sid, False)

    @on(Button.Pressed, "#restore-all")
    def restore_all_pressed(self) -> None:
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
        sid = self.current_session()
        if not sid:
            return
        if self.row(sid)["parked"]:
            self.store.set_parked(sid, False)
            self.refresh_data()
            return
        self.lifecycle(sid, "park",
                       ask=f"Ask {self.label(sid)} to park? It brings its items up to date, then parks.",
                       act=f"Park {self.label(sid)}? It drops off the inbox and Restore all.")

    @on(Button.Pressed, "#rename")
    @session_action
    def rename_pressed(self) -> None:
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
                             lambda choice: self.settle(sid, what, choice))

    @session_action
    def ask(self, sid: str, what: str) -> None:
        self.store.request(sid, what)
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
    def settle(self, sid: str, what: str, choice: str) -> None:
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
        self.refresh_data()
