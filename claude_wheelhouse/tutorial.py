"""The new-user tutorial: a real session, opened in a Windows Terminal tab, that walks the
person through the loop once (a task, two related questions, a decision) while a checklist
ticks off each step from what the store records.

It is always a tab, SDK mode or not: the steps only a hosted session has (a permission
prompt, following the conversation) come back as SDK-only top-ups (.plan/tutorial.md).

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

from . import liveness, models
from .store import Store, runner

NAME = "tutorial"
OFFER_KEY = "tutorial_offer"   # settings: unset until the first-run offer is taken or dismissed
SEEN_KEY = "tutorial_seen"     # settings: the steps only the screen knows about, for the current tutorial
STOP_WAIT = 5.0   # seconds an earlier tutorial's host gets to exit before it is told to
# what the tutorial's directory allows: the wheelhouse's own tools run without a prompt
SETTINGS = {"permissions": {"allow": ["mcp__wheelhouse__*"]}}

BRIEF = """This is the wheelhouse tutorial. You are a demo session: the person is learning the \
wheelhouse by working with you, and a checklist in the wheelhouse tells them what to do, so \
don't explain in chat how to use the wheelhouse. Keep chat short and about the work.

This is a throwaway session in a scratch directory. Touch no files and run no commands. When \
you're asked to end, skip any session-end routine your own instructions describe (no memory, \
transcript or handoff notes): just call end_session.

Your job: draft a two-line welcome message for new wheelhouse users. Do this now, in order:

1. Set your synopsis: "Tutorial: drafting a welcome message with you."
2. Post a task, "Draft the welcome message", with status running.
3. Post two related questions as wheelhouse question items, each body standing on its own:
   - "Tone for the welcome message?": friendly, formal, or pirate.
   - "Sign it off as whom?": the wheelhouse, the team, or no sign-off. Say in the body that \
the right sign-off depends on the tone, so they're best answered together.
4. Post a decision: "Writing it in British English", why: over American English: the \
wheelhouse's own text is British; undo: say so and it's rewritten.
5. Say in one line of chat that you're waiting on the two questions, then stop.

When the answers arrive, reply to each question as the wheelhouse protocol says, marking it \
answered, with the two-line message as your answer to both. Then mark the task done, and end \
your turn with the message in chat and one last line saying you're finished and the session \
can be ended. If they ask for changes, make them."""

