"""Each list's order (T114, .plan/drag-and-sort.md): the sort a header imposes, with the
person's own order breaking ties inside each of its groups, or, with ordering off, the
person's order alone.

A sort is a spec: a column and its marker, "status*", "title▲", "dot*", or "" for ordering
off. A group is the rows that share the sort's key, named by a token. A row is placed in
its group while its pos_group is the group's token: a drag within the group writes it to
every row of the group, a drag across groups writes MANUAL to every row and turns ordering
off. A row not placed (new, or its key changed since) comes first in its group, in the
sort's own order. Pure: no Textual, no store."""

import functools
import re

from .store import FINISHED_RANK, SETTLED_RANK, _neg_time, inbox_rank

UP, DOWN, STAR = "▲", "▼", "*"
MANUAL = "manual"   # pos_group of a row placed with ordering off
ITEMS_DEFAULT = "status*"   # today's inbox order
SESSIONS_DEFAULT = "dot*"   # the session list's one sort, on until a drag across its groups
SESSIONS_SORT = "dot*"   # the session list's one sort: running first, then the latest activity
SORT_KEYS = {"items": "layout.sort.items", "sessions": "layout.sort.sessions"}   # settings, per TUI
DEFAULTS = {"items": ITEMS_DEFAULT, "sessions": SESSIONS_DEFAULT}
SPECS = {"items": {"", "status*", *(c + m for c in ("session", "ref", "status", "title") for m in "▲▼")},
         "sessions": {"", SESSIONS_SORT}}
# Status ▲ (Q104): blocked, waiting, open, then what waits on the session (and any status
# not named here), running, todo
URGENCY = {"blocked": 0, "waiting": 1, "open": 2, "answered": 3, "processing": 3, "unseen": 3, "seen": 3,
           "running": 4, "todo": 5}
REF = re.compile(r"([A-Za-z]*)(\d*)")


def field(row, name: str):
    """A column a row from older code (an older daemon's, or the store's before the column)
    may lack: None then."""
    try:
        return row[name]
    except (KeyError, IndexError):
        return None


@functools.total_ordering
class Desc:
    """A value that sorts the other way: a ▼ column."""
    __slots__ = ("v",)

    def __init__(self, v):
        self.v = v

    def __eq__(self, other) -> bool:
        return self.v == other.v

    def __lt__(self, other) -> bool:
        return other.v < self.v

    def __hash__(self) -> int:
        return hash(self.v)


def band_of(rank: tuple) -> int:
    """Where an inbox rank (store.inbox_rank) puts an item: 0 active, 1 settled, 2 finished."""
    return 2 if rank[0] >= FINISHED_RANK else 1 if rank[0] >= SETTLED_RANK else 0


def parse(spec: str) -> tuple[str | None, str | None]:
    return (spec[:-1], spec[-1]) if spec else (None, None)


def next_item_sort(spec: str, column: str) -> str:
    """A click on the inbox's column header: Status cycles *, ▲, ▼; another column ▲, ▼,
    then back to Status * (the inbox's default). With ordering off, any column starts afresh."""
    col, marker = parse(spec)
    if column == "status":
        return {STAR: "status" + UP, UP: "status" + DOWN}.get(marker, ITEMS_DEFAULT) if col == "status" \
            else ITEMS_DEFAULT
    if col != column:
        return column + UP
    return column + DOWN if marker == UP else ITEMS_DEFAULT


def next_session_sort(spec: str) -> str:
    """A click on the session list's dot column: its one sort, or back to the person's order."""
    return "" if spec else SESSIONS_SORT


def marker(spec: str, column: str) -> str:
    """The column's marker, if it is the one in force (D97)."""
    col, mark = parse(spec)
    return mark if col == column else ""


def natural(ref: str) -> tuple:
    """A ref's key: T2 before T10."""
    m = REF.match(ref)
    return (m.group(1), int(m.group(2)) if m.group(2) else -1, ref)


def placed(row, token: str) -> bool:
    return field(row, "pos_group") == token and field(row, "pos") is not None


def keyed(row, token: str, band: int, value, tie) -> tuple[tuple, str]:
    """A row's sort key in its group, and the group's token: placed rows after the rest, by pos."""
    return ((band, value, 1, row["pos"]) if placed(row, token) else (band, value, 0, tie)), token


