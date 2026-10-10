import json

import pytest
from textual.widgets import DataTable, Static, TextArea

from claude_wheelhouse import launch, tutorial
from claude_wheelhouse.store import Store
from claude_wheelhouse.tui import BUTTONS, Compose, ItemList, KeysHelp, ThreadView, Transcript, TutorialOffer, \
    WheelhouseApp, key_name, keys_help
from claude_wheelhouse.tutorial import should_offer as real_should_offer   # before conftest stubs it
from test_tui import dwell, one_second


@pytest.fixture
def tut(store):
    return tutorial.prepare(store)


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
    """What the tutorial's session posts first."""
    store.post_item(sid, "task", "Draft the welcome message", status="running")
    q1 = store.post_item(sid, "question", "Tone?")
    q2 = store.post_item(sid, "question", "Sign-off?")
    store.post_item(sid, "decision", "British English", alternative="American", why="house style", reverse="say so")
    return q1, q2


def answer(store, sid, q1, q2):
    store.queue(sid, "pirate", q1)
    store.queue(sid, "Holly", q2)


def send(store, sid, q1, q2):
    store.dispatch(sid)


def reply(store, sid, q1, q2):
    store.reply(sid, q1, "noted", status="answered")


def see_decision(store, sid, q1, q2):
    store.mark_seen(sid, "D1")


def close_decision(store, sid, q1, q2):
    store.close_decision(sid, "D1")


def allow(store, sid, q1, q2):
    store.post_item(sid, "permission", "Bash: touch tutorial-ok")
    store.answer_permission(sid, "P1", "allow")


def one_answer(store, sid, q1, q2):
    store.queue(sid, "pirate", q1)


def reply_first(store, sid, q1, q2):   # a reply before anything was sent doesn't count
    store.reply(sid, q1, "still waiting", status="open")


@pytest.mark.parametrize("actions, seen, done, desc", [
    ([], set(), set(), "nothing done yet"),
    ([], {"open"}, {"open"}, "opening a question is seen on screen"),
    ([one_answer], set(), {"open"}, "an answer means a question was opened, but one isn't both"),
    ([answer], set(), {"open", "queue"}, "both questions answered, still queued"),
    ([reply_first, answer], set(), {"open", "queue"}, "a reply from before the answers went isn't the reply"),
    ([answer, send], set(), {"open", "queue", "send"}, "sent"),
    ([answer, send, reply], set(), {"open", "queue", "send", "reply"}, "the session replied after they went"),
    ([see_decision], set(), set(), "its seen status doesn't tick it: automatic selection sets that"),
    ([see_decision, close_decision], set(), {"decision"}, "the person closing the decision does"),
    ([allow], set(), {"permission"}, "the permission allowed"),
    ([], {"follow"}, {"follow"}, "the conversation followed"),
    ([answer, send, reply, see_decision, close_decision, allow], {"follow"},
     {"open", "queue", "send", "reply", "decision", "permission", "follow"}, "all but end, which goes with the session"),
])
def test_checklist_steps_come_from_the_store(store, tut, actions, seen, done, desc):
    q1, q2 = setup_items(store, tut)
    for act in actions:
        act(store, tut, q1, q2)
    got = {key for key, _, _, ok in tutorial.steps(store, tut, seen) if ok}
    assert got == done, desc
    assert [key for key, *_ in tutorial.steps(store, tut, seen)] == [key for key, *_ in tutorial.STEPS]


def test_prepare_starts_afresh(store, tmp_path):
    other = store.create_session(str(tmp_path), name="real work")
    store.post_item(other, "task", "keep me")
    old = tutorial.prepare(store)
    store.post_item(old, "question", "from last time")
    (tutorial.tutorial_dir(store) / "tutorial-ok").touch()
    new = tutorial.prepare(store)
    assert new != old
    assert store.session(old) is None and store.items(old) == [], "the earlier tutorial and its items are gone"
    assert [it["title"] for it in store.items(other)] == ["keep me"], "other sessions are untouched"
    path = tutorial.tutorial_dir(store)
    assert not (path / "tutorial-ok").exists(), "its directory is recreated"
    assert json.loads((path / ".claude/settings.json").read_text()) == tutorial.SETTINGS
    s = store.session(new)
    assert (s["cwd"], s["name"], s["runner"], s["brief"]) == (str(path), "tutorial", "sdk", tutorial.BRIEF)
    assert tutorial.session_id(store) == new


