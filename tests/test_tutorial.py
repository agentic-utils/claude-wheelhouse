import json
import sys

import pytest
from textual.widgets import Button, DataTable, Input, TextArea

from claude_wheelhouse import callout, features, launch, tutorial, whatsnew
from claude_wheelhouse.callout import HIGHLIGHT, TourCallout
from claude_wheelhouse.store import NOTES_SEEN
from claude_wheelhouse.tour import Tour
from claude_wheelhouse.tui import BUTTONS, Compose, Dialog, ItemList, KeysHelp, NewSession, ThreadView, Transcript, \
    TourInvite, TourNotes, TourStop, SDK_BUTTONS, WheelhouseApp, key_name, keys_help
from claude_wheelhouse.tutorial import should_offer as real_should_offer   # before conftest stubs it
from test_tui import dwell, one_second


def demo(store, runner="tab"):
    """The demo session, as New session filled in by the tour makes it."""
    form = tutorial.form(str(tutorial.prepare(store)))
    return store.create_session(form["cwd"], name=form["name"], brief=form["brief"], runner=runner,
                                model=form["model"], effort=form["effort"])


@pytest.fixture
def tut(store):
    return demo(store)


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """A `claude` on PATH that fails at once, as a broken install would."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    claude = bin_dir / "claude"
    claude.write_text('#!/bin/sh\necho "claude: broken install" >&2\nexit 1\n')
    claude.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    return claude


def setup_items(store, sid):
    """What the demo posts first."""
    store.post_item(sid, "task", "Draft the welcome message", status="running")
    q1 = store.post_item(sid, "question", "Tone?")
    q2 = store.post_item(sid, "question", "Sign-off?")
    store.post_item(sid, "decision", "British English", why="over American: house style; undo: say so")
    return q1, q2


def tour_at(app, stop: str, sid: str | None = None) -> None:
    """The tour under way at one of its dialogs, its demo `sid`."""
    app.tour = Tour(app.client.sdk)
    app.tour.at = [d.id for d in app.tour.stops].index(stop)
    app.tour.sid = sid
    app.tour_showing = None
    app.show_tour()


def shown(app) -> tuple[str | None, str | None]:
    """The tour's dialog showing, and the pane its callout is beside (None: centred, or none)."""
    found = list(app.screen_stack[0].query(TourCallout))
    return (app.tour.stop.id if app.tour else None), (found[0].pane if found else None)


def test_the_brief_speaks_to_the_person_first():
    """Doug (#78, 7): a note in caps to the person watching the demo."""
    assert tutorial.BRIEF.startswith("TO THE USER: THIS IS A DUMMY SESSION FOR THE TUTORIAL. FOLLOW THE PROMPTS ON "
                                     "THE WHEELHOUSE SCREEN TO LEARN HOW TO USE IT.\n\nThat line is for the person")


def test_prepare_starts_afresh(store, tmp_path):
    other = store.create_session(str(tmp_path), name="real work")
    store.post_item(other, "task", "keep me")
    old = demo(store)
    store.post_item(old, "question", "from last time")
    (tutorial.tutorial_dir(store) / "tutorial-ok").touch()
    path = tutorial.prepare(store)
    assert path == tutorial.tutorial_dir(store)
    assert store.session(old) is None and store.items(old) == [], "the earlier tutorial and its items are gone"
    assert [it["title"] for it in store.items(other)] == ["keep me"], "other sessions are untouched"
    assert not (path / "tutorial-ok").exists(), "its directory is recreated"
    assert json.loads((path / ".claude/settings.json").read_text()) == tutorial.SETTINGS
    assert "touch" not in json.dumps(tutorial.SETTINGS), "asking no permission"
    assert tutorial.session_id(store) is None, "the demo is the person's to start, from New session"
    assert tutorial.form(str(path)) == {"cwd": str(path), "name": "tutorial", "brief": tutorial.BRIEF,
                                        "model": "claude-sonnet-5-5", "effort": "high"}, "New session's defaults"


