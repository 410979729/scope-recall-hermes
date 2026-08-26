"""Tests for cross-profile rollout planning and safety checks.

They guard against accidental writes to other Hermes profiles without explicit operator intent."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

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


def test_rollout_profiles_accepts_explicit_plan_and_json_flags_without_mutation(tmp_path: Path):
    profiles_root = tmp_path / "profiles"
    alpha = profiles_root / "alpha"
    old_plugin = _write_plugin(alpha, version="0.1.0")
    before = (old_plugin / "plugin.yaml").read_text(encoding="utf-8")

    report = _run_rollout("--profiles-root", str(profiles_root), "--plan", "--json")

    assert report["ok"] is True
    assert report["dry_run"] is True
    assert report["plan"] is True
    assert report["actions"][0]["profile"] == "alpha"
    assert report["actions"][0]["planned"] is True
    assert report["actions"][0]["applied"] is False
    assert (old_plugin / "plugin.yaml").read_text(encoding="utf-8") == before
    assert not (alpha / "backups").exists()


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
    assert "version: 1.10.6" in (alpha / "plugins" / "scope-recall" / "plugin.yaml").read_text(encoding="utf-8")
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
    assert "version: 1.10.6" in (alpha / "plugins" / "scope-recall" / "plugin.yaml").read_text(encoding="utf-8")

    rollback = _run_rollout("--profiles-root", str(profiles_root), "--rollback", "--apply", "--receipt", str(receipt))

    assert rollback["ok"] is True
    assert rollback["rollback"] is True
    assert rollback["dry_run"] is False
    assert rollback["rollback_restored"] == 1
    assert "version: 0.1.0" in (alpha / "plugins" / "scope-recall" / "plugin.yaml").read_text(encoding="utf-8")


def test_rollout_restore_move_failure_compensates_from_current_backup(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_rollout_module()
    profile_home = tmp_path / "profile"
    plugin_dir = _write_plugin(profile_home, version="2.0.0")
    previous_home = tmp_path / "previous"
    previous = _write_plugin(previous_home, version="1.0.0")

    def fail_move(_source: Path, _destination: Path) -> Path:
        raise OSError("injected final move failure")

    monkeypatch.setattr(module, "move_path", fail_move)

    with pytest.raises(OSError, match="injected final move failure"):
        module.restore_plugin(
            profile_home,
            str(previous),
            previous_plugin_existed=True,
        )

    assert "version: 2.0.0" in (plugin_dir / "plugin.yaml").read_text(encoding="utf-8")
    assert not any(plugin_dir.parent.glob(".sr-rb-*"))


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length rollout contract")
def test_rollout_backup_and_repeat_restore_support_deep_profile_paths(tmp_path: Path):
    from scope_recall.windows_filesystem import io_path, make_dirs, remove_path

    component_length = 178 - len(str(tmp_path)) - 1
    if component_length < 8 or component_length > 240:
        pytest.skip("temporary path cannot form the 178-character profile fixture")
    profile_home = tmp_path / ("p" * component_length)
    plugin_dir = _write_plugin(profile_home, version="0.1.0")
    nested = plugin_dir / ("n" * 80)
    make_dirs(nested)
    Path(io_path(nested / "payload.txt")).write_text("old deep payload", encoding="utf-8")
    module = _load_rollout_module()

    try:
        backup_path = module.backup_plugin(profile_home)
        assert "\\\\?\\" not in backup_path
        assert os.path.isfile(io_path(Path(backup_path) / ("n" * 80) / "payload.txt"))

        (plugin_dir / "plugin.yaml").write_text(
            "name: scope-recall\nversion: 2.0.0\n",
            encoding="utf-8",
        )
        first_current_backup = module.restore_plugin(
            profile_home,
            backup_path,
            previous_plugin_existed=True,
        )
        assert "\\\\?\\" not in first_current_backup
        assert "version: 0.1.0" in (plugin_dir / "plugin.yaml").read_text(encoding="utf-8")
        assert Path(io_path(nested / "payload.txt")).read_text(encoding="utf-8") == "old deep payload"

        second_current_backup = module.restore_plugin(
            profile_home,
            backup_path,
            previous_plugin_existed=True,
        )
        assert "\\\\?\\" not in second_current_backup
        assert "version: 0.1.0" in (plugin_dir / "plugin.yaml").read_text(encoding="utf-8")
    finally:
        remove_path(profile_home, missing_ok=True, ignore_errors=True)


def test_apply_requires_durable_receipt_before_mutation(tmp_path: Path):
    module = _load_rollout_module()
    profiles_root = tmp_path / "profiles"
    home = profiles_root / "alpha"
    _write_plugin(home, version="0.1.0")

    with pytest.raises(ValueError, match="receipt"):
        module.rollout_profiles(
            profiles_root=profiles_root,
            selected_profiles=["alpha"],
            canary="alpha",
            apply=True,
            receipt_path=None,
        )

    assert module.read_manifest_version(home / "plugins" / "scope-recall") == "0.1.0"


def test_receipt_is_published_after_backup_before_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    module = _load_rollout_module()
    profiles_root = tmp_path / "profiles"
    home = profiles_root / "alpha"
    receipt = tmp_path / "receipts" / "rollout.json"
    _write_plugin(home, version="0.1.0")
    observed = {"prepublished": False}
    original_install = module.installer.install

    def checking_install(profile_home: Path, *, force: bool = False):
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        action = payload["actions"][0]
        observed["prepublished"] = bool(action["backup_path"])
        return original_install(profile_home, force=force)

    monkeypatch.setattr(module.installer, "install", checking_install)

    report = module.rollout_profiles(
        profiles_root=profiles_root,
        selected_profiles=["alpha"],
        canary="alpha",
        apply=True,
        receipt_path=receipt,
    )

    assert report["ok"] is True
    assert observed["prepublished"] is True


def test_install_not_ok_is_compensated_to_previous_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    module = _load_rollout_module()
    profiles_root = tmp_path / "profiles"
    home = profiles_root / "alpha"
    receipt = tmp_path / "receipt.json"
    _write_plugin(home, version="0.1.0")

    def broken_install(profile_home: Path, *, force: bool = False):
        del force
        (profile_home / "plugins" / "scope-recall" / "plugin.yaml").write_text(
            "name: scope-recall\nversion: 9.9.9-broken\n",
            encoding="utf-8",
        )
        return {"ok": False, "installed": True, "verify": {"ok": False}}

    monkeypatch.setattr(module.installer, "install", broken_install)

    report = module.rollout_profiles(
        profiles_root=profiles_root,
        selected_profiles=["alpha"],
        canary="alpha",
        apply=True,
        receipt_path=receipt,
    )

    action = report["actions"][0]
    assert report["ok"] is False
    assert action["compensated"] is True
    assert module.read_manifest_version(home / "plugins" / "scope-recall") == "0.1.0"


def test_receipt_failure_after_install_compensates_previous_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    module = _load_rollout_module()
    profiles_root = tmp_path / "profiles"
    home = profiles_root / "alpha"
    receipt = tmp_path / "receipt.json"
    _write_plugin(home, version="0.1.0")
    calls = {"count": 0}

    def flaky_publish(report: dict, path: Path) -> None:
        calls["count"] += 1
        if calls["count"] >= 2:
            raise OSError("injected receipt publication failure")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report), encoding="utf-8")

    monkeypatch.setattr(module, "_publish_receipt", flaky_publish)

    report = module.rollout_profiles(
        profiles_root=profiles_root,
        selected_profiles=["alpha"],
        canary="alpha",
        apply=True,
        receipt_path=receipt,
    )

    action = report["actions"][0]
    assert report["ok"] is False
    assert action["compensated"] is True
    assert module.read_manifest_version(home / "plugins" / "scope-recall") == "0.1.0"


@pytest.mark.skipif(os.name != "nt", reason="Windows extended path boundary")
def test_deep_profile_inventory_and_receipt_use_long_path_io(tmp_path: Path):
    from scope_recall.windows_filesystem import io_path, make_dirs, path_is_file, remove_path

    module = _load_rollout_module()
    profiles_root = tmp_path / "profiles"
    suffix_length = 245 - len(str(profiles_root)) - 1
    if suffix_length < 8 or suffix_length > 240:
        pytest.skip("temporary path cannot form the 245-character profile fixture")
    home = profiles_root / ("p" * suffix_length)
    plugin = home / "plugins" / "scope-recall"
    make_dirs(plugin)
    for name, text in {
        "plugin.yaml": "name: scope-recall\nversion: 0.1.0\n",
        "__init__.py": "",
        "provider.py": "",
        "config.json": "{}",
    }.items():
        with open(io_path(plugin / name), "w", encoding="utf-8") as handle:
            handle.write(text)
    receipt = home / "receipts" / "rollout.json"

    try:
        report = module.rollout_profiles(
            profiles_root=profiles_root,
            selected_profiles=[home.name],
            canary=home.name,
            apply=False,
            receipt_path=receipt,
        )

        assert report["ok"] is True
        assert report["profiles"][0]["plugin_exists"] is True
        assert report["profiles"][0]["plugin_version"] == "0.1.0"
        assert path_is_file(receipt)
    finally:
        remove_path(home, missing_ok=True, ignore_errors=True)
