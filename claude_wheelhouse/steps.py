"""Session steps (.plan/session-steps.md): what the wheelhouse says to a session at a
lifecycle event. Announce, on by default, tells the session the event is about to happen (or
has), so whatever its own instructions (CLAUDE.md) say about it takes effect. Add-ons ask for
something more in the same message, never instead. The handoff add-ons keep one short
markdown file per session, written through the write_handoff tool and shown as its doc item.

Every switch resolves as features.given resolves a setting: --step EVENT.SWITCH=on|off, else
WHEELHOUSE_STEP_<EVENT>_<SWITCH>, else STEP_<EVENT>_<SWITCH>= in either .env; then the choice
made in the app (the settings table, step.<event>.<switch>); then the default. STEP_ADDONS=off
turns every add-on off at once. Never imports Textual.
"""

import json
import os
import time
from pathlib import Path

from . import features
from .store import Store, db_path

# the events, in the order the Session steps dialog lists them
EVENTS = ("before_compact", "before_forget", "on_resume", "after_compact", "pr_merged", "session_start")
TITLES = {"before_compact": "Before Compact", "before_forget": "Before Forget", "on_resume": "On resume",
          "after_compact": "After Compact", "pr_merged": "PR merged", "session_start": "Session start"}
# the wheelhouse can drive a compaction only in a session it hosts: outside SDK mode these
# events are disabled, and a tab never gets them (Doug, M7 review)
SDK_ONLY = frozenset({"before_compact", "after_compact"})
# what the person presses to fire these; the others (the 8 h mark, a PR merging, a
# compaction's aftermath) fire with nobody there, so run announce-only until the
# first-press dialog has been answered (CHOSEN)
UNATTENDED = frozenset({"after_compact", "pr_merged", "session_start"})
# "after" events: sent as soon as the event happens, never held behind the session's queued
# answers. Not a switch: the dialogs say so (after_note)
AFTER = frozenset({"on_resume", "after_compact", "pr_merged"})
def after_note(sdk: bool) -> str:
    """The dialogs' plain note on "after" messages: compaction named only in SDK mode."""
    events = "resumed, compacted, PR merged" if sdk else "resumed, PR merged"
    return (f"Messages after an event ({events}) go to the session at once, without waiting for its "
            "queued answers to be sent.")
CHOSEN = "step.chosen"   # set once the first-press dialog has been answered
ALL_ADDONS_KEY = "STEP_ADDONS"
STEP_FLAG = "--step"

ANNOUNCE = {
    "before_compact": "{why} If your own instructions say to do anything before compaction (save a "
                      "transcript or handoff note, say), do it now.",
    "before_forget": "The person pressed Stop & Forget in the wheelhouse. If your own instructions "
                     "describe anything to do when a session ends, do it first.",
    "on_resume": "You were away {away}. If your own instructions say what to do when a session "
                 "resumes, do it now.",
    "after_compact": "{reminder}",
    "pr_merged": "Your PR {pr} has merged. If your own instructions say what to do once a PR merges, do it now.",
}
WRITE = ("call write_handoff with a terse handoff: what was done, decisions and why, open threads, "
         "and the next step")
ADDONS = {
    "before_compact": {"handoff": f"Also {WRITE}."},
    "before_forget": {"handoff": f"Also {WRITE.replace('a terse', 'a final, terse')}.",
                      "items": "Close each open item with update_item, or say in its body who or what it's "
                               "handed to."},
    "on_resume": {"handoff": "{handoff}",
                  "changes": "Check what changed while you were away (git status, new commits on your branch "
                             "and its base, PRs merged), update your items to match, and call write_handoff "
                             "if anything moved."},
    "after_compact": {"handoff": "{handoff}"},
    "pr_merged": {"cleanup": "If the PR's worktree and branch are your own and nothing in them is "
                             "uncommitted, remove the worktree, delete the branch, and say so on {ref}."},
    "session_start": {"handoff": "{last}"},
}
DEFAULTS = {(event, "announce"): True for event in ANNOUNCE}
DEFAULTS.update({(e, a): True for e, addons in ADDONS.items() for a in addons})
DEFAULTS[("session_start", "handoff")] = False   # off: Claude Code's own start applies CLAUDE.md
# what the first-press dialog offers, each one tick for the add-ons it names
OFFERED = (("handoff", "keep a handoff for each session (what was done, decisions, open threads, next "
                       "step), shown as its doc and read back on resume", (("before_compact", "handoff"),
                       ("before_forget", "handoff"), ("on_resume", "handoff"), ("after_compact", "handoff"))),
           ("items", "before Stop & Forget, close or hand over its open items", (("before_forget", "items"),)),
           ("changes", "on resume, report what changed while it was away", (("on_resume", "changes"),)),
           ("cleanup", "when its PR merges, remove the PR's worktree and branch if they're its own and clean",
            (("pr_merged", "cleanup"),)))

