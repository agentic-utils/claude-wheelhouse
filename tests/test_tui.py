import json

import pytest
from textual.widgets import DataTable, TextArea

from claude_wheelhouse import launch, transcript
from claude_wheelhouse.tui import MATRIX, VOICE, WheelhouseApp, Choice, Confirm, ThreadView, render


@pytest.mark.anyio
async def test_answer_reaches_the_session(store, sid):
    q = store.post_item(sid, "question", "which db?", "Postgres or SQLite for the cache?")
    store.post_item(sid, "task", "build", status="running")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        items = app.query_one("#items", DataTable)
        assert items.row_count == 2
        assert items.get_row_at(0)[1] == q, "open question first"
        items.move_cursor(row=0)
        await pilot.pause()
        assert app.selected == (sid, q)
        app.query_one("#answer", TextArea).text = "SQLite, it's local"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert store.pending(sid) == [], "Ctrl+S queues"
        assert str(items.get_row_at(0)[2]) == "queued", "the item list shows the queued answer"
        assert "✉ 1" in str(app.query_one("#session-list", DataTable).get_row_at(0)[3])
        await pilot.press("s")
        await pilot.pause()
    assert [m["body"] for m in store.pending(sid)] == ["SQLite, it's local"]
    assert store.item(sid, q)["status"] == "answered"


@pytest.mark.anyio
async def test_subagents_sit_under_their_session(store, sid, tmp_path):
    other = store.create_session(str(tmp_path), name="other")
    store.post_item(sid, "agent", "fork one", status="running")
    store.post_item(other, "agent", "fork two", status="running")
    store.post_item(sid, "agent", "fork three", status="running")
    q = store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        items = app.query_one("#items", DataTable)
        rows = [[str(c) for c in items.get_row_at(i)][:2] for i in range(items.row_count)]
    assert rows[0] == ["demo", q], "tasks and questions first, in inbox order"
    assert [r[1] for r in rows[1:]] == ["└ A1", "└ A2", "└ A1"]
    assert [r[0] for r in rows[1:]] == ["demo", "", "other"], "each session's agents grouped under its name"


@pytest.mark.anyio
async def test_finished_items_toggle_in_and_take_a_message(store, sid):
    t = store.post_item(sid, "task", "build", status="running")
    done = store.post_item(sid, "task", "ship", status="done")
    closed = store.post_item(sid, "question", "which db?", status="closed")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        items = app.query_one("#items", DataTable)
        refs = lambda: [str(items.get_row_at(i)[1]) for i in range(items.row_count)]
        assert refs() == [t], "finished items hidden by default"
        await pilot.press("f")
        await pilot.pause()
        assert refs()[0] == t and set(refs()[1:]) == {done, closed}, "finished items after the rest"
        items.move_cursor(row=refs().index(closed))
        await pilot.pause()
        app.query_one("#answer", TextArea).text = "the punchline"
        await pilot.press("ctrl+s")
        await pilot.pause()
        app.query_one("#answer", TextArea).text = ""
        app.query_one("#answer", TextArea).focus()
        await pilot.press("f")
        await pilot.pause()
        assert len(refs()) == 3, "f types into the answer box instead of toggling"
    assert [(m["item_ref"], m["body"]) for m in store.drafts(sid)] == [(closed, "the punchline")]
    assert store.item(sid, closed)["status"] == "closed", "a message on a finished item leaves its status alone"


@pytest.mark.anyio
async def test_restore_all_only_launches_dead_sessions(store, sid, tmp_path, monkeypatch):
    parked = store.create_session(str(tmp_path), name="parked")
    store.set_parked(parked, True)
    launched = []
    monkeypatch.setattr(launch, "open_tab", lambda s, i: launched.append(i))
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("2")
        await pilot.click("#restore-all")
        await pilot.press("y")
        await pilot.pause()
    assert launched == [sid]


@pytest.mark.anyio
async def test_timers_keep_running_under_a_dialog(store, sid):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        app.push_screen(Confirm("sure?"))
        await pilot.pause()
        app.animate()
        app.refresh_data()


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_sending_to_an_ended_session_does_not_crash(store, sid):
    """Review #6: the session row went away under the selected item."""
    q = store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.query_one("#items", DataTable).move_cursor(row=0)
        await pilot.pause()
        assert app.selected == (sid, q)
        store.end(sid)
        app.query_one("#answer", TextArea).text = "too late"
        await pilot.press("ctrl+s")
        await pilot.pause()
    assert store.session(sid) is None


