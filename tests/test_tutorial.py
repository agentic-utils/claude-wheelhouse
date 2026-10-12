import json
import sys

import pytest
from textual.widgets import DataTable, Static, TextArea

from claude_wheelhouse import features, launch, tutorial
from claude_wheelhouse.store import Store
from claude_wheelhouse.tui import BUTTONS, Compose, ItemList, KeysHelp, ThreadView, Transcript, TutorialOffer, \
    SDK_BUTTONS, WheelhouseApp, key_name, keys_help
from claude_wheelhouse.tutorial import should_offer as real_should_offer   # before conftest stubs it
from test_tui import dwell, one_second


@pytest.fixture
def tut(store):
    """The tutorial's session: a tab, SDK mode or not."""
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
    store.post_item(sid, "decision", "British English", why="over American: house style; undo: say so")
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


def one_answer(store, sid, q1, q2):
    store.queue(sid, "pirate", q1)


def reply_first(store, sid, q1, q2):   # a reply before anything was sent doesn't count
    store.reply(sid, q1, "still waiting", status="open")


def own_session(store, sid, q1, q2):
    store.create_session("/home/u/work", name="mine")


def own_in_its_directory(store, sid, q1, q2):   # taken for the tutorial's, so not the person's own
    store.create_session(str(tutorial.tutorial_dir(store)), name="mine")


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
    ([], {"follow"}, {"follow"}, "a turn watched arriving is seen on screen"),
    ([own_session], set(), {"new"}, "a session of their own, started after the tutorial, before End too"),
    ([own_in_its_directory], set(), set(), "one in the tutorial's directory isn't their own"),
])
def test_checklist_steps_come_from_the_store(store, tut, actions, seen, done, desc):
    q1, q2 = setup_items(store, tut)
    for act in actions:
        act(store, tut, q1, q2)
    got = {key for key, _, _, ok in tutorial.steps(store, tut, seen) if ok}
    assert got == done | {"start"}, f"{desc}: started, as it has posted"


@pytest.mark.parametrize("actions, setup, done, desc", [
    ([], False, set(), "nothing done: the tab hasn't posted yet (its trust prompt, say)"),
    ([], True, {"start"}, "it has posted: it started"),
    ([answer, send, reply, see_decision, close_decision], True, {"start", "open", "queue", "send", "reply", "decision"},
     "the whole loop, by a tab: all but the turn watched, End and a session of their own"),
])
def test_a_tab_tutorial_has_the_tabs_steps(store, tut, actions, setup, done, desc):
    q1, q2 = setup_items(store, tut) if setup else ("Q1", "Q2")
    for act in actions:
        act(store, tut, q1, q2)
    steps = tutorial.steps(store, tut, set())
    assert [key for key, *_ in steps] == ["start", "open", "queue", "send", "reply", "follow", "decision", "end", "new"], \
        "no permission step: that comes back as an SDK-only top-up; following the conversation is a tab's too"
    assert {key for key, _, _, ok in steps if ok} == done, desc


@pytest.mark.parametrize("before, own, end, record, shows, desc", [
    (False, False, False, None, (9, False), "running: every step"),
    (False, False, True, None, (2, False), "ended: End done, Start your own still to do"),
    (True, False, True, None, (2, False), "a session from before the tutorial isn't their own"),
    (False, True, False, None, (9, True), "their own session ticks it while the tutorial runs"),
    (False, True, True, None, None, "ended with a session of their own: nothing left, the checklist goes"),
    (False, False, True, {"seen": ["open"]}, None, "an earlier version's record, without before: it goes with the session"),
    (False, False, True, "reset", None, "claude-wheelhouse tutorial clears its record"),
])
def test_the_checklist_outlives_the_session_for_its_last_step(store, monkeypatch, before, own, end, record, shows, desc):
    if before:
        store.create_session("/home/u/work", name="from before")
    tut = tutorial.prepare(store)
    if own:
        store.create_session("/home/u/work", name="mine")
    if record == "reset":
        monkeypatch.setattr(tutorial.liveness, "is_alive", lambda pid, start, boot: False)
        tutorial.reset(store)
    elif record:
        store.set_setting(tutorial.SEEN_KEY, json.dumps({**record, "sid": tut}))
    if end:
        store.end(tut)
    found = tutorial.current(store)
    if shows is None:
        assert found is None, desc
        return
    done = {key: ok for key, _, _, ok in found[1]}
    assert (found[0], len(found[1]), done["new"]) == (tut, *shows), desc
    assert done["end"] is end, f"{desc}: End done once the session has gone"


