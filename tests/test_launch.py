import json

import pytest

from claude_wheelhouse import launch


def row(**over):
    return {"id": "abc-123", "name": "demo", "ticket": "#7", "brief": "fix it", "cwd": "/home/u/repo", "adopted": 0} | over


@pytest.mark.parametrize("over, title, cwd, desc", [
    ({}, "demo", "/home/u/repo", "named session"),
    ({"name": ""}, "repo", "/home/u/repo", "falls back to the directory name"),
    ({"name": "a;b", "cwd": "/x;y"}, "a,b", r"/x\;y", "semicolons can't split the wt command"),
    ({"name": "fish & chips"}, "fish  chips", "/home/u/repo", "cmd metacharacters dropped from the title"),
])
def test_wt_argv(over, title, cwd, desc):
    argv = launch.wt_argv(row(**over), python="/py", distro="Ubuntu", user="u", shell="/bin/zsh")
    assert argv[:8] == ["cmd.exe", "/c", "wt.exe", "-w", "0", "new-tab", "--title", title], desc
    assert argv[argv.index("--cd") + 1] == cwd, desc
    assert argv[-4:] == ["--", "/bin/zsh", "-lic", "exec /py -m claude_wheelhouse run abc-123"], desc


@pytest.mark.parametrize("over, resume, has, lacks, last, desc", [
    ({}, False, ["--session-id", "-n"], ["--resume"], "Ticket: #7\n\nfix it", "first launch takes the brief"),
    ({}, True, ["--resume", "-n"], ["--session-id"], None, "restore resumes without the brief"),
    ({"name": "", "ticket": "", "brief": ""}, False, ["--session-id"], ["-n"],
     "Session started from the wheelhouse. Wait for instructions.", "bare launch"),
    ({"adopted": 1}, True, ["--resume", "--system-prompt-snapshot"], ["--session-id"], None,
     "an adopted session renders the prompt fresh, so it sees the protocol"),
])
def test_claude_argv(over, resume, has, lacks, last, desc):
    argv = launch.claude_argv(row(**over), python="/py", resume=resume)
    for flag in has:
        assert flag in argv, desc
    for flag in lacks:
        assert flag not in argv, desc
    assert ("--system-prompt-snapshot" in argv) == bool(over.get("adopted")), desc
    if last:
        assert argv[-1] == last, desc
    mcp = json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]["wheelhouse"]
    assert mcp["env"]["WHEELHOUSE_SESSION_ID"] == "abc-123"
    assert argv[argv.index("--plugin-dir") + 1] == str(launch.PLUGIN_DIR)


def test_wt_argv_refuses_cmd_metacharacters_in_the_directory():
    with pytest.raises(ValueError, match="can't launch"):
        launch.wt_argv(row(cwd="/home/u/a&b"), python="/py", distro="Ubuntu", user="u", shell="/bin/bash")


def test_transcript_exists(tmp_path):
    (tmp_path / "-home-u-repo").mkdir()
    (tmp_path / "-home-u-repo/abc-123.jsonl").write_text("{}")
    assert launch.transcript_exists("abc-123", tmp_path)
    assert not launch.transcript_exists("other", tmp_path)


def test_open_tab_refuses_a_running_session(store, sid, monkeypatch):
    monkeypatch.setattr(launch.liveness, "is_alive", lambda *a: True)
    with pytest.raises(RuntimeError, match="already running"):
        launch.open_tab(store, sid)


def test_a_brief_starting_with_a_dash_is_not_read_as_an_option():
    """`claude "-x"` fails with "unknown option" (review #3)."""
    argv = launch.claude_argv(row(ticket="", brief="- first\n- second"), python="/py", resume=False)
    assert not argv[-1].startswith("-") and argv[-1].endswith("- first\n- second")


