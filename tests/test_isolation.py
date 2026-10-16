import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("inherited, refused, why", [
    (True, True, "a run handed the live WHEELHOUSE_DB stops before any test"),
    (False, False, "a run without it goes ahead"),
])
def test_run_refuses_the_live_database(tmp_path, inherited, refused, why):
    home = tmp_path / "home"   # the live wheelhouse is under this home, so nothing real is near
    live = home / ".local/state/claude-wheelhouse/wheelhouse.db"
    env = {k: v for k, v in os.environ.items() if not k.startswith("WHEELHOUSE_")}
    env["HOME"] = str(home)
    if inherited:
        env["WHEELHOUSE_DB"] = str(live)
    run = subprocess.run([sys.executable, "-m", "pytest", "-n", "0", "-p", "no:cacheprovider", "--co", "-q",
                          "tests/test_models.py"], cwd=ROOT, env=env, capture_output=True, text=True)
    out = run.stdout + run.stderr
    assert (run.returncode != 0) == refused, f"{why}: exit {run.returncode}\n{out}"
    assert ("test isolation: launch.db_path()" in out) == refused, f"{why}\n{out}"
    assert not live.parent.exists(), why
