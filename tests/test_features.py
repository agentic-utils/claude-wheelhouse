import sys
from pathlib import Path

import pytest

from claude_wheelhouse import cli, daemon, features

REAL_ENV_FILES = features.env_files   # before conftest's sdk_off points them away


@pytest.mark.parametrize("text, expected, desc", [
    ("enable-sdk=true\n", {"enable-sdk": "true"}, "a key and its value"),
    ("# a comment\n\nenable-sdk = true \n", {"enable-sdk": "true"}, "comments, blank lines and spaces skipped"),
    ('export enable-sdk="true"\n', {"enable-sdk": "true"}, "an export prefix and quotes"),
    ("enable-sdk\nOTHER=a=b\n", {"OTHER": "a=b"}, "a line without = skipped; a value may hold one"),
    ("", {}, "an empty file"),
    ("\ufeffenable-sdk=true\n", {"enable-sdk": "true"}, "a UTF-8 byte order mark"),
    ("enable-sdk=true # SDK mode\n", {"enable-sdk": "true"}, "an inline comment"),
    ('enable-sdk="true" # SDK mode\n', {"enable-sdk": "true"}, "a quoted value and an inline comment"),
    ("a=x#y\n", {"a": "x#y"}, "a # with no space before it is part of the value"),
])
def test_read_env(tmp_path, text, expected, desc):
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    assert features.read_env(path) == expected, desc


def test_read_env_of_a_byte_that_isnt_utf8(tmp_path):
    path = tmp_path / ".env"
    path.write_bytes(b"NAME=caf\xe9\nenable-sdk=true\n")
    assert features.read_env(path)["enable-sdk"] == "true"


def test_read_env_of_no_file(tmp_path):
    assert features.read_env(tmp_path / "missing.env") == {}


@pytest.mark.parametrize("install_env, config_env, on, desc", [
    (None, None, False, "no .env: off"),
    ("ENABLE_SDK=true", None, True, "the install directory's .env"),
    (None, "ENABLE_SDK=true", True, "the user's config .env"),
    ("ENABLE_SDK=TRUE", None, True, "true in any case"),
    ("ENABLE_SDK=false", None, False, "false"),
    ("ENABLE_SDK=1", None, False, "only true turns it on"),
    ("enable-sdk=true", None, False, "the key hyphenated, in lower case: not read"),
    ("enable_sdk=true", None, False, "the key in lower case: not read"),
    ("ENABLE_SDK=yes", None, False, "nor any value but true"),
    ("\ufeffENABLE_SDK=true # mine", None, True, "a byte order mark and an inline comment"),
    ("ENABLE_SDK=false", "ENABLE_SDK=true", False, "the install directory's .env before the user's"),
])
def test_sdk_enabled_from_either_env_file(tmp_path, monkeypatch, install_env, config_env, on, desc):
    # the real env_files, not conftest's, on a home and an install directory of the test's own
    monkeypatch.setattr(features, "env_files", REAL_ENV_FILES)
    home, install = tmp_path / "home", tmp_path / "install"
    (home / ".config/claude-wheelhouse").mkdir(parents=True)
    install.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(features, "INSTALL_DIR", install)
    assert features.env_files() == [install / ".env", home / ".config/claude-wheelhouse/.env"]
    if install_env:
        (install / ".env").write_text(install_env + "\n")
    if config_env:
        (home / ".config/claude-wheelhouse/.env").write_text(config_env + "\n")
    assert features.sdk_enabled() is on, desc


@pytest.mark.parametrize("cwd, desc", [
    ("install", "started in the install directory (make run)"),
    ("elsewhere", "started anywhere else (a tab, the daemon's state dir)"),
])
def test_the_env_read_is_the_install_directorys_not_the_cwds(tmp_path, monkeypatch, cwd, desc):
    monkeypatch.setattr(features, "env_files", REAL_ENV_FILES)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    install, elsewhere = tmp_path / "install", tmp_path / "elsewhere"
    for d in (install, elsewhere):
        d.mkdir()
    (install / ".env").write_text("PORT=7440\n")
    (elsewhere / ".env").write_text("PORT=7441\n")
    monkeypatch.setattr(features, "INSTALL_DIR", install)
    monkeypatch.chdir(tmp_path / cwd)
    assert features.port() == 7440, desc