def item_key(spec: str, item, processing=frozenset(), busy=frozenset(), names=None) -> tuple[tuple, str]:
    """An item's sort key under spec and its group's token. The key's first element is its
    band (0 active, 1 settled, 2 finished): a sort keeps the bands; with ordering off there
    is one band, and nothing moves between them."""
    rank = inbox_rank(item, processing, busy)
    if not spec:
        return keyed(item, MANUAL, 0, 0, rank)
    band = band_of(rank)
    col, mark = parse(spec)
    tie = rank
    if col == "status" and mark == STAR:
        value = rank[0]
    elif col == "status":
        status = "processing" if item["status"] == "open" and (item["session_id"], item["ref"]) in processing \
            else item["status"]
        value = URGENCY.get(status, URGENCY["answered"]) if band == 0 else 0
        # newest first; the settled and finished as the inbox has them, the latest to settle lowest
        tie = (_neg_time(item["updated_at"]), -item["id"]) if band == 0 else rank
    elif col == "session":
        value = ((names or {}).get(item["session_id"], "").casefold(), item["session_id"])
    elif col == "ref":
        value = natural(item["ref"])
    else:
        value = item["title"].casefold()
    token = f"{spec}|{band}|{value!r}"
    return keyed(item, token, band, Desc(value) if mark == DOWN else value, tie)


def drag_group(token: str) -> str:
    """The group a drag stays within to keep the sort: its row's group, but under Status *
    the whole band (T121, Q108 B): a token there is "status*|<band>|<tier>", and a drop
    anywhere in the band keeps * on."""
    return token.rsplit("|", 1)[0] if token.startswith(ITEMS_DEFAULT + "|") else token


def arrange(spec: str, raw: dict) -> dict:
    """Each row's rank from its item_key (raw: key -> (rank, token)). Under Status * (T121) a
    band keeps the person's order: the rows placed in it (each placed with its tier at the
    drop: placed while its status keeps that tier) by pos, and each row not placed (new, or
    its status changed since) put in by status, before the first placed row of its tier or a
    later one, else at the band's foot. The ranks hold for any subset (one session's view),
    as they go by the placed rows of the whole list. Other sorts: their ranks as they are."""
    if spec != ITEMS_DEFAULT:
        return {k: rank for k, (rank, _) in raw.items()}
    placed: dict[int, list] = {}
    for k, (rank, _) in raw.items():
        if rank[2]:
            placed.setdefault(rank[0], []).append((rank[3], rank[1]))
    for rows in placed.values():
        rows.sort()
    out = {}
    for k, (rank, _) in raw.items():
        band, tier, flag, x = rank
        if flag:
            out[k] = (band, x, 1)
        else:
            anchor = next((pos for pos, t in placed.get(band, ()) if t >= tier), float("inf"))
            out[k] = (band, anchor, 0, tier, x)
    return out


def session_key(spec: str, s, running: bool, at: int) -> tuple[tuple, str]:
    """A session's sort key and its group's token. Ordering off: unparked then parked, in
    creation order (at, its place in the store's list) for those not placed; under dot*, running
    first, then the latest activity, parked still at the foot."""
    if not spec:
        return keyed(s, MANUAL, 0, 0, (s["parked"], at))
    value = (s["parked"], not running)
    last = field(s, "last_at") or s["created_at"]
    return keyed(s, f"{spec}|{value!r}", 0, value, (Desc(last), at))   # ties (one second): creation order


def drop(order: list, tokens: dict, moved, before=None, after=None, shown=None) -> tuple[str | None, list]:
    """A unit (moved) dropped before the visible unit `before`, or after `after`, in the
    whole list's order (order: every row's key, as sorted now), next to it. Within its group, the group's
    drag group (drag_group) and its rows in their new order, each to be placed with its own
    token; across groups, None and every row in the
    new order. A drop where it already was changes nothing: ("", []). Within or across goes
    by its neighbours where it lands as the list shows them (shown: the visible rows a drag
    moves, in order), else in the whole order."""
    rest = [k for k in order if k != moved]
    anchor, ahead = (before, True) if before is not None else (after, False)
    i = rest.index(anchor) + (0 if ahead else 1)
    new = rest[:i] + [moved] + rest[i:]
    group = drag_group(tokens[moved])
    # within, if a neighbour where it lands is of its group: the foot of its group is also
    # the head of the next one's
    seen = [k for k in (shown if shown is not None else order) if k != moved and k in tokens]
    j = seen.index(anchor) + (0 if ahead else 1) if anchor in seen else None
    near = seen[max(0, j - 1):j + 1] if j is not None else rest[max(0, i - 1):i + 1]
    within = group in {drag_group(tokens[k]) for k in near}
    if new == order:
        return "", []
    if within:
        return group, [k for k in new if drag_group(tokens[k]) == group]
    return None, new


def sunk_keys(order: list, settled: set) -> set:
    """What the Sink lays at the list's foot: the settled rows, if they all come after the
    rest; else none, as with ordering off a settled row stays where the person put it."""
    first = next((i for i, k in enumerate(order) if k in settled), len(order))
    return set(settled) if all(k in settled for k in order[first:]) else set()