@pytest.mark.anyio
async def test_a_refused_restore_leaves_the_session_parked(store, sid, monkeypatch):
    """Review #8: unpark only once the launch has gone through."""
    store.set_parked(sid, True)

    def refuse(s, i):
        raise RuntimeError("already running")
    monkeypatch.setattr(launch, "open_tab", refuse)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("2")
        await pilot.pause()
        await pilot.click("#restore")
        await pilot.pause()
    assert store.session(sid)["parked"] == 1


@pytest.mark.anyio
async def test_a_send_racing_an_end_does_not_crash(store, sid, monkeypatch):
    """Round-2 #7: the session can end between the wheelhouse's check and its write."""
    store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.query_one("#items", DataTable).move_cursor(row=0)
        await pilot.pause()
        store.end(sid)
        monkeypatch.setattr(store, "session", lambda s: {"id": s})   # the check still sees it
        app.query_one("#answer", TextArea).text = "too late"
        await pilot.press("ctrl+s")
        await pilot.pause()


@pytest.mark.anyio
@pytest.mark.parametrize("key, expected, desc", [
    ("n", False, "N declines without opening New session"),
    ("y", True, "Y confirms"),
    ("escape", False, "Escape declines instead of clearing the filter underneath"),
])
async def test_confirm_keys_stay_in_the_dialog(store, sid, key, expected, desc):
    """R5 #1: Confirm's keys must not also reach the app bindings."""
    app = WheelhouseApp(store)
    answers = []
    async with app.run_test(size=(160, 40)) as pilot:
        app.push_screen(Confirm("sure?"), answers.append)
        await pilot.pause()
        await pilot.press(key)
        await pilot.pause()
        assert answers == [expected], desc
        assert [type(s).__name__ for s in app.screen_stack] == ["Screen"], desc


@pytest.mark.anyio
async def test_enter_on_a_destructive_confirm_declines(store, sid):
    """R6: Confirm opens on No, so a reflex Enter on "Force end" keeps the wheelhouse data."""
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        app.settle(sid, "end", "force")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert [type(s).__name__ for s in app.screen_stack] == ["Screen"]
    assert store.session(sid) is not None


@pytest.mark.anyio
async def test_escape_closes_new_session(store, sid):
    """R6: Escape closes New session, as it does Confirm and Choice."""
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("n")
        await pilot.pause()
        assert [type(s).__name__ for s in app.screen_stack] == ["Screen", "NewSession"]
        await pilot.press("escape")
        await pilot.pause()
        assert [type(s).__name__ for s in app.screen_stack] == ["Screen"]


def adoptable(sid="adopt-me", pid=None):
    from claude_wheelhouse.adopt import Candidate
    return Candidate(sid, "/home/u/repo", "LG plugin fix", 0.0, pid)


@pytest.mark.anyio
@pytest.mark.parametrize("alive_on_adopt, launched, stack, desc", [
    (None, [("adopt-me", "LG plugin fix")], ["Screen"], "an exited session is adopted at once"),
    (4242, [], ["Screen", "AdoptSession"], "a running one stays in the dialog until /exit"),
])
async def test_adopt(store, monkeypatch, alive_on_adopt, launched, stack, desc):
    from claude_wheelhouse import adopt, liveness
    monkeypatch.setattr(adopt, "candidates", lambda store: [adoptable(pid=4242)])
    monkeypatch.setattr(liveness, "running_pid", lambda sid, *a: alive_on_adopt)
    got = []
    monkeypatch.setattr(adopt, "adopt", lambda store, c, name: got.append((c.id, name)))
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("a")
        await pilot.pause()
        assert "Still running" in str(app.screen.query_one("#adopt-hint").render()), desc
        await pilot.press("n")   # app keys stay out of the dialog
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert got == launched, desc
        assert [type(s).__name__ for s in app.screen_stack] == stack, desc


