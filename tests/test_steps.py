"""Session steps (steps.py): their texts per event and switch, how a switch resolves, and the
handoff file."""

import os
import time

import pytest

from claude_wheelhouse import mcp_server, monitor, steps
from claude_wheelhouse.host import KEEP_ASK, compact_ask


@pytest.fixture(autouse=True)
def no_step_settings(monkeypatch):
    """No step given outside the app, whatever the run's environment or .env files."""
    for key in [k for k in os.environ if k.startswith("WHEELHOUSE_STEP")]:
        monkeypatch.delenv(key)
    monkeypatch.setattr(steps.features, "env_files", lambda: [])
    steps.set_flags([])
    yield
    steps.set_flags([])


@pytest.mark.parametrize("event, settings, has, lacks, desc", [
    ("before_compact", {}, ["pressed Compact", "write_handoff"], [], "announce and handoff by default"),
    ("before_compact", {"handoff": "off"}, ["pressed Compact"], ["write_handoff"], "handoff switched off"),
    ("before_compact", {"announce": "off"}, ["write_handoff"], ["pressed Compact"], "announce switched off"),
    ("before_forget", {}, ["Stop & Forget", "final, terse", "Close each open item"], [], "all on by default"),
    ("before_forget", {"items": "off"}, ["Stop & Forget"], ["Close each open item"], "items off"),
    ("pr_merged", {}, ["PR repo#5", "remove the worktree"], [], "cleanup on by default once chosen"),
    ("on_resume", {}, ["away 3 hours", "Your handoff", "Check what changed"], [], "all on, with a handoff"),
    ("session_start", {}, [], ["last session"], "start's add-on off by default, and no announce"),
    ("session_start", {"handoff": "on"}, ["last session"], [], "start's add-on switched on"),
])
def test_message_per_event_and_switch(store, event, settings, has, lacks, desc):
    store.set_setting(steps.CHOSEN, "1")
    for sw, v in settings.items():
        store.set_setting(steps.setting_key(event, sw), v)
    text = steps.message(store, event, why="The person pressed Compact.", away="3 hours", pr="repo#5",
                         ref="PR5", handoff="Your handoff, written then:\n\nnotes", last="The last session ...")
    assert all(h in text for h in has) and not any(x in text for x in lacks), f"{desc}: {text!r}"
    assert not text or text.startswith("[wheelhouse] "), desc


@pytest.mark.parametrize("event, chosen, on, desc", [
    ("pr_merged", False, False, "unattended: no add-on until the dialog is answered"),
    ("after_compact", False, False, "unattended: no handoff read back until answered"),
    ("pr_merged", True, True, "answered: cleanup on by default"),
    ("before_compact", False, True, "pressed: the dialog shows first, so the default applies"),
])
def test_unattended_events_announce_only_until_chosen(store, event, chosen, on, desc):
    if chosen:
        store.set_setting(steps.CHOSEN, "1")
    addon = next(iter(steps.ADDONS[event]))
    assert steps.switch(store, event, addon) is on, desc
    if event in steps.ANNOUNCE:
        assert steps.switch(store, event, "announce"), f"{desc}: announce stays on"


@pytest.mark.parametrize("flag, env, dotenv, app, all_off, on, desc", [
    ("off", "on", "on", "on", None, False, "the flag wins"),
    (None, "off", "on", "on", None, False, "then the environment"),
    (None, None, "off", "on", None, False, "then a .env"),
    (None, None, None, "off", None, False, "then the app's choice"),
    (None, None, None, None, None, True, "then the default"),
    (None, None, None, "on", "off", False, "STEP_ADDONS=off beats the app's choice"),
    (None, "on", None, None, "off", True, "a switch given by name beats STEP_ADDONS"),
    (None, "maybe", None, "off", None, False, "a bad value is the next source's"),
])
def test_switch_precedence(store, tmp_path, monkeypatch, flag, env, dotenv, app, all_off, on, desc):
    if flag:
        assert steps.set_flags([f"before_compact.handoff={flag}"]) == [], desc
    if env:
        monkeypatch.setenv("WHEELHOUSE_STEP_BEFORE_COMPACT_HANDOFF", env)
    lines = [f"STEP_BEFORE_COMPACT_HANDOFF={dotenv}"] if dotenv else []
    lines += [f"STEP_ADDONS={all_off}"] if all_off else []
    (tmp_path / ".env").write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(steps.features, "env_files", lambda: [tmp_path / ".env"])
    if app:
        store.set_setting(steps.setting_key("before_compact", "handoff"), app)
    assert steps.switch(store, "before_compact", "handoff") is on, desc


@pytest.mark.parametrize("value, ok, desc", [
    ("before_compact.handoff=off", True, "a known switch"),
    ("session_start.announce=on", False, "start has no announce"),
    ("before_compact.cleanup=on", False, "an add-on of another event"),
    ("nope.handoff=on", False, "no such event"),
    ("before_compact.handoff=maybe", False, "neither on nor off"),
])
def test_step_flag(value, ok, desc):
    assert (steps.set_flags([value]) == []) is ok, desc


@pytest.mark.parametrize("event, sdk, enabled, desc", [
    ("before_compact", False, False, "compaction steps need SDK mode"),
    ("after_compact", False, False, "and so does its aftermath"),
    ("before_compact", True, True, "on with SDK mode"),
    ("before_forget", False, True, "Forget runs for tabs"),
    ("pr_merged", False, True, "PR merged runs for tabs"),
])
def test_compaction_steps_are_sdk_only(event, sdk, enabled, desc):
    assert steps.enabled(event, sdk) is enabled, desc


