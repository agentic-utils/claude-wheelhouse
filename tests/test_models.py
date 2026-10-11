"""The models and effort levels offered (models.py, .plan/model-effort.md)."""

import pytest

from claude_wheelhouse import models


def test_the_table_is_the_decided_one():
    """Q51 and Q54: four pinned ids, no [1m] variant, every level for every model, the
    defaults by tier."""
    assert [(m.id, m.name, m.effort) for m in models.MODELS] == [
        ("claude-opus-5-5", "Opus 5.5", "high"), ("claude-sonnet-5-5", "Sonnet 5.5", "high"),
        ("claude-haiku-5-5", "Haiku 5.5", "xhigh"), ("claude-fable-5-1", "Fable 5.1", "high")]
    assert models.EFFORTS == ("low", "medium", "high", "xhigh", "max")
    assert models.DEFAULT_MODEL == "claude-sonnet-5-5", "Q50: Sonnet/High"


@pytest.mark.parametrize("model, effort, expected, desc", [
    (None, None, ("claude-sonnet-5-5", "high"), "nothing given: Sonnet 5.5 at high (Q50)"),
    ("claude-haiku-5-5", None, ("claude-haiku-5-5", "xhigh"), "a model alone: its tier's effort (Q54)"),
    ("claude-opus-5-5", "low", ("claude-opus-5-5", "low"), "any level, for any model"),
    (None, "max", ("claude-sonnet-5-5", "max"), "an effort alone: on the default model"),
])
def test_chosen(model, effort, expected, desc):
    assert models.chosen(model, effort) == expected, desc


@pytest.mark.parametrize("model, effort, error, desc", [
    ("opus[1m]", None, "model must be one of claude-opus-5-5", "an alias isn't a pinned id"),
    ("claude-opus-5-5[1m]", None, "model must be one of", "nor is the [1m] spelling"),
    (None, "bogus", "effort must be one of low, medium, high, xhigh, max", "Claude Code would ignore it silently"),
])
def test_chosen_refuses_what_isnt_in_the_table(model, effort, error, desc):
    with pytest.raises(ValueError, match=error.replace("[", r"\[")):
        models.chosen(model, effort)


def test_the_catalogue_is_the_table():
    cat = models.catalogue()
    assert [m["id"] for m in cat["models"]] == [m.id for m in models.MODELS]
    assert cat["efforts"] == list(models.EFFORTS) and cat["default"] == models.DEFAULT_MODEL


@pytest.mark.parametrize("row, expected, desc", [
    ({"model": "claude-opus-5-5"}, "claude-opus-5-5", "a row with the column"),
    ({"name": "x"}, None, "one without it: a plain dict, or older code's view"),
])
def test_field(row, expected, desc):
    assert models.field(row, "model") == expected, desc
