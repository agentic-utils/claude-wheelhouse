"""The prs module: the PRs pane, a table of open pull requests under the inbox, each with a
traffic light (.plan/pr-pane.md, part 3), plugged in through the module protocol
(claude_wheelhouse.api) like any other module. Its daemon side, the route the pane asks, is
fetch.py's: one `gh api graphql` call, cached in `<state dir>/prs.db`. The pane shows a PR
item in the Detail pane (Context.show_item), any other PR as a card of its own, and opens a
PR in the browser on Enter or a click on its card's link."""

import importlib.metadata
import time

from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import DataTable, Static

from claude_wheelhouse import api

from . import fetch

EVERY = 15   # seconds between the pane's asks: the route fetches only when one is due (fetch.due)
UP, DOWN, STAR = "▲", "▼", "*"   # a header's marker, as the inbox's
DEFAULT = "light*"   # the light's urgency, then most recently updated first (fetch.ordered)
COLUMNS = (("pr", "PR"), ("repo", "Repo"), ("title", "Title"), ("light", ""))
DOT = "●"
DIM = "#5a3500"   # an orange light, flashed off
LOOKS = {"red": "#ff3b30", "orange": "#ff9500", "yellow": "#ffd300", "green": "#00ff9f", "grey": "#8c8c8c"}
SCOPES = ("mine", "all")
RUNNER: fetch.Runner = fetch.gh          # gh itself; the tests put a stub here
CLOCK = time.time                        # the route's clock; the tests move it on


# the daemon side: the route the pane asks

def routes(ctx: api.Context) -> list[api.Route]:
    fetcher = fetch.Fetcher(ctx, lambda argv: RUNNER(argv), lambda: CLOCK())   # asked once a daemon (served.Served)

    def listed(call: api.Call):
        scope = call.query.get("scope", "mine")
        if scope not in SCOPES:
            raise api.Refused(400, f"scope is mine or all, not {scope!r}")
        return fetcher.answer(scope, call.query.get("fresh") == "1")
    return [api.Route("list", listed)]


# the order a header click gives (as the inbox's: ordering.next_item_sort)

def next_sort(spec: str, column: str) -> str:
    """The light cycles *, ▲, ▼; another column ▲, ▼, then back to the light's *."""
    col, mark = spec[:-1], spec[-1]
    if column == "light":
        return {STAR: "light" + UP, UP: "light" + DOWN}.get(mark, DEFAULT) if col == "light" else DEFAULT
    if col != column:
        return column + UP
    return column + DOWN if mark == UP else DEFAULT


def sorted_by(rows: list[dict], spec: str) -> list[dict]:
    """rows (in the route's order, fetch.ordered) in a header's order; the default leaves
    them as they are. Ties keep that order."""
    col, mark = spec[:-1], spec[-1]
    if mark == STAR:
        return list(rows)
    key = {"pr": lambda r: int(r["number"] or 0), "repo": lambda r: short(r).casefold(),
           "title": lambda r: r["title"].casefold(), "light": lambda r: fetch.LIGHTS.index(r["light"])}[col]
    return sorted(rows, key=key, reverse=mark == DOWN)


def short(r: dict) -> str:
    return r["repo"].rpartition("/")[2] or api.repo_of(r["url"])


def tip(r: dict) -> str:
    return f"{r['repo']} #{r['number']}: {r['title']}"


def link(url: str) -> Text:
    """A PR's URL as the card shows it, any line of it opening the whole URL (api.pr_link, #139)."""
    return api.pr_link(url)


class PrCard(VerticalScroll, can_focus=False):
    """The card under the table (card), its text in a Static of its own (#prs-card-text): it
    scrolls, with the wheel, where the pane is too short for all of it. Never focused: a
    click on it leaves the focus on the table, whose focus shows it."""

    def compose(self):
        yield Static(id="prs-card-text", markup=False)

    def update(self, text: Text) -> None:
        self.query_one("#prs-card-text", Static).update(text)


def card(r: dict) -> Text:
    """A PR that isn't an item, as the pane shows it: its link first, the one line a short pane's
    card has room for (#139), then title, branch, checks, review."""
    out = Text().append_text(link(r["url"]))   # a span: the link's line alone opens it
    out.append("\n")
    out.append(r["title"] or f"PR {r['number']}", style="bold")
    for label, value in (("Branch", r["branch"] or "?"),
                         ("Checks", (r["checks"] or "none").lower()),
                         ("Review", (r["review"] or "none required").lower().replace("_", " ")),
                         ("Mergeable", (r["mergeable"] or "unknown").lower())):
        out.append(f"\n{label}: ", style="#777777")
        out.append(value)
    return out


