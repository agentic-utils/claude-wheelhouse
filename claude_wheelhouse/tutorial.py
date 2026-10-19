"""The new-user tutorial's demo session: a real session that walks the person through the
loop once (a task, two related questions, a decision) while the tour (tour.py, painted by
the app) points at each pane in turn and moves on as the store records each step (facts).

The person starts it themselves, from New session filled in (form), so the first thing they
learn is how every session starts. It runs as New session's default does: hosted in the
wheelhouse with SDK mode on, else in a Windows Terminal tab.

The tutorial session is the one whose directory is the tutorial's own scratch directory
(tutorial_dir), next to the database. Taking the tour always starts afresh: any earlier
tutorial session is ended (its items go with it) and its directory recreated (prepare).

The offer is how it starts. `claude-wheelhouse tutorial` (reset) doesn't start it: run while
no wheelhouse is, it ends any tutorial session and has the next start offer it again.
What a start offers (tour.opening) is read from state.
"""

import json
import os
import shutil
import signal
import time
from pathlib import Path

from . import liveness, models
from .store import NOTES_SEEN, TOUR_AT, TUTORIAL_OFFER, Store, runner

NAME = "tutorial"
OFFER_KEY = TUTORIAL_OFFER   # settings: unset until the first-run offer is taken, or answered Never (store.NEVER)
RESET = "reset"   # OFFER_KEY as reset() leaves it: offered at the next start, sessions or not, until answered
STOP_WAIT = 5.0   # seconds an earlier tutorial's host gets to exit before it is told to
STORY = "story.md"   # the one file the demo writes, the doc the tour has the person open
# what the tutorial's directory allows: the wheelhouse's own tools, and writing the story, run without a prompt
SETTINGS = {"permissions": {"allow": ["mcp__wheelhouse__*", f"Write(./{STORY})", f"Edit(./{STORY})"]}}

# the brief's first line, for the person watching the session's tab or conversation
TO_THE_USER = ("TO THE USER: THIS IS A DUMMY SESSION FOR THE TUTORIAL. FOLLOW THE PROMPTS ON THE WHEELHOUSE SCREEN "
               "TO LEARN HOW TO USE IT.")

BRIEF = TO_THE_USER + """

That line is for the person, not you: ignore it.

This is the wheelhouse tutorial. You are a demo session: the person is learning the \
wheelhouse by working with you, and the wheelhouse's tour tells them what to do, so \
don't explain in chat how to use the wheelhouse. Keep chat short and about the work.

This is a throwaway session in a scratch directory. The only file you touch is story.md in \
this directory; run no commands and use no subagents. When you're asked to end, skip any \
session-end routine your own instructions describe (no memory, transcript or handoff notes): \
just call end_session.

Your job: write a short children's bedtime story with the person. Do this now, in order:

1. Set your synopsis: "Tutorial: writing a bedtime story with you."
2. Post a task, "Write the bedtime story", with status running.
3. Post two related questions as wheelhouse question items, each body standing on its own:
   - "Who are the story's main characters?": a sleepy dragon and a brave mouse, two owl \
siblings, or a lighthouse keeper and her cat.
   - "Where is the story set?": a moonlit forest, a seaside village, or a castle in the \
clouds. Say in the body that the setting should suit the characters, so they're best \
answered together.
4. Post a decision: "Keeping the story under 200 words", why: over a longer one: it's for \
reading aloud at bedtime; undo: say so and it's lengthened.
5. Say in one line of chat that you're waiting on the two questions, then stop.

When the answers arrive, write the story (under 200 words, in British English, with a title) \
as markdown to story.md in this directory, and declare it as a doc item: post_item with \
kind "doc", title "The bedtime story", path story.md's absolute path, and the task as its \
parent. Then reply to each question as the wheelhouse protocol says, marking it answered, \
saying the story is in the doc. Then mark the task done, and end your turn with one line \
asking whether they'd like anything changed. When they ask for a change, make it in \
story.md, post the doc again with the same path, then say in one line that you're finished \
and the session can be ended."""

# what has the next start offer the tutorial again (reset), named by every hint (the offer, the
# ? overlay, the tour, the dismiss notice): fixed, as which copy the person's own shell
# runs can't be told from here
COMMAND = "claude-wheelhouse tutorial (make tutorial in a source checkout)"
# every hint shown in a running app says so: the command is refused while the wheelhouse runs
AGAIN = f"With the wheelhouse stopped, {COMMAND} offers the tutorial again"   # named: a notice stands alone


MARKER = ".wheelhouse-tutorial"   # written into the directory: only a directory carrying it is ever deleted


def tutorial_dir(store: Store) -> Path:
    return store.path.parent / "claude-wheelhouse-tutorial"


def is_tutorial(store: Store, session) -> bool:
    return session is not None and session["cwd"] == str(tutorial_dir(store))


def session_id(store: Store) -> str | None:
    """The tutorial's session, if one exists."""
    return next((s["id"] for s in store.sessions() if is_tutorial(store, s)), None)


