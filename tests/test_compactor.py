"""Tests for the compaction-cycle crew and droid excerpts."""

import random
import re

import pytest
from rich.cells import cell_len

from claude_wheelhouse import compactor

LINE_RE = re.compile(r"^(Luke|Leia|Han|Chewie|R2-D2|C-3PO): \S")
ALL_LINES = [line for excerpt in compactor.EXCERPTS for line in excerpt]


def speaker(line: str) -> str:
    return line.split(":", 1)[0]


def test_pool_is_big_enough():
    assert len(compactor.EXCERPTS) >= 90, f"only {len(compactor.EXCERPTS)} excerpts"


def test_no_duplicate_excerpts():
    assert len(set(compactor.EXCERPTS)) == len(compactor.EXCERPTS), "duplicate excerpt"


def test_no_duplicate_lines():
    dupes = {line for line in ALL_LINES if ALL_LINES.count(line) > 1}
    assert not dupes, f"duplicate lines: {dupes}"


@pytest.mark.parametrize("excerpt", compactor.EXCERPTS)
def test_excerpt_shape(excerpt):
    assert len(excerpt) in (1, 2), f"excerpt must have 1 or 2 lines: {excerpt}"
    if len(excerpt) == 2:
        assert speaker(excerpt[0]) != speaker(excerpt[1]), f"same speaker twice: {excerpt}"


@pytest.mark.parametrize("line", ALL_LINES)
def test_line_format(line):
    assert cell_len(line) <= 76, f"{cell_len(line)} cells: {line}"
    assert LINE_RE.match(line), f"bad format: {line}"
    assert "—" not in line and "–" not in line, f"dash in: {line}"


@pytest.mark.parametrize("seed", [0, 1, 7, 42, 2026])
def test_full_pass_has_no_repeats(seed):
    excerpts = compactor.Excerpts(random.Random(seed))
    dealt = [excerpts.next() for _ in compactor.EXCERPTS]
    assert len(set(dealt)) == len(compactor.EXCERPTS), f"repeat within a pass, seed {seed}"


def test_second_pass_continues():
    excerpts = compactor.Excerpts(random.Random(3))
    dealt = [excerpts.next() for _ in range(len(compactor.EXCERPTS) * 2)]
    assert set(dealt) == set(compactor.EXCERPTS), "second pass should draw from the pool"


def test_seeds_give_different_orders():
    a = compactor.Excerpts(random.Random(1))
    b = compactor.Excerpts(random.Random(2))
    n = len(compactor.EXCERPTS)
    assert [a.next() for _ in range(n)] != [b.next() for _ in range(n)], "same order"


def test_default_rng_works():
    assert compactor.Excerpts().next() in compactor.EXCERPTS, "unseeded instance failed"


@pytest.mark.parametrize("seed", range(20))
def test_gap_within_cadence(seed):
    lo, hi = compactor.CADENCE
    value = compactor.gap(random.Random(seed))
    assert lo <= value <= hi, f"gap {value} outside {compactor.CADENCE}"


@pytest.mark.parametrize("state", sorted(compactor.WALLS))
def test_every_stages_rows_are_the_same_width(state):
    """#277: one stage replaces the last in place, so every row of every stage is WIDTH cells,
    well inside an 80-column pane, and the art is several rows, not one."""
    rows = compactor.WALLS[state].split("\n")
    assert len(rows) >= 4, f"{state}: {len(rows)} rows"
    assert {cell_len(r) for r in rows} == {compactor.WIDTH} and compactor.WIDTH <= 40, f"{state}: {rows}"


@pytest.mark.parametrize("state, origin, note, want, desc", [
    ("requested", "you (Compact)", "", None, "requested: no art"),
    ("preparing", "you (Compact)", "", "asked for by you (Compact) · 10:39Z", "who asked, under the art"),
    ("completed", "you (Compact)", "220k → 40k", "220k → 40k · 10:41Z", "what came of it"),
    ("completed", None, "", "10:41Z", "nothing to say: the time alone"),
])
def test_stage_text_puts_the_detail_under_the_art(state, origin, note, want, desc):
    stamp = "10:39Z" if state == "preparing" else "10:41Z"
    text = compactor.stage_text(state, origin, note, stamp)
    if want is None:
        assert text is None, desc
    else:
        assert text == compactor.WALLS[state] + "\n" + want, desc


