"""Final local validation runner boundary and coverage contracts."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run.release_validation.py"
REHEARSAL_SPEC = ROOT / "scripts" / "release.candidate_rehearsals.json"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "scope_recall_release_validation_runner_test",
        SCRIPT,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runner_maps_every_canonical_rehearsal_node_once() -> None:
    module = _load_module()
    spec = json.loads(REHEARSAL_SPEC.read_text(encoding="utf-8"))
    expected = {
        node
        for gate in spec["gates"]
        for node in gate["node_ids"]
    }
    actual = [
        node
        for node_ids in module.REHEARSAL_RECEIPTS.values()
        for node in node_ids
    ]

    assert set(actual) == expected
    assert len(actual) == len(set(actual))


def test_runner_environment_keeps_every_write_target_isolated(
    tmp_path: Path,
) -> None:
    module = _load_module()
    boundary = tmp_path / "boundary"
    active = tmp_path / "active"
    real_home = tmp_path / "real-home"

    environment = module._isolated_environment(
        boundary,
        active_hermes_home=active,
        real_home=real_home,
    )

    for name in (
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "TEMP",
        "TMP",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "PIP_CACHE_DIR",
        "SCOPE_RECALL_TEST_BOUNDARY_PARENT",
        "HERMES_HOME",
        "SCOPE_RECALL_DB",
        "SCOPE_RECALL_LOG_DIR",
        "SCOPE_RECALL_LEASE_DIR",
        "SCOPE_RECALL_PLUGIN_DIR",
    ):
        Path(environment[name]).resolve().relative_to(boundary.resolve())
    plugin_dir = Path(environment["SCOPE_RECALL_PLUGIN_DIR"])
    assert plugin_dir.parent.is_dir()
    assert not plugin_dir.exists()
    assert environment["SCOPE_RECALL_REAL_HOME"] == str(real_home)
    assert environment["SCOPE_RECALL_ACTIVE_HERMES_HOME"] == str(active)


def test_validation_receipt_binds_source_artifact_and_isolation() -> None:
    module = _load_module()
    context = module.ValidationContext(
        source_commit="a" * 40,
        source_tree="b" * 40,
        wheel_sha256="c" * 64,
        sdist_sha256="d" * 64,
    )

    receipt = module._receipt(
        context,
        stage={
            "started_at": "2026-08-28T00:00:00+00:00",
            "finished_at": "2026-08-28T00:00:01+00:00",
            "exit_code": 0,
            "log_sha256": "e" * 64,
        },
        command=["python", "isolated-check.py"],
        database_kind="fixture-copy",
    )

    assert receipt["source_commit"] == "a" * 40
    assert receipt["source_tree"] == "b" * 40
    assert receipt["artifact_sha256"] == "c" * 64
    assert receipt["result"] == "passed"
    assert receipt["environment_boundary"] == {
        "hermes_home_kind": "isolated",
        "database_kind": "fixture-copy",
        "active_instance_touched": False,
    }


def test_validation_script_path_entrypoint_is_importable() -> None:
    result = subprocess.run(
        [sys.executable, "-B", str(SCRIPT), "--help"],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout
    assert "--hermes-0-20-6-source" in result.stdout
