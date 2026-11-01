"""Finding an inbox item: which items a query matches, best first (#273, the search popover),
and the ids in a text that name a session's items (#274, linked). Never imports Textual.

A query shaped like a ref (`T3`, `q25`, `PR276`) puts exact ref matches first. Otherwise, and
after those, a case-insensitive substring match on ref and title, best first: the ref, then
a title starting with the query, then one containing it, each group newest first."""

import functools
import re

from markdown_it import MarkdownIt

from .store import KINDS

LIMIT = 50   # rows the popover lists; a line says how many more matched
REF_SHAPED = re.compile(rf"(?:{'|'.join(sorted(KINDS.values(), key=len, reverse=True))})\d+", re.IGNORECASE)


def ref_shaped(query: str) -> bool:
    """Whether a query reads as an item's ref: a kind's letter (or PR) and digits."""
    return REF_SHAPED.fullmatch(query.strip()) is not None


def rank(item, query: str) -> int | None:
    """Where an item stands for a query (lower first), None if it doesn't match: 0 its ref
    exactly (a ref-shaped query), 1 its ref holding the query, 2 its title starting with it,
    3 its title holding it. An empty query matches everything, as one group."""
    q = query.strip().casefold()
    ref, title = item["ref"].casefold(), item["title"].casefold()
    if not q:
        return 3
    if ref == q and ref_shaped(q):
        return 0
    if q in ref:
        return 1
    if title.startswith(q):
        return 2
    if q in title:
        return 3
    return None


def find(items, query: str, limit: int = LIMIT) -> tuple[list, int]:
    """The items matching a query, best first (rank), newest first within each group, at
    most limit of them; and how many more matched."""
    ranked = [(r, it) for it in items if (r := rank(it, query)) is not None]
    ranked.sort(key=lambda pair: pair[1]["created_at"], reverse=True)   # stable: newest first in each group
    ranked.sort(key=lambda pair: pair[0])
    return [it for _, it in ranked[:limit]], max(0, len(ranked) - limit)


# Clickable ids (#274): an item's id in a doc, a conversation or a thread, linking to that
# session's item with the ref. A whole word: not after # (a GitHub issue), nor inside a longer
# word or a path (T2a, T206.md, docs/T3); inline code and code blocks are left alone. The link
# goes through the markdown as a link to LINK, which the renderer turns into a click (tui).
LINK = "wheelhouse-item:"
ITEM_ID = re.compile(r"(?<![\w#/\\.\-])(?:PR|[TQDMAC])\d+(?![\w/\\\-]|\.\w)")
# what's never linked into, in a line: inline code, a markdown link or image, an autolink
KEEP = re.compile(r"(`+).*?\1|!?\[[^\]]*\]\([^)]*\)|<[^>\s]+>")
PARSER = MarkdownIt().enable("strikethrough").enable("table")   # as Rich's Markdown parses


def code_lines(md: str) -> set[int]:
    """The lines of md in a code block, fenced or indented, wherever it nests (a list, a quote)."""
    return {n for tok in PARSER.parse(md) if tok.type in ("fence", "code_block") and tok.map
            for n in range(*tok.map)}


@functools.lru_cache(maxsize=4096)   # a block each: a long conversation's every turn stays held
def linked(md: str, sid: str, refs: frozenset) -> str:
    """Markdown with each id the session has (refs) a link to its item, outside code: made once
    for a text, however often it's painted."""
    if not refs or not ITEM_ID.search(md):
        return md
    code, out = code_lines(md), []
    for n, line in enumerate(md.split("\n")):
        if n in code:
            out.append(line)
            continue
        parts, at = [], 0
        for kept in KEEP.finditer(line):
            parts.append(link_ids(line[at:kept.start()], sid, refs))
            parts.append(kept.group(0))
            at = kept.end()
        parts.append(link_ids(line[at:], sid, refs))
        out.append("".join(parts))
    return "\n".join(out)


def link_ids(text: str, sid: str, refs) -> str:
    return ITEM_ID.sub(lambda m: f"[{m.group(0)}]({LINK}{sid}/{m.group(0)})" if m.group(0) in refs else m.group(0), text)


def target(link: str | None) -> tuple[str, str] | None:
    """The (session, ref) a rendered link goes to, if it's an item's (linked); else None."""
    if not link or not link.startswith(LINK):
        return None
    sid, _, ref = link[len(LINK):].rpartition("/")
    return (sid, ref) if sid and ref else None
