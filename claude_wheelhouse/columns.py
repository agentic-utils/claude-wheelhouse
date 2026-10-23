"""How a column's slots share its rows (#111, .plan/modules.md): one rule for any column,
without Textual.

- A slot is weighted (shown, sharing the column), compact (a pane with nothing to show, an
  empty Decisions: it keeps its own few rows) or hidden (empty: none). Each has a weight,
  kept per slot (`layout.weights`), else its DEFAULT; one unit everywhere, so a weight means
  the same whichever other slots show.
- The weighted slots share what the compact slots and the splitters leave in proportion to
  their weights, each at least its least rows (`shares`), each line on the row nearest its
  place (`rows`). A column too short for their least rows, a rearranged one at 80x24
  (#132), takes the rows it lacks from the slots with the most above FLOOR (`squeezed`). Being compact or hidden is applied here and never written back: a slot
  coming or going changes no weight, and the others keep their proportions.
- A splitter joins each pair of neighbouring weighted slots, a compact or empty slot
  between them or not (`pairs`): it sits just above the lower one. Dragging it sets those
  two slots' weights alone (`dragged`), so their line lands on the pointer's row and every
  other slot keeps its rows. A double-click drops the column's weights for DEFAULT's.
"""

import json
import math
from typing import NamedTuple

WEIGHTS_KEY = "weights"   # under layout.: {slot: weight}
# each slot's share of its column as the stylesheet lays it out with every slot shown
# (tui.py's #slot-* rules), for a slot with no weight kept
DEFAULT = {"left.top": 0.4, "left.middle": 0.3, "left.bottom": 0.3,
           "middle.top": 0.4, "middle.middle": 0.2, "middle.bottom": 0.4, "right": 1.0}
# the splitters' shares kept before weights, and the second round of #111's, never released:
# deleted on start, each column starting from its defaults (a share dragged once is dragged again)
OLD_KEYS = ("left-middle", "left-bottom", "middle-top", "middle-middle", "middle-bottom")
FLOOR = 3   # rows: the least a squeezed slot keeps, a pane's border and a line (its own least, if less)
EPS = 1e-6   # rows: a line a float's error under a half rounds as the half does, so it never flickers


class Slot(NamedTuple):
    """A slot as its column lays it out: its name, the fewest rows it keeps while weighted,
    and the rows it keeps itself, if any (compact: its panes' least; hidden: 0)."""
    name: str
    least: int = 1
    own: int | None = None   # None: weighted


def pairs(slots: list[str], weighted: set[str]) -> list[tuple[str, str] | None]:
    """For the splitter before each slot but the first, in a column's order: the two slots it
    moves rows between (above, below), or None where it is hidden. It shows above a weighted
    slot with another weighted one somewhere above, and joins those two, a compact or empty
    slot between them or not."""
    out = []
    for k, slot in enumerate(slots[1:], 1):
        above = next((s for s in reversed(slots[:k]) if s in weighted), None)
        out.append((above, slot) if slot in weighted and above else None)
    return out


def shares(room: int, weights: list[float], minimums: list[int]) -> tuple[list[float], float]:
    """`room` rows shared by `weights`, unrounded: each max(its minimum, weight x scale), the
    scale the one that sums them to `room`, and that scale (0 when the minimums don't fit,
    each at its minimum). A slot its share puts under its minimum takes the minimum, and the
    rest share what that leaves, until none is under."""
    if sum(minimums) >= room or not weights:
        return [float(m) for m in minimums], 0.0
    fixed: set[int] = set()
    while True:
        free = [i for i in range(len(weights)) if i not in fixed]
        scale = (room - sum(minimums[i] for i in fixed)) / sum(weights[i] for i in free)
        under = {i for i in free if weights[i] * scale < minimums[i]}
        if not under:
            return [float(minimums[i]) if i in fixed else weights[i] * scale for i in range(len(weights))], scale
        fixed |= under


def rows(room: int, weights: list[float], minimums: list[int]) -> list[int]:
    """The rows each slot has: its share (`shares`) with each line between two slots on the
    row nearest it, so the rows sum to `room` (while the minimums fit; past that, `squeezed`)
    and a line moves only when the slots either side of it change."""
    real, scale = shares(room, weights, minimums)
    if not scale:
        return squeezed(room, minimums)
    lines = [0] + [line(at) for at in tops(real)[1:]] + [room]
    return [b - a for a, b in zip(lines, lines[1:])]


