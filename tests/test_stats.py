import json
import os
import re
import time
from datetime import datetime, timezone

import pytest
from rich.style import Style

from claude_wheelhouse import api, models, plan_usage, stats, transcript
from claude_wheelhouse.stats import Snapshot, Turn


NOW = float(int(time.time()))   # near the files' real modification times


def at(seconds_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(NOW - seconds_ago))


def response(mid, ago=60, inp=2, made=0, read=0, ttl="1h", **over):
    cc = {"ephemeral_1h_input_tokens": made if ttl == "1h" else 0,
          "ephemeral_5m_input_tokens": made if ttl == "5m" else 0}
    return {"type": "assistant", "timestamp": at(ago), "requestId": f"req-{mid}",
            "message": {"id": mid, "model": "claude-opus-5-5", "content": [],
                        "usage": {"input_tokens": inp, "cache_creation_input_tokens": made,
                                  "cache_read_input_tokens": read, "output_tokens": 10, "cache_creation": cc}}} | over


def compacted(ago=60, pre=200_000, post=11_000):
    return {"type": "system", "subtype": "compact_boundary", "timestamp": at(ago),
            "compactMetadata": {"trigger": "manual", "preTokens": pre, "postTokens": post}}


def synthetic(mid, ago=30):
    rec = response(mid, ago=ago, inp=0)
    rec["message"]["model"] = "<synthetic>"
    return rec


def lines(*recs) -> list[bytes]:
    return [json.dumps(r).encode() for r in recs]


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
    follower = stats.UsageFollower("s")
    parsed = stats.parse(lines(*recs), follower.seen, main=True)
    follower.take(*parsed)
    snap = follower.snap
    assert (snap.context, snap.ttl, len(snap.turns), len(snap.compactions)) == (context, ttl, turns, compactions), desc


@pytest.mark.parametrize("read, fresh, new, miss, desc", [
    (80_000, 900, 900, 0, "on top of a cache read, fresh tokens are new input"),
    (0, 80_900, 0, 80_900, "with nothing read, they're a cache miss"),
])
def test_assembly_splits_new_from_miss(read, fresh, new, miss, desc):
    [turn], _ = stats.parse(lines(response("m1", inp=0, made=fresh, read=read)), set(), main=True)
    assert (turn.read, turn.new, turn.miss) == (read, new, miss), desc


def test_the_follower_reads_only_whats_appended_and_finds_subagents(tmp_path):
    folder = tmp_path / "-home-u-repo"
    (folder / "s" / "subagents").mkdir(parents=True)
    main = folder / "s.jsonl"
    main.write_bytes(b"\n".join(lines(response("m1", made=1000))) + b"\n")
    follower = stats.UsageFollower("s", tmp_path)
    assert follower.read(NOW) and follower.snap.context == 1002
    assert not follower.read(NOW), "nothing new: nothing read"
    with open(main, "ab") as f:
        f.write(b"\n".join(lines(response("m2", ago=30, read=1002))) + b"\n" + b'{"type": "assist')
    (folder / "s/subagents/agent-x.jsonl").write_bytes(b"\n".join(lines(response("x1", read=40_000))) + b"\n")
    assert follower.read(NOW)
    assert follower.snap.context == 1004, "the main thread's latest, not the subagent's"
    assert len(follower.snap.turns) == 3, "the subagent's turn counts in the chart"
    assert follower.files[main][0] < main.stat().st_size, "a half-written line waits for the next read"


EVEN = 1791460800 + 7200 - 1791460800 % 7200   # on a multiple of every bucket width below


@pytest.mark.parametrize("columns, ago, index, desc", [
    (60, -119, 59, "the current bucket runs on past now, to its edge"),
    (60, 0, 59, "now is in the last, which starts on an edge here"),
    (60, 1, 58, "a second before that edge is the one before"),
    (60, 59 * 120, 0, "the oldest bucket starts 59 widths back"),
    (60, 59 * 120 + 1, None, "and anything older is left out"),
    (60, 3600, 29, "an hour ago, at 60 columns of 2 minutes"),
    (80, 3600, 39, "and at 80 columns of 90 seconds"),
])
def test_buckets_cover_fixed_clock_intervals(columns, ago, index, desc):
    turn = Turn(EVEN - ago, 5, 1, 0, 6, None, None, True)
    now = max(EVEN, turn.at)
    bs = stats.buckets([turn], now, columns)
    assert len(bs) == columns, desc
    assert [i for i, b in enumerate(bs) if b["read"]] == ([] if index is None else [index]), desc


@pytest.mark.parametrize("columns, ago, desc", [
    (60, 0, "a turn at the start of the current bucket"),
    (60, 50, "one partway through an older bucket"),
    (80, 89, "one at the end of a bucket, at 90-second buckets"),
    (113, 7000, "one near the far end, at a width that isn't a whole second"),
])
def test_a_turns_bucket_holds_still_as_now_advances(columns, ago, desc):
    """The bars cover fixed stretches of clock: until now crosses into the next bucket,
    a turn stays in the same one, which is what stops the chart breathing (#63)."""
    start, width = stats.window(EVEN, columns)
    edge = start + (columns - 1) * width   # where the current bucket starts
    turn = Turn(edge - ago, 5, 1, 0, 6, None, None, True)
    seen = set()
    for f in (0, 0.25, 0.5, 0.99):
        bs = stats.buckets([turn], edge + f * width, columns)
        seen.add(tuple(i for i, b in enumerate(bs) if b["read"]))
        assert stats.window(edge + f * width, columns)[0] == start, desc
    assert len(seen) == 1 and len(next(iter(seen))) == 1, f"{desc}: {seen}"
    bs = stats.buckets([turn], edge + width, columns)
    assert [i for i, b in enumerate(bs) if b["read"]] == [next(iter(seen))[0] - 1], f"{desc}: one step left after"


