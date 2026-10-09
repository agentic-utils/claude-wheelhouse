import json
import time

import pytest
from rich.text import Text
from textual.widgets import Button, Checkbox, DataTable, Footer, Input, Label, TextArea

from claude_wheelhouse import launch, transcript
from claude_wheelhouse.store import PROTOCOL_VERSION
from claude_wheelhouse.tui import (MATRIX, VOICE, WheelhouseApp, Choice, Confirm, Folders, Hint, SendBar, ThreadView,
                                   Transcript, render)


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
        await pilot.press("ctrl+enter")
        await pilot.pause()
        assert store.pending(sid) == [], "Ctrl+Enter queues: sessions start in queued mode"
        assert str(items.get_row_at(0)[2]) == "queued", "the item list shows the queued answer"
        assert "✉ 1" in str(app.query_one("#session-list", DataTable).get_row_at(0)[5])
        await pilot.press("ctrl+s")
        await pilot.pause()
    assert [m["body"] for m in store.pending(sid)] == ["SQLite, it's local"]
    assert store.item(sid, q)["status"] == "open", "the session's reply says whether it's answered"


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
        await pilot.press("ctrl+enter")
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
        await pilot.press("ctrl+enter")
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
        monkeypatch.setattr(store, "session", lambda s: {"id": s, "send_mode": None})   # the check still sees it
        app.query_one("#answer", TextArea).text = "too late"
        await pilot.press("ctrl+enter")
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


@pytest.mark.anyio
@pytest.mark.parametrize("name, named, title, prefill, desc", [
    ("Rare caper", "x" * 100, "x" * 69 + "…", "Rare caper", "a tracked session: its name in the wheelhouse"),
    ("", "x" * 100, "x" * 69 + "…", "x" * 100, "else its own name, whole"),
    ("", "", "y" * 69 + "…", "y" * 40, "else the start of its first prompt"),
])
async def test_adopt_prefills_the_name(store, monkeypatch, name, named, title, prefill, desc):
    from claude_wheelhouse import adopt
    from claude_wheelhouse.adopt import Candidate
    monkeypatch.setattr(adopt, "candidates", lambda store: [Candidate("adopt-me", "/r", title, 0.0, None, name, named)])
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("a")
        await pilot.pause()
        assert app.screen.query_one("#adopt-name", Input).value == prefill, desc


@pytest.mark.parametrize("mode, key, queued, sent, desc", [
    (None, "ctrl+enter", ["now"], [], "a new session starts in queued mode: Ctrl+Enter queues"),
    ("immediate", "ctrl+enter", [], ["now"], "in immediate mode Ctrl+Enter sends at once"),
    ("immediate", "ctrl+j", [], ["now"], "Ctrl+Enter as most terminals send it, a line feed"),
    ("queued", "ctrl+j", ["now"], [], "the line feed queues too, in queued mode"),
    ("immediate", "ctrl+x", [], [], "Ctrl+X cuts, it doesn't send"),
])
@pytest.mark.anyio
async def test_ctrl_enter_submits_by_the_sessions_mode(store, sid, mode, key, queued, sent, desc):
    if mode:
        store.set_mode(sid, mode)
    store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.query_one("#items", DataTable).move_cursor(row=0)
        app.query_one("#answer", TextArea).focus()
        await pilot.press(*"now")
        await pilot.press(key)
        await pilot.pause()
    assert ([m["body"] for m in store.drafts(sid)], [m["body"] for m in store.pending(sid)]) == (queued, sent), desc


@pytest.mark.anyio
async def test_ctrl_t_switches_the_sessions_mode(store, sid, tmp_path):
    other = store.create_session(str(tmp_path), name="other")
    store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.query_one("#items", DataTable).move_cursor(row=0)
        await pilot.pause()
        mode_label = lambda: str(app.screen.query_one("#mode").label)
        assert mode_label() == "Mode: Queued"
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert mode_label() == "Mode: Immediate"
        assert store.session(sid)["send_mode"] == "immediate"
        assert store.session(other)["send_mode"] is None, "only the session in context changes"
        await pilot.click("#mode")
        await pilot.pause()
        assert mode_label() == "Mode: Queued", "the mode button toggles it too"


@pytest.mark.anyio
async def test_ctrl_s_sends_only_the_current_sessions_queue(store, sid, tmp_path):
    other = store.create_session(str(tmp_path), name="other")
    store.post_item(sid, "question", "which db?")
    store.queue(sid, "a", "Q1")
    store.queue(other, "b")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.query_one("#items", DataTable).move_cursor(row=0)
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
    assert [m["body"] for m in store.pending(sid)] == ["a"]
    assert [m["body"] for m in store.drafts(other)] == ["b"], "another session's queue waits"


