"""The design module: the Decisions pane, a table of every repo's decisions (ADRs) under the
inbox, plugged in through the module protocol (claude_wheelhouse.api) like any other module
(.plan/modules.md, "Design, a new module"). Its daemon side, the route the pane asks and the
doc roots the viewer reads through, scans (scan.py); the pane shows a decision in the Detail
pane (Context.show_doc) and full screen (Context.open_doc)."""

import importlib.metadata
import time
from pathlib import Path

from rich.text import Text
from textual.containers import Vertical
from textual.widgets import DataTable, Static

from claude_wheelhouse import api

from . import scan

EVERY = 10   # seconds between the pane's asks: the scan is cheap, and nothing polls in between
UP, DOWN, STAR = "▲", "▼", "*"   # a header's marker, as the inbox's
DEFAULT = "status*"   # PROPOSED and UNRESOLVED first, then by number, newest first (scan.ordered)
COLUMNS = (("number", "#"), ("status", "Status"), ("title", "Title"), ("repo", "Repo"))
LOOKS = {"PROPOSED": "#ffd300", "UNRESOLVED": "#ffd300", "ACCEPTED": "#00ff9f",
         "REJECTED": "#8c8c8c", "SUPERSEDED": "#8c8c8c", "DEPRECATED": "#8c8c8c"}
SCANNER = scan.Scanner()   # the daemon's (or a --local app's) files read, kept between asks


# the daemon side: where decisions are, and the route the pane asks

def dirs(ctx: api.Context) -> list[tuple[Path, str]]:
    """Each directory decisions are found in (api.doc_dirs: docs/adr, or what sessions
    declared), with its repo's name."""
    return [(d, root.name if (root := api.git_root(d)) else d.name) for d in api.doc_dirs(ctx.sessions())]


def routes(ctx: api.Context) -> list[api.Route]:
    def decisions(call: api.Call):
        found = dirs(ctx)
        return {"decisions": scan.as_json(SCANNER.scan(found)), "dirs": [str(d) for d, _ in found]}
    return [api.Route("decisions", decisions)]


def doc_roots(ctx: api.Context) -> list[Path]:
    return [d for d, _ in dirs(ctx)]


# the order a header click gives (as the inbox's: ordering.next_item_sort)

def next_sort(spec: str, column: str) -> str:
    """Status cycles *, ▲, ▼; another column ▲, ▼, then back to Status *."""
    col, mark = spec[:-1], spec[-1]
    if column == "status":
        return {STAR: "status" + UP, UP: "status" + DOWN}.get(mark, DEFAULT) if col == "status" else DEFAULT
    if col != column:
        return column + UP
    return column + DOWN if mark == UP else DEFAULT


def sorted_by(rows: list[dict], spec: str) -> list[dict]:
    """rows (as the route gives them, in scan.ordered's order) in a header's order; the
    default leaves them as they are. Ties keep that order."""
    col, mark = spec[:-1], spec[-1]
    if mark == STAR:
        return list(rows)
    key = {"number": lambda r: int(r["number"]), "status": lambda r: r["status"],
           "title": lambda r: r["title"].casefold(), "repo": lambda r: r["repo"].casefold()}[col]
    return sorted(rows, key=key, reverse=mark == DOWN)


class DecisionTable(DataTable):
    """The decisions, a row each. A list, so a Tab stop after the inbox's (tui.tab_stops)."""
    TAB_LIST = True