@pytest.mark.parametrize("sdk, runner_, desc", [
    (False, "tab", "SDK mode off: the demo runs in a tab"),
    (True, "sdk", "SDK mode on: the demo is hosted in the wheelhouse (Doug, #78 6)"),
])
@pytest.mark.both
@pytest.mark.anyio
async def test_the_tour(store, monkeypatch, fake_claude, sdk, runner_, desc):
    """Doug (#78): the principles first, then a walk round the screen in dialogs painted over
    the panes, starting with the demo's creation (N, New session filled in: Q119), and last
    this release's notes. Each action dialog moves on as the person does what it asks."""
    monkeypatch.setattr(features, "_flag", sdk)
    monkeypatch.delenv("WHEELHOUSE_RUNNER")   # New session's own default
    monkeypatch.setattr(tutorial, "should_offer", real_should_offer)
    opened = []
    monkeypatch.setattr(launch, "open_tab", lambda s, i: opened.append(("tab", i)))
    monkeypatch.setattr(launch, "open_host", lambda s, i: opened.append(("sdk", i)))
    app = WheelhouseApp(store)

    async def step(act, expected, why):
        act()
        app.refresh_data()
        await pilot.pause()
        assert shown(app) == expected, f"{desc}: {why}"

    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        await pilot.press("enter")   # the invite's Start
        await pilot.pause()
        assert isinstance(app.screen, TourStop), f"{desc}: the principles first"
        assert "Every Claude session in one place" in str(app.screen.query_one("#tour-stop-text").render())
        await pilot.press("enter")   # Next
        await pilot.pause()
        assert shown(app) == ("create", "sessions"), f"{desc}: creating the demo comes first"
        assert app.focused is None or not isinstance(app.focused, Button), "an action callout leaves focus be"
        await pilot.press("n")
        await pilot.pause()
        form = app.screen
        assert isinstance(form, NewSession), desc
        cwd = form.query_one("#cwd", Input)
        assert cwd.value == str(tutorial.tutorial_dir(store)) and cwd.disabled, f"{desc}: its directory, fixed"
        assert form.query_one("#name", Input).value == "tutorial"
        assert form.query_one("#brief", TextArea).text == tutorial.BRIEF, f"{desc}: the brief, its caps line first"
        assert "press Launch" in str(form.query_one("#tour-line").render()), f"{desc}: the tour's line on top"
        assert not app.screen_stack[0].query_one(TourCallout).display, f"{desc}: the callout hidden under it"
        form.query_one("#launch", Button).press()
        await pilot.pause()
        sid = tutorial.session_id(store)
        assert store.session(sid)["runner"] == runner_ and opened == [(runner_, sid)], desc
        await step(lambda: None, ("sessions", "sessions"), "its row appeared")
        q1, q2 = None, None

        def post():
            nonlocal q1, q2
            q1, q2 = setup_items(store, sid)
        await step(post, ("inbox", "items"), "it posted")
        await step(lambda: store.queue(sid, "pirate", q1), ("answer", "detail"), "a question answered: it was opened")
        await step(lambda: store.queue(sid, "Holly", q2), ("send", "session-info"), "both answered")
        await step(lambda: store.dispatch(sid), ("reply", "detail"), "sent")
        await step(lambda: store.reply(sid, q1, "noted", status="answered"), ("decision", "items"), "it replied")
        await step(lambda: store.close_decision(sid, "D1"), ("conversation", "detail"), "the decision closed")
        app.query_one("#answer", TextArea).focus()
        await step(lambda: app.saw(sid, None, "follow"), ("stats", "stats"), "a turn watched arriving")
        assert isinstance(app.focused, TextArea), f"{desc}: a Next callout leaves focus in the answer box"
        app.screen_stack[0].query_one("#tour-next", Button).press()
        await pilot.pause()
        assert shown(app) == ("end", "session-info"), f"{desc}: Next"
        await step(lambda: store.end(sid), ("notes", None), "the demo gone")
        assert isinstance(app.screen, TourNotes), desc
        notes = app.screen.notes
        assert f"What's new in {whatsnew.running_version()}" in notes and "guided tour" in notes, \
            f"{desc}: this release's notes, its unreleased features while a development version"
        await pilot.press("enter")
        await pilot.pause()
        assert app.tour is None and not isinstance(app.screen, TourNotes), f"{desc}: the tour over"
    assert store.tour_seen() == {f.id: "shown" for f in whatsnew.FEATURES}, f"{desc}: every feature seen"
    assert store.setting(NOTES_SEEN) == whatsnew.running_version(), desc


@pytest.mark.parametrize("stop", sorted({d.pane for d in whatsnew.DIALOGS if d.pane}))
@pytest.mark.anyio
async def test_every_tour_pane_is_on_the_screen(store, stop):
    """Each dialog's pane id finds its pane in the default layout (callout.PANES)."""
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        pane = callout.find(app.screen, stop)
        assert pane is not None and pane.display and pane.region.area, stop


