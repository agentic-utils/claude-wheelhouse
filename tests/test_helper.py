"""The helper chat's driver (helper.py): its sandboxed command line and environment, what it
is told, and a chat with a fake Claude Code replaying stream-json (no test runs the real
claude): a streamed answer, a tool round, each error, the idle stop and New chat."""

import json
import os
import queue
import sys
import textwrap
from pathlib import Path

import pytest

from claude_wheelhouse import helper, plan_usage

# Claude Code, faked: argv[1] says how it answers. It notes each run's pid, cwd, environment
# and every message it is sent, one JSON line each, in argv[2]; and argv past those, a real
# command line's when started as the helper's claude (claude_md_canary)
FAKE = textwrap.dedent('''
    import json, os, sys
    how, log = sys.argv[1], sys.argv[2]
    def note(**kw):
        with open(log, "a") as f:
            f.write(json.dumps(kw) + "\\n")
    def say(**msg):
        print(json.dumps(msg), flush=True)
    def delta(text):
        say(type="stream_event", event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}})
    note(pid=os.getpid(), cwd=os.getcwd(), env=sorted(os.environ), argv=sys.argv[3:])
    say(type="system", subtype="init")
    for line in sys.stdin:
        content = json.loads(line)["message"]["content"]
        note(pid=os.getpid(), sent=content)
        if how == "die":
            print("API Error: 429 rate limit exceeded", file=sys.stderr, flush=True)
            sys.exit(1)
        if how == "login":
            say(type="result", subtype="success", is_error=True, result="Not logged in · Please run /login")
            continue
        if how == "tool":
            say(type="assistant", message={"content": [{"type": "tool_use", "name": "Grep",
                                                         "input": {"pattern": "alt+p"}}],
                                           "usage": {"input_tokens": 10, "cache_read_input_tokens": 40000}})
            say(type="stream_event", event={"type": "content_block_start", "content_block": {"type": "text"}})
        delta("Press **Alt+P**")
        delta(" in the item list.")
        say(type="assistant", message={"content": [{"type": "text", "text": "Press **Alt+P** in the item list."}],
                                       "usage": {"input_tokens": 5, "cache_creation_input_tokens": 1000,
                                                 "cache_read_input_tokens": 95000}})
        say(type="result", subtype="success", is_error=False, result="Press **Alt+P** in the item list.")
''')


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def chat(tmp_path):
    """make(how) -> (helper, events, log): a Helper on the fake, the Events it gives, and
    what the fake noted."""
    path = tmp_path / "fake_claude.py"
    path.write_text(FAKE)
    log = tmp_path / "log.jsonl"
    made = []

    def make(how="answer", clock=None):
        events = queue.Queue()
        h = helper.Helper(events.put, command_=[sys.executable, str(path), how, str(log)], clock=clock or Clock())
        made.append(h)
        return h, events, log
    yield make
    for h in made:
        h.stop()


def until_end(events: queue.Queue) -> list[helper.Event]:
    """Every event up to and including a done or an error."""
    out = []
    while True:
        out.append(events.get(timeout=10))
        if out[-1].kind in ("done", "error"):
            return out


def noted(log, key):
    return [n[key] for n in map(json.loads, log.read_text().splitlines()) if key in n] if log.exists() else []


@pytest.mark.parametrize("flags, desc", [
    (["--model", helper.MODEL], "Haiku"),
    (["--tools", "Read,Grep,Glob"], "the read-only tools alone"),
    (["--strict-mcp-config"], "no MCP servers: no wheelhouse server, nothing that acts on a session"),
    (["--setting-sources="], "none of the person's settings, hooks or plugins"),
    (["--restricted"], "the file tools confined to the working directories"),
    (["--permission-mode", "dontAsk"], "anything needing permission denied"),
    (["--permission-prompts", "none"], "no one to ask"),
    (["--no-session-persistence"], "no transcript: never in Adopt or the stats"),
    (["--input-format", "stream-json", "--output-format", "stream-json"], "a chat over stream-json"),
    (["--include-partial-messages"], "the answer streams in"),
])
def test_the_command_carries_every_safety_flag(flags, desc, monkeypatch):
    monkeypatch.delenv(plan_usage.CLAUDE_ENV)
    argv = helper.command()
    assert argv[0] == "claude", desc
    at = argv.index(flags[0])
    assert argv[at:at + len(flags)] == flags, desc


