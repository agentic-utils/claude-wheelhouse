"""The module protocol: what a module gives the wheelhouse and what the wheelhouse gives
it back. A module is any package that declares an entry point in the group
`claude_wheelhouse.modules` pointing at a `Module`; the hub finds it at startup, with
nothing to register by hand. The stats pane is one, its own distribution in this repo
(modules/stats, claude-wheelhouse-stats), which `make install` installs alongside. See
.plan/suite-architecture.md ("Module protocol"), .plan/stats-plugin.md and .plan/modules.md.

A module imports this alone: what it may use of the wheelhouse is re-exported here (below).

A pane's `tui` surface is a factory taking the `Context` and returning a Textual widget.
The hub mounts it in the pane's slot, one of `SLOTS` (.plan/modules.md), with the module's id as its id (an id one of the
hub's own widgets or another module has gets a card instead), and calls three optional
methods on it:

- `tick()`: once a second, and as soon as the session in context changes.
- `animate(frame)`: five times a second, except while the person types in an answer box,
  when animation rests.
- `showing(sid, ref)`: the item Detail shows has changed, picked in the inbox or another pane
  (ref None for a session's conversation, both None for nothing): a pane listing it can
  highlight it, and one that doesn't can hide its highlight (#148).
- `detail_parts()`: once, after the panes mount: widgets of the pane's own for the Detail pane,
  {"body": one scrolling under the thread shown, "bar": one fixed under that}, either left
  out. The pane shows and hides them (on `showing`, say); Detail hides both while it shows
  a doc or a session's conversation. The PRs pane's are a PR's buttons and details (#145).
- `landed(sid)`: the hub's own read of a session's transcript has brought something (its
  transcript followers are shared, `follower`): a pane showing that session can repaint.

A pane's widget may also name a section of the F1 overlay for its BINDINGS: `KEYS_HELP =
(title, sdk_only)`, sdk_only to list them only with SDK mode on. A list in a pane (the
Decisions pane's table) that sets `TAB_LIST = True` is a Tab stop after the inbox, before the
answer box (tui.tab_stops, #77). A pane's widget may say the least room it needs, `PANE_MINIMUM = (columns,
rows)`, which its slot and column keep wherever it is swapped, and while it has nothing to
show set `compact = True` and call `Context.relayout`: in a column of two slots or more its
slot shrinks to those rows, the others taking the rest (columns.py); alone in its column, it
keeps its size. A right-click in a pane asks its widget, or the nearest of its ancestors
that has one, `menu_at(x, y)` (screen cells): a `Menu` opens there, None leaves the
right-click to copy and paste as anywhere else.

A module may also serve the daemon side (.plan/modules.md, "The module boundary"):
`routes(ctx)`, its `Route`s, mounted under /m/<id>/ (and called in process with --local),
which its pane reaches with `Context.call`; and `doc_roots(ctx)`, the directories whose .md
files the core viewer may read (and later write), which `Context.show_doc` shows. Both are
asked of a `Context` with no focus. `claude-wheelhouse modules disable <id>` switches a
module off (DISABLED): no pane, no routes and no doc roots, from the next start.
"""

import importlib.metadata
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .docroots import doc_dirs, git_root  # noqa: F401  (where each repo's decisions are found, Q114)
from .docroots import Outside, confine  # noqa: F401  (a path resolved, if a doc root holds it)

# what a module may use of the wheelhouse: a session's token use (transcript_usage.py), read
# by the hub's shared followers, the account's usage, and the effort levels
from .models import EFFORTS, name as model_label  # noqa: F401
# a pull request's URL read (prs.py), and how the lists colour a kind and show a row's full text
from .prs import STATES as PR_STATES, canonical as pr_url, link as pr_link, parse as parse_pr, repo_of  # noqa: F401
from .rows import KIND_COLOUR, RowTips, marked, plain  # noqa: F401
from .plan_usage import EVERY as USAGE_EVERY, AccountUsage  # noqa: F401
from .transcript_usage import (AMBER, BUCKET, CHARTS, DAY, HOT, HOUR, OK, PARTIAL, SERIES,  # noqa: F401
                               SPAN, WARN, WEEK, History, Snapshot, Turn, UsageFollower, clock,
                               current, follower, grade, hexc, load_safely, read_safely, tokens,
                               window_for)

