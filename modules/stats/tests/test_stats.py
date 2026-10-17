import re
import time

import pytest

from claude_wheelhouse import models, plan_usage, transcript_usage
from claude_wheelhouse.transcript_usage import Turn
from claude_wheelhouse_stats import stats
from tests.transcripts import (D, H, NOW, compacted_snap, host, lines, response, snap, write)
EVEN = 1791460800 + 7200 - 1791460800 % 7200   # on a multiple of every bucket width below


SOON = NOW + 2 * 3600


def usage(ago=0, **pct) -> plan_usage.AccountUsage:
    u = plan_usage.AccountUsage()
    u.limits = {kind: (p, SOON) for kind, p in pct.items()}
    u.read = dict.fromkeys(pct, NOW - ago)
    return u


W = stats.WEEK


DAYS = "(Mon|Tue|Wed|Thu|Fri|Sat|Sun)"


def fresh_chart(turns, now, kind):
    """A chart as made before the charts were kept (Series): every turn gone through again."""
    keys = [k for k, _ in stats.CHARTS[kind][1]]
    bs = stats._buckets(turns, now, 56 - stats.MARGIN, stats.SPAN)
    totals = [sum(b[k] for k in keys) for b in bs]
    maxt = stats.nice(max(totals, default=0))
    hour = sum(getattr(t, k) for t in turns if now - stats.SPAN <= t.at <= now for k in keys)
    return [stats.build_column([(stats.CO[k], b[k]) for k in keys], t, maxt, 4) for b, t in zip(bs, totals)], maxt, hour


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


@pytest.mark.parametrize("model, name, desc", [
    ("claude-opus-5-5", "Opus 5.5", "major and minor"),
    ("claude-sonnet-5", "Sonnet 5", "major only"),
    (None, "model unknown", "none logged yet"),
])
def test_model_names(model, name, desc):
    assert stats.model_name(model) == name, desc


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
    s.compactions = [transcript_usage.Compaction(NOW - 600, "manual", 201_000, 11_000)]
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
    [turn], _ = transcript_usage.parse(lines(response("m1", read=5)), set(), main=True)
    assert turn.output == 10
    c = stats.chart([turn], NOW, width=56, height=4, kind="output")
    assert c.maxt == 10 and any(base == stats.CO["output"] for col in c.columns for base, _ in col)
    assert stats.chart_lines(c, 0, ticks=False)[-1].plain.strip().startswith("0 └"), "no hour ticks: they go under the last"


@pytest.mark.parametrize("ago, kind, span, total, desc", [
    (60, "assembly", stats.SPAN, 123, "context assembly: cache read, new and miss, the bars' own series"),
    (60, "output", stats.SPAN, 7, "output: its output tokens"),
    (7200, "assembly", stats.SPAN, 123, "a turn exactly the span ago counts"),
    (3600, "assembly", 3600, 123, "zoomed to an hour: a turn exactly an hour ago counts"),
    (3601, "assembly", 3600, 0, "a second older doesn't, though the chart may still draw it"),
    (-1, "assembly", stats.SPAN, 0, "nor one after now, which the bars leave out too"),
])
def test_each_chart_sums_its_span(ago, kind, span, total, desc):
    turn = Turn(EVEN - ago, 100, 20, 3, 0, None, None, True, output=7)
    assert stats.chart([turn], EVEN, width=56, height=4, kind=kind, span=span).recent == total, desc


@pytest.mark.parametrize("width, span, head, desc", [
    (40, 900, "▸ context assembly  950/15m", "15m's longer note at 40: shortened"),
    (39, stats.SPAN, "▸ context assembly  950/2h", "short of the full note's room: the short note"),
    (26, stats.SPAN, "▸ context assembly  950/2h", "the short note, just"),
    (25, stats.SPAN, "▸ context assembly", "then no note"),
    (13, stats.SPAN, "▸ context ass", "then the name is cut"),
])
def test_the_caption_never_wraps(width, span, head, desc):
    c = stats.chart([Turn(NOW - 60, 950, 0, 0, 0, None, None, True)], NOW, width=width, height=4, span=span)
    line = stats.chart_lines(c, 0)[0]
    assert line.plain.split() == head.split() and line.cell_len <= width, f"{desc}: {line.plain!r}"


