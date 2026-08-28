"""Read-only accidental HOME residue receipt contracts."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "report.home_cleanup.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "scope_recall_home_cleanup_receipt_test",
        SCRIPT,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cleanup_receipt_is_read_only_content_free_and_classified(
    tmp_path: Path,
) -> None:
    module = _load_module()
    accidental = tmp_path / "user-home" / "plugins" / "scope-recall"
    accidental.mkdir(parents=True)
    (accidental / "plugin.yaml").write_text("name: scope-recall\n", encoding="utf-8")
    cache = accidental / "__pycache__"
    cache.mkdir()
    (cache / "provider.pyc").write_bytes(b"compiled fixture")
    quarantine = tmp_path / "quarantine"
    quarantine.mkdir()
    (quarantine / "receipt.json").write_text("{}\n", encoding="utf-8")
    active = tmp_path / "active-hermes" / "plugins" / "scope-recall"

    before = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    payload = module.build_cleanup_receipt(
        accidental_path=accidental,
        active_plugin_path=active,
        quarantine_path=quarantine,
    )
    after = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))

    assert after == before
    assert payload["operation"] == "read_only_inventory"
    assert payload["deletion_performed"] is False
    assert payload["active_instance_touched"] is False
    assert payload["accidental_home_residue"]["exists"] is True
    assert payload["accidental_home_residue"]["empty"] is False
    assert payload["accidental_home_residue"]["residual_classes"] == {
        "pycache": 1,
        "config": 1,
        "plugin_files": 0,
        "other": 0,
    }
    assert payload["quarantine"]["inventory_sha256"]
    rendered = json.dumps(payload, sort_keys=True)
    assert str(tmp_path) not in rendered
    assert "plugin.yaml" not in rendered


def test_cleanup_receipt_reports_missing_path_without_creating_it(
    tmp_path: Path,
) -> None:
    module = _load_module()
    missing = tmp_path / "missing"

    payload = module.build_cleanup_receipt(
        accidental_path=missing,
        active_plugin_path=tmp_path / "active" / "plugins" / "scope-recall",
        quarantine_path=tmp_path / "quarantine",
    )

    assert payload["accidental_home_residue"]["state"] == "missing"
    assert not missing.exists()


def test_cleanup_receipt_refuses_active_quarantine_overlap(tmp_path: Path) -> None:
    module = _load_module()
    active = tmp_path / "active" / "plugins" / "scope-recall"

    with pytest.raises(module.HomeCleanupReceiptError, match="overlap"):
        module.build_cleanup_receipt(
            accidental_path=tmp_path / "accidental",
            active_plugin_path=active,
            quarantine_path=active / "quarantine",
        )