def test_the_install_directory_is_the_checkout():
    """uv sync installs the package editable, so its parent is the clone, Makefile and all."""
    assert (features.INSTALL_DIR / "pyproject.toml").is_file()
    assert (features.INSTALL_DIR / "claude_wheelhouse" / "features.py").is_file()


@pytest.mark.parametrize("flag, environ, install_env, config_env, want, desc", [
    (None, None, None, None, None, "none given: None"),
    (None, None, None, "KEY=c", ("c", "KEY= in {config}"), "the user's .env"),
    (None, None, "KEY=i", "KEY=c", ("i", "KEY= in {install}"), "the install directory's .env before the user's"),
    (None, "e", "KEY=i", None, ("e", "WHEELHOUSE_KEY"), "the environment variable before .env"),
    ("f", "e", "KEY=i", None, ("f", "--key"), "the flag before all"),
    (None, "", "KEY=i", None, ("i", "KEY= in {install}"), "an empty variable: as none"),
    (None, None, "key=i", None, None, "a key in lower case: not read"),
    (None, None, "WHEELHOUSE_KEY=i", None, None, "nor the variable's name in a .env"),
])
def test_a_setting_is_taken_from_the_first_place_that_gives_it(tmp_path, monkeypatch, flag, environ, install_env,
                                                                config_env, want, desc):
    install, config = tmp_path / "install.env", tmp_path / "config.env"
    monkeypatch.setattr(features, "env_files", lambda: [install, config])
    for path, text in ((install, install_env), (config, config_env)):
        if text:
            path.write_text(text + "\n")
    if environ is not None:
        monkeypatch.setenv("WHEELHOUSE_KEY", environ)
    else:
        monkeypatch.delenv("WHEELHOUSE_KEY", raising=False)
    got = features.given(flag, "--key", "KEY")
    assert got == (want and (want[0], want[1].format(install=install, config=config))), desc


@pytest.mark.parametrize("argv, on, desc", [
    (["daemon"], False, "no flag: off"),
    (["--enable-sdk", "daemon"], True, "before the command"),
    (["daemon", "--enable-sdk"], True, "after it"),
])
def test_the_command_line_flag(monkeypatch, argv, on, desc):
    seen = []
    monkeypatch.setattr(daemon, "main", lambda *a: seen.append(features.sdk_enabled()))
    monkeypatch.setattr(sys, "argv", ["claude-wheelhouse", *argv])
    cli.main()
    assert seen == [on], desc


def test_the_flag_is_left_out_of_help(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["claude-wheelhouse", "--help"])
    with pytest.raises(SystemExit):
        cli.main()
    out = capsys.readouterr().out
    assert "sdk" not in out.lower() and "agent" not in out.lower()
    assert "host" not in out, "the host command is SDK mode's alone (review 68)"


def test_an_invalid_command_doesnt_name_host(monkeypatch, capsys):
    """Review 69: argparse's invalid choice message listed every command, host included."""
    monkeypatch.setattr(sys, "argv", ["claude-wheelhouse", "bogus"])
    with pytest.raises(SystemExit):
        cli.main()
    err = capsys.readouterr().err
    assert "invalid choice" in err and "tutorial" in err, err
    assert "host" not in err, err


@pytest.mark.parametrize("argv, on, desc", [
    (["host", "abc"], False, "alone"),
    (["--enable-sdk", "host", "abc"], True, "after the flag, which turns SDK mode on as for any command"),
    (["host", "--enable-sdk", "abc"], True, "the flag after the command, as every command takes it"),
    (["host", "abc", "--enable-sdk"], True, "the flag last"),
])
def test_the_hidden_host_command_still_runs(monkeypatch, argv, on, desc):
    from claude_wheelhouse import host
    seen = []
    monkeypatch.setattr(host, "main", lambda sid: seen.append((sid, features.sdk_enabled())))
    monkeypatch.setattr(sys, "argv", ["claude-wheelhouse", *argv])
    cli.main()
    assert seen == [("abc", on)], desc


@pytest.mark.parametrize("argv, on, desc", [
    (["protocol"], False, "off: a tab's alone"),
    (["--enable-sdk", "protocol"], True, "on: a hosted session's too"),
    (["protocol", "--enable-sdk"], True, "the flag after the command"),
])
def test_the_protocol_command(monkeypatch, capsys, argv, on, desc):
    monkeypatch.setattr(sys, "argv", ["claude-wheelhouse", *argv])
    cli.main()
    out = capsys.readouterr().out
    assert ("protocol_sdk.md" in out) is on, desc
    assert "EXPERIMENTAL" not in out, "the file's note never reaches a session"


