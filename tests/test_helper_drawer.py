"""The helper's drawer (tui.HelperDrawer, Alt+Q): opened and closed, the chat kept between,
an answer streamed in, errors, New chat and the idle stop, with a fake helper.Helper (no
test runs the real claude: test_helper.py covers the driver)."""

import pytest
from textual.widgets import DataTable, Input, Static

from claude_wheelhouse import facts, helper, tui
from claude_wheelhouse.helper import Event
from claude_wheelhouse.tui import HelperDrawer, WheelhouseApp


class FakeHelper:
    """helper.Helper's surface, the answer given by the test: say(event) as the reader would."""
    made: list["FakeHelper"] = []

    def __init__(self, on_event, sdk=False):
        self.on_event, self.sdk = on_event, sdk
        self.asked, self.busy, self.full = [], False, False
        self.resets = self.idles = self.stops = 0
        FakeHelper.made.append(self)

    def ask(self, question):
        if self.busy or not question.strip():
            return False
        self.asked.append(question.strip())
        self.busy = True
        return True

    def say(self, event):
        if event.kind in ("done", "error"):
            self.busy = False
        self.on_event(event)

    def reset(self):
        self.resets += 1
        self.busy = False

    def idle(self):
        self.idles += 1

    def stop(self):
        self.stops += 1


@pytest.fixture(autouse=True)
def fake_helper(monkeypatch):
    FakeHelper.made = []
    monkeypatch.setattr(helper, "Helper", FakeHelper)
    return FakeHelper.made


def chat_text(app) -> str:
    return "\n".join(str(s.render()) for s in app.screen.query("#helper-chat Static"))


async def ask(app, pilot, question):
    await pilot.press(*question)
    await pilot.press("enter")
    await pilot.pause()


@pytest.mark.anyio
async def test_alt_q_opens_the_drawer_and_esc_or_alt_q_closes_it_leaving_focus_where_it_was(store, fake_helper):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        items = app.query_one("#items", DataTable)
        items.focus()
        for close in ("escape", "alt+q"):
            await pilot.press("alt+q")
            await pilot.pause()
            assert isinstance(app.screen, HelperDrawer), close
            assert isinstance(app.focused, Input) and app.focused.id == "helper-question", f"{close}: ready to type"
            assert "leave this machine" in chat_text(app), "what leaves the machine, said before the first question"
            await pilot.press(close)
            await pilot.pause()
            assert not isinstance(app.screen, HelperDrawer) and app.focused is items, f"{close}: focus back"
        assert not app._exit, "Alt+Q is the helper's, not Ctrl+Q's quit"
        assert fake_helper == [] and app.helper is None, "nothing made, let alone started, until a question"


@pytest.mark.anyio
async def test_an_answer_streams_in_and_the_chat_is_kept_between_openings(store, fake_helper):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        await pilot.press("alt+q")
        await pilot.pause()
        await ask(app, pilot, "how do I pin")
        fake = fake_helper[0]
        assert fake.asked == ["how do I pin"] and app.screen.query_one("#helper-question", Input).value == ""
        assert app.screen.query_one("#helper-status", Static).render().plain == "Thinking…"
        fake.say(Event("tool", "Grep alt+p"))
        await pilot.pause()
        assert "Reading Grep alt+p" in str(app.screen.query_one("#helper-status", Static).render())
        fake.say(Event("text", "Press **Alt+P**"))
        await pilot.pause()
        assert app.helper_log[-1] == {"who": "helper", "text": "Press **Alt+P**"}, "the piece taken as it comes"
        assert len(app.screen.query("#helper-chat Static")) == 2, "the question, and the answer coming in" 
        fake.say(Event("done", "Press **Alt+P** in the item list.", 5000))
        await pilot.pause()
        assert app.helper_log[-1] == {"who": "helper", "text": "Press **Alt+P** in the item list."}
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("alt+q")
        await pilot.pause()
        assert app.helper_log[0] == {"who": "you", "text": "how do I pin"}
        assert "how do I pin" in chat_text(app) and len(app.screen.query("#helper-chat Static")) == 2, "the chat as it was"


