"""The new-user tutorial: a real session, run in the wheelhouse, that walks the person
through the loop once (a task, two related questions, a decision, a permission prompt)
while a checklist ticks off each step from what the store records.

The tutorial session is the one whose directory is the tutorial's own scratch directory
(tutorial_dir), next to the database. Starting the tutorial always starts afresh: any
earlier tutorial session is ended (its items go with it) and its directory recreated.
"""

import json
import os
import shutil
import signal
import time
from pathlib import Path

from . import liveness
from .store import Store

NAME = "tutorial"
OFFER_KEY = "tutorial_offer"   # settings: unset until the first-run offer is taken or dismissed
SEEN_KEY = "tutorial_seen"     # settings: the steps only the screen knows about, for the current tutorial
STOP_WAIT = 5.0   # seconds an earlier tutorial's host gets to exit before it is told to
# what the tutorial's directory allows and asks: the wheelhouse's own tools run without a
# prompt, and one harmless command always asks, so a permission item appears even in auto mode
TOUCH = "touch tutorial-ok"
SETTINGS = {"permissions": {"allow": ["mcp__wheelhouse__*"], "ask": [f"Bash({TOUCH})"]}}

BRIEF = f"""This is the wheelhouse tutorial. You are a demo session: the person is learning the \
wheelhouse by working with you, and a checklist in the wheelhouse tells them what to do, so \
don't explain in chat how to use the wheelhouse. Keep chat short and about the work.

This is a throwaway session in a scratch directory. Touch no files and run no commands other \
than the one below. When you're asked to end, skip any session-end routine your own \
instructions describe (no memory, transcript or handoff notes): just call end_session.

Your job: draft a two-line welcome message for new wheelhouse users. Do this now, in order:

1. Set your synopsis: "Tutorial: drafting a welcome message with you."
2. Post a task, "Draft the welcome message", with status running.
3. Post two related questions as wheelhouse question items, each body standing on its own:
   - "Tone for the welcome message?": friendly, formal, or pirate.
   - "Sign it off as whom?": the wheelhouse, the team, or no sign-off. Say in the body that \
the right sign-off depends on the tone, so they're best answered together.
4. Post a decision: "Writing it in British English". Alternative: American English. Why: \
the wheelhouse's own text is British. To reverse: say so and it's rewritten.
5. Say in one line of chat that you're waiting on the two questions, then stop.

When the answers arrive, reply on each question with status answered, then write the \
two-line message in chat. Then run `{TOUCH}` to record that the tutorial ran (it asks the \
person's permission: that's expected). Once it has run, mark the task done and say in one \
line that you're finished and the session can be ended. If they ask for changes, make them."""

# (key, what to do, how): in the order the tutorial's session makes them possible
STEPS = (
    ("open", "Open a question", "highlight Q1 in the item list, or press Enter on it"),
    ("queue", "Answer both questions", "type in the box, Ctrl+Enter queues each answer"),
    ("send", "Send them together", "Ctrl+S, or the Send button below"),
    ("reply", "See the session's reply", "⏳ turns to answered as it replies"),
    ("decision", "Read and close the decision", "highlight D1 to read it, then X closes it, as it does a question"),
    ("permission", "Allow the permission prompt", "highlight P1, then the Allow button"),
    ("follow", "Follow the conversation", "the 💬 Conversation row, or the session on the left"),
    ("end", "End the session", "Sessions tab (2), then End: its items go with it"),
)


MARKER = ".wheelhouse-tutorial"   # written into the directory: only a directory carrying it is ever deleted


def tutorial_dir(store: Store) -> Path:
    return store.path.parent / "claude-wheelhouse-tutorial"


def is_tutorial(store: Store, session) -> bool:
    return session is not None and session["cwd"] == str(tutorial_dir(store))


def session_id(store: Store) -> str | None:
    """The tutorial's session, if one exists."""
    return next((s["id"] for s in store.sessions() if is_tutorial(store, s)), None)


def should_offer(store: Store) -> bool:
    """The first-run offer: a wheelhouse with no sessions that has never offered it."""
    return store.setting(OFFER_KEY) is None and not store.sessions()


