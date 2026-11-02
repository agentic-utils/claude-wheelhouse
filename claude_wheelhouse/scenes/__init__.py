"""The compactor's scenes (#304), one module each, registered here by name (Scene.name): the
compactor picks one per cycle (compactor.scene_for). Never imports Textual."""

from __future__ import annotations

from . import star_wars
from .base import Scene

SCENES: dict[str, Scene] = {s.name: s for s in (star_wars.SCENE,)}
