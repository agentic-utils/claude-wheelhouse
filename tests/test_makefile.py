"""The Makefile's install stamp: a stamp written before the modules' "without:" form (the
modules installed, or none) still installs what it did, and a module new since then too."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

MAKEFILE = Path(__file__).parent.parent / "Makefile"
STAMP = ".venv/.wheelhouse-installed"

pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="make isn't installed")


@pytest.mark.parametrize("stamp, left_out, desc", [
    ("stats", "", "an old stamp naming stats: design, new since, installed too"),
    ("", "", "an old empty stamp: every module"),
    ("stats design", "", "an old stamp naming both: both"),
    (None, "", "no stamp: every module"),
    ("without: ", "", "none left out"),
    ("none", "stats", "an old none: stats left out, design new since"),
    ("without: design", "design", "design left out, as it was"),
])
def test_the_stamp_installs_what_it_says(tmp_path, stamp, left_out, desc):
    shutil.copy(MAKEFILE, tmp_path)
    for m in ("stats", "design"):
        (tmp_path / "modules" / m).mkdir(parents=True)
    (tmp_path / ".venv").mkdir()
    if stamp is not None:
        (tmp_path / STAMP).write_text(stamp + "\n")
        os.utime(tmp_path / STAMP, (0, 0))   # older than pyproject.toml: make installs again
    for name in ("pyproject.toml", "uv.lock"):
        (tmp_path / name).touch()
    out = subprocess.run(["make", "-n", STAMP], cwd=tmp_path, capture_output=True, text=True, check=True).stdout
    assert sorted(re.findall(r"--no-group (\S+)", out)) == left_out.split(), f"{desc}: {out}"
    assert f'echo "without: {left_out}" > {STAMP}' in out, f"{desc}: the stamp written: {out}"
