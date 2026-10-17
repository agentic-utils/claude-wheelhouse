import pytest
from textual.widget import Widget

import subprocess
import sys

from claude_wheelhouse.splitter import Splitter

from claude_wheelhouse import api
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
    stats = pytest.importorskip("claude_wheelhouse_stats", reason="installed without the stats module")
    [found] = [m for m in api.load() if m.name == "stats"]
    assert found.module is stats.module and found.error is None


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


@pytest.mark.parametrize("value, error, desc", [
    ("claude_wheelhouse.stats_pane:module", api.STALE,
     "an install older than the stats module's own distribution: its card says to reinstall"),
    ("claude_wheelhouse_gone:module", "failed to load: no module", "another package's: why it failed"),
])
def test_an_entry_point_that_wont_import(value, error, desc):
    [found] = api.load([Entry("stats", ImportError("no module"), value)])
    assert found.error == error, desc


def test_no_modules_installed_is_no_error(monkeypatch):
    """.plan/modules.md ("No module, no harm"): installed without the stats module, nothing
    is missing; its slot collapses (test_the_slots)."""
    monkeypatch.setattr(api.importlib.metadata, "entry_points", lambda group: [])
    assert api.load() == []


def test_the_wheelhouse_runs_without_the_stats_module():
    """Core never imports a module: with claude_wheelhouse_stats unimportable, the app, hub,
    daemon and client all import, and the ? overlay is drawn."""
    code = ("import sys; sys.modules['claude_wheelhouse_stats'] = None\n"
            "from claude_wheelhouse import cli, client, daemon, hub, tui\n"
            "assert 'Keys' in tui.keys_help(sdk=True)\n")
    run = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr


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
    ([api.Loaded("stats", None, api.STALE)], [f"stats: {api.STALE}"], "a stale install's stats entry point"),
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
    (None, {**CORE, "left.middle": ["stats"], "middle.bottom": ["design"]}, {"left-middle", "middle-top"},
     "the default slots, both modules installed: stats under the session list, decisions under the inbox"),
    ([], CORE, set(), "no modules: left.middle collapses too"),
    ([api.Loaded("demo", module(slot="middle.bottom"))], {**CORE, "middle.bottom": ["demo"]}, {"middle-top"},
     "a pane for middle.bottom: it shows under the inbox"),
])
async def test_the_slots(store, monkeypatch, loaded, slots, splitters, desc):
    """.plan/modules.md: six slots in three columns; an empty slot collapses, with the
    splitter beside it, and its column's other slots share the room."""
    if loaded is None:
        pytest.importorskip("claude_wheelhouse_stats", reason="installed without the stats module")
        pytest.importorskip("claude_wheelhouse_design", reason="installed without the design module")
    else:
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


# stage 5: a pane's own routes (Context.call), its docs (Context.show_doc), and switching a module off

def probe_module(root, made):
    """The test module of test_served, with a pane in middle.bottom keeping the context it's made with."""
    from tests.test_served import probe
    m = probe([root])
    m.panes = [api.Pane("Probe", "middle.bottom", {"tui": lambda ctx: made.append(ctx) or Widget()})]
    return m


