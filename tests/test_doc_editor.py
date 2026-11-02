"""A doc's editor (tui.DocEditor, .plan/modules.md, "Viewing and editing a doc", Q106): opened
by a click on the rendered doc, in Detail and full screen, re-rendered on Ctrl+Enter, kept in
step with the viewer both ways, closed on Esc, and saved with Ctrl+S through the client
(client.write_doc), asking before edits are left or a changed file overwritten."""

import os

import pytest

from claude_wheelhouse import tui
from claude_wheelhouse.tui import DocEditor, LeaveEdits, SaveConflict, SaveFailed, SaveGone, WheelhouseApp
from test_tui import UNDONE_ACROSS_THE_SCROLLBAR, item_list_rows, item_row, undone_across_the_scrollbar

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
    app.items_table.move_cursor(row=item_row(app, f"└ {ref}"))
    await pilot.pause()
    if full:
        app.items_table.focus()
        await pressed(pilot, "enter", lambda: isinstance(app.screen, tui.DocView))
        return app.screen.query_one("#docview", tui.Transcript), app.screen.query_one("#docview-editor", DocEditor)
    return app.detail, app.detail_editor


async def click(pilot, view, row: int) -> None:
    """A click on a row of the viewer, where it's scrolled to (the row in view)."""
    region = view.parent.scrollable_content_region
    await pilot.click(offset=(region.x + 2, region.y + row - round(view.parent.scroll_y)))
    await pilot.pause()
    await pilot.pause()


async def pressed(pilot, key: str, done) -> None:
    """A key pressed and waited for until done() holds (what it does: a prompt up, a file
    written), then one pause more, for what it queued (a prompt's answer, a repaint). The
    driver hands a key to the app from its thread, and pilot.pause() takes a loop starved
    of CPU for an idle one, so on a loaded run a count of pauses can end before the key
    lands. Bounded: one that never does fails the test's own assert after."""
    await pilot.press(key)
    await until(pilot, done)


async def until(pilot, done) -> None:
    """Paused until done() holds, bounded, then once more (pressed): what the app does
    late, as a highlight's handling, waited for by what it does, not a count of pauses."""
    for _ in range(300):
        if done():
            break
        await pilot.pause(0.01)
    await pilot.pause()


def text_of(view) -> str:
    return "\n".join(view._layout(view.size.width)[1])


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
@pytest.mark.both
@pytest.mark.parametrize("immediate", [False, True])
async def test_ctrl_enter_re_renders_and_ctrl_s_saves(store, sid, doc, immediate):
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
        assert app.query_one("#detail-pane").border_title == "Detail · edited · Ctrl+S saves", "the title says so"
        assert "Rebuild" not in "\n".join(view._layout(view.size.width)[1]), "nothing re-renders as it's typed"
        await pressed(pilot, "ctrl+j", lambda: "Rebuild it. Build it." in text_of(view))
        assert "Rebuild it. Build it." in "\n".join(view._layout(view.size.width)[1]), "Ctrl+Enter renders the buffer"
        app.refresh_data()
        await pilot.pause()
        assert "Rebuild" in app._detail_text, "a refresh keeps rendering the buffer"
        saved = DOC.replace("Build it.", "Rebuild it. Build it.")
        assert doc[1].read_text() == (saved if immediate else DOC), "Immediate saves on Ctrl+Enter, Queued doesn't"
        editor.insert("Then ")
        saved = DOC.replace("Build it.", "Rebuild it. Then Build it.")
        await pressed(pilot, "ctrl+s", lambda: doc[1].read_text() == saved)
        assert doc[1].read_text() == saved and notes[-1] == "Saved plan.md", "Ctrl+S saves the buffer"
        assert app.query_one("#detail-pane").border_title == "Detail", "nothing left to save"
        assert "Then Build it." in app._detail_text and editor.display, "Detail shows what was saved, the editor open"
        app.refresh_data()
        await pilot.pause()
        assert editor.text == saved and str(doc[1]) not in app.doc_edits, "the saved text its base"
        assert store.sessions()[0]["drafts"] == 1, "Ctrl+S in the editor doesn't send the session's queue"
        editor.insert("Again. ")
        await pressed(pilot, "ctrl+s", lambda: doc[1].read_text().startswith("# The plan\n\nRebuild it. Then Again. "))
        assert doc[1].read_text().startswith("# The plan\n\nRebuild it. Then Again. "), "a second save, from the new base"


