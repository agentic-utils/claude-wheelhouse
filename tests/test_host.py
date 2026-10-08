import asyncio
import json
from types import SimpleNamespace

import pytest

from claude_wheelhouse import host as host_mod, launch
from claude_wheelhouse.host import (COMPACT_ASK, Host, format_turn, question_answers, question_item,
                                    tool_summary)
from claude_wheelhouse.monitor import REQUEST_TEXT


class FakeClient:
    """Stands in for ClaudeSDKClient: records what the host sends."""

    def __init__(self, options=None):
        self.options = options
        self.queries, self.interrupts, self.connected = [], 0, False

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def query(self, text):
        self.queries.append(text)

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
])
def test_question_answers(inp, reply, expected, desc):
    assert question_answers(inp, reply) == expected, desc


def test_question_item_lists_options():
    title, body = question_item(ONE)
    assert title == "Format"
    assert "- CSV: plain" in body and "- PDF" in body


@pytest.mark.parametrize("setup, expected, desc", [
    (lambda s, sid: s.send(sid, "yes", "Q1"), ["[wheelhouse] from doug on Q1:\nyes"], "a sent answer is a turn"),
    (lambda s, sid: s.queue(sid, "later", "Q1"), [], "a queued draft waits for Send"),
    (lambda s, sid: s.request_end(sid), [REQUEST_TEXT["end"]], "End is passed on as a turn"),
    (lambda s, sid: s.notice(sid, "hello"), ["[wheelhouse] hello"], "a notice"),
    (lambda s, sid: s.command(sid, "compact"), [COMPACT_ASK], "Compact asks for the notes first"),
])
def test_poll_turns(host, store, sid, setup, expected, desc):
    store.post_item(sid, "question", "q")
    setup(store, sid)
    assert run(host.poll()) is True, desc
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


@pytest.mark.parametrize("results, expected, desc", [
    (["<keep>- Q3 open\n- PR 17</keep>"], ["/compact - Q3 open\n- PR 17"], "the reply's notes"),
    (["background task done", "ok <keep>x</keep> bye"], ["/compact x"],
     "a turn Claude Code started itself is passed over"),
    (["no notes at all"], [], "no notes yet: nothing compacted"),
])
def test_compact_sends_the_notes(host, store, sid, results, expected, desc):
    store.command(sid, "compact")
    run(host.poll())
    for text in results:
        run(host.finished(SimpleNamespace(result=text, is_error=False, subtype="success")))
    assert host.client.queries == [COMPACT_ASK, *expected], desc


def test_finished_reports_compaction(host, store, sid):
    host.compacted_from = 48174
    run(host.finished(SimpleNamespace(result="", is_error=False, subtype="success")))
    assert store.session(sid)["activity"] == "idle · compacted 48k → 1k tokens"
    assert host.compacted_from is None


def test_finished_records_context(host, store, sid):
    run(host.finished(SimpleNamespace(result="", is_error=False, subtype="success")))
    s = store.session(sid)
    assert (s["activity"], s["context_tokens"], s["context_max"]) == ("idle", 1234, 200000)


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


@pytest.mark.parametrize("env, expected, desc", [
    (None, "sdk", "default"),
    ("tab", "tab", "WHEELHOUSE_RUNNER=tab"),
])
def test_default_runner(store, tmp_path, monkeypatch, env, expected, desc):
    if env:
        monkeypatch.setenv("WHEELHOUSE_RUNNER", env)
    else:
        monkeypatch.delenv("WHEELHOUSE_RUNNER", raising=False)
    assert store.session(store.create_session(str(tmp_path)))["runner"] == expected, desc


@pytest.mark.parametrize("protocol, has, lacks, desc", [
    (launch.PROTOCOL_SDK, "your user turns", "get_input", "the SDK protocol has no monitor"),
    (launch.PROTOCOL, "get_input", "your user turns", "the tab protocol keeps it"),
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