@pytest.mark.parametrize("stop, pane, focus, desc", [
    ("create", "sessions", False, "an action callout leaves focus where it was"),
    ("inbox", "items", False, "so does this one: the keys it teaches reach the app"),
    ("stats", "stats", True, "a Next callout takes it, so Enter is Next"),
    ("send", "session-info", False, "the send dialog, beside the session's info"),
    ("answer", "detail", False, "the answer dialog, beside Detail"),
])
@pytest.mark.parametrize("size", [(160, 40), (80, 24)])
@pytest.mark.anyio
async def test_a_callout_beside_its_pane(store, stop, pane, focus, desc, size):
    app = WheelhouseApp(store)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        tour_at(app, stop)
        await pilot.pause()
        assert shown(app) == (stop, pane), desc
        assert HIGHLIGHT in callout.find(app.screen, pane).classes, f"{desc}: its pane highlighted"
        assert (app.focused is not None and app.focused.id == "tour-next") is focus, desc
        box = app.screen.query_one(TourCallout).region
        assert box.area and app.screen.region.contains_region(box), f"{desc} at {size}: {box} on screen"


@pytest.mark.parametrize("stop, focus, pauses, desc", [
    ("principles", None, True, "Esc on the principles"),
    ("stats", None, True, "Esc on a callout that has focus"),
    ("inbox", None, False, "an action callout has no focus: Esc keeps its own meaning"),
    ("stats", "#answer", False, "focus in the answer box: Esc is the box's, not the tour's"),
])
@pytest.mark.anyio
async def test_esc_pauses_the_tour(store, stop, focus, pauses, desc):
    """Q122: Esc pauses. Until stage 4 resumes it, the next start offers the tour again."""
    app = WheelhouseApp(store)
    said = []
    app.notify = lambda text, **kw: said.append(text)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        if focus:
            app.query_one(focus).focus()
            await pilot.pause()
        tour_at(app, stop)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.tour.paused is pauses, desc
        assert bool(app.screen_stack[0].query(TourCallout)) is not pauses, desc
        assert ("Tour paused: the next start offers it again." in said) is pauses, desc
        assert (store.setting(tutorial.OFFER_KEY) == tutorial.RESET) is pauses, f"{desc}: offered again"
        if pauses:
            assert not isinstance(app.screen, TourStop), desc
            app.refresh_data()
            await pilot.pause()
            assert not app.screen_stack[0].query(TourCallout), f"{desc}: it stays paused"


@pytest.mark.anyio
async def test_the_tour_never_takes_focus_from_a_box_being_typed_in(store, monkeypatch):
    """Review of #93: a Next callout appearing mid-word took focus, so the next letter, n,
    opened New session. Nor does a centred dialog come up over the typing."""
    sid = demo(store)
    q1, q2 = setup_items(store, sid)
    for act in (lambda: store.queue(sid, "a", q1), lambda: store.queue(sid, "b", q2), lambda: store.dispatch(sid),
                lambda: store.reply(sid, q1, "ok", status="answered"), lambda: store.close_decision(sid, "D1")):
        act()
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        tour_at(app, "conversation", sid)
        box = app.query_one("#answer", TextArea)
        box.focus()
        await pilot.press("h", "i")
        app.saw(sid, None, "follow")   # the Stats callout, a Next one, arrives mid-word
        await pilot.pause()
        assert shown(app) == ("stats", "stats") and app.focused is box, "the box keeps focus"
        await pilot.press("n")
        await pilot.pause()
        assert box.text == "hin" and not isinstance(app.screen, NewSession), "n is typed, not New session"
        tour_at(app, "notes", sid)
        await pilot.pause()
        assert not isinstance(app.screen, TourNotes) and app.focused is box, "the notes wait while there's typing"
        box.text = ""
        app.refresh_data()
        await pilot.pause()
        assert isinstance(app.screen, TourNotes), "and come once the box is empty"


@pytest.mark.parametrize("focus, desc", [(None, "nothing focused behind it"), ("#answer", "the answer box focused behind it")])
@pytest.mark.anyio
async def test_n_on_the_notes_opens_new_session(store, focus, desc):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        if focus:
            app.query_one(focus).focus()
        tour_at(app, "notes")
        await pilot.pause()
        assert isinstance(app.screen, TourNotes), desc
        await pilot.press("n")
        await pilot.pause()
        assert isinstance(app.screen, NewSession) and app.tour is None, desc