@pytest.mark.parametrize("width, tokens, keys, note, desc", [
    (80, 950, ["cache", "new", "miss"], "950 tokens/last 2h", "every key, and the count at the right"),
    (80, 0, ["cache", "new", "miss"], "0 tokens/last 2h", "none"),
    (80, 340_000, ["cache", "new", "miss"], "340k tokens/last 2h", "thousands as k"),
    (80, 1_234_567, ["cache", "new", "miss"], "1.2M tokens/last 2h", "millions to one place"),
    (64, 950, ["cache", "new", "miss"], "950 tokens/last 2h", "every key, a space apart, at 64"),
    (62, 950, ["cache", "new", "miss"], "950 tokens/last 2h", "every key, just"),
    (61, 950, ["new", "miss"], "950 tokens/last 2h", "cache is left out first, whole"),
    (56, 950, ["new", "miss"], "950 tokens/last 2h", "new and miss at 56"),
    (54, 950, ["new", "miss"], "950 tokens/last 2h", "new and miss, just"),
    (53, 950, ["miss"], "950 tokens/last 2h", "then new: miss stays longest"),
    (48, 950, ["miss"], "950 tokens/last 2h", "miss, just"),
    (47, 950, [], "950 tokens/last 2h", "then no key"),
    (40, 12_300_000, [], "12.3M tokens/last 2h", "no key at the narrowest that holds the caption and count"),
    (40, 123_400_000, [], "123M tokens/last 2h", "100M or more still fits at 40"),
    (40, 999_600, [], "1M tokens/last 2h", "999.6k rolls over to 1M"),
])
def test_the_last_hour_is_on_the_caption_row(width, tokens, keys, note, desc):
    c = stats.chart([Turn(NOW - 60, tokens, 0, 0, 0, None, None, True)], NOW, width=width, height=4)
    head = stats.chart_lines(c, 0)[0]
    assert head.plain.startswith("▸ context assembly") and head.plain.endswith(" " + note), desc
    assert head.cell_len == width, f"{desc}: right-aligned, {head.cell_len} cells in {width}"
    assert [k for k in ("cache", "new", "miss") if f"▆ {k}" in head.plain] == keys, desc


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


@pytest.mark.parametrize("offset, desc", [
    (0, "a bucket at the window's start"),
    (100, "one the start cuts: the part the bars show"),
    (299, "nearly all of it before the start"),
])
def test_the_caption_counts_the_history_as_the_bars_do(offset, desc):
    span, columns = 4 * H, 50
    start, step = stats.window(EVEN, columns, span)
    bucket = start - offset
    history = stats.History({bucket: (1000, 0, 0, 0)})
    c = stats.chart([], EVEN, width=stats.MARGIN + columns, height=4, span=span, history=[history])
    shown = sum(col[0] for col in stats.older([history], start, step, columns))
    assert c.recent == round(1000 * (stats.BUCKET - offset) / stats.BUCKET) == round(shown), desc


def test_turns_that_age_past_the_span_are_kept_as_history(tmp_path):
    folder = tmp_path / "-home-u-repo"
    folder.mkdir()
    write(folder / "s.jsonl", response("m1", ago=60, read=40_000))
    follower = transcript_usage.UsageFollower("s", tmp_path)
    follower.read(NOW)
    assert follower.snap.turns and not follower.snap.history
    follower.read(NOW + stats.SPAN)
    assert not follower.snap.turns and list(follower.snap.history.values()) == [(40_000, 2, 0, 10)], \
        "folded into its bucket, not dropped"
    c = stats.chart(follower.snap.turns, NOW + stats.SPAN, width=56, height=4, span=4 * H,
                    history=[follower.snap.history])
    assert c.recent == 40_002 and any(col[0][0] for col in c.columns), "a wider chart draws it, and counts it"


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


