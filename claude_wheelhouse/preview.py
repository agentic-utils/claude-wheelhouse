"""`claude-wheelhouse scene <name>` (`make scene NAME=<name>`, #340): one compaction scene played
in the terminal at the app's speed, for watching it rather than reading its frames, with no
daemon or session. A made-up cycle's stages go through the hub's own compaction_entries (the
scene forced as compactor.FORCE does), and its dock entry into the app's own CompactorOverlay,
over a stand-in conversation that shows each stage's plain line: what the app shows, drawn the
same way. A cycle runs requested, preparing, COMPACTING seconds of compacting, then its end
(completed unless --end says otherwise) for compactor.LINGER seconds, then starts again until
Ctrl+C, q or Esc.
"""

from __future__ import annotations

import itertools
import os
import time
from datetime import datetime, timezone

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Static

from . import compactor, hub
from .tui import CompactorOverlay

PREPARING = 0.5   # seconds from the request to the notes turn, and that turn's own (compaction_hook's)
NOTES = 4.0
COMPACTING = 20.0   # the default compacting stage: long enough for a scene's middle to play out
PAUSE = 1.0   # seconds between one cycle's overlay going and the next's start
STEP = 0.25   # how often the cycle's stages are read again; the overlay draws at its own rate


def iso(at: float) -> str:
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


def stages(cycle: str, start: float, now: float, compacting: float, end: str) -> list[dict]:
    """The cycle's stage rows begun by now, as the store gives them to the hub."""
    times = (("requested", 0.0), ("preparing", PREPARING), ("compacting", PREPARING + NOTES),
             (end, PREPARING + NOTES + compacting))
    return [{"cycle_id": cycle, "state": state, "note": "", "at": iso(start + dt)}
            for state, dt in times if start + dt <= now]


class ScenePreview(App):
    """The preview: a stand-in conversation and the app's compaction overlay over it."""
    CSS = "Screen { background: #0a0a1a; } #lines { padding: 1 2; color: #1f5daa; }"
    BINDINGS = [Binding("ctrl+c", "quit", "Quit", priority=True), Binding("q", "quit", "Quit"),
                Binding("escape", "quit", "Quit")]

    def __init__(self, name: str, compacting: float = COMPACTING, end: str = "completed") -> None:
        super().__init__()
        os.environ[compactor.FORCE] = name   # this process's hub picks it, as the app's would with it set
        self.scene, self.compacting, self.end = name, compacting, end
        self.length = PREPARING + NOTES + compacting + compactor.LINGER + PAUSE
        self.cycles = itertools.count(1)
        self.cycle, self.start = "", 0.0

    def compose(self) -> ComposeResult:
        yield VerticalScroll(Static(id="lines"), id="conversation")
        yield CompactorOverlay(id="compactor-overlay")

    def on_mount(self) -> None:
        self.title = f"{self.scene}: Ctrl+C, q or Esc to quit"
        self.overlay = self.query_one(CompactorOverlay)
        self.overlay.under = self.query_one("#conversation", VerticalScroll)
        self.lines = self.query_one("#lines", Static)
        self.tick()
        self.set_interval(STEP, self.tick)

    def tick(self) -> None:
        """The cycle's entries for now, from the hub: its stage lines under, its dock entry over."""
        now = time.time()
        if not self.cycle or now >= self.start + self.length:
            self.cycle, self.start = f"preview{next(self.cycles):04}", now
        entries = hub.compaction_entries(stages(self.cycle, self.start, now, self.compacting, self.end), True, now)
        heading = f"Previewing the {self.scene} scene (compacting {self.compacting:g} s, then {self.end}). " \
                  "Ctrl+C, q or Esc quits."
        self.lines.update("\n\n".join([heading, *(e.text for e in entries if e.who == "stage")]))
        self.overlay.show(next((e.text for e in entries if e.who == "dock"), None))


def main(name: str, compacting: float = COMPACTING, end: str = "completed") -> None:
    ScenePreview(name, compacting, end).run()
