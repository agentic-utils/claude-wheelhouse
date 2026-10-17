import os
from datetime import datetime, timezone

import pytest
from rich.style import Style

from claude_wheelhouse import transcript_usage
from claude_wheelhouse.transcript_usage import Snapshot
from tests.transcripts import (D, H, HISTORY, NOW, at, command, compacted, compacted_snap, host, lines, picked, prompt, response, snap, synthetic, write)


@pytest.mark.parametrize("recs, context, ttl, turns, compactions, desc", [
    ([response("m1", made=800, read=80_000)], 80_802, "1h", 1, 0, "context is input, cache written and cache read"),
    ([response("m1", read=5), response("m1", read=5), response("m1", read=5)], 7, None, 1, 0,
     "a response written as several lines counts once"),
    ([response("m1", made=900, ttl="5m")], 902, "5m", 1, 0, "a 5-minute cache write is seen as one"),
    ([response("m1", made=900), response("m2", ago=30, read=902)], 904, "1h",
     2, 0, "a read-only response keeps the TTL of the last write"),
    ([compacted(), response("m1", read=11_000)], 11_002, None, 1, 1, "a compaction is counted, with its sizes"),
    ([{"type": "user", "timestamp": at(60), "message": {"content": "hi"}}], 0, None, 0, 0, "no usage, nothing counted"),
    ([response("m1", read=50_000, isSidechain=True)], 0, None, 1, 0,
     "a sidechain response is spend, not the main thread's context"),
    ([response("m1", made=900, read=80_000), synthetic("m2")], 80_902, "1h", 1, 0,
     "Claude Code's stand-in reply (an error, an interrupt) made no request: not a turn"),
    ([response("m1", made=900, read=80_000), response("m2", ago=30, inp=0)], 80_902, "1h", 1, 0,
     "nor does a response with no usage at all"),
])
def test_usage_parse(recs, context, ttl, turns, compactions, desc):
    follower = transcript_usage.UsageFollower("s")
    parsed = transcript_usage.parse(lines(*recs), follower.seen, main=True)
    follower.take(*parsed)
    snap = follower.snap
    assert (snap.context, snap.ttl, len(snap.turns), len(snap.compactions)) == (context, ttl, turns, compactions), desc


@pytest.mark.parametrize("read, fresh, new, miss, desc", [
    (80_000, 900, 900, 0, "on top of a cache read, fresh tokens are new input"),
    (0, 80_900, 0, 80_900, "with nothing read, they're a cache miss"),
])
def test_assembly_splits_new_from_miss(read, fresh, new, miss, desc):
    [turn], _ = transcript_usage.parse(lines(response("m1", inp=0, made=fresh, read=read)), set(), main=True)
    assert (turn.read, turn.new, turn.miss) == (read, new, miss), desc


def test_the_follower_reads_only_whats_appended_and_finds_subagents(tmp_path):
    folder = tmp_path / "-home-u-repo"
    (folder / "s" / "subagents").mkdir(parents=True)
    main = folder / "s.jsonl"
    main.write_bytes(b"\n".join(lines(response("m1", made=1000))) + b"\n")
    follower = transcript_usage.UsageFollower("s", tmp_path)
    assert follower.read(NOW) and follower.snap.context == 1002
    assert not follower.read(NOW), "nothing new: nothing read"
    with open(main, "ab") as f:
        f.write(b"\n".join(lines(response("m2", ago=30, read=1002))) + b"\n" + b'{"type": "assist')
    (folder / "s/subagents/agent-x.jsonl").write_bytes(b"\n".join(lines(response("x1", read=40_000))) + b"\n")
    assert follower.read(NOW)
    assert follower.snap.context == 1004, "the main thread's latest, not the subagent's"
    assert len(follower.snap.turns) == 3, "the subagent's turn counts in the chart"
    assert follower.files[main][0] < main.stat().st_size, "a half-written line waits for the next read"