@pytest.mark.parametrize("exits, killed, desc", [
    (True, [], "a host that exits when its row goes is left to"),
    (False, [4242], "one that doesn't is told to"),
])
def test_an_earlier_tutorials_host_is_stopped(store, monkeypatch, exits, killed, desc):
    sid = tutorial.prepare(store)
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
    (0, "dismissed", False, "not once dismissed"),
    (0, "taken", False, "not once taken"),
])
def test_when_the_tutorial_is_offered(store, tmp_path, sessions, setting, offered, desc):
    for _ in range(sessions):
        store.create_session(str(tmp_path))
    if setting:
        store.set_setting(tutorial.OFFER_KEY, setting)
    assert real_should_offer(store) is offered, desc


def test_start_launches_the_host_and_settles_the_offer(store, monkeypatch, fake_claude):
    launched = []
    monkeypatch.setattr(launch, "open_host", lambda s, i: launched.append(i))
    sid = tutorial.start(store)
    assert launched == [sid]
    assert store.setting(tutorial.OFFER_KEY) == "taken"


@pytest.mark.parametrize("key, started, setting, desc", [
    ("enter", True, "taken", "Enter takes the tutorial"),
    ("escape", False, "dismissed", "Esc dismisses it"),
])
@pytest.mark.anyio
async def test_the_first_run_offer_takes_one_key(store, monkeypatch, fake_claude, key, started, setting, desc):
    monkeypatch.setattr(tutorial, "should_offer", real_should_offer)
    launched = []
    monkeypatch.setattr(launch, "open_host", lambda s, i: launched.append(i))
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, TutorialOffer)
        await pilot.press(key)
        await pilot.pause()
        assert not isinstance(app.screen, TutorialOffer), desc
        assert app.query_one("#checklist", Static).display is started, desc
    assert bool(launched) is started, desc
    assert store.setting(tutorial.OFFER_KEY) == setting, desc
    async with WheelhouseApp(store).run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert not isinstance(pilot.app.screen, TutorialOffer), "answered either way, it never comes back"


@pytest.mark.anyio
@pytest.mark.parametrize("permission, desc", [
    (False, "80 by 24: the checklist scrolls, the answer box below it gives way"),
    (True, "and with the permission buttons over the box too"),
])
async def test_the_answer_box_stays_on_screen_under_the_checklist(store, tut, permission, desc):
    """Review 9: the checklist's 19 rows pushed the answer box off an 80 by 24 screen, and
    neither it nor the permission buttons appearing fitted the right pane again."""
    ref = store.post_item(tut, "permission", "Bash: make test")
    app = WheelhouseApp(store)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        pane = app.query_one("#detail-pane").content_region
        if permission:
            app.items_table.move_cursor(row=app.items_table.get_row_index(f"{tut}|{ref}"))
        else:
            app.follow(tut)
        await pilot.pause()
        await pilot.pause()
        assert app.query_one("PermissionButtons").display is permission, desc
        box, hint = app.query_one("#answer").region, app.query_one(".answer-hint").region
        assert box.height >= 3 and box.bottom <= hint.y and hint.bottom <= pane.bottom, f"{desc}: {box}, {hint}"
        store.end(tut)
        app.refresh_data()
        await pilot.pause()
        await pilot.pause()
        assert app.query_one("#answer").region.height == 8, f"{desc}: the box's own size back with the checklist gone"


@pytest.mark.anyio
async def test_the_checklist_follows_the_tutorial(store, sid, tut):
    q1, _ = setup_items(store, tut)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        checklist = app.query_one("#checklist", Static)
        assert checklist.display
        assert "▶ Open a question" in app._checklist_text, "the next step leads"
        items = app.query_one("#items", DataTable)
        keys = [items.coordinate_to_cell_key((i, 0)).row_key.value for i in range(items.row_count)]
        items.move_cursor(row=keys.index(f"{tut}|T1"))   # a task isn't a question
        await pilot.pause()
        assert "▶ Open a question" in app._checklist_text
        items.move_cursor(row=keys.index(f"{tut}|{q1}"))
        await pilot.pause()
        assert "▶ Open a question" in app._checklist_text, "passing over a question doesn't tick it"
        dwell(app)
        await pilot.pause()
        assert "✔ Open a question" in app._checklist_text, "highlighting a question, and resting there, ticks it"
        app.follow(tut)
        await pilot.pause()
        dwell(app)
        await pilot.pause()
        assert "✔ Follow the conversation" in app._checklist_text
        store.end(tut)
        app.refresh_data()
        await pilot.pause()
        assert not checklist.display, "it goes with the session"


