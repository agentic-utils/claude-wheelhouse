"""The prs module's PRs pane in the app (modules/prs): here, not with the module's own tests,
for the app's fixtures (conftest.py). gh is never run: the route's runner is a stub.
Skipped when the module isn't installed."""

import asyncio
import dataclasses
import json
import threading
import time

import pytest

from claude_wheelhouse import api
from claude_wheelhouse import tui
from claude_wheelhouse.tui import WheelhouseApp, tab_stops
from textual.content import Content
from textual import events
from textual.widgets import Button, Input, OptionList
from textual.pilot import _get_mouse_message_arguments

prs = pytest.importorskip("claude_wheelhouse_prs", reason="installed without the prs module")
from claude_wheelhouse_prs import fetch, pane  # noqa: E402
from conftest import Clock, first_item, keeps_colours, to_first_item  # noqa: E402

URL = "https://github.com/two-inc/wheelhouse/pull/{}"


def node(n, checks="SUCCESS", review=None, draft=False, updated="2026-10-20T10:00:00Z", repo="two-inc/wheelhouse"):
    return {"number": n, "title": f"Change {n}", "url": f"https://github.com/{repo}/pull/{n}", "isDraft": draft,
            "state": "OPEN", "updatedAt": updated, "headRefName": f"doug/{n}", "reviewDecision": review,
            "mergeable": "MERGEABLE", "repository": {"nameWithOwner": repo},
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": checks}}}]}}


class Gh:
    """gh's stand-in: the items' PRs as the tracked aliases, and the search's."""

    def __init__(self, tracked, found, fail=None):
        self.tracked, self.found, self.fail, self.calls = tracked, found, fail, 0
        self.merges, self.merged = [], (0, "")   # gh pr merge's calls, and what it answers (code, stderr)
        self.closes, self.closed = [], {}        # gh pr close's calls, and what it answers for a URL (code, stderr)
        self.readies, self.ready = [], (0, "")   # gh pr ready's calls (#212), and what it answers (code, stderr)
        self.details = []                        # the detail query's calls, by number (#145)
        self.detail = {"body": "Why: [b]bold[/b] isn't markup", "timelineItems": {"nodes": []}, "commits": {"nodes": []}}
        self.gate = None                         # an Event the list's query waits on, if set (#214)

    def __call__(self, argv):
        if argv[1:3] == ["repo", "view"]:
            return 0, json.dumps({"mergeCommitAllowed": False, "squashMergeAllowed": True}), ""
        if argv[1:3] == ["pr", "merge"]:
            self.merges.append(argv[3:])
            return *self.merged[:1], "", self.merged[1]
        if argv[1:3] == ["pr", "ready"]:
            self.readies.append(argv[3:])
            return self.ready[0], "", self.ready[1]
        if argv[1:3] == ["pr", "close"]:
            self.closes.append(argv[3:])
            code, err = self.closed.get(argv[-1], (0, ""))
            return code, "", err
        if any(a.startswith("number=") for a in argv):
            self.details.append(int(argv[-1].removeprefix("number=")))
            return 0, json.dumps({"data": {"repository": {"pullRequest": self.detail}}}), ""
        self.calls += 1
        if self.gate is not None:
            self.gate.wait(10)
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
    monkeypatch.setattr(pane, "SETTLE", 0.05)   # the highlight rests this long before a PR's detail is fetched
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


async def until(pilot, check, desc, seconds=10.0):
    """Paused until check holds: the condition a step needs waited on, not a guess at how
    long it takes under load (#214)."""
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, f"timed out: {desc}"
        await pilot.pause(0.02)


async def on_first(app, pilot):
    """Settled, the highlight on the first item (the conversation rows head the inbox, #236), and
    that item in Detail reached the PRs pane (showing), which under load can be after the list
    lands (#214)."""
    await settled(app, pilot)
    await to_first_item(app, pilot)
    p = app.query_one("#prs")
    await until(pilot, lambda: app.selected is not None and p.in_detail == ("item", *app.selected), "Detail's first item")
    return p


async def pick_in_inbox(app, pilot, key):
    """A click on the inbox's row for key (under its header)."""
    await pilot.click("#items", offset=(4, 1 + app.items_table.get_row_index(key)))
    await pilot.pause()


async def none_picked(app, pilot, sid, q):
    """The PRs pane's highlight hidden on PR109: PR109 picked in the inbox, then question q,
    which the pane doesn't list (#148)."""
    p = await on_first(app, pilot)
    for ref in ("PR109", q):
        await pick_in_inbox(app, pilot, f"{sid}|{ref}")
        await until(pilot, lambda: p.in_detail == ("item", sid, ref), f"Detail on {ref}")
    return p


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
        assert p.parent.id == "slot-middle-middle" and str(p.border_title) == "PRs · mine", "its slot, mine first"
        assert cells(app) == ["PR108", "PR109"], "red before green"
        assert len(app.query_one("#pr-list").columns) == 3, "one repo: no Repo column"
        assert github.calls == 1, "fresh as it's shown, once"
        assert {i["ref"]: i["status"] for i in store.pr_items()} == {"PR108": "open", "PR109": "open"}, \
            "GitHub's state written back"
        app.query_one("#pr-list").focus()
        await pilot.pause()
        await pilot.press("alt+o")
        await settled(app, pilot)
        assert str(p.border_title) == "PRs · all" and cells(app) == ["PR108", "PR7", "PR109"], "all: the search's too, newest first within a light"
        assert cells(app, 1) == ["wheelhouse", "mizpah", "wheelhouse"], "two repos: the Repo column"
        assert github.calls == 1, "the scope switched within the gap: from the cache"


@pytest.mark.anyio
async def test_selecting_shows_an_item_or_a_card_and_enter_opens(store, sid, github, monkeypatch):
    q = store.post_item(sid, "question", "Which way?", status="open")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        p = await none_picked(app, pilot, sid, q)
        shown, opened = [], []
        p.ctx = dataclasses.replace(p.ctx, show_item=lambda s, ref: shown.append((s, ref)))
        monkeypatch.setattr(app, "open_url", lambda url, **kw: opened.append(url))
        p.action_scope()
        await settled(app, pilot)
        table = app.query_one("#pr-list")
        assert not table.show_cursor and cells(app) == ["PR108", "PR7", "PR109"], "all, none picked"
        table.focus()
        await pilot.pause()
        await pilot.press("down")   # none picked here yet: the first key brings the highlight back where it is
        await pilot.pause()
        assert table.show_cursor and shown == [(sid, "PR109")], "the first key: the hidden highlight's row picked"
        await pilot.press("up")
        await pilot.pause()
        card = app.query_one("#prs-card")
        text = str(app.query_one("#prs-card-text").render())
        assert card.display and "doug/7" in text and "success" in text, \
            "a PR that isn't an item: a card with its branch and checks"
        assert shown == [(sid, "PR109")], "nothing more in Detail"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        assert shown == [(sid, "PR109"), (sid, "PR108")], "an item: shown in Detail"
        assert not app.query_one("#prs-card").display, "no card for an item"
        assert opened == ["https://github.com/doug/mizpah/pull/7"], "Enter: the browser"


@pytest.mark.anyio
@pytest.mark.parametrize("picked, desc", [
    (False, "none picked: the highlight hidden, so the table's hover stays put"),
    (True, "one picked: the highlight showing"),
])
async def test_the_tooltip_is_the_hovered_prs(store, sid, github, picked, desc):
    """#184: Doug, "the tooltip on the PR window always shows the description of the first
    PR in the list, even when I hover over other ones"."""
    q = store.post_item(sid, "question", "Which way?", status="open")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await none_picked(app, pilot, sid, q)
        table = app.query_one("#pr-list")
        if picked:
            table.reveal(0)
            await pilot.pause()
        assert table.show_cursor == picked, f"{desc}: the highlight as set up"
        for row, n in [(1, 109), (0, 108), (1, 109)]:
            await pilot.hover(table, offset=(2, table.header_height + row))
            await pilot.pause()
            assert str(table.tooltip) == f"two-inc/wheelhouse #{n}: Change {n}", f"{desc}: row {row}'s PR"


@pytest.mark.anyio
@pytest.mark.parametrize("list_id, desc", [
    ("#pr-list", "the PRs pane"),
    ("#items", "the inbox"),
    ("#session-list", "the session list"),
])
async def test_a_hovered_or_highlighted_row_keeps_its_colours(store, sid, github, tmp_path, list_id, desc):
    """#217: Doug, "when I hover over items in the PR list, their status shows green regardless
    of what the status actually is. Hovering should not change status colour"."""
    store.create_session(str(tmp_path), name="other", ticket="#8", brief="another thing")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        table = app.query_one(list_id)
        kept = await keeps_colours(pilot, table, desc, first_item(app) if list_id == "#items" else 0)
        if list_id == "#pr-list":
            assert pane.LOOKS["red"] in kept, f"{desc}: PR108's light red"


@pytest.mark.anyio
async def test_detail_s_pr_is_highlighted_though_the_list_lands_after_it(store, sid, github):
    """#214: Detail showing a PR item before the list landed left the highlight hidden, where
    landing first it showed: which came first decided it."""
    github.gate = threading.Event()   # the list held until Detail shows its PR
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await until(pilot, lambda: any(not k.endswith("|") for k in app.items_table.keys()), "the inbox listed")
        await to_first_item(app, pilot)   # PR109, under the conversation row (#236)
        await until(pilot, lambda: app.query("#prs") and app.query_one("#prs").in_detail == ("item", sid, "PR109"),
                    "Detail on PR109, the list not landed")
        table = app.query_one("#pr-list")
        assert not table.show_cursor and not table.row_count, "nothing listed to highlight yet"
        github.gate.set()
        await settled(app, pilot)
        assert table.show_cursor and pane.Prs.key_at(table) == URL.format(109), "highlighted once listed"


@pytest.mark.anyio
async def test_r_fetches_again_after_the_gap(store, sid, github, monkeypatch):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        app.query_one("#pr-list").focus()
        await pilot.press("alt+r")
        await settled(app, pilot)
        assert github.calls == 1, "within the gap: the cache"
        monkeypatch.setattr(pane, "CLOCK", lambda: time.time() + fetch.GAP)
        await pilot.press("alt+r")
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
    table.reveal(table.get_row_index(url)) if not table.show_cursor else table.move_cursor(row=table.get_row_index(url))
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
        assert bar.display and [str(b.label) for b in bar.query(Button)] in (
            ["Open", "Merge", "Make draft", "Request review", "Close"], ["Open", "Merge", "Draft", "Review", "Close"])
        await pilot.click("#pr-open")
        await pilot.pause()
        assert opened == [URL.format(109)], "the full URL, the app's opener"
        assert app.query_one("#pr-list").has_focus and bar.display, "the table keeps the focus, and the buttons"


@pytest.mark.anyio
@pytest.mark.parametrize("n, answer, merged, said, desc", [
    (109, "alt+n", None, None, "No: nothing merged"),
    (109, "alt+y", (0, ""), "Merged PR109 (squash).", "Yes: merged the way the repo allows, gone from the list"),
    (109, "alt+y", (1, "X Pull request #109 is not mergeable: the base branch policy prohibits the merge."),
     "Couldn't merge PR109: gh says: X Pull request #109 is not mergeable", "a failure says why"),
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
@pytest.mark.parametrize("draft, button, ready, argv, caption, said, desc", [
    (True, "#pr-ready", (0, ""), ["--", URL.format(109)], "Make draft", "Marked PR109 ready", "a draft: marked ready"),
    (False, "#pr-ready", (0, ""), ["--undo", "--", URL.format(109)], "Mark ready", "Made PR109 a draft", "made a draft"),
    (True, "#prd-ready", (1, "X not allowed"), ["--", URL.format(109)], "Mark ready",
     "Couldn't mark PR109 ready: gh says: X not allowed", "from Detail; gh's refusal said, the caption kept"),
])
async def test_draft_ready_runs_gh_pr_ready_busy_then_refreshed(store, sid, github, draft, button, ready,
                                                               argv, caption, said, desc):
    """#212: one button, not asked first; busy while it runs, the PR as it now is afterwards."""
    github.tracked[1] = github.found[1] = node(109, draft=draft)
    github.ready = ready
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        p = await on_pr(app, pilot, URL.format(109))
        await landed(app, pilot)
        started = []
        p.run_worker = lambda work, **kw: started.append(work) if kw.get("group") == "prs-act" else None   # held, to see it busy
        await pilot.click(button)
        await pilot.pause()
        assert not isinstance(app.screen, tui.Confirm), f"{desc}: not asked first"
        b = app.query_one(button, Button)
        assert b.disabled and str(b.label).endswith("…"), f"{desc}: busy, {b.label}"
        del p.run_worker
        await asyncio.to_thread(started[0])
        await settled(app, pilot)
        assert github.readies == [argv], desc
        assert said in (p.outcome or "") or said in str(app.query_one("#prs-note").render()), f"{desc}: {p.outcome!r}"
        assert str(app.query_one("#pr-ready", Button).label) in (caption, pane.SHORT[caption]), f"{desc}: refreshed"


@pytest.mark.anyio
async def test_a_merge_that_cant_apply_is_off_and_its_tooltip_says_why(store, sid, github):
    """#212: PR108 fails its checks, so Merge is off, in the bar and the menu, saying why."""
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        await on_pr(app, pilot, URL.format(108))
        merge = app.query_one("#pr-merge", Button)
        assert merge.disabled and merge.tooltip == "Can't merge: checks are failing", merge.tooltip
        await pilot.click("#pr-merge")
        await pilot.pause()
        assert not isinstance(app.screen, tui.Confirm), "off: a click does nothing"
        table = app.query_one("#pr-list")
        box = table.content_region
        menu = app.query_one("#prs").menu_at(box.x + 1, box.y + table.header_height + table.get_row_index(URL.format(108)))
        assert ("merge", "Merge", True, "Can't merge: checks are failing") in menu.entries, menu.entries
        await landed(app, pilot)
        detail = app.query_one("#prd-merge", Button)
        assert detail.disabled and detail.tooltip == "Can't merge: checks are failing", f"Detail's: {detail.tooltip}"


@pytest.mark.anyio
async def test_draft_ready_on_marked_prs_acts_on_the_drafts_alone_and_review_skips_them(store, sid, github, monkeypatch):
    """#212: PR108 a draft, PR109 not, both marked: Mark ready, for PR108 alone; Request
    review asks for PR109 alone and says why it skipped PR108."""
    github.tracked[0] = github.found[0] = node(108, draft=True)
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        p = await on_pr(app, pilot, URL.format(108))
        app.query_one("#pr-list").set_marks({URL.format(108), URL.format(109)})   # the draft's grey sorts it after PR109
        p.marks_changed()
        await pilot.pause()
        button = app.query_one("#pr-ready", Button)
        assert str(button.label) in ("Mark ready", "Ready") and "the others are left" in button.tooltip, "a draft among them"
        await pilot.click("#pr-ready")
        await landed(app, pilot)
        await settled(app, pilot)
        assert github.readies == [["--", URL.format(108)]], "the draft alone"
        github.readies.clear()
        sent = []
        p.ctx = dataclasses.replace(p.ctx, ask=lambda q, past, then: then("#reviews"),
                                    send=lambda s, ref, text: sent.append(ref))
        p.rows = [{**r, "draft": r["number"] == 108} for r in p.rows]   # as before the refresh: PR108 still a draft
        p.act("review", [r for r in p.rows])
        await pilot.pause()
        assert sent == ["PR109"], sent
        assert "Not asked for PR108 (still a draft: mark it ready first)." in p.outcome, p.outcome


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
@pytest.mark.parametrize("selected, desc", [(True, "a session selected: not listed (#149), so not asked"),
                                            (False, "no session: off")])
async def test_request_review_of_a_pr_no_session_made(store, sid, github, monkeypatch, selected, desc):
    """In All, a PR no session made: listed only with no session selected (#149), when Request
    review has no session to ask."""
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        sent = []
        monkeypatch.setattr(app.client, "submit", lambda target, text, now=False: sent.append(target) or tui.Done())
        app.follow(sid) if selected else app.clear_filter()
        await pilot.pause()
        if selected:
            app.query_one("#prs").action_scope()
            await settled(app, pilot)
            assert "https://github.com/doug/mizpah/pull/7" not in app.query_one("#pr-list").rows, desc
            return
        p = await on_pr(app, pilot, "https://github.com/doug/mizpah/pull/7", scope="all")
        assert app.query_one("#pr-review", Button).disabled is not selected, desc
        p.review([p.selected()])
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
        assert [str(b.label) for b in menu.query(Button)] == ["Open", "Merge", "Make draft", "Request review", "Close"]
        await pilot.press("enter")
        await pilot.pause()
        assert opened == [URL.format(109)], f"{size}: Open, on the row clicked"
        assert table.has_focus and app.query_one("#prs-actions").display is (size != (80, 24)), \
            f"{size}: the table focused, its buttons shown where they leave it its rows"


@pytest.mark.anyio
@pytest.mark.parametrize("size, shown", [((100, 30), True), ((80, 24), False), ((160, 40), True)])
async def test_the_buttons_fit_the_pane_and_each_click_acts(store, sid, github, monkeypatch, size, shown):
    """Review of #143: in a narrow pane the buttons stack rather than fall outside it; review
    of #156: in one too short for them and its rows, they hide, the table's rows kept, and
    Detail's buttons act instead."""
    app = WheelhouseApp(store)
    async with app.run_test(size=size) as pilot:
        await settled(app, pilot)
        acted = []
        p = await on_pr(app, pilot, URL.format(109))
        monkeypatch.setattr(p, "act", lambda action, rows: acted.append((action, [r["number"] for r in rows])))
        table, bar = app.query_one("#pr-list"), app.query_one("#prs-actions")
        assert table.region.height - table.header_height >= 2, f"{size}: both PRs' rows show, {table.region}"
        assert bar.display is shown and app.query_one("#pr-detail-actions").display, f"{size}: Detail's buttons whatever"
        if not shown:
            bar = app.query_one("#pr-detail-actions")
        pane_box = bar.parent.content_region
        for b in bar.query(Button):
            assert pane_box.contains_region(b.region) and b.region.width >= len(str(b.label)), \
                f"{size}: {b.id} {b.region} {str(b.label)!r} inside {pane_box}"
            await pilot.click(f"#{b.id}")
            await pilot.pause()
        assert acted == [(a, [109]) for a in ("open", "merge", "ready", "review", "close")], f"{size}: every click acts"


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
        await asyncio.to_thread(p.done_each, "merge", [p.selected()])   # off the app's thread, as the pane's worker
        await pilot.pause()
        assert p.outcome == said and "gh says" not in p.outcome, desc


@pytest.mark.anyio
async def test_request_review_shows_a_title_and_past_targets_as_text_not_markup(store, sid, github):
    """As #141 escapes them: a PR title or a Slack target holding markup shows as written."""
    store.set_setting(pane.TARGETS, json.dumps(["[b]#eng[/b]"]))
    github.tracked[1] = {**github.tracked[1], "title": "[@click=app.quit]Change[/]"}
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        p = await on_pr(app, pilot, URL.format(109))
        p.review([p.selected()])
        await pilot.pause()
        ask = app.screen
        prompt = ask.query_one(".dialog-prompt")
        shown = "".join(s.text for s in prompt.render_lines(prompt.region.reset_offset))
        assert "[@click=app.quit]Change[/]" in shown, f"the title as written: {shown!r}"
        options = ask.query_one(OptionList)
        listed = "".join(s.text for s in options.render_lines(options.region.reset_offset))
        assert "[b]#eng[/b]" in listed, f"the past target as written: {listed!r}"


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(160, 40), (100, 30)])
@pytest.mark.parametrize("pick, inbox_lit, prs_lit, shown, desc", [
    ("question", True, False, "question", "an inbox item the PRs pane doesn't list: its highlight hidden"),
    ("PR109", True, True, "PR109", "a PR item in both: both highlight it"),
    ("hidden PR108", False, True, "PR108", "a PR item the inbox doesn't list (its parent shut): the inbox's hidden"),
    ("PR7", False, True, None, "a PR no session made: nothing selected, the inbox's hidden, Detail says why"),
])
async def test_a_pick_in_one_pane_clears_the_others_highlight(store, sid, github, size, pick, inbox_lit, prs_lit,
                                                               shown, desc):
    """#148: Detail shows what was picked last, and a list that doesn't have it highlights nothing."""
    q = store.post_item(sid, "question", "Which way?", status="open")
    task = store.post_item(sid, "task", "Ship it", status="running")
    store.post_pr(sid, URL.format(108), "Change 108", parent=task)
    app = WheelhouseApp(store)
    async with app.run_test(size=size) as pilot:
        await settled(app, pilot)
        app.toggle_children(f"{sid}|{task}")   # PR108 out of the inbox, under its shut parent
        await pilot.pause()
        if pick == "question":
            await on_pr(app, pilot, URL.format(108))
            await pick_in_inbox(app, pilot, f"{sid}|{q}")
        elif pick == "PR109":
            await pick_in_inbox(app, pilot, f"{sid}|{q}")
            await on_pr(app, pilot, URL.format(109))
        else:
            await pick_in_inbox(app, pilot, f"{sid}|{q}")
            await on_pr(app, pilot, URL.format(108) if pick == "hidden PR108" else "https://github.com/doug/mizpah/pull/7",
                        scope="mine" if pick == "hidden PR108" else "all")
        await pilot.pause()
        table = app.query_one("#pr-list")
        assert app.selected == ((sid, {"question": q}.get(pick, shown)) if shown else None), desc
        assert app.items_table.show_cursor is inbox_lit, f"{desc}: the inbox's highlight"
        assert table.show_cursor is prs_lit, f"{desc}: the PRs pane's highlight"
        if inbox_lit:
            assert app.items_table.cursor_key() == f"{sid}|{app.selected[1]}", f"{desc}: on the item shown"
        if prs_lit:
            assert table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value.endswith(
                {"PR7": "/7"}.get(pick, pick[-3:])), f"{desc}: on the PR shown"
        if shown is None:
            assert "PR7 in mizpah isn't an item" in app._detail_text, f"{desc}: Detail says why"
        if not inbox_lit:   # Delete in the inbox closes nothing: no row there is picked
            before = [(i["ref"], i["status"]) for i in store.items(sid)]
            app.items_table.focus()
            await pilot.press("delete")
            await pilot.pause()
            assert [(i["ref"], i["status"]) for i in store.items(sid)] == before, f"{desc}: Delete closes nothing"
            assert app.screen is app.screen_stack[0], f"{desc}: and asks nothing"
            # a click on the inbox brings its highlight back, where clicked, and shows that row
            await pilot.click("#items", offset=(4, 1 + app.items_table.get_row_index(f"{sid}|{q}")))
            await pilot.pause()
            assert app.items_table.show_cursor and app.selected == (sid, q), f"{desc}: clicked back"
            assert not table.show_cursor, f"{desc}: the PRs pane's hidden in turn"
            assert isinstance(app.screen, tui.Screen) and not isinstance(app.screen, tui.ThreadView), \
                f"{desc}: the click selects, opening nothing"


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(160, 40), (100, 30)])
@pytest.mark.parametrize("follow, scope, listed, title, desc", [
    (None, "mine", ["PR108", "PR109", "PR5"], "PRs · mine", "none selected: every session's"),
    (None, "all", ["PR108", "PR7", "PR109", "PR5"], "PRs · all", "none selected, all: the search's too"),
    ("sid", "mine", ["PR108", "PR109"], "PRs · mine · demo", "a session selected: its PRs alone"),
    ("sid", "all", ["PR108", "PR109"], "PRs · all · demo", "a session selected, all: still its PRs alone"),
    ("other", "mine", ["PR5"], "PRs · mine · fix [/] it [b]x[/b]", "another session: its PR alone, its name as written"),
])
async def test_the_pane_follows_the_selected_session(store, sid, github, size, follow, scope, listed, title, desc):
    """#149: as the inbox, the selected session's PRs, or every one with none selected."""
    store.rename(sid, "demo")
    other = store.create_session("/tmp", name="fix [/] it [b]x[/b]")   # markup in a name raised MarkupError
    store.post_pr(other, URL.format(5), "Change 5")
    github.tracked.append(node(5))
    github.found.append(node(5))
    app = WheelhouseApp(store)
    async with app.run_test(size=size) as pilot:
        await settled(app, pilot)
        p = app.query_one("#prs")
        if scope != p.scope:
            p.action_scope()
            await settled(app, pilot)
        if follow:
            app.follow(sid if follow == "sid" else other)
            await pilot.pause()
        assert cells(app) == listed and p.border_title == Content(title).markup, desc   # read back as markup, escaped
        app.unfilter() if follow else None
        await pilot.pause()
        every = ["PR108", "PR109", "PR5"] if scope == "mine" else ["PR108", "PR7", "PR109", "PR5"]
        assert cells(app) == every, f"{desc}: none selected again, every one"


