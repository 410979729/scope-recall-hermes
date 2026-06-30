"""Tests that doctor wrapper exports and modular doctor files stay compatible.

They allow doctor internals to be split without breaking existing operator imports."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DOCTOR_SCRIPT = PLUGIN_ROOT / "scripts" / "doctor.py"


def test_doctor_cli_is_thin_wrapper():
    tree = ast.parse(DOCTOR_SCRIPT.read_text(encoding="utf-8"))
    top_level_functions = [node.name for node in tree.body if isinstance(node, ast.FunctionDef)]

    assert top_level_functions == ["parse_args", "_index_general_enabled", "main"]

    source = DOCTOR_SCRIPT.read_text(encoding="utf-8")
    for module_name in (
        "doctor_common",
        "doctor_source",
        "doctor_sqlite",
        "doctor_journal",
        "doctor_vector",
        "doctor_experience",
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
        "doctor_source": ["source_report"],
        "doctor_sqlite": ["sqlite_report", "memory_candidate_debt_report", "memory_secret_report"],
        "doctor_journal": ["journal_enabled_from_config", "journal_report"],
        "doctor_vector": ["vector_report", "disabled_vector_report"],
        "doctor_experience": ["experience_config_summary", "experience_report", "nightly_digest_report"],
    }

    for module_name, function_names in expected.items():
        module = importlib.import_module(f"scope_recall.{module_name}")
        for function_name in function_names:
            assert callable(getattr(module, function_name))
