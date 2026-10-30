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
MAX_TIPS = 3   # tips a dialog shows, SDK mode on: the rest go to the release notes, ADVANCED (Doug, #101 9)

# how a dialog advances: on Next, on the person doing what it asks, on a choice, on Close, or
# on Next once what it waits for has happened (Next off until then: Doug, #174)
NEXT, ACTION, CHOICE, CLOSE, WAIT = "next", "action", "choice", "close", "wait"


@dataclass(frozen=True)
class Stop:
    """One of the tour's dialogs. `pane` is the id of the pane it is painted beside (None:
    centred), `title` its heading in the release notes. `ask`: what it asks the person to do,
    its last paragraph, under its tips; `button`: the caption of the button that does it for
    them, the action then how to do it themselves (Doug, #174: "Send (Ctrl+S)")."""
    id: str
    pane: str | None
    title: str
    text: str
    advance: str
    ask: str = ""
    button: str = ""


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
         "New here? A five-minute tour, with a demo session that costs a few cents.\n\n"
         "Once it's under way, Esc, then Enter, ends it at any time.", CHOICE),
    Stop("principles", None, "The wheelhouse",
         "Every Claude session in one place. Sessions ask you questions, record decisions and report tasks, "
         "all in one inbox. A session that needs you says so in its row, so you never hunt through tabs.\n\n"
         "Press F1 for every key and button.", NEXT),
    Stop("create", "sessions", "Starting a session", "",
         ACTION, ask="Start a session: press Alt+N, or the button, to create a demo session.", button="New session (Alt+N)"),
    Stop("sessions", "sessions", "The session list",
         "This is your demo. Its row shows whether it's working, idle or needs you. It runs in a tab (or here, "
         "in SDK mode): you won't need to look at it.",
         WAIT, ask="Wait now while it starts: Next lights up once it posts its first items."),
    Stop("inbox", "items", "The inbox",
         "Questions (Q), decisions (D) and tasks (T) from every session land here, open questions first.",
         ACTION, ask="Click one of the questions or press the button to move on.", button="Open Q1 (Click)"),
    Stop("answer", "detail", "Answering",
         "Ctrl+Enter queues each answer, so related answers go together. The line under the box lists the keys.",
         ACTION, ask="To answer a question, type into the text area below it (bottom right) then press Ctrl+Enter "
                     "to queue it. Do that now, or, press the button to enter an answer for you.",
         button="Answer (Ctrl+Enter)"),
    Stop("send", "session-info", "Sending",
         "Ctrl+T (Mode) switches a session between queueing answers and sending each at once.",
         ACTION, ask="Press Ctrl+S, or the button, now to send the queue as one message.", button="Send (Ctrl+S)"),
    Stop("reply", "detail", "Replies", "The session's reply arrives in the question's thread.",
         WAIT, ask="Wait now: Next lights up when it lands."),
    Stop("subagent", "items", "Subagents",
         "A session can start subagents of its own. Each shows in the inbox as an A item, and selecting it shows "
         "its conversation in Detail as it works. The demo started one to suggest titles for "
         "its story: A1.", NEXT),
    Stop("decision", "items", "Decisions", "Decisions record what a session chose.",
         ACTION, ask="Click D1 now to read it, or press the button.", button="Read D1 (Click)"),
    Stop("close", "items", "Closing an item", "Done with an item? Delete closes it, and it leaves the inbox.",
         ACTION, ask="Press Delete now to close D1, or press the button.", button="Close D1 (Delete)"),
    Stop("docs", "items", "Documents",
         "A session declares each document it writes for you to read (a design, plan or report) as a doc item "
         "(M), under the item it belongs to, and Detail shows it rendered. Click the document there to edit it; "
         "a double-click, or Enter, opens it full screen.",
         ACTION, ask="Open the demo's story now: click M1, The bedtime story, under its task, or press the button.",
         button="Read M1 (Click)"),
    Stop("conversation", "detail", "The conversation",
         "The inbox's top row for a session is its conversation: Detail follows it as it works.",
         ACTION, ask="Click the demo's conversation now, the top row of the inbox, or press the button.",
         button="Open the conversation (Click)"),
    Stop("ask", "detail", "Asking for a change",
         "The box below a conversation talks to the session itself, not to one of its items.",
         ACTION, ask="Ask it for a change now: type it in the box and press Alt+S to send it at once, or press the "
                     "button. Then watch its answer arrive.", button="Ask (Alt+S)"),
    Stop("stats", "stats", "Stats", "Tokens, context and your plan's usage, for the highlighted session.", NEXT),
    Stop("decisions", "design", "The Decisions pane",
         "Architecture decision records (ADRs): each design decision a repo keeps as a markdown file in its "
         "docs/adr directory (or wherever a session there says, set_doc_dirs).\n\n"
         "This pane lists them for every repo your sessions work in, open ones first. Highlight one to read it "
         "in Detail.", NEXT),
    Stop("prs", "prs", "The PRs pane",
         "The open pull requests your sessions made, each with a light: red for failing checks, changes asked or "
         "a conflict, orange while checks run, yellow awaiting review, green ready to merge, grey a draft.\n\n"
         "Pick a PR here or in the inbox and the other list highlights it too, if it lists it. R fetches now.", NEXT),
    Stop("end", "session-info", "Ending a session",
         "You can park a session to move it to the bottom of the session list and resume later, or you can "
         "close it entirely by \"forgetting\" it.",
         ACTION, ask="Press [Stop & Forget] now to close the demo session, or the button here.",
         button="Stop & Forget (Click)"),
    Stop("notes", None, "Release notes", "Alt+N starts a session of your own.", CLOSE),
)
# New session's line at its top while the tour has the person create the demo
LAUNCH = "This is part of the tutorial: press [Launch] to proceed."
# the dialogs a feature's tip can join: all but the invite and the notes, which show no tips
TIPPED = tuple(d.id for d in DIALOGS if d.id not in ("invite", "notes"))

