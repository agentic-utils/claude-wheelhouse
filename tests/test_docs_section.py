"""The docs section (.plan/docs-section.md, #106): each doc's thread beside it in Detail and
full screen, split or tabbed. A doc out of reach of clearing the inbox is a pin (test_pins)."""

import pytest

from claude_wheelhouse import tui
from claude_wheelhouse.tui import WheelhouseApp
from test_tui import item_list_rows

DOC = "# The plan\n\nBuild it.\n\n## Recommendation\n\n**Add as tips.** They are cheap.\n"
LONG = "".join(f"## Section {i}\n\nProse for section {i}.\n\n" for i in range(60))


@pytest.fixture
def doc(store, sid, tmp_path):
    """A doc item of the session's, and its file."""
    path = tmp_path / "plan.md"
    path.write_text(DOC)
    return store.post_doc(sid, "the plan", str(path)), path


async def select(app, pilot, ref: str) -> None:
    await pilot.pause()
    app.items_table.move_cursor(row=[r[1] for r in item_list_rows(app)].index(f"└ {ref}"))   # in its session's group
    await pilot.pause()


def notices(app) -> list[str]:
    return [n.message for n in app._notifications]


async def click_line(app, pilot, view, text: str) -> None:
    """A click on the rendered doc's row holding text."""
    texts = view._layout(view.size.width)[1]
    row = next(y for y in range(view.doc_at[0], len(texts)) if text in texts[y])
    region = view.parent.scrollable_content_region
    await pilot.click(offset=(region.x + 2, region.y + row - round(view.parent.scroll_y)))
    await pilot.pause()
    await pilot.pause()


# the inbox


@pytest.mark.anyio
async def test_a_doc_with_unseen_replies_says_how_many(store, sid, doc):
    q = store.post_item(sid, "question", "which?")
    store.mark_seen(sid, doc[0])
    for _ in range(2):
        store.reply(sid, doc[0], "a reply", by_session=True)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        rows = item_list_rows(app)
        assert [r[1] for r in rows] == [q, f"└ {doc[0]}"], "in its session's group"
        assert str(app.items_table.get_row_at(1)[3]) == "2 new"


# Detail


@pytest.mark.anyio
async def test_split_shrinks_the_thread_to_the_box_until_the_first_comment(store, sid, doc):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await select(app, pilot, doc[0])
        panes = app.detail_panes
        shown = lambda: (panes.thread_scroll.display, panes.thread_splitter.display, app.answer.display)
        assert shown() == (False, False, True), "no comment yet: the box alone under the doc"
        store.send(sid, "why tips?", doc[0])
        app.refresh_data()
        await pilot.pause()
        assert shown() == (True, True, True), "the first comment: the thread at its share"
        assert "why tips?" in panes.text, "the comment in the thread"


@pytest.mark.anyio
async def test_ctrl_l_tabs_a_doc_and_the_keys_move_between_tabs(store, sid, doc):
    store.mark_seen(sid, doc[0])
    store.reply(sid, doc[0], "a reply", by_session=True)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await select(app, pilot, doc[0])
        panes = app.detail_panes
        app.items_table.focus()
        await pilot.press("ctrl+l")
        await pilot.pause()
        assert store.setting("layout.doc-view") == "tabbed", "kept"
        assert (panes.tabs.display, panes.tab, app.detail_scroll.display, app.answer.display) == \
            (True, "doc", True, False), "tabbed: the Doc tab, no box"
        assert "Thread (1 new)" in str(panes.tabs.render()), "the Thread tab counts its new replies"
        await pilot.press("ctrl+pagedown")
        await pilot.pause()
        assert (panes.tab, app.detail_scroll.display, panes.thread_scroll.display, app.answer.display) == \
            ("thread", False, True, True), "Ctrl+PageDown: the thread and its box"
        await pilot.press("ctrl+pageup")
        await pilot.pause()
        assert panes.tab == "doc", "Ctrl+PageUp: back to the doc"
        app.screen.set_focus(app.detail)
        await pilot.press("w", "h", "y")
        await pilot.pause()
        assert (panes.tab, app.focused, app.answer.text) == ("thread", app.answer, "why"), \
            "typing on the Doc tab: the Thread tab, with what's typed"
        await pilot.press("ctrl+l")
        await pilot.pause()
        assert (store.setting("layout.doc-view"), panes.tabs.display, app.detail_scroll.display) == ("split", False, True)


