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
INLINE_LIMIT = 1500


def _block(m, limit: int) -> str:
    body = " ⏎ ".join(m["body"].splitlines())
    if len(body) > limit:
        where = f'get_input("{m["item_ref"]}")' if m["item_ref"] else "get_input()"
        body = body[:limit] + f" … [cut short: call {where} for the rest]"
    return body


def _where(m) -> str:
    return f"on {m['item_ref']}" if m["item_ref"] else "(general)"


def format_message(m, person: str | None = None) -> str:
    return f"[wheelhouse] from {person or getpass.getuser()} {_where(m)}: {_block(m, INLINE_LIMIT)}"


def format_batch(msgs, person: str | None = None) -> str:
    """Several messages sent together, as one notification so the session reads them all
    before acting. Each block shares the inline limit."""
    if len(msgs) == 1:
        return format_message(msgs[0], person)
    limit = max(100, INLINE_LIMIT // len(msgs))
    blocks = " ‖ ".join(f"{_where(m)}: {_block(m, limit)}" for m in msgs)
    return f"[wheelhouse] from {person or getpass.getuser()}, {len(msgs)} answers: {blocks}"


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
            print(format_batch(msgs), file=out, flush=True)
            shown = len(msgs)
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
            store = store or Store()
            if poll_once(store, sid) is None:
                return
        except sqlite3.OperationalError as e:
            # e.g. "database is locked": anything unsent is still queued, so try again
            if str(e) not in reported:
                reported.add(str(e))
                print(f"[wheelhouse monitor] {e}: retrying", file=sys.stderr, flush=True)
        time.sleep(POLL_SECONDS)
