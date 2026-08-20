"""CLI mapping, one-object JSON, redaction, packaging, and recovery isolation."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

from journal_source_restore_support import (
    apply_kwargs,
    build_source_restore_pair,
    cli_argv,
    count_rows,
    plan_kwargs,
)
from scope_recall import cli
from scope_recall.maintenance_lease import (
    acquire_activation_lease,
    activation_lease_path,
    release_activation_lease,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "journal.source_restore.py"
RECOVERY_SCRIPT = ROOT / "scripts" / "journal.recovery.py"


def _run_source_restore(args: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    clean_env = os.environ.copy()
    clean_env.pop("PYTHONPATH", None)
    clean_env.pop("PYTHONHOME", None)
    clean_env.pop("VIRTUAL_ENV", None)
    clean_env["PYTHONDONTWRITEBYTECODE"] = "1"
    if env:
        clean_env.update(env)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        check=False,
        capture_output=True,
        text=True,
        env=clean_env,
        cwd=str(ROOT),
    )


def test_cli_mapping_is_distinct_from_journal_recovery() -> None:
    assert cli._SCRIPT_COMMANDS[("journal", "recovery")] == ("journal.recovery.py", [])
    assert cli._SCRIPT_COMMANDS[("journal", "source-restore")] == (
        "journal.source_restore.py",
        [],
    )
    assert "journal source-restore" in cli._HELP
    assert "journal recovery" in cli._HELP


def test_old_recovery_command_still_dispatches(monkeypatch) -> None:
    calls: list[tuple[str, list[str]]] = []

    def fake_run(script_name: str, forwarded_args: list[str]) -> int:
        calls.append((script_name, list(forwarded_args)))
        return 0

    monkeypatch.setattr(cli, "_run_script", fake_run)
    assert cli.main(["journal", "recovery", "--dry-run"]) == 0
    assert cli.main(["journal", "source-restore", "--source", "s", "--target", "t"]) == 0
    assert calls == [
        ("journal.recovery.py", ["--dry-run"]),
        ("journal.source_restore.py", ["--source", "s", "--target", "t"]),
    ]


def test_recovery_script_file_is_unchanged_entrypoint() -> None:
    source = RECOVERY_SCRIPT.read_text(encoding="utf-8")
    assert "schedule_replay" in source
    assert "source_restore" not in source
    assert "run_journal_source_restore" not in source


def test_cli_dry_run_emits_one_json_object(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    kwargs = plan_kwargs(pair)
    completed = _run_source_restore(cli_argv(kwargs))
    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert isinstance(payload, dict)
    assert completed.stdout.strip().startswith("{")
    assert completed.stdout.count("{") >= 1
    json.loads(completed.stdout)
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert not activation_lease_path(pair.target_path).exists()
    assert "content" not in json.dumps(payload)
    assert "synthetic-approved" not in completed.stdout
    assert "synthetic-approved" not in completed.stderr


def test_cli_apply_requires_explicit_gates(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    kwargs = apply_kwargs(pair)
    completed = _run_source_restore(
        [
            *cli_argv(kwargs),
            "--apply",
        ]
    )
    assert completed.returncode != 0
    payload = json.loads(completed.stdout)
    assert payload["ok"] is False
    assert payload["error_code"] in {
        "confirmation_required",
        "activation_lease_required",
        "prewrite_backup_required",
        "target_epoch_required",
    }


def test_recursive_denylist_redacts_forbidden_fields() -> None:
    from scope_recall.journal_source_restore import redact_source_restore_payload

    dirty = {
        "ok": True,
        "stage": "plan",
        "content": "secret-journal-body",
        "metadata": {"token": "abc"},
        "id": 99,
        "digest_id": "jsr-run-ok",
        "id_map": {1: 2},
        "nested": {
            "exception": "Traceback (most recent call last): boom",
            "counts": {"journal_selected_count": 19},
        },
        "error": "OperationalError: token=super-secret",
    }
    clean = redact_source_restore_payload(dirty)
    rendered = json.dumps(clean)
    assert "secret-journal-body" not in rendered
    assert "jsr-run-ok" not in rendered
    assert "Traceback" not in rendered
    assert "super-secret" not in rendered
    assert "token" not in rendered or "[REDACTED" in rendered
    assert clean["ok"] is True
    assert clean["stage"] == "plan"


def test_package_and_static_manifests_include_runtime_files() -> None:
    spec = importlib.util.spec_from_file_location(
        "scope_recall_check_release_journal_source_restore",
        ROOT / "scripts" / "check.release.py",
    )
    assert spec is not None and spec.loader is not None
    release_check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release_check)
    assert (ROOT / "journal_source_restore.py").is_file()
    assert (ROOT / "journal_source_restore_snapshot.py").is_file()
    assert (ROOT / "journal_source_restore_rows.py").is_file()
    assert SCRIPT.is_file()
    assert (ROOT / "docs" / "journal-source-restore.md").is_file()
    assert (ROOT / "tests" / "journal_source_restore_support.py").is_file()
    assert (ROOT / "tests" / "journal_source_restore_oracles.py").is_file()
    assert "journal_source_restore.py" in release_check.REQUIRED_SOURCE_FILES
    assert "journal_source_restore_snapshot.py" in release_check.REQUIRED_SOURCE_FILES
    assert "journal_source_restore_rows.py" in release_check.REQUIRED_SOURCE_FILES
    assert "scripts/journal.source_restore.py" in release_check.REQUIRED_SOURCE_FILES
    assert "docs/journal-source-restore.md" in release_check.REQUIRED_SOURCE_FILES
    assert "tests/journal_source_restore_support.py" in release_check.REQUIRED_SOURCE_RESTORE_SDIST_TESTS
    assert "tests/journal_source_restore_oracles.py" in release_check.REQUIRED_SOURCE_RESTORE_SDIST_TESTS
    assert "scope_recall/journal_source_restore.py" in release_check.REQUIRED_WHEEL
    assert "scope_recall/journal_source_restore_snapshot.py" in release_check.REQUIRED_WHEEL
    assert "scope_recall/journal_source_restore_rows.py" in release_check.REQUIRED_WHEEL
    assert "scope_recall/scripts/journal.source_restore.py" in release_check.REQUIRED_WHEEL
    assert "scope_recall/docs/journal-source-restore.md" in release_check.REQUIRED_WHEEL
    for member in (
        "journal_source_restore.py",
        "journal_source_restore_snapshot.py",
        "journal_source_restore_rows.py",
        "tests/journal_source_restore_support.py",
        "tests/journal_source_restore_oracles.py",
    ):
        assert any(name.endswith(member) for name in release_check.REQUIRED_SDIST)


def test_cli_subprocess_apply_restores_nineteen_and_two(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    completed = _run_source_restore(cli_argv(apply_kwargs(pair), apply=True))
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert completed.stdout.strip().startswith("{")
    json.loads(completed.stdout)
    assert payload["ok"] is True
    assert payload["verdict"] == "applied"
    assert payload["journal_inserted_count"] == 19
    assert payload["digest_run_inserted_count"] == 2
    assert count_rows(pair.target_path, "journal_entries") == 20
    assert count_rows(pair.target_path, "journal_digest_runs") == 2
    assert not activation_lease_path(pair.target_path).exists()
    assert "Traceback" not in completed.stderr
    assert "token" not in completed.stdout
    assert "path" not in payload


def test_cli_subprocess_releases_lease_after_backup_failure(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    pair.backup_path.parent.mkdir(parents=True, exist_ok=True)
    pair.backup_path.write_bytes(b"preexisting-backup")
    before = count_rows(pair.target_path, "journal_entries")
    completed = _run_source_restore(cli_argv(apply_kwargs(pair), apply=True))
    assert completed.returncode != 0
    payload = json.loads(completed.stdout)
    assert payload["ok"] is False
    assert payload["error_code"] == "prewrite_backup_failed"
    assert count_rows(pair.target_path, "journal_entries") == before
    assert not activation_lease_path(pair.target_path).exists()
    assert "Traceback" not in completed.stderr


def test_cli_subprocess_releases_lease_after_apply_refusal(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    kwargs = apply_kwargs(pair)
    kwargs["expected_target_epoch_digest"] = "0" * 64
    before = count_rows(pair.target_path, "journal_entries")
    completed = _run_source_restore(cli_argv(kwargs, apply=True))
    assert completed.returncode != 0
    payload = json.loads(completed.stdout)
    assert payload["ok"] is False
    assert payload["error_code"] == "target_epoch_stale"
    assert count_rows(pair.target_path, "journal_entries") == before
    assert not activation_lease_path(pair.target_path).exists()
    assert "Traceback" not in completed.stderr


def test_cli_foreign_lease_emits_one_bounded_conflict(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    lease = acquire_activation_lease(pair.target_path)
    try:
        completed = _run_source_restore(cli_argv(apply_kwargs(pair), apply=True))
    finally:
        release_activation_lease(lease)
    assert completed.returncode != 0
    payload = json.loads(completed.stdout)
    assert payload["ok"] is False
    assert payload["error_code"] == "activation_lease_conflict"
    assert "Traceback" not in completed.stderr
    assert "token" not in completed.stdout
    assert "path" not in payload
    rendered = json.dumps(payload)
    assert ".activation-maintenance" not in rendered
    assert str(pair.target_path) not in completed.stdout


def test_cli_release_failure_after_committed_apply_is_not_clean_success(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    pair = build_source_restore_pair(tmp_path)
    spec = importlib.util.spec_from_file_location(
        "scope_recall_journal_source_restore_cli_cleanup",
        SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "release_activation_lease", lambda _lease: False)
    code = module.main(cli_argv(apply_kwargs(pair), apply=True))
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code != 0
    assert payload["ok"] is False
    assert payload["error_code"] == "activation_lease_cleanup_failed"
    assert payload["status"] == "manual_recovery_required"
    assert payload["verdict"] == "applied_cleanup_failed"
    assert payload["journal_inserted_count"] == 19
    assert payload["digest_run_inserted_count"] == 2
    assert payload["journal_set_digest"] == pair.expected_journal_set_digest
    assert "token" not in payload
    assert "path" not in payload
    assert "Traceback" not in captured.err
