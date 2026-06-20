from __future__ import annotations

from pathlib import Path

from scope_recall.config import load_runtime_config
from scope_recall.schemas import SCOPE_RECALL_EXPERIENCE_PREFLIGHT_SCHEMA


def test_experience_defaults_enable_prefetch_but_not_auto_promotion(tmp_path):
    plugin_dir = Path(__file__).resolve().parents[1]
    storage_dir = tmp_path / "scope-recall"
    storage_dir.mkdir()

    config = load_runtime_config(plugin_dir, storage_dir)

    assert config["experience"]["enabled"] is True
    assert config["experience"]["prefetch_enabled"] is True
    assert config["experience"]["auto_promotion_enabled"] is False
    assert config["experience"]["auto_promote_low_risk"] is False


def test_experience_docs_and_schema_match_default_promotion_contract():
    plugin_dir = Path(__file__).resolve().parents[1]
    docs = [
        plugin_dir / "docs" / "experience.kernel.md",
        plugin_dir / "docs" / "stability.md",
        plugin_dir / "docs" / "release-readiness.1.4.0.md",
    ]

    for path in docs:
        text = path.read_text(encoding="utf-8")
        assert "experience.auto_promotion_enabled=true" not in text
        assert "auto_promotion_enabled=false" in text

    schema_description = SCOPE_RECALL_EXPERIENCE_PREFLIGHT_SCHEMA["description"]
    assert "default runtime injection remains disabled" not in schema_description
    assert "experience.prefetch_enabled" in schema_description

def test_readme_describes_prefetch_and_auto_promotion_controls_separately():
    plugin_dir = Path(__file__).resolve().parents[1]
    text = (plugin_dir / "README.md").read_text(encoding="utf-8")

    assert "experience.prefetch_enabled=true" in text
    assert "experience.prefetch_enabled=false" in text
    assert "experience.auto_promotion_enabled=true" in text
    assert "background automatic promotion remains an explicit operator opt-in" in text
    assert "with `experience.prefetch_enabled=false` and `experience.auto_promotion_enabled=true` available as an explicit opt-in" not in text
