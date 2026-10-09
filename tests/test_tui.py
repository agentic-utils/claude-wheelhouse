import html
import itertools
import json
import re
import time
import random

import pytest
from rich.color import Color
from rich.text import Text
from textual import events
from textual.widgets import Button, Checkbox, DataTable, Footer, Input, Label, TextArea

from claude_wheelhouse import launch, stats, transcript
from claude_wheelhouse.store import PROTOCOL_VERSION
from claude_wheelhouse.splitter import Splitter
from claude_wheelhouse.store import mode
from claude_wheelhouse.tui import (MATRIX, NOTHING_SELECTED, PERMISSION_HINT, VOICE, WheelhouseApp, Choice, Confirm,
                                   Folders, Hint, PermissionButtons, SendBar, ThreadView, Transcript, hint, render)


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
        app.session_list.focus()
        await pilot.press("S")   # in the footer now, not a button (#62)
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


TICKS = itertools.count()


async def refresh(pilot, sid: str) -> None:
    """A refresh tick, run rather than waited for (review 9), once what's pending has landed.
    The session's new name shows in the list only once it has run."""
    await pilot.pause()
    name = f"tick {next(TICKS)}"
    pilot.app.store.rename(sid, name)
    pilot.app.refresh_data()
    await pilot.pause()
    assert str(pilot.app.session_list.get_row(sid)[1]) == name, "the refresh ran"


@pytest.fixture
def live(monkeypatch):
    """Every session running: Rename, Relaunch and Park show only on a live one (#62)."""
    from claude_wheelhouse import liveness
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "live")


def session_buttons(app) -> dict[str, tuple[str, bool]]:
    return {b.id: (str(b.label), b.disabled) for b in app.query("#conversation-buttons Button") if b.display}


@pytest.mark.anyio
async def test_send_bar_and_session_buttons_follow_the_queues(store, sid, tmp_path):
    other = store.create_session(str(tmp_path), name="other")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        bar = lambda: {b.id: (str(b.label), b.disabled) for b in app.screen.query("SendBar Button") if b.display}
        assert bar() == {"send-all": ("Send all (0)", True)}, "the inbox's bar: only Send all (D20)"
        assert session_buttons(app) == {"mode": ("Mode: Queued", False), "send": ("Send (0)", True)}, \
            "the highlighted session's, nothing queued"
        store.queue(sid, "a")
        store.queue(other, "b")
        store.queue(other, "c")
        app.refresh_data()
        await pilot.pause()
        assert (bar(), session_buttons(app)["send"]) == ({"send-all": ("Send all (3)", False)}, ("Send (1)", False))
        app.session_list.move_cursor(row=1)
        await pilot.pause()
        assert session_buttons(app)["send"] == ("Send (2)", False), "the highlight moved: the other's queue"
        assert all(not b.can_focus for b in app.screen.query("SendBar Button, #sessions-pane Button")), \
            "clicks leave focus alone"
        await pilot.click("#send-all")
        await pilot.pause()
        assert bar()["send-all"] == ("Send all (0)", True)
    assert store.drafts() == []
    assert [m["body"] for m in store.pending(sid)] == ["a"]
    assert [m["body"] for m in store.pending(other)] == ["b", "c"]


@pytest.mark.anyio
@pytest.mark.parametrize("nav, desc", [
    ("item", "the person highlights another session's item: the list's highlight goes with it"),
    ("list", "the person highlights another session in the list: it becomes the context"),
    ("tutorial", "make tutorial's session, created last, its Q1 highlighted (review 8: Send sent another's)"),
])
@pytest.mark.parametrize("how, acts", [
    ("#send", "send"),
    ("ctrl+s", "send"),
    ("#mode", "mode"),
    ("ctrl+t", "mode"),
])
async def test_one_current_session_for_the_buttons_and_keys(store, sid, tmp_path, monkeypatch, nav, desc, how, acts):
    """D20, as revised in review 8: the session list's highlight is the current session, so
    the buttons under it, Ctrl+S and Ctrl+T, the hint and the activity line all act on or
    describe the same one, however the person got there."""
    from claude_wheelhouse import tutorial
    monkeypatch.setattr(tutorial, "tutorial_dir", lambda store: tmp_path / "tut")
    mine = store.post_item(sid, "question", "which db?")
    store.queue(sid, "a", mine)
    target = tutorial.prepare(store) if nav == "tutorial" else store.create_session(str(tmp_path), name="other")
    q = store.post_item(target, "question", "Q1?")
    store.queue(target, "b", q)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        # by key: the inbox sorts newest first to the second, so either question may lead (review 9)
        app.items_table.move_cursor(row=app.items_table.get_row_index(f"{sid}|{mine}"))
        await pilot.pause()
        assert app.current_session() == sid, f"{desc}: demo's item highlighted, so demo is current"
        if nav == "list":
            app.session_list.move_cursor(row=app.session_list.get_row_index(target))
        else:
            app.items_table.move_cursor(row=app.items_table.get_row_index(f"{target}|{q}"))
        await pilot.pause()
        assert app.current_session() == app.bar_session(app.screen) == target, desc
        await (pilot.click(how) if how.startswith("#") else pilot.press(how))
        await pilot.pause()
    got = {i for i in (sid, target) if (store.pending(i) if acts == "send" else store.session(i)["send_mode"])}
    assert got == {target}, f"{desc}: {how} acts on the current session"


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
@pytest.mark.parametrize("synopsis, brief, shown, desc", [
    ("Building the thread view for issue 11.", "do the thing", "Building the thread view for issue 11.",
     "the session's own words"),
    ("", "do the thing", "Brief: do the thing", "its brief until the session sets a synopsis"),
    ("", "", "No synopsis yet", "a placeholder with neither"),
])
async def test_the_session_description_box(store, sid, tmp_path, synopsis, brief, shown, desc):
    """#58: the old Sessions tab's row and synopsis, under the session list."""
    store.set_synopsis(sid, synopsis)
    store.db.execute("UPDATE sessions SET brief = ?", (brief,))
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.query_one("#session-list", DataTable).move_cursor(row=0)
        await pilot.pause()
        lines = app._info_text.splitlines()
        assert lines[:4] == ["demo", "dead · tab · queued", "#7", str(tmp_path)], f"{desc}: name, status, ticket, cwd"
        assert shown in app._info_text, desc


@pytest.mark.anyio
async def test_end_names_the_queued_answers_it_discards(store, sid):
    store.queue(sid, "a", "Q1")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        await pilot.click("#end")
        await pilot.pause()
        assert "1 queued answer(s) will be discarded" in app.screen.prompt


