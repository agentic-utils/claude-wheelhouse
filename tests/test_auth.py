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


def test_a_link_in_place_of_the_token_is_refused(tmp_path):
    (tmp_path / "elsewhere").write_text("a-token\n")
    (tmp_path / "elsewhere").chmod(0o600)
    auth.path(tmp_path).symlink_to(tmp_path / "elsewhere")
    with pytest.raises(auth.TokenError, match="can't read the token"):
        auth.read(auth.path(tmp_path))


@pytest.mark.parametrize("mode_, private, ok, desc", [
    (0o700, False, True, "the user's alone"),
    (0o755, False, True, "readable by others: the token's own mode keeps it"),
    (0o775, True, True, "group-writable, the group the user's private one (a umask of 002)"),
    (0o775, False, False, "group-writable by a shared group"),
    (0o757, True, False, "writable by anyone"),
])
def test_a_state_dir_others_can_write_is_refused(tmp_path, monkeypatch, mode_, private, ok, desc):
    state = tmp_path / "state"
    state.mkdir()
    state.chmod(mode_)
    monkeypatch.setattr(auth, "private_group", lambda gid: private)
    if ok:
        auth.ensure(state)
    else:
        with pytest.raises(auth.TokenError, match="can be written by others"):
            auth.ensure(state)


def test_the_users_own_state_dir_is_accepted_as_it_is():
    """Doug's is 0775 and his private group's: accepted, not changed."""
    import pathlib
    home = pathlib.Path.home() / ".local/state/claude-wheelhouse"
    if not home.exists():
        pytest.skip("no state dir here")
    st = home.stat()
    if st.st_mode & 0o020:
        assert auth.private_group(st.st_gid)
    auth.check_dir(home)   # reads only