WHEELHOUSE_API = 2                 # bumped on any breaking change, matched exactly
GROUP = "claude_wheelhouse.modules"
# an installed copy older than pyproject.toml still declares an entry point this package no
# longer has (stats, before it was its own distribution): its module fails to import
STALE = "not found: the installed package is out of date. Reinstall it with make install"
OWN = "claude_wheelhouse."         # where this package's own entry points pointed
# the seven places a pane goes, by column (the middle's third, middle.middle, from #111); an empty one
# collapses (.plan/modules.md)
SLOTS = ("left.top", "left.middle", "left.bottom", "middle.top", "middle.middle", "middle.bottom", "right")
CARDS = "left.middle"              # where a module that didn't load says why: it has no slot to say
DISABLED = "modules.disabled"      # settings: the modules switched off, a JSON list of their names


class Refused(Exception):
    """A module's route refusing a call, with the status it answers (404, 400, ...); and what
    Context.call raises for any call that fails, the route's own refusal or not (503: the
    daemon couldn't be reached)."""

    def __init__(self, status: int, error: str):
        super().__init__(error)
        self.status, self.error = status, error


@dataclass
class Call:
    """A call to a module's route: the path's {params}, the query string's, and a POST's body."""
    params: dict[str, str]
    query: dict[str, str]
    body: dict | None = None


@dataclass
class Route:
    """One of a module's routes, at /m/<id>/<path>. A segment in braces ("decisions/{n}") is a
    param. A GET is a query, run on a worker thread; a POST a command, done once per
    Idempotency-Key, on the module's own thread (one at a time, never the hub's), and refused
    503 while one has run past its wait; at most 4 GETs run at once (daemon.MODULE_GETS), one
    more refused 503. Either is waited on 10 s (daemon.MODULE_WAIT), then answered 504 while
    it carries on: keep them quick. handler(Call) answers JSON, or raises Refused for a 4xx;
    anything else it raises is a 500, through the daemon or in process alike."""
    path: str
    handler: Callable[[Call], object]
    method: str = "GET"


@dataclass
class Pane:
    title: str
    slot: str                                          # its default slot, one of SLOTS
    surfaces: dict[str, Callable[["Context"], object]]   # {"tui": make_widget}


@dataclass
class Module:
    id: str                        # its widget's id, and later its tab, CLI word and database
    title: str
    version: str
    api: int                       # the WHEELHOUSE_API it was written against
    panes: list[Pane] = field(default_factory=list)
    routes: Callable[["Context"], list[Route]] | None = None     # served under /m/<id>/
    doc_roots: Callable[["Context"], list[Path]] | None = None   # directories of docs it shows


@dataclass
class HostContext:
    """The context size a session's SDK host last had from Claude Code (`get_context_usage`,
    as /context reports it), its window, and when, in epoch seconds."""
    tokens: int
    window: int
    at: float


@dataclass
class Context:
    """All a module gets from the hub. Read-only: a module never writes the hub's data but
    through the commands given here (set_effort)."""
    state_dir: Path
    # the hub's sessions: id, name, whether it's running, "context", a HostContext for an
    # SDK-hosted session whose host has recorded one, else None, and what it is set to run
    # on (Hub.model_state): "model" and "effort" (None where unknown), "pending" (the host
    # has yet to apply them: "applying" or "on relaunch") and "effort_live" (its effort can
    # be changed live: run by a host, and SDK mode on)
    sessions: Callable[[], list[dict]]
    focus: Callable[[], str | None]      # the session in context (highlighted or followed), if any
    config: dict = field(default_factory=dict)
    # the account's session and weekly usage, which the hub reads (plan_usage)
    usage: "AccountUsage | None" = None
    # a hosted session's effort, set live: (session id, level); None where the surface has none
    set_effort: Callable[[str, str], None] | None = None
    # the module's own routes, through the client, local or remote: (path under /m/<id>/,
    # with any query string; body, None for a GET) -> their JSON, or Refused. None daemon side
    call: Callable[[str, dict | None], object] | None = None
    # a doc under a doc root shown in the Detail pane's viewer: (absolute path, title). None daemon side
    show_doc: Callable[[str, str], None] | None = None
    # the same, opened full screen (DocView), as Enter opens an inbox doc. None daemon side
    open_doc: Callable[[str, str], None] | None = None
    # a pane's `compact` changed: its slot shrinks to its least rows, or takes its share again. None daemon side
    relayout: Callable[[], None] | None = None
    # pull requests (#111): every PR item in the store, any session's, as
    # {"url", "session_id", "ref", "status", "title"}. None TUI side
    pr_items: Callable[[], list[dict]] | None = None
    # GitHub's state for PRs, {url: one of PR_STATES}, written to their items. None TUI side
    set_pr_states: Callable[[dict[str, str]], None] | None = None
    # when a session last pushed, merged, readied or closed a PR (epoch seconds), else None. None TUI side
    pr_activity: Callable[[], float | None] | None = None
    # an item shown in the Detail pane, highlighted in the inbox: (session id, ref). False if
    # it isn't, its session compacting (#165). None daemon side
    show_item: Callable[[str, str], bool | None] | None = None
    # nothing selected in the inbox, its highlight hidden, and Detail saying why: (note). For a
    # pick that isn't an item (a PR no session made, #148). None daemon side
    unselect: Callable[[str], None] | None = None
    # text sent to a session as the person's answer would be, queued or at once by its mode:
    # (session id, the item it answers or None, text). None daemon side
    send: Callable[[str, str | None, str], None] | None = None
    # the wheelhouse's settings, which a module keys under its own id ("<id>.<name>"). None daemon side
    setting: Callable[[str], str | None] | None = None
    set_setting: Callable[[str, str | None], None] | None = None
    # the app's Yes/No dialog, No focused: (question, called with the answer). None daemon side
    confirm: Callable[[str, Callable[[bool], None]], None] | None = None
    # a line of text asked for: (question, suggestions, called with it, or None if cancelled). None daemon side
    ask: Callable[[str, list[str], Callable[[str | None], None]], None] | None = None


