import http.server
import json
import os
import re
import threading
import time

import pytest

from claude_wheelhouse import stats, transcript
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
    assert got[0].startswith("1h · warm" if expected is None else s.ttl + " · cold since"), desc


@pytest.mark.parametrize("size, window, colour, flashing, desc", [
    (100_000, 1_000_000, stats.OK, False, "100k of 1M is green"),
    (400_000, 1_000_000, stats.AMBER, False, "400k of 1M is amber"),
    (700_000, 1_000_000, stats.HOT, True, "past 600k of 1M flashes"),
    (130_000, 200_000, stats.AMBER, False, "130k of 200k is amber"),
])
def test_context_grades(size, window, colour, flashing, desc):
    assert stats.grade(size, window) == (colour, flashing), desc


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


@pytest.mark.parametrize("u, width, expect, desc", [
    (usage(session=23, weekly_all=5), 50, ["session", "23%", "weekly", "5%", "resets"], "both bars at 50 columns"),
    (usage(session=23, weekly_all=5), 80, ["session", "23%", "weekly", "5%"], "both bars at 80 columns"),
    (usage(session=97), 50, ["97%"], "one limit only"),
    (stats.AccountUsage(), 50, ["usage loading…"], "before the first fetch"),
])
def test_usage_lines(u, width, expect, desc):
    rows = stats.usage_lines(u, NOW, 0, width)
    text = "\n".join(r.plain for r in rows)
    assert all(e in text for e in expect), desc
    assert all(len(r.plain) <= width for r in rows), f"{desc}: fits the pane"


def test_usage_unavailable_after_a_failed_fetch():
    u = stats.AccountUsage()
    u.failed = True
    assert [r.plain for r in stats.usage_lines(u, NOW, 0, 50)] == ["usage unavailable"]


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
    monkeypatch.setenv("TZ", zone)
    time.tzset()
    try:
        c = stats.chart([], NOW, width=stats.MARGIN + 120, height=4)
        axis = stats.chart_lines(c, 0)[-1].plain[stats.MARGIN:]
        for m in re.finditer(r"(\d+):00", axis):
            at = c.start + m.start() / len(c.columns) * c.span
            local = time.localtime(at)
            minutes = local.tm_min + local.tm_sec / 60
            assert min(minutes, 60 - minutes) <= c.span / len(c.columns) / 60 + 0.5, f"{desc}: {m[0]} at {local.tm_hour}:{local.tm_min:02d}"
            assert int(m[1]) == (local.tm_hour + (1 if local.tm_min >= 30 else 0)) % 24, desc
    finally:
        monkeypatch.undo()
        time.tzset()
