"""The new-user tutorial: a real session, opened in a Windows Terminal tab, that walks the
person through the loop once (a task, two related questions, a decision) while a checklist
ticks off each step from what the store records.

It is always a tab, SDK mode or not: the step only a hosted session has (a permission
prompt) comes back as an SDK-only top-up (.plan/tutorial.md). Following the conversation is a
tab's too: it is read from the session's transcript, whichever way it runs.

The checklist outlives the session for its last step: once the session has ended it shows
End and Start your own until a session of the person's own exists, or the app next starts
(current, drop_ended).

The tutorial session is the one whose directory is the tutorial's own scratch directory
(tutorial_dir), next to the database. Starting the tutorial always starts afresh: any
earlier tutorial session is ended (its items go with it) and its directory recreated.

The offer is how it starts. `claude-wheelhouse tutorial` (reset) doesn't start it: run while
no wheelhouse is, it ends any tutorial session and has the next start offer it again.
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
RESET = "reset"   # OFFER_KEY as reset() leaves it: offered at the next start, sessions or not, until answered
SEEN_KEY = "tutorial_seen"     # settings: the current tutorial's record (record): its screen-only steps
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
your turn with the message in chat and one last line asking whether they'd like anything \
changed. When they ask for a change, make it in chat, then say in one line that you're \
finished and the session can be ended."""

# (key, what to do, how): in the order the tutorial's session makes them possible
STEPS = (
    ("start", "Find the demo in its tab", "it opens in a new Windows Terminal tab, in a window of the "
     "wheelhouse's own: if Claude Code asks whether you trust the folder, say yes there, then come back here as "
     "it posts its items"),
    ("open", "Open a question", "highlight Q1 in the item list, or press Enter on it"),
    ("queue", "Answer both questions", "type in the answer box under its thread, Ctrl+Enter queues each answer"),
    ("send", "Send them together", "Ctrl+S, or the Send button under the session list"),
    ("reply", "See the session's reply", "its message shows in each question's thread as it finishes"),
    ("follow", "Watch it work", "highlight the session on the left (or its 💬 Conversation row), then ask "
     "for a change in the answer box under it: Ctrl+Enter queues it and, as it runs in a tab, Ctrl+S sends "
     "it. Its turns show as it works"),
    ("decision", "Read and close the decision", "highlight D1 to read it, then Delete (or Backspace) in the item list closes it, as it does a question"),
    ("end", "End the session", "highlight it in the session list, then End below: its items go with it, "
     "and its tab can be closed"),
    ("new", "Start your own", "N opens New: pick a directory, give it an opening brief, and it starts. "
     "After a reboot, Shift+S restores your sessions"),
)
AFTER_END = ("end", "new")   # the steps the checklist keeps once the session has ended (current)


# what has the next start offer the tutorial again (reset), named by every hint (the offer, the
# ? overlay, the checklist, the dismiss notice): fixed, as which copy the person's own shell
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


def steps(store: Store, sid: str, seen: set[str], before=None) -> list[tuple[str, str, str, bool]]:
    """Each step and whether it's done, from what the store records; `seen` holds the steps
    only the screen knows about (a question opened, a turn watched arriving), `before` the
    sessions there were as the tutorial started (None, from a record without them: Start your
    own can't tick, as every session would look new). Once its session has ended, only the steps
    after it (AFTER_END), End done."""
    ended = store.session(sid) is None
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
        "follow": "follow" in seen,
        # closed, not seen: automatic selection marks a decision seen, but only the person closes one
        "decision": any(it["kind"] == "decision" and it["status"] == "closed" for it in items.values()),
        "end": ended,
        # a session of the person's own: one there wasn't as the tutorial started, outside its directory
        "new": before is not None and any(s["id"] not in before and not is_tutorial(store, s)
                                          for s in store.sessions()),
    }
    return [(key, what, how, done[key]) for key, what, how in STEPS if not ended or key in AFTER_END]


