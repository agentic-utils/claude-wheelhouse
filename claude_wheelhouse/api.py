"""The module protocol: what a module gives the wheelhouse and what the wheelhouse gives
it back. A module is any package that declares an entry point in the group
`claude_wheelhouse.modules` pointing at a `Module`; the hub finds it at startup, with
nothing to register by hand. The hub's own stats pane is one, declared in this package's
pyproject.toml the same way. See .plan/suite-architecture.md ("Module protocol") and
.plan/stats-plugin.md.

A pane's `tui` surface is a factory taking the `Context` and returning a Textual widget.
The hub mounts it in the pane's slot, one of `SLOTS` (.plan/modules.md), with the module's id as its id (an id one of the
hub's own widgets or another module has gets a card instead), and calls three optional
methods on it:

- `tick()`: once a second, and as soon as the session in context changes.
- `animate(frame)`: five times a second, except while the person types in an answer box,
  when animation rests.
- `landed(sid)`: the hub's own read of a session's transcript has brought something (its
  stats followers are shared, `stats.follower`): a pane showing that session can repaint.
"""

import importlib.metadata
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .plan_usage import AccountUsage

WHEELHOUSE_API = 2                 # bumped on any breaking change, matched exactly
GROUP = "claude_wheelhouse.modules"
OWN = ("stats",)                   # declared in this package's pyproject.toml
# an installed copy older than the entry points in pyproject.toml has no record of them
STALE = "not found: the installed package is out of date. Reinstall it with make install"
# the six places a pane goes, by column; an empty one collapses (.plan/modules.md)
SLOTS = ("left.top", "left.middle", "left.bottom", "middle.top", "middle.bottom", "right")
CARDS = "left.middle"              # where a module that didn't load says why: it has no slot to say


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


@dataclass
class Loaded:
    """A module found at startup, and why it isn't running if it isn't."""
    name: str
    module: Module | None
    error: str | None = None


@dataclass
class Missing:
    """One of the hub's own modules with no entry point installed."""
    name: str


def load(entries=None) -> list[Loaded]:
    """Every module installed, in name order. One that fails to import, isn't a Module,
    was written against another API or has a pane for a slot that isn't one of SLOTS is
    reported with the reason, never raised; so is one of the hub's own that wasn't found,
    which means the package needs reinstalling."""
    found = list(importlib.metadata.entry_points(group=GROUP) if entries is None else entries)
    if entries is None:
        names = {ep.name for ep in found}
        found += [Missing(name) for name in OWN if name not in names]
    out = []
    for ep in sorted(found, key=lambda ep: ep.name):
        if isinstance(ep, Missing):
            out.append(Loaded(ep.name, None, STALE))
            continue
        try:
            module = ep.load()
        except Exception as e:
            out.append(Loaded(ep.name, None, f"failed to load: {e}"))
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
    and one naming a slot that isn't one of SLOTS has nowhere to show it: CARDS for both."""
    out = []
    for item in loaded:
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
