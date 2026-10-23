"""The prs module's PRs pane in the app (modules/prs): here, not with the module's own tests,
for the app's fixtures (conftest.py). gh is never run: the route's runner is a stub.
Skipped when the module isn't installed."""

import asyncio
import dataclasses
import json
import time

import pytest

from claude_wheelhouse import api
from claude_wheelhouse import tui
from claude_wheelhouse.tui import WheelhouseApp, tab_stops
from textual import events
from textual.widgets import Button, Input, OptionList
from textual.pilot import _get_mouse_message_arguments

prs = pytest.importorskip("claude_wheelhouse_prs", reason="installed without the prs module")
from claude_wheelhouse_prs import fetch, pane  # noqa: E402

URL = "https://github.com/two-inc/wheelhouse/pull/{}"


def node(n, checks="SUCCESS", review="APPROVED", draft=False, updated="2026-10-20T10:00:00Z", repo="two-inc/wheelhouse"):
    return {"number": n, "title": f"Change {n}", "url": f"https://github.com/{repo}/pull/{n}", "isDraft": draft,
            "state": "OPEN", "updatedAt": updated, "headRefName": f"doug/{n}", "reviewDecision": review,
            "mergeable": "MERGEABLE", "repository": {"nameWithOwner": repo},
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": checks}}}]}}


class Gh:
    """gh's stand-in: the items' PRs as the tracked aliases, and the search's."""

    def __init__(self, tracked, found, fail=None):
        self.tracked, self.found, self.fail, self.calls = tracked, found, fail, 0
        self.merges, self.merged = [], (0, "")   # gh pr merge's calls, and what it answers (code, stderr)

    def __call__(self, argv):
        if argv[1:3] == ["repo", "view"]:
            return 0, json.dumps({"mergeCommitAllowed": False, "squashMergeAllowed": True}), ""
        if argv[1:3] == ["pr", "merge"]:
            self.merges.append(argv[3:])
            return *self.merged[:1], "", self.merged[1]
        self.calls += 1
        if self.fail:
            raise self.fail
        data = {f"p{i}": {"pullRequest": n} for i, n in enumerate(self.tracked)}
        data["search"] = {"nodes": self.found}
        return 0, json.dumps({"data": data}), ""


@pytest.fixture
def github(store, sid, monkeypatch):
    """Two PR items of the session's (one failing its checks), a third PR only the search
    finds, and the prs module the only one."""
    store.post_pr(sid, URL.format(108), "Change 108")
    store.post_pr(sid, URL.format(109), "Change 109")
    gh = Gh([node(108, checks="FAILURE"), node(109)],
            [node(108, checks="FAILURE"), node(109), node(7, repo="doug/mizpah", updated="2026-10-20T12:00:00Z")])
    monkeypatch.setattr(pane, "RUNNER", gh)
    monkeypatch.setattr(api, "load", lambda: [api.Loaded("prs", prs.module)])
    return gh


async def settled(app, pilot):
    await pilot.pause()
    p = app.query_one("#prs")
    for _ in range(50):
        await app.workers.wait_for_complete()
        await pilot.pause()
        if not p.asking:
            break
    await pilot.pause()


def cells(app, column=0) -> list[str]:
    table = app.query_one("#pr-list")
    return [str(table.get_row_at(i)[column]) for i in range(table.row_count)]


@pytest.mark.anyio
@pytest.mark.both
async def test_the_pane_lists_the_sessions_prs_then_all(store, sid, github):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        p = app.query_one("#prs")
        assert p.parent.id == "slot-middle-middle" and p.border_title == "PRs · mine", "its slot, mine first"
        assert cells(app) == ["PR108", "PR109"], "red before green"
        assert len(app.query_one("#pr-list").columns) == 3, "one repo: no Repo column"
        assert github.calls == 1, "fresh as it's shown, once"
        assert {i["ref"]: i["status"] for i in store.pr_items()} == {"PR108": "open", "PR109": "open"}, \
            "GitHub's state written back"
        app.query_one("#pr-list").focus()
        await pilot.pause()
        await pilot.press("m")
        await settled(app, pilot)
        assert p.border_title == "PRs · all" and cells(app) == ["PR108", "PR7", "PR109"], "all: the search's too, newest first within a light"
        assert cells(app, 1) == ["wheelhouse", "mizpah", "wheelhouse"], "two repos: the Repo column"
        assert github.calls == 1, "the scope switched within the gap: from the cache"


@pytest.mark.anyio
async def test_selecting_shows_an_item_or_a_card_and_enter_opens(store, sid, github, monkeypatch):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        p = app.query_one("#prs")
        shown, opened = [], []
        p.ctx = dataclasses.replace(p.ctx, show_item=lambda s, ref: shown.append((s, ref)))
        monkeypatch.setattr(app, "open_url", lambda url, **kw: opened.append(url))
        p.action_scope()
        await settled(app, pilot)
        table = app.query_one("#pr-list")
        table.focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        card = app.query_one("#prs-card")
        text = str(app.query_one("#prs-card-text").render())
        assert card.display and "doug/7" in text and "success" in text, \
            "a PR that isn't an item: a card with its branch and checks"
        assert shown == [], "nothing in Detail"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert shown == [(sid, "PR109")], "an item: shown in Detail"
        assert not app.query_one("#prs-card").display, "no card for an item"
        assert opened == ["https://github.com/doug/mizpah/pull/7"], "Enter: the browser"


@pytest.mark.anyio
async def test_r_fetches_again_after_the_gap(store, sid, github, monkeypatch):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        app.query_one("#pr-list").focus()
        await pilot.press("r")
        await settled(app, pilot)
        assert github.calls == 1, "within the gap: the cache"
        monkeypatch.setattr(pane, "CLOCK", lambda: time.time() + fetch.GAP)
        await pilot.press("r")
        await settled(app, pilot)
        assert github.calls == 2, "past it: fetched"


@pytest.mark.anyio
async def test_no_gh_says_so_in_place_of_the_table(store, sid, github):
    github.fail = fetch.Unavailable("gh isn't installed: the PRs pane needs the GitHub CLI")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        note = app.query_one("#prs-note")
        assert note.display and "gh isn't installed" in str(note.render()), "the line says why"
        assert not app.query_one("#pr-list").display, "in place of the table"


@pytest.mark.anyio
async def test_tab_stops_at_the_prs_after_the_inbox(store, sid, github):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        ids = [w.id for w, _ in tab_stops(app.screen)]
        assert ids[ids.index("items") + 1] == "pr-list", "after the inbox"


# the middle column's three slots (#111 review 1)

@pytest.mark.anyio
async def test_prs_without_the_decisions_keep_their_splitter_and_it_tracks_the_pointer(store, sid, monkeypatch):
    """Decisions absent: the splitter between the inbox and the PRs still shows (it is
    theirs, not the empty slot's), and a drag moves it by as many rows as the pointer."""
    from test_tui import drag, splitter
    monkeypatch.setattr(api, "load", lambda: [api.Loaded("prs", prs.module)])
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        shown = {s.key for s in app.query("Splitter") if s.display and s.key.startswith("middle")}
        assert shown == {"middle-middle"}, "the PRs' splitter shows; the empty Decisions' doesn't"
        sp = splitter(app, "middle-middle")
        at = sp.region.y
        await drag(pilot, "middle-middle", 0, 4)
        await pilot.pause()
        assert sp.region.y == at + 4, "a drag of 4 rows moves it 4"


@pytest.mark.anyio
async def test_an_empty_decisions_pane_leaves_the_inbox_its_room(store, sid, tmp_path, every_module):
    """The shipped default with no ADRs: the Decisions shrink to their note in the middle
    column's three slots too, and the inbox and the PRs share what they give up, by their
    weights (columns.py): the inbox twice the PRs' rows."""
    (tmp_path / ".git").mkdir()
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        await pilot.pause()
        design, inbox, prs_pane = app.query_one("#design"), app.query_one("#items-pane"), app.query_one("#prs")
        assert design.compact and design.region.height == 3, "the Decisions at their border and note"
        assert inbox.region.height >= 2 * prs_pane.region.height, "the inbox has the room"
        column = app.query_one("#col-middle")
        assert sum(w.outer_size.height for w in column.children if w.display) == column.size.height, "no gap"


LONG = "doug/a-repository-with-a-name-long-enough-to-wrap-in-any-card-the-pane-shows"
LONG_URL = f"https://github.com/{LONG}/pull/7000"


async def click_link(app, pilot, widget, line: int) -> list[str]:
    """A click on the line'th line of the link in widget, as the screen draws it: the URLs opened."""
    opened = []
    app.open_url = lambda url, **kw: opened.append(url)
    at = [(x, y) for y in range(widget.region.y, widget.region.bottom)
          for x in range(widget.region.x, widget.region.right) if "@click" in app.screen.get_style_at(x, y).meta]
    lines = sorted({y for _, y in at})
    assert len(lines) > line, f"the link on lines {lines}"
    x, y = next((x, y) for x, y in at if y == lines[line])
    await pilot.click(widget, offset=(x - widget.region.x, y - widget.region.y))
    await pilot.pause()
    return opened


@pytest.mark.anyio
@pytest.mark.parametrize("size, weights, line, desc", [
    ((80, 24), None, 0, "a 12x7 pane: its card has room for the link's first line alone, still the whole URL"),
    ((80, 24), '{"middle.middle": 0.9}', 1, "the pane taller at 80x24: the link's second line"),
    ((160, 40), None, 1, "the link's second line at 160x40"),
])
async def test_a_click_on_any_line_of_a_wrapped_link_opens_the_whole_url(store, sid, github, size, weights, line, desc):
    """#139: the card's URL shows first, without https://github.com/, and a click on any line
    of it where the card wraps it opens the whole URL, not the line's fragment."""
    github.found.append(node(7000, repo=LONG, updated="2026-10-21T12:00:00Z"))
    if weights:
        store.set_setting("layout.weights", weights)   # the PRs pane's slot, most of its column
    app = WheelhouseApp(store)
    async with app.run_test(size=size) as pilot:
        await settled(app, pilot)
        app.query_one("#prs").action_scope()
        await settled(app, pilot)
        table = app.query_one("#pr-list")
        table.focus()
        await pilot.pause()
        table.move_cursor(row=table.get_row_index(LONG_URL))
        await pilot.pause()
        card = app.query_one("#prs-card")
        assert card.display and str(app.query_one("#prs-card-text").render()).startswith(f"{LONG}/pull/7000"), \
            f"{desc}: the card, its URL first and without https://github.com/"
        assert await click_link(app, pilot, card, line) == [LONG_URL], desc
        text = pane.card(app.query_one("#prs").row(LONG_URL))
        assert not text.style and [(sp.start, sp.end) for sp in text.spans if getattr(sp.style, "meta", None)] == \
            [(0, len(f"{LONG}/pull/7000"))], f"{desc}: the link alone opens it, not the title or branch"
        assert card.display, f"{desc}: the card stays, the table keeping the focus"


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(80, 24), (160, 40)])
async def test_a_click_on_a_pr_items_wrapped_link_in_detail_opens_the_whole_url(store, sid, github, size):
    """#139: a PR item in Detail shows its URL without https://github.com/, and a click on its
    second line opens the whole URL."""
    store.post_pr(sid, LONG_URL, "Change 7000")
    ref = next(i["ref"] for i in store.pr_items() if i["url"] == LONG_URL)
    app = WheelhouseApp(store)
    async with app.run_test(size=size) as pilot:
        await settled(app, pilot)
        app.show_item(sid, ref)
        await settled(app, pilot)
        app.query_one("#detail-scroll").scroll_end(animate=False)   # the link below the head, at 80x24
        await pilot.pause()
        thread = app.query_one("#detail")
        shown = "\n".join(strip.text for strip in thread.render_lines(thread.region.reset_offset))
        assert "https://github.com" not in shown and "/pull/" in shown, f"{size}: without https://github.com/"
        assert await click_link(app, pilot, thread, 1) == [LONG_URL], f"{size}: the whole URL from its second line"


