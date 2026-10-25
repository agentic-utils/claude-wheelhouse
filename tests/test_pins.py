"""Pinned items (#110, .plan/docs-section.md §1): any item can be pinned, a doc by default,
and clearing the inbox (Ctrl+A, Delete) never takes a pinned one."""

import pytest
from rich.cells import cell_len

from claude_wheelhouse import mcp_server
from claude_wheelhouse.store import Store, standing
from claude_wheelhouse.tui import PIN, UNPINNED, Confirm, WheelhouseApp
from test_tui import header_x


def post(store, sid, tmp_path, kind: str, name: str = "x", pinned: bool | None = None, **kw) -> str:
    """An item of kind, a doc with a file of its own; pinned None takes the default."""
    pin = {} if pinned is None else {"pinned": pinned}
    if kind == "doc":
        (tmp_path / f"{name}.md").write_text(f"# {name}\n")
        return store.post_doc(sid, name, str(tmp_path / f"{name}.md"), kw.get("parent"), **pin)
    return store.post_item(sid, kind, name, status=kw.get("status"), **pin)


def notices(app) -> list[str]:
    return [n.message for n in app._notifications]


# the store and the MCP tool

@pytest.mark.parametrize("kind, pinned, expected, desc", [
    ("doc", None, True, "a doc is pinned by default"),
    ("doc", False, False, "a scratch report posted unpinned"),
    ("task", None, False, "a task is not"),
    ("decision", None, False, "nor a decision, unless chosen"),
    ("question", True, True, "anything can be posted pinned"),
])
def test_what_is_pinned_when_posted(store, sid, tmp_path, kind, pinned, expected, desc):
    kw = {"why": "over B: cheaper; undo: revert"} if kind == "decision" else {}
    pin = {} if pinned is None else {"pinned": pinned}
    if kind == "doc":
        ref = post(store, sid, tmp_path, kind, pinned=pinned)
    else:
        ref = store.post_item(sid, kind, "x", **pin, **kw)
    assert bool(store.item(sid, ref)["pinned"]) == expected, desc


@pytest.mark.parametrize("set_to, posted_with, expected, desc", [
    (False, None, False, "unpinned by the person: posting again leaves it unpinned"),
    (True, False, True, "pinned by the person: a repost asking unpinned leaves it pinned"),
])
def test_a_reposted_doc_keeps_the_persons_pin(store, sid, tmp_path, set_to, posted_with, expected, desc):
    ref = post(store, sid, tmp_path, "doc", pinned=not set_to)
    store.set_pinned(sid, ref, set_to)
    assert post(store, sid, tmp_path, "doc", pinned=posted_with) == ref, desc
    assert bool(store.item(sid, ref)["pinned"]) == expected, desc


@pytest.mark.parametrize("kind, pinned, expected, desc", [
    ("doc", None, True, "a doc: pinned"),
    ("doc", False, False, "a doc posted pinned=false"),
    ("task", None, False, "a task: not"),
    ("task", True, True, "a task posted pinned=true"),
])
def test_the_mcp_tools_pinned(store, sid, tmp_path, kind, pinned, expected, desc):
    mcp_server._store, mcp_server._sid = store, sid
    (tmp_path / "r.md").write_text("# R\n")
    where = {"path": str(tmp_path / "r.md")} if kind == "doc" else {}
    ref = mcp_server.post_item(kind, "r", pinned=pinned, **where).split(".")[0]
    assert bool(store.item(sid, ref)["pinned"]) == expected, desc


def test_docs_from_before_pins_come_up_pinned(store, sid, tmp_path, db_file):
    doc, task = post(store, sid, tmp_path, "doc"), post(store, sid, tmp_path, "task")
    store.db.execute("DROP TRIGGER items_pinned_by_kind_v1")   # as before pins: neither it nor the column
    store.db.execute("ALTER TABLE items DROP COLUMN pinned")
    again = Store(db_file)
    assert [bool(again.item(sid, r)["pinned"]) for r in (doc, task)] == [True, False]


@pytest.mark.anyio
async def test_docs_cleared_before_pins_stay_unpinned_and_hidden(store, sid, tmp_path, db_file):
    live, dismissed, deleted = (post(store, sid, tmp_path, "doc", n) for n in ("live", "dismissed", "deleted"))
    store.dismiss(sid, dismissed)
    store.db.execute("UPDATE items SET dismissed = 'deleted' WHERE ref = ?", (deleted,))
    store.db.execute("DROP TRIGGER items_pinned_by_kind_v1")   # as before pins: neither it nor the column
    store.db.execute("ALTER TABLE items DROP COLUMN pinned")
    again = Store(db_file)
    assert [again.item(sid, r)["pinned"] for r in (live, dismissed, deleted)] == [1, 0, 0], "only the live doc"
    app = WheelhouseApp(again)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert set(app.items_table.rows) == {f"{sid}|{live}"}, "the cleared docs stay hidden with F off"