@pytest.mark.parametrize("flag, cwd_env, config_env, mode, warns, desc", [
    (None, None, None, "oneshot", False, "nothing given: oneshot"),
    ("persistent", None, None, "persistent", False, "the flag"),
    (None, "USAGE_MODE=persistent", None, "persistent", False, "the install directory's .env"),
    (None, None, "USAGE_MODE=persistent", "persistent", False, "the user's .env"),
    ("env", "USAGE_MODE=oneshot", None, "persistent", False, "WHEELHOUSE_USAGE_MODE before .env"),
    (None, "USAGE_MODE=Persistent", None, "persistent", False, "any case of value"),
    (None, "usage-mode=persistent", None, "oneshot", False, "the key hyphenated: not read"),
    (None, "usage_mode=persistent", None, "oneshot", False, "the key in lower case: not read"),
    (None, "USAGE_MODE=oneshot", "USAGE_MODE=persistent", "oneshot", False, "the install directory's wins"),
    ("oneshot", "USAGE_MODE=persistent", None, "oneshot", False, "the flag wins over .env"),
    (None, "USAGE_MODE=always", None, "oneshot", True, "a bad value: said, and oneshot"),
])
def test_usage_mode(tmp_path, monkeypatch, capsys, flag, cwd_env, config_env, mode, warns, desc):
    cwd, config = tmp_path / "cwd.env", tmp_path / "config.env"
    monkeypatch.setattr(features, "env_files", lambda: [cwd, config])
    for path, text in ((cwd, cwd_env), (config, config_env)):
        if text:
            path.write_text(text + "\n")
    if flag == "env":
        monkeypatch.setenv("WHEELHOUSE_USAGE_MODE", "persistent")
    elif flag:
        features.set_usage_mode(flag)
    assert [features.usage_mode(), features.usage_mode()] == [mode, mode], desc
    err = capsys.readouterr().err
    assert err.count("not oneshot or persistent") == (1 if warns else 0), f"{desc}: said once at most: {err!r}"


@pytest.mark.parametrize("argv, mode, desc", [
    (["daemon"], "oneshot", "no flag"),
    (["--usage-mode", "persistent", "daemon"], "persistent", "before the command"),
    (["daemon", "--usage-mode", "persistent"], "persistent", "after it"),
])
def test_the_usage_mode_flag(monkeypatch, argv, mode, desc):
    seen = []
    monkeypatch.setattr(daemon, "main", lambda *a: seen.append(features.usage_mode()))
    monkeypatch.setattr(sys, "argv", ["claude-wheelhouse", *argv])
    cli.main()
    assert seen == [mode], desc


def test_a_bad_usage_mode_flag_is_refused(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["claude-wheelhouse", "--usage-mode", "always"])
    with pytest.raises(SystemExit):
        cli.main()
    assert "invalid choice: 'always'" in capsys.readouterr().err


@pytest.mark.parametrize("flag, environ, cwd_env, config_env, port, desc", [
    (None, None, None, None, 7428, "nothing given: 7428"),
    (None, None, None, "PORT=7429", 7429, "the user's .env"),
    (None, None, "PORT=7430", "PORT=7429", 7430, "the install directory's wins over the user's"),
    (None, "7431", "PORT=7430", None, 7431, "WHEELHOUSE_PORT wins over .env"),
    ("7432", "7431", "PORT=7430", None, 7432, "the flag wins over all"),
    (None, None, "port=3000", None, 7428, "the key in lower case: not read"),
    (None, None, "PORT=1024", None, 1024, "the lowest port taken"),
    (None, None, "PORT=65535", None, 65535, "the highest"),
])
def test_port(tmp_path, monkeypatch, flag, environ, cwd_env, config_env, port, desc):
    cwd, config = tmp_path / "cwd.env", tmp_path / "config.env"
    monkeypatch.setattr(features, "env_files", lambda: [cwd, config])
    for path, text in ((cwd, cwd_env), (config, config_env)):
        if text:
            path.write_text(text + "\n")
    if environ:
        monkeypatch.setenv("WHEELHOUSE_PORT", environ)
    if flag:
        features.set_port(flag)
    assert features.port() == port, desc