@pytest.mark.anyio
@pytest.mark.parametrize("version, label, bar, queued, desc", [
    (PROTOCOL_VERSION, "live", "Mode: Queued", 1, "a session on current code queues the answer"),
    (PROTOCOL_VERSION - 1, "live · needs relaunch", "Mode: Queued", 1,
     "one on older code that holds queued answers still queues: a relaunch only brings the new code"),
    (1, "live · needs relaunch", "Can't queue: relaunch", 0,
     "one from before queued answers would deliver one at once, so it's sent now"),
    (None, "live · needs relaunch", "Can't queue: relaunch", 0, "as is one that never stamped its version"),
])
async def test_a_session_on_older_code_cannot_queue(store, sid, monkeypatch, version, label, bar, queued, desc):
    from claude_wheelhouse import liveness
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "live")
    store.db.execute("UPDATE sessions SET code_version = ?", (version,))
    q = store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert app._info_text.splitlines()[1].split(" · tab")[0] == label, desc
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
async def test_one_click_on_a_session_follows_it(store, sid, tmp_path, monkeypatch):
    """Once (review 9): the click's highlight and its selection don't each follow it."""
    other = store.create_session(str(tmp_path), name="other")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        follows = []
        follow = app.follow
        monkeypatch.setattr(app, "follow", lambda s: (follows.append(s), follow(s)))
        table = app.query_one("#session-list", DataTable)
        row = [table.coordinate_to_cell_key((i, 0)).row_key.value for i in range(table.row_count)].index(other)
        await pilot.click("#session-list", offset=(4, row + 1))   # below the header row
        await pilot.pause()
        assert app.viewing == other and "other · conversation" in app._detail_text
        assert follows == [other]


def assert_one_current(app, desc):
    """D22: one current session, the session list's highlight, and everything that acts on
    or describes a session agrees on it: the buttons, Ctrl+S, Ctrl+T and the hint
    (bar_session), the modules (focus_sid), the selection and the answer box's target, whose
    session's mode Ctrl+Enter answers by, and the session info. Each may be None, where
    nothing is in context (an empty inbox selects nothing), but never another session. And
    what shows agrees: the items' cursor is on the selection, the hint and the permission
    buttons are for the item in context, the conversation buttons for the current session."""
    current = app.current_session()
    thread = isinstance(app.screen, ThreadView)
    box_target = (app.screen.sid, app.screen.ref) if thread else app.box_target   # a thread's box answers it
    said = {"bar": app.bar_session(app.screen), "focus_sid": app.focus_sid(),
            "selected": app.selected and app.selected[0], "box_target": box_target and box_target[0]}
    assert {k: v for k, v in said.items() if v not in (None, current)} == {}, f"{desc}: current {current}, {said}"
    assert box_target == app.selected, f"{desc}: the box answers what the pane shows"
    info = app.display_name(app.row(current)) if current else "Select a session to see its description."
    assert app._info_text.split("\n")[0] == info, f"{desc}: the session info"
    table = app.items_table
    if app.selected:
        under = table.coordinate_to_cell_key((table.cursor_row, 0)).row_key.value
        assert under == f"{app.selected[0]}|{app.selected[1] or ''}", f"{desc}: the items' cursor on {under}"
    in_context = box_target
    item = in_context and in_context[1] and app.store.item(*in_context)
    asking = bool(item) and item["kind"] == "permission" and item["status"] == "open"
    s = app.row(app.bar_session(app.screen)) if app.bar_session(app.screen) else None
    sends = "send" if s and (mode(s) == "immediate" or app.sends_now(s)) else "queue"
    assert [h.base for h in app.screen.query(Hint)] == [PERMISSION_HINT if asking else hint(sends)], f"{desc}: the hint"
    assert [p.display for p in app.screen.query(PermissionButtons)] == [asking], f"{desc}: the permission buttons"
    if not thread:
        buttons = [(b.id, str(b.label), b.disabled, b.display) for b in app.conversation_buttons.query(Button)]
        expect = app.controls(next((x for x in app.sessions if x["id"] == current), None))
        assert buttons == expect, f"{desc}: the conversation buttons"
        buttons = [(b.id, str(b.label), b.disabled, b.display) for b in app.session_buttons.query(Button)]
        expect = app.lifecycle_buttons(next((x for x in app.sessions if x["id"] == current), None))
        assert buttons == expect, f"{desc}: the lifecycle buttons, only those that apply (#62)"


LIVE: set[str] = set()   # the sessions the tests below say are running; the rest are dead


def some_live(monkeypatch):
    from claude_wheelhouse import liveness
    LIVE.clear()
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "live" if s["id"] in LIVE else "dead")


async def park_live(app, pilot, sid):
    """Park shows on a running session (#62): it's asked, then parks itself, as its
    park_session tool does."""
    LIVE.add(sid)
    app.refresh_data()
    await pilot.pause()
    await pilot.click("#park")
    await pilot.pause()
    await pilot.press("y")
    await pilot.pause()
    app.store.set_parked(sid, True)
    LIVE.discard(sid)   # and its host goes: Unpark still shows, on a parked dead session
    app.refresh_data()


async def step(app, pilot, how, sid, other):
    """One thing the person (or, for ended elsewhere and refresh, the world) does."""
    if how == "park":
        await park_live(app, pilot, sid)
    elif how in ("unpark", "end"):
        await pilot.click("#park" if how != "end" else "#end")
        await pilot.pause()
        if how != "unpark":
            await pilot.press("y")
    elif how == "delete":
        app.items_table.focus()
        await pilot.press("delete")
    elif how in ("up", "down"):
        app.session_list.focus()
        await pilot.press(how)
    elif how == "click":
        row = [r.key.value for r in app.session_list.ordered_rows].index(other)
        await pilot.click("#session-list", offset=(4, row + 1))   # below the header row
        await pilot.pause()
        if isinstance(app.screen, Confirm):   # the offer to relaunch a dead session
            await pilot.press("n")
    elif how == "esc":
        app.items_table.focus()
        await pilot.press("escape")
    elif how == "ended elsewhere":
        app.store.end(sid)
        app.refresh_data()
    elif how == "refresh":   # a new item, above the selected one
        app.store.post_item(other, "question", "B's second")
        app.refresh_data()
    elif how in ("question", "permission"):   # the person highlights one of A's items
        key = next(r.key.value for r in app.items_table.ordered_rows
                   if r.key.value.startswith(f"{sid}|") and not r.key.value.endswith("|") and app.store.item(*r.key.value.split("|"))["kind"] == how)
        app.items_table.move_cursor(row=app.items_table.get_row_index(key))
    elif how == "enter":   # on A's row in the session list
        app.session_list.move_cursor(row=app.session_list.get_row_index(sid))
        await pilot.pause()
        app.session_list.focus()
        await pilot.press("enter")
        await pilot.pause()
        if isinstance(app.screen, Confirm):   # the offer to relaunch a dead session
            await pilot.press("n")
    elif how == "mark delete":   # A's and B's questions marked, closed together
        app.items_table.set_marks({r.key.value for r in app.items_table.ordered_rows
                                   if r.key.value.split("|")[1].startswith("Q")})
        await pilot.pause()
        app.items_table.focus()
        await pilot.press("delete")
    elif how == "thread":   # the highlighted item's thread, full screen
        app.items_table.focus()
        await pilot.press("enter")
        await pilot.pause()
        app.screen.focus_next()   # off its box, so Esc closes it
    elif how == "back":
        await pilot.press("escape")
    elif how == "burst":   # typed for A's item, then at once: Down, Ctrl+Enter, Ctrl+S
        app.answer.text = "burst"
        app.session_list.focus()
        await pilot.press("down", "ctrl+j", "ctrl+s")
    await pilot.pause()