def test_a_parents_delete_leaves_its_pinned_children(store, sid, tmp_path):
    t = post(store, sid, tmp_path, "task", "t", status="done")
    kept = post(store, sid, tmp_path, "doc", "kept", parent=t)
    scratch = post(store, sid, tmp_path, "doc", "scratch", pinned=False, parent=t)
    q = store.post_item(sid, "question", "which?", pinned=True)
    store.db.execute("UPDATE items SET parent_ref = ? WHERE ref = ?", (t, q))
    assert store.delete_children(sid, t) == [scratch], "the unpinned doc goes, the pinned doc and question stay"
    assert [standing(store.item(sid, r)) for r in (kept, q)] == ["active", "active"]


@pytest.mark.parametrize("columns, kind, expected, desc", [
    ("", "doc", True, "a doc inserted by older code, pinned not written: pinned by its kind"),
    ("", "task", False, "a task the same way: not"),
    (", pinned", "doc", False, "a doc written unpinned stays so"),
])
def test_a_row_inserted_without_pinned_takes_its_kinds_default(store, sid, columns, kind, expected, desc):
    values = ", 0" if columns else ""
    store.db.execute(f"""INSERT INTO items (session_id, ref, kind, title, body, status, created_at, updated_at{columns})
                         VALUES (?, 'X1', ?, 't', 'b', 'new', '2026-10-19T09:00:00+00:00',
                         '2026-10-19T09:00:00+00:00'{values})""", (sid, kind))
    assert store.item(sid, "X1")["pinned"] == expected, desc


@pytest.mark.parametrize("dismissed, pinned, desc", [
    ("done", True, "dismissed and pinned: finished all the same, so Ctrl+R reopens it"),
    ("done", False, "dismissed, not pinned: finished"),
    ("deleted", True, "deleted with its parent, then pinned: finished"),
])
def test_a_pin_leaves_finished_as_it_was(store, sid, dismissed, pinned, desc):
    ref = store.post_item(sid, "task", "t", status="done", pinned=pinned)
    store.db.execute("UPDATE items SET dismissed = ? WHERE ref = ?", (dismissed, ref))
    assert standing(store.item(sid, ref)) == "finished", desc


# the inbox

def test_the_pin_glyphs_widths():
    assert (cell_len(PIN), cell_len(UNPINNED)) == (2, 1), "📌 two cells, as the terminal draws it; the dot one"


@pytest.mark.anyio
@pytest.mark.parametrize("how, desc", [
    ("click", "a click on the pin column"),
    ("p", "P in the item list"),
])
async def test_the_pin_column_toggles(store, sid, tmp_path, how, desc):
    t = post(store, sid, tmp_path, "task", "t")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        table = app.items_table
        assert str(table.get_row(f"{sid}|{t}")[0]) == UNPINNED, desc
        selected, screen = app.selected, app.screen
        for expected in (True, False):   # pinned, then its pin cleared
            if how == "click":
                await pilot.click(table, offset=(header_x(table, "pin"), table.header_height))
            else:
                table.focus()
                await pilot.press("alt+p")
            await pilot.pause()
            assert bool(store.item(sid, t)["pinned"]) == expected, desc
            assert str(table.get_row(f"{sid}|{t}")[0]) == (PIN if expected else UNPINNED), desc
            assert (app.selected, app.screen) == (selected, screen), f"{desc}: nothing selected or opened"


@pytest.mark.anyio
async def test_ctrl_a_never_marks_a_pinned_row(store, sid, tmp_path):
    done = post(store, sid, tmp_path, "task", "done", status="done")
    kept = post(store, sid, tmp_path, "task", "kept", status="done", pinned=True)
    doc = post(store, sid, tmp_path, "doc", "doc")
    scratch = post(store, sid, tmp_path, "doc", "scratch", pinned=False)
    for ref in (doc, scratch):
        store.mark_seen(sid, ref)   # read: settled, as a finished row would be
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.items_table.focus()
        await pilot.press("ctrl+a")
        await pilot.pause()
        assert app.items_table.marked == {f"{sid}|{done}", f"{sid}|{scratch}"}, f"never {kept} or {doc}"