@pytest.mark.parametrize("tallest, maxt, desc", [
    (0, 0, "an empty chart has no scale"),
    (1, 1, "one is a step"),
    (7, 10, "seven rounds to ten"),
    (10, 10, "ten is a step"),
    (11, 20, "past ten, twenty"),
    (20, 20, "twenty is a step"),
    (21, 50, "past twenty, fifty"),
    (51, 100, "past fifty, a hundred"),
    (49_000, 50_000, "a large chart, likewise"),
    (180_001, 200_000, "and past a hundred thousand"),
])
def test_the_y_axis_steps_rather_than_tracks_the_tallest_bar(tallest, maxt, desc):
    turn = Turn(EVEN - 60, tallest, 0, 0, 0, None, None, True)
    assert stats.chart([turn], EVEN, width=56, height=4).maxt == maxt, desc


def test_the_chart_is_as_wide_as_the_pane():
    c = stats.chart([Turn(NOW - 60, 5, 1, 0, 6, None, None, True)], NOW, width=56, height=6)
    assert len(c.columns) == 56 - stats.MARGIN
    assert all(t.cell_len <= 56 for t in stats.chart_lines(c, frame=3))


def snap(context, expires_in, ttl="1h"):
    return Snapshot(model="claude-opus-5-5", context=context, peak=context, last_at=NOW + expires_in - stats.TTL[ttl],
                    ttl=ttl, turns=[Turn(NOW - 60, context, 0, 0, context, ttl, None, True)])


def test_totals_across_sessions():
    snaps = {"a": snap(83_000, 600), "b": snap(40_000, 120), "c": snap(10_000, -60)}
    t = stats.combine(snaps, {"a": "alpha", "b": "beta", "c": "gamma"}, NOW)
    assert (t.sessions, t.context, t.warm) == (3, 133_000, 2)
    assert t.next_cold == (NOW + 120, "beta"), "the warm cache that goes cold first"
    assert len(t.turns) == 3


@pytest.mark.parametrize("s, expected, desc", [
    (snap(83_000, 600), None, "warm: no cost line"),
    (snap(83_000, -60), "next turn re-pays ~83k (~166k effective)", "a cold 1h cache re-pays twice over"),
    (snap(80_000, -60, ttl="5m"), "next turn re-pays ~80k (~100k effective)", "a cold 5m one at 1.25"),
])
def test_cold_cost_line(s, expected, desc):
    got = stats.cache_lines(s, NOW)
    assert (got[1] if len(got) > 1 else None) == expected, desc
    first = f"1h · warm until {stats.clock(NOW + 600)} (10m)" if expected is None else \
        f"{s.ttl} · cold since {stats.clock(NOW - 60)}"
    assert got[0] == first, desc


@pytest.mark.parametrize("size, colour, desc", [
    (100_000, stats.OK, "100k is green"),
    (149_999, stats.OK, "green below 150k"),
    (150_000, stats.WARN, "yellow from 150k"),
    (299_999, stats.WARN, "yellow below 300k"),
    (300_000, stats.AMBER, "amber from 300k"),
    (599_999, stats.AMBER, "amber below 600k"),
    (600_000, stats.HOT, "red from 600k"),
    (1_200_000, stats.HOT, "red above"),
])
def test_context_grades(size, colour, desc):
    """Doug's thresholds (#51), for the stats pane's gauge and the session list's bar."""
    assert stats.grade(size) == colour, desc


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
    bar = stats.context_bar(size)
    assert bar.plain == char, desc
    assert Style.parse(bar.style).color.get_truecolor() == stats.grade(size), desc


@pytest.mark.parametrize("model, name, desc", [
    ("claude-opus-5-5", "Opus 5.5", "major and minor"),
    ("claude-sonnet-5", "Sonnet 5", "major only"),
    (None, "model unknown", "none logged yet"),
])
def test_model_names(model, name, desc):
    assert stats.model_name(model) == name, desc


# account usage

SOON = NOW + 2 * 3600


def usage(ago=0, **pct) -> plan_usage.AccountUsage:
    u = plan_usage.AccountUsage()
    u.limits = {kind: (p, SOON) for kind, p in pct.items()}
    u.read = dict.fromkeys(pct, NOW - ago)
    return u


