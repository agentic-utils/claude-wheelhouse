"""Plugin monitor: runs for the whole session and prints the person's new messages as one
line, every message sent since the last poll together. Claude Code delivers each printed
line to Claude as a notification.
"""

import getpass
import os
import sqlite3
import sys
import time

from .store import GONE_TEXT, Store

POLL_SECONDS = 2
# Claude Code cuts a monitor notification at 500 characters and appends "...(truncated)"
# (observed on a 522-character line), so whatever follows the cut never reaches the
# session. Lines stay under it, and a cut-short message's pointer comes first in its block.
LINE_LIMIT = 480
MIN_TEXT = 40   # characters of a cut-short message worth showing before it waits for the next line
SEP = " ‖ "


def _pointer(m) -> str:
    return f"[cut short, full text: get_input(message_id={m['id']})] "


def _block(m, width: int) -> str:
    """The message in at most width characters: whole, or the pointer then as much as fits."""
    body = " ⏎ ".join(m["body"].splitlines())
    if len(body) <= width:
        return body
    return _pointer(m) + body[:max(0, width - len(_pointer(m)) - 1)] + "…"


def _notice(m) -> bool:
    """Sent by the wheelhouse itself (e.g. on adoption), not typed by the person."""
    return "kind" in m.keys() and m["kind"] == "notice"


def _where(m) -> str:
    return "(from the wheelhouse)" if _notice(m) else f"on {m['item_ref']}" if m["item_ref"] else "(general)"


def format_message(m, person: str | None = None) -> str:
    head = "[wheelhouse] " if _notice(m) else f"[wheelhouse] from {person or getpass.getuser()} {_where(m)}: "
    return head + _block(m, LINE_LIMIT - len(head))


def _widths(needs: list[int], room: int) -> list[int] | None:
    """Share room between blocks: short ones whole, long ones cut to an equal width that
    still shows MIN_TEXT of each. None if they can't all fit."""
    if sum(needs) <= room:
        return needs
    for i, need in enumerate(sorted(needs)):   # the shortest whole, the rest share what's left
        left = len(needs) - i
        cap = (room - sum(sorted(needs)[:i])) // left
        if cap < need:
            break
    if cap < MIN_TEXT + len("[cut short, full text: get_input(message_id=)] ") + 8:
        return None
    return [min(need, cap) for need in needs]


def format_batch(msgs, person: str | None = None) -> tuple[str, int]:
    """Several messages sent together, as one notification so the session reads them all
    before acting. Returns the line and how many messages it carries: those that don't fit
    in LINE_LIMIT are left out, with a note that they follow, for the caller to release to
    the next poll."""
    person = person or getpass.getuser()
    if len(msgs) == 1:
        return format_message(msgs[0], person), 1
    for n in range(len(msgs), 0, -1):
        if n == 1:
            rest = f"{SEP}… {len(msgs) - 1} more follow in the next notification"
            head = f"[wheelhouse] from {person} {_where(msgs[0])}: "
            return head + _block(msgs[0], LINE_LIMIT - len(head) - len(rest)) + rest, 1
        rest = f"{SEP}… {len(msgs) - n} more follow in the next notification" if n < len(msgs) else ""
        head = f"[wheelhouse] from {person}, {len(msgs)} answers: "
        wheres = [f"{_where(m)}: " for m in msgs[:n]]
        room = LINE_LIMIT - len(head) - len(rest) - len(SEP) * (n - 1) - sum(map(len, wheres))
        widths = _widths([len(" ⏎ ".join(m["body"].splitlines())) for m in msgs[:n]], room)
        if widths:
            return head + SEP.join(w + _block(m, width) for w, m, width in zip(wheres, msgs, widths)) + rest, n


REQUEST_TEXT = {
    "end": "[wheelhouse] The person pressed End in the wheelhouse. If your own instructions describe "
           "anything to do when a session ends, do it first, then call end_session.",
    "park": "[wheelhouse] The person pressed Park in the wheelhouse. Bring your wheelhouse items up "
            "to date, then call park_session.",
}


def retry(fn, attempts: int = 5, pause: float = 0.2):
    """Retry a write that hit "database is locked". Used for confirm: the messages have
    been printed, so leaving them claimed would print them again once the claim goes stale."""
    for attempt in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError:
            if attempt == attempts - 1:
                raise
            time.sleep(pause)


def poll_once(store: Store, sid: str, out=sys.stdout) -> int | None:
    """Pass on the person's messages (claim, print and flush, then confirm), then new End or
    Park requests. Messages go first so that a cancel of an earlier request is never heard
    after the fresh request that replaced it. If printing fails (stdout closed as the session
    dies), release the claim so the messages are delivered next time. Returns None once the
    session has gone."""
    session = store.session(sid)
    if session is None:
        print(GONE_TEXT, file=out, flush=True)
        return None
    msgs = store.claim(sid)
    shown = 0
    try:
        if msgs:   # everything sent since the last poll, as one notification
            line, n = format_batch(msgs)
            print(line, file=out, flush=True)
            shown = n   # any that didn't fit are released below, for the next poll
    finally:
        try:
            retry(lambda: store.confirm(msgs[:shown]))
        finally:
            store.release(msgs[shown:])
    for what, text in REQUEST_TEXT.items():
        asked = session[f"{what}_requested_at"]
        if asked and session[f"{what}_told_at"] != asked and store.tell_request(sid, what, asked):
            try:
                print(text, file=out, flush=True)
            except BaseException:
                store.untell_request(sid, what)
                raise
    return shown


def main(sid: str | None = None) -> None:
    sid = sid or os.environ["WHEELHOUSE_SESSION_ID"]
    store, reported = None, set()
    while True:
        try:
            if store is None:
                store = Store()
                store.mark_version(sid)   # tells the wheelhouse this session runs current code
            if poll_once(store, sid) is None:
                return
        except sqlite3.OperationalError as e:
            # e.g. "database is locked": anything unsent is still queued, so try again
            if str(e) not in reported:
                reported.add(str(e))
                print(f"[wheelhouse monitor] {e}: retrying", file=sys.stderr, flush=True)
        time.sleep(POLL_SECONDS)