@pytest.mark.anyio
async def test_send_bar_buttons_follow_the_queues(store, sid, tmp_path):
    other = store.create_session(str(tmp_path), name="other")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        bar = lambda: {b.id: (str(b.label), b.disabled) for b in app.screen.query("SendBar Button") if b.display}
        assert bar() == {"mode": ("Mode", True), "send": ("Send (0)", True), "send-all": ("Send all (0)", True)}, \
            "no session in context, nothing queued"
        store.post_item(sid, "question", "which db?")   # highlighted as it arrives: its session is in context
        store.queue(sid, "a", "Q1")
        store.queue(other, "b")
        store.queue(other, "c")
        app.refresh_data()
        await pilot.pause()
        assert bar() == {"mode": ("Mode: Queued", False), "send": ("Send (1)", False),
                         "send-all": ("Send all (3)", False)}
        assert all(not b.can_focus for b in app.screen.query("SendBar Button")), "clicks leave focus alone"
        await pilot.click("#send-all")
        await pilot.pause()
        assert bar()["send-all"] == ("Send all (0)", True)
    assert store.drafts() == []
    assert [m["body"] for m in store.pending(sid)] == ["a"]
    assert [m["body"] for m in store.pending(other)] == ["b", "c"]


@pytest.mark.anyio
async def test_the_thread_views_bar_is_its_sessions(store, sid, tmp_path):
    other = store.create_session(str(tmp_path), name="other")
    q = store.post_item(other, "question", "which db?")
    store.queue(sid, "a")
    store.queue(other, "b", q)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.follow(sid)   # the inbox's context is the first session
        await pilot.pause()
        app.push_screen(ThreadView(other, q))
        await pilot.pause()
        app.refresh_data()
        await pilot.pause()
        assert str(app.screen.query_one("#send").label) == "Send (1)"
        await pilot.press("ctrl+s")
        await pilot.pause()
    assert [m["body"] for m in store.pending(other)] == ["b"], "the thread's session, not the inbox's"
    assert [m["body"] for m in store.drafts(sid)] == ["a"]


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
        store.reply(sid, q, "noted, any version?", "open")
        app.screen.paint()
        await pilot.pause()
        thread = app.screen.text
        assert "noted, any version?" in thread and "> looked at both" in thread, "replies and quieter notes"
        assert app.screen.box.text == "SQLite", "a reply arriving leaves typed text alone"
        await pilot.press("ctrl+enter")
        await pilot.pause()
        assert "you · queued" in app.screen.text
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert app.screen.box.text == "SQLite" and store.drafts() == [], "taken back to edit"
        await pilot.press("ctrl+enter")
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
@pytest.mark.parametrize("version, label, bar, queued, desc", [
    (PROTOCOL_VERSION, "live", "Mode: Queued", 1, "a session on current code queues the answer"),
    (PROTOCOL_VERSION - 1, "live · needs relaunch", "Mode: Queued", 1,
     "one on older code that holds queued answers still queues: a relaunch only brings the new code"),
    (1, "live · needs relaunch", "Sends now: needs relaunch", 0,
     "one from before queued answers would deliver one at once, so it's sent now"),
    (None, "live · needs relaunch", "Sends now: needs relaunch", 0, "as is one that never stamped its version"),
])
async def test_a_session_on_older_code_cannot_queue(store, sid, monkeypatch, version, label, bar, queued, desc):
    from claude_wheelhouse import liveness
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "live")
    store.db.execute("UPDATE sessions SET code_version = ?", (version,))
    q = store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert str(app.query_one("#session-table", DataTable).get_row_at(0)[0]) == label, desc
        app.query_one("#items", DataTable).move_cursor(row=0)
        await pilot.pause()
        assert str(app.query_one("#mode", Button).label) == bar, desc
        app.query_one("#answer", TextArea).text = "SQLite"
        await pilot.press("ctrl+enter")
        await pilot.pause()
    assert (len(store.drafts()), len(store.pending(sid))) == (queued, 1 - queued), desc



@pytest.mark.anyio
async def test_send_all_with_nothing_left_to_send_says_so(store, sid, monkeypatch):
    store.queue(sid, "a", "Q1")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(store, "dispatch", lambda sid: 0)   # taken back meanwhile
        notes = []
        monkeypatch.setattr(app, "notify", lambda text, **kw: notes.append(text))
        await pilot.click("#send-all")
        await pilot.pause()
    assert notes == ["nothing queued"]


