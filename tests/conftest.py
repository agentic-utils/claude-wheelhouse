import pytest

from claude_wheelhouse.store import Store


@pytest.fixture(autouse=True)
def tab_runner(monkeypatch):
    """Sessions made in tests run as tabs (launch is stubbed per test) unless a test picks
    sdk: a real host would start Claude Code."""
    monkeypatch.setenv("WHEELHOUSE_RUNNER", "tab")


@pytest.fixture(autouse=True)
def no_usage_fetch(monkeypatch):
    """The stats pane's account usage comes from a live endpoint: never called in tests."""
    monkeypatch.setattr("claude_wheelhouse.stats.AccountUsage.fetch", lambda self: None)


@pytest.fixture
def db_file(tmp_path, monkeypatch):
    path = tmp_path / "wheelhouse.db"
    monkeypatch.setenv("WHEELHOUSE_DB", str(path))
    return path


@pytest.fixture
def store(db_file):
    return Store(db_file)


@pytest.fixture
def sid(store, tmp_path):
    return store.create_session(str(tmp_path), name="demo", ticket="#7", brief="do the thing")
