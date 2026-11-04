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


def queued(prompt, mode="prompt"):
    """What Claude Code writes for a message that reaches a session mid-turn: folded into
    the running turn as an attachment, not a user record of its own."""
    return {"type": "attachment", "timestamp": AT,
            "attachment": {"type": "queued_command", "prompt": prompt, "commandMode": mode, "timestamp": AT}}


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
    ([queued("[wheelhouse] from doug (general):\nkeep going")], [Entry("you", "keep going", AT)],
     "a host's turn that reached the session mid-turn"),
    ([queued(event("[wheelhouse] from doug (general): yes ⏎ fix it"), "task-notification")],
     [Entry("you", "yes\nfix it", AT)], "a monitor line that reached the session mid-turn"),
    ([queued("[wheelhouse] from doug on Q1:\nyes")], [], "an answer on an item that reached it mid-turn: its thread has it"),
    ([queued("also check the tests")], [Entry("you", "also check the tests", AT)],
     "a prompt typed in the tab mid-turn"),
    ([queued(event("Agent finished"), "task-notification")], [], "other task notifications mid-turn are hidden"),
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


def op(operation, content=None):
    return {"type": "queue-operation", "operation": operation, "timestamp": AT} | (
        {"content": content} if content is not None else {})


GENERAL = "[wheelhouse] from doug (general):\nship it"


@pytest.mark.parametrize("recs, sending, expected, desc", [
    ([], ["ship it"], ["ship it"], "sent, not yet passed on"),
    ([op("enqueue", GENERAL)], [], ["ship it"], "passed on, Claude Code holding it"),
    ([op("enqueue", event("[wheelhouse] from doug (general): ship it"))], [], ["ship it"],
     "a monitor line Claude Code is holding"),
    ([op("enqueue", GENERAL)], ["ship it"], ["ship it"], "passed on as it was read: shown once"),
    ([op("enqueue", GENERAL), op("dequeue"), user(GENERAL)], [], [], "taken as a turn of its own"),
    ([op("enqueue", GENERAL), queued(GENERAL), op("remove", GENERAL)], [], [], "folded into a running turn"),
    ([op("enqueue", GENERAL), queued(GENERAL)], [], [], "seen taken, though no record says it left the queue"),
    ([op("enqueue", GENERAL), op("popAll")], [], [], "the queue emptied"),
    ([op("enqueue", "older"), op("enqueue", GENERAL), op("dequeue")], [], ["ship it"], "dequeue takes the first"),
    ([op("enqueue", "[wheelhouse] from doug on Q1:\nyes")], [], [], "an answer on an item: its thread has it"),
    ([op("enqueue", GENERAL), op("enqueue", GENERAL), queued(GENERAL), op("remove", GENERAL)], [], ["ship it"],
     "two the same, the first folded into a turn: the second still held"),
    ([op("enqueue", GENERAL), op("enqueue", GENERAL), op("dequeue"), user(GENERAL)], [], ["ship it"],
     "two the same, the first taken as a turn: the second still held"),
    ([op("enqueue", GENERAL), op("enqueue", GENERAL), queued(GENERAL)], [], ["ship it"],
     "two the same, one seen taken with no record of it leaving: the other still held"),
    ([op("enqueue", GENERAL)], ["ship it"] * 2, ["ship it"] * 2, "one held and one the same not yet passed on: both"),
    ([op("enqueue", GENERAL), queued(GENERAL), op("enqueue", "/compact notes"), op("dequeue")], [], [],
     "a take whose record of leaving never came doesn't stand in for a later dequeue (f02699a4's /compact)"),
])
def test_a_message_on_its_way_shows_as_sending(recs, sending, expected, desc):
    shown = [md for who, md in transcript.blocks("demo", None, recs, sending=sending) if "you · sending" in md]
    assert shown == [f"**you · sending**\n\n{body}" for body in expected], desc


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


def lines(*recs):
    return "".join(json.dumps(r) + "\n" for r in recs)


ONE, TWO = lines(user("one")), lines(user("two"))


@pytest.mark.parametrize("text, limit, expected, desc", [
    (ONE + TWO, len(TWO), ["two"], "a window starting exactly on a record keeps it whole"),
    (ONE + TWO, len(TWO) + 1, ["two"], "one starting a byte into a record drops it"),
    (ONE + TWO, len(TWO) - 1, [transcript.OVERSIZE], "a newest record larger than the window: a stand-in for it"),
    (ONE + TWO[:-1], len(TWO) - 2, [transcript.OVERSIZE], "one still being written, too"),
    (ONE + TWO[:10], 12, [], "one still being written that starts in the window: not too large, just not whole yet"),
    (ONE + TWO, 10_000, ["one", "two"], "a short transcript, whole"),
])
def test_the_tail_window(tmp_path, text, limit, expected, desc):
    path = tmp_path / "s.jsonl"
    path.write_text(text)
    got = transcript.tail_records(path, limit=limit)
    assert [r["type"] if r["type"] == transcript.OVERSIZE else r["message"]["content"] for r in got] == expected, desc