def test_a_general_message_cut_short_shows_whole_in_the_conversation(store, sid, tmp_path, monkeypatch):
    long = " ".join(["word"] * 200)
    store.send(sid, long)
    [m] = store.pending(sid)
    line = f"[wheelhouse] from doug (general): [cut short, full text: get_input(message_id={m['id']})] word word…"
    folder = tmp_path / "projects/-home-u-repo"
    folder.mkdir(parents=True)
    folder.joinpath(f"{sid}.jsonl").write_text(json.dumps(
        {"type": "user", "timestamp": "2026-10-07T21:30:00Z", "origin": {"kind": "task-notification"},
         "message": {"content": f"<task-notification><event>{line}</event></task-notification>"}}) + "\n")
    monkeypatch.setattr(transcript, "PROJECTS", tmp_path / "projects")
    [(who, md)] = [b for b in WheelhouseApp(store).conversation(sid) if b[0] == "you"]
    assert md.endswith(long), "the wheelhouse's copy, whole"


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
        await pilot.press("ctrl+enter")
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


@pytest.mark.parametrize("keys, expected, desc", [
    (["h", "i", "space", "colon", "g", "r", "i", "tab"], "hi 😁", "Tab takes the first suggestion"),
    (["colon", "t", "h", "u", "enter"], "👍", "so does Enter"),
    (["o", "k", "space", "colon", "t", "a", "d", "a", "colon"], "ok 🎉", "a closed code turns into its emoji"),
    (["o", "k", "enter", "x"], "ok\nx", "Enter with no suggestion is a new line"),
])
@pytest.mark.anyio
async def test_emoji_codes_in_the_answer_box(store, sid, keys, expected, desc):
    store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.query_one("#items", DataTable).move_cursor(row=0)
        await pilot.click("#answer")
        await pilot.press(*keys)
        await pilot.pause()
        assert app.answer.text == expected, desc


@pytest.mark.anyio
async def test_a_pasted_code_converts_on_send(store, sid):
    store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.query_one("#items", DataTable).move_cursor(row=0)
        await pilot.pause()
        store.set_mode(sid, "immediate")
        app.answer.text = "pasted :rocket: in"   # pasted, never typed: the send converts it
        app.answer.focus()
        await pilot.press("ctrl+enter")
        await pilot.pause()
    assert [m["body"] for m in store.pending(sid)] == ["pasted 🚀 in"]


async def drag_over_the_question(pilot, pane):
    await pilot.mouse_down(pane, offset=(0, 0))
    await pilot.hover(pane, offset=(5, 0))
    await pilot.mouse_up(pane, offset=(5, 0))


async def ctrl_a_in_the_pane(pilot, pane):
    pane.focus()
    await pilot.press("ctrl+a")


@pytest.mark.anyio
@pytest.mark.parametrize("select, expected, desc", [
    (drag_over_the_question, "Q1 · w", "dragging the mouse selects part of the text"),
    (ctrl_a_in_the_pane, "Q1 · which db?", "Ctrl+A selects all of it, starting at the top"),
])
async def test_the_detail_pane_selects_and_copies(store, sid, select, expected, desc):
    store.post_item(sid, "question", "which db?", "Postgres or SQLite for the cache?")
    app = WheelhouseApp(store)
    copied = []
    app.copy_to_clipboard = copied.append
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.query_one("#items", DataTable).move_cursor(row=0)
        await pilot.pause()
        await select(pilot, app.query_one("#detail", Transcript))
        await pilot.pause()
        await pilot.press("ctrl+c")
        await pilot.pause()
    assert copied and copied[0].startswith(expected), desc
    if select is ctrl_a_in_the_pane:
        assert copied[0].endswith("Postgres or SQLite for the cache?"), desc


@pytest.mark.anyio
async def test_ctrl_a_selects_all_of_the_answer_box(store, sid):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.answer.focus()
        app.answer.text = "one two\nthree"
        await pilot.press("ctrl+a")
        assert app.answer.selected_text == "one two\nthree", "Ctrl+A selects all, not line start"


@pytest.mark.anyio
@pytest.mark.parametrize("row, desc", [
    (0, "the first question"),
    (1, "a question below the first: each refresh used to highlight row 0 and come back"),
])
async def test_refresh_leaves_a_selection_in_the_answer_box(store, sid, row, desc):
    from textual.widgets.text_area import Selection
    store.post_item(sid, "question", "first q")
    store.post_item(sid, "question", "second q")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.items_table.focus()
        app.items_table.move_cursor(row=row)
        await pilot.pause()
        picked = app.selected
        app.answer.focus()
        app.answer.text = "hello world selection"
        app.answer.selection = Selection((0, 0), (0, 5))
        painted = []
        app.paint_detail = lambda real=app.paint_detail: (painted.append(app.selected), real())
        for _ in range(3):
            app.refresh_data()
            await pilot.pause()
        assert app.answer.selection == Selection((0, 0), (0, 5)), desc
        assert app.answer.text == "hello world selection", desc
        assert set(painted) == {picked}, f"the pane never flips to another item: {desc}"


