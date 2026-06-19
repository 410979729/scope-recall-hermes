from __future__ import annotations

from pathlib import Path

from scope_recall.config import load_runtime_config


def test_experience_defaults_enable_prefetch_but_not_auto_promotion(tmp_path):
    plugin_dir = Path(__file__).resolve().parents[1]
    storage_dir = tmp_path / "scope-recall"
    storage_dir.mkdir()

    config = load_runtime_config(plugin_dir, storage_dir)

    assert config["experience"]["enabled"] is True
    assert config["experience"]["prefetch_enabled"] is True
    assert config["experience"]["auto_promotion_enabled"] is False
    assert config["experience"]["auto_promote_low_risk"] is True