@pytest.mark.parametrize("view, u, width, expect, desc", [
    (None, usage(session=23, weekly_all=5), 50, ["session", "23%", "weekly", "5%", "resets"], "both bars at 50 columns"),
    (None, usage(session=23, weekly_all=5), 80, ["session", "23%", "weekly", "5%"], "both bars at 80 columns"),
    (None, usage(session=97), 50, ["97%"], "one limit only"),
    (None, plan_usage.AccountUsage(), 50, ["usage loading…"], "before the first reading"),
    (None, usage(session=23, weekly_all=5, weekly_fable=40), 50, ["fable", "40%"], "Fable's week once it's used"),
    (None, usage(ago=12 * 60, session=23), 50, ["23%", "usage read 12m ago"], "a reading older than a poll says its age"),
    (snap(83_000, 600), usage(session=23), 50, ["context", "8%", "83k/1M", "session", "23%"], "a session's context too"),
])
def test_gauges(view, u, width, expect, desc):
    rows = stats.gauges(view, u, NOW, width)
    text = "\n".join(r.plain for r in rows)
    assert all(e in text for e in expect), desc
    assert all(len(r.plain) <= width for r in rows), f"{desc}: fits the pane"
    bars = [r.plain.index("%") for r in rows if "█" in r.plain or "░" in r.plain]
    assert len(set(bars)) <= 1, f"{desc}: the bars line up"


@pytest.mark.parametrize("u, unwanted, desc", [
    (usage(session=23, weekly_all=5, weekly_fable=0), "fable", "Fable's week unused: no gauge"),
    (usage(ago=60, session=23), "ago", "a reading newer than a poll: no age"),
])
def test_gauges_leave_out(u, unwanted, desc):
    assert unwanted not in "\n".join(r.plain for r in stats.gauges(None, u, NOW, 50)), desc


def test_usage_unavailable_after_a_failed_reading():
    u = plan_usage.AccountUsage()
    u.failed = True
    assert [r.plain for r in stats.gauges(None, u, NOW, 50)] == ["usage unavailable"]


@pytest.mark.parametrize("second, shown, desc", [
    (0, stats.HOT, "on even seconds a flashing gauge is lit"),
    (1, tuple(int(c * 0.22) for c in stats.HOT), "on odd ones it's dark, as in the dashboard"),
])
def test_flashing(second, shown, desc):
    now = NOW - NOW % 2 + second   # an offset, not the time, in the test's id: every xdist worker collects the same
    assert stats.flash(stats.HOT, True, now) == shown, desc
    assert stats.flash(stats.HOT, False, now) == stats.HOT, "a steady colour never flashes"


def test_the_panel_holds_the_gauges_then_the_stats():
    """Doug's layout (#43, #50): a blank line, the three gauges with a blank line under
    each, then the cache and compaction rows, all inside one border the width of the pane."""
    s = snap(83_000, 600)
    s.compactions = [stats.Compaction(NOW - 600, "manual", 201_000, 11_000)]
    rows, _ = stats.layout(s, "holly", None, usage(session=23, weekly_all=5), NOW, 56, 40)
    assert {r.cell_len for r in rows} == {56}, "every row is the pane's width"
    assert rows[0].plain.startswith("╭─ holly · Opus 5.5 · 1M window ") and rows[-1].plain.startswith("╰")
    inside = [r.plain[1:-1].split(" ")[0] or "-" for r in rows[1:-1]]
    assert inside == ["-", "context", "-", "session", "-", "weekly", "-", "cache", "compact"]


@pytest.mark.parametrize("height, kinds, bars, desc", [
    (60, ["assembly", "output"], 20, "a tall pane: each chart 40% of the one before"),
    (30, ["assembly", "output"], 7, "a middling pane: as tall as fits, under 40%"),
    (22, ["assembly", "output"], 3, "a short pane: both, short"),
    (17, ["assembly"], 3, "too short for two: context assembly alone"),
    (14, [], None, "too short for either"),
])
def test_charts_fit_under_the_panel(height, kinds, bars, desc):
    rows, shown = stats.layout(snap(83_000, 600), "holly", None, usage(session=23, weekly_all=5), NOW, 56, height)
    assert [c.kind for c in shown] == kinds, desc
    assert all(c.height == bars for c in shown), f"{desc}: {[c.height for c in shown]}"
    drawn = stats.render(rows, shown, 0).plain.split("\n")
    assert len(drawn) <= height, f"{desc}: {len(drawn)} rows in {height}"


def test_the_output_chart_stacks_output_tokens():
    [turn], _ = stats.parse(lines(response("m1", read=5)), set(), main=True)
    assert turn.output == 10
    c = stats.chart([turn], NOW, width=56, height=4, kind="output")
    assert c.maxt == 10 and any(base == stats.CO["output"] for col in c.columns for base, _ in col)
    assert stats.chart_lines(c, 0, ticks=False)[-1].plain.strip().startswith("0 └"), "no hour ticks: they go under the last"


@pytest.mark.parametrize("ago, kind, hour, desc", [
    (60, "assembly", 123, "context assembly: cache read, new and miss, the bars' own series"),
    (60, "output", 7, "output: its output tokens"),
    (3600, "assembly", 123, "a turn exactly an hour ago counts"),
    (3601, "assembly", 0, "a second older doesn't, though the chart still draws it"),
    (-1, "assembly", 0, "nor one after now, which the bars leave out too"),
])
def test_each_chart_sums_its_last_hour(ago, kind, hour, desc):
    turn = Turn(EVEN - ago, 100, 20, 3, 0, None, None, True, output=7)
    assert stats.chart([turn], EVEN, width=56, height=4, kind=kind).hour == hour, desc