@pytest.mark.anyio
async def test_escape_closes_adopt(store, monkeypatch):
    from claude_wheelhouse import adopt
    monkeypatch.setattr(adopt, "candidates", lambda store: [])
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert [type(s).__name__ for s in app.screen_stack] == ["Screen"]


@pytest.mark.parametrize("key, sent, desc", [
    ("ctrl+enter", True, "Ctrl+Enter where the terminal reports it"),
    ("ctrl+j", True, "Ctrl+Enter as most terminals send it, a line feed"),
    ("ctrl+x", False, "Ctrl+X no longer sends"),
])
@pytest.mark.anyio
async def test_ctrl_enter_sends_one_answer_now(store, sid, key, sent, desc):
    q = store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.query_one("#items", DataTable).move_cursor(row=0)
        app.query_one("#answer", TextArea).focus()
        await pilot.press(*"now")
        await pilot.press(key)
        await pilot.pause()
    assert [m["body"] for m in store.pending(sid)] == (["now"] if sent else []), desc
    assert store.item(sid, q)["status"] == ("answered" if sent else "open"), desc


@pytest.mark.anyio
async def test_shift_s_sends_every_session(store, sid, tmp_path):
    other = store.create_session(str(tmp_path), name="other")
    store.queue(sid, "a", "Q1")
    store.queue(sid, "b", "Q2")
    store.queue(other, "c")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert "3 queued" in str(app.query_one("#outbox").render())
        await pilot.press("S")
        await pilot.pause()
        assert str(app.query_one("#outbox").render()).strip() == ""
    assert store.drafts() == []
    assert [m["body"] for m in store.pending(sid)] == ["a", "b"]
    assert [m["body"] for m in store.pending(other)] == ["c"]


@pytest.mark.anyio
async def test_thread_view_holds_the_conversation(store, sid):
    q = store.post_item(sid, "question", "which db?", "Postgres or SQLite?")
    store.update_item(sid, q, note="looked at both")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        items = app.query_one("#items", DataTable)
        items.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ThreadView)
        await pilot.press(*"SQLite")
        store.reply(sid, q, "noted, any version?", asks=True)
        app.screen.paint()
        await pilot.pause()
        thread = app.screen.text
        assert "noted, any version?" in thread and "> looked at both" in thread, "replies and quieter notes"
        assert app.screen.box.text == "SQLite", "a reply arriving leaves typed text alone"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert "you · queued" in app.screen.text
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert app.screen.box.text == "SQLite" and store.drafts() == [], "taken back to edit"
        await pilot.press("ctrl+s")
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, ThreadView)
    assert [m["body"] for m in store.drafts(sid)] == ["SQLite"]


@pytest.mark.anyio
@pytest.mark.parametrize("synopsis, shown, desc", [
    ("Building the thread view for issue 11.", "Building the thread view for issue 11.", "the session's own words"),
    ("", "No synopsis yet", "a placeholder until the session sets one"),
])
async def test_sessions_tab_shows_the_synopsis(store, sid, synopsis, shown, desc):
    store.set_synopsis(sid, synopsis)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("2")
        await pilot.pause()
        app.query_one("#session-table", DataTable).move_cursor(row=0)
        await pilot.pause()
        assert shown in app._synopsis_text, desc


@pytest.mark.anyio
async def test_end_names_the_queued_answers_it_discards(store, sid):
    store.queue(sid, "a", "Q1")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("2")
        await pilot.pause()
        await pilot.click("#end")
        await pilot.pause()
        assert "1 queued answer(s) will be discarded" in app.screen.prompt


@pytest.mark.anyio
@pytest.mark.parametrize("stamped, label, queued, desc", [
    (True, "live", 1, "a session on current code queues the answer"),
    (False, "live · needs relaunch", 0, "a session on older code would deliver a draft at once, so it's sent now"),
])
async def test_a_session_on_older_code_cannot_queue(store, sid, monkeypatch, stamped, label, queued, desc):
    from claude_wheelhouse import liveness
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "live")
    if stamped:
        store.mark_version(sid)
    q = store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert str(app.query_one("#session-table", DataTable).get_row_at(0)[0]) == label, desc
        app.query_one("#items", DataTable).move_cursor(row=0)
        app.query_one("#answer", TextArea).text = "SQLite"
        await pilot.press("ctrl+s")
        await pilot.pause()
    assert (len(store.drafts()), len(store.pending(sid))) == (queued, 1 - queued), desc
    assert store.item(sid, q)["status"] == ("open" if queued else "answered"), desc


