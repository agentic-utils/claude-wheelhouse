import pytest

from claude_wheelhouse import emoji


@pytest.mark.parametrize("text, expected, desc", [
    ("ship it :tada:", "ship it 🎉", "a known code"),
    (":+1: and :thumbs_up:", "👍 and 👍", "GitHub and Unicode names"),
    ("at 10:30: done", "at 10:30: done", "a time isn't a code"),
    (":not_a_real_code:", ":not_a_real_code:", "an unknown code is left as typed"),
    ("no codes here", "no codes here", "plain text untouched"),
])
def test_convert(text, expected, desc):
    assert emoji.convert(text) == expected, desc


@pytest.mark.parametrize("before, expected, desc", [
    ("hi :gri", "gri", "a code being typed"),
    (":th", "th", "at the start of the line"),
    ("hi :g", None, "one character is too few to suggest"),
    ("10:30", None, "a colon inside a word isn't a code"),
    ("hi :grin: ", None, "a closed code"),
])
def test_partial(before, expected, desc):
    assert emoji.partial(before) == expected, desc


def test_suggestions_put_the_shortest_matching_code_first():
    assert emoji.suggest("gri")[0] == ("grin", "😁")
    assert len(emoji.suggest("a", n=5)) == 5
