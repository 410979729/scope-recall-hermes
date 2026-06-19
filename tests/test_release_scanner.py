from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_release_check_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "check.release.py"
    spec = importlib.util.spec_from_file_location("scope_recall_check_release", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_scanner_detects_json_yaml_and_python_secret_assignments(tmp_path):
    module = _load_release_check_module()
    fake_value = "notareal" + "secretvalue12345"
    (tmp_path / "config.json").write_text('{"api_key": "' + fake_value + '"}\n', encoding="utf-8")
    (tmp_path / "config.yaml").write_text("token: " + fake_value + "\n", encoding="utf-8")
    (tmp_path / "settings.py").write_text("password = '" + fake_value + "'\n", encoding="utf-8")

    original_root = getattr(module, "ROOT")
    setattr(module, "ROOT", tmp_path)
    try:
        findings = module.scan_tree()
    finally:
        setattr(module, "ROOT", original_root)

    joined = "\n".join(findings["secrets"])
    assert "config.json" in joined
    assert "config.yaml" in joined
    assert "settings.py" in joined
    assert fake_value not in joined
    assert "[REDACTED]" in joined


def test_release_scanner_uses_runtime_home_for_private_paths(tmp_path, monkeypatch):
    module = _load_release_check_module()
    fake_home = tmp_path / "home" / "agent"
    fake_home.mkdir(parents=True)
    source = tmp_path / "source"
    source.mkdir()
    (source / "notes.md").write_text("local file " + str(fake_home / ".hermes-yuheng" / "secret.log") + "\n", encoding="utf-8")

    original_root = getattr(module, "ROOT")
    setattr(module, "ROOT", source)
    monkeypatch.setattr(module.pathlib.Path, "home", staticmethod(lambda: fake_home))
    try:
        findings = module.scan_tree()
    finally:
        setattr(module, "ROOT", original_root)

    assert findings["private_paths"] == ["notes.md:1"]