# #145: the PR's actions and detail in Detail

async def landed(app, pilot):
    await pilot.pause(pane.SETTLE * 2)   # the highlight rested: its detail asked for
    for _ in range(3):
        await app.workers.wait_for_complete()
        await pilot.pause()


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(160, 40), (100, 30), (80, 24)])
async def test_detail_shows_a_prs_buttons_and_detail_as_text(store, sid, github, monkeypatch, size):
    app = WheelhouseApp(store)
    async with app.run_test(size=size) as pilot:
        await settled(app, pilot)
        bar, body = app.query_one("#pr-detail-actions"), app.query_one("#pr-detail")
        app.follow(sid)   # Detail on the session's conversation
        await pilot.pause()
        assert not bar.display and not body.display, f"{size}: no PR in Detail, no parts"
        await on_pr(app, pilot, URL.format(109))
        await landed(app, pilot)
        assert app.selected == (sid, "PR109") and bar.display and body.display, f"{size}: Detail's PR, its parts"
        assert github.details.count(109) == 1, f"{size}: its detail fetched once: {github.details}"
        assert "Why: [b]bold[/b] isn't markup" in body.render().plain, f"{size}: the description as written"
        detail = app.query_one("#detail-pane").content_region
        for b in bar.query(Button):
            assert detail.contains_region(b.region), f"{size}: {b.id} {b.region} inside Detail {detail}"
        acted = []
        monkeypatch.setattr(app.query_one("#prs"), "act", lambda action, rows: acted.append((action, [r["number"] for r in rows])))
        await pilot.click("#prd-close")
        assert acted == [("close", [109])], f"{size}: Detail's buttons act on its PR"
        monkeypatch.setattr(pane, "CLOCK", lambda: time.time() + fetch.DETAIL_GAP)
        await pilot.press("alt+r")   # the table still focused
        await landed(app, pilot)
        await settled(app, pilot)
        assert github.details.count(109) == 2, f"{size}: r fetches it again"
        app.follow(sid)
        await pilot.pause()
        assert not bar.display and not body.display, f"{size}: the conversation, not a PR: no parts"


