"""#391: the actions #386 left on the UI thread, now acknowledged at once and sent off it: the
effort slider, Recall, Reopen, drag reorder, doc save, session steps, the tour's writes, and the
reads a dialog needs before it opens. Each handler returns before the daemon is asked; what it
did lands after, or, failing, is undone and said."""
import asyncio
import threading
import types

import pytest
from textual.widgets import Button

from claude_wheelhouse.hub import Done
from claude_wheelhouse.store import TOUR_AT
from claude_wheelhouse.tui import (AdoptSession, DocEdit, FirstSteps, Reading, SaveFailed, SessionInfo,
                                   SessionSteps, WheelhouseApp)
from test_pins import notices, post

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
             lambda app, store, sid, ctx: rows(app) == ctx["order"],
             lambda app, store, sid, ctx: rows(app) != ctx["order"],
             lambda app, store, sid, ctx: rows(app) == ctx["order"]),
    "doc save": (setup_doc, "write_doc", None, lambda app, sid, ctx: app.save_doc(ctx["path"], "# saved\n", None, force=True),
                 lambda app, store, sid, ctx: open(ctx["path"]).read() == "# plan\n",
                 lambda app, store, sid, ctx: open(ctx["path"]).read() == "# saved\n" and "Saved plan.md" in notices(app),
                 lambda app, store, sid, ctx: open(ctx["path"]).read() == "# plan\n" and isinstance(app.screen, SaveFailed)
                 and "your edits are kept" in str(app.screen.prompt)),
    "steps": (nothing, "steps_table", None, lambda app, sid, ctx: app.action_session_steps(),
              lambda app, store, sid, ctx: isinstance(app.screen, Reading),
              lambda app, store, sid, ctx: isinstance(app.screen, SessionSteps),
              lambda app, store, sid, ctx: not isinstance(app.screen, (Reading, SessionSteps))),
    "first press": (nothing, "steps_table", None, lambda app, sid, ctx: ctx.update(waits=app.first_press(Button("x"))),
                    lambda app, store, sid, ctx: ctx["waits"] and app.steps_reading,
                    lambda app, store, sid, ctx: isinstance(app.screen, FirstSteps),
                    lambda app, store, sid, ctx: not isinstance(app.screen, FirstSteps) and app.steps_pass is not None),
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


@pytest.mark.parametrize("typed, kept, desc", [
    ("# saved\n", None, "nothing typed since the save: no edits kept"),
    ("# saved\nmore\n", "# saved\nmore\n", "typed since: kept over the new base, never lost"),
])
def test_a_save_landing_keeps_what_was_typed_since(tmp_path, typed, kept, desc):
    """#391: a doc save lands after the editor has moved on; edits kept with no editor open
    (doc_edits) stay, their base the text saved."""
    path = str(tmp_path / "plan.md")
    app = types.SimpleNamespace(doc_edits={path: DocEdit("# plan\n", typed, typed, (0, 0))}, screen_stack=[],
                                each_pane=lambda *a: None)   # no editor open on it
    WheelhouseApp.doc_written(app, path, "# saved\n", 7, keep=True)
    edit = app.doc_edits.get(path)
    assert (edit and edit.text) == kept, desc
    if edit:
        assert (edit.base, edit.mtime) == ("# saved\n", 7), desc