RESUME_AFTER = 3600   # seconds away before a resume runs On resume: a quick relaunch costs no turn
HANDOFF_CAP = 16 * 1024   # bytes: write_handoff refuses more
HANDOFF_DAYS = 30   # a handoff untouched this long is removed when the hub starts

_flags: dict[tuple[str, str], str] = {}   # --step EVENT.SWITCH=VALUE, as the command line gave them
_warned: set[str] = set()


def switches(event: str) -> tuple[str, ...]:
    """An event's switches: announce (if it has one), then its add-ons."""
    return (("announce",) if event in ANNOUNCE else ()) + tuple(ADDONS.get(event, ()))


def set_flags(values: list[str] | None) -> list[str]:
    """--step's values, each EVENT.SWITCH=on|off, kept for switch(). What's wrong with any,
    one line each, which is then ignored."""
    _flags.clear()
    bad = []
    for value in values or ():
        name, _, v = value.partition("=")
        event, _, sw = name.strip().partition(".")
        if event not in EVENTS or sw not in switches(event) or v.strip().lower() not in ("on", "off"):
            bad.append(f"{STEP_FLAG} {value}: expected EVENT.SWITCH=on|off, EVENT one of {', '.join(EVENTS)}")
            continue
        _flags[(event, sw)] = v.strip().lower()
    return bad


def flag_args() -> list[str]:
    """The --step values given, as arguments again: for a daemon or host this process starts."""
    return [a for (event, sw), v in _flags.items() for a in (STEP_FLAG, f"{event}.{sw}={v}")]


def flag_env() -> dict[str, str]:
    """The --step values given, as the environment variables a process this one starts reads
    (WHEELHOUSE_STEP_*): for a tab's Claude Code, whose monitor and MCP server take no flags
    of ours."""
    return {f"WHEELHOUSE_{env_key(event, sw)}": v for (event, sw), v in _flags.items()}


def env_key(event: str, sw: str) -> str:
    return f"STEP_{event.upper()}_{sw.upper()}"


def setting_key(event: str, sw: str) -> str:
    return f"step.{event}.{sw}"


def _on(value: str, where: str) -> bool | None:
    """A given value as on or off; None, said once naming where, if it's neither."""
    v = value.strip().lower()
    if v in ("on", "1", "true", "yes"):
        return True
    if v in ("off", "0", "false", "no"):
        return False
    if (said := f"wheelhouse: {where}={value!r} is not on or off: the default is used") not in _warned:
        _warned.add(said)
        features._say(said)
    return None


def fixed(event: str, sw: str) -> tuple[bool, str] | None:
    """A switch given outside the app, and where: the flag, the environment or a .env
    (STEP_ADDONS=off included, for an add-on). None if the app's choice applies."""
    if (got := features.given(_flags.get((event, sw)), STEP_FLAG, env_key(event, sw))) is not None:
        if (on := _on(*got)) is not None:
            return on, got[1]
    if sw != "announce" and (got := features.given(None, STEP_FLAG, ALL_ADDONS_KEY)) is not None:
        if _on(*got) is False:
            return False, got[1]
    return None


def chosen(store: Store) -> bool:
    """Whether the first-press dialog has been answered, or nothing needs it: every add-on is
    given outside the app."""
    return bool(store.setting(CHOSEN)) or all(fixed(e, a) is not None for e, addons in ADDONS.items() for a in addons)


def switch(store: Store, event: str, sw: str) -> bool:
    """Whether a switch is on: given outside the app, else the app's choice, else the
    default. An add-on of an event nobody pressed is off until the first-press dialog has
    been answered: no one gets turns they haven't seen offered."""
    if (got := fixed(event, sw)) is not None:
        return got[0]
    if (v := store.setting(setting_key(event, sw))) is not None:
        return v == "on"
    if sw != "announce" and event in UNATTENDED and not chosen(store):
        return False
    return DEFAULTS[(event, sw)]