@pytest.mark.anyio
@pytest.mark.parametrize("new_item, desc", [
    (False, "a move just before an unchanged refresh lands (the flaky test: the rebuild's own "
            "highlight of row 0 used to arrive after it and take the pane back)"),
    (True, "a move overtaken by a rebuild is dropped, so the pane shows what is highlighted"),
])
async def test_a_refresh_racing_a_move_keeps_pane_and_highlight_together(store, sid, new_item, desc):
    store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.follow(sid)
        await pilot.pause()
        items = app.items_table
        items.move_cursor(row=1)   # its RowHighlighted is queued, not yet handled
        if new_item:
            store.post_item(sid, "question", "which cache?")
        app.refresh_data()
        await pilot.pause()
        under_cursor = items.coordinate_to_cell_key((items.cursor_row, 0)).row_key.value
        assert under_cursor == f"{app.selected[0]}|{app.selected[1] or ''}", desc
        if not new_item:
            assert app.selected == (sid, "Q1"), desc


@pytest.mark.anyio
@pytest.mark.parametrize("kind, status, finished, focus_box, expected, desc", [
    ("question", "open", False, False, "closed", "x closes a question"),
    ("question", "answered", False, False, "closed", "an answered one too"),
    ("question", "closed", True, False, "answered", "x on a closed question (shown with f) reopens it"),
    ("task", "running", False, False, "running", "a task's status is the session's: x leaves it"),
    ("question", "open", False, True, "open", "in the answer box x is just a letter"),
])
async def test_x_closes_and_reopens_questions(store, sid, kind, status, finished, focus_box, expected, desc):
    ref = store.post_item(sid, kind, "which db?", status=status)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        if finished:
            await pilot.press("f")
        app.items_table.focus()
        app.items_table.move_cursor(row=0)
        await pilot.pause()
        if focus_box:
            app.answer.focus()
        await pilot.press("x")
        await pilot.pause()
    assert store.item(sid, ref)["status"] == expected, desc


@pytest.mark.anyio
@pytest.mark.parametrize("steps, shown, desc", [
    ([], "open", "nothing said yet"),
    (["send"], "⏳ open", "the person spoke last: awaiting the session's reply"),
    (["send", "reply"], "open", "the session replied, still waiting on the person"),
])
async def test_the_item_list_shows_a_question_awaiting_the_session(store, sid, steps, shown, desc):
    q = store.post_item(sid, "question", "which db?")
    act = {"send": lambda: store.send(sid, "what's it for?", q),
           "reply": lambda: store.reply(sid, q, "the cache", "open")}
    for step in steps:
        act[step]()
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert str(app.items_table.get_row_at(0)[2]) == shown, desc


@pytest.mark.anyio
async def test_a_decision_is_seen_once_viewed_and_stays_until_closed(store, sid):
    q = store.post_item(sid, "question", "which db?")
    d = store.post_item(sid, "decision", "cache in SQLite", alternative="Postgres",
                        why="no server to run", reverse="swap the DSN")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        items, sessions = app.items_table, app.query_one("#session-list", DataTable)
        assert [items.get_row_at(i)[1] for i in range(items.row_count)] == [q, d], "the question ranks first"
        assert str(items.get_row_at(1)[2]) == "unseen"
        assert str(sessions.get_row_at(0)[3]) == "1", "the session list counts unseen decisions"
        assert store.item(sid, d)["status"] == "unseen", "not seen until the person looks"
        items.focus()
        items.move_cursor(row=1)
        await pilot.pause(1.2)   # past a refresh tick
        assert store.item(sid, d)["status"] == "seen", "viewing it marks it seen"
        assert str(sessions.get_row_at(0)[3]) == ""
        items.move_cursor(row=0)
        await pilot.pause(1.2)
        assert [items.get_row_at(i)[1] for i in range(items.row_count)] == [q, d], "moving on leaves it there"
        items.move_cursor(row=1)
        await pilot.press("x")
        await pilot.pause()
        assert store.item(sid, d)["status"] == "closed", "x closes it"
        assert [items.get_row_at(i)[1] for i in range(items.row_count)] == [q], "then it's finished"


@pytest.mark.anyio
@pytest.mark.parametrize("start, finished, expected, desc", [
    ("unseen", False, "closed", "x closes an unseen decision"),
    ("seen", False, "closed", "and a seen one"),
    ("closed", True, "seen", "x on a closed decision (shown with f) reopens it as seen"),
])
async def test_x_closes_and_reopens_decisions(store, sid, start, finished, expected, desc):
    d = store.post_item(sid, "decision", "cache in SQLite", alternative="Postgres", why="local", reverse="swap")
    if start == "closed":
        store.close_decision(sid, d)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        if finished:
            await pilot.press("f")
        app.items_table.focus()
        app.items_table.move_cursor(row=0)
        await pilot.pause()
        await pilot.press("x")
        await pilot.pause()
    assert store.item(sid, d)["status"] == expected, desc


