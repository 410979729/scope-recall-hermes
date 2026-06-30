"""Tests for install, upgrade, verify, rollback, and packaged CLI behavior.

They ensure operator copy operations remain dry-run friendly and rollback-aware."""

from __future__ import annotations

import sqlite3
import sys
import tomllib
from pathlib import Path

import pytest

PLUGIN_NAME = "scope-recall"
PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _write_installed_plugin(plugin_dir: Path, *, version: str, marker: str = "old plugin") -> None:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.yaml").write_text(f"name: scope-recall\nversion: {version}\n", encoding="utf-8")
    (plugin_dir / "__init__.py").write_text(f'"""{marker}"""\n# register_memory_provider\n', encoding="utf-8")
    (plugin_dir / "provider.py").write_text(f"MARKER = {marker!r}\n", encoding="utf-8")
    (plugin_dir / "config.json").write_text("{}\n", encoding="utf-8")


def test_distribution_metadata_exposes_official_standalone_install_shape():
    pyproject = tomllib.loads((PLUGIN_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["name"] == "hermes-scope-recall"
    assert pyproject["project"]["version"] == "1.6.0"
    assert pyproject["project"]["scripts"] == {
        "hermes-scope-recall": "scope_recall.cli:main"
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
            content = "name: scope-recall\nversion: 1.4.1\n"
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
    assert verify_result["runtime"] == {"requested": False}
    assert verify_result["plugin_dir"] == str(plugin_dir)
    assert verify_result["missing"] == []
    assert (plugin_dir / "__init__.py").is_file()
    assert (plugin_dir / "provider.py").is_file()
    assert (plugin_dir / "plugin.yaml").read_text(encoding="utf-8").startswith("name: scope-recall")
    assert not (plugin_dir / ".git").exists()
    assert not (plugin_dir / "__pycache__").exists()
    assert not any(plugin_dir.rglob("*.pyc"))


def test_installer_upgrade_backs_up_existing_plugin_and_reports_versions(tmp_path):
    import scope_recall.installer as installer

    plugin_dir = tmp_path / "plugins" / PLUGIN_NAME
    _write_installed_plugin(plugin_dir, version="0.9.0", marker="previous plugin")

    result = installer.install(hermes_home=tmp_path)

    assert result["ok"] is True
    assert result["installed"] is True
    assert result["previous_plugin_existed"] is True
    assert result["previous_version"] == "0.9.0"
    assert result["manifest_version"] == "1.6.0"
    assert result["new_version"] == "1.6.0"
    backup_path = Path(result["backup_path"])
    assert backup_path.is_dir()
    assert tmp_path in backup_path.parents
    assert "version: 0.9.0" in (backup_path / "plugin.yaml").read_text(encoding="utf-8")
    assert "previous plugin" in (backup_path / "__init__.py").read_text(encoding="utf-8")
    assert "version: 1.6.0" in (plugin_dir / "plugin.yaml").read_text(encoding="utf-8")
    assert any("restart" in step.lower() for step in result["next_steps"])
    assert any("doctor" in step for step in result["next_steps"])
    assert result["rollback_command"].endswith(str(backup_path))


def test_installer_rollback_restores_backup_and_backs_up_current_plugin(tmp_path):
    import scope_recall.installer as installer

    plugin_dir = tmp_path / "plugins" / PLUGIN_NAME
    _write_installed_plugin(plugin_dir, version="0.9.0", marker="previous plugin")
    upgrade = installer.install(hermes_home=tmp_path)
    assert "version: 1.6.0" in (plugin_dir / "plugin.yaml").read_text(encoding="utf-8")

    rollback = installer.rollback(hermes_home=tmp_path, backup_dir=upgrade["backup_path"])

    assert rollback["ok"] is True
    assert rollback["dry_run"] is False
    assert rollback["restored"] is True
    assert rollback["restored_version"] == "0.9.0"
    assert rollback["replaced_version"] == "1.6.0"
    current_backup = Path(rollback["current_backup_path"])
    assert current_backup.is_dir()
    assert "version: 1.6.0" in (current_backup / "plugin.yaml").read_text(encoding="utf-8")
    assert "version: 0.9.0" in (plugin_dir / "plugin.yaml").read_text(encoding="utf-8")
    assert "previous plugin" in (plugin_dir / "__init__.py").read_text(encoding="utf-8")


def test_installer_rollback_refuses_bad_backup_without_mutating_current_plugin(tmp_path):
    import scope_recall.installer as installer

    plugin_dir = tmp_path / "plugins" / PLUGIN_NAME
    _write_installed_plugin(plugin_dir, version="1.6.0", marker="current plugin")
    bad_backup = tmp_path / "bad-backup" / PLUGIN_NAME
    bad_backup.mkdir(parents=True)
    (bad_backup / "plugin.yaml").write_text("name: other\nversion: 0.1.0\n", encoding="utf-8")

    with pytest.raises(installer.InstallError):
        installer.rollback(hermes_home=tmp_path, backup_dir=bad_backup)

    assert "version: 1.6.0" in (plugin_dir / "plugin.yaml").read_text(encoding="utf-8")
    assert "current plugin" in (plugin_dir / "__init__.py").read_text(encoding="utf-8")


def test_installer_cli_upgrade_dry_run_and_rollback_are_routed_by_product_cli(tmp_path):
    import scope_recall.cli as cli
    import scope_recall.installer as installer

    plugin_dir = tmp_path / "plugins" / PLUGIN_NAME
    _write_installed_plugin(plugin_dir, version="0.9.0")

    assert cli.main(["upgrade", "--hermes-home", str(tmp_path), "--dry-run", "--json"]) == 0
    assert "version: 0.9.0" in (plugin_dir / "plugin.yaml").read_text(encoding="utf-8")

    upgrade = installer.install(hermes_home=tmp_path)
    assert cli.main(["rollback", "--hermes-home", str(tmp_path), "--backup-dir", upgrade["backup_path"], "--dry-run", "--json"]) == 0
    assert "version: 1.6.0" in (plugin_dir / "plugin.yaml").read_text(encoding="utf-8")


def test_installer_runtime_verify_reports_missing_memory_setup(tmp_path):
    from scope_recall import installer

    installer.install(hermes_home=tmp_path)

    verify_result = installer.verify(hermes_home=tmp_path, runtime=True)

    assert verify_result["ok"] is False
    assert verify_result["runtime"]["provider_loaded"] is True
    assert any("SQLite truth DB missing" in failure for failure in verify_result["failures"])
    assert "hermes memory setup" in verify_result["next_steps"]
    assert not any("install --hermes-home" in step for step in verify_result["next_steps"])


def test_installer_runtime_verify_reports_schema_ledger_repair_steps_without_reinstall(tmp_path):
    import scope_recall.installer as installer
    from scope_recall.sql_store import ensure_schema

    installer.install(hermes_home=tmp_path)
    storage_dir = tmp_path / "scope-recall"
    storage_dir.mkdir(parents=True)
    conn = sqlite3.connect(storage_dir / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        conn.execute("DELETE FROM schema_migrations")
        conn.commit()
    finally:
        conn.close()

    verify_result = installer.verify(hermes_home=tmp_path, runtime=True)

    assert verify_result["ok"] is False
    runtime = verify_result["runtime"]
    assert runtime["provider_loaded"] is True
    assert runtime["sqlite_schema_current"] is False
    assert runtime["schema_migrations"]["missing_migrations"] == ["0001_baseline_v1_6_0"]
    assert "SQLite schema migration ledger is not current" in verify_result["failures"]
    assert any("migrate status" in step for step in verify_result["next_steps"])
    assert "hermes memory setup" in verify_result["next_steps"]
    assert not any("install --hermes-home" in step for step in verify_result["next_steps"])


def test_installer_runtime_verify_loads_provider_tools_and_schema(tmp_path):
    from scope_recall import installer
    from scope_recall.sql_store import ensure_schema

    installer.install(hermes_home=tmp_path)
    storage_dir = tmp_path / "scope-recall"
    storage_dir.mkdir(parents=True)
    conn = sqlite3.connect(storage_dir / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    verify_result = installer.verify(hermes_home=tmp_path, runtime=True)

    assert verify_result["ok"] is True
    runtime = verify_result["runtime"]
    assert runtime["provider_loaded"] is True
    assert runtime["sqlite_schema_current"] is True
    assert runtime["schema_migrations"]["current"] is True
    assert {"scope_recall_memory", "scope_recall_entity"} <= set(runtime["tool_schema_names"])
    assert "auto_recall" in runtime["config_schema_keys"]
    assert not any(name == "_scope_recall_runtime_verify" or name.startswith("_scope_recall_runtime_verify.") for name in sys.modules)


def test_installer_runtime_verify_schema_check_opens_sqlite_read_only_query_only(tmp_path, monkeypatch):
    import scope_recall.installer as installer
    from scope_recall.sql_store import ensure_schema

    installer.install(hermes_home=tmp_path)
    storage_dir = tmp_path / "scope-recall"
    storage_dir.mkdir(parents=True)
    db_path = storage_dir / "memory.sqlite3"
    writer = sqlite3.connect(db_path)
    writer.row_factory = sqlite3.Row
    try:
        ensure_schema(writer)
    finally:
        writer.close()

    real_connect = sqlite3.connect
    observed_databases: list[str] = []
    observed_query_only: list[int] = []

    class ObservedConnection:
        def __init__(self, inner: sqlite3.Connection):
            self._inner = inner

        def execute(self, sql, *args, **kwargs):
            result = self._inner.execute(sql, *args, **kwargs)
            if str(sql).strip().lower() == "pragma query_only=on":
                observed_query_only.append(int(self._inner.execute("PRAGMA query_only").fetchone()[0]))
            return result

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def capture_connect(database, *args, **kwargs):
        observed_databases.append(str(database))
        conn = real_connect(database, *args, **kwargs)
        if str(database).startswith("file:") and str(database).endswith("?mode=ro"):
            return ObservedConnection(conn)
        return conn

    monkeypatch.setattr(installer.sqlite3, "connect", capture_connect)

    verify_result = installer.verify(hermes_home=tmp_path, runtime=True)

    assert verify_result["ok"] is True
    assert f"file:{db_path}?mode=ro" in observed_databases
    assert observed_query_only == [1]


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
            content = "name: scope-recall\nversion: 1.4.1\n"
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

    pytest.importorskip("plugins.memory")
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


def test_installer_cli_runtime_verify_after_memory_setup(tmp_path):
    from scope_recall import installer
    from scope_recall.sql_store import ensure_schema

    assert installer.main(["install", "--hermes-home", str(tmp_path), "--json"]) == 0
    storage_dir = tmp_path / "scope-recall"
    storage_dir.mkdir(parents=True)
    conn = sqlite3.connect(storage_dir / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    assert installer.main(["verify", "--runtime", "--hermes-home", str(tmp_path), "--json"]) == 0


def test_distribution_script_entrypoint_uses_product_cli():
    pyproject = tomllib.loads((PLUGIN_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["scripts"] == {
        "hermes-scope-recall": "scope_recall.cli:main"
    }


def test_product_cli_dispatches_existing_operator_scripts(monkeypatch):
    from scope_recall import cli

    calls = []

    def fake_run(script_name, forwarded_args):
        calls.append((script_name, list(forwarded_args)))
        return 0

    monkeypatch.setattr(cli, "_run_script", fake_run)

    assert cli.main(["doctor", "--json", "--hermes-home", "/tmp/home"]) == 0
    assert cli.main(["dashboard", "--output", "/tmp/dashboard.json"]) == 0
    assert cli.main(["journal", "digest", "--limit-entries", "10"]) == 0
    assert cli.main(["journal", "recovery", "--dry-run"]) == 0
    assert cli.main(["candidates", "report", "--hermes-home", "/tmp/home"]) == 0
    assert cli.main(["candidates", "apply", "--hermes-home", "/tmp/home"]) == 0
    assert cli.main(["vector", "repair", "--dry-run"]) == 0
    assert cli.main(["governance", "cleanup", "--dry-run"]) == 0
    assert cli.main(["governance", "audit-coverage", "--dry-run"]) == 0
    assert cli.main(["benchmark", "golden", "--auto-explain-on-fail"]) == 0
    assert cli.main(["benchmark", "experience", "--case-file", "/tmp/cases.json"]) == 0
    assert cli.main(["playbooks", "bootstrap", "--dry-run"]) == 0
    assert cli.main(["playbooks", "list", "--status", "candidate"]) == 0
    assert cli.main(["playbooks", "dedupe", "--limit", "5"]) == 0
    assert cli.main(["playbooks", "review", "--id", "pb1", "--reason", "ok"]) == 0
    assert cli.main(["playbooks", "promote", "--id", "pb1", "--reason", "ok"]) == 0
    assert cli.main(["playbooks", "quarantine", "--id", "pb1", "--reason", "bad"]) == 0
    assert cli.main(["governance", "rollback", "--batch-id", "b1", "--apply"]) == 0
    assert cli.main(["migrate", "status", "--hermes-home", "/tmp/home"]) == 0
    assert cli.main(["migrate", "apply", "--hermes-home", "/tmp/home"]) == 0
    assert cli.main(["migrate", "openclaw-import", "--source", "/tmp/openclaw", "--hermes-home", "/tmp/home", "--dry-run"]) == 0

    assert calls == [
        ("doctor.py", ["--json", "--hermes-home", "/tmp/home"]),
        ("report.dashboard.py", ["--output", "/tmp/dashboard.json"]),
        ("journal-digest.py", ["--limit-entries", "10"]),
        ("journal.recovery.py", ["--dry-run"]),
        ("promote.memory_candidates.py", ["--dry-run", "--hermes-home", "/tmp/home"]),
        ("promote.memory_candidates.py", ["--apply", "--hermes-home", "/tmp/home"]),
        ("repair.vector_index.py", ["--dry-run"]),
        ("governance.cleanup.py", ["--dry-run"]),
        ("governance.audit_coverage.py", ["--dry-run"]),
        ("benchmark.golden.py", ["--auto-explain-on-fail"]),
        ("experience-replay.py", ["--case-file", "/tmp/cases.json"]),
        ("playbook.bootstrap.py", ["--dry-run"]),
        ("playbooks.py", ["list", "--status", "candidate"]),
        ("playbooks.py", ["dedupe", "--limit", "5"]),
        ("playbooks.py", ["review", "--id", "pb1", "--reason", "ok"]),
        ("playbooks.py", ["promote", "--id", "pb1", "--reason", "ok"]),
        ("playbooks.py", ["quarantine", "--id", "pb1", "--reason", "bad"]),
        ("governance.cleanup.py", ["--rollback-batch", "--batch-id", "b1", "--apply"]),
        ("migrate.status.py", ["--hermes-home", "/tmp/home"]),
        ("migrate.legacy_hygiene.py", ["--apply", "--hermes-home", "/tmp/home"]),
        ("import.openclaw.memory_lancedb_pro.py", ["--source", "/tmp/openclaw", "--hermes-home", "/tmp/home", "--dry-run"]),
    ]


def test_product_cli_keeps_install_and_verify_compatibility(tmp_path):
    from scope_recall import cli

    assert cli.main(["install", "--hermes-home", str(tmp_path), "--json"]) == 0
    assert cli.main(["verify", "--hermes-home", str(tmp_path), "--json"]) == 0
