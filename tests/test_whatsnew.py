import re
import tomllib
from pathlib import Path

import pytest

from claude_wheelhouse import whatsnew
from claude_wheelhouse.whatsnew import ADVANCED, DIALOGS, FEATURES, MAX_TIPS, TIPPED, UNRELEASED, WHENS, Feature

EM_DASH = chr(0x2014)
# the version being worked on, from pyproject.toml: the installed metadata is stale until `uv sync`
VERSION = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())["project"]["version"]


def problems(features, dialogs=DIALOGS) -> set[str]:
    """What's wrong with a registry, by rule: empty for a sound one."""
    found = set()
    ids = [f.id for f in features]
    if len(ids) != len(set(ids)) or len({d.id for d in dialogs}) != len(dialogs):
        found.add("duplicate id")
    for f in features:
        if f.where not in (*TIPPED, ADVANCED):
            found.add("unknown where")
        if f.where != ADVANCED and not f.tip.strip():
            found.add("dialog feature without a tip")
        if f.where == ADVANCED and f.tip:
            found.add("advanced feature with a tip")
        if len(f.tip.splitlines()) > 2:
            found.add("tip over two lines")
        if f.when not in WHENS:
            found.add("unknown when")
        if not f.note.strip() or "\n" in f.note:
            found.add("note not one line")
    if any(len(whatsnew.tips(d.id, True, features)) > MAX_TIPS for d in dialogs):
        found.add("over MAX_TIPS tips")
    texts = [t for f in features for t in (f.note, f.tip)] + [t for d in dialogs for t in (d.title, d.text)] \
        + [whatsnew.LAUNCH]
    if any(EM_DASH in t for t in texts):
        found.add("em-dash")
    return found


def feature(fid="f", where="inbox", tip="A tip.", **kw):
    return Feature(fid, kw.pop("version", UNRELEASED), where, note=kw.pop("note", "A note."), tip=tip, **kw)


@pytest.mark.parametrize("features, expected, desc", [
    (FEATURES, set(), "the shipped registry"),
    ((feature("a"), feature("a")), {"duplicate id"}, "an id used twice"),
    ((feature(where="invite"),), {"unknown where"}, "the invite holds no tips"),
    ((feature(where="notes"),), {"unknown where"}, "the notes dialog holds no tips"),
    ((feature(where="nowhere"),), {"unknown where"}, "a where that is no dialog"),
    ((feature(tip=""),), {"dialog feature without a tip"}, "a dialog feature must have a tip"),
    ((feature(where=ADVANCED, tip=""),), set(), "an advanced feature needs no tip"),
    ((feature(where=ADVANCED),), {"advanced feature with a tip"}, "an advanced feature's tip shows nowhere"),
    ((feature(tip="one\ntwo"),), set(), "a two-line tip"),
    ((feature(tip="one\ntwo\nthree"),), {"tip over two lines"}, "a three-line tip"),
    ((feature(when="sdk"),), set(), "an SDK-only feature"),
    ((feature(when="mac"),), {"unknown when"}, "a when no one checks"),
    ((feature(note="two\nlines"),), {"note not one line"}, "a note over one line"),
    ((feature(tip=f"Drag {EM_DASH} drop."),), {"em-dash"}, "an em-dash in a tip"),
    (tuple(feature(f"t{n}") for n in range(MAX_TIPS)), set(), "a dialog at the cap"),
    (tuple(feature(f"t{n}") for n in range(MAX_TIPS + 1)), {"over MAX_TIPS tips"}, "one over: to the notes"),
    ((*(feature(f"t{n}") for n in range(MAX_TIPS)), feature("s", when="sdk")), {"over MAX_TIPS tips"},
     "an sdk tip counts: SDK mode on shows it too"),
    ((*(feature(f"t{n}") for n in range(MAX_TIPS)), feature("s", where=ADVANCED, tip="")), set(),
     "the notes take the overflow"),
])
def test_registry_rules(features, expected, desc):
    assert problems(features) == expected, desc