@pytest.mark.parametrize("on, desc", [(False, "a new user's"), (True, "with SDK mode on, still a tab (D66)")])
def test_prepare_starts_afresh(store, tmp_path, monkeypatch, on, desc):
    monkeypatch.setattr(features, "_flag", on)
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
    assert json.loads((path / ".claude/settings.json").read_text()) == tutorial.SETTINGS, desc
    s = store.session(new)
    assert (s["cwd"], s["name"], s["runner"], s["brief"]) == (str(path), "tutorial", "tab", tutorial.BRIEF), desc
    assert "touch" not in json.dumps(tutorial.SETTINGS), f"{desc}: asking no permission"
    assert (s["model"], s["effort"]) == ("claude-sonnet-5-5", "high"), "New session's defaults"
    assert tutorial.session_id(store) == new
    assert tutorial.record(store) == {"sid": new, "seen": [], "before": [other]}, \
        f"{desc}: a fresh record, with the sessions there were (the earlier tutorial's gone)"


@pytest.mark.parametrize("sdk, exits, killed, desc", [
    (True, True, [], "a host (an earlier version's tutorial) that exits when its row goes is left to"),
    (True, False, [4242], "one that doesn't is told to"),
    (False, False, [4242], "a tab's Claude Code, which never exits by itself, is told to"),
])
def test_an_earlier_tutorials_host_is_stopped(store, monkeypatch, sdk, exits, killed, desc):
    sid = tutorial.prepare(store)
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
    (0, "dismissed", False, "not once dismissed"),
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
    tut = tutorial.prepare(store)
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


@pytest.mark.parametrize("key, setting, desc", [
    ("enter", "taken", "Enter takes it"),
    ("escape", "dismissed", "Esc dismisses it"),
])
@pytest.mark.anyio
async def test_a_reset_offer_shows_once_over_sessions(store, tmp_path, monkeypatch, fake_claude, key, setting, desc):
    monkeypatch.setattr(tutorial, "should_offer", real_should_offer)
    monkeypatch.setattr(launch, "open_tab", lambda s, i: None)
    store.create_session(str(tmp_path), name="real work")
    store.set_setting(tutorial.OFFER_KEY, tutorial.RESET)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, TutorialOffer), f"{desc}: offered though there is a session"
        await pilot.press(key)
        await pilot.pause()
    assert store.setting(tutorial.OFFER_KEY) == setting, f"{desc}: the answer replaces the reset"
    async with WheelhouseApp(store).run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert not isinstance(pilot.app.screen, TutorialOffer), f"{desc}: offered once"


@pytest.mark.parametrize("on, opens, desc", [
    (False, "tab", "a tab, as a new user has it"),
    (True, "tab", "with SDK mode on, still a tab (D66)"),
])
def test_start_launches_the_session_and_settles_the_offer(store, monkeypatch, fake_claude, on, opens, desc):
    monkeypatch.setattr(features, "_flag", on)
    launched = []
    monkeypatch.setattr(launch, "open_host", lambda s, i: launched.append(("host", i)))
    monkeypatch.setattr(launch, "open_tab", lambda s, i: launched.append(("tab", i)))
    sid = tutorial.start(store)
    assert launched == [(opens, sid)], desc
    assert store.setting(tutorial.OFFER_KEY) == "taken"


@pytest.mark.parametrize("key, started, setting, desc", [
    ("enter", True, "taken", "Enter takes the tutorial"),
    ("escape", False, "dismissed", "Esc dismisses it"),
])
@pytest.mark.anyio
async def test_the_first_run_offer_takes_one_key(store, monkeypatch, fake_claude, key, started, setting, desc):
    monkeypatch.setattr(tutorial, "should_offer", real_should_offer)
    launched = []
    monkeypatch.setattr(launch, "open_tab", lambda s, i: launched.append(i))
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
        own_session(store, tut, "Q1", "Q2")   # its last step done, so the checklist goes with the session
        store.end(tut)
        app.refresh_data()
        await pilot.pause()
        await pilot.pause()
        assert app.query_one("#answer").region.height == 8, f"{desc}: the box's own size back with the checklist gone"


