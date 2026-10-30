import os
import stat

import pytest

from claude_wheelhouse import quickshell


@pytest.mark.parametrize("text, error, desc", [
    ("gh pr view 263 --json mergeStateStatus", None, "one line of ASCII"),
    ("cd repo\ngit status\n", None, "newlines allowed"),
    ("echo '<pr>' | tr a-z A-Z", None, "quotes, pipes and placeholders are ASCII"),
    ("", "command is empty", "empty"),
    ("  \n ", "command is empty", "only blanks"),
    ("echo ’hi", "U+2019 at line 1, column 6", "a curly quote, with its place"),
    ("ls\necho \x1b[31m", "U+001B at line 2, column 6", "an escape sequence"),
    ("ls\r\nrm -rf x", "U+000D at line 1, column 3", "a CR"),
    ("ls\tx", "U+0009 at line 1, column 3", "a tab"),
    ("ls​", "U+200B at line 1, column 3", "a zero-width space"),
    ("echo ‮abc", "U+202E at line 1, column 6", "a bidi override"),
    ("еcho", "U+0435 at line 1, column 1", "a Cyrillic homoglyph"),
    ("\n".join(["x"] * 20), None, "20 lines"),
    ("\n".join(["x"] * 21), "21 lines: at most 20", "21 lines"),
    ("x" * 2000, None, "2000 characters"),
    ("x" * 2001, "2001 characters: at most 2000", "2001 characters"),
])
def test_check_command(text, error, desc):
    """#257, Q239: only what can be read as it runs is run."""
    if error is None:
        quickshell.check_command(text)
    else:
        with pytest.raises(ValueError, match=error.replace("(", r"\(").replace("+", r"\+")):
            quickshell.check_command(text)


@pytest.mark.parametrize("why, error, desc", [
    ("Check the deployed ref", None, "one line"),
    ("", "why is empty", "empty"),
    ("one\ntwo", "one line", "two lines"),
])
def test_check_why(why, error, desc):
    if error is None:
        quickshell.check_why(why)
    else:
        with pytest.raises(ValueError, match=error):
            quickshell.check_why(why)


@pytest.mark.parametrize("raw, want, desc", [
    (b"Script started on 2026-10-30 [COMMAND=\"ls\"]\nfile\nScript done on 2026-10-30 [COMMAND_EXIT_CODE=\"0\"]\n",
     "file", "script's header and footer dropped"),
    (b"\x1b[31mred\x1b[0m\r\n", "red", "ANSI colours stripped, CRLF ends"),
    (b"\x1b]0;title\x07ok\n", "ok", "an OSC title stripped"),
    (b"10%\r50%\r100%\r\ndone\n", "100%\ndone", "a progress bar at its last state"),
    (b"caf\xc3\xa9 \xff\n", "café �", "UTF-8 kept, bad bytes replaced"),
])
def test_clean(raw, want, desc):
    assert quickshell.clean(raw) == want, desc


@pytest.mark.parametrize("lines, width, cut, desc", [
    (10, 10, False, "short: whole"),
    (2000, 9, True, "long: head and tail with the count cut"),
    (1, 20_000, True, "one huge line"),
])
def test_cap(lines, width, cut, desc):
    text = "\n".join(f"{i:0{width}d}" for i in range(lines))
    got = quickshell.cap(text)
    assert len(got) <= quickshell.CAP + 40, desc
    assert ("lines cut ...]" in got) == cut, desc
    if cut and lines > 1:
        assert got.startswith(text[:9]) and got.endswith(text[-9:]), f"{desc}: the first and the last kept"
        n = int(got.split("[... ")[1].split(" ")[0])
        assert got.count("\n") - 1 + n == lines - 1, f"{desc}: every line kept or counted"


def test_the_file_is_private_and_taken_once(tmp_path):
    """#257: the run's file is 0600 in a 0700 directory, and reading it deletes it."""
    db = tmp_path / "wheelhouse.db"
    key = quickshell.write(db, {"text": "ls", "sid": "s", "ref": "C1"})
    path = tmp_path / "shell" / key
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert quickshell.take(db, key)["text"] == "ls"
    assert not path.exists()
    with pytest.raises(FileNotFoundError):
        quickshell.take(db, key)


@pytest.mark.parametrize("key, desc", [("../../etc/passwd", "a path"), ("short", "too short"), ("a b" * 10, "a space")])
def test_take_refuses_what_isnt_a_key(tmp_path, key, desc):
    with pytest.raises(ValueError):
        quickshell.take(tmp_path / "wheelhouse.db", key)


def test_take_checks_the_text_again(tmp_path):
    db = tmp_path / "wheelhouse.db"
    key = quickshell.write(db, {"text": "rm ‮", "sid": "s", "ref": "C1"})
    with pytest.raises(ValueError, match="U\\+202E"):
        quickshell.take(db, key)


def test_sweep_deletes_files_over_a_day_old(tmp_path):
    db = tmp_path / "wheelhouse.db"
    old, new = quickshell.write(db, {"text": "a"}), quickshell.write(db, {"text": "b"})
    os.utime(tmp_path / "shell" / old, (0, 0))
    quickshell.sweep(db)
    assert sorted(p.name for p in (tmp_path / "shell").iterdir()) == [new]


@pytest.mark.parametrize("payload, status, output, want, desc", [
    ({"ref": "C4", "cwd": "/r", "text": "ls"}, 0, "a\nb", "ran C4 in /r, exit 0\n\n```\na\nb\n```", "as run"),
    ({"ref": "C4", "cwd": "/r", "text": "ls -l", "edited": True}, 1, "",
     "ran C4 in /r, exit 1, edited\n\n```bash\nls -l\n```\n\n```\n(no output)\n```", "edited: the text run shown"),
    ({"ref": "C4", "cwd": "/r", "text": "ls"}, 0, "```x```", "ran C4 in /r, exit 0\n\n````\n```x```\n````",
     "a fence in the output: a longer one round it"),
])
def test_message(payload, status, output, want, desc):
    assert quickshell.message(payload, status, output) == want, desc
