import asyncio
import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from claude_wheelhouse import host as host_mod, launch
from claude_wheelhouse.host import (AWAITING_NOTES, CANCELLED, COMPACT_ASK, HOST_QUESTION, NO_NOTES, Host, format_turn,
                                    keep_notes, question_answers, question_item, tool_summary)
from claude_wheelhouse.store import SessionGone
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


def result(text="", error=False):
    from claude_agent_sdk import ResultMessage
    return ResultMessage(subtype="error" if error else "success", duration_ms=1, duration_api_ms=1,
                         is_error=error, num_turns=1, session_id="x", result=text)


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
        h.pending = 2
        for m in (AssistantMessage(content=[ToolUseBlock(id="1", name="Bash", input={"command": "ls"})], model="m"),
                  SystemMessage(subtype="compact_boundary", data={"compact_metadata": {"pre_tokens": 48000}}),
                  result(), AssistantMessage(content=[ToolUseBlock(id="2", name="Bash", input={"command": "pwd"})],
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


@pytest.mark.parametrize("stream, sent, activity, desc", [
    ([text(NOTES), result()], COMPACTED, "compacting", "the ask's reply carries the notes"),
    ([result("background task done"), text(NOTES), result()], COMPACTED, "compacting",
     "a turn Claude Code started itself ends first"),
    ([result("overloaded", error=True), text(NOTES), result()], COMPACTED, "compacting",
     "an error in a turn queued ahead doesn't cancel it"),
    ([result(NOTES)], COMPACTED, "compacting", "notes only in the turn's result"),
    ([text('Saved.\n<keep id="{nonce}">\n- Q3 open'), text("- PR 17\n</keep>"), result()], COMPACTED, "compacting",
     "a block split across text blocks"),
    ([text('<keep id="{nonce}">\n</keep>'), result()], ["/compact"], "compacting",
     "an empty block: compact with no instructions"),
    ([text('I\'ll put them between <keep id="{nonce}"> and </keep>.'), result()], [], AWAITING_NOTES,
     "the instruction quoted in prose isn't the notes"),
    ([text("<keep>\n- Q3\n</keep>"), result()], [], AWAITING_NOTES, "a block without the id isn't the notes"),
    ([text("I'll save first."), result()], [], AWAITING_NOTES, "no notes yet: it keeps waiting"),
])
def test_compact_by_id(streamed, store, sid, stream, sent, activity, desc):
    """Through the real read(): the notes go to /compact whichever turn they arrive in."""
    h = streamed
    store.command(sid, "compact")

    async def go():
        await h.poll()
        nonce = h.compact_nonce
        for m in stream:
            if hasattr(m, "result") and m.result:
                m.result = m.result.format(nonce=nonce)
            if hasattr(m, "content"):
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
    h.pending = running

    async def go():
        reader = asyncio.create_task(h.read())
        store.command(sid, "compact")
        await h.poll()
        nonce = h.compact_nonce
        if not running:   # the ask's turn ends without notes, then a turn Claude Code started itself
            await feed(h, result())
            await feed(h, tool("sleep 900"))
        else:
            await feed(h, tool("sleep 900"))
        for _ in range(3):   # time passes while that turn runs
            await h.poll()
            assert h.compact_nonce == nonce, desc
            assert store.session(sid)["activity"] == "running Bash: sleep 900", desc
        await feed(h, result(), text(NOTES.format(nonce=nonce)), result())
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
        await feed(h, text("Nothing to keep."), result())   # the ask's turn ends without notes
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


@pytest.mark.parametrize("stored, env, alive, opened, recorded, desc", [
    ("tab", None, False, "host", "sdk", "a stored tab session is restored as a host"),
    ("tab", "tab", False, "tab", "tab", "WHEELHOUSE_RUNNER=tab keeps it a tab"),
    ("sdk", "tab", False, "tab", "tab", "and brings a hosted session back as a tab"),
    ("tab", None, True, None, "tab", "a running session is refused and keeps its runner"),
])
def test_restore_session(store, tmp_path, monkeypatch, stored, env, alive, opened, recorded, desc):
    sid = store.create_session(str(tmp_path), runner=stored)
    if env:
        monkeypatch.setenv("WHEELHOUSE_RUNNER", env)
    else:
        monkeypatch.delenv("WHEELHOUSE_RUNNER")
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