def table(store: Store, sdk: bool) -> dict:
    """The Session steps dialog's rows: each event that runs (enabled), its switches, each
    on or off and, if given outside the app, where from; whether the first-press dialog has
    been answered; and the plain note on "after" messages."""
    rows = [{"event": e, "title": TITLES[e],
             "switches": [{"name": sw, "on": switch(store, e, sw), "fixed": got[1] if (got := fixed(e, sw)) else None}
                          for sw in switches(e)]}
            for e in EVENTS if enabled(e, sdk)]
    return {"chosen": chosen(store), "events": rows, "note": after_note(sdk)}


def set_switch(store: Store, event: str, sw: str, on: bool | None) -> None:
    """A switch as chosen in the app; None puts its default back. Refused for a switch the
    event doesn't have."""
    if event not in EVENTS or sw not in switches(event):
        raise ValueError(f"{event}.{sw} is not a session step's switch")
    if on is None:
        store.clear_setting(setting_key(event, sw))
    else:
        store.set_setting(setting_key(event, sw), "on" if on else "off")


def choose(store: Store, ticks: dict[str, bool]) -> None:
    """The first-press dialog answered: each offered tick (OFFERED) on or off for every
    add-on it names, and the dialog never shown again."""
    for name, _, pairs in OFFERED:
        if name in ticks:
            for event, sw in pairs:
                set_switch(store, event, sw, bool(ticks[name]))
    store.set_setting(CHOSEN, "1")


def enabled(event: str, sdk: bool) -> bool:
    """Whether an event runs at all: compaction's only in SDK mode (SDK_ONLY)."""
    return sdk or event not in SDK_ONLY


def message(store: Store, event: str, mechanics: str = "", sep: str = "\n\n", mark: bool = True,
            **fields: str) -> str:
    """The step's message: the announce (if on), then each add-on that's on, then the
    mechanics the event needs (the keep ask, end_session). An add-on whose field is empty (no
    handoff to show) is left out. Starts [wheelhouse], complete on its own; empty if there's
    nothing to say. sep: between the parts (a tab's monitor prints one line). mark=False
    leaves [wheelhouse] off, for a notice, which gets it as it's delivered."""
    parts = []
    if event in ANNOUNCE and switch(store, event, "announce"):
        parts.append(ANNOUNCE[event].format(**fields))
    for name, text in ADDONS.get(event, {}).items():
        if switch(store, event, name):
            needs = [f for f in ("handoff", "last") if "{" + f + "}" in text]
            if all(fields.get(f) for f in needs):
                parts.append(text.format(**fields))
    if mechanics:
        parts.append(mechanics)
    body = sep.join(p.strip() for p in parts if p.strip())
    return f"[wheelhouse] {body}" if body and mark else body


def wants_handoff(store: Store, event: str) -> bool:
    """Whether the step asks for a handoff to be written (checked afterwards: write_handoff
    called since it was sent)."""
    return event in ("before_compact", "before_forget") and switch(store, event, "handoff")


# PR merged on a session that isn't running: held, never a cold start for a notice, and
# joined to its On resume message. Kept in the settings table, so no schema change


def held_key(sid: str) -> str:
    return f"step.held.{sid}"


def hold(store: Store, sid: str, pr: str, ref: str) -> None:
    """A PR merged while the session wasn't running, kept for its resume."""
    held = json.loads(store.setting(held_key(sid)) or "[]")
    if [pr, ref] not in held:
        store.set_setting(held_key(sid), json.dumps([*held, [pr, ref]]))


def held(store: Store, sid: str) -> list[tuple[str, str]]:
    """The PRs held for a session, as (pr, ref)."""
    return [tuple(p) for p in json.loads(store.setting(held_key(sid)) or "[]")]


def clear_held(store: Store, sid: str, told: list[tuple[str, str]]) -> None:
    """Those told let go, once the message telling them is written: a merge held meanwhile
    stays for the next resume."""
    left = [list(p) for p in held(store, sid) if p not in told]
    if left:
        store.set_setting(held_key(sid), json.dumps(left))
    else:
        store.clear_setting(held_key(sid))


def pr_label(url: str, ref: str) -> str:
    """How a PR is named to its session: repo#number (ref), or the ref alone."""
    from .prs import parse
    got = parse(url)
    return f"{got[1]}#{got[2]} ({ref})" if got else ref


