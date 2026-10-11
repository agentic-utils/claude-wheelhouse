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
])
def test_read_env(tmp_path, text, expected, desc):
    path = tmp_path / ".env"
    path.write_text(text)
    assert features.read_env(path) == expected, desc


def test_read_env_of_no_file(tmp_path):
    assert features.read_env(tmp_path / "missing.env") == {}


@pytest.mark.parametrize("cwd_env, config_env, on, desc", [
    (None, None, False, "no .env: off"),
    ("enable-sdk=true", None, True, "the starting directory's .env"),
    (None, "enable-sdk=true", True, "the user's config .env"),
    ("enable-sdk=TRUE", None, True, "true in any case"),
    ("enable-sdk=false", None, False, "false"),
    ("enable-sdk=1", None, False, "only true turns it on"),
    ("ENABLE_SDK=true", None, False, "the key as written, enable-sdk"),
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


@pytest.mark.parametrize("on, desc", [(False, "off: a tab's alone"), (True, "on: a hosted session's too")])
def test_the_protocol_command(monkeypatch, capsys, on, desc):
    monkeypatch.setattr(sys, "argv", ["claude-wheelhouse", *(["--enable-sdk"] if on else []), "protocol"])
    cli.main()
    out = capsys.readouterr().out
    assert ("protocol_sdk.md" in out) is on, desc
    assert "EXPERIMENTAL" not in out, "the file's note never reaches a session"