@pytest.mark.anyio
@pytest.mark.parametrize("filtered", [False, True])
@pytest.mark.parametrize("steps, unfiltered_current, filtered_current, desc", [
    (["park", "unpark"], "A", "A", "Park follows the session (review 10): it stays current, Unpark one press away"),
    (["park", "up"], "B", "B", "then arrowing onto the other session follows it (review 10, bug 2)"),
    (["delete"], "B", "A", "closing A's only item: unfiltered, the next item's session is current, list and all"),
    (["end"], "B", "B", "End"),
    (["ended elsewhere"], "B", "B", "the current session ending elsewhere"),
    (["down"], "B", "B", "an arrow in the session list"),
    (["click"], "B", "B", "a click in the session list"),
    (["esc"], None, None, "Esc: whichever item is then highlighted"),
    (["refresh"], "A", "A", "a refresh bringing a new item"),
    (["permission", "park"], "A", "A", "Park with A's permission item highlighted (review 11, bug 1)"),
    (["park", "permission", "unpark"], "A", "A", "Unpark with A's permission item highlighted"),
    (["permission", "enter"], "A", "A", "Enter on A's row with its permission item highlighted"),
    (["question", "enter"], "A", "A", "Enter on A's row with its question highlighted"),
    (["mark delete"], "A", "A", "closing A's and B's questions together"),
    (["thread", "back"], "A", "A", "a thread open, then Esc"),
    (["permission", "thread", "back"], "A", "A", "a permission item's thread open, then Esc"),
    (["burst"], "B", "B", "Down, Ctrl+Enter and Ctrl+S at once: all on B (review 11, P1)"),
])
async def test_one_current_session_whatever_happens(store, sid, tmp_path, monkeypatch, steps, unfiltered_current,
                                                    filtered_current, desc, filtered):
    """D22, after review 10: the session list's highlight is the current session, and the
    selection, the box, the hint, the modules and the info box never describe another."""
    some_live(monkeypatch)   # all dead: End deletes at once, on a yes
    store.set_mode(sid, "immediate")
    mine = store.post_item(sid, "question", "A's")
    if "permission" in steps:
        store.post_item(sid, "permission", "Bash: rm -rf build")
    other = store.create_session(str(tmp_path), name="other")
    store.set_mode(other, "queued")
    store.post_item(other, "question", "B's")
    store.queue(sid, "A's draft")
    store.queue(other, "B's draft")
    expect = {"A": sid, "B": other, None: None}[filtered_current if filtered else unfiltered_current]
    desc = f"{desc}, {'following A' if filtered else 'the inbox unfiltered'}"
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        if filtered:
            app.follow(sid)
            await pilot.pause()
        app.items_table.move_cursor(row=app.items_table.get_row_index(f"{sid}|{mine}"))
        await pilot.pause()
        assert_one_current(app, f"{desc}: at the start")
        for how in steps:
            await step(app, pilot, how, sid, other)
            assert_one_current(app, f"{desc}: after {how}")
        assert expect is None or app.current_session() == expect, desc
        if steps[-1] == "unpark":
            assert not store.session(sid)["parked"] and app.filter_sid == sid, desc
        if steps == ["burst"]:   # the text stays with A's item, unsent; B's queue went, not A's
            drafts = [(m["session_id"], m["body"]) for m in store.drafts()]
            assert drafts == [(sid, "A's draft")] and not store._all("SELECT 1 FROM messages WHERE body = 'burst'"), \
                f"{desc}: {drafts}"


@pytest.mark.anyio
@pytest.mark.parametrize("filtered, desc", [
    (True, "the followed session (review 10)"),
    (False, "the session whose item was selected"),
])
async def test_a_session_ending_elsewhere_with_nothing_left_clears_the_pane(store, sid, tmp_path, filtered, desc):
    """Its conversation or item isn't left in the pane, nor the answer box aimed at it."""
    store.create_session(str(tmp_path), name="other")
    q = store.post_item(sid, "question", "A's")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        if filtered:
            app.follow(sid)
        else:
            app.items_table.move_cursor(row=app.items_table.get_row_index(f"{sid}|{q}"))
        await pilot.pause()
        app.answer.text = "half typed"
        store.end(sid)
        app.refresh_data()
        await pilot.pause()
        assert (app.selected, app.box_target, app._detail_text) == (None, None, NOTHING_SELECTED), desc
        assert app.answer.text == "", desc
        assert_one_current(app, desc)


@pytest.mark.anyio
@pytest.mark.parametrize("item_arrives, desc", [
    (False, "a refresh that changes no rows"),
    (True, "a refresh that rebuilds the list"),
])
async def test_a_refresh_before_the_persons_highlight_lands(store, sid, item_arrives, desc):
    """The items' cursor follows the selection by key on every paint (review 11, bug 1), but
    not over an arrow whose highlight is still on its way to pick_item: it isn't lost."""
    qs = [store.post_item(sid, "question", t) for t in ("first", "second")]
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        table = app.items_table
        table.move_cursor(row=table.get_row_index(f"{sid}|{qs[0]}"))
        await pilot.pause()
        with table.prevent(DataTable.RowHighlighted):   # the person's arrow, its highlight not yet handled
            table.move_cursor(row=table.get_row_index(f"{sid}|{qs[1]}"))
        if item_arrives:
            store.post_item(sid, "task", "new", status="running")
        app.refresh_data()
        assert table.cursor_key() == f"{sid}|{qs[1]}", f"{desc}: the arrow stands"
        table.post_message(DataTable.RowHighlighted(table, table.cursor_row,
                                                    table.coordinate_to_cell_key((table.cursor_row, 0)).row_key))
        await pilot.pause()
        assert app.selected == (sid, qs[1]), f"{desc}: and is followed when its highlight lands"
        assert_one_current(app, desc)


