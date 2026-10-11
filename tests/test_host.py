import asyncio
import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from claude_wheelhouse import features, host as host_mod, launch
from claude_wheelhouse.host import (AWAITING_NOTES, CANCELLED, COMPACT_ASK, HOST_QUESTION, NO_NOTES, Host, format_turn,
                                    keep_notes, question_answers, question_item, tool_summary)
from claude_wheelhouse.store import SessionGone
from claude_wheelhouse.monitor import REQUEST_TEXT

pytestmark = pytest.mark.usefixtures("sdk_on")   # hosts are SDK mode's


class FakeClient:
    """Stands in for ClaudeSDKClient: records what the host sends."""

    def __init__(self, options=None):
        self.options = options
        self.queries, self.keys, self.interrupts, self.connected = [], [], 0, False

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def query(self, prompt):
        async for m in prompt:   # as the host sends a turn: one user message, with its uuid
            self.queries.append(m["message"]["content"])
            self.keys.append(m["uuid"])

    def replay(self, i=-1):
        """Claude Code taking up the i-th turn sent: its user message, replayed under its uuid."""
        from claude_agent_sdk import UserMessage
        return UserMessage(content=self.queries[i], uuid=self.keys[i])

    async def interrupt(self):
        self.interrupts += 1

    async def get_context_usage(self):
        return {"totalTokens": 1234, "maxTokens": 200000}


@pytest.fixture
def host(store, sid, monkeypatch):
    monkeypatch.setattr(host_mod, "transcript_exists", lambda sid: True)   # resume: no opening turn
    h = Host(store, sid, client_factory=FakeClient, person="doug")
    asyncio.run(h.connect())
    return h


def run(coro):
    return asyncio.run(coro)


def msg(body, ref=None, kind=None):
    return {"body": body, "item_ref": ref, "kind": kind}


@pytest.mark.parametrize("msgs, expected, desc", [
    ([msg("yes", "Q1")], "[wheelhouse] from doug on Q1:\nyes", "one answer"),
    ([msg("go", None)], "[wheelhouse] from doug (general):\ngo", "a general message"),
    ([msg("a", "Q1"), msg("b", "Q2")], "[wheelhouse] from doug on Q1:\na\n\n[wheelhouse] from doug on Q2:\nb",
     "a batch stays one turn"),
    ([msg("joined", kind="notice")], "[wheelhouse] joined", "the wheelhouse's own notice"),
    ([msg("x" * 2000, "Q1")], "[wheelhouse] from doug on Q1:\n" + "x" * 2000, "no length limit"),
])
def test_format_turn(msgs, expected, desc):
    assert format_turn(msgs, "doug") == expected, desc


@pytest.mark.parametrize("name, inp, expected, desc", [
    ("Bash", {"command": "ls  -la\n/tmp"}, "Bash: ls -la /tmp", "command, whitespace folded"),
    ("Edit", {"file_path": "/a/b.py", "old_string": "x"}, "Edit: /a/b.py", "file path"),
    ("Bash", {"command": "y" * 100}, "Bash: " + "y" * 80 + "…", "long command cut"),
    ("TodoWrite", {"todos": []}, "TodoWrite", "nothing to show"),
])
def test_tool_summary(name, inp, expected, desc):
    assert tool_summary(name, inp) == expected, desc


ONE = {"questions": [{"question": "Which format?", "header": "Format",
                      "options": [{"label": "CSV", "description": "plain"}, {"label": "PDF"}]}]}
TWO = {"questions": [{"question": "Colour?", "options": []}, {"question": "Size?", "options": []}]}


@pytest.mark.parametrize("inp, reply, expected, desc", [
    (ONE, " CSV ", {"Which format?": "CSV"}, "one question takes the reply whole"),
    (TWO, "1: red, 2: large", {"Colour?": "red", "Size?": "large"}, "numbered parts"),
    (TWO, "1: red", {"Colour?": "red", "Size?": "1: red"}, "a missing part gets the whole reply"),
    (TWO, "whatever you think", {"Colour?": "whatever you think", "Size?": "whatever you think"}, "free text"),
    (TWO, "1: red, blue, 2: large", {"Colour?": "red, blue", "Size?": "large"}, "commas inside a part stay"),
    (TWO, "1: red\n2: large", {"Colour?": "red", "Size?": "large"}, "one per line"),
    (TWO, "1: at 2:30, 2: large", {"Colour?": "at 2:30", "Size?": "large"}, "a time isn't a marker"),
    (TWO, "1: red, 7: huge", {"Colour?": "red, 7: huge", "Size?": "1: red, 7: huge"},
     "a number past the last question isn't a marker"),
    (TWO, "Q1: red, q2: large", {"Colour?": "red", "Size?": "large"}, "Q-numbered parts"),
    (TWO, "1:30pm works", {"Colour?": "1:30pm works", "Size?": "1:30pm works"}, "a leading time isn't a marker"),
    (TWO, "1: red, 2: large, 1: and blue", {"Colour?": "red, and blue", "Size?": "large"},
     "a number given twice keeps both parts"),
])
def test_question_answers(inp, reply, expected, desc):
    assert question_answers(inp, reply) == expected, desc


K = '<keep id="ab12">'


@pytest.mark.parametrize("text, expected, desc", [
    (f"{K}\n- Q3\n- PR 17\n</keep>", "- Q3\n- PR 17", "the request's block, tags on lines of their own"),
    ("Saved.\n  <keep id=ab12>\nnotes\n  </keep>  \nDone.", "notes", "unquoted id, indented tags, prose around"),
    (f"{K}\n</keep>", "", "an empty block: nothing to keep, still an answer"),
    (f"{K}\nold\n</keep>\n{K}\nnew\n</keep>", "new", "the last block"),
    (f'I\'ll reply between {K} and </keep>.', None, "the instruction quoted in prose isn't notes"),
    (f'I\'ll reply between {K} and </keep>.\n{K}\n- Q3\n</keep>', "- Q3", "a quoted tag before the real block"),
    (f"{K}notes</keep>", None, "tags not on lines of their own"),
    ("<keep>\nnotes\n</keep>", None, "a block without the id"),
    ('<keep id="zz99">\nold\n</keep>', None, "another request's block"),
    (f"{K}\nnever closed", None, "no complete block"),
    (None, None, "an empty reply"),
])
def test_keep_notes(text, expected, desc):
    assert keep_notes(text, "ab12") == expected, desc


def test_question_item_lists_options():
    title, body = question_item(ONE)
    assert title == "Format"
    assert "- CSV: plain" in body and "- PDF" in body


@pytest.mark.parametrize("setup, expected, desc", [
    (lambda s, sid: s.send(sid, "yes", "Q1"), ["[wheelhouse] from doug on Q1:\nyes"], "a sent answer is a turn"),
    (lambda s, sid: s.queue(sid, "later", "Q1"), [], "a queued draft waits for Send"),
    (lambda s, sid: s.request_end(sid), [REQUEST_TEXT["end"]], "End is passed on as a turn"),
    (lambda s, sid: s.notice(sid, "hello"), ["[wheelhouse] hello"], "a notice"),
    (lambda s, sid: s.command(sid, "compact"), [lambda h: COMPACT_ASK.format(nonce=h.compact_nonce)],
     "Compact asks for the notes first, under its own id"),
])
def test_poll_turns(host, store, sid, setup, expected, desc):
    store.post_item(sid, "question", "q")
    setup(store, sid)
    assert run(host.poll()) is True, desc
    expected = [e(host) if callable(e) else e for e in expected]
    assert host.client.queries == expected, desc
    assert run(host.poll()) is True and host.client.queries == expected, f"{desc}: once only"


@pytest.mark.parametrize("change, desc", [
    (lambda s, sid: s.set_parked(sid, True), "parked"),
    (lambda s, sid: s.end(sid), "ended"),
])
def test_poll_stops(host, store, sid, change, desc):
    change(store, sid)
    assert run(host.poll()) is False, desc


def test_poll_interrupt(host, store, sid):
    store.command(sid, "interrupt")
    run(host.poll())
    assert host.client.interrupts == 1


def ctx(**kw):
    return SimpleNamespace(suggestions=kw.get("suggestions", []), title=None, description=None, decision_reason=None)


async def ask_and_answer(host, store, sid, name, inp, answer, context=None):
    """Run can_use_tool while answering from the store, as the person would."""
    task = asyncio.create_task(host.can_use_tool(name, inp, context or ctx()))
    for _ in range(50):
        await asyncio.sleep(0.01)
        items = [i for i in store.items(sid) if i["status"] == "open"]
        if items:
            break
    answer(store, sid, items[0]["ref"])
    await host.poll()
    return await asyncio.wait_for(task, 5), store.item(sid, items[0]["ref"])


@pytest.mark.parametrize("answer, behavior, keep, message, desc", [
    (lambda s, sid, ref: s.answer_permission(sid, ref, "allow"), "allow", None, None, "allow once"),
    (lambda s, sid, ref: s.answer_permission(sid, ref, "always"), "allow", ["rule"], None, "allow always keeps the rule"),
    (lambda s, sid, ref: s.answer_permission(sid, ref, "deny"), "deny", None,
     "The person denied this in the wheelhouse.", "deny"),
    (lambda s, sid, ref: s.send(sid, "use rg instead", ref), "deny", None, "use rg instead",
     "a message on the item denies with that text"),
])
def test_permission(host, store, sid, answer, behavior, keep, message, desc):
    result, item = run(ask_and_answer(host, store, sid, "Bash", {"command": "grep -r x"}, answer,
                                      ctx(suggestions=["rule"])))
    assert item["kind"] == "permission" and item["title"] == "Bash: grep -r x", desc
    assert result.behavior == behavior, desc
    if behavior == "allow":
        assert result.updated_permissions == keep, desc
    else:
        assert result.message == message, desc


@pytest.mark.parametrize("answer, behavior, desc", [
    (lambda s, sid, ref: s.send(sid, "CSV", ref), "allow", "answered with a message"),
    (lambda s, sid, ref: s.update_item(sid, ref, status="closed"), "deny", "closed unanswered"),
])
def test_ask_user_question(host, store, sid, answer, behavior, desc):
    result, item = run(ask_and_answer(host, store, sid, "AskUserQuestion", ONE, answer))
    assert item["kind"] == "question", desc
    assert result.behavior == behavior, desc
    if behavior == "allow":
        assert result.updated_input["answers"] == {"Which format?": "CSV"}, desc
        assert item["status"] == "answered", desc
    assert host.client.queries == [], f"{desc}: the answer is not also a turn"


def test_finished_reports_compaction(host, store, sid):
    host.compacted_from = 48174
    run(host.finished(SimpleNamespace(result="", is_error=False, subtype="success")))
    assert store.session(sid)["activity"] == "idle · compacted 48k → 1k tokens"
    assert host.compacted_from is None


def test_finished_records_context(host, store, sid):
    run(host.finished(SimpleNamespace(result="", is_error=False, subtype="success")))
    s = store.session(sid)
    assert (s["activity"], s["context_tokens"], s["context_max"]) == ("idle", 1234, 200000)
    assert abs(datetime.fromisoformat(s["context_at"]).timestamp() - time.time()) < 60, "and when (#54)"


def test_stale_permission_denied_on_start(host, store, sid):
    ref = store.post_item(sid, "permission", "Bash: ls")
    host.deny_stale()
    item = store.item(sid, ref)
    assert item["status"] == "denied" and json.loads(item["answer"])["message"] == host_mod.LOST_ON_RESTART