@pytest.mark.parametrize("width, tokens, keys, note, desc", [
    (80, 950, ["cache", "new", "miss"], "950 tokens/last hr", "every key, and the count at the right"),
    (80, 0, ["cache", "new", "miss"], "0 tokens/last hr", "none"),
    (80, 340_000, ["cache", "new", "miss"], "340k tokens/last hr", "thousands as k"),
    (80, 1_234_567, ["cache", "new", "miss"], "1.2M tokens/last hr", "millions to one place"),
    (64, 950, ["cache", "new", "miss"], "950 tokens/last hr", "every key, a space apart, at 64"),
    (62, 950, ["cache", "new", "miss"], "950 tokens/last hr", "every key, just"),
    (61, 950, ["new", "miss"], "950 tokens/last hr", "cache is left out first, whole"),
    (56, 950, ["new", "miss"], "950 tokens/last hr", "new and miss at 56"),
    (54, 950, ["new", "miss"], "950 tokens/last hr", "new and miss, just"),
    (53, 950, ["miss"], "950 tokens/last hr", "then new: miss stays longest"),
    (48, 950, ["miss"], "950 tokens/last hr", "miss, just"),
    (47, 950, [], "950 tokens/last hr", "then no key"),
    (40, 12_300_000, [], "12.3M tokens/last hr", "no key at the narrowest that holds the caption and count"),
    (40, 123_400_000, [], "123M tokens/last hr", "100M or more still fits at 40"),
    (40, 999_600, [], "1M tokens/last hr", "999.6k rolls over to 1M"),
])
def test_the_last_hour_is_on_the_caption_row(width, tokens, keys, note, desc):
    c = stats.chart([Turn(NOW - 60, tokens, 0, 0, 0, None, None, True)], NOW, width=width, height=4)
    head = stats.chart_lines(c, 0)[0]
    assert head.plain.startswith("▸ context assembly") and head.plain.endswith(" " + note), desc
    assert head.cell_len == width, f"{desc}: right-aligned, {head.cell_len} cells in {width}"
    assert [k for k in ("cache", "new", "miss") if f"▆ {k}" in head.plain] == keys, desc


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
    assert stats.tokens(n) == shown, desc


@pytest.mark.parametrize("colour, f, rgb, desc", [
    ((52, 224, 150), 1.0, (52, 224, 150), "full brightness is the colour itself"),
    ((52, 224, 150), 0.5, (26, 112, 75), "halved, truncated as the dashboard does"),
    ((255, 88, 96), 0.37, (94, 32, 35), "no rounding to a few dozen shades"),
])
def test_shade_is_the_dashboards(colour, f, rgb, desc):
    assert stats.shade(colour, f).color.triplet == rgb, desc


@pytest.mark.parametrize("pct, colour, flashing, desc", [
    (50, stats.OK, False, "green to 70"), (75, stats.WARN, False, "yellow to 80"),
    (85, stats.AMBER, False, "amber to 90"), (93, stats.HOT, False, "red to 95"),
    (97, stats.HOT, True, "flashing red over 95"),
])
def test_usage_grade(pct, colour, flashing, desc):
    assert stats.usage_grade(pct) == (colour, flashing), desc


def test_a_synthetic_reply_leaves_the_cache_warm_as_it_was(tmp_path):
    follower = stats.UsageFollower("s")
    follower.take(*stats.parse(lines(response("m1", ago=3000, made=900), synthetic("m2", ago=10)), set(), main=True))
    assert follower.snap.model == "claude-opus-5-5" and follower.snap.last_at == NOW - 3000


def write(path, *recs):
    path.write_bytes(b"".join(r + b"\n" for r in lines(*recs)))


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
    monkeypatch.setattr(stats, "CHUNK", chunk)   # small chunks cut lines at every boundary
    folder = tmp_path / "-home-u-repo"
    folder.mkdir()
    write(folder / "s.jsonl", *recs)
    follower = stats.UsageFollower("s", tmp_path)
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
    follower = stats.UsageFollower("s", tmp_path)
    follower.read(NOW)
    assert old not in follower.paths(NOW), "finished before the span: not stat-ed every second"
    new = folder / "s/subagents/agent-new.jsonl"
    write(new, response("n1", read=40_000))
    assert new in follower.paths(NOW), "a new subagent is found when the folder changes"
    assert old in follower.paths(NOW + stats.REGLOB), "and every file is looked at again now and then"


@pytest.mark.parametrize("zone, desc", [
    ("Europe/London", "a whole-hour zone"),
    ("Asia/Kolkata", "a half-hour zone: ticks on its own hours, not UTC's"),
])
def test_hour_ticks_sit_on_local_hours(monkeypatch, zone, desc):
    """At fixed times either side of an hour (not the clock's, which made this pass or fail
    by time of day): every label sits on its own hour, and one that wouldn't fit there at
    the right-hand end is left out rather than moved off it."""
    monkeypatch.setenv("TZ", zone)
    time.tzset()
    try:
        hour = 1791460800   # on a UTC hour, so a half-hour zone's hours fall mid-way
        for minutes_past in (0, 2, 3, 4, 5, 30, 57, 58, 59.5, 60):
            now = hour + minutes_past * 60
            c = stats.chart([], now, width=stats.MARGIN + 120, height=4)
            axis = stats.chart_lines(c, 0)[-1].plain[stats.MARGIN:]
            labels = list(re.finditer(r"(\d+):00", axis))
            assert len(labels) >= 1, f"{desc}, {minutes_past} past: at least one hour labelled"
            for m in labels:
                at = c.start + m.start() / len(c.columns) * c.span
                local = time.localtime(at)
                minutes = local.tm_min + local.tm_sec / 60
                assert min(minutes, 60 - minutes) <= c.span / len(c.columns) / 60 + 0.5, \
                    f"{desc}, {minutes_past} past: {m[0]} at {local.tm_hour}:{local.tm_min:02d}"
                assert int(m[1]) == (local.tm_hour + (1 if local.tm_min >= 30 else 0)) % 24, desc
    finally:
        monkeypatch.undo()
        time.tzset()


