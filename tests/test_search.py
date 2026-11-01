"""#273: the inbox's search popover (search.py, tui.SearchItems, WheelhouseApp.go_to_item)."""

import re
from datetime import datetime, timedelta, timezone

import pytest
from textual.widgets import Checkbox, Input, OptionList

from claude_wheelhouse import search
from claude_wheelhouse.tui import SearchItems, WheelhouseApp


def item(ref, title, age=0):
    at = (datetime(2026, 11, 1, tzinfo=timezone.utc) - timedelta(minutes=age)).isoformat()
    return {"ref": ref, "title": title, "created_at": at}


POOL = [item("T3", "deploy the api", 5), item("T30", "fix T3 follow-up", 4), item("Q25", "which api?", 3),
        item("PR276", "api: retries", 2), item("T4", "the api deploy", 1), item("D2", "Deploy order", 0)]


@pytest.mark.parametrize("query, refs, desc", [
    ("T3", ["T3", "T30"], "ref-shaped: the exact ref first, then refs holding it, then titles holding it"),
    ("t3", ["T3", "T30"], "case-insensitive"),
    ("q25", ["Q25"], "a question's ref"),
    ("pr276", ["PR276"], "a PR's ref"),
    ("deploy", ["D2", "T3", "T4"], "title starting with it, newest first, then containing it"),
    ("api", ["PR276", "T4", "Q25", "T3"], "starting with it, then containing it, each newest first"),
    ("zzz", [], "nothing matches"),
    ("", ["D2", "T4", "PR276", "Q25", "T30", "T3"], "nothing typed: everything, newest first"),
])
def test_find_orders_matches(query, refs, desc):
    found, more = search.find(POOL, query)
    assert [it["ref"] for it in found] == refs, desc
    assert more == 0, desc


def test_find_caps_the_rows_and_counts_the_rest():
    found, more = search.find([item(f"T{n}", "same", n) for n in range(1, 61)], "same")
    assert (len(found), more) == (50, 10), "fifty rows, and how many more"


@pytest.mark.parametrize("query, shaped, desc", [
    ("T3", True, "a task"), ("pr12", True, "a PR, lower case"), ("M7", True, "a doc"),
    ("T", False, "a letter alone"), ("T3a", False, "more after the digits"), ("#273", False, "an issue"),
    ("X3", False, "no such kind"),
])
def test_ref_shaped(query, shaped, desc):
    assert search.ref_shaped(query) is shaped, desc


def popover(app) -> SearchItems:
    assert isinstance(app.screen, SearchItems), "the popover is open"
    return app.screen


def listed(app) -> list[str]:
    options = popover(app).query_one("#search-results", OptionList)
    return [options.get_option_at_index(i).id for i in range(options.option_count)]


@pytest.fixture
def two(store, sid, tmp_path):
    """Two sessions, each with a T1: refs are per session."""
    other = store.create_session(str(tmp_path), name="other")
    store.post_item(sid, "task", "ship the search")
    store.post_item(other, "task", "ship the links")
    return sid, other


@pytest.mark.anyio
@pytest.mark.both
@pytest.mark.parametrize("follow, title, keys, named, desc", [
    (False, "Search all sessions", {"S|T1", "O|T1"}, True, "no session selected: every session's, named"),
    (True, "Search demo", {"S|T1"}, False, "a session selected: its items alone, unnamed"),
])
async def test_scope(store, two, follow, title, keys, named, desc):
    sid, other = two
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        if follow:
            app.follow(sid)
            await pilot.pause()
        app.action_search()
        await pilot.pause()
        box = popover(app)
        assert str(box.query_one(".dialog-title").render()) == title, desc
        want = {k.replace("S", sid, 1).replace("O", other, 1) for k in keys}
        assert set(listed(app)) == want, desc
        assert (box.names is not None) is named, desc
        line = box.query_one("#search-results", OptionList).get_option_at_index(0).prompt.plain
        assert ("demo" in line or "other" in line) is named, f"{desc}: {line!r}"


@pytest.mark.anyio
@pytest.mark.both
async def test_hidden_toggle_lets_hidden_items_in_and_is_kept_for_the_run(store, sid):
    store.post_item(sid, "task", "still to do")
    gone = store.post_item(sid, "task", "dropped one", status="dropped")
    store.dismiss(sid, gone)   # off the inbox until F shows the finished
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.action_search()
        await pilot.pause()
        assert listed(app) == [f"{sid}|T1"], "off: only what the inbox shows"
        popover(app).query_one("#search-hidden", Checkbox).value = True
        await pilot.pause()
        assert f"{sid}|{gone}" in listed(app), "on: the hidden one too"
        await pilot.press("escape")
        await pilot.pause()
        app.action_search()
        await pilot.pause()
        assert popover(app).query_one("#search-hidden", Checkbox).value, "remembered for the run"
        assert f"{sid}|{gone}" in listed(app), "and applied"


