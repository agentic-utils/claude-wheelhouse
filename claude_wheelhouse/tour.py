"""The tour's progress (.plan/tutorial-overhaul.md, "The flow"): which of its dialogs shows,
and what moves it on. The app paints it (a centred dialog, or a callout beside a pane,
callout.py); this holds no Textual.

An action dialog moves on once what it asks has happened: what the store records of the
demo session (tutorial.facts), or what only the screen has seen the person do (seen: a
question or the story's doc opened, a turn of Claude's watched arriving), never the app's
own automatic selection. A Next dialog moves on only on Next; a wait dialog too, its Next
off until what it waits on has happened (ready). A dialog whose pane is absent (Stats turned
off) is passed over. A demo that has gone, or stopped, leaves nothing to wait on: the action
dialog showing says so (dead) and its Next goes straight to the release notes. Ending it is
what the last action dialog asks, so there it simply moves on.

An upgrader's top-up (TOPUP) is the same walk cut down: only the dialogs with a pending tip
(whatsnew.pending), each showing only those tips and moving on at Next, then the release
notes. It never needs the demo, so it spends no tokens. What a start offers, the invite or
only the title row's badge, is `opening`'s to say.
"""

from dataclasses import replace

from . import whatsnew
from .whatsnew import ACTION, ADVANCED, NEXT, TIPPED, WAIT

TOUR, TOPUP = "tour", "topup"   # a new user's whole tour; an upgrader's top-up
# what a start, or the badge, offers (opening): a tour paused (the app quit mid-tour),
# the whole tour's invite, an upgrader's top-up, or only the release notes (advanced features)
PAUSED, NOTES = "paused", "notes"
PANE = {d.id: d.pane for d in whatsnew.DIALOGS}   # each dialog's pane: None, centred


def offered(pending, present) -> list[whatsnew.Feature]:
    """The pending features a top-up can show now: an advanced one, in the notes, and a
    dialog's whose pane is here (present(pane)); one whose pane is absent waits for it."""
    return [f for f in pending if f.where == ADVANCED or PANE.get(f.where) is None or present(PANE[f.where])]


def opening(offer: bool, at: str | None, pending, present) -> tuple[str | None, list[whatsnew.Feature]]:
    """What there is to offer, and the features it brings (Q120 C): a tour stored as under way
    (`at`, tour_at) is paused, and resumes where it was; the whole tour while it's offered
    (tutorial.should_offer); a top-up while a tour dialog has a new tip to show; only the
    release notes while every new feature is advanced; else nothing."""
    if at:
        return PAUSED, []
    if offer:
        return TOUR, []
    found = offered(pending, present)
    if any(f.where != ADVANCED for f in found):
        return TOPUP, found
    return (NOTES, found) if found else (None, [])

# what each action dialog waits on: the store's facts for the demo (tutorial.facts, None while
# there's none) and what only the screen has seen (seen)
CHECKS = {
    "create": lambda facts, seen: facts is not None,
    "sessions": lambda facts, seen: bool(facts and facts["posted"]),
    "inbox": lambda facts, seen: "open" in seen or bool(facts and facts["answered"]),
    "answer": lambda facts, seen: bool(facts and facts["queued"]),
    "send": lambda facts, seen: bool(facts and facts["sent"]),
    "reply": lambda facts, seen: bool(facts and facts["replied"]),
    "decision": lambda facts, seen: "decision" in seen or bool(facts and facts["closed"]),   # D1 opened
    "close": lambda facts, seen: bool(facts and facts["closed"]),
    "docs": lambda facts, seen: "read" in seen,   # the story's doc opened
    "conversation": lambda facts, seen: bool({"talk", "follow"} & seen),   # its conversation picked
    "ask": lambda facts, seen: "follow" in seen,
    "end": lambda facts, seen: False,   # only the demo going ends it
}


# the demo's inbox rows each step needs in sight, by their refs' kind ("" its conversation
# row): what its callout keeps clear of (callout.place). A step not listed needs none: its
# callout keeps clear of its own pane alone
ROWS = {
    "subagent": ("A",),   # the demo's subagent: after its reply, so its first turn (and A1) is done
    "inbox": ("Q",),
    "answer": ("Q",),
    "reply": ("Q",),
    "decision": ("D",),
    "close": ("D",),
    "docs": ("M",),
    "conversation": ("",),
    "ask": ("",),
}
# a pane a step's callout keeps clear of, besides its own: what the step has the person read
# there (Doug, #174: closing D1 never covers D1 shown in Detail)
SHUN = {"close": "detail"}
# the panes each step's text refers to besides its own: left undimmed with it (Doug, #174)
LIT = {
    "inbox": ("items",), "answer": ("items",), "reply": ("items",),
    "decision": ("items", "detail"), "close": ("items", "detail"), "docs": ("items", "detail"),
    "conversation": ("items", "sessions"), "ask": ("items",), "subagent": ("detail",),
    "decisions": ("detail",), "prs": ("items", "detail"), "end": ("sessions",),
}