@pytest.mark.parametrize("seed", range(4))
def test_the_tail_window_fuzzed(tmp_path, seed):
    """Against a plain reference: the records whose first byte is in the last limit bytes, the
    newest perhaps still being written (cut anywhere, so unparseable), and the stand-in only
    when no record starts in the window at all."""
    import random
    rnd, path = random.Random(seed), tmp_path / "s.jsonl"
    for _ in range(500):
        recs = [user("x" * rnd.randint(0, 40)) for _ in range(rnd.randint(0, 6))]
        data = lines(*recs).encode()
        if recs and rnd.random() < 0.5:   # the newest mid-write
            data = data[:rnd.randint(len(data) - len(lines(recs[-1])) + 1, len(data))]
        path.write_bytes(data)
        limit = rnd.randint(1, len(data) + 5)
        start, at, want, begun = max(0, len(data) - limit), 0, [], False
        for rec in recs:
            end = at + len(lines(rec)) - 1   # its newline's offset: whole without it
            begun |= start <= at < len(data)
            if start <= at and end <= len(data):
                want.append(rec)
            at = end + 1
        if start and not begun:
            want.append({"type": transcript.OVERSIZE})
        assert transcript.tail_records(path, limit=limit) == want, (data, limit)


def test_a_record_too_large_to_show_is_said_so():
    assert transcript.entries([{"type": transcript.OVERSIZE}]) == [Entry("note", "a turn too large to show here")]


def test_a_follower_whose_transcript_has_gone_says_so(tmp_path):
    (tmp_path / "p").mkdir()
    path = tmp_path / "p/abc.jsonl"
    path.write_text(ONE)
    follower = transcript.Follower("abc", projects=tmp_path)
    assert len(follower.read()) == 1
    follower.blocks_key, follower.blocks = follower.seen, ["shown"]
    path.unlink()
    assert follower.read() is None
    assert (follower.seen, follower.recs, follower.blocks) == (None, None, None), "nothing of it left standing"


FORK_PROMPT = user([{"type": "tool_result", "tool_use_id": "f", "content": "Fork started"},
                    {"type": "text", "text": "<fork-boilerplate>\nYou are a worker fork.\n</fork-boilerplate>\n\n"
                                             "Your directive: count the PRs"}], uuid="u1")
SKILL_PROMPT = user("Base directory for this skill: /s/two-linear\n\n# two-linear\n\nRuns.\n\n\nARGUMENTS: file 2 tickets",
                    isMeta=True, uuid="u1")
STARTED = assistant({"type": "tool_use", "id": "f", "name": "Agent", "input": {"prompt": "count the PRs"}})


def agent_file(tmp_path, *recs):
    """A session's transcript, and a subagent's beside it, as agent_path has it."""
    folder = tmp_path / "-home-u-repo"
    (folder / "abc/subagents").mkdir(parents=True)
    (folder / "abc.jsonl").write_text("")
    path = folder / "abc/subagents/agent-a1.jsonl"
    path.write_text(lines(*recs))
    return path


def shown(tmp_path, *recs, limit=None, monkeypatch=None):
    agent_file(tmp_path, *recs)
    if limit:
        monkeypatch.setattr(transcript, "TAIL_BYTES", limit)
    follower = transcript.AgentFollower("abc", "a1", projects=tmp_path)
    recs = follower.read()
    return [md.split("\n\n")[-1] if who != "head" else who for who, md in transcript.agent_blocks(recs, prompt=follower.prompt)]