@pytest.mark.anyio
@pytest.mark.both
@pytest.mark.parametrize("hidden, desc", [
    (False, "an item the inbox shows: highlighted there and in Detail"),
    (True, "a hidden one: in Detail alone, the inbox unchanged, said hidden"),
])
async def test_picking(store, sid, hidden, desc):
    store.post_item(sid, "question", "anchor")
    ref = store.post_item(sid, "task", "the one", status="dropped")
    if hidden:
        store.dismiss(sid, ref)
    for n in range(30):   # enough rows that the inbox scrolls to it
        store.post_item(sid, "task", f"filler {n}")
    app = WheelhouseApp(store)
    app.search_hidden = hidden
    async with app.run_test(size=(160, 20)) as pilot:
        await pilot.pause()
        rows = list(app.items_table.rows)
        app.action_search()
        await pilot.pause()
        for ch in "the one":
            await pilot.press("space" if ch == " " else ch)
        await pilot.pause()
        assert listed(app)[0] == f"{sid}|{ref}", desc
        await pilot.press("enter")
        await pilot.pause()
        assert not isinstance(app.screen, SearchItems), f"{desc}: closed"
        assert app.selected == (sid, ref), desc
        assert f"## {ref} · the one" in app._detail_text, desc
        assert app.focused is app.items_table, f"{desc}: focus on the inbox"
        assert ("Hidden from the inbox" in app._detail_text) is hidden, desc
        table = app.items_table
        if hidden:
            assert list(table.rows) == rows and not table.show_cursor, f"{desc}: the inbox as it was"
        else:
            assert table.show_cursor and table.cursor_key() == f"{sid}|{ref}", desc
            assert table.cursor_row >= table.scroll_y, f"{desc}: scrolled to it"


@pytest.mark.anyio
@pytest.mark.both
async def test_picking_one_autocleanse_hid_leaves_it_hidden(store, sid):
    store.post_item(sid, "question", "anchor")
    ref = store.post_item(sid, "task", "done a while ago", status="done")
    at = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(timespec="seconds")
    store.db.execute("UPDATE items SET updated_at = ? WHERE session_id = ? AND ref = ?", (at, sid, ref))
    app = WheelhouseApp(store)
    app.autocleanse = 5
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.go_to_item(sid, ref)
        await pilot.pause()
        app.refresh_data()
        await pilot.pause()
        assert app.selected == (sid, ref), "shown in Detail"
        assert f"{sid}|{ref}" not in app.items_table.rows, "not brought back into the inbox"
        assert "Hidden from the inbox" in app._detail_text


@pytest.mark.anyio
async def test_picking_a_child_under_a_collapsed_parent_shows_it(store, sid):
    store.post_item(sid, "task", "parent")
    child = store.post_item(sid, "task", "child")
    store.db.execute("UPDATE items SET parent_ref = 'T1' WHERE session_id = ? AND ref = ?", (sid, child))
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.toggle_children(f"{sid}|T1")
        await pilot.pause()
        assert f"{sid}|{child}" not in app.items_table.rows, "collapsed to begin with"
        app.go_to_item(sid, child)
        await pilot.pause()
        assert app.items_table.cursor_key() == f"{sid}|{child}", "its parent opened, it highlighted"


@pytest.mark.anyio
@pytest.mark.parametrize("key, typed, opens, desc", [
    ("d", "d", True, "a letter"), ("T", "T", True, "a capital"), ("3", "3", True, "a digit"),
    ("space", "", False, "Space opens nothing"),
    ("depl", "depl", True, "the keys after it typed after it, not over it"),
])
async def test_typing_in_the_inbox_opens_search(store, sid, key, typed, opens, desc):
    store.post_item(sid, "task", "deploy")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.items_table.focus()
        await pilot.pause()
        await pilot.press(*([key] if key == "space" else key))
        await pilot.pause()
        assert isinstance(app.screen, SearchItems) is opens, desc
        if opens:
            assert popover(app).query_one("#search-text", Input).value == typed, desc


@pytest.mark.anyio
async def test_the_glass_opens_it_and_esc_puts_focus_on_the_inbox(store, sid):
    store.post_item(sid, "task", "deploy")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.query_one("#answer").focus()
        await pilot.pause()
        glass = app.query_one("#search-glass")
        assert glass.region.y == app.query_one("#items-pane").region.y, "on the inbox's top border"
        await pilot.click("#search-glass")
        await pilot.pause()
        assert popover(app).query_one("#search-text", Input).value == ""
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, SearchItems) and app.focused is app.items_table, "back on the inbox"


# #274: ids in Detail's text link to their items


