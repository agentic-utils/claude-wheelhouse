import pytest
from textual.widgets import DataTable, TextArea

from claude_wheelhouse import launch
from claude_wheelhouse.tui import WheelhouseApp, Confirm, ThreadView


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


@pytest.mark.anyio
async def test_ctrl_x_sends_one_answer_now(store, sid):
    q = store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.query_one("#items", DataTable).move_cursor(row=0)
        app.query_one("#answer", TextArea).focus()
        await pilot.press(*"now")
        await pilot.press("ctrl+x")
        await pilot.pause()
        assert app.query_one("#answer", TextArea).text == "", "sent, not cut"
    assert [m["body"] for m in store.pending(sid)] == ["now"]
    assert store.item(sid, q)["status"] == "answered"


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
