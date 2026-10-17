"""What's new: the tour's dialogs and every feature a user can see, from #78 on
(.plan/tutorial-overhaul.md).

Each feature is registered against the tour dialog for the pane it changes, with a tip shown
there, or as ADVANCED, listed only in the release notes. A new user's tour shows every tip;
an upgrading user's top-up shows only the pending ones (pending), never judged by version.
The release notes for a version come from the same registry (notes,
`claude-wheelhouse notes`), and `make stamp VERSION=...` gives each UNRELEASED feature its
release (stamp).

Never imports the store or Textual: the store's migration reads FEATURES.
"""

import importlib.metadata
import re
import sys
from dataclasses import dataclass
from pathlib import Path

UNRELEASED = "UNRELEASED"   # a feature's version until `make stamp` gives it the release's
ADVANCED = "advanced"   # a feature's where: for experienced users, in the release notes only
WHENS = ("", "sdk")   # a feature's when: always, or only with SDK mode on

# how a dialog advances: on Next, on the person doing what it asks, on a choice, or on Close
NEXT, ACTION, CHOICE, CLOSE = "next", "action", "choice", "close"


@dataclass(frozen=True)
class Stop:
    """One of the tour's dialogs. `pane` is the id of the pane it is painted beside (None:
    centred), `title` its heading in the release notes."""
    id: str
    pane: str | None
    title: str
    text: str
    advance: str


@dataclass(frozen=True)
class Feature:
    id: str           # stable for ever: the seen record (store.tour_seen) keys on it
    version: str      # UNRELEASED until `make stamp` stamps it
    where: str        # a tour dialog id ("inbox", "stats", ...) or ADVANCED
    note: str         # one line for the release notes
    tip: str = ""     # one or two lines added to that dialog; required unless ADVANCED
    when: str = ""    # "sdk": offered only with SDK mode on


DIALOGS = (
    Stop("invite", None, "The tour",
         "New here? A five-minute tour, with a demo session that costs a few cents.", CHOICE),
    Stop("principles", None, "The wheelhouse",
         "Every Claude session in one place. Sessions ask you questions, record decisions and report tasks, "
         "all in one inbox. A session that needs you says so in its row, so you never hunt through tabs.", NEXT),
    Stop("create", "sessions", "Starting a session",
         "Start a session: press N. For the tour it's filled in for you, so just press Start.", ACTION),
    Stop("sessions", "sessions", "The session list",
         "This is your demo. Its row shows whether it's working, idle or needs you. It runs in a tab (or here, "
         "in SDK mode): you won't need to look at it.", ACTION),
    Stop("inbox", "items", "The inbox",
         "Questions (Q), decisions (D) and tasks (T) from every session land here, open questions first. "
         "Highlight Q1.", ACTION),
    Stop("answer", "detail", "Answering",
         "Answer both questions here. Ctrl+Enter queues each answer, so related answers go together.", ACTION),
    Stop("send", "session-info", "Sending", "Ctrl+S, or Send, sends the queue as one message.", ACTION),
    Stop("reply", "detail", "Replies", "The session's reply arrives in the question's thread.", ACTION),
    Stop("decision", "items", "Decisions",
         "Decisions record what a session chose. Read D1, then Delete closes it.", ACTION),
    Stop("conversation", "detail", "The conversation",
         "Highlight the session to follow its conversation, and ask it for a change in the box below.", ACTION),
    Stop("stats", "stats", "Stats", "Tokens, context and your plan's usage, for the highlighted session.", NEXT),
    Stop("end", "session-info", "Ending a session",
         "Done with the demo: Stop & Forget ends it and clears its items.", ACTION),
    Stop("notes", None, "Release notes", "N starts a session of your own.", CLOSE),
)
# the dialogs a feature's tip can join: all but the invite and the notes, which show no tips
TIPPED = tuple(d.id for d in DIALOGS if d.id not in ("invite", "notes"))

# every feature a user can see, from #78 on: see AGENTS.md, "Docs kept in step"
FEATURES: tuple[Feature, ...] = (
    Feature("swap-panes", UNRELEASED, "principles",
            "Drag a pane by its title onto another to swap the two; Reset layout, in the command palette, puts them back.",
            "Drag a pane by its title onto another to swap them. Ctrl+P, Reset layout, puts them back."),
)


def pending(seen, sdk: bool, features=FEATURES) -> list[Feature]:
    """The registered features with no seen record (shown or skipped), in registry order,
    leaving out an `sdk` one while SDK mode is off. Never reads versions, so a release the
    person skipped still has its features offered; a seen record for a feature no longer
    registered changes nothing."""
    seen = set(seen)
    return [f for f in features if f.id not in seen and (f.when != "sdk" or sdk)]


def notes(version: str, features=FEATURES, dialogs=DIALOGS) -> str:
    """A version's release notes, as Markdown: its features under their dialogs' titles, in
    tour order, then Advanced."""
    mine = [f for f in features if f.version == version]
    if not mine:
        return f"## What's new in {version}\n\nNothing new to show: fixes only.\n"
    groups = [(d.title, [f for f in mine if f.where == d.id]) for d in dialogs]
    groups.append(("Advanced", [f for f in mine if f.where == ADVANCED]))
    out = [f"## What's new in {version}"]
    for title, found in groups:
        if found:
            out.append(f"### {title}\n\n" + "\n".join(f"- {f.note}" for f in found))
    return "\n\n".join(out) + "\n"


def is_release(version: str) -> bool:
    """A version that is published as it stands: not a development one (`0.3.0.dev1`)."""
    return version != UNRELEASED and ".dev" not in version


def unreleased(features=FEATURES) -> list[Feature]:
    return [f for f in features if f.version == UNRELEASED]


def running_version() -> str:
    return importlib.metadata.version("claude-wheelhouse")


# the FEATURES tuple's body: from its opening line to the parenthesis that starts a line
REGISTRY = re.compile(r"^FEATURES\b[^\n]*\(\n(.*?)^\)", re.M | re.S)
# in that body, a feature's version given positionally or by name, never in a comment
UNSTAMPED = re.compile(r"^([^#\n]*?(?:Feature\(\s*\"[^\"]+\",\s*|\bversion=))UNRELEASED\b", re.M)


def stamp(source: str, version: str) -> tuple[str, int]:
    """This module's source with every UNRELEASED feature in FEATURES given `version`, and
    how many were."""
    if not is_release(version):
        raise ValueError(f"stamp a release version, not {version!r}")
    found = REGISTRY.search(source)
    if found is None:
        return source, 0
    body, n = UNSTAMPED.subn(lambda m: f'{m[1]}"{version}"', found[1])
    return source[:found.start(1)] + body + source[found.end(1):], n


def main(argv: list[str]) -> None:
    """`python -m claude_wheelhouse.whatsnew stamp VERSION`: stamps this file in place, for
    `make stamp` (and, once it is built, `make release`, .plan/release.md)."""
    if len(argv) != 2 or argv[0] != "stamp":
        sys.exit("usage: python -m claude_wheelhouse.whatsnew stamp VERSION")
    path = Path(__file__)
    before = path.read_text()
    after, n = stamp(before, argv[1])
    path.write_text(after)
    print(f"stamped {n} feature(s) {argv[1]}")


if __name__ == "__main__":
    main(sys.argv[1:])