def resumed(store: Store, sid: str, away_seconds: float | None, parked: bool,
            joined: str) -> tuple[str, list[tuple[str, str]]]:
    """What a resumed session is told, without the [wheelhouse] mark: joined (JOINED_TEXT),
    then On resume after RESUME_AFTER or more away (or parked), then each PR merged while it
    was away; and those PRs, for clear_held once it's written. joined alone otherwise."""
    parts = [joined]
    if parked or (away_seconds is not None and away_seconds >= RESUME_AFTER):
        parts.append(message(store, "on_resume", mark=False, away=away(away_seconds or 0),
                             handoff=handoff_field(sid)))
    told = held(store, sid)
    for pr, ref in told:
        parts.append(message(store, "pr_merged", mark=False, pr=pr, ref=ref))
    return "\n\n".join(p for p in parts if p), told


# the handoff file


def handoff_dir() -> Path:
    """Beside the database, so a test or a second database has its own."""
    return db_path().parent / "handoffs"


def handoff_path(sid: str) -> Path:
    """Named by session id: stable across a rename, distinct for a fork."""
    return handoff_dir() / f"{sid}.md"


def utc(at: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(at))


def write_handoff(store: Store, sid: str, text: str) -> Path:
    """The session's handoff, rewritten whole (a temporary file, then a rename), with front
    matter the wheelhouse writes: session, name, cwd, updated (UTC). Refused over HANDOFF_CAP."""
    text = text.strip()
    if not text:
        raise ValueError("the handoff is empty: write what was done, decisions, open threads and the next step")
    if len(text.encode()) > HANDOFF_CAP:
        raise ValueError(f"the handoff is {len(text.encode()) // 1024} KB, over the {HANDOFF_CAP // 1024} KB "
                         "limit: keep it short enough to read at a glance")
    s = store.session(sid)
    if s is None:
        raise ValueError("this session has gone from the wheelhouse")
    path = handoff_path(sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    front = "\n".join(f"{k}: {v}" for k, v in (("session", sid), ("name", s["name"] or ""), ("cwd", s["cwd"] or ""),
                                                ("updated", utc(time.time()))))
    tmp = path.with_suffix(".tmp")
    tmp.write_text(f"---\n{front}\n---\n\n{text}\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_handoff(path: Path) -> tuple[dict, str] | None:
    """A handoff's front matter and text; None if there's none, or it can't be read."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    meta = {}
    if raw.startswith("---\n") and (end := raw.find("\n---\n", 4)) != -1:
        for line in raw[4:end].splitlines():
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
        raw = raw[end + 5:]
    return meta, raw.strip()


def handoff_field(sid: str) -> str:
    """The session's handoff inline, for On resume and After Compact: empty if it has none."""
    got = read_handoff(handoff_path(sid))
    if got is None or not got[1]:
        return ""
    return f"Your handoff, written {got[0].get('updated', 'earlier')}:\n\n{got[1]}"


def last_handoff(cwd: str, skip: str = "", live: set | frozenset = frozenset()) -> str:
    """For Session start: the newest handoff of an ended session (not in live) in the same
    directory, inline; empty if there's none."""
    best = None
    for path in handoff_dir().glob("*.md") if handoff_dir().is_dir() else ():
        if path.stem == skip or path.stem in live:
            continue
        got = read_handoff(path)
        if got and got[1] and got[0].get("cwd") == cwd:
            stamp = path.stat().st_mtime
            if best is None or stamp > best[0]:
                best = (stamp, got)
    if best is None:
        return ""
    meta, text = best[1]
    return (f"The last session in this directory, {meta.get('name') or meta.get('session', 'unnamed')}, ended "
            f"{meta.get('updated', 'earlier')} with this handoff:\n\n{text}")


def prune_handoffs(at: float | None = None) -> int:
    """Remove handoffs untouched for HANDOFF_DAYS: how many went."""
    at = time.time() if at is None else at
    gone = 0
    for path in handoff_dir().glob("*.md") if handoff_dir().is_dir() else ():
        try:
            if at - path.stat().st_mtime > HANDOFF_DAYS * 86400:
                path.unlink()
                gone += 1
        except OSError:
            pass
    return gone


def away(seconds: float) -> str:
    """How long a session was away, said plainly."""
    hours = seconds / 3600
    if hours >= 48:
        return f"{hours / 24:.0f} days"
    if hours >= 1.5:
        return f"{hours:.0f} hours"
    return f"{seconds / 60:.0f} minutes"
