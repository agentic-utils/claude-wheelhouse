"""The wheelhouse app: a Textual app. It only reads and writes the database."""

import functools
import math
import os
import sqlite3
import time

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
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

from . import adopt, launch, liveness, transcript
from .store import CLOSED, SessionGone, Store, needs_relaunch

MATRIX = "#00ff41"
SHIMMER = ["#ff2a6d", "#ff7b00", "#ffd300", "#05d9e8", "#7b61ff", "#d300c5"]
STATUS_STYLE = {"live": "bold #00ff41", "stalled": "bold #ffd300", "starting": "#05d9e8",
                "dead": "bold #ff2a6d", "ending": "bold #d300c5", "parking": "bold #d300c5"}
TITLE = " ▓▒░ CLAUDE·WHEELHOUSE ░▒▓ "
RUNNING = ("live", "stalled", "starting")
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


def thread_markdown(store: Store, sid: str, ref: str, session_name: str = "") -> str:
    """An item's detail and conversation, oldest first: the person's messages (queued ones
    marked), the session's replies, and its progress notes as quieter quotes."""
    item = store.item(sid, ref)
    if item is None:
        return "_gone_"
    msgs = store.thread(sid, ref)
    queued = any(m["draft"] for m in msgs)
    where = f"{session_name} · " if session_name else ""
    lines = [f"## {ref} · {item['title']}", f"{where}`{item['kind']}` · **{item['status']}**"
             f"{' · answer queued' if queued else ''} · updated {item['updated_at']}", "",
             item["body"] or "_no detail_", ""]
    for m in msgs:
        if m["author"] == "person":
            lines += [f"**you{' · queued' if m['draft'] else ''}** · {m['created_at']}", "", m["body"], ""]
        elif m["kind"] == "reply":
            lines += [f"**claude** · {m['created_at']}", "", m["body"], ""]
        else:   # a progress note (rows from before replies existed read as notes)
            lines += [f"> _note · {m['created_at']}_", ">"] + [f"> {line}" for line in m["body"].splitlines()] + [""]
    return "\n".join(lines)


class SessionList(DataTable):
    """The inbox's sessions. One click selects a row: a plain table selects only on a second
    click, the first just moving the cursor there."""

    async def _on_click(self, event) -> None:
        before = self.cursor_coordinate
        await super()._on_click(event)
        if self.cursor_coordinate != before:   # the table already selected it if unmoved
            self._post_selected_message()


class Compose(TextArea):
    """An answer box. Ctrl+X sends what's typed now instead of cutting it."""
    BINDINGS = [Binding("ctrl+x", "app.send_now", "Send now")]


class ThreadView(Screen):
    """One item full screen: its detail, the whole conversation and a compose box. The app's
    refresh tick repaints it, so a reply shows up while it's open; typed text is untouched."""
    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

    def __init__(self, sid: str, ref: str):
        super().__init__()
        self.sid, self.ref = sid, ref
        self.text = None

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="thread-scroll", classes="panel"):
            yield Markdown(id="thread")
        yield Compose(id="thread-answer")
        yield Label(HINT, classes="answer-hint")
        yield Footer()

    def on_mount(self) -> None:
        self.box = self.query_one(Compose)
        self.paint()
        self.box.focus()

    def paint(self) -> None:
        s = self.app.store.session(self.sid)
        text = thread_markdown(self.app.store, self.sid, self.ref, s and (s["name"] or short(self.sid)))
        if text != self.text:
            self.text = text
            self.query_one("#thread", Markdown).update(text)
            self.query_one("#thread-scroll").scroll_end(animate=False)


HINT = "Ctrl+S queues · Ctrl+X sends now · Ctrl+R takes a queued answer back to edit or drop"


class NewSession(ModalScreen):
    """Name and ticket are optional; the brief becomes the first prompt."""

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("NEW SESSION", classes="dialog-title")
            yield Input(value=os.getcwd(), placeholder="working directory", id="cwd")
            yield Input(placeholder="name (optional)", id="name")
            yield Input(placeholder="ticket: #42, owner/repo#42 or ABC-123 (optional)", id="ticket")
            yield TextArea(id="brief")
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
                      "brief": self.query_one("#brief", TextArea).text.strip()})

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
                yield Button("[Y]es", variant="error", id="yes")
                yield Button("[N]o", id="no")

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
                yield Button("[C]ancel request", variant="success", id="cancel")
                yield Button("[F]orce", variant="error", id="force")
                yield Button("Leave it [Esc]", id="leave")

    def on_key(self, event) -> None:
        keys = {"c": "cancel", "f": "force", "escape": "leave"}
        if event.key in keys:
            event.stop()
            self.dismiss(keys[event.key])

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id)


