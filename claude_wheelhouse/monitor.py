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
INLINE_LIMIT = 1500   # characters of message text in one notification
LINE_LIMIT = INLINE_LIMIT + 200   # the whole line: text, refs and pointers
BLOCK_OVERHEAD = 80   # roughly a block's ref, separator and cut-short pointer


def _block(m, limit: int) -> str:
    body = " ⏎ ".join(m["body"].splitlines())
    if len(body) > limit:
        body = body[:limit] + f" … [cut short: call get_input(message_id={m['id']}) for the rest]"
    return body


def _notice(m) -> bool:
    """Sent by the wheelhouse itself (e.g. on adoption), not typed by the person."""
    return "kind" in m.keys() and m["kind"] == "notice"


def _where(m) -> str:
    return "(from the wheelhouse)" if _notice(m) else f"on {m['item_ref']}" if m["item_ref"] else "(general)"


def format_message(m, person: str | None = None) -> str:
    if _notice(m):
        return f"[wheelhouse] {_block(m, INLINE_LIMIT)}"
    return f"[wheelhouse] from {person or getpass.getuser()} {_where(m)}: {_block(m, INLINE_LIMIT)}"


def format_batch(msgs, person: str | None = None) -> tuple[str, int]:
    """Several messages sent together, as one notification so the session reads them all
    before acting. Each block shares the inline limit. Returns the line and how many
    messages it carries: blocks that would push the line past LINE_LIMIT are left out, with
    a note that they follow, for the caller to release to the next poll."""
    person = person or getpass.getuser()
    if len(msgs) == 1:
        return format_message(msgs[0], person), 1
    limit = max(100, INLINE_LIMIT // len(msgs) - BLOCK_OVERHEAD)
    blocks = [f"{_where(m)}: {_block(m, limit)}" for m in msgs]
    for n in range(len(msgs), 0, -1):
        rest = f" ‖ … {len(msgs) - n} more follow in the next notification" if n < len(msgs) else ""
        line = f"[wheelhouse] from {person}, {len(msgs)} answers: " + " ‖ ".join(blocks[:n]) + rest
        if len(line) <= LINE_LIMIT or n == 1:
            return line, n


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