class PrTable(api.RowTips, DataTable):
    """The PRs, a row each, the full title on hover. A list, so a Tab stop after the inbox's."""
    TAB_LIST = True

    def row_tip(self, row: int) -> str | None:
        r = self.parent.row(self.coordinate_to_cell_key((row, 0)).row_key.value)
        return tip(r) if r else None


class Prs(Vertical):
    """A bordered pane, titled `PRs · mine` or `PRs · all` (its drag handle), of the open pull
    requests: PR, repo when there's more than one, title and its light. Asked of the module's
    route on a worker thread when it mounts (fresh) and every EVERY seconds after (tick).
    Moving the cursor shows a PR item in the Detail pane, any other PR as a card under the
    table; Enter opens it in the browser. m switches the scope, r fetches now."""

    DEFAULT_CSS = """
    Prs { height: 1fr; }
    Prs > PrTable { height: 1fr; }
    Prs > #prs-note { color: #777777; padding: 0 1; height: auto; }
    Prs > #prs-card { padding: 0 1; height: auto; max-height: 50%; border-top: solid #444444; }
    """
    PANE_MINIMUM = (12, 3)
    KEYS_HELP = ("The PRs pane", False)
    BINDINGS = [Binding("m", "scope", "Mine or all", show=False),
                Binding("r", "refresh", "Fetch now", show=False)]

    def __init__(self, ctx: api.Context, **kwargs):
        super().__init__(classes="panel", **kwargs)
        self.ctx = ctx
        self.scope = "mine"
        self.rows: list[dict] = []           # as the route last gave them
        self.error: str | None = None        # gh unavailable, or the ask failed: in place of the table
        self.warning: str | None = None      # the last fetch failed: the cache shown, and why
        self.sort = DEFAULT
        self.asked_at = 0.0                  # time.monotonic() at the last ask
        self.asking = False
        self.again: bool | None = None       # asked while an ask was out: asked again (fresh?) when it lands
        self.lit = True                      # the orange lights' flash, on or off (animate)
        self.shown: str | None = None        # the URL last shown from here
        self.drawn: tuple | None = None      # what the table was last built from
        self.show_scope()

    def show_scope(self) -> None:
        self.border_title = f"PRs · {self.scope}"

    def compose(self):
        yield PrTable(id="pr-list", cursor_type="row", zebra_stripes=False)
        yield Static(id="prs-note", markup=False)
        yield PrCard(id="prs-card")

    def on_mount(self) -> None:
        self.query_one("#prs-card").display = False
        self.paint()
        self.ask(fresh=True)

    def tick(self) -> None:
        if self.is_mounted and time.monotonic() - self.asked_at >= EVERY:
            self.ask()

    def animate(self, frame: int) -> None:
        """The orange lights flash, on for three frames and off for three, while any is lit."""
        lit = frame // 3 % 2 == 0
        if lit == self.lit or not self.is_mounted:   # not composed yet: nothing to flash
            return
        self.lit = lit
        table = self.query_one(PrTable)
        for r in self.rows:
            if r["light"] == "orange" and r["url"] in table.rows:
                table.update_cell(r["url"], "light", self.dot(r))

    def dot(self, r: dict) -> Text:
        return Text(DOT, style=LOOKS[r["light"]] if r["light"] != "orange" or self.lit else DIM)

    def action_scope(self) -> None:
        self.scope = SCOPES[(SCOPES.index(self.scope) + 1) % len(SCOPES)]
        self.show_scope()
        self.ask()

    def action_refresh(self) -> None:
        self.ask(fresh=True)

    def ask(self, fresh: bool = False) -> None:
        """The route asked again, on a worker thread (a fetch takes seconds); while an ask is
        still out, once more when it lands."""
        if self.ctx.call is None:
            return
        if self.asking:
            self.again = bool(self.again) or fresh
            return
        self.again = None
        self.asking = True
        self.asked_at = time.monotonic()
        path = f"list?scope={self.scope}&fresh={int(fresh)}"
        self.run_worker(lambda: self.fetch(path), thread=True, group="prs", exit_on_error=False)

    def fetch(self, path: str) -> None:
        try:
            got = self.ctx.call(path, None)
        except api.Refused as e:
            got = {"error": e.error}
        except Exception as e:   # never left asking
            got = {"error": f"{type(e).__name__}: {e}"}
        self.app.call_from_thread(self.got, got)

    def got(self, got: dict) -> None:
        self.asking = False
        if self.again is not None:
            self.ask(fresh=self.again)
        self.error, self.warning = got.get("error"), got.get("warning")
        if self.error is None:
            self.rows = got.get("prs", [])
        self.paint()

    def on_resize(self) -> None:
        self.paint()

    def paint(self) -> None:
        """The table built again only if what it shows, its order or its width has changed;
        the cursor kept on the row it was on."""
        table = self.query_one(PrTable)
        note = self.query_one("#prs-note", Static)
        said = self.note()
        note.update(said)
        note.display = bool(said)
        table.display = self.error is None
        rows = sorted_by(self.rows, self.sort)
        width = self.content_size.width
        drawn = (tuple(tuple(sorted((k, str(v)) for k, v in r.items())) for r in rows), self.sort, width)
        if drawn == self.drawn:
            return
        self.drawn = drawn
        at = self.key_at(table)
        table.clear(columns=True)
        repos = len({short(r) for r in rows}) > 1
        columns = COLUMNS if repos else (COLUMNS[0], *COLUMNS[2:])   # one repo: no Repo column
        pr_w = max([4, *(len(f"PR{r['number']}") for r in rows)])
        repo_w = min(14, max([4, *(len(short(r)) for r in rows)])) if repos else 0
        # the title takes what the others leave: a cell of padding either side of each
        # column, and the last cell is the scrollbar's
        title_w = max(6, width - pr_w - repo_w - 1 - 2 * len(columns) - 1)
        widths = {"pr": pr_w, "repo": repo_w, "title": title_w, "light": 1}
        for key, label in columns:
            mark = self.sort[-1] if self.sort[:-1] == key else ""
            table.add_column(f"{label} {mark}".strip() if mark else label, key=key, width=widths[key])
        for r in rows:
            cells = {"pr": Text(f"PR{r['number']}", style=api.KIND_COLOUR["pr"]), "repo": short(r),
                     "title": r["title"], "light": self.dot(r)}
            table.add_row(*(api.plain(cells[k]) for k, _ in columns), key=r["url"])   # a title as itself, never markup
        if at is not None and at in table.rows:
            table.move_cursor(row=table.get_row_index(at), animate=False)
        table.tip_again()
        self.paint_card()

    def note(self) -> str:
        if self.error:
            return f"Couldn't list PRs: {self.error}"
        if self.warning:
            return f"The last fetch failed ({self.warning}): showing what was fetched before."
        if not self.rows:
            return "No open PRs from the sessions: m shows all of yours." if self.scope == "mine" \
                else "No open PRs of yours."
        return ""

    @staticmethod
    def key_at(table: DataTable) -> str | None:
        if not table.row_count:
            return None
        try:
            return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return None

    def row(self, url: str | None) -> dict | None:
        return next((r for r in self.rows if r["url"] == url), None)

    def paint_card(self) -> None:
        """The card under the table: the PR the cursor is on, while the table has the focus
        and it isn't an item (an item shows in Detail)."""
        table = self.query_one(PrTable)
        r = self.row(self.key_at(table)) if table.has_focus else None
        shown = r is not None and r["item"] is None and self.error is None
        box = self.query_one("#prs-card", PrCard)
        if shown:
            box.update(card(r))
        box.display = shown

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        event.stop()
        self.sort = next_sort(self.sort, event.column_key.value)
        self.paint()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """The cursor moved by the person (the table has focus): a PR item in Detail, any
        other PR as a card. Not when the table is built again with the cursor where it was."""
        event.stop()
        url = event.row_key.value
        if not event.data_table.has_focus or url == self.shown or (r := self.row(url)) is None:
            return
        self.shown = url
        self.paint_card()
        if r["item"] is not None and self.ctx.show_item is not None:
            self.ctx.show_item(r["item"]["session_id"], r["item"]["ref"])

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter, or a click on the cursor's row: the PR in the browser."""
        event.stop()
        if (r := self.row(event.row_key.value)) is not None:
            self.app.open_url(r["url"])

    def on_descendant_focus(self) -> None:
        self.paint_card()

    def on_descendant_blur(self) -> None:
        self.shown = None   # back in, the cursor's next move shows its PR again
        self.paint_card()


module = api.Module(
    id="prs", title="PRs", version=importlib.metadata.version("claude-wheelhouse-prs"),
    api=api.WHEELHOUSE_API, panes=[api.Pane("PRs", "middle.middle", {"tui": Prs})],
    routes=routes)