@pytest.mark.parametrize("span, more, name, goes, desc", [
    (900, True, "15m", {"-": 1800}, "the finest: + does nothing"),
    (900, False, "15m", {"-": 1800}, "below the default - always goes back out"),
    (1800, True, "30m", {"-": 3600, "+": 900}, "between: both go a level"),
    (3600, True, "1h", {"-": 2 * H, "+": 1800}, "between: both go a level"),
    (2 * H, True, "2h", {"-": 4 * H, "+": 3600}, "the default: older history to go back to"),
    (2 * H, False, "2h", {"+": 3600}, "the default, covering the oldest turn: - does nothing"),
    (4 * H, True, "4h", {"-": 8 * H, "+": 2 * H}, "4h"),
    (8 * H, True, "8h", {"-": 12 * H, "+": 4 * H}, "8h"),
    (12 * H, True, "12h", {"-": D, "+": 8 * H}, "12h"),
    (D, True, "1d", {"-": 2 * D, "+": 12 * H}, "a day"),
    (2 * D, True, "2d", {"-": 4 * D, "+": D}, "two days"),
    (4 * D, True, "4d", {"-": W, "+": 2 * D}, "four days"),
    (W, True, "1w", {"-": 2 * W, "+": 4 * D}, "a week"),
    (2 * W, True, "2w", {"-": 4 * W, "+": W}, "then doubling"),
    (8 * W, True, "8w", {"-": 16 * W, "+": 4 * W}, "and on"),
    (8 * W, False, "8w", {"+": 4 * W}, "as far as the oldest turn"),
])
def test_the_zoom_buttons(span, more, name, goes, desc):
    c = stats.chart([Turn(NOW - 60, 5, 1, 0, 6, None, None, True)], NOW, width=56, height=4, span=span, more=more)
    line = stats.chart_lines(c, 0)[-1]
    assert line.cell_len == 56 and line.plain.endswith(f"{name}  -   + "), f"{desc}: {line.plain!r}"
    found = {line.plain[start:end].strip(): to for start, end, to in stats.zoom_spans(span, 56, more)}
    assert found == goes, desc
    lit = {line.plain[sp.start:sp.end].strip() for sp in line.spans if " on " in str(sp.style)}
    assert lit == set(goes), f"{desc}: a button lit only where it goes somewhere"