@pytest.mark.anyio
async def test_a_pr_no_session_made_shows_its_detail_and_link_in_detail(store, sid, github):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        await on_pr(app, pilot, "https://github.com/doug/mizpah/pull/7", scope="all")
        await landed(app, pilot)
        text = app.query_one("#pr-detail").render().plain
        assert app.selected is None and text.startswith("doug/mizpah/pull/7\nChange 7"), text
        assert app.query_one("#prd-review", Button).disabled, "no session to ask"


# #146: marks, and actions on every PR marked

@pytest.mark.anyio
@pytest.mark.parametrize("keys, clicks, want, desc", [
    (["ctrl+space", "down", "ctrl+space"], [], {108, 109}, "Ctrl+Space marks the highlighted row, then another"),
    (["shift+down"], [], {108, 109}, "Shift+Down takes the range"),
    (["ctrl+space", "down", "ctrl+space", "escape"], [], set(), "Esc unmarks"),
    ([], [(109, {"control": True})], {108, 109}, "Ctrl+click: the highlighted row and the one clicked"),
    ([], [(109, {"shift": True})], {108, 109}, "Shift+click: the range"),
    ([], [(109, {"control": True}), (109, {"control": True})], {108}, "a second Ctrl+click unmarks"),
    ([], [(109, {"control": True}), (108, {})], set(), "a plain click starts afresh"),
])
async def test_prs_are_marked_as_the_inbox_marks(store, sid, github, keys, clicks, want, desc):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        p = await on_pr(app, pilot, URL.format(108))
        table = app.query_one("#pr-list")
        for key in keys:
            await pilot.press(key)
        for n, mods in clicks:
            await pilot.click("#pr-list", offset=(3, table.header_height + table.get_row_index(URL.format(n))), **mods)
        await pilot.pause()
        assert table.marked == {URL.format(n) for n in want}, desc
        assert p.border_subtitle == (f"{len(want)} marked" if want else ""), f"{desc}: its border counts them"
        assert all(("#3a1060" in str(table.get_row(URL.format(n))[0].style)) is (n in want) for n in (108, 109)), \
            f"{desc}: the marked rows' background"
        assert [r["number"] for r in p.targets()] == (sorted(want) or [p.selected()["number"]]), f"{desc}: the marked, else the cursor's"