@pytest.mark.anyio
async def test_shift_s_with_nothing_left_to_send_says_so(store, sid, monkeypatch):
    store.queue(sid, "a", "Q1")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(store, "dispatch", lambda sid: 0)   # taken back meanwhile
        notes = []
        monkeypatch.setattr(app, "notify", lambda text, **kw: notes.append(text))
        await pilot.press("S")
        await pilot.pause()
    assert notes == ["nothing queued"]


@pytest.mark.anyio
async def test_enter_on_a_session_follows_its_conversation(store, sid, tmp_path, monkeypatch):
    folder = tmp_path / "projects/-home-u-repo"
    folder.mkdir(parents=True)
    recs = [{"type": "user", "timestamp": "2026-10-07T21:30:00Z", "message": {"content": "fix the VAT rounding"}},
            {"type": "assistant", "timestamp": "2026-10-07T21:31:00Z", "message": {"content": [
                {"type": "text", "text": "Found it in invoice.py."},
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "make test"}}]}}]
    folder.joinpath(f"{sid}.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    monkeypatch.setattr(transcript, "PROJECTS", tmp_path / "projects")
    q = store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.query_one("#session-list", DataTable).focus()
        await pilot.press("enter", "n")   # it isn't running: decline the offer to relaunch it
        await pilot.pause()
        text = app._detail_text
        assert "demo · conversation" in text and "fix the VAT rounding" in text and "`⚙ Bash: make test`" in text
        assert "which db?" not in text, "the conversation, not the highlighted item"
        app.query_one("#answer", TextArea).text = "ship it"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert [(m["item_ref"], m["body"]) for m in store.drafts(sid)] == [(None, "ship it")], "a general message"
        assert "**you · queued**\n\nship it" in app._detail_text
        items = app.query_one("#items", DataTable)
        assert [str(items.get_row_at(i)[3]) for i in range(items.row_count)] == ["Conversation", "which db?"], \
            "the session's conversation is the pinned first row, and it is highlighted, not the question"
        assert items.cursor_row == 0
        items.focus()
        await pilot.press("down")
        await pilot.pause()
        assert app.viewing is None and "## Q1 · which db?" in app._detail_text, "moving to the question leaves it"
        await pilot.press("up")
        await pilot.pause()
        assert app.viewing == sid and app.answer.text == "", "and back: the queued message is in the pane, not the box"


@pytest.mark.anyio
async def test_one_click_on_a_session_follows_it(store, sid, tmp_path):
    other = store.create_session(str(tmp_path), name="other")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        table = app.query_one("#session-list", DataTable)
        row = [table.coordinate_to_cell_key((i, 0)).row_key.value for i in range(table.row_count)].index(other)
        await pilot.click("#session-list", offset=(4, row + 1))   # below the header row
        await pilot.pause()
        assert app.viewing == other and "other · conversation" in app._detail_text


@pytest.mark.parametrize("size, desc", [
    ((80, 24), "a small terminal"),
    ((120, 30), "a typical Windows Terminal tab"),
    ((200, 50), "a large terminal"),
])
@pytest.mark.anyio
async def test_the_screen_fits_so_the_tabs_never_scroll_off(store, sid, size, desc):
    from textual import events
    app = WheelhouseApp(store)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        assert app.screen.max_scroll_y == 0, f"{desc}: nothing overflows the screen"
        sessions = app.query_one("#session-list")
        for _ in range(3):   # a wheel over a list too short to scroll bubbles up to the screen
            sessions.post_message(events.MouseScrollDown(sessions, 5, 1, 0, 0, 0, False, False, False))
        await pilot.pause()
        assert app.query_one("#title").region.y == 0, f"{desc}: the title and tabs stay on screen"
        app.push_screen(ThreadView(sid, store.post_item(sid, "task", "build")))
        await pilot.pause()
        assert app.screen.max_scroll_y == 0, f"{desc}: the thread view fits too"


@pytest.mark.parametrize("who, colour, desc", [
    ("you", MATRIX, "the person's prompts stay terminal green"),
    ("wheelhouse", MATRIX, "so do their messages through the wheelhouse"),
    ("claude", VOICE["claude"], "Claude's words are white, as in the Claude app"),
])
def test_each_voice_has_its_colour(who, colour, desc):
    from rich.console import Console
    console = Console(width=60)
    segments = list(console.render(render([(who, "**label**\n\nhello there")])))
    styles = {s.style.color.triplet.hex for s in segments if "hello" in s.text and s.style and s.style.color}
    assert styles == {colour}, desc


@pytest.mark.anyio
async def test_a_long_conversation_is_one_widget(store, sid, tmp_path, monkeypatch):
    folder = tmp_path / "projects/-home-u-repo"
    folder.mkdir(parents=True)
    recs = [{"type": "user" if i % 2 else "assistant", "timestamp": "2026-10-07T21:30:00Z",
             "message": {"content": f"para one {i}\n\npara two\n\n- a\n- b"} if i % 2 else
             {"content": [{"type": "text", "text": f"reply {i}\n\n```\ncode\n```\n\nmore"}]}} for i in range(200)]
    folder.joinpath(f"{sid}.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    monkeypatch.setattr(transcript, "PROJECTS", tmp_path / "projects")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.follow(sid)
        await pilot.pause()
        assert "para one 199" in app._detail_text
        assert len(app.detail.children) == 0, "drawn as one renderable: a widget per paragraph made each keypress slow"


async def restore_button(pilot):
    await pilot.press("2")
    await pilot.click("#restore")


async def select_in_inbox(pilot):
    pilot.app.query_one("#session-list", DataTable).focus()
    await pilot.press("enter", "y")


async def select_in_sessions_tab(pilot):
    await pilot.press("2")
    pilot.app.query_one("#session-table", DataTable).focus()
    await pilot.press("enter", "y")


@pytest.mark.parametrize("how, desc", [
    (restore_button, "the Restore button"),
    (select_in_inbox, "selecting the dead session in the inbox, then y"),
    (select_in_sessions_tab, "selecting it in the Sessions tab, then y"),
])
@pytest.mark.anyio
async def test_bringing_back_a_dead_session_resumes_it_with_the_join_notice(store, sid, monkeypatch, how, desc):
    launched = []
    monkeypatch.setattr(launch.liveness, "is_alive", lambda *a: False)
    monkeypatch.setattr(launch.subprocess, "Popen", lambda argv, **kw: launched.append(argv))
    monkeypatch.setattr(launch, "transcript_exists", lambda sid: True)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        await how(pilot)
        await pilot.pause()
    assert len(launched) == 1, desc
    assert [m["body"] for m in store.pending(sid)] == [launch.JOINED_TEXT], desc


@pytest.mark.anyio
async def test_declining_the_relaunch_leaves_a_dead_session_be(store, sid, monkeypatch):
    monkeypatch.setattr(launch, "open_tab", lambda s, i: pytest.fail("relaunched after n"))
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.query_one("#session-list", DataTable).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, Confirm)
        await pilot.press("n")
        await pilot.pause()
        assert app.viewing == sid, "it still follows the session's conversation"


@pytest.mark.anyio
async def test_a_focused_answer_box_shows_a_hot_blinking_cursor(store, sid):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        box = app.query_one("#answer", TextArea)
        unfocused = box.styles.border_top[1]
        box.focus()
        await pilot.pause()
        assert box.cursor_blink and box.styles.border_top[1] != unfocused, "the box lights up when it has focus"
        cursor = next(iter(box.render_line(0)))
        assert cursor.style.bgcolor.triplet.hex == "#ff2a6d", "a hot pink block, not the pale default"


@pytest.mark.anyio
async def test_unsent_text_stays_with_the_item_it_was_typed_for(store, sid, tmp_path):
    other = store.create_session(str(tmp_path), name="other")
    q1 = store.post_item(sid, "question", "which db?")
    q2 = store.post_item(sid, "question", "which port?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        items, box = app.query_one("#items", DataTable), app.query_one("#answer", TextArea)
        row = lambda ref: [str(items.get_row_at(i)[1]) for i in range(items.row_count)].index(ref)
        steps = [
            (lambda: items.move_cursor(row=row(q1)), "", "a fresh item starts empty"),
            (lambda: setattr(box, "text", "SQLite"), "SQLite", "typed for Q1"),
            (lambda: items.move_cursor(row=row(q2)), "", "another item clears the box"),
            (lambda: setattr(box, "text", "8080"), "8080", "typed for Q2"),
            (lambda: app.follow(other), "", "a session's conversation has its own box"),
            (lambda: app.action_clear_filter(), None, "leaving it: the highlighted item's text, whichever it is"),
            (lambda: items.move_cursor(row=row(q1)), "SQLite", "Q1's text comes back"),
            (lambda: items.move_cursor(row=row(q2)), "8080", "and Q2's"),
        ]
        for act, expected, desc in steps:
            act()
            await pilot.pause()
            app.paint_detail()
            assert expected is None or box.text == expected, desc
        items.focus()
        items.move_cursor(row=row(q1))
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.box.text == "SQLite", "the thread view picks up what was typed for its item"
        await pilot.press("escape")
        await pilot.pause()
        assert box.text == "SQLite", "and hands it back"


@pytest.mark.anyio
async def test_one_click_on_a_dead_session_opens_one_prompt_and_yes_closes_it(store, sid, tmp_path, monkeypatch):
    other = store.create_session(str(tmp_path), name="other")
    launched = []
    monkeypatch.setattr(launch, "open_tab", lambda s, i: launched.append(i))
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        table = app.query_one("#session-list", DataTable)
        row = [table.coordinate_to_cell_key((i, 0)).row_key.value for i in range(table.row_count)].index(other)
        await pilot.click("#session-list", offset=(4, row + 1))
        await pilot.pause()
        assert [type(s) for s in app.screen_stack[1:]] == [Confirm], "one click, one prompt"
        await pilot.click("#yes")
        await pilot.pause()
        assert len(app.screen_stack) == 1 and launched == [other], "Yes relaunches and closes it"


@pytest.mark.parametrize("screen, labels, desc", [
    (lambda: Confirm("ok?"), ["[Y]es", "[N]o"], "the relaunch and destructive confirms"),
    (lambda: Choice("hm?"), ["[C]ancel request", "[F]orce", "Leave it [Esc]"], "a pending request's choice"),
])
@pytest.mark.anyio
async def test_button_labels_keep_their_key_hints(store, screen, labels, desc):
    from textual.widgets import Button
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        app.push_screen(screen())
        await pilot.pause()
        assert [str(b.label) for b in app.screen.query(Button)] == labels, f"{desc}: not read as markup"


@pytest.mark.anyio
async def test_restored_text_is_typed_onto_at_the_end(store, sid):
    q = store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        items = app.query_one("#items", DataTable)
        items.move_cursor(row=0)
        await pilot.pause()
        await pilot.click("#answer")
        await pilot.press(*"SQL")
        items.focus()
        await pilot.press("enter")   # open the question full screen: its text comes along
        await pilot.pause()
        await pilot.press(*"ite")
        await pilot.pause(1.2)   # across a refresh tick
        await pilot.press(*"!")
        assert app.screen.box.text == "SQLite!", "typing carries on at the end, not the start"


@pytest.mark.parametrize("conversation, desc", [
    (False, "answering a question"),
    (True, "writing to the session from its conversation row"),
])
@pytest.mark.anyio
async def test_typing_survives_refresh_ticks_even_when_items_arrive_above(store, sid, conversation, desc):
    q = store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        if conversation:
            app.follow(sid)
        else:
            app.query_one("#items", DataTable).move_cursor(row=0)
        await pilot.pause()
        await pilot.click("#answer")
        await pilot.press(*"SQL")
        store.post_item(sid, "question", "which port?")   # sorts into the list during typing
        await pilot.pause(2.2)
        await pilot.press(*"ite")
        assert app.selected == (sid, None if conversation else q), f"{desc}: the highlight stays put"
        assert (app.answer.text, app.answer.cursor_location) == ("SQLite", (0, 6)), f"{desc}: text and cursor untouched"