@pytest.mark.parametrize("size, colour, desc", [
    (100_000, transcript_usage.OK, "100k is green"),
    (149_999, transcript_usage.OK, "green below 150k"),
    (150_000, transcript_usage.WARN, "yellow from 150k"),
    (299_999, transcript_usage.WARN, "yellow below 300k"),
    (300_000, transcript_usage.AMBER, "amber from 300k"),
    (599_999, transcript_usage.AMBER, "amber below 600k"),
    (600_000, transcript_usage.HOT, "red from 600k"),
    (1_200_000, transcript_usage.HOT, "red above"),
])
def test_context_grades(size, colour, desc):
    """Doug's thresholds (#51), for the stats pane's gauge and the session list's bar."""
    assert transcript_usage.grade(size) == colour, desc


@pytest.mark.parametrize("size, char, desc", [
    (0, " ", "nothing yet"),
    (1, "▁", "any context shows"),
    (125_000, "▁", "125k: the first eighth, full"),
    (125_001, "▂", "past 125k: the second eighth begun"),
    (250_000, "▂", "250k"),
    (500_000, "▄", "500k: half the cell"),
    (600_000, "▅", "600k: into the fifth eighth"),
    (875_001, "█", "past 875k: the last eighth begun"),
    (1_000_000, "█", "1M fills the cell"),
    (1_500_000, "█", "past 1M stays full"),
])
def test_the_context_bar(size, char, desc):
    """Doug (#51): one cell, eight levels, linear at 125k an eighth, in its grade's colour."""
    bar = transcript_usage.context_bar(size)
    assert bar.plain == char, desc
    assert Style.parse(bar.style).color.get_truecolor() == transcript_usage.grade(size), desc


@pytest.mark.parametrize("n, shown, desc", [
    (0, "0", "none"),
    (999, "999", "under a thousand, as it is"),
    (999.7, "999", "a fraction dropped, not rounded up to 1000"),
    (1_000, "1k", "a thousand"),
    (999_499, "999k", "just under the rollover"),
    (999_600, "1M", "999.6k rolls over to 1M, not 1000k"),
    (1_000_000, "1M", "a million, no .0"),
    (1_234_567, "1.2M", "millions to one place"),
    (99_940_000, "99.9M", "one place up to 100M"),
    (99_960_000, "100M", "rounding to 100M drops the place"),
    (123_456_789, "123M", "three figures above 100M, so five characters at most"),
])
def test_tokens_rounds_to_three_figures(n, shown, desc):
    assert transcript_usage.tokens(n) == shown, desc


def test_a_synthetic_reply_leaves_the_cache_warm_as_it_was(tmp_path):
    follower = transcript_usage.UsageFollower("s")
    follower.take(*transcript_usage.parse(lines(response("m1", ago=3000, made=900), synthetic("m2", ago=10)), set(), main=True))
    assert follower.snap.model == "claude-opus-5-5" and follower.snap.last_at == NOW - 3000


@pytest.mark.parametrize("recs, turns, context, compactions, desc", [
    ([compacted(ago=9000, pre=300_000), response("m1", ago=8000, made=500), response("m2", ago=600, read=40_000)],
     ["m2"], 40_002, 1, "only the span is parsed; a compaction before it still counts"),
    ([response("m1", ago=9000, made=700), response("m2", ago=8000, made=900)],
     ["m2"], 902, 0, "nothing in the span: the latest main response still gives the context"),
    ([response("m1", ago=8000, made=900), response("s1", ago=600, read=7, isSidechain=True)],
     ["m1", "s1"], 902, 0, "a sidechain in the span doesn't stop the scan short of the main thread's latest"),
    ([response("m1", ago=600, read=40_000), synthetic("m2", ago=60)],
     ["m1"], 40_002, 0, "a synthetic reply isn't the latest response"),
    ([], [], 0, 0, "an empty transcript"),
])
@pytest.mark.parametrize("chunk", [64, 4 << 20])
def test_a_first_read_starts_at_the_span(tmp_path, monkeypatch, recs, turns, context, compactions, desc, chunk):
    monkeypatch.setattr(transcript_usage, "CHUNK", chunk)   # small chunks cut lines at every boundary
    folder = tmp_path / "-home-u-repo"
    folder.mkdir()
    write(folder / "s.jsonl", *recs)
    follower = transcript_usage.UsageFollower("s", tmp_path)
    follower.read(NOW)
    parsed = sorted(follower.seen)
    assert follower.snap.context == context and len(follower.snap.compactions) == compactions, desc
    assert parsed == sorted(turns), f"{desc}: parsed {parsed}"
    assert follower.ready


