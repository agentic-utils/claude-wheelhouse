"""A doc's editor (tui.DocEditor, .plan/modules.md, "Viewing and editing a doc", Q106): opened
by a click on the rendered doc, in Detail and full screen, re-rendered on Ctrl+Enter, kept in
step with the viewer both ways, and closed on Esc. Saving is stage 8's."""

import pytest

from claude_wheelhouse import tui
from claude_wheelhouse.tui import DocEditor, LeaveEdits, WheelhouseApp
from test_tui import item_list_rows

DOC = "# The plan\n\nBuild it.\n\n- one\n- two\n\n```py\nx = 1\n```\n\n| alpha | beta |\n|---|---|\n| 1 | 2 |\n"
# a doc taller than the viewer: forty sections of a heading and a paragraph, four lines each
LONG = "".join(f"## Section {i}\n\nProse for section {i}.\n\n" for i in range(40))


@pytest.fixture
def doc(store, sid, tmp_path):
    """A doc item of the session's on DOC, and its file."""
    path = tmp_path / "plan.md"
    path.write_text(DOC)
    return store.post_doc(sid, "the plan", str(path)), path


async def shown(app, pilot, ref: str, full: bool = False):
    """The doc item highlighted, in Detail or full screen: its viewer and editor."""
    await pilot.pause()
    app.items_table.move_cursor(row=[r[1] for r in item_list_rows(app)].index(f"└ {ref}"))
    await pilot.pause()
    if full:
        app.items_table.focus()
        await pilot.press("enter")
        await pilot.pause()
        return app.screen.query_one("#docview", tui.Transcript), app.screen.query_one("#docview-editor", DocEditor)
    return app.detail, app.detail_editor


async def click(pilot, view, row: int) -> None:
    """A click on a row of the viewer, where it's scrolled to (the row in view)."""
    region = view.parent.scrollable_content_region
    await pilot.click(offset=(region.x + 2, region.y + row - round(view.parent.scroll_y)))
    await pilot.pause()
    await pilot.pause()


def row_with(view, text: str) -> int:
    texts = view._layout(view.size.width)[1]
    return next(y for y in range(view.doc_at[0], len(texts)) if text in texts[y])


@pytest.mark.anyio
@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize("first, line, desc", [
    ("The plan", 0, "a heading"),
    ("one", 4, "a list"),
    ("x = 1", 7, "a code fence"),
    ("alpha", 11, "a table"),
])
async def test_a_click_puts_the_cursor_on_its_blocks_first_line(store, sid, doc, full, first, line, desc):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        view, editor = await shown(app, pilot, doc[0], full)
        assert not editor.display, f"{desc}: closed until a click"
        await click(pilot, view, row_with(view, first))
        assert editor.display and editor.splitter.display, f"{desc}: open, under its splitter"
        assert editor.has_focus and editor.text == DOC, f"{desc}: focused, on the file's text"
        assert editor.cursor_location == (line, 0), f"{desc}: the cursor on its first line"
        assert abs(editor.region.height - 0.4 * editor.parent.size.height) <= 2, f"{desc}: 40% of the viewer's room"


@pytest.mark.anyio
async def test_a_row_between_blocks_takes_the_nearest(store, sid, doc):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        view, editor = await shown(app, pilot, doc[0])
        await click(pilot, view, row_with(view, "Build it.") + 1)   # the gap under the paragraph
        assert editor.cursor_location == (4, 0), "the gap reads as the space above the list"
        await click(pilot, view, row_with(view, "The plan"))
        assert editor.cursor_location == (0, 0), "a click while it's open moves it there"