@pytest.mark.anyio
async def test_enter_on_the_followed_session_goes_to_the_newest_turn(store, sid, tmp_path, monkeypatch):
    """Review 10: Enter or a click on the session already followed scrolls as following it does."""
    folder = tmp_path / "projects/-home-u-repo"
    folder.mkdir(parents=True)
    recs = [{"type": "user", "timestamp": "2026-10-07T21:30:00Z", "message": {"content": f"turn {i}"}}
            for i in range(60)]
    folder.joinpath(f"{sid}.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    monkeypatch.setattr(transcript, "PROJECTS", tmp_path / "projects")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.follow(sid)
        await pilot.pause()
        scroll = app.detail_scroll
        scroll.scroll_home(animate=False)
        await pilot.pause()
        assert scroll.scroll_y == 0 < scroll.max_scroll_y, "scrolled up to read"
        app.session_list.focus()
        await pilot.press("enter", "n")   # it isn't running: decline the offer to relaunch it
        await pilot.pause()
        assert scroll.scroll_y == scroll.max_scroll_y


@pytest.mark.anyio
async def test_a_followed_session_that_ends_elsewhere_clears_the_filter(store, sid, tmp_path):
    """Review 9: as Esc, rather than a ghost Conversation row for a session that's gone."""
    other = store.create_session(str(tmp_path), name="other")
    store.post_item(other, "question", "B's")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.follow(sid)
        await pilot.pause()
        store.end(sid)
        app.refresh_data()
        await pilot.pause()
        assert app.filter_sid is None
        assert [r.key.value.split("|")[0] for r in app.items_table.ordered_rows] == [other], "the inbox, all of it"


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
    await pilot.click("#restore")


async def select_in_inbox(pilot):
    pilot.app.query_one("#session-list", DataTable).focus()
    await pilot.press("enter", "y")


@pytest.mark.parametrize("how, desc", [
    (restore_button, "the Restore button"),
    (select_in_inbox, "selecting the dead session in the inbox, then y"),
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
        await refresh(pilot, sid)   # a refresh tick between keys
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
@pytest.mark.parametrize("key, kind, status, finished, focus_box, expected, desc", [
    ("delete", "question", "open", False, False, "closed", "Delete closes a question"),
    ("backspace", "question", "open", False, False, "closed", "and so does Backspace"),
    ("delete", "question", "answered", False, False, "closed", "an answered one too"),
    ("delete", "question", "closed", True, False, "answered", "Delete on a closed question (shown with f) reopens it"),
    ("backspace", "question", "closed", True, False, "answered", "as does Backspace"),
    ("delete", "task", "running", False, False, "running", "a task's status is the session's: Delete leaves it"),
    ("x", "question", "open", False, False, "open", "x no longer closes (#64)"),
    ("backspace", "question", "open", False, True, "open", "in the answer box Backspace edits the text"),
    ("delete", "question", "open", False, True, "open", "and so does Delete"),
])
async def test_delete_closes_and_reopens_questions(store, sid, key, kind, status, finished, focus_box, expected, desc):
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
        await pilot.press(key)
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
        assert str(sessions.get_row_at(0)[4]) == "1", "the session list counts unseen decisions"
        assert store.item(sid, d)["status"] == "unseen", "not seen until the person looks"
        items.focus()
        items.move_cursor(row=1)
        await refresh(pilot, sid)
        assert store.item(sid, d)["status"] == "seen", "viewing it marks it seen"
        assert str(sessions.get_row_at(0)[4]) == ""
        items.move_cursor(row=0)
        await refresh(pilot, sid)
        assert [items.get_row_at(i)[1] for i in range(items.row_count)] == [q, d], "moving on leaves it there"
        items.move_cursor(row=1)
        await pilot.press("delete")
        await pilot.pause()
        assert store.item(sid, d)["status"] == "closed", "Delete closes it"
        assert [items.get_row_at(i)[1] for i in range(items.row_count)] == [q], "then it's finished"


@pytest.mark.anyio
@pytest.mark.parametrize("start, finished, expected, desc", [
    ("unseen", False, "closed", "Delete closes an unseen decision"),
    ("seen", False, "closed", "and a seen one"),
    ("closed", True, "seen", "Delete on a closed decision (shown with f) reopens it as seen"),
])
async def test_delete_closes_and_reopens_decisions(store, sid, start, finished, expected, desc):
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
        await pilot.press("delete")
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
        d = store.post_item(sid, "question", "d")   # a rebuild: the marks are by key
        await refresh(pilot, sid)
        assert f"{sid}|{d}" in items.rows, "rebuilt"
        assert sorted(k.split("|")[1] for k in items.marked) == [q1, q3, t], "marks survive a refresh"
        await pilot.press("backspace")
        await pilot.pause()
        assert [store.item(sid, r)["status"] for r in (q1, q2, q3, t)] == ["closed", "open", "closed", "running"]
        assert items.marked == set()
        await pilot.press("f")
        await pilot.pause()
        items.set_marks({f"{sid}|{q1}", f"{sid}|{q3}"})
        await pilot.pause()
        await pilot.press("delete")
        await pilot.pause()
    assert [store.item(sid, r)["status"] for r in (q1, q3)] == ["answered", "answered"], "all closed: Delete reopens"


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
@pytest.mark.parametrize("context, compact, runner, chars, desc", [
    (None, False, "tab", "", "no transcript yet: no bar"),
    (350_000, False, "tab", "▅", "350k: half way from 200k to 500k"),
    (90_000, False, "tab", "▂", "90k: most of the first step"),
    (350_000, True, "sdk", "▂", "compacted since the last response: the host's 90k count stands in (#54)"),
    (350_000, True, "tab", "▅", "a tab session has no host count: the transcript's size until the next response"),
    (350_000, False, "sdk", "▅", "no compaction since the last response: the transcript's size is exact"),
])
async def test_the_session_list_shows_context_size(store, sid, tmp_path, monkeypatch, context, compact, runner,
                                                   chars, desc):
    """Doug (#51): the context size in the session list, read as the stats pane reads it."""
    if context is not None:
        folder = tmp_path / "projects/-home-u-repo"
        folder.mkdir(parents=True)
        def when(ago):
            return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - ago))
        recs = [{"type": "assistant", "timestamp": when(60), "message": {"id": "m1", "model": "claude-opus-5-5",
                 "content": [], "usage": {"input_tokens": 10, "cache_read_input_tokens": context - 10,
                                          "output_tokens": 5}}}]
        if compact:
            recs.append({"type": "system", "subtype": "compact_boundary", "timestamp": when(30),
                         "compactMetadata": {"trigger": "manual", "preTokens": context, "postTokens": 40_000}})
        folder.joinpath(f"{sid}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
    store.set_runner(sid, runner)
    store.set_context(sid, 90_000, 1_000_000)
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
    return {b.id for b in app.screen.query("SendBar Button, PermissionButtons Button, #conversation-buttons Button")
            if b.display and b.parent.display}


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
@pytest.mark.parametrize("mode_, how, desc", [
    ("queued", "ctrl+enter", "Ctrl+Enter in Queued mode: denied at once, never queued"),
    ("immediate", "ctrl+enter", "Ctrl+Enter in Immediate mode: denied the same way"),
    ("queued", "#deny", "Deny takes what's typed as the reason"),
])
async def test_a_message_on_a_permission_denies_it_at_once(store, tmp_path, mode_, how, desc):
    """Doug (#53): permissions are answered with buttons and don't need to be queued; a
    typed message stays as an optional reason to deny."""
    sid = store.create_session(str(tmp_path), name="hosted", runner="sdk")
    store.set_mode(sid, mode_)
    ref = store.post_item(sid, "permission", "Bash: rm -rf build")
    app = WheelhouseApp(store)
    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        assert app.selected == (sid, ref)
        assert "Ctrl+Enter denies" in str(app.query_one(Hint).render()), f"{desc}: the hint says so"
        app.answer.text = "use make clean instead"
        await (pilot.press(how) if how.startswith("ctrl") else pilot.click(how))
        await pilot.pause()
        left = app.answer.text
    item = store.item(sid, ref)
    assert item["status"] == "denied", desc
    assert json.loads(item["answer"])["message"] == "use make clean instead", desc
    assert store.drafts(sid) == [] and store.pending(sid) == [], f"{desc}: nothing queued or sent as a turn"
    assert left == "", f"{desc}: the box is cleared"


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
        await refresh(pilot, sid)
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
async def test_rename_a_session(store, sid, live, typed, keys, expected, desc):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
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
async def test_enter_in_rename_renames_only_to_a_new_name(store, sid, live, typed, said, desc):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
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
async def test_renaming_a_session_that_has_gone_says_so(store, sid, live, monkeypatch, ends, desc):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
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


async def select_in_the_pane(app, pilot):
    app.query_one("#detail", Transcript).focus()
    await pilot.press("ctrl+a")


async def select_in_the_box(app, pilot):
    app.answer.focus()
    app.answer.text = "typed words"
    await pilot.press("ctrl+a")


async def nothing_selected(app, pilot):
    app.answer.text = "typed "
    app.answer.move_cursor(app.answer.document.end)


async def right_click(app, selector: str) -> None:
    """A right-click as the terminal driver delivers it, through the app's own event
    handling: Pilot's clicks skip that, going to the screen."""
    from textual import events
    from textual.pilot import _get_mouse_message_arguments
    args = _get_mouse_message_arguments(app.screen.query_one(selector), (3, 2), button=3)
    for cls in (events.MouseDown, events.MouseUp):
        await app.on_event(cls(**args))


@pytest.mark.anyio
@pytest.mark.parametrize("select, system, copied, box, desc", [
    (select_in_the_pane, "from windows", "Q1 · which db?", None, "a selection in the pane: copied"),
    (select_in_the_box, "from windows", "typed words", "typed words", "a selection in the box: copied, and kept"),
    (nothing_selected, "from windows", None, "typed from windows", "nothing selected: the clipboard pasted in the box"),
    (nothing_selected, None, None, "typed earlier copy", "no system clipboard: the wheelhouse's own last copy"),
])
async def test_right_click_copies_or_pastes(store, sid, monkeypatch, select, system, copied, box, desc):
    """Doug (#52): right-click as in a terminal, copying a selection, else pasting."""
    from claude_wheelhouse import tui
    store.post_item(sid, "question", "which db?", "Postgres or SQLite for the cache?")
    monkeypatch.setattr(tui, "system_clipboard", lambda: system)
    app = WheelhouseApp(store)
    got = []
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.query_one("#items", DataTable).move_cursor(row=0)
        await pilot.pause()
        app._clipboard = "earlier copy"
        app.copy_to_clipboard = got.append
        await select(app, pilot)
        await pilot.pause()
        await right_click(app, "#answer")
        await app.workers.wait_for_complete()
        await pilot.pause()
        text = app.answer.text
    assert (got[0][:len(copied)] if got else None) == copied, desc
    if box is not None:
        assert text == box, desc


@pytest.mark.parametrize("size, virtual, window, position, vertical, glyphs, desc", [
    (10, 100, 30, 0, True, "█⣿█│││││││", "at the top: a three-cell thumb, caps and a knurl, then the track"),
    (10, 100, 50, 50, True, "│││││█⣿⣿⣿█", "at the bottom: a longer thumb knurls its middle"),
    (8, 80, 20, 30, False, "──█⣿█───", "horizontal: the track is a rule"),
    (6, 20, 20, 0, True, "││││││", "nothing to scroll: a bare track"),
])
def test_the_knurled_scrollbar(size, virtual, window, position, vertical, glyphs, desc):
    """Doug's pick (#55): design E inverted, dark Braille grooves on a solid teal thumb."""
    from claude_wheelhouse.knurl import KnurlRender
    segs = [s for s in KnurlRender.render_bar(size, virtual, window, position, vertical=vertical,
                                              back_color=Color.parse("#000000"), bar_color=Color.parse("#05d9e8")).segments
            if s.text.strip("\n")]
    assert "".join(s.text for s in segs) == glyphs, desc
    first = glyphs.find("█")
    for i, s in enumerate(segs):
        thumb = s.text in "█⣿"
        assert s.style.meta["@mouse.down"] == ("grab" if thumb else "scroll_up" if first < 0 or i < first
                                               else "scroll_down"), f"{desc}: cell {i} grabs or pages"
        inverted = s.text == "⣿"
        assert (s.style.bgcolor.name if inverted else s.style.color.name) == ("#05d9e8" if thumb else "#014b51"), desc
        assert (s.style.color if inverted else s.style.bgcolor).name == "#000000", f"{desc}: on black, never a white selection"


@pytest.mark.anyio
async def test_every_scrollbar_is_knurled_and_one_cell(store, sid):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        table = app.query_one("#session-list", DataTable)
        assert type(table.vertical_scrollbar).renderer.__name__ == "KnurlRender"
        assert (table.styles.scrollbar_size_vertical, table.styles.scrollbar_size_horizontal) == (1, 1)
        assert table.styles.scrollbar_color.hex == "#05D9E8"


@pytest.mark.anyio
@pytest.mark.parametrize("before, after, desc", [
    (lambda: stats.context_bar(140_000), lambda: stats.context_bar(160_000), "a context bar changing grade in the same glyph (#51)"),
    (lambda: Text("open", style="bold"), lambda: Text("open", style="bold on #3a1060"), "a cell marked, its text unchanged"),
])
async def test_fill_repaints_a_cell_that_only_changed_colour(before, after, desc):
    """Rich's Text equality ignores the base style: fill must not."""
    from textual.app import App
    from claude_wheelhouse.tui import fill
    app = App()
    async with app.run_test() as pilot:
        table = DataTable()
        await app.screen.mount(table)
        table.add_columns("name", "cell")
        fill(table, [("s1", ("demo", before()))])
        fill(table, [("s1", ("demo", after()))])
        await pilot.pause()
        assert table.get_row("s1")[1].style == after().style, desc


@pytest.mark.anyio
@pytest.mark.parametrize("widget, sizes, desc", [
    (Input, (0,), "an Input's one row: a bar covered its text (#55)"),
    (Footer, (0, 0), "the footer's one row"),
])
async def test_widgets_that_have_no_scrollbar_keep_none(store, sid, widget, sizes, desc):
    """The app-wide one-cell bars outrank Textual's own zero sizes: those are put back."""
    from claude_wheelhouse.tui import NewSession
    app = WheelhouseApp(store)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.push_screen(NewSession())
        await pilot.pause()
        w = next(iter(app.screen.query(widget)), None) or app.screen_stack[0].query_one(widget)
        got = (w.styles.scrollbar_size_horizontal, w.styles.scrollbar_size_vertical)[:len(sizes)]
        assert got == sizes, desc


@pytest.mark.anyio
@pytest.mark.parametrize("clicks, box, desc", [
    (1, "pasted", "one right-click pastes once"),
    (2, "pasted", "a second before the clipboard answers supersedes the first: still once (#52)"),
])
async def test_right_click_paste_is_exclusive(store, sid, monkeypatch, clicks, box, desc):
    from claude_wheelhouse import tui
    store.post_item(sid, "question", "which db?")
    monkeypatch.setattr(tui, "system_clipboard", lambda: time.sleep(0.3) or "pasted")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.answer.focus()
        for _ in range(clicks):
            await right_click(app, "#answer")
        await pilot.pause(0.8)   # not wait_for_complete: it raises for the superseded worker
        assert app.answer.text == box, desc


@pytest.mark.anyio
async def test_the_session_list_and_stats_pane_share_a_reader(store, sid):
    """OBS 2 (#51): each transcript read once, not once for the context bar and again for the pane."""
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.follow(sid)
        app.refresh_data()
        await pilot.pause()
        assert app.contexts[sid] is app.query_one("#stats").followers[sid]


@pytest.mark.anyio
@pytest.mark.parametrize("size, desc", [((100, 40), "100 columns"), ((160, 40), "160 columns")])
async def test_session_names_keep_sixteen_cells(store, tmp_path, size, desc):
    """BUG 3 (#51): the context bar's column took 2 cells from the names; they have 16 again."""
    store.create_session(str(tmp_path), name="n" * 16)
    app = WheelhouseApp(store)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        t = app.query_one("#session-list", DataTable)
        assert t.virtual_size.width <= t.scrollable_content_region.width, desc


def footer_labels(app) -> list[str]:
    from textual.widgets._footer import FooterKey
    return [k.description for k in app.screen.query(FooterKey)]


@pytest.mark.anyio
@pytest.mark.parametrize("presses, label, shown, desc", [
    (0, "Show finished", False, "finished items hidden: F offers to show them (#57)"),
    (1, "Hide finished", True, "shown: F offers to hide them"),
    (2, "Show finished", False, "and back"),
])
async def test_the_f_label_says_what_f_does(store, sid, presses, label, shown, desc):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.items_table.focus()
        for _ in range(presses):
            await pilot.press("f")
            await pilot.pause()
        labels = footer_labels(app)
        assert app.show_finished == shown, desc
        assert label in labels and labels.count(label) == 1, f"{desc}: {labels}"
        assert {"Show finished", "Hide finished"} - {label} - set(labels), f"{desc}: only the one that applies"
        assert not {"Inbox", "Sessions"} & set(labels), "no tab keys: the tabs are gone (#58)"


@pytest.mark.anyio
@pytest.mark.parametrize("size, half", [((80, 24), False), ((100, 30), True), ((160, 40), True)])
@pytest.mark.parametrize("status, parked, shown, desc", [
    ("live", True, ["rename", "relaunch", "park", "end", "mode", "send", "interrupt", "compact", "shell"],
     "a running hosted session, parked and too old to queue: the longest captions"),
    ("live", False, ["rename", "relaunch", "park", "end", "mode", "send", "interrupt", "compact", "shell"],
     "a running one: no Restore"),
    ("dead", False, ["restore", "end", "mode", "send"], "a dead one: Restore and End, nothing to interrupt"),
    ("dead", True, ["restore", "park", "end", "mode", "send"], "a parked dead one: Unpark too"),
])
async def test_the_session_buttons_fit(store, tmp_path, monkeypatch, size, half, status, parked, shown, desc):
    """#58, #62: only the buttons that apply show, each its whole caption, in grey with a
    tooltip, filling the grid from the left with no hole where one is hidden and a blank
    row between rows; and the list and description keep their room, the description half
    the column where there's room."""
    from claude_wheelhouse import liveness
    from claude_wheelhouse.tui import BUTTONS
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: status)
    sid = store.create_session(str(tmp_path), name="hosted", runner="sdk")
    store.db.execute("UPDATE sessions SET code_version = 1")
    store.set_parked(sid, parked)
    app = WheelhouseApp(store)
    desc = f"{desc}, at {size[0]} columns"
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        buttons = [b for b in app.query("#sessions-pane Grid Button") if b.display]
        assert [b.id for b in buttons] == shown, desc
        strips = app.screen._compositor.render_strips()
        for b in buttons:
            drawn = "".join(seg.text for seg in strips[b.region.y])[b.region.x:b.region.right]
            assert str(b.label) in drawn and b.region.height == 1, f"{desc}: {b.label!s} drawn whole, got {drawn!r}"
            assert app.screen.get_widget_at(*b.region.offset)[0] is b, f"{desc}: {b.label} is on screen, not covered"
            assert b.tooltip == BUTTONS[b.id], f"{desc}: {b.id}'s tooltip"
            grey = ("#333333", "#8C8C8C") if b.disabled else ("#4D4D4D", "#FFFFFF")   # dimmed when disabled
            assert (b.styles.background.hex, b.styles.color.hex) == grey, f"{desc}: {b.id} grey"
        for grid in app.query("#sessions-pane Grid"):
            mine = [b for b in buttons if b.parent is grid]
            rows = sorted({b.region.y for b in mine})
            assert all(y2 - y1 == 2 for y1, y2 in zip(rows, rows[1:])), f"{desc}: a blank row between rows"
            cells = [(rows.index(b.region.y), b.region.x) for b in mine]
            assert cells == sorted(cells), f"{desc}: in order, row by row"
            lefts = sorted({b.region.x for b in mine if b.id != "send"})   # Send follows Mode's two columns
            assert lefts == sorted({b.region.x for b in grid.query(Button) if b.display and b.id != "send"}) \
                and lefts[0] == grid.content_region.x, f"{desc}: no hole at the left of a row"
        assert app.query_one("#session-list").region.height >= 5, desc
        column = app.query_one("#sessions-pane").content_region.height
        info = app.query_one("#session-info").region.height
        assert info == column // 2 if half else 3 <= info <= column // 2, f"{desc}: half the column, or less"
        if "park" in shown:
            assert str(app.query_one("#park", Button).label) == ("Unpark" if parked else "Park"), desc
        if status == "live":
            assert str(app.query_one("#mode", Button).label) == "Can't queue: relaunch", desc
        drawn = "".join(seg.text for seg in strips[app.screen.query_one(Footer).region.y])
        assert size[0] < 100 or all(f"{key} {label}" in drawn for key, label in (("N", "New"), ("A", "Adopt"), ("Shift+S", "Restore all"))), \
            f"{desc}: New, Adopt and Restore all in the footer (at 80 it scrolls), got {drawn!r}"


def splitter(app, key: str) -> Splitter:
    return next(s for s in app.query(Splitter) if s.key == key)


def sized(app, key: str) -> int:
    sp = splitter(app, key)
    return sp.extent(sp.panes()[0])


async def drag(pilot, key: str, dx: int, dy: int) -> None:
    sp = splitter(pilot.app, key)
    x, y = sp.region.offset
    await pilot.mouse_down(sp)
    # a move with the button held, as a terminal sends one: one with none is a release that never arrived
    pilot.app.post_message(events.MouseMove(None, x + dx, y + dy, dx, dy, button=1, shift=False, meta=False,
                                            ctrl=False, screen_x=x + dx, screen_y=y + dy))
    await pilot.pause()
    assert sp.has_class("-dragging"), "lit while it's dragged"
    await pilot.mouse_up(None, (x + dx, y + dy))
    await pilot.pause()


@pytest.mark.anyio
@pytest.mark.parametrize("key, dx, dy, change, desc", [
    ("sessions-pane", 10, 0, 10, "the sessions column, wider"),
    ("sessions-pane", -20, 0, 0, "never narrower than its buttons' captions need"),
    ("detail-pane", 10, 0, -10, "the right pane, narrower as its left edge moves right"),
    ("session-info", 0, -4, 4, "the description, taller as its top edge moves up"),
    ("items", 0, 3, 3, "the item list, taller over the stats"),
    ("answer", 0, -3, 3, "the answer box, taller"),
])
async def test_dragging_a_splitter_resizes_and_is_kept(store, sid, key, dx, dy, change, desc):
    """Doug: the boundaries between all areas draggable. A drag is kept across runs, and a
    double-click puts the default back."""
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        before = sized(app, key)
        await drag(pilot, key, dx, dy)
        after = sized(app, key)
        assert after - before == change, desc
        assert not splitter(app, key).has_class("-dragging"), desc
    assert store.setting("layout." + key) is not None, f"{desc}: kept"
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert sized(app, key) == after, f"{desc}: as it was left, next run"
        await pilot.double_click(splitter(app, key))
        await pilot.pause()
        assert sized(app, key) == before, f"{desc}: a double-click puts the default back"
    assert store.setting("layout." + key) is None, desc


@pytest.mark.anyio
@pytest.mark.parametrize("width, desc", [(200, "a wider terminal"), (120, "a narrower one")])
async def test_dragged_sizes_follow_a_resized_terminal(store, sid, width, desc):
    """Sizes are kept as shares, so a resized terminal keeps the proportions."""
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        await drag(pilot, "sessions-pane", 21, 0)   # 60 of the 158 cells inside the two splitters
        share = sized(app, "sessions-pane") / app.query_one("#main").size.width
        await pilot.resize_terminal(width, 40)
        await pilot.pause()
        assert abs(sized(app, "sessions-pane") - share * app.query_one("#main").size.width) <= 1, desc
        assert app.query_one("#items-pane").region.width >= 20, f"{desc}: the inbox keeps its room"


def on_screen(app, *selectors) -> bool:
    """Every pane at least partly on screen, and none past its right edge."""
    width = app.screen.size.width
    return all(0 < (r := app.query_one(sel).region).width and r.right <= width for sel in selectors)


COLUMNS = ("#sessions-pane", "#items-pane", "#detail-pane")


@pytest.mark.anyio
@pytest.mark.parametrize("width, stored, desc", [
    (80, None, "80 columns: the three columns, as before #60 (review 8)"),
    (91, None, "91: under the old minimums' 92"),
    (120, {"detail-pane": "0.69"}, "a share dragged on a wide terminal, shrunk on a narrower one"),
    (120, {"sessions-pane": "0.6", "detail-pane": "0.6"}, "two of them"),
    (160, {"sessions-pane": "abc"}, "an unreadable size is ignored"),
    (160, {"sessions-pane": "nan"}, "so is one not finite"),
    (160, {"detail-pane": "inf"}, "nor infinite"),
    (160, {"sessions-pane": "5.0"}, "one over the whole is clamped"),
    (160, {"sessions-pane": "-1"}, "as is a negative one"),
    # review 9: a share under its pane's minimum, held up by CSS, counted at the minimum
    (90, {"sessions-pane": "0.6025", "detail-pane": "0.1225"}, "120 and 24 of 200, on 90"),
    (80, {"sessions-pane": "0.6025", "detail-pane": "0.1225"}, "120 and 24 of 200, on 80"),
    (113, {"sessions-pane": "0.218016", "detail-pane": "0.933439"}, "the sweep's 127 on 113"),
])
async def test_the_columns_stay_on_screen(store, sid, width, stored, desc):
    for key, value in (stored or {}).items():
        store.set_setting("layout." + key, value)
    app = WheelhouseApp(store)
    async with app.run_test(size=(width, 30)) as pilot:
        await pilot.pause()
        assert on_screen(app, *COLUMNS, "#answer"), desc
        assert app.query_one("#items-pane").region.width >= 12, f"{desc}: the inbox keeps its minimum"
        assert columns_fill(app), f"{desc}: the columns and splitters fill #main exactly"
    assert {k: store.setting("layout." + k) for k in stored or {}} == (stored or {}), f"{desc}: the setting stands"


def columns_fill(app) -> bool:
    return sum(app.query_one(sel).outer_size.width for sel in COLUMNS) + 2 == app.query_one("#main").size.width


@pytest.mark.anyio
async def test_any_shares_fit_any_width(store, sid):
    """Review 9, property-style: random shares for the two columns on random widths, the
    columns always fill #main, none past it."""
    rng = random.Random(9)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 30)) as pilot:
        await pilot.pause()
        for _ in range(15):   # a quarter of these overflowed before review 9's fix
            shares = {key: rng.uniform(0.02, 0.98) for key in ("sessions-pane", "detail-pane")}
            width = rng.randint(80, 220)
            for key, fraction in shares.items():
                splitter(app, key).apply(fraction)
            await pilot.resize_terminal(width, 30)
            await pilot.pause()
            app.fit_layout()   # the same size twice is no resize: fit it here
            await pilot.pause()
            assert columns_fill(app) and on_screen(app, *COLUMNS), f"{width} columns, shares {shares}"


