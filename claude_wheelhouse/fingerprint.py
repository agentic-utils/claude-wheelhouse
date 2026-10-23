"""Which code a process runs: a hash of its package's source files as it found them when
first asked, so a daemon started before a pull says it runs older code than a TUI started
after (.plan/remote.md, phase 2c)."""

import functools
import hashlib
import importlib.metadata
import importlib.util
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
GROUP = "claude_wheelhouse.modules"   # api.GROUP, not imported: api imports half the package
API = 8   # the daemon's routes as this code serves and calls them: another number won't do


@functools.lru_cache(maxsize=1)
def code() -> str:
    """This process's code: asked once, early, so it's the code as loaded, not as it is on
    disk by the time something asks again."""
    digest = hashlib.sha256()
    for file in sorted(PACKAGE.glob("*.py")):
        digest.update(file.name.encode() + b"\0" + file.read_bytes() + b"\0")
    digest.update(modules())
    return digest.hexdigest()[:16]


def modules() -> bytes:
    """The modules installed, as the daemon serves their routes: each entry point's name, its
    distribution's version and its package's source, found without importing it. So a module
    installed, removed or changed is other code, and the app restarts a daemon serving the old."""
    out = b""
    for ep in sorted(importlib.metadata.entry_points(group=GROUP), key=lambda ep: ep.name):
        version = ep.dist.version if ep.dist else "?"
        out += f"{ep.name}\0{ep.value}\0{version}\0".encode()
        try:
            spec = importlib.util.find_spec(ep.module.partition(".")[0])
        except (ImportError, ValueError):
            spec = None
        if spec is None:
            continue
        if spec.submodule_search_locations:   # a package: every source file in it
            files = [f for place in spec.submodule_search_locations for f in sorted(Path(place).rglob("*.py"))]
        else:   # a module of one file
            files = [Path(spec.origin)] if spec.origin and spec.origin.endswith(".py") else []
        for file in files:
            out += str(file).encode() + b"\0" + file.read_bytes() + b"\0"
    return out