class Decisions(Vertical):
    """A bordered pane, titled Decisions (its drag handle), of the decisions in every repo a
    session works in: number, status, title, and repo when there's more than one. Asked of the module's route on a worker
    thread when it mounts and every EVERY seconds after (tick), never on the app's thread.
    Moving the cursor shows the decision in the Detail pane; Enter, or a click on the row
    the cursor is on, opens it full screen. A header click sorts, as the inbox's do."""

    DEFAULT_CSS = """
    Decisions { height: 1fr; }
    Decisions > DecisionTable { height: 1fr; }
    Decisions > #decisions-note { color: #777777; padding: 0 1; height: auto; }
    Decisions > #decisions-note.-one-row { height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    """
    # its border and a row: the note, or the table's header, as the inbox's 3 (tui.PANE_MINIMUM)
    PANE_MINIMUM = (12, 3)

    def __init__(self, ctx: api.Context, **kwargs):
        super().__init__(classes="panel", **kwargs)
        self.border_title = "Decisions"
        self.ctx = ctx
        self.rows: list[dict] = []           # as the route last gave them
        self.looked: list[str] = []          # the directories it looked in
        self.error: str | None = None        # why the last ask failed
        self.sort = DEFAULT
        self.asked_at = 0.0                  # time.monotonic() at the last ask
        self.compact = False                 # no decisions found: the slot shrinks to the note (api)
        self.where: tuple | None = None      # the sessions' directories and doc dirs at the last tick
        self.asking = False
        self.again = False                   # asked while an ask was out: asked again when it lands
        self.shown: str | None = None        # the path last shown in Detail from here
        self.drawn: tuple | None = None      # what the table was last built from

    def compose(self):
        yield DecisionTable(id="decision-list", cursor_type="row", zebra_stripes=False)
        yield Static(id="decisions-note", markup=False)

    def on_mount(self) -> None:
        self.paint()
        self.ask()

    def tick(self) -> None:
        """An ask once EVERY seconds have passed since the last, however often the app ticks
        (a selection change ticks too, for the stats pane); at once when the sessions'
        directories or the doc dirs they declared have changed (a session started in another
        repo, set_doc_dirs)."""
        where = tuple(sorted((s.get("cwd") or "", tuple(s.get("doc_dirs") or ())) for s in self.ctx.sessions()))
        if time.monotonic() - self.asked_at >= EVERY or where != self.where:
            self.where = where
            self.ask()

    def ask(self) -> None:
        """The route asked again, on a worker thread (a remote call blocks); while an ask is
        still out, once more when it lands, as what it was asked for may have changed."""
        if self.ctx.call is None:
            return
        if self.asking:
            self.again = True
            return
        self.again = False
        self.asking = True
        self.asked_at = time.monotonic()
        self.run_worker(self.fetch, thread=True, group="decisions", exit_on_error=False)

    def fetch(self) -> None:
        try:
            got = self.ctx.call("decisions")
        except api.Refused as e:
            got = {"error": e.error}
        except Exception as e:   # never left asking
            got = {"error": f"{type(e).__name__}: {e}"}
        self.app.call_from_thread(self.got, got)

    def got(self, got: dict) -> None:
        self.asking = False
        if self.again:
            self.ask()
        self.error = got.get("error")
        if self.error is None:
            self.rows, self.looked = got.get("decisions", []), got.get("dirs", [])
        self.paint()
        # none found: only the note, which says where it looked; a failed ask keeps it as it was,
        # its error in the note
        compact = self.compact if self.error is not None else not self.rows
        if compact != self.compact:
            self.compact = compact
            self.query_one(DecisionTable).display = not compact
            if self.ctx.relayout is not None:
                self.ctx.relayout()
        self.fit_note()   # compact now in a slot already that small: no resize to say so

    def on_resize(self) -> None:
        self.fit_note()
        self.paint()

    def fit_note(self) -> None:
        """The note, compact in a pane with one row inside (tui.fit_compact, or a slot dragged
        that small): what fits on that row, cut short with an ellipsis."""
        self.query_one("#decisions-note").set_class(self.compact and self.content_size.height <= 1, "-one-row")

    def paint(self) -> None:
        """The table built again, only if what it shows, its order or its width has changed;
        the cursor kept on the row it was on."""
        table = self.query_one(DecisionTable)
        note = self.query_one("#decisions-note", Static)
        rows = sorted_by(self.rows, self.sort)
        said = self.note()
        note.update(said)
        note.display = bool(said)
        width = self.content_size.width
        drawn = (tuple(tuple(sorted(r.items())) for r in rows), self.sort, width)
        if drawn == self.drawn:
            return
        self.drawn = drawn
        at = self.key_at(table)
        table.clear(columns=True)
        columns = COLUMNS if len({r["repo"] for r in rows}) > 1 else COLUMNS[:3]   # one repo: no Repo column
        status_w = max([6, *(len(r["status"]) for r in rows)])
        repo_w = min(12, max([4, *(len(r["repo"]) for r in rows)]))
        # the title takes what the others leave: each column has a cell of padding either side,
        # and the last cell is the scrollbar's
        fixed = 4 + status_w + (repo_w if len(columns) == 4 else 0)
        title_w = max(6, width - fixed - 2 * len(columns) - 1)
        for (key, label), w in zip(columns, (4, status_w, title_w, repo_w)):
            mark = self.sort[-1] if self.sort[:-1] == key else ""
            table.add_column(f"{label} {mark}" if mark else label, key=key, width=w)
        for r in rows:
            cells = [r["number"], Text(r["status"], style=LOOKS.get(r["status"], "")), r["title"], r["repo"]]
            table.add_row(*map(api.plain, cells[:len(columns)]), key=r["path"])   # a title as itself, never markup
        if at is not None and at in table.rows:
            table.move_cursor(row=table.get_row_index(at), animate=False)

    def note(self) -> str:
        if self.error:
            return f"Couldn't list decisions: {self.error}"
        if not self.rows:
            where = ", ".join(self.looked) if self.looked else "docs/adr in a session's repo"
            return f"No decisions (NNNN-*.md) in {where}."
        return ""

    @staticmethod
    def key_at(table: DataTable) -> str | None:
        if not table.row_count:
            return None
        try:
            return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return None

    def row(self, path: str) -> dict | None:
        return next((r for r in self.rows if r["path"] == path), None)

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        event.stop()
        self.sort = next_sort(self.sort, event.column_key.value)
        self.paint()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """The cursor moved by the person (the table has focus): the decision in Detail. Not
        when the table is built again with the cursor where it was."""
        event.stop()
        path = event.row_key.value
        if not event.data_table.has_focus or path == self.shown or (r := self.row(path)) is None:
            return
        self.shown = path
        if self.ctx.show_doc is not None:
            self.ctx.show_doc(path, title_of(r))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        if (r := self.row(event.row_key.value)) is not None and self.ctx.open_doc is not None:
            self.ctx.open_doc(r["path"], title_of(r))

    def on_descendant_blur(self) -> None:
        self.shown = None   # back in, the cursor's next move shows its decision again


def title_of(row: dict) -> str:
    return f"{row['number']} {row['title']}"


module = api.Module(
    id="design", title="Design", version=importlib.metadata.version("claude-wheelhouse-design"),
    api=api.WHEELHOUSE_API, panes=[api.Pane("Decisions", "middle.bottom", {"tui": Decisions})],
    routes=routes, doc_roots=doc_roots)