class Tour:
    """A new user's tour, from the principles to the release notes: every dialog whose pane is here. Given
    `pending` (whatsnew.pending), an upgrader's top-up instead: the dialogs with a pending
    tip, each a Next one showing only those, then the notes, which show every release since
    `notes_seen` (whatsnew.since)."""

    def __init__(self, sdk: bool, dialogs=whatsnew.DIALOGS, features=whatsnew.FEATURES, pending=None,
                 notes_seen: str | None = None) -> None:
        self.kind = TOUR if pending is None else TOPUP
        self.stops = [d for d in dialogs if d.id != "invite"]
        if pending is not None:
            features = list(pending)
            self.stops = [replace(d, text="", advance=NEXT, ask="", button="") for d in self.stops
                          if d.id in TIPPED and whatsnew.tips(d.id, sdk, features)] + [self.stops[-1]]
        self.sdk, self.features, self.notes_seen = sdk, features, notes_seen
        self.at = 0
        self.sid: str | None = None   # the demo session, once made
        self.seen: set[str] = set()   # the steps only the screen sees ("open", "read", "follow")
        self.absent: set[str] = set()   # the dialogs passed over for want of their pane
        self.dead: str | None = None   # why the demo can't go on (gone or stopped), at an action dialog
        self.ready = False   # a wait dialog's: what it waits on has happened, so its Next is on

    @property
    def stop(self) -> whatsnew.Stop | None:
        """The dialog showing, or None once the tour is over."""
        return self.stops[self.at] if self.at < len(self.stops) else None

    def text(self, stop: whatsnew.Stop) -> str:
        """A dialog's text, below it every tip registered to it, then what it asks (ask); in a
        top-up, only its pending tips. Each its own paragraph, a blank line between."""
        tips = [f.tip for f in whatsnew.tips(stop.id, self.sdk, self.features)]
        return "\n\n".join(tips if self.kind == TOPUP and stop.id != "notes"
                           else [p for p in (stop.text, *tips, stop.ask) if p])

    def where(self) -> str | None:
        """Where it is, as tour_at keeps it ("tour:answer"), for resume; None once it's over."""
        return f"{self.kind}:{self.stop.id}" if self.stop is not None else None

    def resume(self, at: str) -> None:
        """At the dialog `at` names, or, where that's no longer one of this tour's (a top-up's
        tips since seen), the next that is. A whole tour resumed past the demo's creation
        expects the demo: with none there, its dialog says the demo has ended."""
        order = [d.id for d in whatsnew.DIALOGS]
        rank = order.index(at) if at in order else 0
        self.at = next((i for i, d in enumerate(self.stops) if order.index(d.id) >= rank), len(self.stops) - 1)
        if self.kind == TOUR and rank > order.index("create"):
            self.sid = ""   # not None, so no demo is gone (follow); falsy, so the demo's facts set it

    def follow(self, facts: dict | None, present, stopped: str | None = None) -> bool:
        """Moves past every dialog done with: an action dialog whose check holds (End's, once
        the demo has gone), and one whose pane is absent (present(pane) false). An action
        dialog left waiting on a demo that has gone, or stopped (`stopped`, why), records why
        (dead). Whether it moved."""
        if facts is not None and not self.sid:
            self.sid = facts["sid"]
        gone = self.sid is not None and (facts is None or facts["sid"] != self.sid)
        moved = False
        while (stop := self.stop) is not None:
            if stop.pane is not None and not present(stop.pane):
                self.absent.add(stop.id)
            elif not (stop.advance == ACTION and (stop.id == "end" and gone or CHECKS[stop.id](facts, self.seen))):
                break
            self.at += 1
            moved = True
        self.ready = stop is not None and stop.advance == WAIT and CHECKS[stop.id](facts, self.seen)
        waiting = stop is not None and stop.advance in (ACTION, WAIT) and self.sid is not None
        self.dead = ("the demo has ended" if gone else stopped) if waiting else None
        return moved

    def next(self) -> None:
        """Next: the dialog after this one, or with the demo dead, the release notes."""
        self.at = next(i for i, d in enumerate(self.stops) if d.id == "notes") if self.dead else self.at + 1
        self.dead = None

    def shown(self, stop: whatsnew.Stop) -> list[str]:
        """The features a dialog showing counts as seen: its tips; the notes, every feature
        but those of a dialog passed over for want of its pane, and an `sdk` one with SDK mode
        off (whatsnew.pending offers it once SDK mode is on)."""
        if stop.id == "notes":
            return [f.id for f in whatsnew.pending((), self.sdk, self.features)
                    if f.where == ADVANCED or f.where not in self.absent]
        return [f.id for f in whatsnew.tips(stop.id, self.sdk, self.features)]