def test_run_registers_its_own_pid_before_exec(store, sid, tmp_path, monkeypatch):
    """The pid survives exec, so it is Claude's: no gap before the MCP server starts (review #1)."""
    class Exec(Exception):
        pass

    def fake_exec(*a):
        raise Exec
    monkeypatch.setattr(launch.os, "execvpe", fake_exec)
    monkeypatch.setattr(launch, "transcript_exists", lambda sid: False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(Exec):
        launch.run(sid)
    row_ = store.session(sid)
    assert row_["claude_pid"] == launch.os.getpid()
    assert launch.liveness.is_alive(row_["claude_pid"], row_["claude_start"], row_["boot_id"])


def test_open_tab_logs_the_launcher_output(store, sid, monkeypatch):
    """A failing cmd.exe or wt.exe must leave a trace (review #11)."""
    calls = []
    monkeypatch.setattr(launch.liveness, "is_alive", lambda *a: False)
    monkeypatch.setattr(launch.subprocess, "Popen", lambda argv, **kw: calls.append((argv, kw)))
    launch.open_tab(store, sid)
    (argv, kw), = calls
    assert kw["stdout"].name == str(store.path.parent / "launch.log")
    assert kw["stderr"] == launch.subprocess.STDOUT
    assert "wt.exe" in (store.path.parent / "launch.log").read_text()


def test_open_tab_refuses_a_session_that_is_still_starting(store, sid, monkeypatch):
    """Round-2 #5: a double press must not open two tabs before the first registers."""
    calls = []
    monkeypatch.setattr(launch.liveness, "is_alive", lambda *a: False)
    monkeypatch.setattr(launch.subprocess, "Popen", lambda argv, **kw: calls.append(argv))
    launch.open_tab(store, sid)
    with pytest.raises(RuntimeError, match="starting"):
        launch.open_tab(store, sid)
    assert len(calls) == 1


def test_two_runs_racing_exec_claude_once(store, sid, tmp_path, monkeypatch):
    """Round-2 #5: run()'s check and register are one compare-and-set."""
    import threading
    import time
    execs, exits = [], []
    real_alive = launch.liveness.is_alive

    def slow_alive(*a):
        alive = real_alive(*a)
        time.sleep(0.3)   # widen the window between check and register
        return alive
    monkeypatch.setattr(launch.liveness, "is_alive", slow_alive)
    monkeypatch.setattr(launch.os, "execvpe", lambda *a: execs.append(a))
    monkeypatch.setattr(launch.os, "chdir", lambda d: None)
    monkeypatch.setattr(launch, "transcript_exists", lambda sid: False)

    def go():
        try:
            launch.run(sid)
        except SystemExit as e:
            exits.append(str(e))
    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(execs) == 1 and len(exits) == 1 and "already running" in exits[0]


def test_protocol_shows_everything_a_session_receives(db_file):
    from claude_wheelhouse import mcp_server, monitor
    text = launch.injected()
    skill = (launch.PLUGIN_DIR / "skills/wheelhouse/SKILL.md").read_text()
    parts = [(launch.PROTOCOL, "protocol.md verbatim"), (skill, "the skill verbatim"),
             (mcp_server.server.instructions, "MCP server instructions"),
             *((t.__doc__.strip(), f"tool {t.__name__}") for t in mcp_server.TOOLS),
             *((t, "monitor request notice") for t in monitor.REQUEST_TEXT.values()),
             ("--append-system-prompt", "launch flags"), ("--plugin-dir", "launch flags"),
             ("--mcp-config", "launch flags")]
    for part, desc in parts:
        assert part in text, desc
    assert {t.__name__ for t in mcp_server.TOOLS} == {
        "post_item", "update_item", "reply", "set_synopsis", "get_input", "list_items", "park_session", "end_session"}


@pytest.mark.parametrize("env_shell, pw_shell, expected, desc", [
    ("/bin/zsh", "/bin/sh", "/bin/zsh", "$SHELL wins"),
    ("", "/usr/bin/fish", "/usr/bin/fish", "falls back to the passwd entry"),
    ("", "", "/bin/bash", "falls back to bash"),
])
def test_login_shell(monkeypatch, env_shell, pw_shell, expected, desc):
    monkeypatch.setenv("SHELL", env_shell)
    monkeypatch.setattr(launch.pwd, "getpwuid", lambda uid: type("pw", (), {"pw_shell": pw_shell}))
    assert launch.login_shell() == expected, desc


@pytest.mark.parametrize("resuming, notices, desc", [
    (True, 1, "a tab resuming a conversation (adopt, Restore, relaunch) asks it to post its open work"),
    (False, 0, "a fresh session has no open work to post"),
])
def test_open_tab_sends_the_join_notice_only_on_resume(store, sid, monkeypatch, resuming, notices, desc):
    monkeypatch.setattr(launch.liveness, "is_alive", lambda *a: False)
    monkeypatch.setattr(launch.subprocess, "Popen", lambda argv, **kw: None)
    monkeypatch.setattr(launch, "transcript_exists", lambda sid: resuming)
    launch.open_tab(store, sid)
    got = [(m["kind"], m["body"]) for m in store.pending(sid)]
    assert got == [("notice", launch.JOINED_TEXT)] * notices, desc


@pytest.mark.parametrize("env, has_decisions, desc", [
    (None, True, "the decisions section is on by default"),
    ("1", True, "on when asked"),
    ("0", False, "WHEELHOUSE_DECISIONS=0 leaves it out, for the priming A/B test"),
])
def test_decisions_section_is_separable(monkeypatch, env, has_decisions, desc):
    if env is None:
        monkeypatch.delenv("WHEELHOUSE_DECISIONS", raising=False)
    else:
        monkeypatch.setenv("WHEELHOUSE_DECISIONS", env)
    text = launch.protocol()
    assert text.startswith("# Wheelhouse protocol"), desc
    assert ("## Decisions" in text) == has_decisions, desc
    assert "This section is about reporting, not deciding" in launch.DECISIONS


@pytest.mark.parametrize("at, expected, desc", [
    ("2999-01-01T00:00:00.000Z", "Columbo check", "a /rename since its last rename: the launch's -n carries it"),
    ("2000-01-01T00:00:00.000Z", "demo", "an older one (adopted under a new name, say): the name it was given"),
])
def test_a_launch_takes_a_newer_rename_made_in_claude_code_first(store, sid, tmp_path, monkeypatch, at, expected, desc):
    from claude_wheelhouse import transcript
    folder = tmp_path / "projects/-home-u-repo"
    folder.mkdir(parents=True)
    recs = [{"type": "custom-title", "customTitle": "Columbo check"},
            {"type": "system", "subtype": "local_command", "timestamp": at,
             "content": "<local-command-stdout>Session renamed to: Columbo check</local-command-stdout>"}]
    (folder / f"{sid}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
    monkeypatch.setattr(transcript, "PROJECTS", tmp_path / "projects")
    launched = []
    monkeypatch.setattr(launch, "open_tab", lambda s, i: launched.append(s.session(i)["name"]))
    launch.open_session(store, sid)
    assert launched == [expected], desc