@pytest.mark.anyio
async def test_a_short_panes_card_scrolls_to_its_last_line(store, sid, github):
    """#139: at 80x24 the card has room for a line or two: it scrolls, down to Mergeable."""
    github.found.append(node(7000, repo=LONG, updated="2026-10-21T12:00:00Z"))
    app = WheelhouseApp(store)
    async with app.run_test(size=(80, 24)) as pilot:
        await settled(app, pilot)
        app.query_one("#prs").action_scope()
        await settled(app, pilot)
        table = app.query_one("#pr-list")
        table.focus()
        await pilot.pause()
        table.move_cursor(row=table.get_row_index(LONG_URL))
        await pilot.pause()
        card = app.query_one("#prs-card")
        await pilot.pause()   # its text laid out at its width: its height is the wrapped one
        assert card.virtual_size.height > card.scrollable_content_region.height, "more card than room"
        card.scroll_end(animate=False, immediate=True)
        await pilot.pause()
        text = app.query_one("#prs-card-text")
        drawn = "".join(strip.text.strip() for strip in text.render_lines(text.region.reset_offset))
        assert 0 < card.scroll_y == card.max_scroll_y and drawn.endswith("mergeable"), "scrolled to its end, Mergeable"
        assert table.has_focus and card.display, "the table keeps the focus, and the card shows"


