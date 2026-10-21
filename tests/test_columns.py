"""A column's slots share its rows by one rule (columns.py, #111 review 2): the pure parts by
table, then the app's columns in each arrangement, after drags and at two terminal sizes."""

import json

import pytest

from claude_wheelhouse import api, columns
from claude_wheelhouse.tui import LAYOUT, SLOTS_KEY, WheelhouseApp

pytest.importorskip("claude_wheelhouse_prs", reason="installed without the prs module")
pytest.importorskip("claude_wheelhouse_design", reason="installed without the design module")
from conftest import SHIPPED_LOAD  # noqa: E402


# the rule, without Textual

@pytest.mark.parametrize("room, weights, minimums, expected, desc", [
    (35, [1, 1, 1], [3, 3, 3], [12, 12, 11], "equal weights: the spare row to the first"),
    (35, [12.8, 11.1, 11.1], [3, 1, 12], [12, 11, 12], "the stylesheet's at 40 rows: a minimum taken first"),
    (55, [20.8, 17.1, 17.1], [3, 1, 12], [21, 17, 17], "the stylesheet's at 60 rows"),
    (10, [1, 0], [3, 3], [7, 3], "a weight of nothing: its minimum"),
    (5, [1, 1], [3, 3], [3, 3], "minimums past the room: each its minimum"),
    (20, [], [], [], "no slots"),
    (9, [float("nan"), 1], [1, 1], [1, 8], "a weight that isn't a number: none"),
])
def test_rows(room, weights, minimums, expected, desc):
    got = columns.rows(room, weights, minimums)
    assert got == expected, desc
    assert not weights or sum(got) == room or sum(minimums) >= room, f"{desc}: the column filled"


T, M, B = "middle.top", "middle.middle", "middle.bottom"


@pytest.mark.parametrize("weighted, expected, desc", [
    ({T, M, B}, [(T, M), (M, B)], "all three: a splitter between each"),
    ({T, B}, [None, (T, B)], "the middle empty or compact: one, before the bottom, joining top and bottom"),
    ({T, M}, [(T, M), None], "the Decisions absent: the PRs' splitter alone (review 1, bug 1)"),
    ({M, B}, [None, (M, B)], "the top empty"),
    ({B}, [None, None], "one slot: none"),
])
def test_pairs(weighted, expected, desc):
    assert columns.pairs([T, M, B], weighted) == expected, desc


H = {T: 14, M: 7, B: 14}
LEAST = {T: 3, M: 3, B: 3}


@pytest.mark.parametrize("above, below, delta, expected, desc", [
    (T, M, 2, {T: 16, M: 5, B: 14}, "down: the inbox gains what the PRs give"),
    (T, M, -4, {T: 10, M: 11, B: 14}, "up"),
    (M, B, 3, {T: 14, M: 10, B: 11}, "the lower splitter: the Decisions give, the inbox untouched"),
    (T, M, 9, {T: 18, M: 3, B: 14}, "never under a minimum"),
    (T, B, -20, {T: 3, M: 7, B: 25}, "across a hidden slot, to the inbox's minimum"),
])
def test_dragged(above, below, delta, expected, desc):
    assert columns.dragged(H, above, below, delta, LEAST) == expected, desc


@pytest.mark.parametrize("slots, kept, styled, expected, desc", [
    ([T, B], {}, [17.5, 17.5], [0.4, 0.4], "none kept: the stylesheet's rows, in DEFAULT's scale"),
    ([T, M, B], {}, [14, 7, 14], [0.4, 0.2, 0.4], "all three, none kept"),
    ([T, B], {T: 0.6, B: 0.2}, [17.5, 17.5], [0.6, 0.2], "kept: as kept"),
    ([T, M, B], {T: 0.5, M: 0.1}, [14, 7, 14], [0.5, 0.1, 0.4], "a slot with none kept: its DEFAULT, the same unit"),
])
def test_weights(slots, kept, styled, expected, desc):
    assert columns.weights(slots, kept, styled) == pytest.approx(expected), desc


@pytest.mark.parametrize("rows_, total, expected, desc", [
    ({T: 16, M: 5, B: 14}, 1.0, {T: 16 / 35, M: 5 / 35, B: 14 / 35}, "in proportion to the rows, the total kept"),
    ({T: 26, M: 7}, 0.6, {T: 0.6 * 26 / 33, M: 0.6 * 7 / 33}, "two of three shown: their DEFAULT total"),
])
def test_as_weights(rows_, total, expected, desc):
    got = columns.as_weights(rows_, total)
    assert got == pytest.approx(expected) and sum(got.values()) == pytest.approx(total), desc
    assert columns.rows(sum(rows_.values()), list(got.values()), [1] * len(got)) == list(rows_.values()), \
        f"{desc}: the same rows back"


# the app's columns

SIZES = [(160, 40), (80, 24)]