def test_finished_subagents_are_not_looked_at_again(tmp_path):
    folder = tmp_path / "-home-u-repo"
    (folder / "s/subagents").mkdir(parents=True)
    write(folder / "s.jsonl", response("m1", made=1000))
    old = folder / "s/subagents/agent-old.jsonl"
    write(old, response("o1", read=5, ago=9000))
    os.utime(old, (NOW - 9000, NOW - 9000))
    follower = transcript_usage.UsageFollower("s", tmp_path)
    follower.read(NOW)
    assert old not in follower.paths(NOW), "finished before the span: not stat-ed every second"
    new = folder / "s/subagents/agent-new.jsonl"
    write(new, response("n1", read=40_000))
    assert new in follower.paths(NOW), "a new subagent is found when the folder changes"
    assert old in follower.paths(NOW + transcript_usage.REGLOB), "and every file is looked at again now and then"


@pytest.mark.parametrize("cutoffs, total, done, desc", [
    ([], 0, False, "the first read: the span alone, the rest left unread"),
    ([4 * H], 52, False, "4h: back to the first prompt older than that"),
    ([6 * H], 354, False, "6h: a response written as two lines counts once"),
    ([5 * H + 0.5], 354, False, "a response whose lines straddle the cutoff is taken whole"),
    ([5 * H + 0.5, 4 * D], 1365, True, "and not again by the next load"),
    ([1 * D], 354, False, "1d: the subagent finished before it isn't read"),
    ([4 * H, 6 * H, 4 * D], 1365, True, "a level at a time: nothing counted twice"),
    ([4 * D], 1365, True, "read to the start: the skipped subagent with it, and everything is in"),
    ([float("inf")], 1365, True, "everything"),
])
@pytest.mark.parametrize("chunk", [64, 4 << 20])
def test_zooming_out_reads_the_history_back(tmp_path, monkeypatch, cutoffs, total, done, desc, chunk):
    monkeypatch.setattr(transcript_usage, "CHUNK", chunk)   # small chunks cut lines at every boundary
    folder = tmp_path / "-home-u-repo"
    (folder / "s/subagents").mkdir(parents=True)
    write(folder / "s.jsonl", *HISTORY)
    old = folder / "s/subagents/agent-old.jsonl"
    write(old, response("o1", ago=2 * D, read=7, isSidechain=True))
    os.utime(old, (NOW - 2 * D, NOW - 2 * D))
    follower = transcript_usage.UsageFollower("s", tmp_path)
    follower.read(NOW)
    for ago in cutoffs:
        follower.load(NOW - ago)
    history = follower.snap.history
    assert sum(sum(sums[:3]) for sums in history.values()) == total, desc
    assert all(b % transcript_usage.BUCKET == 0 for b in history), f"{desc}: kept as {transcript_usage.BUCKET}s sums"
    assert [t.context for t in follower.snap.turns] == [40_002], f"{desc}: the span's turns, as turns"
    assert follower.done == done and follower.short(NOW - 4 * D) != done, desc
    assert (follower.oldest == NOW - 3 * D) == done, f"{desc}: the oldest turn known once everything is read"


def test_a_transcript_all_in_the_span_still_reads_its_skipped_subagents(tmp_path):
    folder = tmp_path / "-home-u-repo"
    (folder / "s/subagents").mkdir(parents=True)
    write(folder / "s.jsonl", response("m4", ago=60, read=40_000))
    old = folder / "s/subagents/agent-old.jsonl"
    write(old, response("o1", ago=2 * D, read=7, isSidechain=True))
    os.utime(old, (NOW - 2 * D, NOW - 2 * D))
    follower = transcript_usage.UsageFollower("s", tmp_path)
    follower.read(NOW)
    assert not follower.done and follower.short(NOW - 4 * H), "the subagent finished before the span is still to read"
    follower.load(NOW - 4 * H)
    assert follower.done and sum(sum(x[:3]) for x in follower.snap.history.values()) == 9 and \
        follower.oldest == NOW - 2 * D, "read whole, so - can dim"