def test_the_command_has_no_other_tool_and_reads_only_the_source(monkeypatch):
    argv = helper.command()
    assert argv.count("--tools") == 1 and "--bare" not in argv, "--bare never reads the login"
    assert not {"--mcp-config", "--allowedTools", "--allowed-tools", "--dangerously-skip-permissions"} & set(argv)
    dirs = argv[argv.index("--add-dir") + 1:]
    assert dirs and all(os.path.isdir(d) for d in dirs), "--add-dir last: it takes every argument after it"
    assert all(d.rstrip("/").split("/")[-1].startswith("claude_wheelhouse_") for d in dirs), "the modules' packages alone"


@pytest.mark.parametrize("sdk, says, desc", [
    (False, "SDK mode is off", "tabs: no SDK-only feature advertised"),
    (True, "SDK mode on", "SDK mode on: its features too"),
])
def test_the_prompt_answers_for_the_persons_mode(sdk, says, desc):
    argv = helper.command(sdk)
    assert says in argv[argv.index("--system-prompt") + 1], desc


def test_the_environment_holds_nothing_that_finds_the_daemon():
    base = {"PATH": "/bin", "HOME": "/h", "WHEELHOUSE_DB": "x", "WHEELHOUSE_TOKEN": "t", "WHEELHOUSE_SESSION_ID": "s",
            "WHEELHOUSE_PORT": "1", "CLAUDECODE": "1"}
    env = helper.environment(base)
    assert env == {"PATH": "/bin", "HOME": "/h"}, "no WHEELHOUSE_ variable, nor a parent Claude Code's"


def test_no_claude_md_reaches_the_helper(tmp_path, monkeypatch):
    """#208: the helper's claude as a question really starts it (its own command line, cwd and
    environment, through a fake binary) is given no setting source: user, project and local
    each load that scope's CLAUDE.md, and Claude Code also walks up from its cwd (a real
    ~/projects/CLAUDE.md sits above a checkout), so the empty --setting-sources= is the one
    defence, and this proves it is passed, alone and in one token. Nor is any memory or
    prompt appended, nor anything in its environment that loads one from an --add-dir
    directory, even when the person's shell sets it. That none sits in the directories it
    starts in or is given is checked too, but walking up passes it by: make helper-eval's
    canary checks the whole live."""
    fake, log, claude = tmp_path / "fake_claude.py", tmp_path / "log.jsonl", tmp_path / "claude"
    fake.write_text(FAKE)
    claude.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" answer "{log}" "$@"\n')
    claude.chmod(0o755)
    monkeypatch.setenv(plan_usage.CLAUDE_ENV, str(claude))
    monkeypatch.setenv("CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD", "1")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))
    events = queue.Queue()
    h = helper.Helper(events.put)
    try:
        assert h.ask("How do I pin an item?")
        assert until_end(events)[-1].kind == "done"
    finally:
        h.stop()
    [argv], [cwd], [env] = noted(log, "argv"), noted(log, "cwd"), noted(log, "env")
    assert [a for a in argv if a.startswith("--setting-sources")] == ["--setting-sources="], \
        "no setting source, in one token (a bare --setting-sources would take the next argument)"
    assert not {"--append-system-prompt", "--append-system-prompt-file"} & set(argv), "nothing appended"
    assert "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD" not in env, "none from the --add-dir directories"
    # kept on purpose (launch.KEPT): the person's login is there; its CLAUDE.md is the user
    # scope's, which the empty --setting-sources= leaves out
    assert "CLAUDE_CONFIG_DIR" in env, "the config directory, deliberately"
    dirs = [cwd, *argv[argv.index("--add-dir") + 1:]]   # none where it starts, though that's no defence
    assert not [d for d in dirs for name in ("CLAUDE.md", "CLAUDE.local.md") if (Path(d) / name).exists()]


