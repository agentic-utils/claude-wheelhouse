# Tutorial overhaul: invite, principles, a tour of the screen, what's new (design)

Issue #78. Status: for review. Builds on `.plan/tutorial.md` (the tutorial as built, and the
top-up design) and `.plan/modules.md` (six slots, panes the user can swap).

Doug's asks, verbatim: "(1) the invite needs to be a centered dialog, not the banner which
is not very obvious to a new user; (2) the tutorial should start with a concise description
of the *principles* of wheelhouse (all sessions in one place, structured interactions
through questions/decisions/tasks in the inbox, sessions show when they need attention so
you don't need to click through multiple tabs); (3) then it should conduct a walkthrough of
the elements of the screen, by painting dialogs on top of the elements it wants to draw the
user's attention to, STARTING WITH creation of the dummy session; (4) each feature we add
from here forwards should be associated either with one of those dialogs (in which case,
when a returning user upgrades, the "top-up" tutorial they see should show only those new
features in each dialog and skip any dialog for which no new features are registered), or
should be marked "advanced" in which case it should be limited to listing in the release
update notes; (5) at the end of the tutorial the user should be shown one final dialog
containing the release notes for this release; (6) use SDK for the dummy session if SDK
mode is enabled; (7) in the prompt to the dummy session, add a note in caps to the user
saying something like "TO THE USER: THIS IS A DUMMY SESSION FOR THE TUTORIAL; FOLLOW THE
PROMPTS ON THE WHEELHOUSE SCREEN TO LEARN HOW TO USE IT"."

## What changes from today

| Today | After |
|---|---|
| A one-line offer across the top of the screen (D98) | A centred dialog: Start, Later, Never |
| A nine-step checklist at the top of the Detail pane | Tour dialogs painted beside the pane they describe |
| The demo is always a tab (D66) | SDK mode on: hosted in the wheelhouse; off: a tab |
| Top-ups designed as steps with `new_in` (not built) | Features registered against tour dialogs; a top-up shows only unseen ones |
| Release notes from commits (`.plan/release.md`) | Release notes generated from the same feature registry |

Kept: the demo's scratch directory and marker, `make tutorial` re-arming the invite, Later
handing over to the title-row badge (D64), and seen state recorded per feature, never
inferred from versions.

## The flow

A new user sees the invite, then dialogs 1 to 12 in order. Each tour dialog (a "callout")
anchors to a pane by its id, highlights that pane's border, and advances either on a user
action (detected from the store or the screen, as the checklist's checks do today) or on
Next (Enter, or a click on Next). Esc on any callout pauses the tour: the badge offers to
resume at the same dialog.

| # | Id | Anchors to | Draft text | Advances on |
|---|---|---|---|---|
| 0 | `invite` | centred | "New here? A five-minute tour, with a demo session that costs a few cents." Enter Start · L Later · N Never (Esc, or a click outside, is Later: a misclick never loses the tour) | a choice |
| 1 | `principles` | centred | "Every Claude session in one place. Sessions ask you questions, record decisions and report tasks, all in one inbox. A session that needs you says so in its row, so you never hunt through tabs." | Next |
| 2 | `create` | Sessions | "Start a session: press N. For the tour it's filled in for you, so just press Start." | the demo session's row appears |
| 3 | `sessions` | Sessions | "This is your demo. Its row shows whether it's working, idle or needs you. It runs in a tab (or here, in SDK mode): you won't need to look at it." | the demo posts its first item |
| 4 | `inbox` | Inbox | "Questions (Q), decisions (D) and tasks (T) from every session land here, open questions first. Highlight Q1." | a question highlighted or opened |
| 5 | `answer` | Detail | "Answer both questions here. Ctrl+Enter queues each answer, so related answers go together." | both answers queued |
| 6 | `send` | Session info | "Ctrl+S, or Send, sends the queue as one message." | the answers sent |
| 7 | `reply` | Detail | "The session's reply arrives in the question's thread." | a reply after sending |
| 8 | `decision` | Inbox | "Decisions record what a session chose. Read D1, then Delete closes it." | the decision closed |
| 9 | `conversation` | Detail | "Highlight the session to follow its conversation, and ask it for a change in the box below." | a turn of Claude's watched arriving |
| 10 | `stats` | Stats | "Tokens, context and your plan's usage, for the highlighted session." | Next |
| 11 | `end` | Session info | "Done with the demo: Stop & Forget ends it and clears its items." | the demo session gone |
| 12 | `notes` | centred | This release's notes (below), then "N starts a session of your own." | Close |

- #101 reworded the dialogs (whatsnew.DIALOGS holds the text now): each one that waits on
  the person says what to do now ("press N now", "Press [Stop & Forget] now"), New session
  opened for the demo says "This is part of the tutorial: press [Launch] to proceed.", and
  each paragraph has a blank line after it. It added `docs` (Inbox, an action) after `decision`:
  the demo now writes a children's bedtime story (questions on its characters and setting, a
  decision on its length) and declares it as a doc, which the person opens. The Decisions
  pane's dialog explains ADRs. Esc ends
  the tour at any time, for good, wherever focus is on the main screen and in the tour's own
  New session (Q122's pause is replaced): tour_at is cleared and every feature counts as
  seen, so no badge offers it; the demo keeps running, and `claude-wheelhouse tutorial` offers
  the tour afresh. Quitting mid-tour still resumes from the badge.
  The launched demo is selected in the session list. A callout's place (callout.place) is
  one rule: of its candidates (beside, inside or centred as before; below every inbox row;
  below the step's rows; the screen's foot; its top), the nearest its pane that covers
  neither the demo's rows its step needs (tour.ROWS: the questions, D1, M1 or the
  conversation row) nor its pane, else the least over them. It is placed as the step starts,
  on a resize or when its pane moves, and otherwise stays put unless it comes to cover them.
  Its pointer faces the pane from where it ends up, and the pane stays highlighted.
- #133: a callout tries two widths, 46 and the screen's width less 4, placing itself by the
  rule above at each, and takes the narrower unless it falls short where the wider doesn't:
  covering the step's rows, then cut short by the screen, then covering its pane beside it.
  A wider box wraps its text to fewer rows, so at 80x24 the inbox and docs steps clear the
  demo's rows and show all their text. The tests walk the whole tour at 160x40, 100x30 and
  80x24 and check each box's pane is there, its text fits and the rows are uncovered.
- #101 and the app of 23 Oct: the principles say `?` lists every key and button; the answer
  step points at the line under the box; the send step names Ctrl+T (Mode); the PRs step
  says a PR picked there or in the inbox is highlighted in both, its tips covering the pane
  following the session, M, and the Open, Merge and Request review actions.
- #174 (Doug's walk of 24 Oct): while a callout shows, everything else on the screen is
  dimmed (callout.DIM) but its pane and, for a step with inbox rows (tour.ROWS), the inbox.
  Each dialog that asks for something has an action button under its text, captioned with
  the action and how to do it yourself ("New session (N)", "Open Q1 (Click)", "Answer
  (Ctrl+Enter)", "Send (Ctrl+S)", "Read D1 (Click)", "Close D1 (Delete)", "Read M1 (Click)",
  "Open the conversation (Click)", "Ask (Alt+S)", "Stop & Forget (Click)"), off until it can
  act; pressing it does what the person would, and the tour moves on the same way. What it
  asks is the last paragraph (Stop.ask), under the tips. `sessions` and `reply` wait (WAIT):
  a Next, off until the demo has posted or replied. Once one answer is queued, the other
  question is selected. `decision` splits into reading D1 and `close`, whose callout keeps
  clear of Detail (tour.SHUN); `conversation` splits into picking it and `ask`. Esc asks
  first (LeaveTour: Enter leaves, Esc stays).
- The checks for 4 to 9 and 11 are today's step checks, moved over unchanged, including the
  rules that the app's own automatic selection never ticks anything.
- A callout whose pane is absent (Stats disabled or uninstalled) is skipped, and its
  features count as seen only once the pane exists and the dialog has shown.
- Start your own (today's last step) folds into the notes dialog's last line, so the
  checklist no longer outlives the demo (D78/D80 retire).

## Painting a callout over its pane

**One overlay, not a modal.** Steps 2 to 11 need the screen underneath to stay usable, so a
callout is a widget, not a `ModalScreen`. The main screen gets a top layer (`layers: base
tour`); the `TourCallout` lives there, positioned absolutely, its `offset` computed from the
target pane's `region` (screen coordinates). Dialogs 0, 1 and 12 are centred `Dialog`s
(modal), as nothing underneath is needed.

**Anchored by identity.** Each dialog names a pane id (`sessions`, `session-info`, `items`,
`detail`, `stats`), never a slot or position. The tour looks the pane up with
`query_one(f"#{pane}")` each time it places the callout, so a pane swapped into another slot
takes its callout with it. The highlight is a CSS class (`tour-target`, a yellow border) on
that pane widget, so it follows the widget through a swap too. Its colour differs from the
swap target's teal.

**Placement.** Width `min(46, screen width - 4)`, height from its text. The tour tries the
sides of the pane's region in order (right, left, below, above) and takes the first where
the callout fits without covering the pane; the callout's border title points at it
(`◀ Inbox`). Where nothing fits, the callout sits inside the pane over its top rows. If the
pane is narrower than the callout or collapsed, it is centred and its first line names the
pane by its title ("In the Inbox pane:").

**Staying correct.** Placement reruns on every layout refresh of the screen (Textual's
`screen_layout_refresh_signal`), which a resize, a swap and a splitter drag all cause, and
does anything only when the screen size or the target's region differs from the one last
placed against. While another screen covers the main one (`ThreadView`,
`DocView`, a `Dialog`), the callout is hidden. Step 2 is the one exception: `NewSession`
shows the tour's line at its top while it is open, and its fields come filled in.

**Focus.** A Next callout takes focus, so Enter is Next. An action callout never takes focus,
so the keys it teaches reach the app.

## The feature registry

A new module, `claude_wheelhouse/whatsnew.py` (`features.py` is the settings), holding the
tour's dialogs and every user-visible feature from here on.

```python
@dataclass(frozen=True)
class Feature:
    id: str           # stable for ever: the seen record keys on it
    version: str      # UNRELEASED until `make release` stamps it
    where: str        # a tour dialog id ("inbox", "stats", ...) or ADVANCED
    note: str         # one line for the release notes
    tip: str = ""     # one or two lines added to that dialog; required unless ADVANCED
    when: str = ""    # "sdk": offered only with SDK mode on

DIALOGS = (Stop("principles", pane=None, title="The wheelhouse", text=..., advance=NEXT), ...)   # the table above
FEATURES = (Feature("pane-swap", UNRELEASED, "sessions", note="Drag a pane by its title to swap it with another.", tip="Drag any pane by its title to move it; Ctrl+P, Reset layout puts them back."), ...)
```

- **Dialog or advanced.** `where` has no default, so whoever adds a feature has to choose.
  A dialog feature's `tip` is shown in that dialog, for new users and in top-ups; an
  advanced feature appears only in the release notes.
- **New users** see each dialog's base text plus every tip registered to it, then every
  feature is recorded as seen.
- **Releases.** `make stamp VERSION=0.3.0` replaces `UNRELEASED` with the version; `make
  release` (`.plan/release.md`, not yet built) runs it in the commit it makes. A test fails if a released package (version without `.dev`)
  still has `UNRELEASED` entries.
- **Release notes come from the registry.** `claude-wheelhouse notes [VERSION]` prints a
  version's notes as Markdown: its features grouped under their dialog's `title` (in tour
  order), then Advanced. The release workflow puts that at the top of the GitHub release, with the
  commit list below it for fixes. The notes dialog renders the same text.
- **Modules** (later, with `.plan/modules.md` stage 5): `api.Module` may add `dialogs` and
  `features` for its own panes, merged into the registry at start.

### The AGENTS.md rule

Added under "Docs kept in step":

> Every change a user can see adds a `Feature` to `whatsnew.FEATURES` in the same commit,
> with `version=UNRELEASED`. Set `where` to the tour dialog for the pane it changes, with a
> `tip` of at most two lines, or to `ADVANCED` when it is for experienced users only. Give
> it a one-line `note` for the release notes. Fixes add nothing. Never change or reuse a
> released feature's id.

## Top-ups for upgrading users

**Stored, per wheelhouse database** (one per user):

- a new table `tour_seen (feature_id TEXT PRIMARY KEY, seen_at TEXT, how TEXT)`, where `how`
  is `shown` or `skipped`;
- settings: `tutorial_offer` (unset, `taken`, `later`, `never`, `reset`), `tour_at` (the
  dialog a paused tour resumes at), `notes_seen` (the last version whose notes were shown).

**Pending** = registered features with no `tour_seen` row whose `when` holds (SDK mode for
`sdk`). The pending rule never reads versions, so a tester who skips a release still gets
its features.

**A top-up** shows, in tour order, only the dialogs with a pending feature, each holding
only its pending tips (no base text), every one advancing on Next. No demo session is
launched and no tokens are spent. Then the notes dialog. Each dialog's features are recorded
`shown` as it is passed. Never records every pending feature `skipped`, so only later ones
are offered.

**Offering it.** At start, if anything is pending or the running version is newer than
`notes_seen`, the centred invite shows: "Updated to 0.3.0: 3 new things in 2 places." Enter
Start, L Later (the badge carries it, D64), N Never. A release with only advanced features
skips straight to the notes dialog.

**Migration** (once, marked by a setting, as `migrated_decisions_close` is):

| Today | Becomes |
|---|---|
| `tutorial_offer = taken`, or unset with sessions | nothing written: the release's registered features are pending, so they get a top-up |
| `tutorial_offer = dismissed` | `never`; every feature registered at migration recorded `skipped` |
| unset, no sessions | nothing: a new user, offered the full tour |
| `reset` | kept: the full tour is offered |

The old code's Never (`dismissed`) is written as `never` from stage 1, so the migration
only meets `dismissed` from older code. `tutorial_seen` (the old checklist's record) is
deleted in stage 3, with the checklist that reads it, so a tutorial under way keeps its
checklist until then. A tutorial session still running
from the old code stays a normal session until Start ends it, as `stop_earlier` does now.

## The demo session

- **Runner.** `prepare` creates it with `default_runner()` instead of `"tab"`, so SDK mode on
  means hosted in the wheelhouse. `launch.restore_runner` drops its tutorial exception.
  This reverses D66, as asked. With SDK mode on, a permission (`P`) item can join dialog 5's
  text through a `when="sdk"` feature.
- **The brief** starts with a line for the person, which the session is told to ignore:

  ```
  TO THE USER: THIS IS A DUMMY SESSION FOR THE TUTORIAL. FOLLOW THE PROMPTS ON THE
  WHEELHOUSE SCREEN TO LEARN HOW TO USE IT.
  ```

  The rest of `BRIEF` is unchanged: synopsis, task, two related questions, a decision, wait.
- Created from `NewSession` filled in (dialog 2): the tutorial directory, name `tutorial`,
  the brief, and the default model and effort. The fields stay editable except the
  directory, as the tour finds its session by directory.

## Banner, badge and checklist

- **Banner** (`TutorialOffer`, a line at the top): replaced by the centred invite.
- **Badge**: kept at the right of the title row, now "Tour: 3 new · L" for pending features,
  or "Tour paused · L" for a paused tour. `L` reopens the invite. Shares the slot with the
  update notice as `.plan/release.md` says.
- **Checklist** (`#checklist`, `steps`, `STEPS`, `AFTER_END`, `tutorial_seen`): removed. Its
  checks move into the dialogs' advance conditions.
- `?` gains a "Tour" section: L, and the command that re-arms the full tour.

## Delivery

Each stage lands and is reviewed on its own. Stage 3 waits for modules stage 1 (pane ids and
titles in their slots); swapping (modules stage 3) needs only its `LayoutChanged` message.

1. **Registry and notes.** `whatsnew.py` with `DIALOGS` and an empty `FEATURES`, the
   `tour_seen` table, the migration, `claude-wheelhouse notes`, the AGENTS.md rule, the
   `make release` stamp. No visible change.
2. **Callout.** `TourCallout`, placement, highlight and re-placement, exercised by a test
   app with fake panes. The invite becomes centred. Visible: the invite.
3. **The tour.** Dialogs 1 to 12, `NewSession` filled in, the brief's caps line, the SDK
   runner, the checklist removed. The first features registered: the tour itself and the
   modules work, as `UNRELEASED`.
   As built: the tour's progress is `tour.py` (no Textual), held by the app for its run
   (`tour_at` comes with resume in stage 4); Esc pauses it with a notice. Dialog 2's line says
   Launch, New session's button. A dialog's tips count as seen as it shows, and the notes
   dialog records the rest and `notes_seen`. A demo that stops says why in dialog 3.
4. **Top-ups.** The pending rule, the upgrade invite, the badge and `L`, pause and resume.
   As built: `tour.opening` decides what a start offers, in order: a tour kept as under way
   (`tour_at`, written at every dialog, so quitting mid-tour is a pause too) shows only the
   badge, "Tour paused · L"; the whole tour's invite; a top-up's invite while a dialog whose
   pane is present has a new tip; the badge alone ("What's new · L", L opens the notes)
   while only advanced features are new. `tour_at` holds the kind as well as the dialog
   (`tour:answer`, `topup:stats`). Later on a top-up records nothing (the badge carries it,
   and the next start offers it again); Never skips the pending features. A whole tour
   resumed past the demo's creation with the demo gone says so, and Next goes to the notes.
   The badge is shown only from pending features, not from the version alone, so a
   fixes-only release offers nothing. `L` is the app's; in the session list it stays
   Relaunch, and a click on the badge does the same as `L`.
5. **Release workflow.** `claude-wheelhouse notes` feeding the GitHub release, when
   `release.yml` is built.

## Tests

Table-driven, one case a line, with the description in the assertion message:

- **Registry**: ids unique; every `where` is a dialog id or `ADVANCED`; every dialog
  feature has a tip of at most two lines; `when` is known; no `UNRELEASED` in a release
  version; no em-dashes in any text.
- **Pending rule**: seen rows and SDK mode in, pending ids out (none seen, all shown, some
  skipped, an `sdk` feature with SDK mode off, a retired id's row ignored).
- **Top-up selection**: pending features in, dialogs shown and their tips out; a dialog with
  none is skipped; advanced-only goes straight to the notes.
- **Migration**: each row of the table above, and running it twice changes nothing.
- **Notes**: a version's Markdown, grouped by dialog in tour order then Advanced.
- **Placement** (pure function: screen size and pane region in, callout region out): each
  side chosen in turn, inside-the-pane fallback, centred fallback for a collapsed pane, the
  80 by 24 screen.
- **TUI pilot** (`both` where it goes through the hub): the invite's three keys and what
  they write; a callout follows its pane after a swap and after a resize; an action callout
  doesn't take focus and Next does; each action dialog advances on its check; Esc pauses and
  L resumes at the same dialog; the demo is created `sdk` with `sdk_on` and `tab` without;
  the brief starts with the caps line.

## Settled questions

Doug's answers to the open questions, settled for the build:

1. **Creating the demo (dialog 2): A.** The person presses N and `NewSession` opens filled
   in, so the first thing they learn is how every session starts.
2. **The invite after an upgrade: C.** The centred invite shows only when a tour dialog has
   new tips; a release with only advanced features gets the badge alone.
3. **Which notes the last dialog shows: B.** Upgraders see every release since the last one
   they saw (`notes_seen`), newest first; new users see this release's.
4. **Esc during the tour: A.** Esc pauses; the badge, or L, resumes at the same dialog.
