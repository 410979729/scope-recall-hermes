from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

PLUGIN_NAME = "scope-recall"
PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def test_distribution_metadata_exposes_official_standalone_install_shape():
    pyproject = tomllib.loads((PLUGIN_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["name"] == "hermes-scope-recall"
    assert pyproject["project"]["version"] == "1.1.0"
    assert pyproject["project"]["scripts"] == {
        "hermes-scope-recall": "scope_recall.installer:main"
    }
    package_data = pyproject["tool"]["setuptools"]["package-data"]["scope_recall"]
    assert "plugin.yaml" in package_data
    assert "config.json" in package_data
    assert "pyproject.toml" in package_data
    assert "docs/*.md" in package_data
    assert "scripts/*.py" in package_data


def test_installer_dry_run_does_not_mutate_hermes_home(tmp_path):
    from scope_recall import installer

    result = installer.install(hermes_home=tmp_path, dry_run=True)

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["installed"] is False
    assert result["plugin_dir"] == str(tmp_path / "plugins" / PLUGIN_NAME)
    assert not (tmp_path / "plugins" / PLUGIN_NAME).exists()
    next_steps = "\n".join(result["next_steps"])
    assert "hermes config set memory.provider scope-recall" in next_steps
    assert f"hermes-scope-recall verify --hermes-home {tmp_path}" in next_steps


def test_installer_copy_ignores_only_relative_artifacts_not_venv_ancestor(tmp_path, monkeypatch):
    from scope_recall import installer

    fake_source = tmp_path / "venv" / "lib" / "python3.12" / "site-packages" / "scope_recall"
    fake_source.mkdir(parents=True)
    for rel in ["__init__.py", "provider.py", "plugin.yaml", "config.json"]:
        target = fake_source / rel
        if rel == "plugin.yaml":
            content = "name: scope-recall\nversion: 1.1.0\n"
        elif rel == "__init__.py":
            content = '"""register_memory_provider marker for Hermes discovery."""\n'
        else:
            content = ""
        target.write_text(content, encoding="utf-8")
    (fake_source / "__pycache__").mkdir()
    (fake_source / "__pycache__" / "ignored.pyc").write_bytes(b"pyc")
    monkeypatch.setattr(installer, "source_root", lambda: fake_source)

    result = installer.install(hermes_home=tmp_path / "home")

    plugin_dir = tmp_path / "home" / "plugins" / PLUGIN_NAME
    assert result["ok"] is True
    assert result["verify"]["ok"] is True
    assert (plugin_dir / "__init__.py").is_file()
    assert (plugin_dir / "provider.py").is_file()
    assert (plugin_dir / "plugin.yaml").is_file()
    assert not (plugin_dir / "__pycache__").exists()


def test_installer_copies_plugin_and_verify_accepts_it(tmp_path):
    from scope_recall import installer

    install_result = installer.install(hermes_home=tmp_path)
    verify_result = installer.verify(hermes_home=tmp_path)

    plugin_dir = tmp_path / "plugins" / PLUGIN_NAME
    assert install_result["ok"] is True
    assert install_result["installed"] is True
    assert install_result["mode"] == "copy"
    assert install_result["plugin_dir"] == str(plugin_dir)
    assert verify_result["ok"] is True
    assert verify_result["plugin_dir"] == str(plugin_dir)
    assert verify_result["missing"] == []
    assert (plugin_dir / "__init__.py").is_file()
    assert (plugin_dir / "provider.py").is_file()
    assert (plugin_dir / "plugin.yaml").read_text(encoding="utf-8").startswith("name: scope-recall")
    assert not (plugin_dir / ".git").exists()
    assert not (plugin_dir / "__pycache__").exists()
    assert not any(plugin_dir.rglob("*.pyc"))


def test_installer_refuses_to_overwrite_foreign_plugin_without_force(tmp_path):
    from scope_recall import installer

    plugin_dir = tmp_path / "plugins" / PLUGIN_NAME
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text("name: other\nversion: 0.0.1\n", encoding="utf-8")

    with pytest.raises(installer.InstallError):
        installer.install(hermes_home=tmp_path)

    result = installer.install(hermes_home=tmp_path, force=True)
    assert result["ok"] is True
    assert (plugin_dir / "plugin.yaml").read_text(encoding="utf-8").startswith("name: scope-recall")


def test_installer_refuses_to_overwrite_unknown_existing_target_without_force(tmp_path):
    from scope_recall import installer

    plugin_dir = tmp_path / "plugins" / PLUGIN_NAME
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "README.md").write_text("unknown existing content\n", encoding="utf-8")

    with pytest.raises(installer.InstallError):
        installer.install(hermes_home=tmp_path)

    assert (plugin_dir / "README.md").read_text(encoding="utf-8") == "unknown existing content\n"


def test_installer_refuses_to_overwrite_regular_file_target_without_force(tmp_path):
    from scope_recall import installer

    plugin_dir = tmp_path / "plugins" / PLUGIN_NAME
    plugin_dir.parent.mkdir(parents=True)
    plugin_dir.write_text("not a plugin directory\n", encoding="utf-8")

    with pytest.raises(installer.InstallError):
        installer.install(hermes_home=tmp_path)

    assert plugin_dir.is_file()
    assert plugin_dir.read_text(encoding="utf-8") == "not a plugin directory\n"


def test_installer_excludes_local_secret_state_and_symlink_artifacts(tmp_path, monkeypatch):
    from scope_recall import installer

    fake_source = tmp_path / "src" / "scope_recall"
    fake_source.mkdir(parents=True)
    for rel in ["__init__.py", "provider.py", "plugin.yaml", "config.json"]:
        target = fake_source / rel
        if rel == "plugin.yaml":
            content = "name: scope-recall\nversion: 1.1.0\n"
        elif rel == "__init__.py":
            content = '"""register_memory_provider marker for Hermes discovery."""\n'
        else:
            content = ""
        target.write_text(content, encoding="utf-8")
    (fake_source / ".env.local").write_text("SECRET=do-not-copy\n", encoding="utf-8")
    (fake_source / "memory.sqlite3").write_text("not a real sqlite db\n", encoding="utf-8")
    (fake_source / "lancedb").mkdir()
    (fake_source / "lancedb" / "fragment").write_text("state\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (fake_source / "outside-link.txt").symlink_to(outside)
    monkeypatch.setattr(installer, "source_root", lambda: fake_source)

    result = installer.install(hermes_home=tmp_path / "home")

    plugin_dir = tmp_path / "home" / "plugins" / PLUGIN_NAME
    assert result["ok"] is True
    assert not (plugin_dir / ".env.local").exists()
    assert not (plugin_dir / "memory.sqlite3").exists()
    assert not (plugin_dir / "lancedb").exists()
    assert not (plugin_dir / "outside-link.txt").exists()


def test_installer_windows_default_matches_hermes_platform_default(tmp_path, monkeypatch):
    from scope_recall import installer

    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))
    monkeypatch.setattr(installer.sys, "platform", "win32")

    assert installer.resolve_hermes_home() == (tmp_path / "LocalAppData" / "hermes").resolve()