@pytest.mark.anyio
@pytest.mark.both
async def test_a_pane_reaches_its_routes_and_shows_its_docs(store, sid, monkeypatch, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "0001-sqlite.md").write_text("# Use SQLite\n\nWe chose SQLite.\n")
    (tmp_path / "out.md").write_text("not for showing\n")
    store.set_doc_dirs(sid, ["docs/decisions"])
    made = []
    monkeypatch.setattr(api, "load", lambda: [api.Loaded("probe", probe_module(root, made))])
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        [ctx] = made
        assert ctx.call("echo/x?q=1") == {"params": {"a": "x"}, "query": {"q": "1"}, "body": None}, "a GET"
        assert ctx.call("echo/y", {"k": 1})["body"] == {"k": 1}, "a POST"
        with pytest.raises(api.Refused) as refused:
            ctx.call("refuse")
        assert (refused.value.status, refused.value.error) == (409, "not now"), "the route's refusal"
        assert [(s["id"], s["cwd"], s["doc_dirs"]) for s in ctx.sessions()] == [
            (sid, str(tmp_path), ["docs/decisions"])], "a session's directory and the doc dirs it declared"
        ctx.show_doc(str(root / "0001-sqlite.md"), "0001 Use SQLite")
        await pilot.pause()
        assert "We chose SQLite." in app._detail_text and "0001 Use SQLite" in app._detail_text
        (root / "0001-sqlite.md").write_text("# Use SQLite\n\nWe chose SQLite, then WAL.\n")
        app.refresh_data()
        await pilot.pause()
        assert "then WAL." in app._detail_text, "a change to the file shows at the next refresh"
        assert not app.query_one("#answer").display, "a doc: no answer box"
        ctx.show_doc(str(root / ".." / "out.md"), "Out")
        await pilot.pause()
        assert "Not shown" in app._detail_text and "not for showing" not in app._detail_text, \
            "a path outside the doc roots is refused, .. resolved first"
        app.selected = (sid, None)
        app.paint_detail()
        assert "Not shown" not in app._detail_text, "the next selection takes the Detail pane back"


@pytest.mark.anyio
async def test_a_module_switched_off_shows_nothing(store, monkeypatch, tmp_path):
    made = []
    monkeypatch.setattr(api, "load", lambda: [api.Loaded("probe", probe_module(tmp_path, made))])
    store.set_setting(api.DISABLED, '["probe"]')
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert made == [] and not app.query("#probe"), "no pane"
        assert not app.query_one("#slot-middle-bottom").display, "its slot collapses"
        with pytest.raises(api.Refused) as e:
            app.client.call_module("probe", "echo/x")
        assert e.value.status == 404, "and no routes"


@pytest.mark.parametrize("argv, kept, says, desc", [
    (["modules"], None, "probe  1.0  API 2  on", "the list: id, version, API, status"),
    (["modules", "disable", "probe"], '["probe"]', "probe disabled: from the wheelhouse's next start", "switched off"),
    (["modules", "enable", "probe"], None, "probe enabled", "and on again"),
])
def test_the_modules_command(store, monkeypatch, capsys, argv, kept, says, desc):
    from claude_wheelhouse import cli
    from tests.test_served import probe
    monkeypatch.setattr(api, "load", lambda: [api.Loaded("probe", probe())])
    if argv[1:2] == ["enable"]:
        store.set_setting(api.DISABLED, '["probe"]')
    monkeypatch.setattr(sys, "argv", ["claude-wheelhouse", *argv])
    cli.main()
    assert says in capsys.readouterr().out, desc
    assert store.setting(api.DISABLED) == kept, desc


@pytest.mark.parametrize("argv, refused, desc", [
    (["modules", "disable", "probe", "--connect", "far:7428"], "run it on the machine whose daemon is at far:7428",
     "with --connect: the daemon's machine is the one to run it on"),
    (["modules", "disable", "probe"], "no wheelhouse database on this machine", "a machine with no database of its own"),
])
def test_the_modules_command_refuses_another_machines_wheelhouse(db_file, monkeypatch, argv, refused, desc):
    from claude_wheelhouse import cli
    monkeypatch.setattr(sys, "argv", ["claude-wheelhouse", *argv])
    if "--connect" not in argv:
        monkeypatch.setenv("WHEELHOUSE_DB", str(db_file.parent / "absent" / "wheelhouse.db"))
    with pytest.raises(SystemExit, match=refused):
        cli.main()
        pytest.fail(desc)


def test_the_modules_command_refuses_a_module_not_installed(store, monkeypatch):
    from claude_wheelhouse import cli
    monkeypatch.setattr(api, "load", lambda: [])
    monkeypatch.setattr(sys, "argv", ["claude-wheelhouse", "modules", "disable", "nosuch"])
    with pytest.raises(SystemExit, match="no module 'nosuch' is installed"):
        cli.main()
    assert store.setting(api.DISABLED) is None