async def marked_both(app, pilot):
    p = await on_pr(app, pilot, URL.format(108))
    await pilot.press("shift+down")
    await pilot.pause()
    return p


@pytest.mark.anyio
@pytest.mark.parametrize("answer, closed, said, desc", [
    ("alt+n", {}, None, "No: nothing closed"),
    ("alt+y", {}, "Closed PR108.\nClosed PR109.", "Yes: each closed, each said"),
    ("alt+y", {URL.format(109): (1, "X Could not close: not allowed")},
     "Closed PR108.\nCouldn't close PR109: gh says: X Could not close: not allowed", "one refused: said, the other closed"),
])
async def test_close_asks_naming_every_pr_marked_and_says_what_came_of_each(store, sid, github, answer, closed, said, desc):
    github.closed = closed
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        p = await marked_both(app, pilot)
        await pilot.click("#pr-close")
        await pilot.pause()
        assert isinstance(app.screen, tui.Confirm) and app.screen.focused.id == "no", f"{desc}: asked, No focused"
        assert all(f"PR{n} (Change {n}) in wheelhouse" in app.screen.prompt for n in (108, 109)), app.screen.prompt
        await pilot.press(answer)
        await landed(app, pilot)
        await settled(app, pilot)
        assert github.closes == ([["--", URL.format(n)] for n in (108, 109)] if answer == "alt+y" else []), desc
        assert (p.outcome == said) if said else not p.outcome, f"{desc}: {p.outcome!r}"