@pytest.mark.parametrize("act, then, desc", [
    (lambda store, sid, q1, q2: store.update_item(sid, q2, status="closed"), "send",
     "the other question closed unanswered: as if answered"),
    (lambda store, sid, q1, q2: (store.update_item(sid, q1, status="closed"), store.update_item(sid, q2, status="closed")),
     "decision", "both closed unanswered: nothing to send or wait for a reply to"),
])
@pytest.mark.anyio
async def test_a_question_closed_unanswered(store, act, then, desc):
    sid = demo(store)
    q1, q2 = setup_items(store, sid)
    app = WheelhouseApp(store)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        tour_at(app, "answer", sid)
        if then == "send":
            store.queue(sid, "a", q1)
        act(store, sid, q1, q2)
        app.refresh_data()
        await pilot.pause()
        assert shown(app)[0] == then, desc


@pytest.mark.parametrize("at", ["inbox", "answer", "decision", "conversation", "end"])
@pytest.mark.anyio
async def test_a_demo_gone_early_offers_the_notes(store, at):
    """Decided in review of #93: a demo that has gone says so in one line, and Next goes
    straight to the release notes. At End, its going is what was asked."""
    sid = demo(store)
    setup_items(store, sid)
    app = WheelhouseApp(store)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        tour_at(app, at, sid)
        await pilot.pause()
        store.end(sid)
        app.refresh_data()
        await pilot.pause()
        if at != "end":
            co = app.screen_stack[0].query_one(TourCallout)
            assert shown(app)[0] == at and co.text.endswith("\nThe demo has ended: Next goes to the release notes."), at
            co.query_one("#tour-next", Button).press()
            await pilot.pause()
        assert isinstance(app.screen, TourNotes), at


@pytest.mark.both
@pytest.mark.anyio
async def test_only_the_persons_own_selection_opens_a_question(store, sid, tut):
    """The inbox dialog moves on once the person highlights a question and rests there, not on
    a task, nor on passing over one."""
    q1, _ = setup_items(store, tut)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        tour_at(app, "inbox", tut)
        await pilot.pause()
        items = app.query_one("#items", DataTable)
        items.move_cursor(row=items.get_row_index(f"{tut}|T1"))
        await pilot.pause()
        dwell(app)
        await pilot.pause()
        assert shown(app)[0] == "inbox", "a task isn't a question"
        items.move_cursor(row=items.get_row_index(f"{tut}|{q1}"))
        await pilot.pause()
        assert shown(app)[0] == "inbox", "passing over a question doesn't count"
        dwell(app)
        app.refresh_data()
        await pilot.pause()
        assert shown(app)[0] == "answer", "highlighting a question, and resting there, does"


@pytest.mark.anyio
async def test_closing_the_decision_moves_the_tour_on(store, sid, tut):
    with one_second():   # P1 just above D1: posted first, so the last of its rank's ties, the open ones
        store.post_item(tut, "permission", "Bash: touch tutorial-ok")
        setup_items(store, tut)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        tour_at(app, "decision", tut)
        await pilot.pause()
        items = app.query_one("#items", DataTable)
        items.move_cursor(row=items.get_row_index(f"{tut}|P1"))
        await pilot.pause()
        app.answer_permission(tut, "P1", "allow")   # the person allows it: it goes at once
        app.refresh_data()
        await pilot.pause()
        dwell(app)
        await pilot.pause()
        assert app.selected == (tut, "D1") and store.item(tut, "D1")["status"] == "seen", \
            "P1 went, so D1 under the cursor was selected automatically, and seen once rested on"
        assert shown(app)[0] == "decision", "automatic selection doesn't move it on"
        items.focus()
        await pilot.press("delete")
        await pilot.pause()
        assert store.item(tut, "D1")["status"] == "closed" and shown(app)[0] == "conversation", "closing it does"


def turn(tmp_path, sid, who: str, text: str) -> None:
    """A record added to the session's transcript: the person's words (user) or Claude's (assistant)."""
    folder = tmp_path / "projects/-tutorial"
    folder.mkdir(parents=True, exist_ok=True)
    content = text if who == "user" else [{"type": "text", "text": text}]
    with (folder / f"{sid}.jsonl").open("a") as f:
        f.write(json.dumps({"type": who, "timestamp": "2026-10-12T10:00:00Z", "message": {"content": content}}) + "\n")