@pytest.mark.parametrize("flag, environ, config_env, said, desc", [
    ("80", None, None, "--port is '80': give a port from 1024 to 65535", "the flag, below 1024"),
    (None, "seven", None, "WHEELHOUSE_PORT is 'seven': give a port from 1024 to 65535", "the variable, not a number"),
    (None, None, "PORT=65536", "PORT= in {config} is '65536': give a port from 1024 to 65535", ".env, naming the file"),
    (None, None, "PORT=-1", "PORT= in {config} is '-1': give a port from 1024 to 65535", "a negative number"),
])
def test_a_bad_port_is_refused_naming_where(tmp_path, monkeypatch, flag, environ, config_env, said, desc):
    config = tmp_path / "config.env"
    monkeypatch.setattr(features, "env_files", lambda: [tmp_path / "cwd.env", config])
    if config_env:
        config.write_text(config_env + "\n")
    if environ:
        monkeypatch.setenv("WHEELHOUSE_PORT", environ)
    if flag:
        features.set_port(flag)
    with pytest.raises(features.BadPort) as e:
        features.port()
    assert str(e.value) == said.format(config=config), desc


@pytest.mark.parametrize("flag, environ, config_env, fix, desc", [
    (None, None, None, "set PORT= in ~/.config/claude-wheelhouse/.env", "the default: where to set another"),
    ("7429", None, None, "it is set by --port: give another there", "the flag"),
    (None, "7429", None, "it is set by WHEELHOUSE_PORT: give another there", "the environment variable"),
    (None, None, "PORT=7429", "it is set by PORT= in {config}: give another there", "a .env, by its path"),
])
def test_a_port_taken_names_where_it_was_set(tmp_path, monkeypatch, flag, environ, config_env, fix, desc):
    config = tmp_path / "config.env"
    monkeypatch.setattr(features, "env_files", lambda: [tmp_path / "install.env", config])
    if config_env:
        config.write_text(config_env + "\n")
    if environ:
        monkeypatch.setenv("WHEELHOUSE_PORT", environ)
    if flag:
        features.set_port(flag)
    want = f"port 7429 is taken by another process (another user's wheelhouse?): {fix.format(config=config)}"
    assert features.port_taken(7429) == want, desc


@pytest.mark.parametrize("argv, port, desc", [
    (["daemon"], 7428, "no flag"),
    (["--port", "7429", "daemon"], 7429, "before the command"),
    (["daemon", "--port", "7429"], 7429, "after it"),
])
def test_the_port_flag_reaches_the_daemon(monkeypatch, argv, port, desc):
    seen = []
    monkeypatch.setattr(daemon, "main", lambda address, port, *a: seen.append(port))
    monkeypatch.setattr(sys, "argv", ["claude-wheelhouse", *argv])
    cli.main()
    assert seen == [port], desc


@pytest.mark.parametrize("argv, said, desc", [
    (["--port", "80"], "claude-wheelhouse: --port is '80': give a port from 1024 to 65535", "the app"),
    (["tui", "--port", "x"], "claude-wheelhouse: --port is 'x': give a port from 1024 to 65535", "the app, after tui"),
    (["daemon", "--port", "80"], "--port is '80': give a port from 1024 to 65535", "the daemon started by hand"),
])
def test_a_bad_port_flag_stops_it_in_a_line(monkeypatch, argv, said, desc):
    monkeypatch.setattr(sys, "argv", ["claude-wheelhouse", *argv])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == said, desc


