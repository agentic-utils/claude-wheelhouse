"""Tests for the compaction cycle's scenes: the pick, the excerpts, the stage art and the big frames."""

import json
import pathlib
import random
import re

import pytest
from rich.cells import cell_len

from claude_wheelhouse import compactor
from claude_wheelhouse.scenes import SCENES, Scene, star_wars

SW = star_wars.SCENE
EVERY = pytest.mark.parametrize("scene", list(SCENES.values()), ids=list(SCENES))
STAGES = compactor.SHOWN + compactor.ENDS
LINE_RE = re.compile(r"^(Luke|Leia|Han|Chewie|R2-D2|C-3PO): \S")
ALL_LINES = [line for excerpt in star_wars.EXCERPTS for line in excerpt]


def speaker(line: str) -> str:
    return line.split(":", 1)[0]


def test_pool_is_big_enough():
    assert len(star_wars.EXCERPTS) >= 90, f"only {len(star_wars.EXCERPTS)} excerpts"


@EVERY
def test_no_duplicate_excerpts_or_lines(scene):
    lines = [line for excerpt in scene.excerpts for line in excerpt]
    assert len(set(scene.excerpts)) == len(scene.excerpts), f"{scene.name}: duplicate excerpt"
    dupes = {line for line in lines if lines.count(line) > 1}
    assert not dupes, f"{scene.name}: duplicate lines: {dupes}"


@EVERY
def test_excerpt_shape(scene):
    for excerpt in scene.excerpts:
        assert len(excerpt) in (1, 2), f"{scene.name}: excerpt must have 1 or 2 lines: {excerpt}"
        if len(excerpt) == 2:
            assert speaker(excerpt[0]) != speaker(excerpt[1]), f"{scene.name}: same speaker twice: {excerpt}"
        for line in excerpt:
            assert cell_len(line) <= 76, f"{scene.name}: {cell_len(line)} cells: {line}"
            assert "—" not in line and "–" not in line, f"{scene.name}: dash in: {line}"


@pytest.mark.parametrize("line", ALL_LINES)
def test_line_format(line):
    assert LINE_RE.match(line), f"bad format: {line}"


@pytest.mark.parametrize("seed", [0, 1, 7, 42, 2026])
def test_full_pass_has_no_repeats(seed):
    excerpts = compactor.Excerpts(star_wars.EXCERPTS, random.Random(seed))
    dealt = [excerpts.next() for _ in star_wars.EXCERPTS]
    assert len(set(dealt)) == len(star_wars.EXCERPTS), f"repeat within a pass, seed {seed}"


def test_second_pass_continues():
    excerpts = compactor.Excerpts(star_wars.EXCERPTS, random.Random(3))
    dealt = [excerpts.next() for _ in range(len(star_wars.EXCERPTS) * 2)]
    assert set(dealt) == set(star_wars.EXCERPTS), "second pass should draw from the pool"


def test_seeds_give_different_orders():
    a = compactor.Excerpts(star_wars.EXCERPTS, random.Random(1))
    b = compactor.Excerpts(star_wars.EXCERPTS, random.Random(2))
    n = len(star_wars.EXCERPTS)
    assert [a.next() for _ in range(n)] != [b.next() for _ in range(n)], "same order"


def test_default_rng_works():
    assert compactor.Excerpts(star_wars.EXCERPTS).next() in star_wars.EXCERPTS, "unseeded instance failed"


@pytest.mark.parametrize("seed", range(20))
def test_gap_within_cadence(seed):
    lo, hi = compactor.CADENCE
    value = compactor.gap(random.Random(seed))
    assert lo <= value <= hi, f"gap {value} outside {compactor.CADENCE}"


@EVERY
def test_every_scene_has_art_and_a_caption_for_every_stage(scene):
    assert set(scene.walls) == set(scene.captions) == set(STAGES), scene.name


@EVERY
@pytest.mark.parametrize("state", STAGES)
def test_every_stages_rows_are_the_same_width(scene, state):
    """#277: one stage replaces the last in place, so every row of every stage of every scene is
    WIDTH cells, well inside an 80-column pane, and the art is several rows, not one."""
    rows = scene.walls[state].split("\n")
    assert len(rows) >= 4, f"{scene.name} {state}: {len(rows)} rows"
    assert {cell_len(r) for r in rows} == {compactor.WIDTH} and compactor.WIDTH <= 40, f"{scene.name} {state}: {rows}"