@pytest.mark.parametrize("pick, arrives, replied, moves, desc", [
    (True, "assistant", True, True, "their own pick of its conversation, and a turn of Claude's arrives while shown"),
    (True, "user", True, False, "their own words arriving don't"),
    (True, None, True, False, "shown, with nothing new, doesn't"),
    (False, "assistant", True, False, "the app's own selection of it doesn't"),
    (True, "assistant", False, False, "before its reply, the demo's first turn running, doesn't"),
])
@pytest.mark.both
@pytest.mark.anyio
async def test_watching_it_work_moves_the_tour_on(store, tut, tmp_path, monkeypatch, pick, arrives, replied, moves,
                                                  desc):
    """Following the conversation is a tab's too: it is read from the transcript (Hub.conversation)."""
    from claude_wheelhouse import transcript
    monkeypatch.setattr(transcript, "PROJECTS", tmp_path / "projects")
    q1, q2 = setup_items(store, tut)
    if replied:
        store.queue(tut, "pirate", q1)
        store.queue(tut, "Holly", q2)
        store.dispatch(tut)
        store.reply(tut, q1, "noted", status="answered")
    turn(tmp_path, tut, "assistant", "Waiting on the two questions.")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        tour_at(app, "conversation", tut)
        app.follow(tut)
        if not pick:   # as the app's own selection leaves it: the person's last pick was something else
            app.picked = None
        await pilot.pause()
        assert "Waiting on the two questions" in app._detail_text, f"{desc}: its conversation shown"
        if arrives:
            turn(tmp_path, tut, arrives, "Made it friendlier.")
        app.refresh_data()
        await pilot.pause()
        assert ("follow" in app.tour.seen) is moves, desc
        assert (shown(app)[0] != "conversation") is moves, desc


@pytest.mark.parametrize("registered, runner_, why, desc", [
    (False, "tab", "stopped: its tab didn't start Claude Code (launch.log", "never registered: wt.exe or Claude Code failed"),
    (True, "tab", "stopped: its tab has closed", "registered, and its Claude Code has gone"),
    (True, "sdk", "the demo has stopped", "a host killed or crashed, which recorded no reason (review of #93)"),
])
@pytest.mark.anyio
async def test_a_stopped_demo_says_why(store, tut, registered, runner_, why, desc):
    """A tab's session has no host to record why it stopped: once it isn't running, the
    tour's dialog says whether its tab never started Claude Code or has closed, and offers
    Next to the release notes, rather than wait on it."""
    store.set_runner(tut, runner_)
    if registered:
        store.register(tut, 4242, 1, "an-old-boot")   # a pid of another boot: not alive
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert app.hub.statuses[tut] == "dead", f"{desc}: not running"
        tour_at(app, "sessions", tut)
        await pilot.pause()
        co = app.screen.query_one(TourCallout)
        assert f"\n{why[0].upper()}{why[1:]}" in co.text and co.text.endswith(": Next goes to the release notes."), desc
        co.query_one("#tour-next").press()
        await pilot.pause()
        assert isinstance(app.screen, TourNotes), f"{desc}: not stranded: Next goes to the notes"


@pytest.mark.anyio
async def test_every_hint_names_the_same_fixed_command(store, monkeypatch, fake_claude):
    """Review 68: the offer, the ? overlay and the dismiss notice all name the command that
    has it offered again. Guessing it from PATH named a command that fails under make run,
    where uv puts the venv's claude-wheelhouse on PATH but the person's shell can't run it,
    so every hint names both. Each is shown in a running app, where the command is refused,
    so each says to stop it first."""
    from claude_wheelhouse.hub import Hub
    said = "With the wheelhouse stopped, claude-wheelhouse tutorial (make tutorial in a source checkout) offers the tutorial again"
    monkeypatch.setattr(tutorial.shutil, "which", lambda cmd, *a, **k: f"/venv/bin/{cmd}")   # as under uv run
    assert f"{said} at the next start" in keys_help(), "? overlay"
    monkeypatch.setattr(tutorial, "should_offer", real_should_offer)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        hint = str(app.screen.query_one("#invite-hint").render())
        assert hint.endswith(f"After Never, {said[0].lower()}{said[1:]}."), "invite"
    assert [e.text for e in Hub(store).answer_offer(False).notices] == \
        [f"{said}; ? lists every key"], "dismiss notice"