@pytest.mark.parametrize("flag, cwd_env, config_env, hours, warns, desc", [
    (None, None, None, 8, False, "nothing given: 8"),
    ("2", None, None, 2, False, "the flag"),
    (None, "KEEPALIVE_HOURS=3", None, 3, False, "the install directory's .env"),
    (None, None, "KEEPALIVE_HOURS=4", 4, False, "the user's .env"),
    (None, "KEEPALIVE_HOURS=3", "KEEPALIVE_HOURS=4", 3, False, "the install directory's wins"),
    ("2", "KEEPALIVE_HOURS=3", "KEEPALIVE_HOURS=4", 2, False, "the flag wins over both"),
    (None, "keepalive-hours=3", None, 8, False, "the old hyphenated key: not read"),
    (None, "keepalive_hours=3", None, 8, False, "the key in lower case: not read"),
    (None, "KEEPALIVE_HOURS=1.5", None, 1.5, False, "a fraction"),
    ("env", "KEEPALIVE_HOURS=3", None, 6, False, "WHEELHOUSE_KEEPALIVE_HOURS before .env"),
    (None, "KEEPALIVE_HOURS=0", None, 0, False, "0: off"),
    (None, "KEEPALIVE_HOURS=lots", None, 8, True, "a bad value: said once, and 8"),
    ("-1", None, None, 8, True, "a negative flag: said once, and 8"),
])
def test_keepalive_hours(tmp_path, monkeypatch, capsys, flag, cwd_env, config_env, hours, warns, desc):
    cwd, config = tmp_path / "cwd.env", tmp_path / "config.env"
    monkeypatch.setattr(features, "env_files", lambda: [cwd, config])
    for path, text in ((cwd, cwd_env), (config, config_env)):
        if text:
            path.write_text(text + "\n")
    if flag == "env":
        monkeypatch.setenv("WHEELHOUSE_KEEPALIVE_HOURS", "6")
    elif flag:
        features.set_keepalive_hours(flag)
    assert [features.keepalive_hours(), features.keepalive_hours()] == [hours, hours], desc
    err = capsys.readouterr().err
    assert err.count("not a number of hours") == (1 if warns else 0), f"{desc}: said once at most: {err!r}"


@pytest.mark.parametrize("argv, hours, desc", [
    (["daemon"], 8, "no flag"),
    (["--keepalive-hours", "2", "daemon"], 2, "before the command"),
    (["daemon", "--keepalive-hours", "0.5"], 0.5, "after it"),
])
def test_the_keepalive_hours_flag_reaches_the_hub(monkeypatch, argv, hours, desc):
    seen = []
    monkeypatch.setattr(daemon, "main", lambda *a: seen.append(features.keepalive_hours()))
    monkeypatch.setattr(sys, "argv", ["claude-wheelhouse", *argv])
    cli.main()
    assert seen == [hours], desc


def test_a_host_takes_the_keepalive_hours_it_is_started_with(monkeypatch):
    from claude_wheelhouse import host
    seen = []
    monkeypatch.setattr(host, "main", lambda sid: seen.append((sid, features.keepalive_hours())))
    monkeypatch.setattr(sys, "argv", ["claude-wheelhouse", "host", "abc", "--keepalive-hours", "2"])
    cli.main()
    assert seen == [("abc", 2)]


@pytest.mark.parametrize("flag, install_env, config_env, fps, warns, desc", [
    (None, None, None, 10, False, "nothing given: 10"),
    ("4", None, None, 4, False, "the flag"),
    (None, "ANIMATION_FPS=5", None, 5, False, "the install directory's .env"),
    (None, None, "ANIMATION_FPS=6", 6, False, "the user's .env"),
    (None, "ANIMATION_FPS=5", "ANIMATION_FPS=6", 5, False, "the install directory's wins"),
    ("4", "ANIMATION_FPS=5", None, 4, False, "the flag wins over .env"),
    ("env", "ANIMATION_FPS=5", None, 3, False, "WHEELHOUSE_ANIMATION_FPS before .env"),
    (None, "animation_fps=5", None, 10, False, "the key in lower case: not read"),
    (None, "ANIMATION_FPS=2.5", None, 2.5, False, "a fraction"),
    ("0", None, None, 0, False, "0: no animation"),
    (None, "ANIMATION_FPS=fast", None, 10, True, "a bad value: said once, and 10"),
    ("-1", None, None, 10, True, "a negative flag: said once, and 10"),
    ("inf", None, None, 10, True, "no end to it: said once, and 10"),
    ("30", None, None, 30, False, "30: the most"),
    (None, "ANIMATION_FPS=60", None, 30, True, "more than 30: said once, and 30"),
])
def test_animation_fps(tmp_path, monkeypatch, capsys, flag, install_env, config_env, fps, warns, desc):
    install, config = tmp_path / "install.env", tmp_path / "config.env"
    monkeypatch.setattr(features, "env_files", lambda: [install, config])
    for path, text in ((install, install_env), (config, config_env)):
        if text:
            path.write_text(text + "\n")
    if flag == "env":
        monkeypatch.setenv("WHEELHOUSE_ANIMATION_FPS", "3")
    elif flag:
        features.set_animation_fps(flag)
    assert [features.animation_fps(), features.animation_fps()] == [fps, fps], desc
    err = capsys.readouterr().err
    assert err.count("frames a second:") == (1 if warns else 0), f"{desc}: said once at most: {err!r}"