def test_options(host, store, sid):
    opts = host.options(resume=True)
    assert opts.resume == sid and opts.session_id is None
    assert opts.system_prompt["append"] == launch.PROTOCOL_SDK
    assert opts.mcp_servers["wheelhouse"]["env"]["WHEELHOUSE_SESSION_ID"] == sid
    assert opts.permission_mode is None   # the user's own settings decide
    new = host.options(resume=False)
    assert new.session_id == sid and new.resume is None


@pytest.mark.parametrize("runner, opened, desc", [
    ("sdk", "host", "a host for sdk"),
    ("tab", "tab", "a tab for tab"),
])
def test_open_session(store, tmp_path, monkeypatch, runner, opened, desc):
    sid = store.create_session(str(tmp_path), runner=runner)
    seen = []
    monkeypatch.setattr(launch, "open_host", lambda s, i: seen.append("host"))
    monkeypatch.setattr(launch, "open_tab", lambda s, i: seen.append("tab"))
    launch.open_session(store, sid)
    assert seen == [opened], desc


@pytest.mark.parametrize("runner, on, hosted, desc", [
    ("sdk", False, True, "an existing SDK session with SDK mode off: Relaunch of a running host keeps working"),
    ("sdk", True, True, "an SDK session with it on"),
    ("tab", False, False, "a tab session, SDK mode off: refused"),
    ("tab", True, False, "a tab session, even with it on: refused"),
])
def test_the_host_command_hosts_only_sdk_sessions(store, tmp_path, monkeypatch, runner, on, hosted, desc):
    monkeypatch.setattr(features, "_flag", on)
    sid = store.create_session(str(tmp_path), runner=runner)
    ran = []

    class FakeHost:
        def __init__(self, store, sid):
            ran.append(sid)

        async def run(self):
            pass
    monkeypatch.setattr(host_mod, "Host", FakeHost)
    monkeypatch.chdir(tmp_path)   # main moves into the session's directory: put back after
    if hosted:
        host_mod.main(sid)
    else:
        with pytest.raises(SystemExit, match="runs in a tab, not in a host"):
            host_mod.main(sid)
    assert ran == ([sid] if hosted else []), desc


@pytest.mark.parametrize("on, env, expected, desc", [
    (True, None, "sdk", "default"),
    (True, "tab", "tab", "WHEELHOUSE_RUNNER=tab"),
    (False, None, "tab", "SDK mode off: a tab"),
    (False, "sdk", "tab", "WHEELHOUSE_RUNNER=sdk doesn't turn SDK mode on by itself"),
])
def test_default_runner(store, tmp_path, monkeypatch, on, env, expected, desc):
    monkeypatch.setattr(features, "_flag", on)
    if env:
        monkeypatch.setenv("WHEELHOUSE_RUNNER", env)
    else:
        monkeypatch.delenv("WHEELHOUSE_RUNNER", raising=False)
    assert store.session(store.create_session(str(tmp_path)))["runner"] == expected, desc


@pytest.mark.parametrize("protocol, has, lacks, desc", [
    (launch.PROTOCOL_SDK, "your user turns", "get_input", "the SDK protocol has no monitor"),
    (launch.PROTOCOL, "get_input", "your user turns", "the tab protocol keeps it"),
    (launch.PROTOCOL_SDK, "update_item(ref, status=...)", "reply(ref, text, status)",
     "the SDK protocol asks for the status alone: the host posts the chat"),
    (launch.PROTOCOL, "reply(ref, text, status)", "update_item(ref, status=...)", "the tab protocol replies as before"),
])
def test_protocol_variants(protocol, has, lacks, desc):
    assert has in protocol and lacks not in protocol, desc
    assert "## Decisions" in protocol, desc


def test_sessions_cannot_post_permissions(store, sid, monkeypatch):
    from claude_wheelhouse import mcp_server
    monkeypatch.setattr(mcp_server, "_store", store)
    monkeypatch.setattr(mcp_server, "_sid", sid)
    post_item = next(t for t in mcp_server.TOOLS if t.__name__ == "post_item")
    with pytest.raises(ValueError, match="kind must be"):
        post_item("permission", "x")


def test_shell_hands_over_and_takes_back(host, store, sid, monkeypatch):
    """Shell: the client goes and the session is free before the tab opens; the host takes
    the session back once the tab has exited."""
    opened, states = [], iter(["starting", "live", "live", "dead"])
    monkeypatch.setattr(host_mod, "SHELL_POLL_SECONDS", 0)
    monkeypatch.setattr(host_mod.liveness, "status", lambda session: next(states))
    monkeypatch.setattr(host, "register", lambda: True)
    first = host.client

    def open_tab(s, i):
        assert not first.connected and s.session(i)["claude_pid"] is None and s.session(i)["shell"] == "tab"
        opened.append(i)
    host.open_tab = open_tab
    store.command(sid, "shell")

    async def go():
        host.reader = asyncio.create_task(asyncio.sleep(3600))
        await host.poll()
        host.reader.cancel()
    run(go())
    assert opened == [sid]
    assert host.client is not first and host.client.connected
    assert store.session(sid)["shell"] is None


def tab_wrote(path, *recs):
    with open(path, "a") as f:
        for rec in recs:
            f.write(json.dumps({"timestamp": datetime.now().astimezone().isoformat()} | rec) + "\n")


def turn(mid, model):
    return {"type": "assistant", "requestedModel": model, "effort": "high",
            "message": {"id": mid, "model": model, "usage": {"input_tokens": 5, "output_tokens": 1}}}


def typed(command, out):
    return {"type": "system", "subtype": "local_command", "commandRun": {"command": command, "args": ""},
            "content": f"<local-command-stdout>{out}</local-command-stdout>"}


def picked(out):
    """A choice made in /model's picker, as Claude Code 2.1.280 writes it (real shapes, text
    scrubbed): a caveat, the command with no args, then a user record holding only the
    command's output, no command named."""
    base = {"parentUuid": "p", "isSidechain": False, "promptId": "q", "type": "user", "userType": "external",
            "entrypoint": "cli", "cwd": "/repo", "version": "2.1.280", "gitBranch": "HEAD"}
    return [base | {"message": {"role": "user", "content": "<local-command-caveat>Caveat: ...</local-command-caveat>"},
                    "isMeta": True, "uuid": "u1"},
            base | {"message": {"role": "user", "content": "<command-name>/model</command-name>\n            "
                                "<command-message>model</command-message>\n            <command-args></command-args>"},
                    "uuid": "u2"},
            base | {"message": {"role": "user", "content": f"<local-command-stdout>{out}</local-command-stdout>"},
                    "uuid": "u3"}]


@pytest.mark.parametrize("in_tab, chosen, launched, desc", [
    ([turn("t1", "claude-sonnet-5-5")], "claude-haiku-5-5", ("claude-haiku-5-5", "high"),
     "Haiku chosen in the wheelhouse meanwhile, the tab's turn on what it launched with: Haiku stands"),
    ([typed("model", "Set model to `Haiku 5.5` for this session only"),
      typed("effort", "Set effort level to low (this session only)")], None, ("claude-haiku-5-5", "low"),
     "a /model and /effort typed, then /exit with no turn: the host launches with them"),
    ([], None, ("claude-sonnet-5-5", "high"), "nothing in the tab: not the hosted turns from before it opened"),
])
def test_the_host_takes_back_what_its_shell_tab_ran(store, sid, tmp_path, monkeypatch, in_tab, chosen, launched, desc):
    """Q56 in a shell hand-over: the tab's transcript since it opened, read as the host takes
    the session back and before it launches again."""
    folder = tmp_path / "projects" / "-repo"
    folder.mkdir(parents=True)
    path = folder / f"{sid}.jsonl"
    tab_wrote(path, turn("h1", "claude-opus-5-5"))   # hosted, on Opus
    monkeypatch.setattr(host_mod.transcript, "PROJECTS", tmp_path / "projects")
    monkeypatch.setattr(host_mod, "transcript_exists", lambda sid: True)
    monkeypatch.setattr(host_mod, "SHELL_POLL_SECONDS", 0)
    monkeypatch.setattr(host_mod.liveness, "status", lambda session: "dead")
    store.set_model(sid, "claude-sonnet-5-5")
    store.set_effort(sid, "high")
    h = Host(store, sid, client_factory=FakeClient, person="doug")
    run(h.connect())
    monkeypatch.setattr(h, "register", lambda: True)

    def open_tab(s, i):
        if chosen:
            s.set_model(i, chosen)   # hub.set_model during the shell
        tab_wrote(path, *in_tab)
    h.open_tab = open_tab
    store.command(sid, "shell")

    async def go():
        h.reader = asyncio.create_task(asyncio.sleep(3600))
        await h.poll()
        h.reader.cancel()
    run(go())
    s = store.session(sid)
    assert (s["model"], s["effort"]) == launched, desc
    assert (h.client.options.model, h.client.options.effort) == launched, f"{desc}: what the host launched with"


def test_a_locked_database_as_the_host_relaunches_is_waited_out(host, store, sid, monkeypatch):
    """Taking a session back from its shell tab, the host's first write before launching
    retries through a locked database, as the hand-over's others do."""
    monkeypatch.setattr(host_mod, "STEADY_TRIES", (0, 0))
    set_applied, fails = store.set_applied, iter([sqlite3.OperationalError("database is locked")])

    def flaky(*a):
        if (e := next(fails, None)) is not None:
            raise e
        set_applied(*a)
    monkeypatch.setattr(store, "set_applied", flaky)
    run(host.connect())
    assert host.client.connected


def shell_host(store, sid, tmp_path, monkeypatch):
    """A host on a session chosen as Sonnet at high, with a transcript a shell tab writes to,
    and Claude Code's process already gone once the tab opens."""
    folder = tmp_path / "projects" / "-repo"
    folder.mkdir(parents=True)
    path = folder / f"{sid}.jsonl"
    tab_wrote(path, turn("h1", "claude-sonnet-5-5"))   # hosted
    monkeypatch.setattr(host_mod.transcript, "PROJECTS", tmp_path / "projects")
    monkeypatch.setattr(host_mod, "transcript_exists", lambda sid: True)
    monkeypatch.setattr(host_mod, "SHELL_POLL_SECONDS", 0)
    store.set_model(sid, "claude-sonnet-5-5")
    store.set_effort(sid, "high")
    h = Host(store, sid, client_factory=FakeClient, person="doug")
    run(h.connect())
    return h, path


