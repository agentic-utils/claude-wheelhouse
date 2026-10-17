"""The tour's progress (.plan/tutorial-overhaul.md, "The flow"): which of its dialogs shows,
and what moves it on. The app paints it (a centred dialog, or a callout beside a pane,
callout.py); this holds no Textual.

An action dialog moves on once what it asks has happened: what the store records of the
demo session (tutorial.facts), or what only the screen has seen the person do (seen: a
question opened, a turn of Claude's watched arriving), never the app's own automatic
selection. A Next dialog moves on only on Next. A dialog whose pane is absent (Stats turned
off) is passed over. A demo that has gone, or stopped, leaves nothing to wait on: the action
dialog showing says so (dead) and its Next goes straight to the release notes. Ending it is
what the last action dialog asks, so there it simply moves on.
"""

from . import whatsnew
from .whatsnew import ACTION, ADVANCED

# what each action dialog waits on: the store's facts for the demo (tutorial.facts, None while
# there's none) and what only the screen has seen (seen)
CHECKS = {
    "create": lambda facts, seen: facts is not None,
    "sessions": lambda facts, seen: bool(facts and facts["posted"]),
    "inbox": lambda facts, seen: "open" in seen or bool(facts and facts["answered"]),
    "answer": lambda facts, seen: bool(facts and facts["queued"]),
    "send": lambda facts, seen: bool(facts and facts["sent"]),
    "reply": lambda facts, seen: bool(facts and facts["replied"]),
    "decision": lambda facts, seen: bool(facts and facts["closed"]),
    "conversation": lambda facts, seen: "follow" in seen,
    "end": lambda facts, seen: False,   # only the demo going ends it
}


class Tour:
    """A new user's tour, from the principles to the release notes: dialogs 1 to 12."""

    def __init__(self, sdk: bool, dialogs=whatsnew.DIALOGS, features=whatsnew.FEATURES) -> None:
        self.stops = [d for d in dialogs if d.id != "invite"]
        self.sdk, self.features = sdk, features
        self.at = 0
        self.sid: str | None = None   # the demo session, once made
        self.seen: set[str] = set()   # the steps only the screen sees ("open", "follow")
        self.absent: set[str] = set()   # the dialogs passed over for want of their pane
        self.dead: str | None = None   # why the demo can't go on (gone or stopped), at an action dialog
        self.paused = False

    @property
    def stop(self) -> whatsnew.Stop | None:
        """The dialog showing, or None once the tour is over."""
        return self.stops[self.at] if self.at < len(self.stops) else None

    def text(self, stop: whatsnew.Stop) -> str:
        """A dialog's text and, below it, every tip registered to it."""
        return "\n".join([stop.text, *(f.tip for f in whatsnew.tips(stop.id, self.sdk, self.features))])

    def follow(self, facts: dict | None, present, stopped: str | None = None) -> bool:
        """Moves past every dialog done with: an action dialog whose check holds (End's, once
        the demo has gone), and one whose pane is absent (present(pane) false). An action
        dialog left waiting on a demo that has gone, or stopped (`stopped`, why), records why
        (dead). Whether it moved."""
        if facts is not None and self.sid is None:
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
        waiting = stop is not None and stop.advance == ACTION and self.sid is not None
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