@dataclass
class Menu:
    """What a pane's `menu_at(x, y)` gives for a right-click on one of its rows: the menu's
    title, its entries (id, caption, disabled, tooltip), the screen line of the row it opens
    under (above it where there's no room below), and what's called with the entry chosen."""
    title: str
    entries: list[tuple[str, str, bool, str]]
    line: int
    chosen: Callable[[str], None]


@dataclass
class Loaded:
    """A module found at startup, and why it isn't running if it isn't."""
    name: str
    module: Module | None
    error: str | None = None
    disabled: bool = False   # switched off (DISABLED): nothing of it runs


def disabled(raw: str | None) -> set[str]:
    """The modules switched off, from the DISABLED setting as kept; none for none or junk."""
    try:
        names = json.loads(raw) if raw else []
    except ValueError:
        return set()
    return {n for n in names if isinstance(n, str)} if isinstance(names, list) else set()


def switch_off(loaded: list[Loaded], names: set[str]) -> list[Loaded]:
    """loaded, each one named in names (by entry point or module id) marked disabled."""
    return [Loaded(i.name, i.module, i.error, True)
            if i.name in names or (i.module is not None and i.module.id in names) else i for i in loaded]


def load(entries=None) -> list[Loaded]:
    """Every module installed, in name order. One that fails to import, isn't a Module,
    was written against another API or has a pane for a slot that isn't one of SLOTS is
    reported with the reason, never raised; so is a stale entry point of this package's own
    (OWN), which means the package needs reinstalling. None installed is no error: their
    slots collapse."""
    found = list(importlib.metadata.entry_points(group=GROUP) if entries is None else entries)
    out = []
    for ep in sorted(found, key=lambda ep: ep.name):
        try:
            module = ep.load()
        except Exception as e:
            out.append(Loaded(ep.name, None, STALE if ep.value.startswith(OWN) else f"failed to load: {e}"))
            continue
        if not isinstance(module, Module):
            out.append(Loaded(ep.name, None, f"{ep.value} is not a Module"))
        elif module.api != WHEELHOUSE_API:
            out.append(Loaded(ep.name, module, f"written for API {module.api}; this wheelhouse has {WHEELHOUSE_API}"))
        elif bad := [p for p in module.panes if p.slot not in SLOTS]:
            out.append(Loaded(ep.name, module, f"its pane {bad[0].title!r} names slot {bad[0].slot!r}, not one of {', '.join(SLOTS)}"))
        else:
            out.append(Loaded(ep.name, module))
    return out


def panes(loaded: list[Loaded], slot: str) -> list[tuple[Loaded, Pane | None]]:
    """What goes in a slot: each running module's panes for it, and each module that
    couldn't start, so its error shows there instead (the stats pane's slot shows a broken
    stats module's reason, not nothing). One that didn't load has no panes to say where,
    and one naming a slot that isn't one of SLOTS has nowhere to show it: CARDS for both. One
switched off (disabled) has nothing anywhere."""
    out = []
    for item in loaded:
        if item.disabled:
            continue
        if item.error:
            if slot in ({home(p) for p in item.module.panes} if item.module else {CARDS}):
                out.append((item, None))
        else:
            out += [(item, p) for p in item.module.panes if p.slot == slot]
    return out


def home(pane: Pane) -> str:
    """Where a pane's card goes: its slot, or CARDS for one that names no slot of SLOTS
    (an API 1 module's inbox.side, say), so the card shows somewhere."""
    return pane.slot if pane.slot in SLOTS else CARDS
