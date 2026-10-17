import importlib.metadata

from claude_wheelhouse import fingerprint


def test_a_module_installed_changed_or_removed_is_other_code(tmp_path, monkeypatch):
    pkg = tmp_path / "fakemod"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("module = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    found = [importlib.metadata.EntryPoint("fake", "fakemod:module", fingerprint.GROUP)]
    monkeypatch.setattr(fingerprint.importlib.metadata, "entry_points", lambda group: found)
    first = fingerprint.modules()
    assert first == fingerprint.modules(), "the same install, the same code"
    (pkg / "__init__.py").write_text("module = 2\n")
    changed = fingerprint.modules()
    assert changed != first, "a module's source changed"
    found.clear()
    assert fingerprint.modules() not in (first, changed), "a module removed"
