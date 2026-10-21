"""How a column's slots share its rows (#111 review 2, .plan/modules.md): one rule for any
column, without Textual.

- Each visible slot that isn't compact has a weight; a compact one (a pane with nothing to
  show, an empty Decisions) keeps its own rows; an empty slot is hidden. The weights share
  what's left, each slot at least its minimum, to the row (`rows`).
- A splitter lies between each pair of neighbouring weighted slots (`pairs`): dragging it
  moves rows between those two alone (`dragged`), so its line follows the pointer exactly.
- Until the person drags one, a column's weights are the stylesheet's sizes in rows (`styled`:
  today's layout, to the row); a drag turns it to weights of its own, kept per slot (`layout.weights`, {slot: weight}), and a double-click
  puts the column's defaults back. A slot with no weight kept weighs its DEFAULT share, in
  the scale of the weights kept beside it (`weights`).
- The splitters' shares kept before (`migrate`) become weights once.
"""

import json
import math

WEIGHTS_KEY = "weights"   # under layout.: {slot: weight}
# each slot's share of its column as the stylesheet lays it out with every slot shown
# (tui.py's #slot-* rules), for a slot with no weight kept
DEFAULT = {"left.top": 0.4, "left.middle": 0.3, "left.bottom": 0.3,
           "middle.top": 0.4, "middle.middle": 0.2, "middle.bottom": 0.4, "right": 1.0}
# the splitters' shares kept before weights (each its slot's share of the column), and the
# second round of #111's, never released: read once by migrate, then deleted
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


def weights(slots: list[str], kept: dict[str, float]) -> list[float] | None:
    """The weights of a column's weighted `slots`, in order, from the weights kept; None while
    none of them has one (then the stylesheet's sizes, styled). One with none weighs its
    DEFAULT share in the scale of those kept: theirs over their defaults."""
    have = [s for s in slots if s in kept]
    if not have:
        return None
    scale = sum(kept[s] for s in have) / (sum(DEFAULT[s] for s in have) or 1.0)
    return [kept[s] if s in kept else DEFAULT[s] * scale for s in slots]


def share(stored) -> float | None:
    """A share kept by a splitter: a finite number from 0 to 1, else None."""
    try:
        value = float(stored)
    except (TypeError, ValueError):
        return None
    return min(max(value, 0.0), 1.0) if math.isfinite(value) else None


def migrate(old: dict[str, str | None]) -> dict[str, float] | None:
    """The weights the splitters' shares kept before become (OLD_KEYS: {key: value as stored}),
    None if none is readable. The left column's: the stats' and session info's shares as
    theirs, the sessions the rest. The middle column's two-slot share s (the inbox's, against
    the Decisions): the inbox at least its default share, the PRs theirs, the Decisions what
    is left, so a person who never dragged sees the default, and the inbox is never squeezed."""
    out: dict[str, float] = {}
    sm, sb = share(old.get("left-middle")), share(old.get("left-bottom"))
    if sm is not None or sb is not None:
        m = DEFAULT["left.middle"] if sm is None else sm
        b = DEFAULT["left.bottom"] if sb is None else sb
        out.update({"left.top": max(1.0 - m - b, 0.0), "left.middle": m, "left.bottom": b})
    s = share(old.get("middle-top"))
    if s is not None:
        inbox = max(s, DEFAULT["middle.top"])
        prs = DEFAULT["middle.middle"]
        out.update({"middle.top": inbox, "middle.middle": prs, "middle.bottom": max(1.0 - inbox - prs, 0.0)})
    return {k: round(v, 6) for k, v in out.items()} or None


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