@pytest.mark.parametrize("events, log, desc", [
    ([Event("error", "Claude Code isn't logged in.")],
     [{"who": "you", "text": "q"}, {"who": "error", "text": "Claude Code isn't logged in."}], "no answer: the error instead"),
    ([Event("text", "Half"), Event("error", "The helper stopped: gone")],
     [{"who": "you", "text": "q"}, {"who": "helper", "text": "Half"}, {"who": "error", "text": "The helper stopped: gone"}],
     "part of an answer kept, the error under it"),
])
@pytest.mark.anyio
async def test_errors(store, fake_helper, events, log, desc):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        await pilot.press("alt+q")
        await pilot.pause()
        await ask(app, pilot, "q")
        for e in events:
            fake_helper[0].say(e)
        await pilot.pause()
        assert app.helper_log == log, desc


@pytest.mark.anyio
async def test_new_chat_and_a_long_chat_offering_it(store, fake_helper):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        await pilot.press("alt+q")
        await pilot.pause()
        await ask(app, pilot, "q")
        fake = fake_helper[0]
        fake.full = True
        fake.say(Event("done", "a", helper.FULL))
        await pilot.pause()
        assert app.helper_log[-1] == {"who": "note", "text": tui.HELPER_FULL}, "past FULL: New chat offered"
        await pilot.press("alt+n")
        await pilot.pause()
        assert isinstance(app.screen, HelperDrawer), "Alt+N in the drawer is New chat, not New session"
        assert fake.resets == 1 and app.helper_log == [] and "leave this machine" in chat_text(app)


@pytest.mark.parametrize("event, says, desc", [
    (Event("done", "a"), "The helper has answered: Alt+Q", "an answer"),
    (Event("error", "gone"), "The helper couldn't answer: Alt+Q", "an error"),
])
@pytest.mark.anyio
async def test_an_answer_landing_with_the_drawer_closed_says_so(store, fake_helper, monkeypatch, event, says, desc):
    app = WheelhouseApp(store)
    said = []
    async with app.run_test(size=(160, 40)) as pilot:
        monkeypatch.setattr(app, "notify", lambda text, **kw: said.append(text))
        await pilot.pause()
        await pilot.press("alt+q")
        await pilot.pause()
        await ask(app, pilot, "q")
        await pilot.press("escape")
        await pilot.pause()
        fake_helper[0].say(event)
        await pilot.pause()
        assert said == [says], desc


@pytest.mark.anyio
async def test_an_event_before_the_drawer_is_composed_waits_for_it(store, fake_helper):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.helper_log = [{"who": "you", "text": "q"}, {"who": "helper", "text": ""}]
        app.push_screen(HelperDrawer())
        app.helper_said(Event("text", "early"))   # before it composes: no NoMatches
        await pilot.pause()
        assert app.helper_log[-1]["text"] == "early" and len(app.screen.query("#helper-chat Static")) == 2


@pytest.mark.anyio
async def test_a_question_while_one_is_answered_stays_in_the_box(store, fake_helper):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        await pilot.press("alt+q")
        await pilot.pause()
        await ask(app, pilot, "q")
        await ask(app, pilot, "again")
        assert fake_helper[0].asked == ["q"] and app.screen.query_one("#helper-question", Input).value == "again"


@pytest.mark.anyio
async def test_the_tick_stops_an_idle_helper_and_quitting_stops_it(store, fake_helper):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.tick()
        await pilot.press("alt+q")
        await pilot.pause()
        await ask(app, pilot, "q")
        fake = fake_helper[0]
        app.tick()
        assert fake.idles >= 1, "each tick asks it whether it has been quiet long enough"
    assert fake.stops == 1, "quitting stops its claude"


@pytest.mark.anyio
async def test_alt_q_over_another_dialog_does_nothing(store):
    app = WheelhouseApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        await pilot.press("f1")
        await pilot.pause()
        await pilot.press("alt+q")
        await pilot.pause()
        assert isinstance(app.screen, tui.KeysHelp)


def test_the_overlay_and_the_palette_list_it():
    text = tui.keys_help()
    assert "**Alt+Q**: Ask the wheelhouse" in text and "## The helper (Alt+Q)" in text
    assert "Alt+G" not in text, "#219: Alt+G opened Gemini, so nothing of the wheelhouse's is on it"
    assert [t for t, _ in facts.palette(tui) if t.startswith("Ask")] == ["Ask the wheelhouse"]
