"""How a column's slots share its rows (#111, .plan/modules.md): one rule for any column,
without Textual.

- Each visible slot that isn't compact has a weight; a compact one (a pane with nothing to
  show, an empty Decisions) keeps its own rows; an empty slot is hidden. The weights share
  what's left, each slot at least its minimum, to the row (`rows`).
- A splitter joins each pair of neighbouring weighted slots (`pairs`), a compact or empty
  slot between them or not: it sits just above the lower of the two, so it may lie beside a
  compact one. Dragging it moves rows between those two alone (`dragged`), its line on the
  pointer's row.
- Weights are unitless, in one scale everywhere: DEFAULT's, each slot's share of its column.
  Until the person drags, a column's weights are the stylesheet's sizes in rows (today's
  layout, to the row), brought to that scale (`weights`). A drag sets the weighted slots'
  weights to their rows, in that scale, keeping their total (`as_weights`): its two slots
  trade rows, their sum kept, the others' rows unchanged, and a compact or hidden slot's
  weight means what it did. Kept per slot
  (`layout.weights`); a double-click puts the column's defaults back. A slot with none kept
  weighs its DEFAULT share.
"""

import json
import math

WEIGHTS_KEY = "weights"   # under layout.: {slot: weight}
# each slot's share of its column as the stylesheet lays it out with every slot shown
# (tui.py's #slot-* rules), for a slot with no weight kept
DEFAULT = {"left.top": 0.4, "left.middle": 0.3, "left.bottom": 0.3,
           "middle.top": 0.4, "middle.middle": 0.2, "middle.bottom": 0.4, "right": 1.0}
# the splitters' shares kept before weights, and the second round of #111's, never released:
# deleted on start, each column starting from its defaults (a share dragged once is dragged again)
OLD_KEYS = ("left-middle", "left-bottom", "middle-top", "middle-middle", "middle-bottom")


def rows(room: int, weights: list[float], minimums: list[int]) -> list[int]:
    """`room` rows shared by `weights`, each at least its minimum, summing to `room` exactly
    while the minimums fit (more than that: each its minimum). Largest remainder, the
    earlier slot first on a tie, so the same weights always give the same rows."""
    n = len(weights)
    if not n:
        return []
    weights = [max(w, 0.0) if math.isfinite(w) else 0.0 for w in weights]
    if sum(minimums) >= room:
        return list(minimums)
    fixed: dict[int, int] = {}
    while True:   # a slot its share puts under its minimum takes the minimum, the rest share again
        free = [i for i in range(n) if i not in fixed]
        left = room - sum(fixed.values())
        total = sum(weights[i] for i in free) or float(len(free))
        share = {i: left * (weights[i] if sum(weights[i] for i in free) else 1.0) / total for i in free}
        under = [i for i in free if share[i] < minimums[i]]
        if not under:
            break
        for i in under:
            fixed[i] = minimums[i]
    out = {i: math.floor(share[i]) for i in share}
    spare = left - sum(out.values())
    for i in sorted(share, key=lambda i: (-(share[i] - out[i]), i))[:spare]:
        out[i] += 1
    return [fixed.get(i, out.get(i)) for i in range(n)]


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


def dragged(heights: dict[str, int], above: str, below: str, delta: int, minimums: dict[str, int]) -> dict[str, int]:
    """The rows each weighted slot has once a splitter's line has moved `delta` rows down
    (up, negative) from where the drag began, `heights`: those two slots alone change, and
    neither goes under its minimum."""
    a, b = heights[above], heights[below]
    moved = max(minimums[above] - a, min(delta, b - minimums[below]))
    return {**heights, above: a + moved, below: b - moved}


def styled(sizes: list[tuple[str, float]], room: int, height: int) -> list[float]:
    """The weights the stylesheet's sizes come to, in rows, for a column `height` rows tall with
    `room` to share: a percentage of the column as it is, and the fr slots what that leaves,
    by their fr. The first slot shown always takes what's left (as the top one does), so a
    pane in a slot below an empty top one isn't held to that slot's percentage."""
    if sizes and sizes[0][0] != "fr":   # the first shown takes what the others leave, as a top slot does
        sizes = [("fr", 1.0)] + sizes[1:]
    fixed = sum(v * height / 100 for unit, v in sizes if unit == "%")
    frs = sum(v for unit, v in sizes if unit == "fr") or 1.0
    rest = max(room - fixed, 0.0)
    return [v * height / 100 if unit == "%" else rest * v / frs for unit, v in sizes]


def weights(slots: list[str], kept: dict[str, float], styled_rows: list[float]) -> list[float]:
    """The weights of a column's weighted `slots`, in order, in DEFAULT's scale: those kept,
    and DEFAULT's for a slot with none. With none of them kept, the stylesheet's rows
    (`styled_rows`), brought to that scale: the same rows, in the same unit as any kept."""
    if any(s in kept for s in slots):
        return [kept.get(s, DEFAULT[s]) for s in slots]
    scale = sum(DEFAULT[s] for s in slots) / (sum(styled_rows) or 1.0)
    return [r * scale for r in styled_rows]


def as_weights(rows: dict[str, int], total: float) -> dict[str, float]:
    """Weighted slots' rows as weights summing to `total` (the weights they had, in DEFAULT's
    scale): each in proportion to its rows, so `rows` gives back exactly these rows, and any
    other slot's weight, compact or hidden, keeps its meaning beside them."""
    count = sum(rows.values()) or 1
    return {slot: n * total / count for slot, n in rows.items()}


def kept(raw: str | None) -> dict[str, float]:
    """layout.weights as kept: {slot: weight}, anything unreadable left out."""
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return {}
    if not isinstance(value, dict):
        return {}
    return {k: float(v) for k, v in value.items() if k in DEFAULT and isinstance(v, (int, float))
            and not isinstance(v, bool) and math.isfinite(v) and v >= 0}
