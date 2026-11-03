"""The compactor's scenes (#304), one module each, registered here by name (Scene.name): the
compactor picks one per cycle (compactor.scene_for). Never imports Textual."""

from __future__ import annotations

from . import doctor_who, goldfinger, hitchhikers, men_in_black, red_dwarf, star_trek, star_wars, temple_of_doom, wrong_trousers
from .base import Scene

SCENES: dict[str, Scene] = {s.name: s for s in (star_wars.SCENE, star_trek.SCENE, red_dwarf.SCENE, men_in_black.SCENE, hitchhikers.SCENE,
                           temple_of_doom.SCENE, wrong_trousers.SCENE, doctor_who.SCENE,
                           goldfinger.SCENE)}