@pytest.mark.parametrize("view, note, desc", [
    (None, "couldn't read the transcript: transcript gone", "a failed read with nothing to show"),
    (snap(83_000, 600), "couldn't read the transcript: transcript gone", "a failed read beside what was read before"),
])
def test_a_note_wraps_inside_the_panel(view, note, desc):
    rows, _ = stats.layout(view, "holly", note, usage(session=23), NOW, 40, 30)
    assert {r.cell_len for r in rows} == {40}, f"{desc}: the border holds"
    inside = " ".join(r.plain[1:-1].strip() for r in rows[1:-1])
    assert note in inside, desc


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


def host(tokens=90_000, window=1_000_000, ago=10):
    return api.HostContext(tokens, window, NOW - ago)


def compacted_snap(compacted_ago=30, after=None):
    s = snap(350_000, 600)
    s.last_at = NOW - 60
    s.compactions = [stats.Compaction(NOW - compacted_ago, "manual", 350_000, 40_000, after)]
    return s


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
    shown = stats.current(s, h)
    assert (shown.context, shown.estimate) == (context, estimate), desc
    assert s.context == 350_000 and not s.estimate, f"{desc}: the follower's snapshot is left as it was"


@pytest.mark.parametrize("s, h, expect, desc", [
    (compacted_snap(), host(), ["~90k/1M", "350k → ~90k"], "the host's estimate, in the gauge and as the post size"),
    (compacted_snap(compacted_ago=90, after=52_000), host(), ["350k/1M", "350k → 52k"],
     "the next response's size, once there is one"),
    (compacted_snap(), None, ["350k/1M", "350k · 40k msgs kept"], "no host count: postTokens, as the messages kept"),
])
def test_the_compact_row_and_the_gauge(s, h, expect, desc):
    rows, _ = stats.layout(stats.current(s, h), "holly", None, usage(session=23), NOW, 70, 40)
    text = "\n".join(r.plain for r in rows)
    assert all(e in text for e in expect), f"{desc}: {text}"


def test_a_compaction_is_sized_by_the_next_response(tmp_path):
    folder = tmp_path / "-home-u-repo"
    folder.mkdir()
    write(folder / "s.jsonl", compacted(ago=9000, pre=300_000), response("m0", ago=8500, made=500),
          response("m1", ago=700, read=200_000), compacted(ago=600, pre=200_002),
          response("m2", ago=500, read=50_000), response("m3", ago=400, read=60_000))
    follower = stats.UsageFollower("s", tmp_path)
    follower.read(NOW)
    assert [c.after for c in follower.snap.compactions] == [0, 50_002], \
        "the first response after it, never a later one; one before the span is left unsized"


@pytest.mark.parametrize("held, shared, desc", [
    (True, True, "while one user holds it, the other gets the same follower (#51)"),
    (False, False, "once every user has dropped it, a fresh one"),
])
def test_followers_are_shared(held, shared, desc):
    import gc
    first = stats.follower("shared-sid")
    first.marker = True
    if not held:
        del first
        gc.collect()
    assert getattr(stats.follower("shared-sid"), "marker", False) == shared, desc


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
    assert stats.max_window(model) == window, desc


def test_the_transcript_says_what_model_each_turn_asked_for():
    recs = [response("m1", ago=90, read=5) | {"requestedModel": "claude-opus-5-5[1m]"},
            response("m2", ago=60, read=5)]
    turns, _ = stats.parse(lines(*recs), set(), True)
    assert [t.requested for t in turns] == ["claude-opus-5-5[1m]", None]
    s = Snapshot()
    stats.take(s, turns, [])
    assert s.requested == "claude-opus-5-5[1m]", "the latest turn that says, a turn that doesn't leaves it"


def test_the_transcript_says_what_effort_each_turn_ran_at():
    recs = [response("m1", ago=90, read=5) | {"effort": "low"}, response("m2", ago=60, read=5) | {"effort": "xhigh"},
            response("m3", ago=30, read=5)]
    turns, _ = stats.parse(lines(*recs), set(), True)
    assert [t.effort for t in turns] == ["low", "xhigh", None]
    s = Snapshot()
    stats.take(s, turns, [])
    assert s.effort == "xhigh", "the latest turn that says, a turn that doesn't leaves it"


