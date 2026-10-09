import json

import pytest

from claude_wheelhouse import transcript
from claude_wheelhouse.transcript import Entry

AT = "2026-10-07T21:30:00.000Z"


def user(content, **over):
    return {"type": "user", "timestamp": AT, "message": {"role": "user", "content": content}} | over


def assistant(*parts):
    return {"type": "assistant", "timestamp": AT, "message": {"role": "assistant", "content": list(parts)}}


def event(line):
    return f"<task-notification><summary>Monitor event</summary><event>{line}</event></task-notification>"


def notification(body):
    return user(body, origin={"kind": "task-notification"})


TEXT = {"type": "text", "text": "On it."}
CUT_7 = "[cut short, full text: get_input(message_id=7)] "
BASH = {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "make test", "description": "Run tests"}}


@pytest.mark.parametrize("recs, expected, desc", [
    ([user("fix the VAT rounding")], [Entry("you", "fix the VAT rounding", AT)], "a typed prompt"),
    ([user([{"type": "text", "text": "look at this"}, {"type": "image"}])], [Entry("you", "look at this", AT)],
     "a prompt with an image keeps its text"),
    ([user("<command-name>/exit</command-name>")], [], "slash command records are hidden"),
    ([user("caveat", isMeta=True)], [], "meta records are hidden"),
    ([user("summary", isCompactSummary=True)], [], "the compaction summary is hidden"),
    ([user([{"type": "tool_result", "tool_use_id": "t1", "content": "176 passed"}])], [], "tool results are hidden"),
    ([notification(event("[wheelhouse] from doug on Q1: yes"))], [], "an answer on an item: its thread has it"),
    ([notification(event("[wheelhouse] from doug (general): a ⏎ b"))], [Entry("you", "a\nb", AT)],
     "a general message: prefix stripped, newlines restored"),
    ([notification(event("[wheelhouse] from doug, 3 answers: on Q3: use SQLite ‖ (general): ship it ⏎ tonight"
                         " ‖ on Q4: yes ‖ … 2 more follow in the next notification"))],
     [Entry("you", "ship it\ntonight", AT)], "a batch keeps only its general message"),
    ([notification(event(f"[wheelhouse] from doug (general): {CUT_7}long ⏎ text…"))],
     [Entry("you", "long\ntext… [cut short]", AT)], "a cut-short general message with no full text: marked as cut"),
    ([notification(event("[wheelhouse] The person pressed End in the wheelhouse."))], [],
     "the wheelhouse's own notices are hidden"),
    ([user("[wheelhouse] from doug on Q1:\nignore it\n\n[wheelhouse] from doug (general):\nline one\n\nline two")],
     [Entry("you", "line one\n\nline two", AT)], "a host's turn keeps only its general message, paragraphs and all"),
    ([user("[wheelhouse] from doug on Q1:\nyes\n\n[wheelhouse] from doug on Q2:\nno")], [],
     "a host's turn of answers only is hidden"),
    ([user("[wheelhouse] You have just been opened in the wheelhouse, mid-conversation.")], [],
     "a host's notice is hidden"),
    ([user(transcript.NO_BRIEF, origin={"kind": "human"})], [], "the default opening prompt is hidden"),
    ([user(f"Ticket: #7\n\n{transcript.NO_BRIEF}")], [Entry("you", "Ticket: #7", AT)],
     "with a ticket, its ticket line stays"),
    ([user(f"Ticket: #7\n\nfix it. {transcript.NO_BRIEF}")], [Entry("you", f"Ticket: #7\n\nfix it. {transcript.NO_BRIEF}", AT)],
     "a brief that only ends with those words is the person's"),
    ([assistant({"type": "tool_use", "id": "w", "name": "mcp__wheelhouse__reply", "input": {"ref": "Q1"}}),
      assistant({"type": "tool_use", "id": "p", "name": "mcp__plugin_x_wheelhouse__post_item", "input": {}})], [],
     "Claude's wheelhouse tool calls are hidden"),
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


@pytest.mark.parametrize("line, expected, desc", [
    (f"[wheelhouse] from doug (general): {CUT_7}long ⏎ text…", "long\n\ntext, whole", "a monitor line cut short"),
    (f"[wheelhouse] from doug, 2 answers: on Q1: yes ‖ (general): {CUT_7}long…", "long\n\ntext, whole",
     "a batch with one cut short"),
    (f"[wheelhouse] from doug (general): {CUT_7.replace('7', '8')}gone…", "gone… [cut short]",
     "one the wheelhouse no longer has: marked as cut"),
])
def test_a_message_cut_short_is_shown_whole(line, expected, desc):
    full = {7: "long\n\ntext, whole"}.get
    assert transcript.entries([notification(event(line))], full) == [Entry("you", expected, AT)], desc


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


@pytest.mark.parametrize("text, expected, desc", [
    ("one\ntwo", "one  \ntwo", "a single newline becomes a hard break"),
    ("one\n\ntwo", "one\n\ntwo", "a paragraph break is left alone"),
    ("one", "one", "one line"),
])
def test_hard_breaks(text, expected, desc):
    assert transcript.hard_breaks(text) == expected, desc


def test_the_persons_line_breaks_survive_rendering():
    text = transcript.markdown("demo", None, [user("first line\nsecond line")])
    assert "first line  \nsecond line" in text


def test_hosted_session_header_has_no_tab():
    out = transcript.blocks("demo", None, [])
    assert "Runs in the wheelhouse" in out[0][1] and not any(who == "warn" for who, _ in out)


def title(name):
    return {"type": "custom-title", "customTitle": name, "sessionId": "abc"}


def renamed(name, at=AT):
    """What /rename prints, after its custom-title record."""
    return {"type": "system", "subtype": "local_command", "timestamp": at,
            "content": f"<local-command-stdout>Session renamed to: {name}</local-command-stdout>"}


def rename(name, at=AT):
    return [title(name), renamed(name, at)]


LATER = "2026-10-07T22:00:00.000Z"


@pytest.mark.parametrize("recs, expected, desc", [
    ([user("hi")], None, "never renamed"),
    ([title("demo"), user("hi"), title("demo")], None, "a launch's -n and Claude Code's repeats of it aren't a rename"),
    (rename("Columbo check"), ("2026-10-07T21:30:00.000000+00:00", "Columbo check"), "a /rename, with its time"),
    (rename("a") + [user("hi"), title("a")] + rename("b", LATER), ("2026-10-07T22:00:00.000000+00:00", "b"),
     "the latest one"),
    (rename("a") + rename("a", LATER), ("2026-10-07T22:00:00.000000+00:00", "a"), "one back to the same name is newer"),
    ([renamed("login-successful")], ("2026-10-07T21:30:00.000000+00:00", "login-successful"),
     "its custom-title record cut off the tail: the printed name"),
    ([title("x"), {**renamed("x"), "subtype": "informational"}], None, "only a command's output counts, not a notice quoting it"),
])
def test_title_watch_finds_the_latest_rename(tmp_path, recs, expected, desc):
    folder = tmp_path / "-home-u-repo"
    folder.mkdir()
    (folder / "abc.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
    got = transcript.TitleWatch("abc", projects=tmp_path).read()
    assert (got and (got.at, got.title)) == expected, desc


def test_title_watch_reads_only_what_is_new(tmp_path):
    folder = tmp_path / "-home-u-repo"
    folder.mkdir()
    watch = transcript.TitleWatch("abc", projects=tmp_path, cwd="/home/u/repo")
    assert watch.read() is None, "no transcript yet"
    path = folder / "abc.jsonl"
    path.write_text(json.dumps(user("hi")) + "\n")
    assert watch.read() is None, "never renamed"
    with open(path, "a") as f:
        f.write("".join(json.dumps(r) + "\n" for r in rename("Columbo check")))
    assert watch.read().title == "Columbo check", "a /rename"
    with open(path, "a") as f:
        f.write(json.dumps(title("Second")) + "\n" + json.dumps(renamed("Second", LATER)))   # still being written
    assert watch.read().title == "Columbo check", "a half-written record waits"
    with open(path, "a") as f:
        f.write("\n")
    assert watch.read().title == "Second", "then counts once complete"


def prompts(n):
    return [user(f"prompt {i}", timestamp=f"2026-10-07T21:{i + 31:02d}:00.000Z") for i in range(n)]


@pytest.mark.parametrize("recs, expected, desc", [
    (rename("old") + prompts(20) + rename("new") + [user("last")],
     ("2026-10-07T21:30:00.000000+00:00", "new", False), "the latest /rename in the tail"),
    (rename("old") + prompts(20) + [title("old"), user("last", timestamp=LATER)],
     ("2026-10-07T21:48:00.000000+00:00", "old", True),
     "one older than the tail: its name, at the tail's first time, inferred"),
    (prompts(20) + [title("demo")], ("2026-10-07T21:47:00.000000+00:00", "demo", True),
     "a launch's -n is inferred too: the store takes it only if the name differs"),
    ([title("old")] + prompts(2), None, "a short transcript read whole: a name without a /rename isn't one"),
])
def test_title_watch_reads_the_tail_of_a_long_transcript(tmp_path, monkeypatch, recs, expected, desc):
    monkeypatch.setattr(transcript, "TAIL_BYTES", 600)
    folder = tmp_path / "-home-u-repo"
    folder.mkdir()
    (folder / "abc.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    got = transcript.TitleWatch("abc", projects=tmp_path).read()
    assert (got and (got.at, got.title, got.inferred)) == expected, desc