def test_the_briefing_is_the_fact_sheet_readme_and_protocol():
    text = helper.briefing()
    assert "<fact-sheet>\n# Wheelhouse fact sheet" in text, "the sheet, its stamp left off"
    assert "fact sheet stamp" not in text
    assert "<readme>" in text and "<protocol>" in text


def test_nothing_runs_until_the_first_question(chat):
    h, events, log = chat()
    assert h.proc is None and not log.exists(), "no claude started by making the chat"


def test_a_streamed_answer_and_the_next_question_on_the_same_claude(chat, tmp_path):
    h, events, log = chat()
    assert h.ask("How do I pin an item?")
    got = until_end(events)
    assert [e.text for e in got if e.kind == "text"] == ["Press **Alt+P**", " in the item list."], "streamed"
    assert got[-1] == helper.Event("done", "Press **Alt+P** in the item list.", 96005), "the context, from the usage"
    assert h.history == [("How do I pin an item?", "Press **Alt+P** in the item list.")] and h.full
    first = noted(log, "sent")[0]
    assert first.startswith("<fact-sheet>") and first.endswith("The person asks:\nHow do I pin an item?"), "briefed first"
    assert h.ask("And unpin?")
    until_end(events)
    assert noted(log, "sent")[1] == "And unpin?", "only the question, the briefing already given"
    assert len(set(noted(log, "pid"))) == 1, "one claude for the chat"
    run = json.loads(log.read_text().splitlines()[0])
    assert run["cwd"] == str(helper.PACKAGE), "run in the package directory"
    assert not [k for k in run["env"] if k.startswith("WHEELHOUSE_")], "nothing that finds the daemon"


def test_a_tool_round_says_what_it_reads(chat):
    h, events, _ = chat("tool")
    h.ask("How do I pin an item?")
    got = until_end(events)
    assert helper.Event("tool", "Grep alt+p") in got
    assert got[-1].text == "Press **Alt+P** in the item list." and got[-1].tokens == 96005


@pytest.mark.parametrize("how, says, desc", [
    ("login", "isn't logged in", "an error result: said plainly"),
    ("die", "usage limit", "it dies mid-answer: stderr said plainly"),
])
def test_errors_are_said_plainly(chat, how, says, desc):
    h, events, _ = chat(how)
    h.ask("How do I pin an item?")
    got = until_end(events)
    assert got[-1].kind == "error" and says in got[-1].text, desc
    assert not h.busy and h.history == [], desc


def test_a_missing_claude_is_said(tmp_path):
    events = queue.Queue()
    h = helper.Helper(events.put, command_=[str(tmp_path / "no-such-claude")])
    assert h.ask("How do I pin an item?")
    assert "isn't installed" in events.get(timeout=5).text and h.proc is None and not h.busy


def test_one_question_at_a_time(chat):
    h, events, log = chat()
    assert not h.ask("   "), "nothing to ask"
    h.busy = True   # as while an answer comes
    assert not h.ask("How do I pin an item?") and h.proc is None, "refused while it answers"


def test_an_idle_chat_stops_its_claude_and_the_next_starts_another_told_the_chat(chat):
    clock = Clock()
    h, events, log = chat(clock=clock)
    h.ask("How do I pin an item?")
    until_end(events)
    clock.now += helper.IDLE - 1
    assert not h.idle() and h.proc is not None, "not yet"
    clock.now += 1
    assert h.idle() and h.proc is None, "quiet IDLE seconds: stopped"
    h.ask("And unpin?")
    until_end(events)
    assert len(set(noted(log, "pid"))) == 2, "another claude"
    second = noted(log, "sent")[1]
    assert "<chat-so-far>\n<question>How do I pin an item?</question>" in second and second.startswith("<fact-sheet>")


def test_new_chat_forgets_it(chat):
    h, events, log = chat()
    h.ask("How do I pin an item?")
    until_end(events)
    h.reset()
    assert h.proc is None and h.history == [] and h.tokens == 0
    h.ask("And unpin?")
    until_end(events)
    assert "<chat-so-far>" not in noted(log, "sent")[1], "a new chat starts clean"