@pytest.mark.anyio
@pytest.mark.parametrize("layout, desc", [("split", "split: in the thread's place"), ("tabbed", "tabbed: a third tab")])
async def test_a_click_on_a_block_opens_the_editor_in_the_threads_place(store, sid, doc, layout, desc):
    store.set_setting("layout.doc-view", layout)
    store.send(sid, "a comment", doc[0])
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await select(app, pilot, doc[0])
        panes = app.detail_panes
        await click_line(app, pilot, app.detail, "Recommendation")
        assert app.detail_editor.display and app.focused is app.detail_editor, desc
        assert not app.answer.display and not panes.thread_scroll.display, f"{desc}: no thread nor box"
        if layout == "tabbed":
            assert panes.tab == "editor" and not app.detail_scroll.display, desc
        await pilot.press("escape")   # no edits: closed at once
        await pilot.pause()
        assert app.answer.display and panes.thread_scroll.display, f"{desc}: the thread back"


@pytest.mark.anyio
@pytest.mark.parametrize("mode, sent, full, desc", [
    ("queued", False, False, "queued, as an answer is"),
    ("immediate", True, False, "sent at once, as an answer is"),
    ("queued", False, True, "from full screen, its own box"),
])
async def test_a_comment_quotes_the_block_clicked_and_goes_by_the_sessions_mode(store, sid, doc, mode, sent, full, desc):
    store.set_mode(sid, mode)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await select(app, pilot, doc[0])
        if full:
            app.items_table.focus()
            await pilot.press("enter")
            await pilot.pause()
        view, box = (app.screen.view, app.screen.box) if full else (app.detail, app.answer)
        await click_line(app, pilot, view, "Add as tips")
        await pilot.press("escape")
        await pilot.pause()
        assert box.border_title == '> "Add as tips. They are cheap."', desc
        app.screen.set_focus(box)
        await pilot.press("o", "k")
        await pilot.press("ctrl+enter")
        await pilot.pause()
        msgs = store.thread(sid, doc[0])
        assert [m["body"] for m in msgs] == ['> "Add as tips. They are cheap."\n\nok'], desc
        assert (not msgs[0]["draft"]) == sent, desc
        assert box.border_title is None and not app.doc_quotes, f"{desc}: said, the next comment general"


@pytest.mark.anyio
async def test_esc_in_an_empty_box_drops_the_quote(store, sid, doc):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await select(app, pilot, doc[0])
        await click_line(app, pilot, app.detail, "Build it")
        await pilot.press("escape")
        await pilot.pause()
        app.screen.set_focus(app.answer)
        await pilot.press("escape")
        await pilot.pause()
        assert app.answer.border_title is None and app.selected == (sid, doc[0])
        await pilot.press("h", "i", "ctrl+enter")
        await pilot.pause()
        assert [m["body"] for m in store.thread(sid, doc[0])] == ["hi"], "a general comment"


@pytest.mark.anyio
async def test_a_reposted_doc_reloads_where_it_was_scrolled(store, sid, tmp_path):
    path = tmp_path / "long.md"
    path.write_text(LONG)
    ref = store.post_doc(sid, "long", str(path))
    store.mark_seen(sid, ref)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await select(app, pilot, ref)
        app.detail_scroll.scroll_to(y=40, animate=False)
        await pilot.pause()
        path.write_text(LONG.replace("Prose for section 50.", "Revised prose."))
        store.post_doc(sid, "long", str(path))
        app.refresh_data()
        await pilot.pause()
        assert "Revised prose." in app._detail_text and store.item(sid, ref)["status"] == "updated"
        assert app.detail_scroll.scroll_y == 40, "the reading place kept"


@pytest.mark.anyio
async def test_unsaved_edits_survive_a_revision_with_a_notice(store, sid, doc):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await select(app, pilot, doc[0])
        await click_line(app, pilot, app.detail, "Build it")
        await pilot.press("end", "!")
        await pilot.pause()
        doc[1].write_text(DOC.replace("Build it.", "Build it twice."))
        store.post_doc(sid, "the plan", str(doc[1]))
        app.refresh_data()
        await pilot.pause()
        assert "Build it.!" in app.detail_editor.text, "the buffer kept"
        assert any("changed on disk since you started editing" in n for n in notices(app))


# full screen


@pytest.mark.anyio
@pytest.mark.parametrize("layout, box, desc", [("split", True, "split: the box under the doc"),
                                                ("tabbed", False, "tabbed: the Doc tab first")])
async def test_full_screen_lays_a_doc_out_as_detail_does(store, sid, doc, layout, box, desc):
    store.set_setting("layout.doc-view", layout)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await select(app, pilot, doc[0])
        app.items_table.focus()
        await pilot.press("enter")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, tui.DocView), desc
        assert (screen.box.display, screen.panes.tabs.display) == (box, layout == "tabbed"), desc
        if layout == "tabbed":
            await pilot.press("ctrl+pagedown")
            await pilot.pause()
            assert screen.box.display and screen.panes.tab == "thread", f"{desc}: Ctrl+PageDown to the thread"
        screen.set_focus(screen.box)
        await pilot.press("h", "i", "ctrl+enter")
        await pilot.pause()
        assert [m["body"] for m in store.thread(sid, doc[0])] == ["hi"], f"{desc}: a comment from full screen"
