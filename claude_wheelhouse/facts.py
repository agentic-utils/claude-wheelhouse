"""The helper's fact sheet (helper_facts.md, .plan/helper-chat.md): what the helper chat
(helper.py) is told before any question, kept in step with the code it describes.

It is a committed file in two parts: a guide written by hand (or by the agent changing the
code) and, below GENERATED, what `make facts` writes from the code itself: every key and
button as F1 lists them, the command palette's commands, the settings and every feature's
note. Its first line, the stamp, holds a version (one more at each restamp), when it was
stamped (UTC) and a hash of the source it describes as git's index holds it: the package's
.py and .md files, the modules' .py files but their tests, and the README; never itself,
the tests or .plan/. The pre-commit hook (.githooks/pre-commit, `make hooks`) and a test
(test_facts.py) fail when that hash isn't the source's, so every commit that changes the
source restamps it, its guide checked against the change first (AGENTS.md).

Only the standard library at the top: the hook runs this file as a script, `python3
claude_wheelhouse/facts.py check`, with no venv. Generating imports the app (generated),
which needs one."""

import hashlib
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
ROOT = PACKAGE.parent   # the checkout
SHEET = PACKAGE / "helper_facts.md"
# the source the stamp's hash covers, as git pathspecs from the checkout's root
SOURCE = ("README.md", ":(glob)claude_wheelhouse/**/*.py", ":(glob)claude_wheelhouse/**/*.md",
          ":(glob)modules/*/**/*.py", ":(exclude,glob)modules/*/tests/**",
          ":(exclude)claude_wheelhouse/helper_facts.md")
STAMP = re.compile(r"<!-- fact sheet stamp: version (\d+), (\S+), source (\w+) ")
TITLE = "# Wheelhouse fact sheet"
GENERATED = "<!-- generated: everything below is written by make facts from the code. Edit the code, not this -->"
CONFLICT = re.compile(r"^(<{7}|={7}|>{7})( |$)", re.M)
FIX = "run make facts (after git add), then commit again (AGENTS.md, The helper's fact sheet)"


class Stale(Exception):
    """The sheet doesn't describe the source as it is."""


