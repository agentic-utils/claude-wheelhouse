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
         "Start a session: press N. For the tour it's filled in for you, so just press Launch.", ACTION),
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
    Feature("tour", UNRELEASED, "principles",
            note="A guided tour: a box beside each pane walks you through a demo session.",
            tip="This tour is new: a box beside each pane says what it is for."),
    Feature("session-sort", UNRELEASED, "sessions",
            note="Every session list column sorts: click its heading to cycle up, down and back.",
            tip="Click a column heading to sort by it; click again to reverse it."),
    Feature("stats-pane", UNRELEASED, "stats",
            note="Stats has a pane of its own, under the session list.",
            tip="Stats now has a pane of its own, under the session list."),
    Feature("pane-titles", UNRELEASED, ADVANCED, note="Each pane carries its title in its top border."),
    Feature("dialog-keys", UNRELEASED, ADVANCED,
            note="Every dialog answers the same keys: Esc or a click outside cancels, Enter presses its main button."),
    Feature("tab-order", UNRELEASED, "answer",
            note="Tab goes round the session list, the inbox, the answer box and the buttons.",
            tip="Tab goes round the session list, the inbox, the answer box and the buttons."),
    Feature("make-targets", UNRELEASED, ADVANCED,
            note="make run installs when it needs to, make install fetches uv, and make args lists the app's flags."),
    Feature("swap-panes", UNRELEASED, "principles",
            "Drag a pane by its title onto another to swap the two; Reset layout, in the command palette, puts them back.",
            "Drag a pane by its title onto another to swap them. Ctrl+P, Reset layout, puts them back."),
    Feature("tour-topups", UNRELEASED, "principles",
            note="After an update, the tour shows only what's new, pane by pane, with no demo; Esc pauses it, "
                 "and L or the title bar's badge resumes it.",
            tip="After an update, the badge in the title bar (L) offers just what's new."),
)


def pending(seen, sdk: bool, features=FEATURES) -> list[Feature]:
    """The registered features with no seen record (shown or skipped), in registry order,
    leaving out an `sdk` one while SDK mode is off. Never reads versions, so a release the
    person skipped still has its features offered; a seen record for a feature no longer
    registered changes nothing."""
    seen = set(seen)
    return [f for f in features if f.id not in seen and (f.when != "sdk" or sdk)]


def notes(version: str, features=FEATURES, dialogs=DIALOGS, label: str | None = None) -> str:
    """A version's release notes, as Markdown: its features under their dialogs' titles, in
    tour order, then Advanced. `label` names the version in the heading, where that isn't
    the one the features carry (this_release)."""
    mine = [f for f in features if f.version == version]
    version = label or version
    if not mine:
        return f"## What's new in {version}\n\nNothing new to show: fixes only.\n"
    groups = [(d.title, [f for f in mine if f.where == d.id]) for d in dialogs]
    groups.append(("Advanced", [f for f in mine if f.where == ADVANCED]))
    out = [f"## What's new in {version}"]
    for title, found in groups:
        if found:
            out.append(f"### {title}\n\n" + "\n".join(f"- {f.note}" for f in found))
    return "\n\n".join(out) + "\n"


def this_release(features=FEATURES) -> str:
    """The running version's notes, for the tour's last dialog: a development version's are
    the features not yet stamped (UNRELEASED)."""
    version = running_version()
    return notes(version if is_release(version) else UNRELEASED, features, label=version)


def since(seen: str | None, features=FEATURES) -> str:
    """An upgrader's release notes (Q121 B): this release's (this_release), then every
    earlier release's newer than `seen` (the last version whose notes the tour showed, None
    for none), newest first. A release with no features registered has nothing to list."""
    running = running_version()
    older = {f.version for f in features if is_release(f.version) and f.version != running
             and version_key(f.version) < version_key(running)
             and (seen is None or version_key(f.version) > version_key(seen))}
    return "\n".join([this_release(features), *(notes(v, features) for v in sorted(older, key=version_key, reverse=True))])


def version_key(version: str) -> tuple:
    """A version's order: its release numbers, a development one (`0.3.0.dev1`) before its
    release. Anything unreadable sorts first."""
    found = re.match(r"(\d+(?:\.\d+)*)(?:\.dev(\d+))?", version)
    if found is None:
        return ((), 0, 0)
    return tuple(int(n) for n in found[1].split(".")), found[2] is None, int(found[2] or 0)


def tips(stop: str, sdk: bool, features=FEATURES) -> list[Feature]:
    """The features whose tips a tour dialog shows, in registry order, leaving out an `sdk`
    one while SDK mode is off."""
    return [f for f in features if f.where == stop and f.tip and (f.when != "sdk" or sdk)]


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
