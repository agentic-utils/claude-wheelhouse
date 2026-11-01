"""Finding an inbox item (#273, the search popover): which items a query matches, best first.
Never imports Textual.

A query shaped like a ref (`T3`, `q25`, `PR276`) puts exact ref matches first. Otherwise, and
after those, a case-insensitive substring match on ref and title, best first: the ref, then
a title starting with the query, then one containing it, each group newest first."""

import re

from .store import KINDS

LIMIT = 50   # rows the popover lists; a line says how many more matched
REF_SHAPED = re.compile(rf"(?:{'|'.join(sorted(KINDS.values(), key=len, reverse=True))})\d+", re.IGNORECASE)


def ref_shaped(query: str) -> bool:
    """Whether a query reads as an item's ref: a kind's letter (or PR) and digits."""
    return REF_SHAPED.fullmatch(query.strip()) is not None


def rank(item, query: str) -> int | None:
    """Where an item stands for a query (lower first), None if it doesn't match: 0 its ref
    exactly (a ref-shaped query), 1 its ref holding the query, 2 its title starting with it,
    3 its title holding it. An empty query matches everything, as one group."""
    q = query.strip().casefold()
    ref, title = item["ref"].casefold(), item["title"].casefold()
    if not q:
        return 3
    if ref == q and ref_shaped(q):
        return 0
    if q in ref:
        return 1
    if title.startswith(q):
        return 2
    if q in title:
        return 3
    return None


def find(items, query: str, limit: int = LIMIT) -> tuple[list, int]:
    """The items matching a query, best first (rank), newest first within each group, at
    most limit of them; and how many more matched."""
    ranked = [(r, it) for it in items if (r := rank(it, query)) is not None]
    ranked.sort(key=lambda pair: pair[1]["created_at"], reverse=True)   # stable: newest first in each group
    ranked.sort(key=lambda pair: pair[0])
    return [it for _, it in ranked[:limit]], max(0, len(ranked) - limit)