# every feature a user can see, from #78 on: see AGENTS.md, "Docs kept in step"
FEATURES: tuple[Feature, ...] = (
    Feature("tour", UNRELEASED, ADVANCED,
            note="A guided tour: a box beside each pane walks you through a demo session."),
    Feature("session-sort", UNRELEASED, ADVANCED,
            note="Every session list column sorts: click its heading to cycle up, down and back."),
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
    Feature("modules-switch", UNRELEASED, ADVANCED,
            note="claude-wheelhouse modules lists the modules installed; modules disable and enable switch one off and on."),
    Feature("doc-dirs", UNRELEASED, ADVANCED,
            note="A session can tell the wheelhouse where its repo keeps its decisions, when that isn't docs/adr (set_doc_dirs)."),
    Feature("decisions-pane", UNRELEASED, "decisions",
            note="A Decisions pane under the inbox lists every repo's design decisions (docs/adr), shown in Detail.",
            tip="This pane is new: it lists every repo's design decisions, under the inbox."),
    Feature("swap-panes", UNRELEASED, "principles",
            "Drag a pane by its title onto another to swap the two; Reset layout, in the command palette, puts them back.",
            "Drag a pane by its title onto another to swap them. Ctrl+P, Reset layout, puts them back."),
    Feature("doc-editor", UNRELEASED, ADVANCED,
            note="A click on a doc opens an editor below it; Ctrl+Enter shows the edits rendered. Saving comes next."),
    Feature("helper-chat", UNRELEASED, "principles",
            note="Alt+Q asks the wheelhouse how to do something: a chat that answers from its help and its source, "
                 "through Haiku on your own Claude Code login.",
            tip="Stuck? Alt+Q asks the wheelhouse how to do something."),
    Feature("helper-alt-q", UNRELEASED, ADVANCED,
            note="The helper chat moved from Alt+G, which opens Gemini on some desktops, to Alt+Q (Q for question)."),
    Feature("tour-topups", UNRELEASED, "principles",
            note="After an update, the tour shows only what's new, pane by pane, with no demo; Esc ends it at any "
                 "time, and after quitting mid-tour, Alt+T or the title bar's badge resumes it.",
            tip="After an update, the badge in the title bar (Alt+T) offers just what's new."),
    Feature("busy-buttons", UNRELEASED, "send",
            note="A pressed button (Compact, Interrupt, Relaunch, Park, Fork...) stays off, dark grey, until what it "
                 "started has finished, or for 30 seconds with no word from the session.",
            tip="A pressed button stays dark grey until what it started has finished."),
    Feature("pins", UNRELEASED, "inbox",
            note="Pin any inbox item (📌, a click on its first column or Alt+P) and clearing the inbox leaves it; "
                 "docs come pinned.",
            tip="Ctrl+Delete removes all completed items except those that are pinned (📌). Pin an item with Alt+P, "
                "or click the first column."),
    Feature("doc-threads", UNRELEASED, "docs",
            note="A doc has a thread of its own: comment on it below the doc (split) or on a Thread tab (tabbed); "
                 "Ctrl+L switches the two, Ctrl+PgUp and Ctrl+PgDn the tabs.",
            tip="Comment on a doc in its thread, below it. Ctrl+L switches to tabs, Ctrl+PgUp and Ctrl+PgDn between them."),
    Feature("kind-colours", UNRELEASED, "decision",
            note="An inbox item's ref is coloured by its kind (a task green, a question yellow, a decision purple, "
                 "a PR light blue), and resting the pointer on a row, there or in the session list, shows it in full.",
            tip="A ref's colour says its kind. Rest the pointer on a row to read its full title."),
    Feature("pr-items", UNRELEASED, ADVANCED,
            note="A pull request a session makes shows in the inbox as PR and its number, under the task it was "
                 "made for, with GitHub's status: merged and closed ones dim."),
    Feature("prs-pane", UNRELEASED, "prs",
            note="The PRs pane lists the open PRs your sessions made, or with M every open PR you authored, each "
                 "with a traffic light for checks, review and conflicts; with a session selected, its PRs alone.",
            tip="It follows the session selected, as the inbox does. M: your sessions' PRs or all of yours."),
    Feature("session-menu", UNRELEASED, ADVANCED,
            note="Right-click a session in the session list for a menu of its buttons, acting on that session."),
    Feature("send-now", UNRELEASED, "send",
            note="Alt+S sends the answer in the box at once, even in Queued mode; the rest of the queue waits.",
            tip="Alt+S allows you to send the answer you have just typed, bypassing the queue."),
    Feature("send-wakes", UNRELEASED, ADVANCED,
            note="Ctrl+S or Alt+S on a session that isn't running asks to Unpark (a parked one) or Relaunch it "
                 "and send: it wakes with your message and its queue as its first prompt."),
    Feature("pr-actions", UNRELEASED, "prs",
            note="A PR in the PRs pane has Open, Merge and Request review buttons, and a right-click on its row gives "
                 "them as a menu: Merge asks first and says what stood in its way, and Request review asks the "
                 "session that made the PR to post the request on Slack.",
            tip="Open, Merge and Request review act on the PR selected; right-click a PR for them as a menu."),
    Feature("undo", UNRELEASED, "inbox",
            note="Ctrl+Z in the inbox undoes the last delete, pin or reopen, an open question coming back open; "
                 "Delete on an open question asks first.",
            tip="Deleted something by mistake? Ctrl+Z brings it back as it was."),
    Feature("pr-detail", UNRELEASED, "prs",
            note="Detail, showing a PR, has its Open, Merge, Request review and Close buttons, and its failing "
                 "checks, description, history and comments.",
            tip="Pick a PR to read its failing checks, description, history and comments in Detail."),
    Feature("pr-marks", UNRELEASED, ADVANCED,
            note="Mark PRs in the PRs pane as in the inbox (Ctrl+click, Shift+click, Ctrl+Space, Shift+Up/Down) and act "
                 "on them all; Close closes a PR unmerged, asked first, as Merge is."),
    Feature("pr-action-states", UNRELEASED, ADVANCED,
            note="A PR's buttons that can't apply are greyed, and say why on hover (Merge on a PR awaiting review, "
                 "say), and Mark ready or Make draft switches a PR between draft and ready for review."),
    Feature("session-steps", UNRELEASED, ADVANCED,
            note="The wheelhouse tells a session before it ends or resumes (and, in SDK mode, compacts), so its own "
                 "instructions can act, and can keep a handoff doc for it. Session steps, in the command palette, "
                 "has every switch; the first press asks once."),
    Feature("compacting-shown", UNRELEASED, "sessions", when="sdk",
            note="While a session is compacting, its Compact button is off, its inbox items and its Detail dim and "
                 "ignore clicks, and its busy eye in the session list turns blue.",
            tip="A blue eye: the session is compacting; its items dim until it's done."),
    Feature("keepalive-50", UNRELEASED, ADVANCED,
            note="The keepalive comes every 50 minutes, ahead of Claude Code's idle compaction at about 54."),
    Feature("compact-at-keepalive-end", UNRELEASED, ADVANCED,
            note="A session run in the wheelhouse that was being kept warm is compacted when the keepalive's "
                 "hours run out, while its cache is warm; a tab idle-compacts then, past 150k tokens."),
    Feature("handoff-tool", UNRELEASED, ADVANCED,
            note="Sessions keep a handoff with the write_handoff tool, shown as their handoff doc; Stop & Forget "
                 "and Compact ask for one."),
    Feature("model-compact", UNRELEASED, ADVANCED, when="sdk",
            note="A session run in the wheelhouse can ask to be compacted itself, with the request_compact tool "
                 "and what it wants kept: it is compacted once its turn ends, as Compact would."),
    Feature("model-park", UNRELEASED, ADVANCED, when="sdk",
            note="A session run in the wheelhouse parks itself once its useful work is done, compacted first "
                 "as Park with Compact would; one that skips the compaction says why, as a decision."),
    # the tour's showcase (#101 9): what a new user would otherwise miss, highest-ranked first
    Feature("relaunch-tip", UNRELEASED, "sessions",
            note="After a reboot or a crash, a session that isn't running shows a red dot: Alt+L relaunches it, "
                 "Alt+Shift+L relaunches every one.",
            tip="A red dot: not running, after a reboot, say. Alt+L relaunches it, Alt+Shift+L all."),
    Feature("adopt-tip", UNRELEASED, "sessions",
            note="Alt+A adopts a Claude Code session you started outside the wheelhouse: exit it in its tab first.",
            tip="Already have sessions running? Alt+A adopts them (exit each in its tab first)."),
    Feature("recall-tip", UNRELEASED, "answer",
            note="Ctrl+R takes a queued answer back into the box to edit it, before it's sent.",
            tip="Typo in a queued answer? Ctrl+R takes it back into the box to edit."),
    Feature("show-all-tip", UNRELEASED, "create",
            note="Selecting a session narrows the inbox to its items: Esc, or a click on the selected session, "
                 "shows every session's again.",
            tip="Selecting a session narrows the inbox to it: a click on it again shows every session's."),
    Feature("bulk-clear-tip", UNRELEASED, "close",
            note="Ctrl+A in the inbox marks every finished item and Delete clears them together; Alt+V shows "
                 "finished items again.",
            tip="Alt+V shows finished items, or hides them again."),
    Feature("fork-tip", UNRELEASED, "end",
            note="Fork (Alt+K) carries a session on in a copy, in a new worktree, taking the items you choose.",
            tip="Fork (Alt+K) carries a session on in a copy, in a new worktree."),
    Feature("compact-tip", UNRELEASED, "send", when="sdk",
            note="A hosted session's dot turns amber past 200k tokens of context, when it asks whether to "
                 "compact: Compact (Alt+C) shrinks it.",
            tip="An amber dot: past 200k tokens, and the session asks whether to compact. Alt+C shrinks it."),
    Feature("permission-tip", UNRELEASED, "inbox", when="sdk",
            note="A hosted session asking to run a tool shows in the inbox as an orange row: Alt+Y Allow, "
                 "Alt+W Always, Alt+D Deny.",
            tip="An orange row asks to run a tool: Alt+Y Allow, Alt+W Always, Alt+D Deny."),
    Feature("interrupt-shell-tip", UNRELEASED, "create", when="sdk",
            note="Interrupt (Alt+I) stops a hosted session's turn; Shell (Alt+H) opens it in a real tab, for slash "
                 "commands.",
            tip="Alt+I interrupts a turn. Alt+H opens the session in a real tab, for slash commands."),
    Feature("model-effort-tip", UNRELEASED, "stats", when="sdk",
            note="Model (Alt+M) changes a hosted session's model live, and the Stats slider sets its effort.",
            tip="Alt+M changes the model live; the slider sets the effort."),
    Feature("stats-zoom-tip", UNRELEASED, "stats",
            note="Stats' - and + zoom its charts out to days, and its cache line says when the next turn "
                 "costs more.",
            tip="- and + zoom the charts out to days. The cache line says when a turn gets dearer."),
    # notes only: the docs box has no line to spare at 80x24 without covering M1
    Feature("doc-quote", UNRELEASED, ADVANCED,
            note="Click a block of a doc and your next comment on it quotes that block."),
    Feature("detail-double-click", UNRELEASED, ADVANCED,
            note="Double-click what Detail shows, a doc included, to open it full screen, and double-click again "
                 "to go back. A single click keeps its own job, such as editing a doc."),
    Feature("layout-keys", UNRELEASED, ADVANCED,
            note="Alt+Up and Alt+Down reorder rows; drag a splitter to resize panes, and double-click it to reset."),
    Feature("key-modifiers", UNRELEASED, ADVANCED,
            note="Every key has a modifier, so a letter typed in the answer box is always just typed, and the "
                 "app's own keys work from there: Ctrl+Q quits, F1 lists the keys, Alt+N is New session."),
    Feature("mcp-reauth", UNRELEASED, ADVANCED, when="sdk",
            note="When a session needs an MCP server that wants signing in, or has failed, it asks you, with the "
                 "sign-in link; the question closes once the server is connected. Relaunch a session started "
                 "before this to stop it asking about every server."),
    Feature("mcp-servers", UNRELEASED, ADVANCED, when="sdk",
            note="Alt+Shift+M lists the MCP servers of the session in context, with a button to sign in to or "
                 "reconnect each; a claude.ai connector reconnects in every session that has it."),
    Feature("all-conversations", UNRELEASED, ADVANCED,
            note="With no session selected, the inbox lists each session's conversation (💬, named after the "
                 "session) at the top, and orders its items by session name within each status."),
    Feature("emoji-pick", UNRELEASED, ADVANCED,
            note="Emoji suggestions take a click: type :star and click the one you want, or choose it with Up "
                 "and Down and take it with Tab or Enter."),
    # notes only: the inbox's dialog has its MAX_TIPS already
    Feature("delete-completed", UNRELEASED, ADVANCED,
            note="Ctrl+Delete in the inbox deletes every completed item at once, but the pinned, with no question: "
                 "Ctrl+Z brings them all back."),
    Feature("autocleanse", UNRELEASED, ADVANCED,
            note="A completed item leaves the inbox 5 minutes after it completed (AUTOCLEANSE_MINUTES in a .env, 0 "
                 "for never); Alt+V shows it again."),
    Feature("since-away", UNRELEASED, ADVANCED,
            note="Back after two hours or more away, a card at the top of the inbox says what happened meanwhile: "
                 "what needs you, what landed, which sessions went quiet and what was decided. Esc dismisses it."),
    # notes only: the stats dialog has its MAX_TIPS already
    Feature("session-info", UNRELEASED, ADVANCED,
            note="The ⓘ at the stats pane's top right shows the selected session's info: its id (C copies it), "
                 "user, machine, branch, model, transcript, and its MCP server's code, flagged when it needs a relaunch."),

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