@pytest.mark.parametrize("env, expected, desc", [
    ({"WT_SESSION": "x", "TERM": "xterm-256color"}, "truecolor", "Windows Terminal says nothing of 24-bit colour: assume it"),
    ({"WT_SESSION": "x", "COLORTERM": "truecolor"}, None, "a terminal that says so needs nothing"),
    ({"WT_SESSION": "x", "TEXTUAL_COLOR_SYSTEM": "256"}, "256", "the person's own setting stands"),
    ({"TERM": "xterm-256color"}, None, "another terminal is left to Rich's detection"),
])
def test_windows_terminal_gets_truecolour(env, expected, desc):
    """The shimmer's colours step by 3 to 5 levels a frame in 24-bit colour; at 256 colours
    each cell jumps 40 to 95 levels between two to four colours (#50)."""
    from claude_wheelhouse import cli
    cli.truecolour(env)
    assert env.get("TEXTUAL_COLOR_SYSTEM") == expected, desc


@pytest.mark.parametrize("s, h, context, estimate, desc", [
    (compacted_snap(), host(), 90_000, True, "compacted since the last response: the host's count, as an estimate"),
    (compacted_snap(), None, 350_000, False, "a tab session has no host count: the transcript's size"),
    (compacted_snap(), host(ago=45), 350_000, False, "the host's count is from before the compaction"),
    (compacted_snap(compacted_ago=90), host(), 350_000, False,
     "a response since the compaction sized it exactly: the host's count isn't needed"),
    (snap(350_000, 600), host(), 350_000, False, "never compacted: the transcript's size"),
])
def test_the_hosts_count_stands_in_after_a_compaction(s, h, context, estimate, desc):
    """Doug (#54): the context size straight after a compact, not at the next response."""
    shown = transcript_usage.current(s, h)
    assert (shown.context, shown.estimate) == (context, estimate), desc
    assert s.context == 350_000 and not s.estimate, f"{desc}: the follower's snapshot is left as it was"


def test_a_compaction_is_sized_by_the_next_response(tmp_path):
    folder = tmp_path / "-home-u-repo"
    folder.mkdir()
    write(folder / "s.jsonl", compacted(ago=9000, pre=300_000), response("m0", ago=8500, made=500),
          response("m1", ago=700, read=200_000), compacted(ago=600, pre=200_002),
          response("m2", ago=500, read=50_000), response("m3", ago=400, read=60_000))
    follower = transcript_usage.UsageFollower("s", tmp_path)
    follower.read(NOW)
    assert [c.after for c in follower.snap.compactions] == [0, 50_002], \
        "the first response after it, never a later one; one before the span is left unsized"


@pytest.mark.parametrize("held, shared, desc", [
    (True, True, "while one user holds it, the other gets the same follower (#51)"),
    (False, False, "once every user has dropped it, a fresh one"),
])
def test_followers_are_shared(held, shared, desc):
    import gc
    first = transcript_usage.follower("shared-sid")
    first.marker = True
    if not held:
        del first
        gc.collect()
    assert getattr(transcript_usage.follower("shared-sid"), "marker", False) == shared, desc


# model and effort (.plan/model-effort.md)

@pytest.mark.parametrize("model, window, desc", [
    ("claude-opus-5-5", 1_000_000, "Opus"),
    ("claude-sonnet-5-5", 1_000_000, "Sonnet"),
    ("claude-haiku-5-5", 1_000_000, "Haiku 5.5 has 1M by default"),
    ("claude-fable-5-1", 1_000_000, "as has Fable 5.1"),
    ("claude-haiku-4-5", 200_000, "older Haiku: 200k"),
    (None, 200_000, "none logged yet"),
])
def test_max_window(model, window, desc):
    assert transcript_usage.max_window(model) == window, desc


def test_the_transcript_says_what_model_each_turn_asked_for():
    recs = [response("m1", ago=90, read=5) | {"requestedModel": "claude-opus-5-5[1m]"},
            response("m2", ago=60, read=5)]
    turns, _ = transcript_usage.parse(lines(*recs), set(), True)
    assert [t.requested for t in turns] == ["claude-opus-5-5[1m]", None]
    s = Snapshot()
    transcript_usage.take(s, turns, [])
    assert s.requested == "claude-opus-5-5[1m]", "the latest turn that says, a turn that doesn't leaves it"


