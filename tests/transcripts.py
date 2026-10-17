"""Transcripts to read, written for the tests: shared by the wheelhouse's transcript_usage
tests and the stats module's (modules/stats/tests)."""

import json
import time

from claude_wheelhouse import api, transcript_usage
from claude_wheelhouse.transcript_usage import Snapshot, Turn


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


def snap(context, expires_in, ttl="1h"):
    return Snapshot(model="claude-opus-5-5", context=context, peak=context, last_at=NOW + expires_in - transcript_usage.TTL[ttl],
                    ttl=ttl, turns=[Turn(NOW - 60, context, 0, 0, context, ttl, None, True)])


# account usage


def write(path, *recs):
    path.write_bytes(b"".join(r + b"\n" for r in lines(*recs)))


def prompt(ago):
    return {"type": "user", "timestamp": at(ago), "message": {"role": "user", "content": "go on"}}


H, D = transcript_usage.HOUR, transcript_usage.DAY
# a session three days old: one response written as two lines, prompts between, a subagent
# that finished two days ago, and the span's one response. Each turn's tokens (read + new):
# m1 1002, m2 302, m3 52, o1 9, m4 40_002
HISTORY = (response("m1", ago=3 * D, read=1000), prompt(2.9 * D), response("m2", ago=5 * H + 1, read=300),
           response("m2", ago=5 * H, read=300), prompt(4.5 * H), response("m3", ago=3 * H, read=50),
           response("m4", ago=60, read=40_000))


def host(tokens=90_000, window=1_000_000, ago=10):
    return api.HostContext(tokens, window, NOW - ago)


def compacted_snap(compacted_ago=30, after=None):
    s = snap(350_000, 600)
    s.last_at = NOW - 60
    s.compactions = [transcript_usage.Compaction(NOW - compacted_ago, "manual", 350_000, 40_000, after)]
    return s


def command(name, args, out, ago=30):
    """A local command's record, as Claude Code 2.1.296 writes /model and /effort."""
    return {"type": "system", "subtype": "local_command", "timestamp": at(ago), "isMeta": False,
            "content": f"<local-command-stdout>{out}</local-command-stdout>",
            "commandRun": {"command": name, "args": args}}


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


def prompt(text, ago):
    return {"type": "user", "timestamp": at(ago), "message": {"role": "user", "content": text}}