@pytest.mark.anyio
@pytest.mark.parametrize("full", [False, True])
async def test_esc_closes_asking_first_with_edits(store, sid, doc, full):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        view, editor = await shown(app, pilot, doc[0], full)
        await click(pilot, view, row_with(view, "Build it."))
        await pressed(pilot, "escape", lambda: not editor.display)
        assert not editor.display and view.has_focus, "no edits: Esc closes it, the viewer focused"
        await click(pilot, view, row_with(view, "Build it."))
        editor.insert("Saved. ")
        await pressed(pilot, "escape", lambda: isinstance(app.screen, LeaveEdits))
        assert isinstance(app.screen, LeaveEdits), "edits: Esc asks first"
        await pressed(pilot, "escape", lambda: not isinstance(app.screen, LeaveEdits))
        assert editor.display and editor.text.startswith("# The plan\n\nSaved."), "Keep editing keeps them"
        await pressed(pilot, "escape", lambda: isinstance(app.screen, LeaveEdits))
        await pressed(pilot, "enter", lambda: doc[1].read_text().startswith("# The plan\n\nSaved. Build it."))
        assert not editor.display and doc[1].read_text().startswith("# The plan\n\nSaved. Build it."), \
            "Save, the default, saves and closes"
        assert str(doc[1]) not in app.doc_edits and "Saved. Build it." in "\n".join(view._layout(view.size.width)[1])
        await click(pilot, view, row_with(view, "Build it."))
        editor.insert("Not yet. ")
        await pressed(pilot, "escape", lambda: isinstance(app.screen, LeaveEdits))
        await pressed(pilot, "alt+d", lambda: editor.key is None)
        assert not editor.display and editor.key is None, "Discard closes it"
        assert "Not yet" not in "\n".join(view._layout(view.size.width)[1]), "the file shown again"
        assert "Not yet" not in doc[1].read_text()
        if full:
            await pressed(pilot, "escape", lambda: not isinstance(app.screen, tui.DocView))
            assert not isinstance(app.screen, tui.DocView), "closed, Esc leaves as before"


async def edited(app, pilot, view, editor) -> None:
    """The editor opened on Build it., edited there, and the edit rendered (Ctrl+Enter)."""
    await click(pilot, view, row_with(view, "Build it."))
    editor.insert("Kept. ")
    await pressed(pilot, "ctrl+j", lambda: "Kept. Build it." in text_of(view))


@pytest.mark.anyio
@pytest.mark.parametrize("choice, file, selected, desc", [
    ("alt+s", "Kept. Build it.", "q", "Save: saved, and the selection where it went"),
    ("alt+d", "Build it.", "q", "Discard: dropped, the file as it was"),
    ("escape", "Build it.", "doc", "Keep editing: back on the doc, its editor open where it was"),
])
async def test_moving_off_a_doc_with_edits_asks(store, sid, doc, choice, file, selected, desc):
    q = store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        view, editor = await shown(app, pilot, doc[0])
        await edited(app, pilot, view, editor)
        editor.move_cursor((2, 3))
        await pilot.pause()
        app.items_table.move_cursor(row=item_row(app, q))
        await until(pilot, lambda: isinstance(app.screen, LeaveEdits))   # its highlight handled late: asked
        assert isinstance(app.screen, LeaveEdits) and not editor.display, f"{desc}: asked, the editor closed"
        await pressed(pilot, choice, lambda: str(doc[1]) not in app.doc_edits   # saved or dropped, or open again
                      or editor.display and editor.cursor_location == (2, 3))   # with its cursor put back (place)
        assert doc[1].read_text() == DOC.replace("Build it.", file), desc
        assert app.selected == (sid, q if selected == "q" else doc[0]), f"{desc}: {app.selected}"
        if selected == "doc":
            assert editor.display and editor.has_focus and editor.cursor_location == (2, 3), f"{desc}: open again"
            assert editor.text.startswith("# The plan\n\nKept. ") and str(doc[1]) in app.doc_edits, desc
            assert app.query_one("#detail-pane").border_title == "Detail · edited · Ctrl+S saves", desc
        else:
            assert str(doc[1]) not in app.doc_edits and app.query_one("#detail-pane").border_title == "Detail", desc


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
        await pressed(pilot, "escape", lambda: isinstance(app.screen, LeaveEdits))   # asked,
        await pressed(pilot, "enter", lambda: "More. " in doc[1].read_text())   # Save,
        await pressed(pilot, "escape", lambda: not isinstance(app.screen, tui.DocView))   # and out
        assert not isinstance(app.screen, tui.DocView), "full screen left, its edits saved"
        app.refresh_data()
        await pilot.pause()
        assert "More. " in editor.text and editor.text == doc[1].read_text(), \
            "back in Detail, its editor has the text saved full screen"
        assert app.query_one("#detail-pane").border_title == "Detail", "and nothing to save"


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
        await pressed(pilot, "down", lambda: editor.cursor_location[0] == 1)
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
        # rendered, then anchored after the refresh (or once laid out off the loop), which clears anchor
        await pressed(pilot, "ctrl+j", lambda: "A new first section" in text_of(view) and editor.anchor is None)
        assert view.block_rows(52)[0] - round(view.parent.scroll_y) == before, "the block on its row, the rows above grown"


