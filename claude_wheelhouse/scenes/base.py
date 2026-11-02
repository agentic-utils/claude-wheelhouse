"""What every compactor scene shares (#304): the canvas, the frame sizes, the tick rate, the
colour roles, and the interface a scene gives the compactor (Scene).

A scene is a culture reference played out while a session compacts: its cast's excerpts, a
caption per stage, a small still per stage for the conversation (WIDTH cells wide), and a big
animated frame (BIG_WIDTH by BIG_HEIGHT) for the dock. Never imports Textual.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

# the small stage art (#277): every row of every stage WIDTH cells, so it never shifts as one
# stage replaces the last in place
WIDTH = 28
# the big art (#287): BIG_WIDTH by BIG_HEIGHT cells in every frame of every stage (a test pins
# it); a pane narrower than BIG_WIDTH gets the small art instead. Everything moves on TICKS
# frames a second of the stage's own time, so any rate draws the same scene
BIG_WIDTH, BIG_HEIGHT = 60, 12   # 11 rows of art and the caption
TICKS = 6
# a frame's cells, cell for cell, carry the role each is drawn in, so the app colours it without
# parsing the art (tui.ART_STYLE). One palette for every scene, named for the first: a scene
# draws its own pieces in whichever of these colours suits them
ROLES = {"w": "wall", "s": "seam", "c": "crew", "g": "sludge", "k": "spark", "h": "hatch", "d": "dianoga", "r": "alert", " ": "text"}

Frame = tuple[tuple[str, ...], tuple[str, ...]]   # rows, and each row's roles


class Canvas:
    """A frame being drawn: rows of cells and the role of each."""

    def __init__(self) -> None:
        self.cells = [[" "] * BIG_WIDTH for _ in range(BIG_HEIGHT)]
        self.roles = [[" "] * BIG_WIDTH for _ in range(BIG_HEIGHT)]

    def put(self, row: int, col: int, text: str, role: str, lo: int = 0, hi: int = BIG_WIDTH) -> None:
        """text from col on row, in role, clipped to the columns lo to hi."""
        for i, ch in enumerate(text):
            if lo <= col + i < hi and 0 <= row < BIG_HEIGHT and ch != "\0":
                self.cells[row][col + i], self.roles[row][col + i] = ch, role

    def done(self) -> Frame:
        return tuple("".join(r) for r in self.cells), tuple("".join(r) for r in self.roles)


@dataclass(frozen=True)
class Scene:
    """One scene: name (its registry key, carried in the dock entry), its cast's excerpts (each
    one line or a two-line exchange), a caption per stage, a small still per stage that has art
    (walls: the stages a cycle shows, each row WIDTH cells), and draw(state, t, animate), its big
    frame t seconds into a stage, or the stage's still with animate False."""

    name: str
    excerpts: tuple[tuple[str, ...], ...]
    captions: dict[str, str]
    walls: dict[str, str]
    draw: Callable[[str, float, bool], Frame]

    def frame(self, state: str, t: float, animate: bool = True) -> Frame:
        """One big frame of a stage, t seconds into it, or its still with animate False."""
        return self.draw(state, t, animate)

    def still(self, state: str) -> str:
        """A stage's big still frame as plain text (animation off, or its last frame left in the
        conversation)."""
        return "\n".join(self.draw(state, 0.0, False)[0])
