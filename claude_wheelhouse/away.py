"""Since you were away (#223, .plan/since-you-were-away.md): what happened across every
session while the person was gone, for the card at the top of the inbox. Never imports
Textual.

The app measures the gap (features.away_hours) and keeps the last input in LAST_INPUT. What
the items say is read from them (card). What no row dates, a session parking, ending or
stopping, the hub notes as it sees it (observe) in QUIET, the latest per session."""

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

LAST_INPUT = "away.last_input"   # settings: the app's last key press, click or scroll (UTC)
QUIET = "away.quiet"   # settings: {sid: {name, how, at}}, the latest each session went quiet
SEEN = "away.seen"   # settings: the sessions as a hub last saw them, so a restarted one compares with it
KEPT = timedelta(days=7)   # a QUIET entry older is dropped
PER_GROUP = 3   # lines a group shows, then "+N more": the card fits an 80x24 inbox
NEEDS, LANDED, QUIET_GROUP, DECIDED = "Needs you", "Landed", "Went quiet", "Decided without you"
HOW = {"parked": "parked", "ended": "ended", "stopped": "stopped"}   # observe's words, as shown
BLOCKED = ("blocked", "waiting")   # a task's statuses that wait on the person


def now() -> datetime:
    """The wall clock, in UTC: a test sets its own."""
    return datetime.now(timezone.utc)


def stamp(at: datetime) -> str:
    return at.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse(at: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(at) if at else None
    except ValueError:
        return None


@dataclass
class Line:
    text: str
    sid: str
    ref: str | None   # None: the session (a Went quiet line)


@dataclass
class Group:
    title: str
    lines: list[Line] = field(default_factory=list)   # every one, newest first

    def shown(self) -> list[Line]:
        return self.lines[:PER_GROUP]

    def more(self) -> int:
        return max(0, len(self.lines) - PER_GROUP)


def needs_you(item) -> bool:
    """An item waiting on the person: a question or permission open, a task blocked or waiting."""
    kind, status = item["kind"], item["status"]
    return (kind in ("question", "permission") and status == "open") or (kind == "task" and status in BLOCKED)


def card(items, quiet: dict, running, start: datetime, end: datetime) -> list[Group]:
    """What happened between start and end, by group, in the card's order; empty groups
    left out. items as client.all_items gives them, quiet as QUIET holds it, running whether
    a session id runs now."""
    def inside(at: str | None) -> bool:
        t = parse(at)
        return t is not None and start <= t <= end

    def line(item) -> tuple[str, Line]:
        name = item["session_name"] or item["session_id"][:8]
        return item["updated_at"], Line(f"{item['ref']} {item['title']} · {name}", item["session_id"], item["ref"])

    rows = {NEEDS: [], LANDED: [], QUIET_GROUP: [], DECIDED: []}
    for item in items:
        kind, status = item["kind"], item["status"]
        if needs_you(item) and inside(item["updated_at"] if kind == "task" else item["created_at"]):
            rows[NEEDS].append(line(item))
        elif (kind, status) in (("pr", "merged"), ("task", "done")) and inside(item["updated_at"]):
            rows[LANDED].append(line(item))
        if kind == "decision" and inside(item["created_at"]):
            rows[DECIDED].append(line(item))
    for sid, q in quiet.items():
        if inside(q.get("at")) and not running(sid):
            rows[QUIET_GROUP].append((q["at"], Line(f"{q.get('name') or sid[:8]} {HOW.get(q.get('how'), q.get('how'))}",
                                                    sid, None)))
    groups = []
    for title, found in rows.items():
        if found:
            found.sort(key=lambda r: parse(r[0]) or start, reverse=True)
            groups.append(Group(f"{title} ({len(found)})" if title == DECIDED else title, [ln for _, ln in found]))
    return groups


def dealt_with(lines: list[Line], items) -> bool:
    """Every Needs-you line no longer waiting on the person (or gone)."""
    waiting = {(i["session_id"], i["ref"]) for i in items if needs_you(i)}
    return not any((ln.sid, ln.ref) in waiting for ln in lines)


def kept(raw: str | None) -> dict:
    """QUIET as stored: {} when there's none or it doesn't parse."""
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def seen(raw: str | None) -> dict | None:
    """SEEN as stored, each session's (name, parked, alive): None when there's none or it doesn't parse."""
    try:
        value = json.loads(raw) if raw else None
        return {sid: (str(v[0]), bool(v[1]), bool(v[2])) for sid, v in value.items()} if isinstance(value, dict) else None
    except (ValueError, TypeError, IndexError, AttributeError):
        return None


def observe(before: dict | None, after: dict, quiet: dict, at: datetime) -> dict | None:
    """QUIET with what changed between two reads of the sessions: before and after map each
    id to (name, parked, alive). A session parked, gone (ended), or alive then dead and not
    parked (stopped). Entries past KEPT dropped. None when nothing changed. A first read
    (before None) notes nothing: there is nothing to compare."""
    if before is None:
        return None
    noted = {}
    for sid, (name, parked, alive) in before.items():
        now_ = after.get(sid)
        if now_ is None:
            noted[sid] = "ended"
        elif now_[1] and not parked:
            noted[sid] = "parked"
        elif alive and not now_[2] and not now_[1]:
            noted[sid] = "stopped"
    cutoff = at - KEPT
    left = {sid: q for sid, q in quiet.items() if (t := parse(q.get("at") if isinstance(q, dict) else None)) and t >= cutoff}
    for sid, how in noted.items():
        name = (after.get(sid) or before[sid])[0]
        left[sid] = {"name": name, "how": how, "at": stamp(at)}
    return left if left != quiet else None