@pytest.mark.anyio
async def test_a_click_on_a_block_the_edits_took_out_keeps_the_cursor(store, sid, doc, clock):
    q = store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        view, editor = await shown(app, pilot, doc[0])
        await edited(app, pilot, view, editor)
        editor.delete((4, 0), (6, 0))   # the list, not rendered again
        editor.move_cursor((2, 3))
        await pilot.pause()
        app.items_table.move_cursor(row=item_row(app, q))
        await until(pilot, lambda: isinstance(app.screen, LeaveEdits))   # its highlight handled late: asked
        await pressed(pilot, "escape", lambda: editor.display and editor.cursor_location == (2, 3))   # Keep editing
        assert editor.display and editor.cursor_location == (2, 3), "open again where it was"
        assert not app.answer.display, "laid out for it at once, no tick needed: the box gone from under it"
        view.parent.scroll_to(y=row_with(view, "one"), animate=False, immediate=True)   # in the viewer's smaller room
        await pilot.pause()
        app.CLICK_CHAIN_TIME_THRESHOLD = 0   # this click, soon after the first, not taken as its double-click
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
        await pressed(pilot, "escape", lambda: isinstance(app.screen, LeaveEdits))
        await pressed(pilot, "alt+d", lambda: editor.key is None)
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
        assert app.query_one("#detail-pane").border_title == "Detail · edited · file changed · Ctrl+S saves", \
            "and in the title"
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
        assert app.query_one("#detail-pane").border_title == "Detail · edited · file gone · Ctrl+S saves", \
            "and in the title"
        assert "Kept. Build it." in app._detail_text, "the edits still shown"


def test_a_refresh_before_the_editor_mounts_or_after_it_goes_paints_nothing_of_it():
    editor = DocEditor("#detail", "#detail-pane", lambda: False, lambda: None)
    blocks = [("doc", "# x")]
    assert editor.shown_in(blocks, "/x.md") == blocks, "unmounted: the blocks as they are"
    editor.paint_title()   # no screen, no viewer: nothing, and no error


