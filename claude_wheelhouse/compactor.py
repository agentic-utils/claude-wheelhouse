"""A compaction's scene (#244, #304): what is shown while a session compacts, and when. Each
cycle plays one scene (scenes/, picked by scene_for): the big animated frame and its cast's
excerpts, dealt without repeats for the length of the cycle (schedule), which the app overlays
on the conversation (#324), while the conversation has a plain line per stage (stage_line).
This module holds what every scene shares: the pick, the cadence and the stages.
Never imports Textual.
"""

from __future__ import annotations

import random
from typing import NamedTuple

from .scenes import SCENES, Scene
from .scenes.base import BIG_HEIGHT, BIG_WIDTH, ROLES, TICKS, WIDTH   # shared by every scene: the app reads them here

CADENCE = (20, 30)
SHOWN = ("preparing", "compacting")   # the states the crew panic through
ENDS = ("completed", "failed", "cancelled")
LINGER = 8   # seconds a cycle's final frame stays on the overlay after its end (#324)


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


def stage_line(state: str, note: str, took: float) -> str | None:
    """A stage's plain line in the conversation (#324), sober while the overlay has the art: None
    for requested (usually under a second). took: seconds since the cycle's first stage."""
    said = {"preparing": "preparing (notes turn)", "compacting": "compacting",
            "completed": " · ".join(filter(None, (f"completed in {duration(took)}", note))),
            "failed": ": ".join(filter(None, ("failed", note))), "cancelled": ": ".join(filter(None, ("cancelled", note)))}.get(state)
    return said and "Compaction: " + said


def duration(seconds: float) -> str:
    """Seconds as "42 s", "1 m 42 s" or "1 h 3 m"."""
    m, s = divmod(round(max(seconds, 0)), 60)
    h, m = divmod(m, 60)
    return f"{h} h {m} m" if h else f"{m} m {s} s" if m else f"{s} s"


class Docked(NamedTuple):
    """A dock entry read back (docked)."""
    scene: Scene
    state: str
    since: float
    cycle: str
    excerpt: tuple[str, ...]


def dock_text(scene: Scene, state: str, since: float, cycle: str, excerpt: tuple[str, ...] = ()) -> str:
    """The "dock" entry the app's overlay shows (#287, #324): its stage (requested drawn as
    preparing), when that stage began (epoch: the animation's time zero, and an end's LINGER
    from), the cycle's id (what hides it for the rest of the cycle), its scene, then the newest
    excerpt, one a line (docked)."""
    shown = state if state in scene.walls else "preparing"
    return "\n".join((shown, repr(since), cycle, scene.name, *excerpt))


def docked(text: str) -> Docked:
    """A dock entry's fields (dock_text): a scene no longer registered is the first one, a bad
    start time zero."""
    parts = text.split("\n")
    state, since, cycle, name = (parts + [""] * 4)[:4]
    scene = SCENES.get(name) or next(iter(SCENES.values()))
    try:
        start = float(since)
    except ValueError:
        start = 0.0
    return Docked(scene, state, start, cycle, tuple(parts[4:]))


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
