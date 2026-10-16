"""Emoji shortcodes in the answer boxes: `:grin:` becomes 😁, and a code being typed gets
suggestions. Rich's own table (about 3,600 codes, GitHub and Unicode names), no new
dependency."""

import re

from rich._emoji_codes import EMOJI

CODE = re.compile(r":([a-z0-9_+\-]+):")
# a code being typed: a colon at the start or after a space, then at least two characters
PARTIAL = re.compile(r"(?:^|(?<=\s)):([a-z0-9_+\-]{2,})$")


def convert(text: str) -> str:
    """Every complete, known :code: as its emoji; anything else left as typed."""
    return CODE.sub(lambda m: EMOJI.get(m.group(1), m.group(0)), text)


def partial(before_cursor: str) -> str | None:
    """The code being typed just before the cursor, without its colon, if any."""
    m = PARTIAL.search(before_cursor)
    return m.group(1) if m else None


def suggest(prefix: str, n: int = 5) -> list[tuple[str, str]]:
    """Codes starting with prefix, shortest first, then codes containing it."""
    starts = sorted((c for c in EMOJI if c.startswith(prefix)), key=lambda c: (len(c), c))
    within = sorted((c for c in EMOJI if prefix in c and not c.startswith(prefix)), key=lambda c: (len(c), c))
    return [(c, EMOJI[c]) for c in (starts + within)[:n]]
