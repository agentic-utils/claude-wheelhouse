"""The wheelhouse app: a Textual app. It only reads and writes the database."""

import functools
import math
import os
import re
import sqlite3
import time

from rich.markdown import Markdown as RichMarkdown
from rich.segment import Segment
from rich.style import Style
from rich.cells import cell_len
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Input,
    Label,
    Markdown,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)

from . import adopt, emoji, launch, liveness, stats, transcript, tutorial
from .store import CLOSED, SessionGone, Store, default_runner, mode, needs_relaunch, runner

MATRIX = "#00ff41"
SHIMMER = ["#ff2a6d", "#ff7b00", "#ffd300", "#05d9e8", "#7b61ff", "#d300c5"]
# a hosted session's activity when nothing is under way: errored and stopped count as resting
RESTING = ("idle", "interrupted", "stopped", "in a shell tab", "error")
MARKED = Style(bgcolor="#3a1060")   # rows picked to close together
DECISION = "bold #b967ff"   # an unseen decision: noticeable, not urgent
STATUS_STYLE = {"live": "bold #00ff41", "stalled": "bold #ffd300", "starting": "#05d9e8",
                "dead": "bold #ff2a6d", "ending": "bold #d300c5", "parking": "bold #d300c5"}
TITLE = " ▓▒░ CLAUDE·WHEELHOUSE ░▒▓ "
RUNNING = ("live", "stalled", "starting")
# the person's words in terminal green, Claude's in white as in the Claude app
VOICE = {"you": MATRIX, "claude": "#e8e8e8", "head": "#05d9e8",
         "warn": "bold #ffd300", "note": "#777777", "tool": "#777777"}
PENDING = {"end": "ending", "park": "parking"}


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
                if new != old:
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
    click, the first just moving the cursor there."""

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


class SessionStats(Widget):
    """Under the items: a bordered panel of the context, session and weekly gauges and the
    cache and compaction rows, then charts of the last two hours' context assembly and
    output; with no session in context, the running sessions' totals. The panel and the
    charts' columns are rebuilt on the 1-second refresh; the shimmer, on the animation tick,
    only recolours the columns. What it draws is stats.layout's and stats.render's."""

    DEFAULT_CSS = "SessionStats { height: 1fr; background: #000000; border-top: solid #7b61ff; }"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.view, self.name_, self.now = None, "", 0.0
        self.note: str | None = None   # what shows with nothing to show
        self.usage = stats.AccountUsage()
        self.rows: list[Text] = []
        self.charts: list[stats.Chart] = []
        self.frame = 0

    def show(self, view, name: str, now: float, note: str | None = None) -> None:
        self.view, self.name_, self.now, self.note = view, name, now, note
        self.rebuild()

    def rebuild(self) -> None:
        self.rows, self.charts = stats.layout(self.view, self.name_, self.note, self.usage, self.now,
                                              self.size.width, self.size.height)
        self.refresh()

    def on_resize(self) -> None:
        self.rebuild()

    def shimmer(self, frame: int) -> None:
        self.frame = frame
        if self.charts and self.display:
            self.refresh()

    def render(self) -> Text:
        return stats.render(self.rows, self.charts, self.frame)


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
    Allow, Always and Deny on an open permission item, Interrupt, Compact and Shell (the
    real Claude Code, in a tab), and a line saying what it's doing now."""
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
                                    ("Send all", "send-all", "warning"), ("Allow", "allow", "success"),
                                    ("Always", "always", "primary"), ("Deny", "deny", "error"),
                                    ("Interrupt", "interrupt", "error"), ("Compact", "compact", "default"),
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
            yield Input(value=os.getcwd(), placeholder="working directory", id="cwd")
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
        self.query_one("#adopt-name", Input).value = c.name or c.title[:40]
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
    "show_tab('inbox')": "Inbox tab",
    "show_tab('sessions')": "Sessions tab",
    "toggle_finished": "Show or hide finished items",
    "close_question": "Close the question (or every marked one); on a closed one, reopen it",
    "help": "This list",
    "quit": "Quit the wheelhouse (sessions carry on without it)",
    "toggle_mark": "Mark or unmark the highlighted row",
    "extend(-1)": "Extend the marks up",
    "extend(1)": "Extend the marks down",
    "select_all": "Select all of it, to copy",
    "leave": "Back to the inbox",
}
# the send bar's buttons and the Sessions tab's, by id
BUTTONS = {
    "mode": "Mode: the session's send mode, Queued or Immediate (Ctrl+T)",
    "send": "Send (n): send this session's queued answers as one message (Ctrl+S)",
    "send-all": "Send all (n): send every session's queue",
    "allow": "Allow: let the tool call a permission item asks about run",
    "always": "Always: allow it, and keep the rule Claude Code suggests",
    "deny": "Deny: refuse it (or type a message on the item: denied, with what to do instead)",
    "interrupt": "Interrupt: stop the session's current turn, like Esc in Claude Code",
    "compact": "Compact: the session says what to keep, then is compacted with that",
    "shell": "Shell: open the session in a real Claude Code tab; it comes back when you /exit",
    "new": "New session",
    "adopt-open": "Adopt: take on a Claude Code session started outside the wheelhouse",
    "restore": "Restore: bring back a dead session where it left off",
    "restore-all": "Restore all: every dead session that isn't parked",
    "park": "Park / unpark: drop a session off the inbox, or bring it back",
    "end": "End: the session does its own end steps, then its wheelhouse data is deleted",
}
SEND_RULES = (
    "**Queued** (the default): Ctrl+Enter holds each answer until Ctrl+S (or Send) sends the "
    "session's queue as one message, so related answers arrive together; ✉ counts what's queued. "
    "**Immediate**: Ctrl+Enter sends at once. Ctrl+T switches the session's mode; Ctrl+R takes a "
    "queued answer back to edit.")
