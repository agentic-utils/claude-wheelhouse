"""The module protocol: what a module gives the wheelhouse and what the wheelhouse gives
it back. A module is any package that declares an entry point in the group
`claude_wheelhouse.modules` pointing at a `Module`; the hub finds it at startup, with
nothing to register by hand. The hub's own stats pane is one, declared in this package's
pyproject.toml the same way. See .plan/suite-architecture.md ("Module protocol") and
.plan/stats-plugin.md.

A pane's `tui` surface is a factory taking the `Context` and returning a Textual widget.
The hub mounts it in the pane's slot, with the module's id as its id (an id one of the
hub's own widgets or another module has gets a card instead), and calls two optional
methods on it:

- `tick()`: once a second, and as soon as the session in context changes.
- `animate(frame)`: five times a second, except while the person types in an answer box,
  when animation rests.
"""

import importlib.metadata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

WHEELHOUSE_API = 1                 # bumped on any breaking change, matched exactly
GROUP = "claude_wheelhouse.modules"
OWN = ("stats",)                   # declared in this package's pyproject.toml
# an installed copy older than the entry points in pyproject.toml has no record of them
STALE = "not found: the installed package is out of date. Reinstall it with make install"
SLOTS = ("inbox.side",)            # under the inbox's item list; tabs come with the first tab pane


@dataclass
class Pane:
    title: str
    slot: str                                          # one of SLOTS
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
    """All a module gets from the hub. Read-only: a module never writes the hub's data."""
    state_dir: Path
    # the hub's sessions: id, name, whether it's running, and "context", a HostContext for an
    # SDK-hosted session whose host has recorded one, else None
    sessions: Callable[[], list[dict]]
    focus: Callable[[], str | None]      # the session in context (highlighted or followed), if any
    config: dict = field(default_factory=dict)


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
    """Every module installed, in name order. One that fails to import, isn't a Module, or
    was written against another API is reported with the reason, never raised; so is one
    of the hub's own that wasn't found, which means the package needs reinstalling."""
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
        else:
            out.append(Loaded(ep.name, module))
    return out


def panes(loaded: list[Loaded], slot: str) -> list[tuple[Loaded, Pane | None]]:
    """What goes in a slot: each running module's panes for it, and each module that
    couldn't start, so its error shows there instead (the stats pane's slot shows a broken
    stats module's reason, not nothing)."""
    out = []
    for item in loaded:
        if item.error:
            if item.module is None or any(p.slot == slot for p in item.module.panes):
                out.append((item, None))
        else:
            out += [(item, p) for p in item.module.panes if p.slot == slot]
    return out
