"""The daemon's token (auth.py): its file, its mode, rotation and the compare."""

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


def test_one_others_can_read_is_replaced(tmp_path):
    token, _ = auth.ensure(tmp_path)
    auth.path(tmp_path).chmod(0o644)
    new, said = auth.ensure(tmp_path)
    assert new != token and mode(auth.path(tmp_path)) == 0o600 and "readable by others" in said


def test_one_another_user_owns_is_refused(tmp_path, monkeypatch):
    auth.ensure(tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(auth.TokenError, match="belongs to another user"):
        auth.ensure(tmp_path)


def test_rotation_is_seen_by_the_file_watcher(tmp_path):
    first, _ = auth.ensure(tmp_path)
    watched = auth.TokenFile(auth.path(tmp_path))
    assert watched.token == first and not watched.check(), "unchanged: nothing read again"
    second = auth.write(tmp_path)
    assert watched.check() and watched.token == second != first, "rotated: the new one"
    auth.path(tmp_path).unlink()
    assert not watched.check() and watched.token == second, "a file gone keeps the last token"


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
