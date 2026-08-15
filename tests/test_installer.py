"""Tests for install, upgrade, verify, rollback, and packaged CLI behavior.

They ensure operator copy operations remain dry-run friendly and rollback-aware."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tomllib
import zipfile
from contextlib import closing
from pathlib import Path

import pytest
import yaml

from scope_recall.response_schemas import DOCTOR_REQUIRED_CHECK_NAMES

PLUGIN_NAME = "scope-recall"
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_POSTDEPLOY_DOCTOR_CHECKS = DOCTOR_REQUIRED_CHECK_NAMES


def _isolate_fresh_installer_credentials(monkeypatch, tmp_path: Path) -> None:
    """Remove ambient user/provider state from credential-free install tests."""

    user_home = tmp_path / "isolated-user-home"
    config_home = tmp_path / "isolated-xdg-config"
    ambient_hermes_home = tmp_path / "isolated-ambient-hermes"
    user_home.mkdir()
    config_home.mkdir()
    ambient_hermes_home.mkdir()
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("HERMES_HOME", str(ambient_hermes_home))
    for key in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "OPENROUTER_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "COHERE_API_KEY",
        "VOYAGE_API_KEY",
        "MISTRAL_API_KEY",
        "SCOPE_RECALL_GEMINI_EMBEDDING_API_KEY",
        "SCOPE_RECALL_OPENAI_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)


def _write_installed_plugin(plugin_dir: Path, *, version: str, marker: str = "old plugin") -> None:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.yaml").write_text(f"name: scope-recall\nversion: {version}\n", encoding="utf-8")
    (plugin_dir / "__init__.py").write_text(f'"""{marker}"""\n# register_memory_provider\n', encoding="utf-8")
    (plugin_dir / "provider.py").write_text(f"MARKER = {marker!r}\n", encoding="utf-8")
    (plugin_dir / "config.json").write_text("{}\n", encoding="utf-8")


def _write_installer_state(home: Path) -> tuple[bytes, bytes]:
    config_path = home / "config.yaml"
    config_path.write_text(
        "model:\n  provider: openrouter\nmemory:\n  provider: legacy-memory\n",
        encoding="utf-8",
    )
    storage = home / "scope-recall"
    storage.mkdir(parents=True, exist_ok=True)
    storage_config = storage / "config.json"
    storage_config.write_text('{"auto_capture":false}\n', encoding="utf-8")
    conn = sqlite3.connect(storage / "memory.sqlite3")
    try:
        conn.execute("CREATE TABLE installer_sentinel(value TEXT NOT NULL)")
        conn.execute("INSERT INTO installer_sentinel(value) VALUES ('before')")
        conn.execute("PRAGMA user_version=10600")
        conn.commit()
    finally:
        conn.close()
    return config_path.read_bytes(), storage_config.read_bytes()


def _assert_installer_state_restored(
    home: Path,
    *,
    config_bytes: bytes,
    storage_config_bytes: bytes,
) -> None:
    assert (home / "config.yaml").read_bytes() == config_bytes
    assert (home / "scope-recall" / "config.json").read_bytes() == storage_config_bytes
    conn = sqlite3.connect(home / "scope-recall" / "memory.sqlite3")
    try:
        assert conn.execute("SELECT value FROM installer_sentinel").fetchone()[0] == "before"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 10600
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='activation_partial'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def _patch_doctor_subprocess(
    monkeypatch,
    installer,
    *,
    stdout_text: str,
    returncode: int = 0,
    stderr_text: str = "",
) -> None:
    """Support both PIPE and bounded-file subprocess capture implementations."""

    def fake_run(*args, **kwargs):
        stdout_target = kwargs.get("stdout")
        stderr_target = kwargs.get("stderr")
        assert stdout_target != subprocess.PIPE
        assert stderr_target != subprocess.PIPE
        stdout_target.write(stdout_text.encode("utf-8"))
        stderr_target.write(stderr_text.encode("utf-8"))
        return subprocess.CompletedProcess(
            args=args[0] if args else kwargs.get("args"),
            returncode=returncode,
        )

    monkeypatch.setattr(installer.subprocess, "run", fake_run)


def test_distribution_metadata_exposes_official_standalone_install_shape():
    pyproject = tomllib.loads((PLUGIN_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["name"] == "hermes-scope-recall"
    assert pyproject["project"]["version"] == "1.9.3"
    assert pyproject["project"]["scripts"] == {
        "hermes-scope-recall": "scope_recall.cli:main"
    }
    package_data = pyproject["tool"]["setuptools"]["package-data"]["scope_recall"]
    assert "plugin.yaml" in package_data
    assert "config.json" in package_data
    assert "pyproject.toml" in package_data
    assert "docs/*.md" in package_data
    assert "scripts/*.py" in package_data
    assert "scripts/*.json" in package_data


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
        elif rel == "config.json":
            content = "{}\n"
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


def test_install_activate_sets_memory_provider_and_bootstraps_schema(tmp_path, monkeypatch):
    from scope_recall import installer

    _isolate_fresh_installer_credentials(monkeypatch, tmp_path)
    (tmp_path / "config.yaml").write_text("model:\n  provider: openrouter\n", encoding="utf-8")

    result = installer.install(hermes_home=tmp_path, activate=True)

    assert result["ok"] is True, {
        "mode": result.get("mode"),
        "activation_error": result.get("activation_error"),
        "runtime_verify": result.get("runtime_verify"),
        "postdeploy_doctor": result.get("postdeploy_doctor"),
        "activation_transaction": result.get("activation_transaction"),
    }
    assert result["installed"] is True
    assert result["activated"] is True
    assert result["config_updated"] is True
    assert result["sqlite_schema_current"] is True
    assert result["runtime_verify"]["ok"] is True
    runtime_vector = result["runtime_verify"]["runtime"]["vector_companion"]
    assert runtime_vector["status"] == "ready"
    assert runtime_vector["active_backend"] == "sqlite-bruteforce"
    assert runtime_vector["generation_status"] == "active"
    assert result["postdeploy_doctor"]["requested"] is True
    assert result["postdeploy_doctor"]["ok"] is True
    assert result["postdeploy_doctor"]["returncode"] == 0
    assert result["postdeploy_doctor"]["failed_checks"] == []
    assert result["activation_transaction"]["status"] == "committed"
    assert result["verify"]["runtime"]["sqlite_schema_current"] is True
    assert result["rollback_command"]
    config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "model:\n  provider: openrouter" in config_text
    assert "memory:\n  provider: scope-recall" in config_text
    db_path = tmp_path / "scope-recall" / "memory.sqlite3"
    assert db_path.is_file()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] >= 1
        assert conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 0
        generation = conn.execute(
            """
            SELECT g.*
            FROM vector_generation_state AS s
            JOIN vector_generations AS g ON g.generation_id = s.value
            WHERE s.key = 'current_generation'
            """
        ).fetchone()
        assert generation is not None
        assert generation["status"] == "active"
        assert generation["provider"] == "local-hash"
        assert generation["model"] == "hash-v1"
        assert generation["row_count"] == 0
    finally:
        conn.close()


@pytest.mark.parametrize(
    "checks",
    [{}, [], None],
    ids=["empty", "wrong-type", "missing"],
)
def test_postdeploy_doctor_rejects_missing_required_check_contract(
    tmp_path,
    monkeypatch,
    checks,
):
    import scope_recall.installer as installer

    plugin_dir = tmp_path / "candidate"
    doctor_script = plugin_dir / "scripts" / "doctor.py"
    doctor_script.parent.mkdir(parents=True)
    doctor_script.write_text("# injected doctor stub\n", encoding="utf-8")
    _patch_doctor_subprocess(
        monkeypatch,
        installer,
        stdout_text=json.dumps(
            {
                "schema_version": "doctor_report.v1",
                "ok": True,
                "checks": checks,
                "recommendations": [],
            }
        ),
    )

    report = installer._postdeploy_doctor_verify(tmp_path / "home", plugin_dir)

    assert report["ok"] is False
    assert set(REQUIRED_POSTDEPLOY_DOCTOR_CHECKS) <= set(report["failed_checks"])
    assert "required" in " ".join(report["failures"]).lower()


@pytest.mark.parametrize("schema_version", ["", "doctor_report.v0", 1])
def test_postdeploy_doctor_rejects_wrong_schema_version(
    tmp_path,
    monkeypatch,
    schema_version,
):
    import scope_recall.installer as installer

    plugin_dir = tmp_path / "candidate"
    doctor_script = plugin_dir / "scripts" / "doctor.py"
    doctor_script.parent.mkdir(parents=True)
    doctor_script.write_text("# injected doctor stub\n", encoding="utf-8")
    checks = {
        name: {"ok": True}
        for name in REQUIRED_POSTDEPLOY_DOCTOR_CHECKS
    }
    _patch_doctor_subprocess(
        monkeypatch,
        installer,
        stdout_text=json.dumps(
            {
                "schema_version": schema_version,
                "ok": True,
                "checks": checks,
                "recommendations": [],
            }
        ),
    )

    report = installer._postdeploy_doctor_verify(tmp_path / "home", plugin_dir)

    assert report["ok"] is False
    assert "doctor_schema_version" in report["failed_checks"]
    assert "schema" in " ".join(report["failures"]).lower()


@pytest.mark.parametrize("stdout_text", ["{not-json", "[]", "null"])
def test_postdeploy_doctor_rejects_malformed_or_non_object_json(
    tmp_path,
    monkeypatch,
    stdout_text,
):
    import scope_recall.installer as installer

    plugin_dir = tmp_path / "candidate"
    doctor_script = plugin_dir / "scripts" / "doctor.py"
    doctor_script.parent.mkdir(parents=True)
    doctor_script.write_text("# injected doctor stub\n", encoding="utf-8")
    _patch_doctor_subprocess(
        monkeypatch,
        installer,
        stdout_text=stdout_text,
    )

    report = installer._postdeploy_doctor_verify(tmp_path / "home", plugin_dir)

    assert report["ok"] is False
    assert report["failed_checks"] == ["doctor_payload"]
    assert "json" in " ".join(report["failures"]).lower()


def test_postdeploy_doctor_timeout_fails_closed_without_returning_output(
    tmp_path,
    monkeypatch,
):
    import scope_recall.installer as installer

    plugin_dir = tmp_path / "candidate"
    doctor_script = plugin_dir / "scripts" / "doctor.py"
    doctor_script.parent.mkdir(parents=True)
    doctor_script.write_text("# injected doctor stub\n", encoding="utf-8")

    def timeout_run(*args, **kwargs):
        kwargs["stdout"].write(b"PRIVATE_TIMEOUT_OUTPUT")
        kwargs["stderr"].write(b"PRIVATE_TIMEOUT_ERROR")
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(installer.subprocess, "run", timeout_run)
    report = installer._postdeploy_doctor_verify(tmp_path / "home", plugin_dir)
    serialized = json.dumps(report, ensure_ascii=False)

    assert report["ok"] is False
    assert report["timed_out"] is True
    assert report["failed_checks"] == ["doctor_process"]
    assert "PRIVATE_TIMEOUT" not in serialized


def test_postdeploy_doctor_nonzero_exit_fails_even_with_valid_payload(
    tmp_path,
    monkeypatch,
):
    import scope_recall.installer as installer

    plugin_dir = tmp_path / "candidate"
    doctor_script = plugin_dir / "scripts" / "doctor.py"
    doctor_script.parent.mkdir(parents=True)
    doctor_script.write_text("# injected doctor stub\n", encoding="utf-8")
    checks = {
        name: {"ok": True}
        for name in REQUIRED_POSTDEPLOY_DOCTOR_CHECKS
    }
    _patch_doctor_subprocess(
        monkeypatch,
        installer,
        stdout_text=json.dumps(
            {
                "schema_version": "doctor_report.v1",
                "ok": True,
                "checks": checks,
            }
        ),
        returncode=7,
    )

    report = installer._postdeploy_doctor_verify(tmp_path / "home", plugin_dir)

    assert report["ok"] is False
    assert report["returncode"] == 7
    assert "doctor_process" in report["failed_checks"]


def test_postdeploy_doctor_requires_strict_true_report_status(
    tmp_path,
    monkeypatch,
):
    import scope_recall.installer as installer

    plugin_dir = tmp_path / "candidate"
    doctor_script = plugin_dir / "scripts" / "doctor.py"
    doctor_script.parent.mkdir(parents=True)
    doctor_script.write_text("# injected doctor stub\n", encoding="utf-8")
    checks = {
        name: {"ok": True}
        for name in REQUIRED_POSTDEPLOY_DOCTOR_CHECKS
    }
    _patch_doctor_subprocess(
        monkeypatch,
        installer,
        stdout_text=json.dumps(
            {
                "schema_version": "doctor_report.v1",
                "ok": "true",
                "checks": checks,
            }
        ),
    )

    report = installer._postdeploy_doctor_verify(tmp_path / "home", plugin_dir)

    assert report["ok"] is False
    assert report["failed_checks"] == ["doctor_report"]


def test_postdeploy_doctor_returns_only_bounded_check_statuses(
    tmp_path,
    monkeypatch,
):
    import scope_recall.installer as installer

    plugin_dir = tmp_path / "candidate"
    doctor_script = plugin_dir / "scripts" / "doctor.py"
    doctor_script.parent.mkdir(parents=True)
    doctor_script.write_text("# injected doctor stub\n", encoding="utf-8")
    checks = {
        name: {"ok": True}
        for name in REQUIRED_POSTDEPLOY_DOCTOR_CHECKS
    }
    checks["source_metadata"] = {
        "ok": True,
        "details": {
            "sample": "PRIVATE_DOCTOR_SAMPLE",
            "path": "/home/private-user/memory/raw.txt",
        },
    }
    checks["unknown_private_check"] = {
        "ok": True,
        "details": "PRIVATE_UNKNOWN_CHECK_DETAIL",
    }
    _patch_doctor_subprocess(
        monkeypatch,
        installer,
        stdout_text=json.dumps(
            {
                "schema_version": "doctor_report.v1",
                "ok": True,
                "checks": checks,
                "runtime": {"private": "PRIVATE_RUNTIME_DETAIL"},
                "recommendations": [],
            }
        ),
        stderr_text=(
            "PRIVATE_STDERR_SAMPLE /home/private-user/memory/private.sqlite3"
        ),
    )

    report = installer._postdeploy_doctor_verify(tmp_path / "home", plugin_dir)
    serialized = json.dumps(report, ensure_ascii=False)

    assert report["ok"] is False
    assert "doctor_check_contract" in report["failed_checks"]
    assert report["checks"] == {
        name: {"ok": True}
        for name in REQUIRED_POSTDEPLOY_DOCTOR_CHECKS
    }
    assert "PRIVATE_DOCTOR_SAMPLE" not in serialized
    assert "PRIVATE_UNKNOWN_CHECK_DETAIL" not in serialized
    assert "PRIVATE_RUNTIME_DETAIL" not in serialized
    assert "PRIVATE_STDERR_SAMPLE" not in serialized
    assert "/home/private-user" not in serialized


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_postdeploy_doctor_rejects_oversized_output(
    tmp_path,
    monkeypatch,
    stream,
):
    import scope_recall.installer as installer

    plugin_dir = tmp_path / "candidate"
    doctor_script = plugin_dir / "scripts" / "doctor.py"
    doctor_script.parent.mkdir(parents=True)
    doctor_script.write_text("# injected doctor stub\n", encoding="utf-8")
    checks = {
        name: {"ok": True}
        for name in REQUIRED_POSTDEPLOY_DOCTOR_CHECKS
    }
    valid_stdout = json.dumps(
        {
            "schema_version": "doctor_report.v1",
            "ok": True,
            "checks": checks,
            "recommendations": [],
        }
    )
    oversized_stdout = json.dumps(
        {
            "schema_version": "doctor_report.v1",
            "ok": True,
            "checks": checks,
            "runtime": {"padding": "x" * 1_100_000},
            "recommendations": [],
        }
    )
    _patch_doctor_subprocess(
        monkeypatch,
        installer,
        stdout_text=oversized_stdout if stream == "stdout" else valid_stdout,
        stderr_text="x" * 1_100_000 if stream == "stderr" else "",
    )

    report = installer._postdeploy_doctor_verify(tmp_path / "home", plugin_dir)

    assert report["ok"] is False
    assert report["failed_checks"] == ["doctor_output"]
    assert "output limit" in " ".join(report["failures"]).lower()


def test_postdeploy_doctor_child_output_is_rejected_without_unbounded_capture(tmp_path):
    import scope_recall.installer as installer

    plugin_dir = tmp_path / "candidate"
    doctor_script = plugin_dir / "scripts" / "doctor.py"
    doctor_script.parent.mkdir(parents=True)
    doctor_script.write_text(
        "import sys\nsys.stdout.write('x' * 2_000_000)\nsys.stdout.flush()\n",
        encoding="utf-8",
    )

    report = installer._postdeploy_doctor_verify(tmp_path / "home", plugin_dir)

    assert report["ok"] is False
    assert report["failed_checks"] == ["doctor_output"]
    assert "output limit" in " ".join(report["failures"]).lower()


def test_postdeploy_doctor_forces_utf8_mode_in_isolated_subprocess(tmp_path):
    """The Windows doctor child must emit UTF-8 even under ``-I`` isolation."""
    import scope_recall.installer as installer

    plugin_dir = tmp_path / "candidate"
    doctor_script = plugin_dir / "scripts" / "doctor.py"
    doctor_script.parent.mkdir(parents=True)
    checks = {
        name: {"ok": True}
        for name in REQUIRED_POSTDEPLOY_DOCTOR_CHECKS
    }
    payload = {
        "schema_version": "doctor_report.v1",
        "ok": True,
        "checks": checks,
        "runtime": {"encoding_canary": "£"},
        "recommendations": [],
    }
    doctor_script.write_text(
        "import json, sys\n"
        "assert sys.flags.utf8_mode == 1, sys.flags.utf8_mode\n"
        f"print(json.dumps({payload!r}, ensure_ascii=False))\n",
        encoding="utf-8",
    )

    report = installer._postdeploy_doctor_verify(tmp_path / "home", plugin_dir)

    assert report["ok"] is True
    assert report["returncode"] == 0
    assert report["failed_checks"] == []


def test_postdeploy_doctor_output_limit_does_not_cap_doctor_work_files(tmp_path):
    import scope_recall.installer as installer

    plugin_dir = tmp_path / "candidate"
    doctor_script = plugin_dir / "scripts" / "doctor.py"
    doctor_script.parent.mkdir(parents=True)
    work_file = tmp_path / "doctor-work.sqlite3"
    checks = {
        name: {"ok": True}
        for name in REQUIRED_POSTDEPLOY_DOCTOR_CHECKS
    }
    payload = json.dumps(
        {
            "schema_version": "doctor_report.v1",
            "ok": True,
            "checks": checks,
            "recommendations": [],
        }
    )
    doctor_script.write_text(
        "from pathlib import Path\n"
        f"Path({str(work_file)!r}).write_bytes(b'x' * 2_000_000)\n"
        f"print({payload!r})\n",
        encoding="utf-8",
    )

    report = installer._postdeploy_doctor_verify(tmp_path / "home", plugin_dir)

    assert report["ok"] is True
    assert report["failed_checks"] == []
    assert work_file.stat().st_size == 2_000_000


def test_inline_memory_yaml_preserves_sibling_keys_and_single_top_level_key():
    import scope_recall.installer as installer

    before = "memory: {provider: legacy-memory, max_items: 42}\nmodel:\n  provider: openrouter\n"

    after, changed = installer._set_memory_provider_yaml_text(before)
    parsed = yaml.safe_load(after)

    assert changed is True
    assert after.count("memory:") == 1
    assert parsed["memory"] == {"provider": "scope-recall", "max_items": 42}
    assert parsed["model"]["provider"] == "openrouter"


def test_quoted_inline_memory_key_and_comments_are_preserved():
    import scope_recall.installer as installer

    before = (
        "# operator comment\n"
        '"memory": {provider: legacy-memory, max_items: 42} # keep memory\n'
        "model:\n"
        "  provider: openrouter # keep model\n"
    )

    after, changed = installer._set_memory_provider_yaml_text(before)
    parsed = yaml.safe_load(after)

    assert changed is True
    assert after.count('"memory":') == 1
    assert "# operator comment" in after
    assert "# keep memory" in after
    assert "# keep model" in after
    assert parsed["memory"] == {"provider": "scope-recall", "max_items": 42}


@pytest.mark.parametrize(
    "text, message",
    [
        (
            "defaults: &defaults\n  provider: legacy-memory\nmemory: *defaults\n",
            "anchors and aliases",
        ),
        (
            "memory:\n  provider: first\nmemory:\n  provider: second\n",
            "duplicate YAML mapping key",
        ),
        (
            "memory:\n  provider: first\n---\nmemory:\n  provider: second\n",
            "document YAML",
        ),
        ("memory: {provider: [broken}\n", "malformed YAML"),
    ],
)
def test_provider_yaml_rejects_ambiguous_or_lossy_documents(text, message):
    import scope_recall.installer as installer

    with pytest.raises(installer.InstallError, match=message):
        installer._set_memory_provider_yaml_text(text)


def test_atomic_config_replace_keeps_original_on_replace_failure(tmp_path, monkeypatch):
    import scope_recall.installer as installer
    import scope_recall.installer_yaml as installer_yaml

    config_path = tmp_path / "config.yaml"
    original = "memory:\n  provider: legacy-memory\n  max_items: 42\n"
    config_path.write_text(original, encoding="utf-8")

    def fail_replace(_source, _destination):
        raise OSError("injected replace interruption")

    monkeypatch.setattr(installer_yaml.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected replace interruption"):
        installer._write_memory_provider_config(tmp_path)

    assert config_path.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob(".config.yaml.scope-recall.*.tmp"))


def test_atomic_config_replace_rejects_concurrent_content_change(tmp_path):
    from scope_recall.installer_yaml import InstallerYamlError, atomic_replace_text

    config_path = tmp_path / "config.yaml"
    before = b"memory:\n  provider: legacy\n"
    concurrent = b"memory:\n  provider: another-writer\n"
    config_path.write_bytes(before)
    config_path.write_bytes(concurrent)

    with pytest.raises(InstallerYamlError, match="changed concurrently"):
        atomic_replace_text(
            config_path,
            "memory:\n  provider: scope-recall\n",
            expected_before=before,
        )

    assert config_path.read_bytes() == concurrent


def test_atomic_replace_treats_unsupported_parent_fsync_as_success_after_replace(
    tmp_path,
    monkeypatch,
):
    import scope_recall.installer_yaml as installer_yaml

    config_path = tmp_path / "config.yaml"
    before = "memory:\n  provider: legacy-memory\n"
    after = "memory:\n  provider: scope-recall\n"
    config_path.write_text(before, encoding="utf-8")
    real_open = installer_yaml.os.open

    def windows_like_open(path, flags, *args, **kwargs):
        if Path(path) == tmp_path and not (int(flags) & int(os.O_CREAT)):
            raise PermissionError(13, "directory fsync unsupported", str(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(installer_yaml.os, "open", windows_like_open)

    replaced = installer_yaml.atomic_replace_text(
        config_path,
        after,
        expected_before=before,
    )

    assert replaced == config_path
    assert config_path.read_text(encoding="utf-8") == after
    assert not list(tmp_path.glob(".config.yaml.scope-recall.*.tmp"))


def test_atomic_config_replace_preserves_symlink_identity_and_target_mode(tmp_path):
    import scope_recall.installer as installer

    home = tmp_path / "home"
    home.mkdir()
    external = tmp_path / "external.yaml"
    external.write_text(
        "memory: {provider: legacy-memory, max_items: 42}\n",
        encoding="utf-8",
    )
    external.chmod(0o640)
    config_path = home / "config.yaml"
    config_path.symlink_to("../external.yaml")

    receipt = installer._write_memory_provider_config(home)

    assert receipt["config_updated"] is True
    assert config_path.is_symlink()
    assert os.readlink(config_path) == "../external.yaml"
    if os.name == "nt":
        assert external.is_file()
    else:
        assert external.stat().st_mode & 0o777 == 0o640
    assert yaml.safe_load(external.read_text(encoding="utf-8"))["memory"] == {
        "provider": "scope-recall",
        "max_items": 42,
    }


def test_existing_truth_activation_requires_explicit_maintenance_confirmation(tmp_path):
    import scope_recall.installer as installer

    plugin_dir = tmp_path / "plugins" / PLUGIN_NAME
    _write_installed_plugin(plugin_dir, version="1.7.2", marker="stable-old")
    config_bytes, storage_config_bytes = _write_installer_state(tmp_path)

    with pytest.raises(installer.InstallError, match="maintenance"):
        installer.install(hermes_home=tmp_path, activate=True)

    assert "version: 1.7.2" in (plugin_dir / "plugin.yaml").read_text(encoding="utf-8")
    assert "stable-old" in (plugin_dir / "provider.py").read_text(encoding="utf-8")
    _assert_installer_state_restored(
        tmp_path,
        config_bytes=config_bytes,
        storage_config_bytes=storage_config_bytes,
    )


def test_maintenance_activation_refuses_an_active_sqlite_writer_before_replacement(
    tmp_path,
):
    import scope_recall.installer as installer

    plugin_dir = tmp_path / "plugins" / PLUGIN_NAME
    _write_installed_plugin(plugin_dir, version="1.7.2", marker="stable-old")
    _write_installer_state(tmp_path)
    db_path = tmp_path / "scope-recall" / "memory.sqlite3"
    writer = sqlite3.connect(db_path, timeout=0.1, isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        with pytest.raises(installer.InstallError, match="active writer"):
            installer.install(
                hermes_home=tmp_path,
                activate=True,
                maintenance_mode=True,
            )
    finally:
        writer.execute("ROLLBACK")
        writer.close()

    assert "version: 1.7.2" in (plugin_dir / "plugin.yaml").read_text(
        encoding="utf-8"
    )


def test_unconfirmed_activation_compensation_never_overwrites_post_snapshot_truth(tmp_path):
    from scope_recall.activation_transaction import (
        capture_activation_state,
        compensate_activation_failure,
    )
    from scope_recall.maintenance_lease import ACTIVATION_AUTHORIZATION_FUNCTION

    plugin_dir = tmp_path / "plugins" / PLUGIN_NAME
    _write_installed_plugin(plugin_dir, version="1.7.2", marker="stable-old")
    _write_installer_state(tmp_path)
    snapshot = capture_activation_state(tmp_path)
    db_path = tmp_path / "scope-recall" / "memory.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.create_function(ACTIVATION_AUTHORIZATION_FUNCTION, 0, lambda: 1)
        conn.execute("INSERT INTO installer_sentinel(value) VALUES ('after-snapshot')")
        conn.commit()

    receipt = compensate_activation_failure(
        snapshot,
        plugin_dir=plugin_dir,
        previous_plugin_existed=True,
        previous_version="1.7.2",
        plugin_backup_path="",
        plugin_replaced=False,
    )

    assert receipt["automatic_rollback"] is False
    assert receipt["status"] == "rollback_failed"
    with sqlite3.connect(db_path) as conn:
        values = [str(row[0]) for row in conn.execute("SELECT value FROM installer_sentinel")]
    assert values == ["before", "after-snapshot"]


def test_confirmed_activation_compensation_refuses_post_snapshot_truth_drift(
    tmp_path,
):
    from scope_recall.activation_transaction import (
        capture_activation_state,
        compensate_activation_failure,
    )
    from scope_recall.maintenance_lease import ACTIVATION_AUTHORIZATION_FUNCTION

    plugin_dir = tmp_path / "plugins" / PLUGIN_NAME
    plugin_backup = tmp_path / "plugin-backup"
    _write_installed_plugin(plugin_dir, version="1.7.2", marker="stable-old")
    _write_installed_plugin(plugin_backup, version="1.7.2", marker="stable-old")
    _write_installer_state(tmp_path)
    snapshot = capture_activation_state(tmp_path, writer_quiesced=True)
    _write_installed_plugin(plugin_dir, version="1.8.0", marker="candidate-new")
    config_path = tmp_path / "config.yaml"
    storage_config_path = tmp_path / "scope-recall" / "config.json"
    candidate_config = b"memory:\n  provider: scope-recall\ncandidate: true\n"
    candidate_storage_config = b'{"candidate": true}\n'
    candidate_vector = b"candidate-vector-generation\n"
    vector_path = tmp_path / "scope-recall" / "vector.sqlite3"
    config_path.write_bytes(candidate_config)
    storage_config_path.write_bytes(candidate_storage_config)
    vector_path.write_bytes(candidate_vector)
    db_path = tmp_path / "scope-recall" / "memory.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.create_function(ACTIVATION_AUTHORIZATION_FUNCTION, 0, lambda: 1)
        conn.execute("INSERT INTO installer_sentinel(value) VALUES ('after-snapshot')")
        conn.commit()

    receipt = compensate_activation_failure(
        snapshot,
        plugin_dir=plugin_dir,
        previous_plugin_existed=True,
        previous_version="1.7.2",
        plugin_backup_path=str(plugin_backup),
        plugin_replaced=True,
    )

    assert receipt["automatic_rollback"] is False
    assert receipt["status"] == "rollback_failed"
    assert receipt["compensation_started"] is False
    assert receipt["plugin"]["restored"] is False
    assert receipt["config"]["restored"] is False
    assert receipt["storage_config"]["restored"] is False
    assert receipt["sqlite"]["restored"] is False
    assert receipt["sqlite"]["manual_recovery_required"] is True
    assert any("changed after activation snapshot" in item for item in receipt["failures"])
    assert "version: 1.8.0" in (plugin_dir / "plugin.yaml").read_text()
    assert config_path.read_bytes() == candidate_config
    assert storage_config_path.read_bytes() == candidate_storage_config
    assert vector_path.read_bytes() == candidate_vector
    assert all(
        item["status"] == "compensation_skipped_preflight"
        and item["discarded"] is False
        for item in receipt["vector_companions"]
    )
    with sqlite3.connect(db_path) as conn:
        values = [
            str(row[0])
            for row in conn.execute(
                "SELECT value FROM installer_sentinel ORDER BY rowid"
            )
        ]
    assert values == ["before", "after-snapshot"]


def test_activation_guard_precedes_online_backup_and_backup_is_guard_free(
    tmp_path,
    monkeypatch,
):
    import scope_recall.activation_transaction as activation
    from scope_recall.maintenance_lease import ACTIVATION_GUARD_TRIGGER_PREFIX

    plugin_dir = tmp_path / "plugins" / PLUGIN_NAME
    _write_installed_plugin(plugin_dir, version="1.7.2", marker="stable-old")
    _write_installer_state(tmp_path)
    db_path = tmp_path / "scope-recall" / "memory.sqlite3"
    original_backup = activation._sqlite_online_backup
    observed = {"raw_writer_blocked": False}

    def probing_backup(source: Path, destination: Path) -> None:
        with closing(sqlite3.connect(source)) as raw_writer:
            with pytest.raises(sqlite3.OperationalError, match="no such function"):
                raw_writer.execute(
                    "INSERT INTO installer_sentinel(value) VALUES ('during-backup')"
                )
            raw_writer.rollback()
        observed["raw_writer_blocked"] = True
        original_backup(source, destination)

    monkeypatch.setattr(activation, "_sqlite_online_backup", probing_backup)
    snapshot = activation.capture_activation_state(tmp_path, writer_quiesced=True)
    assert observed["raw_writer_blocked"] is True
    assert snapshot["sqlite"]["guards_installed"] is True
    assert snapshot["sqlite"]["backup_guards_removed"] is True

    backup_path = Path(snapshot["sqlite"]["backup_path"])
    with sqlite3.connect(backup_path) as backup:
        backup_guard_count = int(
            backup.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE ?",
                (f"{ACTIVATION_GUARD_TRIGGER_PREFIX}%",),
            ).fetchone()[0]
        )
        backup_values = [
            str(row[0])
            for row in backup.execute("SELECT value FROM installer_sentinel")
        ]
    assert backup_guard_count == 0
    assert backup_values == ["before"]

    receipt = activation.compensate_activation_failure(
        snapshot,
        plugin_dir=plugin_dir,
        previous_plugin_existed=True,
        previous_version="1.7.2",
        plugin_backup_path="",
        plugin_replaced=False,
    )
    assert receipt["status"] == "rolled_back", receipt
    assert receipt["automatic_rollback"] is True
    assert receipt["sqlite"]["backup_guards_removed"] is True
    with sqlite3.connect(db_path) as check:
        values = [
            str(row[0])
            for row in check.execute("SELECT value FROM installer_sentinel")
        ]
    assert values == ["before"]


def test_activation_epoch_registrar_cannot_mask_raw_post_snapshot_write(tmp_path):
    from scope_recall.activation_transaction import (
        capture_activation_state,
        compensate_activation_failure,
        refresh_activation_sqlite_epoch,
    )

    plugin_dir = tmp_path / "plugins" / PLUGIN_NAME
    _write_installed_plugin(plugin_dir, version="1.7.2", marker="stable-old")
    _write_installer_state(tmp_path)
    snapshot = capture_activation_state(tmp_path, writer_quiesced=True)
    db_path = tmp_path / "scope-recall" / "memory.sqlite3"

    with closing(sqlite3.connect(db_path)) as external_writer:
        with pytest.raises(sqlite3.OperationalError, match="no such function"):
            external_writer.execute(
                "INSERT INTO installer_sentinel(value) VALUES ('external')"
            )
        external_writer.rollback()

    refresh_activation_sqlite_epoch(snapshot)
    receipt = compensate_activation_failure(
        snapshot,
        plugin_dir=plugin_dir,
        previous_plugin_existed=True,
        previous_version="1.7.2",
        plugin_backup_path="",
        plugin_replaced=False,
    )
    assert receipt["status"] == "rolled_back", receipt
    assert receipt["automatic_rollback"] is True
    assert receipt["sqlite"]["guards_removed"] is True
    with sqlite3.connect(db_path) as check:
        values = [
            str(row[0])
            for row in check.execute("SELECT value FROM installer_sentinel")
        ]
    assert values == ["before"]


@pytest.mark.parametrize(
    "failure_stage",
    ["config_write", "schema_migration", "provider_load", "runtime_verify"],
)
def test_activation_guard_blocks_unregistered_writer_at_each_failure_stage(
    tmp_path,
    monkeypatch,
    failure_stage,
):
    import scope_recall.installer as installer

    plugin_dir = tmp_path / "plugins" / PLUGIN_NAME
    _write_installed_plugin(plugin_dir, version="1.7.2", marker="previous plugin")
    _write_installer_state(tmp_path)

    def injected_activation(
        home: Path,
        installed_plugin: Path,
        activation_snapshot: dict[str, object],
    ):
        assert installed_plugin == plugin_dir
        (home / "config.yaml").write_text(
            "memory:\n  provider: scope-recall\n",
            encoding="utf-8",
        )
        db_path = home / "scope-recall" / "memory.sqlite3"
        with closing(sqlite3.connect(db_path)) as external_writer:
            with pytest.raises(sqlite3.OperationalError, match="no such function"):
                external_writer.execute(
                    "INSERT INTO installer_sentinel(value) VALUES (?)",
                    (f"external-{failure_stage}",),
                )
            external_writer.rollback()
        if failure_stage == "runtime_verify":
            return {
                "activation_requested": True,
                "activated": False,
                "runtime_verify": {
                    "ok": False,
                    "failures": ["injected runtime verify failure"],
                },
            }
        raise RuntimeError(f"injected {failure_stage} failure")

    monkeypatch.setattr(installer, "_activation_payload", injected_activation)
    result = installer.install(
        hermes_home=tmp_path,
        activate=True,
        maintenance_mode=True,
    )

    transaction = result["activation_transaction"]
    assert transaction["status"] == "rolled_back", transaction
    assert transaction["automatic_rollback"] is True
    assert transaction["sqlite"]["restored"] is True
    assert transaction["sqlite"]["guards_removed"] is True
    assert transaction["maintenance_lease"]["released"] is True
    with sqlite3.connect(tmp_path / "scope-recall" / "memory.sqlite3") as check:
        values = [str(row[0]) for row in check.execute("SELECT value FROM installer_sentinel")]
    assert values == ["before"]


@pytest.mark.parametrize(
    "failure_stage",
    ["config_write", "schema_migration", "provider_load", "runtime_verify"],
)
def test_install_activate_failure_compensates_upgrade_state(
    tmp_path,
    monkeypatch,
    failure_stage,
):
    import scope_recall.installer as installer
    from scope_recall.maintenance_lease import install_activation_lease_authorizer

    plugin_dir = tmp_path / "plugins" / PLUGIN_NAME
    _write_installed_plugin(plugin_dir, version="1.7.2", marker="previous plugin")
    config_bytes, storage_config_bytes = _write_installer_state(tmp_path)

    def injected_activation(
        home: Path,
        installed_plugin: Path,
        activation_snapshot: dict[str, object],
    ):
        assert installed_plugin == plugin_dir
        (home / "config.yaml").write_text(
            "memory:\n  provider: scope-recall\n",
            encoding="utf-8",
        )
        if failure_stage != "config_write":
            (home / "scope-recall" / "config.json").write_text(
                '{"marker":"after"}\n',
                encoding="utf-8",
            )
            db_path = home / "scope-recall" / "memory.sqlite3"
            lease_payload = activation_snapshot["maintenance_lease"]
            assert isinstance(lease_payload, dict)
            conn = sqlite3.connect(db_path)
            install_activation_lease_authorizer(
                conn,
                db_path,
                lease_token=str(lease_payload["token"]),
            )
            try:
                conn.execute("UPDATE installer_sentinel SET value='after'")
                conn.execute("CREATE TABLE activation_partial(value TEXT)")
                conn.execute("PRAGMA user_version=10800")
                conn.commit()
            finally:
                conn.close()
            installer._register_activation_sqlite_epoch(activation_snapshot)
        if failure_stage == "runtime_verify":
            return {
                "activation_requested": True,
                "activated": False,
                "runtime_verify": {
                    "ok": False,
                    "failures": ["injected runtime verify failure"],
                },
            }
        raise RuntimeError(f"injected {failure_stage} failure")

    monkeypatch.setattr(installer, "_activation_payload", injected_activation)

    result = installer.install(
        hermes_home=tmp_path,
        activate=True,
        maintenance_mode=True,
    )

    assert result["ok"] is False
    assert result["installed"] is False
    assert result["activated"] is False
    assert result["mode"] == "activation-failed-rolled-back"
    transaction = result["activation_transaction"]
    assert transaction["status"] == "rolled_back"
    assert transaction["automatic_rollback"] is True
    assert transaction["failures"] == []
    assert transaction["plugin"]["restored"] is True
    assert transaction["config"]["restored"] is True
    assert transaction["storage_config"]["restored"] is True
    assert transaction["sqlite"]["restored"] is True
    assert Path(transaction["sqlite"]["backup_path"]).is_file()
    assert "version: 1.7.2" in (plugin_dir / "plugin.yaml").read_text(encoding="utf-8")
    assert "previous plugin" in (plugin_dir / "__init__.py").read_text(encoding="utf-8")
    _assert_installer_state_restored(
        tmp_path,
        config_bytes=config_bytes,
        storage_config_bytes=storage_config_bytes,
    )


def test_postdeploy_doctor_failure_rolls_back_before_committed_receipt(
    tmp_path,
    monkeypatch,
):
    import scope_recall.installer as installer

    plugin_dir = tmp_path / "plugins" / PLUGIN_NAME
    _write_installed_plugin(plugin_dir, version="1.7.2", marker="previous plugin")
    config_bytes, storage_config_bytes = _write_installer_state(tmp_path)

    def injected_activation(
        home: Path,
        installed_plugin: Path,
        _activation_snapshot: dict[str, object],
    ) -> dict[str, object]:
        assert installed_plugin == plugin_dir
        (home / "config.yaml").write_text(
            "memory:\n  provider: scope-recall\n",
            encoding="utf-8",
        )
        return {
            "activation_requested": True,
            "activated": True,
            "runtime_verify": {"ok": True, "failures": []},
            "postdeploy_doctor": {
                "ok": False,
                "failures": ["injected vector generation source is stale"],
                "checks": {"vector_companion": {"ok": False}},
            },
        }

    monkeypatch.setattr(installer, "_activation_payload", injected_activation)

    result = installer.install(
        hermes_home=tmp_path,
        activate=True,
        maintenance_mode=True,
    )

    assert result["ok"] is False
    assert result["installed"] is False
    assert result["activated"] is False
    assert result["activation_transaction"]["status"] == "rolled_back"
    assert result["activation_transaction"]["automatic_rollback"] is True
    assert "postdeploy doctor" in result["activation_error"]["message"]
    assert "version: 1.7.2" in (plugin_dir / "plugin.yaml").read_text(encoding="utf-8")
    assert "previous plugin" in (plugin_dir / "__init__.py").read_text(encoding="utf-8")
    _assert_installer_state_restored(
        tmp_path,
        config_bytes=config_bytes,
        storage_config_bytes=storage_config_bytes,
    )


def test_committed_receipt_oserror_rolls_back_plugin_config_and_sqlite(
    tmp_path,
    monkeypatch,
):
    import scope_recall.installer as installer
    from scope_recall.maintenance_lease import install_activation_lease_authorizer

    plugin_dir = tmp_path / "plugins" / PLUGIN_NAME
    _write_installed_plugin(plugin_dir, version="1.7.2", marker="previous plugin")
    config_bytes, storage_config_bytes = _write_installer_state(tmp_path)
    db_path = tmp_path / "scope-recall" / "memory.sqlite3"

    def injected_activation(
        home: Path,
        installed_plugin: Path,
        activation_snapshot: dict[str, object],
    ) -> dict[str, object]:
        assert installed_plugin == plugin_dir
        (home / "config.yaml").write_text(
            "memory:\n  provider: scope-recall\n",
            encoding="utf-8",
        )
        (home / "scope-recall" / "config.json").write_text(
            '{"marker":"after"}\n',
            encoding="utf-8",
        )
        lease_payload = activation_snapshot["maintenance_lease"]
        assert isinstance(lease_payload, dict)
        conn = sqlite3.connect(db_path)
        install_activation_lease_authorizer(
            conn,
            db_path,
            lease_token=str(lease_payload["token"]),
        )
        try:
            conn.execute("UPDATE installer_sentinel SET value='after'")
            conn.execute("CREATE TABLE activation_partial(value TEXT)")
            conn.execute("PRAGMA user_version=10800")
            conn.commit()
        finally:
            conn.close()
        installer._register_activation_sqlite_epoch(activation_snapshot)
        return {
            "activation_requested": True,
            "activated": True,
            "runtime_verify": {"ok": True, "failures": []},
            "postdeploy_doctor": {
                "ok": True,
                "failures": [],
                "failed_checks": [],
            },
        }

    def fail_committed_receipt(*_args, **_kwargs):
        raise OSError("injected committed receipt I/O failure")

    monkeypatch.setattr(installer, "_activation_payload", injected_activation)
    monkeypatch.setattr(
        installer,
        "committed_activation_receipt",
        fail_committed_receipt,
    )

    result = installer.install(
        hermes_home=tmp_path,
        activate=True,
        maintenance_mode=True,
    )

    assert result["ok"] is False
    assert result["installed"] is False
    assert result["activated"] is False
    assert result["mode"] == "activation-failed-rolled-back"
    assert result["activation_error"] == {
        "type": "OSError",
        "message": "injected committed receipt I/O failure",
    }
    transaction = result["activation_transaction"]
    assert transaction["status"] == "rolled_back"
    assert transaction["automatic_rollback"] is True
    assert transaction["plugin"]["restored"] is True
    assert transaction["config"]["restored"] is True
    assert transaction["storage_config"]["restored"] is True
    assert transaction["sqlite"]["restored"] is True
    assert "version: 1.7.2" in (plugin_dir / "plugin.yaml").read_text(
        encoding="utf-8"
    )
    assert "previous plugin" in (plugin_dir / "__init__.py").read_text(
        encoding="utf-8"
    )
    _assert_installer_state_restored(
        tmp_path,
        config_bytes=config_bytes,
        storage_config_bytes=storage_config_bytes,
    )


def test_noncommitted_receipt_status_is_compensated_fail_closed(
    tmp_path,
    monkeypatch,
):
    import scope_recall.installer as installer

    plugin_dir = tmp_path / "plugins" / PLUGIN_NAME
    _write_installed_plugin(plugin_dir, version="1.7.2", marker="previous plugin")
    config_bytes, storage_config_bytes = _write_installer_state(tmp_path)

    def injected_activation(
        home: Path,
        installed_plugin: Path,
        _activation_snapshot: dict[str, object],
    ) -> dict[str, object]:
        assert installed_plugin == plugin_dir
        (home / "config.yaml").write_text(
            "memory:\n  provider: scope-recall\n",
            encoding="utf-8",
        )
        return {
            "activation_requested": True,
            "activated": True,
            "runtime_verify": {"ok": True, "failures": []},
            "postdeploy_doctor": {
                "ok": True,
                "failures": [],
                "failed_checks": [],
            },
        }

    def incomplete_committed_receipt(*_args, **_kwargs):
        return {
            "status": "commit_cleanup_failed",
            "failures": ["injected lease cleanup failure"],
            "restore_commands": [],
        }

    monkeypatch.setattr(installer, "_activation_payload", injected_activation)
    monkeypatch.setattr(
        installer,
        "committed_activation_receipt",
        incomplete_committed_receipt,
    )

    result = installer.install(
        hermes_home=tmp_path,
        activate=True,
        maintenance_mode=True,
    )

    assert result["ok"] is False
    assert result["installed"] is False
    assert result["activated"] is False
    assert result["mode"] == "activation-failed-rolled-back"
    assert result["activation_error"]["type"] == "InstallError"
    assert "commit_cleanup_failed" in result["activation_error"]["message"]
    transaction = result["activation_transaction"]
    assert transaction["status"] == "rolled_back"
    assert transaction["automatic_rollback"] is True
    assert "version: 1.7.2" in (plugin_dir / "plugin.yaml").read_text(
        encoding="utf-8"
    )
    _assert_installer_state_restored(
        tmp_path,
        config_bytes=config_bytes,
        storage_config_bytes=storage_config_bytes,
    )


@pytest.mark.parametrize("backend", ["lancedb", "sqlite-bruteforce"])
def test_activation_failure_discards_changed_vector_companion_with_rebuild_receipt(
    tmp_path,
    monkeypatch,
    backend,
):
    import scope_recall.installer as installer

    plugin_dir = tmp_path / "plugins" / PLUGIN_NAME
    _write_installed_plugin(plugin_dir, version="1.7.2", marker="stable-old")
    _write_installer_state(tmp_path)
    storage = tmp_path / "scope-recall"
    if backend == "lancedb":
        companion = storage / "lancedb"
        companion.mkdir()
        (companion / "generation.bin").write_bytes(b"before")
    else:
        companion = storage / "vector.sqlite3"
        companion.write_bytes(b"before")

    from scope_recall.sql_store import ensure_schema
    from scope_recall.vector_generation import GenerationIdentity, bootstrap_legacy_generation

    with closing(sqlite3.connect(storage / "memory.sqlite3")) as conn:
        conn.row_factory = sqlite3.Row
        ensure_schema(conn)
        identity = GenerationIdentity(
            backend=backend,
            provider="openai-compatible" if backend == "lancedb" else "local-hash",
            model="gemini-embedding-001" if backend == "lancedb" else "hash-v1",
            dimensions=3072 if backend == "lancedb" else 256,
            metric="cosine",
            table_name="memories",
        )
        bootstrap_legacy_generation(
            conn,
            identity=identity,
            storage_path=".",
            row_count=0,
            unique_id_count=0,
        )
        conn.commit()

    def injected_activation(
        home: Path,
        _installed_plugin: Path,
        _activation_snapshot: dict[str, object],
    ):
        if backend == "lancedb":
            (home / "scope-recall" / "lancedb" / "generation.bin").write_bytes(
                b"after"
            )
        else:
            (home / "scope-recall" / "vector.sqlite3").write_bytes(b"after")
        raise RuntimeError("injected vector generation failure")

    monkeypatch.setattr(installer, "_activation_payload", injected_activation)
    result = installer.install(
        tmp_path,
        activate=True,
        maintenance_mode=True,
    )

    transaction = result["activation_transaction"]
    receipt = next(
        item for item in transaction["vector_companions"] if item["name"] == backend
    )
    assert transaction["automatic_rollback"] is True
    assert receipt["status"] == "discarded_rebuild_required"
    assert receipt["rebuild_required"] is True
    assert not companion.exists()
    assert any("vector repair apply" in command for command in transaction["restore_commands"])


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing violation contract")
def test_locked_vector_rollback_fails_closed_and_physically_retains_lease(
    tmp_path, monkeypatch
):
    import scope_recall.installer as installer

    plugin_dir = tmp_path / "plugins" / PLUGIN_NAME
    _write_installed_plugin(plugin_dir, version="1.7.2", marker="stable-old")
    _write_installer_state(tmp_path)
    storage = tmp_path / "scope-recall"
    companion = storage / "vector.sqlite3"

    from scope_recall.sql_store import ensure_schema
    from scope_recall.vector_generation import GenerationIdentity, bootstrap_legacy_generation

    with closing(sqlite3.connect(storage / "memory.sqlite3")) as conn:
        conn.row_factory = sqlite3.Row
        ensure_schema(conn)
        bootstrap_legacy_generation(
            conn,
            identity=GenerationIdentity(
                backend="sqlite-bruteforce",
                provider="local-hash",
                model="hash-v1",
                dimensions=256,
                metric="cosine",
                table_name="memories",
            ),
            storage_path=".",
            row_count=0,
            unique_id_count=0,
        )
        conn.commit()

    vector_lock = sqlite3.connect(companion)
    vector_lock.execute("CREATE TABLE locked_marker(value TEXT NOT NULL)")
    vector_lock.commit()

    def injected_activation(
        _home: Path,
        _installed_plugin: Path,
        _activation_snapshot: dict[str, object],
    ):
        vector_lock.execute("INSERT INTO locked_marker(value) VALUES ('changed')")
        vector_lock.commit()
        raise RuntimeError("injected failure with externally locked vector")

    monkeypatch.setattr(installer, "_activation_payload", injected_activation)
    try:
        result = installer.install(
            tmp_path,
            activate=True,
            maintenance_mode=True,
        )
        transaction = result["activation_transaction"]
        lease = transaction["maintenance_lease"]

        assert transaction["status"] == "rollback_failed"
        assert transaction["automatic_rollback"] is False
        assert lease["retained"] is True
        assert lease["present"] is True
        assert lease["token_matches"] is True
        assert Path(lease["path"]).is_file()
        assert any(
            "companion discard failed" in failure and "WinError 32" in failure
            for failure in transaction["failures"]
        )
        assert any(
            command.startswith("powershell.exe ")
            for command in transaction["restore_commands"]
        )
    finally:
        vector_lock.close()


def test_install_activate_failure_compensates_fresh_install(tmp_path, monkeypatch):
    import scope_recall.installer as installer

    def injected_activation(
        home: Path,
        installed_plugin: Path,
        activation_snapshot: dict[str, object],
    ):
        assert installed_plugin == home / "plugins" / PLUGIN_NAME
        (home / "config.yaml").write_text(
            "memory:\n  provider: scope-recall\n",
            encoding="utf-8",
        )
        storage = home / "scope-recall"
        storage.mkdir(parents=True, exist_ok=True)
        (storage / "config.json").write_text('{"new": true}\n', encoding="utf-8")
        with sqlite3.connect(storage / "memory.sqlite3") as conn:
            conn.execute("CREATE TABLE partial_activation(value TEXT NOT NULL)")
            conn.execute("INSERT INTO partial_activation(value) VALUES ('new')")
            conn.commit()
        installer._register_activation_sqlite_epoch(activation_snapshot)
        raise RuntimeError("fresh activation failed")

    monkeypatch.setattr(installer, "_activation_payload", injected_activation)
    result = installer.install(tmp_path, activate=True)

    transaction = result["activation_transaction"]
    assert result["ok"] is False
    assert transaction["status"] == "rolled_back"
    assert transaction["automatic_rollback"] is True
    assert not (tmp_path / "plugins" / PLUGIN_NAME).exists()
    assert not (tmp_path / "config.yaml").exists()
    assert not (tmp_path / "scope-recall").exists()


@pytest.mark.parametrize("surface", ["hermes_config", "provider_config"])
def test_install_activate_failure_restores_symlink_target(
    tmp_path,
    monkeypatch,
    surface,
):
    import scope_recall.installer as installer

    home = tmp_path / "home"
    plugin_dir = home / "plugins" / PLUGIN_NAME
    _write_installed_plugin(plugin_dir, version="1.7.2", marker="stable-old")
    home.mkdir(parents=True, exist_ok=True)
    _write_installer_state(home)
    storage = home / "scope-recall"

    if surface == "hermes_config":
        external = tmp_path / "external-config.yaml"
        original = b"model:\n  provider: openrouter\n"
        external.write_bytes(original)
        link_path = home / "config.yaml"
        link_target = "../external-config.yaml"
        link_path.unlink()
        link_path.symlink_to(link_target)
        (storage / "config.json").write_text('{"auto_capture": false}\n', encoding="utf-8")
    else:
        (home / "config.yaml").write_text(
            "memory:\n  provider: scope-recall\n",
            encoding="utf-8",
        )
        external = tmp_path / "external-provider-config.json"
        original = b'{"auto_capture": false}\n'
        external.write_bytes(original)
        link_path = storage / "config.json"
        link_target = "../../external-provider-config.json"
        link_path.unlink()
        link_path.symlink_to(link_target)

    def injected_activation(
        activation_home: Path,
        _installed_plugin: Path,
        _activation_snapshot: dict[str, object],
    ):
        if surface == "hermes_config":
            installer._write_memory_provider_config(activation_home)
        else:
            (activation_home / "scope-recall" / "config.json").write_text(
                '{"partial": true}\n',
                encoding="utf-8",
            )
        raise RuntimeError(f"{surface} symlink failure")

    monkeypatch.setattr(installer, "_activation_payload", injected_activation)
    result = installer.install(home, activate=True, maintenance_mode=True)

    transaction = result["activation_transaction"]
    receipt_key = "config" if surface == "hermes_config" else "storage_config"
    receipt = transaction[receipt_key]
    assert result["ok"] is False
    assert transaction["status"] == "rolled_back"
    assert transaction["automatic_rollback"] is True
    assert link_path.is_symlink()
    assert os.readlink(link_path) == link_target
    assert external.read_bytes() == original
    assert receipt["restored"] is True
    assert receipt["target_restored"] is True
    assert "stable-old" in (plugin_dir / "provider.py").read_text(encoding="utf-8")
    assert "version: 1.7.2" in (plugin_dir / "plugin.yaml").read_text(
        encoding="utf-8"
    )


def test_activation_snapshot_failure_precedes_plugin_replacement(tmp_path, monkeypatch):
    import scope_recall.activation_transaction as activation_transaction
    import scope_recall.installer as installer

    plugin_dir = tmp_path / "plugins" / PLUGIN_NAME
    _write_installed_plugin(plugin_dir, version="1.7.2", marker="previous plugin")
    config_bytes, storage_config_bytes = _write_installer_state(tmp_path)

    def fail_backup(source_path: Path, backup_path: Path) -> None:  # noqa: ARG001
        raise activation_transaction.ActivationSnapshotError("injected backup failure")

    monkeypatch.setattr(activation_transaction, "_sqlite_online_backup", fail_backup)

    with pytest.raises(installer.InstallError, match="cannot safely snapshot"):
        installer.install(
            hermes_home=tmp_path,
            activate=True,
            maintenance_mode=True,
        )

    assert "version: 1.7.2" in (plugin_dir / "plugin.yaml").read_text(encoding="utf-8")
    assert "previous plugin" in (plugin_dir / "__init__.py").read_text(encoding="utf-8")
    _assert_installer_state_restored(
        tmp_path,
        config_bytes=config_bytes,
        storage_config_bytes=storage_config_bytes,
    )


def test_pre_activation_copy_failure_releases_maintenance_lease(
    tmp_path,
    monkeypatch,
):
    """Failures after snapshot but before activation handoff must compensate."""

    import sqlite3

    import scope_recall.installer as installer
    from scope_recall.maintenance_lease import (
        ACTIVATION_GUARD_TRIGGER_PREFIX,
        activation_lease_path,
    )

    plugin_dir = tmp_path / "plugins" / PLUGIN_NAME
    _write_installed_plugin(plugin_dir, version="1.7.2", marker="previous plugin")
    _write_installer_state(tmp_path)
    db_path = tmp_path / "scope-recall" / "memory.sqlite3"

    def fail_copy(_source: Path, _target: Path) -> None:
        raise RuntimeError("injected pre-activation copy failure")

    monkeypatch.setattr(installer, "_copy_tree", fail_copy)

    with pytest.raises(RuntimeError, match="pre-activation copy failure"):
        installer.install(
            hermes_home=tmp_path,
            activate=True,
            maintenance_mode=True,
        )

    assert not activation_lease_path(db_path).exists()
    conn = sqlite3.connect(db_path)
    guard_count = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE ?",
        (f"{ACTIVATION_GUARD_TRIGGER_PREFIX}%",),
    ).fetchone()[0]
    conn.close()
    assert guard_count == 0


def test_installer_upgrade_backs_up_existing_plugin_and_reports_versions(tmp_path):
    import scope_recall.installer as installer

    plugin_dir = tmp_path / "plugins" / PLUGIN_NAME
    _write_installed_plugin(plugin_dir, version="0.9.0", marker="previous plugin")

    result = installer.install(hermes_home=tmp_path)

    assert result["ok"] is True
    assert result["installed"] is True
    assert result["previous_plugin_existed"] is True
    assert result["previous_version"] == "0.9.0"
    assert result["manifest_version"] == "1.9.3"
    assert result["new_version"] == "1.9.3"
    backup_path = Path(result["backup_path"])
    assert backup_path.is_dir()
    assert tmp_path in backup_path.parents
    assert "version: 0.9.0" in (backup_path / "plugin.yaml").read_text(encoding="utf-8")
    assert "previous plugin" in (backup_path / "__init__.py").read_text(encoding="utf-8")
    assert "version: 1.9.3" in (plugin_dir / "plugin.yaml").read_text(encoding="utf-8")
    assert any("restart" in step.lower() for step in result["next_steps"])
    assert any("doctor" in step for step in result["next_steps"])
    assert result["rollback_command"].endswith(str(backup_path))


def test_installer_rollback_restores_backup_and_backs_up_current_plugin(tmp_path):
    import scope_recall.installer as installer

    plugin_dir = tmp_path / "plugins" / PLUGIN_NAME
    _write_installed_plugin(plugin_dir, version="0.9.0", marker="previous plugin")
    upgrade = installer.install(hermes_home=tmp_path)
    assert "version: 1.9.3" in (plugin_dir / "plugin.yaml").read_text(encoding="utf-8")

    rollback = installer.rollback(hermes_home=tmp_path, backup_dir=upgrade["backup_path"])

    assert rollback["ok"] is True
    assert rollback["dry_run"] is False
    assert rollback["restored"] is True
    assert rollback["restored_version"] == "0.9.0"
    assert rollback["replaced_version"] == "1.9.3"
    current_backup = Path(rollback["current_backup_path"])
    assert current_backup.is_dir()
    assert "version: 1.9.3" in (current_backup / "plugin.yaml").read_text(encoding="utf-8")
    assert "version: 0.9.0" in (plugin_dir / "plugin.yaml").read_text(encoding="utf-8")
    assert "previous plugin" in (plugin_dir / "__init__.py").read_text(encoding="utf-8")


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length installer contract")
def test_installer_install_and_repeat_rollback_support_deep_home_paths(
    tmp_path,
    monkeypatch,
):
    import scope_recall.installer as installer
    from scope_recall.windows_filesystem import io_path, make_dirs, remove_path

    component_length = 178 - len(str(tmp_path)) - 1
    if component_length < 8 or component_length > 240:
        pytest.skip("temporary path cannot form the 178-character Hermes-home fixture")
    home = tmp_path / ("h" * component_length)
    plugin_dir = home / "plugins" / PLUGIN_NAME
    _write_installed_plugin(plugin_dir, version="0.9.0", marker="previous deep plugin")
    old_nested = plugin_dir / ("o" * 80)
    make_dirs(old_nested)
    Path(io_path(old_nested / "old.txt")).write_text("old", encoding="utf-8")

    fake_source = tmp_path / "candidate-source"
    _write_installed_plugin(fake_source, version="2.0.0", marker="new deep plugin")
    source_nested = fake_source / ("n" * 80)
    source_nested.mkdir()
    (source_nested / "new.txt").write_text("new", encoding="utf-8")
    monkeypatch.setattr(installer, "source_root", lambda: fake_source)

    try:
        installed = installer.install(hermes_home=home)
        assert installed["ok"] is True
        assert "\\\\?\\" not in installed["backup_path"]
        assert os.path.isfile(io_path(plugin_dir / ("n" * 80) / "new.txt"))

        first = installer.rollback(hermes_home=home, backup_dir=installed["backup_path"])
        assert first["ok"] is True
        assert "\\\\?\\" not in first["current_backup_path"]
        assert "version: 0.9.0" in (plugin_dir / "plugin.yaml").read_text(encoding="utf-8")
        assert os.path.isfile(io_path(plugin_dir / ("o" * 80) / "old.txt"))

        second = installer.rollback(hermes_home=home, backup_dir=installed["backup_path"])
        assert second["ok"] is True
        assert "version: 0.9.0" in (plugin_dir / "plugin.yaml").read_text(encoding="utf-8")
    finally:
        remove_path(home, missing_ok=True, ignore_errors=True)


def test_installer_rollback_refuses_bad_backup_without_mutating_current_plugin(tmp_path):
    import scope_recall.installer as installer

    plugin_dir = tmp_path / "plugins" / PLUGIN_NAME
    _write_installed_plugin(plugin_dir, version="1.8.2", marker="current plugin")
    bad_backup = tmp_path / "bad-backup" / PLUGIN_NAME
    bad_backup.mkdir(parents=True)
    (bad_backup / "plugin.yaml").write_text("name: other\nversion: 0.1.0\n", encoding="utf-8")

    with pytest.raises(installer.InstallError):
        installer.rollback(hermes_home=tmp_path, backup_dir=bad_backup)

    assert "version: 1.8.2" in (plugin_dir / "plugin.yaml").read_text(encoding="utf-8")
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
    assert "version: 1.9.3" in (plugin_dir / "plugin.yaml").read_text(encoding="utf-8")


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
    assert runtime["schema_migrations"]["missing_migrations"] == [
        "0001_baseline_v1_6_0",
        "0002_fact_claims_v1",
        "0003_relation_rebuild_queue_v1_8_0",
        "0004_operator_ledger_v1_8_0",
        "0005_relation_rebuild_lease_token_v1_8_0",
        "0006_relation_scope_receipt_v1_8_0",
        "0007_relation_frequency_index_v1_8_0",
        "0008_relation_rebuild_progress_v1_8_0",
        "0009_vector_reconciliation_watermark_v1_8_0",
        "0010_relation_rebuild_lease_expiry_budget_v1_8_0",
        "0011_relation_frequency_failure_queue_v1_8_0",
        "0012_lexical_shadow_index_v1_9_0",
    ]
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


def test_verify_runtime_reports_layered_diagnostics(tmp_path):
    from scope_recall import installer

    installer.install(hermes_home=tmp_path, activate=True)

    verify_result = installer.verify(hermes_home=tmp_path, runtime=True)

    assert verify_result["ok"] is True
    assert verify_result["plugin_files"]["ok"] is True
    assert verify_result["provider_load"]["ok"] is True
    assert verify_result["hermes_config"] == {
        "exists": True,
        "memory_provider": "scope-recall",
        "ok": True,
    }
    assert verify_result["sqlite_truth"]["exists"] is True
    assert verify_result["sqlite_truth"]["schema_current"] is True
    assert verify_result["tool_schemas"]["compact_required_present"] is True
    assert "scope_recall_memory" in verify_result["tool_schemas"]["names"]
    assert verify_result["vector_companion"]["configured_backend"] in {"lancedb", "sqlite-bruteforce"}
    assert verify_result["vector_companion"]["status"] in {"ready", "disabled", "not_initialized", "degraded"}


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
    import scope_recall.installer as installer

    plugin_dir = tmp_path / "plugins" / PLUGIN_NAME
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text("name: other\nversion: 0.0.1\n", encoding="utf-8")

    with pytest.raises(installer.InstallError):
        installer.install(hermes_home=tmp_path)

    result = installer.install(hermes_home=tmp_path, force=True)
    assert result["ok"] is True
    assert (plugin_dir / "plugin.yaml").read_text(encoding="utf-8").startswith("name: scope-recall")


def test_installer_refuses_to_overwrite_unknown_existing_target_without_force(tmp_path):
    import scope_recall.installer as installer

    plugin_dir = tmp_path / "plugins" / PLUGIN_NAME
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "README.md").write_text("unknown existing content\n", encoding="utf-8")

    with pytest.raises(installer.InstallError):
        installer.install(hermes_home=tmp_path)

    assert (plugin_dir / "README.md").read_text(encoding="utf-8") == "unknown existing content\n"


def test_installer_refuses_to_overwrite_regular_file_target_without_force(tmp_path):
    import scope_recall.installer as installer

    plugin_dir = tmp_path / "plugins" / PLUGIN_NAME
    plugin_dir.parent.mkdir(parents=True)
    plugin_dir.write_text("not a plugin directory\n", encoding="utf-8")

    with pytest.raises(installer.InstallError):
        installer.install(hermes_home=tmp_path)

    assert plugin_dir.is_file()
    assert plugin_dir.read_text(encoding="utf-8") == "not a plugin directory\n"


def test_installer_from_read_only_source_root_can_atomically_rename_staging(
    tmp_path,
    monkeypatch,
):
    import scope_recall.installer as installer

    fake_source = tmp_path / "readonly-source" / "scope-recall"
    _write_installed_plugin(fake_source, version="1.8.0", marker="readonly source")
    fake_source.chmod(0o555)
    monkeypatch.setattr(installer, "source_root", lambda: fake_source)
    home = tmp_path / "home"

    try:
        result = installer.install(hermes_home=home)
    finally:
        fake_source.chmod(0o755)

    installed = home / "plugins" / PLUGIN_NAME
    assert result["ok"] is True
    assert installed.is_dir()
    assert os.access(installed, os.W_OK)


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
        elif rel == "config.json":
            content = "{}\n"
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
    init_file.chmod(init_file.stat().st_mode | 0o200)
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
    try:
        assert provider.name == PLUGIN_NAME
        assert provider.is_available() is True
    finally:
        try:
            provider.shutdown()
        finally:
            for name in list(sys.modules):
                if name.startswith("_hermes_user_memory.scope-recall"):
                    sys.modules.pop(name, None)
            sys.modules.pop("_hermes_user_memory", None)


def test_installed_plugin_loads_from_outside_repo_without_source_alias(tmp_path):
    from scope_recall import installer

    hermes_home = tmp_path / "hermes-home"
    result = installer.install(hermes_home=hermes_home)
    assert result["ok"] is True
    outside = tmp_path / "outside-cwd"
    outside.mkdir()
    env = dict(os.environ)
    env["HERMES_HOME"] = str(hermes_home)
    env["PYTHONNOUSERSITE"] = "1"
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    code = """
import json, pathlib, sys
from plugins.memory import load_memory_provider
provider = load_memory_provider('scope-recall')
module = sys.modules[provider.__class__.__module__]
payload = {
    'name': provider.name,
    'available': provider.is_available(),
    'module_file': str(pathlib.Path(module.__file__).resolve()),
    'sys_path': [str(pathlib.Path(item or '.').resolve()) for item in sys.path],
}
provider.shutdown()
print(json.dumps(payload, sort_keys=True))
"""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=outside,
        env=env,
        text=True,
        capture_output=True,
        timeout=90,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    installed_root = (hermes_home / "plugins" / PLUGIN_NAME).resolve()
    assert payload["name"] == PLUGIN_NAME
    assert payload["available"] is True
    assert Path(payload["module_file"]).is_relative_to(installed_root)
    assert str(PLUGIN_ROOT.resolve()) not in payload["sys_path"]


def test_nested_clone_build_and_fresh_venv_ignore_polluted_parent(tmp_path):
    outer = tmp_path / "polluted-parent"
    poison = outer / "scope_recall"
    poison.mkdir(parents=True)
    (poison / "__init__.py").write_text(
        "raise RuntimeError('polluted parent package imported')\n",
        encoding="utf-8",
    )
    clone = outer / "nested" / "scope-recall"
    shutil.copytree(
        PLUGIN_ROOT,
        clone,
        ignore=shutil.ignore_patterns(
            ".git",
            ".hermes",
            ".pytest_cache",
            "__pycache__",
            "*.pyc",
            "build",
            "dist",
            "*.egg-info",
        ),
    )
    dist_dir = tmp_path / "dist"
    build_env = dict(os.environ)
    build_env["PYTHONPATH"] = str(outer)
    build = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(dist_dir),
        ],
        cwd=clone,
        env=build_env,
        text=True,
        capture_output=True,
        timeout=180,
    )
    assert build.returncode == 0, build.stderr + build.stdout
    wheels = list(dist_dir.glob("*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as archive:
        init_payload = archive.read("scope_recall/__init__.py").decode("utf-8")
        names = set(archive.namelist())
    assert "polluted parent package imported" not in init_payload
    assert {
        "scope_recall/operator_ledger.py",
        "scope_recall/relation_rebuild_queue.py",
        "scope_recall/relation_scope_state.py",
        "scope_recall/vector_outbox_replay.py",
    } <= names

    venv_dir = tmp_path / "fresh-venv"
    create = subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert create.returncode == 0, create.stderr + create.stdout
    venv_python = (
        venv_dir / "Scripts" / "python.exe"
        if os.name == "nt"
        else venv_dir / "bin" / "python"
    )
    clean_env = dict(os.environ)
    clean_env["PYTHONNOUSERSITE"] = "1"
    clean_env.pop("PYTHONPATH", None)
    clean_env.pop("PYTHONHOME", None)
    install = subprocess.run(
        [str(venv_python), "-m", "pip", "install", "--no-deps", str(wheels[0])],
        env=clean_env,
        text=True,
        capture_output=True,
        timeout=180,
    )
    assert install.returncode == 0, install.stderr + install.stdout
    outside = tmp_path / "fresh-outside-cwd"
    outside.mkdir()
    probe = subprocess.run(
        [
            str(venv_python),
            "-c",
            (
                "import json, pathlib, scope_recall; "
                "from scope_recall import operator_ledger, relation_rebuild_queue, relation_scope_state, vector_outbox_replay; "
                "print(json.dumps({'package': str(pathlib.Path(scope_recall.__file__).resolve()), "
                "'operator': str(pathlib.Path(operator_ledger.__file__).resolve())}, sort_keys=True))"
            ),
        ],
        cwd=outside,
        env=clean_env,
        text=True,
        capture_output=True,
        timeout=90,
    )
    assert probe.returncode == 0, probe.stderr + probe.stdout
    imported = json.loads(probe.stdout.strip())
    assert Path(imported["package"]).is_relative_to(venv_dir.resolve())
    assert Path(imported["operator"]).is_relative_to(venv_dir.resolve())


def test_installer_cli_json_verify_round_trip(tmp_path):
    from scope_recall import installer

    install_exit = installer.main(["install", "--hermes-home", str(tmp_path), "--json"])
    verify_exit = installer.main(["verify", "--hermes-home", str(tmp_path), "--json"])

    assert install_exit == 0
    assert verify_exit == 0
    assert (tmp_path / "plugins" / PLUGIN_NAME / "plugin.yaml").is_file()


def test_installer_cli_activate_bootstraps_runtime(tmp_path, monkeypatch):
    from scope_recall import installer

    _isolate_fresh_installer_credentials(monkeypatch, tmp_path)
    assert installer.main(["install", "--activate", "--hermes-home", str(tmp_path), "--json"]) == 0
    assert "memory:\n  provider: scope-recall" in (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert (tmp_path / "scope-recall" / "memory.sqlite3").is_file()
    assert installer.main(["verify", "--runtime", "--hermes-home", str(tmp_path), "--json"]) == 0


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
