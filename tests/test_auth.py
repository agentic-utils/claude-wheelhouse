"""The daemon's shared secret (auth.py): its file, its mode and the compare."""

import os
import stat

import pytest

from claude_wheelhouse import auth


def mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_the_first_start_makes_it_readable_by_the_user_alone(tmp_path):
    old = os.umask(0)   # a umask that would let anyone read it
    try:
        token, said = auth.ensure(tmp_path)
    finally:
        os.umask(old)
    assert mode(auth.path(tmp_path)) == 0o600 and "made a token" in said
    assert len(token) >= 43 and auth.read(auth.path(tmp_path)) == token, "32 random bytes, URL-safe"
    assert auth.ensure(tmp_path) == (token, None), "the next start keeps it"
    assert [p.name for p in tmp_path.iterdir()] == [auth.TOKEN], "no temporary file left"


def test_an_empty_file_is_said_not_used(tmp_path):
    auth.path(tmp_path).write_text("\n")
    auth.path(tmp_path).chmod(0o600)
    with pytest.raises(auth.TokenError, match="holds no token"):
        auth.ensure(tmp_path)


@pytest.mark.parametrize("file_mode, uid, said, desc", [
    (0o644, None, "chmod 600", "readable by others: refused, as ssh refuses a key file"),
    (0o620, None, "chmod 600", "writable by the group"),
    (0o600, 12345, "isn't yours", "another user's"),
    (0o400, None, None, "the user's alone, read-only: used"),
])
def test_a_token_file_open_to_others_is_refused_at_the_start(tmp_path, monkeypatch, file_mode, uid, said, desc):
    token, _ = auth.ensure(tmp_path)
    auth.path(tmp_path).chmod(file_mode)
    if uid is not None:
        monkeypatch.setattr(os, "geteuid", lambda: uid)
    if said is None:
        assert auth.ensure(tmp_path) == (token, None), desc
        return
    with pytest.raises(auth.TokenError, match=said) as e:
        auth.ensure(tmp_path)
    assert str(auth.path(tmp_path)) in str(e.value), desc


def test_a_temporary_file_left_by_a_start_that_died_is_removed(tmp_path):
    (tmp_path / f".{auth.TOKEN}.4242.deadbeef").write_text("half")
    auth.ensure(tmp_path)
    assert [p.name for p in tmp_path.iterdir()] == [auth.TOKEN]
    (tmp_path / f".{auth.TOKEN}.4243.cafef00d").write_text("half")
    auth.ensure(tmp_path)
    assert [p.name for p in tmp_path.iterdir()] == [auth.TOKEN], "on a start that finds the token too"


@pytest.mark.parametrize("given, token, ok, desc", [
    ("Bearer abc", "abc", True, "the token"),
    ("bearer abc", "abc", True, "the scheme in any case"),
    ("Bearer abd", "abc", False, "another"),
    ("Bearer abcd", "abc", False, "one longer"),
    ("Basic abc", "abc", False, "another scheme"),
    ("abc", "abc", False, "no scheme"),
    (None, "abc", False, "none given"),
    ("Bearer ", "", False, "an empty token matches nothing"),
])
def test_matches(given, token, ok, desc):
    assert auth.matches(given, token) is ok, desc
