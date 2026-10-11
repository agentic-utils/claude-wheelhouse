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