MOUSE = ("Click selects a row. Ctrl+click marks rows and Shift+click a range (Windows Terminal may "
         "keep Shift+click for itself: Space and Shift+Up/Down do the same), then X closes them together.")


def keys_help() -> str:
    """The ? overlay: every key, from the bindings themselves, and every button."""
    sections = [("Everywhere", WheelhouseApp.BINDINGS), ("The item list", ItemList.BINDINGS),
                ("An answer box", Compose.BINDINGS), ("The conversation pane", Transcript.BINDINGS),
                ("An item opened full screen", ThreadView.BINDINGS)]
    out = ["# Keys"]
    for title, bindings in sections:
        out.append(f"## {title}")
        actions: dict[str, list[str]] = {}
        for b in bindings:
            actions.setdefault(b.action, []).append(key_name(b.key))
        out += [f"- **{' or '.join(keys)}**: {DESCRIBE[action]}" for action, keys in actions.items()]
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
    TabbedContent {{ height: 1fr; }}   /* leaves room for the send bar and footer: no screen scroll */
    .panel {{ background: #000000; color: {MATRIX}; border: round #7b61ff; }}
    .panel:focus-within {{ border: round #ff2a6d; }}
    DataTable {{ background: #000000; color: {MATRIX}; }}
    DataTable > .datatable--header {{ background: #12122a; color: #05d9e8; text-style: bold; }}
    DataTable > .datatable--cursor {{ background: #003b0f; color: #ffffff; }}
    #sessions-pane {{ width: 34; }}
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
    #synopsis {{ height: 7; background: #000000; color: {MATRIX}; border: round #05d9e8; }}
    Markdown {{ background: #000000; color: {MATRIX}; }}
    #session-buttons {{ height: 3; }}
    #dialog {{ width: 80; height: auto; padding: 1 2; background: #000000; color: {MATRIX};
               border: thick #ff2a6d; }}
    .dialog-title {{ color: #ffd300; text-style: bold; }}
    #dialog TextArea {{ height: 8; }}
    .buttons {{ height: 3; }}
    #dialog.wide {{ width: 120; }}
    #adopt-list {{ height: 16; }}
    #adopt-hint {{ color: #ffd300; }}
    NewSession, Confirm, Choice, AdoptSession, KeysHelp {{ align: center middle; }}
    TutorialOffer {{ align: center top; }}
    #offer {{ width: 100%; height: 1; background: #12122a; color: #e8e8e8; padding: 0 1; }}
    #keys-dialog {{ width: 100; max-width: 100%; height: 90%; background: #000000; color: {MATRIX};
                    border: thick #ff2a6d; padding: 0 1; }}
    #checklist {{ height: auto; display: none; background: #000000; color: #e8e8e8;
                  border-bottom: solid #7b61ff; padding: 0 1; }}
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
        Binding("1", "show_tab('inbox')", "Inbox"),
        Binding("2", "show_tab('sessions')", "Sessions"),
        Binding("f", "toggle_finished", "Finished", key_display="F"),
        Binding("x", "close_question", "Close", key_display="X"),
        Binding("question_mark", "help", "Keys", key_display="?"),
        Binding("q", "quit", "Quit", key_display="Q"),
    ]

    def __init__(self, store: Store | None = None):
        super().__init__()
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
        self.usage: dict[str, stats.UsageFollower] = {}
        # unsent text typed for each target, (session id, ref or None), kept in memory only
        self.unsent: dict[tuple, str] = {}
        self.box_target: tuple | None = None
        self.statuses: dict[str, str] = {}
        self.sessions = []
        # the tutorial steps only the screen sees (a question opened, the conversation
        # followed), by tutorial session

    def compose(self) -> ComposeResult:
        yield Static(id="title")
        with TabbedContent(initial="inbox"):
            with TabPane("Inbox", id="inbox"):
                with Horizontal():
                    with Vertical(id="sessions-pane", classes="panel"):
                        yield SessionList(id="session-list", cursor_type="row")
                    with Vertical(id="items-pane", classes="panel"):
                        yield ItemList(id="items", cursor_type="row")
                        yield SessionStats(id="stats")
                    with Vertical(id="detail-pane", classes="panel"):
                        yield Static(id="checklist")
                        with VerticalScroll(id="detail-scroll"):
                            yield Transcript("Select an item, or a session to follow its conversation.",
                                         id="detail")
                        yield Compose(id="answer")
                        yield Hint(classes="answer-hint")
            with TabPane("Sessions", id="sessions"):
                with Vertical(classes="panel"):
                    yield DataTable(id="session-table", cursor_type="row")
                    yield Markdown(id="synopsis")
                    with Horizontal(id="session-buttons"):
                        yield Button("New session", id="new", variant="success")
                        yield Button("Adopt", id="adopt-open")
                        yield Button("Restore", id="restore")
                        yield Button("Restore all", id="restore-all", variant="warning")
                        yield Button("Park / unpark", id="park")
                        yield Button("End", id="end", variant="error")
        yield SendBar()
        yield Footer()

    def on_mount(self) -> None:
        # held, not queried: timers fire while a dialog is on top and during shutdown
        self.title_bar = self.query_one("#title", Static)
        self.items_table = self.query_one("#items", ItemList)
        self.stats = self.query_one("#stats", SessionStats)
        self.detail = self.query_one("#detail", Transcript)
        self.detail_scroll = self.query_one("#detail-scroll", VerticalScroll)
        self.answer = self.query_one("#answer", Compose)
        self.tabs = self.query_one(TabbedContent)
        self.synopsis = self.query_one("#synopsis", Markdown)
        self.tables = {"#session-list": self.query_one("#session-list", DataTable),
                       "#session-table": self.query_one("#session-table", DataTable)}
        self.eye_cols = {
            "#session-list": self.tables["#session-list"].add_columns("", "session", "?", "D", "✉", "")[-1],
            "#session-table": self.tables["#session-table"].add_columns(
                "status", "name", "ticket", "dir", "open Q", "running", "unseen D", "queued", "")[-1],
        }
        self.items_table.add_columns("session", "ref", "status", "title")
        self.checklist = self.query_one("#checklist", Static)
        self.set_interval(0.1, self.animate)
        self.set_interval(1.0, self.refresh_data)
        self.refresh_data()
        if tutorial.should_offer(self.store):
            self.push_screen(TutorialOffer(), self.offer_answered)

    # periodic work

    def animate(self) -> None:
        self.frame += 1
        self.title_bar.update(shimmer(self.frame))
        if self.frame % 2 == 0:
            self.sweep_eyes()
            # it rests while you type. No screen at all while the app shuts down, when
            # asking what has focus would raise
            if self.screen_stack and not isinstance(self.focused, Compose):
                self.stats.shimmer(self.frame // 2)   # 5 frames a second, as in the dashboard

    def refresh_data(self) -> None:
        self.waking = self.wake.tick()
        self.sessions = self.store.sessions()
        # liveness only: the wheelhouse never deletes or parks anything by itself
        self.statuses = {s["id"]: liveness.status(s, waking=self.waking) for s in self.sessions}
        self.paint_sessions()
        self.paint_items()
        self.paint_stats()
        self.paint_synopsis()
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
        """Running older wheelhouse code, which would deliver a queued answer at once."""
        return self.running(s["id"]) and needs_relaunch(s)

    @staticmethod
    def pending(s) -> str | None:
        """The request (end or park) a session has been asked to act on, if any."""
        return next((what for what in PENDING if s[f"{what}_requested_at"]), None)

    def shown_status(self, s) -> str:
        st = self.statuses.get(s["id"], "dead")
        what = self.pending(s)
        return PENDING[what] if what and st in RUNNING else st

    def busy(self, s) -> bool:
        working = bool(s["running"]) or (runner(s) == "sdk" and not (s["activity"] or "idle").startswith(RESTING))
        return working and self.statuses.get(s["id"]) in ("live", "stalled")

    def sweep_eyes(self) -> None:
        """Move the Cylon eye on busy rows without rebuilding the tables."""
        for table_id, col in self.eye_cols.items():
            table = self.tables[table_id]
            for s in self.sessions:
                if self.busy(s) and s["id"] in table.rows:
                    table.update_cell(s["id"], col, cylon(self.frame))

    def paint_sessions(self) -> None:
        for table_id, compact in (("#session-list", True), ("#session-table", False)):
            table = self.tables[table_id]
            keep = table.cursor_row
            rows = []
            for s in self.sessions:
                st = self.shown_status(s)
                if compact and s["parked"]:
                    continue
                busy = cylon(self.frame) if self.busy(s) else Text("")
                dot = Text("●", style=STATUS_STYLE[st])
                label = Text(st, style=STATUS_STYLE[st])
                if s["parked"]:
                    label.append(" · parked", style="#777777")
                if self.stale(s):
                    label.append(" · needs relaunch", style="bold #ff2a6d")
                name = s["name"] or os.path.basename(s["cwd"]) or short(s["id"])
                if compact and self.stale(s):
                    name = Text.assemble(name, (" ⟳", "bold #ff2a6d"))
                queued = Text(f"✉ {s['drafts']}", style="bold #05d9e8") if s["drafts"] else ""
                # decisions inform, they don't block: counted, but not blinking like questions
                unseen = Text(str(s["unseen_decisions"]), style=DECISION) if s["unseen_decisions"] else ""
                if compact:
                    q = Text(str(s["open_questions"]), style="bold #ffd300 blink") if s["open_questions"] else ""
                    rows.append((s["id"], (dot, name, q, unseen, queued, busy)))
                else:
                    rows.append((s["id"], (label, name, s["ticket"], s["cwd"], str(s["open_questions"]),
                                           str(s["running"]), unseen, queued, busy)))
            if fill(table, rows) and table.row_count:
                table.move_cursor(row=min(keep, table.row_count - 1), animate=False)

    def paint_items(self) -> None:
        table = self.items_table
        keep = table.cursor_row
        rows_out = []
        names = {s["id"]: s["name"] or short(s["id"]) for s in self.sessions}
        items = self.store.items(self.filter_sid)
        # a decision turns seen as it's viewed: keep the one being viewed in place until the
        # person moves on, rather than pull it from under them
        shown = [it for it in items if it["status"] not in CLOSED
                 or (it["kind"] == "decision" and self.selected == (it["session_id"], it["ref"]))]
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
            follower.blocks_key, follower.blocks = key, transcript.blocks(*key[:2], recs, queued)
        return follower.blocks

    def stats_sids(self) -> list[str]:
        """The sessions the stats pane shows: the one in context, else every running one."""
        sid = self.filter_sid or (self.selected[0] if self.selected else None)
        if any(s["id"] == sid for s in self.sessions):
            return [sid]
        return [s["id"] for s in self.sessions if self.running(s["id"])]

    def paint_stats(self) -> None:
        """The stats pane: the session in context's, else every running session's added up.
        Transcripts are read on worker threads, never here: a first read of a big one takes
        a while, and even a steady one stats every subagent file."""
        now = time.time()
        if self.stats.usage.due(now):
            self.run_worker(self.stats.usage.fetch, thread=True, group="usage", exit_on_error=False)
        present = {s["id"] for s in self.sessions}
        for sid in [sid for sid in self.usage if sid not in present]:   # ended: its follower goes
            del self.usage[sid]
        for sid in self.stats_sids():
            follower = self.usage.setdefault(sid, stats.UsageFollower(sid))
            if not follower.reading:
                follower.reading = True
                self.run_worker(functools.partial(self.read_usage, follower, now), thread=True, group="stats",
                                exit_on_error=False)
        self.show_stats(now)

    def read_usage(self, follower: stats.UsageFollower, now: float) -> None:
        """On a worker thread: one read of a session's transcripts, then a repaint if it
        brought anything (or was the first)."""
        first = not follower.ready
        try:
            changed = follower.read(now)
            follower.error = None
        except Exception as e:   # a transcript gone between stat and open, say: the pane says so, the app carries on
            changed, follower.error = True, f"couldn't read the transcript: {e}"[:120]
        finally:
            follower.reading = False
        if changed or first:
            try:
                self.call_from_thread(self.show_stats)
            except RuntimeError:   # the app is closing
                pass

    def show_stats(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        names = {s["id"]: s["name"] or short(s["id"]) for s in self.sessions}
        sids = [sid for sid in self.stats_sids() if sid in self.usage]
        sid = self.filter_sid or (self.selected[0] if self.selected else None)
        if sid in names:
            follower = self.usage.get(sid)
            if follower and follower.ready:
                self.stats.show(follower.snap, names[sid], now, follower.error)
            else:
                self.stats.show(None, names[sid], now, (follower and follower.error)
                                or f"reading {names[sid]}'s transcript…")
            return
        snaps = {sid: self.usage[sid].snap for sid in sids if self.usage[sid].ready}
        waiting = len(snaps) < len(sids)
        self.stats.show(stats.combine(snaps, names, now) if snaps else None, "", now,
                        "reading transcripts…" if waiting else None)

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
        elif self.stale(s):   # its old monitor would deliver a draft at once anyway
            bar.show("mode", "Sends now: needs relaunch", True)
        else:
            bar.show("mode", f"Mode: {mode(s).capitalize()}", False)
        sends = "send" if s is not None and (mode(s) == "immediate" or self.stale(s)) else "queue"
        for h in self.screen.query(Hint):
            h.set_base(hint(sends))
        n = s["drafts"] if s else 0
        bar.show("send", f"Send ({n})", not n)
        bar.show("send-all", f"Send all ({total})", not total)
        hosted = s is not None and runner(s) == "sdk" and self.running(s["id"]) and not s["shell"]
        for button in ("interrupt", "compact", "shell"):
            bar.show(button, button.capitalize(), False, hosted)
        item = self.bar_item(self.screen)
        item = item and self.store.item(*item)
        asking = bool(item) and item["kind"] == "permission" and item["status"] == "open"
        for button in ("allow", "always", "deny"):
            bar.show(button, button.capitalize(), False, asking)
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
        """The person opened something of the tutorial's: a question, its decision, or its
        conversation. Called only from what the person does, never from automatic selection."""
        s = next((s for s in self.sessions if s["id"] == sid), None)
        if not tutorial.is_tutorial(self.store, s):
            return
        if step is None:
            item = ref and self.store.item(sid, ref)
            step = "follow" if ref is None else {"question": "open", "decision": "decision"}.get(item and item["kind"])
        if step:
            tutorial.see(self.store, sid, step)
            self.paint_checklist()

    def paint_synopsis(self) -> None:
        sid = self.current_session()
        s = sid and self.store.session(sid)
        text = (f"**{s['name'] or os.path.basename(s['cwd']) or short(sid)}**\n\n{s['synopsis'] or '_No synopsis yet._'}"
                if s else "_Select a session to see its synopsis._")
        if text != getattr(self, "_synopsis_text", None):
            self._synopsis_text = text
            self.synopsis.update(text)

    # selection

    @on(DataTable.RowSelected, "#session-list")
    def pick_session(self, event: DataTable.RowSelected) -> None:
        self.follow(event.row_key.value)
        self.offer_relaunch(event.row_key.value)

    @on(DataTable.RowSelected, "#session-table")
    def pick_session_in_table(self, event: DataTable.RowSelected) -> None:
        self.offer_relaunch(event.row_key.value)

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
            self.paint_stats()
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

    @on(DataTable.RowHighlighted, "#session-table")
    def pick_session_row(self) -> None:
        self.paint_synopsis()
        self.paint_sendbar()

    @on(TabbedContent.TabActivated)
    def tab_changed(self) -> None:
        self.paint_sendbar()

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

    def action_toggle_finished(self) -> None:
        if not isinstance(self.focused, (TextArea, Input)):
            self.show_finished = not self.show_finished
            self.notify("showing finished items" if self.show_finished else "hiding finished items")
            self.paint_items()

    @session_action
    def action_close_question(self) -> None:
        """Closing a question is the person's call: x closes the highlighted (or open) one,
        and on a closed one (shown with f) reopens it as answered."""
        if isinstance(self.focused, (TextArea, Input)):
            return
        if self.items_table.marked and self.screen is self.screen_stack[0]:
            self.close_marked()
            return
        target = self.composing()[1]
        item = target and target[1] and self.store.item(*target)
        if not item:
            return
        if item["kind"] == "decision":
            self.notify(f"{item['ref']} is a decision: it's marked seen as you view it", severity="warning")
            return
        if item["kind"] != "question":
            self.notify(f"{item['ref']} is a {item['kind']}: its status is the session's to set",
                        severity="warning")
            return
        reopen = item["status"] == "closed"
        self.store.update_item(*target, status="answered" if reopen else "closed")
        self.notify(f"reopened {item['ref']} as answered" if reopen else
                    f"closed {item['ref']}" + ("" if self.show_finished else ": F shows finished items"))
        self.refresh_data()

    def close_marked(self) -> None:
        """X on a multi-selection: closes its questions, or reopens them as answered if
        they're all closed. Tasks, decisions and subagents in it are left alone."""
        questions = [it for key in self.items_table.keys() if key in self.items_table.marked
                     if (it := self.store.item(*key.split("|"))) and it["kind"] == "question"]
        if not questions:
            self.notify("no questions among the marked rows", severity="warning")
            return
        reopen = all(it["status"] == "closed" for it in questions)
        done = []
        for it in questions:
            if reopen or it["status"] != "closed":
                try:
                    self.store.update_item(it["session_id"], it["ref"], status="answered" if reopen else "closed")
                except SessionGone:   # ended meanwhile: the rest still go
                    continue
                done.append(it["ref"])
        self.items_table.set_marks(set())
        self.notify(("reopened " if reopen else "closed ") + ", ".join(done)
                    + ("" if reopen or self.show_finished else ": F shows finished items"))
        self.refresh_data()

    def action_show_tab(self, tab: str) -> None:
        if not isinstance(self.focused, (TextArea, Input)):
            self.tabs.active = tab

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
        if mode(s) == "immediate":
            self.store.send(target[0], text, target[1])
            self.notify(f"sent to {aimed(target)}")
        elif self.stale(s):   # its old monitor would deliver a draft at once anyway
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
        """The session in context on a screen: the thread's; the Sessions tab's highlighted
        row; else the inbox's filter or the selected item's session."""
        if isinstance(screen, ThreadView):
            return screen.sid
        if self.tabs.active == "sessions":
            return self.current_session()
        return self.filter_sid or (self.selected[0] if self.selected else None)

    def bar_item(self, screen) -> tuple[str, str] | None:
        """The item in context on a screen: the thread's, else the inbox's selected item."""
        if isinstance(screen, ThreadView):
            return (screen.sid, screen.ref)
        if self.tabs.active == "inbox" and self.selected and self.selected[1]:
            return self.selected
        return None

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
        """Answer a permission item. A message typed on it instead denies with that text."""
        target = self.bar_item(event.button.screen)
        if not target:
            return
        try:
            self.store.answer_permission(*target, event.button.id)
        except (KeyError, SessionGone) as e:   # answered meanwhile, or the session went
            self.notify(str(e.args[0] if e.args else e), severity="warning")
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

    # sessions page

    def current_session(self) -> str | None:
        table = self.tables["#session-table"]
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