@pytest.mark.anyio
@pytest.mark.parametrize("back, title, desc", [
    (None, "Detail · edited · file gone · Ctrl+S saves", "gone, and not back"),
    (DOC, "Detail · edited · Ctrl+S saves", "back unchanged (an atomic save, a checkout): neither"),
    (DOC.replace("Build it.", "Changed."), "Detail · edited · file changed · Ctrl+S saves", "back changed: a change"),
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


ON_DISK = DOC.replace("Build it.", "Changed on disk.")


@pytest.mark.anyio
@pytest.mark.parametrize("choice, file, buffer, open_, desc", [
    ("alt+o", "Kept. Build it.", "Kept. Build it.", True, "Overwrite: the edits written over the change"),
    ("alt+r", "Changed on disk.", "Changed on disk.", True, "Reload: the file's text in the buffer, the edits lost"),
    ("escape", "Changed on disk.", "Kept. Build it.", True, "Cancel: nothing written, the edits kept"),
])
async def test_a_save_over_a_file_changed_on_disk_asks(store, sid, doc, choice, file, buffer, open_, desc):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        view, editor = await shown(app, pilot, doc[0])
        await edited(app, pilot, view, editor)
        doc[1].write_text(ON_DISK)
        await pressed(pilot, "ctrl+s", lambda: isinstance(app.screen, SaveConflict))
        assert isinstance(app.screen, SaveConflict), f"{desc}: asked"
        assert "plan.md has changed on disk" in str(app.screen.query_one(".dialog-prompt").render()), desc
        answered = (lambda: not isinstance(app.screen, SaveConflict)) if choice == "escape" else \
            (lambda: doc[1].read_text() == DOC.replace("Build it.", file) and editor.text == DOC.replace("Build it.", buffer))
        await pressed(pilot, choice, answered)   # what the answer does, which runs after the prompt has gone
        assert doc[1].read_text() == DOC.replace("Build it.", file), desc
        assert editor.text == DOC.replace("Build it.", buffer) and editor.display == open_, desc
        assert (str(doc[1]) in app.doc_edits) == (choice == "escape"), f"{desc}: edits left only on Cancel"
        assert buffer in app._detail_text, f"{desc}: Detail shows the buffer"


@pytest.mark.anyio
@pytest.mark.parametrize("choice, desc", [("alt+w", "Write it anew"), ("escape", "Cancel")])
async def test_a_save_of_a_file_gone_offers_to_write_it_anew(store, sid, doc, choice, desc):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        view, editor = await shown(app, pilot, doc[0])
        await edited(app, pilot, view, editor)
        doc[1].unlink()
        await pressed(pilot, "ctrl+s", lambda: isinstance(app.screen, SaveGone))
        assert isinstance(app.screen, SaveGone), f"{desc}: asked"
        await pressed(pilot, choice, (lambda: not isinstance(app.screen, SaveGone)) if choice == "escape" else doc[1].exists)
        assert (doc[1].read_text() if doc[1].exists() else None) == (
            DOC.replace("Build it.", "Kept. Build it.") if choice == "alt+w" else None), desc


@pytest.mark.anyio
@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
async def test_a_save_the_filesystem_refuses_says_where_and_why(store, sid, doc):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        view, editor = await shown(app, pilot, doc[0])
        await edited(app, pilot, view, editor)
        doc[1].parent.chmod(0o555)
        try:
            await pressed(pilot, "ctrl+s", lambda: isinstance(app.screen, SaveFailed))
        finally:
            doc[1].parent.chmod(0o755)
        assert isinstance(app.screen, SaveFailed), "said in a prompt"
        said = str(app.screen.query_one(".dialog-prompt").render())
        assert said == f"plan.md wasn't saved: {doc[1]} can't be written: Permission denied", said
        await pressed(pilot, "escape", lambda: not isinstance(app.screen, SaveFailed))
        assert doc[1].read_text() == DOC and str(doc[1]) in app.doc_edits, "nothing written, the edits kept"
        assert app.query_one("#detail-pane").border_title == "Detail · edited · Ctrl+S saves"


@pytest.mark.anyio
async def test_a_save_outside_what_may_be_written_is_refused(store, sid, doc, monkeypatch):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        view, editor = await shown(app, pilot, doc[0])
        await edited(app, pilot, view, editor)
        monkeypatch.setattr(store, "doc_paths", lambda: [])   # the item's file no longer a doc's,
        app.client.granted.clear()   # nor granted to the editor that opened it (#293)
        await pressed(pilot, "ctrl+s", lambda: isinstance(app.screen, SaveFailed))
        assert isinstance(app.screen, SaveFailed)
        assert "is not under any doc root" in str(app.screen.query_one(".dialog-prompt").render())
        assert doc[1].read_text() == DOC


@pytest.mark.anyio
@pytest.mark.parametrize("trouble, answer, desc", [
    ("changed", "escape", "the file changed, the save cancelled"),
    ("readonly", "escape", "the save refused"),
])
async def test_a_save_from_moving_off_that_doesnt_land_goes_back_to_the_edits(store, sid, doc, trouble, answer, desc):
    if trouble == "readonly" and os.geteuid() == 0:
        pytest.skip("root writes anywhere")
    q = store.post_item(sid, "question", "which db?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        view, editor = await shown(app, pilot, doc[0])
        await edited(app, pilot, view, editor)
        if trouble == "changed":
            doc[1].write_text(ON_DISK)
        else:
            doc[1].chmod(0o444)
        app.items_table.move_cursor(row=item_row(app, q))
        await until(pilot, lambda: isinstance(app.screen, LeaveEdits))   # its highlight handled late: asked
        await pressed(pilot, "alt+s", lambda: isinstance(app.screen, (SaveConflict, SaveFailed)))
        assert isinstance(app.screen, SaveConflict if trouble == "changed" else SaveFailed), desc
        await pressed(pilot, answer, lambda: editor.display)
        assert app.selected == (sid, doc[0]) and editor.display, f"{desc}: back on the doc, editing"
        assert editor.text.startswith("# The plan\n\nKept. "), desc
        assert app.query_one("#detail-pane").border_title.startswith("Detail · edited"), desc


@pytest.mark.anyio
async def test_a_file_rewritten_unchanged_is_no_change_to_ask_about(store, sid, doc):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        view, editor = await shown(app, pilot, doc[0])
        await edited(app, pilot, view, editor)
        later = doc[1].stat().st_mtime_ns + 10 ** 9
        os.utime(doc[1], ns=(later, later))   # touched, its text the same
        app.refresh_data()
        await pilot.pause()
        await pressed(pilot, "ctrl+s", lambda: isinstance(app.screen, SaveConflict) or str(doc[1]) not in app.doc_edits)
        assert not isinstance(app.screen, SaveConflict), "no 409: the base's mtime taken up"
        assert doc[1].read_text().startswith("# The plan\n\nKept. ")


@pytest.mark.anyio
@pytest.mark.parametrize("trigger, edits, focus, desc", [
    ("compaction", True, "editor", "typing in the editor: half-typed edits never saved by the next Enter"),
    ("compaction", True, "viewer", "unsaved edits, the focus on the viewer: the editor stays open on them"),
    ("compaction", False, "editor", "a clean editor with the focus: left open"),
    ("launch", True, "viewer", "a launch, unsaved edits, the focus on the viewer (the editor's own: a text box's guard)"),
])
async def test_the_app_never_moves_the_selection_off_a_doc_being_edited(store, sid, doc, tmp_path, monkeypatch,
                                                                         trigger, edits, focus, desc):
    """#282 (#277 with #290): a compaction starting in another session, or a session just
    launched, moved the selection, closing Detail's editor; LeaveEdits' Save, the Enter default,
    then wrote half-typed text to the file. Neither selection nor focus moves while it's edited."""
    from claude_wheelhouse import liveness
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: "live")
    other = store.create_session(str(tmp_path), name="alpha", runner="sdk")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        view, editor = await shown(app, pilot, doc[0])
        await click(pilot, view, row_with(view, "Build it."))
        if edits:
            editor.insert("Typing ")
        (view if focus == "viewer" else editor).focus()
        await pilot.pause()
        if trigger == "compaction":
            store.command(other, "compact")
        else:   # just launched here: the app's pick (select_new), not the person's
            app.select_new, app.select_from = other, app.selected
        app.refresh_data()
        await pilot.pause()
        await pilot.pause()
        assert app.selected == (sid, doc[0]), f"{desc}: the selection kept, {app.selected}"
        assert not isinstance(app.screen, LeaveEdits) and editor.display, f"{desc}: the editor open, nothing asked"
        assert app.focused is (view if focus == "viewer" else editor), f"{desc}: the focus kept, {app.focused}"
        typed = editor.text
        if focus == "editor":   # Enter types a line there: landed once the buffer has it
            await pressed(pilot, "enter", lambda: editor.text != typed)
        else:   # Enter does nothing on the viewer: landed once a Ctrl+A behind it has selected the doc
            await pilot.press("enter")
            await pressed(pilot, "ctrl+a", lambda: view.text_selection is not None)
        assert doc[1].read_text() == DOC, f"{desc}: nothing written"


@pytest.mark.anyio
@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize("keys, desc", [
    (["enter"], "Enter on the last row in sight"),
    (["x"], "a long line wrapping past the last row in sight"),
])
async def test_the_editor_follows_the_cursor_down(store, sid, doc, full, keys, desc):
    """#300, as #298 for the answer box: Textual scrolls to the cursor before it grows the
    editor for the edit, so the new row was left just under it."""
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        view, editor = await shown(app, pilot, doc[0], full)
        await click(pilot, view, row_with(view, "The plan"))
        height = editor.scrollable_content_region.height
        # a row more than the editor holds, so it has scrolled once already (see #298's test)
        editor.text = "\n".join(["line"] * (height + 1))
        await pilot.pause()
        if keys == ["x"]:   # the last line a full row: one more key wraps it
            editor.text = "\n".join(["line"] * height + ["x" * editor.wrap_width])
        editor.move_cursor(editor.document.end)
        await pilot.pause()
        assert editor.scroll_offset.y == 1, f"{desc}: scrolled to the cursor on the last row"
        assert editor.wrapped_document.location_to_offset(editor.cursor_location).y == height, f"{desc}: on its last row"
        await pilot.press(*keys)
        await pilot.pause()
        row = editor.wrapped_document.location_to_offset(editor.cursor_location).y
        assert row == height + 1, f"{desc}: the cursor a row further down"
        assert editor.scroll_offset.y <= row < editor.scroll_offset.y + height, \
            f"{desc}: the cursor's row {row} in sight, rows {editor.scroll_offset.y} to {editor.scroll_offset.y + height - 1}"


@pytest.mark.anyio
@pytest.mark.parametrize("extra, keys, desc", UNDONE_ACROSS_THE_SCROLLBAR)
async def test_an_undo_that_shrinks_the_editor_keeps_the_cursor_in_it(store, sid, doc, extra, keys, desc):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        view, editor = await shown(app, pilot, doc[0])
        await click(pilot, view, row_with(view, "The plan"))
        await undone_across_the_scrollbar(pilot, editor, extra, keys, desc)


@pytest.mark.anyio
@pytest.mark.both
@pytest.mark.parametrize("how, asked, selected, desc", [
    ("delete", True, "doc", "Delete in the inbox: the person's own, so asked; Keep editing goes back to it"),
    ("ctrl+delete", False, "doc", "Ctrl+Delete in the inbox: a new doc isn't completed, so it stays"),
    ("dismissed", False, "doc", "dismissed by another app: the selected item held, the editor open"),
    ("parked", False, "doc", "its session parked by another app: held, hidden from the inbox, nothing asked"),
    ("ended", False, None, "its session ended: the editor open on the file by its path, nothing asked"),
    ("ended while asked", True, "q", "its session ended under the prompt: Keep editing goes back by its path"),
])
async def test_the_item_going_from_under_the_editor_never_loses_the_edits(store, sid, doc, tmp_path, how, asked,
                                                                          selected, desc):
    """#293: the doc item under Detail's editor going away, each way it can. Moved by the app,
    LeaveEdits fired unasked with Save, the Enter default, under the person's typing; with the
    session ended the save was refused and the edits stranded. Now asked only for the person's
    own move, and the edits always reach the file with Ctrl+S."""
    other = store.create_session(str(tmp_path), name="other")
    q = store.post_item(other if how == "ended while asked" else sid, "question", "which db?")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        view, editor = await shown(app, pilot, doc[0])
        await click(pilot, view, row_with(view, "Build it."))
        editor.insert("Typing ")
        await pilot.pause()
        if how in ("delete", "ctrl+delete"):
            app.items_table.focus()
            await pilot.pause()
            if how == "delete":   # a doc is pinned: Delete confirmed first
                await pressed(pilot, "delete", lambda: isinstance(app.screen, tui.Confirm))
                await pressed(pilot, "alt+y", lambda: isinstance(app.screen, LeaveEdits))
            else:
                await pilot.press("ctrl+delete")
        elif how == "ended while asked":
            app.items_table.move_cursor(row=item_row(app, q))
            await until(pilot, lambda: isinstance(app.screen, LeaveEdits))
            store.end(sid)
            app.refresh_data()
        else:
            if how == "dismissed":
                store.set_pinned(sid, doc[0], False)
                store.dismiss(sid, doc[0])
            else:
                store.set_parked(sid, True) if how == "parked" else store.end(sid)
            app.refresh_data()
        await pilot.pause()
        await pilot.pause()
        assert isinstance(app.screen, LeaveEdits) == asked, f"{desc}: asked {asked}, {app.screen}"
        if asked:   # Keep editing
            await pressed(pilot, "escape", lambda: editor.display and editor.has_focus)
        assert editor.display and editor.text.startswith("# The plan\n\nTyping "), f"{desc}: the editor open on the edits"
        assert app.selected == {"doc": (sid, doc[0]), "q": (other, q), None: None}[selected], f"{desc}: {app.selected}"
        if selected != "doc":
            assert app.shown_doc == (str(doc[1]), "the plan"), f"{desc}: shown by its path, {app.shown_doc}"
        assert doc[1].read_text() == DOC, f"{desc}: nothing written unasked"
        editor.focus()
        await pressed(pilot, "ctrl+s", lambda: doc[1].read_text() != DOC)
        assert doc[1].read_text() == DOC.replace("Build it.", "Typing Build it."), f"{desc}: saved"