def test_dialogs_are_the_tour_in_order():
    assert [d.id for d in DIALOGS] == ["invite", "principles", "create", "sessions", "inbox", "answer", "send",
                                       "reply", "subagent", "decision", "close", "docs", "conversation", "ask", "stats", "decisions", "prs", "end", "notes"]
    assert {d.pane for d in DIALOGS} == {None, "sessions", "items", "detail", "session-info", "stats", "design", "prs"}


@pytest.mark.parametrize("version, features, ok, desc", [
    (VERSION, FEATURES, True, "the package as it stands"),
    ("0.3.0", (feature(version=UNRELEASED),), False, "a release with an unstamped feature"),
    ("0.3.0", (feature(version="0.3.0"),), True, "a release, stamped"),
    ("0.3.0.dev1", (feature(version=UNRELEASED),), True, "a development version may hold unreleased features"),
])
def test_no_unreleased_feature_in_a_release(version, features, ok, desc):
    assert (not whatsnew.is_release(version) or not whatsnew.unreleased(features)) is ok, desc


def test_the_package_has_no_unreleased_feature_once_released():
    assert not whatsnew.is_release(VERSION) or not whatsnew.unreleased(), \
        f"{VERSION} is a release: make stamp VERSION={VERSION} gives its features their version"


REGISTRY = (feature("tab", where="sessions"), feature("sdk-p", where="answer", when="sdk"),
            feature("adv", where=ADVANCED, tip=""), feature("inbox-sort", where="inbox"))


@pytest.mark.parametrize("seen, sdk, expected, desc", [
    ({}, False, ["tab", "adv", "inbox-sort"], "none seen, SDK mode off: the sdk feature held back"),
    ({}, True, ["tab", "sdk-p", "adv", "inbox-sort"], "none seen, SDK mode on: every one"),
    ({"tab": "shown", "sdk-p": "shown", "adv": "shown", "inbox-sort": "shown"}, True, [], "all shown"),
    ({"tab": "skipped", "adv": "shown"}, False, ["inbox-sort"], "some skipped, some shown"),
    ({"retired": "shown"}, False, ["tab", "adv", "inbox-sort"], "a retired id's row changes nothing"),
])
def test_pending(seen, sdk, expected, desc):
    assert [f.id for f in whatsnew.pending(seen, sdk, REGISTRY)] == expected, desc


NOTED = (feature("late", where="end", note="Ending, later in the tour."),
         feature("adv", where=ADVANCED, tip="", note="For old hands."),
         feature("early", where="create", note="Starting, early in the tour."),
         feature("old", version="0.2.0", where="create", note="Last release's."),
         feature("also", where="create", note="Starting, second."))


@pytest.mark.parametrize("version, expected, desc", [
    (UNRELEASED, ("## What's new in UNRELEASED\n\n### Starting a session\n\n- Starting, early in the tour.\n"
                  "- Starting, second.\n\n### Ending a session\n\n- Ending, later in the tour.\n\n"
                  "### Advanced\n\n- For old hands.\n"),
     "grouped by dialog in tour order, registry order within one, then Advanced"),
    ("0.2.0", "## What's new in 0.2.0\n\n### Starting a session\n\n- Last release's.\n", "only that version's"),
    ("0.9.0", "## What's new in 0.9.0\n\nNothing new to show: fixes only.\n", "a release with no features"),
])
def test_notes(version, expected, desc):
    assert whatsnew.notes(version, NOTED) == expected, desc


SOURCE = '''UNRELEASED = "UNRELEASED"
# like Feature("id", UNRELEASED, ...), or version=UNRELEASED
FEATURES = (
    # Feature("x", UNRELEASED, "inbox") in a comment
    Feature("a", UNRELEASED, "inbox", note="UNRELEASED stays in prose"),
    Feature("b", version=UNRELEASED, where="inbox"),
    Feature(
        "d",
        UNRELEASED,
        "inbox"),
    Feature("c", "0.2.0", "inbox"),
)
OTHER = (Feature("e", UNRELEASED, "inbox"),)
'''