@pytest.mark.parametrize("steps, launched, desc", [
    ([("choose", "claude-haiku-5-5"), ("type", "Opus 5.5")], "claude-opus-5-5",
     "Haiku chosen in the wheelhouse, then /model opus typed in the tab: the later, the tab's, wins"),
    ([("type", "Opus 5.5"), ("choose", "claude-haiku-5-5")], "claude-haiku-5-5",
     "/model opus typed in the tab, then Haiku chosen in the wheelhouse: the later, the wheelhouse's, wins"),
    ([("choose", "claude-haiku-5-5"), ("turn", "claude-sonnet-5-5")], "claude-haiku-5-5",
     "Haiku chosen, then a turn in the tab on what it launched with: nothing typed, Haiku stands"),
    ([("choose", "claude-haiku-5-5"), ("pick", "Opus 5.5 (1M context)")], "claude-opus-5-5",
     "Haiku chosen, then Opus picked in /model's picker in the tab: the later, the tab's, wins"),
])
def test_the_later_of_a_wheelhouse_choice_and_a_tabs_model_wins(store, sid, tmp_path, monkeypatch, steps, launched,
                                                                 desc):
    """Last write wins, by time (round 62, M2): what the host launches with on taking the
    session back from its shell tab."""
    h, path = shell_host(store, sid, tmp_path, monkeypatch)
    monkeypatch.setattr(host_mod.liveness, "status", lambda session: "dead")
    monkeypatch.setattr(h, "register", lambda: True)

    def open_tab(s, i):
        for what, value in steps:
            time.sleep(0.002)   # each a moment after the last
            if what == "choose":
                s.set_model(i, value)   # hub.set_model during the shell
            elif what == "type":
                tab_wrote(path, typed("model", f"Set model to `{value}` for this session only"))
            elif what == "pick":
                tab_wrote(path, *picked(f"Set model to `{value}` and saved as your default for new sessions"))
            else:
                tab_wrote(path, {"type": "user", "message": {"content": "go on"}}, turn("t1", value))
    h.open_tab = open_tab
    store.command(sid, "shell")

    async def go():
        h.reader = asyncio.create_task(asyncio.sleep(3600))
        await h.poll()
        h.reader.cancel()
    run(go())
    assert store.session(sid)["model"] == launched, desc
    assert h.client.options.model == launched, f"{desc}: what the host launched with"


@pytest.mark.parametrize("how, desc", [
    ("died", "the host died while its shell tab was open: a Restore's host, after the tab exits, carries it"),
    ("restored", "a Restore's host takes the session while the first still waits on the tab: it carries it"),
])
def test_a_host_taking_a_shell_session_carries_what_its_tab_typed(store, sid, tmp_path, monkeypatch, how, desc):
    """Round 62, bug 1: nothing about the shell tab lives only in the first host's memory."""
    first, path = shell_host(store, sid, tmp_path, monkeypatch)
    monkeypatch.setattr(host_mod, "POLL_SECONDS", 0.01)
    status = {"now": "live"}
    monkeypatch.setattr(host_mod.liveness, "status", lambda session: status["now"])
    first.open_tab = lambda s, i: tab_wrote(path, typed("model", "Set model to `Haiku 5.5` for this session only"))
    store.command(sid, "shell")

    async def go():
        first.reader = asyncio.create_task(asyncio.sleep(3600))
        handing = asyncio.create_task(first.poll())
        while store.session(sid)["shell"] != "tab":
            await asyncio.sleep(0.01)
        if how == "died":
            handing.cancel()
            status["now"] = "dead"   # the tab exits, with no host to take the session back
        second = Host(store, sid, client_factory=FakeClient, person="doug")
        task = asyncio.create_task(second.run())
        await asyncio.sleep(0.1)
        store.set_parked(sid, True)
        await asyncio.wait_for(task, 5)
        if how == "restored":
            await asyncio.wait_for(handing, 5)
            assert first.stopping, f"{desc}: the first host lets go"
        first.reader.cancel()
        return second
    second = run(go())
    assert store.session(sid)["model"] == "claude-haiku-5-5", desc
    assert second.client is None and second.applied[0] == "claude-haiku-5-5", f"{desc}: what the second launched with"


def test_a_locked_database_after_connecting_never_stops_the_host(host, store, sid, monkeypatch):
    """Round 62, bug 2: Claude Code connected, the activity line waits out a locked database
    (steady), and one still locked past that is written by the next poll, not fatal."""
    monkeypatch.setattr(host_mod, "STEADY_TRIES", (0, 0))
    set_activity, fails = store.set_activity, iter([sqlite3.OperationalError("database is locked")] * 6)

    def flaky(*a):
        if (e := next(fails, None)) is not None:
            raise e
        set_activity(*a)
    monkeypatch.setattr(store, "set_activity", flaky)
    h = Host(store, sid, client_factory=FakeClient, person="doug")
    run(h.connect())   # "starting" and "idle" each given up on, after three tries
    assert h.client.connected and h.unsaid == "idle"
    run(h.poll())
    assert store.session(sid)["activity"] == "idle" and h.unsaid is None


