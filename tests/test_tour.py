import pytest

from claude_wheelhouse import whatsnew
from claude_wheelhouse.tour import NOTES, PAUSED, TOPUP, TOUR, Tour, offered, opening
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
    ("decision", "demo", facts(closed=True), set(), (), "docs", "the decision closed"),
    ("docs", "demo", facts(closed=True), set(), (), "docs", "waits for the story's doc to be opened"),
    ("docs", "demo", facts(closed=True), {"read"}, (), "conversation", "the story opened (Doug, #101 8)"),
    ("conversation", "demo", facts(), {"follow"}, (), "stats", "a turn watched arriving"),
    ("stats", "demo", facts(), set(), ("stats",), "decisions", "Stats absent: passed over"),
    ("decisions", "demo", facts(), set(), ("design",), "end", "the design module absent: its Decisions dialog passed over"),
    ("end", "demo", facts(), set(), (), "end", "waits for the demo to go"),
    ("end", "demo", None, set(), (), "notes", "the demo gone"),
    ("answer", "demo", None, set(), (), "answer", "gone early: the dialog stays, saying so"),
    ("answer", "demo", facts(sid="other"), set(), (), "answer", "another tutorial session isn't the demo"),
    ("notes", "demo", None, set(), (), "notes", "the notes wait for Close"),
])
def test_the_tour_moves_on(at, sid, given, seen, absent, expected, desc):
    tour = Tour(sdk=False)
    tour.at = [d.id for d in tour.stops].index(at)
    tour.sid, tour.seen = sid, set(seen)
    tour.follow(given, lambda pane: pane not in absent)
    assert tour.stop.id == expected, desc
    assert tour.absent == {d.id for d in tour.stops if d.pane in absent and d.id == at}, f"{desc}: what was passed over"
    gone = sid is not None and (given is None or given["sid"] != sid) and expected != "notes"
    assert (tour.dead == "the demo has ended") is gone, f"{desc}: says the demo has ended"


@pytest.mark.parametrize("at, sid, given, stopped, dead, then, desc", [
    ("reply", "demo", facts(sent=True), "stopped: its tab has closed", "stopped: its tab has closed", "notes",
     "a stopped demo: said, and Next goes to the notes"),
    ("decision", "demo", None, None, "the demo has ended", "notes", "an ended one, the same"),
    ("reply", "demo", facts(sent=True), None, None, "decision", "running: Next is the next dialog"),
    ("create", None, None, "stopped: never", None, "sessions", "no demo yet: nothing to be dead"),
    ("stats", "demo", None, None, None, "decisions", "a Next dialog isn't waiting on the demo"),
])
def test_a_dead_demo_offers_the_notes(at, sid, given, stopped, dead, then, desc):
    tour = Tour(sdk=False)
    tour.at = [d.id for d in tour.stops].index(at)
    tour.sid = sid
    tour.follow(given, lambda pane: True, stopped)
    assert (tour.stop.id, tour.dead) == (at, dead), desc
    tour.next()
    assert (tour.stop.id, tour.dead) == (then, None), desc


FEATURES = (
    Feature("a", UNRELEASED, "inbox", note="A.", tip="Tip A."),
    Feature("b", UNRELEASED, "inbox", note="B.", tip="Tip B.", when="sdk"),
    Feature("c", UNRELEASED, "stats", note="C.", tip="Tip C."),
    Feature("d", UNRELEASED, ADVANCED, note="D."),
)


@pytest.mark.parametrize("stop, sdk, absent, text, shown, desc", [
    ("inbox", False, set(), "Tip A.", ["a"], "its tips under its text"),
    ("inbox", True, set(), "Tip A.\n\nTip B.", ["a", "b"], "an sdk tip with SDK mode on"),
    ("answer", False, set(), "", [], "no tips: its text alone"),
    ("notes", False, set(), "", ["a", "c", "d"], "the notes: every feature seen, but an sdk one with it off"),
    ("notes", True, set(), "", ["a", "b", "c", "d"], "with SDK mode on, that one too"),
    ("notes", False, {"stats"}, "", ["a", "d"], "nor one of a dialog passed over for want of its pane"),
])
def test_what_a_dialog_shows(stop, sdk, absent, text, shown, desc):
    tour = Tour(sdk=sdk, features=FEATURES)
    tour.absent = absent
    found = next(d for d in tour.stops if d.id == stop)
    assert tour.text(found) == "\n\n".join(filter(None, [found.text, text])), f"{desc}: a blank line between paragraphs"
    assert tour.shown(found) == shown, desc