@pytest.mark.parametrize("argv, fps, desc", [
    ([], 10, "no flag"),
    (["--animation-fps", "2"], 2, "before tui, or with no command"),
    (["tui", "--animation-fps", "0.5"], 0.5, "after tui"),
    (["--animation-fps", "0"], 0, "0"),
])
def test_the_animation_fps_flag_reaches_the_app(monkeypatch, argv, fps, desc):
    seen = []
    monkeypatch.setattr(cli, "run_app", lambda args: seen.append(features.animation_fps()))
    monkeypatch.setattr(sys, "argv", ["claude-wheelhouse", *argv])
    cli.main()
    assert seen == [fps], desc


@pytest.mark.parametrize("items, printed, desc", [
    ([], "", "nothing open: nothing printed"),
    ([("task", "waiting", "build it"), ("task", "done", "old")],
     "[wheelhouse] After the compaction, these items are still open in the wheelhouse. Keep them current with "
     "update_item; correct any that's finished:\nT1 [waiting] build it\n", "the open items, with titles"),
    (None, "", "no session: nothing printed"),
])
def test_open_items_prints_the_list_for_the_hook(store, sid, monkeypatch, capsys, items, printed, desc):
    for kind, status, title in items or []:
        store.post_item(sid, kind, title, status=status)
    if items is None:
        monkeypatch.delenv("WHEELHOUSE_SESSION_ID", raising=False)
    else:
        monkeypatch.setenv("WHEELHOUSE_SESSION_ID", sid)
    monkeypatch.setattr(sys, "argv", ["claude-wheelhouse", "open-items"])
    cli.main()
    assert capsys.readouterr().out == printed, desc


def test_the_plugin_runs_open_items_after_a_compaction():
    import json

    from claude_wheelhouse import launch
    hooks = json.loads((launch.PLUGIN_DIR / "hooks" / "hooks.json").read_text())["hooks"]["SessionStart"]
    assert [h["matcher"] for h in hooks] == ["compact"]
    assert hooks[0]["hooks"][0]["command"].endswith("-m claude_wheelhouse open-items")


@pytest.mark.parametrize("env, cwd_env, config_env, expected, warns, desc", [
    (None, None, None, "parent", False, "nothing given: the install directory's parent"),
    (None, "SESSION_DIR={a}", None, "a", False, "the install directory's .env"),
    (None, None, "SESSION_DIR={b}", "b", False, "the user's .env"),
    (None, "SESSION_DIR={a}", "SESSION_DIR={b}", "a", False, "the install directory's wins"),
    ("{b}", "SESSION_DIR={a}", None, "b", False, "WHEELHOUSE_SESSION_DIR before .env"),
    (None, "SESSION_DIR={missing}", None, "parent", True, "not a directory: said once, and the default"),
    (None, "SESSION_DIR=~", None, "home", False, "~ expanded"),
    (None, "SESSION_DIR=rel", None, "rel", False, "a relative path: from the home directory, made absolute"),
])
def test_session_dir(tmp_path, monkeypatch, capsys, env, cwd_env, config_env, expected, warns, desc):
    dirs = {"a": tmp_path / "a", "b": tmp_path / "b", "missing": tmp_path / "missing"}
    dirs["a"].mkdir(), dirs["b"].mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    dirs["rel"] = tmp_path / "rel"
    dirs["rel"].mkdir()
    cwd, config = tmp_path / "cwd.env", tmp_path / "config.env"
    monkeypatch.setattr(features, "env_files", lambda: [cwd, config])
    monkeypatch.setattr(features, "_warned", set())
    for path, text in ((cwd, cwd_env), (config, config_env)):
        if text:
            path.write_text(text.format(**dirs) + "\n")
    if env:
        monkeypatch.setenv("WHEELHOUSE_SESSION_DIR", env.format(**dirs))
    want = {"parent": features.INSTALL_DIR.parent, "home": tmp_path, **dirs}[expected]
    assert [features.session_dir(), features.session_dir()] == [want, want], desc
    err = capsys.readouterr().err
    assert err.count("not a directory") == (1 if warns else 0), f"{desc}: said once at most: {err!r}"