def squeezed(room: int, minimums: list[int]) -> list[int]:
    """The minimums, short of `room`: a row at a time from the slot with the most above its
    floor (FLOOR, or its minimum if less), the lower on a tie, until they fit or each is at
    its floor. A slot with rows to spare (session info, its description) gives way first."""
    out = list(minimums)
    floors = [min(m, FLOOR) for m in minimums]
    while out and sum(out) > room:
        i = max(range(len(out)), key=lambda k: (out[k] - floors[k], k))
        if out[i] <= floors[i]:
            break
        out[i] -= 1
    return out


def tops(real: list[float]) -> list[float]:
    """Where each slot starts, unrounded, as `rows` and `dragged` both add them up."""
    out, at = [], 0.0
    for r in real:
        out.append(at)
        at += r
    return out


def line(at: float) -> int:
    """The row a line `at` (unrounded) is drawn on: the nearest, a half rounding down the column."""
    return math.floor(at + 0.5 + EPS)


def weights(slots: list[Slot], kept: dict[str, float]) -> list[float]:
    """The weighted slots' weights, in order: each its kept, else DEFAULT's."""
    return [kept.get(s.name, DEFAULT[s.name]) for s in slots if s.own is None]


def room(height: int, slots: list[Slot]) -> int:
    """The rows a column `height` tall has for its weighted slots: less its splitters
    shown and the rows its compact slots keep."""
    shown = sum(1 for p in pairs([s.name for s in slots], {s.name for s in slots if s.own is None}) if p)
    return height - shown - sum(s.own for s in slots if s.own is not None)


def lay(height: int, slots: list[Slot], kept: dict[str, float]) -> dict[str, int]:
    """Each slot's rows in a column `height` tall: a weighted one's by its weight (`rows`),
    a compact or hidden one's its own."""
    weighted = [s for s in slots if s.own is None]
    got = dict(zip([s.name for s in weighted],
                   rows(room(height, slots), weights(slots, kept), [s.least for s in weighted])))
    return {s.name: s.own if s.own is not None else got[s.name] for s in slots}


def dragged(height: int, slots: list[Slot], kept: dict[str, float], above: str, below: str,
            delta: int) -> dict[str, float]:
    """`kept` once the splitter between the weighted slots `above` and `below` has moved
    `delta` rows down (up, negative) from where `kept` lays it: those two slots' weights
    alone are set, their unrounded rows summing to what they did, so every other slot keeps
    its rows. The line lands on the pointer's row, or where one of the two reaches its
    least rows. Their weights keep their sum, so the other slots keep their shares at any
    size, unless one of the two was held at its least and another slot holds the scale (one
    sharing the room, or one the pair's own scale would free): then the two take the rows
    they show, a little more weight, never less. A slot left at its least keeps the weight it had, if that's less, so a
    drag never makes a slot bigger anywhere for being shrunk."""
    weighted = [s for s in slots if s.own is None]
    names = [s.name for s in weighted]
    if above not in names or names.index(above) + 1 >= len(names) or names[names.index(above) + 1] != below:
        return kept   # not neighbouring weighted slots (a slot came or went mid-drag): nothing to move
    i = names.index(above)
    ws, least = weights(slots, kept), [s.least for s in weighted]
    real, scale = shares(room(height, slots), ws, least)
    if not scale or not delta:
        return kept
    top = tops(real)[i]   # where `above` starts, unrounded
    now = line(top + real[i])
    pair = real[i] + real[i + 1]
    a = min(max(now + delta - top, least[i]), pair - least[i + 1])
    if line(top + a) == now:   # either slot at its least already: nothing moves, nothing kept
        return kept
    others = [k for k in range(len(ws)) if k not in (i, i + 1)]
    if not any(real[k] > least[k] + EPS for k in others):   # none shares the room: the scale is the pair's
        held = min((least[k] / ws[k] for k in others), default=math.inf)   # past it, another would grow
        scale = min(pair / (ws[i] + ws[i + 1]), held)

    def weight(n: int, rows_: float) -> float:
        return min(ws[n], least[n] / scale) if rows_ <= least[n] + EPS else rows_ / scale

    return {**kept, above: weight(i, a), below: weight(i + 1, pair - a)}


def kept(raw: str | None) -> dict[str, float]:
    """layout.weights as kept: {slot: weight}, anything unreadable left out."""
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return {}
    if not isinstance(value, dict):
        return {}
    return {k: float(v) for k, v in value.items() if k in DEFAULT and isinstance(v, (int, float))
            and not isinstance(v, bool) and math.isfinite(v) and v > 0}
