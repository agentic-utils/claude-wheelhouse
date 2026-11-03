"""Tests for watching a scene (#340): WHEELHOUSE_SCENE forcing the cycle's pick, and the
`claude-wheelhouse scene <name>` preview, which plays one through the app's own overlay."""

import sys

import pytest

from claude_wheelhouse import cli, compactor, preview
from claude_wheelhouse.scenes import SCENES
from claude_wheelhouse.tui import CompactorOverlay


@pytest.mark.parametrize("forced, previous, want, desc", [
    ("hitchhikers", None, {"hitchhikers"}, "a scene named: every cycle plays it"),
    ("hitchhikers", "hitchhikers", {"hitchhikers"}, "even straight after itself"),
    ("nosuch", None, set(SCENES), "an unknown name: the random pick, over every scene"),
    ("", "star_wars", set(SCENES) - {"star_wars"}, "set empty: the random pick, never the previous"),
])
def test_wheelhouse_scene_forces_the_pick(monkeypatch, forced, previous, want, desc):
    monkeypatch.setenv(compactor.FORCE, forced)
    picks = {compactor.scene_for(f"cycle{i:03}", previous).name for i in range(200)}
    assert picks == want, desc


def test_the_preview_refuses_an_unknown_scene_and_lists_them(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["claude-wheelhouse", "scene", "nosuch"])
    with pytest.raises(SystemExit, match="no scene 'nosuch': one of " + ", ".join(sorted(SCENES))):
        cli.main()


@pytest.mark.parametrize("at, state, desc", [
    (0.1, "preparing", "the request: drawn as preparing"),
    (preview.PREPARING + preview.NOTES + 1, "compacting", "then compacting"),
    (preview.PREPARING + preview.NOTES + preview.COMPACTING + 1, "completed", "then its end"),
])
@pytest.mark.anyio
async def test_the_preview_plays_the_scene_through_the_apps_overlay(monkeypatch, at, state, desc):
    monkeypatch.setenv(compactor.FORCE, "")   # restored after: the preview sets it
    clock = [1_000_000.0]
    monkeypatch.setattr(preview.time, "time", lambda: clock[0])
    monkeypatch.setattr(CompactorOverlay, "now", staticmethod(lambda: clock[0]))
    app = preview.ScenePreview("hitchhikers")
    async with app.run_test(size=(100, 30)) as pilot:
        clock[0] += at
        app.tick()
        await pilot.pause()
        overlay = app.query_one(CompactorOverlay)
        assert overlay.display and overlay.shown is not None, desc
        assert (overlay.shown.scene.name, overlay.shown.state) == ("hitchhikers", state), desc
        assert overlay.big(), f"{desc}: the big frame, the terminal being wide enough"