@pytest.mark.both
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
        app.refresh_data()   # the tick's: through a daemon, the checklist is its last snapshot until then
        await pilot.pause()
        assert "✔ Open a question" in app._checklist_text, "highlighting a question, and resting there, ticks it"
        app.follow(tut)
        await pilot.pause()
        dwell(app)
        await pilot.pause()
        assert tutorial.seen(store, tut) == {"open"}, "following the conversation alone isn't watching it work"
        store.end(tut)
        app.refresh_data()
        await pilot.pause()
        assert "✔ End the session\n▶ Start your own: N opens New" in app._checklist_text, \
            "it outlives the session for its last step"
        assert "Open a question" not in app._checklist_text, "the steps that went with the session go too"
        own_session(store, tut, q1, None)
        app.refresh_data()
        await pilot.pause()
        assert not checklist.display, "a session of their own: nothing left, it goes"


def turn(tmp_path, sid, who: str, text: str) -> None:
    """A record added to the session's transcript: the person's words (user) or Claude's (assistant)."""
    folder = tmp_path / "projects/-tutorial"
    folder.mkdir(parents=True, exist_ok=True)
    content = text if who == "user" else [{"type": "text", "text": text}]
    with (folder / f"{sid}.jsonl").open("a") as f:
        f.write(json.dumps({"type": who, "timestamp": "2026-10-12T10:00:00Z", "message": {"content": content}}) + "\n")


@pytest.mark.parametrize("pick, arrives, ticks, desc", [
    (True, "assistant", True, "their own pick of its conversation, and a turn of Claude's arrives while shown"),
    (True, "user", False, "their own words arriving don't"),
    (True, None, False, "shown, with nothing new, doesn't"),
    (False, "assistant", False, "the app's own selection of it doesn't"),
])
@pytest.mark.both
@pytest.mark.anyio
async def test_watching_it_work_ticks_as_a_turn_arrives(store, tut, tmp_path, monkeypatch, pick, arrives, ticks, desc):
    """Following the conversation is a tab's too: it is read from the transcript (Hub.conversation)."""
    from claude_wheelhouse import transcript
    monkeypatch.setattr(transcript, "PROJECTS", tmp_path / "projects")
    setup_items(store, tut)
    turn(tmp_path, tut, "assistant", "Waiting on the two questions.")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.follow(tut)
        if not pick:   # as the app's own selection leaves it: the person's last pick was something else
            app.picked = None
        await pilot.pause()
        assert "Waiting on the two questions" in app._detail_text, f"{desc}: its conversation shown"
        if arrives:
            turn(tmp_path, tut, arrives, "Made it friendlier.")
        app.refresh_data()
        await pilot.pause()
        assert "Made it friendlier" in app._detail_text or not arrives, f"{desc}: the turn shown"
        assert ("follow" in tutorial.seen(store, tut)) is ticks, desc
        assert ("✔ Watch it work" in app._checklist_text) is ticks, desc


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
    assert tutorial.session_id(store) is None, "nothing created, so no dead session and no checklist"
    assert store.setting(tutorial.OFFER_KEY) is None


@pytest.mark.anyio
async def test_a_host_that_cant_start_says_why(store, fake_claude):
    """An earlier version's tutorial, run in the wheelhouse, relaunched with SDK mode off.
    The real path: the host starts detached (setsid returns at once), then Claude Code
    fails to start inside it. The host records why, and the checklist says so."""
    import asyncio
    sid = tutorial.prepare(store)
    store.set_runner(sid, "sdk")
    launch.open_session(store, sid)
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
        assert why in app._checklist_text and f"{tutorial.AGAIN} at the next start" in app._checklist_text


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
    with one_second():   # P1 just above D1: posted first, so the last of its rank's ties, the open ones
        store.post_item(tut, "permission", "Bash: touch tutorial-ok")
        setup_items(store, tut)
    app = WheelhouseApp(store)
    step = "✔ Read and close the decision"
    async with app.run_test(size=(160, 40)) as pilot:
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
        assert step not in app._checklist_text, "automatic selection doesn't tick it"
        for ref in ("T1", "D1"):   # each row looked up as it's moved to: a sinking D1 moves under a stale index
            items.move_cursor(row=items.get_row_index(f"{tut}|{ref}"))
            await pilot.pause()
        assert step not in app._checklist_text, "nor does reading it"
        items.focus()
        await pilot.press("delete")
        await pilot.pause()
        assert store.item(tut, "D1")["status"] == "closed" and step in app._checklist_text, "closing it with Delete does"
    async with WheelhouseApp(store).run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert step in pilot.app._checklist_text, "kept across a restart of the app"


