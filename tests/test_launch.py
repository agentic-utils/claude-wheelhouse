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
    assert argv[:8] == ["cmd.exe", "/c", "wt.exe", "-w", "wheelhouse-u", "new-tab", "--title", title], desc
    assert argv[argv.index("--cd") + 1] == cwd, desc
    # not exec'd: a stop's 143 or 137 exits 0 and closes the tab; any other code is kept
    # each `;` escaped for wt.exe, which unescapes it for the shell
    assert argv[-4:] == ["--", "/bin/zsh", "-lic", r"/py -m claude_wheelhouse run abc-123\; "
                         r"rc=$?\; if [ $rc -eq 143 -o $rc -eq 137 ]\; then exit 0\; fi\; exit $rc"], desc
    assert not launch.CMD_META & set(argv[-1]), f"{desc}: cmd.exe would act on its own operators"
    assert ";" not in argv[-1].replace(r"\;", ""), f"{desc}: a bare ; would split wt.exe's command line"


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


@pytest.mark.parametrize("user, window, desc", [
    ("doug", "wheelhouse-doug", "a user's tabs gather in a window of their own"),
    ("tfu", "wheelhouse-tfu", "another user's in another"),
    ("doug_2-x", "wheelhouse-doug_2-x", "letters, digits, - and _ kept"),
    ("d.lindsay", "wheelhouse-d-lindsay", "anything else becomes -"),
    ("a&b;c", "wheelhouse-a-b-c", "nothing cmd or wt would read as more than a name"),
])
def test_window_name(user, window, desc):
    assert launch.window_name(user) == window, desc


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


@pytest.mark.parametrize("over, resume, flags, desc", [
    ({"model": "claude-haiku-5-5", "effort": "low"}, False, ["--model", "claude-haiku-5-5", "--effort", "low"],
     "a new session launches as chosen"),
    ({"model": "claude-haiku-5-5", "effort": "low"}, True, ["--model", "claude-haiku-5-5", "--effort", "low"],
     "a resume passes both again: it would drop a live effort change"),
    ({"model": None, "effort": "xhigh"}, True, ["--effort", "xhigh"], "only what is set"),
    ({"model": None, "effort": None}, True, [], "an adopted session with neither runs as it did"),
    ({}, True, [], "a row with no such columns (older code's, or a dict)"),
])
def test_claude_argv_passes_model_and_effort(over, resume, flags, desc):
    argv = launch.claude_argv(row(**over), python="/py", resume=resume)
    given = [a for i, a in enumerate(argv) if a in ("--model", "--effort") or argv[i - 1] in ("--model", "--effort")]
    assert given == flags, desc


def test_the_shell_hand_over_and_restore_take_the_columns(store, monkeypatch):
    """open_tab, which Restore, Relaunch, Adopt and a host's shell hand-over all use, builds
    its claude command from the row: the run in the tab reads it again."""
    sid = store.create_session("/tmp", runner="sdk", model="claude-opus-5-5", effort="max")
    argv = launch.claude_argv(store.session(sid), python="/py", resume=True)
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5" and argv[argv.index("--effort") + 1] == "max"


PARENT = {"CLAUDECODE": "1", "CLAUDE_CODE_CHILD_SESSION": "1", "CLAUDE_CODE_ENTRYPOINT": "cli",
          "CLAUDE_CONFIG_DIR": "/home/u/.claude-alt", "PATH": "/usr/bin"}


def launched_env(path, store, sid, monkeypatch, tmp_path):
    """The environment each launch path hands on, run with a parent session's in os.environ."""
    seen = []
    monkeypatch.setattr(launch.liveness, "is_alive", lambda *a: False)
    monkeypatch.setattr(launch, "transcript_exists", lambda sid: False)
    if path == "run":
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(launch.os, "execvpe", lambda file, argv, env: seen.append(env))
        launch.run(sid)
    elif path == "tab":
        monkeypatch.setattr(launch.subprocess, "Popen", lambda argv, **kw: seen.append(kw["env"]))
        launch.open_tab(store, sid)
    else:
        store.set_runner(sid, "sdk")
        monkeypatch.setattr(launch.subprocess, "run", lambda argv, **kw: seen.append(kw["env"]))
        launch.open_host(store, sid)
    return seen[0]


@pytest.mark.parametrize("path, desc", [
    ("run", "a tab's run execs claude without the parent session's variables"),
    ("tab", "wt.exe is started without them"),
    ("host", "an SDK host is started without them, so the claude it runs never sees them"),
])
def test_every_launch_scrubs_a_parent_sessions_environment(path, desc, store, sid, monkeypatch, tmp_path):
    for key, value in PARENT.items():
        monkeypatch.setenv(key, value)
    env = launched_env(path, store, sid, monkeypatch, tmp_path)
    assert not [k for k in env if k.startswith("CLAUDE") and k != "CLAUDE_CONFIG_DIR"], desc
    assert env["CLAUDE_CONFIG_DIR"] == "/home/u/.claude-alt" and env["PATH"] == "/usr/bin", desc


@pytest.mark.parametrize("base, extra, want, desc", [
    ({"CLAUDECODE": "1", "HOME": "/h"}, {}, {"HOME": "/h"}, "CLAUDECODE goes"),
    ({"CLAUDE_CODE_CHILD_SESSION": "1"}, {"A": "b"}, {"A": "b"}, "a child-session marker goes; extra is set"),
    ({"CLAUDE_CONFIG_DIR": "/c"}, {}, {"CLAUDE_CONFIG_DIR": "/c"}, "the person's config directory stays"),
])
def test_clean_env(base, extra, want, desc):
    assert launch.clean_env(base, **extra) == want, desc


@pytest.mark.parametrize("tty, waited, desc", [
    (True, True, "in a tab: the reason stays up until Enter, as the tab closes once run exits"),
    (False, False, "no terminal: nothing to wait on"),
])
def test_run_refusing_says_why_before_the_tab_closes(monkeypatch, tty, waited, desc):
    asked = []
    monkeypatch.setattr(launch.sys.stdin, "isatty", lambda: tty, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt: asked.append(prompt))
    with pytest.raises(SystemExit) as e:
        launch.run("no-such-session")
    assert "no session no-such-session" in e.value.code, desc
    assert [("no session no-such-session" in p) for p in asked] == ([True] if waited else []), desc