@pytest.mark.parametrize("windows, at_least, desc", [
    ([(0, 0)], 1, "a stage entered: one excerpt at once"),
    ([(0, 25), (25, 100)], 4, "a stage change brings one, then every 20 to 30 s"),
])
def test_schedule_is_seeded_by_the_cycle_and_never_repeats(windows, at_least, desc):
    first = compactor.schedule("c0ffee01", windows)
    assert first == compactor.schedule("c0ffee01", windows), f"{desc}: the same for every refresh and app"
    assert len(first) >= at_least and len({e for _, e in first}) == len(first), desc
    assert [t for t, _ in first] == sorted(t for t, _ in first), desc
    starts = {s for s, _ in windows}
    assert starts <= {t for t, _ in first}, f"{desc}: one at each stage change"


TIMES = [i / compactor.TICKS for i in range(0, 40 * compactor.TICKS, 7)] + [90.0, 600.0]


@pytest.mark.parametrize("state", sorted(compactor.CAPTIONS))
def test_every_big_frame_is_the_same_size(state):
    """#287: every frame of every stage BIG_WIDTH by BIG_HEIGHT, its roles cell for cell, so
    the dock never shifts as it moves or a stage replaces the last."""
    for t in TIMES:
        for animate in (True, False):
            rows, roles = compactor.scene(state, t, animate)
            assert len(rows) == len(roles) == compactor.BIG_HEIGHT, (state, t)
            assert {cell_len(r) for r in rows} == {len(r) for r in roles} == {compactor.BIG_WIDTH}, (state, t, rows)
            assert set("".join(roles)) <= set(compactor.ROLES), (state, t)


@pytest.mark.parametrize("state", sorted(compactor.CAPTIONS))
def test_each_stage_moves_and_stills_hold(state):
    """Animated, every stage's frames change over a few seconds; with animation off it's one still."""
    assert len({compactor.scene(state, t)[0] for t in TIMES[:20]}) > 1, state
    assert len({compactor.scene(state, t, animate=False) for t in TIMES}) == 1, state
    assert compactor.still(state) == "\n".join(compactor.scene(state, 0, animate=False)[0])


@pytest.mark.parametrize("state, t, want, desc", [
    ("preparing", 30.0, compactor.OPEN, "preparing: the walls still"),
    ("compacting", 0.0, compactor.OPEN + 1, "compacting: grinding in"),
    ("compacting", 600.0, compactor.OPEN + 14, "a long /compact: nearly shut, never quite"),
    ("completed", 0.0, compactor.SHUT, "compacted: met"),
    ("cancelled", 60.0, compactor.OPEN, "called off: back out"),
])
def test_the_walls_by_stage(state, t, want, desc):
    assert compactor.walls(state, t) == want, desc


def test_the_crew_and_the_dianoga_show():
    """Han, Leia, Luke and Chewie all in a compacting frame; the tentacle has Luke for a while."""
    rows = "\n".join(compactor.scene("compacting", 1.0)[0])
    assert all(mark in rows for mark in ("H", "@o@", "#")), rows
    grab = compactor.GRAB / 2 + 0.5
    rows, roles = compactor.scene("compacting", grab)
    assert "d" in "".join(roles[1:9]) and ")" in "".join(rows[5:9]), rows


@pytest.mark.parametrize("state", sorted(compactor.WALLS))
def test_dock_text_and_state_of_round_trip(state):
    text = compactor.dock_text(state, 1761990000.5, "you (Compact)", "", "23:30Z")
    assert compactor.docked(text)[:2] == (state, 1761990000.5)
    assert compactor.state_of(compactor.stage_text(state, None, "", "23:30Z")) == state
    assert compactor.state_of("Han: hello") is None