def test_installed_plugin_verify_requires_hermes_discovery_marker(tmp_path):
    from scope_recall import installer

    result = installer.install(hermes_home=tmp_path)
    assert result["ok"] is True
    init_file = tmp_path / "plugins" / PLUGIN_NAME / "__init__.py"
    init_file.write_text("def register(ctx):\n    return None\n", encoding="utf-8")

    verify = installer.verify(hermes_home=tmp_path)

    assert verify["ok"] is False
    assert "__init__.py discovery marker" in verify["failures"]


def test_installed_plugin_loads_through_hermes_memory_discovery(tmp_path, monkeypatch):
    from scope_recall import installer

    installer.install(hermes_home=tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    # Clear provider-discovery module cache entries that can otherwise point at
    # a previous temporary HERMES_HOME during the same pytest process.
    for name in list(sys.modules):
        if name.startswith("_hermes_user_memory.scope-recall"):
            sys.modules.pop(name, None)

    from plugins.memory import load_memory_provider

    provider = load_memory_provider(PLUGIN_NAME)
    assert provider is not None
    assert provider.name == PLUGIN_NAME
    assert provider.is_available() is True


def test_installer_cli_json_verify_round_trip(tmp_path):
    from scope_recall import installer

    install_exit = installer.main(["install", "--hermes-home", str(tmp_path), "--json"])
    verify_exit = installer.main(["verify", "--hermes-home", str(tmp_path), "--json"])

    assert install_exit == 0
    assert verify_exit == 0
    assert (tmp_path / "plugins" / PLUGIN_NAME / "plugin.yaml").is_file()
