"""Tests that doctor wrapper exports and modular doctor files stay compatible.

They allow doctor internals to be split without breaking existing operator imports."""

from __future__ import annotations

import ast
import importlib
import json
import subprocess
import sys
from pathlib import Path

from scope_recall.response_schemas import DOCTOR_REQUIRED_CHECK_NAMES

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DOCTOR_SCRIPT = PLUGIN_ROOT / "scripts" / "doctor.py"
INSTALLER_MODULE = PLUGIN_ROOT / "installer.py"


def test_doctor_cli_is_thin_wrapper():
    tree = ast.parse(DOCTOR_SCRIPT.read_text(encoding="utf-8"))
    top_level_functions = [node.name for node in tree.body if isinstance(node, ast.FunctionDef)]

    assert top_level_functions == ["parse_args", "_index_general_enabled", "main"]

    source = DOCTOR_SCRIPT.read_text(encoding="utf-8")
    for module_name in (
        "doctor_common",
        "doctor_endpoint",
        "doctor_source",
        "doctor_sqlite",
        "doctor_journal",
        "doctor_vector",
        "doctor_experience",
        "doctor_extensions",
    ):
        assert module_name in source


def test_doctor_import_fallback_only_catches_import_error():
    tree = ast.parse(DOCTOR_SCRIPT.read_text(encoding="utf-8"))
    handlers = [node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)]

    assert handlers
    assert all(isinstance(handler.type, ast.Name) and handler.type.id == "ImportError" for handler in handlers)


def test_doctor_modules_importable_from_source_checkout():
    expected = {
        "graph_hygiene": ["graph_hygiene_counts", "delete_graph_hygiene_rows", "repair_graph_hygiene"],
        "doctor_common": ["load_runtime_config", "expected_embedder_from_config", "vector_backend_from_config"],
        "doctor_endpoint": ["endpoint_policy_report"],
        "doctor_source": ["source_report"],
        "doctor_sqlite": ["sqlite_report", "memory_candidate_debt_report", "memory_secret_report"],
        "doctor_journal": ["journal_enabled_from_config", "journal_report"],
        "doctor_vector": ["vector_report", "disabled_vector_report"],
        "doctor_experience": ["experience_config_summary", "experience_report", "nightly_digest_report"],
        "doctor_extensions": ["extension_report"],
    }

    for module_name, function_names in expected.items():
        module = importlib.import_module(f"scope_recall.{module_name}")
        for function_name in function_names:
            assert callable(getattr(module, function_name))


def test_doctor_required_check_registry_is_single_source_of_truth():
    assert DOCTOR_REQUIRED_CHECK_NAMES == tuple(sorted(set(DOCTOR_REQUIRED_CHECK_NAMES)))
    assert "endpoint_policy" in DOCTOR_REQUIRED_CHECK_NAMES

    doctor_source = DOCTOR_SCRIPT.read_text(encoding="utf-8")
    installer_source = INSTALLER_MODULE.read_text(encoding="utf-8")
    assert "set(DOCTOR_REQUIRED_CHECK_NAMES)" in doctor_source
    assert "for name in DOCTOR_REQUIRED_CHECK_NAMES" in installer_source
    assert "REQUIRED_POSTDEPLOY_DOCTOR_CHECKS = (" not in installer_source


def test_doctor_reports_writer_handoff_config_without_claiming_live_state(tmp_path):
    completed = subprocess.run(
        [
            sys.executable,
            str(DOCTOR_SCRIPT),
            "--json",
            "--source-root",
            str(PLUGIN_ROOT),
            "--hermes-home",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    payload = json.loads(completed.stdout)
    handoff = payload["runtime"]["writer_handoff"]
    assert handoff["writer_lease_scope"] == "process-wide-os-lock"
    assert handoff["idle_release_enabled"] is True
    assert handoff["idle_release_seconds"] == 1800.0
    assert handoff["snapshot_kind"] == "offline_config_only"
    assert handoff["runtime_state_observed"] is False
    assert handoff["live_counters"]["source"] == "scope_recall_stats"
    assert handoff["live_counters"]["observed"] is False
    assert "writer_role" in handoff["live_counters"]["fields"]
    assert "last_handoff_reason_code" in handoff["live_counters"]["fields"]
    assert "writer_role" not in handoff
