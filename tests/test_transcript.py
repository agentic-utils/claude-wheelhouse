import json

import pytest

from claude_wheelhouse import transcript
from claude_wheelhouse.transcript import Entry

AT = "2026-10-07T21:30:00.000Z"


def user(content, **over):
    return {"type": "user", "timestamp": AT, "message": {"role": "user", "content": content}} | over


def assistant(*parts):
    return {"type": "assistant", "timestamp": AT, "message": {"role": "assistant", "content": list(parts)}}


def notification(body):
    return user(body, origin={"kind": "task-notification"})


TEXT = {"type": "text", "text": "On it."}
BASH = {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "make test", "description": "Run tests"}}


@pytest.mark.parametrize("recs, expected, desc", [
    ([user("fix the VAT rounding")], [Entry("you", "fix the VAT rounding", AT)], "a typed prompt"),
    ([user([{"type": "text", "text": "look at this"}, {"type": "image"}])], [Entry("you", "look at this", AT)],
     "a prompt with an image keeps its text"),
    ([user("<command-name>/exit</command-name>")], [], "slash command records are hidden"),
    ([user("caveat", isMeta=True)], [], "meta records are hidden"),
    ([user("summary", isCompactSummary=True)], [], "the compaction summary is hidden"),
    ([user([{"type": "tool_result", "tool_use_id": "t1", "content": "176 passed"}])], [], "tool results are hidden"),
    ([notification("<task-notification><summary>Monitor event</summary>"
                   "<event>[wheelhouse] from doug on Q1: yes</event></task-notification>")],
     [Entry("wheelhouse", "[wheelhouse] from doug on Q1: yes", AT)], "a wheelhouse notification"),
    ([notification("<task-notification><summary>Agent finished</summary></task-notification>")], [],
     "other task notifications are hidden"),
    ([assistant({"type": "thinking", "thinking": "hmm"}, TEXT)], [Entry("claude", "On it.", AT)],
     "thinking is hidden"),
    ([assistant(TEXT), assistant({"type": "text", "text": "Done."})], [Entry("claude", "On it.\n\nDone.", AT)],
     "one reply split across records reads as one"),
    ([assistant(BASH)], [Entry("tool", "Bash: Run tests", AT)], "a tool call is one line"),
    ([{"type": "system", "subtype": "compact_boundary", "timestamp": AT}],
     [Entry("note", "conversation compacted", AT)], "compaction is marked"),
    ([{"type": "permission-mode", "permissionMode": "auto"}], [], "bookkeeping is hidden"),
])
def test_entries(recs, expected, desc):
    assert transcript.entries(recs) == expected, desc


@pytest.mark.parametrize("recs, expected, desc", [
    ([assistant({"type": "tool_use", "id": "a", "name": "AskUserQuestion", "input": {}})], "a question",
     "an unanswered AskUserQuestion"),
    ([assistant({"type": "tool_use", "id": "a", "name": "AskUserQuestion", "input": {}}),
      user([{"type": "tool_result", "tool_use_id": "a"}])], None, "answered"),
    ([assistant({"type": "tool_use", "id": "p", "name": "ExitPlanMode", "input": {}})], "a plan to approve",
     "a plan waiting for approval"),
    ([assistant(BASH)], None, "a running tool is not a wait (permission prompts can't be seen)"),
])
def test_waiting_in_tab(recs, expected, desc):
    assert transcript.waiting_in_tab(recs) == expected, desc


@pytest.mark.parametrize("args, expected, desc", [
    ({"command": "make test", "description": "Run tests"}, "Bash: Run tests", "description first"),
    ({"command": "ls\n  -la"}, "Bash: ls -la", "whitespace flattened"),
    ({}, "Bash", "no arguments"),
    ({"command": "x" * 200}, "Bash: " + "x" * 83 + "…", "cut to the width"),
])
def test_tool_line(args, expected, desc):
    assert transcript.tool_line("Bash", args) == expected, desc


def test_only_the_tail_is_read_and_sidechains_are_dropped(tmp_path):
    path = tmp_path / "s.jsonl"
    recs = [user(f"prompt {i}") for i in range(200)] + [user("subagent", isSidechain=True)]
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    got = transcript.tail_records(path, limit=2000)
    assert 0 < len(got) < 200 and got[-1]["message"]["content"] == "prompt 199"


def test_markdown_flags_a_wait_and_shows_queued_messages():
    recs = [user("which db?"), assistant({"type": "tool_use", "id": "a", "name": "AskUserQuestion", "input": {}})]
    text = transcript.markdown("demo", "demo tab", recs, queued=["SQLite"])
    assert "Tab: **demo tab**" in text
    assert "⚠ **Waiting for you in its tab:** a question." in text
    assert text.rstrip().endswith("**you · queued**\n\nSQLite")


def test_follower_rereads_only_when_the_file_changes(tmp_path):
    folder = tmp_path / "-home-u-repo"
    folder.mkdir()
    follower = transcript.Follower("abc", projects=tmp_path)
    assert follower.read() is None, "no transcript yet"
    path = folder / "abc.jsonl"
    path.write_text(json.dumps(user("one")) + "\n")
    assert len(follower.read()) == 1
    with open(path, "a") as f:
        f.write(json.dumps(user("two")) + "\n")
    assert [r["message"]["content"] for r in follower.read()] == ["one", "two"]
