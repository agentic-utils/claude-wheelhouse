"""#391: the actions #386 left on the UI thread, now acknowledged at once and sent off it: the
effort slider, Recall, Reopen, drag reorder, doc save, session steps, the tour's writes, and the
reads a dialog needs before it opens. Each handler returns before the daemon is asked; what it
did lands after, or, failing, is undone and said."""
import asyncio
import threading
import types

import pytest

from claude_wheelhouse.hub import Done
from claude_wheelhouse import steps
from claude_wheelhouse.store import TOUR_AT
from claude_wheelhouse.tui import (AdoptSession, Confirm, DocEdit, FirstSteps, Reading, SaveConflict, SaveFailed,
                                   SessionInfo, SessionSteps, WheelhouseApp)
from test_pins import notices, post
from test_tui import pick

REFUSED = "refused for the test"


def effort_shown(app, sid):
    return next(s for s in app.module_sessions() if s["id"] == sid)["effort"]


def rows(app):
    return [k for k, _, _ in app.items_table.units()]


async def setup_recall(app, pilot, store, sid, tmp_path, ctx):
    t = ctx["t"] = post(store, sid, tmp_path, "task", "t")
    store.queue(sid, "taken back", t)
    app.refresh_data()
    await pilot.pause()
    table = app.items_table
    table.move_cursor(row=table.get_row_index(f"{sid}|{t}"))
    table.focus()
    await pilot.pause()


async def setup_items(app, pilot, store, sid, tmp_path, ctx):
    ctx["t1"], ctx["t2"] = post(store, sid, tmp_path, "task", "one"), post(store, sid, tmp_path, "task", "two")
    ctx["q"] = post(store, sid, tmp_path, "question", "which?", status="closed")
    app.refresh_data()
    await pilot.pause()
    ctx["order"] = rows(app)


async def setup_doc(app, pilot, store, sid, tmp_path, ctx):
    post(store, sid, tmp_path, "doc", "plan")
    ctx["path"] = str(tmp_path / "plan.md")


async def nothing(app, pilot, store, sid, tmp_path, ctx):
    pass


def moved(app, sid, ctx):
    """The lower of the two tasks dropped before the higher: a move, whichever way they sort."""
    upper, lower = sorted((f"{sid}|{ctx['t1']}", f"{sid}|{ctx['t2']}"), key=ctx["order"].index)
    app.drop_row(app.items_table, lower, upper, None)


