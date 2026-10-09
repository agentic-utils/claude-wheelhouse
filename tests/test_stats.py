import http.server
import json
import os
import re
import threading
import time

import pytest
from rich.style import Style

from claude_wheelhouse import api, stats, transcript
from claude_wheelhouse.stats import Snapshot, Turn

REAL_FETCH = stats.AccountUsage.fetch   # before conftest stubs it

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


@pytest.mark.parametrize("columns, ago, index, desc", [
    (60, 7199, 0, "the oldest moment in the span is the first bucket"),
    (60, 0, 59, "now is the last"),
    (60, 3600, 30, "an hour ago is halfway, at 60 columns of 2 minutes"),
    (80, 3600, 40, "and halfway at 80 columns of 90 seconds"),
    (60, 7300, None, "older than two hours is left out"),
])
def test_buckets_fill_the_width_with_two_hours(columns, ago, index, desc):
    turn = Turn(NOW - ago, 5, 1, 0, 6, None, None, True)
    bs = stats.buckets([turn], NOW, columns)
    assert len(bs) == columns, desc
    assert [i for i, b in enumerate(bs) if b["read"]] == ([] if index is None else [index]), desc


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
    (10_000, "▁", "any context shows"),
    (100_000, "▂", "100k: two eighths"),
    (125_000, "▃", "125k: two and a half eighths round half up, not to even (#51)"),
    (150_000, "▃", "150k: half way to 200k"),
    (200_000, "▄", "200k: half way up"),
    (350_000, "▅", "350k: half way to 500k"),
    (500_000, "▆", "500k"),
    (750_000, "▇", "750k: half way to 1M"),
    (1_000_000, "█", "1M fills the cell"),
    (1_500_000, "█", "past 1M stays full"),
])
def test_the_context_bar(size, char, desc):
    """Doug (#51): one cell, eight levels, on the 100k/200k/500k/1M scale, in its grade's colour."""
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


@pytest.mark.parametrize("body, expected, desc", [
    ({"limits": [{"kind": "session", "percent": 23, "resets_at": "2026-10-08T12:39:59.772001+00:00"},
                 {"kind": "weekly_all", "percent": 5, "resets_at": "2026-10-14T23:59:59+00:00"}]},
     {"session": 23.0, "weekly_all": 5.0}, "both limits, as the endpoint sends them"),
    ({"limits": [{"kind": "session", "percent": 40}]}, {"session": 40.0}, "no reset time still shows the bar"),
    ({"limits": [{"kind": "session"}, "junk", {"kind": "weekly_all", "percent": None}]}, {}, "malformed entries are skipped"),
    ({}, {}, "no limits at all"),
    (None, {}, "no body"),
])
def test_usage_limits_parse(body, expected, desc):
    assert {k: pct for k, (pct, _) in stats.limits(body).items()} == expected, desc


def usage(**pct) -> stats.AccountUsage:
    u = stats.AccountUsage()
    u.limits = {kind: (p, SOON) for kind, p in pct.items()}
    return u


@pytest.mark.parametrize("view, u, width, expect, desc", [
    (None, usage(session=23, weekly_all=5), 50, ["session", "23%", "weekly", "5%", "resets"], "both bars at 50 columns"),
    (None, usage(session=23, weekly_all=5), 80, ["session", "23%", "weekly", "5%"], "both bars at 80 columns"),
    (None, usage(session=97), 50, ["97%"], "one limit only"),
    (None, stats.AccountUsage(), 50, ["usage loading…"], "before the first fetch"),
    (snap(83_000, 600), usage(session=23), 50, ["context", "8%", "83k/1M", "session", "23%"], "a session's context too"),
])
def test_gauges(view, u, width, expect, desc):
    rows = stats.gauges(view, u, NOW, width)
    text = "\n".join(r.plain for r in rows)
    assert all(e in text for e in expect), desc
    assert all(len(r.plain) <= width for r in rows), f"{desc}: fits the pane"
    bars = [r.plain.index("%") for r in rows if "█" in r.plain or "░" in r.plain]
    assert len(set(bars)) <= 1, f"{desc}: the bars line up"


def test_usage_unavailable_after_a_failed_fetch():
    u = stats.AccountUsage()
    u.failed = True
    assert [r.plain for r in stats.gauges(None, u, NOW, 50)] == ["usage unavailable"]


@pytest.mark.parametrize("now, shown, desc", [
    (NOW - NOW % 2, stats.HOT, "on even seconds a flashing gauge is lit"),
    (NOW - NOW % 2 + 1, tuple(int(c * 0.22) for c in stats.HOT), "on odd ones it's dark, as in the dashboard"),
])
def test_flashing(now, shown, desc):
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


def test_usage_is_fetched_at_most_once_a_minute():
    u = stats.AccountUsage()
    assert [u.due(NOW), u.due(NOW + 30), u.due(NOW + 61)] == [True, False, True]


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


class Redirect(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def do_GET(self):
        Redirect.seen.append((self.path, self.headers.get("Authorization")))
        if self.path == "/usage":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/elsewhere")
            self.end_headers()
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"limits": []}')

    def log_message(self, *a):
        pass


def test_the_usage_token_never_follows_a_redirect(tmp_path, monkeypatch):
    (tmp_path / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "secret"}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    server = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=server.handle_request, daemon=True).start()
    monkeypatch.setattr(stats, "USAGE_URL", f"http://127.0.0.1:{server.server_port}/usage")
    Redirect.seen = []
    u = stats.AccountUsage()
    REAL_FETCH(u)
    server.server_close()
    assert u.failed, "a redirect is a failed fetch"
    assert Redirect.seen == [("/usage", "Bearer secret")], "the token went to the endpoint only"


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
