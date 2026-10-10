"""The hub: what the wheelhouse knows and does, whatever shows it (.plan/remote.md, phase 1).
The TUI owns one, in process, and reads and acts through it; phase 2 puts it in a daemon,
behind HTTP. It never imports Textual."""

from .store import Store


class Hub:
    """The wheelhouse's service over the store. Its state lives as long as it does."""

    def __init__(self, store: Store | None = None):
        self.store = store or Store()
        # unsent text typed for each target, (session id, ref or None), kept in memory only
        self.unsent: dict[tuple, str] = {}

    # text typed but not submitted

    def keep(self, target: tuple, text: str) -> None:
        """What's typed for a target, kept until the box shows it again; blank, forgotten."""
        if text.strip():
            self.unsent[target] = text
        else:
            self.unsent.pop(target, None)

    def take(self, target: tuple) -> str:
        """What was typed for a target, handed back to show: no longer kept here."""
        return self.unsent.pop(target, "")