# action: (setup, the client call held, its answer when done (None: the real one), the handler,
#          shown while held, done once landed, failed once landed)
ACTIONS = {
    "effort": (nothing, "set_effort", lambda *a: Done(), lambda app, sid, ctx: app.set_effort(sid, "max"),
               lambda app, store, sid, ctx: effort_shown(app, sid) == "max",
               lambda app, store, sid, ctx: not app.efforts and not notices(app),
               lambda app, store, sid, ctx: not app.efforts and effort_shown(app, sid) != "max"),
    "recall": (setup_recall, "unqueue", None, lambda app, sid, ctx: app.action_recall(),
               lambda app, store, sid, ctx: (setattr(app.answer, "text", "typed meanwhile"), True)[1],
               lambda app, store, sid, ctx: app.answer.text == "typed meanwhile\n\ntaken back" and not store.drafts(sid),
               lambda app, store, sid, ctx: app.answer.text == "typed meanwhile" and store.drafts(sid)),
    "reopen": (setup_items, "close", None, lambda app, sid, ctx: app.reopen((sid, ctx["q"]), set()),
               lambda app, store, sid, ctx: store.item(sid, ctx["q"])["status"] == "closed",
               lambda app, store, sid, ctx: store.item(sid, ctx["q"])["status"] == "answered"
               and f"reopened {ctx['q']} as answered" in notices(app),
               lambda app, store, sid, ctx: store.item(sid, ctx["q"])["status"] == "closed"),
    "drag": (setup_items, "reorder_items", None, moved,
             lambda app, store, sid, ctx: rows(app) != ctx["order"],   # shown moved at once
             lambda app, store, sid, ctx: rows(app) != ctx["order"],
             lambda app, store, sid, ctx: rows(app) == ctx["order"]),   # refused: as fetched again
    "doc save": (setup_doc, "write_doc", None, lambda app, sid, ctx: app.save_doc(ctx["path"], "# saved\n", None, force=True),
                 lambda app, store, sid, ctx: open(ctx["path"]).read() == "# plan\n",
                 lambda app, store, sid, ctx: open(ctx["path"]).read() == "# saved\n" and "Saved plan.md" in notices(app),
                 lambda app, store, sid, ctx: open(ctx["path"]).read() == "# plan\n" and isinstance(app.screen, SaveFailed)
                 and "your edits are kept" in str(app.screen.prompt)),
    "steps": (nothing, "steps_table", None, lambda app, sid, ctx: app.action_session_steps(),
              lambda app, store, sid, ctx: isinstance(app.screen, Reading),
              lambda app, store, sid, ctx: isinstance(app.screen, SessionSteps),
              lambda app, store, sid, ctx: not isinstance(app.screen, (Reading, SessionSteps))),
    "first press": (nothing, "steps_table", None,
                    lambda app, sid, ctx: app.first_press("relaunch", lambda: ctx.setdefault("ran", []).append(1)),
                    lambda app, store, sid, ctx: app.steps_reading and "ran" not in ctx,
                    lambda app, store, sid, ctx: isinstance(app.screen, FirstSteps) and "ran" not in ctx,
                    lambda app, store, sid, ctx: not isinstance(app.screen, FirstSteps) and ctx.get("ran") == [1]),
    "adopt": (nothing, "adoptable", None, lambda app, sid, ctx: app.action_adopt(),
              lambda app, store, sid, ctx: isinstance(app.screen, Reading),
              lambda app, store, sid, ctx: isinstance(app.screen, AdoptSession),
              lambda app, store, sid, ctx: not isinstance(app.screen, (Reading, AdoptSession))),
    "session info": (nothing, "session_info", None, lambda app, sid, ctx: app.show_session_info(sid),
                     lambda app, store, sid, ctx: isinstance(app.screen, Reading),
                     lambda app, store, sid, ctx: isinstance(app.screen, SessionInfo),
                     lambda app, store, sid, ctx: not isinstance(app.screen, (Reading, SessionInfo))
                     and any("can't show the session info" in n for n in notices(app))),
}


@pytest.mark.anyio
@pytest.mark.parametrize("via", ["threaded"], indirect=True)
@pytest.mark.parametrize("answer", ["done", "refused"])
@pytest.mark.parametrize("action", list(ACTIONS))
async def test_an_action_returns_before_the_daemon_answers(store, sid, tmp_path, via, first_press, action, answer):
    """#391: the handler returns at once, its daemon call made off the UI thread; then what it
    did lands, or, refused, is undone and said."""
    setup, method, done_value, act, meanwhile, landed, failed = ACTIONS[action]
    desc = f"{action}, {answer}"
    app = WheelhouseApp(store)
    go, sent, threads, ctx = threading.Event(), threading.Event(), [], {}
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        await setup(app, pilot, store, sid, tmp_path, ctx)
        real = getattr(app.client, method)

        def held(*args, **kw):
            threads.append(threading.get_ident())
            sent.set()
            go.wait(10)
            if answer == "refused":
                raise ValueError(REFUSED)
            return done_value(*args) if done_value else real(*args, **kw)
        setattr(app.client, method, held)
        ui = threading.get_ident()
        act(app, sid, ctx)   # returns with the call still held: it never waits on the daemon
        await pilot.pause()
        assert await asyncio.get_running_loop().run_in_executor(None, sent.wait, 10), f"{desc}: sent"
        assert threads and ui not in threads, f"{desc}: the daemon asked off the UI thread"
        assert meanwhile(app, store, sid, ctx), f"{desc}: acknowledged before the daemon answers"
        go.set()
        for _ in range(50):
            await app.workers.wait_for_complete()
            await pilot.pause()
            if not (app.sending or app.behind):
                break
        await pilot.pause()
        if answer == "done":
            assert landed(app, store, sid, ctx), f"{desc}: {notices(app)}"
        else:
            assert failed(app, store, sid, ctx), f"{desc}: {notices(app)}"
            if action not in ("doc save", "first press", "session info"):   # said in a prompt, or let through
                assert any(REFUSED in n for n in notices(app)), f"{desc}: said: {notices(app)}"


