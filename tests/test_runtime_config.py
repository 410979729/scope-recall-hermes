"""Runtime configuration loading diagnostics."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scope_recall.config import load_runtime_config, load_runtime_config_errors, save_runtime_config
from scope_recall.doctor_common import load_runtime_config as doctor_load_runtime_config

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _path_tail(path: str) -> tuple[str, ...]:
    return tuple(str(path).replace("\\", "/").split("/")[-2:])


def test_load_runtime_config_reports_malformed_storage_json(tmp_path: Path):
    plugin_dir = tmp_path / "plugin"
    storage_dir = tmp_path / "scope-recall"
    plugin_dir.mkdir()
    storage_dir.mkdir()
    (plugin_dir / "config.json").write_text(json.dumps({"auto_recall": False}), encoding="utf-8")
    (storage_dir / "config.json").write_text('{"vector": ', encoding="utf-8")

    config = load_runtime_config(plugin_dir, storage_dir)
    errors = load_runtime_config_errors(config)

    assert config["auto_recall"] is False
    assert errors
    assert errors[0]["kind"] == "json_decode"
    assert _path_tail(errors[0]["path"]) == ("scope-recall", "config.json")
    assert "message" in errors[0]


def test_load_runtime_config_reports_non_dict_payload(tmp_path: Path):
    plugin_dir = tmp_path / "plugin"
    storage_dir = tmp_path / "scope-recall"
    plugin_dir.mkdir()
    storage_dir.mkdir()
    (plugin_dir / "config.json").write_text("[]", encoding="utf-8")

    config = load_runtime_config(plugin_dir, storage_dir)
    errors = load_runtime_config_errors(config)

    assert errors == [
        {
            "path": str(plugin_dir / "config.json"),
            "kind": "non_dict_payload",
            "message": "config payload must be a JSON object",
        }
    ]


def test_doctor_runtime_config_surfaces_config_load_errors(tmp_path: Path):
    source_root = tmp_path / "source"
    hermes_home = tmp_path / "home"
    source_root.mkdir()
    storage_dir = hermes_home / "scope-recall"
    storage_dir.mkdir(parents=True)
    (source_root / "config.json").write_text(json.dumps({"vector": {"enabled": False}}), encoding="utf-8")
    (storage_dir / "config.json").write_text("{bad-json", encoding="utf-8")

    config = doctor_load_runtime_config(source_root, hermes_home)

    errors = config.get("_config_load_errors")
    assert isinstance(errors, list)
    assert errors[0]["kind"] == "json_decode"
    assert _path_tail(errors[0]["path"]) == ("scope-recall", "config.json")


def test_save_runtime_config_does_not_persist_internal_diagnostics(tmp_path: Path):
    hermes_home = tmp_path / "home"

    save_runtime_config(
        {
            "_config_load_errors": [{"path": "leak", "kind": "synthetic", "message": "should-not-persist"}],
            "_runtime_state.enabled": True,
            "vector.enabled": False,
            "vector._internal_note": "drop me",
        },
        str(hermes_home),
    )

    persisted = json.loads((hermes_home / "scope-recall" / "config.json").read_text(encoding="utf-8"))

    assert "_config_load_errors" not in persisted
    assert "_runtime_state" not in persisted
    assert persisted["vector"]["enabled"] is False
    assert "_internal_note" not in persisted["vector"]


def test_doctor_cli_reports_config_load_errors(tmp_path: Path):
    hermes_home = tmp_path / "home"
    storage_dir = hermes_home / "scope-recall"
    storage_dir.mkdir(parents=True)
    (storage_dir / "config.json").write_text("{bad-json", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "scripts" / "doctor.py"),
            "--source-root",
            str(PLUGIN_ROOT),
            "--hermes-home",
            str(hermes_home),
            "--json",
        ],
        cwd=PLUGIN_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["checks"]["config_load"]["ok"] is False
    assert payload["runtime"]["config_load"]["errors"][0]["kind"] == "json_decode"
    assert any("config" in item.lower() for item in payload["recommendations"])