@pytest.mark.parametrize("recs, expected, desc", [
    ([user("find the bug", uuid="u1"), assistant(TEXT), user("[Request interrupted by user]", uuid="u2")],
     ["head", "find the bug", "On it.", "[Request interrupted by user]"], "only the first user record is its prompt"),
    ([{"type": "fork-context-ref", "agentId": "a1"}, STARTED, FORK_PROMPT, assistant(TEXT)],
     ["head", "count the PRs", "On it."], "a fork: its directive, and none of its session's records before it"),
    ([{"type": "fork-context-ref", "agentId": "a1"}, user("[SYSTEM NOTIFICATION] a task ended", isMeta=True), STARTED,
      FORK_PROMPT, assistant(TEXT)], ["head", "count the PRs", "On it."], "past its session's own notices too"),
    ([user("<system-reminder>x</system-reminder>", isMeta=True), user("find the bug", uuid="u1")],
     ["head", "find the bug"], "a meta record isn't its prompt"),
    ([SKILL_PROMPT, assistant(TEXT)], ["head", "file 2 tickets", "On it."], "a skill: what it was asked"),
    ([user("Base directory for this skill: /s/two-linear\n\n# two-linear\n\nRuns.", isMeta=True, uuid="u1"),
      assistant(TEXT)], ["head", "two-linear", "On it."], "a skill asked nothing: its name"),
    ([user("Base directory for this skill: /s/two-linear/\n\nRuns.\n\n\nARGUMENTS: ", isMeta=True, uuid="u1"),
      assistant(TEXT)], ["head", "two-linear", "On it."], "one given empty arguments: its name too"),
    ([user("[SYSTEM NOTIFICATION] a task ended", isMeta=True), assistant(BASH), user([{"type": "tool_result",
      "tool_use_id": "t1", "content": "ok"}], uuid="u1"), assistant(TEXT)], ["head", "`⚙ Bash: Run tests`", "On it."],
     "one with no prompt in it at all keeps all it has"),
    ([{"type": "fork-context-ref", "agentId": "a1"}, STARTED], ["head", "_Nothing in its transcript yet._"],
     "a fork not yet given its directive"),
])
def test_a_subagents_prompt(tmp_path, recs, expected, desc):
    assert shown(tmp_path, *recs) == expected, desc