@pytest.mark.anyio
async def test_a_shrunk_share_comes_back_with_room(store, sid):
    """Review 8: a share that didn't fit is shrunk, not forgotten: a wide terminal has it back."""
    store.set_setting("layout.detail-pane", "0.69")
    app = WheelhouseApp(store)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        assert on_screen(app, *COLUMNS)
        await pilot.resize_terminal(200, 30)
        await pilot.pause()
        await pilot.pause()
        assert abs(sized(app, "detail-pane") - 0.69 * app.query_one("#main").size.width) <= 1


@pytest.mark.anyio
@pytest.mark.parametrize("missing, desc", [
    (False, "a module pane swapped for a card: the card keeps its place under the splitter"),
    (True, "a neighbour gone altogether: the splitter does nothing"),
])
async def test_the_items_splitter_survives_a_pane_that_raised(store, sid, monkeypatch, missing, desc):
    """Review 8: a pane that raised was swapped for a card without the -split class, and a
    press on the splitter above it then raised NoMatches and closed the app."""
    from claude_wheelhouse import stats_pane

    def boom(self):
        raise RuntimeError("pane bug")
    monkeypatch.setattr(stats_pane.SessionStats, "tick", boom)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        card = app.query_one(".module-error")
        assert card.has_class("-split"), desc
        if missing:
            card.remove_class("-split")
        before = sized(app, "items") if not missing else None
        if missing:
            await pilot.mouse_down(splitter(app, "items"))
            await pilot.pause()
        else:
            await drag(pilot, "items", 0, -3)
            assert sized(app, "items") == before - 3, f"{desc}: and still drags"
        assert app.is_running and app.return_code is None, desc


