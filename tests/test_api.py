import pytest
from textual.widget import Widget

from claude_wheelhouse.splitter import Splitter

from claude_wheelhouse import api, stats_pane
from claude_wheelhouse.tui import WheelhouseApp


class Entry:
    """An entry point as importlib.metadata gives it, loading whatever it's handed."""

    def __init__(self, name, loads, value="pkg:module"):
        self.name, self.value, self.loads = name, value, loads

    def load(self):
        if isinstance(self.loads, Exception):
            raise self.loads
        return self.loads


def module(id="demo", api_version=api.WHEELHOUSE_API, slot="left.middle", widget=Widget):
    return api.Module(id, id.title(), "1.0", api_version, [api.Pane(id.title(), slot, {"tui": lambda ctx: widget()})])


def test_stats_is_found_by_its_entry_point():
    [found] = [m for m in api.load() if m.name == "stats"]
    assert found.module is stats_pane.module and found.error is None


@pytest.mark.parametrize("loads, error, desc", [
    (module(), None, "a module written against this API runs"),
    (ImportError("no module named demo"), "failed to load: no module named demo", "one that won't import is reported"),
    ("not a module", "pkg:module is not a Module", "nor does an entry point that isn't a Module"),
    (module(api_version=0), f"written for API 0; this wheelhouse has {api.WHEELHOUSE_API}",
     "one written against another API isn't started"),
    (module(slot="inbox.side"), "its pane 'Demo' names slot 'inbox.side', not one of " + ", ".join(api.SLOTS),
     "nor one with a pane for a slot that isn't one of the six"),
])
def test_load(loads, error, desc):
    [found] = api.load([Entry("demo", loads)])
    assert found.error == error, desc


def test_the_stats_module_missing_says_to_reinstall(monkeypatch):
    """An installed copy older than the stats entry point: the slot says why it's empty."""
    monkeypatch.setattr(api.importlib.metadata, "entry_points", lambda group: [])
    assert [(m.name, m.error) for m in api.load()] == [("stats", api.STALE)]


@pytest.mark.parametrize("loaded, ids, slot, desc", [
    ([api.Loaded("a", module("a"))], [("a", True)], api.CARDS, "a running module's pane"),
    ([api.Loaded("a", module("a", slot="tab"))], [], api.CARDS, "a pane for another slot"),
    ([api.Loaded("a", None, "failed to load: boom")], [("a", False)], api.CARDS, "a module that didn't load: its reason"),
    ([api.Loaded("a", None, "failed to load: boom")], [], "right", "and only in CARDS"),
    ([api.Loaded("a", module("a", api_version=0), "old")], [("a", False)], api.CARDS, "one that didn't start: its reason"),
    ([api.Loaded("a", module("a", api_version=1, slot="inbox.side"), "old")], [("a", False)], api.CARDS,
     "an API 1 module's inbox.side, no slot now: its reason in CARDS"),
    ([api.Loaded("a", module("a", slot="nowhere"), "bad slot")], [("a", False)], api.CARDS, "any slot not in SLOTS"),
    ([api.Loaded("a", module("a", slot="nowhere"), "bad slot")], [], "nowhere", "and not in the slot it names"),
    ([api.Loaded("a", module("a", api_version=0, slot="right"), "old")], [], api.CARDS,
     "one naming a real slot: its reason there, not in CARDS"),
])
def test_panes_for_a_slot(loaded, ids, slot, desc):
    assert [(item.name, pane is not None) for item, pane in api.panes(loaded, slot)] == ids, desc


class Breaks(Widget):
    def tick(self):
        raise ValueError("bad tick")


@pytest.mark.anyio
@pytest.mark.parametrize("loaded, cards, desc", [
    ([api.Loaded("demo", None, "failed to load: boom")], ["demo: failed to load: boom"],
     "a module that didn't load shows why in its slot"),
    ([api.Loaded("demo", module(widget=Breaks))], ["demo: ValueError: bad tick"],
     "a pane that raises is swapped for a card saying so"),
    ([api.Loaded("demo", module(widget=lambda: "a pane"))], ["demo: its tui surface made a str, not a widget"],
     "a factory that makes something other than a widget"),
    ([api.Loaded("a", module("demo")), api.Loaded("b", module("demo"))], ["b: its id 'demo' is taken by a"],
     "a second module with the same id: the first runs"),
    ([api.Loaded("detail", module("detail"))], ["detail: its id 'detail' is taken by the wheelhouse"],
     "a module with the id of one of the app's widgets, after its slot"),
    ([api.Loaded("title", module("title"))], ["title: its id 'title' is taken by the wheelhouse"], "or before it"),
    ([api.Loaded("stats", None, api.STALE)], [f"stats: {api.STALE}"], "the stats module not installed"),
    (api.load([Entry("demo", module(api_version=1, slot="inbox.side"))]),
     [f"demo: written for API 1; this wheelhouse has {api.WHEELHOUSE_API}"], "an API 1 module's card shows, in CARDS"),
    ([api.Loaded("demo", module())], [], "a pane with no hooks runs: Textual's own Widget.animate isn't one"),
])
async def test_a_broken_module_leaves_the_app_running(store, monkeypatch, clock, loaded, cards, desc):
    monkeypatch.setattr(api, "load", lambda: loaded)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        app.refresh_data()
        for _ in range(2):   # animate's turn too: the panes animate every other frame
            clock.animate(app)
            await pilot.pause()
        assert app.is_running, desc
        shown = [str(w.render()) for w in app.query(".module-error")]
        assert shown == cards, desc


@pytest.mark.anyio
async def test_with_no_modules_the_items_fill_their_column(store, monkeypatch):
    monkeypatch.setattr(api, "load", lambda: [])
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        items = app.query_one("#items")
        assert items.size.height >= items.parent.content_size.height - 1, "no stats pane: no empty half"


CORE = {"left.top": ["sessions-pane"], "left.bottom": ["session-info-pane"], "middle.top": ["items-pane"],
        "right": ["detail-pane"]}
SPLITTERS = {"sessions-pane", "items-pane", "left-bottom", "answer"}   # beside no slot that can be empty


@pytest.mark.anyio
@pytest.mark.parametrize("loaded, slots, splitters, desc", [
    (None, {**CORE, "left.middle": ["stats"]}, {"left-middle"},
     "the default slots: stats under the session list, middle.bottom empty and collapsed"),
    ([], CORE, set(), "no modules: left.middle collapses too"),
    ([api.Loaded("demo", module(slot="middle.bottom"))], {**CORE, "middle.bottom": ["demo"]}, {"middle-top"},
     "a pane for middle.bottom: it shows under the inbox"),
])
async def test_the_slots(store, monkeypatch, loaded, slots, splitters, desc):
    """.plan/modules.md: six slots in three columns; an empty slot collapses, with the
    splitter beside it, and its column's other slots share the room."""
    if loaded is not None:
        monkeypatch.setattr(api, "load", lambda: loaded)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        shown = {s.name: [w.id for w in s.children] for s in app.query(".slot") if s.display}
        assert shown == slots, desc
        assert {s.key for s in app.query(Splitter) if s.display} == SPLITTERS | splitters, desc
        for column in app.query("#main > Vertical"):
            assert sum(w.outer_size.height for w in column.children if w.display) == column.size.height, \
                f"{desc}: {column.id} filled"