@pytest.mark.parametrize("steps, marked, desc", [
    ([("ctrl", 2)], ["Q1", "Q3"], "Ctrl+click adds to the highlighted row"),
    ([("ctrl", 2), ("ctrl", 2)], ["Q1"], "a second Ctrl+click unmarks"),
    ([("click", 1), ("shift", 3)], ["Q2", "Q3", "Q4"], "Shift+click marks the range"),
    ([("ctrl", 0), ("ctrl", 1), ("shift", 3)], ["Q2", "Q3", "Q4"], "the range runs from the last one toggled"),
    ([("key", "space"), ("key", "down"), ("key", "down"), ("key", "space")], ["Q1", "Q3"], "Space toggles"),
    ([("key", "shift+down"), ("key", "shift+down")], ["Q1", "Q2", "Q3"], "Shift+Down extends"),
    ([("ctrl", 2), ("key", "escape")], [], "Esc clears the marks"),
    ([("ctrl", 2), ("click", 3)], [], "a plain click starts afresh"),
])
@pytest.mark.anyio
async def test_marking_rows(store, sid, steps, marked, desc, monkeypatch):
    # one timestamp, so Q1-Q4 keep their order: under load they could straddle a second,
    # and the newest-first inbox would put the later ones above Q1
    with monkeypatch.context() as m:
        m.setattr("claude_wheelhouse.store.now", lambda: "2026-10-08T10:00:00+00:00")
        for title in "abcd":
            store.post_item(sid, "question", title)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        items = app.items_table
        items.focus()
        for how, arg in steps:
            if how == "key":
                await pilot.press(arg)
            else:
                await pilot.click("#items", offset=(20, 1 + arg), control=how == "ctrl", shift=how == "shift")
            await pilot.pause()
        assert sorted(k.split("|")[1] for k in items.marked) == marked, desc
        shaded = [items.get_row_at(i)[1] for i in range(items.row_count)
                  if isinstance(cell := items.get_row_at(i)[3], Text) and cell.style.bgcolor]
        assert [str(r) for r in shaded] == marked, f"{desc}: marked rows are shaded"


@pytest.mark.anyio
async def test_x_closes_the_marked_questions_and_reopens_them(store, sid):
    q1, q2, q3 = (store.post_item(sid, "question", t) for t in "abc")
    t = store.post_item(sid, "task", "build", status="running")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        items = app.items_table
        items.focus()
        await pilot.click("#items", offset=(20, 3), control=True)   # Q1 and Q3
        await pilot.click("#items", offset=(20, 4), control=True)   # and the task
        store.post_item(sid, "question", "d")   # a rebuild: the marks are by key
        await pilot.pause(1.2)
        assert sorted(k.split("|")[1] for k in items.marked) == [q1, q3, t], "marks survive a refresh"
        await pilot.press("x")
        await pilot.pause()
        assert [store.item(sid, r)["status"] for r in (q1, q2, q3, t)] == ["closed", "open", "closed", "running"]
        assert items.marked == set()
        await pilot.press("f")
        await pilot.pause()
        items.set_marks({f"{sid}|{q1}", f"{sid}|{q3}"})
        await pilot.pause()
        await pilot.press("x")
        await pilot.pause()
    assert [store.item(sid, r)["status"] for r in (q1, q3)] == ["answered", "answered"], "all closed: X reopens"


@pytest.mark.anyio
async def test_the_stats_pane_follows_the_session_in_context(store, sid, tmp_path, monkeypatch):
    import time
    folder = tmp_path / "projects/-home-u-repo"
    folder.mkdir(parents=True)
    now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    rec = {"type": "assistant", "timestamp": now, "message": {"id": "m1", "model": "claude-opus-5-5", "content": [],
           "usage": {"input_tokens": 2, "cache_creation_input_tokens": 829, "cache_read_input_tokens": 82_000,
                     "cache_creation": {"ephemeral_1h_input_tokens": 829}}}}
    folder.joinpath(f"{sid}.jsonl").write_text(json.dumps(rec) + "\n")
    monkeypatch.setattr(transcript, "PROJECTS", tmp_path / "projects")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert "no sessions running" in app.query_one("#stats").render().plain, "nothing in context and none running"
        app.follow(sid)
        app.refresh_data()
        await app.workers.wait_for_complete()
        await pilot.pause()
        shown = app.query_one("#stats").render().plain
        assert "demo · Opus 5.5" in shown and "83k/1M" in shown and "1h · warm" in shown
        assert "context assembly" in shown, "half the column is room enough for the chart"
        store.end(sid)
        app.refresh_data()
        await pilot.pause()
        assert sid not in app.query_one("#stats").followers, "an ended session's follower goes"