@pytest.mark.anyio
@pytest.mark.parametrize("via", ["threaded"], indirect=True)
@pytest.mark.parametrize("answer, kept, desc", [
    ("done", "tour:answer", "kept: shown at once, and the daemon has it"),
    ("refused", None, "not kept: shown at once, then as the daemon has it once a fresh inbox comes"),
])
async def test_the_tour_keeps_its_place_off_the_ui_thread(store, sid, via, answer, kept, desc):
    """#391: the tour's place is shown kept at once and written off the UI thread (aside)."""
    app = WheelhouseApp(store)
    go, sent, threads = threading.Event(), threading.Event(), []
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        real = app.client.quietly

        def held(path, body, refetch="later"):
            if path != "/tour/at":
                return real(path, body, refetch)
            threads.append(threading.get_ident())
            sent.set()
            go.wait(10)
            return real(path, body, refetch) if answer == "done" else False
        app.client.quietly = held
        app.client.tour_at("tour:answer")
        assert app.client.tour_state()["at"] == "tour:answer", f"{desc}: shown at once"
        assert await asyncio.get_running_loop().run_in_executor(None, sent.wait, 10), desc
        assert threading.get_ident() not in threads, f"{desc}: written off the UI thread"
        app.refresh_data()   # an inbox fetched before it was made: it stays shown
        await pilot.pause()
        assert app.client.tour_state()["at"] == "tour:answer", f"{desc}: a stale fetch doesn't undo it"
        go.set()
        for _ in range(200):   # a fresh inbox, fetched after the write, shows it as the daemon has it
            app.refresh_data()
            await pilot.pause(0.02)
            if not app.client.tour_kept and app.client.tour_state().get("at") == kept:
                break
        assert store.setting(TOUR_AT) == kept, desc
        assert app.client.tour_state().get("at") == kept and not app.client.tour_kept, desc


@pytest.mark.parametrize("since, kept, desc", [
    (False, None, "nothing stashed since the save was asked: the edits go, saved"),
    (True, "# saved\nmore\n", "stashed since (typed, then the editor closed): kept over the new base, never lost"),
])
def test_a_save_landing_keeps_what_was_typed_since(tmp_path, since, kept, desc):
    """#391: a doc save lands after the editor has moved on; edits stashed since it was asked
    (doc_edits) stay, their base the text saved."""
    path = str(tmp_path / "plan.md")
    asked = DocEdit("# plan\n", "# saved\n", "# saved\n", (0, 0))
    now = DocEdit("# plan\n", "# saved\nmore\n", "# saved\nmore\n", (0, 0)) if since else asked
    app = types.SimpleNamespace(doc_edits={path: now}, screen_stack=[], each_pane=lambda *a: None)
    WheelhouseApp.doc_written(app, path, "# saved\n", 7, since=(None, asked))
    edit = app.doc_edits.get(path)
    assert (edit and edit.text) == kept, desc
    if edit:
        assert (edit.base, edit.mtime) == ("# saved\n", 7), desc


class Held:
    """A client call held until released (go), noting each time it's made (sent)."""

    def __init__(self, real, times: int = 1):
        self.real, self.go, self.sent, self.held = real, threading.Event(), threading.Event(), times

    def __call__(self, *args, **kw):
        if self.held:
            self.held -= 1
            self.sent.set()
            self.go.wait(10)
        return self.real(*args, **kw)

    async def wait_sent(self):
        assert await asyncio.get_running_loop().run_in_executor(None, self.sent.wait, 10), "sent"


async def lane_landed(app, pilot):
    for _ in range(100):
        await app.workers.wait_for_complete()
        await pilot.pause()
        if not (app.sending or app.behind or app.steps_reading):
            return