def should_offer(store: Store) -> bool:
    """The first-run offer: a wheelhouse with no sessions that has never offered it, or one
    reset() has asked to offer it again. Answering it either way overwrites the setting."""
    offer = store.setting(OFFER_KEY)
    return offer == RESET or (offer is None and not store.sessions())


def state(store: Store) -> dict:
    """What the app decides the tour's offer from (tour.opening): whether the whole tour is
    offered (should_offer), where a tour under way or paused resumes (TOUR_AT), the features
    seen (store.tour_seen) and the last version whose notes were shown (NOTES_SEEN)."""
    return {"offer": should_offer(store), "at": store.setting(TOUR_AT), "seen": sorted(store.tour_seen()),
            "notes_seen": store.setting(NOTES_SEEN)}


def stopped(session) -> str | None:
    """Why the tutorial's session stopped, once it isn't running, so the tour can say
    so rather than wait on a dead session. A tab's: it never registered for this launch (wt.exe
    didn't open the tab, or Claude Code didn't start in it: liveness.registered_this_launch,
    since a pid can be the last launch's), or its tab has closed. A host's: its recorded
    reason."""
    if runner(session) == "tab":
        never = session["claude_pid"] is None \
            or (session["launched_at"] and not liveness.registered_this_launch(session))
        if never:
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


def reset(store: Store) -> bool:
    """claude-wheelhouse tutorial: end any tutorial session (stop_earlier: the person's other
    sessions are kept) and have the next start offer the tutorial, sessions or not. Only while
    no wheelhouse runs (cli.py), as the app decides on the offer once, as it starts. Whether
    a tutorial session was ended."""
    ended = session_id(store) is not None
    stop_earlier(store)
    store.set_setting(OFFER_KEY, RESET)
    store.clear_setting(TOUR_AT)   # a tour paused before is over: the whole tour is offered afresh
    return ended


def prepare(store: Store) -> Path:
    """Start afresh: end any earlier tutorial and recreate its directory with its settings,
    for the demo session the tour has the person start from New session (form). Returns the
    directory."""
    stop_earlier(store)
    path = tutorial_dir(store)
    if path.exists():
        if not (path / MARKER).exists():
            raise FileExistsError(f"{path} exists and isn't the tutorial's: move it, then run the tutorial again")
        shutil.rmtree(path)
    (path / ".claude").mkdir(parents=True)
    (path / MARKER).write_text("The wheelhouse tutorial's scratch directory: recreated each time it starts.\n")
    (path / ".claude/settings.json").write_text(json.dumps(SETTINGS, indent=2) + "\n")
    return path


def start(store: Store) -> Path:
    """The tour taken: prepare() its directory and settle the offer. Refuses before anything
    is touched if Claude Code isn't installed. The session itself is the person's to start,
    from New session filled in (form)."""
    if shutil.which("claude") is None:
        raise RuntimeError("Claude Code (claude) isn't on PATH: install it, then run the tutorial")
    path = prepare(store)
    store.set_setting(OFFER_KEY, "taken")
    return path


def form(path: str) -> dict:
    """New session's fields as the tour fills them in, for the directory prepare() made: the
    name, the brief and New session's own default model and effort. Its runner is New
    session's default, so the demo is hosted in the wheelhouse when SDK mode is on."""
    model, effort = models.chosen(None, None)
    return {"cwd": path, "name": NAME, "brief": BRIEF, "model": model, "effort": effort}


def facts(store: Store) -> dict | None:
    """What the tour's dialogs wait on that the store records, for the tutorial's session
    (None while there is none): it has posted, a question has an answer, both have, the
    answers were sent, the session replied after that, and the decision was closed (closed,
    not seen: automatic selection marks a decision seen, but only the person closes one). A
    question the person closed unanswered counts as answered; with none answered at all,
    there is nothing to send or reply to, so those count as done."""
    sid = session_id(store)
    if sid is None:
        return None
    items = {it["ref"]: it for it in store.items(sid)}
    questions = {ref for ref, it in items.items() if it["kind"] == "question"}
    msgs = store.messages(sid)
    answers = [m for m in msgs if m["author"] == "person" and m["item_ref"] in questions]
    first_sent = min((m["id"] for m in answers if not m["draft"]), default=None)
    answered = {m["item_ref"] for m in answers}
    queued = len(questions) >= 2 and all(q in answered or items[q]["status"] == "closed" for q in questions)
    none = queued and not answers   # every question closed unanswered
    return {
        "sid": sid,
        "posted": bool(items),   # it started, past any trust prompt in its tab
        "answered": bool(answers),
        "queued": queued,
        "sent": first_sent is not None or none,
        "replied": none or first_sent is not None and any(
            m["author"] == "claude" and m["kind"] == "reply" and m["item_ref"] in questions
            and m["id"] > first_sent for m in msgs),
        "closed": any(it["kind"] == "decision" and it["status"] == "closed" for it in items.values()),
    }