@pytest.mark.anyio
@pytest.mark.parametrize("immediate", [False, True])
async def test_ctrl_enter_re_renders_and_saving_comes_next(store, sid, doc, immediate):
    if immediate:
        store.set_mode(sid, "immediate")
    store.queue(sid, "an answer")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        view, editor = await shown(app, pilot, doc[0])
        await click(pilot, view, row_with(view, "Build it."))
        notes = []
        app.notify = lambda message, **_: notes.append(message)
        editor.insert("Rebuild it. ")
        await pilot.pause()
        assert app.query_one("#detail-pane").border_title == "Detail · edited", "the title says edited"
        assert "Rebuild" not in "\n".join(view._layout(view.size.width)[1]), "nothing re-renders as it's typed"
        await pilot.press("ctrl+j")
        await pilot.pause()
        assert "Rebuild it. Build it." in "\n".join(view._layout(view.size.width)[1]), "Ctrl+Enter renders the buffer"
        app.refresh_data()
        await pilot.pause()
        assert "Rebuild" in app._detail_text, "a refresh keeps rendering the buffer, not the file"
        assert notes == ([tui.SAVING_NEXT] if immediate else []), "Immediate saves on Ctrl+Enter, here saying so"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert notes[-1] == tui.SAVING_NEXT, "Ctrl+S saves the doc, here saying saving comes next"
        assert doc[1].read_text() == DOC, "the file untouched"
        assert store.sessions()[0]["drafts"] == 1, "Ctrl+S in the editor doesn't send the session's queue"


@pytest.mark.anyio
@pytest.mark.parametrize("full", [False, True])
async def test_esc_closes_asking_first_with_edits(store, sid, doc, full):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        view, editor = await shown(app, pilot, doc[0], full)
        await click(pilot, view, row_with(view, "Build it."))
        await pilot.press("escape")
        await pilot.pause()
        assert not editor.display and view.has_focus, "no edits: Esc closes it, the viewer focused"
        await click(pilot, view, row_with(view, "Build it."))
        editor.insert("Not yet. ")
        await pilot.press("ctrl+j")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, LeaveEdits), "edits: Esc asks first"
        await pilot.press("escape")
        await pilot.pause()
        assert editor.display and editor.text.startswith("# The plan\n\nNot yet."), "Keep editing keeps them"
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert not editor.display and str(doc[1]) in app.doc_edits, "Close, the default, keeps the edits"
        title = "#detail-pane" if not full else "#docview-scroll"
        assert app.screen.query_one(title).border_title.endswith("edited"), "and the title says so"
        await click(pilot, view, row_with(view, "Build it."))
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("alt+d")
        await pilot.pause()
        assert not editor.display and editor.key is None, "Discard closes it"
        assert "Not yet" not in "\n".join(view._layout(view.size.width)[1]), "the file shown again"
        if full:
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, tui.DocView), "closed, Esc leaves as before"


async def edited(app, pilot, view, editor) -> None:
    """The editor opened on Build it., edited there, and the edit rendered (Ctrl+Enter)."""
    await click(pilot, view, row_with(view, "Build it."))
    editor.insert("Kept. ")
    await pilot.press("ctrl+j")
    await pilot.pause()


@pytest.mark.anyio
async def test_edits_outlive_another_doc_shown_and_come_back(store, sid, doc):
    q = store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        view, editor = await shown(app, pilot, doc[0])
        await edited(app, pilot, view, editor)
        app.items_table.move_cursor(row=[r[1] for r in item_list_rows(app)].index(q))
        await pilot.pause()
        assert not editor.display, "another item: the editor closes"
        assert app.query_one("#detail-pane").border_title == "Detail", "the title back"
        assert app.doc_edits[str(doc[1])].text.startswith("# The plan\n\nKept. "), "its buffer kept, by its path"
        view, editor = await shown(app, pilot, doc[0])
        assert "Kept. Build it." in "\n".join(view._layout(view.size.width)[1]), "shown from the buffer meanwhile"
        await click(pilot, view, row_with(view, "x = 1"))
        assert editor.text.startswith("# The plan\n\nKept. ") and editor.cursor_location == (7, 0), \
            "reopened: the buffer back, the cursor on the clicked block's line"
        assert app.query_one("#detail-pane").border_title == "Detail · edited", "and edited"
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("alt+d")
        await pilot.pause()
        assert str(doc[1]) not in app.doc_edits, "Discard drops the buffer"
        assert "Kept" not in "\n".join(view._layout(view.size.width)[1]), "the file shown again"
        assert doc[1].read_text() == DOC, "the file untouched"


@pytest.mark.anyio
async def test_edits_in_detail_show_full_screen(store, sid, doc):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        view, editor = await shown(app, pilot, doc[0])
        await edited(app, pilot, view, editor)
        full, full_editor = await shown(app, pilot, doc[0], full=True)
        assert "Kept. Build it." in app.screen.text, "full screen renders the buffer, not the file"
        await click(pilot, full, row_with(full, "The plan"))
        assert full_editor.text == editor.text, "its editor opens on the same buffer"
        full_editor.insert("More. ")
        await pilot.pause()
        for key in ("escape", "enter", "escape"):   # asked, Close keeping them, and out
            await pilot.press(key)
            await pilot.pause()
        assert not isinstance(app.screen, tui.DocView), "full screen left with its edits kept"
        app.refresh_data()
        await pilot.pause()
        assert "More. " in editor.text, "back in Detail, its editor takes up the edits made full screen"


