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
        await pilot.press("d")
        await pilot.pause()
        assert not editor.display and editor.key is None, "Discard closes it"
        assert "Not yet" not in "\n".join(view._layout(view.size.width)[1]), "the file shown again"
        if full:
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, tui.DocView), "closed, Esc leaves as before"


@pytest.mark.anyio
async def test_the_shown_doc_changing_closes_the_editor(store, sid, doc):
    q = store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        view, editor = await shown(app, pilot, doc[0])
        await click(pilot, view, row_with(view, "Build it."))
        editor.insert("Dropped. ")
        notes = []
        app.notify = lambda message, **_: notes.append(message)
        app.items_table.move_cursor(row=[r[1] for r in item_list_rows(app)].index(q))
        await pilot.pause()
        assert not editor.display and "dropped" in notes[-1], "another item: closed, the edits' loss said"
        assert app.query_one("#detail-pane").border_title == "Detail", "the title back"


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