@pytest.mark.parametrize("version, expected, desc", [
    ("0.3.0", (SOURCE.replace('"a", UNRELEASED', '"a", "0.3.0"').replace("version=UNRELEASED,", 'version="0.3.0",')
               .replace('"d",\n        UNRELEASED', '"d",\n        "0.3.0"'), 3),
     "positional, named and wrapped versions stamped; the constant, comments, prose, released features "
     "and anything outside FEATURES left alone"),
    ("0.3.0.dev1", ValueError, "a development version refused"),
    (UNRELEASED, ValueError, "UNRELEASED refused"),
])
def test_stamp(version, expected, desc):
    if expected is ValueError:
        with pytest.raises(ValueError):
            whatsnew.stamp(SOURCE, version)
        return
    assert whatsnew.stamp(SOURCE, version) == expected, desc


def test_stamp_the_real_source():
    """This module's own comments and docstrings never stamped: its source with FEATURES
    emptied comes back unchanged, and as it stands, only its unreleased features count."""
    source = Path(whatsnew.__file__).read_text()
    found = whatsnew.REGISTRY.search(source)
    assert found, "FEATURES found"
    empty = source[:found.start(1)] + source[found.end(1):]
    assert whatsnew.stamp(empty, "9.9.9") == (empty, 0), "nothing to stamp, nothing changed"
    assert whatsnew.stamp(source, "9.9.9")[1] == len(whatsnew.unreleased()), "each unreleased feature, once"


@pytest.mark.parametrize("argv, version, desc", [
    (["notes", "0.2.0"], "0.2.0", "the version asked for"),
    (["notes"], None, "this package's version by default"),
])
def test_the_notes_command(monkeypatch, capsys, argv, version, desc):
    from claude_wheelhouse import cli
    monkeypatch.setattr("sys.argv", ["claude-wheelhouse", *argv])
    cli.main()
    assert capsys.readouterr().out == whatsnew.notes(version or whatsnew.running_version()), desc


RELEASES = (feature("new", where="create", note="This one's."),
            feature("v3", version="0.3.0", where="create", note="0.3.0's."),
            feature("v2", version="0.2.0", where="create", note="0.2.0's."),
            feature("v1", version="0.1.0", where="create", note="0.1.0's."))


@pytest.mark.parametrize("running, seen, expected, desc", [
    ("0.4.0.dev0", "0.3.0", ["0.4.0.dev0"], "seen last release's: this one's alone"),
    ("0.4.0.dev0", "0.1.0", ["0.4.0.dev0", "0.3.0", "0.2.0"], "skipped a release: every one since, newest first"),
    ("0.4.0.dev0", None, ["0.4.0.dev0", "0.3.0", "0.2.0", "0.1.0"], "never seen any: every release"),
    ("0.3.0", "0.1.0", ["0.3.0", "0.2.0"], "a release running: its own, not repeated"),
    ("0.2.0", None, ["0.2.0", "0.1.0"], "nothing newer than the one running"),
])
def test_an_upgraders_notes(monkeypatch, running, seen, expected, desc):
    """Q121 B: an upgrader's last dialog shows every release since the last one they saw."""
    monkeypatch.setattr(whatsnew, "running_version", lambda: running)
    found = re.findall(r"^## What's new in (\S+)", whatsnew.since(seen, RELEASES), re.M)
    assert found == expected, desc


@pytest.mark.parametrize("a, b, desc", [
    ("0.3.0.dev0", "0.3.0", "a development version before its release"),
    ("0.3.0.dev1", "0.3.0.dev2", "development versions in order"),
    ("0.2.9", "0.10.0", "by number, not by text"),
    ("junk", "0.0.1", "an unreadable one first"),
])
def test_version_order(a, b, desc):
    assert whatsnew.version_key(a) < whatsnew.version_key(b), desc


@pytest.mark.parametrize("feature", ["show-all-tip", "interrupt-shell-tip"])
def test_session_list_tips_hang_on_its_dialog(feature):
    """Doug (#183): a tip about the session list shows beside it, titled Sessions, never on Detail's."""
    where = next(f.where for f in FEATURES if f.id == feature)
    assert next(d.pane for d in DIALOGS if d.id == where) == "sessions", f"{feature} on {where}"