@pytest.fixture
def long_doc(store, sid, tmp_path):
    path = tmp_path / "long.md"
    path.write_text(LONG)
    return store.post_doc(sid, "the long one", str(path))


def scrolls(widget) -> list[float]:
    """Each scroll_y a widget takes from now on."""
    seen = []
    widget.watch(widget, "scroll_y", lambda y: seen.append(y), init=False)
    return seen


@pytest.mark.anyio
async def test_the_viewer_scrolled_moves_the_editor_not_its_cursor(store, sid, long_doc):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        view, editor = await shown(app, pilot, long_doc)
        await click(pilot, view, row_with(view, "Section 1"))
        cursor = editor.cursor_location
        seen = scrolls(editor), scrolls(view.parent)
        view.parent.scroll_to(y=row_with(view, "Section 20"), animate=False, immediate=True)
        await pilot.pause()
        await pilot.pause()
        assert editor.cursor_location == cursor, "the cursor stays where it was"
        middle = view.line_at(round(view.parent.scroll_y) + view.parent.scrollable_content_region.height // 2)
        at = editor.wrapped_document.location_to_offset((middle, 0)).y - round(editor.scroll_y)
        assert abs(at - editor.scrollable_content_region.height // 2) <= 1, "the middle block's source at its middle"
        assert (len(seen[0]), len(seen[1])) == (1, 1), f"one scroll on each side: {seen}"


@pytest.mark.anyio
async def test_the_cursor_into_another_block_scrolls_the_viewer_the_least(store, sid, long_doc):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        view, editor = await shown(app, pilot, long_doc)
        await click(pilot, view, row_with(view, "Section 0"))
        top = view.parent.scroll_y
        await pilot.press("down")
        await pilot.pause()
        assert view.parent.scroll_y == top, "within one block, or into one in view, nothing moves"
        bottom = round(view.parent.scroll_y) + view.parent.scrollable_content_region.height
        hidden = next(i for i in range(40) if row_with(view, f"Section {i}") >= bottom)
        seen = scrolls(editor), scrolls(view.parent)
        editor.move_cursor((4 * hidden, 0))
        await pilot.pause()
        await pilot.pause()
        rows = view.block_rows(4 * hidden)
        assert round(view.parent.scroll_y) + view.parent.scrollable_content_region.height == rows[1], \
            "the viewer scrolled just enough to show the block, at its foot"
        assert len(seen[1]) == 1, f"one scroll of the viewer, not followed back: {seen}"


@pytest.mark.anyio
async def test_a_render_keeps_the_cursors_block_on_its_row(store, sid, long_doc):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        view, editor = await shown(app, pilot, long_doc)
        view.parent.scroll_to(y=row_with(view, "Section 10"), animate=False, immediate=True)
        await pilot.pause()
        await click(pilot, view, row_with(view, "Section 12"))
        before = view.block_rows(48)[0] - round(view.parent.scroll_y)
        editor.move_cursor((0, 0))
        editor.insert("## A new first section\n\nWith prose.\n\n")
        editor.move_cursor((52, 0))   # Section 12's heading, four lines down
        await pilot.press("ctrl+j")
        await pilot.pause()
        await pilot.pause()
        assert view.block_rows(52)[0] - round(view.parent.scroll_y) == before, "the block on its row, the rows above grown"


@pytest.mark.anyio
async def test_a_click_on_a_block_the_edits_took_out_keeps_the_cursor(store, sid, doc):
    q = store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        view, editor = await shown(app, pilot, doc[0])
        await edited(app, pilot, view, editor)
        editor.delete((4, 0), (6, 0))   # the list, not rendered again
        editor.move_cursor((2, 3))
        await pilot.pause()
        app.items_table.move_cursor(row=[r[1] for r in item_list_rows(app)].index(q))
        await pilot.pause()
        view, editor = await shown(app, pilot, doc[0])
        await click(pilot, view, row_with(view, "one"))
        assert editor.cursor_location == (2, 3), "the list gone from the buffer: the cursor where it was"


@pytest.mark.anyio
async def test_discard_full_screen_clears_detail_at_once(store, sid, doc):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        view, editor = await shown(app, pilot, doc[0])
        await edited(app, pilot, view, editor)
        full, full_editor = await shown(app, pilot, doc[0], full=True)
        await click(pilot, full, row_with(full, "The plan"))
        for key in ("escape", "d"):
            await pilot.press(key)
            await pilot.pause()
        assert not editor.display and editor.key is None, "Detail's editor on it closed too"
        assert app.screen_stack[0].query_one("#detail-pane").border_title == "Detail", "its title at once"
        assert "Kept" not in "\n".join(view._layout(view.size.width)[1]), "and its viewer the file's"
        assert str(doc[1]) not in app.doc_edits


@pytest.mark.anyio
async def test_the_file_changing_on_disk_keeps_the_edits_and_says_so(store, sid, doc):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        notes = []
        app.notify = lambda message, **_: notes.append(message)
        view, editor = await shown(app, pilot, doc[0])
        await edited(app, pilot, view, editor)
        doc[1].write_text(DOC.replace("Build it.", "Changed on disk."))
        for _ in range(2):
            app.refresh_data()
            await pilot.pause()
        assert editor.text.startswith("# The plan\n\nKept. Build it."), "the buffer kept"
        assert "Kept. Build it." in app._detail_text, "and shown"
        assert notes == ["plan.md changed on disk since you started editing; your edits are kept"], f"once: {notes}"
        assert app.query_one("#detail-pane").border_title == "Detail · edited · file changed", "and in the title"
        assert app.doc_edits[str(doc[1])].changed, "noted for the save to come"


@pytest.mark.anyio
async def test_one_file_has_one_buffer_however_reached(store, sid, doc, tmp_path):
    link = tmp_path / "link.md"
    link.symlink_to(doc[1])
    other = store.post_doc(sid, "the plan again", str(link))
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        view, editor = await shown(app, pilot, doc[0])
        await edited(app, pilot, view, editor)
        view, editor = await shown(app, pilot, other)
        assert list(app.doc_edits) == [str(doc[1].resolve())], "kept by the real path"
        assert "Kept. Build it." in app._detail_text, "the link's doc shows the same buffer"


@pytest.mark.anyio
async def test_the_file_gone_under_kept_edits_says_so(store, sid, doc):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        notes = []
        app.notify = lambda message, **_: notes.append(message)
        view, editor = await shown(app, pilot, doc[0])
        await edited(app, pilot, view, editor)
        doc[1].unlink()
        for _ in range(2):
            app.refresh_data()
            await pilot.pause()
        assert notes == ["plan.md has gone from disk since you started editing; your edits are kept"], f"once: {notes}"
        assert app.query_one("#detail-pane").border_title == "Detail · edited · file gone", "and in the title"
        assert "Kept. Build it." in app._detail_text, "the edits still shown"


def test_a_refresh_before_the_editor_mounts_or_after_it_goes_paints_nothing_of_it():
    editor = DocEditor("#detail", "#detail-pane", lambda: False, lambda: None)
    blocks = [("doc", "# x")]
    assert editor.shown_in(blocks, "/x.md") == blocks, "unmounted: the blocks as they are"
    editor.paint_title()   # no screen, no viewer: nothing, and no error


@pytest.mark.anyio
@pytest.mark.parametrize("back, title, desc", [
    (None, "Detail · edited · file gone", "gone, and not back"),
    (DOC, "Detail · edited", "back unchanged (an atomic save, a checkout): neither"),
    (DOC.replace("Build it.", "Changed."), "Detail · edited · file changed", "back changed: a change"),
])
async def test_a_file_gone_and_back_under_kept_edits(store, sid, doc, back, title, desc):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        view, editor = await shown(app, pilot, doc[0])
        await edited(app, pilot, view, editor)
        doc[1].unlink()
        app.refresh_data()
        await pilot.pause()
        if back is not None:
            doc[1].write_text(back)
        for _ in range(2):
            app.refresh_data()
            await pilot.pause()
        assert app.query_one("#detail-pane").border_title == title, f"{desc}: {app.query_one('#detail-pane').border_title}"