@pytest.mark.anyio
async def test_merge_and_the_menu_act_on_every_pr_marked(store, sid, github):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        await marked_both(app, pilot)
        table = app.query_one("#pr-list")
        args = _get_mouse_message_arguments(table, (3, table.header_height + table.get_row_index(URL.format(108))), button=3)
        for cls in (events.MouseDown, events.MouseUp):
            await app.on_event(cls(**args))
        await pilot.pause()
        menu = app.screen
        assert isinstance(menu, tui.PaneMenu) and menu.heading == "2 marked PRs", \
            "a marked row's menu: for them all"
        await pilot.click(f"#{next(b.id for b in menu.query(Button) if str(b.label) == 'Merge')}")
        await pilot.pause()
        assert isinstance(app.screen, tui.Confirm) and "Merge these 2 PRs" in app.screen.prompt, "asked first"
        assert "PR108 (Change 108) in wheelhouse: checks are failing" in app.screen.prompt \
            and "are tried too" in app.screen.prompt, f"what blocks each named: {app.screen.prompt}"
        await pilot.press("alt+y")
        await landed(app, pilot)
        await settled(app, pilot)
        assert github.merges == [["--squash", "--", URL.format(n)] for n in (108, 109)], "each merged in turn"


@pytest.mark.anyio
async def test_arrowing_past_prs_fetches_only_the_one_it_rests_on_and_a_failure_is_tried_again(store, sid, github, monkeypatch):
    """Review of #156: a detail asked on every highlight ran the route out of queries
    (daemon.MODULE_GETS), the list's ask with them; and a failure was kept as fetched."""
    for n in range(1, 7):
        store.post_pr(sid, URL.format(n), f"Change {n}")
        github.tracked.append(node(n))
        github.found.append(node(n))
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await on_first(app, pilot)
        p = await on_pr(app, pilot, URL.format(108))
        await until(pilot, lambda: URL.format(108) in p.detail_asked and p.detail_timer is None and not p.detail_asking,
                    "PR108's detail fetched, none other waited on")
        github.details.clear()
        clock = Clock()   # the highlight's rest on the test's own time, however long six keys take
        monkeypatch.setattr(p, "set_timer", clock.after)
        out, real = [], p.ctx.call
        monkeypatch.setattr(p, "ctx", dataclasses.replace(p.ctx, call=lambda path, body: out.append(path) or real(path, body)))
        for _ in range(6):
            await pilot.press("down")
        await pilot.pause()
        assert github.details == [] and len([t for t in clock.timers if not t.stopped]) == 1, \
            "passing PRs: none asked, one wait left"
        clock.advance(pane.SETTLE)
        await until(pilot, lambda: github.details and not p.detail_asking, "the rested PR's detail landed")
        at = p.detail_row()["number"]
        assert github.details == [at] and at != 108, f"the PR it rested on alone: {github.details}"
        assert sum(path.startswith("detail") for path in out) == 1, "one detail asked"

        def refuse(path, body):
            if path.startswith("detail"):
                raise api.Refused(503, "module prs is busy")
            return real(path, body)
        p.ctx = dataclasses.replace(p.ctx, call=refuse)
        p.detail_asked.clear()   # none fetched yet
        await pilot.press("down")
        await pilot.pause()
        clock.advance(pane.SETTLE)
        url = p.detail_row()["url"]
        await until(pilot, lambda: "error" in p.details.get(url, {}) and not p.detail_asking, "the refusal landed")
        assert url not in p.detail_asked and "busy" in p.details[url]["error"], "a failure isn't counted as fetched"
        p.ctx = dataclasses.replace(p.ctx, call=real)
        monkeypatch.setattr(pane, "RETRY", 0)
        p.want_detail()
        clock.advance(pane.SETTLE)
        await until(pilot, lambda: url in p.detail_asked, "tried again")
        assert url in p.detail_asked and "error" not in p.details[url], "tried again, and kept"