@pytest.mark.anyio
async def test_the_stats_pane_never_reads_on_the_ui_thread(store, sid, monkeypatch):
    import threading
    from claude_wheelhouse import stats
    ui = threading.get_ident()
    release, threads = threading.Event(), []

    def slow_read(self, now=None):
        threads.append(threading.get_ident())
        release.wait(5)
        self.ready = True
        return True
    monkeypatch.setattr(stats.UsageFollower, "read", slow_read)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.follow(sid)
        app.refresh_data()
        await pilot.pause()
        assert "reading demo's transcript…" in app.query_one("#stats").render().plain, "a first read in flight shows as such"
        release.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "reading" not in app.query_one("#stats").render().plain, "and is replaced once it lands"
    assert threads and ui not in threads, "reads ran on worker threads"


@pytest.mark.anyio
async def test_a_failed_transcript_read_is_shown_not_fatal(store, sid, monkeypatch):
    """A worker's read raising (the transcript deleted between stat and open, say) leaves
    the app running and says so in the pane."""
    from claude_wheelhouse import stats

    def broken(self, now=None):
        raise FileNotFoundError("transcript gone")
    monkeypatch.setattr(stats.UsageFollower, "read", broken)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.follow(sid)
        app.refresh_data()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.is_running, "the app carries on"
        shown = " ".join(line[1:-1].strip() for line in app.query_one("#stats").render().plain.split("\n"))   # wrapped in the panel
        assert "couldn't read the transcript: transcript gone" in shown


@pytest.mark.anyio
@pytest.mark.parametrize("focus_answer, moves, desc", [
    (False, True, "the shimmer runs while the answer box is idle"),
    (True, False, "the shimmer rests while you type"),
])
async def test_the_stats_shimmer_rests_while_typing(store, sid, focus_answer, moves, desc):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        if focus_answer:
            app.answer.focus()
            await pilot.pause()
        before = app.query_one("#stats").frame
        for _ in range(4):
            app.animate()
        assert (app.query_one("#stats").frame != before) == moves, desc


@pytest.mark.anyio
async def test_the_stats_pane_shows_account_usage(store, sid):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        app.query_one("#stats").usage.limits = {"session": (23.0, None), "weekly_all": (5.0, None)}
        app.refresh_data()
        await pilot.pause()
        shown = app.query_one("#stats").render().plain
        assert "session" in shown and "23%" in shown and "weekly" in shown, "usage shows with no session in context"


@pytest.mark.anyio
@pytest.mark.parametrize("context, chars, desc", [
    (None, "", "no transcript yet: no bar"),
    (350_000, "▅", "350k: half way from 200k to 500k"),
    (90_000, "▂", "90k: most of the first step"),
])
async def test_the_session_list_shows_context_size(store, sid, tmp_path, monkeypatch, context, chars, desc):
    """Doug (#51): the context size in the session list, read as the stats pane reads it."""
    if context is not None:
        folder = tmp_path / "projects/-home-u-repo"
        folder.mkdir(parents=True)
        when = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - 60))
        folder.joinpath(f"{sid}.jsonl").write_text(json.dumps(
            {"type": "assistant", "timestamp": when, "message": {"id": "m1", "model": "claude-opus-5-5", "content": [],
             "usage": {"input_tokens": 10, "cache_read_input_tokens": context - 10, "output_tokens": 5}}}) + "\n")
    monkeypatch.setattr(transcript, "PROJECTS", tmp_path / "projects")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        app.refresh_data()
        await pilot.pause()
        cell = app.query_one("#session-list", DataTable).get_row_at(0)[2]
    assert str(cell) == chars, desc


def shown_buttons(app) -> set[str]:
    return {b.id for b in app.screen.query("SendBar Button") if b.display}


@pytest.mark.anyio
@pytest.mark.parametrize("press, status, decision, desc", [
    ("#allow", "allowed", "allow", "allow once"),
    ("#always", "allowed", "always", "allow, and keep the rule"),
    ("#deny", "denied", "deny", "deny"),
])
async def test_permission_buttons(store, tmp_path, press, status, decision, desc):
    sid = store.create_session(str(tmp_path), name="hosted", runner="sdk")
    ref = store.post_item(sid, "permission", "Bash: rm -rf build")
    app = WheelhouseApp(store)
    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        assert {"allow", "always", "deny"} <= shown_buttons(app), desc
        await pilot.click(press)
        await pilot.pause()
        assert not {"allow", "always", "deny"} & shown_buttons(app), f"{desc}: answered, the buttons go"
    item = store.item(sid, ref)
    assert item["status"] == status and json.loads(item["answer"])["decision"] == decision, desc