@pytest.mark.parametrize("state, origin, note, want, desc", [
    ("requested", "you (Compact)", "", None, "requested: no art"),
    ("preparing", "you (Compact)", "", "asked for by you (Compact) · 10:39Z", "who asked, under the art"),
    ("completed", "you (Compact)", "220k → 40k", "220k → 40k · 10:41Z", "what came of it"),
    ("completed", None, "", "10:41Z", "nothing to say: the time alone"),
])
def test_stage_text_puts_the_detail_under_the_art(state, origin, note, want, desc):
    stamp = "10:39Z" if state == "preparing" else "10:41Z"
    text = compactor.stage_text(SW, state, origin, note, stamp)
    if want is None:
        assert text is None, desc
    else:
        assert text == SW.walls[state] + "\n" + want, desc


@pytest.mark.parametrize("windows, at_least, desc", [
    ([(0, 0)], 1, "a stage entered: one excerpt at once"),
    ([(0, 25), (25, 100)], 4, "a stage change brings one, then every 20 to 30 s"),
])
def test_schedule_is_seeded_by_the_cycle_and_never_repeats(windows, at_least, desc):
    first = compactor.schedule("c0ffee01", windows, SW)
    assert first == compactor.schedule("c0ffee01", windows, SW), f"{desc}: the same for every refresh and app"
    assert len(first) >= at_least and len({e for _, e in first}) == len(first), desc
    assert [t for t, _ in first] == sorted(t for t, _ in first), desc
    starts = {s for s, _ in windows}
    assert starts <= {t for t, _ in first}, f"{desc}: one at each stage change"


TIMES = [i / compactor.TICKS for i in range(0, 40 * compactor.TICKS, 7)] + [90.0, 600.0]


@EVERY
@pytest.mark.parametrize("state", STAGES)
def test_every_big_frame_is_the_same_size(scene, state):
    """#287: every frame of every stage of every scene BIG_WIDTH by BIG_HEIGHT, its roles cell
    for cell, so the dock never shifts as it moves or a stage replaces the last."""
    for t in TIMES:
        for animate in (True, False):
            rows, roles = scene.frame(state, t, animate)
            assert len(rows) == len(roles) == compactor.BIG_HEIGHT, (scene.name, state, t)
            assert {cell_len(r) for r in rows} == {len(r) for r in roles} == {compactor.BIG_WIDTH}, (scene.name, state, t, rows)
            assert set("".join(roles)) <= set(compactor.ROLES), (scene.name, state, t)


@EVERY
@pytest.mark.parametrize("state", STAGES)
def test_each_stage_moves_and_stills_hold(scene, state):
    """Animated, every stage's frames change over a few seconds; with animation off it's one still."""
    assert len({scene.frame(state, t)[0] for t in TIMES[:20]}) > 1, (scene.name, state)
    assert len({scene.frame(state, t, animate=False) for t in TIMES}) == 1, (scene.name, state)
    assert scene.still(state) == "\n".join(scene.frame(state, 0, animate=False)[0]), (scene.name, state)


@pytest.mark.parametrize("state, t, want, desc", [
    ("preparing", 30.0, star_wars.OPEN, "preparing: the walls still"),
    ("compacting", 0.0, star_wars.OPEN + 1, "compacting: grinding in"),
    ("compacting", 600.0, star_wars.OPEN + 14, "a long /compact: nearly shut, never quite"),
    ("completed", 0.0, star_wars.SHUT, "compacted: met"),
    ("cancelled", 60.0, star_wars.OPEN, "called off: back out"),
])
def test_the_walls_by_stage(state, t, want, desc):
    assert star_wars.walls(state, t) == want, desc


def test_the_crew_and_the_dianoga_show():
    """Han, Leia, Luke and Chewie all in a compacting frame; the tentacle has Luke for a while."""
    rows = "\n".join(star_wars.scene("compacting", 1.0)[0])
    assert all(mark in rows for mark in ("H", "@o@", "#")), rows
    grab = star_wars.GRAB / 2 + 0.5
    rows, roles = star_wars.scene("compacting", grab)
    assert "d" in "".join(roles[1:9]) and ")" in "".join(rows[5:9]), rows


@EVERY
@pytest.mark.parametrize("state", STAGES)
def test_dock_text_and_state_of_round_trip(scene, state):
    """The dock entry carries its scene; a stage's art is recognised against every scene's."""
    text = compactor.dock_text(scene, state, 1761990000.5, "you (Compact)", "", "23:30Z")
    assert compactor.docked(text)[:3] == (scene, state, 1761990000.5)
    assert compactor.state_of(compactor.stage_text(scene, state, None, "", "23:30Z")) == (scene, state)
    assert compactor.state_of("Han: hello") is None