@pytest.mark.anyio
async def test_a_bulk_action_tries_every_pr_and_says_what_each_came_to(store, sid, github, monkeypatch):
    """Review of #156: a merge past the hub's wait (504) left every PR after it refused busy
    (503). Now each is sent again until taken, and the one past the wait asked what it came to."""
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        p = await on_pr(app, pilot, URL.format(109))
        monkeypatch.setattr(pane, "PAUSE", 0)
        said = {"busy": 2, "pending": 1}

        asked, sent_tokens = [], []

        def hub(path, body):
            if body is not None:
                sent_tokens.append(body["token"])
            if path.startswith("result"):
                asked.append(path)
                if said["pending"]:
                    said["pending"] -= 1
                    return {"pending": True}
                return {"method": "squash"}
            if body["url"] == URL.format(108):
                raise api.Refused(504, "the module took longer than 10 s and carries on")
            if said["busy"]:
                said["busy"] -= 1
                raise api.Refused(503, "module prs is busy: a command is still under way. Try again shortly")
            return {"error": "X Pull request #109 is not mergeable"}
        p.ctx = dataclasses.replace(p.ctx, call=hub)
        await asyncio.to_thread(p.done_each, "merge", [p.row(URL.format(108)), p.row(URL.format(109))])
        await pilot.pause()
        assert p.outcome.splitlines() == ["Merged PR108 (squash).",
                                          "Couldn't merge PR109: gh says: X Pull request #109 is not mergeable"], p.outcome
        assert said == {"busy": 0, "pending": 0}, "sent again while busy; asked again while pending"
        tokens = {path.partition("token=")[2] for path in asked}
        assert tokens == {sent_tokens[0]} and len(set(sent_tokens)) == 2, \
            "the result asked by the 504's own token, each command its own"