def test_the_transcript_says_what_effort_each_turn_ran_at():
    recs = [response("m1", ago=90, read=5) | {"effort": "low"}, response("m2", ago=60, read=5) | {"effort": "xhigh"},
            response("m3", ago=30, read=5)]
    turns, _ = transcript_usage.parse(lines(*recs), set(), True)
    assert [t.effort for t in turns] == ["low", "xhigh", None]
    s = Snapshot()
    transcript_usage.take(s, turns, [])
    assert s.effort == "xhigh", "the latest turn that says, a turn that doesn't leaves it"


@pytest.mark.parametrize("recs, ran, desc", [
    ([response("m1", ago=90, read=5), command("model", "haiku", "Set model to `Haiku 5.5` for this session only")],
     ("claude-haiku-5-5", None), "a /model typed with no turn after it: the model it set"),
    ([command("model", "opus[1m]", "Set model to `Opus 5.5 (1M context)` for this session only")],
     ("claude-opus-5-5", None), "a display name with its window: the table's id"),
    ([command("model", "default", "Set model to `Opus 5.5 (default)` for this session only")],
     ("claude-opus-5-5", None), "the default, by the name it resolved to"),
    ([command("effort", "medium", "Set effort level to medium (this session only): Balanced approach")],
     (None, "medium"), "an /effort typed"),
    ([command("model", "bogus", "Model 'bogus' not found")], (None, None), "one Claude Code refused: nothing"),
    ([command("effort", "", "Usage: /effort <low|medium|high|xhigh|max|auto>")], (None, None), "a usage line: nothing"),
    ([command("effort", "auto", "Set effort level to auto (this session only)")], (None, None), "a level off the table"),
    ([command("model", "haiku", "Set model to `Haiku 5.5` for this session only", ago=90),
      response("m1", ago=60, read=5) | {"requestedModel": "claude-sonnet-5-5"}],
     ("claude-sonnet-5-5", None), "a turn after the /model: the turn's"),
])
def test_a_model_or_effort_typed_counts_before_the_next_turn(tmp_path, recs, ran, desc):
    """Material: a /model's reply is Claude Code's own, with no usage, so no turn says it."""
    (tmp_path / "p").mkdir()
    (tmp_path / "p" / "s.jsonl").write_bytes(b"\n".join(lines(*recs)) + b"\n")
    follower = transcript_usage.UsageFollower("s", tmp_path)
    follower.read(NOW)
    assert (follower.snap.requested, follower.snap.effort) == ran, desc
    served = "claude-opus-5-5" if any(r["type"] == "assistant" for r in recs) else None
    latest = tuple(x and x[1] for x in (transcript_usage.latest(follower.snap, "model"), transcript_usage.latest(follower.snap, "effort")))
    assert latest == (ran[0] or served, ran[1]), f"{desc}: as a tab's is carried (the served model where nothing asked)"