@pytest.mark.parametrize("text, want, desc", [
    ("compacting\n1761990000.5\nasked for by you (Compact) · 23:30Z", (SW, "compacting", 1761990000.5, "asked for by you (Compact) · 23:30Z"), "a daemon on older code: no scene line, Star Wars"),
    ("compacting\n1761990000.5\n\nstar_wars", (SW, "compacting", 1761990000.5, ""), "no dim line, the scene still read"),
    ("preparing\nnot a time\n\nunknown", (SW, "preparing", 0.0, ""), "a scene no longer registered, a bad time: the first scene, time zero"),
])
def test_docked_reads_every_dock_entry(text, want, desc):
    """#304: the scene goes last, so the stage, start and line stay where older code reads them."""
    assert compactor.docked(text) == want, desc


def test_failed_cheers_and_cancelled_is_caught_pulling_back():
    """#287 review: a failure's crew all have their arms up; a cancelled still shows the walls
    part way back out, not as open as preparing's."""
    rows = star_wars.scene("failed", 0.0, animate=False)[0]
    assert sum(r.count("\\o/") for r in rows) == 2 and any("\\|/" in r for r in rows) and any("\\#/" in r for r in rows), rows
    assert star_wars.OPEN < star_wars.walls("cancelled", star_wars.STILL["cancelled"]) < star_wars.walls("failed", 0), \
        "the still caught retracting"


def fake(name: str) -> Scene:
    return Scene(name, SW.excerpts, SW.captions, SW.walls, SW.draw)


@pytest.mark.parametrize("names, previous, desc", [
    (["star_wars"], None, "one scene, a session's first cycle: that scene"),
    (["star_wars"], "star_wars", "one scene: it again, there being no other"),
    (["a", "b"], "a", "two: never the previous"),
    (["a", "b", "c"], "b", "three: never the previous"),
    (["a", "b", "c"], "gone", "a previous no longer registered: any"),
])
def test_scene_for_is_seeded_by_the_cycle_and_never_repeats_the_last(monkeypatch, names, previous, desc):
    """D253: every app and refresh agrees on a cycle's scene, never the session's previous one
    while there's another."""
    monkeypatch.setattr(compactor, "SCENES", {n: SW if n == "star_wars" else fake(n) for n in names})
    picks = {cid: compactor.scene_for(cid, previous).name for cid in (f"cycle{i:02}" for i in range(40))}
    assert picks == {cid: compactor.scene_for(cid, previous).name for cid in picks}, f"{desc}: the same every time"
    want = set(names) - {previous} or set(names)
    assert set(picks.values()) == want, f"{desc}: {sorted(set(picks.values()))}"


# #304: the Star Wars scene's output recorded before it moved into scenes/, byte for byte
GOLDEN = json.loads((pathlib.Path(__file__).parent / "golden" / "star_wars.json").read_text(encoding="utf-8"))
GOLDEN_TICKS = (0.0, 1.5, 8.0, 40.0)


def golden_cases() -> dict:
    """Each recorded case's id and how the moved code makes it now."""
    cases = {}
    for state in sorted(SW.captions):
        for t in GOLDEN_TICKS:
            cases[f"frame {state} t={t}"] = lambda s=state, t=t: list(map(list, SW.frame(s, t)))
        cases[f"frame {state} still"] = lambda s=state: list(map(list, SW.frame(s, 3.0, animate=False)))
        cases[f"still {state}"] = lambda s=state: SW.still(s)
        cases[f"caption {state}"] = lambda s=state: SW.captions[s]
    for state in ("requested", *SW.walls):
        cases[f"walls {state}"] = lambda s=state: SW.walls.get(s)
        cases[f"stage_text {state}"] = lambda s=state: compactor.stage_text(SW, s, "you (Compact)", "", "10:39Z")
        cases[f"stage_text {state} note"] = lambda s=state: compactor.stage_text(SW, s, None, "220k → 40k", "10:41Z")
    cases["excerpts"] = lambda: list(map(list, SW.excerpts))
    for cid, windows in (("c0ffee01", [(0, 25), (25, 100)]), ("deadbeef", [(0, 300)])):
        cases[f"schedule {cid}"] = lambda c=cid, w=windows: [[t, list(e)] for t, e in compactor.schedule(c, w, SW)]
    return cases


CASES = golden_cases()


def test_golden_covers_every_case():
    assert set(CASES) == set(GOLDEN), sorted(set(CASES) ^ set(GOLDEN))


@pytest.mark.parametrize("case", sorted(CASES))
def test_star_wars_is_unchanged_by_the_move(case):
    assert CASES[case]() == GOLDEN[case], f"{case}: differs from before the move"
