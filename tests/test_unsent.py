"""Text typed but not submitted: the client's, kept per target."""

import pytest

from claude_wheelhouse.unsent import Unsent


@pytest.mark.parametrize("kept, typed, taken, left, desc", [
    ({}, "half an answer", "half an answer", {}, "what's typed comes back once, then it's gone"),
    ({("s", "Q1"): "old"}, "   ", "", {}, "a blank box forgets what was kept"),
    ({("s", None): "general"}, "for Q1", "for Q1", {("s", None): "general"}, "each target keeps its own"),
])
def test_unsent_text_is_kept_per_target(kept, typed, taken, left, desc):
    unsent = Unsent()
    unsent.texts.update(kept)
    unsent.keep(("s", "Q1"), typed)
    assert unsent.take(("s", "Q1")) == taken, desc
    assert unsent.texts == left, desc
