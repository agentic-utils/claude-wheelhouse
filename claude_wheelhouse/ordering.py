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

from .store import FINISHED_RANK, SETTLED_RANK, _neg_time, inbox_rank, ranked_status

UP, DOWN, STAR = "▲", "▼", "*"
MANUAL = "manual"   # pos_group of a row placed with ordering off
ITEMS_DEFAULT = "status*"   # today's inbox order
SESSIONS_DEFAULT = "dot*"   # the session list's own sort: running first, then the latest activity
# the session list's sortable columns, in its order (the eye's, last, has no sort)
SESSION_COLUMNS = ("dot", "session", "ctx", "questions", "decisions", "drafts")
MOST_FIRST = {"ctx", "questions", "decisions", "drafts"}   # counts and sizes: a first click puts the most at the top
SORT_KEYS = {"items": "layout.sort.items", "sessions": "layout.sort.sessions"}   # settings, per TUI
DEFAULTS = {"items": ITEMS_DEFAULT, "sessions": SESSIONS_DEFAULT}
SPECS = {"items": {"", "status*", *(c + m for c in ("session", "ref", "status", "title") for m in "▲▼")},
         "sessions": {"", SESSIONS_DEFAULT, *(c + m for c in SESSION_COLUMNS[1:] for m in "▲▼")}}
# Status ▲ (Q104): blocked, waiting, open, then what waits on the session (and any status
# not named here), running, todo
URGENCY = {"blocked": 0, "waiting": 1, "open": 2, "answered": 3, "processing": 3, "unseen": 3, "seen": 3,
           "running": 4, "todo": 5}
REF = re.compile(r"([A-Za-z]*)(\d*)")
DIGITS = re.compile(r"(\d+)")


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

    def __repr__(self) -> str:   # as its value: a group's token is the same either way
        return repr(self.v)


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


def next_session_sort(spec: str, column: str) -> str:
    """A click on the session list's column header: the dot column puts its * on (it stays
    on); the name cycles ▲, ▼, then back to the dot's *, as the inbox's columns do; a count
    or size (MOST_FIRST) ▼, ▲, then back. With ordering off, any column starts afresh."""
    col, marker = parse(spec)
    if column == "dot":
        return SESSIONS_DEFAULT
    first, second = (DOWN, UP) if column in MOST_FIRST else (UP, DOWN)
    if col != column:
        return column + first
    return column + second if marker == first else SESSIONS_DEFAULT


def marker(spec: str, column: str) -> str:
    """The column's marker, if it is the one in force (D97)."""
    col, mark = parse(spec)
    return mark if col == column else ""


def natural(ref: str) -> tuple:
    """A ref's key: T2 before T10."""
    m = REF.match(ref)
    return (m.group(1), int(m.group(2)) if m.group(2) else -1, ref)


def natural_name(name: str) -> tuple:
    """A name's key: casefolded, its numbers by value ("s2" before "s10")."""
    return tuple(int(p) if i % 2 else p for i, p in enumerate(DIGITS.split(name.casefold())))


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
    # the inbox's own order within a status: by session name (#236), then as the inbox has it
    tie = (natural_name((names or {}).get(item["session_id"], "")), item["session_id"], rank)
    if col == "status" and mark == STAR:
        value = rank[0]
    elif col == "status":
        status = "processing" if item["status"] == "open" and (item["session_id"], item["ref"]) in processing \
            else ranked_status(item)
        value = URGENCY.get(status, URGENCY["answered"]) if band == 0 else 0
        # newest first; the settled and finished as the inbox has them (by session name, then the
        # latest to settle lowest)
        if band == 0:
            tie = (_neg_time(item["updated_at"]), -item["id"])
    elif col == "session":
        value = ((names or {}).get(item["session_id"], "").casefold(), item["session_id"])
    elif col == "ref":
        value = natural(item["ref"])
        tie = rank
    else:
        value = item["title"].casefold()
        tie = rank
    token = f"{spec}|{band}|{value!r}"
    return keyed(item, token, band, Desc(value) if mark == DOWN else value, tie)


def drag_group(token: str) -> str:
    """The group a drag stays within to keep the sort: its row's group, but under Status *
    the whole band (T121, Q108 B): a token there is "status*|<band>|<tier>", and a drop
    anywhere in the band keeps * on."""
    return token.rsplit("|", 1)[0] if token.startswith(ITEMS_DEFAULT + "|") else token


def arrange(spec: str, raw: dict) -> dict:
    """Each row's rank from its item_key (raw: key -> (rank, token)). Under Status * (T121) a
    band keeps the person's order: the rows placed in it by pos (each placed with its tier at
    the drop; the store lets a placement go once the row's status changes), and each row not
    placed (new, or its status changed since) put in by status: straight before the first
    placed row of its own tier; else straight after the last placed row of a more urgent
    one; else at the head of the band. Rows going into one spot keep the sort's own order
    there (by session name, then newest first). So the newest of a status shows first, as without a drag, and a
    low row dragged to the top never pulls later urgent ones above the more urgent.
    The ranks hold for any subset (one session's view), as they go by the placed rows of the
    whole list. Other sorts: their ranks as they are."""
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
            rows = placed.get(band, ())
            same = next((pos for pos, t in rows if t == tier), None)
            above = [pos for pos, t in rows if t < tier]
            if same is not None:   # before the first of its own status
                out[k] = (band, same, 0, tier, x)
            elif above:   # after the last of a more urgent one
                out[k] = (band, above[-1], 2, tier, x)
            else:
                out[k] = (band, float("-inf"), 0, tier, x)
    return out


def session_key(spec: str, s, running: bool, at: int, name: str = "", context: int | None = None) -> tuple[tuple, str]:
    """A session's sort key and its group's token. Ordering off: unparked then parked, in
    creation order (at, its place in the store's list) for those not placed; under dot*, running
    first, then the latest activity; under another column (name, its natural key; context, its
    size in tokens; the open questions, unseen decisions and drafts, their counts), by it, then
    as under dot*; a context not known yet last either way. Parked sessions keep to the foot
    under every sort."""
    if not spec:
        return keyed(s, MANUAL, 0, 0, (s["parked"], at))
    col, mark = parse(spec)
    last = Desc(field(s, "last_at") or s["created_at"])
    if col not in SESSION_COLUMNS[1:]:   # dot*
        value = (s["parked"], not running)
        return keyed(s, f"{spec}|{value!r}", 0, value, (last, at))   # ties (one second): creation order
    x = natural_name(name) if col == "session" else context if col == "ctx" else \
        field(s, {"questions": "open_questions", "decisions": "unseen_decisions", "drafts": "drafts"}[col]) or 0
    unknown = x is None   # only a context can be: not read yet, or none
    x = 0 if unknown else x
    value = (s["parked"], unknown, Desc(x) if mark == DOWN else x)
    return keyed(s, f"{spec}|{value!r}", 0, value, (not running, last, at))


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