@pytest.mark.parametrize("cls, desc", [
    (WheelhouseApp, "the app's keys"), (ItemList, "the item list's"), (Compose, "an answer box's"),
    (Transcript, "the conversation pane's"), (ThreadView, "a full-screen item's"),
])
def test_the_overlay_lists_every_key(cls, desc):
    text = keys_help()
    for b in cls.BINDINGS:
        assert f"**{key_name(b.key)}" in text or f" or {key_name(b.key)}" in text, f"{desc}: {b.key}"


def test_the_overlay_lists_every_button():
    text = keys_help()
    bar = {"mode", "send", "send-all", "allow", "always", "deny", "interrupt", "compact", "shell"}
    assert bar <= set(BUTTONS), "every send bar button is described"
    assert all(BUTTONS[b] in text for b in BUTTONS)


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
    assert tutorial.session_id(store) is None, "nothing created, so no dead session and no checklist"
    assert store.setting(tutorial.OFFER_KEY) is None


@pytest.mark.anyio
async def test_a_host_that_cant_start_says_why(store, fake_claude):
    """The real path: the host starts detached (setsid returns at once), then Claude Code
    fails to start inside it. The host records why, and the checklist says so."""
    import asyncio
    sid = tutorial.start(store)
    for _ in range(300):
        why = tutorial.stopped(store.session(sid))
        if why:
            break
        await asyncio.sleep(0.1)
    assert why and why.startswith("stopped: Claude Code didn't start"), why
    assert "\n" not in why, "one line"
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        for _ in range(50):   # the host's own exit, a moment after it records why
            await pilot.pause(0.1)
            if why in app._checklist_text:
                break
        assert why in app._checklist_text and "make tutorial starts it afresh" in app._checklist_text


def test_an_earlier_host_that_exits_as_its_told_is_no_error(store, monkeypatch):
    sid = tutorial.prepare(store)
    store.register(sid, 4242, 1, "boot")
    monkeypatch.setattr(tutorial, "STOP_WAIT", 0)
    monkeypatch.setattr(tutorial.liveness, "is_alive", lambda pid, start, boot: True)

    def gone(pid, sig):
        raise ProcessLookupError(pid)
    monkeypatch.setattr(tutorial.os, "kill", gone)
    assert tutorial.prepare(store) != sid


@pytest.mark.anyio
async def test_closing_the_decision_ticks_it(store, sid, tut):
    with one_second():   # P1 just above D1 (inbox order): a second's boundary between them put P1 first
        setup_items(store, tut)
        store.post_item(tut, "permission", "Bash: touch tutorial-ok")
    app = WheelhouseApp(store)
    step = "✔ Read and close the decision"
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        items = app.query_one("#items", DataTable)
        keys = [items.coordinate_to_cell_key((i, 0)).row_key.value for i in range(items.row_count)]
        items.move_cursor(row=keys.index(f"{tut}|P1"))
        await pilot.pause()
        app.answer_permission(tut, "P1", "allow")   # the person allows it: it goes at once
        app.refresh_data()
        await pilot.pause()
        dwell(app)
        await pilot.pause()
        assert app.selected == (tut, "D1") and store.item(tut, "D1")["status"] == "seen", \
            "P1 went, so D1 under the cursor was selected automatically, and seen once rested on"
        assert step not in app._checklist_text, "automatic selection doesn't tick it"
        keys = [items.coordinate_to_cell_key((i, 0)).row_key.value for i in range(items.row_count)]
        for ref in ("T1", "D1"):
            items.move_cursor(row=keys.index(f"{tut}|{ref}"))
            await pilot.pause()
        assert step not in app._checklist_text, "nor does reading it"
        items.focus()
        await pilot.press("delete")
        await pilot.pause()
        assert store.item(tut, "D1")["status"] == "closed" and step in app._checklist_text, "closing it with Delete does"
    async with WheelhouseApp(store).run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert step in pilot.app._checklist_text, "kept across a restart of the app"
