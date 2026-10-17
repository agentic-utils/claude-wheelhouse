"""The design module's Decisions pane in the app (modules/design): here, not with the module's own
tests, for the app's fixtures (conftest.py). Skipped when it isn't installed."""

import pytest

from claude_wheelhouse import api
from claude_wheelhouse.tui import DocView, WheelhouseApp, tab_stops

design = pytest.importorskip("claude_wheelhouse_design", reason="installed without the design module")
from claude_wheelhouse_design import pane  # noqa: E402


def repo(root, files, where="docs/adr"):
    """A git repo at root with files in `where`."""
    (root / ".git").mkdir(parents=True, exist_ok=True)
    d = root / where
    d.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (d / name).write_text(text)
    return d


@pytest.fixture
def decisions(store, sid, tmp_path, monkeypatch):
    """The session's directory a repo with three decisions, and the design module the only one."""
    d = repo(tmp_path, {"0001-ACCEPTED-use-sqlite.md": "# Use SQLite\n\nWe chose SQLite.\n",
                        "0002-PROPOSED-drop-redis.md": "# Drop Redis\n\nRedis goes.\n",
                        "0003-ACCEPTED-wal.md": "# WAL\n\nWrite-ahead logging.\n"})
    monkeypatch.setattr(api, "load", lambda: [api.Loaded("design", design.module)])
    return d


async def settled(app, pilot):
    """The pane's asks answered, after a tick: the first one, as it mounts, comes before a
    hub in process has read its sessions, so the tick asks again once it lands."""
    await pilot.pause()
    app.each_pane("tick")
    p = app.query_one("#design")
    for _ in range(50):
        await app.workers.wait_for_complete()
        await pilot.pause()
        if not p.asking:
            break
    await pilot.pause()


def numbers(app) -> list[str]:
    table = app.query_one("#decision-list")
    return [table.get_row_at(i)[0] for i in range(table.row_count)]


@pytest.mark.anyio
@pytest.mark.both
async def test_the_pane_lists_and_shows_decisions(store, sid, decisions):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        p = app.query_one("#design")
        assert p.parent.id == "slot-middle-bottom" and p.border_title == "Decisions", "its slot and title"
        assert numbers(app) == ["0002", "0003", "0001"], "PROPOSED first, then newest first"
        before = app._detail_text
        table = app.query_one("#decision-list")
        table.focus()
        await pilot.pause()
        assert app._detail_text == before, "focus alone shows nothing"
        await pilot.press("down")
        await pilot.pause()
        assert "Write-ahead logging." in app._detail_text and "0003 WAL" in app._detail_text, "the cursor's decision"
        assert not app.query_one("#answer").display, "a doc: no answer box"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DocView) and "Write-ahead logging." in app.screen.text, "Enter: full screen"
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, DocView)


@pytest.mark.anyio
async def test_a_header_sorts_and_a_new_decision_shows(store, sid, decisions):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        table = app.query_one("#decision-list")
        column = table.ordered_columns[0]
        table.post_message(table.HeaderSelected(table, column.key, 0, column.label))
        await pilot.pause()
        assert numbers(app) == ["0001", "0002", "0003"] and str(table.ordered_columns[0].label) == "# ▲", \
            "number ▲, marked in its header"
        (decisions / "0004-UNRESOLVED-x.md").write_text("# X\n")
        p = app.query_one("#design")
        for _ in range(pane.EVERY):
            p.tick()
        await settled(app, pilot)
        assert numbers(app) == ["0001", "0002", "0003", "0004"], "the next ask, EVERY ticks on, finds it"


@pytest.mark.anyio
async def test_tab_stops_at_the_decisions_after_the_inbox(store, sid, decisions):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        ids = [w.id for w, _ in tab_stops(app.screen)]
        assert ids[ids.index("items") + 1] == "decision-list", "after the inbox"
        assert ids.index("decision-list") < ids.index("answer"), "before the answer box"


@pytest.mark.anyio
async def test_no_decisions_says_where_it_looked(store, sid, tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(api, "load", lambda: [api.Loaded("design", design.module)])
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        note = app.query_one("#decisions-note")
        assert note.display and "docs/adr" in str(note.render()), "the empty pane says why"