@pytest.mark.anyio
@pytest.mark.parametrize("runner_, live, shown, desc", [
    ("sdk", True, True, "a running hosted session"),
    ("sdk", False, False, "not while it isn't running"),
    ("tab", True, False, "a tab has its own terminal"),
])
async def test_host_buttons(store, tmp_path, monkeypatch, runner_, live, shown, desc):
    from claude_wheelhouse import liveness
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "live" if live else "dead")
    sid = store.create_session(str(tmp_path), name="hosted", runner=runner_)
    store.post_item(sid, "task", "work")
    store.set_activity(sid, "running Bash: make test")
    app = WheelhouseApp(store)
    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        assert ({"interrupt", "compact", "shell"} <= shown_buttons(app)) == shown, desc
        activity = str(app.screen.query_one("#activity", Label).content)
        assert (activity == "running Bash: make test") == shown, desc
        if shown:
            await pilot.click("#interrupt")
            await pilot.pause()
            assert store.session(sid)["host_command"] == "interrupt", desc


@pytest.mark.anyio
@pytest.mark.parametrize("tick, runner_, desc", [
    (False, "sdk", "runs in the wheelhouse by default"),
    (True, "tab", "the box opens a tab instead"),
])
async def test_new_session_runner(store, tmp_path, monkeypatch, tick, runner_, desc):
    launched = []
    monkeypatch.setattr(launch, "open_session", lambda s, i, watch=None: launched.append(s.session(i)["runner"]))
    monkeypatch.setenv("WHEELHOUSE_RUNNER", "sdk")
    app = WheelhouseApp(store)
    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.press("n")
        await pilot.pause()
        app.screen.query_one("#cwd", Input).value = str(tmp_path)
        if tick:
            app.screen.query_one("#tab", Checkbox).value = True
        await pilot.click("#launch")
        await pilot.pause()
    assert launched == [runner_], desc


@pytest.mark.parametrize("activity, busy, desc", [
    ("thinking", True, "working"),
    ("running Bash: ls", True, "running a tool"),
    ("idle", False, "idle"),
    ("idle · compacted 48k → 8k tokens", False, "idle after a compaction"),
    ("error: overloaded", False, "an errored session isn't busy"),
    ("stopped: Claude Code died", False, "stopped with a reason"),
    ("in a shell tab", False, "handed to a shell tab"),
    ("interrupted", False, "interrupted"),
])
def test_hosted_busy(activity, busy, desc):
    from types import SimpleNamespace
    s = {"id": "s1", "running": 0, "runner": "sdk", "activity": activity}
    app = SimpleNamespace(statuses={"s1": "live"})
    assert WheelhouseApp.busy(app, s) is busy, desc


@pytest.mark.anyio
@pytest.mark.parametrize("mode, sends_now, expected, desc", [
    (None, False, "Ctrl+Enter to queue", "a new session queues"),
    ("immediate", False, "Ctrl+Enter to send", "immediate mode sends"),
    (None, True, "Ctrl+Enter to send", "a session from before queued answers sends whatever its mode"),
])
async def test_the_hint_says_what_ctrl_enter_does(store, sid, mode, sends_now, expected, desc, monkeypatch):
    if mode:
        store.set_mode(sid, mode)
    if sends_now:
        monkeypatch.setattr(WheelhouseApp, "sends_now", lambda self, s: True)
    store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.items_table.move_cursor(row=0)
        await pilot.pause(1.2)
        assert str(app.screen.query_one(Hint).content).startswith(expected + " · "), desc
        app.items_table.focus()
        await pilot.press("enter")   # the item full screen has its own box and hint
        await pilot.pause()
        assert isinstance(app.screen, ThreadView)
        assert str(app.screen.query_one(Hint).content).startswith(expected + " · "), f"{desc}, in a thread"


@pytest.mark.anyio
async def test_the_hint_follows_a_mode_switched_in_a_thread_at_once(store, sid, monkeypatch):
    from textual.app import App
    every = App.set_interval   # no refresh tick: only the return itself can repaint the hint
    monkeypatch.setattr(App, "set_interval", lambda self, t, cb, **kw: None if t == 1.0 else every(self, t, cb, **kw))
    store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.items_table.move_cursor(row=0)
        app.items_table.focus()
        await pilot.press("enter", "ctrl+t", "escape")
        await pilot.pause()
        assert not isinstance(app.screen, ThreadView)
        assert str(app.screen.query_one(Hint).content).startswith("Ctrl+Enter to send · ")


@pytest.mark.anyio
@pytest.mark.parametrize("screen, desc", [
    ("inbox", "the inbox's send bar"),
    ("thread", "an item's, full screen"),
])
async def test_send_bar_buttons_sit_above_the_footer(store, sid, screen, desc):
    store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        if screen == "thread":
            app.items_table.move_cursor(row=0)
            app.items_table.focus()
            await pilot.press("enter")
            await pilot.pause()
        bar, footer = app.screen.query_one(SendBar), app.screen.query_one(Footer)
        for button in bar.query(Button):
            if button.display:
                assert button.region.height == 1 and button.region.y == bar.region.y, f"{desc}: {button.id}"
        assert bar.region.bottom <= footer.region.y, desc