async def on_pr(app, pilot, url, scope="mine"):
    """The PRs table focused, its cursor on url (after m, for all)."""
    p = app.query_one("#prs")
    if scope != p.scope:
        p.action_scope()
        await settled(app, pilot)
    table = app.query_one("#pr-list")
    table.focus()
    await pilot.pause()
    table.move_cursor(row=table.get_row_index(url))
    await pilot.pause()
    return p


@pytest.mark.anyio
async def test_the_selected_pr_has_its_actions_and_open_opens_it(store, sid, github, monkeypatch):
    """#138: Open, Merge and Request review under the table, for the PR the cursor is on."""
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        opened = []
        monkeypatch.setattr(app, "open_url", lambda url, **kw: opened.append(url))
        assert not app.query_one("#prs-actions").display, "no PR selected: no buttons"
        await on_pr(app, pilot, URL.format(109))
        bar = app.query_one("#prs-actions")
        assert bar.display and [str(b.label) for b in bar.query(Button)] == ["Open", "Merge", "Request review"]
        await pilot.click("#pr-open")
        await pilot.pause()
        assert opened == [URL.format(109)], "the full URL, the app's opener"
        assert app.query_one("#pr-list").has_focus and bar.display, "the table keeps the focus, and the buttons"


@pytest.mark.anyio
@pytest.mark.parametrize("n, answer, merged, said, desc", [
    (109, "n", None, None, "No: nothing merged"),
    (109, "y", (0, ""), "Merged PR109 (squash).", "Yes: merged the way the repo allows, gone from the list"),
    (108, "y", (1, "X Pull request #108 is not mergeable: the base branch policy prohibits the merge."),
     "Couldn't merge PR108: checks are failing. gh says: X Pull request #108 is not mergeable", "a failure says why"),
])
async def test_merge_asks_first_and_says_what_came_of_it(store, sid, github, n, answer, merged, said, desc):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        github.merged = merged or (0, "")
        p = await on_pr(app, pilot, URL.format(n))
        await pilot.click("#pr-merge")
        await pilot.pause()
        assert isinstance(app.screen, tui.Confirm) and app.screen.focused.id == "no", f"{desc}: asked, No focused"
        await pilot.press(answer)
        await settled(app, pilot)
        await app.workers.wait_for_complete()
        await pilot.pause()
        await settled(app, pilot)
        assert github.merges == ([["--squash", "--", URL.format(n)]] if merged else []), desc
        note = str(app.query_one("#prs-note").render())
        assert (said in note) if said else not p.outcome, f"{desc}: {note!r}"
        if merged == (0, ""):
            assert cells(app) == ["PR108"], f"{desc}: gone from the list, the others still in it"