@pytest.mark.anyio
async def test_a_lost_release_ends_the_drag(store, sid):
    """Review 8: with the release lost (let go outside the terminal), a move with no button
    held ends the drag, rather than the pane following the pointer about."""
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        sp = splitter(app, "sessions-pane")
        x, y = sp.region.offset
        before = sized(app, "sessions-pane")
        await pilot.mouse_down(sp)
        await pilot.hover(None, (x + 15, y + 2))
        await pilot.pause()
        assert (sized(app, "sessions-pane"), sp.has_class("-dragging"), app.mouse_captured) == (before, False, None)
    assert store.setting("layout.sessions-pane") is None


@pytest.mark.anyio
async def test_parked_sessions_stay_in_the_list_after_the_rest(store, sid, tmp_path):
    """#58: with the Sessions tab gone, the list is the only way to a parked session (Unpark,
    Restore, End): it stays, dimmed, at the foot."""
    first = store.create_session(str(tmp_path), name="first")
    other = store.create_session(str(tmp_path), name="other")
    store.set_parked(sid, True)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        t = app.query_one("#session-list", DataTable)
        assert [r.key.value for r in t.ordered_rows] == [first, other, sid]
        assert "dim" in str(t.get_row(sid)[1].style)


def test_the_keys_overlay_has_relaunch_and_no_tabs():
    from claude_wheelhouse.tui import keys_help
    text = keys_help()
    assert "Relaunch:" in text and "Sessions tab" not in text and "Inbox tab" not in text
    assert text.count("Show or hide finished items") == 1, "F once, though it has two bindings"
    assert text.count("Shift+S") == 1, "Shift+S once, though it has two bindings"


