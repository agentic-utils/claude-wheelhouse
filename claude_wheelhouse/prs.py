"""Pull requests a session makes, found in its transcripts as PR items (#111, .plan/pr-pane.md).

- Made: a Bash tool call running `gh pr create`, or a tool named `*create_pull_request*`
  (the GitHub MCP server). Its tool_result, matched by the call's id, gives the PR's URL: the
  first GitHub pull URL in it (gh prints it last, and its "already exists: <url>" counts, as
  the session made it). A result with no URL (an error, `--web`) makes nothing.
- The title is the call's own (`--title`, or the tool's `title`), else `PR <n>` until the PRs
  pane's first refresh brings GitHub's.
- A subagent's transcript is read the same way, and its PR filed under the item the
  subagent was made for.
- Activity: a Bash `git push`, `gh pr merge`, `gh pr ready` or `gh pr close`, whose time the
  PRs pane takes as a cue to fetch again (Hub.pr_activity_at).

Reads are incremental, each transcript from where the last read stopped, after a byte
filter. A record older than the cut-off counts for nothing, a subagent's too (whose
transcript is read from its start). A main transcript's first read starts at the cut-off (store.PRS_SINCE, or when the
session joined, whichever is later), so a PR made while the wheelhouse was down still arrives.
Never imports Textual.
"""

import json
import re
import shlex
from datetime import datetime
from pathlib import Path

from rich.style import Style
from rich.text import Text

# a pull request's URL, as GitHub gives it: owner, repo, number
PULL = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)")
STATES = ("draft", "open", "merged", "closed")   # a PR item's statuses: GitHub's, never the session's
CREATES = re.compile(rb"pr create|create_pull_request")
ACTS = re.compile(r"\b(?:git\s+push|gh\s+pr\s+(?:merge|ready|close))\b")
ACTS_BYTES = re.compile(rb"git push|gh pr (?:merge|ready|close)")
RESULT_ID = re.compile(rb'"tool_use_id":\s*"([^"]+)"')


def parse(text: str | None) -> tuple[str, str, int] | None:
    """The first pull request URL in text: (owner, repo, number); None if there's none."""
    m = PULL.search(text or "")
    return (m[1], m[2], int(m[3])) if m else None


def link(url: str) -> Text:
    """A PR's URL as the panes show it (#139): without https://github.com/, and a click on any
    of it opens the whole URL, on whichever line it wraps to: every segment carries it (the
    app's open_url action), so none opens a fragment, as the terminal's own detection did."""
    return Text(url.removeprefix("https://github.com/"), style=Style.from_meta({"@click": f"app.open_url({url!r})"}))


def canonical(text: str | None) -> str | None:
    """The first pull request URL in text, as `https://github.com/<owner>/<repo>/pull/<n>`
    (no trailing path, query or fragment); None if there's none."""
    found = parse(text)
    return f"https://github.com/{found[0]}/{found[1]}/pull/{found[2]}" if found else None


def repo_of(url: str | None) -> str:
    """A PR item's repo, its short name (the owner left off, the pane shows it); '' if none."""
    found = parse(url)
    return found[1] if found else ""


def title_in(command: str) -> str:
    """`gh pr create`'s --title (or -t), if the command parses and has one; else ''."""
    try:
        words = shlex.split(command)
    except ValueError:
        return ""
    for i, w in enumerate(words):
        if w in ("--title", "-t") and i + 1 < len(words):
            return words[i + 1]
        if w.startswith("--title="):
            return w.split("=", 1)[1]
    return ""


def result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, list):
        content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return str(content or "")


def stamp_of(rec: dict) -> float | None:
    from . import transcript_usage   # here: it imports store, which imports this
    at = rec.get("timestamp")
    return transcript_usage.epoch(at) if isinstance(at, str) else None


class PrWatcher:
    """One session's PRs, followed from its transcripts. sync() runs off the UI thread, beside
    the subagent watcher's (Hub.sync_agents), which says where the transcripts are."""

    def __init__(self, sid: str):
        self.sid = sid
        self.offsets: dict[Path, int] = {}   # each transcript read, to here
        self.stamps: dict[Path, tuple] = {}  # its (size, mtime) then
        self.pending: dict[str, tuple[str, str | None]] = {}   # a creating call's id: (title, parent)
        self.activity: float | None = None   # the latest push or merge seen, epoch seconds

    def sync(self, store, main: Path | None, logs: dict[Path, str | None], cutoff: datetime) -> list[str]:
        """Reads what's new in the main transcript and each subagent's (`logs`: its path and
        the item it was made for, the PR's parent), and posts each PR found (store.post_pr,
        detected). Returns the refs posted."""
        made: list[tuple[str, str, str | None]] = []
        for path, parent in [(main, None), *logs.items()]:
            if path is not None:
                made += self.read(path, parent, cutoff.timestamp(), main=path == main)
        refs = []
        for url, title, parent in made:
            ref = store.post_pr(self.sid, url, title, parent=parent, detected=True)
            if ref:
                refs.append(ref)
        return refs

    def read(self, path: Path, parent: str | None, since: float, main: bool = True) -> list[tuple[str, str, str | None]]:
        """One transcript's new lines: the PRs made in them, as (url, title, parent). Records
        before `since` count for nothing; a main transcript's first read starts there."""
        try:
            st = path.stat()
        except OSError:
            return []
        if (st.st_size, st.st_mtime_ns) == self.stamps.get(path):
            return []
        from . import transcript_usage   # here: it imports store, which imports this
        with open(path, "rb") as f:
            offset = self.offsets.get(path)
            if offset is None or st.st_size < offset:
                offset = transcript_usage.window_start(f, st.st_size, since) if main else 0
            f.seek(offset)
            data = f.read(st.st_size - offset)
        end = data.rfind(b"\n") + 1   # a half-written last line waits for the next read
        self.offsets[path], self.stamps[path] = offset + end, (st.st_size, st.st_mtime_ns)
        found = []
        for line in data[:end].splitlines():
            try:
                found += self.take(line, parent, since)
            except (ValueError, AttributeError, TypeError):
                continue
        return found

    def take(self, line: bytes, parent: str | None, since: float | None = None) -> list[tuple[str, str, str | None]]:
        """One record, parsed only when its bytes say it may matter; nothing from one dated
        before `since` (store.PRS_SINCE, or when the session joined)."""
        creates, acts = CREATES.search(line), ACTS_BYTES.search(line)
        answers = self.pending and b'"tool_result"' in line and any(
            i.decode() in self.pending for i in RESULT_ID.findall(line))
        if not (creates or acts or answers):
            return []
        rec = json.loads(line)
        if since is not None and (at := stamp_of(rec)) is not None and at < since:
            return []
        blocks = (rec.get("message") or {}).get("content") or []
        found = []
        for block in blocks if isinstance(blocks, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                name, given = block.get("name") or "", block.get("input") or {}
                command = given.get("command") or "" if name == "Bash" else ""
                if name == "Bash" and "gh pr create" in command:
                    self.pending[block.get("id")] = (title_in(command), parent)
                elif "create_pull_request" in name:
                    self.pending[block.get("id")] = (str(given.get("title") or ""), parent)
                if name == "Bash" and ACTS.search(command):
                    at = stamp_of(rec)
                    if at is not None:
                        self.activity = max(self.activity or 0, at)
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in self.pending:
                title, under = self.pending.pop(block["tool_use_id"])
                url = canonical(result_text(block))
                if url:
                    found.append((url, title or f"PR {parse(url)[2]}", under))
        return found
