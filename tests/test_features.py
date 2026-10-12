import sys

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


@pytest.mark.parametrize("cwd_env, config_env, on, desc", [
    (None, None, False, "no .env: off"),
    ("enable-sdk=true", None, True, "the starting directory's .env"),
    (None, "enable-sdk=true", True, "the user's config .env"),
    ("enable-sdk=TRUE", None, True, "true in any case"),
    ("enable-sdk=false", None, False, "false"),
    ("enable-sdk=1", None, False, "only true turns it on"),
    ("ENABLE_SDK=true", None, True, "the key in capitals and underscores"),
    ("enable_sdk=true", None, True, "the key with an underscore"),
    ("Enable-Sdk=true", None, False, "no other spelling of the key"),
    ("enable-sdk=yes", None, False, "nor any value but true"),
    ("\ufeffenable-sdk=true # mine", None, True, "a byte order mark and an inline comment"),
    ("enable-sdk=false", "enable-sdk=true", True, "either file turns it on"),
])
def test_sdk_enabled_from_either_env_file(tmp_path, monkeypatch, cwd_env, config_env, on, desc):
    # the real env_files, not conftest's, on a home and a starting directory of the test's own
    monkeypatch.setattr(features, "env_files", REAL_ENV_FILES)
    home, start = tmp_path / "home", tmp_path / "start"
    (home / ".config/claude-wheelhouse").mkdir(parents=True)
    start.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(start)
    assert features.env_files() == [start / ".env", home / ".config/claude-wheelhouse/.env"]
    if cwd_env:
        (start / ".env").write_text(cwd_env + "\n")
    if config_env:
        (home / ".config/claude-wheelhouse/.env").write_text(config_env + "\n")
    assert features.sdk_enabled() is on, desc


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
    (None, "usage-mode=persistent", None, "persistent", False, "the starting directory's .env"),
    (None, None, "USAGE_MODE=persistent", "persistent", False, "the user's .env, the key in capitals"),
    (None, "usage_mode=Persistent", None, "persistent", False, "the key with an underscore, any case of value"),
    (None, "usage-mode=oneshot", "usage-mode=persistent", "oneshot", False, "the starting directory's wins"),
    ("oneshot", "usage-mode=persistent", None, "oneshot", False, "the flag wins over .env"),
    (None, "usage-mode=always", None, "oneshot", True, "a bad value: said, and oneshot"),
])
def test_usage_mode(tmp_path, monkeypatch, capsys, flag, cwd_env, config_env, mode, warns, desc):
    cwd, config = tmp_path / "cwd.env", tmp_path / "config.env"
    monkeypatch.setattr(features, "env_files", lambda: [cwd, config])
    for path, text in ((cwd, cwd_env), (config, config_env)):
        if text:
            path.write_text(text + "\n")
    if flag:
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
    (None, None, None, "port=7429", 7429, "the user's .env"),
    (None, None, "port=7430", "port=7429", 7430, "the starting directory's wins over the user's"),
    (None, "7431", "port=7430", None, 7431, "WHEELHOUSE_PORT wins over .env"),
    ("7432", "7431", "port=7430", None, 7432, "the flag wins over all"),
    (None, None, "PORT=3000", None, 7428, "PORT, a project's own server's, isn't the wheelhouse's"),
    (None, None, "port=1024", None, 1024, "the lowest port taken"),
    (None, None, "port=65535", None, 65535, "the highest"),
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
    (None, None, "port=65536", "port= in {config} is '65536': give a port from 1024 to 65535", ".env, naming the file"),
    (None, None, "port=-1", "port= in {config} is '-1': give a port from 1024 to 65535", "a negative number"),
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
    (None, "keepalive-hours=3", None, 3, False, "the starting directory's .env"),
    (None, None, "keepalive-hours=4", 4, False, "the user's .env"),
    (None, "keepalive-hours=3", "keepalive-hours=4", 3, False, "the starting directory's wins"),
    ("2", "keepalive-hours=3", "keepalive-hours=4", 2, False, "the flag wins over both"),
    (None, "KEEPALIVE_HOURS=3", None, 8, False, "the key is lower case only, as port's"),
    (None, "keepalive-hours=1.5", None, 1.5, False, "a fraction"),
    (None, "keepalive-hours=0", None, 0, False, "0: off"),
    (None, "keepalive-hours=lots", None, 8, True, "a bad value: said once, and 8"),
    ("-1", None, None, 8, True, "a negative flag: said once, and 8"),
])
def test_keepalive_hours(tmp_path, monkeypatch, capsys, flag, cwd_env, config_env, hours, warns, desc):
    cwd, config = tmp_path / "cwd.env", tmp_path / "config.env"
    monkeypatch.setattr(features, "env_files", lambda: [cwd, config])
    for path, text in ((cwd, cwd_env), (config, config_env)):
        if text:
            path.write_text(text + "\n")
    if flag:
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