@pytest.mark.anyio
@pytest.mark.parametrize("pinned, said, desc", [
    (["doc"], "dismissed T1; 1 pinned kept", "a task and a pinned doc: the doc kept, and said"),
    (["doc", "task"], "dismissed T1; 2 pinned kept", "a pinned task kept too"),
])
async def test_a_multi_select_delete_skips_pinned_rows_and_says_so(store, sid, tmp_path, pinned, said, desc):
    t = post(store, sid, tmp_path, "task", "tidy", status="done")
    refs = [post(store, sid, tmp_path, kind, f"p{n}", status="done", pinned=True) for n, kind in enumerate(pinned)]
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.items_table.focus()
        app.items_table.set_marks({f"{sid}|{r}" for r in [t, *refs]})
        await pilot.pause()
        await pilot.press("delete")
        await pilot.pause()
        assert any(n.startswith(said) for n in notices(app)), f"{desc}: {notices(app)}"
        assert all(store.item(sid, r)["dismissed"] is None and store.item(sid, r)["pinned"] for r in refs), desc
        assert store.item(sid, t)["dismissed"] == "done", desc


@pytest.mark.anyio
@pytest.mark.parametrize("kind, pinned, comment, key, prompt, gone, desc", [
    ("doc", None, False, "enter", "Delete pinned M1? The file stays on disk.", False, "Enter takes the default, No"),
    ("doc", None, False, "y", "Delete pinned M1? The file stays on disk.", True, "Y deletes it"),
    ("doc", None, True, "n", "Delete pinned M1? The file stays on disk. Your comment", False, "awaiting a reply: said"),
    ("task", True, False, "y", "Delete pinned T1?", True, "a pinned task asks too"),
    ("doc", False, False, None, None, True, "an unpinned doc goes at once, unasked"),
])
async def test_delete_on_a_pinned_item_asks_first(store, sid, tmp_path, kind, pinned, comment, key, prompt, gone, desc):
    ref = post(store, sid, tmp_path, kind, "it", status="done", pinned=pinned)
    if comment:
        store.send(sid, "why this?", ref)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.items_table.focus()
        app.items_table.move_cursor(row=app.items_table.get_row_index(f"{sid}|{ref}"))
        await pilot.pause()
        await pilot.press("delete")
        await pilot.pause()
        assert isinstance(app.screen, Confirm) == (prompt is not None), desc
        if prompt:
            assert app.screen.prompt.startswith(prompt), f"{desc}: {app.screen.prompt}"
            await pilot.press(key)
            await pilot.pause()
        item = store.item(sid, ref)
        assert (standing(item) == "finished", bool(item["pinned"])) == (gone, not gone and pinned is not False), desc
        if kind == "doc":
            assert (tmp_path / "it.md").exists(), f"{desc}: the file stays"


@pytest.mark.anyio
async def test_f_never_hides_a_pinned_item(store, sid, tmp_path):
    kept = post(store, sid, tmp_path, "task", "kept", status="done", pinned=True)
    gone = post(store, sid, tmp_path, "task", "gone", status="done")
    store.dismiss(sid, kept)
    store.dismiss(sid, gone)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert set(app.items_table.rows) == {f"{sid}|{kept}"}, "the pinned one shows with F off"


@pytest.mark.anyio
@pytest.mark.parametrize("key, gone, desc", [
    ("n", False, "No: the parent and its child kept, the pin too"),
    ("y", True, "Yes: both gone, the pin cleared"),
])
async def test_delete_on_a_pinned_parent_asks_once(store, sid, key, gone, desc):
    t = store.post_item(sid, "task", "build it", status="done", pinned=True)
    q = store.post_item(sid, "question", "which?")
    store.db.execute("UPDATE items SET parent_ref = ? WHERE ref = ?", (t, q))
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.items_table.focus()
        app.items_table.move_cursor(row=app.items_table.get_row_index(f"{sid}|{t}"))
        await pilot.pause()
        await pilot.press("delete")
        await pilot.pause()
        assert app.screen.prompt.startswith(f"Delete pinned {t} (build it) and its 1 child?"), app.screen.prompt
        await pilot.press(key)
        await pilot.pause()
        assert not isinstance(app.screen, Confirm), f"{desc}: one dialog, not two"
        assert [standing(store.item(sid, r)) == "finished" for r in (t, q)] == [gone, gone], desc
        assert bool(store.item(sid, t)["pinned"]) == (not gone), desc


@pytest.mark.anyio
async def test_a_pinned_finished_item_reopens_and_is_not_deleted_again(store, sid):
    t = store.post_item(sid, "task", "kept", status="done", pinned=True)
    store.dismiss(sid, t)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.items_table.focus()
        app.items_table.move_cursor(row=app.items_table.get_row_index(f"{sid}|{t}"))
        await pilot.pause()
        await pilot.press("delete")
        await pilot.pause()
        assert not isinstance(app.screen, Confirm) and any("already" in n for n in notices(app)), notices(app)
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert standing(store.item(sid, t)) == "settled", "Ctrl+R reopens it"