def record(store: Store) -> dict:
    """The current tutorial's record (SEEN_KEY): its session (sid), the steps only the screen
    has seen, and the sessions there were as it started (before, from prepare)."""
    try:
        kept = json.loads(store.setting(SEEN_KEY) or "{}")
    except ValueError:
        return {}
    return kept if isinstance(kept, dict) else {}


def current(store: Store) -> tuple[str, list] | None:
    """The checklist, if one shows: the tutorial session's steps, or, once that has ended,
    End and Start your own, until a session of the person's own exists (its record is then
    cleared, so ending that session doesn't bring the checklist back) or the app next starts
    (drop_ended). Only a tutorial prepare() recorded is followed past its end: an earlier
    version's record has no before."""
    sid, kept = session_id(store), record(store)
    if sid is None and "before" in kept:
        sid = kept.get("sid")
    if sid is None:
        return None
    found = steps(store, sid, seen(store, sid), kept.get("before") if kept.get("sid") == sid else None)
    if store.session(sid) is None and all(done for *_, done in found):
        store.clear_setting(SEEN_KEY)
        return None
    return sid, found


def drop_ended(store: Store) -> None:
    """As the app starts: an ended tutorial's checklist (current) stays only for the app run
    it ended in, so its record goes. A running tutorial's is kept."""
    if session_id(store) is None and record(store):
        store.clear_setting(SEEN_KEY)


def seen(store: Store, sid: str) -> set[str]:
    """The steps the person has done that only the screen sees (a question opened, a turn
    watched arriving). Kept in the store, so a restart keeps them."""
    kept = record(store)
    return set(kept.get("seen", [])) if kept.get("sid") == sid else set()


def see(store: Store, sid: str, step: str) -> None:
    kept = record(store)
    kept = kept if kept.get("sid") == sid else {"sid": sid}
    store.set_setting(SEEN_KEY, json.dumps({**kept, "seen": sorted(set(kept.get("seen", [])) | {step})}))


def stopped(session) -> str | None:
    """Why the tutorial's session stopped, once it isn't running, so the checklist can say
    so rather than wait on a dead session. A tab's: it never registered for this launch (wt.exe
    didn't open the tab, or Claude Code didn't start in it: liveness.registered_this_launch,
    since a pid can be the last launch's), or its tab has closed. A host's recorded
    reason, for a tutorial an earlier version ran in the wheelhouse."""
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
    a tutorial session was ended. Its record goes too, so no checklist stays for it."""
    ended = session_id(store) is not None
    stop_earlier(store)
    store.clear_setting(SEEN_KEY)
    store.set_setting(OFFER_KEY, RESET)
    return ended


def prepare(store: Store) -> str:
    """Start afresh: end any earlier tutorial, recreate its directory with its settings, and
    create its session, a tab whether SDK mode is on or not, recording the sessions there are
    already (record: none of them is the person's new one). Returns the new session's id."""
    stop_earlier(store)
    before = [s["id"] for s in store.sessions()]
    path = tutorial_dir(store)
    if path.exists():
        if not (path / MARKER).exists():
            raise FileExistsError(f"{path} exists and isn't the tutorial's: move it, then run the tutorial again")
        shutil.rmtree(path)
    (path / ".claude").mkdir(parents=True)
    (path / MARKER).write_text("The wheelhouse tutorial's scratch directory: recreated each time it starts.\n")
    (path / ".claude/settings.json").write_text(json.dumps(SETTINGS, indent=2) + "\n")
    model, effort = models.chosen(None, None)   # New session's defaults: Sonnet 5.5 at high
    sid = store.create_session(str(path), name=NAME, brief=BRIEF, runner="tab", model=model, effort=effort)
    store.set_setting(SEEN_KEY, json.dumps({"sid": sid, "seen": [], "before": before}))
    return sid


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
        store.clear_setting(SEEN_KEY)
        raise
    store.set_setting(OFFER_KEY, "taken")
    return sid
