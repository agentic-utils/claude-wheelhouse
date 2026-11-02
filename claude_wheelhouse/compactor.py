"""A compaction's scene (#244, #304): what is shown while a session compacts, and when. Each
cycle plays one scene (scenes/, picked by scene_for): its stage art, the big animated frame the
dock pins (#287) and its cast's excerpts, dealt without repeats for the length of the cycle
(schedule). This module holds what every scene shares: the pick, the cadence and the stages.
Never imports Textual.
"""

from __future__ import annotations

import random

from .scenes import SCENES, Scene
from .scenes.base import BIG_HEIGHT, BIG_WIDTH, ROLES, TICKS, WIDTH   # shared by every scene: the app reads them here

CADENCE = (20, 30)
SHOWN = ("preparing", "compacting")   # the states the crew panic through
ENDS = ("completed", "failed", "cancelled")


def scene_for(cycle_id: str, previous: str | None) -> Scene:
    """The cycle's scene (D253): seeded by its id, so every app and every refresh agrees on it,
    and never previous (the session's last cycle's scene, by name) while there is another."""
    names = sorted(n for n in SCENES if n != previous) or sorted(SCENES)
    return SCENES[random.Random("scene " + cycle_id).choice(names)]


def gap(rng: random.Random) -> float:
    """Return seconds to wait before the next excerpt."""
    return rng.uniform(*CADENCE)


class Excerpts:
    """Deal a scene's excerpts without repeats for one compaction cycle."""

    def __init__(self, pool: tuple[tuple[str, ...], ...], rng: random.Random | None = None) -> None:
        self._pool = pool
        self._rng = rng or random.Random()
        self._order: list[int] = []

    def next(self) -> tuple[str, ...]:
        """Return an excerpt not yet given, reshuffling once all are used."""
        if not self._order:
            self._order = list(range(len(self._pool)))
            self._rng.shuffle(self._order)
        return self._pool[self._order.pop()]


def stage_text(scene: Scene, state: str, origin: str | None, note: str, stamp: str = "") -> str | None:
    """A stage's art (the scene's walls), then (dim) a line of who asked or what came of it, and
    when (stamp). None for a stage with no art (requested: usually under a second, and the eye
    covers it). Plain Text in the compaction blue, never Markdown."""
    if state not in scene.walls:
        return None
    return scene.walls[state] + "\n" + detail(state, origin, note, stamp)


def detail(state: str, origin: str | None, note: str, stamp: str) -> str:
    """The dim line under a stage's art: what came of it, else who asked, and when."""
    said = note or (f"asked for by {origin}" if state in SHOWN + ("requested",) and origin else "")
    return " · ".join(filter(None, (said, stamp)))


def dock_text(scene: Scene, state: str, since: float, origin: str | None, note: str, stamp: str) -> str:
    """The running cycle's "dock" entry (#287): its scene, its stage (requested drawn as
    preparing), when that stage began (epoch, the animation's time zero) and its dim line, one a
    line (docked)."""
    shown = state if state in scene.walls else "preparing"
    return "\n".join((scene.name, shown, repr(since), detail(state, origin, note, stamp)))


def docked(text: str) -> tuple[Scene, str, float, str]:
    """A dock entry's scene, stage, start and dim line (dock_text)."""
    name, state, since, line = (text.split("\n", 3) + ["", "", ""])[:4]
    scene = SCENES.get(name) or next(iter(SCENES.values()))
    try:
        return scene, state, float(since), line
    except ValueError:
        return scene, state, 0.0, line


def schedule(cycle_id: str, windows: list[tuple[float, float]], scene: Scene) -> list[tuple[float, tuple[str, ...]]]:
    """The scene's excerpts for one cycle, as (epoch second, excerpt): one at the start of each
    window (a stage it panics through, from its start to its end or now), then one every
    CADENCE seconds within it. Seeded by the cycle's id, so every refresh and every app shows
    the same lines at the same times, none repeated within the cycle."""
    rng = random.Random(cycle_id)
    pool, out = Excerpts(scene.excerpts, rng), []
    for start, end in windows:
        at = start
        while at <= end:
            out.append((at, pool.next()))
            at += gap(rng)
    return out


def state_of(art: str) -> tuple[Scene, str] | None:
    """Which scene and stage a stage entry's art shows, from its rows, against every registered
    scene's walls: None for any other text."""
    for scene in SCENES.values():
        for state, rows in scene.walls.items():
            if art.startswith(rows):
                return scene, state
    return None