def session_names(app) -> list[str]:
    return [app.store.session(r.key.value)["name"] for r in app.session_list.ordered_rows]


@pytest.mark.anyio
@pytest.mark.parametrize("parked, start, order, desc", [
    ([], "demo", ["bee", "sea", "demo"], "Park moves the row to the foot, and the cursor with it (review 7)"),
    (["bee", "sea"], "sea", ["demo", "sea", "bee"], "Unpark moves it up, and the cursor with it"),
])
async def test_the_cursor_stays_on_a_session_that_moves(store, sid, tmp_path, monkeypatch, parked, start, order, desc):
    some_live(monkeypatch)
    ids = {"demo": sid, **{n: store.create_session(str(tmp_path), name=n) for n in ("bee", "sea")}}
    for name in parked:
        store.set_parked(ids[name], True)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.session_list.move_cursor(row=session_names(app).index(start))
        await pilot.pause()
        if parked:
            await pilot.click("#park")
        else:
            await park_live(app, pilot, ids[start])
        await pilot.pause()
        assert session_names(app) == order, desc
        assert app.current_session() == ids[start], desc


@pytest.mark.anyio
async def test_tab_goes_from_the_session_list_to_the_items(store, sid):
    """Review 7: the description and the buttons stay out of the Tab order."""
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.session_list.focus()
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is app.items_table