@pytest.mark.parametrize("sdk, exits, killed, desc", [
    (True, True, [], "a host (an earlier version's tutorial) that exits when its row goes is left to"),
    (True, False, [4242], "one that doesn't is told to"),
    (False, False, [4242], "a tab's Claude Code, which never exits by itself, is told to"),
])
def test_an_earlier_tutorials_host_is_stopped(store, monkeypatch, sdk, exits, killed, desc):
    sid = demo(store)
    if sdk:
        store.set_runner(sid, "sdk")
    store.register(sid, 4242, 1, "boot")
    calls = []
    monkeypatch.setattr(tutorial, "STOP_WAIT", 0)
    monkeypatch.setattr(tutorial.liveness, "is_alive", lambda pid, start, boot: not exits and pid == 4242)
    monkeypatch.setattr(tutorial.os, "kill", lambda pid, sig: calls.append(pid))
    tutorial.prepare(store)
    assert calls == killed, desc


@pytest.mark.parametrize("sessions, setting, offered, desc", [
    (0, None, True, "an empty wheelhouse offers it"),
    (1, None, False, "not once there are sessions"),
    (0, "never", False, "not once answered Never"),
    (0, "taken", False, "not once taken"),
    (2, tutorial.RESET, True, "reset by claude-wheelhouse tutorial: offered though there are sessions"),
    (0, tutorial.RESET, True, "reset, and empty"),
])
def test_when_the_tutorial_is_offered(store, tmp_path, sessions, setting, offered, desc):
    for _ in range(sessions):
        store.create_session(str(tmp_path))
    if setting:
        store.set_setting(tutorial.OFFER_KEY, setting)
    assert real_should_offer(store) is offered, desc


@pytest.fixture
def daemon_lock(db_file):
    """A daemon's lock held, as a running daemon holds it (connect.holder)."""
    import fcntl
    from claude_wheelhouse import connect
    with open(db_file.parent / connect.LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock.write("pid 4242 at http://127.0.0.1:7428\n")
        lock.flush()
        yield


@pytest.mark.parametrize("argv, said, daemon, reset, desc", [
    (["tutorial"], "y", None, True, "y: reset"),
    (["tutorial"], "YES\n", None, True, "yes, in any case: reset"),
    (["tutorial"], "n", None, False, "n: nothing changed"),
    (["tutorial"], "", None, False, "Enter: no, the default"),
    (["tutorial"], EOFError, None, False, "no input to read: no"),
    (["tutorial", "--yes"], None, None, True, "--yes: reset without asking"),
    (["tutorial", "-y"], None, "before", False, "a daemon running: refused, even with --yes"),
    (["tutorial"], None, "before", False, "a daemon running: refused before asking"),
    (["tutorial"], "y", "asking", False, "a daemon started while it asked: refused after the answer"),
])
def test_the_tutorial_command_resets_the_offer(store, tmp_path, monkeypatch, capsys, request,
                                               argv, said, daemon, reset, desc):
    from claude_wheelhouse import cli
    if daemon == "before":
        request.getfixturevalue("daemon_lock")
    other = store.create_session(str(tmp_path), name="real work")
    store.post_item(other, "task", "keep me")
    tut = demo(store)
    store.set_setting(tutorial.OFFER_KEY, "dismissed")
    asked = []

    def ask(prompt):
        asked.append(prompt)
        if daemon == "asking":   # the app started while the question waited
            request.getfixturevalue("daemon_lock")
        if said is EOFError:
            raise EOFError
        return said
    monkeypatch.setattr("builtins.input", ask)
    monkeypatch.setattr(tutorial.liveness, "is_alive", lambda pid, start, boot: False)
    monkeypatch.setattr(sys, "argv", ["claude-wheelhouse", *argv])
    if daemon:
        with pytest.raises(SystemExit, match="quit the app, stop the daemon .*make stop"):
            cli.main()
    else:
        cli.main()
    assert bool(asked) is (said is not None), f"{desc}: asked only without --yes, and never with a daemon already running"
    assert store.setting(tutorial.OFFER_KEY) == (tutorial.RESET if reset else "dismissed"), desc
    assert (store.session(tut) is None) is reset, f"{desc}: the tutorial session ended only on a reset"
    assert [it["title"] for it in store.items(other)] == ["keep me"], f"{desc}: other sessions are kept"
    assert real_should_offer(store) is reset, f"{desc}: offered at the next start"
    out = capsys.readouterr().out
    if reset:
        assert "ended the earlier tutorial session" in out and "offers the tutorial" in out, desc
    elif not daemon:
        assert out.strip() == "nothing changed", desc


@pytest.mark.parametrize("key, setting, again, desc", [
    ("enter", "taken", False, "Enter takes it"),
    ("n", "never", False, "N answers Never"),
    ("escape", "reset", True, "Esc is Later: the reset stands, so it is offered again"),
])
@pytest.mark.anyio
async def test_a_reset_offer_shows_over_sessions(store, tmp_path, monkeypatch, fake_claude, key, setting, again, desc):
    monkeypatch.setattr(tutorial, "should_offer", real_should_offer)
    monkeypatch.setattr(launch, "open_tab", lambda s, i: None)
    store.create_session(str(tmp_path), name="real work")
    store.set_setting(tutorial.OFFER_KEY, tutorial.RESET)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, TourInvite), f"{desc}: offered though there is a session"
        await pilot.press(key)
        await pilot.pause()
    assert store.setting(tutorial.OFFER_KEY) == setting, f"{desc}: what the answer leaves"
    async with WheelhouseApp(store).run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert isinstance(pilot.app.screen, TourInvite) is again, f"{desc}: offered at the next start"


