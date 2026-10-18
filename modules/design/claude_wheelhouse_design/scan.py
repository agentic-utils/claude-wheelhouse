"""Where a repo's decisions are and what each says of itself (.plan/modules.md, "Design, a new
module"; Q114): every NNNN-*.md file in the directories api.doc_dirs gives (docs/adr under
each session's git root, unless a session there declared others with set_doc_dirs), with its
number, status, title and repo. A file is read again only when its mtime or size has changed,
so a scan every 10 s costs a directory listing and a stat a file. Never imports Textual."""

import logging
import os
import re
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from claude_wheelhouse import api

log = logging.getLogger(__name__)

NAME = re.compile(r"^(\d{4})-(.+)\.md$")    # a decision's file: NNNN-*.md
WORD = re.compile(r"^([A-Z]+)(?:-(.*))?$")   # its status in its name, the tfu convention: 0042-ACCEPTED-slug.md
FRONT_STATUS = re.compile(r"^status\s*:\s*(.*)$", re.I)
FRONT_TITLE = re.compile(r"^title\s*:\s*(.*)$", re.I)
STATUS_LINE = re.compile(r"^\**status\**\s*:\**\s*(.+)$", re.I)   # Status: Accepted, **Status:** Accepted
STATUS_HEADING = re.compile(r"^#{2,6}\s*status\s*$", re.I)        # an adr-tools ## Status section
HEADING = re.compile(r"^#\s+(.+)$")
NUMBERED = re.compile(r"^(?:ADR[-\s]?)?\d+\s*[.:)-]?\s+", re.I)    # "1. ", "ADR-0042: " before a title
HEAD = 8192          # bytes of a file read for its front matter, status and title
UNKNOWN = "?"
OPEN = ("PROPOSED", "UNRESOLVED")   # first in the default order


@dataclass(frozen=True)
class Decision:
    number: str      # as in its name, "0042"
    status: str      # upper case, its first word: "ACCEPTED", or UNKNOWN
    title: str
    repo: str        # its git root's name
    path: str        # absolute


def word(status: str) -> str:
    """A status as the pane shows it: its first word, upper case ("Superseded by 0007" is
    SUPERSEDED); UNKNOWN for none."""
    found = re.match(r"[\w-]+", status.strip().strip("*_`\"'"))
    return found[0].upper() if found else UNKNOWN


def front_matter(lines: list[str]) -> tuple[dict[str, str], int]:
    """A doc's YAML front matter as plain key: value lines (status and title only), and the
    line its body starts on. None unless the doc opens with ---."""
    if not lines or lines[0].strip() != "---":
        return {}, 0
    out = {}
    for i, line in enumerate(lines[1:], 1):
        if line.strip() in ("---", "..."):
            return out, i + 1
        for key, pattern in (("status", FRONT_STATUS), ("title", FRONT_TITLE)):
            if (m := pattern.match(line.strip())) and m[1].strip():
                out.setdefault(key, m[1].strip().strip("\"'"))
    return {}, 0   # never closed: not front matter


def parse(path: Path, text: str, repo: str) -> Decision | None:
    """What a decision's file says of itself, None if its name isn't NNNN-*.md. Its status:
    its front matter's, else a Status line or ## Status section in the doc, else its name's
    (0042-ACCEPTED-slug.md), else UNKNOWN. Its title: its front matter's, else its first #
    heading (a leading number such as "1. " or "ADR-0042: " left out), else its name's slug."""
    found = NAME.match(path.name)
    if found is None:
        return None
    number, rest = found[1], found[2]
    named = WORD.match(rest)
    slug = (named[2] or "") if named else rest
    lines = text.splitlines()
    front, start = front_matter(lines)
    status, title = front.get("status"), front.get("title")
    after_heading = False
    for line in lines[start:]:
        line = line.strip()
        if title is None and (m := HEADING.match(line)):
            title = NUMBERED.sub("", m[1]).strip() or None
            continue
        if status is None:
            if after_heading and line:
                status = line
            elif m := STATUS_LINE.match(line):
                status = m[1]
            after_heading = after_heading or bool(STATUS_HEADING.match(line))
        if title is not None and status is not None:
            break
    if status is None and named:
        status = named[1]
    title = title or slug.replace("-", " ").replace("_", " ").strip() or path.stem
    return Decision(number, word(status or ""), title, repo, str(path))


class Scanner:
    """The decisions in some directories, each file read again only when its stat has changed
    (Scanner.files), safe to call from several threads at once (the daemon runs a module's
    GETs on up to four)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.files: dict[str, tuple[tuple[int, int], Decision | None]] = {}   # path: (mtime_ns, size), parsed

    def scan(self, dirs: list[tuple[Path, str]]) -> list[Decision]:
        """Every decision in dirs, each (directory, its repo's name); a directory that has
        gone, a file that can't be read, or a link to one outside its directory
        (api.confine), adds none. Not recursive: a decision is a file in the directory itself."""
        out, seen = [], set()
        for directory, repo in dirs:
            try:
                entries = list(os.scandir(directory))
            except OSError:
                continue
            for entry in entries:
                if not NAME.match(entry.name):
                    continue
                try:
                    if not entry.is_file():
                        continue
                    st = entry.stat()
                except OSError:
                    continue
                stamp = (st.st_mtime_ns, st.st_size)
                seen.add(entry.path)
                with self.lock:
                    kept = self.files.get(entry.path)
                if kept is None or kept[0] != stamp:
                    try:   # opened as resolved, never following a link swapped in since
                        real = api.confine(entry.path, [directory])
                        fd = os.open(real, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
                        with open(fd, "rb") as f:
                            text = f.read(HEAD).decode("utf-8", errors="replace")
                    except (OSError, api.Outside) as e:
                        log.debug("decision %s skipped: %s", entry.path, e)
                        with self.lock:
                            self.files.pop(entry.path, None)
                        continue
                    kept = (stamp, parse(Path(entry.path), text, repo))
                    with self.lock:
                        self.files[entry.path] = kept
                if kept[1] is not None:
                    out.append(kept[1])
        with self.lock:   # files gone since: forgotten
            for path in [p for p in self.files if p not in seen and any(p.startswith(str(d) + os.sep) for d, _ in dirs)]:
                del self.files[path]
        return ordered(out)


def ordered(decisions: list[Decision]) -> list[Decision]:
    """The default order: PROPOSED and UNRESOLVED first, then the rest, each by number,
    newest first, then by repo."""
    by_repo = sorted(decisions, key=lambda d: d.repo.casefold())
    return sorted(by_repo, key=lambda d: (d.status not in OPEN, -int(d.number)))


def as_json(decisions: list[Decision]) -> list[dict]:
    return [asdict(d) for d in decisions]
