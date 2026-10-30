import bisect
import functools
import getpass
import html
import itertools
import json
import re
import time
import random
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import pytest
from rich.cells import cell_len
from rich.color import Color
from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.pilot import _get_mouse_message_arguments
from textual.strip import Strip
from textual.visual import Visual, visualize
from textual.widgets import Button, Checkbox, DataTable, Footer, Input, Label, Select, TextArea

from claude_wheelhouse import emoji, features, launch, models, ordering, transcript, transcript_usage, tui, tutorial
from claude_wheelhouse.hub import Done, Hub
from claude_wheelhouse.store import PROTOCOL_VERSION

# the stats pane's tests among these, and the slots' defaults, need the stats module
stats = pytest.importorskip("claude_wheelhouse_stats.stats", reason="installed without the stats module")
from claude_wheelhouse.splitter import Splitter
from conftest import first_item, keeps_colours, to_first_item
from claude_wheelhouse.store import COMPACT_QUESTION, Store, mode
from claude_wheelhouse.tui import (BLANK, COMPACT_HINT, DENY_HINT, MATRIX, NOTHING_SELECTED, PERMISSION_HINT, VOICE, WheelhouseApp, Choice,
                                   CompactButtons, CompactSession, Confirm, ForgetChoice, ParkSession,
                                   DenyReason, Folders, Hint, PermissionButtons, RelaunchChoice, RenameSession, SendBar, SessionList,
                                   ThreadView, Transcript, cylon, fill, hint, render)


@pytest.mark.anyio
@pytest.mark.both
async def test_answer_reaches_the_session(store, sid, live):
    store.db.execute("UPDATE sessions SET code_version = ?", (PROTOCOL_VERSION,))   # live, on code that queues
    q = store.post_item(sid, "question", "which db?", "Postgres or SQLite for the cache?")
    store.post_item(sid, "task", "build", status="running")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        items = app.query_one("#items", DataTable)
        assert items.row_count == 3, "the two items and the session's conversation"
        assert str(items.get_row_at(first_item(app))[2]) == q, "open question first"
        items.move_cursor(row=first_item(app))
        await pilot.pause()
        assert app.selected == (sid, q)
        app.query_one("#answer", TextArea).text = "SQLite, it's local"
        await pilot.press("ctrl+enter")
        await pilot.pause()
        assert store.pending(sid) == [], "Ctrl+Enter queues: sessions start in queued mode"
        assert str(items.get_row_at(first_item(app))[3]) == "queued", "the item list shows the queued answer"
        assert str(app.query_one("#session-list", DataTable).get_row_at(0)[5]) == "1"
        await pilot.press("ctrl+s")
        await pilot.pause()
    assert [m["body"] for m in store.pending(sid)] == ["SQLite, it's local"]
    assert store.item(sid, q)["status"] == "open", "the session's reply says whether it's answered"


@pytest.mark.anyio
@pytest.mark.both
@pytest.mark.parametrize("send_mode, key, drafts, sent, desc", [
    ("queued", "ctrl+enter", ["earlier", "this"], [], "queued: Ctrl+Enter holds it behind the earlier draft"),
    ("queued", "ctrl+j", ["earlier", "this"], [], "queued: Ctrl+Enter as most terminals send it"),
    ("queued", "alt+s", ["earlier"], ["this"], "queued: Alt+S sends this one now, the earlier draft left queued"),
    ("immediate", "ctrl+enter", ["earlier"], ["this"], "immediate: Ctrl+Enter sends at once"),
    ("immediate", "alt+s", ["earlier"], ["this"], "immediate: Alt+S does as Ctrl+Enter"),
])
async def test_alt_s_sends_one_answer_now(store, sid, live, clock, send_mode, key, drafts, sent, desc):
    store.db.execute("UPDATE sessions SET code_version = ?", (PROTOCOL_VERSION,))   # live, on code that queues
    store.set_mode(sid, send_mode)
    store.queue(sid, "earlier")
    store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.query_one("#items", DataTable).move_cursor(row=0)
        await pilot.pause()
        box = app.query_one("#answer", TextArea)
        box.focus()
        box.text = "this"
        if key == "alt+s":   # as a terminal sends it (ESC s): printable, so the box would type it
            app.post_message(events.Key("alt+s", "s"))
        else:
            await pilot.press(key)
        await pilot.pause()
        assert box.text == "", f"{desc}: submitted, and no s typed into the box"
    assert [m["body"] for m in store.drafts(sid)] == drafts, desc
    assert [m["body"] for m in store.pending(sid)] == sent, desc
    assert mode(store.session(sid)) == send_mode, f"{desc}: the session's mode left as it was"


@pytest.mark.parametrize("sends, shown, desc", [
    ("queue", True, "queued: the hint offers Alt+S"),
    ("send", False, "immediate: Ctrl+Enter sends already, so no Alt+S"),
])
def test_the_hint_offers_alt_s_only_when_queued(sends, shown, desc):
    assert ("Alt+S: send" in hint(sends)) is shown, desc


@pytest.mark.anyio
async def test_subagents_sit_under_their_session(store, sid, tmp_path):
    other = store.create_session(str(tmp_path), name="other")
    with one_second():   # active, so the last posted first: A2 before A1 in demo's group
        store.post_item(sid, "agent", "fork one", status="running")
        store.post_item(other, "agent", "fork two", status="running")
        store.post_item(sid, "agent", "fork three", status="running")
        q = store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        rows = item_list_rows(app)   # the conversation rows above them (#236) left out
    assert rows[0] == ["demo", q], "tasks and questions first, in inbox order"
    assert [r[1] for r in rows[1:4]] == ["└ A2", "└ A1", "└ A1"]
    assert [r[0] for r in rows[1:4]] == ["demo", "", "other"], "each session's agents grouped under its name"


@pytest.mark.anyio
async def test_finished_items_toggle_in_and_take_a_message(store, sid):
    t = store.post_item(sid, "task", "build", status="running")
    done = store.post_item(sid, "task", "ship", status="done")
    closed = store.post_item(sid, "question", "which db?", status="closed")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        items = app.query_one("#items", DataTable)
        refs = lambda: [str(items.get_row_at(i)[2]) for i in range(items.row_count)]
        assert refs() == ["💬", t, done], "a done task stays, settled, at the foot; a closed question is hidden (T71)"
        await pilot.press("alt+v")
        await pilot.pause()
        assert refs() == ["💬", t, done, closed], "finished items after the rest"
        items.move_cursor(row=refs().index(closed))
        await pilot.pause()
        app.query_one("#answer", TextArea).text = "the punchline"
        await pilot.press("ctrl+enter")
        await pilot.pause()
        app.query_one("#answer", TextArea).text = ""
        box = app.query_one("#answer", TextArea)
        box.focus()
        await pilot.press("f")
        await pilot.pause()
        assert len(refs()) == 4 and box.text == "f", "a plain f types into the answer box instead of toggling"
        await pilot.press("alt+v")
        await pilot.pause()
        assert len(refs()) == 3, "Alt+V toggles from the answer box too (#189)"
    assert [(m["item_ref"], m["body"]) for m in store.drafts(sid)] == [(closed, "the punchline")]
    assert store.item(sid, closed)["status"] == "closed", "a message on a finished item leaves its status alone"


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
        app.query_one("#items", DataTable).move_cursor(row=first_item(app))
        await pilot.pause()
        assert app.selected == (sid, q)
        store.end(sid)
        app.query_one("#answer", TextArea).text = "too late"
        await pilot.press("ctrl+enter")
        await pilot.pause()
    assert store.session(sid) is None


@pytest.mark.anyio
async def test_a_refused_unpark_leaves_the_session_parked(store, sid, monkeypatch):
    """Review #8: unpark only once the launch has gone through."""
    store.set_parked(sid, True)

    def refuse(s, i):
        raise RuntimeError("already running")
    monkeypatch.setattr(launch, "open_tab", refuse)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        await pilot.click("#relaunch")   # Unpark, on a parked session
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
    ("alt+n", False, "Alt+N declines without opening New session"),
    ("alt+y", True, "Alt+Y confirms"),
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
        app.resolve_request(sid, "end", "force")
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
        await pilot.press("alt+n")
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
    monkeypatch.setattr(adopt, "adopt", lambda store, c, name, sdk=None, may_tab=True: got.append((c.id, name)))
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("alt+a")
        await pilot.pause()
        assert "Still running" in str(app.screen.query_one("#adopt-hint").render()), desc
        await pilot.press("alt+n")   # app keys stay out of the dialog
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert got == launched, desc
        assert [type(s).__name__ for s in app.screen_stack] == stack, desc


@pytest.mark.anyio
async def test_a_hovered_or_highlighted_adoptable_keeps_its_running_mark_amber(store, monkeypatch):
    """#217: hovering a row, or the cursor on it, left its colours in the table's."""
    from claude_wheelhouse import adopt
    monkeypatch.setattr(adopt, "candidates", lambda store: [adoptable(pid=4242), adoptable("adopt-too")])
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("alt+a")
        await pilot.pause()
        kept = await keeps_colours(pilot, app.screen.query_one("#adopt-list"), "Adopt's list")
        assert "#ffd300" in kept, "the running session's mark amber"


@pytest.mark.anyio
async def test_escape_closes_adopt(store, monkeypatch):
    from claude_wheelhouse import adopt
    monkeypatch.setattr(adopt, "candidates", lambda store: [])
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("alt+a")
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
        await pilot.press("alt+a")
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
        await pick(app, pilot, sid)
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
async def test_ctrl_s_sends_only_the_current_sessions_queue(store, sid, tmp_path, live):
    other = store.create_session(str(tmp_path), name="other")
    store.db.execute("UPDATE sessions SET code_version = ?", (PROTOCOL_VERSION,))   # live, on code that queues
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


async def pick(app, pilot, sid: str) -> None:
    """The session selected, as a click on it does: unfiltered none is, and its buttons, keys
    and description wait for one (#16)."""
    app.follow(sid)
    await pilot.pause()


@pytest.fixture
def live(monkeypatch):
    """Every session running: Rename and Relaunch show only on a live one (#62)."""
    from claude_wheelhouse import liveness
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "live")


def session_buttons(app) -> dict[str, tuple[str, bool]]:
    return {b.id: (str(b.label), b.disabled) for b in app.query("#conversation-buttons Button") if b.display}


@pytest.mark.anyio
async def test_send_bar_and_session_buttons_follow_the_queues(store, sid, tmp_path, live):
    other = store.create_session(str(tmp_path), name="other")
    store.db.execute("UPDATE sessions SET code_version = ?", (PROTOCOL_VERSION,))   # live, on code that queues
    same_second(store)   # in creation order under * (T114), however slowly they were made
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pick(app, pilot, sid)
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
        app.answer.focus()
        await pilot.pause()
        await pilot.click("#send-all")
        await pilot.pause()
        assert focused_id(app) == "answer", "a click on a button leaves focus alone"
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
async def test_one_current_session_for_the_buttons_and_keys(store, sid, tmp_path, monkeypatch, live, nav, desc, how,
                                                            acts):
    """D20, as revised in review 8: the session list's highlight is the current session, so
    the buttons under it, Ctrl+S and Ctrl+T, the hint and the activity line all act on or
    describe the same one, however the person got there. Unfiltered (#16) none is
    highlighted: the keys act on the highlighted item's session, the buttons on none."""
    monkeypatch.setattr(tutorial, "tutorial_dir", lambda store: tmp_path / "tut")
    mine = store.post_item(sid, "question", "which db?")
    store.queue(sid, "a", mine)
    target = store.create_session(str(tutorial.prepare(store)) if nav == "tutorial" else str(tmp_path),
                                  name="tutorial" if nav == "tutorial" else "other")
    q = store.post_item(target, "question", "Q1?")
    store.queue(target, "b", q)
    store.db.execute("UPDATE sessions SET code_version = ?", (PROTOCOL_VERSION,))   # live, on code that queues
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        # by key: the inbox sorts newest first to the second, so either question may lead (review 9)
        app.items_table.move_cursor(row=app.items_table.get_row_index(f"{sid}|{mine}"))
        await pilot.pause()
        assert app.bar_session(app.screen) == sid, f"{desc}: demo's item highlighted, so demo is in context"
        if nav == "list":
            await pick(app, pilot, sid)
            app.session_list.move_cursor(row=app.session_list.get_row_index(target))
        else:
            app.items_table.move_cursor(row=app.items_table.get_row_index(f"{target}|{q}"))
        await pilot.pause()
        current = target if nav == "list" else None   # unfiltered, none is highlighted
        assert (app.current_session(), app.bar_session(app.screen)) == (current, target), desc
        await (pilot.click(how) if how.startswith("#") else pilot.press(how))
        await pilot.pause()
    got = {i for i in (sid, target) if (store.pending(i) if acts == "send" else store.session(i)["send_mode"])}
    want = set() if how.startswith("#") and nav != "list" else {target}   # the buttons disabled with none selected
    assert got == want, f"{desc}: {how} acts on the session in context"


@pytest.mark.anyio
async def test_the_thread_views_bar_is_its_sessions(store, sid, tmp_path, live):
    other = store.create_session(str(tmp_path), name="other")
    store.db.execute("UPDATE sessions SET code_version = ?", (PROTOCOL_VERSION,))   # live, on code that queues
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
        await to_first_item(app, pilot)
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
        await pick(app, pilot, sid)
        lines = app._info_text.splitlines()
        assert lines[:4] == ["demo", "dead · tab · queued", "#7", str(tmp_path)], f"{desc}: name, status, ticket, cwd"
        assert shown in app._info_text, desc


LONG_SYNOPSIS = " ".join(f"Sentence {n} of the synopsis says what the session is up to at some length." for n in range(6))


@pytest.mark.anyio
@pytest.mark.parametrize("runner_, live, activity, after, desc", [
    ("sdk", True, "running Bash: make test", "running Bash: make test", "a running hosted session's"),
    ("sdk", True, "thinking", "thinking", "whatever it says"),
    ("sdk", True, "", "", "none yet"),
    ("sdk", True, "running Bash: " + "x" * 300, "running Bash: " + "x" * 300, "a long one, cropped to its two rows"),
    ("sdk", False, "thinking", "", "none while it isn't running"),
    ("tab", True, "thinking", "", "none for a tab, which has its own terminal"),
])
async def test_the_activity_has_its_own_rows(store, tmp_path, monkeypatch, runner_, live, activity, after, desc):
    """D81: the activity sits in two fixed rows under the description's scroll, always in view
    under a long synopsis, and nothing above it moves, rewraps or gains a scrollbar as it changes."""
    from claude_wheelhouse import liveness
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "live" if live else "dead")
    sid = store.create_session(str(tmp_path), name="hosted", runner=runner_)
    store.set_synopsis(sid, LONG_SYNOPSIS)
    store.set_activity(sid, activity)
    app = WheelhouseApp(store)
    async with app.run_test(size=(120, 40)) as pilot:
        await pick(app, pilot, sid)
        box, scroll = app.query_one("#session-info"), app.query_one("#session-info-scroll")
        line, text = app.query_one("#session-activity"), app.query_one("#session-info-text")
        assert (app._activity_text, bool(activity) and activity in app._info_text) == (after, False), desc
        rows = 2 if runner_ == "sdk" else 0   # a tab's session has no activity, so no rows for one
        assert line.region.height == rows and (not rows or line.region.y == scroll.region.bottom), \
            f"{desc}: {rows} rows under the scroll"
        assert line.region.bottom <= box.region.bottom, f"{desc}: in view"
        geometry = (scroll.region, scroll.virtual_size, text.region, scroll.vertical_scrollbar.display)
    store.set_activity(sid, "")
    app = WheelhouseApp(store)
    async with app.run_test(size=(120, 40)) as pilot:
        await pick(app, pilot, sid)
        scroll, text = app.query_one("#session-info-scroll"), app.query_one("#session-info-text")
        assert (scroll.region, scroll.virtual_size, text.region, scroll.vertical_scrollbar.display) == geometry, \
            f"{desc}: the same as with no activity"


@pytest.mark.anyio
async def test_end_names_the_queued_answers_it_discards(store, sid):
    store.queue(sid, "a", "Q1")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pick(app, pilot, sid)
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
        await pick(app, pilot, sid)
        assert app._info_text.splitlines()[1].split(" · tab")[0] == label, desc
        app.query_one("#items", DataTable).move_cursor(row=app.items_table.get_row_index(f"{sid}|{q}"))
        await pilot.pause()
        assert str(app.query_one("#mode", Button).label) == bar, desc
        app.query_one("#answer", TextArea).text = "SQLite"
        await pilot.press("ctrl+enter")
        await pilot.pause()
    assert (len(store.drafts()), len(store.pending(sid))) == (queued, 1 - queued), desc



@pytest.mark.anyio
async def test_send_all_with_nothing_left_to_send_says_so(store, sid, monkeypatch, live):
    store.db.execute("UPDATE sessions SET code_version = ?", (PROTOCOL_VERSION,))   # live, on code that queues
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


def by_name(monkeypatch, statuses: dict[str, str]) -> None:
    """Each session's liveness, by its name; on code that queues, so none sends at once."""
    from claude_wheelhouse import liveness
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: statuses[s["name"]])


@pytest.mark.anyio
@pytest.mark.parametrize("status, left, note, desc", [
    ("live", [], "sent 1 to demo", "a live session sends its queue"),
    ("dead", None, "nothing queued for that session", "a dead one with nothing queued: no offer to wake it"),
    ("stalled", [], "sent 1 to demo", "a stalled session is still running, so it sends too"),
])
async def test_ctrl_s_on_a_dead_session_says_restore_it_first(store, sid, monkeypatch, status, left, note, desc):
    by_name(monkeypatch, {"demo": status})
    store.db.execute("UPDATE sessions SET code_version = ?", (PROTOCOL_VERSION,))
    if left is not None:   # None: nothing queued, and none left
        store.queue(sid, "a")
    left = left or []
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pick(app, pilot, sid)
        assert app.current_session() == sid, f"{desc}: demo is current"
        notes = []
        monkeypatch.setattr(app, "notify", lambda text, **kw: notes.append(text))
        await pilot.press("ctrl+s")
        await pilot.pause()
    assert notes == [note], f"{desc}: the notice"
    assert [m["body"] for m in store.drafts(sid)] == left, f"{desc}: the drafts"


ALL_DEAD = {"demo": "dead", "gone": "dead", "slow": "dead"}
ALL_KEPT = {"demo": ["a"], "gone": ["b", "c"], "slow": ["d"]}


@pytest.mark.anyio
@pytest.mark.parametrize("statuses, by_key, label, left, note, desc", [
    ({"demo": "live", "gone": "dead", "slow": "stalled"}, True, "Send all (2)", {"gone": ["b", "c"]},
     "sent 1 to demo; 1 to slow", "the dead session's drafts are neither counted nor sent"),
    ({"demo": "live", "gone": "live", "slow": "starting"}, True, "Send all (4)", {},
     "sent 1 to demo; 2 to gone; 1 to slow", "with none dead, every queue goes"),
    (ALL_DEAD, True, "Send all (0)", ALL_KEPT, "Send all (0): nothing to do",
     "only dead sessions have drafts: Shift+A finds nothing to do"),
    (ALL_DEAD, False, "Send all (0)", ALL_KEPT,
     "nothing queued for a running session: a dead or parked one's queue waits until it's relaunched or unparked",
     "only dead sessions have drafts, pressed before the bar repainted: nothing sent, and it says why"),
])
async def test_send_all_skips_dead_sessions(store, sid, tmp_path, monkeypatch, statuses, by_key, label, left, note,
                                            desc):
    by_name(monkeypatch, statuses)
    ids = {"demo": sid, "gone": store.create_session(str(tmp_path), name="gone"),
           "slow": store.create_session(str(tmp_path), name="slow")}
    store.db.execute("UPDATE sessions SET code_version = ?", (PROTOCOL_VERSION,))
    for name, body in (("demo", "a"), ("gone", "b"), ("gone", "c"), ("slow", "d")):
        store.queue(ids[name], body)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert str(app.query_one("#send-all", Button).label) == label, f"{desc}: the label"
        notes = []
        monkeypatch.setattr(app, "notify", lambda text, **kw: notes.append(text))
        app.session_list.focus()   # out of the answer box, where Shift+A is typing
        if by_key:
            await app.action_press("send-all")
        else:
            app.send_all()
        await pilot.pause()
    assert notes == [note], f"{desc}: the notice"
    assert {n: [m["body"] for m in store.drafts(i)] for n, i in ids.items() if store.drafts(i)} == left, \
        f"{desc}: what stays queued"


@pytest.mark.anyio
@pytest.mark.parametrize("status, parked, ctrl_s, desc", [
    ("dead", False, "Ctrl+S: send queue", "a dead session's hint has Ctrl+S: it offers a relaunch first (#221)"),
    ("dead", True, "Ctrl+S: send queue", "as does a parked one's: it offers Unpark"),
    ("live", False, "Ctrl+S: send queue", "a live session's hint just sends"),
    ("stalled", False, "Ctrl+S: send queue", "as does a stalled one's: it's still running"),
])
async def test_the_hint_says_what_ctrl_s_does(store, sid, monkeypatch, status, parked, ctrl_s, desc):
    by_name(monkeypatch, {"demo": status})
    store.db.execute("UPDATE sessions SET code_version = ?", (PROTOCOL_VERSION,))
    store.set_parked(sid, parked)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pick(app, pilot, sid)
        base = app.screen.query_one(Hint).base
    assert ctrl_s in base and base == hint("queue"), f"{desc}: {base}"


@pytest.mark.anyio
@pytest.mark.parametrize("status, down, note, desc", [
    ("dead", 1, "queued for Q1, but the session isn't running: Ctrl+S relaunches it and sends its queue",
     "an answer queued for a dead session's question says its queue isn't running"),
    ("dead", 0, "queued for the session, but the session isn't running: Ctrl+S relaunches it and sends its queue",
     "a general message queued for a dead session says the same"),
    ("live", 1, "queued for Q1: Ctrl+S or Send sends the session's queue", "a live session's queue just waits"),
])
async def test_queueing_for_a_dead_session_says_to_restore_it(store, sid, monkeypatch, status, down, note, desc):
    by_name(monkeypatch, {"demo": status})
    store.db.execute("UPDATE sessions SET code_version = ?", (PROTOCOL_VERSION,))
    store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.follow(sid)
        app.items_table.focus()
        await pilot.press(*["down"] * down)
        await pilot.pause()
        notes = []
        monkeypatch.setattr(app, "notify", lambda text, **kw: notes.append(text))
        app.answer.text = "ship it"
        await pilot.press("ctrl+enter")
        await pilot.pause()
    assert notes == [note], f"{desc}: the notice"
    assert [m["body"] for m in store.drafts(sid)] == ["ship it"], f"{desc}: queued"


@pytest.mark.anyio
@pytest.mark.parametrize("parked, key, yes, prompt, drafts, sent, box, desc", [
    (True, "ctrl+s", True, "Unpark demo and send?", [], ["queued"], "ship it", "Ctrl+S, parked: unparked with its queue"),
    (False, "ctrl+s", True, "Relaunch demo and send?", [], ["queued"], "ship it", "Ctrl+S, dead: relaunched with it"),
    (True, "alt+s", True, "Unpark demo and send?", [], ["queued", "ship it"], "", "Alt+S, parked: the answer too"),
    (False, "alt+s", True, "Relaunch demo and send?", [], ["queued", "ship it"], "", "Alt+S, dead: the same"),
    (True, "ctrl+s", False, "Unpark demo and send?", ["queued"], [], "ship it", "Ctrl+S, No: nothing sent"),
    (False, "ctrl+s", False, "Relaunch demo and send?", ["queued"], [], "ship it", "Ctrl+S, No on a dead one"),
    (True, "alt+s", False, "Unpark demo and send?", ["queued"], [], "ship it", "Alt+S, No: the box keeps its text"),
    (False, "alt+s", False, "Relaunch demo and send?", ["queued"], [], "ship it", "Alt+S, No on a dead one"),
])
async def test_sending_to_a_session_not_running_offers_to_wake_it(store, sid, monkeypatch, parked, key, yes, prompt,
                                                                   drafts, sent, box, desc):
    """#221: Ctrl+S or Alt+S on a parked or dead session asks to Unpark or Relaunch it and
    send; Yes wakes it with its queue (and Alt+S's answer) as its first prompt, behind the
    resume notice; No sends nothing."""
    by_name(monkeypatch, {"demo": "dead"})
    monkeypatch.setattr(launch, "restore_session", lambda st, i, **kw: st.notice(i, "RESUME"))
    store.db.execute("UPDATE sessions SET code_version = ?", (PROTOCOL_VERSION,))
    store.set_parked(sid, parked)
    store.queue(sid, "queued")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.follow(sid)
        await pilot.pause()
        app.answer.focus()
        app.answer.text = "ship it"
        if key == "alt+s":   # as a terminal sends it (ESC s)
            app.post_message(events.Key("alt+s", "s"))
        else:
            await pilot.press(key)
        await pilot.pause()
        assert isinstance(app.screen, Confirm) and app.screen.prompt == prompt, f"{desc}: the dialog"
        await pilot.press("alt+y" if yes else "alt+n")
        await pilot.pause()
        assert app.answer.text == box, f"{desc}: the box"
    assert [m["body"] for m in store.drafts(sid)] == drafts, f"{desc}: the queue"
    assert [m["body"] for m in store.pending(sid)] == (["RESUME", *sent] if yes else []), \
        f"{desc}: the resume notice, then the person's words, each once"
    assert store.session(sid)["parked"] == (parked and not yes), f"{desc}: unparked only on Yes"


def test_a_general_message_cut_short_shows_whole_in_the_conversation(store, sid, tmp_path, monkeypatch):
    long = " ".join(["word"] * 200)
    store.send(sid, long)
    [m] = store.take_pending(sid)   # the monitor printed it, cut short, and the session took it up
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
        await pilot.press("enter", "alt+n")   # it isn't running: decline the offer to relaunch it
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
        assert [str(items.get_row_at(i)[-1]) for i in range(items.row_count)] == ["Conversation", "which db?"], \
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
@pytest.mark.parametrize("keys, desc", [
    (("delete",), "Delete"),
    (("backspace",), "Backspace"),
    (("ctrl+space",), "Ctrl+Space marks no conversation row"),
    (("ctrl+space", "down", "ctrl+space", "up", "delete"), "nor does a range: Delete on it closes nothing"),
    (("alt+p",), "Alt+P pins nothing"),
    (("alt+up",), "Alt+Up moves nothing"),
    (("alt+down",), "Alt+Down moves nothing"),
    (("ctrl+r",), "Ctrl+R reopens nothing"),
    (("ctrl+z",), "Ctrl+Z, nothing done before it"),
])
async def test_an_unfiltered_conversation_row_acts_on_no_item(store, sid, tmp_path, keys, desc):
    """The item list's keys on a session's conversation row in the unfiltered inbox (#192) leave
    every item as it was, as on the per-session conversation row."""
    other = store.create_session(str(tmp_path), name="other")
    o = store.post_item(sid, "question", "open one")
    c = store.post_item(sid, "question", "closed one", status="closed")
    b = store.post_item(other, "task", "B's", status="running")
    state = lambda: [(it["status"], tui.standing(it), tui.pinned(it)) for it in
                     (store.item(sid, o), store.item(sid, c), store.item(other, b))]
    before = state()
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        await pilot.press("alt+v")   # the closed one in sight, for Ctrl+R
        table = app.items_table
        table.focus()
        table.move_cursor(row=table.get_row_index(f"{sid}|"))
        await pilot.pause()
        rows = list(table.final())
        for key in keys:
            await pilot.press(key)
            await pilot.pause()
        assert not isinstance(app.screen, Confirm), f"{desc}: nothing to ask about"
        assert not {k for k in table.marked if k.endswith("|")}, f"{desc}: no conversation marked"
        assert list(table.final()) == rows, f"{desc}: every row where it was"
    assert state() == before, f"{desc}: every item as it was"


@pytest.mark.anyio
async def test_the_unfiltered_inbox_has_every_sessions_conversation(store, sid, tmp_path):
    """No session selected: each unparked session's conversation row, named after it, at the top
    under the inbox's own order (#236, once under the active items, #192). Picking one shows its
    conversation, the session list's highlight still off."""
    other = store.create_session(str(tmp_path), name="other")
    parked = store.create_session(str(tmp_path), name="napping")
    store.park(parked, compact=False, hosted=False)
    store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        items = app.query_one("#items", DataTable)
        rows = [(items.coordinate_to_cell_key((i, 0)).row_key.value, str(items.get_row_at(i)[2]),
                 str(items.get_row_at(i)[-1])) for i in range(items.row_count)]
        assert rows == [(f"{sid}|", "💬", "demo"), (f"{other}|", "💬", "other"), (f"{sid}|Q1", "Q1", "which db?")], \
            "a conversation row per unparked session, named after it, then the items"
        assert items.cursor_row == 0 and app.viewing == sid, "the first conversation highlighted, as the top row"
        items.focus()
        await pilot.press("down")
        await pilot.pause()
        assert app.viewing == other and app.filter_sid is None, "its conversation, the inbox still every session's"
        assert "other · conversation" in app._detail_text
        app.follow(sid)
        await pilot.pause()
        app.action_clear_filter()
        await pilot.pause()
        assert app.viewing == sid and items.cursor_key() == f"{sid}|", "Esc keeps the conversation it showed"


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


async def session_step(app, pilot, how, ids) -> None:
    """One move on the session list: "click A", "dclick A" (a double-click), "item B" (B's
    question highlighted in the items), "title" (a click on the list's title), "esc", or a
    key with the list focused."""
    what, _, name = how.partition(" ")
    if what in ("click", "dclick"):
        app._click_chain_last_time = None   # a click of its own, not the last one's second, however soon
        raw_click(app, app.session_list, (3, 1 + app.session_list.get_row_index(ids[name])), 1 + (what == "dclick"))
    elif what == "item":
        key = next(r.key.value for r in app.items_table.ordered_rows
                   if r.key.value.startswith(f"{ids[name]}|") and not r.key.value.endswith("|"))
        app.items_table.move_cursor(row=app.items_table.get_row_index(key))
    elif what == "title":   # the list's header line, at its unsorted end: the others sort (#75)
        raw_click(app, app.session_list, (header_x(app.session_list, 6), 0), 1)
    elif what == "esc":
        app.items_table.focus()
        await pilot.press("escape")
    else:
        app.session_list.focus()
        await pilot.press(what)
    for _ in range(4):
        await pilot.pause()


@pytest.mark.anyio
@pytest.mark.parametrize("steps, current, desc", [
    ([], None, "at the start none is selected"),
    (["click A"], "A", "a click selects a session"),
    (["click A", "click A"], None, "a click on the selected one unselects it"),
    (["click A", "click A", "click A"], "A", "clicking the one just unselected selects it again"),
    (["click A", "click B"], "B", "a click on another selects that"),
    (["click A", "click A", "click B"], "B", "unselected, a click on another selects it"),
    (["click A", "esc"], None, "Esc unselects, as the click does"),
    (["click A", "esc", "click A"], "A", "after Esc, a click on the one it was on selects it"),
    (["click A", "title"], None, "a click on the list's title unselects, as Esc does"),
    (["title"], None, "a click on the title with none selected changes nothing"),
    (["click A", "title", "click A"], "A", "after the title, a click on the one it was on selects it"),
    (["dclick B"], "B", "a double-click selects, its second click not unselecting"),
    (["click A", "enter"], "A", "Enter on the selected one keeps it"),
    (["item B"], None, "an item highlighted unfiltered brings no highlight back"),
    (["click A", "esc", "item B", "item A"], None, "nor do items highlighted after Esc"),
    (["click A", "esc", "down"], "A", "Down brings the highlight back on the one last followed"),
    (["click B", "esc", "up"], "B", "Up does the same"),
    (["down"], "A", "Down with none ever followed: the first"),
    (["enter"], "A", "Enter with none selected picks as Down does"),
])
async def test_a_click_on_the_selected_session_unselects_it(store, sid, tmp_path, monkeypatch, steps, current, desc):
    """Doug: click the selected session to unselect it, so the inbox shows every session's
    items; with none selected no highlight, the session buttons and their keys disabled, and
    the info box says All sessions (D20)."""
    other = store.create_session(str(tmp_path), name="other")
    ids = {"A": sid, "B": other}
    by_name(monkeypatch, {"demo": "live", "other": "live"})
    store.post_item(sid, "question", "A's")
    store.post_item(other, "question", "B's")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        for _ in range(3):
            await pilot.pause()
        for how in steps:
            await session_step(app, pilot, how, ids)
        app.refresh_data()   # a refresh brings no highlight back either
        await pilot.pause()
        want = ids.get(current)
        got = (app.filter_sid, app.session_list.show_cursor, app.current_session())
        assert got == (want, want is not None, want), f"{desc}: (filter, highlight, current) {got}"
        lifecycle = [(b.id, b.disabled) for b in app.session_buttons.query(Button) if b.display]
        assert all(disabled for _, disabled in lifecycle) == (want is None) and lifecycle, f"{desc}: {lifecycle}"
        assert app._info_text.startswith("All sessions" if want is None else "demo" if want == sid else "other"), \
            f"{desc}: {app._info_text!r}"
        shown = {r.key.value.split("|")[0] for r in app.items_table.ordered_rows}
        assert shown == ({want} if want else {sid, other}), f"{desc}: the items"


@pytest.mark.anyio
@pytest.mark.parametrize("key, desc", [
    ("alt+l", "Alt+L: Relaunch"),
    ("alt+p", "Alt+P: Park"),
    ("delete", "Delete: Forget"),
    ("backspace", "Backspace: Forget"),
    ("alt+r", "Alt+R: Rename"),
])
async def test_session_keys_do_nothing_with_none_selected(store, sid, tmp_path, monkeypatch, key, desc):
    other = store.create_session(str(tmp_path), name="other")
    by_name(monkeypatch, {"demo": "dead", "other": "dead"})
    launched = []
    monkeypatch.setattr(Hub, "open_session", lambda self, s, restore=False, **k: launched.append(s) or Done())
    q = store.post_item(other, "question", "B's")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.items_table.move_cursor(row=app.items_table.get_row_index(f"{other}|{q}"))   # an item's session in context
        await pilot.pause()
        app.session_list.focus()
        await pilot.press(key)
        await pilot.pause()
        assert [type(s).__name__ for s in app.screen_stack] == ["Screen"], f"{desc}: no dialog"
        assert [n.message for n in app._notifications] == [], f"{desc}: nothing said"
        assert (launched, [bool(store.session(s)["parked"]) for s in (sid, other)]) == ([], [False, False]), desc


@pytest.mark.anyio
@pytest.mark.parametrize("status, clicks, keys, stack, opened, desc", [
    ("dead", 1, (), ["Screen", "Confirm"], 0, "a click on a dead session offers to relaunch it"),
    ("dead", 2, (), ["Screen", "Confirm"], 0, "a double-click offers it once: its second click is the dialog's"),
    ("dead", 1, ("alt+y",), ["Screen"], 1, "a click, then Alt+Y: relaunched"),
    ("dead", 2, ("alt+y",), ["Screen"], 1, "a double-click, then Alt+Y: relaunched, no second offer left behind"),
    ("dead", 1, ("alt+n", "down"), ["Screen"], 0, "a click, then Alt+N: declined"),
    ("dead", 2, ("alt+n", "down"), ["Screen"], 0, "a double-click, then Alt+N: declined, no second offer left behind"),
    ("live", 2, (), ["Screen"], 0, "a double-click on a running session offers nothing"),
])
async def test_a_double_click_on_a_dead_session_offers_one_relaunch(store, sid, tmp_path, monkeypatch, status, clicks,
                                                                    keys, stack, opened, desc):
    """Review 19: each click of a double-click selected the session, and each offered a relaunch."""
    other = store.create_session(str(tmp_path), name="other")
    by_name(monkeypatch, {"demo": "live", "other": status})
    launched = []
    monkeypatch.setattr(Hub, "open_session", lambda self, s, restore=False, **k: launched.append(s) or Done())
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        for _ in range(3):
            await pilot.pause()
        raw_click(app, app.session_list, (3, 1 + app.session_list.get_row_index(other)), clicks)
        raw_keys(app, *keys)
        for _ in range(12):
            await pilot.pause()
        assert [type(s).__name__ for s in app.screen_stack] == stack, f"{desc}: the screens"
        assert app.current_session() == other, f"{desc}: followed"
    assert launched == [other] * opened, f"{desc}: relaunched"


def assert_one_current(app, desc):
    """D22: one current session, the session list's highlight, and everything that acts on
    or describes a session agrees on it: the buttons, Ctrl+S, Ctrl+T and the hint
    (bar_session), the modules (focus_sid), the selection and the answer box's target, whose
    session's mode Ctrl+Enter answers by, and the session info. Each may be None, where
    nothing is in context (an empty inbox selects nothing), but never another session.
    Unfiltered none is selected (#16): no highlight and no current session, the session
    buttons disabled, and the rest agree on the session in context, the selected item's.
    And what shows agrees: the items' cursor is on the selection, the hint and the permission
    buttons are for the item in context, the conversation buttons for the current session."""
    current = app.current_session()
    thread = isinstance(app.screen, ThreadView)
    assert (app.filter_sid, app.session_list.show_cursor) == (current, current is not None), \
        f"{desc}: highlighted only while followed, the followed session"
    box_target = (app.screen.sid, app.screen.ref) if thread else app.box_target   # a thread's box answers it
    said = {"bar": app.bar_session(app.screen), "focus_sid": app.focus_sid(),
            "selected": app.selected and app.selected[0], "box_target": box_target and box_target[0]}
    context = current or said["bar"]
    assert {k: v for k, v in said.items() if v not in (None, context)} == {}, f"{desc}: in context {context}, {said}"
    assert box_target == app.selected, f"{desc}: the box answers what the pane shows"
    info = app.display_name(app.row(current)) if current else "All sessions"
    assert app._info_text.split("\n")[0] == info, f"{desc}: the session info"
    table = app.items_table
    if app.selected:
        under = table.coordinate_to_cell_key((table.cursor_row, 0)).row_key.value
        assert under == f"{app.selected[0]}|{app.selected[1] or ''}", f"{desc}: the items' cursor on {under}"
    in_context = box_target
    item = in_context and in_context[1] and app.store.item(*in_context)
    asking = bool(item) and item["kind"] == "permission" and item["status"] == "open"
    nudged = bool(item) and item["status"] == "open" and item["answer"] == COMPACT_QUESTION
    s = app.row(app.bar_session(app.screen)) if app.bar_session(app.screen) else None
    sends = "send" if s and (mode(s) == "immediate" or app.sends_now(s["id"])) else "queue"
    assert [h.base for h in app.screen.query(Hint)] == [PERMISSION_HINT if asking else COMPACT_HINT if nudged
                                                        else hint(sends)], f"{desc}: the hint"
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
    await pilot.click("#park")   # asked at once: no confirm
    await pilot.pause()
    app.store.set_parked(sid, True)
    LIVE.discard(sid)   # and it stops: Unpark shows, on a Parked session
    app.refresh_data()


async def step(app, pilot, how, sid, other):
    """One thing the person (or, for ended elsewhere and refresh, the world) does."""
    if how in ("park", "unpark", "end") and app.filter_sid is None:   # its buttons need it selected (#16)
        app.follow(sid)
        await pilot.pause()
    if how == "park":
        await park_live(app, pilot, sid)
    elif how in ("unpark", "end"):
        await pilot.click("#relaunch" if how == "unpark" else "#end")   # Unpark is Relaunch, captioned
        await pilot.pause()
        if how != "unpark":
            await pilot.press("alt+y")
    elif how == "delete":
        app.items_table.focus()
        await pilot.press("delete")
        await pilot.pause()
        if app.screen is not app.screen_stack[0]:   # an open question asks first (#151)
            await pilot.press("alt+y")
    elif how in ("up", "down"):
        app.session_list.focus()
        await pilot.press(how)
    elif how == "click":
        row = [r.key.value for r in app.session_list.ordered_rows].index(other)
        await pilot.click("#session-list", offset=(4, row + 1))   # below the header row
        await pilot.pause()
        if isinstance(app.screen, Confirm):   # the offer to relaunch a dead session
            await pilot.press("alt+n")
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
            await pilot.press("alt+n")
    elif how == "mark delete":   # A's and B's questions marked, closed together
        app.items_table.set_marks({r.key.value for r in app.items_table.ordered_rows
                                   if r.key.value.split("|")[1].startswith("Q")})
        await pilot.pause()
        app.items_table.focus()
        await pilot.press("delete")
        await pilot.pause()
        if app.screen is not app.screen_stack[0]:   # open questions among them: asked once (#151)
            await pilot.press("alt+y")
    elif how == "thread":   # the highlighted item's thread, full screen
        app.items_table.focus()
        await pilot.press("enter")
        await pilot.pause()
        app.screen.focus_next()   # off its box, so Esc closes it
    elif how == "back":
        await pilot.press("escape")
    elif how == "burst":   # typed for A's item, then at once: the arrows onto B (unfiltered, the
        # first Down picks the first row), Ctrl+Enter, Ctrl+S. B runs, so * (T114) lists it first
        app.answer.text = "burst"
        app.session_list.focus()
        rows = [k for k in app.session_list.final() if k]
        if app.session_list.show_cursor:
            arrows = ["up" if rows.index(other) < rows.index(sid) else "down"]
        else:
            arrows = ["down"] if rows[0] == other else ["down", "down"]
        await pilot.press(*arrows, "ctrl+j", "ctrl+s")
    await pilot.pause()


@pytest.mark.anyio
@pytest.mark.both
@pytest.mark.parametrize("filtered", [False, True])
@pytest.mark.parametrize("steps, unfiltered_current, filtered_current, desc", [
    (["park", "unpark"], "A", "A", "Park follows the session (review 10): it stays current, Unpark one press away"),
    (["park", "up"], "B", "B", "then arrowing onto the other session follows it (review 10, bug 2)"),
    (["delete"], None, "A", "closing A's only item: unfiltered, still none selected"),
    (["end"], None, None, "End: the filter goes with it, none selected"),
    (["ended elsewhere"], None, None, "the current session ending elsewhere"),
    (["down"], "A", "B", "an arrow in the session list: unfiltered, it picks the first (#16)"),
    (["click"], "B", "B", "a click in the session list"),
    (["esc"], None, None, "Esc: whichever item is then highlighted"),
    (["refresh"], None, "A", "a refresh bringing a new item"),
    (["permission", "park"], "A", "A", "Park with A's permission item highlighted (review 11, bug 1)"),
    (["park", "permission", "unpark"], "A", "A", "Unpark with A's permission item highlighted"),
    (["permission", "enter"], "A", "A", "Enter on A's row with its permission item highlighted"),
    (["question", "enter"], "A", "A", "Enter on A's row with its question highlighted"),
    (["mark delete"], None, "A", "closing A's and B's questions together"),
    (["thread", "back"], None, "A", "a thread open, then Esc"),
    (["permission", "thread", "back"], None, "A", "a permission item's thread open, then Esc"),
    (["burst"], "B", "B", "Down, Ctrl+Enter and Ctrl+S at once: all on B (review 11, P1)"),
])
async def test_one_current_session_whatever_happens(store, sid, tmp_path, monkeypatch, steps, unfiltered_current,
                                                    filtered_current, desc, filtered):
    """D22, after review 10: the session list's highlight is the current session, and the
    selection, the box, the hint, the modules and the info box never describe another."""
    some_live(monkeypatch)   # all dead: Forget deletes at once, on a yes
    monkeypatch.setattr(launch, "restore_session", lambda *a, **kw: None)   # Unpark relaunches
    store.set_mode(sid, "immediate")
    with one_second():   # one second for all of it: under * (T114) A, made first, stays first
        mine = store.post_item(sid, "question", "A's")
        if "permission" in steps:
            store.post_item(sid, "permission", "Bash: rm -rf build")
        other = store.create_session(str(tmp_path), name="other")
        store.set_mode(other, "queued")
        store.post_item(other, "question", "B's")
        store.queue(sid, "A's draft")
        store.queue(other, "B's draft")
    if steps == ["burst"]:   # B running: Ctrl+S refuses a dead session (#62)
        LIVE.add(other)
        store.db.execute("UPDATE sessions SET code_version = ?", (PROTOCOL_VERSION,))   # live, on code that queues
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
        assert app.current_session() == expect, desc
        if steps[-1] == "unpark":
            assert not store.session(sid)["parked"] and app.filter_sid == sid, desc
        if steps == ["burst"]:   # the text stays with A's item, unsent; B's queue went, not A's
            drafts = [(m["session_id"], m["body"]) for m in store.drafts()]
            assert drafts == [(sid, "A's draft")] and not store._all("SELECT 1 FROM messages WHERE body = 'burst'"), \
                f"{desc}: {drafts}"


def dwell(app) -> None:
    """The dwell lapsing (tui.DWELL): the person rested on what they're looking at. Its timer
    fired at once, rather than waited for."""
    assert app.dwell_timer is not None, "a dwell is running"
    app.dwelt(app.dwell_gen)


def terminal_character(key: str) -> str | None:
    """A key's character as Textual's parser gives it from a terminal: an Alt key arrives as Esc
    and its letter (alt+n with "n", alt+shift+l with "L"), a plain letter as itself."""
    if len(key) == 1:
        return key
    if key.startswith("alt+") and len(last := key.rpartition("+")[2]) == 1:
        return last.upper() if "shift+" in key else last
    return None


def raw_keys(app, *keys) -> None:
    """Keys as a terminal sends a burst: all posted at once, with no idle wait between them.
    Pilot.press waits for idle after each key, which hid review 12's bugs: an action key
    ran before the arrow ahead of it had its highlight handled."""
    for key in keys:
        event = events.Key(key, terminal_character(key))
        event.set_sender(app)
        app._driver.send_message(event)


def burst_state(store, sids) -> dict:
    """What a key or button can change, by (session, what): its mode, its sent and queued
    messages, and each item's status, by ref."""
    out = {}
    for s in sids:
        out |= {(s, "mode"): mode(store.session(s)), (s, "sent"): len(store.pending(s)),
                (s, "drafts"): tuple((m["item_ref"], m["body"]) for m in store.drafts(s))}
        out |= {(s, it["ref"]): it["status"] for it in store.items(s)}
    return out


@pytest.mark.anyio
@pytest.mark.parametrize("filtered", [False, True])
@pytest.mark.parametrize("where", ["items", "sessions"])
@pytest.mark.parametrize("burst, hits", [
    (("delete",), True), (("ctrl+s",), True), (("ctrl+t",), True), (("ctrl+j",), False),
    (("ctrl+r",), True), (("ctrl+j", "ctrl+s"), True), ("allow", False),
])
@pytest.mark.both
async def test_a_raw_burst_acts_on_the_cursors_session(store, sid, tmp_path, filtered, where, burst, hits, live):
    """Q35, review 12: Down then at once an action key (or Allow) acts on where the cursor
    went, never on the row it left, from either list, following a session or not: every
    action settles the lists' cursors first. With "typed" in the box for A's permission,
    Down moves the box off it, so Ctrl+Enter has nothing to send and nothing is denied.
    Unfiltered (#16), Down in the session list picks the first session, A, and in the items
    there is no current session: the keys act on the item's. Unfiltered, the inbox orders by
    session name (#236), so B, named first, sits above A's permission and Up goes to it."""
    store.set_mode(sid, "queued")
    other = store.create_session(str(tmp_path), name="aardvark")
    store.set_mode(other, "queued")
    perm = store.post_item(sid, "permission", "Bash: rm -rf build")
    theirs = store.post_item(other, "question", "B's")
    mine = store.post_item(sid, "question", "A's")
    for s, ref, at in ((sid, perm, "10:03"), (other, theirs, "10:02"), (sid, mine, "10:01")):   # newest first
        store.db.execute("UPDATE items SET updated_at = ? WHERE ref = ? AND session_id = ?",
                         (f"2026-10-09T{at}:00+00:00", ref, s))
    store.queue(sid, "A's answer", mine)
    store.queue(other, "B's draft")
    store.queue(other, "B's answer", theirs)
    store.db.execute("UPDATE sessions SET code_version = ?", (PROTOCOL_VERSION,))   # live, on code that queues
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        if filtered:
            app.follow(sid)
            await pilot.pause()
        table = app.items_table
        table.move_cursor(row=table.get_row_index(f"{sid}|{perm}"))
        await pilot.pause()
        app.answer.text = "typed"
        lst = table if where == "items" else app.session_list
        lst.focus()
        await pilot.pause()
        before = burst_state(store, (sid, other))
        arrow = "up" if (where, filtered) == ("items", False) else "down"   # to B's question (#236)
        if burst == "allow":   # a button has no key: the arrow moved, its highlight not handled, then Allow
            getattr(lst, f"action_cursor_{arrow}")()
            app.permission_pressed(Button.Pressed(app.query_one("#allow", Button)))
        else:
            raw_keys(app, arrow, *burst)
        for _ in range(3):
            await pilot.pause()
        if isinstance(app.screen, Confirm):   # Delete on an open question asks first (#151): yes
            await pilot.press("alt+y")
            await pilot.pause()
        desc = f"{arrow.title()}, {burst} from the {where} list, {'following A' if filtered else 'unfiltered'}"
        # the session acted on: following A, Down goes to A's question; unfiltered, Up goes to B and the list's Down picks A
        ctx = sid if (where, filtered) in (("items", True), ("sessions", False)) else other
        if (where, burst) == ("sessions", ("delete",)):   # Stop & Forget on it, its answers queued: asked first
            queued = 2 if ctx == other else 1
            assert isinstance(app.screen, ForgetChoice) and f"({ctx[:6]}," in app.screen.prompt \
                and f"{queued} queued answer(s) will be discarded" in app.screen.prompt, desc
            await pilot.press("escape")
            await pilot.pause()
        target = (sid, mine) if (where, filtered) == ("items", True) else \
            (ctx, theirs) if where == "items" else (ctx, None)
        current = None if (where, filtered) == ("items", False) else ctx
        if burst != ("delete",):   # which closes the item, the highlight going to another
            assert (app.current_session(), app.selected) == (current, target), desc
        assert_one_current(app, desc)
        after = burst_state(store, (sid, other))
        changed = {k for k in after if after[k] != before[k]}
        assert changed <= {(ctx, "mode"), (ctx, "sent"), (ctx, "drafts"), target}, \
            f"{desc}: only the cursor's session and item, got {changed}"
        # Ctrl+R on A's conversation: A has no general message queued to take back
        acted = hits and (where, burst) != ("sessions", ("delete",)) and (burst, target) != (("ctrl+r",), (sid, None))
        assert bool(changed) == acted, \
            f"{desc}: {'acted' if hits else 'nothing to act on'}, got {changed}"
        assert after[(sid, perm)] == "open" and not store._all("SELECT 1 FROM messages WHERE body = 'typed'"), \
            f"{desc}: A's permission untouched, the typed text unsent"


@pytest.mark.anyio
@pytest.mark.parametrize("where, burst, desc", [
    ("sessions", ("down", "delete"), "Forget, pressed by its key (Delete): asks about B"),
    ("items", ("down", "enter"), "Enter: opens B's question full screen"),
])
@pytest.mark.both
async def test_a_raw_burst_opens_what_the_cursor_is_on(store, sid, tmp_path, where, burst, desc):
    """Review 12's probes: a burst ending in a key that opens something opens it for the row
    the cursor went to."""
    mine = store.post_item(sid, "question", "A's")
    with one_second():   # B made and posted in one past second, so A shows first under * (T114)
        other = store.create_session(str(tmp_path), name="other")
        theirs = store.post_item(other, "question", "B's")
    store.db.execute("UPDATE items SET updated_at = ? WHERE session_id = ?", ("2026-10-09T10:00:00+00:00", other))
    same_second(store)   # in creation order under * (T114), however slowly they were made
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        if where == "sessions":   # A selected: unfiltered, Down would pick the first, A (#16)
            await pick(app, pilot, sid)
        app.items_table.move_cursor(row=app.items_table.get_row_index(f"{sid}|{mine}"))
        await pilot.pause()
        (app.items_table if where == "items" else app.session_list).focus()
        await pilot.pause()
        raw_keys(app, *burst)
        for _ in range(3):
            await pilot.pause()
        if where == "items":
            assert isinstance(app.screen, ThreadView) and (app.screen.sid, app.screen.ref) == (other, theirs), desc
        else:
            assert isinstance(app.screen, Confirm) and f"({other[:6]}," in app.screen.prompt, desc
            await pilot.press("alt+n")
            await pilot.pause()
            assert_one_current(app, desc)
        assert app.current_session() == (other if where == "sessions" else None), desc   # unfiltered: none (#16)


@pytest.mark.anyio
@pytest.mark.parametrize("where, burst, selected, dwelt, desc", [
    ("items", ("down", "shift+tab", "down"), None, [], "from the items: Down, Shift+Tab, Down (review 13)"),
    ("sessions", ("down", "tab", "down"), "one", ["B:one"], "from the session list: Down, Tab, Down (review 13)"),
    ("items", ("shift+tab", "down"), None, [], "from the items: Shift+Tab, Down"),
])
@pytest.mark.both
async def test_a_raw_burst_across_both_lists_follows_the_session(store, sid, tmp_path, clock, where, burst, selected,
                                                                   dwelt, desc):
    """Following A, a burst that moves both lists' cursors and ends on session B follows B
    (D22), and ends as the same keys typed slowly do: each key settles the one before it
    (land), so the decision a Down landed on before a Tab moved on is selected, A's in the
    items or B's in the list following B rebuilt, as it would be typed slowly. A pass over a
    decision marks nothing: only the one the burst ends on is seen, once the person has
    rested on it (the dwell)."""
    extra = {"why": "over x: y; undo: z"}
    other = store.create_session(str(tmp_path), name="other")
    with one_second():   # the last posted first: one above two
        for s in (sid, other):
            for title in ("two", "one"):
                store.post_item(s, "decision", title, **extra)
    same_second(store)   # in creation order under * (T114), however slowly they were made
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.follow(sid)
        await pilot.pause()
        store.db.execute("UPDATE items SET status = 'unseen'")
        app.refresh_data()   # the decision seen as it was selected at the start rises again (T71): let it land
        await clock.finish_moves(pilot, app)
        (app.items_table if where == "items" else app.session_list).focus()
        await pilot.pause()
        raw_keys(app, *burst)
        for _ in range(4):
            await pilot.pause()
        names = {sid: "A", other: "B"}
        title = {(s, it["ref"]): it["title"] for s in (sid, other) for it in store.items(s)}
        assert app.current_session() == other, desc
        assert app.selected[0] == other and title.get(app.selected) == selected, f"{desc}: selected {app.selected}"
        def marked():
            return sorted(f"{names[s]}:{it['title']}" for s in (sid, other) for it in store.items(s)
                          if it["status"] != "unseen")
        assert marked() == [], f"{desc}: passed over, nothing seen yet, got {marked()}"
        dwell(app)
        await pilot.pause()
        assert marked() == dwelt, f"{desc}: seen once rested on, got {marked()}"
        assert_one_current(app, desc)


def same_second(store) -> None:
    """Every session made as if in one second, the fixture's too: under * (T114) a second
    between two puts the later first, so a test that counts on creation order holds it."""
    store.db.execute("UPDATE sessions SET created_at = (SELECT min(created_at) FROM sessions)")


def one_second():
    """Items posted inside it share one timestamp, so their order in the inbox is fixed: now()
    stamps whole seconds, so posts that straddle one could land either side of it. A tie goes
    by id in its band's direction (inbox_rank): active ones show the last posted first,
    settled ones the first posted first."""
    return mock.patch("claude_wheelhouse.store.now", lambda: "2026-10-08T10:00:00+00:00")


def raw_click(app, widget, offset, times=1) -> None:
    """A click, or two as a double-click, as the terminal delivers it: MouseDown and MouseUp
    through the app's queue, behind anything sent before them. The app makes the Click."""
    args = _get_mouse_message_arguments(widget, offset, button=1) | {"widget": None}
    for cls in (events.MouseDown, events.MouseUp) * times:
        event = cls(**args)
        event.set_sender(app)
        app._driver.send_message(event)


async def two_lists(clock, tmp_path, where, steps, slow, counts=(2, 2), tick=False) -> dict:
    """Sessions A, followed, and B, each with decisions one and two (or as many of one, two,
    three and four as counts gives), all unseen; then the steps
    from the items or the session list, typed slowly or sent as one raw burst. A step is a key,
    or a click as the terminal delivers it: "click A" or "click B" on that session in the list,
    "click 1" or "double 1" on the items' row 1 (row 0 is the conversation). What they left, by
    name: the thread open, the current session, the selection, the text in the box, what was
    marked seen once the person rested at the end (the dwell: a pass marks nothing) and the
    focus. D22 holds either way. With tick, a raw burst has the refresh tick fall due as each
    key's binding acts, behind what it set in motion, so it fires as the next key lands."""
    store = Store(tmp_path / f"{'slow' if slow else 'raw'}.db")
    with one_second():   # the sessions too: under * (T114) a second between them would put B first
        a, b = (store.create_session(str(tmp_path), name=n) for n in ("alpha", "bravo"))
        for s, count in zip((a, b), counts):
            for title in ("one", "two", "three", "four")[:count]:
                store.post_item(s, "decision", title, why="over x: y; undo: z")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.follow(a)
        await pilot.pause()
        store.db.execute("UPDATE items SET status = 'unseen'")
        app.refresh_data()   # the decision seen as it was selected at the start rises again (T71): let it land
        await clock.finish_moves(pilot, app)
        (app.items_table if where == "items" else app.session_list).focus()
        await pilot.pause()
        if tick and not slow:
            check = app._check_bindings

            async def acting(key, priority=False):
                had = app.focused or app.screen
                done = await check(key, priority)
                if done:   # queued behind what the key set in motion, which the next key's land waits for
                    had.call_later(clock.tick, app)
                return done
            app._check_bindings = acting
        for step in steps:
            how, _, at = step.partition(" ")
            if how in ("click", "double"):   # below the header row
                widget, offset = ((app.session_list, (3, 1 + app.session_list.get_row_index({"A": a, "B": b}[at])))
                                  if at in "AB" else (app.items_table, (20, 1 + int(at))))
                raw_click(app, widget, offset, 2 if how == "double" else 1)
            elif slow:
                await pilot.press(step)
            else:
                raw_keys(app, step)
            if slow:   # one double-click is one gesture: both its clicks go at once either way
                for _ in range(3):
                    await pilot.pause()
        for _ in range(4):
            await pilot.pause()
        if app.dwell_timer is not None:   # the same dwell, raw or slow: a rest at the end
            dwell(app)
            await pilot.pause()
        names = {a: "A", b: "B"}
        title = {(s, it["ref"]): it["title"] for s in (a, b) for it in store.items(s)}

        def label(at):
            return at and f"{names[at[0]]}:{title.get(at, 'conversation')}"
        thread = app.screen if isinstance(app.screen, ThreadView) else None
        assert_one_current(app, f"{'slow' if slow else 'raw'} {steps}")
        return {"thread": thread and label((thread.sid, thread.ref)), "current": names.get(app.current_session()),
                "selected": label(app.selected), "box": (thread.box if thread else app.answer).text,
                "focus": app.focused and app.focused.id,
                "seen": sorted(label((s, it["ref"])) for s in (a, b) for it in store.items(s)
                               if it["status"] != "unseen")}


@pytest.mark.anyio
@pytest.mark.parametrize("where, keys, desc", [
    ("sessions", ("down", "tab", "down"), "from the session list: Down, Tab, Down"),
    ("sessions", ("down", "tab", "down", "down"), "from the session list: Down, Tab, Down, Down"),
    ("sessions", ("down", "tab", "down", "enter"), "from the session list: Down, Tab, Down, Enter opens B's"),
    ("items", ("down", "shift+tab", "down"), "from the items: Down, Shift+Tab, Down"),
    ("items", ("down", "down", "down"), "from the items: Down, Down, Down"),
    ("items", ("down", "down", "shift+tab", "down", "tab", "down"), "from the items: across and back"),
    ("items", ("down", "shift+tab", "down", "tab", "enter"), "from the items: across, back, Enter opens B's"),
])
@pytest.mark.both
async def test_a_raw_burst_across_both_lists_ends_as_typed_slowly(tmp_path, clock, where, keys, desc):
    """Review 17: every key settles what the ones before it moved before it acts (land), so a
    burst ends where the same keys typed slowly do, and never opens A's decision while B is
    current."""
    raw = await two_lists(clock, tmp_path / "raw", where, keys, slow=False)
    slow = await two_lists(clock, tmp_path / "slow", where, keys, slow=True)
    assert raw == slow, f"{desc}: raw {raw}, slow {slow}"
    ticked = await two_lists(clock, tmp_path / "ticked", where, keys, slow=False, tick=True)
    assert ticked == slow, f"{desc}: the refresh tick falling due as the keys land (tick), {ticked}"


@pytest.mark.anyio
@pytest.mark.parametrize("where, steps, desc", [
    ("items", ("click B", "tab", "down"), "a click on B, Tab, Down: B's first decision selected"),
    ("items", ("click B", "tab", "down", "enter"), "a click on B, Tab, Down, Enter: opens B's, not A's"),
    ("items", ("click B", "tab", "enter"), "a click on B, Tab, Enter: B's conversation, its box"),
    ("items", ("double 2", "o", "k"), "a double-click on A's two opens it: o k is typed in its thread"),
    ("sessions", ("double 2", "o", "k"), "a double-click on A's two from the session list: o k in its thread"),
    ("items", ("double 1", "escape", "down"), "a double-click on A's one, Esc, Down: the thread shut, two selected"),
    ("sessions", ("double 1", "escape", "down"), "the same from the session list"),
    ("items", ("double 2", "tab", "x"), "a double-click on A's two, Tab, x: in its thread"),
    ("items", ("click 1", "enter", "o", "k"), "a click on A's one, Enter opens it, o k in its thread"),
    ("items", ("click 1", "down", "enter"), "a click on A's one, Down, Enter opens two"),
])
@pytest.mark.both
async def test_a_click_then_a_raw_burst_ends_as_typed_slowly(tmp_path, clock, monkeypatch, where, steps, desc):
    """Review 17: a click's selection is handled before the keys typed after it (land runs it),
    not behind them, where it followed B again and undid what they did. Review 18: so is a
    double-click's on an item, which opens it. Running sessions: a click on a dead one offers
    a relaunch, which the keys would answer."""
    from claude_wheelhouse import liveness
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "live")
    raw = await two_lists(clock, tmp_path / "raw", where, steps, slow=False)
    slow = await two_lists(clock, tmp_path / "slow", where, steps, slow=True)
    assert raw == slow, f"{desc}: raw {raw}, slow {slow}"


@pytest.mark.anyio
@pytest.mark.parametrize("where, steps, desc", [
    ("items", ("down", "click B"), "Down on A's items, a click on B: A's one passed over, unseen"),
    ("items", ("down", "down", "click B"), "Down, Down on A's items, a click on B: both passed over"),
    ("items", ("down", "click A"), "Down on A's items, a click on A: its one stays selected"),
    ("items", ("enter", "click B"), "Enter on A's conversation, a click on B"),
    ("sessions", ("down", "click 1"), "Down in the session list, a click on an item: of the session Down reached"),
    ("sessions", ("down", "click 2"), "Down in the session list, a click on its second item"),
])
@pytest.mark.both
async def test_a_raw_burst_then_a_click_ends_as_typed_slowly(tmp_path, clock, monkeypatch, where, steps, desc):
    """Review 18: a click settles what the keys before it moved, as a key does (land), with no
    key behind it to: else the Down's decision was never selected, or the items' click was
    on the session the list left."""
    from claude_wheelhouse import liveness
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "live")
    raw = await two_lists(clock, tmp_path / "raw", where, steps, slow=False)
    slow = await two_lists(clock, tmp_path / "slow", where, steps, slow=True)
    assert raw == slow, f"{desc}: raw {raw}, slow {slow}"


@pytest.mark.anyio
@pytest.mark.parametrize("steps, desc", [
    (("down", "click 4"), "Down to B, a click on a row only A's longer list had: nothing, no IndexError"),
    (("down", "double 4"), "the same as a double-click: nothing opens, the cursor not clamped onto B's last row"),
    (("down", "click 3"), "Down to B, a click on another row B's list doesn't have"),
    (("down", "click 1"), "Down to B, a click on a row B's list has too: B's one"),
    (("down", "double 1"), "Down to B, a double-click on B's one opens it"),
])
@pytest.mark.both
async def test_a_raw_burst_then_a_click_on_a_shorter_list_ends_as_typed_slowly(tmp_path, clock, monkeypatch, steps,
                                                                                desc):
    """Review 19: a click's settle can follow another session and refill the items, so the
    row it was on may be gone: as typed slowly, where the click lands below the rows, it
    does nothing."""
    from claude_wheelhouse import liveness
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "live")
    raw = await two_lists(clock, tmp_path / "raw", "sessions", steps, slow=False, counts=(4, 1))
    slow = await two_lists(clock, tmp_path / "slow", "sessions", steps, slow=True, counts=(4, 1))
    assert raw == slow, f"{desc}: raw {raw}, slow {slow}"


@pytest.mark.anyio
@pytest.mark.parametrize("keys, desc", [
    (("ctrl+q", "alt+a"), "Ctrl+Q then Alt+A: no Adopt dialog opening as the app shuts down"),
    (("ctrl+q", "down", "tab", "enter"), "Ctrl+Q then a burst that opens a thread"),
])
async def test_keys_after_quit_are_dropped(store, sid, keys, desc):
    """Review 17: once Q has the app exiting, the keys typed after it don't act."""
    store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(store)
    opened = []
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.items_table.focus()
        await pilot.pause()
        original = app.push_screen
        app.push_screen = lambda screen, *a, **kw: opened.append(type(screen).__name__) or original(screen, *a, **kw)
        raw_keys(app, *keys)
        for _ in range(6):
            await pilot.pause()
    assert app.return_code == 0 and opened == [], f"{desc}: opened {opened}"


@pytest.mark.anyio
async def test_a_resize_runs_under_the_clock_fixture(store, sid, clock):
    """Review 49: the clock fixture refuses only the app's own timers it doesn't hold. It also
    refused Textual's resize timer (App._check_resize), so a resize under it raised."""
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        await pilot.resize_terminal(120, 40)
        await pilot.pause()
        assert app.size.width == 120


@pytest.mark.anyio
@pytest.mark.parametrize("ticks, desc", [
    (False, "a shutdown that is over before the next tick"),
    (True, "a shutdown that takes over a second: the tick fires once the screens are gone, and does nothing"),
])
async def test_the_refresh_tick_lets_the_app_shut_down(store, sid, monkeypatch, clock, ticks, desc):
    """Review 18: Textual stops the timers only after it has closed the screens, so under load
    the refresh tick can fire with none left, and painting raised ScreenStackError."""
    from textual.app import App
    close_all = App._close_all

    async def slow_close(self):
        await close_all(self)
        if ticks:   # the rest of the app closing, the tick falling due meanwhile
            clock.tick(self)
            clock.animate(self)
            clock.animate(self)   # two frames: one of them even, where the guarded branch runs
    monkeypatch.setattr(App, "_close_all", slow_close)
    app = WheelhouseApp(store)
    try:
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
    except Exception as e:
        pytest.fail(f"{desc}: {e!r}")


class Burst:
    """What a burst left behind, by the names its cases use: sessions alpha and bravo, both
    running in the wheelhouse (so every button applies) with an answer queued; alpha's
    question qa and permission pa, bravo's question qb."""

    def __init__(self, app, store, sids: dict, refs: dict):
        self.app, self.store, self.sids, self.refs = app, store, sids, refs
        self.keys = {f"{sids['bravo' if name == 'qb' else 'alpha']}|{ref}": name for name, ref in refs.items()}
        self.at = {name: key.split("|") for key, name in self.keys.items()}   # (session, ref)

    def status(self, name: str) -> str:
        return self.store.item(*self.at[name])["status"]

    def answer(self, name: str) -> list[str]:
        """What was queued or sent to the item: these sessions run older code, so Ctrl+Enter sends."""
        return [m["body"] for m in self.store._all("SELECT body FROM messages WHERE session_id = ? AND item_ref = ?",
                                                     tuple(self.at[name]))]

    def sent(self, who: str) -> int:
        return len(self.store.pending(self.sids[who]))

    def requested(self) -> str | None:
        """The session asked to park or forget, and which: who:what."""
        return next((f"{who}:{what}" for who, sid in self.sids.items() for what in ("park", "end")
                     if self.store.session(sid)[f"{what}_requested_at"]), None)

    def command(self, who: str) -> str | None:
        return self.store.session(self.sids[who])["host_command"]

    def marked(self) -> set[str]:
        return {self.keys[k] for k in self.app.items_table.marked}

    def cursor(self) -> str:
        return self.keys[self.app.items_table.cursor_key()]

    def focus(self) -> str | None:
        return focused_id(self.app)

    def asked(self) -> str | None:
        """The session the dialog in front asks about, by name: Rename's box holds it, a
        Confirm's prompt names it, as Compact's and Park's dialogs do."""
        s = self.app.screen
        text = (s.current if isinstance(s, RenameSession) else s.prompt if isinstance(s, (Confirm, ForgetChoice))
                else s.name_ if isinstance(s, (CompactSession, ParkSession)) else "")
        return next((who for who in self.sids if who in text), None)


@pytest.mark.anyio
@pytest.mark.parametrize("focus, at, burst, seen, want, desc", [
    ("answer", "qa", ("a", "b", "backspace", "ctrl+enter"), lambda b: (b.answer("qa"), b.status("qa"), b.focus()),
     (["a"], "open", "answer"), "review 14 (1): Backspace edits the box, then Ctrl+Enter sends it; the question stays open"),
    ("items", "qa", ("delete", "alt+y", "down"), lambda b: (b.status("qa"), b.status("qb")), ("closed", "open"),
     "review 14 (2): Delete closes the question it was on, once confirmed (an open one), not the next"),
    ("items", "qa", ("ctrl+s", "down"), lambda b: (b.sent("alpha"), b.sent("bravo")), (1, 0),
     "review 14 (3): Ctrl+S sends the queue of the session it was on, not the next row's"),
    ("items", "qa", ("ctrl+space", "down"), lambda b: b.marked(), {"qa"}, "review 14 (4): Ctrl+Space marks the row it was on"),
    ("items", "pa", ("alt+y", "tab"), lambda b: (b.status("pa"), b.focus()), ("allowed", "answer"),
     "review 14 (5): Alt+Y allows the permission, then Tab goes to the box"),
    ("lines", "qb", ("up", "shift+tab"), lambda b: (b.app.answer.cursor_location, b.cursor(), b.focus()),
     ((0, 5), "qb", "items"), "review 14 (6): Up moves the box's cursor, not the items', then Shift+Tab goes to them"),
    ("session-list", "qa", ("alt+r", "down"), lambda b: b.asked(), "alpha", "Alt+R renames the session it was on"),
    ("session-list", "qa", ("alt+p", "down"), lambda b: b.asked(), "alpha",
     "Alt+P on a session run in the wheelhouse asks first, about the session it was on (Compact first)"),
    ("session-list", "qa", ("delete", "down"), lambda b: b.asked(), "alpha",
     "Delete on the session it was on asks first (Let it know or Just stop it), its queued answer at stake"),
    ("items", "qa", ("alt+i", "down"), lambda b: (b.command("alpha"), b.command("bravo")), ("interrupt", None),
     "Alt+I interrupts the session of the item it was on"),
    ("items", "qa", ("alt+c", "down"), lambda b: b.asked(), "alpha", "Alt+C compacts the session of the item it was on"),
])
@pytest.mark.both
async def test_a_raw_burst_acts_in_the_order_typed(store, tmp_path, monkeypatch, sdk_on, focus, at, burst, seen, want,
                                                   desc):
    """Review 14: each key acts as it arrives, on what has focus then, so no key overtakes
    the one typed before it: a later key that moves the focus or a cursor never changes what
    an earlier one acts on."""
    from claude_wheelhouse import liveness
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "live")
    sids = {who: store.create_session(str(tmp_path), name=who, runner="sdk") for who in ("alpha", "bravo")}
    refs = {}
    for name, who, kind, at_time in (("qa", "alpha", "question", "10:03"), ("qb", "bravo", "question", "10:02"),
                                     ("pa", "alpha", "permission", "10:01")):
        refs[name] = store.post_item(sids[who], kind, name)
        store.db.execute("UPDATE items SET updated_at = ? WHERE session_id = ? AND ref = ?",
                         (f"2026-10-09T{at_time}:00+00:00", sids[who], refs[name]))
    for who, s in sids.items():
        store.queue(s, f"{who}'s queued")
    app = WheelhouseApp(store)
    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        b = Burst(app, store, sids, refs)
        table = app.items_table
        assert [b.keys.get(k, "💬") for k in table.keys()] == ["💬", "💬", "qa", "pa", "qb"], desc   # by session name, then newest (#236)
        if burst[0] in ("alt+r", "alt+p", "delete", "alt+i", "alt+c", "alt+h"):   # a session's keys: alpha selected first (#16)
            await pick(app, pilot, sids["alpha"])
        table.move_cursor(row=table.get_row_index(next(k for k, name in b.keys.items() if name == at)))
        await pilot.pause()
        app.query_one("#answer" if focus == "lines" else f"#{focus}").focus()
        await pilot.pause()
        if focus == "lines":
            app.answer.text = "line1\nline2"
            app.answer.move_cursor((1, 5))
        raw_keys(app, *burst)
        for _ in range(6):
            await pilot.pause()
        assert seen(b) == want, f"{desc}: got {seen(b)}"
        if isinstance(app.screen, (RenameSession, Confirm, CompactSession, ParkSession, ForgetChoice)):
            await pilot.press("escape")
            await pilot.pause()
        assert_one_current(app, desc)


@pytest.mark.anyio
@pytest.mark.parametrize("focus, at, burst, want, desc", [
    ("items", "qa", ("enter", "h", "i"), ("ThreadView", "hi", [], False, "park"),
     "review 15 (1): Enter opens the item, and the letters behind it are typed in its box, not H's Shell or I's Interrupt"),
    ("items", "qa", ("enter", "x", "y", "q"), ("ThreadView", "xyq", [], False, "park"),
     "review 15 (1): Q behind Enter is typed in the item's box, it doesn't quit"),
    ("items", "qa", ("enter", "q"), ("ThreadView", "q", [], False, "park"), "review 15 (1): raw Enter Q doesn't quit"),
    ("items", "conversation", ("enter", "h", "i"), ("Screen", "hi", [], False, "park"),
     "Enter on the conversation row goes to its box, and the letters behind it land there"),
    ("session-list", "gone", ("enter", "alt+n", "ctrl+q"), ("Screen", "", [], True, "park"),
     "Enter on a dead session offers a relaunch: Alt+N declines it, then Ctrl+Q quits"),
    ("session-list", "alpha", ("alt+p", "alt+c", "ctrl+q"), ("Screen", "", [], True, None),
     "review 15 (2): Alt+P on a session asked to park offers the Choice, Alt+C cancels the request, then Ctrl+Q quits"),
    ("session-list", "alpha", ("alt+p", "alt+c", "alt+c"), ("CompactSession", "", [], False, None),
     "review 15 (2): a second Alt+C once the Choice has closed is the inbox's: Compact"),
    ("items", "qa", ("f1", "f1"), ("Screen", "", [], False, "park"),
     "F1 opens the keys, and the F1 behind it closes them"),
])
@pytest.mark.both
async def test_a_key_that_opens_something_takes_the_keys_behind_it(store, tmp_path, monkeypatch, focus, at, burst,
                                                                    want, desc):
    """Review 15: a key that opens a screen or moves the focus does so before the next key
    acts, so the keys typed behind it go to what it opened, in order, and a dialog's key
    typed twice closes it once."""
    out = await burst_outcome(store, tmp_path, monkeypatch, focus, at, burst, desc)
    seen = (out["stack"][-1], out["box"], out["hosts"], out["quit"], out["pending"])
    assert seen == want, f"{desc}: got {seen}"
    assert out["launched"] == [], desc


async def burst_outcome(store, tmp_path, monkeypatch, focus, at, burst, desc, slow=False) -> dict:
    """A burst typed on an inbox of alpha, run in the wheelhouse and asked to park, with
    question qa, and gone, a dead session: raw, as a terminal sends it, or slow, each key
    landing before the next is pressed (Pilot.press). What it left behind, by name: the
    screens, the focus, the box in front's text, what was asked of the sessions and the app."""
    from claude_wheelhouse import liveness
    alpha = store.create_session(str(tmp_path), name="alpha", runner="sdk")
    gone = store.create_session(str(tmp_path), name="gone", runner="sdk")
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "dead" if s["id"] == gone else "live")
    qa = store.post_item(alpha, "question", "which db?")
    store.request(alpha, "park")
    store.db.execute("UPDATE sessions SET park_requested_at = ?", ("2020-01-01T00:00:00.000000+00:00",))   # asked long ago: P reaches Cancel or Force (#107)
    hosts, exits, launched = [], [], []
    monkeypatch.setattr(WheelhouseApp, "host_command", lambda self, sid, what: hosts.append(what))
    monkeypatch.setattr(WheelhouseApp, "exit", lambda self, *a, **k: exits.append(True))
    monkeypatch.setattr(Hub, "open_session",
                        lambda self, sid, restore=False, **k: launched.append(store.session(sid)["name"]) or Done())
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        if focus == "items":
            app.session_list.move_cursor(row=app.session_list.get_row_index(alpha))
            await pilot.pause()
            app.follow(alpha)
            app.items_table.move_cursor(row=app.items_table.keys().index(f"{alpha}|{qa if at == 'qa' else ''}"))
        else:   # selected, as a click: with none, the list's keys do nothing (#16)
            app.follow(alpha if at == "alpha" else gone)
        await pilot.pause()
        app.query_one(f"#{focus}").focus()
        await pilot.pause()
        if slow:
            for key in burst:
                await pilot.press(key)
                await pilot.pause()
        else:
            raw_keys(app, *burst)
        for _ in range(10):
            await pilot.pause()
        s = store.session(alpha)
        current = app.current_session()
        out = {"stack": tuple(type(screen).__name__ for screen in app.screen_stack), "focus": focused_id(app),
               "box": app.screen.box.text if isinstance(app.screen, ThreadView) else app.answer.text,
               "unsent": sorted(app.unsent.values()), "hosts": hosts, "quit": bool(exits), "launched": launched,
               "names": sorted(row["name"] for row in store.sessions()),
               "pending": s and app.pending(alpha), "parked": bool(s and s["parked"]),
               "queued": [m["body"] for m in store.drafts(alpha)] + [m["body"] for m in store.pending(alpha)],
               "current": current and store.session(current)["name"], "finished": app.show_finished,
               "model": s and s["model"], "effort": s and s["effort"],
               "open": [it["title"] for it in store.items(alpha) if it["status"] == "open"] if s else []}
        if isinstance(app.screen, ThreadView) or len(app.screen_stack) == 1:
            assert_one_current(app, desc)
    return out


@pytest.mark.usefixtures("sdk_on")
@pytest.mark.anyio
@pytest.mark.parametrize("focus, at, burst, want, desc", [
    ("items", "qa", ("delete", "enter", "alt+y"), {"stack": ("Screen",), "open": ["which db?"]},
     "review 16 (1): Delete's Confirm opens on No, so Enter answers No, and the Y behind it isn't a Yes"),
    ("items", "qa", ("delete", "enter", "ctrl+q"), {"stack": ("Screen",), "open": ["which db?"], "quit": True},
     "review 16 (1): Enter on No answers No, then Q quits"),
    ("session-list", "gone", ("enter", "enter", "alt+y"), {"stack": ("Screen",), "launched": []},
     "review 16 (1): Relaunch's Confirm opens on No, so Enter declines, and the Y behind it doesn't relaunch"),
    ("session-list", "alpha", ("alt+p", "enter", "alt+o"), {"stack": ("Screen",), "pending": None, "parked": False},
     "review 16 (1): the Choice opens on Cancel request: Enter cancels the request, and the F behind it isn't Force"),
    ("session-list", "alpha", ("alt+p", "enter", "escape"), {"stack": ("Screen",), "pending": None},
     "review 16 (1): Enter cancels the request, and the Esc behind it isn't Leave it"),
    ("session-list", "alpha", ("alt+r", "x", "enter", "ctrl+q"), {"stack": ("Screen",), "names": ["gone", "x"], "quit": True},
     "review 16 (1): Rename takes X, Enter renames to it, then Q quits"),
    ("session-list", "alpha", ("alt+r", "enter", "enter"), {"stack": ("Screen",), "names": ["alpha", "gone"]},
     "review 16 (2): Enter closes Rename once, and the second Enter is the session list's"),
    ("session-list", "alpha", ("alt+r", "x", "enter", "enter"), {"stack": ("Screen",), "names": ["gone", "x"]},
     "review 16 (2): X Enter renames, and the second Enter is the session list's"),
    ("session-list", "alpha", ("alt+n", "tab", "enter", "escape"), {"stack": ("Screen", "NewSession"), "focus": "browse"},
     "review 16 (3): Enter on Browse opens the picker, and the Esc behind it closes the picker, not New session"),
    ("session-list", "alpha", ("alt+p", "alt+o", "alt+y"), {"stack": ("Screen",), "parked": True},
     "F on the Choice asks to confirm Force, and the Y behind it confirms"),
    ("session-list", "alpha", ("alt+p", "alt+c", "alt+p"), {"stack": ("Screen", "ParkSession"), "pending": None},
     "C cancels the request before the P behind it, which asks afresh: Park's dialog, as alpha runs in the wheelhouse"),
    ("session-list", "alpha", ("alt+m", "down", "enter"), {"stack": ("Screen",), "model": "claude-sonnet-5-5"},
     "M opens Model, Down moves to the next model before Enter takes it"),
    ("items", "qa", ("alt+m", "enter", "ctrl+q"), {"stack": ("Screen",), "model": "claude-opus-5-5", "quit": True},
     "Model's Enter takes the highlighted model, then the Q behind it quits"),
    ("session-list", "alpha", ("alt+m", "escape", "alt+m", "escape"), {"stack": ("Screen",), "model": None},
     "Esc leaves the model as it was, each time"),
    ("stats", "alpha", ("right", "right", "left"), {"effort": "xhigh", "focus": "stats"},
     "the effort slider: each arrow moves on from where the last left it"),
    ("stats", "alpha", ("end", "left", "home", "right"), {"effort": "medium"}, "Home and End, then arrows"),
])
@pytest.mark.both
async def test_a_key_lands_before_the_next_acts(store, tmp_path, monkeypatch, focus, at, burst, want, desc):
    """Review 16: what a key sets in motion lands before the key behind it acts: the press
    a focused button's Enter posts, the Submitted an input's, the screen it opens or closes,
    and the dialog's answer, so a key typed behind it never answers in its place."""
    out = await burst_outcome(store, tmp_path, monkeypatch, focus, at, burst, desc)
    assert {k: out[k] for k in want} == want, f"{desc}: got {out}"


@pytest.mark.anyio
@pytest.mark.parametrize("focus, at, burst, desc", [
    ("items", "qa", ("alt+h", "enter", "alt+y"), "a focused button's Enter, then a dialog key"),
    ("session-list", "alpha", ("alt+p", "enter", "escape"), "the Choice's Enter, then Esc"),
    ("session-list", "alpha", ("alt+p", "alt+o", "alt+y"), "a dialog's answer that opens another"),
    ("session-list", "alpha", ("alt+p", "alt+c", "alt+p"), "a dialog's answer, then a key that reads what it did"),
    ("session-list", "alpha", ("alt+r", "x", "enter", "ctrl+q"), "an input's Enter, then a key on the screen behind"),
    ("session-list", "alpha", ("alt+r", "x", "enter", "enter"), "an input's Enter twice"),
    ("session-list", "alpha", ("alt+n", "tab", "enter", "escape", "tab", "a"), "the picker opened and closed by keys"),
    ("session-list", "alpha", ("alt+n", "a", "b", "escape", "ctrl+q"), "typing in a dialog, then closing it"),
    ("session-list", "gone", ("enter", "enter", "alt+y"), "Enter on a dead session's relaunch offer declines it"),
    ("items", "qa", ("enter", "x", "escape", "tab", "y"), "a thread left with Esc, then the inbox's box"),
    ("items", "qa", ("tab", "a", "b", "backspace", "ctrl+enter", "down"), "typing, editing and sending"),
    ("items", "qa", ("f1", "f1", "ctrl+q"), "F1 opens and closes the keys, then Ctrl+Q quits"),
    ("session-list", "alpha", ("alt+m", "down", "down", "enter", "alt+m", "escape"), "Model chosen by keys, opened again"),
    ("stats", "alpha", ("right", "right", "left", "tab", "right"), "the slider's arrows, then Tab on round"),
])
@pytest.mark.both
async def test_a_raw_burst_ends_as_the_same_keys_typed_slowly(store, tmp_path, monkeypatch, focus, at, burst, desc):
    """Type-ahead: a burst sent as a terminal sends it ends where the same keys, each landing
    before the next is pressed, do."""
    from claude_wheelhouse.store import Store
    raw = await burst_outcome(store, tmp_path, monkeypatch, focus, at, burst, desc)
    (tmp_path / "slow").mkdir()
    slow = await burst_outcome(Store(tmp_path / "slow" / "wheelhouse.db"), tmp_path, monkeypatch, focus, at, burst,
                               desc, slow=True)
    assert raw == slow, f"{desc}: raw {raw}, slow {slow}"


@pytest.mark.anyio
@pytest.mark.parametrize("focus, desc", [
    ("items", "on the items"),
    ("session-list", "on the session list"),
])
async def test_escape_clears_a_text_selection_before_the_filter(store, sid, tmp_path, focus, desc):
    """Review 15 (3): Esc on the inbox clears a mouse selection when there is one, and
    only then, pressed again, shows all sessions."""
    from textual.selection import Selection
    store.create_session(str(tmp_path), name="other")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.follow(sid)
        app.query_one(f"#{focus}").focus()
        await pilot.pause()
        app.screen.selections = {app.detail: Selection(None, None)}
        raw_keys(app, "escape")
        await pilot.pause()
        assert (bool(app.screen.selections), app.filter_sid) == (False, sid), f"{desc}: the first Esc clears the selection"
        raw_keys(app, "escape")
        await pilot.pause()
        assert app.filter_sid is None, f"{desc}: the second clears the filter"


@pytest.mark.anyio
@pytest.mark.parametrize("filtered, desc", [
    (True, "the followed session (review 10)"),
    (False, "the session whose item was selected"),
])
async def test_a_session_ending_elsewhere_with_nothing_left_clears_the_pane(store, sid, tmp_path, filtered, desc):
    """Its conversation or item isn't left in the pane, nor the answer box aimed at it: the
    other session's conversation row, all that's left, takes the highlight (#192)."""
    other = store.create_session(str(tmp_path), name="other")
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
        assert (app.selected, app.box_target) == ((other, None), (other, None)), desc
        assert "other · conversation" in app._detail_text and app.answer.text == "", desc
        assert_one_current(app, desc)


@pytest.mark.anyio
@pytest.mark.parametrize("item_arrives, desc", [
    (False, "a refresh that changes no rows"),
    (True, "a refresh that rebuilds the list"),
])
@pytest.mark.both
async def test_a_refresh_before_the_persons_highlight_lands(store, sid, item_arrives, desc):
    """The items' cursor follows the selection by key on every paint (review 11, bug 1), but
    not over an arrow whose highlight is still on its way to settle: it isn't lost."""
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
        await pilot.press("enter", "alt+n")   # it isn't running: decline the offer to relaunch it
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
        assert [r.key.value.split("|")[0] for r in app.items_table.ordered_rows] == [other, other], \
            "the inbox, all of it: B's question and its conversation"


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


def long_conversation(sid, tmp_path, monkeypatch, turns: int = 200, twice: bool = False, tools: int = 0) -> None:
    """A conversation many pages long, with markdown in every block (twice: the person's
    turns alone, each but the first said twice over; tools: that many of the same tool line
    before the last four turns)."""
    folder = tmp_path / "projects/-home-u-repo"
    folder.mkdir(parents=True)
    recs = [{"type": "user" if i % 2 or twice else "assistant", "timestamp": "2026-10-07T21:30:00Z",
             "message": {"content": f"para one {(i + 1) // 2 if twice else i}\n\npara two\n\n- a\n- b"}
             if i % 2 or twice else
             {"content": [{"type": "text", "text": f"reply {i}\n\n```\ncode\n```\n\nmore"}]}} for i in range(turns)]
    recs[-4:-4] = [{"type": "assistant", "timestamp": "2026-10-07T21:30:00Z", "message": {"content": [
        {"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {"command": "gh pr checks"}}]}}
        for i in range(tools)]
    folder.joinpath(f"{sid}.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    monkeypatch.setattr(transcript, "PROJECTS", tmp_path / "projects")


def laid_in_full(app) -> list[str]:
    """The conversation pane's lines, by text, as laying out every block at once gives them."""
    width = app.detail.size.width
    return [t for who, md in app.detail.blocks for t in tui._block_strips(who, md, width, app.console)[1]]


def painted(app) -> list[str]:
    """What the conversation pane shows on screen, line by line."""
    x, width, r = app.detail.region.x, app.detail.size.width, app.detail_scroll.scrollable_content_region
    strips = app.screen._compositor.render_strips()
    return [strips[y].crop(x, x + width).text.rstrip() for y in range(r.y, r.bottom)]


@pytest.mark.anyio
async def test_a_long_conversation_is_one_widget(store, sid, tmp_path, monkeypatch):
    long_conversation(sid, tmp_path, monkeypatch)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.follow(sid)
        await pilot.pause()
        assert "para one 199" in app._detail_text
        assert len(app.detail.children) == 0, "drawn as one renderable: a widget per paragraph made each keypress slow"


# what the conversation pane's keys did while it was a Tab stop: its scroll's actions now (#77)
SCROLLS = {"home": "scroll_home", "pageup": "page_up", "up": "scroll_up"}


async def scroll_by(pilot, keys) -> list[list[str]]:
    """What the conversation pane shows as it settles, then after each key's scroll."""
    app = pilot.app

    async def settled() -> list[str]:
        await pilot.wait_for_scheduled_animations()
        await pilot.pause()
        return painted(app)

    seen = [await settled()]
    for key in keys:
        getattr(app.detail_scroll, f"action_{SCROLLS[key]}")()
        seen.append(await settled())
    return seen


@pytest.mark.anyio
async def test_home_takes_a_long_conversation_to_its_top(store, sid, tmp_path, monkeypatch):
    long_conversation(sid, tmp_path, monkeypatch)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.follow(sid)
        await pilot.pause()
        seen = await scroll_by(pilot, ["home"])
        full, page = laid_in_full(app), app.detail_scroll.size.height
        assert seen[0] == full[-page:], "first shown at its end"
        assert seen[-1] == full[:page], "Home, straight to the top"


def append_turn(sid, tmp_path, text: str) -> None:
    rec = {"type": "user", "timestamp": "2026-10-07T21:31:00Z", "message": {"content": text}}
    with (tmp_path / f"projects/-home-u-repo/{sid}.jsonl").open("a") as f:
        f.write(json.dumps(rec) + "\n")


def start_waiting(sid, tmp_path) -> None:
    """The session asks in its tab, which puts a warning under the conversation's head."""
    rec = {"type": "assistant", "timestamp": "2026-10-07T21:31:00Z", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": next(iter(transcript.ASKS_IN_TAB)), "input": {}}]}}
    with (tmp_path / f"projects/-home-u-repo/{sid}.jsonl").open("a") as f:
        f.write(json.dumps(rec) + "\n")


def stream_into(sid, tmp_path, app, below: int) -> None:
    """The reply at the top of the view, or below that many blocks under it, grows in place as
    it streams, and a new turn drops the oldest off."""
    path = tmp_path / f"projects/-home-u-repo/{sid}.jsonl"
    recs = [json.loads(line) for line in path.read_text().splitlines()]
    top = bisect.bisect_right(app.detail._layout(app.detail.size.width)[2], app.detail_scroll.scroll_y) - 1
    i = int(re.search(r"(?:para one|reply) (\d+)", app.detail.blocks[top + below][1])[1])
    content = recs[i]["message"]["content"]
    grown = "\n\n".join(f"streamed {k}" for k in range(9))
    if isinstance(content, str):
        recs[i]["message"]["content"] = f"{content}\n\n{grown}"
    else:
        content[0]["text"] += f"\n\n{grown}"
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    append_turn(sid, tmp_path, "the newest turn")


UP, NEAR_END = ("pageup",) * 3, ("up",) * 3


@pytest.mark.anyio
@pytest.mark.parametrize("change, keys, shape, desc", [
    ("turn", (), {}, "a new turn, followed at its end: it shows at the true end"),
    ("turn", UP, {}, "a new turn, the oldest dropping off, scrolled up to read: what shows stays put"),
    ("rename", UP, {}, "renamed, scrolled up to read: what shows stays put, not sent to the end"),
    ("long name", NEAR_END, {}, "renamed at length, 3 lines from the end: kept once laid out, past the old end"),
    ("shell", UP, {}, "handed to a shell, its tab line changed, scrolled up to read: what shows stays put"),
    ("waiting", UP, {"turns": 60}, "a warning put in under its head, scrolled up to read: what shows stays put"),
    ("waiting", UP, {"turns": 60, "twice": True}, "a warning put in, each block said twice: what shows stays put"),
    ("turn", ("pageup",), {"tools": 60}, "a new turn, the view's top among the same tool line 60 times: kept on its own"),
    ("streamed below", UP, {}, "the block under the view's top grows as it streams: the reader's lines stay put, "
     "what grew shows below them"),
    ("streamed", UP, {}, "the block at the view's top grows as it streams: the reader's lines stay put, what grew "
     "shows below them"),
])
@pytest.mark.parametrize("width", [160, 100])   # the lines each block takes, and so the shift, differ
@pytest.mark.both
async def test_a_change_to_a_long_conversation(store, sid, tmp_path, monkeypatch, change, keys, shape, desc, width):
    """A reader scrolled up keeps their place across a change; one at the end stays there.
    waiting: under the 80 entries shown, so no oldest turn drops off to undo the warning's shift."""
    if change == "shell":   # a host's session, whose tab line shows only once handed to a shell
        sid = store.create_session(str(tmp_path), name="demo", runner="sdk")
    long_conversation(sid, tmp_path, monkeypatch, **shape)
    app = WheelhouseApp(store)
    async with app.run_test(size=(width, 40)) as pilot:
        await pilot.pause()
        app.follow(sid)
        await pilot.pause()
        before = (await scroll_by(pilot, keys))[-1]
        starts, y = app.detail._layout(app.detail.size.width)[2], int(app.detail_scroll.scroll_y)
        rows = [at - y for at in starts[bisect.bisect_right(starts, y):][:2]]   # the next two blocks' on screen
        grown = {"streamed": rows[0], "streamed below": rows[1]}.get(change)   # where the grown block ended
        kept = slice(grown) if grown else slice(None)
        head = app.detail.blocks[:2]
        {"turn": lambda: append_turn(sid, tmp_path, "the newest turn"),
         "rename": lambda: store.rename(sid, "renamed"),
         "long name": lambda: store.rename(sid, "renamed " + "at length " * 100),
         "shell": lambda: store.set_shell(sid, "open"),
         "waiting": lambda: start_waiting(sid, tmp_path),
         "streamed below": lambda: stream_into(sid, tmp_path, app, 1),
         "streamed": lambda: stream_into(sid, tmp_path, app, 0)}[change]()
        app.refresh_data()
        await pilot.wait_for_scheduled_animations()
        await pilot.pause()
        assert app.detail.blocks[:2] != head or change in ("turn", "streamed below", "streamed"), f"{desc}: the change made"
        full = laid_in_full(app)
        assert painted(app)[kept] == (full[-app.detail_scroll.size.height:] if not keys else before)[kept], desc
        assert ("the newest turn" in "\n".join(painted(app))) == (not keys), desc
        assert grown is None or painted(app)[grown:grown + 1] == ["streamed 0"], f"{desc}: what grew, below"


@pytest.mark.anyio
@pytest.mark.parametrize("up, own, desc", [
    (False, False, "at the newest reply: a new one shows at the end"),
    (True, False, "scrolled up to read: what shows stays put, not sent to the end"),
    (False, True, "your own answer, at the newest reply: it shows at the end"),
    (True, True, "your own answer, scrolled up to read: back to the end, where it shows"),
])
@pytest.mark.both
async def test_a_new_reply_in_a_long_thread(store, sid, up, own, desc):
    ref = store.post_item(sid, "question", "q", body="\n\n".join(f"para {i}" for i in range(80)), status="open")
    app = WheelhouseApp(store)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.push_screen(ThreadView(sid, ref))
        await pilot.pause()
        scroll = app.screen.query_one("#thread-scroll")
        if up:
            scroll.scroll_to(y=20, animate=False, immediate=True)
            await pilot.pause()
        y = scroll.scroll_y
        if own:
            app.screen.box.text = "my answer"
            await pilot.press("ctrl+enter")
        else:
            store.reply(sid, ref, "a new reply", status="open")
            app.refresh_data()   # the tick, which repaints the thread
        await pilot.wait_for_scheduled_animations()
        await pilot.pause()
        assert ("my answer" in app.screen.text) == own, f"{desc}: the answer shows in the thread"
        assert scroll.max_scroll_y > y, f"{desc}: the reply made it longer"
        assert scroll.scroll_y == (y if up and not own else scroll.max_scroll_y), desc


@pytest.mark.anyio
@pytest.mark.parametrize("up, own, desc", [
    (False, False, "at the newest reply: a new one shows at the end"),
    (True, False, "scrolled up to read: what shows stays put"),
    (False, True, "your own answer, at the newest reply: it shows at the end"),
    (True, True, "your own answer, scrolled up to read: what shows stays put"),
])
@pytest.mark.parametrize("size", [(160, 40), (100, 30)])
async def test_a_new_reply_in_a_long_thread_in_detail(store, sid, up, own, desc, size):
    """#147: an item's thread in the Detail pane follows its newest reply, as the conversation
    does, unless scrolled more than a line up from its end. Selected, it shows from its top."""
    ref = store.post_item(sid, "question", "q", body="\n\n".join(f"para {i}" for i in range(80)), status="open")
    app = WheelhouseApp(store)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        app.items_table.move_cursor(row=app.items_table.get_row_index(f"{sid}|{ref}"))
        await pilot.pause()
        scroll = app.detail_scroll
        assert app.selected == (sid, ref) and scroll.scroll_y == 0 < scroll.max_scroll_y, f"{desc}: shown from its top"
        scroll.scroll_to(y=20 if up else scroll.max_scroll_y, animate=False, immediate=True)
        await pilot.pause()
        y = scroll.scroll_y
        if own:
            app.answer.text = "my answer"
            app.answer.focus()
            await pilot.press("ctrl+enter")
        else:
            store.reply(sid, ref, "a new reply", status="open")
            app.refresh_data()
        await pilot.wait_for_scheduled_animations()
        await pilot.pause()
        assert ("my answer" if own else "a new reply") in app._detail_text, f"{desc}: it shows in the thread"
        assert scroll.max_scroll_y > y, f"{desc}: the reply made it longer"
        if up:   # within a line: the answer box's own change may shift the view by one
            assert abs(scroll.scroll_y - y) <= 1 < scroll.max_scroll_y - scroll.scroll_y, desc
        else:
            assert scroll.scroll_y == scroll.max_scroll_y, desc


@pytest.mark.anyio
@pytest.mark.parametrize("view, shrink, desc", [
    ("conversation", "emoji", "a conversation, emoji suggestions wrapping under the box as a code is typed"),
    ("conversation", "splitter", "a conversation, its answer box made taller with the splitter"),
    ("thread", "emoji", "an item's thread, emoji suggestions wrapping under the box"),
    ("thread", "splitter", "an item's thread, its answer box made taller with the splitter"),
    ("full", "splitter", "an item full screen, its answer box made taller"),
    ("thread", "narrower", "an item's thread, the pane narrower: its text wraps taller"),
    ("thread", "up", "an item's thread scrolled up to read, then shorter: what shows stays put"),
])
async def test_detail_at_its_end_stays_there_when_its_view_shrinks(store, sid, tmp_path, monkeypatch, view, shrink,
                                                                     desc):
    """#200: Detail at its end, its view made shorter by what's under it (the hint's emoji
    suggestions, the answer box's splitter): its last line stays in view, and what's new
    after, a reply, still shows at the end."""
    store.set_mode(sid, "immediate")   # its hint, which the suggestions replace, a line shorter than they are
    folder = tmp_path / "projects/-home-u-repo"
    folder.mkdir(parents=True)
    path = folder.joinpath(f"{sid}.jsonl")
    path.write_text("".join(json.dumps({"type": "user", "timestamp": "2026-10-07T21:30:00Z",
                                        "message": {"content": f"turn {i}"}}) + "\n" for i in range(60)))
    monkeypatch.setattr(transcript, "PROJECTS", tmp_path / "projects")
    ref = store.post_item(sid, "question", "q", body="\n\n".join(f"para {i}" for i in range(80)), status="open")
    app = WheelhouseApp(store)
    async with app.run_test(size=(100, 30)) as pilot:
        await laid(pilot)
        scroll, box = app.detail_scroll, app.answer
        if view == "conversation":
            app.follow(sid)
        elif view == "thread":
            app.items_table.move_cursor(row=app.items_table.get_row_index(f"{sid}|{ref}"))
        else:
            app.push_screen(ThreadView(sid, ref))
        await laid(pilot)
        if view == "full":
            scroll, box = app.screen.query_one("#thread-scroll"), app.screen.box
        scroll.scroll_end(animate=False, immediate=True)
        if shrink == "up":
            scroll.scroll_to(y=scroll.max_scroll_y - 10, animate=False, immediate=True)
        await laid(pilot)
        size, y = scroll.size, scroll.scroll_y
        assert scroll.max_scroll_y > 0 and (shrink == "up") == (y < scroll.max_scroll_y), f"{desc}: at the end, or not"
        box.focus()
        if shrink == "emoji":
            await pilot.press("colon", "s", "t")
        elif shrink == "narrower":
            await pilot.resize_terminal(80, 30)
        else:
            box.styles.height = box.size.height + 4
        await laid(pilot)
        assert scroll.size != size, f"{desc}: the view another size"
        if shrink == "up":
            assert scroll.scroll_y == y, desc
            return
        assert scroll.scroll_y == scroll.max_scroll_y, f"{desc}: its last line still in view"
        if view == "conversation":
            say_in(path, "a reply\n\n" * 4)
        else:
            store.reply(sid, ref, "a reply\n\n" * 4, status="open")
        app.refresh_data()
        await laid(pilot)
        assert scroll.scroll_y == scroll.max_scroll_y, f"{desc}: the reply shows at the end"


def say_in(path, text: str) -> None:
    """A reply of Claude's at the end of a session's transcript."""
    with open(path, "a") as f:
        f.write(json.dumps({"type": "assistant", "timestamp": "2026-10-07T21:31:00Z",
                            "message": {"content": [{"type": "text", "text": text}]}}) + "\n")


class OneTranscript(App):
    """A Transcript alone in its scroll: blocks laid out and changed, nothing else."""
    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Transcript()


def blk(i: int, lines: int = 3, tag: str = "", who: str = "you") -> tuple[str, str]:
    """A block headed with its number, then that many lines, each a line of its own."""
    return who, f"**{who} {i}{tag}**\n\n" + "\\\n".join(f"line {i}.{k}" for k in range(lines))


BASE = [blk(i, 3 + i % 4) for i in range(50)]
TOOL = ("tool", "`⚙ Bash gh pr checks`")
LONG = BASE[:30] + [blk(30, 60)] + BASE[31:]
QUEUED = [("you", f"**you · queued**\n\nyes {n}\n\nmore") for n in range(4)]
TIED = BASE[:10] + [TOOL, blk(10, 4, who="claude")] + BASE[11:40] + [TOOL, blk(40, 30, who="claude")]
TOOLS = BASE[:20] + [TOOL] * 6 + [blk(26, 12, who="claude")]


@pytest.mark.anyio
@pytest.mark.parametrize("blocks, top, new, grown, desc", [
    (BASE, (30, 0), BASE[1:30] + [blk(k, 3 + k % 4, " x") for k in (30, 31, 32)] + BASE[33:] + [blk(50)], None,
     "the three blocks from the view's top change, the oldest drops: the one above keeps its place"),
    (LONG, (30, 10), LONG[1:30] + [blk(30, 61, " x")] + LONG[31:] + [blk(77)], None,
     "every block in view changes (the view is inside one), the oldest drops: the one above keeps its place"),
    (BASE[:40] + [blk(40, 12, who="claude")] + QUEUED, (40, 2), BASE[:40] + [blk(40, 18, who="claude")] + QUEUED,
     "line 40.12", "the reply at the view's top grows at its end, answers queued under it: the reader's line "
     "stays put and what grew shows below it"),
    (TIED, (41, 0), TIED[:-1] + [blk(40, 36, who="claude")], None,
     "the view's top a tool line said earlier too, each followed by one block: the nearest, not the earlier one"),
    (TOOLS + BASE[27:], (26, 4), [("note", "warning")] + TOOLS[:-1] + [blk(26, 18, who="claude")] + BASE[27:],
     "line 26.12", "the reply at the view's top grows under a run of the same tool line, a warning put in at the "
     "head: the tool line right above it keeps its place, not its neighbour in the run"),
    (BASE[:29] + [blk(29, 3, " queued"), blk(30, 12, who="claude")] + BASE[31:], (30, 4),
     BASE[:29] + [blk(29, 3, " sent"), blk(30, 18, who="claude")] + BASE[31:],
     "line 30.12", "the reply at the view's top grows and the block right above it changes too: the one above "
     "that keeps its place"),
])
async def test_a_changed_transcript_keeps_a_scrolled_up_place(blocks, top, new, grown, desc):
    """top: the block the view starts in and the line into it."""
    async with OneTranscript().run_test(size=(100, 20)) as pilot:
        t, scroll = pilot.app.query_one(Transcript), pilot.app.query_one(VerticalScroll)
        t.update(blocks)
        await pilot.pause()
        starts = t._layout(t.size.width)[2]
        y = starts[top[0]] + top[1]
        scroll.scroll_to(y=y, animate=False, immediate=True)
        await pilot.pause()

        def shown() -> list[str]:
            return t._layout(t.size.width)[1][int(scroll.scroll_y):][:scroll.size.height]

        before = shown()
        t.update(new)
        await pilot.wait_for_scheduled_animations()
        await pilot.pause()
        after = [line.replace(" x", "") for line in shown()]   # the headers changed alone
        keep = starts[top[0] + 1] - y - 1 if grown else len(before)   # down to the end of what grew
        assert after[:keep] == before[:keep], desc
        assert grown is None or after[keep] == grown, f"{desc}: what grew, below"


class WordTranscript(OneTranscript):
    on_click, full_screen_click = WheelhouseApp.on_click, WheelhouseApp.full_screen_click


@pytest.mark.anyio
@pytest.mark.parametrize("at, chain, selected, desc", [
    ("beta", 2, "beta", "a double click on a word selects that word"),
    ("-g", 2, "-", "on punctuation, that character alone"),
    ("  d", 2, "  ", "on spaces, the run of them"),
    ("delta", 1, None, "a single click selects nothing"),
    ("delta", 3, None, "a triple click selects the whole conversation, as before"),
])
async def test_a_double_click_selects_the_word_under_it(at, chain, selected, desc):
    async with WordTranscript().run_test(size=(100, 20)) as pilot:
        t = pilot.app.query_one(Transcript)
        t.update([("you", "alpha beta-gamma  delta"), ("claude", "the reply")])
        await pilot.pause()
        texts = t._layout(t.size.width)[1]
        y = next(i for i, line in enumerate(texts) if "alpha" in line)
        await pilot.click(t, offset=(texts[y].index(at), y), times=chain)
        await pilot.pause()
        got = pilot.app.screen.get_selected_text()
        if chain == 3:
            assert "alpha" in got and "the reply" in got, desc
        else:
            assert got == selected, desc


class OneBox(App):
    on_click, full_screen_click = WheelhouseApp.on_click, WheelhouseApp.full_screen_click

    def compose(self) -> ComposeResult:
        yield TextArea("first line\nalpha beta-gamma  delta")


@pytest.mark.anyio
@pytest.mark.parametrize("at, chain, selected, desc", [
    ("beta", 2, "beta", "a double click in an answer box selects the word"),
    ("-g", 2, "-", "on punctuation, that character alone"),
    ("delta", 1, "", "a single click only moves the cursor"),
])
async def test_a_double_click_in_an_answer_box_selects_the_word(at, chain, selected, desc):
    async with OneBox().run_test(size=(60, 10)) as pilot:
        box = pilot.app.query_one(TextArea)
        await pilot.click(box, offset=(box.gutter.left + "alpha beta-gamma  delta".index(at), box.gutter.top + 1),
                          times=chain)
        await pilot.pause()
        assert box.selected_text == selected, desc


async def relaunch_button(pilot):
    await pick(pilot.app, pilot, pilot.app.sessions[0]["id"])   # its buttons need it selected (#16)
    await pilot.click("#relaunch")


async def select_in_inbox(pilot):
    pilot.app.query_one("#session-list", DataTable).focus()
    await pilot.press("enter", "alt+y")


@pytest.mark.parametrize("how, desc", [
    (relaunch_button, "the Relaunch button"),
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
        await pilot.press("alt+n")
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


def blinking(box) -> bool:
    """Whether a text box's cursor blink is running: Textual's own timer, on real time."""
    return (box.blink_timer if isinstance(box, TextArea) else box._blink_timer)._active.is_set()


@pytest.mark.anyio
@pytest.mark.parametrize("box_id, key, elsewhere, desc", [
    ("answer", "up", None, "an arrow in the answer box"),
    ("answer", "end", None, "End in the answer box"),
    ("answer", "x", None, "typing in the answer box"),
    ("brief", "left", None, "an arrow in the new session's brief"),
    ("name", "home", None, "Home in a dialog's input, where the cursor can't move"),
    ("answer", "up", "name", "an arrow in the answer box, then typing in a dialog opened and closed meanwhile"),
])
async def test_a_held_key_shows_a_text_box_cursor_solid_until_a_quiet_spell(store, sid, clock, box_id, key, elsewhere,
                                                                            desc):
    from claude_wheelhouse import tui
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        if box_id != "answer":
            app.push_screen(tui.NewSession())
            await pilot.pause()
        box = app.screen.query_one(f"#{box_id}")
        box.focus()
        await pilot.pause()
        for n in range(4):   # a held key's repeats, each sooner than the quiet spell
            await pilot.press(key)
            await pilot.pause()
            assert box._cursor_visible and not blinking(box), f"{desc}: solid at press {n + 1}"
            clock.advance(tui.QUIET * 0.9)
        if elsewhere:   # a key in another box, within this one's quiet spell, leaves its spell running
            app.push_screen(tui.NewSession())
            await pilot.pause()
            app.screen.query_one(f"#{elsewhere}").focus()
            await pilot.pause()
            await pilot.press("x")
            await pilot.pause()
            app.pop_screen()
            await pilot.pause()
        assert box._cursor_visible and not blinking(box), f"{desc}: solid within the quiet spell"
        clock.advance(tui.QUIET * 0.2)
        assert blinking(box), f"{desc}: blinking again after the quiet spell"


@pytest.mark.anyio
async def test_unsent_text_stays_with_the_item_it_was_typed_for(store, sid, tmp_path):
    other = store.create_session(str(tmp_path), name="other")
    q1 = store.post_item(sid, "question", "which db?")
    q2 = store.post_item(sid, "question", "which port?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        items, box = app.query_one("#items", DataTable), app.query_one("#answer", TextArea)
        row = lambda ref: [str(items.get_row_at(i)[2]) for i in range(items.row_count)].index(ref)
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
    (lambda: Confirm("ok?"), ["Yes [Alt+Y]", "No [Alt+N]"], "the relaunch and destructive confirms"),
    (lambda: Choice("hm?"), ["Cancel request [Alt+C]", "Force [Alt+O]", "Leave it [Esc]"], "a pending request's choice"),
    (lambda: RelaunchChoice("big?"), ["Compact first [Alt+C]", "Relaunch now [Alt+R]", "Cancel [Esc]"],
     "Relaunch of a big session"),
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
        items.move_cursor(row=first_item(app))
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
@pytest.mark.both
async def test_typing_survives_refresh_ticks_even_when_items_arrive_above(store, sid, clock, conversation, desc):
    q = store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        if conversation:
            app.follow(sid)
        else:
            app.query_one("#items", DataTable).move_cursor(row=first_item(app))
        await pilot.pause()
        await pilot.click("#answer")
        await pilot.press(*"SQL")
        port = store.post_item(sid, "question", "which port?")   # sorts into the list during typing
        for _ in range(2):
            clock.tick(app)
            await pilot.pause()
        assert f"{sid}|{port}" in app.items_table.rows, f"{desc}: the ticks refreshed the list"
        await pilot.press(*"ite")
        assert app.selected == (sid, None if conversation else q), f"{desc}: the highlight stays put"
        assert (app.answer.text, app.answer.cursor_location) == ("SQLite", (0, 6)), f"{desc}: text and cursor untouched"


@pytest.mark.parametrize("keys, expected, desc", [
    (["h", "i", "space", "colon", "g", "r", "i", "tab"], "hi 😁", "Tab takes the first suggestion"),
    (["colon", "t", "h", "u", "enter"], "👍", "so does Enter"),
    (["o", "k", "space", "colon", "t", "a", "d", "a", "colon"], "ok 🎉", "a closed code turns into its emoji"),
    (["o", "k", "enter", "x"], "ok\nx", "Enter with no suggestion is a new line"),
    (["colon", "s", "t", "a", "r", "down", "down", "tab"], "🌠", "Down chooses the next suggestion, Tab takes it"),
    (["colon", "s", "t", "a", "r", "up", "enter"], "✡", "Up from the first wraps to the last, Enter takes it"),
    (["o", "k", "enter", "x", "up", "y"], "oyk\nx", "Up with no suggestion moves up a line"),
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
@pytest.mark.parametrize("at, chose, desc", [
    (0, [], "the first"),
    (1, [], "the second"),
    (4, [], "the last"),
    (0, ["down"], "the first, though Down chose the second"),
])
async def test_a_click_on_an_emoji_suggestion_takes_it(store, sid, at, chose, desc):
    """#201: the code typed so far becomes the emoji clicked, focus stays in the box and the
    cursor goes after the emoji, so typing carries on."""
    store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(200, 40)) as pilot:
        await pilot.pause()
        app.items_table.move_cursor(row=0)
        await pilot.click("#answer")
        await pilot.press(*"go ", "colon", *"star", *chose)
        await pilot.pause()
        hint = app.query_one("#detail-pane .answer-hint", Hint)
        found = emoji.suggest("star")
        x = sum(cell_len(f"{g} :{c}:  ") for c, g in found[:at]) + 1   # inside the suggestion, past its first cell
        await pilot.click(hint, offset=(x, 0))
        await pilot.pause()
        assert app.answer.text == f"go {found[at][1]}", f"{desc}: the code replaced by the emoji clicked"
        assert app.focused is app.answer, f"{desc}: focus still in the box"
        await pilot.press("x")
        assert app.answer.text == f"go {found[at][1]}x", f"{desc}: the cursor after the emoji"
        assert hint.suggesting is None, f"{desc}: the suggestions gone"


@pytest.mark.anyio
async def test_emoji_suggestions_go_once_the_cursor_moves_off_the_code(store, sid):
    """#201: moved off the code typed, the hint is the keys' again, so nothing out of date is
    clicked or taken, and Up and Down move between lines, even onto a line holding a code."""
    store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(200, 40)) as pilot:
        await pilot.pause()
        app.items_table.move_cursor(row=0)
        await pilot.click("#answer")
        await pilot.press("colon", *"star")
        hint = app.query_one("#detail-pane .answer-hint", Hint)
        assert hint.suggesting is not None, "suggested as typed"
        await pilot.press("down", "left", "left")
        await pilot.pause()
        assert hint.suggesting is None, "moved off the code: the suggestions gone"
        assert app.answer.suggestions() == (None, []), "and none to take"
        app.answer.text = "top\nsee :sta\nbottom"
        app.answer.move_cursor((2, 6))
        await pilot.press("up", "up")
        assert app.answer.cursor_location[0] == 0, "Up goes up past a line holding a code"


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


async def drag_from_the_box(pilot, pane):
    pilot.app.answer.focus()   # where focus always is with the conversation in sight (#77)
    await pilot.pause()
    await drag_over_the_question(pilot, pane)


@pytest.mark.anyio
@pytest.mark.parametrize("select, expected, desc", [
    (drag_over_the_question, "Q1 · w", "dragging the mouse selects part of the text"),
    (drag_from_the_box, "Q1 · w", "Ctrl+C in the answer box, with nothing selected in it, copies the pane's (#77)"),
])
async def test_the_detail_pane_selects_and_copies(store, sid, select, expected, desc):
    store.post_item(sid, "question", "which db?", "Postgres or SQLite for the cache?")
    app = WheelhouseApp(store)
    copied = []
    app.copy_to_clipboard = copied.append
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.query_one("#items", DataTable).move_cursor(row=first_item(app))
        await pilot.pause()
        await select(pilot, app.query_one("#detail", Transcript))
        await pilot.pause()
        await pilot.press("ctrl+c")
        await pilot.pause()
    assert copied and copied[0] == expected, desc


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
@pytest.mark.both
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
    ("delete", "question", "closed", True, False, "closed", "Delete on a closed question (shown with f) leaves it closed"),
    ("backspace", "question", "closed", True, False, "closed", "as does Backspace"),
    ("ctrl+r", "question", "closed", True, False, "answered", "Ctrl+R reopens a closed question (shown with f) as answered"),
    ("ctrl+r", "question", "closed", True, True, "closed", "not from the answer box: there it only takes back (D44)"),
    ("ctrl+r", "question", "open", False, False, "open", "Ctrl+R on an open question with nothing queued leaves it"),
    ("delete", "task", "running", False, False, "running", "a task's status is the session's: Delete leaves it"),
    ("x", "question", "open", False, False, "open", "x no longer closes (#64)"),
    ("backspace", "question", "open", False, True, "open", "in the answer box Backspace edits the text"),
    ("delete", "question", "open", False, True, "open", "and so does Delete"),
])
@pytest.mark.both
async def test_delete_closes_and_ctrl_r_reopens_questions(store, sid, key, kind, status, finished, focus_box, expected, desc):
    ref = store.post_item(sid, kind, "which db?", status=status)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        if finished:
            await pilot.press("alt+v")
        app.items_table.focus()
        app.items_table.move_cursor(row=app.items_table.get_row_index(f"{sid}|{ref}"))
        await pilot.pause()
        if focus_box:
            app.answer.focus()
        await pilot.press(key)
        await pilot.pause()
        if isinstance(app.screen, Confirm):   # an open question asks first (#151)
            await pilot.press("alt+y")
            await pilot.pause()
    assert store.item(sid, ref)["status"] == expected, desc


@pytest.mark.anyio
@pytest.mark.parametrize("kind, steps, shown, count, desc", [
    ("question", [], "open", "1", "nothing said yet"),
    ("question", ["send"], "processing", "", "the person spoke last: awaiting the session's reply"),
    ("question", ["send", "reply"], "open", "1", "the session replied, still waiting on the person"),
    ("question", ["queue"], "queued", "1", "a queued answer still awaits the person until it's sent"),
    ("question", ["queue", "dispatch"], "processing", "", "sent with Ctrl+S: the session's move"),
    ("question", ["send", "queue"], "queued", "1", "more queued: with the person until it's sent"),
    ("task", ["send"], "processing", "", "a task: the session's move"),
    ("task", ["queue"], "queued", "", "a task with a queued answer"),
    ("task", ["send", "reply"], "todo", "", "a task the session replied on"),
    ("decision", ["send"], "processing", "", "a decision: the session's move, never counted as a question"),
    ("decision", ["queue"], "queued", "", "a decision with a queued answer"),
    ("decision", ["queue", "dispatch"], "processing", "", "a decision's answer sent with Ctrl+S"),
])
async def test_the_item_list_shows_a_question_awaiting_the_session(store, sid, kind, steps, shown, count, desc):
    extra = {"why": "over Postgres: no server; undo: swap the DSN"} if kind == "decision" else {}
    q = store.post_item(sid, kind, "which db?", **extra)
    act = {"send": lambda: store.send(sid, "what's it for?", q), "queue": lambda: store.queue(sid, "the cache", q),
           "dispatch": lambda: store.dispatch(sid),
           "reply": lambda: store.reply(sid, q, "the cache", "open" if kind == "question" else None)}
    for step in steps:
        act[step]()
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert str(app.items_table.get_row_at(first_item(app))[3]) == shown, desc
        assert str(app.query_one("#session-list", DataTable).get_row_at(0)[3]) == count, f"{desc}: the Q count"


@pytest.mark.anyio
@pytest.mark.parametrize("kind, pick, answer, after, desc", [
    ("decision", "new", False, ["old", "new"],
     "a decision seen as it's selected keeps its place; once left, it settles at the foot (T71)"),
    ("question", "new", True, ["old", "new"], "a question answered while selected would fall below the open one"),
])
@pytest.mark.both
async def test_the_selected_item_keeps_its_place(store, sid, clock, kind, pick, answer, after, desc):
    """Doug: "only move items when not selected". The selected row keeps its place while
    it's selected, however its status changes; it re-sorts once the selection moves on."""
    extra = {"why": "over Postgres: no server; undo: swap the DSN"} if kind == "decision" else {}
    refs = {age: store.post_item(sid, kind, f"{age} one", **extra) for age in ("old", "new")}
    for age, at in (("old", "10:00"), ("new", "10:01")):
        store.db.execute("UPDATE items SET updated_at = ? WHERE ref = ?", (f"2026-10-09T{at}:00+00:00", refs[age]))
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        table = app.items_table
        order = lambda: [{v: k for k, v in refs.items()}[key.split("|")[1]] for key in table.keys()
                         if not key.endswith("|")]   # the items, not the conversation
        assert order() == ["new", "old"], f"{desc}: newest first"
        for age in (pick, "new" if pick == "old" else "old"):   # the one, then away from it
            table.move_cursor(row=table.get_row_index(f"{sid}|{refs[age]}"))
            await pilot.pause()
            if age == pick and answer:
                store.send(sid, "the cache", refs[age])
            elif age == pick:   # a decision seen once rested on
                dwell(app)
            app.refresh_data()
            await pilot.pause()
            await clock.finish_moves(pilot, app)   # a settled item sinks to the foot (T71)
            assert order() == (["new", "old"] if age == pick else after), \
                f"{desc}: {'held while selected' if age == pick else 're-sorted once the selection moved on'}"
            assert app.selected == (sid, refs[age]) and table.cursor_key() == f"{sid}|{refs[age]}", desc


def pin_target(store, sid, start):
    """The target item for test_a_selected_item_moves_only_on_delete, in its start state."""
    if start in ("agent running", "agent dismissed"):
        store.track_agent(sid, "ag1", "TARGET", "", datetime(2026, 10, 8, 10, tzinfo=timezone.utc))
        ref = store.agent_links(sid)["ag1"][0]
        if start == "agent dismissed":
            store.agent_status(sid, "ag1", "done")
            store.dismiss(sid, ref)
        return ref
    kind = start.split()[0]
    extra = {"why": "over a: b; undo: c"} if kind == "decision" else {}
    ref = store.post_item(sid, kind, "TARGET", **extra)
    if start in ("task done", "task dismissed", "task dismissed awaiting"):
        store.update_item(sid, ref, status="done")
    if start in ("task dismissed", "task dismissed awaiting"):
        store.dismiss(sid, ref)
    if start == "question closed":
        store.update_item(sid, ref, status="closed")
    if start == "task dismissed awaiting":
        store.send(sid, "a word", ref)
    return ref


PIN_ACTS = {
    "done": lambda store, sid, ref: store.update_item(sid, ref, status="done"),
    "answered": lambda store, sid, ref: store.reply(sid, ref, "ok", status="answered"),
    "closed": lambda store, sid, ref: store.update_item(sid, ref, status="closed"),
    "reopened": lambda store, sid, ref: store.update_item(sid, ref, status="open"),
    "running": lambda store, sid, ref: store.update_item(sid, ref, status="running"),
    "note": lambda store, sid, ref: store.update_item(sid, ref, note="progress"),
    "queue": lambda store, sid, ref: store.queue(sid, "a word", ref),
    "reply": lambda store, sid, ref: store.reply(sid, ref, "answer to your word"),
    "seen": lambda store, sid, ref: store.mark_seen(sid, ref),
    "allowed": lambda store, sid, ref: store.answer_permission(sid, ref, "allow"),
    "agent done": lambda store, sid, ref: store.agent_status(sid, "ag1", "done"),
    "agent failed": lambda store, sid, ref: store.agent_status(sid, "ag1", "failed"),
    "agent running": lambda store, sid, ref: store.agent_status(sid, "ag1", "running"),
}


@pytest.mark.anyio
@pytest.mark.parametrize("start, act, moves, desc", [
    ("task running", "done", False, "a task the session marks done"),
    ("question open", "answered", False, "a question the session answers"),
    ("decision unseen", "seen", False, "a decision marked seen"),
    ("agent running", "agent done", False, "a subagent that finishes"),
    ("agent running", "agent failed", False, "a subagent that fails"),
    ("question open", "closed", False, "a question the session closes"),
    ("question closed", "reopened", False, "a question the session reopens"),
    ("task dismissed", "running", False, "a dismissed task whose status the session changes"),
    ("agent dismissed", "agent running", False, "a dismissed subagent that resumes"),
    ("task dismissed", "note", False, "a note on a dismissed task"),
    ("task dismissed", "queue", False, "the person queueing a word on a dismissed task"),
    ("task dismissed awaiting", "reply", False, "a reply to the person's word on a dismissed task"),
    ("question open", "delete", True, "the person closing a question with Delete (D33)"),
    ("task done", "delete", True, "the person dismissing a done task with Delete (D33)"),
    ("question closed", "ctrl+r", True, "the person reopening a question with Ctrl+R, F on (D33)"),
    ("task dismissed", "ctrl+r", True, "the person bringing back a dismissed task with Ctrl+R, F on (D33)"),
    ("question closed", "delete", False, "Delete on a closed question, F on: it only ever closes, so it stays"),
    ("permission open", "#allow", True, "the person answering a permission: an explicit act, as Delete is"),
    ("permission open", "allowed", False, "a permission answered elsewhere (its own terminal)"),
])
@pytest.mark.both
async def test_a_selected_item_moves_only_on_delete(store, sid, live, clock, start, act, moves, desc):
    """Doug: "only move items when not selected (so eg the falling animation for completed
    tasks, which I think should also apply to seen decisions and answered questions, should
    only take place after I move focus to something else)". The one exception is the
    person's own Delete closing or Ctrl+R reopening, which moves it at once (D33)."""
    tick = itertools.count()
    stamp = lambda: (datetime(2026, 10, 8, 10, tzinfo=timezone.utc) + timedelta(seconds=next(tick))).isoformat()
    with mock.patch("claude_wheelhouse.store.now", stamp):   # one second apart: a fixed order
        fill = [store.post_item(sid, "task", f"fill{n}") for n in range(2)]
        store.update_item(sid, fill[0], status="done")   # something settled
        finish = lambda title: store.dismiss(sid, store.post_item(sid, "task", title, status="done"))
        store.update_item(sid, store.post_item(sid, "question", "closed q"), status="closed")
        finish("dismissed t")   # finished, before the target and after it
        ref = pin_target(store, sid, start)
        finish("dismissed t2")
        for n in range(2, 4):
            store.post_item(sid, "task", f"fill{n}")
        app = WheelhouseApp(store)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            table = app.items_table
            table.focus()
            await pilot.press("alt+v")   # finished items shown, so a finished target is in the list
            await pilot.pause()
            table.move_cursor(row=table.get_row_index(f"{sid}|{ref}"))
            await pilot.pause()
            where = lambda: [r.key.value for r in table.ordered_rows].index(f"{sid}|{ref}")
            before = where()
            assert app.selected == (sid, ref), desc
            if act in ("delete", "ctrl+r"):
                await pilot.press(act)
                if isinstance(app.screen, Confirm):   # Delete on an open question asks first (#151)
                    await pilot.press("alt+y")
            elif act.startswith("#"):
                await pilot.click(act)
            else:
                PIN_ACTS[act](store, sid, ref)
                app.refresh_data()
            await pilot.pause()
            await clock.finish_moves(pilot, app)
            app.refresh_data()   # a later tick holds it too
            await pilot.pause()
            assert (where() != before) == moves, f"{desc}: {'moved at once' if moves else 'held while selected'}"
            assert app.selected == (sid, ref) and table.cursor_key() == f"{sid}|{ref}", f"{desc}: still selected"


DECIDED = {"why": "over Postgres: no server; undo: swap the DSN"}


REST, SHORT = 1.5, 0.5   # seconds: a rest outlasts the 1 s dwell, a short look doesn't (the clock fixture)


async def dwell_steps(pilot, app, keys, steps, clock) -> None:
    """The steps of test_an_unseen_decision_is_seen_only_after_the_dwell, on the items by key."""
    for step in steps:
        how, _, what = step.partition(" ")
        if how == "on":
            app.items_table.move_cursor(row=app.items_table.get_row_index(keys[what]))
            await pilot.pause()
        elif how in ("rest", "short"):
            clock.advance(REST if how == "rest" else SHORT)
        elif how == "follow":
            app.follow(keys[what])
            await pilot.pause()
        elif how == "session":   # the person's move in the session list
            app.session_list.move_cursor(row=app.session_list.get_row_index(keys[what]))
            await pilot.pause()
        elif how == "at":   # the app's own selection, not the person's
            assert app.selected == tuple(keys[what].split("|")), f"the app selected {what}: {app.selected}"
        elif how == "end":   # the session ends: its items go
            app.store.end(keys[what])
            app.refresh_data()
        elif how in ("blur", "focus"):   # the terminal window loses focus, or gets it back
            app.post_message(events.AppBlur() if how == "blur" else events.AppFocus())
        elif how in ("gone", "late", "sorted"):   # before startup: the item closed, B's posted above D, a column sort
            continue
        elif how == "answer":
            app.answer.text = "noted"
            app.answer.focus()
            await pilot.press("ctrl+enter")
        else:   # a key on the items: enter, delete, question_mark, escape
            if not isinstance(app.screen, ThreadView) and app.screen is app.screen_stack[0]:
                app.items_table.focus()
            await pilot.press(how)
        await pilot.pause()


@pytest.mark.anyio
@pytest.mark.parametrize("steps, want, desc", [
    (("on D", "on E", "rest"), "unseen", "a pass over it: unseen"),
    (("on D", "short", "on E", "rest"), "unseen", "a rest shorter than the dwell, then on: unseen"),
    (("on D", "rest"), "seen", "a rest of the dwell, in the all-sessions view: seen"),
    (("follow A", "on D", "rest"), "seen", "the same following its session"),
    (("on D", "short", "on E", "on D", "short"), "unseen",
     "a rest, away, back: the dwell starts again, so two short looks don't add up"),
    (("on D", "short", "on E", "on D", "short", "short"), "seen", "and the restarted dwell lapses"),
    (("on D", "enter"), "unseen", "a thread opened on it: not at once"),
    (("on D", "enter", "rest"), "seen", "a thread opened on it: seen after the dwell"),
    (("on D", "short", "enter"), "unseen", "opening the thread starts the dwell again (it takes a while to open)"),
    (("on D", "f1", "rest"), "unseen", "the keys overlay over it: not looked at"),
    (("on D", "f1", "rest", "escape", "rest"), "seen", "back from the overlay: the dwell starts again"),
    (("on D", "delete"), "closed", "Delete: at once"),
    (("on D", "answer"), "seen", "answering it: seen at once"),
    (("follow A", "on D", "short", "session B", "rest"), "unseen", "a switch to another session mid-dwell"),
    (("gone E", "rest"), "unseen", "the conversation tops the inbox at startup (#236): D not looked at"),
    (("sorted ▲", "gone E", "at D", "short"), "unseen", "under a column sort D tops it: the app's selection, not at once"),
    (("sorted ▲", "gone E", "at D", "rest"), "seen", "but seen after the dwell all the same"),
    # B's above D, so D comes under the cursor as it goes, not demo's conversation row (#192)
    (("late B's", "on B's", "end B", "at D", "rest"), "seen",
     "the next item, selected as a session ends: seen after the dwell"),
    (("late B's", "on B's", "end B", "at D", "short", "blur", "rest"), "unseen", "unless the window loses focus first"),
    (("on D", "short", "blur", "rest"), "unseen", "the window losing focus mid-dwell: a break in looking"),
    (("on D", "short", "blur", "short", "focus", "rest"), "seen", "focus again, then a rest of the dwell: seen"),
])
@pytest.mark.both
async def test_an_unseen_decision_is_seen_only_after_the_dwell(store, sid, tmp_path, clock, steps, want, desc):
    """Doug: "I need to look at an item for at least 1 second before the state changes". An
    unseen decision becomes seen only once it has stayed selected, on the inbox or in its
    thread, for the dwell without a break, but not while the window is out of focus. Doug: "state
    should change after 1 second visible whether or not I selected it myself", so the app's
    own selection counts too. The person's explicit acts, Delete and answering, act at once."""
    other = store.create_session(str(tmp_path), name="other")
    with one_second():
        b = store.post_item(other, "decision", "B's", **DECIDED)   # first, so D, of its rank and second, is above it
        d = store.post_item(sid, "decision", "D", **DECIDED)
        e = store.post_item(sid, "question", "E")
    keys = {"D": f"{sid}|{d}", "E": f"{sid}|{e}", "A": sid, "B": other, "B's": f"{other}|{b}"}
    if "gone E" in steps:
        store.update_item(sid, e, status="closed")
    if "sorted ▲" in steps:   # the conversations under the active items, so D the top row (#236)
        store.set_setting("layout.sort.items", "status▲")
    if "late B's" in steps:
        keys["B's"] = f"{other}|{store.post_item(other, 'decision', 'B late', **DECIDED)}"
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        await dwell_steps(pilot, app, keys, steps, clock)
        assert store.item(sid, d)["status"] == want, f"{desc}: {store.item(sid, d)['status']}"
        if "session B" in steps:
            assert app.selected == (other, None), f"{desc}: B's conversation followed"
        if app.screen is app.screen_stack[0]:
            assert_one_current(app, desc)


@pytest.mark.anyio
@pytest.mark.parametrize("leave, desc", [
    (False, "seen by the dwell while selected: held in place, not sinking"),
    (True, "and once the selection moves on, it sinks to the foot"),
])
@pytest.mark.both
async def test_a_decision_seen_by_the_dwell_sinks_only_once_left(store, sid, clock, leave, desc):
    """The owner's rule for settled items: a decision that becomes seen while selected stays
    where it is until the selection moves on, then sinks (ranked, Sink), with seen arriving
    by the dwell's own timer."""
    with one_second():
        old = store.post_item(sid, "task", "old", status="done")
        d = store.post_item(sid, "decision", "D", **DECIDED)
        q = store.post_item(sid, "question", "Q")
    for ref, at in ((old, "09:00"), (d, "10:02"), (q, "10:01")):   # a fixed order
        store.db.execute("UPDATE items SET updated_at = ? WHERE ref = ?", (f"2026-10-09T{at}:00+00:00", ref))
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        table = app.items_table
        order = lambda: [k.split("|")[1] for k in table.keys() if not k.endswith("|")]   # not the conversation
        before = order()
        assert before[-1] == old, f"{desc}: the settled task at the foot, {before}"
        await dwell_steps(pilot, app, {"D": f"{sid}|{d}", "Q": f"{sid}|{q}"}, ("on D", "rest"), clock)
        app.refresh_data()
        await pilot.pause()
        assert store.item(sid, d)["status"] == "seen", f"{desc}: seen by the timer"
        assert order() == before and app.item_sink.move is None, f"{desc}: held while selected"
        if leave:
            await dwell_steps(pilot, app, {"Q": f"{sid}|{q}"}, ("on Q",), clock)
            frames = 0
            while app.item_sink.move is not None:
                assert_one_current(app, f"{desc}: mid-move")
                frames += 1
                clock.advance(tui.Sink.SECONDS / 8)
                await pilot.pause()
            assert frames > 3, f"{desc}: it sank a frame at a time ({frames})"
            assert order()[-2:] == [old, d] and app.selected == (sid, q), f"{desc}: {order()}"


@pytest.mark.anyio
@pytest.mark.both
async def test_a_lapse_as_an_arrow_acts_marks_nothing_new(store, sid, clock, monkeypatch):
    """Review 42: the dwell's timer puts its lapse in the app's queue (look), so it can fire as
    Down acts, before Down's highlight lands, and run once it has. The lapse is the old look's
    (dwell_gen): the row Down lands on starts a dwell of its own and is not seen at once."""
    with one_second():
        refs = [store.post_item(sid, "decision", t, **DECIDED) for t in ("D", "E", "F")]
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        table = app.items_table
        table.focus()
        table.move_cursor(row=1)   # the middle row: a fresh look, its own timer
        await pilot.pause()
        top = app.selected[1]
        check = app._check_bindings

        async def acting(key, priority=False):
            done = await check(key, priority)
            if done and key == "down":   # Down's binding has acted; its highlight is still to land
                clock.advance(REST)
            return done
        monkeypatch.setattr(app, "_check_bindings", acting)
        raw_keys(app, "down")
        for _ in range(4):
            await pilot.pause()
        landed = app.selected[1]
        assert landed != top and landed in refs, f"Down moved on: {landed}"
        assert store.item(sid, landed)["status"] == "unseen", "the row Down landed on, seen at once"


# Each case's items, posted in one second, so their stamps tie: refs number from 1 per kind
TIE_POSTS = {
    "Q1 open, T1-T3 running": [("question", "open")] + [("task", "running")] * 3,
    "D1-D3 unseen": [("decision", None)] * 3,
    "T1-T3 done": [("task", "done")] * 3,
    "Q1-Q3 open": [("question", "open")] * 3,
    "Q1-Q3 answered": [("question", "answered")] * 3,
}
TIE_SECOND = "2026-10-08T10:00:05+00:00"   # every write a case makes, after its posts, lands in this one


@pytest.mark.anyio
@pytest.mark.parametrize("typing", ["slow", "raw"])
@pytest.mark.parametrize("posts, selected, acts, held, leave, after, lands, desc", [
    ("Q1 open, T1-T3 running", "T1", "T1 done, T2 done", "Q1 T3 T1 T2", "up", "Q1 T3 T1 T2", "T3",
     "review 50: the selected settles first, the other next: held, then the two by id, T1 above T2"),
    ("Q1 open, T1-T3 running", "T2", "T1 done, T2 done", "Q1 T3 T2 T1", "up", "Q1 T3 T1 T2", "T3",
     "review 50: the other settles first, the selected next: held, then by id, so T2 sinks below T1"),
    ("Q1 open, T1-T3 running", "T1", "T2 done, T1 done", "Q1 T3 T1 T2", "up", "Q1 T3 T1 T2", "T3",
     "review 50: the other first, the selected next: by id, as when the selected settled first"),
    ("Q1 open, T1-T3 running", "T2", "T2 done, T1 done", "Q1 T3 T2 T1", "up", "Q1 T3 T1 T2", "T3",
     "review 50: the selected first, the other next: by id, as when the other settled first"),
    ("Q1 open, T1-T3 running", "Q1", "T1 done, T2 done", "Q1 T3 T1 T2", "", "Q1 T3 T1 T2", "Q1",
     "review 49: two settle in one second, unselected, a refresh apart: by id"),
    ("Q1 open, T1-T3 running", "Q1", "T2+T1 done", "Q1 T3 T1 T2", "", "Q1 T3 T1 T2", "Q1",
     "review 50: two settle between refreshes: by id, whatever order they settled in"),
    ("D1-D3 unseen", "D2", "lapse", "D3 D2 D1", "down", "D3 D1 D2", "D1",
     "review 47: seen by the dwell while selected: held, then Down goes to the row below"),
    ("D1-D3 unseen", "D2", "", "D3 D2 D1", "down+lapse", "D3 D1 D2", "D1",
     "review 47: Down typed as the dwell lapses: Down goes to the row below"),
    ("T1-T3 done", "T2", "alt+v delete ctrl+r", "T1 T2 T3", "up", "T1 T2 T3", "T1",
     "Delete then Ctrl+R in one second: a task dismissed and brought back returns to where it settled"),
    ("Q1-Q3 answered", "Q2", "alt+v delete ctrl+r", "Q1 Q3 Q2", "up", "Q1 Q3 Q2", "Q3",
     "Delete then Ctrl+R in one second: a question closed and reopened, answered again, settles last"),
    ("Q1-Q3 open", "Q2", "Q2 answered, Q3 answered", "Q2 Q1 Q3", "down", "Q1 Q2 Q3", "Q1",
     "a question answered while selected, then another: held, then the two by id, Q2 above Q3"),
    ("Q1-Q3 open", "Q2", "Q3 answered, Q2 answered", "Q2 Q1 Q3", "down", "Q1 Q2 Q3", "Q1",
     "another answered, then the selected: held, then by id, as when the selected was answered first"),
])
@pytest.mark.both
async def test_items_that_tie_to_the_second_keep_one_order(store, sid, clock, posts, selected, acts, held, leave,
                                                           after, lands, desc, typing):
    """#70: now() stamps whole seconds, so items an agent posts or settles together tie on
    updated_at. Doug: "Sort on time first, id second, to ensure stable ordering even when items
    are updated in the same second bucket." So a tie goes by id in its band's direction (active,
    the higher id first; settled, the lower), whatever the order of the writes and refreshes,
    and typed slowly or as a raw burst: the selected item stays put under the cursor (held) and
    moves once the selection leaves it (after); Delete and Ctrl+R move it at once (D33); and
    one session is current. Acts and leave are steps: "T1 done" or "Q2 answered" a write (two
    joined by + share one refresh), "lapse" the dwell lapsing, "down+lapse" Down typed as it
    lapses, else keys."""
    with one_second():
        for kind, status in TIE_POSTS[posts]:
            store.post_item(sid, kind, kind, status=status, **(DECIDED if kind == "decision" else {}))
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        table = app.items_table
        order = lambda: " ".join(k.split("|")[1] for k in table.keys() if not k.endswith("|"))   # not the conversation

        async def steps(text):
            for step in filter(None, text.split(", ")):
                if step.endswith((" done", " answered")):
                    refs, what = step.split(" ")
                    for ref in refs.split("+"):
                        if what == "done":
                            store.update_item(sid, ref, status="done")
                        else:
                            store.reply(sid, ref, "yes", status="answered")
                    app.refresh_data()
                elif step == "lapse":
                    clock.advance(REST)
                    await pilot.pause()
                    clock.tick(app)
                elif step == "down+lapse":   # the lapse fires before Down lands, either way
                    raw_keys(app, "down")
                    clock.advance(REST)
                elif typing == "raw":
                    raw_keys(app, *step.split(" "))
                else:
                    for key in step.split(" "):
                        await pilot.press(key)
                for _ in range(4):
                    await pilot.pause()
                await clock.finish_moves(pilot, app)
        await pilot.pause()
        table.focus()
        table.move_cursor(row=table.get_row_index(f"{sid}|{selected}"))
        await pilot.pause()
        with mock.patch("claude_wheelhouse.store.now", lambda: TIE_SECOND):
            await steps(acts)
            assert (order(), table.cursor_key()) == (held, f"{sid}|{selected}"), f"{desc}: held"
            await steps(leave)
            assert (order(), table.cursor_key()) == (after, f"{sid}|{lands}"), f"{desc}: after"
        assert_one_current(app, desc)


@pytest.mark.anyio
async def test_a_decision_is_seen_once_viewed_and_stays_until_closed(store, sid):
    q = store.post_item(sid, "question", "which db?")
    d = store.post_item(sid, "decision", "cache in SQLite", why="over Postgres: no server to run; undo: swap the DSN")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        items, sessions = app.items_table, app.query_one("#session-list", DataTable)
        assert [str(items.get_row_at(i)[2]) for i in range(items.row_count)] == ["💬", q, d], \
            "the session's conversation on top (#236), then the question ranks first"
        assert str(items.get_row_at(2)[3]) == "unseen"
        assert str(sessions.get_row_at(0)[4]) == "1", "the session list counts unseen decisions"
        assert store.item(sid, d)["status"] == "unseen", "not seen until the person looks"
        items.focus()
        items.move_cursor(row=items.get_row_index(f"{sid}|{d}"))
        await refresh(pilot, sid)
        assert store.item(sid, d)["status"] == "unseen", "not seen until the person has rested on it"
        dwell(app)
        await refresh(pilot, sid)
        assert store.item(sid, d)["status"] == "seen", "viewing it for the dwell marks it seen"
        assert str(sessions.get_row_at(0)[4]) == ""
        items.move_cursor(row=first_item(app))
        await refresh(pilot, sid)
        assert [str(items.get_row(k)[2]) for k in items.final() if k] == ["💬", q, d], \
            "moving on, it sinks (once the move ends): settled, under the question"
        items.move_cursor(row=items.get_row_index(f"{sid}|{d}"))
        await pilot.press("delete")
        await pilot.pause()
        assert store.item(sid, d)["status"] == "closed", "Delete closes it"
        assert [str(items.get_row_at(i)[2]) for i in range(items.row_count)] == ["💬", q], "then it's finished"


@pytest.mark.anyio
@pytest.mark.parametrize("start, key, finished, expected, desc", [
    ("unseen", "delete", False, "closed", "Delete closes an unseen decision"),
    ("seen", "delete", False, "closed", "and a seen one"),
    ("closed", "delete", True, "closed", "Delete on a closed decision (shown with f) leaves it closed"),
    ("closed", "ctrl+r", True, "seen", "Ctrl+R reopens it as seen"),
])
@pytest.mark.both
async def test_delete_closes_and_ctrl_r_reopens_decisions(store, sid, start, key, finished, expected, desc):
    d = store.post_item(sid, "decision", "cache in SQLite", why="over Postgres: local; undo: swap")
    if start == "closed":
        store.close_decision(sid, d)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        if finished:
            await pilot.press("alt+v")
        app.items_table.focus()
        app.items_table.move_cursor(row=app.items_table.get_row_index(f"{sid}|{d}"))
        await pilot.pause()
        await pilot.press(key)
        await pilot.pause()
    assert store.item(sid, d)["status"] == expected, desc


@pytest.mark.parametrize("steps, marked, desc", [
    ([("ctrl", 2)], ["Q1", "Q3"], "Ctrl+click adds to the highlighted row"),
    ([("ctrl", 2), ("ctrl", 2)], ["Q1"], "a second Ctrl+click unmarks"),
    ([("click", 1), ("shift", 3)], ["Q2", "Q3", "Q4"], "Shift+click marks the range"),
    ([("click", 1), ("alt", 3)], ["Q2", "Q3", "Q4"], "Alt+click marks the range, as Shift+click"),
    ([("ctrl", 0), ("ctrl", 1), ("shift", 3)], ["Q2", "Q3", "Q4"], "the range runs from the last one toggled"),
    ([("key", "ctrl+space"), ("key", "down"), ("key", "down"), ("key", "ctrl+space")], ["Q1", "Q3"], "Ctrl+Space toggles"),
    ([("key", "shift+down"), ("key", "shift+down")], ["Q1", "Q2", "Q3"], "Shift+Down extends"),
    ([("ctrl", 2), ("key", "escape")], [], "Esc clears the marks"),
    ([("ctrl", 2), ("click", 3)], [], "a plain click starts afresh"),
])
@pytest.mark.anyio
async def test_marking_rows(store, sid, steps, marked, desc):
    for at, title in zip("4321", "abcd"):   # Q1-Q4 in order, newest first, under the rows clicked
        with mock.patch("claude_wheelhouse.store.now", lambda: f"2026-10-08T10:00:0{at}+00:00"):
            store.post_item(sid, "question", title)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        items = app.items_table
        items.focus()
        await to_first_item(app, pilot)
        for how, arg in steps:
            if how == "key":
                await pilot.press(arg)
            else:
                await pilot.click("#items", offset=(20, 1 + first_item(app) + arg), control=how == "ctrl", shift=how == "shift",
                                meta=how == "alt")
            await pilot.pause()
        assert sorted(k.split("|")[1] for k in items.marked) == marked, desc
        shaded = [items.get_row_at(i)[2] for i in range(items.row_count)
                  if isinstance(cell := items.get_row_at(i)[4], Text) and (Style.parse(cell.style) if isinstance(cell.style, str) else cell.style).bgcolor]
        assert [str(r) for r in shaded] == marked, f"{desc}: marked rows are shaded"


async def five_questions(store, sid, pilot, steps) -> None:
    """Q1-Q5, newest first under the rows clicked so Q1 tops the list, the item list focused,
    then the steps: a key, or a plain, Ctrl, Shift or Alt click on a row."""
    app = pilot.app
    await pilot.pause()
    app.items_table.focus()
    await to_first_item(app, pilot)
    for how, arg in steps:
        if how == "key":
            await pilot.press(arg)
        else:
            await pilot.click("#items", offset=(20, 1 + first_item(app) + arg), control=how == "ctrl", shift=how == "shift",
                              meta=how == "alt")
        await pilot.pause()
        if isinstance(app.screen, Confirm):   # Delete on an open question asks first (#151): yes
            await pilot.press("alt+y")
            await pilot.pause()


def post_five(store, sid) -> None:
    for at, title in zip("54321", "abcde"):
        with mock.patch("claude_wheelhouse.store.now", lambda: f"2026-10-08T10:00:0{at}+00:00"):
            store.post_item(sid, "question", title)


@pytest.mark.parametrize("steps, lands, desc", [
    ([("click", 1), ("ctrl", 2), ("key", "delete")], "Q1", "Q2 and Q3 marked: the row above Q2, not Q5 in Q3's place"),
    ([("shift", 2), ("key", "delete")], "💬", "Q1-Q3 marked from the top: the conversation row above (#236)"),
    ([("click", 1), ("ctrl", 3), ("key", "delete")], "Q1", "Q2 and Q4 marked: above the topmost, Q2"),
    ([("click", 3), ("ctrl", 1), ("key", "delete")], "Q1", "marked bottom first: still above the topmost"),
    ([("click", 2), ("key", "delete")], "Q2", "one row, unmarked: the row above it"),
    ([("key", "delete")], "💬", "the first item: the conversation row above it (#236)"),
    ([("key", "alt+v"), ("click", 2), ("key", "delete")], "Q3", "Alt+V on: the row stays in sight, the highlight with it"),
])
@pytest.mark.anyio
async def test_delete_puts_the_highlight_above_the_topmost_row_it_took(store, sid, steps, lands, desc):
    post_five(store, sid)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await five_questions(store, sid, pilot, steps)
        await pilot.pause()
        refs = {k: str(app.items_table.get_row(k)[2]) for k in app.items_table.rows}
        assert refs[app.items_table.cursor_key()] == lands, desc
        sid_, ref = app.items_table.cursor_key().split("|")
        assert app.selected == (sid_, ref or None), f"{desc}: selected there too"


@pytest.mark.parametrize("finished, focus, marked, desc", [
    (False, "items", ["T2", "Q2"], "the settled rows, not the open, running or hidden ones"),
    (True, "items", ["T2", "Q2", "Q3"], "F on: the finished rows shown too"),
    (False, "answer", [], "in the answer box Ctrl+A is its own"),
])
@pytest.mark.anyio
async def test_ctrl_a_marks_the_finished_rows(store, sid, finished, focus, marked, desc):
    with one_second():
        for kind, status in [("question", "open"), ("task", "running"), ("task", "done"),
                             ("question", "answered"), ("question", "closed")]:
            store.post_item(sid, kind, kind, status=status)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        if finished:
            await pilot.press("alt+v")
        app.query_one(f"#{focus}").focus()
        await pilot.pause()
        await pilot.press("ctrl+a")
        await pilot.pause()
        assert sorted(k.split("|")[1] for k in app.items_table.marked) == sorted(marked), desc


@pytest.mark.parametrize("modifier, desc", [
    ({"control": True}, "Ctrl held: a mark, never a drag"),
    ({"shift": True}, "Shift held: a range, never a drag"),
])
@pytest.mark.anyio
async def test_a_modified_press_starts_no_drag(store, sid, modifier, desc):
    post_five(store, sid)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        table = app.items_table
        table.focus()
        await pilot.mouse_down(table, offset=(2, 1 + 3), **modifier)
        x, y = table.content_region.x + 2, table.content_region.y + 1
        pilot.app.post_message(events.MouseMove(None, x, y, 0, -3, button=1, shift=False, meta=False,
                                                ctrl=False, screen_x=x, screen_y=y))
        await pilot.pause()
        assert table.dragging is None, desc
        await pilot.mouse_up(table, offset=(2, 1))
        await pilot.pause()
        assert item_refs(app) == ["Q1", "Q2", "Q3", "Q4", "Q5"], f"{desc}: nothing moved"


@pytest.mark.anyio
@pytest.mark.parametrize("queued, where, box, status, desc", [
    (True, "items", "a word", "closed", "a word queued on it: Ctrl+R takes it back, the item stays closed"),
    (False, "items", "", "answered", "nothing queued: Ctrl+R reopens it"),
    (False, "thread", "", "closed", "full screen, Ctrl+R only takes back: nothing queued, nothing done"),
    (True, "thread", "a word", "closed", "and takes the word back there"),
])
@pytest.mark.both
async def test_ctrl_r_takes_a_queued_answer_back_before_it_reopens(store, sid, queued, where, box, status, desc):
    """Ctrl+R keeps its meaning where the item has an answer queued: taking it back. Only with
    none, in the item list, where Delete closes, does it reopen a finished item."""
    q = store.post_item(sid, "question", "which db?", status="closed")
    if queued:
        store.queue(sid, "a word", q)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        await pilot.press("alt+v")
        app.items_table.focus()
        app.items_table.move_cursor(row=app.items_table.get_row_index(f"{sid}|{q}"))
        await pilot.pause()
        if where == "thread":
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ThreadView), desc
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert (app.screen.box if where == "thread" else app.answer).text == box, desc
    assert store.item(sid, q)["status"] == status, desc
    assert bool(store.drafts(sid)) == (queued and not box), f"{desc}: the queue"


async def ctrl_r_where(tmp_path, follow, focus, at, marks, typed, slow) -> dict:
    """Q1 and Q2 closed, Q3 open, F on, nothing queued; the rows at marked, the highlight on
    at (or, following, the conversation row), focus on the item list or the answer box, then
    what's typed and Ctrl+R, typed slowly or sent as one raw burst. What it left: each status,
    the box, the marks and the last word said."""
    store = Store(tmp_path / f"{'slow' if slow else 'raw'}.db")
    sid = store.create_session(str(tmp_path), name="demo")
    with one_second():
        refs = dict(zip(("Q1", "Q2", "Q3"), (store.post_item(sid, "question", t) for t in "abc")))
    for name in ("Q1", "Q2"):
        store.update_item(sid, refs[name], status="closed")
    app = WheelhouseApp(store)
    notes = []
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        await pilot.press("alt+v")
        if follow:
            app.follow(sid)
        await pilot.pause()
        items = app.items_table
        items.focus()
        items.move_cursor(row=items.get_row_index(f"{sid}|{refs[at] if at else ''}"))
        await pilot.pause()
        items.set_marks({f"{sid}|{refs[m]}" for m in marks})
        if focus == "box":
            app.answer.focus()
        await pilot.pause()
        app.notify = lambda text, **kw: notes.append(text)
        keys = (*typed, "ctrl+r")
        if slow:
            await pilot.press(*keys)
        else:
            raw_keys(app, *keys)
        for _ in range(4):
            await pilot.pause()
        assert_one_current(app, f"{'slow' if slow else 'raw'} {focus} {typed}")
        return {"status": {n: store.item(sid, r)["status"] for n, r in refs.items()}, "box": app.answer.text,
                "marked": sorted(n for n, r in refs.items() if f"{sid}|{r}" in items.marked),
                "said": notes[-1:]}


@pytest.mark.anyio
@pytest.mark.parametrize("follow, focus, at, marks, typed, reopened, said, desc", [
    (False, "items", "Q3", ("Q1", "Q2"), "", ("Q1", "Q2"), "reopened Q1, Q2", "the item list reopens the marked rows"),
    (True, "items", None, ("Q1", "Q2"), "", ("Q1", "Q2"), "reopened Q1, Q2", "and so it does following the conversation"),
    (False, "items", "Q1", (), "", ("Q1",), "reopened Q1 as answered", "or the highlighted finished one"),
    (False, "box", "Q3", ("Q1", "Q2"), "typing", (), "the box isn't empty: queue or clear it first",
     "review: typing in the answer box, Ctrl+R never reopens the marked rows"),
    (True, "box", None, ("Q1", "Q2"), "typing", (), "the box isn't empty: queue or clear it first",
     "nor following the conversation"),
    (False, "box", "Q1", (), "half an answer", (), "the box isn't empty: queue or clear it first",
     "review: half an answer to a closed question, Ctrl+R in the box leaves it closed, as before 4ea937b"),
    (False, "box", "Q1", (), "", (), None, "an empty box with nothing queued: Ctrl+R there still only takes back"),
])
@pytest.mark.both
async def test_ctrl_r_reopens_only_from_the_item_list(tmp_path, follow, focus, at, marks, typed, reopened, said, desc):
    """D44: Ctrl+R reopens only with the item list focused. In the answer box it only takes a
    queued answer back, as it did before it reopened anything, so what's typed is never lost
    to it and no other item changes."""
    raw = await ctrl_r_where(tmp_path / "raw", follow, focus, at, marks, typed, slow=False)
    slow = await ctrl_r_where(tmp_path / "slow", follow, focus, at, marks, typed, slow=True)
    assert raw == slow, f"{desc}: raw {raw}, slow {slow}"
    expected = {n: "answered" if n in reopened else "open" if n == "Q3" else "closed" for n in ("Q1", "Q2", "Q3")}
    assert slow["status"] == expected, desc
    assert slow["box"] == typed, f"{desc}: the box"
    assert slow["marked"] == ([] if reopened else sorted(marks)), f"{desc}: the marks"
    if said:
        assert slow["said"] == [said], f"{desc}: the notice"
    else:
        assert slow["said"][0].startswith("nothing queued for"), f"{desc}: the notice"


@pytest.mark.anyio
@pytest.mark.parametrize("kinds, key, said, desc", [
    (("question", "task"), "delete", "closed Q1; dismissed T1: Alt+V shows finished items, Ctrl+Z undoes it",
     "Delete on mixed marks says which it closed and which it dismissed"),
    (("task", "agent"), "delete", "dismissed T1, A1: Alt+V shows finished items, Ctrl+Z undoes it", "all dismissed: said so"),
    (("question", "task"), "ctrl+r", "reopened T1, Q1", "Ctrl+R brings either back"),
])
async def test_the_word_after_acting_on_marks_names_each_act(store, sid, kinds, key, said, desc):
    with one_second():   # so the settled keep the order they're named in
        refs = [store.post_item(sid, k, k, status={"question": "answered", "task": "done", "agent": "done"}[k])
                for k in kinds]
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        items = app.items_table
        items.focus()
        if key == "ctrl+r":   # finished first, shown with F
            # closing stamps Q1 and dismissing leaves T1's stamp: T1, the older, is first among
            # the finished, and in the notice
            for k, r in zip(kinds, refs):
                app.close(store.item(sid, r), True)
            await pilot.press("alt+v")
        await pilot.pause()
        items.set_marks({f"{sid}|{r}" for r in refs})
        notes = []
        app.notify = lambda text, **kw: notes.append(text)
        await pilot.press(key)
        await pilot.pause()
    assert notes == [said], desc


@pytest.mark.anyio
async def test_the_word_after_acting_on_marks_mid_sink_names_them_as_they_will_stand(store, sid, clock):
    """A question answered sinks below a settled task; Delete on both, the move still under
    way (the clock stands still), names them in the order they'll stand, not the frame's."""
    q = store.post_item(sid, "question", "q")
    t = store.post_item(sid, "task", "t", status="done")
    at = "UPDATE items SET updated_at = ? WHERE ref = ?"   # settled oldest first: the question to go below
    store.db.execute(at, ("2026-10-09T09:00:00+00:00", t))
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        items = app.items_table
        items.focus()
        items.move_cursor(row=items.get_row_index(f"{sid}|{t}"))   # selected, a question answered isn't pinned
        await pilot.pause()
        store.reply(sid, q, "thanks", status="answered")
        store.db.execute(at, ("2026-10-09T10:00:00+00:00", q))
        app.refresh_data()
        await pilot.pause()
        marks = [f"{sid}|{r}" for r in (q, t)]
        assert app.item_sink.move and [k for k in items.keys() if k in marks] == marks, "mid-sink: Q1 still shows above"
        items.set_marks(set(marks))
        notes = []
        app.notify = lambda text, **kw: notes.append(text)
        await pilot.press("delete")
        await pilot.pause()
    assert notes == ["dismissed T1; closed Q1: Alt+V shows finished items, Ctrl+Z undoes it"]


@pytest.mark.anyio
@pytest.mark.both
async def test_delete_closes_the_marked_questions_and_ctrl_r_reopens_them(store, sid):
    with one_second():   # Q1-Q3 in order, under the rows clicked
        q1, q2, q3 = (store.post_item(sid, "question", t) for t in "abc")
        t = store.post_item(sid, "task", "build", status="running")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        await to_first_item(app, pilot)
        items = app.items_table
        items.focus()
        await pilot.click("#items", offset=(20, 3 + first_item(app)), control=True)   # Q1 and Q3
        await pilot.click("#items", offset=(20, 4 + first_item(app)), control=True)   # and the task
        d = store.post_item(sid, "question", "d")   # a rebuild: the marks are by key
        await refresh(pilot, sid)
        assert f"{sid}|{d}" in items.rows, "rebuilt"
        assert sorted(k.split("|")[1] for k in items.marked) == [q1, q3, t], "marks survive a refresh"
        await pilot.press("backspace")
        await pilot.pause()
        assert isinstance(app.screen, Confirm) and "2 are open questions" in app.screen.prompt, "asked once"
        await pilot.press("alt+y")
        await pilot.pause()
        assert [store.item(sid, r)["status"] for r in (q1, q2, q3, t)] == ["closed", "open", "closed", "running"]
        assert items.marked == set()
        await pilot.press("alt+v")
        await pilot.pause()
        items.set_marks({f"{sid}|{q1}", f"{sid}|{q2}", f"{sid}|{q3}"})
        await pilot.pause()
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert [store.item(sid, r)["status"] for r in (q1, q2, q3)] == ["answered", "open", "answered"], \
            "Ctrl+R reopens the finished ones marked, and leaves the open one"
        assert items.marked == set()
        items.set_marks({f"{sid}|{q1}", f"{sid}|{q3}"})
        await pilot.press("delete")
        await pilot.pause()
        items.set_marks({f"{sid}|{q1}", f"{sid}|{q3}"})
        await pilot.press("delete")
        await pilot.pause()
        assert [store.item(sid, r)["status"] for r in (q1, q3)] == ["closed", "closed"], \
            "all finished: Delete only ever closes, so they stay closed"
        assert items.marked == {f"{sid}|{q1}", f"{sid}|{q3}"}, "nothing to act on: the marks stay"


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
    other = store.create_session(str(tmp_path), name="other")
    q = store.post_item(other, "question", "B's")
    async with app.run_test(size=(160, 60)) as pilot:   # room for the charts: none at 40 rows
        await pilot.pause()
        app.items_table.move_cursor(row=app.items_table.get_row_index(f"{other}|{q}"))   # another's item highlighted
        app.refresh_data()
        await app.workers.wait_for_complete()
        await pilot.pause()
        shown = app.query_one("#stats").render().plain
        assert "all sessions · 2" in shown, f"none selected: every session's totals, running or not, not the item's: {shown}"
        assert "context" not in shown.replace("context assembly", ""), "and no context: each session has its own"
        assert "context assembly" in shown, "the charts show their activity"
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
@pytest.mark.parametrize("size, shown", [((200, 60), True), ((160, 50), True),
                                         ((80, 24), False)])   # an 11x9 pane: no charts, so no zoom
@pytest.mark.parametrize("selected, desc", [
    (True, "a session selected"),
    (False, "none selected: every session's totals"),
])
async def test_the_zoom_buttons_zoom_both_charts(store, sid, tmp_path, monkeypatch, size, shown, selected, desc):
    """+ and -, clicked at the right of the hour ticks: one level for both charts, held
    through a repaint, and going no further than the ladder's ends. At every size the
    charts show at, the line they're on is inside the pane."""
    folder = tmp_path / "projects/-home-u-repo"
    folder.mkdir(parents=True)
    rec = {"type": "assistant", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
           "message": {"id": "m1", "model": "claude-opus-5-5", "content": [],
                       "usage": {"input_tokens": 2, "cache_read_input_tokens": 82_000, "output_tokens": 10}}}
    folder.joinpath(f"{sid}.jsonl").write_text(json.dumps(rec) + "\n")
    monkeypatch.setattr(transcript, "PROJECTS", tmp_path / "projects")
    app = WheelhouseApp(store)
    desc = f"{desc} at {size[0]}x{size[1]}"
    async with app.run_test(size=size) as pilot:
        if selected:
            app.follow(sid)
        app.refresh_data()
        await app.workers.wait_for_complete()
        await pilot.pause()
        pane = app.query_one("#stats")

        async def click(button):
            await click_zoom(pilot, pane, button, desc)

        assert bool(pane.charts) == shown and pane.span == transcript_usage.SPAN, f"{desc}: the charts, at the default"
        if not shown:
            return
        for button, span in (("+", 3600), ("+", 1800), ("+", 900), ("-", 1800)):
            await click(button)
            app.refresh_data()
            await pilot.pause()
            assert pane.charts and {c.span for c in pane.charts} == {span}, f"{desc}: {button} to {span}"
        assert pane.render().plain.split("\n")[-1].endswith("30m  -   + "), desc
        await click("-")
        await click("-")
        assert pane.span == transcript_usage.SPAN and not zoomable(pane, "-"), f"{desc}: a transcript all in the span: - goes no wider"


@pytest.mark.anyio
@pytest.mark.both
@pytest.mark.parametrize("size", [(160, 50), (80, 24)])
@pytest.mark.parametrize("mcp, running, warns, desc", [
    (None, False, None, "no MCP server recorded (older code), not running: nothing to flag"),
    ("current", True, None, "its MCP server on the wheelhouse's code"),
    ("deadbeef00000000", True, "stale, the wheelhouse runs", "its MCP server on other code: a pull since it started"),
    ("deadbeef00000000", False, None, "an ended session's last MCP server: nothing to relaunch, so not flagged"),
])
async def test_the_session_info_icon_shows_the_sessions_info(store, sid, tmp_path, monkeypatch, request, size, mcp,
                                                             running, warns, desc):
    """#242: the stats pane's (i), clicked or reached by Tab and Enter, opens the selected
    session's info: its id (C copies it), user, directory, mode, transcript, and its MCP
    server's pid and code, flagged when that isn't the wheelhouse's. It fits at 80x24."""
    from claude_wheelhouse import fingerprint
    transcript_of(tmp_path / "projects/-home-u-repo", sid, usage_rec("m1", 0, 82_000))
    monkeypatch.setattr(transcript, "PROJECTS", tmp_path / "projects")
    if running:
        request.getfixturevalue("live")
    if mcp:
        store.mark_mcp(sid, 4242, fingerprint.code() if mcp == "current" else mcp)
    desc = f"{desc} at {size[0]}x{size[1]}"
    app = WheelhouseApp(store)
    async with app.run_test(size=size) as pilot:
        app.follow(sid)
        app.refresh_data()
        await app.workers.wait_for_complete()
        await pilot.pause()
        pane = app.query_one("#stats")
        at = stats.info_x(pane.size.width)
        assert at is not None and pane.render().plain.split("\n")[0][at] == stats.INFO, f"{desc}: the icon is drawn"
        top = pane.content_region.offset - pane.region.offset
        await pilot.click("#stats", offset=(top.x + at, top.y))
        await pilot.pause()
        assert isinstance(app.screen, tui.SessionInfo), f"{desc}: a click on the icon opens the info"
        box = app.screen.query_one("#dialog")
        assert box.region.right <= size[0] and box.region.bottom <= size[1], f"{desc}: the dialog fits the screen"
        rows = {label: (value, warn) for label, value, warn in tui.info_rows(app.screen.info)}
        assert rows["session"] == (sid, False) and rows["directory"][0] == str(tmp_path), desc
        assert rows["mode"][0] == "terminal tab" and rows["transcript"][0].endswith(f"{sid}.jsonl"), desc
        assert rows["user"][0].startswith(f"{getpass.getuser()} on "), desc
        assert ("pid 4242" in rows["mcp server"][0]) == bool(mcp), desc
        assert rows["mcp server"][1] == bool(warns) and (warns or "") in rows["mcp server"][0], desc
        await pilot.press("c")
        assert app.clipboard == sid, f"{desc}: C copies the session id"
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, tui.SessionInfo), desc
        pane.focus()
        pane.focus_part("info")
        await pilot.pause()
        assert pane.at == "info", f"{desc}: the icon is a Tab stop"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, tui.SessionInfo), f"{desc}: Enter on the icon opens the info"


async def click_zoom(pilot, pane, button, desc):
    """A click on the charts' zoom's - or +, where it's drawn."""
    await pilot.pause()   # drawn, so where the buttons are is known
    start = next(start for start, end, _ in stats.zoom_spans(pane.span, pane.size.width, pane.more)
                 if pane.render().plain.split("\n")[-1][start:end].strip() == button)
    top = pane.content_region.offset - pane.region.offset
    assert pane.line_ys(pane.size.width)[-1] < pane.size.height, f"{desc}: the buttons are inside the pane"
    await pilot.click("#stats", offset=(top.x + start + 1, top.y + pane.line_ys(pane.size.width)[-1]))
    await pilot.pause()


def zoomable(pane, button) -> bool:
    """Whether the zoom's - or + goes anywhere, as drawn."""
    line = pane.render().plain.split("\n")[-1]
    return any(line[start:end].strip() == button for start, end, _ in stats.zoom_spans(pane.span, pane.size.width, pane.more))


def usage_rec(mid, ago, read):
    return {"type": "assistant", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - ago)),
            "message": {"id": mid, "model": "claude-opus-5-5", "content": [],
                        "usage": {"input_tokens": 2, "cache_read_input_tokens": read, "output_tokens": 10}}}


def prompt_rec(ago):
    return {"type": "user", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - ago)),
            "message": {"role": "user", "content": "go on"}}


def transcript_of(folder, sid, *recs):
    folder.mkdir(parents=True, exist_ok=True)
    folder.joinpath(f"{sid}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))


def caption(pane) -> str:
    """The context assembly chart's caption's note, its short form however wide the pane."""
    line = next(line for line in pane.render().plain.split("\n") if "context assembly" in line)
    return line.replace(" tokens/last ", "/").split()[-1]


class HeldLoads:
    """UsageFollower.load held until let go, so a test sees the charts while they fill."""

    def __init__(self, monkeypatch):
        import threading
        self.go, real = threading.Event(), transcript_usage.UsageFollower.load
        monkeypatch.setattr(transcript_usage.UsageFollower, "load", lambda f, cutoff: self.go.wait(10) and real(f, cutoff))


DAY = 86400


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(200, 60), (160, 50)])   # the zoom test's sizes the charts show at
@pytest.mark.parametrize("selected, desc", [
    (True, "a session selected"),
    (False, "none selected: every session's totals"),
])
async def test_zooming_out_fills_the_charts_from_older_history(store, sid, tmp_path, monkeypatch, clock, size,
                                                              selected, desc):
    """- past two hours reads the older history on a worker, the charts saying loading…
    meanwhile, and goes on until the span covers the oldest turn."""
    transcript_of(tmp_path / "projects/-home-u-repo", sid, usage_rec("m1", 3 * DAY, 2000), prompt_rec(2.9 * DAY),
                  usage_rec("m2", 3 * 3600, 1000), prompt_rec(2.5 * 3600), usage_rec("m3", 60, 82_000))
    monkeypatch.setattr(transcript, "PROJECTS", tmp_path / "projects")
    held = HeldLoads(monkeypatch)
    app = WheelhouseApp(store)
    desc = f"{desc} at {size[0]}x{size[1]}"
    async with app.run_test(size=size) as pilot:
        if selected:
            app.follow(sid)
        clock.tick(app)
        await app.workers.wait_for_complete()
        await pilot.pause()
        pane = app.query_one("#stats")
        follower = pane.followers[sid]
        assert not follower.snap.history and zoomable(pane, "-"), f"{desc}: 2h read at start, older left for later"
        assert caption(pane) == "82k/2h", desc
        await click_zoom(pilot, pane, "-", desc)
        assert pane.span == 4 * 3600 and caption(pane) == "loading…", f"{desc}: loading… while it fills"
        assert not app.query("LoadingIndicator") and pane.size.height, \
            f"{desc}: the caption says so, and Textual's own loading cover doesn't hide the pane"
        held.go.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert caption(pane) == "83k/4h" and follower.snap.history, f"{desc}: the 3h-old turn, as history"
        for _ in range(8):
            if not zoomable(pane, "-"):
                break
            await click_zoom(pilot, pane, "-", desc)
            await app.workers.wait_for_complete()
            clock.tick(app)
            await pilot.pause()
        assert pane.span == 4 * DAY and not zoomable(pane, "-"), f"{desc}: - goes until the oldest turn is covered"
        assert caption(pane) == "85k/4d" and follower.done, f"{desc}: every turn in"
        assert [t.context for t in follower.snap.turns] == [82_002], f"{desc}: turns for the last 2h only"


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(200, 64), (160, 64)])   # a session's chart, under its gauges, needs 64 rows
@pytest.mark.parametrize("selected", [True, False])
async def test_a_failed_load_is_tried_once_a_backoff(store, sid, tmp_path, monkeypatch, clock, size, selected):
    """A load that fails isn't started again on every tick: its error shows, steadily, and
    it's tried again once transcript_usage.RETRY has passed."""
    transcript_of(tmp_path / "projects/-home-u-repo", sid, usage_rec("m1", 3 * DAY, 2000), prompt_rec(2.9 * DAY),
                  usage_rec("m2", 60, 82_000))
    monkeypatch.setattr(transcript, "PROJECTS", tmp_path / "projects")
    tries = []

    def failing(f, cutoff):
        tries.append(cutoff)
        raise OSError("disk on fire")
    monkeypatch.setattr(transcript_usage.UsageFollower, "load", failing)
    app = WheelhouseApp(store)
    desc = f"{'selected' if selected else 'totals'} at {size[0]}x{size[1]}"
    async with app.run_test(size=size) as pilot:
        if selected:
            app.follow(sid)
        clock.tick(app)
        await app.workers.wait_for_complete()
        await pilot.pause()
        pane = app.query_one("#stats")
        await click_zoom(pilot, pane, "-", desc)
        for _ in range(5):
            await app.workers.wait_for_complete()
            clock.tick(app)
            await pilot.pause()
            shown = " ".join(pane.render().plain.translate(str.maketrans("│╭╮╰╯─", "      ")).split())
            assert "transcript's history: disk on fire" in shown and caption(pane) != "loading…", f"{desc}: the error, steadily: {shown}"
        assert len(tries) == 1, f"{desc}: one try, not one a tick: {len(tries)}"
        pane.followers[sid].failed -= transcript_usage.RETRY
        clock.tick(app)
        await app.workers.wait_for_complete()
        assert len(tries) == 2, f"{desc}: tried again after the backoff"


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(200, 60), (160, 50)])
async def test_sessions_that_start_and_end_while_zoomed_out(store, sid, tmp_path, monkeypatch, clock, size):
    """With every session's totals zoomed out: a new session's history is read to the span
    too, and one that ends takes its own with it, loading or not."""
    folder = tmp_path / "projects/-home-u-repo"
    transcript_of(folder, sid, usage_rec("a1", 3 * DAY, 2000), prompt_rec(2.9 * DAY), usage_rec("a2", 60, 82_000))
    monkeypatch.setattr(transcript, "PROJECTS", tmp_path / "projects")
    app = WheelhouseApp(store)
    desc = f"at {size[0]}x{size[1]}"
    async with app.run_test(size=size) as pilot:
        clock.tick(app)
        await app.workers.wait_for_complete()
        await pilot.pause()
        pane = app.query_one("#stats")
        for _ in range(8):
            if not zoomable(pane, "-"):
                break
            await click_zoom(pilot, pane, "-", desc)
            await app.workers.wait_for_complete()
            clock.tick(app)
            await pilot.pause()
        assert pane.span == 4 * DAY and caption(pane) == "84k/4d", f"{desc}: {caption(pane)}"
        new = store.create_session(str(tmp_path), name="new")
        transcript_of(folder, new, usage_rec("n1", 0.8 * DAY, 5000), prompt_rec(0.7 * DAY), usage_rec("n2", 30, 1000))
        for _ in range(3):   # listed, its follower read, then its history: each a tick
            clock.tick(app)
            await app.workers.wait_for_complete()
            await pilot.pause()
        assert caption(pane) == "90k/4d" and not zoomable(pane, "-"), \
            f"{desc}: the new session's day of history in, 4d still covering every turn: {caption(pane)}"
        held = HeldLoads(monkeypatch)
        await click_zoom(pilot, pane, "+", desc)
        await click_zoom(pilot, pane, "+", desc)   # 1d: the new one's history is in, the old one's too
        assert pane.span == DAY and zoomable(pane, "-"), desc
        later = store.create_session(str(tmp_path), name="later")
        transcript_of(folder, later, usage_rec("l1", 8 * 3600, 700), prompt_rec(7 * 3600), usage_rec("l2", 30, 300))
        clock.tick(app)
        await app.workers.wait_for_complete()
        clock.tick(app)   # its first read done: its history, held, loading
        await pilot.pause()
        assert caption(pane) == "loading…", desc
        store.end(later)
        clock.tick(app)
        await pilot.pause()
        assert caption(pane) != "loading…" and later not in pane.followers, \
            f"{desc}: ended while loading: its load no longer waited on: {caption(pane)}"
        store.end(sid)
        clock.tick(app)
        await pilot.pause()
        held.go.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert caption(pane) == "6k/1d" and not zoomable(pane, "-"), \
            f"{desc}: the ended session's history gone with it, - now as far as the new one's: {caption(pane)}"


@pytest.mark.anyio
async def test_the_stats_pane_never_reads_on_the_ui_thread(store, sid, monkeypatch):
    import threading
    from claude_wheelhouse import transcript_usage
    ui = threading.get_ident()
    release, threads = threading.Event(), []

    def slow_read(self, now=None):
        threads.append(threading.get_ident())
        release.wait(5)
        self.ready = True
        return True
    monkeypatch.setattr(transcript_usage.UsageFollower, "read", slow_read)
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
    from claude_wheelhouse import transcript_usage

    def broken(self, now=None):
        raise FileNotFoundError("transcript gone")
    monkeypatch.setattr(transcript_usage.UsageFollower, "read", broken)
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
    (False, True, "the shimmer and the eyes run while the answer box is idle"),
    (True, False, "the shimmer and the eyes rest while you type (D41)"),
])
@pytest.mark.both
async def test_the_stats_shimmer_and_eyes_rest_while_typing(store, sid, monkeypatch, frame_clock, focus_answer, moves,
                                                             desc):
    monkeypatch.setattr(WheelhouseApp, "busy", lambda self, s: True)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        if focus_answer:
            app.answer.focus()
            await pilot.pause()
        before = app.query_one("#stats").frame
        eye = app.session_list.eye_frame
        for _ in range(4):
            app.animate()
            app.refresh_data()   # nor does the refresh move them
        assert (app.query_one("#stats").frame != before) == moves, desc
        assert (app.session_list.eye_frame != eye) == moves, desc


@pytest.mark.anyio
@pytest.mark.parametrize("fps, interval, desc", [
    (None, 0.1, "the default: 10 a second"),
    ("2.5", 0.4, "a fraction"),
    ("0", None, "0: no animation at all"),
])
async def test_the_animation_runs_at_the_rate_given(store, sid, monkeypatch, fps, interval, desc):
    monkeypatch.setattr(WheelhouseApp, "busy", lambda self, s: True)
    if fps is not None:
        features.set_animation_fps(fps)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert (app.animation and app.animation._interval) == interval, desc
        if interval is None:   # still, and drawn: the title's first look, the busy row's eye where it starts
            await pilot.pause(0.3)
            app.tick()
            assert (app.frame, app.session_list.eye_frame) == (0, 0), desc
            assert app.title_bar.look == tui.shimmer_phase(0), desc
            assert sid in app.session_list.eyes, desc


@pytest.mark.anyio
@pytest.mark.parametrize("fps, desc", [
    ("10", "the default"),
    ("2", "a fifth of the frames"),
    ("2.5", "a fraction"),
    ("30", "the most"),
])
async def test_the_animation_moves_at_the_same_speed_at_any_rate(store, sid, monkeypatch, fps, desc):
    """Wherever 10 fps puts the title's shimmer, the eyes and the bars after a time, every
    rate puts them there too: a lower one draws fewer frames, not slower ones."""
    monkeypatch.setattr(WheelhouseApp, "busy", lambda self, s: True)
    at = [100.0]
    monkeypatch.setattr(WheelhouseApp, "now", staticmethod(lambda: at[0]))
    features.set_animation_fps(fps)
    rate = float(fps)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        for n in range(1, round(rate * 6) + 1):   # six seconds of this rate's frames
            at[0] = 100.0 + n / rate
            app.animate()
            if n % round(rate * 2) == 0:   # every 2 s: where 10 fps has 20 more frames
                seconds = round(n / rate)
                frame = seconds * tui.FRAMES
                assert app.title_bar.look == tui.shimmer_phase(frame), f"{desc}: the shimmer at {seconds} s"
                assert app.session_list.eye_frame == frame, f"{desc}: the eyes at {seconds} s"
                assert app.query_one("#stats").frame == frame // 2, f"{desc}: the bars at {seconds} s"


@pytest.mark.anyio
@pytest.mark.parametrize("busy, steps, animating, desc", [
    (False, (), True, "focused, as a terminal that never reports focus counts: runs"),
    (False, ("blur",), False, "unfocused, nothing busy: rests"),
    (True, ("blur",), True, "unfocused, a session busy: runs"),
    (False, ("blur", "focus"), True, "focus back: runs again"),
    (False, ("blur", "busy"), True, "a session turns busy while unfocused: runs again"),
    (True, ("blur", "idle"), False, "the busy session goes quiet while unfocused: rests"),
    (False, ("blur", "busy", "idle", "focus"), True, "rests, runs, rests, then focus: runs"),
])
async def test_the_animation_rests_unfocused_only_when_nothing_is_busy(store, sid, monkeypatch, busy, steps,
                                                                        animating, desc):
    state = {"busy": busy}
    monkeypatch.setattr(WheelhouseApp, "busy", lambda self, s: state["busy"])
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        for step in steps:
            if step in ("blur", "focus"):
                app.post_message(events.AppBlur() if step == "blur" else events.AppFocus())
            else:
                state["busy"] = step == "busy"
                app.refresh_data()
            await pilot.pause()
        assert app.animating is animating, desc
        frame, title = app.frame, app.title_bar.look
        await pilot.pause(0.4)
        assert (app.frame > frame) is animating, f"{desc}: frames {frame} to {app.frame}"
        assert (app.title_bar.look != title) is animating, f"{desc}: the shimmer moves only while it runs"


def cells(strip) -> list[tuple]:
    """A drawn line, character by character with its style: the same look whatever its segments."""
    return [(ch, seg.style) for seg in strip for ch in seg.text]


@pytest.mark.anyio
@pytest.mark.parametrize("on_busy, focused, desc", [
    (True, True, "the cursor on the busy row, the list focused"),
    (False, True, "the cursor on another row, the list focused"),
    (True, False, "the cursor on the busy row, the list not focused"),
    (False, False, "the cursor on another row, the list not focused"),
])
async def test_the_eyes_look_as_when_they_were_the_tables_data(store, sid, tmp_path, monkeypatch, on_busy, focused, desc):
    other = store.create_session(str(tmp_path), name="calm")
    monkeypatch.setattr(WheelhouseApp, "busy", lambda self, s: s == sid)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        t = app.session_list
        t.focus() if focused else app.items_table.focus()
        t.move_cursor(row=t.get_row_index(sid if on_busy else other))
        await pilot.pause()
        for frame in [f for f in range(0, 26, 5) if not tui.dot_dimmed(f)]:   # a dot dimmed was never data
            t.sweep(frame)
            got = [cells(t.render_line(y)) for y in range(t.size.height)]
            t.eyes = set()   # as it was: the eye in the table's data
            t.update_cell(sid, app.eye_col, cylon(frame))
            want = [cells(t.render_line(y)) for y in range(t.size.height)]
            t.update_cell(sid, app.eye_col, Text(" " * SessionList.EYE))
            t.eyes = {sid}
            assert got == want, f"{desc}, frame {frame}"


@pytest.mark.anyio
async def test_an_eye_frame_draws_the_busy_rows_alone(store, sid, tmp_path, monkeypatch):
    features.set_animation_fps("0")   # no frames but the test's: a timer's sweep would leave nothing to draw
    store.create_session(str(tmp_path), name="calm")
    monkeypatch.setattr(WheelhouseApp, "busy", lambda self, s: s == sid)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        t = app.session_list
        drawn = []
        monkeypatch.setattr(t, "refresh_row", lambda row: drawn.append(row))
        count = t._update_count
        app.sweep_eyes()
        assert t._update_count == count, "the table's data is as it was: its caches stand"
        assert drawn == [t.get_row_index(sid)], "the busy row alone is drawn again"


@pytest.mark.anyio
async def test_a_cursor_move_draws_only_the_rows_it_leaves_and_lands_on(store, sid, tmp_path):
    for n in range(3):
        store.create_session(str(tmp_path), name=f"s{n}")
    store.set_parked(sid, True)   # at the foot of the list's room, below blank rows
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 50)) as pilot:   # room for blank rows under the sessions
        await pilot.pause()
        t = app.session_list
        assert any(t.blank(row) for row in range(t.row_count))
        t.focus()
        await pick(app, pilot, t.ordered_rows[0].key.value)   # the cursor shown, on the first row
        lines = range(t.size.height)
        for y in lines:
            t.render_line(y)
        drawn = []
        draw = t._render_line_in_row
        t._render_line_in_row = lambda key, *a, **k: (drawn.append(key.value), draw(key, *a, **k))[1]
        await pilot.press("down")
        await pilot.pause()
        assert t.cursor_row == 1
        for y in lines:
            t.render_line(y)
        assert set(drawn) == {t.ordered_rows[0].key.value, t.ordered_rows[1].key.value}, \
            "the rows the cursor left and landed on, not the blank ones or the rest"
        drawn.clear()
        fill(t, [(r.key.value, ("" if t.blank(i) else "x",) * len(t.columns)) for i, r in enumerate(t.ordered_rows)])
        for y in lines:
            t.render_line(y)
        assert drawn and not any(k and k.startswith(BLANK) for k in drawn), "a change to the data leaves the blank rows be"
        drawn.clear()
        rows = [(r.key.value, t.get_row_at(i)) for i, r in enumerate(t.ordered_rows)]
        assert fill(t, [rows[1], rows[0], *rows[2:]]), "rebuilt, through clear()"
        for y in lines:
            t.render_line(y)
        assert drawn and not any(k and k.startswith(BLANK) for k in drawn), "a rebuild leaves the blank rows be"
        drawn.clear()
        t.zebra_stripes = True
        for y in lines:
            t.render_line(y)
        assert any(k and k.startswith(BLANK) for k in drawn), "a new look draws the blank rows again"


@pytest.mark.anyio
async def test_typing_redraws_the_hint_only_when_it_changes(store, sid, monkeypatch):
    store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.items_table.move_cursor(row=0)
        await refresh(pilot, sid)
        app.answer.focus()
        await pilot.pause()
        drawn = []
        monkeypatch.setattr(Hint, "update", lambda self, *a, **kw: drawn.append(a))
        await pilot.press(*"sqlite")
        assert drawn == [], "a key that changes no suggestion leaves the hint alone"
        await pilot.press(*" :smi")
        assert drawn and all(x != y for x, y in zip(drawn, drawn[1:])), "suggestions redraw only as they change"
        assert all(a[0] != app.query_one("#detail-pane .answer-hint", Hint).base for a in drawn), \
            "a key typed in a code never flickers the keys' hint back (#201)"


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
@pytest.mark.both
@pytest.mark.parametrize("name, stale, width, shown, desc", [
    ("Fiskaly", False, 39, "Fiskaly", "a short name as it is"),
    ("Fiskaly", False, 30, "Fiskaly", "a short name as it is in a narrower pane"),
    ("Check use of Creditsafe bulk data", False, 39, "Check use of Cr…", "a long name cut to the room left"),
    ("Check use of Creditsafe bulk data", False, 30, "Check …", "cut further in a narrower pane"),
    ("Check use of Creditsafe bulk data", False, 60, "Check use of Creditsafe bulk data", "whole in a wider one"),
    ("Check use of Creditsafe bulk data", True, 39, "Check use of … ⟳", "the text cut, never its ⟳"),
])
async def test_the_session_list_keeps_its_counts_in_sight(store, tmp_path, via, monkeypatch, name, stale, width, shown, desc):
    sid = store.create_session(str(tmp_path), name=name, ticket="#16", brief="b")
    store.post_item(sid, "question", "which db?")
    store.post_item(sid, "decision", "chose sqlite", why="local, swap later")
    app = WheelhouseApp(store)
    monkeypatch.setattr(app, "stale", lambda s: stale)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        pane = app.query_one("#sessions-pane")
        pane.styles.width = pane.styles.min_width = width   # resized after the first paint at 39
        await pilot.pause()
        await pilot.pause()
        table = app.query_one("#session-list", SessionList)
        assert table.virtual_size.width <= table.scrollable_content_region.width, desc
        assert table.render_line(0).text.split() == ["*", "session", "ctx", "?", "D", "✉"], desc
        assert table.render_line(1).text.split() == ["●", *shown.split(), "1", "1"], desc

@pytest.mark.anyio
@pytest.mark.parametrize("context, compact, runner, chars, desc", [
    (None, False, "tab", "", "no transcript yet: no bar"),
    (350_000, False, "tab", "▃", "350k: into the third 125k"),
    (90_000, False, "tab", "▁", "90k: within the first 125k"),
    (350_000, True, "sdk", "▁", "compacted since the last response: the host's 90k count stands in (#54)"),
    (350_000, True, "tab", "▃", "a tab session has no host count: the transcript's size until the next response"),
    (350_000, False, "sdk", "▃", "no compaction since the last response: the transcript's size is exact"),
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


@pytest.mark.anyio
@pytest.mark.parametrize("context, status, parked, colour, desc", [
    (None, "live", False, "#00ff41", "live, no transcript yet: green"),
    (199_999, "live", False, "#00ff41", "live, just under the Compact question's size: green"),
    (tui.NUDGE_TOKENS, "live", False, "#ff9f1c", "live, at the Compact question's size: amber"),
    (350_000, "live", False, "#ff9f1c", "live, past it: amber"),
    (350_000, "dead", False, "#ff2a6d", "dead, however large: red"),
    (350_000, "stalled", False, "#ffd300", "stalled, however large: yellow"),
    (350_000, "live", True, "#777777", "parked, however large: grey"),
])
@pytest.mark.both
async def test_a_live_sessions_dot_is_amber_with_a_large_context(store, sid, tmp_path, monkeypatch, context, status,
                                                                 parked, colour, desc):
    """Doug (#16): "status icon green for active, red for dead - good. But let's add amber for
    large context. Same threshold as the current prompt to compact, currently 200k." """
    if context is not None:
        folder = tmp_path / "projects/-home-u-repo"
        folder.mkdir(parents=True)
        rec = {"type": "assistant", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - 60)),
               "message": {"id": "m1", "model": "claude-opus-5-5", "content": [],
                           "usage": {"input_tokens": 10, "cache_read_input_tokens": context - 10, "output_tokens": 5}}}
        folder.joinpath(f"{sid}.jsonl").write_text(json.dumps(rec) + "\n")
    if parked:
        store.set_parked(sid, True)
    monkeypatch.setattr(transcript, "PROJECTS", tmp_path / "projects")
    monkeypatch.setattr(WheelhouseApp, "shown_status", lambda self, s: status)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        app.refresh_data()
        await pilot.pause()
        dot = app.query_one("#session-list", DataTable).get_row(sid)[0]   # by key: a parked row sinks
    assert dot.style.split()[-1] == colour, desc


def drawn_dot(app, sid) -> str:
    """The colour a session's dot is drawn in, as its line renders."""
    t = app.session_list
    y = t.header_height + t.get_row_index(sid)
    return next(s.style for s in t._render_line(y, 0, t.size.width, t.rich_style) if "●" in s.text).color.name


@pytest.mark.anyio
@pytest.mark.parametrize("busy, fps, desc", [
    (True, "10", "busy: its colour for 0.7 s, then dimmed for 0.7 s"),
    (False, "10", "idle: steady"),
    (True, "0", "busy, animation off: steady"),
])
async def test_a_busy_sessions_dot_flashes(store, sid, monkeypatch, clock, busy, fps, desc):
    """Doug (#16): "When a session is actually doing something, make that status icon flash
    between on and dimmed." Then: half a second is too quick, 0.7 s each."""
    features.set_animation_fps(fps)
    monkeypatch.setattr(WheelhouseApp, "busy", lambda self, s: busy)
    monkeypatch.setattr(WheelhouseApp, "shown_status", lambda self, s: "live")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.refresh_data()
        seen = []
        for _ in range(4 * tui.BLINK_FRAMES):
            if fps != "0":
                clock.animate(app)   # one frame on (frame_clock)
            await pilot.pause()
            seen.append(drawn_dot(app, sid))
    full, dim = "#00ff41", "#00661a"   # the live green, and at tui.DIM of it
    runs = [(colour, len(list(run))) for colour, run in itertools.groupby(seen)]
    if busy and fps != "0":   # whole runs between the first and last, which the frames seen may cut
        assert [c for c, _ in runs] == [(full, dim)[i % 2] for i in range(len(runs))], desc
        assert {n for _, n in runs[1:-1]} == {7} and len(runs) >= 4, f"{desc}: {runs}"
    else:
        assert runs == [(full, len(seen))], desc


@pytest.mark.anyio
@pytest.mark.parametrize("first, then, row, cell, desc", [
    (6, 7, False, True, "the dot dims, the eye still: the dot's cell alone drawn again"),
    (27, 28, True, False, "the dot back to its colour as the eye moves: the row drawn again, once"),
    (0, 4, True, False, "the eye moved, the dot as it was: the row drawn again"),
    (0, 1, False, False, "neither changed: nothing drawn again"),
])
async def test_a_dot_that_flashes_draws_its_cell_alone(store, sid, monkeypatch, first, then, row, cell, desc):
    monkeypatch.setattr(WheelhouseApp, "busy", lambda self, s: s == sid)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        t = app.session_list
        t.sweep(first)
        rows, cells_ = [], []
        monkeypatch.setattr(t, "refresh_row", lambda r: rows.append(r))
        monkeypatch.setattr(t, "refresh_coordinate", lambda c: cells_.append(c))
        t.sweep(then)
        assert (bool(rows), cells_ == [(t.get_row_index(sid), 0)]) == (row, cell), desc


def shown_buttons(app) -> set[str]:
    return {b.id for b in app.screen.query("SendBar Button, PermissionButtons Button, #conversation-buttons Button")
            if b.display and b.parent.display}


@pytest.mark.anyio
@pytest.mark.parametrize("press, status, decision, desc", [
    ("#allow", "allowed", "allow", "allow once"),
    ("#always", "allowed", "always", "allow, and keep the rule"),
])
@pytest.mark.both
async def test_permission_buttons(store, tmp_path, press, status, decision, desc):
    sid = store.create_session(str(tmp_path), name="hosted", runner="sdk")
    ref = store.post_item(sid, "permission", "Bash: rm -rf build")
    app = WheelhouseApp(store)
    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        await to_first_item(app, pilot)
        assert {"allow", "always", "deny"} <= shown_buttons(app), desc
        await pilot.click(press)
        await pilot.pause()
        assert not {"allow", "always", "deny"} & shown_buttons(app), f"{desc}: answered, the buttons go"
    item = store.item(sid, ref)
    assert item["status"] == status and json.loads(item["answer"])["decision"] == decision, desc


@pytest.mark.anyio
@pytest.mark.parametrize("where, press, typed, then, status, message, desc", [
    ("inbox", "alt+d", "use rg", "enter", "denied", "use rg", "Deny, a reason, Enter: denied with it"),
    ("inbox", "#deny", "use rg", "enter", "denied", "use rg", "the Deny button, as its key"),
    ("inbox", "alt+d", "", "enter", "denied", "", "Deny then Enter: a plain deny"),
    ("inbox", "alt+d", "use rg", "escape", "open", None, "Deny then Esc: nothing sent, the buttons back"),
    ("inbox", "alt+y", "", None, "allowed", "", "Allow: at once, no reason asked"),
    ("thread", "alt+d", "use rg", "enter", "denied", "use rg", "in a full-screen item, denied with it"),
    ("thread", "alt+d", "use rg", "escape", "open", None, "Esc there: the buttons back, the item still open"),
])
@pytest.mark.both
async def test_denys_optional_reason(store, tmp_path, where, press, typed, then, status, message, desc):
    """Doug: Deny shows a one-line box for an optional reason, its caption saying what it's for;
    Enter sends it (empty: a plain deny), Esc goes back. Allow stays one press."""
    sid = store.create_session(str(tmp_path), name="hosted", runner="sdk")
    ref = store.post_item(sid, "permission", "Bash: rm -rf build")
    app = WheelhouseApp(store)
    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        await to_first_item(app, pilot)
        if where == "thread":
            app.push_screen(ThreadView(sid, ref))
            await pilot.pause()
        await (pilot.click(press) if press.startswith("#") else pilot.press(press))
        await pilot.pause()
        reason, buttons = app.screen.query_one(DenyReason), app.screen.query_one(PermissionButtons)
        if then:
            assert reason.display and not buttons.display, f"{desc}: the reason in place of the buttons"
            assert app.focused is reason.query_one(Input) and "Why deny?" in str(reason.query_one(Label).content), desc
            assert {h.base for h in app.screen.query(Hint)} == {DENY_HINT}, f"{desc}: the hint fits the reason box"
            await pilot.press(*[k if k != " " else "space" for k in typed], then)
            await pilot.pause()
        assert not reason.display, f"{desc}: the reason gone"
        assert buttons.display == (status == "open"), f"{desc}: the buttons back while it asks"
        assert isinstance(app.screen, ThreadView) == (where == "thread"), f"{desc}: Esc leaves only the reason"
    item = store.item(sid, ref)
    assert item["status"] == status, desc
    assert (json.loads(item["answer"])["message"] if item["answer"] else None) == message, desc


@pytest.mark.anyio
@pytest.mark.parametrize("where, desc", [
    ("inbox", "focus clicked away from the reason in the inbox: Esc still goes back"),
    ("thread", "in a full-screen item: Esc goes back, the item stays open"),
])
async def test_deny_reason_esc_without_focus(store, tmp_path, where, desc):
    """Esc while Deny's reason shows goes back to the buttons, nothing sent, wherever focus is."""
    sid = store.create_session(str(tmp_path), name="hosted", runner="sdk")
    ref = store.post_item(sid, "permission", "Bash: rm -rf build")
    app = WheelhouseApp(store)
    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        await to_first_item(app, pilot)
        if where == "thread":
            app.push_screen(ThreadView(sid, ref))
            await pilot.pause()
        await pilot.press("alt+d")
        await pilot.pause()
        reason, buttons = app.screen.query_one(DenyReason), app.screen.query_one(PermissionButtons)
        assert reason.display, desc
        app.screen.set_focus(app.items_table if where == "inbox" else None)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert not reason.display and buttons.display, f"{desc}: the buttons back"
        assert {h.base for h in app.screen.query(Hint)} == {PERMISSION_HINT}, f"{desc}: the hint back"
        assert isinstance(app.screen, ThreadView) == (where == "thread"), f"{desc}: Esc leaves only the reason"
    assert store.item(sid, ref)["status"] == "open", desc


@pytest.mark.anyio
@pytest.mark.parametrize("kind, answer, live, box, permission, compact, hint_, desc", [
    ("permission", None, True, False, True, False, PERMISSION_HINT, "a permission: its buttons alone, no answer box"),
    ("question", COMPACT_QUESTION, True, True, False, True, COMPACT_HINT,
     "the question suggesting Compact: its buttons, and the box for its instructions"),
    ("question", COMPACT_QUESTION, False, True, False, False, None,
     "its host not running: no Compact, as the conversation's own button"),
    ("question", None, True, True, False, False, None, "any other question: the box alone"),
])
@pytest.mark.both
async def test_what_shows_over_the_answer_box(store, tmp_path, monkeypatch, kind, answer, live, box, permission, compact,
                                              hint_, desc):
    """Doug: a permission is answered by its buttons, with no text area; the Compact question
    keeps one, for custom instructions."""
    from claude_wheelhouse import liveness
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "live" if live else "dead")
    sid = store.create_session(str(tmp_path), name="hosted", runner="sdk")
    ref = store.post_item(sid, kind, "Bash: rm -rf build" if kind == "permission" else "context 220k: compact?")
    if answer:
        store.set_answer(sid, ref, answer)
    app = WheelhouseApp(store)
    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        await to_first_item(app, pilot)
        assert app.selected == (sid, ref), desc
        shown = (app.answer.display, app.query_one(PermissionButtons).display, app.query_one(CompactButtons).display)
        assert shown == (box, permission, compact), desc
        assert (app.query_one(Hint).base == hint_) if hint_ else app.query_one(Hint).base not in (PERMISSION_HINT, COMPACT_HINT), desc


@pytest.mark.anyio
@pytest.mark.parametrize("typed, then, press, command, plan, status, desc", [
    ("", None, "keys:alt+x,alt+y", "compact", (None, "park"), "open", "keys: Alt+X chooses park, Alt+Y compacts"),
    ("", None, "keys:alt+d,alt+y", "compact", (None, "compact"), "open", "Alt+D chooses just compact"),
    ("", None, "keys:alt+w", None, (None, None), "closed", "Alt+W leaves it"),
    ("", None, "#compact-now", "compact", (None, "send"), "open",
     "Compact: the session is asked what to keep, then its queued answers go with the reminder (the default)"),
    ("keep the PR list", "#compact-then-park", "#compact-now", "compact", ("keep the PR list", "park"), "open",
     "instructions typed, and park chosen"),
    ("keep the PR list", "#compact-then-compact", "ctrl+enter", "compact", ("keep the PR list", "compact"), "open",
     "Ctrl+Enter compacts with the box's text, just compact chosen"),
    ("", None, "#leave-it", None, (None, None), "closed", "Leave it closes it, and nothing is sent"),
])
@pytest.mark.both
async def test_the_compact_question(store, tmp_path, monkeypatch, typed, then, press, command, plan, status, desc):
    from claude_wheelhouse import liveness
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "live")
    sid = store.create_session(str(tmp_path), name="hosted", runner="sdk")
    store.mark_version(sid)   # on current code: an older host is never given a cycle (#244)
    ref = store.post_item(sid, "question", "context 220k: compact?")
    store.set_answer(sid, ref, COMPACT_QUESTION)
    app = WheelhouseApp(store)
    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        await to_first_item(app, pilot)
        app.answer.text = typed
        if then:
            await pilot.click(then)
        if press.startswith("#"):
            await pilot.click(press)
        elif press.startswith("keys:"):
            app.items_table.focus()
            for key in press[5:].split(","):
                await pilot.press(key)
                await pilot.pause()
        else:
            app.answer.focus()
            await pilot.press(press)
        await pilot.pause()
        left = app.answer.text
    assert (store.session(sid)["host_command"], store.compact_plan(sid)) == (command, plan), desc
    assert store.item(sid, ref)["status"] == status, desc
    assert store.drafts(sid) == [] and store.pending(sid) == [], f"{desc}: nothing queued or sent to the session"
    assert left == ("" if command and typed else typed), f"{desc}: the instructions leave the box"


@pytest.mark.anyio
@pytest.mark.parametrize("press, abort, desc", [
    ("#keep-waiting", False, "Keep waiting: closed, nothing interrupted"),
    ("#abort-compaction", True, "Abort: the notes turn interrupted, which cancels the compaction"),
    ("keys:alt+y", False, "Alt+Y: Keep waiting"),
    ("keys:alt+d", True, "Alt+D: Abort"),
])
@pytest.mark.both
async def test_the_notes_wait_question(store, tmp_path, monkeypatch, press, abort, desc):
    """#244: past NOTES_TIMEOUT the host asks whether to keep waiting for a compaction's notes;
    its buttons over the answer box, in place of the question suggesting Compact's."""
    from claude_wheelhouse import liveness
    from claude_wheelhouse.store import NOTES_WAIT
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "live")
    sid = store.create_session(str(tmp_path), name="hosted", runner="sdk")
    store.command(sid, "compact")
    store.take_command(sid)
    ref = store.post_marked_question(sid, "notes: keep waiting?", "", NOTES_WAIT)
    app = WheelhouseApp(store)
    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        await to_first_item(app, pilot)
        await pilot.pause()
        assert app.query_one(CompactButtons).display, desc
        assert [b.id for b in app.query_one(CompactButtons).query(Button) if b.display] == \
            ["keep-waiting", "abort-compaction"], f"{desc}: only its own buttons"
        if press.startswith("#"):
            await pilot.click(press)
        else:
            app.items_table.focus()
            await pilot.press(press[5:])
        await pilot.pause()
    assert store.item(sid, ref)["status"] == "closed", desc
    assert store.session(sid)["host_command"] == ("interrupt" if abort else None), desc


@pytest.mark.anyio
@pytest.mark.parametrize("mode_, desc", [("queued", "queued mode"), ("immediate", "Immediate mode")])
async def test_a_refused_send_keeps_the_text_and_says_why(store, tmp_path, monkeypatch, mode_, desc):
    """#244: during a compaction cycle a submit is refused, said in the one wording, and the
    box keeps what was typed."""
    from claude_wheelhouse import liveness
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "live")
    sid = store.create_session(str(tmp_path), name="hosted", runner="sdk")
    store.set_mode(sid, mode_)
    store.post_item(sid, "question", "which db?")
    store.command(sid, "compact")
    store.db.execute("DELETE FROM items WHERE answer = '{\"from\": \"compaction\"}'")   # its task: refused apart
    app = WheelhouseApp(store)
    toasts = []
    monkeypatch.setattr(app, "notify", lambda text, **kw: toasts.append(text))
    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        await to_first_item(app, pilot)
        app.answer.text = "use Postgres"
        app.answer.focus()
        await pilot.press("ctrl+enter")
        await pilot.pause()
        left = app.answer.text
    assert left == "use Postgres", f"{desc}: the box keeps the text"
    assert "hosted is compacting: send once it's done, or Interrupt to cancel the compaction." in toasts, \
        f"{desc}: {toasts}"
    assert not store.drafts(sid) and not store.pending(sid), desc


@pytest.mark.anyio
@pytest.mark.parametrize("key, desc", [
    ("alt+y", "allowed: the box back, and the focus in it"),
    ("alt+d,enter", "denied: the same"),
])
async def test_a_threads_box_takes_the_focus_back_after_a_permission(store, tmp_path, key, desc):
    sid = store.create_session(str(tmp_path), name="hosted", runner="sdk")
    ref = store.post_item(sid, "permission", "Bash: rm -rf build")
    app = WheelhouseApp(store)
    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        app.push_screen(ThreadView(sid, ref))
        await pilot.pause()
        await pilot.pause()
        assert not app.screen.box.display and app.screen.focused is not app.screen.box, f"{desc}: no box while it asks"
        await pilot.press(*key.split(","))
        for _ in range(3):
            await pilot.pause()
        assert app.screen.box.display and app.screen.focused is app.screen.box, desc


@pytest.mark.anyio
@pytest.mark.parametrize("typed, then, plan, desc", [
    ("", None, (None, "send"), "nothing typed: the session is asked what to keep; its queue sent after (the default)"),
    ("keep Q3", "#compact-dialog-then-park", ("keep Q3", "park"), "instructions, then park"),
    ("", "#compact-dialog-then-compact", (None, "compact"), "just compact"),
])
async def test_the_compact_button_asks_how(store, tmp_path, monkeypatch, typed, then, plan, desc):
    from claude_wheelhouse import liveness
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "live")
    sid = store.create_session(str(tmp_path), name="hosted", runner="sdk")
    store.mark_version(sid)   # on current code: an older host is never given a cycle (#244)
    store.post_item(sid, "task", "work")
    app = WheelhouseApp(store)
    async with app.run_test(size=(180, 40)) as pilot:
        await pick(app, pilot, sid)
        await pilot.click("#compact")
        await pilot.pause()
        assert isinstance(app.screen, CompactSession), desc
        app.screen.query_one("#compact-notes", TextArea).text = typed
        if then:
            await pilot.click(then)
        await pilot.click("#compact-go")
        await pilot.pause()
    assert (store.session(sid)["host_command"], store.compact_plan(sid)) == ("compact", plan), desc


@pytest.mark.anyio
@pytest.mark.parametrize("warm, untick, compact, desc", [
    (True, False, True, "a warm cache: Compact first ticked"),
    (False, False, False, "a cold one: unticked, as compacting saves nothing"),
    (True, True, False, "unticked by hand: parked without compacting"),
])
async def test_park_offers_to_compact_first_by_cache_warmth(store, tmp_path, monkeypatch, sdk_on, warm, untick, compact,
                                                         desc):
    from claude_wheelhouse import liveness
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "live")
    sid = store.create_session(str(tmp_path), name="hosted", runner="sdk")
    app = WheelhouseApp(store)
    monkeypatch.setattr(app.client, "warm", lambda s: warm)
    async with app.run_test(size=(180, 40)) as pilot:
        await pick(app, pilot, sid)
        await pilot.click("#park")
        await pilot.pause()
        assert isinstance(app.screen, ParkSession), desc
        assert app.screen.query_one("#park-compact", Checkbox).value == warm, f"{desc}: ticked by warmth"
        if untick:
            await pilot.click("#park-compact")
        await pilot.click("#park-go")
        await pilot.pause()
    row = store.session(sid)
    assert (bool(row["park_requested_at"]), bool(row["park_compact"])) == (True, compact), desc


@pytest.mark.anyio
@pytest.mark.parametrize("runner_, warm, keys, asked, stopped, desc", [
    ("sdk", True, ("enter",), 1, 0, "hosted, a warm cache: Let it know by default, the end request"),
    ("sdk", False, ("enter",), 0, 1, "hosted, a cold one: Just stop it by default, stopped and forgotten"),
    ("tab", True, ("enter",), 1, 0, "a tab, a warm cache: Let it know by default"),
    ("tab", False, ("enter",), 0, 1, "a tab, a cold one: Just stop it by default"),
    ("sdk", False, ("alt+l",), 1, 0, "Alt+L lets it know, the cache cold"),
    ("tab", True, ("alt+j",), 0, 1, "Alt+J just stops it, the cache warm"),
    ("sdk", True, ("click:#forget-stop",), 0, 1, "Just stop it clicked"),
    ("tab", False, ("click:#forget-tell",), 1, 0, "Let it know clicked"),
    ("sdk", True, ("escape",), 0, 0, "Esc: neither, the session left be"),
])
async def test_stop_and_forget_offers_a_choice_by_cache_warmth(store, tmp_path, monkeypatch, runner_, warm, keys,
                                                               asked, stopped, desc):
    """Stop & Forget on a running session asks: Let it know (the end request, its end steps)
    or Just stop it (stopped now, its row deleted, nothing asked of it), the default by the
    prompt cache's warmth, as Park's Compact first is. The queued-answers warning stays."""
    from claude_wheelhouse import liveness
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "live")
    sid = store.create_session(str(tmp_path), name="forgettable", runner=runner_)
    store.queue(sid, "an answer")
    app = WheelhouseApp(store)
    stops = []
    monkeypatch.setattr(app.client, "warm", lambda s: warm)
    monkeypatch.setattr(app.client, "cache_expires", lambda s: 1_800_000_000.0)
    monkeypatch.setattr(app.client, "context_tokens", lambda s: 180_000)
    monkeypatch.setattr(app.client, "stop_process", lambda s: stops.append(s["id"]) or True)
    async with app.run_test(size=(180, 40)) as pilot:
        await pick(app, pilot, sid)
        await pilot.click("#end")
        await pilot.pause()
        assert isinstance(app.screen, ForgetChoice), desc
        prompt = app.screen.prompt
        assert "1 queued answer(s) will be discarded" in prompt, f"{desc}: the queued-answers warning"
        why = "warm until" if warm else "cold since"
        assert why in prompt and ("let it know (the default)" if warm else "re-read ~180k") in prompt, \
            f"{desc}: the default and why: {prompt}"
        assert focused_id(app) == ("forget-tell" if warm else "forget-stop"), f"{desc}: Enter takes the default"
        for key in keys:
            await (pilot.click(key.removeprefix("click:")) if key.startswith("click:") else pilot.press(key))
        await pilot.pause()
    row = store.session(sid)
    got = (int(bool(row and row["end_requested_at"])), len(stops), row is None)
    assert got == (asked, stopped, bool(stopped)), f"{desc}: (end requests, stops, forgotten) {got}"


@pytest.mark.anyio
@pytest.mark.parametrize("key, desc", [
    ("alt+l", "Let it know after the session stopped: the confirm keeps the queued-answers warning"),
    ("alt+j", "Just stop it after the session stopped: the same"),
])
async def test_stop_and_forget_stopped_while_open_keeps_queued_warning(store, tmp_path, monkeypatch, key, desc):
    from claude_wheelhouse import liveness
    status = {"now": "live"}
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: status["now"])
    sid = store.create_session(str(tmp_path), name="forgettable", runner="tab")
    store.queue(sid, "an answer")
    app = WheelhouseApp(store)
    monkeypatch.setattr(app.client, "warm", lambda s: True)
    monkeypatch.setattr(app.client, "cache_expires", lambda s: 1_800_000_000.0)
    monkeypatch.setattr(app.client, "context_tokens", lambda s: 180_000)
    async with app.run_test(size=(180, 40)) as pilot:
        await pick(app, pilot, sid)
        await pilot.click("#end")
        await pilot.pause()
        assert isinstance(app.screen, ForgetChoice), desc
        status["now"] = "dead"
        app.refresh_data()
        await pilot.pause()
        await pilot.press(key)
        await pilot.pause()
        assert isinstance(app.screen, Confirm), desc
        assert "1 queued answer(s) will be discarded" in app.screen.prompt, f"{desc}: {app.screen.prompt}"


@pytest.mark.anyio
@pytest.mark.parametrize("runner_, desc", [
    ("sdk", "hosted: a pending end request pressed again offers C cancel / F force, not ForgetChoice"),
    ("tab", "a tab: the same"),
])
async def test_stop_and_forget_pending_keeps_its_choice(store, tmp_path, monkeypatch, runner_, desc):
    from claude_wheelhouse import liveness
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "live")
    sid = store.create_session(str(tmp_path), name="asked", runner=runner_)
    store.request(sid, "end")
    now = [0.0]
    monkeypatch.setattr(tui.Press, "now", staticmethod(lambda: now[0]))
    app = WheelhouseApp(store)
    async with app.run_test(size=(180, 40)) as pilot:
        await pick(app, pilot, sid)
        assert app.session_buttons.query_one("#end", Button).disabled, f"{desc}: off while it's asked (#107)"
        now[0] = tui.BUSY_FOR
        app.refresh_data()
        await pilot.pause()
        await pilot.click("#end")
        await pilot.pause()
        assert isinstance(app.screen, Choice), f"{desc}: on again after BUSY_FOR"


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
        await pick(app, pilot, sid)
        assert ({"interrupt", "compact"} <= shown_buttons(app)) == shown, desc
        assert "shell" in shown_buttons(app), f"{desc}: Shell, a quick shell, shows for any session (#257)"
        activity = str(app.screen.query_one("#activity", Label).content)
        assert (activity == "running Bash: make test") == shown, desc
        if shown:
            await pilot.click("#interrupt")
            await pilot.pause()
            assert store.session(sid)["host_command"] == "interrupt", desc


@pytest.mark.anyio
@pytest.mark.parametrize("on, tick, runner_, desc", [
    (True, False, "sdk", "SDK mode: runs in the wheelhouse by default"),
    (True, True, "tab", "the box opens a tab instead"),
    (False, None, "tab", "SDK mode off: no box, and a tab, whatever WHEELHOUSE_RUNNER says"),
])
async def test_new_session_runner(store, tmp_path, monkeypatch, on, tick, runner_, desc):
    monkeypatch.setattr(features, "_flag", on)
    launched = []
    monkeypatch.setattr(launch, "open_session", lambda s, i, watch=None, may_tab=True: launched.append(s.session(i)["runner"]))
    monkeypatch.setenv("WHEELHOUSE_RUNNER", "sdk")
    app = WheelhouseApp(store)
    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.press("alt+n")
        await pilot.pause()
        assert bool(app.screen.query("#tab")) is on, desc
        assert app.screen.query_one("#model", Select).display, f"{desc}: model and effort are a tab's too"
        app.screen.query_one("#cwd", Input).value = str(tmp_path)
        if tick:
            app.screen.query_one("#tab", Checkbox).value = True
        await pilot.click("#launch")
        await pilot.pause()
    assert launched == [runner_], desc


@pytest.mark.anyio
async def test_new_session_selected_as_it_appears(store, sid, tmp_path, monkeypatch):
    monkeypatch.setattr(launch, "open_session", lambda s, i, watch=None, may_tab=True: None)
    app = WheelhouseApp(store)
    async with app.run_test(size=(180, 40)) as pilot:
        await pick(app, pilot, sid)
        app.session_list.focus()
        await pilot.press("alt+n")
        await pilot.pause()
        app.screen.query_one("#cwd", Input).value = str(tmp_path)
        app.screen.query_one("#name", Input).value = "fresh"
        await pilot.click("#launch")
        await pilot.pause()
        new = next(s["id"] for s in store.sessions() if s["name"] == "fresh")
        assert app.current_session() == new and app.filter_sid == new, "the new session selected, not the one before"


@pytest.mark.anyio
@pytest.mark.parametrize("meanwhile, selected, desc", [
    (None, "new", "nothing changed while it registered: selected as its row appears"),
    ("pick", "other", "another session picked meanwhile: that pick stays"),
    ("type", "old", "typing in the answer box: the selection stays under it"),
])
async def test_new_session_selected_only_if_left_be(store, sid, tmp_path, meanwhile, selected, desc):
    other = store.create_session(str(tmp_path), name="other")
    app = WheelhouseApp(store)
    async with app.run_test(size=(180, 40)) as pilot:
        await pick(app, pilot, sid)
        app.select_on_show("s-later", app.selected)   # launched, its row not yet registered
        await pilot.pause()
        assert app.current_session() == sid, f"{desc}: nothing to select yet"
        if meanwhile == "pick":
            await pick(app, pilot, other)
        elif meanwhile == "type":
            app.answer.focus()
            await pilot.pause()
        store.create_session(str(tmp_path), name="later", sid="s-later")
        app.refresh_data()
        await pilot.pause()
        assert app.current_session() == {"new": "s-later", "other": other, "old": sid}[selected], desc
        assert app.select_new is None, f"{desc}: asked once, not again on a later refresh"


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
    hub = SimpleNamespace(statuses={"s1": "live"}, listed={"s1": s}.get)
    assert Hub.busy(hub, "s1") is busy, desc


@pytest.mark.anyio
@pytest.mark.parametrize("mode, sends_now, expected, desc", [
    (None, False, "Ctrl+Enter: queue", "a new session queues"),
    ("immediate", False, "Ctrl+Enter: send", "immediate mode sends"),
    (None, True, "Ctrl+Enter: send", "a session from before queued answers sends whatever its mode"),
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
        app.items_table.move_cursor(row=first_item(app))
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
        assert str(app.screen.query_one(Hint).content).startswith("Ctrl+Enter: send · ")


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
async def test_new_session_starts_in_the_session_dir(store, tmp_path, monkeypatch):
    monkeypatch.setenv("WHEELHOUSE_SESSION_DIR", str(tmp_path))
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.press("alt+n")
        await pilot.pause()
        assert app.screen.query_one("#cwd", Input).value == str(tmp_path)


@pytest.mark.anyio
async def test_browse_fills_in_the_working_directory(store, tmp_path):
    (tmp_path / "repo").mkdir()
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.press("alt+n")
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
        await pick(app, pilot, sid)
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
        await pick(app, pilot, sid)
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
        await pick(app, pilot, sid)
        if ends == "before the button":   # pressed before the app's next tick hides the button
            store.end(sid)
            monkeypatch.setattr(app, "current_session", lambda: sid)   # the row still showing it
            app.query_one("#rename", Button).press()
        else:
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
    await drag_over_the_question(pilot, app.query_one("#detail", Transcript))


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
    (select_in_the_pane, "from windows", "Q1 · w", None, "a selection in the pane: copied"),
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
        app.query_one("#items", DataTable).move_cursor(row=first_item(app))
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
    (lambda: transcript_usage.context_bar(140_000), lambda: transcript_usage.context_bar(160_000), "a context bar changing grade in the same glyph (#51)"),
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


async def right_click_at(app, selector: str, offset: tuple[int, int]) -> None:
    """A right-click at an offset in a widget, through the app's own event handling (right_click)."""
    args = _get_mouse_message_arguments(app.screen.query_one(selector), offset, button=3)
    for cls in (events.MouseDown, events.MouseUp):
        await app.on_event(cls(**args))


async def session_menu(app, pilot, sid, offset=None):
    """A right-click on the session's row (or at offset in the list), the menu it opens if any,
    and the real buttons' presses recorded rather than acted on."""
    table = app.session_list
    await right_click_at(app, "#session-list", offset or (3, table.header_height + table.keys().index(sid)))
    await pilot.pause()
    got = []
    for id_, *_ in app.session_menu_buttons(next(x for x in app.sessions if x["id"] == sid)):
        if id_ != "handover":   # the menu's alone, no button (#257)
            app.screen_stack[0].query_one(f"#{id_}", Button).press = functools.partial(got.append, id_)
    return got


@pytest.mark.anyio
@pytest.mark.parametrize("runner_, answer, sent, said, desc", [
    ("sdk", True, [("shell", None, None)], None, "a hosted session: asked first, then handed over (the hub's shell)"),
    ("sdk", False, [], None, "No hands nothing over"),
    ("tab", None, [], "Handover to Claude Code in a shell applies only to a session run in the wheelhouse",
     "a tab's: said, nothing asked"),
])
async def test_handover_is_in_the_session_menu(store, tmp_path, monkeypatch, sdk_on, runner_, answer, sent, said, desc):
    """#257: Handover, today's Shell renamed, is the session menu's alone: no button, no key."""
    sid, app = await hosted_app(store, tmp_path, monkeypatch, runner_)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        notes, got = [], []
        app.notify = lambda message, **kw: notes.append(message)
        app.client.host_command = lambda s, what, notes=None, then=None: got.append((what, notes, then)) or tui.Done()
        await session_menu(app, pilot, sid)
        entry = next(e for e in app.screen.buttons if e[0] == "handover")
        assert entry[1] == "Handover to Claude Code in a shell" and entry[3] == (runner_ == "sdk"), desc
        assert not app.screen_stack[0].query("#handover"), f"{desc}: no button of its own"
        app.screen.action_answer("handover")
        await pilot.pause()
        if answer is not None:
            assert isinstance(app.screen, tui.Confirm), desc
            app.screen.action_answer(answer)
            await pilot.pause()
        assert got == sent and (notes[-1] if notes else None) == said, desc
        assert ("handover" in app.busy_buttons(app.row(sid))) == bool(sent), f"{desc}: off till it lands"


@pytest.mark.anyio
@pytest.mark.parametrize("how, select, elsewhere, opened, desc", [
    ("alt+h", "item", False, ["item"], "Alt+H: the selected item's session"),
    ("alt+h", "none", False, [None], "Alt+H with no session in context: home"),
    ("button", "session", False, ["session"], "the Shell button: the highlighted session's"),
    ("thread", "item", False, ["item"], "Alt+H on a full-screen item: its session's"),
    ("alt+h", "item", True, [], "with --connect the tab would open out of sight: said, nothing opened"),
])
async def test_shell_opens_a_quick_shell(store, tmp_path, monkeypatch, how, select, elsewhere, opened, desc):
    """#257: Shell (Alt+H) asks the hub for a quick shell in the session in context's directory;
    nothing about the session changes, and it applies whatever the session is doing."""
    from claude_wheelhouse import liveness
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "live")
    one = store.create_session(str(tmp_path), name="item")
    two = store.create_session(str(tmp_path), name="session")
    ref = store.post_item(one, "question", "Which?")
    names = {one: "item", two: "session"}
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        got, notes = [], []
        app.notify = lambda message, **kw: notes.append(message)
        app.client.open_shell = lambda sid: got.append(names.get(sid)) or tui.Done()
        app.client.elsewhere = elsewhere
        if select == "item":
            await to_first_item(app, pilot)
        elif select == "session":
            await pick(app, pilot, two)
        else:
            app.filter_sid = app.selected = None
        await pilot.pause()
        if how == "thread":
            app.push_screen(ThreadView(one, ref))
            await pilot.pause()
        if how == "button":
            app.screen.query_one("#shell", Button).press()
        else:
            await pilot.press("alt+h")
        await pilot.pause()
        assert got == opened, desc
        assert bool(notes and "isn't this one" in notes[-1]) == elsewhere, desc


@pytest.mark.anyio
@pytest.mark.parametrize("busy, during, keys, pressed, said, desc", [
    (None, None, ["enter"], ["relaunch"], None, "Enter presses the first that applies, on the session clicked"),
    (None, None, ["down", "enter"], ["park"], None, "Down moves to the next"),
    ("relaunch", None, ["enter"], ["park"], None, "a busy button greyed, as under the list, and passed over"),
    (None, "relaunch", ["enter"], ["park"], None, "a button going busy while it's open greys there at the next refresh"),
    (None, "stale", [], [], "Relaunch: still under way", "a choice gone busy since: said, not pressed"),
    (None, None, ["escape"], [], None, "Esc closes it, pressing nothing"),
])
async def test_right_click_on_a_session_opens_its_menu(store, sid, clock, busy, during, keys, pressed, said, desc):
    """#125: a right-click on a session's row selects it and shows its buttons as a menu."""
    other = store.create_session("/tmp", name="other")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        notes = []
        app.notify = lambda message, **kw: notes.append(message)
        if busy:
            app.press(sid, busy)
        app.follow(other)
        clock.tick(app)
        await pilot.pause()
        got = await session_menu(app, pilot, sid)
        menu = app.screen
        assert isinstance(menu, tui.SessionMenu), desc
        assert app.current_session() == sid and app.filter_sid == sid, f"{desc}: the session clicked selected"
        assert menu.buttons == app.session_menu_buttons(next(x for x in app.sessions if x["id"] == sid)), \
            f"{desc}: the buttons under the list, as they show"
        if during:
            app.press(sid, "relaunch")
            clock.tick(app)
            await pilot.pause()
            assert menu.query_one("#menu-relaunch", Button).disabled, f"{desc}: greyed in the menu too"
        if during == "stale":
            menu.action_answer("relaunch")
            await pilot.pause()
        for key in keys:
            await pilot.press(key)
            await pilot.pause()
        assert not isinstance(app.screen, tui.SessionMenu) and got == pressed, desc
        assert (notes[-1] if notes else None) == said, desc


@pytest.mark.anyio
@pytest.mark.parametrize("where, desc", [
    ("header", "the list's header"),
    ("scrollbar", "the list's scrollbar column"),
    ("callout", "a tour callout lying over the list"),
])
async def test_right_click_off_a_sessions_row_opens_no_menu(store, sid, clock, where, desc):
    for i in range(30):   # rows enough for a scrollbar
        store.create_session("/tmp", name=f"s{i}")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        table = app.session_list
        row = (2, table.header_height + 1)
        if where == "callout":
            from claude_wheelhouse.callout import TourCallout
            callout = TourCallout("sessions", "A callout.")
            callout.place = lambda: None   # held over the rows, wherever it would go beside the pane
            await app.screen.mount(callout)
            await pilot.pause()
            base = callout.region   # where it lays out at no offset: the offset moves it from there
            callout.styles.offset = (table.region.x - base.x, table.region.y + table.header_height - base.y)
            await pilot.pause()
            x, y = callout.region.x + 2, callout.region.y + 1
            assert table.content_region.contains(x, y) and y > table.content_region.y + table.header_height, \
                f"{desc}: it lies over a session's row"
            row = (x - table.region.x, y - table.region.y)
        elif where == "scrollbar":
            row = (table.region.width - 1, row[1])   # the list's last column
            under = app.screen.get_widget_at(table.region.x + row[0], table.region.y + row[1])[0]
            assert type(under).__name__ == "ScrollBar", f"{desc}: the scrollbar there"
        else:
            row = (2, 0)
        await session_menu(app, pilot, sid, row)
        assert app.screen is app.screen_stack[0], f"{desc}: copy or paste, as anywhere else"


@pytest.mark.anyio
@pytest.mark.parametrize("size, at, side, desc", [
    ((80, 24), "row", "below", "a right-click on the first row at 80x24: under the row, at the pointer"),
    ((160, 40), "last", "below", "a right-click on the last row in view at 160x40"),
    ((80, 24), (3, 20), "above", "no room below at 80x24: over the row instead"),
    ((160, 40), (3, 36), "above", "no room below at 160x40"),
    ((80, 24), (75, 3), "below", "near the right edge at 80x24: kept on the screen across"),
    ((160, 40), (155, 3), "below", "near the right edge at 160x40"),
])
async def test_a_sessions_menu_opens_at_its_row(store, sid, clock, size, at, side, desc):
    """#137: the menu opens below the session's row, or above it without the room below; not mid-screen."""
    for i in range(30):
        store.create_session("/tmp", name=f"s{i}")
    app = WheelhouseApp(store)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        table = app.session_list
        if isinstance(at, tuple):   # a row there, as a tall list would put it
            s = next(x for x in app.sessions if x["id"] == sid)
            app.push_screen(tui.SessionMenu(sid, "name", app.session_menu_buttons(s), tui.Offset(*at)))
            x, line = at
        else:
            row = 0 if at == "row" else table.content_region.height - table.header_height - 1
            x, line = table.region.x + 3, table.region.y + table.header_height + row
            await session_menu(app, pilot, table.keys()[row], (3, table.header_height + row))
        await pilot.pause()
        assert isinstance(app.screen, tui.SessionMenu), desc
        box = app.screen.query_one("#dialog").region
        width, height = size
        assert (box.y == line + 1) if side == "below" else (box.bottom == line), f"{desc}: {box} for line {line}"
        assert box.bottom <= height and box.y >= 0, f"{desc}: on the screen down"
        assert box.x == min(x, width - box.width) and box.width < width // 2, f"{desc}: {box} at the pointer, on the screen"


@pytest.mark.anyio
@pytest.mark.parametrize("focus, change, expect, desc", [
    (1, "disabled", 2, "the one focused greys: the next one below"),
    (1, "hidden", 2, "the one focused hides: the next one below"),
    (-1, "disabled", -2, "the last one greys: the one above"),
])
async def test_a_menu_button_going_off_moves_focus_to_a_neighbour(store, sid, clock, focus, change, expect, desc):
    """#128: focus goes to a neighbour of the menu button that greys or hides, not the top."""
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        await session_menu(app, pilot, sid)
        menu = app.screen
        usable = [b for b in menu.query(Button) if b.display and not b.disabled]
        menu.set_focus(usable[focus])
        await pilot.pause()
        off = usable[focus].id.removeprefix("menu-")
        menu.paint([(id_, caption, disabled or (id_ == off and change == "disabled"),
                     shown and not (id_ == off and change == "hidden")) for id_, caption, disabled, shown in menu.buttons])
        await pilot.pause()
        assert menu.focused is usable[expect], f"{desc}: {menu.focused.id}, not {usable[expect].id}"


@pytest.mark.anyio
async def test_a_stale_menu_choice_is_named_by_its_own_caption(store, sid, clock):
    """#128: a choice no longer applying, the selection moved to another session, says the
    menu's caption, not the one under the list for the session selected now."""
    other = store.create_session("/tmp", name="other")
    store.set_parked(other, True)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        notes = []
        app.notify = lambda message, **kw: notes.append(message)
        await session_menu(app, pilot, sid)
        menu = app.screen
        app.follow(other)
        app.paint_session_info()
        await pilot.pause()
        assert str(app.screen_stack[0].query_one("#relaunch", Button).label) == "Unpark", "the other's caption under the list"
        menu.action_answer("relaunch")
        await pilot.pause()
        assert notes[-1] == "Relaunch no longer applies to this session"


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
            await pilot.press("alt+v")
            await pilot.pause()
        labels = footer_labels(app)
        assert app.show_finished == shown, desc
        assert label in labels and labels.count(label) == 1, f"{desc}: {labels}"
        assert {"Show finished", "Hide finished"} - {label} - set(labels), f"{desc}: only the one that applies"
        assert not {"Inbox", "Sessions"} & set(labels), "no tab keys: the tabs are gone (#58)"


@pytest.mark.usefixtures("sdk_on")
@pytest.mark.anyio
@pytest.mark.parametrize("size", [(80, 24), (100, 30), (160, 40), (160, 60)])
@pytest.mark.parametrize("status, parked, shown, desc", [
    ("live", True, ["rename", "relaunch", "park", "end", "model", "fork", "mode", "send", "interrupt", "compact", "shell"],
     "a running hosted session too old to queue, parked under the old rules (Active once the hub starts): the "
     "longest captions"),
    ("live", False, ["rename", "relaunch", "park", "end", "model", "fork", "mode", "send", "interrupt", "compact", "shell"],
     "an Active one: Stop & Forget"),
    ("dead", False, ["relaunch", "park", "end", "fork", "mode", "shell"],
     "an Available one: Relaunch, Park and Forget, nothing to interrupt, and no Send: it can't receive (D30)"),
    ("dead", True, ["relaunch", "end", "fork", "mode", "shell"], "a Parked one: Unpark in Relaunch's place, no Park, and Forget"),
    ("stalled", False, ["rename", "relaunch", "park", "end", "model", "fork", "mode", "send", "interrupt", "compact", "shell"],
     "a stalled one still takes what's sent"),
])
async def test_the_session_buttons_fit(store, tmp_path, monkeypatch, size, status, parked, shown, desc):
    """#58, #62: only the buttons that apply show, each its whole caption, in grey with a
    tooltip, filling the grid from the left with no hole where one is hidden and a blank
    row between rows; and the list and description keep their room, the stats giving way."""
    from claude_wheelhouse import liveness
    from claude_wheelhouse.tui import BUTTONS
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: status)
    sid = store.create_session(str(tmp_path), name="hosted", runner="sdk")
    store.db.execute("UPDATE sessions SET code_version = 1")
    store.set_parked(sid, parked)
    app = WheelhouseApp(store)
    desc = f"{desc}, at {size[0]} columns"
    async with app.run_test(size=size) as pilot:
        await pick(app, pilot, sid)
        buttons = [b for b in app.query("#session-info-pane Grid Button") if b.display]
        assert [b.id for b in buttons] == shown, desc
        strips = app.screen._compositor.render_strips()
        for b in buttons:
            drawn = "".join(seg.text for seg in strips[b.region.y])[b.region.x:b.region.right]
            assert str(b.label) in drawn and b.region.height == 1, f"{desc}: {b.label!s} drawn whole, got {drawn!r}"
            assert app.screen.get_widget_at(*b.region.offset)[0] is b, f"{desc}: {b.label} is on screen, not covered"
            assert b.tooltip == BUTTONS[b.id], f"{desc}: {b.id}'s tooltip"
            grey = (tui.OFF.upper(), "#000000") if b.disabled else ("#4D4D4D", "#FFFFFF")   # OFF when disabled (#107)
            assert (b.styles.background.hex, b.styles.color.hex) == grey, f"{desc}: {b.id} grey"
        for grid in app.query("#session-info-pane Grid"):
            mine = [b for b in buttons if b.parent is grid]
            rows = sorted({b.region.y for b in mine})
            assert all(y2 - y1 == 2 for y1, y2 in zip(rows, rows[1:])), f"{desc}: a blank row between rows"
            cells = [(rows.index(b.region.y), b.region.x) for b in mine]
            assert cells == sorted(cells), f"{desc}: in order, row by row"
            lefts = sorted({b.region.x for b in mine if b.id != "send"})   # Send follows Mode's two columns
            assert lefts == sorted({b.region.x for b in grid.query(Button) if b.display and b.id != "send"}) \
                and lefts[0] == grid.content_region.x, f"{desc}: no hole at the left of a row"
        assert app.query_one("#session-list").region.height >= 4, f"{desc}: the header and three sessions"
        # the description gives way to the buttons, after the stats; at 40 rows the session list's share takes the rest
        rows = 1 if size[1] <= 40 else 3
        assert app.query_one("#session-info").region.height >= rows, f"{desc}: {rows} rows of description"
        captions = {"relaunch": "Unpark" if parked and status == "dead" else "Relaunch", "park": "Park",
                    "end": "Forget" if status == "dead" else "Stop & Forget"}
        assert {id_: str(app.query_one(f"#{id_}", Button).label) for id_ in captions if id_ in shown} == \
            {id_: caption for id_, caption in captions.items() if id_ in shown}, f"{desc}: captioned for its state"
        if status == "live":
            assert str(app.query_one("#mode", Button).label) == "Can't queue: relaunch", desc
        drawn = "".join(seg.text for seg in strips[app.screen.query_one(Footer).region.y])
        assert size[0] < 100 or all(f"{key} {label}" in drawn for key, label in (("N", "New"), ("A", "Adopt"), ("Shift+L", "Relaunch all"))), \
            f"{desc}: New, Adopt and Relaunch all in the footer (at 80 it scrolls), got {drawn!r}"


@pytest.mark.anyio
async def test_the_footer_fits_at_100_columns_whatever_has_focus(store, sid, tmp_path, monkeypatch, every_module):
    """#135: at 100 columns New, Adopt and Relaunch all are drawn whole, never under the
    palette's key, at every Tab stop: each pane's list, the answer box, every button, and
    the inbox with a row selected, whose Del Close stays off the footer for it."""
    from textual.widgets._footer import FooterKey
    from claude_wheelhouse import liveness
    prs = pytest.importorskip("claude_wheelhouse_prs.pane", reason="installed without the prs module")
    pytest.importorskip("claude_wheelhouse_design", reason="installed without the design module")
    pr = {"number": 7, "title": "Change 7", "url": "https://github.com/two-inc/wheelhouse/pull/7", "isDraft": False,
          "state": "OPEN", "updatedAt": "2026-10-20T10:00:00Z", "headRefName": "doug/7", "reviewDecision": None,
          "mergeable": "MERGEABLE", "repository": {"nameWithOwner": "two-inc/wheelhouse"}, "commits": {"nodes": []}}
    monkeypatch.setattr(prs, "RUNNER", lambda argv: (0, json.dumps({"data": {"search": {"nodes": [pr]}}}), ""))
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "adr").mkdir(parents=True)
    (tmp_path / "docs" / "adr" / "0001-a.md").write_text("# 1. A\n\nStatus: accepted\n")
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "live")
    store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(100, 30)) as pilot:
        for _ in range(30):
            await app.workers.wait_for_complete()
            await pilot.pause()
        await pick(app, pilot, sid)
        stops = [w for w, _ in tui.tab_stops(app.screen)]
        assert {"session-list", "items", "pr-list", "decision-list", "answer"} <= {w.id for w in stops}, \
            f"every pane's stop: {[w.id for w in stops]}"
        for widget in stops:
            widget.focus()
            if widget.id == "items":
                widget.move_cursor(row=0)
            await pilot.pause()
            await pilot.pause()
            name = widget.id or type(widget).__name__
            shown = {(k.key_display, k.description) for k in app.screen.query(FooterKey)}
            drawn = "".join(seg.text for seg in app.screen._compositor.render_strips()[app.screen.query_one(Footer).region.y])
            for key in (("N", "New"), ("A", "Adopt"), ("Shift+L", "Relaunch all")):
                assert key not in shown or " ".join(key) in drawn, f"{name}: {' '.join(key)} drawn whole, got {drawn!r}"
            assert widget.id not in ("session-list", "items") or ("Alt+Shift+L", "Relaunch all") in shown, \
                f"{name}: Relaunch all in the footer"


def splitter(app, key: str) -> Splitter:
    return next(s for s in app.query(Splitter) if s.key == key)


def sized(app, key: str) -> int:
    sp = splitter(app, key)
    return sp.extent(sp.panes()[0])


SLOT_SPLITTERS = {"left-middle", "left-bottom", "middle-middle", "middle-bottom"}   # columns.py's


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
@pytest.mark.parametrize("key, dx, dy, change, rows, desc", [
    ("sessions-pane", 10, 0, 10, 40, "the sessions column, wider"),
    ("sessions-pane", -20, 0, -6, 40, "never narrower than its buttons' captions need: 39 of 45"),
    ("items-pane", 10, 0, 10, 40, "the inbox column, wider"),
    ("left-bottom", 0, 1, -1, 52, "the session info, a row shorter as its top edge moves down a row"),
    ("left-bottom", 0, -1, 1, 52, "a row taller as it moves up a row"),
    ("left-bottom", 0, 1, 0, 40, "never shorter than its buttons and a row of description: 12 at 40 rows"),
    ("left-middle", 0, 1, -1, 40, "the stats, a row shorter"),
    ("left-middle", 0, 3, -3, 40, "the stats, shorter as its top edge moves down"),
    ("answer", 0, -3, 3, 40, "the answer box, taller"),
])
async def test_dragging_a_splitter_resizes_and_is_kept(store, sid, key, dx, dy, change, rows, desc):
    """Doug: the boundaries between all areas draggable. A drag is kept across runs, and a
    double-click puts the default back."""
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, rows)) as pilot:
        await pilot.pause()
        before = sized(app, key)
        await drag(pilot, key, dx, dy)
        after = sized(app, key)
        assert after - before == change, desc
        assert not splitter(app, key).has_class("-dragging"), desc
    kept = "layout." + ("weights" if key in SLOT_SPLITTERS else key)   # a column's slots: their weights (#111)
    assert (store.setting(kept) is not None) is (bool(change) or key not in SLOT_SPLITTERS), \
        f"{desc}: kept, unless a column's line didn't move (columns.dragged)"
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, rows)) as pilot:
        await pilot.pause()
        assert sized(app, key) == after, f"{desc}: as it was left, next run"
        await pilot.double_click(splitter(app, key))
        await pilot.pause()
        assert sized(app, key) == before, f"{desc}: a double-click puts the default back"
    assert store.setting(kept) is None, desc


@pytest.mark.anyio
@pytest.mark.parametrize("width, desc", [(200, "a wider terminal"), (120, "a narrower one")])
async def test_dragged_sizes_follow_a_resized_terminal(store, sid, width, desc):
    """Sizes are kept as shares, so a resized terminal keeps the proportions."""
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        await drag(pilot, "sessions-pane", 15, 0)   # 60 of the 158 cells inside the two splitters
        share = sized(app, "sessions-pane") / app.query_one("#main").size.width
        await pilot.resize_terminal(width, 40)
        await pilot.pause()
        assert abs(sized(app, "sessions-pane") - share * app.query_one("#main").size.width) <= 1, desc
        assert app.query_one("#items-pane").region.width >= 20, f"{desc}: the inbox keeps its room"


def on_screen(app, *selectors) -> bool:
    """Every pane at least partly on screen, and none past its right edge."""
    width = app.screen.size.width
    return all(0 < (r := app.query_one(sel).region).width and r.right <= width for sel in selectors)


COLUMNS = ("#col-left", "#col-middle", "#col-right")


@pytest.mark.anyio
@pytest.mark.parametrize("width, stored, desc", [
    (80, None, "80 columns: the three columns, as before #60 (review 8)"),
    (91, None, "91: under the old minimums' 92"),
    (120, {"items-pane": "0.69"}, "a share dragged on a wide terminal, shrunk on a narrower one"),
    (120, {"sessions-pane": "0.6", "items-pane": "0.6"}, "two of them"),
    (160, {"sessions-pane": "abc"}, "an unreadable size is ignored"),
    (160, {"sessions-pane": "nan"}, "so is one not finite"),
    (160, {"items-pane": "inf"}, "nor infinite"),
    (160, {"sessions-pane": "5.0"}, "one over the whole is clamped"),
    (160, {"sessions-pane": "-1"}, "as is a negative one"),
    # review 9: a share under its pane's minimum, held up by CSS, counted at the minimum
    (90, {"sessions-pane": "0.6025", "items-pane": "0.1225"}, "120 and 24 of 200, on 90"),
    (80, {"sessions-pane": "0.6025", "items-pane": "0.1225"}, "120 and 24 of 200, on 80"),
    (113, {"sessions-pane": "0.218016", "items-pane": "0.933439"}, "the sweep's 127 on 113"),
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
            shares = {key: rng.uniform(0.02, 0.98) for key in ("sessions-pane", "items-pane")}
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
    store.set_setting("layout.items-pane", "0.6")
    app = WheelhouseApp(store)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        assert on_screen(app, *COLUMNS)
        await pilot.resize_terminal(200, 30)
        await pilot.pause()
        await pilot.pause()
        assert abs(sized(app, "items-pane") - 0.6 * app.query_one("#main").size.width) <= 1


@pytest.mark.anyio
@pytest.mark.parametrize("width, stored, widths, desc", [
    (200, None, (45, 45, 108), "room: 45 and 45 (Q113), Detail the rest"),
    (120, None, (39, 39, 40), "narrow: the first two give way in proportion, Detail keeps 40"),
    (100, None, (39, 19, 40), "narrower: the session column at its minimum, the Inbox gives the rest"),
    (120, {"sessions-pane": "0.5"}, (60, 18, 40), "a dragged width wins; the Inbox at its default gives way"),
])
async def test_detail_keeps_its_room_on_a_narrow_terminal(store, sid, width, stored, widths, desc):
    """Review of #86: at 120 columns 45 + 45 left Detail 28 wide."""
    for key, value in (stored or {}).items():
        store.set_setting("layout." + key, value)
    app = WheelhouseApp(store)
    async with app.run_test(size=(200 if width < 200 else 160, 40)) as pilot:   # from another width, as a resize
        await pilot.pause()
        await pilot.resize_terminal(width, 40)
        await pilot.pause()
        assert tuple(app.query_one(sel).region.width for sel in COLUMNS) == widths, desc
        assert columns_fill(app), desc


@pytest.mark.anyio
@pytest.mark.parametrize("size, rows, desc", [
    ((160, 40), (13, 10, 12), "40 rows: the session list keeps a share, the stats too small for a chart"),
    ((200, 60), (22, 17, 16), "60 rows: the session list grows most (columns.DEFAULT's shares)"),
])
async def test_the_left_column_rows(store, sid, size, rows, desc):
    """Review of #86: Sessions, Stats and Session info, top to bottom; the session list
    took 4 rows at 160x40 with the stats and description at 40% each."""
    app = WheelhouseApp(store)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        got = tuple(app.query_one(f"#slot-left-{name}").region.height for name in ("top", "middle", "bottom"))
        assert got == rows, desc


@pytest.mark.anyio
@pytest.mark.parametrize("stored, kept, desc", [
    ({"items": "0.5"}, {"items": None}, "the old items-over-stats share: deleted, and sizes nothing"),
    ({"detail-pane": "0.3"}, {"detail-pane": None}, "the old Detail column share: deleted"),
    ({"session-info": "0.7"}, {"session-info": None}, "the old description-in-sessions share: deleted"),
    ({"middle-top": "0.3"}, {"middle-top": None, "weights": None},
     "the old Inbox-over-Decisions share (#111): deleted, the defaults"),
    ({"weights": '{"middle.bottom": 0.9}'}, {"weights": '{"middle.bottom": 0.9}'}, "a weight for an empty slot: kept, not applied"),
])
async def test_the_inbox_fills_its_column_whatever_was_stored(store, sid, stored, kept, desc):
    """Review of #86: a share kept for a splitter beside a collapsed slot left half the Inbox
    column blank."""
    for key, value in stored.items():
        store.set_setting("layout." + key, value)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert app.query_one("#slot-middle-top").region.height == app.query_one("#col-middle").size.height, desc
    assert {key: store.setting("layout." + key) for key in kept} == kept, desc


@pytest.mark.anyio
@pytest.mark.parametrize("missing, desc", [
    (False, "a module pane swapped for a card: the card keeps its slot, under the splitter"),
    (True, "a neighbour gone altogether: the splitter does nothing"),
])
async def test_the_stats_splitter_survives_a_pane_that_raised(store, sid, monkeypatch, missing, desc):
    """Review 8: a pane that raised was swapped for a card the splitter above it couldn't
    find, and a press on the splitter then raised NoMatches and closed the app."""
    from claude_wheelhouse_stats import pane as stats_pane

    def boom(self):
        raise RuntimeError("pane bug")
    monkeypatch.setattr(stats_pane.SessionStats, "tick", boom)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        card = app.query_one(".module-error")
        assert card.parent.name == "left.middle", desc
        if missing:
            await card.parent.remove()
        before = sized(app, "left-middle") if not missing else None
        if missing:
            await pilot.mouse_down(splitter(app, "left-middle"))
            await pilot.pause()
        else:
            await drag(pilot, "left-middle", 0, 3)
            assert sized(app, "left-middle") == before - 3, f"{desc}: and still drags"
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
@pytest.mark.parametrize("on", ["hover", "cursor"])
@pytest.mark.parametrize("status,desc", [
    ("live", "Live: green"),
    ("dead", "Available: red, not the table's green"),
    ("stalled", "Stalled: yellow"),
    ("starting", "Starting: cyan"),
    ("relaunching", "Relaunching: cyan"),
    ("forgetting", "Forgetting: magenta"),
    ("parking", "Parking: magenta"),
])
async def test_a_sessions_dot_keeps_its_status_colour_under_the_mouse_or_cursor(store, sid, tmp_path, monkeypatch,
                                                                                status, desc, on):
    """#16: the mouse over a session's row shades it, and its dot keeps its status's colour: the
    hover style's foreground, the table's green, was laid over it. T113: the cursor's row the
    same, on the cursor's background: Doug, "keep it red or green regardless of whether
    selected or not"; the cursor's white was laid over it."""
    other = store.create_session(str(tmp_path), name="other")
    monkeypatch.setattr(WheelhouseApp, "shown_status", lambda self, s: status if s == other else "live")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        t = app.query_one("#session-list", DataTable)
        row = t.get_row_index(other)
        y = t.header_height + row
        if on == "cursor":
            app.follow(other)   # selected: the cursor shows (D20)
        else:
            t.move_cursor(row=t.get_row_index(sid))
            await pilot.hover(t, offset=(20, y))
        await pilot.pause()
        assert (t.hover_coordinate.row if on == "hover" else t.cursor_row) == row, f"{desc}: the {on} is on the row"
        dot = next(s.style for s in t._render_line(y, 0, t.size.width, t.rich_style) if "●" in s.text)
        shade = t.get_component_rich_style(f"datatable--{on}")
        assert (dot.color.name, dot.bgcolor) == (tui.STATUS_STYLE[status].split()[-1], shade.bgcolor), \
            f"{desc} ({on}): its own colour, on the {on}'s shade"


@pytest.mark.anyio
@pytest.mark.parametrize("status, colour, desc", [
    ("open", "#ffd300", "an open question stays yellow"),
    ("blocked", "#ff2a6d", "a blocked task stays red"),
    ("done", None, "a finished one, dim, keeps no colour of its own: the cursor's white"),
])
async def test_the_selected_items_status_keeps_its_colour(store, sid, status, colour, desc):
    """T113: the cursor's row in the item list keeps its status's colour, on the cursor's
    background, where the cursor's white was laid over it; a cell with no colour takes the
    cursor's."""
    kind = "question" if status == "open" else "task"
    store.post_item(sid, kind, "the item", status=status)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.action_show_finished(status == "done")
        await pilot.pause()
        t = app.query_one("#items", DataTable)
        row = next(i for i, k in enumerate(t.keys()) if not k.startswith(BLANK) and not k.endswith("|"))
        t.move_cursor(row=row)
        await pilot.pause()
        y = t.header_height + row
        cell = next(s.style for s in t._render_line(y, 0, t.size.width, t.rich_style) if status in s.text)
        cursor = t.get_component_rich_style("datatable--cursor")
        assert (cell.color.name, cell.bgcolor) == (colour or cursor.color.name, cursor.bgcolor), desc


@pytest.mark.anyio
async def test_parked_sessions_stay_in_the_list_after_the_rest(store, sid, tmp_path):
    """#58: with the Sessions tab gone, the list is the only way to a parked session (Unpark,
    Restore, End): it stays, dimmed, at the foot."""
    first = store.create_session(str(tmp_path), name="first")
    other = store.create_session(str(tmp_path), name="other")
    store.set_parked(sid, True)
    for s in (sid, first):
        store.post_item(s, "question", "which db?")
    same_second(store)   # in creation order under * (T114), however slowly they were made
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        t = app.query_one("#session-list", DataTable)
        assert [r.key.value for r in t.ordered_rows if not r.key.value.startswith(BLANK)] == [first, other, sid]
        assert "dim" in str(t.get_row(sid)[1].style)
        # #210: a parked session's question count is dimmed and still; an active one's flashes
        parked_q, active_q = str(t.get_row(sid)[3].style), str(t.get_row(first)[3].style)
        assert "dim" in parked_q and "blink" not in parked_q, parked_q
        assert "blink" in active_q and "dim" not in active_q, active_q


def dimmed_cell(cell) -> bool:
    """Whether a table cell is drawn dim, by its own style or a span over it."""
    return isinstance(cell, Text) and ("dim" in str(cell.style) or any("dim" in str(sp.style) for sp in cell.spans))


@pytest.mark.anyio
@pytest.mark.both
@pytest.mark.parametrize("row, life, dim, desc", [
    ("session", "dead", True, "a dead session's row dims, its dot kept red"),
    ("session", "live", False, "a live session's row stays bright"),
    ("session", "parked", True, "a parked session's name dims, as it did before"),
    ("session", "revived", False, "relaunched: its row brightens at the next refresh"),
    ("item", "dead", True, "a dead session's inbox item dims"),
    ("item", "live", False, "a live session's inbox item stays bright"),
    ("item", "parked", None, "a parked session's items are off the inbox"),
    ("item", "revived", False, "relaunched: its items brighten at the next refresh"),
    ("conversation", "dead", True, "a dead session's conversation row dims"),
    ("conversation", "live", False, "a live session's conversation row stays bright"),
    ("conversation", "parked", None, "a parked session's conversation is off the inbox"),
    ("conversation", "revived", False, "relaunched: its conversation row brightens"),
])
async def test_a_dead_sessions_rows_are_dimmed(store, sid, monkeypatch, row, life, dim, desc):
    """#252: a session dead and not parked keeps its red dot, and its rows dim in the session
    list and the inbox; they stay selectable, and brighten once it runs again. A parked one
    is dimmed as it was (#58, #210), its items off the inbox."""
    from claude_wheelhouse import liveness
    from claude_wheelhouse.tui import STATUS_STYLE
    status = {"now": "live" if life == "live" else "dead"}
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: status["now"])
    store.post_item(sid, "question", "which db?")
    if life == "parked":
        store.set_parked(sid, True)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        if life == "revived":
            if row == "session":
                assert dimmed_cell(app.query_one("#session-list", DataTable).get_row(sid)[1]), f"{desc}: dim first"
            else:
                t = app.query_one("#items", DataTable)
                key = f"{sid}|" if row == "conversation" else next(k for k in t.keys() if k and k.endswith("Q1"))
                assert dimmed_cell(t.get_row(key)[-1]), f"{desc}: dim first"
            status["now"] = "live"
            app.refresh_data()
            await pilot.pause()
        if row == "session":
            t = app.query_one("#session-list", DataTable)
            cells = t.get_row(sid)
            assert not dimmed_cell(cells[0]), f"{desc}: the dot keeps its colour"
            if life == "dead":
                assert str(cells[0].style) == STATUS_STYLE["dead"], f"{desc}: the dot stays red"
            assert dimmed_cell(cells[1]) == dim, desc
            return
        t = app.query_one("#items", DataTable)
        key = f"{sid}|" if row == "conversation" else next((k for k in t.keys() if k and k.endswith("Q1")), None)
        if dim is None:
            assert key not in t.rows, desc
            return
        assert dimmed_cell(t.get_row(key)[-1]) == dim, desc
        if row == "item":   # visual only: still picked by a click
            await pilot.click(t, offset=(t.region.width // 2, t.get_row_index(key) + 1))
            await pilot.pause()
            assert app.selected == (sid, "Q1"), f"{desc}: selectable"


def test_the_keys_overlay_has_relaunch_and_no_tabs():
    from claude_wheelhouse.tui import keys_help
    text = keys_help(sdk=True)
    assert "Relaunch / Unpark (Alt+L in the session list):" in text and "Sessions tab" not in text and "Inbox tab" not in text
    for key in ("**Alt+I**", "**Alt+C**", "**Alt+H**", "**Alt+Y**", "**Alt+W**", "**Alt+D**", "**Alt+Shift+A**", "**Tab**", "**Shift+Tab**",
                "## Tab order"):
        assert text.count(key) == 1, f"{key} listed once (#73)"
    assert text.count("Show or hide finished items") == 1, "F once, though it has two bindings"
    assert text.count("Shift+L") == 1 and "Shift+S" not in text, "Shift+L once, though it has two bindings; Shift+S unbound"


def session_names(app) -> list[str]:
    return [app.store.session(r.key.value)["name"] for r in app.session_list.ordered_rows
            if not r.key.value.startswith(BLANK)]


@pytest.mark.anyio
@pytest.mark.parametrize("parked, start, order, desc", [
    ([], "demo", ["bee", "sea", "demo"], "Park moves the row to the foot, and the cursor with it (review 7)"),
    (["bee", "sea"], "sea", ["demo", "sea", "bee"], "Unpark moves it up, and the cursor with it"),
])
async def test_the_cursor_stays_on_a_session_that_moves(store, sid, tmp_path, monkeypatch, clock, parked, start, order,
                                                         desc):
    some_live(monkeypatch)
    ids = {"demo": sid, **{n: store.create_session(str(tmp_path), name=n) for n in ("bee", "sea")}}
    for name in parked:
        store.set_parked(ids[name], True)
    same_second(store)   # in creation order under * (T114), however slowly they were made
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pick(app, pilot, ids[start])
        if parked:
            monkeypatch.setattr(launch, "restore_session", lambda *a, **kw: None)
            await pilot.click("#relaunch")   # Unpark
        else:
            await park_live(app, pilot, ids[start])
        await pilot.pause()
        frames = 0
        while app.session_sink.move:   # it falls or rises there (T66), the cursor on it throughout
            assert app.current_session() == ids[start], f"{desc}: mid-move"
            frames += 1
            clock.advance(tui.Sink.SECONDS / 8)
            await pilot.pause()
        assert frames > 3, f"{desc}: it moved a frame at a time ({frames})"
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
@pytest.mark.parametrize("key, button, live, desc", [
    ("alt+r", "rename", True, "Alt+R renames"),
    ("alt+l", "relaunch", True, "Alt+L relaunches"),
    ("alt+l", "relaunch", False, "Alt+L relaunches one that isn't running"),
    ("alt+p", "park", True, "Alt+P parks"),
    ("alt+p", "park", False, "Alt+P parks a dead one too"),
    ("delete", "end", True, "Delete forgets (Stop & Forget)"),
    ("backspace", "end", False, "so does Backspace, on one that isn't running"),
    ("alt+k", "fork", True, "Alt+K forks"),
    ("alt+k", "fork", False, "one that isn't running too"),
])
async def test_the_session_lists_keys_press_its_buttons(store, sid, monkeypatch, key, button, live, desc):
    """The buttons are out of the Tab order, so the list's keys keep them on the keyboard.
    Each on a session it applies to: on another it says so (#73)."""
    from claude_wheelhouse import liveness
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "live" if live else "dead")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pick(app, pilot, sid)
        pressed = presses(app)
        app.session_list.focus()
        await pilot.press(key)
        await pilot.pause()
        assert pressed == [button], desc
        assert app.focused is app.session_list, f"{desc}: focus stays on the list"


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
        app.session_list.focus()
        await pilot.press("enter")   # none selected: Enter picks the first, its only session (#16)
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


@pytest.mark.anyio
@pytest.mark.parametrize("make, desc", [
    (lambda app, sid, cwd: tui.NewSession(), "New session"),
    (lambda app, sid, cwd: tui.PickDirectory(cwd), "the folder picker"),
    (lambda app, sid, cwd: RenameSession("demo"), "Rename"),
    (lambda app, sid, cwd: tui.ChooseModel("demo", None), "the model"),
    (lambda app, sid, cwd: tui.McpServers(sid), "MCP servers"),
    (lambda app, sid, cwd: tui.AdoptSession([]), "Adopt"),
    (lambda app, sid, cwd: tui.ForkSession(app.client.fork_plan(sid)), "Fork"),
    (lambda app, sid, cwd: CompactSession("demo"), "Compact: was top-left"),
    (lambda app, sid, cwd: ParkSession("demo", True), "Park: was top-left"),
    (lambda app, sid, cwd: ForgetChoice("Forget demo?", True), "Forget: was top-left"),
    (lambda app, sid, cwd: Confirm("Relaunch demo?"), "a confirmation"),
    (lambda app, sid, cwd: Choice("Force it?"), "a choice"),
    (lambda app, sid, cwd: RelaunchChoice("Relaunch demo?"), "Relaunch's choice: was top-left"),
    (lambda app, sid, cwd: tui.TourInvite(), "the tour's invite: was a line across the top"),
    (lambda app, sid, cwd: tui.KeysHelp(), "the keys"),
])
async def test_a_dialog_is_centred_over_the_screen(store, sid, tmp_path, make, desc):
    """T115, Doug: "I'd prefer them rendered on top of the screen ... always center them".
    The app's Screen rule matched every dialog, its opaque background blanking the screen
    behind; the dialogs missing from the lists that centred them sat in the top left."""
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.push_screen(make(app, sid, str(tmp_path)))
        await pilot.pause()
        dialog = app.screen
        assert isinstance(dialog, tui.ModalScreen), f"{desc}: a modal screen"
        assert dialog.styles.background.a < 1, f"{desc}: the screen behind shows through"
        box = dialog.children[0].region
        assert (abs(box.x * 2 + box.width - 160) <= 1, abs(box.y * 2 + box.height - 40) <= 1) == (True, True), \
            f"{desc}: centred, at {box}"
        assert "demo" in app.export_screenshot(), f"{desc}: the session list is still in sight"


def focused_id(app) -> str | None:
    return app.focused.id if app.focused is not None else None


OPEN = object()   # the dialog still open: Enter was its focus's own (a list's, a multi-line box's)


@pytest.mark.anyio
@pytest.mark.parametrize("make, outside, esc, enter, tabs, desc", [
    (lambda app, sid, cwd: tui.NewSession(), None, None, {"name": "", "ticket": "", "brief": ""},
     ["cwd", "browse", "name", "ticket", "brief", "model", "effort", "launch", "cancel"], "New session: Enter launches"),
    (lambda app, sid, cwd: tui.PickDirectory(cwd), None, None, OPEN, ["folders", "choose", "up", "cancel"],
     "the folder picker: Enter opens the folder"),
    (lambda app, sid, cwd: RenameSession("demo"), None, None, "demo", ["new-name", "ok", "cancel"], "Rename"),
    (lambda app, sid, cwd: tui.ChooseModel("demo", None), None, None, models.MODELS[0].id, ["models", "ok", "cancel"],
     "the model: Enter takes the highlighted one"),
    (lambda app, sid, cwd: tui.McpServers(sid), None, None, None, ["cancel"], "MCP servers: nothing asked"),
    (lambda app, sid, cwd: tui.AdoptSession([]), None, None, OPEN, ["adopt-list", "adopt-name", "adopt", "cancel"],
     "Adopt, with nothing to adopt"),
    (lambda app, sid, cwd: tui.ForkSession(app.client.fork_plan(sid)), None, None, {"refs": [], "branch": None},
     ["fork-go", "cancel"], "Fork"),
    (lambda app, sid, cwd: CompactSession("demo"), None, None, OPEN,
     ["compact-notes", "compact-dialog-then", "compact-go", "compact-cancel"], "Compact: Enter is a new line"),
    (lambda app, sid, cwd: ParkSession("demo", True), None, None, {"compact": True},
     ["park-compact", "park-go", "park-cancel"], "Park: Enter on the checkbox parks"),
    (lambda app, sid, cwd: ForgetChoice("Forget demo?", True), None, None, "tell",
     ["forget-tell", "forget-stop", "forget-cancel"], "Forget: Enter the focused default"),
    (lambda app, sid, cwd: Confirm("Relaunch demo?"), False, False, False, ["no", "yes"], "a confirmation: No focused"),
    (lambda app, sid, cwd: Choice("Force it?"), "leave", "leave", "cancel", ["cancel", "force", "leave"], "a choice"),
    (lambda app, sid, cwd: RelaunchChoice("Relaunch demo?"), None, None, None,
     ["relaunch-cancel", "relaunch-compact", "relaunch-now"], "Relaunch's choice: Cancel focused"),
    (lambda app, sid, cwd: tui.TourInvite(), "later", "later", "start", ["invite-start", "invite-later", "invite-never"],
     "the tour's invite: a click outside is Later, never Never, so a misclick doesn't lose the tour"),
    (lambda app, sid, cwd: tui.NewSession(tutorial.form(cwd), "Press Launch."), None, None,
     {"name": "tutorial", "ticket": ""}, ["name", "ticket", "brief", "model", "effort", "launch", "cancel"],
     "New session filled in by the tour: its directory fixed, so no Tab stop"),
    (lambda app, sid, cwd: tui.TourStop("The wheelhouse", "Every session in one place."), OPEN, "pause", "next",
     ["tour-stop-next"], "the tour's principles: Esc pauses the tour, a click outside does nothing"),
    (lambda app, sid, cwd: tui.TourNotes("N starts a session.", "## What's new"), "close", "close", "close", None,
     "the tour's release notes: no answer asked"),
    (lambda app, sid, cwd: tui.KeysHelp(), None, None, None, None, "the keys: no answer asked"),
])
async def test_dialog_keys_and_click_outside(store, sid, tmp_path, make, outside, esc, enter, tabs, desc):
    """#81, Doug: clicking outside a dialog dismisses it; one that asks nothing closes on Esc or
    Enter; one that asks has Tab order on its buttons, Enter its single action and Esc its
    cancel."""
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()

        async def answer(gesture):
            answers = []
            app.push_screen(make(app, sid, str(tmp_path)), answers.append)
            await pilot.pause()
            await gesture()
            await pilot.pause()
            if isinstance(app.screen, tui.Dialog):
                app.screen.dismiss(None)
                await pilot.pause()
                return OPEN
            return answers[0]

        async def inside():
            await pilot.click(app.screen.children[0], offset=(0, 0))   # its border: not outside
        async def outside_later():   # past the grace a double-click's second click gets
            app.screen.opened -= tui.Dialog.GRACE
            await pilot.click(offset=(0, 39))
        assert await answer(inside) is OPEN, f"{desc}: a click on the box leaves it open"
        assert await answer(outside_later) == outside, f"{desc}: a click outside"
        assert await answer(lambda: pilot.press("escape")) == esc, f"{desc}: Esc"
        got = await answer(lambda: pilot.press("enter"))
        if isinstance(enter, dict):
            got = {k: got.get(k) for k in enter} if isinstance(got, dict) else got
        assert got == enter, f"{desc}: Enter"
        if tabs is None:
            return
        app.push_screen(make(app, sid, str(tmp_path)))
        await pilot.pause()
        seen = []
        for _ in tabs:
            seen.append(focused_id(app))
            await pilot.press("tab")
        assert seen == tabs and focused_id(app) == (tabs[0] if tabs else None), f"{desc}: Tab order"
        for want in reversed(tabs):
            await pilot.press("shift+tab")
            assert focused_id(app) == want, f"{desc}: Shift+Tab back to {want}"


@pytest.mark.anyio
@pytest.mark.parametrize("after, want, desc", [
    (0.0, OPEN, "at once: a double-click's second click on the button that opened it"),
    (0.39, OPEN, "just inside the grace"),
    (0.4, None, "at the grace: an outside click cancels"),
    (2.0, None, "later: an outside click cancels"),
])
async def test_dialog_ignores_a_click_outside_as_it_opens(store, tmp_path, monkeypatch, after, want, desc):
    clock = [100.0]
    monkeypatch.setattr(tui.Dialog, "now", staticmethod(lambda: clock[0]))
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        answers = []
        app.push_screen(RenameSession("demo"), answers.append)
        await pilot.pause()
        clock[0] += after
        await pilot.click(offset=(0, 39))
        await pilot.pause()
        got = OPEN if isinstance(app.screen, tui.Dialog) else answers[0]
        assert got is want, desc


@pytest.mark.anyio
@pytest.mark.parametrize("keys, desc", [
    ("pilot", "Ctrl+Enter, Shift+Tab, Down, Tab, type, Ctrl+Enter answers the next item (#73, #77)"),
    ("raw", "the same as a burst, no idle wait between keys (#73)"),
])
async def test_answer_one_then_the_next_from_the_keyboard(store, sid, keys, desc):
    for title in ("one?", "two?", "three?"):
        store.post_item(sid, "question", title)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        first, second = app.items_table.keys()[first_item(app):][:2]
        app.items_table.move_cursor(row=first_item(app))
        await pilot.pause()
        app.answer.focus()
        await pilot.pause()
        flow = [*"aa", "ctrl+enter", "shift+tab", "down", "tab", *"bb", "ctrl+enter"]
        if keys == "raw":
            raw_keys(app, *flow)
            for _ in range(6):
                await pilot.pause()
        else:
            await pilot.press(*flow[:3])
            assert focused_id(app) == "answer", f"{desc}: Ctrl+Enter leaves focus in the box (#77)"
            assert app.items_table.cursor_key() == first, f"{desc}: the cursor where it was"
            await pilot.press(*flow[3:])
        await pilot.pause()
        drafts = {(m["session_id"], m["item_ref"]): m["body"] for m in store.drafts(sid)}
        assert drafts == {tuple(first.split("|")): "aa", tuple(second.split("|")): "bb"}, f"{desc}: {drafts}"
        assert focused_id(app) == "answer" and app.items_table.cursor_key() == second, desc
        assert app.answer.text == "", desc
        assert_one_current(app, desc)


@pytest.mark.anyio
@pytest.mark.parametrize("text, focus, desc", [
    ("", "answer", "nothing typed: nothing submitted, the box keeps focus"),
    ("hi", "answer", "sent: the box keeps focus (#77)"),
    ("hi", "thread-answer", "a full-screen item's box keeps focus too"),
])
async def test_ctrl_enter_leaves_focus_in_the_box(store, sid, text, focus, desc):
    q = store.post_item(sid, "question", "one?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        if focus == "thread-answer":
            app.push_screen(ThreadView(sid, q))
        else:
            app.answer.focus()
        await pilot.pause()
        await pilot.press(*text, "ctrl+enter")
        await pilot.pause()
        assert focused_id(app) == focus, desc


# the inbox's Tab stops with a running tab session selected and an answer queued (#77): the
# lists, the box, then every button shown and enabled in reading order, the session pane's
# then the send bar's (Model, Mode, Interrupt, Compact and Shell disabled: a wheelhouse-run session's)
INBOX_STOPS = ["session-list", "items", "answer", "rename", "relaunch", "park", "end", "fork", "send", "shell", "send-all",
               "stats:zoom-out", "stats:zoom-in", "stats:info"]   # then the stats pane's zoom (no slider: a tab session), and its ⓘ (#242)


@pytest.mark.anyio
@pytest.mark.parametrize("screen, key, order, desc", [
    ("inbox", "tab", INBOX_STOPS + ["session-list"],
     "Tab: the session list, the items, the answer box, every button, round again (#77)"),
    ("inbox", "shift+tab", ["session-list", *INBOX_STOPS[:0:-1], "session-list"], "Shift+Tab: the reverse"),
    ("tour", "tab", INBOX_STOPS[:3] + ["tour-next"] + INBOX_STOPS[3:] + ["session-list"],
     "a tour box's Next after the answer box, before the buttons (review of #93)"),
    ("thread", "tab", ["thread-answer", "send", "send-all", "shell", "thread-answer"],
     "a full-screen item: its box, then its buttons, not its thread (#77)"),
    ("thread", "shift+tab", ["thread-answer", "shell", "send-all", "send", "thread-answer"], "and back"),
])
async def test_the_tab_order(store, sid, monkeypatch, screen, key, order, desc):
    by_name(monkeypatch, {"demo": "live"})
    q = store.post_item(sid, "question", "one?")
    store.queue(sid, "queued")   # Send and Send all enabled
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 60)) as pilot:   # the stats' charts, so their zoom: none at 40 rows
        await pilot.pause()
        await to_first_item(app, pilot)
        if screen == "thread":
            app.push_screen(ThreadView(sid, q))
        else:
            await pick(app, pilot, sid)
            if screen == "tour":   # the Stats dialog, a Next one
                from claude_wheelhouse.tour import Tour
                app.tour = Tour(False)
                app.tour.at = [d.id for d in app.tour.stops].index("stats")
                app.show_tour()
                await pilot.pause()
            app.session_list.focus()
        await pilot.pause()
        seen = [stop_id(app)]
        for _ in order[1:]:
            await pilot.press(key)
            seen.append(stop_id(app))
        assert seen == order, f"{desc}: {seen}"
        assert_one_current(app, desc)


@pytest.mark.anyio
@pytest.mark.parametrize("screen, focus, how, want, desc", [
    ("inbox", "send", "enter", "answer", "Tab to Send, Enter: Send disables itself, focus to the answer box (#77)"),
    ("inbox", "answer", "ctrl+s", "answer", "Send by its key from the box: focus stays in it"),
    ("inbox", "items", "ctrl+s", "items", "Send by its key from the items: focus stays there"),
    ("inbox", "session-list", "ctrl+s", "session-list", "and from the session list"),
    ("inbox", "items", "click", "items", "a click on Send from the items: focus stays there"),
    ("inbox", "fork", "disable", "answer", "a focused button disabled: focus to the answer box"),
    ("inbox", "fork", "hide", "answer", "a focused button hidden: the same"),
    ("inbox", "park", "no box", "end", "with no answer box: the next Tab stop"),
    ("thread", "send", "enter", "thread-answer", "a full-screen item: Send, then focus to its box"),
    ("thread", "send-all", "hide", "thread-answer", "a full-screen item: a focused button hidden, focus to its box"),
])
async def test_focus_lost_with_a_button_goes_to_the_answer_box(store, sid, monkeypatch, clock, screen, focus, how, want,
                                                               desc):
    """clock: no refresh tick puts back a button disabled or hidden here, or the answer box."""
    by_name(monkeypatch, {"demo": "live"})
    q = store.post_item(sid, "question", "one?")
    store.queue(sid, "queued")   # Send and Send all enabled
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        await pick(app, pilot, sid)
        if screen == "thread":
            app.push_screen(ThreadView(sid, q))
            await pilot.pause()
        app.screen.query_one(f"#{focus}").focus()
        await pilot.pause()
        assert focused_id(app) == focus, desc
        button = app.screen.query_one(f"#{focus}")
        if how == "click":
            await pilot.click("#send")
        elif how in ("enter", "ctrl+s"):
            await pilot.press(how)
        elif how == "hide":
            button.display = False
        else:
            if how == "no box":
                app.answer.display = False
            button.disabled = True
        for _ in range(20):   # the blur, a Hide after the next layout, then the bubbling: a few hops each
            await pilot.pause()
            if focused_id(app) == want:
                break
        assert focused_id(app) == want, f"{desc}: {focused_id(app)}"
        if how in ("enter", "ctrl+s", "click"):
            assert store.drafts(sid) == [], f"{desc}: sent"


@pytest.mark.anyio
@pytest.mark.parametrize("how, key, desc", [
    ("tab", "pagedown", "Tab from the items reaches the box under a doc, then the doc; PgDn scrolls it (#77)"),
    ("tab", "end", "and End"),
    ("tab", "down", "and Down"),
    ("escape", "pagedown", "a click on the doc opens its editor, and Esc closing it focuses the doc for PgDn"),
])
async def test_a_doc_in_the_inbox_scrolls_from_the_keyboard(store, sid, tmp_path, how, key, desc):
    doc = tmp_path / "plan.md"
    doc.write_text("\n\n".join(f"line {i}" for i in range(200)))
    m = store.post_doc(sid, "the plan", str(doc))
    q = store.post_item(sid, "question", "one?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.items_table.move_cursor(row=app.items_table.get_row_index(f"{sid}|{m}"))
        await pilot.pause()
        assert app.selected == (sid, m), desc
        app.items_table.focus()
        await pilot.pause()
        if how == "tab":
            await pilot.press("tab")
            await pilot.pause()
            assert focused_id(app) == "answer", f"{desc}: the box first, for a comment"
            await pilot.press("tab")
        else:
            await pilot.click(app.detail_scroll, offset=(2, 2))
            await pilot.pause()
            assert focused_id(app) == "detail-editor", desc
            await pilot.press("escape")
        await pilot.pause()
        assert focused_id(app) == "detail", desc
        await pilot.press(key)
        await pilot.wait_for_scheduled_animations()
        await pilot.pause()
        assert app.detail_scroll.scroll_y > 0, desc
        app.items_table.move_cursor(row=app.items_table.get_row_index(f"{sid}|{q}"))
        await pilot.pause()
        assert focused_id(app) == "answer", f"{desc}: off the doc, focus goes to the box back in its place"
        await pilot.press("shift+tab")
        assert focused_id(app) == "items", f"{desc}: the box is the stop again"
        await pilot.press("tab")
        assert focused_id(app) == "answer", f"{desc}: the box is the stop again"


@pytest.mark.anyio
@pytest.mark.parametrize("key, desc", [
    (None, "a click in a full-screen item's thread focuses its box (#77)"),
    ("pageup", "PgUp in its box scrolls the thread up"),
    ("pagedown", "PgDn scrolls it down"),
])
async def test_a_click_in_a_thread_focuses_its_box(store, sid, key, desc):
    q = store.post_item(sid, "question", "long?", "\n\n".join(f"line {i}" for i in range(200)))
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.push_screen(ThreadView(sid, q))
        await pilot.pause()
        scroll = app.screen.query_one("#thread-scroll")
        scroll.scroll_to(y=30, animate=False)
        app.screen.query_one("#send-all").focus()
        await pilot.pause()
        await pilot.click(scroll, offset=(2, 2))
        await pilot.pause()
        assert focused_id(app) == "thread-answer", desc
        assert scroll.scroll_y == 30, f"{desc}: where it was scrolled"
        if key:
            await pilot.press(key)
            await pilot.wait_for_scheduled_animations()
            await pilot.pause()
            assert (scroll.scroll_y < 30 if key == "pageup" else scroll.scroll_y > 30), desc
            assert focused_id(app) == "thread-answer", f"{desc}: focus stays in the box"


def stop_id(app) -> str | None:
    """The focused stop: its id, and inside the stats pane which of its stops."""
    pane = next(iter(app.query("#stats")), None)
    return f"stats:{pane.at}" if pane is not None and app.focused is pane else focused_id(app)


@pytest.mark.usefixtures("sdk_on")
@pytest.mark.anyio
@pytest.mark.parametrize("key, order, desc", [
    ("tab", ["shell", "stats:slider", "stats:zoom-out", "stats:zoom-in", "stats:info", "session-list"],
     "after the buttons (Shell the last: Send all has nothing to send): the stats pane's effort slider, then "
     "its zoom's - and + (#77), then its session info's icon (#242)"),
    ("shift+tab", ["session-list", "stats:info", "stats:zoom-in", "stats:zoom-out", "stats:slider", "shell"],
     "and back"),
])
async def test_the_stats_panes_stops_follow_the_buttons(store, tmp_path, monkeypatch, key, order, desc):
    sid, app = await hosted_app(store, tmp_path, monkeypatch)
    async with app.run_test(size=(160, 60)) as pilot:   # the stats' charts, so their zoom: none at 40 rows
        await shown_stats(app, pilot)
        pane = app.query_one("#stats")
        assert pane.charts, f"{desc}: the charts, so the zoom, show"
        app.query_one(f"#{order[0]}").focus()
        await pilot.pause()
        seen = [stop_id(app)]
        for _ in order[1:]:
            await pilot.press(key)
            await pilot.pause()
            seen.append(stop_id(app))
        assert seen == order, f"{desc}: {seen}"
        pane.focus_part("zoom-in")
        app.screen.set_focus(pane)
        await pilot.pause()
        assert pane.render().plain.split("\n")[-1].endswith("2h  -   + "), desc
        await pilot.press("enter")
        await pilot.pause()
        assert pane.span == 3600 and stop_id(app) == "stats:zoom-in", f"{desc}: Enter presses the focused +"
        await pilot.press("right")
        await pilot.pause()
        assert store.session(sid)["effort"] == "high", f"{desc}: the slider's keys only on the slider"


@pytest.mark.anyio
@pytest.mark.parametrize("pane, key, desc", [
    ("conversation", None, "a long conversation, at its newest turn, stays there"),
    ("item", None, "a long item scrolled down part way stays there"),
    ("item", "pageup", "PgUp in the box scrolls the conversation up"),
    ("item", "pagedown", "PgDn in the box scrolls it down"),
])
async def test_a_click_in_the_conversation_focuses_the_answer_box(store, sid, tmp_path, monkeypatch, pane, key, desc):
    """#77: a click in the conversation puts focus in the answer box, the pane where it is
    scrolled (as D43 had it for Tab), and the box's PgUp and PgDn scroll the pane."""
    if pane == "conversation":
        long_conversation(sid, tmp_path, monkeypatch)
    else:
        store.post_item(sid, "question", "long?", "\n\n".join(f"line {i}" for i in range(200)))
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        if pane == "conversation":
            app.follow(sid)
            await scroll_by(pilot, [])
        else:
            await to_first_item(app, pilot)
            app.detail_scroll.scroll_to(y=30, animate=False)
        await pilot.pause()
        before, y = painted(app), app.detail_scroll.scroll_y
        assert y > 0, desc
        app.items_table.focus()
        await pilot.click(app.detail_scroll, offset=(2, 2))
        await pilot.wait_for_scheduled_animations()
        await pilot.pause()
        assert focused_id(app) == "answer", desc
        assert (app.detail_scroll.scroll_y, painted(app)) == (y, before), desc
        if key:
            await pilot.press(key)
            await pilot.wait_for_scheduled_animations()
            await pilot.pause()
            moved = app.detail_scroll.scroll_y - y
            assert (moved < 0 if key == "pageup" else moved > 0), desc
            assert focused_id(app) == "answer", f"{desc}: focus stays in the box"


@pytest.fixture
def keyed(store, tmp_path, monkeypatch):
    """A session running in the wheelhouse with an open permission (selected, so Allow,
    Always and Deny show) and an answer queued (so Send all is enabled): every button with
    a key in BUTTON_KEYS applies."""
    from claude_wheelhouse import liveness
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "live")
    sid = store.create_session(str(tmp_path), name="hosted", runner="sdk")
    ref = store.post_item(sid, "permission", "Bash: make")
    store.queue(sid, "queued", None)
    return sid, ref


def presses(app) -> list[str]:
    """Each button's press, recorded instead of done: a key's is dispatched to the app at
    once (action_press), as a click's reaches it."""
    pressed, dispatch = [], app._dispatch_message

    async def record(message):
        await dispatch(message) if not isinstance(message, Button.Pressed) else pressed.append(message.button.id)
    app._dispatch_message = record
    return pressed


@pytest.mark.anyio
@pytest.mark.parametrize("key, button", [("alt+i", "interrupt"), ("alt+c", "compact"),
                                         ("alt+y", "allow"), ("alt+w", "always"), ("alt+d", "deny"),
                                         ("alt+shift+a", "send-all")])
@pytest.mark.parametrize("where, pressed", [
    ("items", True), ("session-list", True), ("send-all", True), ("thread", True), ("answer", True), ("dialog", False),
])
async def test_the_button_keys(store, keyed, key, button, where, pressed):
    """#73: every button that was mouse-only has a key, pressing it on the screen in front,
    from a text box too (#189: each has a modifier), but not under a dialog."""
    desc = f"{key} for {button} from {where}"
    sid, ref = keyed
    app = WheelhouseApp(store)
    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        await to_first_item(app, pilot)
        assert app.selected == (sid, ref), desc
        await pick(app, pilot, sid)   # selected, for its buttons (#16), then its permission again
        app.items_table.move_cursor(row=app.items_table.get_row_index(f"{sid}|{ref}"))
        await pilot.pause()
        assert app.selected == (sid, ref), desc
        if where == "thread":
            app.push_screen(ThreadView(sid, ref))
            await pilot.pause()
            app.screen.query_one("#send-all").focus()   # out of its box: a button (#77)
        elif where == "dialog":
            app.push_screen(Confirm("sure?"))
        else:
            app.query_one(f"#{where}").focus()
        await pilot.pause()
        got = presses(app)
        await pilot.press(key)
        await pilot.pause()
        assert got == ([button] if pressed else []), desc
        if where == "answer":
            assert app.answer.text == "", f"{desc}: nothing typed"
        assert_one_current(app, desc) if where != "dialog" else None


@pytest.mark.anyio
@pytest.mark.parametrize("on, key, setup, said, desc", [
    (True, "alt+i", "tab", "Interrupt doesn't apply to this session", "Interrupt on a tab session"),
    (True, "alt+y", "question", "Allow doesn't apply to the item in context", "Allow on a question"),
    (False, "alt+i", "tab", "Interrupt doesn't apply to this session", "SDK mode off: Interrupt on a tab session says so too"),
    (False, "alt+y", "question", "Allow doesn't apply to the item in context", "and so does Allow on a question"),
    (False, "alt+m", "tab", "Model doesn't apply to this session", "and Model"),
    (False, "alt+shift+a", "nothing queued", "Send all (0): nothing to do", "Send all with nothing queued"),
    (False, "alt+r", "dead", "Rename doesn't apply to this session", "the session list's Alt+R on a dead session"),
])
async def test_a_button_key_that_does_not_apply_says_so(store, tmp_path, monkeypatch, on, key, setup, said, desc):
    from claude_wheelhouse import liveness
    monkeypatch.setattr(features, "_flag", on)
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "dead" if setup == "dead" else "live")
    sid = store.create_session(str(tmp_path), name="s", runner="tab" if setup == "tab" else "sdk")
    store.post_item(sid, "question", "q?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        if key in ("alt+r", "alt+m"):   # a session's own keys: it selected first, as with none they do nothing (#16)
            await pick(app, pilot, sid)
        (app.session_list if key == "alt+r" else app.items_table).focus()
        await pilot.pause()
        got, notes = presses(app), []
        monkeypatch.setattr(app, "notify", lambda text, **kw: notes.append(text))
        await pilot.press(key)
        await pilot.pause()
        assert (got, notes) == ([], [said] if said else []), desc
        assert not any("SDK" in n or "wheelhouse" in n for n in notes), f"{desc}: SDK mode goes unmentioned"


@pytest.mark.anyio
@pytest.mark.parametrize("runner_, shown, desc", [
    ("sdk", ["interrupt", "compact", "handover"], "a host's session, SDK mode off: the buttons it shows, and its menu's Handover"),
    ("tab", [], "a tab's: none of SDK mode's"),
])
async def test_the_overlay_lists_the_buttons_the_screen_shows(store, tmp_path, monkeypatch, runner_, shown, desc):
    from claude_wheelhouse.tui import KeysHelp, keys_help
    sid, app = await hosted_app(store, tmp_path, monkeypatch, runner_)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.follow(sid)
        await pilot.pause()
        app.session_list.focus()
        await pilot.press("f1")
        await pilot.pause()
        assert isinstance(app.screen, KeysHelp) and app.screen.shown == shown, desc
    text = keys_help(False, shown)
    for word in ("Interrupt", "Compact", "Handover"):
        assert (word in text) is bool(shown), f"{desc}: {word}"
    assert "Shell (Alt+H" in text, f"{desc}: Shell, the quick shell, is everyone's (#257)"
    for word in ("Model", "Allow", "effort"):
        assert word not in text, f"{desc}: {word} isn't showing"


def subagent_file(tmp_path, sid, aid, n):
    """A session's transcript in a fake projects folder, and a subagent's beside it: its
    prompt, then n tool calls."""
    folder = tmp_path / "projects/-home-u-repo"
    (folder / sid / "subagents").mkdir(parents=True, exist_ok=True)
    folder.joinpath(f"{sid}.jsonl").write_text("")
    recs = [{"type": "user", "isSidechain": True, "timestamp": "2026-10-10T22:25:07Z",
             "message": {"content": "find the VAT rounding bug"}}]
    recs += [{"type": "assistant", "isSidechain": True, "timestamp": "2026-10-10T22:25:30Z", "message": {"content": [
        {"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {"command": f"step {i}"}}]}} for i in range(n)]
    path = folder / sid / "subagents" / f"agent-{aid}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in recs))
    return path


async def laid(pilot):
    """The app's moves have landed and been laid out: a scroll to the end after a refresh too."""
    for _ in range(2):
        await pilot.wait_for_scheduled_animations()
        await pilot.pause()


def say(path, text):
    with open(path, "a") as f:
        f.write(json.dumps({"type": "assistant", "isSidechain": True, "timestamp": "2026-10-10T22:30:00Z",
                            "message": {"content": [{"type": "text", "text": text}]}}) + "\n")


@pytest.mark.anyio
@pytest.mark.both
async def test_a_subagent_items_own_conversation_follows_it_live(store, sid, tmp_path, monkeypatch, via):
    monkeypatch.setattr(transcript, "PROJECTS", tmp_path / "projects")
    path = subagent_file(tmp_path, sid, "ag1", 60)
    store.track_agent(sid, "ag1", "find the bug", "A `general-purpose` subagent.", datetime.now(timezone.utc))
    ref = store.agent_links(sid)["ag1"][0]
    q = store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await laid(pilot)
        items = app.query_one("#items", DataTable)
        items.focus()
        items.move_cursor(row=items.get_row_index(f"{sid}|{ref}"))
        await laid(pilot)
        text, scroll = app._detail_text, app.detail_scroll
        assert text.index("A `general-purpose` subagent.") < text.index(transcript.AGENT_HEAD) \
            < text.index("find the VAT rounding bug") < text.index("`⚙ Bash: step 59`"), "its body, then its conversation"
        assert scroll.max_scroll_y > 0 and scroll.scroll_y == scroll.max_scroll_y, "at the end"
        say(path, "It rounds per line.")
        app.refresh_data()
        await laid(pilot)
        assert app._detail_text.endswith("It rounds per line.") and scroll.scroll_y == scroll.max_scroll_y, \
            "followed live, at the end"
        scroll.scroll_to(y=5, animate=False, immediate=True)
        await laid(pilot)
        line = app.detail._layout(app.detail.size.width)[1][5]
        say(path, "Fixed it.")
        app.refresh_data()
        await laid(pilot)
        assert app._detail_text.endswith("Fixed it.")
        assert app.detail._layout(app.detail.size.width)[1][int(scroll.scroll_y)] == line, "a scrolled-up reader keeps their line"
        await pilot.press("enter")
        await laid(pilot)
        convo = app._detail_text[app._detail_text.index(transcript.AGENT_HEAD):]
        assert isinstance(app.screen, ThreadView) and app.screen.text.endswith(convo), "full screen, the same"
        thread = app.screen.query_one("#thread-scroll")
        assert thread.scroll_y == thread.max_scroll_y > 0, "opened at the end"
        await pilot.press("escape")
        items.focus()
        items.move_cursor(row=items.get_row_index(f"{sid}|{q}"))
        await laid(pilot)
        if via == "local":
            assert app.hub.agent_followers == {}, "no longer followed once another item is selected"
        else:   # the daemon keeps the last two any client asked for; this one no longer asks
            app.refresh_data()   # what was shown before the selection moved: then not
            app.refresh_data()
            assert not [k for k in app.client.wanted if k[0] == "agent"], "no longer fetched once another is selected"


@pytest.mark.anyio
async def test_a_subagent_item_whose_transcript_is_missing_says_so(store, sid, tmp_path, monkeypatch):
    monkeypatch.setattr(transcript, "PROJECTS", tmp_path / "projects")
    store.track_agent(sid, "gone", "find the bug", "A `general-purpose` subagent.", datetime.now(timezone.utc))
    ref = store.agent_links(sid)["gone"][0]
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        items = app.query_one("#items", DataTable)
        items.move_cursor(row=items.get_row_index(f"{sid}|{ref}"))
        await pilot.pause()
        assert "A `general-purpose` subagent." in app._detail_text and transcript.NO_AGENT_FILE in app._detail_text


def placed(strip) -> list[tuple]:
    """A drawn line, character by character with its style and where it is in the widget's
    Text, as a selection by mouse finds it: the same whatever its segments."""
    out = []
    for seg in strip:
        meta = seg.style.meta if seg.style else {}
        for j, ch in enumerate(seg.text):
            x, n = meta.get("offset") or (None, None)
            out.append((ch, seg.style and seg.style.without_color, seg.style and seg.style.color,
                        seg.style and seg.style.bgcolor, None if n is None else (x + j, n),
                        sorted((k, v) for k, v in meta.items() if k != "offset")))
    return out


# a fixed time for a test that needs a given chart: the charts' buckets sit on epoch multiples
# (stats.window), so the bars' heights, and which lines a shimmer moves, follow the time.
# At 160x58 the top bar line shimmers at this one, and at about half of real times
SHIMMER_NOW = 1_800_000_000.0


def busy_snapshot(now: float) -> transcript_usage.Snapshot:
    """A session with turns all across the charts' span, of every kind: bars of every colour."""
    turns = [transcript_usage.Turn(now - 7000 + 37 * i, 1000 * (i % 50), 500 * (i % 7), 300 * (i % 3), 90_000 + 100 * i, "1h",
                        "claude-opus-5-5", i % 4 > 0, output=200 * (i % 11)) for i in range(180)]
    return transcript_usage.Snapshot(model="claude-opus-5-5", context=110_000, peak=120_000, last_at=now - 30, ttl="1h", turns=turns)


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(160, 60), (315, 98)])
async def test_a_shimmer_frame_draws_the_charts_as_the_whole_panel_did(store, sid, size, monkeypatch):
    import time
    app = WheelhouseApp(store)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        pane = app.query_one("#stats")
        monkeypatch.setattr(pane, "tick", lambda: None)   # the made-up session stays shown
        pane.show(busy_snapshot(time.time()), "demo", time.time())
        await pilot.pause()
        assert pane.charts and pane.bars, "the charts show, and their bars are known"
        height = pane.size.height
        for frame in (0, 1, 7, 40, 123, 1 + stats.PERIOD, 7 + 2 * stats.PERIOD):   # the last two recalled
            pane.frame = frame
            got = [placed(pane.render_line(y)) for y in range(height)]
            whole = Visual.to_strips(pane, visualize(pane, pane.render()), *pane.size, pane.visual_style)
            whole += [Strip.blank(pane.size.width, pane.visual_style.rich_style)] * (height - len(whole))   # as Widget's
            want = [placed(s) for s in whole]
            assert got == want, f"frame {frame}"


@pytest.mark.anyio
async def test_a_shimmer_cycle_is_drawn_once_and_kept_through_a_turn_into_the_last_bar(store, sid, monkeypatch):
    import time
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 60)) as pilot:
        await pilot.pause()
        pane = app.query_one("#stats")
        monkeypatch.setattr(pane, "tick", lambda: None)   # the made-up session stays shown
        now = time.time()
        snap = busy_snapshot(now)
        pane.show(snap, "demo", now)
        await pilot.pause()
        for frame in range(1, stats.PERIOD + 1):   # a whole cycle drawn
            pane.frame = frame
            for y in pane.bars:
                pane.render_line(y)
        drawn, was = [], [c.columns for c in pane.charts]
        monkeypatch.setattr(pane, "draw", lambda y, k: drawn.append((y, k)))
        snap.turns.append(transcript_usage.Turn(now - 1, 2000, 100, 100, 99_000, "1h", "claude-opus-5-5", True, output=100))
        pane.show(snap, "demo", now)   # a turn: into the last bar, on the same scale
        assert all(c.columns[:-1] == w[:-1] for c, w in zip(pane.charts, was)), "the bars but the last as they were"
        assert any(c.columns[-1] != w[-1] for c, w in zip(pane.charts, was)), "the last bar grown"
        for frame in (stats.PERIOD + 3, 2 * stats.PERIOD + 40):
            pane.frame = frame
            got = [placed(pane.render_line(y)) for y in range(pane.size.height)]
            whole = Visual.to_strips(pane, visualize(pane, pane.render()), *pane.size, pane.visual_style)
            assert got[:len(whole)] == [placed(s) for s in whole], f"frame {frame}"
        assert not drawn, "every frame recalled, the last bar drawn on its own"


@pytest.mark.anyio
async def test_a_shimmer_frame_redraws_only_the_charts_bars(store, sid, monkeypatch):
    import time
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 60)) as pilot:
        await pilot.pause()
        pane = app.query_one("#stats")
        monkeypatch.setattr(pane, "tick", lambda: None)   # the made-up session stays shown
        pane.show(busy_snapshot(time.time()), "demo", time.time())
        await pilot.pause()
        built = []
        monkeypatch.setattr(stats, "render", lambda *a, _r=stats.render: (built.append(1), _r(*a))[1])
        dirty = []
        monkeypatch.setattr(pane, "refresh", lambda *regions, **k: dirty.extend(regions))
        pane.animate(pane.frame + 1)
        lines = {y for r in dirty for y in range(r.y, r.bottom)}
        assert lines == set(pane.bars), "the bars' lines alone, in the content's place"
        for y in range(pane.size.height):
            pane.render_line(y)
        assert not built, "the panel's Text isn't built again for a frame"
        pane.rebuild()   # a tick with nothing new: nothing drawn again
        assert not built and not dirty[len(lines):]


@pytest.mark.anyio
async def test_a_shimmer_frame_moves_the_bars_on_screen_under_a_border(store, sid, monkeypatch, clock):
    """Each frame the test's own (clock): the app's animation, on real time, would otherwise
    move the pane to a frame of its own during a slow pause, the same as one already seen.
    And the time its turns are at (SHIMMER_NOW)."""
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 58)) as pilot:   # a chart whose top bar line moves
        await pilot.pause()
        pane = app.query_one("#stats")
        monkeypatch.setattr(pane, "tick", lambda: None)   # the made-up session stays shown
        pane.styles.border_top = ("solid", "#7b61ff")   # a top border: the content one line down
        pane.styles.padding = (1, 0, 0, 0)   # and a line more
        pane.show(busy_snapshot(SHIMMER_NOW), "demo", SHIMMER_NOW)
        await pilot.pause()
        assert pane.gutter.top == 2 and pane.bars
        region, tops = pane.content_region, []
        for frame in (1, 7, 40):
            pane.animate(frame)
            await pilot.pause()
            screen = app.screen._compositor.render_update(full=True).strips
            for y in sorted(pane.bars):
                shown = Strip.join(screen[region.y + y]).crop(region.x, region.right)
                looks = [[(ch, style.color, style.bgcolor) for ch, style in cells(line)]
                         for line in (shown, pane.render_line(y))]
                assert looks[0] == looks[1], f"frame {frame}: bar line {y} on screen as drawn"
            tops.append(str([(ch, style.color) for ch, style in cells(Strip.join(screen[region.y + min(pane.bars)]))]))
        assert len(set(tops)) == 3, "the top bar line moves on screen each frame"


# model and effort (.plan/model-effort.md)

@pytest.mark.anyio
@pytest.mark.parametrize("model, effort, launched, desc", [
    (None, None, ("claude-sonnet-5-5", "high"), "left as they open: Sonnet 5.5 at high (Q50)"),
    ("claude-haiku-5-5", None, ("claude-haiku-5-5", "xhigh"), "another model: Effort goes to its tier's (Q54)"),
    ("claude-opus-5-5", "max", ("claude-opus-5-5", "max"), "then any level"),
])
async def test_new_session_model_and_effort(store, tmp_path, monkeypatch, model, effort, launched, desc):
    from textual.widgets import Select
    opened = []
    monkeypatch.setattr(launch, "open_session", lambda s, i, may_tab=True: opened.append((s.session(i)["model"], s.session(i)["effort"])))
    app = WheelhouseApp(store)
    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.press("alt+n")
        await pilot.pause()
        app.screen.query_one("#cwd", Input).value = str(tmp_path)
        models_, efforts = app.screen.query_one("#model", Select), app.screen.query_one("#effort", Select)
        assert [o[1] for o in models_._options] == ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5",
                                                     "claude-fable-5-1"], desc
        assert [o[1] for o in efforts._options] == ["low", "medium", "high", "xhigh", "max"], f"{desc}: every level"
        if model:
            models_.value = model
            await pilot.pause()
        if effort:
            efforts.value = effort
            await pilot.pause()
        await pilot.click("#launch")
        await pilot.pause()
    assert opened == [launched], desc


async def hosted_app(store, tmp_path, monkeypatch, runner_="sdk"):
    """A session run by a host (or a tab), live, on Sonnet 5.5 at high, applied, with a
    transcript the stats pane reads, highlighted."""
    from claude_wheelhouse import liveness
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "live")
    sid = store.create_session(str(tmp_path), name="demo", runner=runner_, model="claude-sonnet-5-5", effort="high")
    store.set_applied(sid, "claude-sonnet-5-5", "high")
    folder = tmp_path / "projects/-home-u-repo"
    folder.mkdir(parents=True, exist_ok=True)
    now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    rec = {"type": "assistant", "timestamp": now, "effort": "high", "message": {
        "id": "m1", "model": "claude-sonnet-5-5", "content": [],
        "usage": {"input_tokens": 2, "cache_creation_input_tokens": 829, "cache_read_input_tokens": 82_000,
                  "cache_creation": {"ephemeral_1h_input_tokens": 829}}}}
    folder.joinpath(f"{sid}.jsonl").write_text(json.dumps(rec) + "\n")
    monkeypatch.setattr(transcript, "PROJECTS", tmp_path / "projects")
    return sid, WheelhouseApp(store)


async def shown_stats(app, pilot) -> str:
    """The stats pane, on the one session there is (followed), once its transcript is read."""
    if app.filter_sid is None:
        app.follow(app.sessions[0]["id"])
    app.refresh_data()
    await app.workers.wait_for_complete()
    await pilot.pause()
    return app.query_one("#stats").render().plain


@pytest.mark.anyio
@pytest.mark.parametrize("on, runner_, slider, desc", [
    (True, "sdk", True, "a session run by a host: Model and the slider, which is no Tab stop (#77)"),
    (True, "tab", False, "a tab: neither (Q52)"),
    (False, "sdk", False, "SDK mode off: a host's session runs on as it was set, with neither"),
])
async def test_model_and_effort_are_offered_only_for_a_hosted_session(store, tmp_path, monkeypatch, on, runner_, slider,
                                                                        desc):
    monkeypatch.setattr(features, "_flag", on)
    sid, app = await hosted_app(store, tmp_path, monkeypatch, runner_)
    async with app.run_test(size=(160, 40)) as pilot:
        shown = await shown_stats(app, pilot)
        assert "demo · Sonnet 5.5 · high · 1M window" in shown, desc
        assert ("effort  low medium high xhigh max" in shown) == slider, desc
        assert app.query_one("#model", Button).display == slider, desc
        app.query_one("#detail").focus()
        await pilot.press("tab")
        await pilot.pause()
        assert focused_id(app) != "stats" or stop_id(app) == "stats:info", \
            f"{desc}: Tab goes round the inbox's own stops alone (#77), the stats pane's session info (#242) its one stop"
        app.session_list.focus()
        await pilot.press("alt+m")
        await pilot.pause()
        assert [type(s).__name__ for s in app.screen_stack] == (["Screen", "ChooseModel"] if slider else ["Screen"])
        if slider:
            assert tui.MODEL_WARNING in str(app.screen.query_one("#model-warning", Label).content), "the cache warning"
            options = app.screen.query_one("#models")
            assert options.highlighted == 1 and "Sonnet 5.5 (now)" in str(options.get_option_at_index(1).prompt)
            await pilot.press("escape")
            await pilot.pause()
        assert_one_current(app, desc)
    assert store.session(sid)["model"] == "claude-sonnet-5-5", desc


@pytest.mark.usefixtures("sdk_on")
@pytest.mark.anyio
async def test_a_model_change_says_so_and_shows_as_applying(store, tmp_path, monkeypatch):
    sid, app = await hosted_app(store, tmp_path, monkeypatch)
    async with app.run_test(size=(160, 40)) as pilot:
        await shown_stats(app, pilot)
        app.session_list.focus()
        await pilot.press("alt+m", "up", "enter")
        await pilot.pause()
        shown = await shown_stats(app, pilot)
        assert "demo · Opus 5.5 · high · 1M window" in shown and "applying…" in shown, "until its host confirms"
        store.set_applied(sid, "claude-opus-5-5", "high")
        assert "applying…" not in await shown_stats(app, pilot)
        assert_one_current(app, "after a model change")
    assert store.session(sid)["model"] == "claude-opus-5-5"


@pytest.mark.usefixtures("sdk_on")
@pytest.mark.anyio
@pytest.mark.parametrize("level", ["low", "xhigh", "max"])
async def test_a_click_on_a_level_sets_it(store, tmp_path, monkeypatch, level):
    sid, app = await hosted_app(store, tmp_path, monkeypatch)
    async with app.run_test(size=(160, 40)) as pilot:
        await shown_stats(app, pilot)
        pane = app.query_one("#stats")
        start = next(start for start, end, name in stats.slider_spans() if name == level)
        # from the pane's content: the panel's left border, then a cell into the level
        top = pane.content_region.offset - pane.region.offset
        await pilot.click("#stats", offset=(top.x + 1 + start + 1, top.y + stats.SLIDER_ROW))
        await pilot.pause()
        assert store.session(sid)["effort"] == level
        shown = await shown_stats(app, pilot)
        assert f"demo · Sonnet 5.5 · {level} · 1M" in shown and "applying…" in shown
        assert pane.has_focus, "a click focuses the slider, so the arrows go on from there"


@pytest.mark.anyio
@pytest.mark.parametrize("tokens, keys, dialog, stopped, desc", [
    (200_000, ("alt+c",), "RelaunchChoice", ("compact", (4242, 1)), "past 150k: Alt+C compacts first"),
    (200_000, ("alt+r",), "RelaunchChoice", ("now", (4242, 1)), "past 150k: Alt+R relaunches now"),
    (200_000, ("escape",), "RelaunchChoice", None, "Esc cancels"),
    (200_000, ("enter",), "RelaunchChoice", None, "it opens on Cancel: a reflex Enter relaunches nothing"),
    (100_000, (), None, ("now", (4242, 1)), "a small one, idle: relaunched at once, no confirm"),
    (200_000, ("tab", "enter"), "RelaunchChoice", ("compact", (4242, 1)),
     "Tab to Compact first, then Enter: it compacts first, and the app's Compact button doesn't answer too"),
    (200_000, ("click:#relaunch-compact",), "RelaunchChoice", ("compact", (4242, 1)), "Compact first clicked"),
    (200_000, ("click:#relaunch-now",), "RelaunchChoice", ("now", (4242, 1)), "Relaunch now clicked"),
    (200_000, ("click:#relaunch-cancel",), "RelaunchChoice", None, "Cancel clicked"),
])
@pytest.mark.both
async def test_relaunch_offers_to_compact_a_big_session_first(sdk_on, store, sid, monkeypatch, tokens, keys, dialog,
                                                              stopped, desc):
    from claude_wheelhouse import hub as hub_mod, liveness
    store.set_runner(sid, "sdk")
    store.db.execute("UPDATE sessions SET claude_pid = 4242, claude_start = 1, boot_id = 'b', context_tokens = ?",
                     (tokens,))
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "live")
    calls = []
    monkeypatch.setattr(hub_mod.Hub, "stop_host",
                        lambda self, sid, host, may_tab=True: calls.append(("now", host)) or hub_mod.Done())
    monkeypatch.setattr(hub_mod.Hub, "compact_then_relaunch",
                        lambda self, sid, host, may_tab=True: calls.append(("compact", host)) or hub_mod.Done())
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pick(app, pilot, sid)
        app.session_list.focus()
        main = type(app.screen).__name__
        await pilot.press("alt+l")
        await pilot.pause()
        assert type(app.screen).__name__ == (dialog or main), desc
        for key in keys:
            if key.startswith("click:"):
                await pilot.click(key.removeprefix("click:"))
            else:
                await pilot.press(key)
            await pilot.pause()
        assert calls == ([stopped] if stopped else []), desc
        assert type(app.screen).__name__ == main, f"{desc}: answered"
        assert app.store.session(sid)["host_command"] is None, f"{desc}: no Compact, Interrupt or Shell sent besides"


@pytest.mark.anyio
async def test_a_tabs_header_shows_what_its_transcript_says_it_runs(store, tmp_path, monkeypatch):
    """Created on Sonnet, then a /model in the tab: its transcript records Opus, and so do the
    header and the columns, so a relaunch keeps it (Q56)."""
    sid, app = await hosted_app(store, tmp_path, monkeypatch, "tab")
    path = tmp_path / f"projects/-home-u-repo/{sid}.jsonl"
    rec = json.loads(path.read_text())
    later = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() + 2))   # a turn begun since
    prompt = {"type": "user", "timestamp": later, "message": {"role": "user", "content": "go on"}}
    rec |= {"timestamp": later, "requestedModel": "claude-opus-5-5[1m]", "effort": "max"}
    rec["message"] |= {"id": "m2", "model": "claude-opus-5-5"}
    path.write_text(path.read_text() + json.dumps(prompt) + "\n" + json.dumps(rec) + "\n")
    async with app.run_test(size=(160, 40)) as pilot:
        assert "demo · Opus 5.5 · max · 1M window" in await shown_stats(app, pilot)
    s = store.session(sid)
    assert (s["model"], s["effort"]) == ("claude-opus-5-5", "max"), "copied back, as the pinned id"


@pytest.mark.usefixtures("sdk_on")
@pytest.mark.anyio
async def test_a_shell_tabs_model_shows_while_it_runs(store, tmp_path, monkeypatch):
    """Round 62, M1: a hosted session out in its shell tab, with a /model typed there: the
    stats pane shows the tab's model, not what the host launched with."""
    sid, app = await hosted_app(store, tmp_path, monkeypatch)
    store.set_shell(sid, "tab")
    path = tmp_path / f"projects/-home-u-repo/{sid}.jsonl"
    later = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() + 2))   # typed since
    rec = {"type": "system", "subtype": "local_command", "timestamp": later, "commandRun": {"command": "model"},
           "content": "<local-command-stdout>Set model to `Haiku 5.5` for this session only</local-command-stdout>"}
    path.write_text(path.read_text() + json.dumps(rec) + "\n")
    async with app.run_test(size=(160, 40)) as pilot:
        assert "demo · Haiku 5.5 · high" in await shown_stats(app, pilot)


@pytest.mark.usefixtures("sdk_on")
@pytest.mark.anyio
async def test_model_in_a_full_screen_item_says_so(store, tmp_path, monkeypatch):
    sid, app = await hosted_app(store, tmp_path, monkeypatch)
    ref = store.post_item(sid, "question", "q?")
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.push_screen(ThreadView(sid, ref))
        await pilot.pause()
        await pilot.press("tab")   # to its thread: out of the answer box, where M is typing
        await pilot.pause()
        assert not isinstance(app.focused, TextArea)
        got, notes = presses(app), []
        monkeypatch.setattr(app, "notify", lambda text, **kw: notes.append(text))
        await pilot.press("alt+m")
        await pilot.pause()
        assert (got, notes) == ([], ["Model doesn't apply here: it's with the session list's buttons"])
        assert [type(s).__name__ for s in app.screen_stack] == ["Screen", "ThreadView"]


@pytest.mark.usefixtures("sdk_on")
@pytest.mark.anyio
async def test_the_arrows_start_from_the_transcripts_effort_with_nothing_set(store, tmp_path, monkeypatch):
    """An old host: NULL columns, nothing applied. The slider lights the transcript's level,
    and Right goes on from there."""
    sid, app = await hosted_app(store, tmp_path, monkeypatch)
    store.db.execute("UPDATE sessions SET model = NULL, effort = NULL, applied_model = NULL, applied_effort = NULL")
    async with app.run_test(size=(160, 40)) as pilot:
        await shown_stats(app, pilot)
        app.query_one("#stats").focus()
        await pilot.press("right")
        await pilot.pause()
        assert store.session(sid)["effort"] == "xhigh", "high, from the transcript, then a level up"
        assert_one_current(app, "after the arrow")


@pytest.mark.usefixtures("sdk_on")
@pytest.mark.anyio
@pytest.mark.parametrize("arrow, level", [("◂", "medium"), ("▸", "xhigh")])
async def test_a_click_on_an_arrow_steps_the_narrow_slider(store, tmp_path, monkeypatch, arrow, level):
    sid, app = await hosted_app(store, tmp_path, monkeypatch)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one("#stats").styles.width = 32   # narrower than column 1 lets it be
        shown = await shown_stats(app, pilot)
        pane = app.query_one("#stats")
        width = pane.size.width - 2
        assert width < stats.LABEL + len(stats.LEVELS), "the pane too narrow for every level"
        assert "◂  high  ▸" in shown and "Sonnet 5.5" in shown, "the effort and model readable"
        start = next(start for start, end, name in stats.slider_spans("high", width)
                     if stats.slider("high", False, width).plain[start:end] == arrow)
        top = pane.content_region.offset - pane.region.offset
        await pilot.click("#stats", offset=(top.x + 1 + start, top.y + stats.SLIDER_ROW))
        await pilot.pause()
        assert store.session(sid)["effort"] == level
        assert_one_current(app, "after a click on an arrow")



@pytest.mark.usefixtures("sdk_on")
@pytest.mark.anyio
async def test_focusing_the_slider_lays_it_out_focused_at_once(store, tmp_path, monkeypatch):
    """Focus lays the slider out as focused (its ←/→, where there's room) in the same frame,
    not on the next tick; blur as unfocused."""
    sid, app = await hosted_app(store, tmp_path, monkeypatch)
    async with app.run_test(size=(160, 40)) as pilot:
        await shown_stats(app, pilot)
        pane = app.query_one("#stats")
        focused = []
        real = stats.layout

        def spy(*args, **kwargs):
            focused.append(args[-1])
            return real(*args, **kwargs)
        monkeypatch.setattr(stats, "layout", spy)
        monkeypatch.setattr(type(pane), "tick", lambda self: None)   # no tick to catch it up
        pane.focus()
        await pilot.pause()
        assert pane.has_focus and focused[-1:] == [True], f"laid out on focus as {focused}"
        app.session_list.focus()
        await pilot.pause()
        assert focused[-1:] == [False], f"laid out on blur as {focused}"


# through the daemon, as the TUI runs by default (phase 2c)

def until(check, seconds=10.0, desc=""):
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, f"timed out: {desc}"
        time.sleep(0.02)


@pytest.mark.anyio
async def test_what_the_stream_brings_waits_for_the_tick(store, sid, served, clock):
    """An item a host posts, fetched in the background as the daemon's event says, shows
    at the refresh tick, never at a repaint a key makes: a burst acts on the rows it began on."""
    d = served(store.path)
    q = store.post_item(sid, "question", "first")
    app = WheelhouseApp(client=d.client())
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        until(lambda: app.client.last_event is not None, desc="the stream connected")
        app.refresh_data()
        rows = lambda: [r.key.value for r in app.items_table.ordered_rows if not r.key.value.endswith("|")]
        assert rows() == [f"{sid}|{q}"]
        q2 = store.post_item(sid, "question", "second")
        d.tick()
        until(lambda: ("inbox",) in app.client.waiting, desc="fetched")
        app.items_table.focus()
        await pilot.press("down", "up")   # repaints the list as the keys land
        assert rows() == [f"{sid}|{q}"], "not before the tick"
        clock.tick(app)
        await pilot.pause()
        assert set(rows()) == {f"{sid}|{q}", f"{sid}|{q2}"}, "at the tick"


@pytest.mark.anyio
async def test_a_lost_daemon_shows_in_the_title_and_refuses_what_would_act(store, sid, served, clock):
    d = served(store.path)
    q = store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(client=d.client())
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        until(lambda: app.client.last_event is not None, desc="the stream connected")
        app.refresh_data()
        app.items_table.move_cursor(row=app.items_table.get_row_index(f"{sid}|{q}"))
        await pilot.pause()
        d.stop()
        until(lambda: not app.client.connected, desc="lost")
        await pilot.pause()
        clock.animate(app)
        assert app.title_bar.render().plain.strip() == "daemon unreachable: reconnecting…"
        assert any("daemon unreachable" in n.message for n in app._notifications)
        app.answer.text = "SQLite"
        app.answer.focus()
        await pilot.press("ctrl+enter")
        await pilot.pause()
        assert app.answer.text == "SQLite", "what's typed stays"
        assert any(n.message == "not connected to the daemon: nothing was sent" for n in app._notifications)
        assert store.drafts(sid) == []


@pytest.mark.anyio
async def test_quitting_through_the_daemon_leaves_a_relaunch_to_it(store, sid, served, monkeypatch):
    d = served(store.path)
    exits = []
    monkeypatch.setattr(WheelhouseApp, "exit", lambda self, *a, **k: exits.append(True))
    app = WheelhouseApp(client=d.client(synchronous=True, before_fetch=d.tick))
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.client.relaunching = {sid}   # as the daemon's view says
        monkeypatch.setattr(app.client, "refresh", lambda: None)
        await app.action_quit()
        await pilot.pause()
        assert exits == [True] and not isinstance(app.screen, Confirm), "nothing to ask: the daemon carries it on"


# the idle app draws only what changes (#16)

@pytest.mark.anyio
@pytest.mark.parametrize("connected, frames, updates, desc", [
    (True, tui.SHIMMER_PERIOD, tui.SHIMMER_PERIOD, "the shimmer: each frame a new look"),
    (False, 6, 1, "the daemon out of reach: said once, not again each frame"),
])
async def test_the_title_shows_a_look_only_once_it_changes(store, monkeypatch, frame_clock, connected, frames, updates,
                                                            desc):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.client.connected = connected
        app.title_bar.show("before", Text("before"))
        shown = []
        update = app.title_bar.update
        monkeypatch.setattr(app.title_bar, "update", lambda *a, **k: (shown.append(a), update(*a, **k)))
        for _ in range(frames):
            app.animate()
        assert len(shown) == updates, desc


@pytest.mark.anyio
async def test_the_titles_kept_lines_are_its_looks_as_drawn_afresh(store, monkeypatch):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        title = app.title_bar
        for frame in range(3 * tui.SHIMMER_PERIOD):   # the first round draws, the rest recall
            monkeypatch.setattr(WheelhouseApp, "now", staticmethod(lambda: app.started + frame / tui.FRAMES))
            app.animate()
            got = cells(title.render_line(0))
            title._render_content()   # as a plain Static draws it
            assert got == cells(title._render_cache.lines[0]), f"frame {frame}"
            assert "".join(ch for ch, _ in got).strip() == tui.TITLE.strip(), f"frame {frame}"


@pytest.mark.anyio
@pytest.mark.parametrize("first, then, drawn, desc", [
    (0, 1, False, "the eye where it was: nothing drawn again"),
    (0, 4, True, "the eye moved: its row drawn again"),
])
async def test_an_eye_that_stays_put_draws_nothing_again(store, sid, monkeypatch, first, then, drawn, desc):
    monkeypatch.setattr(WheelhouseApp, "busy", lambda self, s: s == sid)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        t = app.session_list
        assert (tui.eye_at(first) != tui.eye_at(then)) == drawn, desc
        t.sweep(first)
        rows = []
        monkeypatch.setattr(t, "refresh_row", lambda row: rows.append(row))
        t.sweep(then)
        assert bool(rows) == drawn, desc
        assert t.eye_frame == then, f"{desc}: the frame drawn from is the latest"


@pytest.mark.anyio
@pytest.mark.parametrize("paint", ["paint_session_info", "paint_sendbar"])
async def test_a_repaint_finds_its_widgets_without_querying(store, sid, monkeypatch, paint):
    from textual.dom import DOMNode
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        getattr(app, paint)()   # finds them, the once
        asked = []
        query = DOMNode.query
        monkeypatch.setattr(DOMNode, "query", lambda self, *a: (asked.append(a), query(self, *a))[1])
        getattr(app, paint)()
        assert asked == [], paint


@pytest.mark.anyio
@pytest.mark.both
async def test_a_refresh_with_nothing_new_draws_nothing_again(store, sid, monkeypatch):
    store.post_item(sid, "task", "a task")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.refresh_data()
        await pilot.pause()
        drawn = []
        for w in app.screen.query("*"):
            refresh = w.refresh
            monkeypatch.setattr(w, "refresh", functools.partial(
                lambda w, refresh, *a, **k: (drawn.append(w), refresh(*a, **k))[1], w, refresh))
        app.refresh_data()
        assert drawn == [], "nothing changed, nothing drawn"


@pytest.mark.anyio
@pytest.mark.both
@pytest.mark.parametrize("where, dirty, tick, same, want_cwd, desc", [
    ("plain", False, ["T1"], False, "plain", "not a worktree: no choice, the same directory, a ticked task moves"),
    ("linked", True, ["Q1", "T1"], False, "new", "a worktree: a new one by default, the uncommitted warning shown"),
    ("linked", True, [], True, "linked", "this worktree chosen: no branch, the warning hidden"),
])
async def test_fork_dialog(store, tmp_path, worktree, claude_home, monkeypatch, via, where, dirty, tick, same, want_cwd, desc):
    from textual.widgets import RadioButton, SelectionList
    monkeypatch.setattr(launch, "open_tab", lambda s, i, notice=True: None)
    main, wt = worktree
    (tmp_path / "plain").mkdir()
    cwd = str(wt if where == "linked" else tmp_path / "plain")
    sid = store.create_session(cwd, name="demo")
    claude_home(sid, cwd)
    if dirty:
        (wt / "wip.txt").write_text("wip")
    store.post_item(sid, "task", "todo one")
    store.post_item(sid, "task", "running one", status="running")
    store.post_item(sid, "question", "which way?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pick(app, pilot, sid)
        app.session_list.focus()
        await pilot.press("alt+k")
        await pilot.pause()
        dialog = app.screen
        assert type(dialog).__name__ == "ForkSession", desc
        offered = dialog.query_one("#fork-items", SelectionList)
        assert sorted(offered.get_option_at_index(i).value for i in range(offered.option_count)) == ["Q1", "T1"], \
            f"{desc}: open questions and todo or blocked tasks, not running ones"
        assert offered.selected == [], f"{desc}: all unticked"
        for ref in tick:
            offered.select(ref)
        assert bool(dialog.query("#fork-where")) == (where == "linked"), desc
        if where == "linked":
            assert dialog.query_one("#fork-new", RadioButton).value, f"{desc}: a new worktree by default"
            assert dialog.query_one("#fork-branch", Input).value == "feat/x-fork", desc
            if same:
                dialog.query_one("#fork-same", RadioButton).value = True
                await pilot.pause()
            assert dialog.query_one("#fork-warning").display == (not same), desc
            assert dialog.query_one("#fork-branch", Input).disabled == same, desc
        await pilot.click("#fork-go")
        await app.workers.wait_for_complete()   # the worktree and the copy are made on a worker
        await pilot.pause()
        forks = [s for s in store.sessions() if s["id"] != sid]
        assert [s["name"] for s in forks] == ["demo (fork)"], desc
        want = {"plain": cwd, "linked": cwd, "new": str(main.resolve() / ".worktrees/feat-x-fork")}[want_cwd]
        assert forks[0]["cwd"] == want, desc
        assert sorted(i["ref"] for i in store.items(forks[0]["id"])) == sorted(tick), f"{desc}: the ticked moved"
        if via == "daemon":   # that test client has no stream: it never hears the fork's notice
            return
        for _ in range(50):   # through the daemon, word of the fork comes on the stream
            if app.current_session() == forks[0]["id"]:
                break
            app.refresh_data()
            await pilot.pause(0.05)
        assert app.current_session() == forks[0]["id"], f"{desc}: the fork selected as its row appears (#116)"


def item_row(app, ref: str) -> int:
    """The item list's row showing `ref` (as its ref cell reads), wherever the conversation rows are."""
    items = app.query_one("#items", DataTable)
    return next(i for i in range(items.row_count) if str(items.get_row_at(i)[2]) == ref)


def item_list_rows(app) -> list[list[str]]:
    """The items' rows, their session and ref cells: not the sessions' conversation rows."""
    items = app.query_one("#items", DataTable)
    return [[str(c) for c in items.get_row(k)][1:3] for k in items.keys() if not k.endswith("|")]


def family(store, sid):
    """T1 with two subagents under it, and A3 with no parent, in its session's group."""
    with one_second():
        t = store.post_item(sid, "task", "build it", status="running")
        kids = [store.post_item(sid, "agent", f"helper {n}", status="running") for n in (1, 2)]
        store.post_item(sid, "agent", "orphan", status="running")
    for k in kids:
        store.db.execute("UPDATE items SET parent_ref = ? WHERE session_id = ? AND ref = ?", (t, sid, k))
    return t, kids


@pytest.mark.anyio
@pytest.mark.both
async def test_children_sit_under_their_parent_and_its_control_hides_them(store, sid):
    t, kids = family(store, sid)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        items = app.query_one("#items", DataTable)
        assert item_list_rows(app) == [["demo", t], ["", "└ A2"], ["", "└ A1"], ["demo", "└ A3"]], \
            "children under their parent; the orphan in its session's group"
        n = first_item(app)   # the conversation rows above (#236)
        line = items.render_line(1 + n).text   # the header is line 0
        assert line.endswith("-"), "an expanded parent ends with its -"
        assert not items.render_line(2 + n).text.endswith(("-", "+")), "a child has no control"
        app.items_table.move_cursor(row=3 + n)   # the orphan selected
        await pilot.pause()
        selected = app.selected
        await pilot.click("#items", offset=(items.size.width - 1, 1 + n))
        await pilot.pause()
        assert item_list_rows(app) == [["demo", t], ["demo", "└ A3"]], "collapsed: its children hidden"
        assert items.render_line(1 + n).text.endswith("+"), "a collapsed parent ends with its +"
        assert app.selected == selected and isinstance(app.screen, type(app.screen_stack[0])), \
            "the click neither selects nor opens the row"
        await pilot.click("#items", offset=(items.size.width - 1, 1 + n))
        await pilot.pause()
        assert len(item_list_rows(app)) == 4, "expanded again"


@pytest.mark.anyio
@pytest.mark.both
@pytest.mark.parametrize("answer, said, closed, desc", [
    ("alt+y", "and its 2 children? 1 still open and 1 unread.", True, "yes takes the parent and every child not running"),
    ("alt+n", "and its 2 children? 1 still open and 1 unread.", False, "no takes nothing"),
    ("escape", "and its 2 children? 1 still open and 1 unread.", False, "Esc takes nothing"),
])
async def test_delete_on_a_parent_asks_then_takes_its_children(store, sid, answer, said, closed, desc):
    with one_second():
        q = store.post_item(sid, "question", "which db?")
        kids = [store.post_item(sid, "agent", "look", status="running"),
                store.post_item(sid, "question", "and which port?"),
                store.post_item(sid, "decision", "used SQLite", why="local")]
    for k in kids:
        store.db.execute("UPDATE items SET parent_ref = ? WHERE session_id = ? AND ref = ?", (q, sid, k))
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.items_table.focus()
        app.items_table.move_cursor(row=item_row(app, q))
        await pilot.pause()
        await pilot.press("delete")
        await pilot.pause()
        assert isinstance(app.screen, Confirm), desc
        assert said in app.screen.prompt and "1 running child stays in the list" in app.screen.prompt, desc
        await pilot.press(answer)
        await pilot.pause()
    finished = [tui.standing(store.item(sid, r)) == "finished" for r in (q, *kids)]
    assert finished == [closed, False, closed, closed], desc
    assert (store.item(sid, kids[0])["parent_ref"] is None) == closed, f"{desc}: the running subagent orphaned"


@pytest.mark.anyio
@pytest.mark.parametrize("shape, said, kept, desc", [
    # each child: (its parent's index, -1 for T1, and its status); kept: the indexes that stay, orphaned
    ([(-1, "running"), (-1, "done")], "and its 1 child? It stays deleted, even once finished or changed. "
     "1 running child stays in the list, at the top level.", {0}, "a running child stays, a finished one goes"),
    ([(-1, "running"), (-1, "running")], "(build it)? 2 running children stay in the list, at the top level.",
     {0, 1}, "only running children: the parent alone goes"),
    ([(-1, "answered"), (0, "running"), (0, "done")], "and its 2 children? They stay deleted, even once finished "
     "or changed. 1 running child stays", {1}, "a running grandchild under a finished child stays too"),
])
async def test_delete_on_a_parent_leaves_its_running_children(store, sid, shape, said, kept, desc):
    """T125: Delete on a parent orphans its running children, at any depth, so nothing runs out of
    sight; Ctrl+R brings the parent back and leaves them where they are."""
    t = store.post_item(sid, "task", "build it", status="done")
    refs = []
    for up, status in shape:
        kind = "question" if status == "answered" else "agent"
        refs.append(store.post_item(sid, kind, "a child", status=status))
        store.db.execute("UPDATE items SET parent_ref = ? WHERE session_id = ? AND ref = ?",
                         (t if up < 0 else refs[up], sid, refs[-1]))
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.items_table.focus()
        await pilot.press("alt+v")   # the parent stays in sight, finished, for Ctrl+R
        await pilot.pause()
        app.items_table.move_cursor(row=item_row(app, t))
        await pilot.pause()
        await pilot.press("delete")
        await pilot.pause()
        assert said in app.screen.prompt, f"{desc}: {app.screen.prompt}"
        await pilot.press("alt+y")
        await pilot.pause()
        for n, r in enumerate(refs):
            item = store.item(sid, r)
            assert (item["dismissed"] != "deleted", item["parent_ref"] is None) == (n in kept, n in kept), f"{desc}: {r}"
        assert [r[1] for r in item_list_rows(app)].count(t) == 1, f"{desc}: the parent shown, finished"
        await pilot.press("ctrl+r")
        await pilot.pause()
    assert tui.standing(store.item(sid, t)) != "finished", f"{desc}: Ctrl+R brings the parent back"
    assert all(store.item(sid, refs[n])["parent_ref"] is None for n in kept), f"{desc}: its orphans stay orphaned"


@pytest.mark.anyio
async def test_delete_on_an_item_with_no_children_asks_nothing(store, sid):
    q = store.post_item(sid, "question", "which db?", status="answered")   # an open one asks for its own sake
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.items_table.focus()   # answered, so settled: under the session's conversation (#192)
        app.items_table.move_cursor(row=app.items_table.get_row_index(f"{sid}|{q}"))
        await pilot.pause()
        await pilot.press("delete")
        await pilot.pause()
        assert not isinstance(app.screen, Confirm)
    assert store.item(sid, q)["status"] == "closed"



@pytest.mark.anyio
@pytest.mark.both
async def test_a_doc_sits_under_its_parent_and_opens_rendered(store, sid, tmp_path):
    doc = tmp_path / "plan.md"
    doc.write_text("# The plan\n\nBuild it **well**.\n")
    t = store.post_item(sid, "task", "design it", status="running")
    m = store.post_doc(sid, "the plan", str(doc), t)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert item_list_rows(app) == [["demo", t], ["", f"└ {m}"]], "the doc under its parent"
        app.items_table.move_cursor(row=app.items_table.get_row_index(f"{sid}|{m}"))
        await pilot.pause()
        assert app.selected == (sid, m)
        assert app.query_one("#answer").display, "the answer box under a doc, for a comment (split)"
        assert str(doc) in app._detail_text and "Build it **well**" in app._detail_text, "its path, then the file"
        assert "Build it well" in "\n".join(app.detail.render_line(y).text for y in range(12)), "rendered"
        dwell(app)
        assert store.item(sid, m)["status"] == "read", "in sight for the dwell: read"
        doc.write_text("# The plan\n\nBuild it twice.\n")
        app.refresh_data()
        await pilot.pause()
        assert "twice" in app._detail_text, "a change to the file shows"
        app.items_table.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, tui.DocView), "Enter opens it full screen"
        assert app.screen.query_one("#docview-answer", tui.Compose).display, "with its answer box, as in Detail"
        assert "twice" in app.screen.text
        doc.unlink()
        app.refresh_data()
        await pilot.pause()
        assert "The file has gone: this is the last text read." in app.screen.text, "a clear line"
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, tui.DocView)
        app.items_table.move_cursor(row=first_item(app))
        await pilot.pause()
        assert app.query_one("#answer").display, "the box back on a task"


@pytest.mark.anyio
async def test_a_doc_with_no_parent_sits_in_its_sessions_group(store, sid, tmp_path):
    (tmp_path / "notes.md").write_text("# Notes\n")
    q = store.post_item(sid, "question", "which db?")
    m = store.post_doc(sid, "the notes", str(tmp_path / "notes.md"))
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert item_list_rows(app) == [["demo", q], ["demo", f"└ {m}"]]
        assert str(app.items_table.get_row(f"{sid}|{m}")[3]) == "new"


@pytest.mark.anyio
@pytest.mark.parametrize("size, extra, desc", [
    ((160, 40), 0, "wide, no scrollbar"),
    ((160, 14), 30, "a vertical scrollbar showing"),
    ((80, 40), 0, "narrow: the title cut short"),
])
async def test_the_toggle_answers_its_click_at_any_width(store, sid, size, extra, desc):
    t, kids = family(store, sid)
    store.db.execute("UPDATE items SET title = ? WHERE ref = ?", ("a long title " * 8, t))
    for n in range(extra):
        store.post_item(sid, "task", f"filler {n}")
    app = WheelhouseApp(store)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        items = app.query_one("#items", DataTable)
        row = item_row(app, t)
        items.scroll_to(y=row, animate=False)
        await pilot.pause()
        y = row - int(items.scroll_y) + 1   # the header is line 0
        line = items.render_line(y).text
        assert line.endswith(" -"), f"{desc}: the - at the end, a blank before it: {line!r}"
        before, selected = len(item_list_rows(app)), app.selected
        x = items.scrollable_content_region.right - items.region.x - 1   # the last cell left of any scrollbar
        await pilot.click("#items", offset=(x, y), times=2)
        await pilot.pause()
        assert len(item_list_rows(app)) == before - 2, f"{desc}: a double-click toggles once"
        assert app.selected == selected and not isinstance(app.screen, ThreadView), f"{desc}: nothing selected nor opened"


@pytest.mark.anyio
@pytest.mark.both
async def test_delete_takes_agent_children_and_leaves_docs(store, sid, tmp_path):
    (tmp_path / "plan.md").write_text("# Plan\n")
    q = store.post_item(sid, "question", "which db?")
    store.track_agent(sid, "agent-1", f"{q}: look into it", "", datetime.now(timezone.utc))
    agent = store.agent_links(sid)["agent-1"][0]
    doc = store.post_doc(sid, "the plan", str(tmp_path / "plan.md"), q)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert item_list_rows(app) == [["demo", q], ["", f"└ {agent}"], ["", f"└ {doc}"]]
        app.items_table.focus()
        app.items_table.move_cursor(row=first_item(app))
        await pilot.pause()
        await pilot.press("delete")
        await pilot.pause()
        assert "child?" not in app.screen.prompt and "unread" not in app.screen.prompt, "the doc isn't counted"
        assert "1 running child stays in the list, at the top level." in app.screen.prompt
        await pilot.press("alt+y")
        await pilot.pause()
        assert item_list_rows(app) == [["demo", f"└ {agent}"], ["", f"└ {doc}"]], \
            "the running subagent and the doc stay, in their session's group"
        assert store.item(sid, doc)["dismissed"] is None, "a pinned doc never deleted with its parent"
        store.agent_status(sid, "agent-1", "done", "finished")   # as the transcript says it ended
        store.post_doc(sid, "the plan, v2", str(tmp_path / "plan.md"))
        await refresh(pilot, sid)
        # sorted: the highlight lands on the 💬 row above (#236), so neither is pinned, and their order
        # then varies with the client (in process or through the daemon)
        assert sorted(r[1] for r in item_list_rows(app)) == [f"└ {agent}", f"└ {doc}"], \
            "the pinned doc still there, at its session's level"


def test_items_from_an_older_daemon_have_no_parents():
    items = [{"session_id": "s", "ref": "T1", "kind": "task"}, {"session_id": "s", "ref": "A1", "kind": "agent"}]
    rows, counts = tui.item_rows([items, [], []], {"s": "demo"})
    assert [(it["ref"], place) for it, place, _ in rows] == [("T1", None), ("A1", 0)] and counts == {}


@pytest.mark.anyio
@pytest.mark.both
async def test_delete_on_marked_rows_asks_once_and_takes_the_children(store, sid, tmp_path):
    (tmp_path / "plan.md").write_text("# Plan\n")
    with one_second():
        t = store.post_item(sid, "task", "tidy up", status="done")
        q = store.post_item(sid, "question", "which db?")
    store.track_agent(sid, "agent-1", f"{q}: look into it", "", datetime.now(timezone.utc))
    agent = store.agent_links(sid)["agent-1"][0]
    doc = store.post_doc(sid, "the plan", str(tmp_path / "plan.md"), q)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.items_table.focus()
        app.items_table.set_marks({f"{sid}|{q}", f"{sid}|{t}"})
        await pilot.pause()
        await pilot.press("delete")
        await pilot.pause()
        assert isinstance(app.screen, Confirm), "a marked parent asks first"
        assert app.screen.prompt.startswith("Delete the 2 marked rows? 1 running"), \
            f"the doc under {q} neither taken nor counted: {app.screen.prompt}"
        assert "1 running child stays in the list" in app.screen.prompt, app.screen.prompt
        await pilot.press("alt+y")
        await pilot.pause()
        store.agent_status(sid, "agent-1", "done", "finished")
        await refresh(pilot, sid)
        # sorted: the highlight lands on the 💬 row above (#236), so neither is pinned, and their order
        # then varies with the client (in process or through the daemon)
        assert sorted(r[1] for r in item_list_rows(app)) == [f"└ {agent}", f"└ {doc}"], \
            "both rows gone, the running subagent and the pinned doc kept, at their session's level"


@pytest.mark.anyio
async def test_a_doc_maps_its_rows_to_source_lines_in_detail_and_full_screen(store, sid, tmp_path, monkeypatch):
    doc = tmp_path / "plan.md"
    doc.write_text("# The plan\n\nBuild it.\n\n- one\n- two\n\n```py\nx = 1\n```\n\n| alpha | beta |\n|---|---|\n| 1 | 2 |\n")
    # each block's first text: its block's first line, and the rows its block draws above it
    # (a code fence's padding, a table's blank line above its header)
    starts = {"The plan": (0, 0), "Build it.": (2, 0), "one": (4, 0), "x = 1": (7, 1), "alpha": (11, 1)}
    m = store.post_doc(sid, "the plan", str(doc))
    renders = []
    real = tui.docblocks.render
    monkeypatch.setattr(tui.docblocks, "render", lambda *a: (renders.append(1), real(*a))[1])
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.items_table.move_cursor(row=item_row(app, f"└ {m}"))
        await pilot.pause()
        for where in ("detail", "full screen"):
            view = app.detail if where == "detail" else app.screen.query_one("#docview", tui.Transcript)
            texts = view._layout(view.size.width)[1]
            for first, (line, above) in starts.items():
                row = next(y for y in range(view.doc_at[0], len(texts)) if first in texts[y])
                assert view.line_at(row) == line, f"{where}: a click on {first!r} is its first line"
                assert view.row_of(line) == row - above, f"{where}: and line {line} back to its block's first row"
            assert view.line_at(0) == 0, f"{where}: a row above the doc (its heading) takes the first block"
            count = len(renders)
            for _ in range(3):
                app.refresh_data()
                await pilot.pause()
            assert len(renders) == count, f"{where}: refreshing an unchanged doc renders nothing again"
            if where == "detail":
                app.items_table.focus()
                await pilot.press("enter")
                await pilot.pause()
                assert isinstance(app.screen, tui.DocView), "Enter opens it full screen"


@pytest.mark.anyio
async def test_a_long_doc_renders_off_the_loop(store, sid):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        long = "\n\n".join(f"## Section {i}\n\nprose **bold** `code`\n\n| a | b |\n|---|---|\n| 1 | 2 |" for i in range(400))
        detail = app.detail
        detail.update([("head", "## M1"), ("doc", long)])
        started = time.perf_counter()
        lines = detail._layout(detail.size.width)[1]
        assert time.perf_counter() - started < 0.2, "laid out without rendering it on the loop"
        assert any("rendering…" in line for line in lines), "a placeholder meanwhile"
        await app.workers.wait_for_complete()
        await pilot.pause()
        lines = detail._layout(detail.size.width)[1]
        assert any("Section 399" in line for line in lines), "then the doc"
        key = ("doc", long, detail.size.width + 3)
        detail.rendered(key)   # rendered for a width since left
        assert key not in detail.warm, "a stale render dropped"


async def rendered_off_loop(app, pilot) -> None:
    """Every doc render under way finished, and what it brought laid out."""
    for _ in range(100):
        await app.workers.wait_for_complete()
        await pilot.pause()
        if not app.detail.warming:
            break
    await pilot.pause()


@pytest.mark.anyio
async def test_a_long_doc_read_to_its_end_stays_there_through_a_rewrite(store, sid, tmp_path):
    """#200: rendered again off the loop and longer, a doc read to its end is still at its end,
    not short of it by the proportion kept for a place mid-way."""
    doc = tmp_path / "plan.md"
    doc.write_text("\n".join(f"## Section {i}\n\nprose **bold**\n\n- a\n- b\n" for i in range(300)))
    m = store.post_doc(sid, "the plan", str(doc))
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.items_table.move_cursor(row=item_row(app, f"└ {m}"))
        await pilot.pause()
        await rendered_off_loop(app, pilot)
        scroll = app.detail_scroll
        scroll.scroll_end(animate=False, immediate=True)
        await pilot.pause()
        lines = scroll.max_scroll_y
        doc.write_text(doc.read_text() + "\n".join(f"## More {i}\n\nprose\n" for i in range(40)))
        app.refresh_data()
        await pilot.pause()
        await rendered_off_loop(app, pilot)
        await pilot.pause()
        assert scroll.max_scroll_y > lines and scroll.scroll_y == scroll.max_scroll_y, "at the end of the longer doc"


@pytest.mark.anyio
async def test_a_long_doc_keeps_its_reading_place_through_a_rewrite_and_a_resize(store, sid, tmp_path, monkeypatch):
    doc = tmp_path / "plan.md"
    doc.write_text("\n".join(f"## Section {i}\n\nprose **bold**\n\n- a\n- b\n" for i in range(300)))
    m = store.post_doc(sid, "the plan", str(doc))
    laid = []
    real = tui.Transcript._layout
    monkeypatch.setattr(tui.Transcript, "_layout", lambda self, w: (
        laid.append(w) if self is getattr(app, "detail", None) and (self._laid is None or self._laid[0] != w) else None, real(self, w))[1])
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.items_table.move_cursor(row=item_row(app, f"└ {m}"))
        await pilot.pause()
        laid.clear()
        await rendered_off_loop(app, pilot)
        assert len(laid) <= 4, f"first show laid out a few times, not a scrollbar's thrash: {laid}"
        assert app.detail_scroll.styles.scrollbar_gutter == "stable", "its width the same, scrolling or not"
        scroll = app.detail_scroll
        scroll.scroll_to(y=scroll.max_scroll_y // 2, animate=False)
        await pilot.pause()
        place, lines = scroll.scroll_y, scroll.max_scroll_y
        doc.write_text(doc.read_text() + "\n\nmore\n")
        app.refresh_data()
        await pilot.pause()
        assert abs(scroll.scroll_y - place) <= 2, "mid-render: the last render stands in, at the same place"
        await rendered_off_loop(app, pilot)
        assert abs(scroll.scroll_y - place) <= 2, "rewritten: the place kept"
        await pilot.resize_terminal(120, 40)
        await pilot.pause()
        assert scroll.scroll_y > 0, "mid-render after a resize: not back at the top"
        await rendered_off_loop(app, pilot)
        assert abs(scroll.scroll_y / scroll.max_scroll_y - place / lines) < 0.02, "resized: the place kept, in proportion"
        app.items_table.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.query_one("#docview-scroll").styles.scrollbar_gutter == "stable", "full screen too"


@pytest.mark.anyio
@pytest.mark.both
async def test_delete_asks_and_takes_finished_children_too(store, sid, tmp_path):
    t = store.post_item(sid, "task", "design it", status="done")
    m = store.post_item(sid, "agent", "look", status="done")   # a finished child (a doc is never one: #106)
    store.db.execute("UPDATE items SET parent_ref = ? WHERE ref = ?", (t, m))
    store.dismiss(sid, m)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.items_table.focus()   # under the session's conversation, nothing being active (#192)
        app.items_table.move_cursor(row=app.items_table.get_row_index(f"{sid}|{t}"))
        await pilot.pause()
        await pilot.press("delete")
        await pilot.pause()
        assert isinstance(app.screen, Confirm), "children, even all finished: it asks"
        assert app.screen.prompt.endswith("and its 1 child? It stays deleted, even once finished or changed.")
        await pilot.press("alt+y")
        await pilot.pause()
        store.update_item(sid, m, status="failed")
        await refresh(pilot, sid)
        assert item_list_rows(app) == [], "the child changed again stays deleted"


@pytest.mark.anyio
async def test_a_marked_child_its_parent_took_is_not_said_twice(store, sid, tmp_path, monkeypatch):
    with one_second():
        q = store.post_item(sid, "question", "which db?")
        a = store.post_item(sid, "agent", "look", status="done")   # settled: closable when marked
    store.db.execute("UPDATE items SET parent_ref = ? WHERE ref = ?", (q, a))
    app = WheelhouseApp(store)
    said = []
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(app, "notify", lambda text, **kw: said.append(text))
        app.items_table.focus()
        app.items_table.set_marks({f"{sid}|{q}", f"{sid}|{a}"})
        await pilot.press("delete")
        await pilot.pause()
        await pilot.press("alt+y")
        await pilot.pause()
    assert said and said[-1].startswith(f"deleted with its children {q}") and a not in said[-1], said


# drag to reorder and header sort (T114, ordering.py)

def item_refs(app) -> list[str]:
    table = app.items_table
    # the order once any move has ended (Sink): a settled row rising to its place falls into it over 0.4 s
    return [str(table.get_row(k)[1 if app.filter_sid else 2]).lstrip("└ ") for k in table.final() if k and not k.endswith("|")]


def header_x(table, name: str | int) -> int:
    """A column's header, by key or (the session list's, which have none) by index."""
    x = 0
    for i, c in enumerate(table.ordered_columns):
        if name in (c.key.value, i):
            return x + 1
        x += c.get_render_width(table)
    raise KeyError(name)


async def drag_row(pilot, table, row: int, to: int, release: bool = True) -> None:
    """A row pressed, moved with the button held to another row, and let go there."""
    header = table.header_height
    await pilot.mouse_down(table, offset=(2, header + row))
    x, y = table.content_region.x + 2, table.content_region.y + header + to
    # a move with the button held, as a terminal sends one, at the screen's coordinates
    pilot.app.post_message(events.MouseMove(None, x, y, 0, to - row, button=1, shift=False, meta=False,
                                            ctrl=False, screen_x=x, screen_y=y))
    await pilot.pause()
    if release:
        await pilot.mouse_up(table, offset=(2, header + to))
        await pilot.pause()


@pytest.mark.anyio
@pytest.mark.both
async def test_drag_within_the_band_keeps_the_sort_and_across_turns_it_off(store, sid):
    """Q102, Q103, Q108 B (T121): a question open and three tasks running, the active band
    under Status *, and a task done below. A drag within the band keeps * on, even past
    another status: the row stays where it was dropped until its status changes, then goes
    in by status. A drag into the settled band turns ordering off, and the marker goes."""
    q = store.post_item(sid, "question", "which?")
    t1, t2, t3 = (store.post_item(sid, "task", f"t{n}", status="running") for n in (1, 2, 3))
    done = store.post_item(sid, "task", "d", status="done")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        table = app.items_table
        n = first_item(app)   # the conversation row above the items (#236)
        assert item_refs(app) == [q, t3, t2, t1, done], "today's order: newest first within each status"
        selected = app.selected
        await drag_row(pilot, table, 3 + n, 1 + n)
        assert item_refs(app) == [q, t1, t3, t2, done], "t1 dragged to the head of its status"
        assert app.selected == selected, "the click after the drag selects nothing"
        await drag_row(pilot, table, 2 + n, 0 + n)
        assert item_refs(app) == [t3, q, t1, t2, done], "t3 above the question: still within the band"
        assert "status *" in [str(c.label) for c in table.ordered_columns], "the sort stays"
        store.update_item(sid, t3, status="blocked")
        app.refresh_data()
        await pilot.pause()
        assert item_refs(app) == [q, t3, t1, t2, done], "its status changed: in by status, after the question"
        store.update_item(sid, t3, status="running")
        app.refresh_data()
        await pilot.pause()
        assert item_refs(app) == [q, t3, t1, t2, done], "back to running: at the head of running, not above q"
        await drag_row(pilot, table, 2 + n, 4 + n)   # t1 onto d
        assert item_refs(app) == [q, t3, t2, done, t1], "t1 into the settled band: across"
        assert [str(c.label) for c in table.ordered_columns] == ["", "session", "ref", "status", "title"], "no marker"
        assert store.setting("layout.sort.items") == ""
        store.update_item(sid, t2, status="todo")
        app.refresh_data()
        await pilot.pause()
        assert item_refs(app) == [q, t3, t2, done, t1], "ordering off: a status change moves nothing"


@pytest.mark.anyio
async def test_drag_esc_cancels_and_alt_keys_move(store, sid):
    t1, t2, t3 = (store.post_item(sid, "task", f"t{n}", status="running") for n in (1, 2, 3))
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        table = app.items_table
        n = first_item(app)   # the conversation row above the items (#236)
        await drag_row(pilot, table, 0 + n, 2 + n, release=False)
        assert table.drop_line == (2 + n, False), "the line under the row it would take the place of"
        await pilot.press("escape")
        await pilot.pause()
        assert table.dragging is None and item_refs(app) == [t3, t2, t1], "Esc: nothing moved"
        assert store.item(sid, t3)["pos"] is None
        table.focus()
        table.move_cursor(row=first_item(app))
        await pilot.press("alt+down")
        await pilot.pause()
        assert item_refs(app) == [t2, t3, t1], "Alt+Down: one place down"
        await pilot.press("alt+up")
        await pilot.pause()
        assert item_refs(app) == [t3, t2, t1], "Alt+Up: back"


@pytest.mark.anyio
async def test_header_clicks_cycle_the_markers(store, sid):
    store.post_item(sid, "task", "b", status="running")
    store.post_item(sid, "task", "a", status="todo")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        table = app.items_table
        seen = []
        for column in ("status", "status", "status", "title", "title", "title"):
            await pilot.click(table, offset=(header_x(table, column), 0))
            await pilot.pause()
            seen.append([str(c.label) for c in table.ordered_columns if " " in str(c.label)])
        assert seen == [["status ▲"], ["status ▼"], ["status *"], ["title ▲"], ["title ▼"], ["status *"]]
        await pilot.click(table, offset=(header_x(table, "title"), 0))
        await pilot.pause()
        assert [str(table.get_row(k)[4]) for k in table.keys()] == ["a", "b", "demo"], \
            "title ▲, the conversation under the items"
        assert store.setting("layout.sort.items") == "title▲", "kept for the next start"
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert "title ▲" in [str(c.label) for c in app.items_table.ordered_columns], "remembered"


@pytest.mark.parametrize("spec, column, room, want, desc", [
    ("session▲", 1, None, "session ▲", "the column in force: its marker"),
    ("session▼", 1, 9, "session ▼", "room enough: as it is"),
    ("session▲", 1, 6, "ses… ▲", "a narrow pane cuts the name, not the marker"),
    ("dot*", 1, 6, "session", "no marker: the cell cuts it, as any name"),
    ("dot*", 0, None, "*", "the dot's header is its marker alone"),
    ("ctx▼", 2, None, "ctx ▼", "a size, most first"),
    ("ctx▼", 3, None, "?", "another column: no marker"),
])
def test_session_label(spec, column, room, want, desc):
    from types import SimpleNamespace
    assert WheelhouseApp.session_label(SimpleNamespace(session_sort=spec), column, room) == want, desc


@pytest.mark.anyio
async def test_session_headers_sort_and_drag_turns_it_off(store, sid, tmp_path):
    other = store.create_session(str(tmp_path), name="later")
    store.post_item(other, "task", "fresh", status="running")   # the latest activity
    store.db.execute("UPDATE items SET created_at = '2999-01-01T00:00:00+00:00' WHERE session_id = ?", (other,))
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        table = app.session_list
        assert str(table.ordered_columns[0].label) == "*", "the list starts sorted, its * showing (Q102)"
        assert table.keys()[:2] == [other, sid], "*: the latest activity first"
        await pilot.click(table, offset=(1, 0))
        await pilot.pause()
        assert str(table.ordered_columns[0].label) == "*", "the dot's * stays on: no other column took over"
        names = {k: app.display_name(store.session(k)) for k in (sid, other)}
        by_name = sorted(names, key=lambda k: names[k].casefold())
        seen = []
        widths = []
        for column in (1, 1, 1, 2, 0):   # session ▲, ▼, back to *; ctx ▼ (most first); the dot's * again
            await pilot.click(table, offset=(header_x(table, column), 0))
            await pilot.pause()
            seen.append([str(c.label) for c in table.ordered_columns[:6]])
            widths.append(table.ordered_columns[2].content_width)
            if seen[-1][1] == "session ▲":
                assert table.keys()[:2] == by_name, "session ▲: by name"
            if seen[-1][1] == "session ▼":
                assert table.keys()[:2] == by_name[::-1], "session ▼: reversed"
        plain = ["", "session", "ctx", "?", "D", "✉"]
        assert seen == [["", "session ▲", *plain[2:]], ["", "session ▼", *plain[2:]], ["*", *plain[1:]],
                        ["", "session", "ctx ▼", *plain[3:]], ["*", *plain[1:]]], "only the column in force is marked"
        assert widths[3:] == [5, 3], "ctx widens for its marker, and narrows again when its sort goes off"
        await pilot.click(table, offset=(header_x(table, 1), 0))
        await pilot.pause()
        assert store.setting("layout.sort.sessions") == "session▲", "kept for the next start"
        await pilot.click(table, offset=(1, 0))
        await pilot.pause()
        assert str(table.ordered_columns[0].label) == "*", "and on again"
        await drag_row(pilot, table, 1, 0)
        assert table.keys()[:2] == [sid, other], "dragged within its group (neither running)"
        assert str(table.ordered_columns[0].label) == "*", "the sort stays"
        parked = store.create_session(str(tmp_path), name="parked")
        store.db.execute("UPDATE sessions SET parked = 1 WHERE id = ?", (parked,))
        app.refresh_data()
        await pilot.pause()
        assert table.keys()[-1] == parked, "parked at the foot"
        await drag_row(pilot, table, 0, table.row_count - 1)
        assert str(table.ordered_columns[0].label) == "", "across groups: ordering off"
        assert [k for k in table.final() if k] == [other, parked, sid], "as dropped: the parked one no longer sinks"
        assert store.setting("layout.sort.sessions") == ""


@pytest.mark.anyio
@pytest.mark.both
async def test_drag_past_the_selected_row_shows_at_once(store, sid):
    """T114 review: B dragged above A, the selected row in its group, shows above it at once,
    not only once the selection moves (ranked_keyed's pin)."""
    a, b = (store.post_item(sid, "task", n, status="running") for n in ("a", "b"))
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        table = app.items_table
        n = first_item(app)   # the conversation row above the items (#236)
        assert item_refs(app) == [b, a]
        table.move_cursor(row=first_item(app))
        await pilot.pause()
        assert app.selected == (sid, b)
        await drag_row(pilot, table, 1 + n, 0 + n)
        assert item_refs(app) == [a, b], "the selected row moved down past at once"
        assert [store.item(sid, r)["pos"] for r in (a, b)] == [0, 1]


@pytest.mark.anyio
async def test_a_drag_not_written_changes_nothing(store, sid):
    """T114 review: a drag across groups that the daemon refuses (older code, or unreachable)
    leaves the sort on, its marker showing and kept, and every row where it was."""
    q = store.post_item(sid, "question", "which?")
    t = store.post_item(sid, "task", "t", status="running")
    app = WheelhouseApp(store)
    said = []
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        n = first_item(app)   # the conversation row above the items (#236)
        app.client.reorder_items = lambda group, keys: Done(False).say("too old", "error")
        app.told = lambda done: said.extend(n.text for n in done.notices)
        await drag_row(pilot, app.items_table, 1 + n, 0 + n)
        assert said == ["too old"]
        assert "status *" in [str(c.label) for c in app.items_table.ordered_columns]
        assert app.item_sort == "status*" and store.setting("layout.sort.items") is None
        assert item_refs(app) == [q, t]


@pytest.mark.anyio
async def test_the_selected_session_holds_its_place_under_the_sort(store, sid, tmp_path):
    """D113: with * on, a session with new activity rises, but the selected one holds its
    place until the selection leaves it, then settles into its sorted place."""
    busy, held = (store.create_session(str(tmp_path), name=n) for n in ("busy", "held"))
    stamp = lambda s, at: store.db.execute("UPDATE sessions SET created_at = ? WHERE id = ?", (at, s))
    for s, at in ((sid, "2026-01-03"), (busy, "2026-01-02"), (held, "2026-01-01")):
        stamp(s, f"{at}T00:00:00+00:00")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        table = app.session_list
        real = lambda: [k for k in table.final() if k]
        assert real() == [sid, busy, held], "*: the latest activity first"
        async def click(row):
            await pilot.click(table, offset=(3, table.header_height + row))
            await pilot.pause()
            if isinstance(app.screen, Confirm):   # the offer to relaunch a dead session
                await pilot.press("alt+n")
                await pilot.pause()

        await click(2)
        assert app.current_session() == held
        stamp(held, "2026-02-01T00:00:00+00:00")
        stamp(busy, "2026-01-05T00:00:00+00:00")
        app.refresh_data()
        await pilot.pause()
        assert real() == [busy, sid, held], "the unselected one moved; the selected one held"
        assert app.current_session() == held, "and the cursor stays on it"
        await click(0)   # the selection leaves it, for busy
        assert app.current_session() == busy
        app.refresh_data()
        await pilot.pause()
        assert real() == [held, busy, sid], "settled into its sorted place"


@pytest.mark.anyio
@pytest.mark.parametrize("spec", [f"{col}{mark}" for col in ordering.SESSION_COLUMNS[1:] for mark in (ordering.UP, ordering.DOWN)])
async def test_a_session_selected_under_any_column_sort_holds_its_place(store, sid, tmp_path, spec):
    """#102: the pin read each sort's value as the dot's (parked, idle), so under any other
    column a click on the list raised ValueError. The selected one holds under every column."""
    other = store.create_session(str(tmp_path), name="other")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.set_sort("sessions", spec)
        app.refresh_data()
        await pilot.pause()
        table = app.session_list
        await pilot.click(table, offset=(3, table.header_height))
        await pilot.pause()
        if isinstance(app.screen, Confirm):   # the offer to relaunch a dead session
            await pilot.press("alt+n")
            await pilot.pause()
        picked = app.current_session()
        assert picked in (sid, other), spec
        app.refresh_data()
        await pilot.pause()
        assert app.current_session() == picked, f"{spec}: the selected session is kept"


@pytest.mark.anyio
async def test_a_drop_beside_the_pinned_session_goes_by_what_shows(store, sid, tmp_path):
    """T114 review 2: s1 selected has new activity, held where it was by its pin. s2 dragged
    onto it swaps the two as shown: the sort stays, nothing jumps."""
    s1, s2 = (store.create_session(str(tmp_path), name=n) for n in ("s1", "s2"))
    stamp = lambda s, at: store.db.execute("UPDATE sessions SET created_at = ? WHERE id = ?", (at, s))
    for s, at in ((sid, "2026-01-03"), (s1, "2026-01-02"), (s2, "2026-01-01")):
        stamp(s, f"{at}T00:00:00+00:00")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        table = app.session_list
        real = lambda: [k for k in table.final() if k]
        await pilot.click(table, offset=(3, table.header_height + 1))
        await pilot.pause()
        if isinstance(app.screen, Confirm):   # the offer to relaunch a dead session
            await pilot.press("alt+n")
            await pilot.pause()
        assert app.current_session() == s1
        stamp(s1, "2026-02-01T00:00:00+00:00")
        app.refresh_data()
        await pilot.pause()
        assert real() == [sid, s1, s2], "s1 held by its pin"
        await drag_row(pilot, table, 2, 1)
        assert real() == [sid, s2, s1] and str(table.ordered_columns[0].label) == "*", "a swap, as it showed"


@pytest.mark.anyio
async def test_a_drop_beside_the_pinned_item_goes_by_what_shows(store, sid):
    """T114 review 2: the selected task blocked, held among the running by its pin; a task
    dragged onto it swaps the two as shown, and Status * stays."""
    a, b, c = (store.post_item(sid, "task", n, status="running") for n in ("a", "b", "c"))
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        table = app.items_table
        n = first_item(app)   # the conversation row above the items (#236)
        assert item_refs(app) == [c, b, a]
        table.move_cursor(row=1 + n)
        await pilot.pause()
        assert app.selected == (sid, b)
        store.update_item(sid, b, status="blocked")
        app.refresh_data()
        await pilot.pause()
        assert item_refs(app) == [c, b, a], "b held by its pin"
        await drag_row(pilot, table, 2 + n, 1 + n)
        assert item_refs(app) == [c, a, b], "a swap, as it showed"
        assert "status *" in [str(col.label) for col in table.ordered_columns], "the sort stays"


@pytest.mark.parametrize("draft, delivered_at, head, desc", [
    (1, None, "**you · queued** · 10:00", "queued"),
    (0, None, "**you · sending** · 10:00", "sent, the session not yet given it"),
    (0, "10:01", "**you** · 10:00", "the session has it"),
])
def test_an_answer_in_its_thread_is_marked_until_the_session_has_it(draft, delivered_at, head, desc):
    item = {"ref": "Q1", "title": "which db?", "kind": "question", "status": "open", "updated_at": "10:00", "body": "?"}
    msg = {"author": "person", "draft": draft, "delivered_at": delivered_at, "created_at": "10:00", "body": "SQLite"}
    assert tui.thread_blocks((item, [msg]))[-1] == ("you", f"{head}\n\nSQLite"), desc


# swapping panes by their titles (D116, .plan/modules.md "Swapping panes")

DEFAULT_LAYOUT = {"left.top": "sessions", "left.middle": "stats", "left.bottom": "session-info",
                  "middle.top": "items", "right": "detail"}
DEFAULT_LAYOUT_SLOT = {p: s.replace(".", "-") for s, p in DEFAULT_LAYOUT.items()}
CORE_DEFAULTS = {pane: home for pane, (_, home) in tui.CORE_PANES.items()}
SHIPPED_DEFAULTS = {**CORE_DEFAULTS, "design": "middle.bottom", "prs": "middle.middle", "stats": "left.middle"}
SIX_KEPT = {"left.top": "sessions", "left.middle": "stats", "left.bottom": "session-info", "middle.top": "items",
            "middle.bottom": "design", "right": "detail"}   # a layout kept before the seventh slot


@pytest.mark.parametrize("saved, defaults, want, desc", [
    ({}, {**CORE_DEFAULTS, "stats": "left.middle"}, {**CORE_DEFAULTS, "stats": "left.middle"}, "nothing kept: the defaults"),
    ({"left.top": "items", "middle.top": "sessions"}, CORE_DEFAULTS,
     {**CORE_DEFAULTS, "items": "left.top", "sessions": "middle.top"}, "a kept swap; the panes it doesn't name stay home"),
    ({"left.top": "stats", "left.middle": "sessions"}, CORE_DEFAULTS, {**CORE_DEFAULTS, "sessions": "left.middle"},
     "a slot kept for an absent module stays empty"),
    ({"left.top": "items"}, CORE_DEFAULTS, {**CORE_DEFAULTS, "items": "left.top", "sessions": "left.middle"},
     "a pane the layout doesn't name, its default taken: the first empty slot"),
    ({"left.top": "items", "left.middle": "gone", "middle.bottom": "gone2"}, CORE_DEFAULTS,
     {**CORE_DEFAULTS, "items": "left.top", "sessions": "middle.top"}, "a slot kept for an absent pane is passed over"),
    ({"left.top": "items", "left.middle": "x", "middle.top": "y", "middle.middle": "w", "middle.bottom": "z"}, CORE_DEFAULTS,
     {**CORE_DEFAULTS, "items": "left.top", "sessions": "left.middle"}, "and taken once no other is empty"),
    ({"left.top": "items", "middle.top": "items", "nowhere": "sessions"}, CORE_DEFAULTS,
     {**CORE_DEFAULTS, "items": "left.top", "sessions": "left.middle"}, "a pane named twice, or a slot not one of seven: once, ignored"),
    ({}, {**CORE_DEFAULTS, "a": "left.middle", "b": "left.middle"}, {**CORE_DEFAULTS, "a": "left.middle", "b": "middle.middle"},
     "two panes for one slot: the second to the first empty one"),
    # upgrading to seven slots (#111): a layout kept with six loads as it was, PRs in the new one
    (SIX_KEPT, SHIPPED_DEFAULTS, {**SHIPPED_DEFAULTS}, "six kept as they were: PRs takes middle.middle"),
    ({**SIX_KEPT, "left.middle": "design", "middle.bottom": "stats"}, SHIPPED_DEFAULTS,
     {**SHIPPED_DEFAULTS, "design": "left.middle", "stats": "middle.bottom"}, "a kept swap stays; PRs still takes its own"),
    ({**SIX_KEPT, "middle.middle": "design", "middle.bottom": "stats", "left.middle": "prs"}, SHIPPED_DEFAULTS,
     {**SHIPPED_DEFAULTS, "design": "middle.middle", "stats": "middle.bottom", "prs": "left.middle"},
     "swaps across the new slot are kept"),
    (SIX_KEPT, {k: v for k, v in SHIPPED_DEFAULTS.items() if k != "prs"},
     {k: v for k, v in SHIPPED_DEFAULTS.items() if k != "prs"}, "without the PRs module: as before, middle.middle empty"),
])
def test_arrange(saved, defaults, want, desc):
    assert tui.arrange(saved, defaults) == want, desc


@pytest.mark.parametrize("value, want, desc", [
    (None, {}, "none kept"),
    ('{"left.top": "items"}', {"left.top": "items"}, "kept"),
    ("not json", {}, "unreadable: none"),
    ('["left.top"]', {}, "not a map: none"),
    ('{"left.top": 3}', {}, "a pane that isn't a name: left out"),
])
def test_kept_slots(value, want, desc):
    assert tui.kept_slots(value) == want, desc


def pane_widget(app, pane: str):
    return app.query_one("#" + tui.CORE_PANES[pane][0] if pane in tui.CORE_PANES else "#" + pane)


async def drag_title(pilot, pane: str, to: tuple[int, int] | str | None, release: bool = True) -> None:
    """A pane's title pressed, moved with the button held to `to` (another pane, its middle,
    or a screen point), and let go there."""
    widget = pane_widget(pilot.app, pane)
    await pilot.mouse_down(widget, offset=(3, 0))
    if to is None:   # a plain click
        await pilot.mouse_up(widget, offset=(3, 0))
        await pilot.pause()
        return
    if isinstance(to, str):
        region = pane_widget(pilot.app, to).region
        to = (region.x + region.width // 2, region.y + region.height // 2)
    x, y = to
    pilot.app.post_message(events.MouseMove(None, x, y, 1, 1, button=1, shift=False, meta=False,
                                            ctrl=False, screen_x=x, screen_y=y))
    await pilot.pause()
    if release:
        await pilot.mouse_up(None, offset=(x, y))
        await pilot.pause()


class Relaid(WheelhouseApp):
    """The app, counting LayoutChanged, as the tour's callout listens for it."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.relaid_count = 0

    def on_wheelhouse_app_layout_changed(self) -> None:
        self.relaid_count += 1


@pytest.mark.anyio
@pytest.mark.parametrize("pane, onto, desc", [
    ("sessions", "items", "the session list and the inbox"),
    ("session-info", "detail", "session info into the wide right column, Detail into column 1"),
    ("stats", "items", "a module's pane and a core one"),
    ("detail", "sessions", "dragged the other way"),
])
async def test_a_title_drag_swaps_and_survives_a_restart(store, sid, pane, onto, desc):
    want = {s: {pane: onto, onto: pane}.get(p, p) for s, p in DEFAULT_LAYOUT.items()}
    app = Relaid(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        moved, other = pane_widget(app, pane), pane_widget(app, onto)
        await drag_title(pilot, pane, onto, release=False)
        assert app.query_one("#slot-" + DEFAULT_LAYOUT_SLOT[onto]).has_class("-swap-target"), f"{desc}: the target lit"
        await pilot.mouse_up(None, offset=(other.region.x + 1, other.region.y + 1))
        await pilot.pause()
        assert app.layout_now() == want, desc
        assert pane_widget(app, pane) is moved and pane_widget(app, onto) is other, f"{desc}: the same widgets, state and all"
        assert not app.query(".-swap-target"), f"{desc}: the light goes out"
        assert app.relaid_count == 1, f"{desc}: LayoutChanged posted, once"
        assert json.loads(store.setting("layout.slots")) == want, f"{desc}: kept"
        stops = [w.id for w, _ in tui.tab_stops(app.screen)]
        assert [s for s in stops if s in ("session-list", "items", "answer")] == ["session-list", "items", "answer"], \
            f"{desc}: Tab goes by pane, not by place"
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert app.layout_now() == want, f"{desc}: as it was, after a restart"



@pytest.mark.anyio
@pytest.mark.parametrize("how, desc", [
    ("escape", "Esc during the drag"),
    ("own", "a release on its own slot"),
    ("outside", "a release outside the grid, on the send bar"),
    ("click", "a plain click on a title"),
])
async def test_a_title_drag_that_swaps_nothing(store, sid, how, desc):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        items = pane_widget(app, "items").region
        if how == "escape":
            await drag_title(pilot, "sessions", "items", release=False)
            await pilot.press("escape")
            await pilot.pause()
            await pilot.mouse_up(None, offset=(items.x + 2, items.y + 2))
        elif how == "own":
            await drag_title(pilot, "sessions", "sessions")
        elif how == "outside":
            bar = app.query_one(SendBar).region
            await drag_title(pilot, "sessions", (bar.x + 2, bar.y))
        else:
            await drag_title(pilot, "sessions", None)
        await pilot.pause()
        assert app.layout_now() == DEFAULT_LAYOUT, desc
        assert store.setting("layout.slots") is None, desc
        assert not app.query(".-swap-target") and not any(s.dragging for s in app.query(tui.Slot)), f"{desc}: the drag over"
        assert not app.screen.selections, f"{desc}: and no text selected by it"


@pytest.mark.anyio
async def test_reset_layout(store, sid):
    app = Relaid(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        await drag_title(pilot, "session-info", "items")
        await drag(pilot, "items-pane", 10, 0)
        assert app.layout_now()["middle.top"] == "session-info"
        assert app.query_one("#col-middle").styles.min_width.value == 39, "the column grows to the buttons' 39"
        assert app.query_one("#slot-middle-top").styles.min_height.value == 12, "and the slot to their 12 rows"
        assert store.setting("layout.items-pane") is not None
        commands = [c.title for c in app.get_system_commands(app.screen)]
        assert "Reset layout" in commands, "in the command palette"
        app.action_reset_layout()
        await pilot.pause()
        assert app.layout_now() == DEFAULT_LAYOUT, "every pane home"
        assert store.setting("layout.slots") is None and store.setting("layout.items-pane") is None, "nothing kept"
        assert app.query_one("#col-left").styles.min_width.value == 39, "the buttons' minimum home with them"
        assert app.query_one("#col-middle").styles.min_width.value == 12
        assert sized(app, "items-pane") == 45, "the splitters at the stylesheet's sizes"
        assert app.relaid_count == 2, "LayoutChanged for the swap and for the reset"


@pytest.mark.anyio
async def test_a_missing_modules_slot_stays_empty_until_it_returns(store, sid, monkeypatch):
    from claude_wheelhouse import api
    loaded = api.load()
    store.set_setting("layout.slots", json.dumps({"left.top": "stats", "left.middle": "sessions"}))
    monkeypatch.setattr(api, "load", lambda: [])
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert app.layout_now() == {"left.middle": "sessions", "left.bottom": "session-info",
                                    "middle.top": "items", "right": "detail"}, "stats gone: its slot empty"
        assert not app.query_one("#slot-left-top").display, "and collapsed"
        await drag_title(pilot, "items", "detail")
        assert json.loads(store.setting("layout.slots"))["left.top"] == "stats", "a swap keeps the absent pane's place"
    monkeypatch.setattr(api, "load", lambda: loaded)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert app.layout_now()["left.top"] == "stats", "back where it was"


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(160, 40), (80, 24)])
@pytest.mark.parametrize("saved, modules, desc", [
    ({"left.middle": "detail", "right": "stats"}, False, "Detail swapped with Stats, then Stats uninstalled"),
    ({"left.top": "detail", "right": "detail"}, True, "a corrupt layout naming Detail twice"),
])
async def test_an_empty_right_column_leaves_no_gap(store, sid, monkeypatch, size, saved, modules, desc):
    """Review of #95: with the right column collapsed, the last column shown takes the rest of
    the width, and the splitter between the two left stays usable."""
    from claude_wheelhouse import api
    if not modules:
        monkeypatch.setattr(api, "load", lambda: [])
    store.set_setting("layout.slots", json.dumps(saved))
    app = WheelhouseApp(store)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        await pilot.pause()
        assert not app.query_one("#col-right").display, f"{desc}: the right column collapsed"
        main = app.query_one("#main")
        shown = [w for w in main.children if w.display]
        assert sum(w.outer_size.width for w in shown) == main.size.width, f"{desc}: no gap at {size}"
        assert [w.id for w in shown if not isinstance(w, Splitter)] == ["col-left", "col-middle"], desc
        assert [s.key for s in shown if isinstance(s, Splitter)] == ["sessions-pane"], f"{desc}: one splitter, between them"
        before = app.query_one("#col-left").size.width
        await drag(pilot, "sessions-pane", -3 if size[0] == 160 else 3, 0)
        assert app.query_one("#col-left").size.width != before, f"{desc}: and it still resizes"
        for pane in ("sessions", "session-info", "items", "detail"):
            region = pane_widget(app, pane).region
            assert region.right <= size[0] and region.width > 2, f"{desc}: {pane} on screen at {size}"


SWAPPABLE = ["sessions", "stats", "session-info", "items", "detail"]


@pytest.mark.anyio
@pytest.mark.parametrize("pane, onto", list(itertools.combinations(SWAPPABLE, 2)))
async def test_every_swap_fits_80_by_24(store, sid, pane, onto):
    """Review of #95: each core pane keeps its own minimum wherever it goes, so no swap puts
    one in a slot smaller than it, or cuts it to its border. The stats pane, a module's,
    gives way to a row on a short terminal, as it does in its own slot."""
    app = WheelhouseApp(store)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.swap(pane_widget(app, pane).parent, pane_widget(app, onto).parent)
        await pilot.pause()
        await pilot.pause()
        for each in SWAPPABLE:
            widget = pane_widget(app, each)
            assert widget.parent.region.contains_region(widget.region), f"{pane}<->{onto}: {each} within its slot"
            assert widget.region.right <= 80 and widget.region.bottom <= 24, f"{pane}<->{onto}: {each} on screen"
            if each != "stats":
                assert widget.region.height >= 3, f"{pane}<->{onto}: {each} more than its border"


@pytest.mark.anyio
async def test_reset_puts_module_panes_in_their_declared_slots(store, sid, monkeypatch):
    """Review of #95: a module pane's default is the slot it declares (api.Pane.slot), not
    the one it's in, so Reset layout brings two swapped module panes home."""
    from textual.widget import Widget
    from claude_wheelhouse import api
    loaded = [api.Loaded(m, api.Module(m, m.title(), "1.0", api.WHEELHOUSE_API,
                                       [api.Pane(m.title(), home, {"tui": lambda ctx: Widget()})]))
              for m, home in (("one", "left.middle"), ("two", "middle.bottom"))]
    monkeypatch.setattr(api, "load", lambda: loaded)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.swap(app.slots()["left.middle"], app.slots()["middle.bottom"])
        await pilot.pause()
        assert (app.layout_now()["left.middle"], app.layout_now()["middle.bottom"]) == ("two", "one"), "swapped"
        app.action_reset_layout()
        await pilot.pause()
        assert (app.layout_now()["left.middle"], app.layout_now()["middle.bottom"]) == ("one", "two"), "home again"


class Presses:
    """The app's busy-button bookkeeping (#107) on a session row of the test's own, with no
    app: WheelhouseApp's own methods, borrowed, over plain attributes."""
    follow_presses = WheelhouseApp.follow_presses
    under_way = WheelhouseApp.under_way
    busy_buttons = WheelhouseApp.busy_buttons
    compacting = WheelhouseApp.compacting
    press = WheelhouseApp.press
    running = lambda self, sid: self.statuses.get(sid) in ("live", "stalled", "starting")   # as Hub.running
    heard = WheelhouseApp.heard
    display_name = staticmethod(lambda s: s["name"])

    def __init__(self, row: dict, status: str):
        self.pressed, self.toasts, self.question, self.forking, self.fork_from = {}, [], True, {}, {}
        self.sessions, self.statuses, self.relaunching = [row], {row["id"]: status}, set()
        self.client = SimpleNamespace(compact_question=lambda target: {} if self.question else None)

    def notify(self, text, severity="information"):
        self.toasts.append(text)

    def paint_session_info(self):
        pass

    def change(self, step: dict) -> None:
        """A step of the store's, or the hub's: columns, and _status, _relaunching, _gone,
        _closed (the question suggesting Compact) or _notice (an announce about the session)."""
        row = self.sessions[0] if self.sessions else None
        for key, value in step.items():
            if key == "_status":
                self.statuses[row["id"]] = value
            elif key == "_relaunching":
                self.relaunching = {row["id"]} if value else set()
            elif key == "_gone":
                self.sessions = []
            elif key == "_closed":
                self.question = False
            elif key == "_notice":
                self.heard(tui.Event("notice", "forked demo as demo (fork)", "information", row["id"]))
            else:
                row[key] = value
        self.follow_presses()


BUSY_ROW = {"id": "s1", "name": "demo", "host_command": None, "activity": "idle", "model": "opus",
            "applied_model": "opus", "park_requested_at": None, "end_requested_at": None, "interrupted_at": None,
            "shell": None, "compaction": None}


@pytest.mark.parametrize("button, status, starts, lands, desc", [
    ("interrupt", "live", {"host_command": "interrupt"}, {"host_command": None, "interrupted_at": "9999"}, "Interrupt: until the host stamps it"),
    ("interrupt", "live", {}, {"interrupted_at": "9999"}, "Interrupt taken before a refresh saw it: its stamp lands it"),
    ("compact", "live", {"host_command": "compact"}, {"host_command": None, "activity": "idle · compacted 300k → 40k tokens"}, "Compact: until compacted"),
    ("compact", "live", {"host_command": "compact"}, {"_status": "dead"}, "Compact: the host dying ends it"),
    ("handover", "live", {"host_command": "shell"}, {"host_command": None, "activity": "", "shell": "tab"}, "Handover: until handed over"),
    ("model", "live", {"model": "sonnet"}, {"applied_model": "sonnet"}, "Model: until applied"),
    ("park", "live", {"park_requested_at": "x"}, {"park_requested_at": None, "_status": "dead"}, "Park: until parked"),
    ("end", "live", {"end_requested_at": "x"}, {"_gone": True}, "Forget, let it know: until it's gone"),
    ("relaunch", "live", {"_relaunching": True}, {"_relaunching": False}, "Relaunch a running one: until it's back"),
    ("relaunch", "dead", {"_status": "starting"}, {"_status": "live"}, "Relaunch or Unpark a dead one: until it runs"),
    ("fork", "live", {}, {"_notice": True}, "Fork: until the hub's word of it, with the parent's id"),
    ("compact-now", "live", {"host_command": "compact"}, {"host_command": None, "compaction": "cancelled", "_closed": True}, "the question's Compact: until it closes"),
])
def test_a_pressed_button_stays_off_until_its_action_lands(monkeypatch, button, status, starts, lands, desc):
    now = [100.0]
    monkeypatch.setattr(tui.Press, "now", staticmethod(lambda: now[0]))
    for phase in ("lands", "fallback"):
        app = Presses(dict(BUSY_ROW), status)
        app.press("s1", button, "Q1" if button == "compact-now" else None)
        app.forking = {"s1": [now[0]]} if button == "fork" else {}
        app.change(starts)
        now[0] += 1
        app.change({})
        assert button in app.busy_buttons(app.sessions[0]), f"{desc}: off once pressed"
        if phase == "lands":
            app.change(lands)
            assert not app.sessions or button not in app.busy_buttons(app.sessions[0]), f"{desc}: on again once it lands"
            assert not [x for x in app.toasts if x.startswith("no word")] and not [k for k in app.pressed if k[1] == button], \
                f"{desc}: no word of a fallback, nothing kept"
        else:
            now[0] += tui.BUSY_FOR - 1.5
            app.change({})
            assert button in app.busy_buttons(app.sessions[0]), f"{desc}: off still, just short of BUSY_FOR"
            now[0] += 1
            app.change({})
            assert button not in app.busy_buttons(app.sessions[0]), f"{desc}: on again after BUSY_FOR"
            assert app.toasts == [f"no word from demo after {tui.BUSY_FOR} s"], f"{desc}: said once"
            now[0] += tui.BUSY_FOR
            app.change({})
            assert button not in app.busy_buttons(app.sessions[0]) and len(app.toasts) == 1, \
                f"{desc}: still on, for a press again (Park and Forget's: Cancel or Force)"


@pytest.mark.parametrize("ago, busy, desc", [
    (5, {"park"}, "asked 5 s ago: off for the rest of BUSY_FOR"),
    (3600, set(), "asked an hour ago: on at once, counted from the store's stamp, not this app's start"),
])
def test_a_request_followed_after_a_restart_counts_from_when_it_was_asked(monkeypatch, ago, busy, desc):
    monkeypatch.setattr(tui.Press, "now", staticmethod(lambda: 1000.0))
    at = (datetime.now(timezone.utc) - timedelta(seconds=ago)).isoformat(timespec="microseconds")
    app = Presses(dict(BUSY_ROW, park_requested_at=at), "live")
    app.follow_presses()
    assert app.busy_buttons(app.sessions[0]) == busy and not app.toasts, desc


def test_a_fork_never_heard_of_is_let_go_and_the_next_clears_on_its_own_word(monkeypatch):
    """A fork that ends unsaid (a daemon stopping mid-fork, a notice lost on reconnecting) is
    let go at BUSY_FOR, so the next Fork's own word brings the button back on (#107)."""
    now = [0.0]
    monkeypatch.setattr(tui.Press, "now", staticmethod(lambda: now[0]))
    app = Presses(dict(BUSY_ROW), "live")
    app.press("s1", "fork")
    app.forking = {"s1": [now[0]]}
    now[0] = tui.BUSY_FOR
    app.change({})
    assert app.forking == {} and "fork" not in app.busy_buttons(app.sessions[0]), "unsaid: let go at BUSY_FOR"
    now[0] += 1
    app.press("s1", "fork")
    app.forking = {"s1": [now[0]]}   # as fork() records it
    app.change({})
    assert "fork" in app.busy_buttons(app.sessions[0]), "the next: off"
    app.change({"_notice": True})
    assert "fork" not in app.busy_buttons(app.sessions[0]) and app.forking == {}, "the next's own word: on again"


def test_an_action_under_way_not_pressed_here_is_followed_silently(monkeypatch):
    """A Compact under way before this app started, or by another: off as if pressed, back on
    after BUSY_FOR without a word, as no one here pressed it."""
    now = [0.0]
    monkeypatch.setattr(tui.Press, "now", staticmethod(lambda: now[0]))
    app = Presses(dict(BUSY_ROW, host_command="compact"), "live")
    app.follow_presses()
    assert app.busy_buttons(app.sessions[0]) == {"compact", "compact-now"}, "off, Compact's with the question's"
    now[0] = tui.BUSY_FOR
    app.follow_presses()
    assert app.busy_buttons(app.sessions[0]) == set() and not app.toasts, "on again, unsaid"


@pytest.mark.anyio
async def test_a_disabled_button_is_dark_grey_with_black_text(store, sid, clock):
    """Every button off, whatever the reason (#107): a dialog's full-height one, its borders
    the same grey, and the one-row (compact) ones of the bar, the question suggesting Compact
    (the success variant) and the session pane's grid. Each label drawn in black on OFF, at
    full strength: not faded into what's behind, as Textual's own disabled look is. clock: no
    refresh puts the question's Compact back on meanwhile."""
    app = WheelhouseApp(store)

    def drawn(b) -> list[str]:
        row = app.screen._compositor.render_strips()[b.content_region.y]
        return [str(seg.style) for seg in row if seg.text.strip() and seg.text.strip() in str(b.label)]

    async with app.run_test(size=(160, 40)) as pilot:
        app.push_screen(tui.Confirm("Go?"))
        await pilot.pause()
        dialog = app.screen.query_one("#yes", Button)
        dialog.disabled = True
        await pilot.pause()
        off = Color.parse(tui.OFF)
        assert dialog.styles.border_top[1].rich_color == off == dialog.styles.border_bottom[1].rich_color, \
            "a dialog's button: its tall borders the same grey, its height kept"
        assert drawn(dialog) == [f"bold #000000 on {tui.OFF}"], f"the dialog's: {drawn(dialog)}"
        await pilot.press("escape")
        await pilot.pause()
        compact_now = app.screen.query_one("#compact-now", Button)
        compact_now.disabled = True
        compact_now.parent.display = True
        await pilot.pause()
        for b in (app.screen.query_one("#send-all", Button), compact_now, app.session_buttons.query_one("#fork", Button)):
            assert b.disabled and b.region.height == 1, f"{b.id}: off, and one row still"
            assert drawn(b) == [f"bold #000000 on {tui.OFF}"], f"{b.id}: black on dark grey, got {drawn(b)}"


MARKUP = "[@click=app.bell]ZQ[/] [bold]B[/bold]"   # its mark (ZQ) inside a 22-cell column


@pytest.mark.anyio
@pytest.mark.parametrize("where, selector, desc", [
    ("title", "#items", "an item's title in the inbox"),
    ("title", "#detail", "an item's title in Detail"),
    ("body", "#detail", "an item's body in Detail"),
    ("synopsis", "#session-info-text", "a session's synopsis in its info"),
])
async def test_a_sessions_text_is_never_markup(store, sid, clock, where, selector, desc):
    """#139: a session's text shows as written, and a [@click=...] in it runs nothing."""
    ref = store.post_item(sid, "task", MARKUP if where == "title" else "t", MARKUP if where == "body" else "")
    if where == "synopsis":
        store.set_synopsis(sid, MARKUP)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        ran = []
        app.action_bell = lambda: ran.append("bell")
        app.follow(sid)
        clock.tick(app)
        await pilot.pause()
        if selector == "#detail":
            app.show_item(sid, ref)
            await pilot.pause()
        widget = app.query_one(selector)
        lines = [strip.text for strip in widget.render_lines(widget.region.reset_offset)]
        y = next((i for i, line in enumerate(lines) if "ZQ" in line), None)
        assert y is not None, f"{desc}: shown, in {lines}"
        assert "]ZQ" in lines[y], f"{desc}: its markup shows as written: {lines[y]!r}"
        await pilot.click(widget, offset=(lines[y].index("ZQ") + 1, y))
        await pilot.pause()
        assert ran == [], f"{desc}: a click on it runs nothing"


@pytest.mark.anyio
@pytest.mark.parametrize("url, opens", [
    ("https://github.com/o/r/pull/1", True),
    ("http://localhost:8000/x", True),
    ("file:///etc/passwd", False),
    ("javascript:alert(1)", False),
])
async def test_open_url_opens_web_pages_alone(store, sid, url, opens):
    """#139: whatever asks it, the app opens an http(s) page and nothing else."""
    app = WheelhouseApp(store)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        opened = []
        app._driver.open_url = lambda u, new_tab=True: opened.append(u)
        app.open_url(url)
        assert opened == ([url] if opens else []), url


def test_the_keys_overlay_describes_a_module_panes_keys():
    """#154: the PRs pane's m and r aren't in core's DESCRIBE; ? raised KeyError 'scope'."""
    from claude_wheelhouse.tui import keys_help
    from claude_wheelhouse_prs.pane import Prs
    text = keys_help(panes=[Prs])
    assert "## The PRs pane" in text and "**Alt+O**: Mine or all" in text and "**Alt+R**: Fetch now" in text, text


def undo_setup(store, sid, shape: str) -> list:
    """The rows for test_ctrl_z_undoes_the_last_inbox_act, oldest first: the first is the one highlighted."""
    with one_second():
        if shape == "family":   # an open question with an open child question and a done child task
            q = store.post_item(sid, "question", "which db?")
            kids = [store.post_item(sid, "question", "which port?"), store.post_item(sid, "task", "t", status="done")]
            for k in kids:
                store.db.execute("UPDATE items SET parent_ref = ? WHERE session_id = ? AND ref = ?", (q, sid, k))
            return [q, *kids]
        if shape == "two open":
            return [store.post_item(sid, "question", t) for t in "ab"]
        kind, status = shape.removesuffix(" pinned").split()
        extra = {"why": "local"} if kind == "decision" else {}
        ref = store.post_item(sid, kind, "it", **extra)
        if status != "open" or kind != "question":
            store.db.execute("UPDATE items SET status = ? WHERE session_id = ? AND ref = ?", (status, sid, ref))
        if shape.endswith("pinned"):
            store.set_pinned(sid, ref, True)
        return [ref]


@pytest.mark.anyio
@pytest.mark.both
@pytest.mark.parametrize("shape, keys, want, said, desc", [
    # want: each row's (status, finished, pinned) at the end; said: notices that must appear
    ("question open", ["delete", "alt+y", "ctrl+z"], [("open", False, False)],
     ["closed Q1: Ctrl+Z undoes it", "undone: closed Q1"], "an open question deleted comes back open, not answered"),
    ("question open", ["delete", "alt+n", "ctrl+z"], [("open", False, False)], ["nothing to undo"],
     "No deletes nothing, so there's nothing to undo"),
    ("question answered", ["delete", "ctrl+z", "ctrl+z"], [("answered", False, False)],
     ["undone: closed Q1", "nothing to undo"], "one step: a second Ctrl+Z has nothing left"),
    ("decision unseen", ["delete", "ctrl+z"], [("unseen", False, False)], ["undone: closed D1"],
     "a decision comes back unseen, as it was"),
    ("task done", ["delete", "ctrl+z"], [("done", False, False)], ["undone: dismissed T1"], "a dismissed task comes back"),
    ("question open", ["alt+p", "ctrl+z"], [("open", False, False)], ["undone: pinned Q1"], "a pin undone"),
    ("question open pinned", ["alt+p", "ctrl+z"], [("open", False, True)], ["undone: unpinned Q1"], "an unpin undone"),
    ("question answered pinned", ["delete", "alt+y", "ctrl+z"], [("answered", False, True)], ["undone: closed Q1"],
     "a pinned item deleted comes back pinned"),
    ("question closed", ["ctrl+r", "ctrl+z"], [("closed", True, False)], ["undone: reopened Q1"],
     "Ctrl+R undone: closed again"),
    ("family", ["delete", "alt+y", "ctrl+z"], [("open", False, False), ("open", False, False), ("done", False, False)],
     ["undone: deleted Q1 and 2 children"], "a parent and its children all come back as they were"),
    ("two open", ["ctrl+space", "down", "ctrl+space", "delete", "alt+y", "ctrl+z"], [("open", False, False)] * 2,
     ["undone: closed Q2, Q1"], "marked rows asked once, and undone together"),
])
async def test_ctrl_z_undoes_the_last_inbox_act(store, sid, shape, keys, want, said, desc):
    refs = undo_setup(store, sid, shape)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        await pilot.press("alt+v")   # finished rows in sight, so Ctrl+R has one to reopen
        app.items_table.focus()
        app.items_table.move_cursor(row=app.items_table.get_row_index(f"{sid}|{refs[-1] if shape == 'two open' else refs[0]}"))
        await pilot.pause()
        notes = []
        app.notify = lambda text, **kw: notes.append(text)
        for key in keys:
            await pilot.press(key)
            await landed(app, pilot)   # Delete's and Ctrl+Z's calls on their worker thread
    got = [(it["status"], tui.standing(it) == "finished", tui.pinned(it)) for it in map(lambda r: store.item(sid, r), refs)]
    assert got == want, desc
    assert all(s in notes for s in said), f"{desc}: {notes}"


@pytest.mark.anyio
async def test_ctrl_z_in_the_answer_box_is_its_own_undo(store, sid):
    q = store.post_item(sid, "question", "which db?", status="answered")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.items_table.focus()   # answered, so settled: under the session's conversation (#192)
        app.items_table.move_cursor(row=app.items_table.get_row_index(f"{sid}|{q}"))
        await pilot.pause()
        await pilot.press("delete")
        await pilot.pause()
        app.answer.focus()
        await pilot.press("a", "b")
        await pilot.pause()
        await pilot.press("ctrl+z")
        await pilot.pause()
        assert app.answer.text == "", "the box's own undo took the typing back"
    assert store.item(sid, q)["status"] == "closed", "and the delete stays"


def held_calls(app, fail=None):
    """The app's hub calls that Delete makes (close, delete_family) held on their worker thread
    until the returned event is set, then made, or failed with `fail`: an exception raised, or
    a Done returned (#234)."""
    import threading
    gate = threading.Event()
    for name in ("close", "delete_family"):
        real = getattr(app.client, name)

        def held(*a, real=real, **kw):
            if threading.current_thread() is threading.main_thread():   # Ctrl+R: not held (Ctrl+Z waits for the gate too)
                return real(*a, **kw)
            assert gate.wait(10), "the test never let the call through"
            if isinstance(fail, Exception):
                raise fail
            return fail if fail is not None else real(*a, **kw)
        setattr(app.client, name, held)
    return gate


async def landed(app, pilot) -> None:
    """Every Delete and Ctrl+Z under way landed (delete_off_thread, action_undo)."""
    for _ in range(500):
        if not app.deleting:
            break
        await pilot.pause(0.01)
    assert not app.deleting, "the delete never landed"
    await pilot.pause()


@pytest.mark.anyio
@pytest.mark.both
@pytest.mark.parametrize("marked, fail, gone, said, desc", [
    (False, None, True, "dismissed T1", "one row: gone at once, and stays gone once the call lands"),
    (True, None, True, "dismissed T1, T2, T3", "Ctrl+A's rows: all gone at once, together"),
    (False, KeyError("no item T1 in this session"), False, "no item T1", "the call fails: the row comes back, said"),
    (True, Done(False).say("the wheelhouse daemon can't be reached", "error"), False, "can't be reached",
     "the daemon lost: the rows come back, said"),
])
async def test_delete_takes_rows_at_once_and_the_hub_call_lands_after(store, sid, marked, fail, gone, said, desc):
    """#234, Doug: "when I request deletion, remove the visual element immediately, then delegate
    deletion to a background thread so that the UI remains responsive." A refresh while the call
    is held never brings the rows back; a failed call does, and says why."""
    refs = [store.post_item(sid, "task", f"t{i}", status="done") for i in range(3)]
    keys = [f"{sid}|{r}" for r in (refs if marked else refs[:1])]
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        notes = []
        app.notify = lambda text, **kw: notes.append(text)
        app.items_table.focus()
        app.items_table.move_cursor(row=app.items_table.get_row_index(keys[0]))
        await pilot.pause()
        gate = held_calls(app, fail)
        if marked:
            await pilot.press("ctrl+a")
        await pilot.press("delete")
        await pilot.pause()
        assert not set(keys) & set(app.items_table.rows), f"{desc}: gone before the call lands"
        assert all(store.item(sid, k.split("|")[1])["dismissed"] is None for k in keys), f"{desc}: not landed yet"
        app.refresh_data()   # a refresh racing the call
        await pilot.pause()
        assert not set(keys) & set(app.items_table.rows), f"{desc}: no flash back on a refresh meanwhile"
        gate.set()
        await landed(app, pilot)
        assert (not set(keys) & set(app.items_table.rows)) == gone, f"{desc}: {list(app.items_table.rows)}"
        assert any(said in n for n in notes), f"{desc}: {notes}"


def aged(store, sid, ref, minutes: float) -> None:
    """An item last changed `minutes` ago."""
    at = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    store.db.execute("UPDATE items SET updated_at = ? WHERE session_id = ? AND ref = ?", (at, sid, ref))


@pytest.mark.anyio
@pytest.mark.both
@pytest.mark.parametrize("kind, status, age, pin, setting, show, shown, desc", [
    ("task", "done", 6, False, 5, False, False, "a task done 6 minutes ago: hidden"),
    ("task", "done", 4, False, 5, False, True, "done 4 minutes ago: still shown"),
    ("agent", "failed", 6, False, 5, False, False, "a subagent failed a while ago: hidden"),
    ("pr", "merged", 6, False, 5, False, False, "a PR merged a while ago: hidden"),
    ("question", "answered", 6, False, 5, False, False, "a question answered a while ago: hidden"),
    ("decision", "seen", 6, False, 5, False, False, "a decision seen a while ago: hidden"),
    ("task", "todo", 60, False, 5, False, True, "a task still to do: never hidden"),
    ("task", "done", 60, True, 5, False, True, "pinned: never hidden"),
    ("task", "done", 60, False, 0, False, True, "the setting 0: off"),
    ("task", "done", 60, False, 5, True, True, "Alt+V shows the hidden ones"),
])
async def test_autocleanse_hides_completed_items_after_a_while(store, sid, kind, status, age, pin, setting, show,
                                                                 shown, desc):
    """#235, Doug: "an autocleanse option, on by default, that hides completed items after 5
    minutes". Hidden, not deleted: Alt+V shows them, as it shows the finished."""
    store.post_item(sid, "question", "anchor")   # something else in the inbox to select
    extra = {"why": "local"} if kind == "decision" else {}
    ref = (store.post_pr(sid, "https://github.com/o/r/pull/5", "it") if kind == "pr" else
           store.post_item(sid, kind, "it", **extra))
    store.db.execute("UPDATE items SET status = ? WHERE session_id = ? AND ref = ?", (status, sid, ref))
    if pin:
        store.set_pinned(sid, ref, True)
    aged(store, sid, ref, age)
    app = WheelhouseApp(store)
    app.autocleanse = setting
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        if show:
            await pilot.press("alt+v")
        app.refresh_data()
        await pilot.pause()
        assert (f"{sid}|{ref}" in app.items_table.rows) == shown, desc
    assert store.item(sid, ref)["dismissed"] is None, f"{desc}: hidden, never deleted"


@pytest.mark.anyio
@pytest.mark.both
async def test_ctrl_delete_deletes_every_completed_item_and_ctrl_z_brings_them_back(store, sid):
    """#235, Doug: "a 'delete all completed' option (Ctrl+Del?) for me to delete all completed
    inbox items without having to select them first". Hidden ones too, never an active or a
    pinned one, no dialog, through #234's fast path; a parent with a child under way is kept."""
    done = [store.post_item(sid, "task", f"d{i}", status="done") for i in range(3)]
    aged(store, sid, done[2], 30)   # hidden by autocleanse, deleted all the same
    answered = store.post_item(sid, "question", "q", status="answered")
    active = store.post_item(sid, "task", "still going", status="todo")
    pin = store.post_item(sid, "task", "kept", status="done")
    store.set_pinned(sid, pin, True)
    parent = store.post_item(sid, "task", "parent", status="done")
    child = store.post_item(sid, "question", "open child")
    store.db.execute("UPDATE items SET parent_ref = ? WHERE session_id = ? AND ref = ?", (parent, sid, child))
    gone = [*done, answered]
    app = WheelhouseApp(store)
    app.autocleanse = 5
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        notes = []
        app.notify = lambda text, **kw: notes.append(text)
        app.items_table.focus()
        await pilot.press("ctrl+delete")
        await pilot.pause()
        assert [type(s).__name__ for s in app.screen_stack] == ["Screen"], "no dialog"
        await landed(app, pilot)
        assert [store.item(sid, r)["dismissed"] or store.item(sid, r)["status"] for r in gone] == \
            ["done", "done", "done", "closed"], "every completed one deleted"
        assert all(f"{sid}|{r}" not in app.items_table.rows for r in gone), "and off the inbox"
        assert [tui.standing(store.item(sid, r)) for r in (active, pin, parent, child)] == \
            ["active", "settled", "settled", "active"], "the active, the pinned and a parent of an open child kept"
        assert "deleted 4 completed items: Ctrl+Z to undo; 1 kept: its children are still under way" in notes, notes
        await pilot.press("ctrl+z")
        await landed(app, pilot)
        assert all(tui.standing(store.item(sid, r)) == "settled" for r in gone), "Ctrl+Z brings them all back"
        assert all(f"{sid}|{r}" in app.items_table.rows for r in gone), "in sight: one aged out too, its minutes afresh"


@pytest.mark.anyio
async def test_ctrl_delete_takes_a_nested_family_once(store, sid):
    """#235 review: a done parent, a done child and its done grandchild go in one delete_family
    call, counted once each, whichever of them settled first."""
    grand = store.post_item(sid, "task", "grandchild", status="done")
    mid = store.post_item(sid, "task", "child", status="done")
    top = store.post_item(sid, "task", "parent", status="done")
    for child, parent in ((grand, mid), (mid, top)):
        store.db.execute("UPDATE items SET parent_ref = ? WHERE session_id = ? AND ref = ?", (parent, sid, child))
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        notes, calls = [], []
        app.notify = lambda text, **kw: notes.append(text)
        real = app.client.delete_family
        app.client.delete_family = lambda s_, r: calls.append(r) or real(s_, r)
        app.items_table.focus()
        await pilot.press("ctrl+delete")
        await landed(app, pilot)
    assert calls == [top], calls
    assert "deleted 3 completed items: Ctrl+Z to undo" in notes, notes
    assert all(tui.standing(store.item(sid, r)) == "finished" for r in (top, mid, grand))


@pytest.mark.anyio
async def test_ctrl_delete_leaves_a_compacting_sessions_rows(store, tmp_path, monkeypatch):
    """#235 with #150 and #233: a compacting session's rows are locked, keys ignored, so
    Ctrl+Delete passes them by and deletes the rest."""
    from claude_wheelhouse import liveness
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "live")
    busy = store.create_session(str(tmp_path), name="hosted", runner="sdk")
    locked = store.post_item(busy, "task", "done", status="done")
    free = store.create_session(str(tmp_path), name="other")
    other = store.post_item(free, "task", "done", status="done")
    store.command(busy, "compact")   # its compaction cycle (#244)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.items_table.focus()
        await pilot.press("ctrl+delete")
        await landed(app, pilot)
    assert tui.standing(store.item(busy, locked)) == "settled", "the compacting session's row kept"
    assert tui.standing(store.item(free, other)) == "finished", "the other deleted"


@pytest.mark.anyio
@pytest.mark.parametrize("child_status, parent_shown, desc", [
    ("todo", True, "a child still to do keeps its done parent in sight"),
    ("done", False, "a done child: the parent goes, with it"),
])
async def test_autocleanse_keeps_a_parent_with_a_child_under_way(store, sid, child_status, parent_shown, desc):
    """#235 review: never a child shown without its parent."""
    parent = store.post_item(sid, "task", "parent", status="done")
    child = store.post_item(sid, "task", "child", status=child_status)
    store.db.execute("UPDATE items SET parent_ref = ? WHERE session_id = ? AND ref = ?", (parent, sid, child))
    for r in (parent, child):
        aged(store, sid, r, 30)
    app = WheelhouseApp(store)
    app.autocleanse = 5
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert (f"{sid}|{parent}" in app.items_table.rows) == parent_shown, desc


@pytest.mark.anyio
async def test_autocleanse_never_hides_the_selected_item(store, sid):
    """#235: the selected row stays under the cursor; it goes once the selection moves on."""
    other = store.post_item(sid, "question", "anchor")
    ref = store.post_item(sid, "task", "done a while ago", status="done")
    app = WheelhouseApp(store)
    app.autocleanse = 5
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.select_row(f"{sid}|{ref}")
        aged(store, sid, ref, 30)
        app.refresh_data()
        await pilot.pause()
        assert f"{sid}|{ref}" in app.items_table.rows, "selected: kept"
        app.select_row(f"{sid}|{other}")
        app.refresh_data()
        await pilot.pause()
        assert f"{sid}|{ref}" not in app.items_table.rows, "the selection moved on: hidden"


@pytest.mark.anyio
async def test_show_item_brings_back_a_row_autocleanse_hid(store, sid):
    """#235 with #223 and the PRs pane: a pick of an item autocleanse hid (a Landed line, a
    merged PR) shows it in the inbox under the highlight, so Delete acts on it."""
    store.post_item(sid, "question", "anchor")
    ref = store.post_item(sid, "task", "done a while ago", status="done")
    aged(store, sid, ref, 30)
    app = WheelhouseApp(store)
    app.autocleanse = 5
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert f"{sid}|{ref}" not in app.items_table.rows, "hidden to begin with"
        assert app.show_item(sid, ref)
        await pilot.pause()
        assert f"{sid}|{ref}" in app.items_table.rows, "picked: back in the inbox"
        assert app.items_table.show_cursor and app.items_cursor == f"{sid}|{ref}", "and highlighted"



@pytest.mark.anyio
async def test_show_item_leaves_a_hidden_row_of_a_compacting_session_unpicked(store, tmp_path, monkeypatch):
    """#235 with #165: a pick of an item autocleanse hid, in a session being compacted, is
    refused as a listed one is: False, said, and the highlight not put on the locked row."""
    from claude_wheelhouse import liveness
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "live")
    busy = store.create_session(str(tmp_path), name="hosted", runner="sdk")
    ref = store.post_item(busy, "task", "done a while ago", status="done")
    aged(store, busy, ref, 30)
    store.command(busy, "compact")   # its compaction cycle (#244)
    app = WheelhouseApp(store)
    app.autocleanse = 5
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        notes = []
        app.notify = lambda text, **kw: notes.append(text)
        assert not app.show_item(busy, ref), "refused"
        assert tui.COMPACTING_LOCKED in notes, notes
        assert app.items_cursor != f"{busy}|{ref}", "the highlight not on it"


@pytest.mark.anyio
async def test_ctrl_delete_is_refused_while_an_undo_lands(store, sid):
    """#235 batch review: Ctrl+Delete asked while a Ctrl+Z's calls are held is refused, said,
    so it never reads a half-restored inbox."""
    refs = [store.post_item(sid, "task", f"t{i}", status="done") for i in range(3)]
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.items_table.focus()
        await pilot.press("ctrl+delete")
        await landed(app, pilot)
        gate = held_calls(app)
        await pilot.press("ctrl+z")
        await pilot.pause()
        assert app.undoing, "the undo held"
        notes = []
        app.notify = lambda text, **kw: notes.append(text)
        await pilot.press("ctrl+delete")
        await pilot.pause()
        assert tui.UNDO_LANDING.format(key="Ctrl+Delete") in notes, notes
        assert not app.behind, "nothing held to act later"
        gate.set()
        await landed(app, pilot)
    assert all(tui.standing(store.item(sid, r)) == "settled" for r in refs), "the undo's rows back, none deleted"


@pytest.mark.anyio
async def test_delete_is_refused_while_an_undo_lands(store, sid):
    """#235 batch review: Delete asked while a Ctrl+Z's calls are held is refused, said: its row
    and family read mid-undo could be half back (a parent before its children)."""
    refs = undo_setup(store, sid, "family")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.items_table.focus()
        app.items_table.move_cursor(row=app.items_table.get_row_index(f"{sid}|{refs[0]}"))
        await pilot.pause()
        await pilot.press("delete", "alt+y")
        await landed(app, pilot)
        gate = held_calls(app)
        await pilot.press("ctrl+z")
        await pilot.pause()
        notes = []
        app.notify = lambda text, **kw: notes.append(text)
        await pilot.press("delete", "delete")
        await pilot.pause()
        assert notes.count(tui.UNDO_LANDING.format(key="Delete")) == 2, notes
        assert not app.behind, "nothing held to act later"
        assert [type(s).__name__ for s in app.screen_stack] == ["Screen"], "no dialog asked mid-undo"
        gate.set()
        await landed(app, pilot)
        assert not app.undoing
    assert all(tui.standing(store.item(sid, r)) != "finished" for r in refs), "the family back, none deleted"

def test_ctrl_delete_reaches_the_app_from_windows_terminal():
    """#235: Windows Terminal sends Ctrl+Delete as CSI 3;5~, which Textual's parser reads as
    ctrl+delete, the key the item list binds."""
    from textual._ansi_sequences import ANSI_SEQUENCES_KEYS
    assert [k.value for k in ANSI_SEQUENCES_KEYS["\x1b[3;5~"]] == ["ctrl+delete"]
    assert any(b.key == "ctrl+delete" for b in tui.ItemList.BINDINGS)


@pytest.mark.anyio
@pytest.mark.both
@pytest.mark.parametrize("keys, desc", [
    (["delete", "ctrl+z"], "Ctrl+Z pressed before the delete lands undoes it once it has"),
    (["ctrl+a", "delete", "ctrl+z"], "Ctrl+A's rows, the same: all back"),
    (["alt+v", "delete", "ctrl+r"], "Ctrl+R straight after Delete, finished shown: reopened once the delete lands"),
])
async def test_ctrl_z_waits_for_a_delete_still_landing(store, sid, keys, desc):
    """#234: Ctrl+Z (#151) acts on the last act, so one asked while a Delete's call is held waits
    for it, then puts every row back."""
    refs = [store.post_item(sid, "task", f"t{i}", status="done") for i in range(2)]
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.items_table.focus()
        app.items_table.move_cursor(row=app.items_table.get_row_index(f"{sid}|{refs[0]}"))
        await pilot.pause()
        gate = held_calls(app)
        for key in keys:
            await pilot.press(key)
        await pilot.pause()
        gate.set()
        await landed(app, pilot)
        await pilot.pause()
        took = refs if "ctrl+a" in keys else refs[:1]
        assert all(f"{sid}|{r}" in app.items_table.rows for r in took), f"{desc}: {list(app.items_table.rows)}"
    assert all(store.item(sid, r)["dismissed"] is None for r in refs), desc


@pytest.mark.anyio
@pytest.mark.both
@pytest.mark.parametrize("fail, pinned_after, desc", [
    (None, False, "confirmed: the row goes at once, its pin cleared and it closed off the UI thread"),
    (KeyError("no item T1 in this session"), True, "the delete fails: it comes back, its pin kept"),
])
async def test_delete_on_a_pinned_item_goes_at_once_too(store, sid, fail, pinned_after, desc):
    """#234: Delete on a pinned item asks first, then goes as any other: the pin cleared on the
    worker thread with the delete, never waited for on the UI thread."""
    ref = store.post_item(sid, "task", "kept", status="done")
    store.set_pinned(sid, ref, True)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.items_table.focus()
        app.items_table.move_cursor(row=app.items_table.get_row_index(f"{sid}|{ref}"))
        await pilot.pause()
        gate = held_calls(app, fail)
        await pilot.press("delete", "alt+y")
        await pilot.pause()
        assert f"{sid}|{ref}" not in app.items_table.rows, f"{desc}: gone before the call lands"
        gate.set()
        await landed(app, pilot)
        assert (f"{sid}|{ref}" in app.items_table.rows) == pinned_after, desc
    assert tui.pinned(store.item(sid, ref)) == pinned_after, desc


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(160, 40), (100, 30)])
async def test_a_compacting_session_shows_it(store, tmp_path, monkeypatch, size):
    """#150: while its host records a compaction (the question's Compact now, say), Compact is
    off, the session's inbox rows dim and ignore clicks and keys, Detail dims on one of them,
    and its eye in the session list is blue; an item it posts meanwhile is never locked; all
    back once the cycle ends (#244), and blue throughout it, from the press to /compact's end."""
    from claude_wheelhouse import host as host_mod, liveness
    from claude_wheelhouse.tui import EYE_BLUE, ItemList, SessionList
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "live")
    sid = store.create_session(str(tmp_path), name="hosted", runner="sdk")
    store.post_item(sid, "task", "work")
    store.post_item(sid, "question", "which?")
    app = WheelhouseApp(store)

    async def click(key):
        await pilot.click(table, offset=(table.region.width // 2, table.get_row_index(key) + 1))
        await pilot.pause()

    async def again():
        app.refresh_data()
        await pilot.pause()

    async with app.run_test(size=size) as pilot:
        await pick(app, pilot, sid)
        table = app.query_one(ItemList)
        t1, q1 = (next(k for k in table.keys() if k and k.endswith(r)) for r in ("T1", "Q1"))
        await click(t1)
        assert app.selected == (sid, "T1"), f"{size}: picked by a click"

        store.command(sid, "compact")   # pressed: blue from here, before the host takes it
        await again()
        assert sid in app.query_one(SessionList).blue, f"{size}: blue at the press"
        store.take_command(sid)
        cid = store.compaction(sid)["id"]
        store.move_compaction(sid, cid, "preparing")   # as the host writes it
        store.set_activity(sid, "compacting: collecting notes")
        # its task item (#244 PR 2), posted after the cut-off, is never locked: put aside here, so
        # the keys below are about the session's own rows
        store.db.execute("DELETE FROM items WHERE answer = '{\"from\": \"compaction\"}'")
        await again()
        compact = app.query_one("#compact", Button)
        assert compact.disabled, f"{size}: Compact off while compacting"
        assert {t1, q1} <= table.locked, f"{size}: the session's rows locked"
        assert "dim" in str(table.get_row(q1)[-1].spans), f"{size}: dimmed"
        await click(q1)
        assert app.selected == (sid, "T1"), f"{size}: a click on a locked row does nothing"
        table.focus()
        for key in ("up", "up", "down"):
            await pilot.press(key)
            await pilot.pause()
            assert app.selected == (sid, "T1") and table.cursor_key() == t1, f"{size}: {key} never lands on a locked row"
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is app.screen_stack[0], f"{size}: Enter opens nothing locked"
        assert app.detail.has_class("compacting"), f"{size}: Detail dims on the session's item"
        sessions = app.query_one(SessionList)
        assert sid in sessions.eyes and sid in sessions.blue, f"{size}: its eye sweeps, blue"
        assert any(EYE_BLUE[0] in str(sp.style) for sp in tui.cylon(0, colours=EYE_BLUE).spans), size

        store.post_item(sid, "question", "asked meanwhile")
        await again()
        q2 = next(k for k in table.keys() if k and k.endswith("Q2"))
        assert q2 not in table.locked, f"{size}: an item posted while compacting is never locked"
        await click(q2)
        assert app.selected == (sid, "Q2"), f"{size}: and can be picked"

        store.set_activity(sid, "idle")   # a line between turns: the cycle, not the line, says compacting
        await again()
        assert sid in sessions.blue and q1 in table.locked, f"{size}: still blue between the notes and /compact"

        store.move_compaction(sid, cid, "compacting")
        store.set_activity(sid, "compacting")
        await again()
        store.move_compaction(sid, cid, "completed")
        store.set_activity(sid, "idle · compacted")
        await again()
        assert not compact.disabled and not table.locked, f"{size}: Compact and the rows back"
        assert not app.detail.has_class("compacting") and sid not in sessions.blue, f"{size}: all back to normal"
        await click(q1)
        assert app.selected == (sid, "Q1"), f"{size}: clicks work again"


@pytest.mark.anyio
@pytest.mark.both
@pytest.mark.parametrize("answer, chosen, desc", [
    ("#steps-ok", True, "OK: the ticks kept, and the press goes on"),
    ("escape", False, "Esc: the press cancelled, asked again next time"),
])
async def test_the_first_press_offers_the_steps(store, sid, live, first_press, answer, chosen, desc):
    from claude_wheelhouse import steps
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pick(app, pilot, sid)
        await pilot.click("#end")
        await pilot.pause()
        assert isinstance(app.screen, tui.FirstSteps), desc
        if answer == "escape":
            await pilot.press("escape")
        else:
            app.screen.query_one("#steps-cleanup", Checkbox).value = False
            await pilot.click(answer)
        await pilot.pause()
        await pilot.pause()
        assert bool(store.setting(steps.CHOSEN)) is chosen, desc
        assert not isinstance(app.screen, tui.FirstSteps), desc
        assert (app.screen is not app.screen_stack[0]) is chosen, f"{desc}: Forget's own dialog next, or nothing"
        if not chosen:
            await pilot.click("#end")
            await pilot.pause()
            assert isinstance(app.screen, tui.FirstSteps), f"{desc}: asked again at the next press"
    if chosen:
        assert store.setting(steps.setting_key("pr_merged", "cleanup")) == "off", f"{desc}: unticked kept"


@pytest.mark.anyio
@pytest.mark.both
async def test_session_steps_keeps_what_changed(store, sid):
    from claude_wheelhouse import steps
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        app.action_session_steps()
        await pilot.pause()
        assert isinstance(app.screen, tui.SessionSteps)
        assert not app.screen.query("#step-before_compact-announce"), "no compaction rows outside SDK mode"
        box = app.screen.query_one("#step-pr_merged-cleanup", Checkbox)
        assert not box.value, "unattended add-ons off until a choice is made"
        box.value = True
        box.value = False
        await pilot.click("#session-steps-ok")
        await pilot.pause()
    assert store.setting(steps.setting_key("pr_merged", "cleanup")) == "off"
    assert store.setting(steps.CHOSEN), "answered here: the first-press dialog isn't shown"
    assert store.setting(steps.setting_key("before_forget", "items")) == "on", "before any choice, all kept as shown"


@pytest.mark.anyio
@pytest.mark.both
@pytest.mark.parametrize("scenario", ["follow another", "restart", "two apps"])
async def test_a_compactions_cut_off_is_the_stores_for_every_app(store, tmp_path, monkeypatch, scenario):
    """#150: which items a compaction locks is set once, where it starts (Store.compact_cut),
    never by what an app happened to show: following another session meanwhile, restarting
    the app, or a second app on the same store all lock the items there before it started,
    and never one posted since. Detail dims on a locked item only."""
    from claude_wheelhouse import liveness
    from claude_wheelhouse.tui import ItemList
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "live")
    a = store.create_session(str(tmp_path), name="alpha", runner="sdk")
    b = store.create_session(str(tmp_path), name="bravo", runner="sdk")
    store.post_item(a, "task", "work")
    store.post_item(b, "task", "other")

    async def locked_refs(app, pilot):
        await pick(app, pilot, a)
        app.refresh_data()
        await pilot.pause()
        table = app.query_one(ItemList)
        refs = {k.split("|")[1] for k in table.locked}
        dims = {}
        for ref in ("T1", "Q1"):
            app.selected = (a, ref)
            app.paint_detail()
            dims[ref] = app.detail.has_class("compacting")
        return refs, dims

    seen = []
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pick(app, pilot, b if scenario == "follow another" else a)
        store.command(a, "compact")   # pressed: the cut-off is the newest item then
        store.set_activity(a, "compacting: collecting notes")
        app.refresh_data()
        await pilot.pause()
        store.post_item(a, "question", "asked meanwhile")   # after the cut-off
        if scenario != "restart":
            seen.append(await locked_refs(app, pilot))
    if scenario != "follow another":
        again = WheelhouseApp(store)
        async with again.run_test(size=(160, 40)) as pilot:
            seen.append(await locked_refs(again, pilot))
    for refs, dims in seen:
        assert refs == {"", "T1"}, f"{scenario}: the conversation and T1 locked, never Q1 posted since: {refs}"
        assert dims == {"T1": True, "Q1": False}, f"{scenario}: Detail dims on a locked item only: {dims}"
    assert len({frozenset(r) for r, _ in seen}) == 1, f"{scenario}: every app agrees"


def bound_combos() -> list[str]:
    """Every Alt and Ctrl+letter combo bound anywhere: the app's, its widgets' and dialogs', and
    the PRs pane's."""
    import re
    from claude_wheelhouse import tui
    from claude_wheelhouse_prs import pane
    keys = {k.strip() for mod in (tui, pane) for obj in vars(mod).values()
            if isinstance(obj, type) and obj.__module__ == mod.__name__
            for b in obj.__dict__.get("BINDINGS", []) for k in b.key.split(",")}
    keys |= {b.key for b in tui.BUTTON_KEYS}
    return sorted(k for k in keys if re.fullmatch(r"alt\+(shift\+)?[a-z]|ctrl\+[a-z@]", k))


def terminal_bytes(key: str) -> str:
    """What a terminal (Windows Terminal among them) sends for a combo: Alt as Esc before the
    letter, Ctrl+letter as its control character, Ctrl+Space as NUL (ctrl+@)."""
    *mods, letter = key.split("+")
    if mods[0] == "alt":
        return "\x1b" + (letter.upper() if "shift" in mods else letter)
    return "\x00" if letter == "@" else chr(ord(letter) - 96)


@pytest.mark.parametrize("key", bound_combos())
def test_every_bound_combo_arrives_as_bound(key):
    """#189: Textual reads some Esc sequences as other keys (Esc f is ctrl+right, Esc b
    ctrl+left), so a combo bound to one of those would never arrive."""
    from textual._xterm_parser import XTermParser
    parser = XTermParser(debug=False)
    got = [e.key for e in [*parser.feed(terminal_bytes(key)), *parser.feed("")] if isinstance(e, events.Key)]
    assert got == [key], f"{key}: the terminal's bytes parse as {got}"


@pytest.mark.anyio
async def test_a_lists_alt_key_is_not_typed_in_the_answer_box(store, sid):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        box = app.query_one("#answer", TextArea)
        box.focus()
        app.post_message(events.Key("alt+r", "r"))   # the session list's Rename, as a terminal sends it
        await pilot.pause()
        assert box.text == "" and app.screen is app.screen_stack[0], "dropped: neither typed nor acted on"


MCP_SEEN = [{"name": "sentry", "status": "disabled", "kind": "stdio"},
            {"name": "claude.ai Clay", "status": "connected", "kind": "claudeai-proxy"},
            {"name": "local-oauth", "status": "needs-auth", "kind": "http"},
            {"name": "claude.ai Slack", "status": "needs-auth", "kind": "claudeai-proxy"},
            {"name": "github", "status": "failed", "kind": "stdio", "error": "ECONNREFUSED"}]


@pytest.mark.parametrize("server, press, desc", [
    ({"status": "disabled", "kind": "stdio"}, None, "disabled: no button (Q3)"),
    ({"status": "needs-auth", "kind": "claudeai-proxy"}, ("Sign in", "signin"), "a connector to sign in"),
    ({"status": "needs-auth", "kind": "claudeai-proxy", "link": "https://x"}, ("Reconnect", "reconnect"),
     "a connector with its link out: reconnect once authorised"),
    ({"status": "needs-auth", "kind": "http", "link": "https://x"}, ("Sign in", "signin"),
     "a local OAuth server: Claude Code connects it, so Sign in again for a fresh link"),
    ({"status": "failed", "kind": "stdio"}, ("Reconnect", "reconnect"), "failed: reconnect (a restart)"),
    ({"status": "connected", "kind": "claudeai-proxy"}, ("Reconnect", "reconnect"), "connected: reconnect"),
])
def test_mcp_press(server, press, desc):
    assert tui.mcp_press(server) == press, desc


@pytest.mark.anyio
async def test_mcp_servers_dialog_lists_and_presses(store, tmp_path, monkeypatch):
    monkeypatch.setattr(features, "_flag", True)
    sid, app = await hosted_app(store, tmp_path, monkeypatch, "sdk")
    store.set_mcp_status(sid, MCP_SEEN)
    opened = []
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(app, "open_url", lambda url, **kw: opened.append(url))
        app.session_list.focus()
        await pilot.press("alt+shift+m")
        await pilot.pause()
        dialog = app.screen
        assert isinstance(dialog, tui.McpServers)
        assert [a["do"] for a in store.take_mcp_asks(sid)] == ["check"], "opening it asks the host to look again"
        texts = [str(w.content).split(" · ")[0] for w in dialog.query(".mcp-text")]
        assert texts == ["claude.ai Slack", "local-oauth", "github", "claude.ai Clay", "sentry"], \
            "those that need you first"
        assert "ECONNREFUSED" in str(list(dialog.query(".mcp-text"))[2].content)
        buttons = {b.mcp[1]: str(b.label) for b in dialog.query(".mcp-row Button")}
        assert buttons == {"claude.ai Slack": "Sign in", "local-oauth": "Sign in", "github": "Reconnect",
                           "claude.ai Clay": "Reconnect"}, "disabled has none"
        slack = next(b for b in dialog.query(".mcp-row Button") if b.mcp[1] == "claude.ai Slack")
        slack.press()
        await pilot.pause()
        assert store.take_mcp_asks(sid) == [{"do": "signin", "server": "claude.ai Slack"}]
        seen = [dict(s, **({"link": "https://claude.ai/start", "note": "Authorise it"} if s["name"] == "claude.ai Slack"
                           else {"link": "https://auth/x", "waits": True} if s["name"] == "local-oauth" else {}))
                for s in MCP_SEEN]
        store.set_mcp_status(sid, seen)
        app.refresh_data()
        await pilot.pause()
        assert opened == ["https://claude.ai/start"], "the link pressed for opens once the host has it"
        buttons = {b.mcp[1]: str(b.label) for b in dialog.query(".mcp-row Button")}
        assert buttons["claude.ai Slack"] == "Reconnect"
        paste = dialog.query_one("#mcp-rows Input", Input)
        paste.value = "http://localhost:4321/callback?code=C"
        paste.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert store.take_mcp_asks(sid) == [{"do": "callback", "server": "local-oauth",
                                             "url": "http://localhost:4321/callback?code=C"}]
        for note in ("Still reconnecting", "Sign-in failed: x"):   # the host answers with the box still showing
            store.set_mcp_status(sid, [dict(s, note=note) if s["name"] == "local-oauth" else s for s in seen])
            app.refresh_data()
            await pilot.pause()
        assert len(dialog.query("#mcp-rows Input")) == 1 and "Sign-in failed: x" in " ".join(
            str(w.content) for w in dialog.query(".mcp-text")), "repainted in place, twice, with no clash"
        assert opened == ["https://claude.ai/start"], "opened once"


@pytest.mark.anyio
@pytest.mark.parametrize("runner_, live, says, desc", [
    ("tab", True, tui.MCP_TAB, "a tab: /mcp there (Q4)"),
    ("sdk", False, "Not running", "a hosted session not running: the last look, no buttons"),
])
async def test_mcp_servers_dialog_without_a_running_host(store, tmp_path, monkeypatch, runner_, live, says, desc):
    monkeypatch.setattr(features, "_flag", True)
    sid, app = await hosted_app(store, tmp_path, monkeypatch, runner_)
    if not live:
        from claude_wheelhouse import liveness
        monkeypatch.setattr(liveness, "status", lambda s, waking=False: "dead")
    store.set_mcp_status(sid, MCP_SEEN)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.session_list.focus()
        await pilot.press("alt+shift+m")
        await pilot.pause()
        dialog = app.screen
        assert says in str(dialog.query_one("#mcp-when", Label).content), desc
        assert list(dialog.query(".mcp-row Button")) == [], desc
        assert store.take_mcp_asks(sid) == [], f"{desc}: no host to ask"


@pytest.mark.parametrize("kind, status, mark, locked, desc", [
    ("task", "running", {}, True, "an item before the press: locked while compacting (#150)"),
    ("permission", "open", {}, False, "a permission the turn waits on: answerable, or the cycle never moves (#244)"),
    ("permission", "allowed", {}, True, "a permission already answered: locked as any other"),
    ("question", "open", {"answer": '{"from": "AskUserQuestion"}'}, False, "the host's AskUserQuestion, in process"),
    ("question", "open", {"host_question": True}, False, "the same through the daemon, which serves no answers"),
    ("question", "open", {"answer": None}, True, "the session's own question: locked"),
])
def test_a_call_the_turn_waits_on_is_never_locked_by_a_compaction(kind, status, mark, locked, desc):
    it = {"session_id": "s1", "id": 3, "kind": kind, "status": status, **mark}
    assert WheelhouseApp.locks(it, {"s1": 5}) == locked, desc


@pytest.mark.parametrize("width", [36, 76])
@pytest.mark.parametrize("who, text", [
    ("stage", "▐█          █▌ walls closing in\nasked for by you (Compact) · 10:39Z"),
    ("stage", "▐████████████▌ compacted\n220k → 40k · 10:41Z"),
    ("crew", "Han: I've got a bad feeling about these walls.\nC-3PO: Sir, the odds of survival are rather poor!"),
])
def test_stage_blocks_render_as_plain_text_never_markdown(width, who, text):
    """#244: Markdown would turn the art into rules and bullets; each line is cut, never wrapped."""
    from rich.console import Console
    from rich.segment import Segment
    console = Console(width=width, color_system=None)   # rendered as the app renders a block (_block_lines)
    rendered = console.render(tui.stage_text(who, text), console.options.update_width(width))
    shown = ["".join(seg.text for seg in line) for line in Segment.split_lines(rendered)]
    assert len(shown) == len(text.split("\n")), shown
    for line, want in zip(shown, text.split("\n")):
        assert line.rstrip() == want or (line.rstrip().endswith("…") and want.startswith(line.rstrip()[:-1])), (line, want)


@pytest.mark.anyio
@pytest.mark.parametrize("kind, layout, row, times, full, desc", [
    ("question", "split", 3, 1, None, "a question, one click: stays in Detail"),
    ("question", "split", 3, 2, "ThreadView", "a question, double-click: full screen"),
    ("task", "split", 3, 1, None, "a task, one click: stays in Detail"),
    ("task", "split", 3, 2, "ThreadView", "a task, double-click: full screen"),
    ("doc", "split", 3, 1, None, "a doc, one click: its editor, in Detail"),
    ("doc", "split", 3, 2, "DocView", "a doc, double-click: full screen, not its editor"),
    ("doc", "split", 20, 2, "DocView", "a doc's lower rows, where the editor opens: full screen all the same"),
    ("doc", "tabbed", 3, 1, None, "a doc tabbed, one click: its editor's tab"),
    ("doc", "tabbed", 3, 2, "DocView", "a doc tabbed, the editor's tab over it: full screen all the same"),
    ("conversation", "split", 3, 2, None, "a conversation, double-click: no full screen to go to"),
])
async def test_double_click_on_detail_goes_full_screen_and_back(store, sid, tmp_path, kind, layout, row, times, full,
                                                                desc):
    """#253: a double-click on Detail opens what it shows full screen, documents included, and
    one on the full screen goes back; a single click keeps its own job (a doc's editor). As the
    terminal sends it (raw_click), where each press goes to what's under the pointer then."""
    if kind == "doc":
        (tmp_path / "plan.md").write_text("# The plan\n\n" + "".join(f"Step {i}, built well.\n\n" for i in range(30)))
        ref = store.post_doc(sid, "plan", str(tmp_path / "plan.md"))
    elif kind != "conversation":
        ref = store.post_item(sid, kind, "which db?", "Postgres or SQLite? " * 20)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.doc_layout = tui.TABBED if layout == "tabbed" else tui.SPLIT
        app.items_table.move_cursor(row=app.items_table.get_row_index(f"{sid}|" if kind == "conversation" else f"{sid}|{ref}"))
        await pilot.pause()
        at = app.detail.region.offset + (3, row)   # the same place twice, whatever opens under it
        for _ in range(times):   # as a hand clicks: each click handled before the next
            raw_click(app, app.screen, at)
            await pilot.pause(0.05)
        await pilot.pause(0.2)
        assert type(app.screen).__name__ == (full or "Screen"), desc
        opened = kind == "doc" and times == 1
        assert (app.detail_editor.key is not None) == opened, f"{desc}: Detail's editor"
        assert bool(app.doc_quotes) == opened, f"{desc}: a comment's quote, as the editor"
        if full is None:
            return
        await pilot.pause(0.6)   # a new double-click, not more of the last one
        at = app.screen.query_one("#docview" if full == "DocView" else "#thread").region.offset + (3, row)
        for _ in range(2):
            raw_click(app, app.screen, at)
            await pilot.pause(0.05)
        await pilot.pause(0.2)
        assert len(app.screen_stack) == 1, f"{desc}: a double-click there goes back"
        assert app.detail_editor.key is None and not app.screen.selections, f"{desc}: no editor, nothing selected"
        if kind == "doc":
            assert app.detail_panes.tab == tui.DOC_TAB, f"{desc}: the doc's tab, as before"