class WheelhouseApp(App):
    CSS = f"""
    Screen {{ background: #0a0a12; }}
    #title {{ height: 1; background: #12122a; content-align: center middle; }}
    .panel {{ background: #000000; color: {MATRIX}; border: round #7b61ff; }}
    .panel:focus-within {{ border: round #ff2a6d; }}
    DataTable {{ background: #000000; color: {MATRIX}; }}
    DataTable > .datatable--header {{ background: #12122a; color: #05d9e8; text-style: bold; }}
    DataTable > .datatable--cursor {{ background: #003b0f; color: #ffffff; }}
    #sessions-pane {{ width: 34; }}
    #items-pane {{ width: 1fr; }}
    #detail-pane {{ width: 2fr; }}
    #detail-scroll {{ height: 1fr; }}
    #detail {{ background: #000000; color: {MATRIX}; }}
    #answer, #thread-answer {{ height: 8; background: #000000; color: {MATRIX}; border: round #05d9e8; }}
    .answer-hint {{ color: #777777; height: 1; }}
    #outbox {{ height: 1; color: #05d9e8; background: #12122a; }}
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
    NewSession, Confirm, Choice, AdoptSession {{ align: center middle; }}
    """

    BINDINGS = [
        Binding("ctrl+s", "queue", "Queue"),
        Binding("ctrl+enter", "queue", "Queue", show=False),
        Binding("ctrl+r", "recall", "Edit queued", show=False),
        Binding("s", "dispatch", "Send session"),
        Binding("S", "dispatch_all", "Send all"),
        Binding("n", "new_session", "New session"),
        Binding("a", "adopt", "Adopt"),
        Binding("escape", "clear_filter", "All sessions"),
        Binding("1", "show_tab('inbox')", "Inbox"),
        Binding("i", "show_tab('inbox')", "Inbox", show=False),
        Binding("2", "show_tab('sessions')", "Sessions"),
        Binding("f", "toggle_finished", "Finished"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, store: Store | None = None):
        super().__init__()
        self.store = store or Store()
        self.wake = liveness.WakeDetector()
        self.waking = False
        self.frame = 0
        self.filter_sid: str | None = None
        self.show_finished = False   # done, dropped, closed and failed items, after the rest
        self.selected: tuple[str, str] | None = None   # (session id, item ref)
        # the session whose conversation the right pane follows, instead of the selected item
        self.viewing: str | None = None
        self.followers: dict[str, transcript.Follower] = {}
        self.statuses: dict[str, str] = {}
        self.sessions = []

    def compose(self) -> ComposeResult:
        yield Static(id="title")
        with TabbedContent(initial="inbox"):
            with TabPane("Inbox", id="inbox"):
                with Horizontal():
                    with Vertical(id="sessions-pane", classes="panel"):
                        yield SessionList(id="session-list", cursor_type="row")
                    with Vertical(id="items-pane", classes="panel"):
                        yield DataTable(id="items", cursor_type="row")
                    with Vertical(id="detail-pane", classes="panel"):
                        with VerticalScroll(id="detail-scroll"):
                            yield Markdown("Select an item, or Enter on a session to follow its conversation.",
                                           id="detail")
                        yield Compose(id="answer")
                        yield Label(HINT, classes="answer-hint")
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
        yield Static(id="outbox")
        yield Footer()

    def on_mount(self) -> None:
        # held, not queried: timers fire while a dialog is on top and during shutdown
        self.title_bar = self.query_one("#title", Static)
        self.items_table = self.query_one("#items", DataTable)
        self.detail = self.query_one("#detail", Markdown)
        self.detail_scroll = self.query_one("#detail-scroll", VerticalScroll)
        self.answer = self.query_one("#answer", Compose)
        self.outbox = self.query_one("#outbox", Static)
        self.synopsis = self.query_one("#synopsis", Markdown)
        self.tables = {"#session-list": self.query_one("#session-list", DataTable),
                       "#session-table": self.query_one("#session-table", DataTable)}
        self.eye_cols = {
            "#session-list": self.tables["#session-list"].add_columns("", "session", "?", "✉", "")[-1],
            "#session-table": self.tables["#session-table"].add_columns(
                "status", "name", "ticket", "dir", "open Q", "running", "queued", "")[-1],
        }
        self.items_table.add_columns("session", "ref", "status", "title")
        self.set_interval(0.1, self.animate)
        self.set_interval(1.0, self.refresh_data)
        self.refresh_data()

    # periodic work

    def animate(self) -> None:
        self.frame += 1
        self.title_bar.update(shimmer(self.frame))
        if self.frame % 2 == 0:
            self.sweep_eyes()

    def refresh_data(self) -> None:
        self.waking = self.wake.tick()
        self.sessions = self.store.sessions()
        # liveness only: the wheelhouse never deletes or parks anything by itself
        self.statuses = {s["id"]: liveness.status(s, waking=self.waking) for s in self.sessions}
        self.paint_sessions()
        self.paint_items()
        self.paint_outbox()
        self.paint_synopsis()
        if isinstance(self.screen, ThreadView):
            self.screen.paint()   # an action here (queue, take back) shows at once

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
        return bool(s["running"]) and self.statuses.get(s["id"]) in ("live", "stalled")

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
            table.clear()
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
                if compact:
                    q = Text(str(s["open_questions"]), style="bold #ffd300 blink") if s["open_questions"] else ""
                    table.add_row(dot, name, q, queued, busy, key=s["id"])
                else:
                    table.add_row(label, name, s["ticket"], s["cwd"],
                                  str(s["open_questions"]), str(s["running"]), queued, busy, key=s["id"])
            if table.row_count:
                table.move_cursor(row=min(keep, table.row_count - 1), animate=False)

    def paint_items(self) -> None:
        table = self.items_table
        keep = table.cursor_row
        table.clear()
        names = {s["id"]: s["name"] or short(s["id"]) for s in self.sessions}
        items = self.store.items(self.filter_sid)
        rows = item_rows([it for it in items if it["status"] not in CLOSED], names)
        if self.show_finished:
            rows += item_rows([it for it in items if it["status"] in CLOSED], names)
        queued = {(m["session_id"], m["item_ref"]) for m in self.store.drafts()}
        for it, nested in rows:
            # an open question with an answer waiting to be sent shows as queued; it's stored as open
            status = "queued" if it["status"] == "open" and (it["session_id"], it["ref"]) in queued else it["status"]
            style = "dim" if status in CLOSED else "bold #05d9e8" if status == "queued" \
                else "bold #ffd300" if status == "open" \
                else "bold #ff2a6d" if status in ("blocked", "waiting") else MATRIX
            name = names.get(it["session_id"], "")[:14]
            if nested is None:
                cells = (name, it["ref"], Text(status, style=style), it["title"])
            else:   # a subagent, tucked under its session's name
                cells = (name if nested == 0 else "", Text(f"└ {it['ref']}", style="dim"),
                         Text(status, style=style), Text(it["title"], style="dim"))
            table.add_row(*cells, key=f"{it['session_id']}|{it['ref']}")
        if table.row_count:
            table.move_cursor(row=min(keep, table.row_count - 1), animate=False)
        self.paint_detail()

    def paint_detail(self) -> None:
        if self.viewing:
            text = self.conversation(self.viewing)
        elif self.selected:
            text = thread_markdown(self.store, *self.selected)
        else:
            return
        if text != getattr(self, "_detail_text", None):
            following = self.viewing and self.detail_scroll.scroll_y >= self.detail_scroll.max_scroll_y - 1
            self._detail_text = text
            self.detail.update(text)
            if following:   # stay at the newest turn, unless the person has scrolled up to read
                self.call_after_refresh(self.detail_scroll.scroll_end, animate=False)

    def conversation(self, sid: str) -> str:
        s = self.store.session(sid)
        if s is None:
            self.viewing = None
            return "_gone_"
        follower = self.followers.setdefault(sid, transcript.Follower(sid))
        queued = [m["body"] for m in self.store.drafts(sid) if m["item_ref"] is None]
        return transcript.markdown(s["name"] or short(sid), launch.tab_title(s), follower.read(), queued)

    def paint_outbox(self) -> None:
        n = sum(s["drafts"] for s in self.sessions)
        self.outbox.update(f" ✉ {n} queued: s sends the selected session's, S sends all" if n else "")

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

    def follow(self, sid: str) -> None:
        """Filter the items to the session and follow its conversation on the right."""
        self.filter_sid = self.viewing = sid
        self.paint_items()
        self.call_after_refresh(self.detail_scroll.scroll_end, animate=False)

    @on(DataTable.RowHighlighted, "#items")
    def pick_item(self, event: DataTable.RowHighlighted) -> None:
        if not event.row_key.value:
            return
        sid, ref = event.row_key.value.split("|")
        if self.selected != (sid, ref):
            self.selected = (sid, ref)
            if self.viewing is None:   # repainting the table moves its cursor too
                self.paint_detail()

    def on_descendant_focus(self, event) -> None:
        """Going to the item list leaves a session's conversation for the highlighted item."""
        if event.widget is self.items_table and self.viewing:
            self.viewing = None
            self.paint_detail()

    @on(DataTable.RowSelected, "#items")
    def open_thread(self, event: DataTable.RowSelected) -> None:
        sid, ref = event.row_key.value.split("|")
        self.push_screen(ThreadView(sid, ref))

    @on(DataTable.RowHighlighted, "#session-table")
    def pick_session_row(self) -> None:
        self.paint_synopsis()

    def action_clear_filter(self) -> None:
        self.filter_sid = self.viewing = None
        self.paint_items()

    def action_toggle_finished(self) -> None:
        if not isinstance(self.focused, (TextArea, Input)):
            self.show_finished = not self.show_finished
            self.notify("showing finished items" if self.show_finished else "hiding finished items")
            self.paint_items()

    def action_show_tab(self, tab: str) -> None:
        if not isinstance(self.focused, (TextArea, Input)):
            self.query_one(TabbedContent).active = tab

    def composing(self):
        """The compose box in use and the item it answers: the thread view's, or the inbox's."""
        if isinstance(self.screen, ThreadView):
            return self.screen.box, (self.screen.sid, self.screen.ref)
        if self.screen is self.screen_stack[0]:
            # following a conversation, the box sends the session a general message
            return self.answer, (self.viewing, None) if self.viewing else self.selected
        return None, None   # a dialog is open: its keys are its own

    def typed(self):
        box, target = self.composing()
        if box is None:
            return None, None, None
        text = box.text.strip()
        if not text or not target:
            self.notify("pick an item and type something first", severity="warning")
            return None, None, None
        return box, target, text

    @session_action
    def action_queue(self) -> None:
        box, target, text = self.typed()
        if box:
            s = self.store.session(target[0])
            if s and self.stale(s):   # its old monitor would deliver a draft at once anyway
                self.store.send(target[0], text, target[1])
                self.notify(f"sent to {aimed(target)} now: that session runs older wheelhouse code, "
                            "so it can't queue until it's relaunched", severity="warning")
            else:
                self.store.queue(target[0], text, target[1])
                self.notify(f"queued for {aimed(target)}: s sends it")
            box.text = ""
            self.refresh_data()

    @session_action
    def action_send_now(self) -> None:
        box, target, text = self.typed()
        if box:
            self.store.send(target[0], text, target[1])
            box.text = ""
            self.notify(f"sent to {aimed(target)}")
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
        self.notify("taken back: queue it again, or clear it to drop it")
        self.refresh_data()

    def dispatch_target(self) -> str | None:
        """The selected session: the thread's, the Sessions tab's row, else the inbox filter or
        the selected item's session."""
        if isinstance(self.screen, ThreadView):
            return self.screen.sid
        if self.query_one(TabbedContent).active == "sessions":
            return self.current_session()
        return self.filter_sid or (self.selected[0] if self.selected else None)

    def sent_note(self, sid: str, n: int) -> str:
        s = self.store.session(sid)
        name = (s["name"] or short(sid)) if s else short(sid)
        late = "" if self.running(sid) else " (not running: delivered when it's restored)"
        return f"{n} to {name}{late}"

    @session_action
    def action_dispatch(self) -> None:
        if isinstance(self.focused, (TextArea, Input)) or self.composing()[0] is None:
            return
        sid = self.dispatch_target()
        if sid is None:
            self.notify("select a session first", severity="warning")
            return
        n = self.store.dispatch(sid)
        self.notify(f"sent {self.sent_note(sid, n)}" if n else "nothing queued for that session")
        self.refresh_data()

    @session_action
    def action_dispatch_all(self) -> None:
        if isinstance(self.focused, (TextArea, Input)) or self.composing()[0] is None:
            return
        sent = [(sid, n) for sid in dict.fromkeys(m["session_id"] for m in self.store.drafts())
                if (n := self.store.dispatch(sid))]
        self.notify("sent " + "; ".join(self.sent_note(sid, n) for sid, n in sent) if sent else "nothing queued")
        self.refresh_data()

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
        self.open_tab(sid)

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
        self.notify(f"adopting {form['name'] or short(form['candidate'].id)} in a new tab")
        self.refresh_data()

    def open_tab(self, sid: str) -> bool:
        try:
            launch.open_tab(self.store, sid)
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
        if self.open_tab(sid):
            self.store.set_parked(sid, False)

    @on(Button.Pressed, "#restore-all")
    def restore_all_pressed(self) -> None:
        dead = [s["id"] for s in self.sessions if self.statuses.get(s["id"]) == "dead" and not s["parked"]]
        if not dead:
            self.notify("nothing to restore")
            return

        def go(yes: bool) -> None:
            if yes:
                n = sum(self.open_tab(sid) for sid in dead)
                self.notify(f"restoring {n} session(s)")
        self.push_screen(Confirm(f"Restore {len(dead)} dead session(s) in new tabs?"), go)

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