@pytest.mark.parametrize("name, shown, desc", [
    ("src", True, "a directory"),
    (".git", False, "hidden directories are left out"),
    (".worktrees", True, "but for a repo's worktrees"),
    ("notes.md", False, "files are left out"),
])
def test_the_picker_shows_directories(tmp_path, name, shown, desc):
    path = tmp_path / name
    path.mkdir() if "." not in name[1:] else path.write_text("x")
    assert (path in list(Folders(tmp_path).filter_paths([path]))) == shown, desc


@pytest.mark.anyio
async def test_browse_fills_in_the_working_directory(store, tmp_path):
    (tmp_path / "repo").mkdir()
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.press("n")
        await pilot.pause()
        app.screen.query_one("#cwd", Input).value = str(tmp_path)
        await pilot.click("#browse")
        await pilot.pause(0.5)
        assert type(app.screen).__name__ == "PickDirectory"
        await pilot.press("down")   # from the root to repo
        await pilot.pause()
        await pilot.press("ctrl+j")
        await pilot.pause()
        assert type(app.screen).__name__ == "NewSession"
        assert app.screen.query_one("#cwd", Input).value == str(tmp_path / "repo")
        await pilot.click("#browse")
        await pilot.pause(0.5)
        await pilot.press("backspace")   # from repo up a level
        await pilot.pause()
        await pilot.press("ctrl+j")   # and take it
        await pilot.pause()
        assert app.screen.query_one("#cwd", Input).value == str(tmp_path)


@pytest.mark.anyio
@pytest.mark.parametrize("typed, keys, expected, desc", [
    ("Columbo check", ["enter"], "Columbo check", "Enter renames"),
    ("", ["enter"], "", "empty clears it: the session shows its directory"),
    ("Columbo check", ["escape"], "demo", "Esc leaves it"),
])
async def test_rename_a_session(store, sid, typed, keys, expected, desc):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("2")
        await pilot.pause()
        await pilot.click("#rename")
        await pilot.pause()
        box = app.screen.query_one("#new-name", Input)
        assert box.value == "demo", "it starts from the current name"
        box.value = typed
        await pilot.press(*keys)
        await pilot.pause()
    assert store.session(sid)["name"] == expected, desc


@pytest.mark.anyio
@pytest.mark.parametrize("typed, said, desc", [
    (None, [], "Enter on its name unchanged does nothing"),
    ("Columbo check", ["renamed to Columbo check"], "a new name renames"),
])
async def test_enter_in_rename_renames_only_to_a_new_name(store, sid, typed, said, desc):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("2")
        await pilot.pause()
        await pilot.click("#rename")
        await pilot.pause()
        if typed:
            app.screen.query_one("#new-name", Input).value = typed
        await pilot.press("enter")
        await pilot.pause()
        assert store.session(sid)["name"] == (typed or "demo"), desc
        assert [n.message for n in app._notifications] == said, desc


@pytest.mark.anyio
@pytest.mark.parametrize("ends, desc", [
    ("while the dialog is open", "renaming a session that ended while its dialog was open"),
    ("before the button", "pressing Rename on a session that has just ended"),
])
async def test_renaming_a_session_that_has_gone_says_so(store, sid, monkeypatch, ends, desc):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("2")
        await pilot.pause()
        if ends == "before the button":
            store.end(sid)
            monkeypatch.setattr(app, "current_session", lambda: sid)   # the row still showing it
        await pilot.click("#rename")
        await pilot.pause()
        if ends == "while the dialog is open":
            store.end(sid)
            app.screen.query_one("#new-name", Input).value = "Columbo check"
            await pilot.press("enter")
            await pilot.pause()
        assert app.is_running and store.session(sid) is None, desc
        assert [n.message for n in app._notifications] == ["session no longer exists"], desc


@pytest.mark.anyio
@pytest.mark.parametrize("element, colour, background, desc", [
    ("screen--selection", "#000000", "#ffffff", "a selection in the conversation or a thread"),
    ("text-area--selection", "#000000", "#ffffff", "a selection in an answer box"),
])
async def test_selections_read_black_on_white(store, sid, element, colour, background, desc):
    """Doug (#52): dark teal blocks hid the selected characters."""
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        owner = app.answer if element.startswith("text-area") else app.screen
        style = owner.get_component_rich_style(element)
    assert (style.color.get_truecolor().hex, style.bgcolor.get_truecolor().hex) == (colour, background), desc