def modules(names):
    return lambda entries=None: [m for m in SHIPPED_LOAD(entries) if m.name in names]


async def opened(app, pilot):
    for _ in range(4):
        await pilot.pause()
    app.relaid()
    for _ in range(3):
        await pilot.pause()


def filled(app) -> bool:
    """Every column shown filled by its children to the row."""
    return all(sum(w.outer_size.height for w in c.children if w.display) == c.size.height
               for c in app.query(".column") if c.display)


def shown_splitters(app, column: str) -> set[str]:
    return {s.key for s in app.query(f"#{column} > SlotSplitter") if s.display}


@pytest.mark.anyio
@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("loaded, saved, adrs, middle, splitters, desc", [
    ({"design", "prs", "stats"}, {}, True, ["items", "prs", "design"], {"middle-middle", "middle-bottom"},
     "every module, decisions found: three slots, two splitters"),
    ({"design", "prs", "stats"}, {}, False, ["items", "prs", "design"], {"middle-middle"},
     "the Decisions compact: no splitter beside them"),
    ({"prs", "stats"}, {}, False, ["items", "prs"], {"middle-middle"}, "the Decisions absent (review 1, bug 1)"),
    ({"design", "stats"}, {}, True, ["items", "design"], {"middle-bottom"}, "the PRs absent: as before #111"),
    ({"stats"}, {}, False, ["items"], set(), "neither: the inbox alone"),
    ({"design", "stats"}, {"middle.top": "prs", "middle.middle": "items"}, True, ["items", "design"],
     {"middle-bottom"}, "a saved map naming the absent PRs on top: the inbox lower down, the column filled"),
])
async def test_arrangements_fill_their_columns(store, sid, tmp_path, monkeypatch, size, loaded, saved, adrs,
                                               middle, splitters, desc):
    (tmp_path / ".git").mkdir()
    if adrs:
        (tmp_path / "docs" / "adr").mkdir(parents=True)
        (tmp_path / "docs" / "adr" / "0001-a.md").write_text("# 1. A\n\nStatus: accepted\n")
    monkeypatch.setattr(api, "load", modules(loaded))
    if saved:
        store.set_setting(SLOTS_KEY, json.dumps(saved))
    app = WheelhouseApp(store)
    async with app.run_test(size=size) as pilot:
        await opened(app, pilot)
        if "design" in loaded:
            design = app.query_one("#design")
            for _ in range(50):
                if design.compact is not adrs:
                    break
                await app.workers.wait_for_complete()
                await pilot.pause()
            await opened(app, pilot)
        panes = [w.children[0].id.removesuffix("-pane") for w in app.query("#col-middle > .slot") if w.display]
        assert panes == middle, f"{desc} at {size}"
        assert shown_splitters(app, "col-middle") == splitters, f"{desc} at {size}"
        assert filled(app), f"{desc} at {size}: no gaps"
        inbox = app.query_one("#items-pane").region.height
        assert inbox >= min(12, app.query_one("#col-middle").size.height // 3), f"{desc} at {size}: the inbox never squeezed ({inbox})"


@pytest.mark.anyio
@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("key, dy, desc", [
    ("middle-middle", 2, "the inbox and PRs' line, down"),
    ("middle-middle", -2, "up"),
    ("middle-bottom", 2, "the PRs and Decisions' line, down"),
    ("middle-bottom", -2, "up"),
    ("left-middle", 2, "the sessions and stats' line, down"),
    ("left-bottom", -1, "the stats and session info's line, up"),
])
async def test_a_drag_moves_its_line_with_the_pointer(store, sid, tmp_path, monkeypatch, size, key, dy, desc):
    """Only the splitter's two slots change, by as many rows as the pointer moved; kept, and
    as it was left on the next run."""
    from test_tui import drag, splitter
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "adr").mkdir(parents=True)
    (tmp_path / "docs" / "adr" / "0001-a.md").write_text("# 1. A\n\nStatus: accepted\n")
    monkeypatch.setattr(api, "load", modules({"design", "prs", "stats"}))
    app = WheelhouseApp(store)
    async with app.run_test(size=size) as pilot:
        await opened(app, pilot)
        design = app.query_one("#design")
        for _ in range(50):
            if not design.compact:
                break
            await app.workers.wait_for_complete()
            await pilot.pause()
        await opened(app, pilot)
        sp = splitter(app, key)
        assert sp.display, f"{desc} at {size}: shown"
        below, above = sp.pair
        column = sp.column
        before = {s.name: s.outer_size.height for s in app.query(f"#{column} > .slot") if s.display}
        at = sp.region.y
        # to the pointer, but neither slot under its least rows (columns.dragged)
        from claude_wheelhouse.tui import least
        moved = max(least(above) - before[above.name], min(dy, before[below.name] - least(below)))
        assert moved, f"{desc} at {size}: room to move"
        await drag(pilot, key, 0, dy)
        await pilot.pause()
        after = {s.name: s.outer_size.height for s in app.query(f"#{column} > .slot") if s.display}
        assert sp.region.y == at + moved, f"{desc} at {size}: the line follows the pointer"
        assert after[above.name] == before[above.name] + moved and after[below.name] == before[below.name] - moved, desc
        assert {k: v for k, v in after.items() if k not in (above.name, below.name)} == \
               {k: v for k, v in before.items() if k not in (above.name, below.name)}, f"{desc}: the rest unmoved"
        assert filled(app), desc
    assert store.setting(LAYOUT + "weights"), f"{desc}: kept"
    app = WheelhouseApp(store)
    async with app.run_test(size=size) as pilot:
        await opened(app, pilot)
        again = {s.name: s.outer_size.height for s in app.query(f"#{column} > .slot") if s.display}
        assert again[above.name] == after[above.name], f"{desc}: as it was left, next run"