@pytest.mark.parametrize("how, desc", [
    ("park", "parked while it waits on a locked database: it stops, never launching"),
    ("stop", "told to stop while it waits: it stops, never launching"),
    ("park_request", "Park pressed while it waits: it keeps waiting, then passes it on once the lock clears"),
    ("end_request", "End pressed while it waits: it keeps waiting, then passes it on once the lock clears"),
    ("malformed", "a database error other than a lock: said as the database's, not Claude Code's"),
])
def test_a_host_carrying_back_at_start_stops_when_it_should_and_says_why(store, sid, monkeypatch, how, desc):
    """Round 64, M3: carry_back's retries through a locked database are visible and end.
    Round 71: a Park or End pressed meanwhile is honoured by poll, not dropped by stopping."""
    monkeypatch.setattr(host_mod, "STEADY_TRIES", (0, 0))
    monkeypatch.setattr(host_mod, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(host_mod, "transcript_exists", lambda sid: True)
    said, set_activity = [], store.set_activity
    monkeypatch.setattr(store, "set_activity", lambda i, a: (said.append(a), set_activity(i, a)))
    locked, carry = {"on": True}, host_mod.stats.carry

    def flaky(*a):
        if how == "malformed":
            raise sqlite3.DatabaseError("database disk image is malformed")
        if locked["on"]:
            raise sqlite3.OperationalError("database is locked")
        return carry(*a)
    monkeypatch.setattr(host_mod.stats, "carry", flaky)
    h = Host(store, sid, client_factory=StreamClient, person="doug")

    async def until(done):
        while not done():
            await asyncio.sleep(0.01)

    async def go():
        task = asyncio.create_task(h.run())
        await asyncio.sleep(0.05)
        if how != "malformed":   # under load: waiting before it's asked to stop
            await asyncio.wait_for(until(lambda: said or task.done()), 5)
        if how == "park":
            store.set_parked(sid, True)
        elif how == "stop":
            h.stopping = True
        elif how.endswith("_request"):
            what = how.split("_")[0]
            store.request(sid, what)
            await asyncio.sleep(0.1)
            assert not task.done() and h.client is None, f"{desc}: still waiting, not launched"
            locked["on"] = False
            await asyncio.wait_for(until(lambda: h.client and REQUEST_TEXT[what] in h.client.queries), 5)
            h.stopping = True
        await asyncio.wait_for(task, 5)
    run(go())
    assert h.client is None, desc
    if how == "malformed":
        assert said == ["stopped: the wheelhouse database failed at start: database disk image is malformed"], desc
    else:
        assert said[0] == "waiting on a locked database" and said.count(said[0]) == 1, desc


def test_carry_back_reads_the_transcript_off_the_event_loop(host, monkeypatch):
    """Round 64, nit 6: a whole transcript, maybe hundreds of MB, read on a worker thread."""
    import threading
    threads = []
    monkeypatch.setattr(host_mod.stats, "typed_in",
                        lambda path: (threads.append(threading.current_thread()), host_mod.stats.Snapshot())[1])
    assert run(host.carry_back())
    assert threads and threads[0] is not threading.main_thread()


# review fixes (T29): cancelled calls, ordering, races, lost turns, the shell hand-back

async def until_open(store, sid):
    for _ in range(50):
        await asyncio.sleep(0.01)
        items = [i for i in store.items(sid) if i["status"] == "open"]
        if items:
            return items[0]
    raise AssertionError("no item opened")


@pytest.mark.parametrize("name, inp, status, desc", [
    ("Bash", {"command": "ls"}, "denied", "a permission"),
    ("AskUserQuestion", ONE, "closed", "a question from Claude's dialog"),
])
def test_cancelled_call_closes_its_item(host, store, sid, name, inp, status, desc):
    """Claude Code withdrawing the call (interrupt, or the host letting go for a shell tab)
    closes the item, and a message typed on it afterwards becomes a turn, not lost."""
    async def go():
        task = asyncio.create_task(host.can_use_tool(name, inp, ctx()))
        item = await until_open(store, sid)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        store.send(sid, "do it this way", item["ref"])
        await host.poll()
        return item["ref"]
    ref = run(go())
    item = store.item(sid, ref)
    assert item["status"] == status, desc
    assert not host.asking and not host.waiting, desc
    assert host.client.queries == [f"[wheelhouse] from doug on {ref}:\ndo it this way"], desc


def test_reply_then_close_answers(host, store, sid):
    """Sent just before X: the reply still counts as the answer."""
    def answer(s, i, ref):
        s.send(i, "CSV", ref)
        s.update_item(i, ref, status="closed")
    result, _ = run(ask_and_answer(host, store, sid, "AskUserQuestion", ONE, answer))
    assert result.behavior == "allow" and result.updated_input["answers"] == {"Which format?": "CSV"}


def test_button_beats_message_race(host, store, sid):
    """Allow lands first, a message on the same item in the same pass: the message is a
    turn, and the host doesn't crash on the item no longer being open."""
    def answer(s, i, ref):
        s.send(i, "and then run the tests", ref)
        s.answer_permission(i, ref, "allow")
    result, item = run(ask_and_answer(host, store, sid, "Bash", {"command": "ls"}, answer))
    assert result.behavior == "allow" and item["status"] == "allowed"
    assert host.client.queries == [f"[wheelhouse] from doug on {item['ref']}:\nand then run the tests"]


class FailingClient(FakeClient):
    async def query(self, text):
        raise ConnectionError("Claude Code exited")


def test_turn_not_sent_stays_undelivered(store, sid, monkeypatch):
    monkeypatch.setattr(host_mod, "transcript_exists", lambda sid: True)
    h = Host(store, sid, client_factory=FailingClient, person="doug")
    run(h.connect())
    store.send(sid, "hello")
    with pytest.raises(ConnectionError):
        run(h.poll())
    assert [m["body"] for m in store.pending(sid)] == ["hello"]
    assert [m["body"] for m in store.claim(sid)] == ["hello"], "released, so the next host takes it"


def test_deny_stale_closes_only_what_the_host_asked(host, store, sid):
    perm = store.post_item(sid, "permission", "Bash: ls")
    asked = store.post_item(sid, "question", "Format")
    store.set_answer(sid, asked, HOST_QUESTION)
    own = store.post_item(sid, "question", "The session's own question")
    host.deny_stale()
    assert [store.item(sid, r)["status"] for r in (perm, asked, own)] == ["denied", "closed", "open"]


class StreamClient(FakeClient):
    """A client whose messages the test feeds in."""

    def __init__(self, options=None):
        super().__init__(options)
        self.feed = asyncio.Queue()

    async def receive_messages(self):
        while (m := await self.feed.get()) is not None:
            yield m


def result(text="", error=False, origin=None, terminal_reason=None):
    from claude_agent_sdk import ResultMessage
    return ResultMessage(subtype="error_during_execution" if error else "success", duration_ms=1, duration_api_ms=1,
                         is_error=error, num_turns=1, session_id="x", result=text, origin=origin,
                         terminal_reason=terminal_reason)


def own(text=""):
    """The end of a turn Claude Code started itself, as it says so (a background task's notification)."""
    return result(text, origin={"kind": "task-notification"})


def test_read(store, sid, monkeypatch):
    """The reader counts turns down, records a compaction, and survives a locked database."""
    import sqlite3
    from claude_agent_sdk import AssistantMessage, SystemMessage
    from claude_agent_sdk.types import ToolUseBlock
    monkeypatch.setattr(host_mod, "transcript_exists", lambda sid: True)
    h = Host(store, sid, client_factory=StreamClient, person="doug")
    real = store.set_activity
    locked = iter([True])

    def flaky(s, text):
        if text.startswith("running") and next(locked, False):
            raise sqlite3.OperationalError("database is locked")
        real(s, text)
    monkeypatch.setattr(store, "set_activity", flaky)

    async def go():
        await h.connect()
        await h.turn("one")
        await h.turn("two")
        for m in (h.client.replay(0),
                  AssistantMessage(content=[ToolUseBlock(id="1", name="Bash", input={"command": "ls"})], model="m"),
                  SystemMessage(subtype="compact_boundary", data={"compact_metadata": {"pre_tokens": 48000}}),
                  result(), h.client.replay(1),
                  AssistantMessage(content=[ToolUseBlock(id="2", name="Bash", input={"command": "pwd"})],
                                   model="m"), result(), None):
            h.client.feed.put_nowait(m)
        await h.read()
    run(go())
    assert h.pending == 0
    assert store.session(sid)["activity"] == "idle"
    assert store.session(sid)["context_tokens"] == 1234


@pytest.mark.parametrize("ending, activity, desc", [
    ("crash", "stopped: Claude Code died", "Claude Code dying leaves the reason"),
    ("end", None, "an ended session stops quietly"),
    ("gone", None, "force-ended mid-pass: no crash"),
])
def test_run_stops(store, sid, monkeypatch, ending, activity, desc):
    monkeypatch.setattr(host_mod, "transcript_exists", lambda sid: True)
    monkeypatch.setattr(host_mod, "POLL_SECONDS", 0.01)
    h = Host(store, sid, client_factory=StreamClient, person="doug")
    store.set_shell(sid, "tab")   # left by a host killed while its shell tab was open

    async def go():
        task = asyncio.create_task(h.run())
        await asyncio.sleep(0.05)
        assert store.session(sid)["shell"] is None, f"{desc}: run() clears a stale shell flag"
        if ending == "crash":
            async def boom():
                raise RuntimeError("Claude Code died")
            h.reader.cancel()
            h.reader = asyncio.create_task(boom())
        elif ending == "end":
            store.set_parked(sid, True)
        else:
            monkeypatch.setattr(h, "poll", lambda: (_ for _ in ()).throw(SessionGone(sid)))
        await asyncio.wait_for(task, 5)
    run(go())
    if activity:
        assert store.session(sid)["activity"] == activity, desc


def test_run_refuses_a_taken_session(store, sid, monkeypatch):
    h = Host(store, sid, client_factory=StreamClient, person="doug")
    monkeypatch.setattr(h, "register", lambda: False)
    with pytest.raises(SystemExit, match="already running"):
        run(h.run())


def liveness_start(pid):
    return host_mod.liveness.start_time(pid)


@pytest.mark.parametrize("runner, shell, takes, desc", [
    ("sdk", None, False, "hosted: messages arrive as user turns, get_input takes none"),
    ("sdk", "tab", True, "handed to a shell tab: its monitor and get_input deliver"),
    ("tab", None, True, "a tab session as before"),
])
def test_get_input_hosted(store, tmp_path, monkeypatch, runner, shell, takes, desc):
    from claude_wheelhouse import mcp_server
    sid = store.create_session(str(tmp_path), runner=runner)
    store.set_shell(sid, shell)
    monkeypatch.setattr(mcp_server, "_store", store)
    monkeypatch.setattr(mcp_server, "_sid", sid)
    get_input = next(t for t in mcp_server.TOOLS if t.__name__ == "get_input")
    ref = store.post_item(sid, "question", "q")
    store.send(sid, "general")
    store.send(sid, "on q", ref)
    out = get_input()
    assert ("general" in out) == takes, desc
    assert (out == mcp_server.HOSTED_TEXT) != takes, desc
    get_input(ref=ref)
    assert bool(store.pending(sid)) != takes, f"{desc}: undelivered messages stay for the host"


@pytest.mark.parametrize("resuming, desc", [(True, "a restore"), (False, "a new session")])
def test_open_host_detaches(store, sid, monkeypatch, resuming, desc):
    """The host is started through setsid --fork so it is never the TUI's child (and never
    its zombie), with its output logged; a restore gets the joined notice."""
    calls = []
    monkeypatch.setattr(launch, "transcript_exists", lambda i: resuming)
    monkeypatch.setattr(launch.subprocess, "run", lambda argv, **kw: calls.append((argv, kw)))
    launch.open_host(store, sid)
    argv, kw = calls[0]
    assert argv[:2] == ["setsid", "--fork"] and argv[-2:] == ["host", sid], desc
    assert kw["check"] and kw["stdin"] is launch.subprocess.DEVNULL, desc
    log = store.path.parent / "hosts" / f"{sid}.log"
    assert ("resume" if resuming else "new") in log.read_text(), desc
    assert any(m["kind"] == "notice" for m in store.pending(sid)) == resuming, desc


# round-2 review fixes (T30): Compact by id, the shell hand-back, redelivery, withdrawn answers

def text(t):
    from claude_agent_sdk import AssistantMessage
    from claude_agent_sdk.types import TextBlock
    return AssistantMessage(content=[TextBlock(text=t)], model="m")


@pytest.fixture
def streamed(store, sid, monkeypatch):
    monkeypatch.setattr(host_mod, "transcript_exists", lambda sid: True)
    h = Host(store, sid, client_factory=StreamClient, person="doug")
    asyncio.run(h.connect())
    return h


NOTES = 'Saved.\n<keep id="{nonce}">\n- Q3 open\n- PR 17\n</keep>'
COMPACTED = ["/compact - Q3 open\n- PR 17"]


REPLAY = "replay"   # in a stream: Claude Code taking up the turn last sent


@pytest.mark.parametrize("stream, sent, activity, desc", [
    ([REPLAY, text(NOTES), result()], COMPACTED, "compacting", "the ask's reply carries the notes"),
    ([own("background task done"), REPLAY, text(NOTES), result()], COMPACTED, "compacting",
     "a turn Claude Code started itself ends first"),
    ([result("overloaded", error=True), REPLAY, text(NOTES), result()], COMPACTED, "compacting",
     "an error in a turn queued ahead doesn't cancel it"),
    ([REPLAY, result(NOTES)], COMPACTED, "compacting", "notes only in the turn's result"),
    ([REPLAY, text('Saved.\n<keep id="{nonce}">\n- Q3 open'), text("- PR 17\n</keep>"), result()], COMPACTED,
     "compacting", "a block split across text blocks"),
    ([REPLAY, text('<keep id="{nonce}">\n</keep>'), result()], ["/compact"], "compacting",
     "an empty block: compact with no instructions"),
    ([REPLAY, text('I\'ll put them between <keep id="{nonce}"> and </keep>.'), result()], [], AWAITING_NOTES,
     "the instruction quoted in prose isn't the notes"),
    ([REPLAY, text("<keep>\n- Q3\n</keep>"), result()], [], AWAITING_NOTES, "a block without the id isn't the notes"),
    ([REPLAY, text("I'll save first."), result()], [], AWAITING_NOTES, "no notes yet: it keeps waiting"),
])
def test_compact_by_id(streamed, store, sid, stream, sent, activity, desc):
    """Through the real read(): the notes go to /compact whichever turn they arrive in."""
    h = streamed
    store.command(sid, "compact")

    async def go():
        await h.poll()
        nonce = h.compact_nonce
        for m in stream:
            if m == REPLAY:
                m = h.client.replay()
            elif hasattr(m, "result") and m.result:
                m.result = m.result.format(nonce=nonce)
            elif hasattr(m, "content"):
                for b in m.content:
                    b.text = b.text.format(nonce=nonce)
            h.client.feed.put_nowait(m)
        h.client.feed.put_nowait(None)
        await h.read()
        return nonce
    nonce = run(go())
    assert h.client.queries[1:] == sent, desc
    assert (h.compact_nonce is None) == bool(sent), desc
    assert store.session(sid)["activity"] == activity, desc
    assert nonce and h.client.queries[0] == COMPACT_ASK.format(nonce=nonce), desc


async def feed(h, *msgs):
    """Hand the running reader some of Claude Code's messages and let it take them in."""
    for m in msgs:
        h.client.feed.put_nowait(m)
    for _ in range(20):
        await asyncio.sleep(0)


def tool(command):
    from claude_agent_sdk import AssistantMessage
    from claude_agent_sdk.types import ToolUseBlock
    return AssistantMessage(content=[ToolUseBlock(id=command, name="Bash", input={"command": command})], model="m")


@pytest.mark.parametrize("running, desc", [
    (1, "pressed during a long turn: the ask waits behind it"),
    (0, "pressed while idle, then Claude Code starts a turn of its own"),
])
def test_compact_waits_out_a_long_turn(streamed, store, sid, monkeypatch, running, desc):
    """Only quiet time counts towards Compact's timeout, so a turn of any length never times
    it out, and never has its activity overwritten; the notes, when they come, compact."""
    monkeypatch.setattr(host_mod, "COMPACT_TIMEOUT", 0)   # any quiet moment would time it out
    h = streamed

    async def go():
        reader = asyncio.create_task(h.read())
        if running:
            await h.turn("a long one")
        store.command(sid, "compact")
        await h.poll()
        nonce = h.compact_nonce
        if not running:   # the ask's turn ends without notes, then a turn Claude Code started itself
            await feed(h, h.client.replay(), result())
            await feed(h, tool("sleep 900"))
        else:
            await feed(h, h.client.replay(0), tool("sleep 900"))
        for _ in range(3):   # time passes while that turn runs
            await h.poll()
            assert h.compact_nonce == nonce, desc
            assert store.session(sid)["activity"] == "running Bash: sleep 900", desc
        await feed(h, result() if running else own(), *([h.client.replay(1)] if running else []),
                   text(NOTES.format(nonce=nonce)), result() if running else own())
        await feed(h, None)
        await reader
    run(go())
    assert h.client.queries[-1:] == COMPACTED, desc


@pytest.mark.parametrize("ending, activity, desc", [
    ("interrupt", "interrupted", "Interrupt cancels Compact"),
    ("timeout", NO_NOTES, "quiet for the whole timeout with no notes: give up and say so"),
])
def test_compact_abandoned(streamed, store, sid, ending, activity, desc):
    h = streamed

    async def go():
        reader = asyncio.create_task(h.read())
        store.command(sid, "compact")
        await h.poll()
        await feed(h, h.client.replay(), text("Nothing to keep."), result())   # the ask's turn ends without notes
        if ending == "interrupt":
            store.command(sid, "interrupt")
        else:
            h.compact_idle -= host_mod.COMPACT_TIMEOUT + 1
        await h.poll()
        await feed(h, None)
        await reader
    run(go())
    assert h.compact_nonce is None, desc
    assert store.session(sid)["activity"] == activity, desc


def other_owner(s, i):
    """A Restore's host: registered as a live process, shell flag cleared, its own activity."""
    s.register(i, 1, liveness_start(1), host_mod.liveness.boot_id())
    s.set_shell(i, None)
    s.set_activity(i, "new owner")


@pytest.mark.parametrize("after_exit, takes_back, desc", [
    ([], True, "the tab exits: the host takes the session back"),
    ([other_owner], False, "a Restore's host took it as the tab exited: let go quietly"),
    ([lambda s, i: s.mark_launched(i), other_owner], False,
     "a Restore launching (starting), then registering: let go quietly"),
    ([lambda s, i: s.set_parked(i, True)], False, "parked meanwhile: stop"),
])
def test_shell_hand_back(store, sid, monkeypatch, after_exit, takes_back, desc):
    """Real processes and registration: the tab is a live process that exits."""
    monkeypatch.setattr(host_mod, "transcript_exists", lambda sid: True)
    monkeypatch.setattr(host_mod, "POLL_SECONDS", 0.01)
    h = Host(store, sid, client_factory=StreamClient, person="doug")
    tab = subprocess.Popen(["sleep", "60"])

    def open_tab(s, i):
        assert s.session(i)["claude_pid"] is None and s.session(i)["shell"] == "tab", desc
        s.register(i, tab.pid, liveness_start(tab.pid), host_mod.liveness.boot_id())
        with s.tx() as db:   # the tab ran a while: a relaunch after it is a new launch, to the second
            db.execute("UPDATE sessions SET heartbeat_at = ? WHERE id = ?", ("2026-01-01T00:00:00+00:00", i))

    def exit_tab():
        tab.kill()
        tab.wait()
    # each step runs in one of the host's waits; the first change after the exit lands in
    # the same wait, before the host looks again
    first = after_exit[0] if after_exit else (lambda s, i: None)
    steps = iter([lambda: None, lambda: (exit_tab(), first(store, sid)),
                  *[lambda f=f: f(store, sid) for f in after_exit[1:]]])

    async def wait():
        await asyncio.sleep(0)
        next(steps, lambda: None)()
    h.open_tab, h.shell_wait = open_tab, wait

    async def until(cond):
        for _ in range(500):
            if cond():
                return
            await asyncio.sleep(0.01)
        raise AssertionError(desc)

    async def go():
        task = asyncio.create_task(h.run())
        await until(lambda: store.session(sid)["claude_pid"] == os.getpid())
        store.command(sid, "shell")
        if takes_back:
            await until(lambda: h.client is not None and h.client.connected and store.session(sid)["shell"] is None
                        and store.session(sid)["claude_pid"] == os.getpid())
            store.set_parked(sid, True)
        await asyncio.wait_for(task, 5)
    try:
        run(go())
    finally:
        if tab.poll() is None:
            exit_tab()
    session = store.session(sid)
    assert h.owner == takes_back, desc
    if after_exit and after_exit[-1] is other_owner:
        assert session["claude_pid"] == 1 and session["activity"] == "new owner", \
            f"{desc}: the new owner's activity isn't overwritten"


class LockingStore:
    """Wraps the store: confirm() fails with a locked database while `locked` is set."""

    def __init__(self, store):
        self._store, self.locked = store, True

    def __getattr__(self, name):
        return getattr(self._store, name)

    def confirm(self, claimed):
        import sqlite3
        if self.locked:
            raise sqlite3.OperationalError("database is locked")
        self._store.confirm(claimed)


def test_lock_on_confirm_never_resends(store, sid, monkeypatch):
    """Sent, then confirm fails on every try: once the claim goes stale and the message is
    claimed again, it is confirmed, not sent a second time."""
    monkeypatch.setattr(host_mod, "transcript_exists", lambda sid: True)
    monkeypatch.setattr(host_mod, "CONFIRM_TRIES", (0,))
    monkeypatch.setattr("claude_wheelhouse.store.CLAIM_TIMEOUT", -1)   # every claim is stale at once
    locking = LockingStore(store)
    h = Host(locking, sid, client_factory=FakeClient, person="doug")
    run(h.connect())
    store.send(sid, "hello")
    run(h.poll())
    locking.locked = False
    run(h.poll())
    assert h.client.queries == ["[wheelhouse] from doug (general):\nhello"]
    assert store.pending(sid) == []


@pytest.mark.parametrize("name, inp, then, desc", [
    ("Bash", {"command": "ls"}, "same host", "a permission, withdrawn by an interrupt"),
    ("AskUserQuestion", ONE, "same host", "a question from Claude's dialog, withdrawn by an interrupt"),
    ("Bash", {"command": "ls"}, "new host", "a permission, withdrawn as the host stops"),
    ("AskUserQuestion", ONE, "new host", "a question, withdrawn as the host stops"),
])
def test_answer_to_a_withdrawn_call_is_a_turn(host, store, sid, monkeypatch, name, inp, then, desc):
    """The person's message reached the call, then Claude Code withdrew it before the call
    read it (cancelled the moment poll handed it over): the message is undelivered again in
    the store, so whoever delivers next (this host, a new one, or a shell tab's monitor)
    sends it, and the item is closed."""
    async def go():
        task = asyncio.create_task(host.can_use_tool(name, inp, ctx()))
        item = await until_open(store, sid)
        store.send(sid, "use the other one", item["ref"])
        await host.poll()   # handed to the waiting call
        task.cancel()   # before the call resumes
        with pytest.raises(asyncio.CancelledError):
            await task
        return item["ref"]
    ref = run(go())
    assert [(m["body"], m["claimed_at"]) for m in store.pending(sid)] == [("use the other one", None)], \
        f"{desc}: undelivered and unclaimed, in the store"
    assert store.item(sid, ref)["status"] != "open", desc
    deliverer = host
    if then == "new host":
        deliverer = Host(store, sid, client_factory=FakeClient, person="doug")
        run(deliverer.connect())
    run(deliverer.poll())
    assert deliverer.client.queries == [f"[wheelhouse] from doug on {ref}:\nuse the other one"], desc
    assert store.pending(sid) == [], desc


class DyingClient(FakeClient):
    async def query(self, text):
        raise ConnectionError("Claude Code exited")


def test_send_failure_leaves_a_stop_reason(store, sid, monkeypatch):
    monkeypatch.setattr(host_mod, "transcript_exists", lambda sid: True)
    monkeypatch.setattr(host_mod, "POLL_SECONDS", 0.01)

    class Client(StreamClient, DyingClient):
        pass
    h = Host(store, sid, client_factory=Client, person="doug")
    store.send(sid, "hello")
    run(asyncio.wait_for(h.run(), 5))
    assert store.session(sid)["activity"] == "stopped: Claude Code exited"
    assert [m["body"] for m in store.pending(sid)] == ["hello"], "left for the next host"


@pytest.mark.parametrize("state, shown, desc", [
    (lambda s, i: None, "(reaches you as a user turn): on its way", "not yet sent: marked, so it isn't acted on twice"),
    (lambda s, i: s.claim(i), "(passed to you): on its way",
     "passed on by the host (a turn, or a waiting call) but not yet marked delivered: still shown"),
    (lambda s, i: s.take_pending(i), ": on its way", "delivered: shown plainly"),
])
def test_get_input_ref_in_a_hosted_session(store, tmp_path, monkeypatch, state, shown, desc):
    from claude_wheelhouse import mcp_server
    sid = store.create_session(str(tmp_path), runner="sdk")
    monkeypatch.setattr(mcp_server, "_store", store)
    monkeypatch.setattr(mcp_server, "_sid", sid)
    get_input = next(t for t in mcp_server.TOOLS if t.__name__ == "get_input")
    ref = store.post_item(sid, "question", "q")
    store.send(sid, "on its way", ref)
    store.queue(sid, "a draft", ref)
    state(store, sid)
    out = get_input(ref=ref)
    assert out.splitlines()[-1].endswith(shown), desc
    assert ("(reaches you" in out) == ("reaches" in shown) and ("(passed" in out) == ("passed" in shown), desc
    assert "a draft" not in out, f"{desc}: drafts never show"


class NoStartClient(StreamClient):
    async def connect(self):
        raise ConnectionError("Command failed with exit code 1\nError output: see stderr")


def test_run_says_why_claude_code_didnt_start(store, sid, monkeypatch):
    monkeypatch.setattr(host_mod, "transcript_exists", lambda sid: True)
    h = Host(store, sid, client_factory=NoStartClient, person="doug")
    run(asyncio.wait_for(h.run(), 5))
    assert store.session(sid)["activity"] == \
        "stopped: Claude Code didn't start: Command failed with exit code 1 Error output: see stderr"


@pytest.mark.parametrize("fails, raises, desc", [
    (0, False, "a free database: written at once"),
    (2, False, "locked for a while: retried until it goes"),
    (9, True, "locked throughout: the error surfaces"),
])
def test_steady_writes_through_a_lock(monkeypatch, fails, raises, desc):
    import sqlite3
    monkeypatch.setattr(host_mod, "STEADY_TRIES", (0, 0, 0, 0))
    calls = []

    def write(x):
        calls.append(x)
        if len(calls) <= fails:
            raise sqlite3.OperationalError("database is locked")
    if raises:
        with pytest.raises(sqlite3.OperationalError):
            run(Host.steady(write, "x"))
    else:
        run(Host.steady(write, "x"))
    assert len(calls) == (5 if raises else fails + 1), desc


@pytest.mark.parametrize("name, inp, desc", [
    ("AskUserQuestion", ONE, "a question from Claude's dialog"),
    ("Bash", {"command": "ls"}, "a permission"),
])
def test_withdrawn_during_a_locked_confirm(host, store, sid, monkeypatch, name, inp, desc):
    """The call took the person's message, but a locked database holds up recording it, and
    Claude Code withdraws the call meanwhile: the call never returns, so the message goes out
    as a turn, once, rather than being marked delivered unread."""
    real, tries = store.confirm, []

    def locked_once(msgs):
        if msgs:
            tries.append(1)
            if len(tries) == 1:
                raise sqlite3.OperationalError("database is locked")
        return real(msgs)
    monkeypatch.setattr(store, "confirm", locked_once)
    monkeypatch.setattr(host_mod, "CONFIRM_TRIES", (1.0,))
    monkeypatch.setattr("claude_wheelhouse.store.CLAIM_TIMEOUT", -1)   # every claim is stale at once

    async def go():
        task = asyncio.create_task(host.can_use_tool(name, inp, ctx()))
        item = await until_open(store, sid)
        store.send(sid, "use the other one", item["ref"])
        await host.poll()   # handed to the call
        while not tries:   # the call has resumed, and waits out the locked confirm
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await host.poll()
        await host.poll()
        return item["ref"]
    ref = run(go())
    assert host.client.queries == [f"[wheelhouse] from doug on {ref}:\nuse the other one"], desc
    assert store.pending(sid) == [] and host.handed == {} and host.delivered == set(), desc
    assert store.item(sid, ref)["status"] != "open", desc


@pytest.mark.parametrize("name, inp, desc", [
    ("AskUserQuestion", ONE, "a question from Claude's dialog"),
    ("Bash", {"command": "ls"}, "a permission"),
])
def test_reclaimed_while_its_call_waits(host, store, sid, monkeypatch, name, inp, desc):
    """A message handed to a call and claimed again as stale before the call takes it is
    neither sent as a turn nor left behind."""
    monkeypatch.setattr("claude_wheelhouse.store.CLAIM_TIMEOUT", -1)

    async def go():
        task = asyncio.create_task(host.can_use_tool(name, inp, ctx()))
        item = await until_open(store, sid)
        store.send(sid, "answer", item["ref"])
        for _ in range(3):
            await host.poll()
        await asyncio.wait_for(task, 5)
        await host.poll()
    run(go())
    assert host.client.queries == [], desc
    assert store.pending(sid) == [] and host.handed == {} and host.delivered == set(), desc


def test_delivered_elsewhere_is_forgotten(host, store, sid):
    """An id remembered after a failed confirm, then delivered by someone else (a shell tab's
    monitor): a claim never returns it, so the next pass drops it."""
    store.send(sid, "hello")
    m = store.take_pending(sid)[0]
    host.delivered.add(m["id"])
    run(host.poll())
    assert host.delivered == set()


class LockedAtStart(LockingStore):
    def set_shell(self, *args):
        raise sqlite3.OperationalError("database is locked")


def test_run_says_a_locked_database_isnt_claude_code(store, sid, monkeypatch):
    monkeypatch.setattr(host_mod, "transcript_exists", lambda sid: True)
    h = Host(LockedAtStart(store), sid, client_factory=StreamClient, person="doug")
    run(asyncio.wait_for(h.run(), 5))
    activity = store.session(sid)["activity"]
    assert "database" in activity and "Claude Code didn't start" not in activity, activity


@pytest.mark.parametrize("locks, returns, desc", [
    (0, True, "no lock: the write's own result"),
    (2, True, "locked twice, then through: still its result"),
    (0, False, "a refused registration comes back as False"),
])
def test_steady_rides_out_a_lock(monkeypatch, locks, returns, desc):
    monkeypatch.setattr(host_mod, "STEADY_TRIES", (0, 0, 0))
    left = [locks]

    def write():
        if left[0]:
            left[0] -= 1
            raise sqlite3.OperationalError("database is locked")
        return returns
    assert run(Host.steady(write)) is returns, desc


@pytest.mark.parametrize("on, stored, env, alive, opened, recorded, desc", [
    (True, None, None, False, "host", "sdk", "SDK mode on: a session from before hosts existed comes back as a host"),
    (True, "tab", None, False, "host", "sdk", "and so does a tab: the way new sessions run now (Q27, option A)"),
    (True, "tab", "tab", False, "tab", "tab", "unless WHEELHOUSE_RUNNER=tab"),
    (True, "sdk", "tab", False, "tab", "tab", "which brings a host's session back as a tab too"),
    (True, "sdk", None, False, "host", "sdk", "a host's session keeps its host"),
    (True, None, None, True, None, None, "a running session is refused, and nothing is recorded"),
    (False, None, "sdk", False, "tab", "tab", "SDK mode off: as a tab, whatever WHEELHOUSE_RUNNER says"),
    (False, "sdk", None, False, "tab", "tab", "a host's session comes back as a tab (Q27, option A)"),
    (False, "tab", "sdk", False, "tab", "tab", "and a tab stays a tab"),
])
def test_restore_session(store, tmp_path, monkeypatch, on, stored, env, alive, opened, recorded, desc):
    monkeypatch.setattr(features, "_flag", on)
    sid = store.create_session(str(tmp_path), runner=stored or "tab")
    if stored is None:   # as the runner column's migration left a session from before hosts
        with store.tx() as db:
            db.execute("UPDATE sessions SET runner = NULL WHERE id = ?", (sid,))
    if env:
        monkeypatch.setenv("WHEELHOUSE_RUNNER", env)
    else:
        monkeypatch.delenv("WHEELHOUSE_RUNNER", raising=False)
    monkeypatch.setattr(launch.liveness, "is_alive", lambda *a: alive)
    seen = []
    monkeypatch.setattr(launch, "open_host", lambda s, i: seen.append("host"))
    monkeypatch.setattr(launch, "open_tab", lambda s, i: seen.append("tab"))
    if alive:
        with pytest.raises(RuntimeError, match="already running"):
            launch.restore_session(store, sid)
    else:
        launch.restore_session(store, sid)
    assert seen == ([opened] if opened else []), desc
    assert store.session(sid)["runner"] == recorded, desc


# model and effort, changed live (.plan/model-effort.md)

class SettingsClient(FakeClient):
    """FakeClient with Claude Code's model and effort: set_model, and the control requests
    the host sends through the SDK's own path (client._query._send_control_request).
    `refuse` names a model set_model refuses, as an unknown one is; `ignore` an effort
    level apply_flag_settings accepts but never applies, as Claude Code does a bogus one."""

    def __init__(self, options=None, model="claude-sonnet-5-5", effort="high", refuse=None, ignore=None):
        super().__init__(options)
        self.model, self.effort, self.refuse, self.ignore, self.sent = model, effort, refuse, ignore, []
        self._query = SimpleNamespace(_send_control_request=self.control)

    async def set_model(self, model):
        self.sent.append(("set_model", model))
        if model == self.refuse:
            raise Exception(f"Model '{model}' not found")
        self.model = model

    async def control(self, request, timeout=60.0):
        self.sent.append((request["subtype"], request.get("settings")))
        if request["subtype"] == "apply_flag_settings":
            level = request["settings"]["effortLevel"]
            self.effort = self.effort if level == self.ignore else level
            return {}
        if request["subtype"] == "get_settings":
            return {"applied": {"model": self.model, "effort": self.effort}}
        raise AssertionError(request)


@pytest.fixture
def hosted(store, tmp_path, monkeypatch):
    """A hosted session on Sonnet 5.5 at high, connected (resumed) to a SettingsClient."""
    monkeypatch.setattr(host_mod, "transcript_exists", lambda sid: True)
    sid = store.create_session(str(tmp_path), runner="sdk", model="claude-sonnet-5-5", effort="high")
    h = Host(store, sid, client_factory=SettingsClient, person="doug")
    run(h.connect())
    h.client.sent.clear()
    return h


@pytest.mark.parametrize("model, effort, launched, desc", [
    ("claude-haiku-5-5", "low", ("claude-haiku-5-5", "low"), "launched as chosen, resumed or not"),
    (None, None, (None, None), "neither set (from before): Claude Code's own"),
])
def test_options_launch_with_the_columns(store, tmp_path, model, effort, launched, desc):
    sid = store.create_session(str(tmp_path), runner="sdk", model=model, effort=effort)
    h = Host(store, sid, client_factory=FakeClient)
    for resume in (False, True):
        opts = h.options(resume=resume)
        assert (opts.model, opts.effort) == launched, f"{desc}, resume={resume}"


@pytest.mark.parametrize("client, applied, desc", [
    (SettingsClient, ("claude-sonnet-5-5", "high"), "confirmed by get_settings"),
    (FakeClient, ("claude-sonnet-5-5", "high"), "get_settings failing: as launched, and the session starts"),
])
def test_connect_records_what_is_in_force(store, tmp_path, monkeypatch, client, applied, desc):
    monkeypatch.setattr(host_mod, "transcript_exists", lambda sid: True)
    sid = store.create_session(str(tmp_path), runner="sdk", model="claude-sonnet-5-5", effort="high")
    h = Host(store, sid, client_factory=client)
    run(h.connect())
    s = store.session(sid)
    assert (s["applied_model"], s["applied_effort"]) == applied and h.applied == applied, desc
    assert s["activity"] == "idle", desc


@pytest.mark.parametrize("change, client, sent, columns, activity, desc", [
    ({"effort": "xhigh"}, {}, [("apply_flag_settings", {"effortLevel": "xhigh"}), ("get_settings", None)],
     ("claude-sonnet-5-5", "xhigh"), "idle · now Sonnet 5.5 at xhigh", "effort: applied, then confirmed"),
    ({"model": "claude-opus-5-5"}, {}, [("set_model", "claude-opus-5-5"), ("get_settings", None)],
     ("claude-opus-5-5", "high"), "idle · now Opus 5.5 at high", "model: set_model, effort left as it is"),
    ({"model": "claude-haiku-5-5", "effort": "low"}, {},
     [("set_model", "claude-haiku-5-5"), ("apply_flag_settings", {"effortLevel": "low"}), ("get_settings", None)],
     ("claude-haiku-5-5", "low"), "idle · now Haiku 5.5 at low", "both"),
    ({"model": "claude-fable-5-1"}, {"refuse": "claude-fable-5-1"},
     [("set_model", "claude-fable-5-1"), ("get_settings", None)], ("claude-sonnet-5-5", "high"),
     "idle · model change refused: Model 'claude-fable-5-1' not found", "refused: the column goes back, and says why"),
    ({"effort": "max"}, {"ignore": "max"}, [("apply_flag_settings", {"effortLevel": "max"}), ("get_settings", None)],
     ("claude-sonnet-5-5", "high"), "idle · effort change refused: Claude Code kept claude-sonnet-5-5, high",
     "not confirmed: put back, as what runs"),
    ({}, {}, [], ("claude-sonnet-5-5", "high"), "idle", "nothing changed: nothing sent"),
])
def test_a_change_is_applied_live(hosted, store, change, client, sent, columns, activity, desc):
    for k, v in client.items():
        setattr(hosted.client, k, v)
    for column, value in change.items():
        getattr(store, f"set_{column}")(hosted.sid, value)
    assert run(hosted.poll()) is True, desc
    s = store.session(hosted.sid)
    assert hosted.client.sent == sent, desc
    assert (s["model"], s["effort"]) == columns, f"{desc}: the columns"
    assert (s["applied_model"], s["applied_effort"]) == (hosted.client.model, hosted.client.effort), f"{desc}: in force"
    assert s["activity"] == activity, desc
    hosted.client.sent.clear()
    run(hosted.poll())
    assert hosted.client.sent == [], f"{desc}: once, not again each poll"


def test_a_change_made_while_a_turn_runs_leaves_the_activity_line_to_the_turn(hosted, store):
    hosted.sent = {"k": host_mod.Sent({})}   # a turn of ours running
    store.set_activity(hosted.sid, "running Bash: ls")
    store.set_effort(hosted.sid, "low")
    run(hosted.poll())
    s = store.session(hosted.sid)
    assert (s["applied_effort"], s["activity"]) == ("low", "running Bash: ls")


def test_a_refused_value_can_be_asked_for_again(hosted, store):
    """Put back, the columns match what's in force: asking again for the same is a change,
    tried again (Claude Code may know the model by then)."""
    hosted.client.refuse = "claude-fable-5-1"
    store.set_model(hosted.sid, "claude-fable-5-1")
    run(hosted.poll())
    hosted.client.refuse = None
    store.set_model(hosted.sid, "claude-fable-5-1")
    run(hosted.poll())
    assert store.session(hosted.sid)["applied_model"] == "claude-fable-5-1"


def test_the_sdks_control_request_path_is_where_the_host_expects_it():
    """The host reaches Claude Code's apply_flag_settings and get_settings through the SDK's
    internals (Host.control), as the SDK has no method for them: an SDK upgrade that moves
    them fails here, not in a live session."""
    import inspect
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
    from claude_agent_sdk._internal.query import Query
    assert "self._query" in inspect.getsource(ClaudeSDKClient), "the client keeps its Query as _query"
    params = inspect.signature(Query._send_control_request).parameters
    assert list(params)[:2] == ["self", "request"] and inspect.iscoroutinefunction(Query._send_control_request)
    assert "set_model" in vars(ClaudeSDKClient)
    fields = ClaudeAgentOptions.__dataclass_fields__
    assert "model" in fields and "effort" in fields


def finish_at(h, tokens, compacted=False):
    """A turn ends with the context at tokens, as read() ends one: every turn sent taken up."""
    async def usage():
        return {"totalTokens": tokens, "maxTokens": 1_000_000}
    h.client.get_context_usage = usage
    ended = list(h.sent.values())
    h.sent.clear()
    h.compacted_from = 300_000 if compacted else None
    run(h.finished(SimpleNamespace(result="", is_error=False, subtype="success"),
                   bool(ended) and all(t.keepalive for t in ended)))


def nudges(store, sid):
    return [(i["status"], i["title"]) for i in store.items(sid) if i["answer"] in (host_mod.NUDGE, host_mod.NUDGE_DONE)]


@pytest.mark.parametrize("steps, expected, desc", [
    ([150_000], [], "under the line: no nudge"),
    ([220_000], [("open", "context 220k: compact?")], "past it: one question suggesting Compact"),
    ([220_000, 240_000, 260_000], [("open", "context 220k: compact?")], "one per crossing, not one per turn"),
    ([220_000, ("compacted", 40_000)], [("closed", "context 220k: compact?")], "compacted: it closes by itself"),
    ([220_000, ("compacted", 180_000)], [("closed", "context 220k: compact?")],
     "compacted, if still past 150k: it closes too"),
    ([220_000, 180_000], [("open", "context 220k: compact?")], "under 200k but not compacted: it stays"),
    ([220_000, 180_000, 230_000], [("open", "context 220k: compact?")],
     "hovering about 200k: one nudge, not one per crossing"),
    ([220_000, 140_000], [("closed", "context 220k: compact?")], "back under 150k by itself: it closes"),
    ([220_000, ("compacted", 40_000), 210_000], [("open", "context 210k: compact?"),
                                                 ("closed", "context 220k: compact?")],
     "a second crossing nudges again"),
    ([220_000, "close", 240_000], [("closed", "context 220k: compact?")],
     "closed by the person: not asked again until it has been under"),
    ([220_000, "restart", 240_000], [("open", "context 220k: compact?")],
     "a host started again remembers the crossing"),
])
def test_compact_nudge(host, store, sid, steps, expected, desc):
    h = host
    for step in steps:
        if step == "close":
            ref = next(i["ref"] for i in store.items(sid) if i["answer"] == host_mod.NUDGE)
            store.update_item(sid, ref, status="closed")
        elif step == "restart":
            h = Host(store, sid, client_factory=FakeClient, person="doug")
            run(h.connect())
        elif isinstance(step, tuple):
            finish_at(h, step[1], compacted=True)
        else:
            finish_at(h, step)
    assert nudges(store, sid) == expected, desc


@pytest.mark.parametrize("compacted, note, desc", [
    (True, "Compacted: the context is 40k now.", "compacted"),
    (False, "The context is 40k now.", "back under by itself"),
])
def test_compact_nudge_closes_saying_so(host, store, sid, compacted, note, desc):
    finish_at(host, 220_000)
    finish_at(host, 40_000, compacted=compacted)
    ref = store.items(sid)[0]["ref"]
    assert [m["body"] for m in store.thread(sid, ref)] == [note], desc
    assert host_mod.HOST_QUESTION != store.item(sid, ref)["answer"], "a restart's deny_stale leaves it be"


def test_a_message_on_the_nudge_goes_as_a_general_one(streamed, store, sid):
    """The session never posted the nudge: a message on it is an ordinary message, and the
    host answers it there itself, so it never waits on the session."""
    h = streamed
    finish_at(h, 220_000)
    ref = store.items(sid)[0]["ref"]
    store.send(sid, "not now", ref)

    async def go():
        reader = asyncio.create_task(h.read())
        await h.poll()
        await feed(h, h.client.replay(), text("Fine, carrying on."), result(), None)
        await reader
    run(go())
    assert h.client.queries == ["[wheelhouse] from doug (general):\nnot now"]
    assert [m["body"] for m in store.thread(sid, ref) if m["author"] == "claude"] == [host_mod.NUDGE_PASSED]
    assert (sid, ref) not in store.awaiting()


H = 3600


@pytest.mark.parametrize("state, due, desc", [
    ({}, True, "big, quiet, waiting on an open question and 55 minutes on: keep it warm"),
    ({"tokens": 150_000}, False, "at the line, not past it: not worth it"),
    ({"tokens": None}, False, "no turn has ended under this host: its cache may be cold already"),
    ({"last": 54 * 60}, False, "not 55 minutes since the last turn"),
    ({"pending": 1}, False, "a turn is running (or waits on a permission)"),
    ({"compact_nonce": "ab12"}, False, "Compact is under way"),
    ({"quiet": None}, False, "a turn Claude Code started itself is running"),
    ({"quiet": 8 * H}, False, "quiet for 8 hours: abandoned, stop paying"),
    ({"quiet": 8 * H - 60}, True, "just inside the 8 hours"),
    ({"waits": None}, False, "nothing waits on the person: the session is done, not waiting"),
    ({"waits": "answered"}, False, "its question answered: nothing waits on the person"),
    ({"waits": "nudge"}, False, "only the host's own Compact nudge: not the session waiting"),
    ({"waits": "decision"}, True, "an unseen decision waits on the person"),
    ({"waits": "permission"}, True, "an open permission waits on the person"),
])
def test_keepalive_due(host, store, sid, state, due, desc):
    at = 100 * H
    waits = state.get("waits", "question")
    if waits in ("question", "answered"):
        ref = store.post_item(sid, "question", "which?")
        if waits == "answered":
            store.update_item(sid, ref, status="answered")
    elif waits == "decision":
        store.post_item(sid, "decision", "used SQLite", why="over Postgres: no server; undo: swap the DSN")
    elif waits == "permission":
        store.post_item(sid, "permission", "Bash: ls")
    elif waits == "nudge":
        store.set_answer(sid, store.post_item(sid, "question", "context 220k: compact?"), host_mod.NUDGE)
    host.tokens = state.get("tokens", 200_000)
    host.last_turn = at - state.get("last", 55 * 60)
    quiet = state.get("quiet", 2 * H)
    host.quiet_since = None if quiet is None else at - quiet
    host.sent = {"k": host_mod.Sent({})} if state.get("pending") else {}
    host.compact_nonce = state.get("compact_nonce")
    assert host.keepalive_due(at) is due, desc


def test_no_keepalive_after_a_compaction(host, store, sid):
    """A compaction leaves no cache to preserve: no keepalive until a turn ends without one."""
    store.post_item(sid, "question", "which?")
    finish_at(host, 200_000, compacted=True)
    at = host.last_turn + host_mod.KEEPALIVE_AFTER
    assert not host.keepalive_due(at), "the last turn compacted: nothing to keep warm"
    finish_at(host, 200_000)
    assert host.keepalive_due(host.last_turn + host_mod.KEEPALIVE_AFTER), "a turn since: its cache is worth keeping"


def test_keepalive_turn(host, store, sid, monkeypatch):
    """Sent once, as a keepalive: its end restarts the 55 minutes but not the quiet time,
    so the 8 hours run out; a real turn starts them both again."""
    store.post_item(sid, "question", "which?")
    finish_at(host, 200_000)
    quiet = host.quiet_since
    host.last_turn -= host_mod.KEEPALIVE_AFTER
    run(host.poll())
    run(host.poll())
    assert host.client.queries == [host_mod.KEEPALIVE_TEXT], "once, not every poll"
    finish_at(host, 200_000)
    assert host.quiet_since == quiet and host.last_turn > quiet, "the keepalive's end isn't the person's"
    store.send(sid, "go on")
    run(host.poll())
    assert host.quiet_since is None, "the person's turn ends the quiet"
    finish_at(host, 200_000)
    assert host.quiet_since > quiet


@pytest.mark.parametrize("line, error, after, desc", [
    ("error: overloaded", False, "error: overloaded", "an error's line"),
    ("interrupted", False, "interrupted", "Interrupt's line"),
    ("idle", True, "error: failed", "the keepalive itself failing says so"),
])
def test_a_keepalive_leaves_the_activity_line_as_it_was(streamed, store, sid, line, error, after, desc):
    h = streamed
    store.post_item(sid, "question", "which?")
    finish_at(h, 200_000)
    store.set_activity(sid, line)
    h.last_turn -= host_mod.KEEPALIVE_AFTER
    seen = []

    async def go():
        reader = asyncio.create_task(h.read())
        await h.poll()
        seen.append(store.session(sid)["activity"])
        await feed(h, h.client.replay(), text("ok"))
        seen.append(store.session(sid)["activity"])
        await feed(h, result("failed", error=error), None)
        await reader
    run(go())
    assert h.client.queries == [host_mod.KEEPALIVE_TEXT], desc
    assert seen == [line, line], f"{desc}: untouched while it runs"
    assert store.session(sid)["activity"] == after, desc


def session_replies(store, sid, ref):
    store.reply(sid, ref, "Replied myself.", "answered")


def subagent_text(t):
    from claude_agent_sdk import AssistantMessage
    from claude_agent_sdk.types import TextBlock
    return AssistantMessage(content=[TextBlock(text=t)], model="m", parent_tool_use_id="toolu_1")


@pytest.mark.parametrize("sent, stream, replies, desc", [
    ([("yes", "Q1")], [REPLAY, text("Done: Q1 settled."), result()], {"Q1": ["Done: Q1 settled."]},
     "what the turn ends by saying is the reply on the ref answered"),
    ([("go", None)], [REPLAY, text("On it."), result()], {"Q1": [], "Q2": []}, "a general message: no item to reply on"),
    ([("a", "Q1"), ("b", "Q2")], [REPLAY, text("Both done."), result()], {"Q1": ["Both done."], "Q2": ["Both done."]},
     "a batch: the same text on each"),
    ([("yes", "Q1")], [REPLAY, text("Checking."), tool("ls"), text("All good."), result()],
     {"Q1": ["All good."]}, "only the text after its last tool call: not the narration"),
    ([("a", "Q1"), ("b", "Q2")], [REPLAY, text("Looking."), tool("ls"), text("Q1: yes. Q2: no."), result()],
     {"Q1": ["Q1: yes. Q2: no."], "Q2": ["Q1: yes. Q2: no."]}, "a batch: the final text on each, no narration"),
    ([("yes", "Q1")], [REPLAY, text("Main answer."), subagent_text("A subagent's words."), result()],
     {"Q1": ["Main answer."]}, "a subagent's text isn't the session's answer"),
    ([("yes", "Q1")], [REPLAY, text("half"), result(error=True)], {"Q1": []}, "an error: not answered, still awaited"),
    ([("yes", "Q1")], [REPLAY, tool("ls"), result()], {"Q1": []}, "no text after its tool calls: nothing to post"),
    ([("yes", "Q1")], [text("A background task finished."), own(), REPLAY, text("Q1 settled."), result()],
     {"Q1": ["Q1 settled."]}, "a turn Claude Code started itself, ahead of ours: it answers nothing"),
    ([("yes", "Q1")], [REPLAY, session_replies, text("Q1 settled."), result()], {"Q1": ["Replied myself."]},
     "the session replied itself (copying its history): not posted twice"),
])
def test_host_posts_the_final_text_as_the_reply(streamed, store, sid, sent, stream, replies, desc):
    h = streamed
    for title in ("one", "two"):
        store.post_item(sid, "question", title)
    for body, ref in sent:
        store.send(sid, body, ref)

    async def go():
        reader = asyncio.create_task(h.read())
        await h.poll()
        for m in stream:
            if callable(m):
                m(store, sid, "Q1")
            else:
                await feed(h, h.client.replay() if m == REPLAY else m)
        await feed(h, None)
        await reader
    run(go())
    got = {ref: [m["body"] for m in store.thread(sid, ref) if m["author"] == "claude"] for ref in replies}
    assert got == replies, desc
    assert h.pending == 0, f"{desc}: every turn sent has ended"
    assert all(store.item(sid, ref)["status"] == ("answered" if session_replies in stream else "open")
               for ref in replies), f"{desc}: the status is the session's"


@pytest.mark.parametrize("stream, replies, desc", [
    (["A", tool("sleep 9"), "B", text("Both settled."), result()], {"Q1": ["Both settled."], "Q2": ["Both settled."]},
     "the second taken up into the turn running: one end answers both"),
    (["A", tool("sleep 9"), result(error=True), "B", text("Q2 settled."), result()], {"Q1": [], "Q2": ["Q2 settled."]},
     "the first interrupted: the second, still queued, runs and answers its own"),
    (["A", text("Q1 settled."), result(), "B", text("Q2 settled."), result()],
     {"Q1": ["Q1 settled."], "Q2": ["Q2 settled."]}, "one after the other: each its own"),
])
def test_each_turn_end_answers_the_turns_it_took_up(streamed, store, sid, stream, replies, desc):
    """As claude --input-format stream-json was seen to do (#16, round 43): a turn sent while
    one runs is replayed when taken up, into the running turn or after it; an interrupted
    turn's end lists it alone, and the turns queued behind it still run."""
    h = streamed
    for title in ("one", "two"):
        store.post_item(sid, "question", title)

    async def go():
        reader = asyncio.create_task(h.read())
        store.send(sid, "first", "Q1")
        await h.poll()
        store.send(sid, "second", "Q2")
        await h.poll()
        for m in stream:
            await feed(h, h.client.replay("AB".index(m)) if m in ("A", "B") else m)
        await feed(h, None)
        await reader
    run(go())
    got = {ref: [m["body"] for m in store.thread(sid, ref) if m["author"] == "claude"] for ref in replies}
    assert got == replies, desc
    assert h.pending == 0 and store.session(sid)["activity"] == "idle", desc


def test_the_host_asks_claude_code_to_replay_its_turns(host):
    assert "replay-user-messages" in host.options(resume=True).extra_args


@pytest.mark.parametrize("interrupt, end, activity, desc", [
    (True, result(error=True, terminal_reason="aborted_tools"), "interrupted",
     "interrupted: its end is no error, and the line says so"),
    (True, result("x", error=True), "error: x",
     "an Interrupt sent, but the turn ended in an error of its own: the result says which"),
    (False, result(error=True, terminal_reason="aborted_streaming"), "interrupted",
     "stopped by an interrupt from elsewhere: Claude Code says aborted"),
    (False, result("overloaded", error=True), "error: overloaded", "a real error still says so"),
])
def test_an_interrupted_turn_ends_as_interrupted(streamed, store, sid, interrupt, end, activity, desc):
    h = streamed

    async def go():
        reader = asyncio.create_task(h.read())
        await h.turn("a long one")
        await feed(h, h.client.replay(), tool("sleep 900"))
        if interrupt:
            store.command(sid, "interrupt")
            await h.poll()
        await feed(h, end, None)
        await reader
    run(go())
    s = store.session(sid)
    assert s["activity"] == activity, desc
    assert (s["interrupted_at"] is not None) == interrupt, f"{desc}: kept for the hub, whatever the line says"
    assert h.pending == 0, desc


def test_an_interrupted_keepalive_leaves_the_line_it_found(streamed, store, sid):
    h = streamed
    store.post_item(sid, "question", "which?")
    finish_at(h, 200_000)
    store.set_activity(sid, "idle · compacted 300k → 160k tokens")
    h.last_turn -= host_mod.KEEPALIVE_AFTER

    async def go():
        reader = asyncio.create_task(h.read())
        await h.poll()
        await feed(h, h.client.replay())
        store.command(sid, "interrupt")
        await h.poll()
        await feed(h, result(error=True, terminal_reason="aborted_streaming"), None)
        await reader
    run(go())
    assert h.client.queries == [host_mod.KEEPALIVE_TEXT]
    assert store.session(sid)["activity"] == "idle · compacted 300k → 160k tokens"


@pytest.mark.parametrize("end, drained, said, desc", [
    (result(), True, True, "ours, by its missing origin, though nothing was replayed: the oldest is taken, and said"),
    (own(), False, False, "Claude Code's own: ours stays sent"),
])
def test_a_turn_end_with_no_replay_never_wedges_the_host(streamed, store, sid, capsys, end, drained, said, desc):
    h = streamed
    store.post_item(sid, "question", "which?")
    store.send(sid, "this one", "Q1")

    async def go():
        reader = asyncio.create_task(h.read())
        await h.poll()
        await feed(h, text("An answer."), end, None)   # Claude Code replayed nothing
        await reader
    run(go())
    assert (h.pending == 0) == drained, desc
    assert ("none of our turns replayed" in capsys.readouterr().err) == said, desc
    assert [m["body"] for m in store.thread(sid, "Q1") if m["author"] == "claude"] == [], \
        f"{desc}: nothing posted on a guess"


async def idle_interrupt(h, store, sid):
    """An Interrupt taken with nothing running: no turn ends for it."""
    store.command(sid, "interrupt")
    await h.poll()


@pytest.mark.parametrize("case, desc", [
    ("idle", "an Interrupt taken while idle, then a turn's genuine error"),
    ("queued", "a turn queued behind an interrupted one, ending in a genuine error"),
    ("keepalive", "a keepalive's genuine error, after a stale Interrupt: not the line it kept"),
    ("compact", "Compact's ask turn ending in an error: not taken for an Interrupt"),
])
def test_a_genuine_error_after_an_interrupt_says_error(streamed, store, sid, case, desc):
    h = streamed
    error = result("overloaded", error=True, terminal_reason="api_error")
    if case == "keepalive":   # a big session that has had a turn, waiting on the person
        store.post_item(sid, "question", "which?")
        finish_at(h, 200_000)

    async def go():
        reader = asyncio.create_task(h.read())
        if case == "idle":
            await idle_interrupt(h, store, sid)
            await h.turn("go on")
            await feed(h, h.client.replay(), error)
        elif case == "queued":
            await h.turn("first")
            await h.turn("second")
            await feed(h, h.client.replay(0), tool("sleep 900"))
            store.command(sid, "interrupt")
            await h.poll()
            await feed(h, result(error=True, terminal_reason="aborted_tools"), h.client.replay(1), error)
        elif case == "keepalive":
            await idle_interrupt(h, store, sid)
            store.set_activity(sid, "idle")
            h.last_turn -= host_mod.KEEPALIVE_AFTER
            await h.poll()
            assert h.client.queries == [host_mod.KEEPALIVE_TEXT], desc
            await feed(h, h.client.replay(), error)
        else:
            await idle_interrupt(h, store, sid)
            store.command(sid, "compact")
            await h.poll()
            await feed(h, h.client.replay(), error)
        await feed(h, None)
        await reader
    run(go())
    assert store.session(sid)["activity"] == "error: overloaded", desc


class WatchedClient(SettingsClient):
    """SettingsClient whose connect records the applied columns as it starts, and can fail."""
    fail = False

    async def connect(self):
        s = self.store.session(self.sid)
        self.seen = (s["applied_model"], s["applied_effort"])
        if self.fail:
            raise ConnectionError("Claude Code didn't start")
        await super().connect()


@pytest.mark.parametrize("fail, applied, desc", [
    (False, ("claude-haiku-5-5", "low"), "written as launched before connecting: never shown as pending meanwhile"),
    (True, ("claude-opus-5-5", "max"), "a failed connect leaves them as they were"),
])
def test_connect_writes_the_launched_values_first(store, tmp_path, monkeypatch, fail, applied, desc):
    monkeypatch.setattr(host_mod, "transcript_exists", lambda sid: True)
    sid = store.create_session(str(tmp_path), runner="sdk", model="claude-haiku-5-5", effort="low")
    store.set_applied(sid, "claude-opus-5-5", "max")   # the last process's

    def factory(options):
        client = WatchedClient(options, model="claude-haiku-5-5", effort="low")
        client.store, client.sid, client.fail = store, sid, fail
        return client

    h = Host(store, sid, client_factory=factory)
    if fail:
        with pytest.raises(ConnectionError):
            run(h.connect())
    else:
        run(h.connect())
    assert h.client.seen == ("claude-haiku-5-5", "low"), desc
    s = store.session(sid)
    assert (s["applied_model"], s["applied_effort"]) == applied, desc


class LockedApplied:
    """Wraps the store: set_applied (or put_back) fails with a locked database while `locked` is set."""

    def __init__(self, store, method):
        self._store, self.method, self.locked = store, method, True

    def __getattr__(self, name):
        if name == self.method and self.locked:
            def locked(*args, **kwargs):
                raise sqlite3.OperationalError("database is locked")
            return locked
        return getattr(self._store, name)


@pytest.mark.parametrize("method, client, change, columns, desc", [
    ("set_applied", {}, {"effort": "xhigh"}, ("claude-sonnet-5-5", "xhigh"), "applied, then the write locked"),
    ("put_back", {"refuse": "claude-fable-5-1"}, {"model": "claude-fable-5-1"}, ("claude-sonnet-5-5", "high"),
     "refused, then the put-back locked"),
])
def test_a_locked_write_is_tried_again_not_left_pending(hosted, store, method, client, change, columns, desc):
    for k, v in client.items():
        setattr(hosted.client, k, v)
    for column, value in change.items():
        getattr(store, f"set_{column}")(hosted.sid, value)
    locking = LockedApplied(store, method)
    hosted.store = locking
    with pytest.raises(sqlite3.OperationalError):
        run(hosted.poll())
    assert hosted.applied == ("claude-sonnet-5-5", "high"), f"{desc}: not taken as in force"
    locking.locked = False
    run(hosted.poll())
    s = store.session(hosted.sid)
    assert (s["model"], s["effort"]) == columns, desc
    assert (s["applied_model"], s["applied_effort"]) == (hosted.client.model, hosted.client.effort), desc
    assert hosted.applied == (hosted.client.model, hosted.client.effort), desc


def test_a_put_back_leaves_a_newer_choice_made_meanwhile(hosted, store):
    """Refused while the person chose again: the newer choice stands, and is applied next."""
    hosted.client.refuse = "claude-fable-5-1"
    store.set_model(hosted.sid, "claude-fable-5-1")
    set_model = hosted.client.set_model

    async def chosen_again_meanwhile(model):
        store.set_model(hosted.sid, "claude-opus-5-5")
        await set_model(model)

    hosted.client.set_model = chosen_again_meanwhile
    run(hosted.poll())
    assert store.session(hosted.sid)["model"] == "claude-opus-5-5", "not put back over the newer choice"
    hosted.client.set_model = set_model
    run(hosted.poll())
    s = store.session(hosted.sid)
    assert (s["model"], s["applied_model"]) == ("claude-opus-5-5", "claude-opus-5-5")