def test_a_subagents_prompt_shows_however_long_it_runs(tmp_path, monkeypatch):
    steps = [assistant({"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {"command": f"step {i}"}})
             for i in range(200)]
    got = shown(tmp_path, user("find the bug", uuid="u1"), *steps, user("stop", uuid="u2"), limit=5000,
                monkeypatch=monkeypatch)
    assert got[:2] == ["head", "find the bug"] and got[-1] == "stop" and len(got) < 200
    labels = [md.split("**")[1] for who, md in transcript.agent_blocks(
        [user("stop")], prompt=user("find the bug")) if who == "you"]
    assert labels == ["prompt", "session"], "a later turn of the session's is labelled as its"


def test_stage_lines_go_by_time_and_an_untimed_entry_stays_put():
    from claude_wheelhouse import transcript as tr
    recs = [{"type": tr.OVERSIZE},
            {"type": "user", "message": {"content": "first"}, "timestamp": "2026-10-29T10:00:00.000000+00:00"},
            {"type": "user", "message": {"content": "second"}, "timestamp": "2026-10-29T10:05:00.000000+00:00"}]
    stage = tr.Entry("stage", "▐█          █▌ walls closing in", "2026-10-29T10:02:00.000000+00:00")
    out = tr.blocks("demo", None, recs, stages=(stage,))
    whos = [who for who, _ in out[1:]]
    assert whos == ["note", "you", "stage", "you"], out


def notes_ask(nonce="c0ffee12"):
    """The host's ask for a compaction's notes, as it sends it (host.compact_ask's KEEP_ASK)."""
    from claude_wheelhouse.host import KEEP_ASK
    return "[wheelhouse] The person pressed Compact in the wheelhouse.\n\n" + KEEP_ASK.format(nonce=nonce)


NOTES = {"type": "text", "text": 'Keeping these.\n<keep id="c0ffee12">\n- PR #12 open\n- next: tests\n</keep>'}
WRITING = {"type": "text", "text": 'Keeping these.\n<keep id="c0ffee12">\n- PR #12 open'}


@pytest.mark.parametrize("recs, want, desc", [
    ([user("fix it"), user(notes_ask()), assistant(BASH), assistant(WRITING)],
     ["you:fix it", "note:writing its notes for after compaction · 3 lines"], "under way: one line, its tool calls folded too"),
    ([user(notes_ask()), assistant(NOTES), user("<command-name>/compact</command-name>"),
      {"type": "system", "subtype": "compact_boundary", "timestamp": AT}, user("go on"), assistant(TEXT)],
     ["note:wrote its notes for after compaction · 5 lines", "note:conversation compacted", "you:go on", "claude:On it."],
     "after the cycle: still folded, and what follows shows"),
    ([user("fix it"), assistant(BASH), queued(notes_ask()), assistant(NOTES)],
     ["you:fix it", "tool:Bash: Run tests", "note:wrote its notes for after compaction · 5 lines"],
     "the ask taken into a running turn: folded from there"),
    ([user(notes_ask()), queued("[wheelhouse] from doug (general): hello"), assistant(NOTES)],
     ["note:wrote its notes for after compaction · 5 lines", "you:hello"],
     "the person's words taken into it still show"),
    ([user("the line <keep id=\"x\"> before them, I said"), assistant(TEXT)],
     ["you:the line <keep id=\"x\"> before them, I said", "claude:On it."], "the tag quoted by the person: not an ask"),
])
def test_a_compactions_notes_turn_is_folded(recs, want, desc):
    """#287: the session's reply to the wheelhouse's ask for notes shows as one dim line, while
    it's written and after, found by the ask the host sends (KEEP_ASK), not by the reply's text."""
    got = [f"{e.who}:{e.text}" for e in transcript.entries(recs)]
    assert got == want, f"{desc}: {got}"


def test_the_notes_ask_is_the_hosts():
    """KEEP_ASKED finds every ask the host sends, whatever its id."""
    from claude_wheelhouse.host import KEEP_ASK
    assert all(transcript.KEEP_ASKED.search(KEEP_ASK.format(nonce=n)) for n in ("c0ffee12", "0a1b2c3d"))


# a session's conversation read a page at a time from its end (#347)
RELOAD = [user("summary of what came before", isCompactSummary=True),
          {"type": "attachment", "timestamp": AT, "attachment": {"type": "skill_listing", "content": "s" * 500}},
          {"type": "api-request-blob", "timestamp": AT, "blob": "b" * 500}]
BOUNDARY = {"type": "system", "subtype": "compact_boundary", "timestamp": AT}


def said(recs) -> list[str]:
    return [r["type"] if r["type"] in (transcript.OVERSIZE, "system") else r["message"]["content"] for r in recs]


@pytest.mark.parametrize("recs, tail, want, expected, desc", [
    ([user(f"p{i}") for i in range(5)], "", 2, ["p3", "p4"], "the last want said"),
    ([user("p0"), BOUNDARY, *RELOAD, user("p1")], "", 3, ["p0", "system", "p1"],
     "past a compaction's reload, which never shows: its divider kept"),
    ([user("p0"), user("p1")], json.dumps(user("p2"))[:20], 5, ["p0", "p1"], "one still being written: not yet"),
    ([user("p0"), user("x" * 300)], "", 5, ["p0", transcript.OVERSIZE], "a newest record too long: a stand-in"),
    ([user("p0"), user("p1")], "x" * 300, 5, ["p0", "p1", transcript.OVERSIZE], "one being written too long, too"),
    ([user("p0"), user("x" * 300), user("p2")], "", 5, ["p0", "p2"], "an older record too long: left out"),
    ([user("p0", isSidechain=True), user("p1")], "", 5, ["p1"], "a subagent's left out"),
])
def test_a_page_read_back(tmp_path, monkeypatch, recs, tail, want, expected, desc):
    monkeypatch.setattr(transcript, "CHUNK", 16)
    monkeypatch.setattr(transcript, "LINE_MAX", 200)
    path = tmp_path / "s.jsonl"
    path.write_text(lines(*recs) + tail)
    got, start, whole = transcript.read_back(path, want=want)
    assert said(got) == expected, desc
    assert whole == len(lines(*recs).encode()), f"{desc}: the whole records end"
    assert start == 0 or said(transcript.read_back(path, start, want=100)[0])[-1] != expected[0], \
        f"{desc}: the next page ends before this one starts"


@pytest.mark.parametrize("seed", range(4))
def test_pages_read_back_to_the_start_are_the_transcript(tmp_path, monkeypatch, seed):
    """Paged back to its start, whatever the chunk, page and record sizes, a transcript's pages
    put together are the records the pane reads, in order, never one twice, long ones left out."""
    import random
    rnd, path = random.Random(seed), tmp_path / "s.jsonl"
    monkeypatch.setattr(transcript, "LINE_MAX", 120)
    kinds = [lambda: user("x" * rnd.randint(0, 150)), lambda: assistant(TEXT), lambda: BOUNDARY, *[lambda r=r: r for r in RELOAD]]
    for _ in range(200):
        monkeypatch.setattr(transcript, "CHUNK", rnd.randint(1, 300))
        recs = [rnd.choice(kinds)() for _ in range(rnd.randint(0, 30))]
        path.write_text(lines(*recs))
        want = [r for r in recs if transcript.pane_record(r) and len(json.dumps(r)) + 1 <= 120]
        got, start, _ = transcript.read_back(path, want=rnd.randint(1, 5))
        got = [r for r in got if r["type"] != transcript.OVERSIZE]
        while start:
            page, start, _ = transcript.read_back(path, start, want=rnd.randint(1, 5))
            got = page + got
        assert got == want, recs


def session_file(tmp_path, *recs):
    (tmp_path / "p").mkdir(exist_ok=True)
    path = tmp_path / "p/abc.jsonl"
    path.write_text(lines(*recs))
    return path, transcript.Follower("abc", projects=tmp_path)


def test_a_follower_reads_what_is_appended_alone_and_pages_back(tmp_path, monkeypatch):
    monkeypatch.setattr(transcript, "PAGE", 2)
    path, follower = session_file(tmp_path, *[user(f"p{i}") for i in range(5)])
    assert said(follower.read()) == ["p3", "p4"] and follower.more()
    with open(path, "a") as f:
        f.write(lines(user("p5")) + json.dumps(user("p6"))[:10])
    monkeypatch.setattr(transcript, "read_back", lambda *a, **k: pytest.fail("appended: read alone"))
    assert said(follower.read()) == ["p3", "p4", "p5"], "what's appended, after; one being written, not yet"
    monkeypatch.undo()
    monkeypatch.setattr(transcript, "PAGE", 2)
    assert follower.older() and said(follower.recs) == ["p1", "p2", "p3", "p4", "p5"]
    assert follower.older() and said(follower.recs)[0] == "p0" and not follower.more() and not follower.older()


def test_a_follower_grown_past_two_pages_reads_its_last_afresh(tmp_path, monkeypatch):
    monkeypatch.setattr(transcript, "PAGE", 2)
    path, follower = session_file(tmp_path, user("p0"), user("p1"))
    follower.read()
    for i in range(2, 20):
        with open(path, "a") as f:
            f.write(lines(user(f"p{i}")))
        assert said(follower.read())[-1] == f"p{i}" and len(follower.recs) <= 4, "never more than two pages held"


WH_CALL = assistant({"type": "tool_use", "id": "w", "name": "mcp__wheelhouse__post_item", "input": {}})


@pytest.mark.parametrize("recs, pages, depth, expected, more, desc", [
    ([user(f"p{i}") for i in range(7)], 2, 1, ["p3", "p4", "p5", "p6"], True, "a page back, of two read"),
    ([user(f"p{i}") for i in range(7)], 2, 0, ["p5", "p6"], True, "its last page, with two read before it"),
    ([user(f"p{i}") for i in range(7)], 2, 5, ["p1", "p2", "p3", "p4", "p5", "p6"], True, "deeper than read: all read"),
    ([user("p0"), WH_CALL, WH_CALL, WH_CALL, user("p4")], 1, 1, ["p0", "mcp", "mcp", "mcp", "p4"], False,
     "a page showing nothing (the wheelhouse's own calls): read on till one does"),
])
def test_a_followers_pages_each_shown_at_its_own_depth(tmp_path, monkeypatch, recs, pages, depth, expected, more, desc):
    monkeypatch.setattr(transcript, "PAGE", 2)
    _, follower = session_file(tmp_path, *recs)
    follower.read()
    for _ in range(pages):
        follower.older()
    got = [r["message"]["content"] if r["type"] == "user" else "mcp" for r in follower.view(depth)]
    assert (got, follower.more(depth)) == (expected, more), desc


def test_a_followers_pages_let_go_leave_its_last(tmp_path, monkeypatch):
    monkeypatch.setattr(transcript, "PAGE", 2)
    path, follower = session_file(tmp_path, *[user(f"p{i}") for i in range(7)])
    follower.read()
    follower.older()
    follower.older()
    follower.drop()
    assert (said(follower.recs), follower.depth) == (["p5", "p6"], 0)
    assert follower.older() and said(follower.recs) == ["p3", "p4", "p5", "p6"], "read again from where its last starts"


def stage(text, at):
    return Entry("stage", text, at)


EARLY, LATE = "2026-10-07T20:00:00.000Z", "2026-10-07T22:00:00.000Z"


@pytest.mark.parametrize("older, stages, expected, desc", [
    (False, [stage("old", EARLY), stage("new", LATE)], ["old", "p", "new"], "all read: every compaction's lines"),
    (True, [stage("old", EARLY), stage("new", LATE)], [transcript.OLDER, "p", "new"],
     "more before: a compaction before what's read leaves its lines with it"),
    (True, [stage("now", AT), Entry("dock", "art", EARLY)], [transcript.OLDER, "art", "p", "now"],
     "one at the first record's time stays, and the live cycle's overlay always does"),
])
def test_stage_lines_show_with_the_span_they_are_in(older, stages, expected, desc):
    got = transcript.blocks("demo", None, [user("p")], stages=stages, older=older)[1:]
    assert [md.split("\n\n")[-1] for _, md in got] == expected, desc