def git(root: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    if done.returncode:
        raise ValueError(f"git {args[0]} failed: {done.stderr.strip()}")
    return done.stdout


def source_hash(root: Path = ROOT) -> str:
    """The source the sheet describes, as git's index holds it (what the next commit holds):
    each file's path and blob id. Not a file read: a commit's tree and its index agree."""
    listed = git(root, "ls-files", "-s", "-z", "--", *SOURCE)
    entries = sorted({(path, line.split()[1]) for line in listed.split("\0") if line
                      for path in [line.partition("\t")[2]]})
    digest = hashlib.sha256()
    for path, blob in entries:
        digest.update(f"{path}\0{blob}\0".encode())
    return digest.hexdigest()[:16]


def stamp_line(version: int, at: datetime, source: str) -> str:
    return (f"<!-- fact sheet stamp: version {version}, {at.strftime('%Y-%m-%dT%H:%M:%SZ')}, source {source} "
            "(make facts restamps it: AGENTS.md) -->")


def read_stamp(text: str) -> tuple[int, str] | None:
    """The stamp's version and source hash, None if the first line isn't one."""
    m = STAMP.match(text)
    return (int(m[1]), m[3]) if m else None


def check(root: Path = ROOT, sheet: Path | None = None, commit: bool = False) -> None:
    """Stale if the sheet's stamp isn't for the source in git's index; for a commit (the
    hook), also if the sheet isn't part of it: every commit restamps, source or not."""
    sheet = sheet or root / SHEET.relative_to(ROOT)
    rel = str(sheet.relative_to(root))
    if commit and git(root, "rev-parse", "--verify", "-q", "HEAD").strip() and \
            rel not in git(root, "diff", "--cached", "--name-only", "--", rel).split():
        raise Stale(f"every commit restamps the fact sheet, and this one doesn't: {FIX}")
    try:   # the staged copy, which is what the commit holds
        text = git(root, "show", f":{rel}")
    except ValueError:   # not added yet
        text = ""
    stamp = read_stamp(text)
    if stamp is None:
        raise Stale(f"{sheet.name} has no stamp on its first line: {FIX}")
    if stamp[1] != (now := source_hash(root)):
        raise Stale(f"{sheet.name} was stamped for other source ({stamp[1]}, not {now}): check its guide "
                    f"against your change, then {FIX}")


def restamp(text: str, source: str, at: datetime, generated: str | None = None) -> str:
    """The sheet with a new stamp, a version past every one it holds (two branches' stamps,
    in a merge's conflict, both count), and, given generated, that below GENERATED. Whatever
    stood above the title (a stamp, or a conflict over it) goes. ValueError if the guide
    still holds a conflict, which only a person can resolve."""
    versions = [int(v) for v in re.findall(r"fact sheet stamp: version (\d+)", text)]
    start = text.find(TITLE)
    body = text[start:] if start >= 0 else f"{TITLE}\n"
    guide, mark, below = body.partition(GENERATED)
    if generated is not None:
        mark, below = GENERATED, "\n\n" + generated.strip() + "\n"
    if CONFLICT.search(guide if generated is not None else guide + below):
        raise ValueError("the fact sheet still holds a merge conflict: resolve it, then make facts")
    return f"{stamp_line(max(versions, default=0) + 1, at, source)}\n{guide.rstrip()}\n\n{mark}{below}"


def generated() -> str:
    """The sheet's generated part, from the code: imports the app, so needs its venv."""
    from . import cli, tui, whatsnew

    out = ["## Version", f"This fact sheet describes claude-wheelhouse {whatsnew.running_version()}.",
           keys_section(tui, module_widgets()).replace("# Keys", "## Keys, from the bindings (F1 lists the same)", 1),
           "## The command palette (Ctrl+P)", *[f"- **{title}**: {help}" for title, help in palette(tui)],
           "## Command line and settings (make args prints the same)", "```", cli.args_text(cli.parser()), "```",
           "## Features, by release (the release notes)", *features(whatsnew)]
    return "\n\n".join(out)


def keys_section(tui, widgets) -> str:
    """F1's text as a person sees it with SDK mode off, any module's keys shown, its headings
    one level down."""
    return re.sub(r"^## ", "### ", tui.keys_help(False, (), widgets), flags=re.M)


def module_widgets() -> list:
    """The installed modules' pane widgets that name a section of F1 (api.py, KEYS_HELP)."""
    import importlib

    from . import api
    out = []
    for loaded in api.load():
        if loaded.module is None:
            continue
        package = loaded.module.panes[0].surfaces["tui"].__module__ if loaded.module.panes else ""
        try:
            pane = importlib.import_module(package)
        except (ImportError, ValueError):
            continue
        out += [c for c in vars(pane).values()
                if isinstance(c, type) and c.__module__ == pane.__name__ and getattr(c, "KEYS_HELP", None)]
    return out


def palette(tui) -> list[tuple[str, str]]:
    """The wheelhouse's own command palette commands, by title and help, read from the app's
    get_system_commands without running it."""
    found = re.findall(r'SystemCommand\("([^"]+)",\s*((?:"[^"]*"\s*)+)', Path(tui.__file__).read_text())
    return [(title, "".join(re.findall(r'"([^"]*)"', help))) for title, help in found]


def features(whatsnew) -> list[str]:
    """Every feature's release note, newest release first, SDK-only ones left out (the helper
    answers for tab sessions: helper.py)."""
    shown = [f for f in whatsnew.FEATURES if f.when != "sdk"]
    released = sorted({f.version for f in shown} - {whatsnew.UNRELEASED}, key=whatsnew.version_key, reverse=True)
    out = []
    for v in [whatsnew.UNRELEASED, *released]:
        if not any(f.version == v for f in shown):
            continue
        label = "Not yet released" if v == whatsnew.UNRELEASED else v
        out.append(f"### {label}\n\n" + "\n".join(f"- {f.note}" for f in shown if f.version == v))
    return out


def make(root: Path = ROOT, sheet: Path | None = None, now: datetime | None = None, regenerate: bool = True) -> str:
    """make facts: the generated part written again, the sheet restamped for the source in
    git's index and added to it. What it says: the stamp, and any source changed but not
    added, which the stamp doesn't cover."""
    sheet = sheet or root / SHEET.relative_to(ROOT)
    if unmerged := git(root, "ls-files", "-u", "--", *SOURCE).strip():
        raise ValueError("the source has unresolved conflicts, which the stamp can't cover: resolve them, "
                         f"git add them, then make facts ({unmerged.split()[-1]})")
    text = restamp(sheet.read_text() if sheet.exists() else "", source_hash(root), now or datetime.now(UTC),
                   generated() if regenerate else None)
    sheet.write_text(text)
    git(root, "add", "--", str(sheet))
    out = [text.partition("\n")[0]]
    if unstaged := git(root, "diff", "--name-only", "--", *SOURCE).split():
        out.append("not added, so not in the stamp: " + ", ".join(unstaged) + " (git add them, then make facts again)")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    """The repo is the one it's run in (a hook's, make's), whichever copy of this file runs."""
    what = argv[0] if argv else "check"
    try:
        root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
        if what == "check":
            check(root, commit="--commit" in argv)
            return 0
        if what == "make":
            print(make(root))
            return 0
    except Stale as e:
        print(f"wheelhouse: fact sheet: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"wheelhouse: fact sheet: {e}", file=sys.stderr)
        return 2
    print("usage: python -m claude_wheelhouse.facts [check|make]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