async def adrs_found(app, pilot, found: bool) -> None:
    design = app.query_one("#design")
    for _ in range(50):
        if design.compact is not found:
            break
        await app.workers.wait_for_complete()
        await pilot.pause()
    await opened(app, pilot)


def middle_rows(app) -> list[int]:
    return [app.query_one(f"#slot-{n}").outer_size.height if app.query_one(f"#slot-{n}").display else 0
            for n in ("middle-top", "middle-middle", "middle-bottom")]


@pytest.mark.anyio
@pytest.mark.parametrize("stored, desc", [
    ("0.3", "s 0.3"), ("0", "s 0"), ("1", "s 1"), ("junk", "garbage"), (None, "missing"),
])
async def test_old_shares_are_dropped_for_the_defaults(store, sid, tmp_path, monkeypatch, stored, desc):
    """The splitters' shares kept before #111 are deleted on start (review 3): the column as
    a person who never dragged sees it, whatever was kept."""
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(api, "load", modules({"design", "prs", "stats"}))
    for key in columns.OLD_KEYS:
        if stored is not None:
            store.set_setting(LAYOUT + key, stored)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await opened(app, pilot)
        await adrs_found(app, pilot, False)
        got = middle_rows(app)
        assert got[2] == 3 and got[0] > 3 * got[1] and filled(app), f"{desc}: the default, the inbox with the room ({got})"
    assert all(store.setting(LAYOUT + key) is None for key in columns.OLD_KEYS), f"{desc}: the old keys gone"
    assert store.setting(LAYOUT + "weights") is None, f"{desc}: nothing kept in their place"


@pytest.mark.anyio
@pytest.mark.parametrize("absent, later, desc", [
    ("design-empty", "design", "the Decisions compact during the drag, then decisions appear (review 3 repro 1)"),
    ("prs", "prs", "the PRs absent during the drag, then loaded (review 3 repro 2)"),
])
async def test_a_slot_out_of_a_drag_comes_back_at_its_default(store, sid, tmp_path, monkeypatch, absent, later, desc):
    """Weights are in one unit: a drag changes only its two slots', so a slot compact or absent
    meanwhile comes back with the rows its default gives, not squeezed."""
    from test_tui import drag
    (tmp_path / ".git").mkdir()
    store.set_setting(LAYOUT + "middle-top", "0.5")   # an old share, dropped on start
    # the default with every pane shown, decisions found: the rows the slot comes back to
    adr = tmp_path / "docs" / "adr"
    adr.mkdir(parents=True)
    (adr / "0001-a.md").write_text("# 1. A\n\nStatus: accepted\n")
    monkeypatch.setattr(api, "load", modules({"design", "prs", "stats"}))
    default = {}
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await opened(app, pilot)
        await adrs_found(app, pilot, True)
        default = dict(zip(("items", "prs", "design"), middle_rows(app)))
    store.clear_setting(LAYOUT + "weights")
    if absent == "design-empty":
        (adr / "0001-a.md").unlink()
    monkeypatch.setattr(api, "load", modules({"design", "stats"} if absent == "prs" else {"design", "prs", "stats"}))
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await opened(app, pilot)
        await adrs_found(app, pilot, absent == "prs")
        key = "middle-middle" if absent == "design-empty" else "middle-bottom"
        await drag(pilot, key, 0, 2)
        await pilot.pause()
    kept = json.loads(store.setting(LAYOUT + "weights"))
    assert all(v < 2 for v in kept.values()), f"{desc}: one unit, DEFAULT's ({kept})"
    if absent == "design-empty":
        (adr / "0001-a.md").write_text("# 1. A\n\nStatus: accepted\n")
    monkeypatch.setattr(api, "load", modules({"design", "prs", "stats"}))
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await opened(app, pilot)
        await adrs_found(app, pilot, True)
        back = dict(zip(("items", "prs", "design"), middle_rows(app)))
        assert abs(back[later] - default[later]) <= 1, f"{desc}: {later} at {back[later]} rows, its default {default[later]}"
        assert filled(app), desc