@pytest.mark.anyio
@pytest.mark.parametrize("via", ["threaded"], indirect=True)
async def test_a_press_waiting_on_the_steps_acts_on_the_session_pressed_on(store, sid, tmp_path, via, first_press):
    """#391 review: Relaunch pressed on A, the selection moved to B while the steps are read:
    A relaunched, not B. And a press of another button meanwhile isn't dropped."""
    other = store.create_session(str(tmp_path), name="other")
    store.set_setting(steps.CHOSEN, "1")
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        await pick(app, pilot, sid)
        assert app.current_session() == sid
        app.client.steps_table = held = Held(app.client.steps_table)
        got = []
        app.relaunch_now = lambda s: got.append(("relaunch", s))
        app.relaunch_pressed()
        await held.wait_sent()
        await pick(app, pilot, other)
        assert app.current_session() == other
        app.end_now = lambda s: got.append(("end", s))
        app.end_pressed()   # another button meanwhile, on B
        held.go.set()
        await lane_landed(app, pilot)
        assert got == [("relaunch", sid), ("end", other)], got


@pytest.mark.anyio
@pytest.mark.parametrize("via", ["threaded"], indirect=True)
async def test_a_read_landing_under_a_prompt_never_pops_it(store, sid, via):
    """#391 review: a prompt that opened over Reading before its read landed stays; the dialog
    takes Reading's place once the prompt closes."""
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.client.steps_table = held = Held(app.client.steps_table)
        app.action_session_steps()
        await held.wait_sent()
        answers = []
        app.push_screen(Confirm("over it?"), answers.append)
        await pilot.pause()
        held.go.set()
        await lane_landed(app, pilot)
        assert isinstance(app.screen, Confirm), "the prompt over it stays"
        app.screen.action_answer(True)
        await pilot.pause()
        await pilot.pause()
        assert answers == [True], "its answer heard"
        assert isinstance(app.screen, SessionSteps), "the dialog in Reading's place"
        assert not any(isinstance(sc, Reading) for sc in app.screen_stack), "Reading gone"


@pytest.mark.anyio
@pytest.mark.parametrize("via", ["threaded"], indirect=True)
async def test_two_drags_in_a_row_compose(store, sid, tmp_path, via):
    """#391 review: a second move made before the first is written reads the first: both stand."""
    refs = [post(store, sid, tmp_path, "task", n) for n in ("one", "two", "three")]
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        order = rows(app)
        keys = [f"{sid}|{r}" for r in refs]
        ranked = sorted(keys, key=order.index)   # top to bottom as shown
        app.client.reorder_items = held = Held(app.client.reorder_items)
        app.drop_row(app.items_table, ranked[2], ranked[0], None)   # the bottom one to the top
        await held.wait_sent()
        assert rows(app).index(ranked[2]) < rows(app).index(ranked[0]), "shown moved at once"
        app.drop_row(app.items_table, ranked[1], ranked[2], None)   # then the middle one above it
        held.go.set()
        await lane_landed(app, pilot)
        app.refresh_data()
        await pilot.pause()
        shown = [k for k in rows(app) if k in keys]
        assert shown == [ranked[1], ranked[2], ranked[0]], shown


@pytest.mark.anyio
@pytest.mark.parametrize("via", ["threaded"], indirect=True)
async def test_a_quick_second_save_never_conflicts_with_the_first(store, sid, tmp_path, via):
    """#391 review: Ctrl+S twice before the first save lands: the second goes from what the
    first wrote, so no conflict with itself, and the file has the second text."""
    post(store, sid, tmp_path, "doc", "plan")
    path = str(tmp_path / "plan.md")
    mtime = __import__("os").stat(path).st_mtime_ns
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.client.write_doc = held = Held(app.client.write_doc)
        app.save_doc(path, "# first\n", mtime)
        await held.wait_sent()
        app.save_doc(path, "# second\n", mtime)   # the mtime its editor still has
        held.go.set()
        await lane_landed(app, pilot)
        assert not isinstance(app.screen, (SaveConflict, SaveFailed)), type(app.screen).__name__
        assert open(path).read() == "# second\n"
        assert notices(app)[-2:] == ["Saved plan.md", "Saved plan.md"], notices(app)