@pytest.mark.parametrize("registered, why, desc", [
    (False, "stopped: its tab didn't start Claude Code (launch.log", "never registered: wt.exe or Claude Code failed"),
    (True, "stopped: its tab has closed", "registered, and its Claude Code has gone"),
])
@pytest.mark.anyio
async def test_a_stopped_tab_says_why(store, tut, registered, why, desc):
    """A tab's session has no host to record why it stopped: once it isn't running, the
    checklist says whether its tab never started Claude Code or has closed, with how to
    start afresh."""
    if registered:
        store.register(tut, 4242, 1, "an-old-boot")   # a pid of another boot: not alive
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert app.hub.statuses[tut] == "dead", f"{desc}: not running"
        assert why in app._checklist_text, desc
        assert f"{tutorial.AGAIN} at the next start" in app._checklist_text
        assert "▶ Find the demo in its tab" in app._checklist_text


@pytest.mark.parametrize("on", [True, False])
@pytest.mark.parametrize("how", ["restore", "relaunch", "adopt", "adopt ended"])
@pytest.mark.parametrize("stored, desc", [
    ("tab", "the tutorial, recorded as a tab"),
    ("sdk", "a tutorial an earlier version hosted"),
    (None, "a tutorial with no runner recorded, as one from before hosts would be"),
])
def test_the_tutorial_comes_back_as_a_tab(store, tut, monkeypatch, on, how, stored, desc):
    """Review 68: Restore, Relaunch and Adopt with SDK mode on once made every tab, the
    tutorial's included, an SDK session. Review 69: adopting an ended tutorial's transcript
    did too. Any other session comes back the way new sessions run (test_restore_session)."""
    from claude_wheelhouse import adopt
    from claude_wheelhouse.hub import Hub
    monkeypatch.setattr(features, "_flag", on)
    monkeypatch.delenv("WHEELHOUSE_RUNNER", raising=False)
    with store.tx() as db:
        db.execute("UPDATE sessions SET runner = ? WHERE id = ?", (stored, tut))
    cwd = store.session(tut)["cwd"]
    seen = []
    monkeypatch.setattr(launch, "open_host", lambda s, i: seen.append("host"))
    monkeypatch.setattr(launch, "open_tab", lambda s, i: seen.append("tab"))
    monkeypatch.setattr(adopt.liveness, "running_pid", lambda *a, **k: None)
    if how == "restore":
        launch.restore_session(store, tut)
    elif how == "relaunch":
        assert Hub(store, sdk=on).relaunch(tut).ok, desc
    else:
        if how == "adopt ended":   # its row gone, its transcript still offered
            store.end(tut)
        adopt.adopt(store, adopt.Candidate(tut, cwd, "tutorial", 0.0, None), "", sdk=on)
    assert seen == ["tab"], f"{how}, SDK mode {on}: {desc}"
    assert store.session(tut)["runner"] == "tab", f"{how}, SDK mode {on}: {desc}"


@pytest.mark.anyio
async def test_every_hint_names_the_same_fixed_command(store, monkeypatch, fake_claude):
    """Review 68: the offer, the ? overlay, the dismiss notice and a stopped tutorial's
    checklist all name the command that has it offered again. Guessing it from
    PATH named a command that fails under make run, where uv puts the venv's
    claude-wheelhouse on PATH but the person's shell can't run it, so every hint names both.
    Each is shown in a running app, where the command is refused, so each says to stop it first."""
    from claude_wheelhouse.hub import Hub
    said = "With the wheelhouse stopped, claude-wheelhouse tutorial (make tutorial in a source checkout) offers it again"
    monkeypatch.setattr(tutorial.shutil, "which", lambda cmd, *a, **k: f"/venv/bin/{cmd}")   # as under uv run
    assert f"{said} at the next start" in keys_help(), "? overlay"
    monkeypatch.setattr(tutorial, "should_offer", real_should_offer)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        offer = app.screen.query_one("#offer")
        assert str(offer.render()).endswith(f"· {said}"), "offer"
        assert len(str(offer.render())) <= offer.content_region.width, "offer: one line at 160 columns"
    assert [e.text for e in Hub(store).answer_offer(False).notices] == \
        [f"{said}; ? lists every key"], "dismiss notice"
    tutorial.prepare(store)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert f"\n{said} at the next start" in app._checklist_text, "checklist"


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
