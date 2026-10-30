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
def test_stage_lines_fit_36_cells(state):
    assert cell_len(compactor.WALLS[state]) <= 36, state


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