@pytest.mark.anyio
@pytest.mark.parametrize("key, button, desc", [
    ("r", "rename", "R renames"),
    ("l", "relaunch", "L relaunches"),
    ("s", "restore", "S restores"),
    ("p", "park", "P parks"),
    ("e", "end", "E ends"),
])
async def test_the_session_lists_keys_press_its_buttons(store, sid, key, button, desc):
    """The buttons are out of the Tab order, so the list's keys keep them on the keyboard."""
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        pressed = []
        app.query_one(f"#{button}", Button).press = lambda: pressed.append(button)
        app.session_list.focus()
        await pilot.press(key)
        await pilot.pause()
        assert pressed == [button], desc
        assert not app.query_one(f"#{button}", Button).can_focus, f"{desc}: never focused"


@pytest.mark.anyio
@pytest.mark.parametrize("parked, prompted, desc", [
    (False, True, "selecting a dead session offers a relaunch"),
    (True, False, "not a parked one: selecting it is how to reach its buttons (review 7)"),
])
async def test_selecting_a_dead_session_offers_a_relaunch_unless_parked(store, sid, monkeypatch, parked, prompted,
                                                                       desc):
    monkeypatch.setattr(launch, "open_tab", lambda s, i: None)
    store.set_parked(sid, parked)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        await pilot.click("#session-list", offset=(4, 1))
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, Confirm) == prompted, desc
        assert app.viewing == sid, f"{desc}: it follows the session either way"


@pytest.mark.anyio
@pytest.mark.parametrize("parked, status, read, desc", [
    (False, "live", True, "a running session has a context bar"),
    (False, "dead", True, "so does a dead one, read once"),
    (True, "live", True, "so does a parked one that is running (review 7)"),
    (True, "dead", False, "not a parked dead one"),
])
async def test_which_sessions_get_a_context_bar(store, sid, monkeypatch, parked, status, read, desc):
    from claude_wheelhouse import liveness
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: status)
    store.set_parked(sid, parked)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert (sid in app.contexts) == read, desc


@pytest.mark.anyio
@pytest.mark.parametrize("followed, repaints, desc", [
    ("demo", 1, "a read for the session the pane shows repaints it"),
    ("other", 0, "one for another session doesn't (review 7)"),
])
async def test_a_landed_context_read_repaints_the_pane_without_a_tick(store, sid, tmp_path, followed, repaints,
                                                                     desc):
    other = store.create_session(str(tmp_path), name="other")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.follow({"demo": sid, "other": other}[followed])
        await pilot.pause()
        pane, calls = app.query_one("#stats"), []
        pane.tick = lambda: calls.append("tick")
        pane.repaint = lambda *a: calls.append("repaint")
        app.read_landed(sid)
        assert calls == ["repaint"] * repaints, desc


@pytest.mark.anyio
@pytest.mark.parametrize("cancel, pasted, desc", [
    (False, "pasted", "a paste lands in the box"),
    (True, "", "not once its worker is cancelled while the paste waits on the UI thread (review 7)"),
])
async def test_a_paste_is_checked_again_on_the_ui_thread(store, sid, monkeypatch, cancel, pasted, desc):
    from claude_wheelhouse import tui

    class Worker:
        is_cancelled = False
    worker = Worker()
    monkeypatch.setattr(tui, "system_clipboard", lambda: "pasted")
    monkeypatch.setattr(tui, "get_current_worker", lambda: worker)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.answer.focus()
        await pilot.pause()

        def ui_thread(callback):
            worker.is_cancelled = cancel   # superseded between the worker's check and this
            callback()
        monkeypatch.setattr(app, "call_from_thread", ui_thread)
        app.paste_into(app.answer)
        await pilot.pause()
        assert app.answer.text == pasted, desc


LONG_PROMPT = ("Compact demo? It's asked for what to keep, then compacted with that: the conversation carries "
               "on from the summary, which frees room in the window but drops detail it may want later")


@pytest.mark.anyio
@pytest.mark.parametrize("dialog, height, desc", [
    (Confirm, 24, "a confirmation wraps, every word on screen"),
    (Choice, 24, "and the request's choice"),
    (Confirm, 8, "on a tiny terminal the dialog stays on screen, scrolling"),
])
async def test_a_long_prompt_wraps_in_its_dialog(store, sid, dialog, height, desc):
    """Dialogs showed one line of their prompt, cut off at the border."""
    app = WheelhouseApp(store)
    async with app.run_test(size=(80, height)) as pilot:
        await pilot.pause()
        app.push_screen(dialog(LONG_PROMPT))
        await pilot.pause()
        box = app.screen.query_one("#dialog")
        assert box.region.bottom <= height, desc
        if height < 24:
            return
        rows = html.unescape(re.sub(r"<[^>]+>", "", app.export_screenshot())).replace("\xa0", " ").splitlines()
        inside = " ".join(r.strip("█") for r in rows if len(r) > 2 and r[0] == r[-1] == "█")   # the dialog's border
        assert LONG_PROMPT in " ".join(inside.split()), desc
