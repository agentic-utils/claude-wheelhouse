"""Text typed but not submitted, for each target: the client's, never the hub's
(.plan/remote.md, State and reconnection). Kept in memory for now; with a remote client it
goes to a local file per target, so it survives a closed lid."""


class Unsent:
    """What's typed for each target, (session id, ref or None), until the box shows it again."""

    def __init__(self):
        self.texts: dict[tuple, str] = {}

    def keep(self, target: tuple, text: str) -> None:
        """What's typed for a target, kept until the box shows it again; blank, forgotten."""
        if text.strip():
            self.texts[target] = text
        else:
            self.texts.pop(target, None)

    def take(self, target: tuple) -> str:
        """What was typed for a target, handed back to show: no longer kept here."""
        return self.texts.pop(target, "")
