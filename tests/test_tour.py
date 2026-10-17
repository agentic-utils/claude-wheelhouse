import pytest

from claude_wheelhouse import whatsnew
from claude_wheelhouse.tour import Tour
from claude_wheelhouse.whatsnew import ADVANCED, UNRELEASED, Feature

NONE = {"sid": "demo", "posted": False, "answered": False, "queued": False, "sent": False, "replied": False,
        "closed": False}


def facts(**kw) -> dict:
    return {**NONE, **kw}


@pytest.mark.parametrize("at, sid, given, seen, absent, expected, desc", [
    ("principles", None, None, set(), (), "principles", "a Next dialog waits for Next"),
    ("create", None, None, set(), (), "create", "no demo yet"),
    ("create", None, facts(), set(), (), "sessions", "the demo's row appeared"),
    ("create", None, facts(posted=True), set(), (), "inbox", "it has posted already: past its row's dialog too"),
    ("inbox", "demo", facts(posted=True), set(), (), "inbox", "waits for a question opened"),
    ("inbox", "demo", facts(posted=True), {"open"}, (), "answer", "a question opened, as the screen saw"),
    ("inbox", "demo", facts(answered=True), set(), (), "answer", "an answer means a question was opened"),
    ("answer", "demo", facts(answered=True), set(), (), "answer", "one answer isn't both"),
    ("answer", "demo", facts(answered=True, queued=True), set(), (), "send", "both answered"),
    ("send", "demo", facts(queued=True, sent=True), set(), (), "reply", "sent"),
    ("reply", "demo", facts(sent=True, replied=True), set(), (), "decision", "it replied"),
    ("decision", "demo", facts(replied=True), set(), (), "decision", "waits for the decision closed"),
    ("decision", "demo", facts(closed=True), set(), (), "conversation", "the decision closed"),
    ("conversation", "demo", facts(), {"follow"}, (), "stats", "a turn watched arriving"),
    ("stats", "demo", facts(), set(), ("stats",), "end", "Stats absent: passed over"),
    ("end", "demo", facts(), set(), (), "end", "waits for the demo to go"),
    ("end", "demo", None, set(), (), "notes", "the demo gone"),
    ("answer", "demo", None, set(), (), "stats", "gone early: every action dialog passes, a Next one still shows"),
    ("answer", "demo", facts(sid="other"), set(), (), "stats", "another tutorial session isn't the demo"),
    ("notes", "demo", None, set(), (), "notes", "the notes wait for Close"),
])
def test_the_tour_moves_on(at, sid, given, seen, absent, expected, desc):
    tour = Tour(sdk=False)
    tour.at = [d.id for d in tour.stops].index(at)
    tour.sid, tour.seen = sid, set(seen)
    tour.follow(given, lambda pane: pane not in absent)
    assert tour.stop.id == expected, desc
    assert tour.absent == {d.id for d in tour.stops if d.pane in absent and d.id == at}, f"{desc}: what was passed over"


FEATURES = (
    Feature("a", UNRELEASED, "inbox", note="A.", tip="Tip A."),
    Feature("b", UNRELEASED, "inbox", note="B.", tip="Tip B.", when="sdk"),
    Feature("c", UNRELEASED, "stats", note="C.", tip="Tip C."),
    Feature("d", UNRELEASED, ADVANCED, note="D."),
)


@pytest.mark.parametrize("stop, sdk, absent, text, shown, desc", [
    ("inbox", False, set(), "Tip A.", ["a"], "its tips under its text"),
    ("inbox", True, set(), "Tip A.\nTip B.", ["a", "b"], "an sdk tip with SDK mode on"),
    ("answer", False, set(), "", [], "no tips: its text alone"),
    ("notes", False, set(), "", ["a", "c", "d"], "the notes: every feature seen, but an sdk one with it off"),
    ("notes", True, set(), "", ["a", "b", "c", "d"], "with SDK mode on, that one too"),
    ("notes", False, {"stats"}, "", ["a", "d"], "nor one of a dialog passed over for want of its pane"),
])
def test_what_a_dialog_shows(stop, sdk, absent, text, shown, desc):
    tour = Tour(sdk=sdk, features=FEATURES)
    tour.absent = absent
    found = next(d for d in tour.stops if d.id == stop)
    assert tour.text(found) == "\n".join(filter(None, [found.text, text])), desc
    assert tour.shown(found) == shown, desc


def test_the_tour_is_every_dialog_but_the_invite():
    assert [d.id for d in Tour(sdk=False).stops] == [d.id for d in whatsnew.DIALOGS][1:]
