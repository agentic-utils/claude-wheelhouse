"""The hub on its own, with no Textual: what it keeps, decides and says."""

import pytest

from claude_wheelhouse.hub import Hub


@pytest.fixture
def hub(store):
    return Hub(store)


@pytest.mark.parametrize("kept, typed, taken, left, desc", [
    ({}, "half an answer", "half an answer", {}, "what's typed comes back once, then it's gone"),
    ({("s", "Q1"): "old"}, "   ", "", {}, "a blank box forgets what was kept"),
    ({("s", None): "general"}, "for Q1", "for Q1", {("s", None): "general"}, "each target keeps its own"),
])
def test_unsent_text_is_kept_per_target(hub, kept, typed, taken, left, desc):
    hub.unsent.update(kept)
    hub.keep(("s", "Q1"), typed)
    assert hub.take(("s", "Q1")) == taken, desc
    assert hub.unsent == left, desc


def test_the_hub_never_imports_textual():
    import subprocess
    import sys
    code = "import sys, claude_wheelhouse.hub; print(any(m.startswith('textual') for m in sys.modules))"
    assert subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip() == "False"