@pytest.mark.anyio
async def test_request_review_asks_the_session_that_made_the_pr_and_remembers_the_target(store, sid, github, monkeypatch):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        sent = []
        monkeypatch.setattr(app.client, "submit", lambda target, text, now=False: sent.append((target, text)) or tui.Done())
        store.set_setting(pane.TARGETS, json.dumps(["@sam"]))
        await on_pr(app, pilot, URL.format(109))
        await pilot.click("#pr-review")
        await pilot.pause()
        assert isinstance(app.screen, tui.Ask), "a target asked for"
        assert [str(o.prompt) for o in app.screen.query_one(OptionList).options] == ["@sam"], "past targets offered"
        app.screen.query_one(Input).value = "#eng-reviews"
        await pilot.press("enter")
        await pilot.pause()
        assert sent == [((sid, "PR109"), pane.request({"url": URL.format(109), "title": "Change 109"}, "#eng-reviews"))], \
            "the session that made it, on its item"
        assert sent[0][1].endswith(f'PR: {URL.format(109)}\nTitle: "Change 109"\nTo: "#eng-reviews"'), "the values quoted, last"
        assert json.loads(store.setting(pane.TARGETS)) == ["#eng-reviews", "@sam"], "most recent first"


@pytest.mark.anyio
@pytest.mark.parametrize("selected, desc", [(True, "the selected session asked"), (False, "no session: off")])
async def test_request_review_of_a_pr_no_session_made(store, sid, github, monkeypatch, selected, desc):
    """In All, a PR no session made: the session selected in the session list, else none."""
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        sent = []
        monkeypatch.setattr(app.client, "submit", lambda target, text, now=False: sent.append(target) or tui.Done())
        app.follow(sid) if selected else app.clear_filter()
        await pilot.pause()
        p = await on_pr(app, pilot, "https://github.com/doug/mizpah/pull/7", scope="all")
        assert app.query_one("#pr-review", Button).disabled is not selected, desc
        p.review(p.selected())
        await pilot.pause()
        if selected:
            app.screen.query_one(Input).value = "#mizpah"
            await pilot.press("enter")
            await pilot.pause()
        assert sent == ([(sid, None)] if selected else []), desc


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(160, 40), (80, 24)])
async def test_a_right_click_on_a_pr_opens_its_actions_below_the_row(store, sid, github, monkeypatch, size):
    """#138: a PR row's menu, below the row as a session's (#137), Open acting on the row clicked."""
    app = WheelhouseApp(store)
    async with app.run_test(size=size) as pilot:
        await settled(app, pilot)
        opened = []
        monkeypatch.setattr(app, "open_url", lambda url, **kw: opened.append(url))
        table = app.query_one("#pr-list")
        row = table.get_row_index(URL.format(109))
        args = _get_mouse_message_arguments(table, (3, table.header_height + row), button=3)
        for cls in (events.MouseDown, events.MouseUp):
            await app.on_event(cls(**args))
        await pilot.pause()
        assert isinstance(app.screen, tui.PaneMenu), f"{size}: a menu"
        menu = app.screen
        line = table.region.y + table.header_height + row
        box = menu.query_one("#dialog").region
        assert box.y == line + 1 or box.bottom == line, f"{size}: {box} below or above line {line}"
        assert [str(b.label) for b in menu.query(Button)] == ["Open", "Merge", "Request review"]
        await pilot.press("enter")
        await pilot.pause()
        assert opened == [URL.format(109)], f"{size}: Open, on the row clicked"
        assert table.has_focus and app.query_one("#prs-actions").display, f"{size}: the table focused, its buttons shown"


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(100, 30), (80, 24), (160, 40)])
async def test_the_buttons_fit_the_pane_and_each_click_acts(store, sid, github, monkeypatch, size):
    """Review of #143: in a narrow pane the buttons stack rather than fall outside it."""
    app = WheelhouseApp(store)
    async with app.run_test(size=size) as pilot:
        await settled(app, pilot)
        acted = []
        p = await on_pr(app, pilot, URL.format(109))
        monkeypatch.setattr(p, "act", lambda action, r: acted.append((action, r["number"])))
        pane_box = p.content_region
        for b in app.query_one("#prs-actions").query(Button):
            assert pane_box.contains_region(b.region) and b.region.width >= len(str(b.label)), \
                f"{size}: {b.id} {b.region} {str(b.label)!r} inside {pane_box}"
            await pilot.click(f"#{b.id}")
            await pilot.pause()
        assert acted == [("open", 109), ("merge", 109), ("review", 109)], f"{size}: every click acts"


@pytest.mark.anyio
@pytest.mark.parametrize("status, error, said, desc", [
    (404, "no route POST /m/prs/merge", "Couldn't merge PR109: the hub is older: update it", "an older hub"),
    (503, "the daemon couldn't be reached", "Couldn't merge PR109: the daemon couldn't be reached", "no hub: not gh's"),
])
async def test_a_merge_the_hub_refuses_says_so_not_gh(store, sid, github, status, error, said, desc):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        p = await on_pr(app, pilot, URL.format(109))

        def refuse(path, body):
            if body is None:
                return {"prs": []}
            raise api.Refused(status, error)
        p.ctx = dataclasses.replace(p.ctx, call=refuse)
        await asyncio.to_thread(p.merged, p.selected())   # off the app's thread, as the pane's worker
        await pilot.pause()
        assert p.outcome == said and "gh says" not in p.outcome, desc