@pytest.mark.parametrize("span, step, desc", [
    (900, 300, "15 minutes: ticks every 5"),
    (1800, 600, "30 minutes: every 10"),
    (3600, 900, "an hour: every 15"),
    (stats.SPAN, 3600, "two hours: on the hour, as before the zoom"),
])
def test_ticks_sit_on_their_zooms_steps(span, step, desc):
    hour = 1791460800
    c = stats.chart([], hour + 7 * 60, width=stats.MARGIN + 120, height=4, span=span)
    assert c.span == span, desc
    axis = stats.chart_lines(c, 0)[-1].plain[stats.MARGIN:]
    labels = list(re.finditer(r"(\d+):(\d\d)", axis))
    assert labels, f"{desc}: at least one tick labelled"
    for m in labels:
        local = time.localtime(c.start + m.start() / len(c.columns) * c.span)
        assert int(m[2]) % (step // 60) == 0, f"{desc}: {m[0]} is on a {step // 60}-minute step"
        off = ((local.tm_min - int(m[2])) % 60) * 60 + local.tm_sec
        assert min(off, 3600 - off) <= c.span / len(c.columns) + 30, \
            f"{desc}: {m[0]} drawn at {local.tm_min}:{local.tm_sec:02d}"


@pytest.mark.parametrize("span, label, date, desc", [
    (4 * H, r"\d+:\d\d", None, "4h: times"),
    (8 * H, r"\d+:00", None, "8h: on the hour"),
    (12 * H, DAYS + r" \d+:00", None, "12h: the weekday and hour"),
    (D, r"\d+:00|" + DAYS + r" \d+", DAYS + r" \d+", "a day: the date at midnight, between it the hours"),
    (2 * D, r"\d+:00|" + DAYS + r" \d+", DAYS + r" \d+", "two days"),
    (4 * D, r"12:00|" + DAYS + r" \d+", DAYS + r" \d+", "four days"),
    (W, DAYS + r" \d+", DAYS + r" \d+", "a week: dates"),
    (2 * W, r"\d+ [A-Z][a-z]{2}", r"\d+ [A-Z][a-z]{2}", "wider: day and month"),
    (8 * W, r"\d+ [A-Z][a-z]{2}", r"\d+ [A-Z][a-z]{2}", "eight weeks"),
])
@pytest.mark.parametrize("columns", [50, 120, 190])
def test_wide_ticks_read_well(monkeypatch, span, label, date, desc, columns):
    """Every label in its span's form, on its own local time, apart enough to read, and
    drawn where its time falls; from a day up, at least one date."""
    monkeypatch.setenv("TZ", "Europe/London")
    time.tzset()
    try:
        now = 1791460800 + 7 * 60
        start, _ = stats.window(now, columns, span)
        found = stats.axis_ticks(start, span, columns)
        desc = f"{desc} at {columns}"
        assert len(found) >= 2, f"{desc}: {found}"
        assert all(re.fullmatch(label, lab) for _, lab in found), f"{desc}: {found}"
        assert date is None or any(re.fullmatch(date, lab) for _, lab in found), f"{desc}: a date shows"
        for at, lab in found:
            lt = time.localtime(at)
            assert lt.tm_min == 0 or span < 8 * H, f"{desc}: {lab} on the hour"
            assert not re.fullmatch(date or "$^", lab) or (lt.tm_hour, lt.tm_min) == (0, 0), f"{desc}: {lab} at midnight"
        gaps = [(b - a) / span * columns for (a, _), (b, _) in zip(found, found[1:])]
        assert min(gaps) >= max(len(lab) for _, lab in found) + 1, f"{desc}: apart enough to read: {found}"
        c = stats.chart([], now, width=stats.MARGIN + columns, height=4, span=span)
        axis = stats.chart_lines(c, 0)[-1].plain[stats.MARGIN:][:columns - stats.zoom(span).cell_len - 1]
        drawn, line = [], [" "] * len(axis)
        for t, lab in found:
            pos = round((t - c.start) / span * columns)
            if axis[pos:pos + len(lab)] == lab:
                drawn.append(lab)
                line[pos:pos + len(lab)] = lab
        assert drawn and "".join(line) == axis, f"{desc}: each label drawn where its time falls: {axis!r}"
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


@pytest.mark.parametrize("width, buttons, desc", [
    (stats.MARGIN + stats.ZOOM_MIN, True, "room enough: the buttons"),
    (stats.MARGIN + stats.ZOOM_MIN - 1, False, "too narrow: the ticks alone, and nothing to click"),
])
def test_a_narrow_chart_leaves_the_zoom_out(width, buttons, desc):
    c = stats.chart([], NOW, width=width, height=4)
    assert stats.chart_lines(c, 0)[-1].plain.endswith(" + ") == buttons, desc
    assert bool(stats.zoom_spans(stats.SPAN, width)) == buttons, desc


def test_both_charts_take_the_zoom():
    _, shown = stats.layout(snap(83_000, 600), "holly", None, usage(session=23), NOW, 56, 60, span=1800)
    assert [c.span for c in shown] == [1800, 1800]


@pytest.mark.parametrize("view, note, desc", [
    (None, "couldn't read the transcript: transcript gone", "a failed read with nothing to show"),
    (snap(83_000, 600), "couldn't read the transcript: transcript gone", "a failed read beside what was read before"),
])
def test_a_note_wraps_inside_the_panel(view, note, desc):
    rows, _ = stats.layout(view, "holly", note, usage(session=23), NOW, 40, 30)
    assert {r.cell_len for r in rows} == {40}, f"{desc}: the border holds"
    inside = " ".join(r.plain[1:-1].strip() for r in rows[1:-1])
    assert note in inside, desc


@pytest.mark.parametrize("s, h, expect, desc", [
    (compacted_snap(), host(), ["~90k/1M", "350k → ~90k"], "the host's estimate, in the gauge and as the post size"),
    (compacted_snap(compacted_ago=90, after=52_000), host(), ["350k/1M", "350k → 52k"],
     "the next response's size, once there is one"),
    (compacted_snap(), None, ["350k/1M", "350k · 40k msgs kept"], "no host count: postTokens, as the messages kept"),
])
def test_the_compact_row_and_the_gauge(s, h, expect, desc):
    rows, _ = stats.layout(transcript_usage.current(s, h), "holly", None, usage(session=23), NOW, 70, 40)
    text = "\n".join(r.plain for r in rows)
    assert all(e in text for e in expect), f"{desc}: {text}"


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
        assert (c.columns, c.maxt, c.recent) == fresh_chart(turns, now, kind), f"{desc}, at +{now - NOW:g}s"


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
    (0, 6, "the turn, a minute old, is in the span"),
    (7140, 6, "the span to the second after it"),
    (7141, 0, "a second later it has gone: the total still follows the clock"),
])
def test_the_spans_total_follows_the_clock_with_the_turns_unchanged(later, hour, desc):
    turns = [Turn(EVEN - 60, 5, 1, 0, 6, None, None, True)]
    stats.chart(turns, EVEN, width=56, height=4)   # kept
    assert stats.chart(turns, EVEN + later, width=56, height=4).recent == hour, desc
