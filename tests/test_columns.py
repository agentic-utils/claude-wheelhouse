"""A column's slots share its rows by one rule (columns.py, #111): the pure parts by table,
the rule's invariants by property over random sequences of drags, slots compacting,
expanding, hiding and showing, resizes, resets and reloads, then the app's columns in each
arrangement, after drags and at two terminal sizes."""

import json

import pytest
from hypothesis import given, settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule

from claude_wheelhouse import api, columns
from claude_wheelhouse.columns import Slot
from claude_wheelhouse.tui import LAYOUT, SLOTS_KEY, WheelhouseApp


# the rule, without Textual

@pytest.mark.parametrize("room, weights, minimums, expected, desc", [
    (35, [1, 1, 1], [3, 3, 3], [12, 11, 12], "equal weights: each line on its nearest row"),
    (35, [0.4, 0.3, 0.3], [5, 1, 12], [13, 10, 12], "the left column at 40 rows: session info at its least"),
    (35, [0.4, 0.2, 0.4], [3, 3, 3], [14, 7, 14], "the middle column at 40 rows, every slot shown"),
    (33, [0.4, 0.2], [3, 3], [22, 11], "an empty Decisions: its room shared in proportion"),
    (5, [1, 1], [3, 3], [3, 3], "minimums past the room: each its minimum"),
    (6, [1, 1], [3, 3], [3, 3], "minimums the room exactly"),
    (20, [], [], [], "no slots"),
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


def middle(top=None, mid=None, bottom=None) -> list[Slot]:
    """The middle column, each slot weighted (None), compact at its rows, or hidden (0)."""
    return [Slot(T, 3, top), Slot(M, 3, mid), Slot(B, 3, bottom)]


@pytest.mark.parametrize("slots, kept, expected, desc", [
    (middle(), {}, {T: 14, M: 7, B: 14}, "every slot shown: DEFAULT's shares"),
    (middle(bottom=3), {}, {T: 22, M: 11, B: 3}, "an empty Decisions at its note, its room shared 2:1"),
    (middle(mid=0), {}, {T: 18, M: 0, B: 18}, "the PRs absent: halves, as before #111"),
    (middle(mid=0, bottom=3), {}, {T: 34, M: 0, B: 3}, "the inbox alone beside an empty Decisions, no splitter"),
    (middle(), {T: 0.6, M: 0.2, B: 0.2}, {T: 21, M: 7, B: 7}, "kept weights"),
])
def test_lay(slots, kept, expected, desc):
    assert columns.lay(37, slots, kept) == expected, desc


@pytest.mark.parametrize("slots, kept, above, below, delta, expected, desc", [
    (middle(), {}, T, M, 2, {T: 16, M: 5, B: 14}, "down: the inbox gains what the PRs give"),
    (middle(), {}, T, M, -4, {T: 10, M: 11, B: 14}, "up"),
    (middle(), {}, M, B, 3, {T: 14, M: 10, B: 11}, "the lower splitter: the Decisions give, the inbox untouched"),
    (middle(), {}, T, M, 9, {T: 18, M: 3, B: 14}, "never under a minimum"),
    (middle(mid=4), {}, T, B, -20, {T: 3, M: 4, B: 29}, "across a compact slot, to the inbox's minimum"),
    (middle(bottom=3), {}, T, M, -1, {T: 21, M: 12, B: 3}, "beside an empty Decisions (review 4)"),
])
def test_dragged(slots, kept, above, below, delta, expected, desc):
    to = columns.dragged(37, slots, kept, above, below, delta)
    assert columns.lay(37, slots, to) == expected, desc
    assert set(to) - set(kept) <= {above, below}, f"{desc}: only the pair's weights set"


@pytest.mark.parametrize("after, expected, desc", [
    (middle(), {T: 13, M: 8, B: 14}, "the Decisions back: the PRs keep the row they were given, the inbox gives"),
    (middle(mid=0), {T: 18, M: 0, B: 18}, "the PRs gone too: the inbox no bigger for giving them a row"),
])
def test_a_drag_beside_a_compact_slot_keeps_its_direction(after, expected, desc):
    """Review 4's bug: at 160x40, the PRs dragged a row bigger while the Decisions were
    empty, then decisions found: the PRs went to 5 rows (16/5/14), the way the drag hadn't."""
    to = columns.dragged(37, middle(bottom=3), {}, T, M, -1)
    assert columns.lay(37, after, to) == expected, desc


@pytest.mark.parametrize("height, desc", [(37, "at 40 rows"), (57, "at 60"), (90, "at 93")])
def test_a_drag_on_a_short_column_leaves_the_others_their_shares(height, desc):
    """Review 5: on a short column the Sessions and the Session info are held at their least;
    a drag between Sessions and Stats keeps the pair's weights' sum, so the Session info,
    outside it, has its default rows at any size (it had 20 of 26 at 93 rows)."""
    left = [Slot("left.top", 6), Slot("left.middle", 1), Slot("left.bottom", 12)]
    to = columns.dragged(23, left, {}, "left.top", "left.middle", 1)
    assert columns.lay(23, left, to) == {"left.top": 7, "left.middle": 2, "left.bottom": 12}, "dragged a row"
    assert to["left.top"] + to["left.middle"] == pytest.approx(0.7), "the pair's sum kept"
    assert columns.lay(height, left, to)["left.bottom"] == columns.lay(height, left, {})["left.bottom"], desc


@pytest.mark.parametrize("raw, expected, desc", [
    ('{"middle.top": 0.5, "right": 1}', {T: 0.5, "right": 1.0}, "as kept"),
    ('{"middle.top": 0, "middle.middle": -1, "x": 1, "left.top": true, "right": "1"}', {}, "nothing usable"),
    ('{"middle.top": NaN}', {}, "not a number"),
    ("[1]", {}, "not an object"),
    ("junk", {}, "garbage"),
    (None, {}, "none"),
])
def test_kept(raw, expected, desc):
    assert columns.kept(raw) == expected, desc


# the rule's invariants, over random sequences (hypothesis)

COLUMNS = {"left": ["left.top", "left.middle", "left.bottom"], "middle": [T, M, B]}


def reals(height: int, slots: list[Slot], kept: dict) -> dict[str, float]:
    """The weighted slots' unrounded rows, by name."""
    return state(height, slots, kept)[0]


def state(height: int, slots: list[Slot], kept: dict) -> tuple[dict[str, float], set[str]]:
    """The weighted slots' unrounded rows, by name, and those held at their least rows (their
    weight's share under it)."""
    weighted = [s for s in slots if s.own is None]
    ws = columns.weights(slots, kept)
    real, scale = columns.shares(columns.room(height, slots), ws, [s.least for s in weighted])
    held = {s.name for s, w in zip(weighted, ws) if w * scale < s.least - 1e-9}
    return dict(zip([s.name for s in weighted], real)), held


def fits(height: int, slots: list[Slot]) -> bool:
    return sum(s.least for s in slots if s.own is None) <= columns.room(height, slots)




class Column(RuleBasedStateMachine):
    """A column under a person's hands: every step leaves it filled, each weighted slot at
    its least rows at least, the weights in one scale and written only by a drag or a
    reset; and each drag, compact and resize keeps the invariants of its own."""

    @initialize(names=st.sampled_from(sorted(COLUMNS)), least=st.lists(st.integers(1, 12), min_size=3, max_size=3),
                height=st.integers(4, 90))
    def start(self, names, least, height):
        self.names, self.least, self.height = COLUMNS[names], least, height
        self.own: list[int | None] = [None, None, None]
        self.kept: dict[str, float] = {}

    def slots(self, own=None) -> list[Slot]:
        own = self.own if own is None else own
        return [Slot(n, m, o) for n, m, o in zip(self.names, self.least, own)]

    def splitters(self) -> list[tuple[str, str]]:
        slots = self.slots()
        return [p for p in columns.pairs(self.names, {s.name for s in slots if s.own is None}) if p]

    def other_states(self, data) -> tuple[int, list[Slot]]:
        own = [data.draw(st.one_of(st.none(), st.integers(0, m)), label="other state") for m in self.least]
        return data.draw(st.integers(4, 90), label="other height"), self.slots(own)

    @invariant()
    def laid_out_by_the_rule(self):
        if not hasattr(self, "kept"):
            return
        slots, before = self.slots(), dict(self.kept)
        got = columns.lay(self.height, slots, self.kept)
        assert self.kept == before, "laying out writes no weight"
        assert all(n >= 0 for n in got.values()), f"no slot under no rows: {got}"
        weighted = [s for s in slots if s.own is None]
        if not weighted:
            return
        if fits(self.height, slots):
            shown = len(self.splitters())
            assert sum(got.values()) + shown == self.height, f"the column filled to the row: {got}"
            assert all(got[s.name] >= s.least for s in weighted), f"each at its least at least: {got}"
            real = reals(self.height, slots, self.kept)
            ws = dict(zip([s.name for s in weighted], columns.weights(slots, self.kept)))
            free = [n for n, r in real.items() if r > self.least[self.names.index(n)] + 1e-9]
            scales = [real[n] / ws[n] for n in free]
            assert max(scales, default=1) - min(scales, default=1) <= 1e-9 * max(scales, default=1), \
                f"one unit: every free slot's rows its weight times one scale ({scales})"

    @rule(data=st.data())
    @precondition(lambda self: self.splitters())
    def drag(self, data):
        above, below = data.draw(st.sampled_from(self.splitters()), label="splitter")
        delta = data.draw(st.integers(-40, 40), label="delta")
        slots = self.slots()
        before = columns.lay(self.height, slots, self.kept)
        to = columns.dragged(self.height, slots, self.kept, above, below, delta)
        after = columns.lay(self.height, slots, to)
        assert {k: v for k, v in to.items() if k not in (above, below)} == \
               {k: v for k, v in self.kept.items() if k not in (above, below)}, "only the pair's weights written"
        assert {k: v for k, v in after.items() if k not in (above, below)} == \
               {k: v for k, v in before.items() if k not in (above, below)}, "every other slot keeps its rows"
        a, b = before[above], before[below]
        least_a, least_b = (self.least[self.names.index(n)] for n in (above, below))
        if fits(self.height, slots):
            expected = min(max(a + delta, least_a), a + b - least_b)
            assert after[above] == expected and after[below] == a + b - expected, \
                f"the line on the pointer's row, or where one reaches its least ({a}+{delta} -> {after[above]})"
        assert columns.dragged(self.height, slots, self.kept, above, below, 0) == self.kept, "back at the start: as it began"
        grown, shrunk = (above, below) if after[above] > a else (below, above)
        if after[above] != a:
            ws = dict(zip([s.name for s in slots if s.own is None], columns.weights(slots, self.kept)))
            total, now_total = ws[above] + ws[below], to[above] + to[below]
            real, held = state(self.height, slots, self.kept)
            least = dict(zip(self.names, self.least))
            others = set(real) - {above, below}
            # the scale held by another slot: one sharing the room, or one the pair's own scale would free
            scaled = any(real[o] > least[o] + 1e-9 or least[o] / ws[o] < (real[above] + real[below]) / total
                         for o in others)
            assert now_total >= total * (1 - 1e-9), "the pair's weights never lose weight to the others"
            # by the state the drag began in, not by what dragged() gave: the sum is kept unless one
            # of the pair was held at its least and another slot holds the scale
            kept_sum = not (held & {above, below} and scaled)
            assert not kept_sum or abs(now_total - total) <= 1e-9 * total, \
                "the pair's weights keep their sum, unless one was held at its least and another holds the scale"
            for _ in range(4):   # never reversed, in any other state at any height, nor the others moved
                height, other = self.other_states(data)
                (old, held_old), (new, held_new) = state(height, other, self.kept), state(height, other, to)
                if grown in new:
                    assert new[grown] >= old[grown] - 1e-9, f"{grown} grown here, never smaller elsewhere"
                if shrunk in new:
                    assert new[shrunk] <= old[shrunk] + 1e-9, f"{shrunk} shrunk here, never bigger elsewhere"
                was, now = columns.lay(height, other, self.kept), columns.lay(height, other, to)
                assert now[grown] >= was[grown] - 1 and now[shrunk] <= was[shrunk] + 1, "nor by more than a row's rounding"
                if {above, below} <= set(new) and not (held_old | held_new) & {above, below}:
                    for name in set(new) - {above, below}:   # the others: their shares, at any size
                        assert new[name] <= old[name] + 1e-9, f"{name}, outside the pair, never grown by it"
                        if kept_sum:
                            assert abs(new[name] - old[name]) <= 1e-6 * max(old[name], 1) and \
                                abs(now[name] - was[name]) <= 1, f"{name}, outside the pair, keeps its rows"
        self.kept = to

    @rule(data=st.data(), slot=st.integers(0, 2))
    def compact_or_hide(self, data, slot):
        """A slot compacting or hiding takes no room from another (the inbox never squeezed)."""
        if self.own[slot] is not None or sum(o is None for o in self.own) < 2:
            return
        own = list(self.own)
        own[slot] = data.draw(st.integers(0, self.least[slot]), label="compact (its panes' least) or hidden (0)")
        before, after = self.slots(), self.slots(own)
        old, new = reals(self.height, before, self.kept), reals(self.height, after, self.kept)
        was, now = columns.lay(self.height, before, self.kept), columns.lay(self.height, after, self.kept)
        for name in new:
            assert new[name] >= old[name] - 1e-9 and now[name] >= was[name] - 1, f"{name} never squeezed"
        self.own = own

    @rule(slot=st.integers(0, 2))
    def expand_or_show(self, slot):
        self.own = [None if i == slot else o for i, o in enumerate(self.own)]

    @rule(slot=st.integers(0, 2), weight=st.floats(0.01, 5))
    def weighed_elsewhere(self, slot, weight):
        """A weight kept by another run, at another size: any weight holds the rule."""
        self.kept = {**self.kept, self.names[slot]: weight}

    @rule(height=st.integers(4, 90))
    def resize(self, height):
        self.height = height

    @rule()
    def reset(self):
        """A double-click: the column's weights dropped, DEFAULT's layout."""
        self.kept = {k: v for k, v in self.kept.items() if k not in self.names}
        slots = self.slots()
        weighted = [s.name for s in slots if s.own is None]
        assert columns.weights(slots, self.kept) == [columns.DEFAULT[n] for n in weighted], "DEFAULT's weights"
        real, held = state(self.height, slots, self.kept)
        free = [n for n in weighted if n not in held]
        if fits(self.height, slots) and free:
            k = real[free[0]] / columns.DEFAULT[free[0]]
            assert all(abs(real[n] - k * columns.DEFAULT[n]) <= 1e-9 * real[n] for n in free), \
                f"each not held at its least has its DEFAULT share: {real}"

    @rule()
    def reload(self):
        """Kept and read back on the next run: the same weights, the same rows."""
        again = columns.kept(json.dumps(self.kept))
        assert again == self.kept, "the weights round-trip"
        assert columns.lay(self.height, self.slots(), again) == columns.lay(self.height, self.slots(), self.kept)
        self.kept = again


TestColumn = Column.TestCase
TestColumn.settings = settings(max_examples=300, stateful_step_count=40, deadline=None)


@settings(max_examples=500, deadline=None)
@given(room=st.integers(0, 200), weights=st.lists(st.floats(0.001, 10), min_size=1, max_size=5), data=st.data())
def test_rows_fill_the_room_and_keep_the_minimums(room, weights, data):
    minimums = data.draw(st.lists(st.integers(0, 20), min_size=len(weights), max_size=len(weights)))
    got = columns.rows(room, weights, minimums)
    if sum(minimums) <= room:
        assert sum(got) == room and all(n >= m for n, m in zip(got, minimums)), (got, minimums)
        real, _ = columns.shares(room, weights, minimums)
        assert all(abs(n - r) < 1 for n, r in zip(got, real)), f"each within a row of its share: {got} {real}"
    else:
        assert got == minimums


# the app's columns

SIZES = [(160, 40), (80, 24)]


def modules(names):
    """The app's modules, those named: a test of the app's columns skips without them installed."""
    pytest.importorskip("claude_wheelhouse_prs", reason="installed without the prs module")
    pytest.importorskip("claude_wheelhouse_design", reason="installed without the design module")
    from conftest import SHIPPED_LOAD
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
        await drag(pilot, key, 0, dy)
        await pilot.pause()
        after = {s.name: s.outer_size.height for s in app.query(f"#{column} > .slot") if s.display}
        if not moved:   # one of the two at its least rows already (the PRs, at 80 by 24)
            assert size != (160, 40), f"{desc} at {size}: room to move"
            assert after == before and sp.region.y == at, f"{desc} at {size}: nothing moves"
            return
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
        assert got == [22, 11, 3] and filled(app), f"{desc}: the default, the empty Decisions' room shared 2:1 ({got})"
    assert all(store.setting(LAYOUT + key) is None for key in columns.OLD_KEYS), f"{desc}: the old keys gone"
    assert store.setting(LAYOUT + "weights") is None, f"{desc}: nothing kept in their place"


@pytest.mark.anyio
async def test_slot_splitters_cost_no_settings_round_trips(store, sid, monkeypatch):
    """A column's weights size its SlotSplitters (#134): start reads their old keys only to
    delete them, and Reset layout neither reads nor writes them."""
    calls = []
    for name in ("setting", "set_setting"):
        monkeypatch.setattr(store, name, lambda key, *a, f=getattr(store, name): calls.append(key) or f(key, *a))
    slot_keys = {LAYOUT + key for key in columns.OLD_KEYS}
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await opened(app, pilot)
        assert sorted(k for k in calls if k in slot_keys) == sorted(slot_keys), "start: each read once, to delete"
        calls.clear()
        app.action_reset_layout()
        await pilot.pause()
        assert not [k for k in calls if k in slot_keys], "reset: none"


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


@pytest.mark.anyio
@pytest.mark.parametrize("dy, desc", [
    (-1, "the PRs a row bigger (review 4: 16/5/14, the PRs smaller than their default)"),
    (3, "the PRs three rows smaller"),
])
async def test_a_drag_beside_an_empty_decisions_keeps_its_direction_once_they_fill(store, sid, tmp_path, monkeypatch,
                                                                                    dy, desc):
    """The PRs' line dragged while the Decisions are empty, then a decision found, in the same
    run: the PRs keep the drag's direction against their default, the Decisions come back at
    theirs, and the inbox gives or takes the difference."""
    from test_tui import drag
    from test_decisions_pane import settled
    from claude_wheelhouse_design import pane
    (tmp_path / ".git").mkdir()
    adr = tmp_path / "docs" / "adr"
    adr.mkdir(parents=True)
    (adr / "0001-a.md").write_text("# 1. A\n\nStatus: accepted\n")
    monkeypatch.setattr(api, "load", modules({"design", "prs", "stats"}))
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await opened(app, pilot)
        await adrs_found(app, pilot, True)
        default = middle_rows(app)
    (adr / "0001-a.md").unlink()
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await opened(app, pilot)
        await adrs_found(app, pilot, False)
        empty = middle_rows(app)
        await drag(pilot, "middle-middle", 0, dy)
        dragged_ = middle_rows(app)
        assert dragged_[1] == empty[1] - dy and dragged_[2] == 3, f"{desc}: dragged ({empty} -> {dragged_})"
        (adr / "0001-a.md").write_text("# 1. A\n\nStatus: accepted\n")
        app.query_one("#design").asked_at -= pane.EVERY
        await settled(app, pilot)
        await opened(app, pilot)
        back = middle_rows(app)
        grew = dy < 0
        assert (back[1] > default[1]) if grew else (back[1] < default[1]), \
            f"{desc}: the PRs {back[1]} rows, their default {default[1]}"
        assert (back[0] < default[0]) if grew else (back[0] > default[0]), f"{desc}: the inbox gives or takes ({back})"
        assert back[2] == default[2] and filled(app), f"{desc}: the Decisions at their default ({back}, {default})"
