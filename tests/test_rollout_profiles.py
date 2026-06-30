"""Tests for cross-profile rollout planning and safety checks.

They guard against accidental writes to other Hermes profiles without explicit operator intent."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN_ROOT / "scripts" / "rollout.profiles.py"


def _write_plugin(profile_home: Path, *, version: str) -> Path:
    plugin_dir = profile_home / "plugins" / "scope-recall"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.yaml").write_text(f"name: scope-recall\nversion: {version}\n", encoding="utf-8")
    (plugin_dir / "__init__.py").write_text('"""old plugin"""\n', encoding="utf-8")
    (plugin_dir / "provider.py").write_text("class OldProvider: pass\n", encoding="utf-8")
    (plugin_dir / "config.json").write_text("{}\n", encoding="utf-8")
    return plugin_dir


def _load_rollout_module():
    spec = importlib.util.spec_from_file_location("scope_recall_rollout_profiles_test_runtime", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_rollout_raw(*args: str) -> tuple[subprocess.CompletedProcess[str], dict]:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.stdout, result.stderr
    return result, json.loads(result.stdout)


def _run_rollout(*args: str) -> dict:
    result, payload = _run_rollout_raw(*args)
    assert result.returncode == 0, result.stderr or json.dumps(payload, ensure_ascii=False)
    return payload


def test_rollout_profiles_cli_dispatch_is_registered():
    import scope_recall.cli as cli

    assert cli._SCRIPT_COMMANDS[("rollout", "profiles")] == ("rollout.profiles.py", [])
    assert "hermes-scope-recall rollout profiles" in cli._HELP


def test_rollout_profiles_default_dry_run_inventories_without_mutation(tmp_path: Path):
    profiles_root = tmp_path / "profiles"
    alpha = profiles_root / "alpha"
    beta = profiles_root / "beta"
    old_plugin = _write_plugin(alpha, version="0.1.0")
    beta.mkdir(parents=True)
    before = (old_plugin / "plugin.yaml").read_text(encoding="utf-8")

    report = _run_rollout("--profiles-root", str(profiles_root))

    assert report["ok"] is True
    assert report["dry_run"] is True
    assert report["rollback"] is False
    assert {profile["name"] for profile in report["profiles"]} == {"alpha", "beta"}
    assert {action["profile"] for action in report["actions"]} == {"alpha", "beta"}
    assert all(action["planned"] for action in report["actions"])
    assert all(not action["applied"] for action in report["actions"])
    assert (old_plugin / "plugin.yaml").read_text(encoding="utf-8") == before
    assert not (alpha / "backups").exists()
    assert not (beta / "plugins" / "scope-recall").exists()


def test_rollout_profiles_apply_canary_backs_up_only_selected_profile(tmp_path: Path):
    profiles_root = tmp_path / "profiles"
    alpha = profiles_root / "alpha"
    beta = profiles_root / "beta"
    _write_plugin(alpha, version="0.1.0")
    _write_plugin(beta, version="0.2.0")
    receipt = tmp_path / "rollout-receipt.json"

    report = _run_rollout(
        "--profiles-root",
        str(profiles_root),
        "--canary",
        "alpha",
        "--apply",
        "--receipt",
        str(receipt),
    )

    assert report["ok"] is True
    assert report["dry_run"] is False
    assert receipt.exists()
    by_profile = {action["profile"]: action for action in report["actions"]}
    assert by_profile["alpha"]["applied"] is True
    assert by_profile["alpha"]["backup_path"]
    assert Path(by_profile["alpha"]["backup_path"]).exists()
    assert by_profile["alpha"]["previous_plugin_existed"] is True
    assert by_profile["beta"]["applied"] is False
    assert by_profile["beta"]["reason"] == "not_canary"
    assert "version: 0.1.0" in (Path(by_profile["alpha"]["backup_path"]) / "plugin.yaml").read_text(encoding="utf-8")
    assert "version: 1.6.0" in (alpha / "plugins" / "scope-recall" / "plugin.yaml").read_text(encoding="utf-8")
    assert "version: 0.2.0" in (beta / "plugins" / "scope-recall" / "plugin.yaml").read_text(encoding="utf-8")


def test_rollout_profiles_apply_records_partial_failure_receipt(tmp_path: Path, monkeypatch):
    profiles_root = tmp_path / "profiles"
    alpha = profiles_root / "alpha"
    beta = profiles_root / "beta"
    _write_plugin(alpha, version="0.1.0")
    _write_plugin(beta, version="0.2.0")
    receipt = tmp_path / "partial-failure-receipt.json"
    rollout = _load_rollout_module()
    calls: list[str] = []

    def fake_install(profile_home: Path, *, force: bool):
        calls.append(profile_home.name)
        if profile_home.name == "beta":
            raise RuntimeError("simulated install failure")
        return {"ok": True, "installed": True, "verify": {"ok": True}}

    monkeypatch.setattr(rollout.installer, "install", fake_install)

    report = rollout.rollout_profiles(profiles_root=profiles_root, apply=True, receipt_path=receipt)

    assert calls == ["alpha", "beta"]
    assert report["ok"] is False
    assert receipt.exists()
    written = json.loads(receipt.read_text(encoding="utf-8"))
    by_profile = {action["profile"]: action for action in written["actions"]}
    assert by_profile["alpha"]["applied"] is True
    assert by_profile["alpha"]["backup_path"]
    assert Path(by_profile["alpha"]["backup_path"]).exists()
    assert by_profile["beta"]["applied"] is False
    assert by_profile["beta"]["ok"] is False
    assert "simulated install failure" in by_profile["beta"]["error"]
    assert "version: 0.2.0" in (beta / "plugins" / "scope-recall" / "plugin.yaml").read_text(encoding="utf-8")


def test_rollout_profiles_rollback_refuses_missing_backup_without_deleting_current_plugin(tmp_path: Path):
    profiles_root = tmp_path / "profiles"
    alpha = profiles_root / "alpha"
    _write_plugin(alpha, version="9.9.9")
    receipt = tmp_path / "bad-receipt.json"
    missing_backup = alpha / "backups" / "scope-recall-rollout" / "missing" / "scope-recall"
    receipt.write_text(
        json.dumps(
            {
                "ok": True,
                "rollback": False,
                "profiles_root": str(profiles_root),
                "actions": [
                    {
                        "profile": "alpha",
                        "hermes_home": str(alpha),
                        "applied": True,
                        "previous_plugin_existed": True,
                        "backup_path": str(missing_backup),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result, report = _run_rollout_raw("--profiles-root", str(profiles_root), "--rollback", "--apply", "--receipt", str(receipt))

    assert result.returncode != 0
    assert report["ok"] is False
    assert report["rollback"] is True
    assert report["actions"][0]["error"]
    assert "version: 9.9.9" in (alpha / "plugins" / "scope-recall" / "plugin.yaml").read_text(encoding="utf-8")


def test_rollout_profiles_rollback_rejects_receipt_outside_profiles_root_without_mutation(tmp_path: Path):
    profiles_root = tmp_path / "profiles"
    outside = tmp_path / "outside-profile"
    _write_plugin(outside, version="7.7.7")
    backup = outside / "backups" / "scope-recall-rollout" / "safe" / "scope-recall"
    backup.mkdir(parents=True)
    (backup / "plugin.yaml").write_text("name: scope-recall\nversion: 0.1.0\n", encoding="utf-8")
    (backup / "__init__.py").write_text("", encoding="utf-8")
    (backup / "provider.py").write_text("", encoding="utf-8")
    (backup / "config.json").write_text("{}\n", encoding="utf-8")
    receipt = tmp_path / "forged-receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "ok": True,
                "rollback": False,
                "profiles_root": str(profiles_root),
                "actions": [
                    {
                        "profile": "outside-profile",
                        "hermes_home": str(outside),
                        "applied": True,
                        "previous_plugin_existed": True,
                        "backup_path": str(backup),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    dry_result, dry_report = _run_rollout_raw("--profiles-root", str(profiles_root), "--rollback", "--receipt", str(receipt))
    apply_result, apply_report = _run_rollout_raw("--profiles-root", str(profiles_root), "--rollback", "--apply", "--receipt", str(receipt))

    assert dry_result.returncode != 0
    assert apply_result.returncode != 0
    assert dry_report["ok"] is False
    assert apply_report["ok"] is False
    assert "outside profiles root" in apply_report["actions"][0]["error"]
    assert "version: 7.7.7" in (outside / "plugins" / "scope-recall" / "plugin.yaml").read_text(encoding="utf-8")


def test_rollout_profiles_apply_missing_profile_fails_closed_without_mutation(tmp_path: Path):
    profiles_root = tmp_path / "profiles"
    alpha = profiles_root / "alpha"
    _write_plugin(alpha, version="0.1.0")

    result, report = _run_rollout_raw("--profiles-root", str(profiles_root), "--profile", "does-not-exist", "--apply")

    assert result.returncode != 0
    assert report["ok"] is False
    assert report["missing_profiles"] == ["does-not-exist"]
    assert report["actions"] == []
    assert not (alpha / "backups").exists()
    assert "version: 0.1.0" in (alpha / "plugins" / "scope-recall" / "plugin.yaml").read_text(encoding="utf-8")


def test_rollout_profiles_apply_missing_canary_fails_closed_without_mutation(tmp_path: Path):
    profiles_root = tmp_path / "profiles"
    alpha = profiles_root / "alpha"
    _write_plugin(alpha, version="0.1.0")

    result, report = _run_rollout_raw("--profiles-root", str(profiles_root), "--canary", "does-not-exist", "--apply")

    assert result.returncode != 0
    assert report["ok"] is False
    assert report["missing_canary"] == "does-not-exist"
    assert all(not action["applied"] for action in report["actions"])
    assert not (alpha / "backups").exists()
    assert "version: 0.1.0" in (alpha / "plugins" / "scope-recall" / "plugin.yaml").read_text(encoding="utf-8")


def test_rollout_profiles_rollback_restores_plugin_from_receipt(tmp_path: Path):
    profiles_root = tmp_path / "profiles"
    alpha = profiles_root / "alpha"
    _write_plugin(alpha, version="0.1.0")
    receipt = tmp_path / "rollout-receipt.json"

    _run_rollout("--profiles-root", str(profiles_root), "--canary", "alpha", "--apply", "--receipt", str(receipt))
    assert "version: 1.6.0" in (alpha / "plugins" / "scope-recall" / "plugin.yaml").read_text(encoding="utf-8")

    rollback = _run_rollout("--profiles-root", str(profiles_root), "--rollback", "--apply", "--receipt", str(receipt))

    assert rollback["ok"] is True
    assert rollback["rollback"] is True
    assert rollback["dry_run"] is False
    assert rollback["rollback_restored"] == 1
    assert "version: 0.1.0" in (alpha / "plugins" / "scope-recall" / "plugin.yaml").read_text(encoding="utf-8")