def test_compact_ask_ends_with_the_keep_ask(store):
    text = compact_ask(store, "ab12")
    assert text.endswith(KEEP_ASK.format(nonce="ab12")) and "write_handoff" in text


def test_end_request_is_the_before_forget_step(store):
    text = monitor.request_text(store, "end", sep=" ")
    assert "\n" not in text and text.endswith(monitor.END_MECHANICS), "one line for a tab, end_session last"
    assert monitor.request_text(store, "park") == monitor.REQUEST_TEXT["park"], "Park keeps its own text"


@pytest.fixture
def tool(store, sid, monkeypatch):
    monkeypatch.setattr(mcp_server, "_store", store)
    monkeypatch.setattr(mcp_server, "_sid", sid)
    return mcp_server.write_handoff


def test_write_handoff_writes_and_reposts_its_doc(store, sid, tool):
    first = tool("did the thing")
    path = steps.handoff_path(sid)
    meta, text = steps.read_handoff(path)
    assert text == "did the thing" and meta["session"] == sid and meta["name"] == "demo", "front matter written"
    second = tool("did more")
    assert first == second, "the same doc item, re-posted"
    assert steps.read_handoff(path)[1] == "did more", "rewritten whole"
    assert "Your handoff, written" in steps.handoff_field(sid) and "did more" in steps.handoff_field(sid)


@pytest.mark.parametrize("text, refused, desc", [
    ("x" * steps.HANDOFF_CAP, False, "at the cap"),
    ("x" * (steps.HANDOFF_CAP + 1), True, "over the cap"),
    ("   ", True, "empty"),
])
def test_write_handoff_limits(store, sid, text, refused, desc):
    if refused:
        with pytest.raises(ValueError):
            steps.write_handoff(store, sid, text)
    else:
        assert steps.write_handoff(store, sid, text).is_file(), desc


def test_last_handoff_is_the_newest_ended_in_the_directory(store, tmp_path):
    old, new, live = (store.create_session(str(tmp_path), name=n) for n in ("old", "new", "live"))
    other = store.create_session(str(tmp_path / "elsewhere"), name="other")
    for i, s in enumerate((old, new, live, other)):
        path = steps.write_handoff(store, s, f"from {s}")
        os.utime(path, (1000 + i, 1000 + i))
    text = steps.last_handoff(str(tmp_path), live={live})
    assert f"from {new}" in text and "new" in text, "the newest of the ended ones, in this directory"
    assert steps.last_handoff(str(tmp_path / "nowhere")) == "", "none in another directory"


def test_prune_handoffs(store, sid):
    path = steps.write_handoff(store, sid, "notes")
    now = time.time()
    assert steps.prune_handoffs(now) == 0 and path.is_file(), "a fresh one stays"
    os.utime(path, (now - 31 * 86400, now - 31 * 86400))
    assert steps.prune_handoffs(now) == 1 and not path.exists(), "untouched 30 days: removed"


@pytest.mark.parametrize("away, parked, held, has, lacks, desc", [
    (2 * 3600, False, [], ["JOINED", "away 2 hours"], ["has merged"], "two hours away: On resume"),
    (600, False, [], ["JOINED"], ["away"], "a quick relaunch: no extra turn"),
    (600, True, [], ["JOINED", "away 10 minutes"], [], "parked: On resume whatever the time"),
    (600, False, [("repo#5 (PR5)", "PR5")], ["JOINED", "repo#5 (PR5) has merged"], ["away"], "a held PR joins it"),
])
def test_resumed(store, sid, away, parked, held, has, lacks, desc):
    store.set_setting(steps.CHOSEN, "1")
    for pr, ref in held:
        steps.hold(store, sid, pr, ref)
    text = steps.resumed(store, sid, away, parked, "JOINED")
    assert all(h in text for h in has) and not any(x in text for x in lacks), f"{desc}: {text!r}"
    assert steps.take_held(store, sid) == [], f"{desc}: held PRs are told once"


@pytest.mark.parametrize("running, parked, told, desc", [
    (True, False, True, "running: told at once, as a notice"),
    (False, False, False, "not running: held for its resume"),
    (True, True, False, "parked: held, never a cold start"),
])
def test_pr_merged_is_told_or_held(store, sid, running, parked, told, desc):
    from claude_wheelhouse.hub import Hub
    hub = Hub(store)
    url = "https://github.com/o/repo/pull/5"
    store.post_pr(sid, url, "fix it", None, False)
    hub.statuses[sid] = "live" if running else "available"
    if parked:
        store.set_parked(sid, True)
    hub.set_pr_states({url: "open"})
    hub.set_pr_states({url: "merged"})
    notices = [m["body"] for m in store.pending(sid) if m["kind"] == "notice"]
    assert bool([n for n in notices if "repo#5 (PR5) has merged" in n]) is told, desc
    assert (steps.take_held(store, sid) == [("repo#5 (PR5)", "PR5")]) is not told, desc
    hub.set_pr_states({url: "merged"})
    assert len(notices) <= 1 and steps.take_held(store, sid) == [], f"{desc}: once, not at every fetch"


@pytest.mark.parametrize("on, has_last, desc", [
    (False, False, "off by default: the brief alone"),
    (True, True, "on: the last handoff in this directory follows the brief"),
])
def test_session_start(store, sid, tmp_path, on, has_last, desc):
    from claude_wheelhouse import launch
    gone = store.create_session(str(tmp_path), name="before")
    steps.write_handoff(store, gone, "where I left it")
    store.end(gone)
    if on:
        store.set_setting(steps.setting_key("session_start", "handoff"), "on")
    prompt = launch.opening_prompt(store.session(sid), store)
    assert prompt.startswith("Ticket: #7") and ("where I left it" in prompt) is has_last, desc