def command(name, args, out, ago=30):
    """A local command's record, as Claude Code 2.1.296 writes /model and /effort."""
    return {"type": "system", "subtype": "local_command", "timestamp": at(ago), "isMeta": False,
            "content": f"<local-command-stdout>{out}</local-command-stdout>",
            "commandRun": {"command": name, "args": args}}


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
    follower = stats.UsageFollower("s", tmp_path)
    follower.read(NOW)
    assert (follower.snap.requested, follower.snap.effort) == ran, desc
    served = "claude-opus-5-5" if any(r["type"] == "assistant" for r in recs) else None
    latest = tuple(x and x[1] for x in (stats.latest(follower.snap, "model"), stats.latest(follower.snap, "effort")))
    assert latest == (ran[0] or served, ran[1]), f"{desc}: as a tab's is carried (the served model where nothing asked)"


def picked(out, ago=30, **over):
    """A local command's output as Claude Code 2.1.280 writes a choice made in /model's
    picker (real shapes, text scrubbed): a caveat, the command, then a user record holding
    only its output, with no command named."""
    base = {"parentUuid": "p", "isSidechain": False, "promptId": "q", "type": "user", "timestamp": at(ago),
            "userType": "external", "entrypoint": "cli", "cwd": "/repo", "sessionId": "s", "version": "2.1.280"}
    return [base | {"message": {"role": "user", "content": "<local-command-caveat>Caveat: ...</local-command-caveat>"},
                    "isMeta": True, "uuid": "u1"},
            base | {"message": {"role": "user", "content": "<command-name>/model</command-name>\n            "
                                "<command-message>model</command-message>\n            <command-args></command-args>"},
                    "uuid": "u2"},
            base | {"message": {"role": "user", "content": f"<local-command-stdout>{out}</local-command-stdout>"},
                    "uuid": "u3"} | over]


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
    follower = stats.UsageFollower("s", tmp_path)
    follower.read(NOW)
    for how, snap in (("read", follower.snap), ("typed_in", stats.typed_in(path))):
        assert {k: v for k, (_, v) in snap.typed.items()} == typed, f"{desc}: {how}"


def test_what_was_typed_before_the_span_counts(tmp_path):
    """Round 64, M4: a first read starts at the span, but a /model typed hours before (then
    turns the span starts after) is still taken, as a host would take it."""
    recs = [response("m0", ago=5 * 3600, read=5), *picked("Set model to `Haiku 5.5`", ago=4 * 3600),
            response("m1", ago=3 * 3600, read=5), response("m2", ago=60, read=5)]
    (tmp_path / "p").mkdir()
    (tmp_path / "p" / "s.jsonl").write_bytes(b"\n".join(lines(*recs)) + b"\n")
    follower = stats.UsageFollower("s", tmp_path)
    follower.read(NOW)
    assert follower.files[follower.main][0] > 0 and len(follower.snap.turns) == 1, "the read began at the span"
    assert stats.latest(follower.snap, "model", turns=False) == (NOW - 4 * 3600, "claude-haiku-5-5")


def prompt(text, ago):
    return {"type": "user", "timestamp": at(ago), "message": {"role": "user", "content": text}}


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
    follower = stats.UsageFollower(sid, tmp_path)
    follower.read(NOW)
    assert len(follower.snap.turns) == 1, "the read began at the span"
    assert follower.snap.begun == NOW - 9000, "the turn under way began at its prompt"
    stats.carry(store, sid, follower.snap)
    assert store.session(sid)["model"] == "claude-haiku-5-5"


@pytest.mark.parametrize("setting, effort, room, expected, desc", [
    (None, "low", 0, "holly · Opus 5.5 · low · 1M window", "a tab's: what its transcript ran"),
    (None, None, 0, "holly · Opus 5.5 · 1M window", "no effort recorded yet"),
    ({"model": "claude-haiku-5-5", "effort": "xhigh"}, "low", 0, "holly · Haiku 5.5 · xhigh · 1M window",
     "a hosted session's: what it is set to"),
    ({"model": None, "effort": None}, "medium", 0, "holly · Opus 5.5 · medium · 1M window", "no columns: the transcript's"),
    (None, "xhigh", 31, "holly · Opus 5.5 · xhigh · 1M", "no room for the word window"),
    (None, "low", 40, "holly · Opus 5.5 · low · 1M window", "room enough"),
    (None, "low", 24, "holly · Opus 5.5 · low", "less: the window goes"),
    (None, "low", 14, "Opus 5.5 · low", "less still: the name goes"),
    (None, "low", 11, "Opus 5.5", "the model is the last to go"),
    (None, None, 0, "holly · Opus 5.5 · 1M window", "no effort recorded yet"),
])
def test_the_title_says_model_and_effort(setting, effort, room, expected, desc):
    s = snap(83_000, 600)
    s.model, s.effort = "claude-opus-5-5", effort
    assert stats.title(s, "holly", setting, room) == expected, desc