@pytest.mark.parametrize("recs, typed, desc", [
    (picked("Set model to `Opus 5.5 (1M context)` and saved as your default for new sessions"),
     {"model": "claude-opus-5-5"}, "a model picked in /model's picker"),
    (picked("Set model to `Haiku 5.5`"), {"model": "claude-haiku-5-5"}, "the shorter user record"),
    (picked("Set effort level to low (this session only)"), {"effort": "low"}, "an /effort as a user record"),
    ([command("model", "haiku", "Set model to `Haiku 5.5` for this session only")], {"model": "claude-haiku-5-5"},
     "the system record still counts"),
    ([command("effort", "medium", "Set effort level to medium (this session only): Balanced approach")],
     {"effort": "medium"}, "an /effort's system record too"),
    (picked("Set model to `claude-sonnet-5-5`"), {"model": "claude-sonnet-5-5"}, "/model given an id"),
    (picked("Set model to `claude-opus-5-5[1m]`"), {"model": "claude-opus-5-5"}, "an id with its window"),
    (picked("Set model to `haiku (claude-haiku-5-5)`"), {"model": "claude-haiku-5-5"},
     "an alias, with its id in brackets"),
    (picked("Set model to `sonnet (claude-sonnet-5-5[1m])`"), {"model": "claude-sonnet-5-5"},
     "an alias, with its id and window in brackets"),
    (picked("Set model to `Sonnet 5.5[1m]`"), {"model": "claude-sonnet-5-5"}, "a display name with its window"),
    (picked("Set model to `Fable 5.1`"), {"model": "claude-fable-5-1"}, "Fable, by its display name"),
    ([command("model", "claude-sonnet-5-5", "Set model to `claude-sonnet-5-5` for this session only")],
     {"model": "claude-sonnet-5-5"}, "an id in the system record"),
    (picked("Set model to `Haiku 5.5`", isSidechain=True), {}, "a subagent's: not the session's"),
    (picked("Set model to `Sonnet 1`"), {}, "a model off the table: nothing"),
    (picked("Set model to `Sonnet 5`"), {}, "an older model's display name: nothing"),
    (picked("Set model to `claude-haiku-4-5`"), {}, "an id off the table: nothing"),
    (picked("Set model to `opus (claude-opus-4-1)`"), {}, "an alias for an id off the table: nothing"),
    (picked("Set model to `auto`"), {}, "auto: nothing"),
    ([{"type": "user", "timestamp": at(30), "message": {"role": "user", "content":
       "why does it say <local-command-stdout>Set model to `Haiku 5.5`</local-command-stdout>?"}}], {},
     "a prompt quoting one: nothing"),
])
def test_every_shape_of_a_model_or_effort_taken_counts(tmp_path, recs, typed, desc):
    """Round 64, blocker 1: both shapes Claude Code 2.1 writes, by the hub's reads and by a
    host's whole-transcript scan alike."""
    (tmp_path / "p").mkdir()
    path = tmp_path / "p" / "s.jsonl"
    path.write_bytes(b"\n".join(lines(*recs)) + b"\n")
    follower = transcript_usage.UsageFollower("s", tmp_path)
    follower.read(NOW)
    for how, snap in (("read", follower.snap), ("typed_in", transcript_usage.typed_in(path))):
        assert {k: v for k, (_, v) in snap.typed.items()} == typed, f"{desc}: {how}"


def test_what_was_typed_before_the_span_counts(tmp_path):
    """Round 64, M4: a first read starts at the span, but a /model typed hours before (then
    turns the span starts after) is still taken, as a host would take it."""
    recs = [response("m0", ago=5 * 3600, read=5), *picked("Set model to `Haiku 5.5`", ago=4 * 3600),
            response("m1", ago=3 * 3600, read=5), response("m2", ago=60, read=5)]
    (tmp_path / "p").mkdir()
    (tmp_path / "p" / "s.jsonl").write_bytes(b"\n".join(lines(*recs)) + b"\n")
    follower = transcript_usage.UsageFollower("s", tmp_path)
    follower.read(NOW)
    assert follower.files[follower.main][0] > 0 and len(follower.snap.turns) == 1, "the read began at the span"
    assert transcript_usage.latest(follower.snap, "model", turns=False) == (NOW - 4 * 3600, "claude-haiku-5-5")


def test_a_turn_begun_before_the_span_is_dated_by_its_prompt(store, tmp_path):
    """Round 66, bug 2: a /model typed mid-turn before the span beats that turn's model,
    though the hub's first read starts at the turn's next response, in the span."""
    sid = store.create_session(str(tmp_path), runner="tab", model="claude-opus-5-5", effort="high")
    store.db.execute("UPDATE sessions SET model_chosen_at = ? WHERE id = ?",
                     (datetime.fromtimestamp(NOW - 10_000, timezone.utc).isoformat(), sid))
    recs = [prompt("go", 9000), response("m1", ago=8900, read=5),
            command("model", "haiku", "Set model to `Haiku 5.5` for this session only", ago=8000),
            response("m2", ago=600, read=5)]
    (tmp_path / "p").mkdir()
    (tmp_path / "p" / f"{sid}.jsonl").write_bytes(b"\n".join(lines(*recs)) + b"\n")
    follower = transcript_usage.UsageFollower(sid, tmp_path)
    follower.read(NOW)
    assert len(follower.snap.turns) == 1, "the read began at the span"
    assert follower.snap.begun == NOW - 9000, "the turn under way began at its prompt"
    transcript_usage.carry(store, sid, follower.snap)
    assert store.session(sid)["model"] == "claude-haiku-5-5"