async def click_outside(pilot):
    pilot.app.screen.opened -= Dialog.GRACE   # past the grace a double-click's second click gets
    await pilot.click(offset=(0, 39))


@pytest.mark.parametrize("gesture, started, setting, again, desc", [
    (lambda p: p.press("enter"), True, "taken", False, "Enter is Start"),
    (lambda p: p.click("#invite-start"), True, "taken", False, "a click on Start"),
    (lambda p: p.press("l"), False, "reset", True, "L is Later: offered at the next start"),
    (lambda p: p.press("L"), False, "reset", True, "Shift+L too"),
    (lambda p: p.press("N"), False, "never", False, "Shift+N too"),
    (lambda p: p.click("#invite-later"), False, "reset", True, "a click on Later"),
    (lambda p: p.press("escape"), False, "reset", True, "Esc is Later, not Never"),
    (click_outside, False, "reset", True, "a click outside is Later: a misclick doesn't lose the tour"),
    (lambda p: p.press("n"), False, "never", False, "N is Never: it never comes back"),
    (lambda p: p.click("#invite-never"), False, "never", False, "a click on Never"),
])
@pytest.mark.anyio
async def test_the_invite(store, tmp_path, monkeypatch, fake_claude, gesture, started, setting, again, desc):
    """Doug (#78): "the invite needs to be a centered dialog, not the banner which is not very
    obvious to a new user". Start, Later and Never, by key or click."""
    monkeypatch.setattr(tutorial, "should_offer", real_should_offer)
    launched = []
    monkeypatch.setattr(launch, "open_tab", lambda s, i: launched.append(i))
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, TourInvite), desc
        await gesture(pilot)
        await pilot.pause()
        assert not isinstance(app.screen, TourInvite), desc
        assert isinstance(app.screen, TourStop) is started, f"{desc}: the tour opens on its principles"
    assert launched == [], f"{desc}: nothing launched: the person starts the demo from New session"
    assert store.setting(tutorial.OFFER_KEY) == setting, desc
    seen = {f.id: "shown" for f in whatsnew.tips("principles", False)} if started else {}   # its tips shown
    assert store.tour_seen() == ({f.id: "skipped" for f in whatsnew.FEATURES} if setting == "never" else seen), desc
    store.create_session(str(tmp_path), name="real work")   # Later still offers it once there are sessions
    async with WheelhouseApp(store).run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert isinstance(pilot.app.screen, TourInvite) is again, f"{desc}: offered at the next start"


@pytest.mark.parametrize("width, height, desc", [
    (160, 40, "a wide screen"), (80, 24, "80 by 24"),
    (50, 30, "50 columns: the buttons stack, not spill"), (40, 30, "40 columns"),
])
@pytest.mark.anyio
async def test_the_invite_fits_whole(store, monkeypatch, width, height, desc):
    monkeypatch.setattr(tutorial, "should_offer", real_should_offer)
    app = WheelhouseApp(store)
    async with app.run_test(size=(width, height)) as pilot:
        await pilot.pause()
        box = app.screen.query_one("#dialog").region
        assert app.screen.region.contains_region(box), f"{desc}: {box} on screen"
        for widget in app.screen.query("#dialog Label, #dialog Button"):
            assert box.contains_region(widget.region) and widget.region.height, f"{desc}: {widget.id} shown whole"


@pytest.mark.parametrize("cls, desc", [
    (WheelhouseApp, "the app's keys"), (ItemList, "the item list's"), (Compose, "an answer box's"),
    (Transcript, "the conversation pane's"), (ThreadView, "a full-screen item's"),
])
def test_the_overlay_lists_every_key(cls, desc):
    text = keys_help(sdk=True)
    for b in cls.BINDINGS:
        assert f"**{key_name(b.key)}" in text or f" or {key_name(b.key)}" in text, f"{desc}: {b.key}"