@pytest.mark.parametrize("setting, slider, pending, desc", [
    ({"model": "claude-opus-5-5", "effort": "xhigh", "pending": None, "effort_live": True}, True, "",
     "a session run by a host: the slider, over the gauges"),
    ({"model": "claude-opus-5-5", "effort": "xhigh", "pending": "applying", "effort_live": True}, True, "applying…",
     "a change its host has yet to confirm, said under it"),
    ({"model": "claude-opus-5-5", "effort": "xhigh", "pending": "on relaunch", "effort_live": True}, True,
     "on relaunch: old host", "a host on older code never will"),
    ({"model": "claude-opus-5-5", "effort": "xhigh", "pending": None, "effort_live": False}, False, None,
     "a tab's: none (Q52)"),
    (None, False, None, "nothing known: none"),
])
@pytest.mark.parametrize("width", [37, 64])
def test_the_effort_slider_shows_for_a_hosted_session(setting, slider, pending, desc, width):
    rows, _ = stats.layout(snap(83_000, 600), "holly", None, usage(session=23, weekly_all=5), NOW, width, 40, setting)
    inside = [r.plain[1:-1].split(" ")[0] or "-" for r in rows[1:-1]]
    gauges = ["context", "-", "session", "-", "weekly", "-", "cache"]
    expect = ["-", "effort", "-"] + gauges if slider else ["-"] + gauges   # the pending line is under the label
    assert inside[:len(expect)] == expect, desc
    if slider:
        line = rows[stats.SLIDER_ROW]
        assert line.plain[1:].startswith("effort  low medium high xhigh max"), f"{desc}: whole at {width}"
        for start, end, level in stats.slider_spans():   # where a click finds each level
            assert line.plain[1 + start:1 + end] == level, f"{desc}: {level}"
        lit = [line.plain[s.start:s.end] for s in line.spans if "on " in str(s.style)]
        assert lit == ["xhigh"], f"{desc}: the current level lit"
        assert rows[stats.SLIDER_ROW + 1].plain[1:-1].strip() == pending, desc


@pytest.mark.parametrize("focused, keys, desc", [(True, True, "focused: the keys say how"), (False, False, "not")])
def test_the_slider_shows_its_keys_when_focused(focused, keys, desc):
    assert ("←/→" in stats.slider("high", focused).plain) == keys, desc


@pytest.mark.parametrize("width, drawn, desc", [
    (38, "effort  low medium high xhigh max  ←/→", "room: the keys after the levels"),
    (33, "eff ←/→ low medium high xhigh max", "a pane about 35 wide (160 columns): the keys in the label's place"),
])
def test_a_focused_slider_says_its_keys_wherever_it_shows_every_level(width, drawn, desc):
    line = stats.slider("high", True, width)
    assert line.plain == drawn and line.cell_len <= width, desc
    assert [(line.plain[a:b], level) for a, b, level in stats.slider_spans("high", width)] == \
        [(level, level) for level in models.EFFORTS], f"{desc}: the levels where a click picks them"


def test_the_title_reads_the_requested_model_before_the_served_one():
    s = snap(83_000, 600)
    s.model, s.requested = "claude-sonnet-5-5", "claude-opus-5-5[1m]"
    assert stats.title(s, "holly") == "holly · Opus 5.5 · 1M window"


@pytest.mark.parametrize("width, current, drawn, picks, desc", [
    (33, "high", "effort  low medium high xhigh max", [(level, level) for level in models.EFFORTS],
     "room for every level: each is where a click picks it"),
    (32, "high", "effort  ◂  high  ▸", [("◂", "medium"), ("high", "high"), ("▸", "xhigh")],
     "too narrow: the current level between arrows that step it"),
    (18, "medium", "effort  ◂ medium ▸", [("◂", "low"), ("medium", "medium"), ("▸", "high")],
     "the arrows stay put for the widest level"),
    (15, "xhigh", "eff ◂ xhigh  ▸", [("◂", "high"), ("xhigh", "xhigh"), ("▸", "max")],
     "narrower still: the label shortens"),
    (15, "low", "eff ◂  low   ▸", [("low", "low"), ("▸", "medium")], "the lowest: no step down"),
    (15, "max", "eff ◂  max   ▸", [("◂", "xhigh"), ("max", "max")], "the highest: no step up"),
    (15, None, "eff ◂   ?    ▸", [("◂", "medium"), ("▸", "xhigh")], "nothing known: the keys start from high"),
    (13, "medium", "◂ medium ▸", [("◂", "low"), ("medium", "medium"), ("▸", "high")], "narrower: the label goes"),
    (9, "medium", "◂ med  ▸", [("◂", "low"), ("med", "medium"), ("▸", "high")],
     "an 80x24 pane: the level shortened, both arrows kept"),
    (9, "xhigh", "◂ xhi  ▸", [("◂", "high"), ("xhi", "xhigh"), ("▸", "max")], "xhigh shortened too"),
])
def test_the_slider_fits_its_line(width, current, drawn, picks, desc):
    line = stats.slider(current, False, width)
    assert line.plain == drawn and line.cell_len <= width, desc
    spans = stats.slider_spans(current, width)
    assert [(line.plain[a:b], level) for a, b, level in spans] == picks, f"{desc}: where a click picks what"
    lit = [line.plain[s.start:s.end] for s in line.spans if "on " in str(s.style)]
    assert lit == ([stats.SHORT.get(current, current) if width < stats.WIDEST + 4 else current] if current else []), \
        f"{desc}: the current level lit"


@pytest.mark.parametrize("pending, width, shown, desc", [
    ("on relaunch", 35, "        on relaunch: old host", "room: under the levels"),
    ("on relaunch", 22, "on relaunch: old host", "less: from the line's start"),
    ("on relaunch", 15, "on relaunch", "narrow: shorter"),
    ("applying", 15, "applying…", "applying fits under the label"),
    (None, 15, "", "nothing pending"),
])
def test_the_pending_line_fits(pending, width, shown, desc):
    assert stats.pending_line(pending, width).plain.rstrip() == shown.rstrip(), desc