@pytest.mark.anyio
async def test_a_hub_without_the_result_route_says_the_outcome_is_unknown_at_once(store, sid, github, monkeypatch):
    """Review of #156: a hub without `result` (404) was polled WAIT_BUSY seconds."""
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        p = await on_pr(app, pilot, URL.format(109))
        # its detail fetched first: the highlight reaches PR109 from the 💬 row (#236), so later
        await until(pilot, lambda: URL.format(109) in p.detail_asked and p.detail_timer is None and not p.detail_asking,
                    "PR109's detail fetched")
        monkeypatch.setattr(pane, "PAUSE", 0)
        asked = []

        def hub(path, body):
            if body is None:
                asked.append(path)
                raise api.Refused(404, "no route GET /m/prs/result")
            raise api.Refused(504, "the module took longer than 10 s and carries on")
        p.ctx = dataclasses.replace(p.ctx, call=hub)
        await asyncio.to_thread(p.done_each, "close", [p.row(URL.format(109))])
        await pilot.pause()
        assert len(asked) == 1 and "outcome is unknown" in p.outcome and "GitHub" in p.outcome, p.outcome


@pytest.mark.anyio
async def test_actions_on_different_prs_at_once_keep_each_outcome(store, sid, github):
    """Review of #156: a close landing after a merge wrote over the merge's line."""
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        p = await on_pr(app, pilot, URL.format(109))
        a, b = p.row(URL.format(108)), p.row(URL.format(109))
        p.acted("merge", [(a, {"method": "squash"})])
        p.acted("close", [(b, {"closed": True})])
        assert p.outcome == "Merged PR108 (squash).\nClosed PR109.", p.outcome