REFS = {"T3", "Q252", "PR276", "M2", "P4"}


@pytest.mark.parametrize("text, linked, desc", [
    ("see T3 now", ["T3"], "a whole word"),
    ("Q252. and PR276,", ["Q252", "PR276"], "before punctuation, and a PR's"),
    ("allow P4?", ["P4"], "a permission's"),
    ("(T3)", ["T3"], "in brackets"),
    ("#273 and #T3", [], "a GitHub issue, and after #"),
    ("T3a xT3 T3-ops", [], "inside a longer word"),
    ("docs/T3 T3.md T206.md", [], "in a path or a file name"),
    ("`T3` and ``a T3``", [], "inline code"),
    ("```\nT3\n```", [], "a fenced block"),
    ("~~~\nT3\n~~~\nthen T3", ["T3"], "after a fenced block closes"),
    ("- item\n\n    ```\n    T3\n    ```", [], "a fence in a list item"),
    ("> ```\n> T3\n> ```", [], "a fence in a quote"),
    ("text\n\n    T3 indented", [], "an indented code block"),
    ("```\n```python\nT3\n```", [], "a fence's info line inside a fence doesn't close it"),
    ("T9 and D1", [], "an id the session doesn't have"),
    ("[T3](https://x) T3", ["T3"], "inside a link's text left alone"),
    ("t3", [], "lower case is not an id"),
])
def test_linked(text, linked, desc):
    out = search.linked(text, "S", frozenset(REFS))
    assert [m for m in re.findall(r"\(wheelhouse-item:S/(\w+)\)", out)] == linked, f"{desc}: {out!r}"


def detail_link(app, word: str):
    """Where `word` shows as a link in Detail: its screen offset, and its click action."""
    view = app.detail
    strips, texts, _ = view._layout(view.size.width)
    for y, (strip, text) in enumerate(zip(strips, texts)):
        x = 0
        for seg in strip:
            if seg.text.strip() == word and seg.style and "@click" in seg.style.meta:
                return view.region.offset + (x, y - round(app.detail_scroll.scroll_y)), seg.style.meta["@click"]
            x += seg.cell_length
    return None, None


@pytest.mark.anyio
@pytest.mark.both
@pytest.mark.parametrize("hidden, desc", [
    (False, "an item the inbox shows: highlighted there, in Detail"),
    (True, "a hidden one: in Detail alone"),
])
async def test_a_click_on_an_id_goes_to_the_item(store, sid, hidden, desc):
    q = store.post_item(sid, "task", "choose the db", status="dropped")
    if hidden:
        store.dismiss(sid, q)
    t = store.post_item(sid, "task", "pick a db", body=f"waiting on {q} and #12, `{q}` too")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.go_to_item(sid, t)
        await pilot.pause()
        at, action = detail_link(app, q)
        assert at is not None, f"{desc}: {q} is a link"
        assert action == f"app.go_to_item({sid!r}, {q!r})", desc
        await pilot.click(offset=at)
        await pilot.pause()
        assert app.selected == (sid, q), desc
        assert (app.items_table.show_cursor and app.items_table.cursor_key() == f"{sid}|{q}") is not hidden, desc


@pytest.mark.anyio
async def test_ids_link_to_the_texts_own_session(store, sid, tmp_path):
    """Q253 a: an id links to that ref in the session the text belongs to; refs repeat across sessions."""
    other = store.create_session(str(tmp_path), name="other")
    store.post_item(other, "task", "other's T1")
    store.post_item(other, "task", "mentions T1 and T2", body="see T1 and T2")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.go_to_item(other, "T2")
        await pilot.pause()
        _, action = detail_link(app, "T1")
        assert action == f"app.go_to_item({other!r}, 'T1')", "the other session's T1, not demo's"


@pytest.mark.anyio
@pytest.mark.parametrize("where, desc", [
    ("doc", "a doc's text"),
    ("thread", "a doc's thread, a reply mentioning it"),
])
async def test_a_docs_ids_link(store, sid, tmp_path, where, desc):
    store.post_item(sid, "task", "the task")
    path = tmp_path / "plan.md"
    path.write_text("# Plan\n\nDepends on T1.\n" if where == "doc" else "# Plan\n")
    m = store.post_doc(sid, "the plan", str(path))
    if where == "thread":
        store.reply(sid, m, "T1 covers it")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.go_to_item(sid, m)
        await pilot.pause()
        if where == "doc":
            _, action = detail_link(app, "T1")
        else:
            view = app.query_one("#detail-thread")
            strips, _, _ = view._layout(view.size.width)
            action = next((seg.style.meta["@click"] for strip in strips for seg in strip
                           if seg.text.strip() == "T1" and seg.style and "@click" in seg.style.meta), None)
        assert action == f"app.go_to_item({sid!r}, 'T1')", desc