@pytest.mark.parametrize("setting, rows, desc", [
    ({"model": "claude-sonnet-5-5", "effort": "high", "pending": "on relaunch", "effort_live": True},
     ["╭─ Sonnet 5.5 ─╮", "│eff ◂  high  ▸│", "│on relaunch   │"], "hosted: the model in the title, the effort lit"),
    ({"model": "claude-sonnet-5-5", "effort": "high", "pending": None, "effort_live": False},
     ["╭─ Sonnet 5.5 ─╮", "│effort  high  │"], "a tab: the effort the title can't hold on a line of its own"),
])
def test_model_and_effort_are_readable_in_a_narrow_pane(setting, rows, desc):
    got, _ = stats.layout(snap(83_000, 600), "demo", None, usage(), NOW, 16, 40, setting)
    shown = [r.plain for r in got]
    assert [shown[0]] + [r for r in shown[1:len(rows) + 1] if r.strip("│ ")] == rows, desc


@pytest.mark.parametrize("setting, view_effort, lit, desc", [
    ({"model": None, "effort": None, "pending": None, "effort_live": True}, "medium", "medium",
     "NULL columns and nothing applied (an old host): the transcript's"),
    ({"model": None, "effort": "xhigh", "pending": None, "effort_live": True}, "medium", "xhigh", "set: what it's set to"),
])
def test_the_slider_falls_back_on_the_transcript(setting, view_effort, lit, desc):
    s = snap(83_000, 600)
    s.effort = view_effort
    assert stats.effort_of(s, setting) == lit, desc
    rows, _ = stats.layout(s, "holly", None, usage(), NOW, 64, 40, setting)
    line = rows[stats.SLIDER_ROW]
    assert [line.plain[x.start:x.end] for x in line.spans if "on " in str(x.style)] == [lit], desc


def fresh_chart(turns, now, kind):
    """A chart as made before the charts were kept (Series): every turn gone through again."""
    keys = [k for k, _ in stats.CHARTS[kind][1]]
    bs = stats._buckets(turns, now, 56 - stats.MARGIN, stats.SPAN)
    totals = [sum(b[k] for k in keys) for b in bs]
    maxt = stats.nice(max(totals, default=0))
    hour = sum(getattr(t, k) for t in turns if now - 3600 <= t.at <= now for k in keys)
    return [stats.build_column([(stats.CO[k], b[k]) for k in keys], t, maxt, 4) for b, t in zip(bs, totals)], maxt, hour


@pytest.mark.parametrize("later, desc", [
    (0, "now, a turn on it"),
    (1, "a second on, the same window"),
    (5, "past the turn that was in the future"),
    (3600 - 30, "just before the oldest turn leaves the hour"),
    (3600 - 30 + 1, "as it leaves, in the same window"),
    (3600 + 400, "windows on, others leaving"),
])
@pytest.mark.parametrize("kind", ["assembly", "output"])
def test_a_kept_chart_is_the_one_made_afresh_at_every_second(later, kind, desc):
    turns = [Turn(NOW - 3570, 5, 1, 0, 6, None, None, True, output=3),
             Turn(NOW - 600, 7, 2, 1, 9, None, None, True, output=4),
             Turn(NOW - 600, 1, 1, 1, 9, None, None, False, output=1),   # the same second as another
             Turn(NOW, 2, 0, 3, 9, None, None, True, output=2),
             Turn(NOW + 4, 9, 9, 9, 9, None, None, True, output=9)]   # a clock ahead: shown once it's past
    for now in (NOW, NOW + later):   # the first fills the cache the second may reuse
        c = stats.chart(list(turns), now, width=56, height=4, kind=kind)
        assert (c.columns, c.maxt, c.hour) == fresh_chart(turns, now, kind), f"{desc}, at +{now - NOW:g}s"


@pytest.mark.parametrize("change, kept, desc", [
    (lambda turns, now: (turns, now + 1), True, "a second on in the same window: the same columns"),
    (lambda turns, now: (list(turns), now), True, "a new list of the same turns (combine): the same"),
    (lambda turns, now: (turns + [Turn(now, 1, 1, 1, 1, None, None, True)], now), False, "a turn more: made again"),
    (lambda turns, now: (turns, now + stats.SPAN), False, "the window moved on: made again"),
])
def test_a_charts_columns_are_made_once_for_a_window_and_its_turns(change, kept, desc):
    turns, now = [Turn(EVEN - 60, 5, 1, 0, 6, None, None, True)], EVEN
    first = stats.chart(turns, now, width=56, height=4)
    turns, now = change(turns, now)
    assert (stats.chart(turns, now, width=56, height=4).columns is first.columns) == kept, desc


@pytest.mark.parametrize("later, hour, desc", [
    (0, 6, "the turn, a minute old, is in the hour"),
    (3540, 6, "an hour to the second after it"),
    (3541, 0, "a second later it has gone: the total still follows the clock"),
])
def test_the_hours_total_follows_the_clock_with_the_turns_unchanged(later, hour, desc):
    turns = [Turn(EVEN - 60, 5, 1, 0, 6, None, None, True)]
    stats.chart(turns, EVEN, width=56, height=4)   # kept
    assert stats.chart(turns, EVEN + later, width=56, height=4).hour == hour, desc