def test_the_tour_is_every_dialog_but_the_invite():
    assert [d.id for d in Tour(sdk=False).stops] == [d.id for d in whatsnew.DIALOGS][1:]


def pending_of(*ids):
    return [f for f in FEATURES if f.id in ids]


@pytest.mark.parametrize("offer, at, pending, absent, kind, brings, desc", [
    (False, None, (), set(), None, [], "nothing new: nothing offered"),
    (True, None, ("a", "c"), set(), TOUR, [], "the whole tour offered wins over a top-up"),
    (True, "tour:answer", (), set(), PAUSED, [], "a tour paused wins over everything: resumed, never lost"),
    (False, "topup:stats", ("a",), set(), PAUSED, [], "a top-up paused, likewise"),
    (False, None, ("a", "d"), set(), TOPUP, ["a", "d"], "a dialog with a new tip: the top-up invite (Q120 C)"),
    (False, None, ("d",), set(), NOTES, ["d"], "advanced only: the badge alone, pointing at the notes"),
    (False, None, ("c", "d"), {"stats"}, NOTES, ["d"], "a tip whose pane is absent waits for it"),
    (False, None, ("c",), {"stats"}, None, [], "only that: nothing offered yet"),
])
def test_what_a_start_offers(offer, at, pending, absent, kind, brings, desc):
    found_kind, found = opening(offer, at, pending_of(*pending), lambda pane: pane not in absent)
    assert (found_kind, [f.id for f in found]) == (kind, brings), desc


@pytest.mark.parametrize("pending, sdk, stops, texts, desc", [
    (("a",), False, ["inbox", "notes"], ["Tip A."], "one dialog, its new tip alone, no base text"),
    (("c", "a"), False, ["inbox", "stats", "notes"], ["Tip A.", "Tip C."], "in tour order, whatever the registry's"),
    (("a", "b"), True, ["inbox", "notes"], ["Tip B.\n\nTip A."], "a dialog's every pending tip, as given"),
    (("d",), False, ["notes"], [], "advanced only: straight to the notes"),
    ((), False, ["notes"], [], "nothing pending: the notes alone"),
])
def test_a_top_up(pending, sdk, stops, texts, desc):
    tour = Tour(sdk=sdk, pending=list(reversed(pending_of(*pending))))   # dialogs in tour order, whatever the order given
    assert tour.kind == TOPUP and [d.id for d in tour.stops] == stops, desc
    assert [tour.text(d) for d in tour.stops[:-1]] == texts, desc
    assert all(d.advance == whatsnew.NEXT for d in tour.stops[:-1]), f"{desc}: every one moves on at Next"
    tour.follow(None, lambda pane: True)
    assert tour.stop.id == stops[0] and tour.dead is None, f"{desc}: no demo to wait on, or to be dead"
    assert sorted(tour.shown(tour.stops[-1])) == sorted(pending), f"{desc}: the notes record what it brought"


@pytest.mark.parametrize("pending, at, given, stop, dead, desc", [
    (None, "stats", "facts", "stats", None, "the whole tour at the dialog it was at"),
    (None, "principles", None, "principles", None, "before the demo: nothing to wait on"),
    (None, "answer", None, "answer", "the demo has ended", "past its creation with the demo gone: said"),
    (None, "answer", "facts", "answer", None, "past its creation with the demo there: carried on"),
    (("a", "c"), "stats", None, "stats", None, "a top-up at its dialog"),
    (("c",), "inbox", None, "stats", None, "a top-up dialog since seen: the next one that isn't"),
    (("a",), "stats", None, "notes", None, "past every dialog left: the notes"),
])
def test_resume(pending, at, given, stop, dead, desc):
    tour = Tour(sdk=False, features=FEATURES, pending=None if pending is None else pending_of(*pending))
    tour.resume(at)
    tour.follow(facts(posted=True) if given else None, lambda pane: True)
    assert (tour.stop.id, tour.dead) == (stop, dead), desc
    assert tour.where() == f"{tour.kind}:{stop}", f"{desc}: kept as tour_at"


def test_a_module_pane_tip_waits_for_its_module():
    """Core never advertises a module's pane: the Decisions pane's tip is the design module's
    dialog's, offered only with that pane here."""
    feature = next(f for f in whatsnew.FEATURES if f.id == "decisions-pane")
    assert offered([feature], lambda pane: pane != "design") == []
    assert offered([feature], lambda pane: True) == [feature]