def steps(store: Store, sid: str, seen: set[str]) -> list[tuple[str, str, str, bool]]:
    """Each step and whether it's done, from what the store records; `seen` holds the
    steps only the screen knows about (a question opened, the conversation followed)."""
    items = {it["ref"]: it for it in store.items(sid)}
    questions = {ref for ref, it in items.items() if it["kind"] == "question"}
    msgs = store.messages(sid)
    answers = [m for m in msgs if m["author"] == "person" and m["item_ref"] in questions]
    first_sent = min((m["id"] for m in answers if not m["draft"]), default=None)
    done = {
        "open": "open" in seen or bool(answers),
        "queue": len({m["item_ref"] for m in answers}) >= 2,
        "send": first_sent is not None,
        "reply": first_sent is not None and any(
            m["author"] == "claude" and m["kind"] == "reply" and m["item_ref"] in questions
            and m["id"] > first_sent for m in msgs),
        # closed, not seen: automatic selection marks a decision seen, but only the person closes one
        "decision": any(it["kind"] == "decision" and it["status"] == "closed" for it in items.values()),
        "permission": any(it["kind"] == "permission" and it["status"] == "allowed" for it in items.values()),
        "follow": "follow" in seen,
        "end": False,   # the checklist goes with the session
    }
    return [(key, what, how, done[key]) for key, what, how in STEPS]


def seen(store: Store, sid: str) -> set[str]:
    """The steps the person has done that only the screen sees (a question opened, the
    conversation followed). Kept in the store, so a restart keeps them."""
    try:
        kept = json.loads(store.setting(SEEN_KEY) or "{}")
    except ValueError:
        return set()
    return set(kept.get("seen", [])) if kept.get("sid") == sid else set()


def see(store: Store, sid: str, step: str) -> None:
    store.set_setting(SEEN_KEY, json.dumps({"sid": sid, "seen": sorted(seen(store, sid) | {step})}))


def stopped(session) -> str | None:
    """Why the tutorial's session stopped, if its host recorded a reason (Claude Code didn't
    start, say), so the checklist can say so rather than wait on a dead session."""
    activity = session["activity"] or ""
    return activity if activity.startswith("stopped") else None


def stop_earlier(store: Store) -> None:
    """End any earlier tutorial session. Its host sees the row go and exits within a
    second; one that hasn't after STOP_WAIT is told to."""
    for s in store.sessions():
        if not is_tutorial(store, s):
            continue
        store.end(s["id"])
        pid, start, boot = s["claude_pid"], s["claude_start"], s["boot_id"]
        deadline = time.monotonic() + STOP_WAIT
        while liveness.is_alive(pid, start, boot) and time.monotonic() < deadline:
            time.sleep(0.2)
        if liveness.is_alive(pid, start, boot):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:   # it went in the meantime
                pass


def prepare(store: Store) -> str:
    """Start afresh: end any earlier tutorial, recreate its directory with its settings, and
    create its session (run in the wheelhouse). Returns the new session's id."""
    stop_earlier(store)
    path = tutorial_dir(store)
    if path.exists():
        if not (path / MARKER).exists():
            raise FileExistsError(f"{path} exists and isn't the tutorial's: move it, then run the tutorial again")
        shutil.rmtree(path)
    (path / ".claude").mkdir(parents=True)
    (path / MARKER).write_text("The wheelhouse tutorial's scratch directory: recreated each time it starts.\n")
    (path / ".claude/settings.json").write_text(json.dumps(SETTINGS, indent=2) + "\n")
    return store.create_session(str(path), name=NAME, brief=BRIEF, runner="sdk")


def start(store: Store) -> str:
    """prepare(), then launch the session's host. Refuses before creating anything if
    Claude Code isn't installed: the host starts detached, so its own failure to start
    comes later, as the session's stop reason, which the checklist shows."""
    from .launch import open_host
    if shutil.which("claude") is None:
        raise RuntimeError("Claude Code (claude) isn't on PATH: install it, then run the tutorial")
    sid = prepare(store)
    try:
        open_host(store, sid)
    except BaseException:
        store.end(sid)   # no dead tutorial session left behind, nor its checklist
        raise
    store.set_setting(OFFER_KEY, "taken")
    return sid
