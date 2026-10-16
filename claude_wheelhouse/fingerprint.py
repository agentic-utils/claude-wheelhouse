"""Which code a process runs: a hash of its package's source files as it found them when
first asked, so a daemon started before a pull says it runs older code than a TUI started
after (.plan/remote.md, phase 2c)."""

import functools
import hashlib
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
API = 2   # the daemon's routes as this code serves and calls them: another number won't do


@functools.lru_cache(maxsize=1)
def code() -> str:
    """This process's code: asked once, early, so it's the code as loaded, not as it is on
    disk by the time something asks again."""
    digest = hashlib.sha256()
    for file in sorted(PACKAGE.glob("*.py")):
        digest.update(file.name.encode() + b"\0" + file.read_bytes() + b"\0")
    return digest.hexdigest()[:16]
