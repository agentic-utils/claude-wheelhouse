import pytest
from textual.widget import Widget

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


def module(id="demo", api_version=api.WHEELHOUSE_API, slot="inbox.side", widget=Widget):
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
])
def test_load(loads, error, desc):
    [found] = api.load([Entry("demo", loads)])
    assert found.error == error, desc


@pytest.mark.parametrize("loaded, ids, desc", [
    ([api.Loaded("a", module("a"))], [("a", True)], "a running module's pane"),
    ([api.Loaded("a", module("a", slot="tab"))], [], "a pane for another slot"),
    ([api.Loaded("a", None, "failed to load: boom")], [("a", False)], "a module that didn't load: its reason"),
    ([api.Loaded("a", module("a", api_version=0), "old")], [("a", False)], "one that didn't start: its reason"),
])
def test_panes_for_a_slot(loaded, ids, desc):
    assert [(item.name, pane is not None) for item, pane in api.panes(loaded, "inbox.side")] == ids, desc


class Breaks(Widget):
    def tick(self):
        raise ValueError("bad tick")


@pytest.mark.anyio
@pytest.mark.parametrize("loaded, card, desc", [
    ([api.Loaded("demo", None, "failed to load: boom")], "demo: failed to load: boom",
     "a module that didn't load shows why in its slot"),
    ([api.Loaded("demo", module(widget=Breaks))], "demo: ValueError: bad tick",
     "a pane that raises is swapped for a card saying so"),
])
async def test_a_broken_module_leaves_the_app_running(store, monkeypatch, loaded, card, desc):
    monkeypatch.setattr(api, "load", lambda: loaded)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        app.refresh_data()
        await pilot.pause()
        assert app.is_running, desc
        shown = [str(w.render()) for w in app.query(".module-error")]
        assert shown == [card], desc


@pytest.mark.anyio
async def test_with_no_modules_the_items_fill_their_column(store, monkeypatch):
    monkeypatch.setattr(api, "load", lambda: [])
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        items = app.query_one("#items")
        assert items.size.height >= items.parent.content_size.height - 1, "no stats pane: no empty half"