# (key, what to do, how): in the order the tutorial's session makes them possible
STEPS = (
    ("start", "Find the demo in its tab", "it opens in a new Windows Terminal tab: if Claude Code asks "
     "whether you trust the folder, say yes there, then come back here as it posts its items"),
    ("open", "Open a question", "highlight Q1 in the item list, or press Enter on it"),
    ("queue", "Answer both questions", "type in the box, Ctrl+Enter queues each answer"),
    ("send", "Send them together", "Ctrl+S, or the Send button under the session list"),
    ("reply", "See the session's reply", "its message shows in each question's thread as it finishes"),
    ("decision", "Read and close the decision", "highlight D1 to read it, then Delete (or Backspace) in the item list closes it, as it does a question"),
    ("end", "End the session", "highlight it in the session list, then End below: its items go with it, "
     "and its tab can be closed"),
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
    """Each step and whether it's done, from what the store records; `seen` holds the steps
    only the screen knows about (a question opened)."""
    items = {it["ref"]: it for it in store.items(sid)}
    questions = {ref for ref, it in items.items() if it["kind"] == "question"}
    msgs = store.messages(sid)
    answers = [m for m in msgs if m["author"] == "person" and m["item_ref"] in questions]
    first_sent = min((m["id"] for m in answers if not m["draft"]), default=None)
    done = {
        "start": bool(items),   # it has posted: it started, past any trust prompt in its tab
        "open": "open" in seen or bool(answers),
        "queue": len({m["item_ref"] for m in answers}) >= 2,
        "send": first_sent is not None,
        "reply": first_sent is not None and any(
            m["author"] == "claude" and m["kind"] == "reply" and m["item_ref"] in questions
            and m["id"] > first_sent for m in msgs),
        # closed, not seen: automatic selection marks a decision seen, but only the person closes one
        "decision": any(it["kind"] == "decision" and it["status"] == "closed" for it in items.values()),
        "end": False,   # the checklist goes with the session
    }
    return [(key, what, how, done[key]) for key, what, how in STEPS]


def seen(store: Store, sid: str) -> set[str]:
    """The steps the person has done that only the screen sees (a question opened). Kept in the store, so a restart keeps them."""
    try:
        kept = json.loads(store.setting(SEEN_KEY) or "{}")
    except ValueError:
        return set()
    return set(kept.get("seen", [])) if kept.get("sid") == sid else set()


def see(store: Store, sid: str, step: str) -> None:
    store.set_setting(SEEN_KEY, json.dumps({"sid": sid, "seen": sorted(seen(store, sid) | {step})}))


def stopped(session) -> str | None:
    """Why the tutorial's session stopped, once it isn't running, so the checklist can say
    so rather than wait on a dead session. A tab's: it never registered (wt.exe didn't open
    the tab, or Claude Code didn't start in it), or its tab has closed. A host's recorded
    reason, for a tutorial an earlier version ran in the wheelhouse."""
    if runner(session) == "tab":
        if session["claude_pid"] is None:
            return "stopped: its tab didn't start Claude Code (launch.log, beside the database, may say why)"
        return "stopped: its tab has closed"
    activity = session["activity"] or ""
    return activity if activity.startswith("stopped") else None


def stop_earlier(store: Store) -> None:
    """End any earlier tutorial session. Its host sees the row go and exits within a
    second; one that hasn't after STOP_WAIT is told to. A tab's Claude Code doesn't exit
    by itself, so it is told to at once."""
    for s in store.sessions():
        if not is_tutorial(store, s):
            continue
        store.end(s["id"])
        pid, start, boot = s["claude_pid"], s["claude_start"], s["boot_id"]
        deadline = time.monotonic() + (STOP_WAIT if runner(s) == "sdk" else 0)
        while liveness.is_alive(pid, start, boot) and time.monotonic() < deadline:
            time.sleep(0.2)
        if liveness.is_alive(pid, start, boot):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:   # it went in the meantime
                pass


def prepare(store: Store) -> str:
    """Start afresh: end any earlier tutorial, recreate its directory with its settings, and
    create its session, a tab whether SDK mode is on or not. Returns the new session's id."""
    stop_earlier(store)
    path = tutorial_dir(store)
    if path.exists():
        if not (path / MARKER).exists():
            raise FileExistsError(f"{path} exists and isn't the tutorial's: move it, then run the tutorial again")
        shutil.rmtree(path)
    (path / ".claude").mkdir(parents=True)
    (path / MARKER).write_text("The wheelhouse tutorial's scratch directory: recreated each time it starts.\n")
    (path / ".claude/settings.json").write_text(json.dumps(SETTINGS, indent=2) + "\n")
    model, effort = models.chosen(None, None)   # New session's defaults: Sonnet 5.5 at high
    return store.create_session(str(path), name=NAME, brief=BRIEF,
                                runner="tab", model=model, effort=effort)


def start(store: Store) -> str:
    """prepare(), then open the session in its tab. Refuses before creating anything if
    Claude Code isn't installed: the tab starts detached, so its own failure to start comes
    later, which the checklist shows."""
    from . import launch
    if shutil.which("claude") is None:
        raise RuntimeError("Claude Code (claude) isn't on PATH: install it, then run the tutorial")
    sid = prepare(store)
    try:
        launch.open_session(store, sid)
    except BaseException:
        store.end(sid)   # no dead tutorial session left behind, nor its checklist
        raise
    store.set_setting(OFFER_KEY, "taken")
    return sid