def test_the_overlay_lists_every_button():
    text = keys_help(sdk=True)
    bar = {"mode", "send", "send-all", "allow", "always", "deny", "interrupt", "compact", "shell"}
    assert bar <= set(BUTTONS), "every send bar button is described"
    assert all(BUTTONS[b] in text for b in BUTTONS)


def test_the_overlay_leaves_sdk_mode_out_unless_it_is_on():
    text = keys_help()
    for word in ("Interrupt", "Compact", "Shell", "Model", "Allow", "Always", "Deny", "effort", "run in the wheelhouse"):
        assert word not in text, word
    for b in WheelhouseApp.BINDINGS:
        if b.action not in {f"press('{x}')" for x in SDK_BUTTONS}:
            assert f"**{key_name(b.key)}" in text or f" or {key_name(b.key)}" in text, b.key
    assert all(BUTTONS[b] in text for b in BUTTONS if b not in SDK_BUTTONS)


@pytest.mark.anyio
async def test_question_mark_opens_the_keys_and_types_in_a_box(store, sid):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, KeysHelp)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, KeysHelp)
        app.query_one("#answer", TextArea).focus()
        await pilot.press("question_mark")
        await pilot.pause()
        assert not isinstance(app.screen, KeysHelp)
        assert app.query_one("#answer", TextArea).text == "?"


@pytest.mark.parametrize("marked, refused, desc", [
    (True, False, "its own directory, carrying the marker, is recreated"),
    (False, True, "a directory it didn't make is refused, never deleted"),
])
def test_prepare_deletes_only_its_own_directory(store, marked, refused, desc):
    path = tutorial.tutorial_dir(store)
    path.mkdir()
    (path / "precious.txt").write_text("keep")
    if marked:
        (path / tutorial.MARKER).touch()
    if refused:
        with pytest.raises(FileExistsError):
            tutorial.prepare(store)
        assert (path / "precious.txt").exists() and tutorial.session_id(store) is None, desc
    else:
        tutorial.prepare(store)
        assert not (path / "precious.txt").exists() and (path / tutorial.MARKER).exists(), desc
    assert path.name == "claude-wheelhouse-tutorial", "a folder of its own name beside the database"


def test_start_refuses_without_claude(store, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))   # no claude anywhere
    with pytest.raises(RuntimeError, match="isn't on PATH"):
        tutorial.start(store)
    assert not tutorial.tutorial_dir(store).exists(), "nothing made"
    assert store.setting(tutorial.OFFER_KEY) is None


def test_an_earlier_host_that_exits_as_its_told_is_no_error(store, monkeypatch):
    sid = demo(store)
    store.register(sid, 4242, 1, "boot")
    monkeypatch.setattr(tutorial, "STOP_WAIT", 0)
    monkeypatch.setattr(tutorial.liveness, "is_alive", lambda pid, start, boot: True)

    def gone(pid, sig):
        raise ProcessLookupError(pid)
    monkeypatch.setattr(tutorial.os, "kill", gone)
    tutorial.prepare(store)
    assert store.session(sid) is None


@pytest.mark.parametrize("how", ["restore", "open"])
def test_a_relaunched_tab_that_never_starts_says_so(store, tut, monkeypatch, how):
    """Review 68: a restored tutorial whose tab fails to start once carried the last
    launch's pid, so its checklist said its tab had closed. It goes by whether anything
    registered since this launch, as liveness.status does. Review 69: the launch keeps the
    pid, so register_if_free's compare-and-set still sees the last run's Claude Code."""
    store.register(tut, 4242, 1, "an-old-boot")   # the last launch's Claude Code, gone
    with store.tx() as db:   # an hour ago, so this launch is plainly later
        db.execute("UPDATE sessions SET launched_at = ?, heartbeat_at = ? WHERE id = ?",
                   ("2026-01-01T10:00:00+00:00", "2026-01-01T10:00:05+00:00", tut))
    assert tutorial.stopped(store.session(tut)) == "stopped: its tab has closed"
    opened = []
    monkeypatch.setattr(launch.subprocess, "Popen", lambda argv, **k: opened.append(argv))   # no real tab
    (launch.restore_session if how == "restore" else launch.open_session)(store, tut)
    assert opened, how
    assert tutorial.stopped(store.session(tut)).startswith("stopped: its tab didn't start Claude Code"), how
    assert store.session(tut)["claude_pid"] == 4242, f"{how}: the launch keeps the last run's pid"