@pytest.fixture
def hosted(store, sid, monkeypatch):
    """The session run in the wheelhouse (SDK mode), live until a test says otherwise
    (hosted["liveness"]): one that can be compacting."""
    from claude_wheelhouse import liveness
    store.set_runner(sid, "sdk")
    state = {"liveness": "live"}
    monkeypatch.setattr(liveness, "status", lambda s, waking=False: state["liveness"])
    return state


async def compacting(app, pilot, store, sid, hosted, end: str | None = None):
    """The session's host records a compaction starting (end None), or its end: "compacted",
    or "host gone", stopped mid-way with its line left (store.compacting_now). The app ticks."""
    if end == "host gone":
        hosted["liveness"] = "dead"
    else:
        store.set_activity(sid, "idle · compacted" if end else "compacting: collecting notes")
    app.refresh_data()
    await pilot.pause()


@pytest.mark.anyio
@pytest.mark.parametrize("button, action, end", [
    ("prd-open", "open", "compacted"), ("prd-merge", "merge", "compacted"),
    ("prd-review", "review", "compacted"), ("prd-close", "close", "compacted"),
    ("prd-close", "close", "host gone"),
])
async def test_a_compacting_sessions_pr_in_detail_dims_and_its_buttons_lock(store, sid, github, hosted, monkeypatch,
                                                                             button, action, end):
    """#165: Detail's whole view of a locked PR item dims, its buttons off, as Detail does (#150);
    all live again once it's compacted, or its host stopped mid-way."""
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        await on_pr(app, pilot, URL.format(109))
        await landed(app, pilot)
        p = app.query_one("#prs")
        acted = []
        monkeypatch.setattr(p, "act", lambda a, rows: acted.append((a, [r["number"] for r in rows])))
        parts = [app.query_one("#detail-more"), app.query_one("#detail-bar")]
        await compacting(app, pilot, store, sid, hosted)
        assert app.selected == (sid, "PR109") and app.detail.has_class("compacting"), f"{button}: Detail dimmed"
        assert all(w.styles.opacity == 0.5 for w in parts), f"{button}: the PR's parts dimmed with it"
        assert app.query_one(f"#{button}", Button).is_disabled, f"{button}: off while compacting"
        await pilot.click(f"#{button}")
        await pilot.pause()
        assert acted == [], f"{button}: a click does nothing"
        await compacting(app, pilot, store, sid, hosted, end)
        assert all(w.styles.opacity == 1 for w in [app.detail, *parts]), f"{button}, {end}: undimmed"
        assert not app.query_one(f"#{button}", Button).is_disabled, f"{button}, {end}: on again"
        await pilot.click(f"#{button}")
        await pilot.pause()
        assert acted == [(action, [109])], f"{button}, {end}: and acts"


@pytest.mark.anyio
@pytest.mark.parametrize("start, lit, desc", [
    ("Q1", None, "Detail on an item the pane doesn't list: its highlight hidden (#148)"),
    ("PR108", "/108", "Detail on a PR: its highlight back there"),
])
async def test_picking_a_compacting_sessions_pr_in_the_pane(store, sid, github, hosted, monkeypatch, start, lit, desc):
    """#165: the PRs pane's pick of a PR item whose session is compacting leaves Detail on
    what it showed, as a pick of the item's inbox row does (#150), and the pane's highlight
    with it; once compacted, a pick of it shows it."""
    q = store.post_item(sid, "question", "Which way?", status="open")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await settled(app, pilot)
        if start == "Q1":
            await pick_in_inbox(app, pilot, f"{sid}|{q}")
        else:
            await on_pr(app, pilot, URL.format(108))
            await landed(app, pilot)
        await compacting(app, pilot, store, sid, hosted)
        notes = []
        monkeypatch.setattr(app, "notify", lambda text, **kw: notes.append(text))
        p = await on_pr(app, pilot, URL.format(109))
        await landed(app, pilot)
        table = app.query_one("#pr-list")
        assert app.selected == (sid, start) and notes == [tui.COMPACTING_LOCKED], f"{desc}: not picked, said why"
        here = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value if table.show_cursor else None
        assert (here and here[-4:]) == lit and p.shown == here, f"{desc}: the highlight on what Detail shows"
        await compacting(app, pilot, store, sid, hosted, "compacted")
        await on_pr(app, pilot, URL.format(109))
        await landed(app, pilot)
        assert app.selected == (sid, "PR109"), f"{desc}: compacted, picked as ever"
