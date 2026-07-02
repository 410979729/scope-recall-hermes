"""Comprehensive release-gate tests for packaging, metadata, workflows, docs, and public contracts.

This file encodes what must stay true before a tag can be published."""

import importlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import types
from pathlib import Path

import lancedb
import pyarrow as pa
import pytest

from plugins.memory import load_memory_provider

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "import.openclaw.memory_lancedb_pro.py"
DOCTOR_PATH = Path(__file__).resolve().parents[1] / "scripts" / "doctor.py"
REPAIR_PATH = Path(__file__).resolve().parents[1] / "scripts" / "repair.vector_index.py"
CHECK_RELEASE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check.release.py"
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "scope_recall"
if PACKAGE_NAME not in sys.modules:
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(PLUGIN_ROOT)]
    sys.modules[PACKAGE_NAME] = package

embedders_module = importlib.import_module(f"{PACKAGE_NAME}.embedders")
build_embedder = embedders_module.build_embedder


def _write_local_debug_vector_config(hermes_home: Path) -> None:
    config_path = hermes_home / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "vector": {
                    "embedder": {"provider": "local-debug", "dimensions": 16, "model": "debug-hash-v1"},
                    "fallback_embedder": {"provider": "local-debug", "dimensions": 16, "model": "debug-hash-v1"},
                }
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


class _FakeSentenceTransformer:
    def __init__(self, model: str, **kwargs):
        self.model = model
        self.kwargs = kwargs

    def get_embedding_dimension(self) -> int:
        return 384

    def encode(self, items, *, normalize_embeddings=True, convert_to_numpy=True):
        del normalize_embeddings, convert_to_numpy
        return [[1.0, *([0.0] * 383)] for _ in items]


def _install_fake_sentence_transformer(monkeypatch):
    try:
        import sentence_transformers as sentence_transformers_pkg

        monkeypatch.setattr(sentence_transformers_pkg, "SentenceTransformer", _FakeSentenceTransformer, raising=False)
    except Exception:
        pass
    monkeypatch.setattr(embedders_module, "SentenceTransformer", _FakeSentenceTransformer)
    embedders_module._SENTENCE_TRANSFORMER_CACHE.clear()
    for module in list(sys.modules.values()):
        if getattr(module, "__file__", "") and str(getattr(module, "__file__", "")).endswith("/embedders.py"):
            monkeypatch.setattr(module, "SentenceTransformer", _FakeSentenceTransformer, raising=False)
            cache = getattr(module, "_SENTENCE_TRANSFORMER_CACHE", None)
            if isinstance(cache, dict):
                cache.clear()



def _package_version() -> str:
    import tomllib

    return tomllib.loads((PLUGIN_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def _load_release_check_module(module_name: str = "scope_recall_check_release_contract"):
    spec = importlib.util.spec_from_file_location(module_name, CHECK_RELEASE_PATH)
    assert spec is not None
    assert spec.loader is not None
    release_check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release_check)
    return release_check



def test_release_environment_check_reports_interpreter_and_required_modules(monkeypatch):
    release_check = _load_release_check_module("scope_recall_check_release_environment")

    monkeypatch.setattr(release_check.importlib.util, "find_spec", lambda name: object() if name != "lancedb" else None)

    report = release_check.release_environment_check()

    assert report["ok"] is False
    assert report["python_executable"] == sys.executable
    assert report["required_modules"]["pytest"] is True
    assert report["required_modules"]["lancedb"] is False
    assert report["missing_modules"] == ["lancedb"]
    assert report["install_command"] == "python -m pip install -e '.[dev,all]'"


def test_public_response_contract_files_are_release_packaged():
    release_check = _load_release_check_module("scope_recall_check_release_response_contracts")

    assert "response_schemas.py" in release_check.REQUIRED_SOURCE_FILES
    assert "docs/response-contracts.md" in release_check.REQUIRED_SOURCE_FILES
    assert "scope_recall/response_schemas.py" in release_check.REQUIRED_WHEEL
    assert "scope_recall/docs/response-contracts.md" in release_check.REQUIRED_WHEEL


def test_release_readiness_note_is_release_packaged():
    release_check = _load_release_check_module("scope_recall_check_release_readiness_doc")

    expected_source = f"docs/release-readiness.{release_check.PACKAGE_VERSION}.md"
    expected_wheel = f"scope_recall/{expected_source}"
    assert release_check.RELEASE_READINESS_DOC == expected_source
    assert expected_source in release_check.REQUIRED_SOURCE_FILES
    assert expected_wheel in release_check.REQUIRED_WHEEL


def test_internal_plan_docs_are_not_release_contracts():
    release_check = _load_release_check_module("scope_recall_check_release_public_docs")

    assert "docs/upstream-recommendation.md" in release_check.REQUIRED_SOURCE_FILES
    assert "scope_recall/docs/upstream-recommendation.md" in release_check.REQUIRED_WHEEL
    assert "docs/hermes-upstream-recommendation-plan.md" not in release_check.REQUIRED_SOURCE_FILES
    assert "scope_recall/docs/hermes-upstream-recommendation-plan.md" not in release_check.REQUIRED_WHEEL
    assert (PLUGIN_ROOT / "docs" / "plans").exists() is False
    assert release_check.public_doc_hygiene_check()["ok"] is True


def test_public_doc_hygiene_blocks_private_plan_markers(tmp_path, monkeypatch):
    release_check = _load_release_check_module("scope_recall_check_release_private_doc_markers")
    docs_plans = tmp_path / "docs" / "plans"
    docs_plans.mkdir(parents=True)
    (tmp_path / "README.md").write_text("The product promise Joy cares about\n", encoding="utf-8")
    (docs_plans / "internal.md").write_text("由插件/玉衡自动提取，不需要 Joy 人工复审。\n", encoding="utf-8")
    monkeypatch.setattr(release_check, "ROOT", tmp_path)

    result = release_check.public_doc_hygiene_check()

    assert result["ok"] is False
    assert result["forbidden_paths"] == ["docs/plans/internal.md"]
    markers = {(finding["path"], finding["marker"]) for finding in result["findings"]}
    assert ("README.md", "personal_name_joy") in markers
    assert ("README.md", "private_product_promise") in markers
    assert ("docs/plans/internal.md", "agent_persona_yuheng") in markers
    assert ("docs/plans/internal.md", "manual_review_private_context") in markers


def test_distribution_hygiene_blocks_plan_artifacts():
    release_check = _load_release_check_module("scope_recall_check_release_distribution_hygiene")

    names = {
        "scope_recall/docs/upstream-recommendation.md",
        "scope_recall/docs/plans/internal.md",
        "scope_recall/docs/hermes-upstream-recommendation-plan.md",
    }

    assert release_check.forbidden_distribution_entries(names) == [
        "scope_recall/docs/hermes-upstream-recommendation-plan.md",
        "scope_recall/docs/plans/internal.md",
    ]


def test_changelog_completeness_gate_requires_current_release_terms():
    release_check = _load_release_check_module("scope_recall_check_release_changelog")

    empty_current = "# Changelog\n\n## [1.6.1] - 2026-06-30\n\n## [1.5.3] - 2026-06-26\n"
    failed = release_check.changelog_completeness_check(empty_current)
    assert failed["ok"] is False
    assert failed["section_found"] is True
    assert "governance" in failed["missing_terms"]
    assert "journal recovery" in failed["missing_terms"]

    complete = "# Changelog\n\n## [1.6.1] - 2026-06-30\n" + "\n".join(release_check.REQUIRED_CHANGELOG_TERMS)
    assert release_check.changelog_completeness_check(complete)["ok"] is True


def test_live_dashboard_waiver_check_detects_stale_snapshot():
    release_check = _load_release_check_module("scope_recall_check_release_live_waiver")
    dashboard = {
        "ok": True,
        "severity": "DEGRADED",
        "summary": {
            "journal_unprocessed": 724,
            "journal_dead_letter_replay_candidates": 272,
            "journal_llm_quarantine_runs": 4,
            "journal_digest_status": "degraded",
            "experience_duplicate_groups": 2,
            "experience_needs_review": 5,
            "memory_quality_active_hits": 0,
            "memory_secret_active": 0,
            "vector_status": "ready",
            "schema_migration_current": True,
        },
    }
    stale_readiness = """
Date: 2026-06-29

## Live dashboard waiver

Current read-only snapshot from the local Hermes home at audit time:

- `ok=true`
- `severity=DEGRADED`
- `journal_unprocessed=701`
- `journal_dead_letter_replay_candidates=272`
- `journal_llm_quarantine_runs=4`
- `journal_digest_status=degraded`
- `experience_duplicate_groups=2`
- `experience_needs_review=5`
- `memory_quality_active_hits=0`
- `memory_secret_active=0`
- `vector_status=ready`
- `schema_migration_current=true`

Reason:

Clearance condition: show `severity=OK` after live recovery.
"""

    snapshot = release_check.release_readiness_snapshot_values(stale_readiness)
    assert snapshot["severity"] == "DEGRADED"

    result = release_check.live_dashboard_waiver_check(dashboard, stale_readiness)

    assert result["ok"] is False
    assert result["live_ok"] is False
    assert result["waiver_used"] is True
    assert result["mismatches"] == [{"field": "journal_unprocessed", "recorded": "701", "current": "724"}]

    accepted = release_check.live_dashboard_waiver_check(dashboard, stale_readiness, accept_stale=True)
    assert accepted["ok"] is True
    assert accepted["accept_stale"] is True


def test_live_dashboard_file_check_is_disabled_without_explicit_payload():
    release_check = _load_release_check_module("scope_recall_check_release_live_disabled")

    assert release_check.live_dashboard_file_check("") == {"ok": True, "enabled": False}


def test_response_contract_doc_lists_public_schema_versions():
    from scope_recall.response_schemas import PUBLIC_RESPONSE_SCHEMA_VERSIONS

    doc = (PLUGIN_ROOT / "docs" / "response-contracts.md").read_text(encoding="utf-8")
    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/response-contracts.md" in readme
    assert "docs/response-contracts.md" in (PLUGIN_ROOT / "docs" / "contract.matrix.md").read_text(encoding="utf-8")
    for schema_version in PUBLIC_RESPONSE_SCHEMA_VERSIONS.values():
        assert schema_version in doc


def test_historical_release_readiness_is_marked_not_current():
    historical = (PLUGIN_ROOT / "docs" / "release-readiness.1.4.0.md").read_text(encoding="utf-8")
    upstream = (PLUGIN_ROOT / "docs" / "upstream-recommendation.md").read_text(encoding="utf-8")

    assert "Historical note" in historical
    assert "not the current release checklist" in historical
    assert "standalone-provider visibility" in upstream
    assert "after the `v1.4.0` release tag is created" not in upstream
    assert "Joy" not in upstream


def test_readme_public_version_matches_package_metadata():
    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
    version = _package_version()

    assert f"Version `{version}`" in readme
    assert "Version `1.0.6`" not in readme


def test_default_config_includes_documented_retrieval_top_k():
    config_module = importlib.import_module(f"{PACKAGE_NAME}.config")
    source_config = json.loads((PLUGIN_ROOT / "config.json").read_text(encoding="utf-8"))

    assert source_config["retrieval"]["top_k"] == 5
    assert config_module.DEFAULT_CONFIG["retrieval"]["top_k"] == source_config["retrieval"]["top_k"]
    assert source_config["tool_schema_profile"] == "compact"
    assert config_module.DEFAULT_CONFIG["tool_schema_profile"] == "compact"
    assert source_config["tool_schema_extra_tools"] == []
    assert config_module.DEFAULT_CONFIG["tool_schema_extra_tools"] == []


def test_scope_recall_stats_reports_journal_digest_health(tmp_path, monkeypatch):
    config_path = tmp_path / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({"journal": {"background_digest_enabled": False}}, ensure_ascii=False) + "\n", encoding="utf-8")
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-journal-health",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        stats = json.loads(plugin.handle_tool_call("scope_recall_stats", {}))
        assert stats["journal_digest"]["last_status"] == "never_run"
        assert stats["journal_digest"]["consecutive_failures"] == 0

        module = sys.modules[plugin.__class__.__module__]

        def fail_digest(**_kwargs):
            return {"ok": False, "status": "error", "error": "simulated digest failure at /home/a/private"}

        monkeypatch.setattr(module, "run_journal_digest", fail_digest)
        plugin._run_background_journal_digest({"extractor": "heuristic", "max_entries_per_digest": 1})
        stats = json.loads(plugin.handle_tool_call("scope_recall_stats", {}))
        assert stats["journal_digest"]["last_status"] == "error"
        assert stats["journal_digest"]["consecutive_failures"] == 1
        assert "[REDACTED_PATH]" in stats["journal_digest"]["last_error"]
    finally:
        plugin.shutdown()



def test_readme_documents_hermes_venv_test_command():
    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")

    assert "PYTHONPATH=/path/to/hermes-agent:" in readme
    assert "venv/bin/python -m pytest -q" in readme
    assert "Plain `pytest` from an unrelated Python environment" in readme


def test_readme_documents_artifact_anchors_and_secret_indexes():
    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")

    assert "Artifact anchors:" in readme
    assert "scope_recall_store_secret_index" in readme
    assert "secret_value_stored" in readme
    assert "plaintext secret" in readme
    assert "SQL/FTS/vector" in readme


def test_release_git_tree_check_ignores_ci_runtime_checkout(monkeypatch):
    spec = importlib.util.spec_from_file_location("scope_recall_check_release", CHECK_RELEASE_PATH)
    assert spec is not None
    assert spec.loader is not None
    release_check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release_check)

    monkeypatch.setattr(
        release_check,
        "run",
        lambda _cmd: {
            "returncode": 0,
            "stdout": "?? .hermes-agent-src/\n?? docs/new.md\n M README.md\n",
            "stderr": "",
        },
    )

    result = release_check.git_tree_check(allow_dirty=False)

    assert result["ok"] is False
    assert "?? .hermes-agent-src/" not in result["untracked"]
    assert "?? docs/new.md" in result["untracked"]
    assert " M README.md" in result["dirty"]


def test_release_git_tree_check_allows_only_known_scratch(monkeypatch):
    spec = importlib.util.spec_from_file_location("scope_recall_check_release_scratch", CHECK_RELEASE_PATH)
    assert spec is not None
    assert spec.loader is not None
    release_check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release_check)

    monkeypatch.setattr(
        release_check,
        "run",
        lambda _cmd: {"returncode": 0, "stdout": "?? .hermes-agent-src/\n?? .hermes/\n?? build/\n", "stderr": ""},
    )

    result = release_check.git_tree_check(allow_dirty=False)

    assert result == {"ok": True, "allow_dirty": False, "dirty": [], "untracked": []}


def test_doctor_script_reports_source_versions():

    result = subprocess.run(
        [sys.executable, str(DOCTOR_PATH), "--source-root", str(PLUGIN_ROOT)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    version = _package_version()
    assert payload["ok"] is True
    assert payload["schema_version"] == "doctor_report.v1"
    assert payload["source"]["pyproject_version"] == version
    assert payload["source"]["plugin_version"] == version
    assert payload["source"]["readme_public_versions"] == [version]
    assert payload["checks"]["source_metadata"]["ok"] is True



def test_doctor_script_accepts_explicit_json_flag():
    result = subprocess.run(
        [sys.executable, str(DOCTOR_PATH), "--json", "--source-root", str(PLUGIN_ROOT)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True



def test_doctor_script_reports_missing_sqlite_truth_db(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(DOCTOR_PATH),
            "--source-root",
            str(PLUGIN_ROOT),
            "--hermes-home",
            str(tmp_path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["runtime"]["sqlite"]["status"] == "missing"
    assert "repair.vector_index.py" in "\n".join(payload["recommendations"])


def test_doctor_script_reports_missing_journal_schema(tmp_path):
    storage = tmp_path / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(json.dumps({"vector": {"enabled": False}}), encoding="utf-8")
    conn = sqlite3.connect(storage / "memory.sqlite3")
    try:
        from scope_recall.sql_store import ensure_schema  # type: ignore[import-not-found]

        ensure_schema(conn)
    finally:
        conn.close()

    result = subprocess.run(
        [
            sys.executable,
            str(DOCTOR_PATH),
            "--source-root",
            str(PLUGIN_ROOT),
            "--hermes-home",
            str(tmp_path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["checks"]["journal_provenance"]["ok"] is False
    assert payload["runtime"]["journal"]["status"] == "schema_missing"



def test_doctor_vector_report_accepts_lancedb_list_tables_dict_response(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("scope_recall_doctor", DOCTOR_PATH)
    assert spec is not None
    assert spec.loader is not None
    doctor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doctor)

    vector_dir = tmp_path / "scope-recall" / "lancedb"
    vector_dir.mkdir(parents=True)

    class FakeTable:
        def count_rows(self):
            return 7

    class FakeDB:
        def list_tables(self):
            return {"tables": ["memories"], "page_token": None}

        def open_table(self, name):
            assert name == "memories"
            return FakeTable()

    fake_lancedb = types.SimpleNamespace(connect=lambda path: FakeDB())
    monkeypatch.setitem(sys.modules, "lancedb", fake_lancedb)

    payload, check, recommendations = doctor.vector_report(tmp_path)

    assert payload["status"] == "ready"
    assert payload["tables"] == ["memories"]
    assert payload["row_count"] == 7
    assert check["ok"] is True
    assert recommendations == []



def test_doctor_vector_report_marks_search_smoke_failure_needs_repair(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("scope_recall_doctor", DOCTOR_PATH)
    assert spec is not None
    assert spec.loader is not None
    doctor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doctor)

    vector_dir = tmp_path / "scope-recall" / "lancedb"
    vector_dir.mkdir(parents=True)

    class FakeVectorType:
        list_size = 3

    class FakeField:
        type = FakeVectorType()

    class FakeSchema:
        def field(self, name):
            assert name == "vector"
            return FakeField()

    class FakeTable:
        schema = FakeSchema()

        def count_rows(self):
            return 7

        def search(self, vector):
            raise RuntimeError("missing lance fragment")

    class FakeDB:
        def list_tables(self):
            return {"tables": ["memories"], "page_token": None}

        def open_table(self, name):
            assert name == "memories"
            return FakeTable()

    fake_lancedb = types.SimpleNamespace(connect=lambda path: FakeDB())
    monkeypatch.setitem(sys.modules, "lancedb", fake_lancedb)

    payload, check, recommendations = doctor.vector_report(tmp_path)

    assert payload["status"] == "needs_repair"
    assert payload["ready"] is False
    assert "missing lance fragment" in payload["error"]
    assert check["ok"] is False
    assert "repair.vector_index.py" in "\n".join(recommendations)



def test_doctor_vector_report_marks_dimension_mismatch_needs_repair(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("scope_recall_doctor", DOCTOR_PATH)
    assert spec is not None
    assert spec.loader is not None
    doctor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doctor)

    vector_dir = tmp_path / "scope-recall" / "lancedb"
    vector_dir.mkdir(parents=True)

    class FakeVectorType:
        list_size = 256

    class FakeField:
        type = FakeVectorType()

    class FakeSchema:
        def field(self, name):
            assert name == "vector"
            return FakeField()

    class FakeQuery:
        def limit(self, value):
            assert value == 1
            return self

        def to_list(self):
            return []

    class FakeTable:
        schema = FakeSchema()

        def count_rows(self):
            return 7

        def search(self, vector):
            assert len(vector) == 256
            return FakeQuery()

    class FakeDB:
        def list_tables(self):
            return {"tables": ["memories"], "page_token": None}

        def open_table(self, name):
            assert name == "memories"
            return FakeTable()

    fake_lancedb = types.SimpleNamespace(connect=lambda path: FakeDB())
    monkeypatch.setitem(sys.modules, "lancedb", fake_lancedb)

    expected_embedder = {"source": "embedder", "provider": "openai-compatible", "model": "gemini-embedding-001", "dimensions": 3072}
    payload, check, recommendations = doctor.vector_report(tmp_path, expected_embedder=expected_embedder)

    assert payload["status"] == "needs_repair"
    assert payload["ready"] is False
    assert payload["dimensions"] == 256
    assert payload["expected_embedder"]["dimensions"] == 3072
    assert "dimension mismatch" in payload["error"]
    assert check["ok"] is False
    assert "repair.vector_index.py" in "\n".join(recommendations)


def test_doctor_expected_embedder_loads_profile_dotenv_before_fallback(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("scope_recall_doctor", DOCTOR_PATH)
    assert spec is not None
    assert spec.loader is not None
    doctor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doctor)

    monkeypatch.delenv("SCOPE_RECALL_GEMINI_EMBEDDING_API_KEY", raising=False)
    (tmp_path / ".env").write_text("SCOPE_RECALL_GEMINI_EMBEDDING_API_KEY=test-key\n", encoding="utf-8")
    storage = tmp_path / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(
        json.dumps(
            {
                "vector": {
                    "enabled": True,
                    "embedder": {
                        "provider": "openai-compatible",
                        "dimensions": 3072,
                        "model": "gemini-embedding-001",
                        "api_key_env": ["SCOPE_RECALL_GEMINI_EMBEDDING_API_KEY"],
                    },
                    "fallback_embedder": {"provider": "local-hash", "dimensions": 256, "model": "hash-v1"},
                }
            }
        ),
        encoding="utf-8",
    )

    config = doctor.load_runtime_config(PLUGIN_ROOT, tmp_path)
    expected = doctor.expected_embedder_from_config(config)

    assert expected["source"] == "embedder"
    assert expected["dimensions"] == 3072
    assert "SCOPE_RECALL_GEMINI_EMBEDDING_API_KEY" not in os.environ



def test_doctor_experience_config_summary_reports_auto_promotion_flag():
    spec = importlib.util.spec_from_file_location("scope_recall_doctor", DOCTOR_PATH)
    assert spec is not None
    assert spec.loader is not None
    doctor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doctor)

    payload = doctor.experience_config_summary(
        {
            "experience": {
                "enabled": True,
                "prefetch_enabled": True,
                "auto_promotion_enabled": True,
                "auto_promotion_limit_sessions": 20,
                "promotion_require_verification": True,
            },
            "vector": {"embedder": {"api_key": "should-not-appear"}},
        }
    )

    assert payload == {
        "enabled": True,
        "prefetch_enabled": True,
        "auto_promotion_enabled": True,
        "auto_promotion_limit_sessions": 20,
        "promotion_require_verification": True,
    }
    assert "should-not-appear" not in str(payload)


def test_doctor_nightly_digest_report_surfaces_fallback_and_recent_errors(tmp_path):
    spec = importlib.util.spec_from_file_location("scope_recall_doctor", DOCTOR_PATH)
    assert spec is not None
    assert spec.loader is not None
    doctor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doctor)

    storage = tmp_path / "scope-recall"
    storage.mkdir(parents=True)
    conn = sqlite3.connect(storage / "memory.sqlite3")
    try:
        conn.execute(
            """
            CREATE TABLE nightly_digest_runs (
                id TEXT PRIMARY KEY,
                digest_date TEXT NOT NULL,
                source_db TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                extractor TEXT NOT NULL,
                model TEXT,
                dry_run INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                inserted INTEGER NOT NULL DEFAULT 0,
                updated INTEGER NOT NULL DEFAULT 0,
                skipped INTEGER NOT NULL DEFAULT 0,
                deleted INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                metadata TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        rows = [
            ("run-1", "2026-06-17", "memory.sqlite3", "2026-06-17T15:00:00+00:00", "2026-06-17T15:01:00+00:00", "llm", "model", 0, "error", 0, 0, 0, 0, "The read operation timed out", "{}"),
            ("run-2", "2026-06-18", "memory.sqlite3", "2026-06-18T15:00:00+00:00", "2026-06-18T15:01:00+00:00", "llm", "model", 0, "ok", 1, 0, 0, 0, None, "{}"),
            ("run-3", "2026-06-19", "memory.sqlite3", "2026-06-19T15:00:00+00:00", "2026-06-19T15:01:00+00:00", "llm", "model", 0, "ok_with_fallback", 2, 0, 0, 0, None, "{}"),
        ]
        conn.executemany(
            """
            INSERT INTO nightly_digest_runs (
                id, digest_date, source_db, started_at, finished_at, extractor, model, dry_run,
                status, inserted, updated, skipped, deleted, error, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()

    payload, check, recommendations = doctor.nightly_digest_report(tmp_path)

    assert payload["status"] == "degraded"
    assert payload["latest_run"]["status"] == "ok_with_fallback"
    assert payload["runs"]["by_status"] == {"error": 1, "ok": 1, "ok_with_fallback": 1}
    assert payload["consecutive_errors"] == 0
    assert check == {"ok": True, "failures": []}
    joined = "\n".join(recommendations)
    assert "fallback" in joined
    assert "Recent nightly digest errors" in joined


def test_doctor_vector_report_accepts_sqlite_bruteforce_backend(tmp_path):
    spec = importlib.util.spec_from_file_location("scope_recall_doctor", DOCTOR_PATH)
    assert spec is not None
    assert spec.loader is not None
    doctor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doctor)

    from scope_recall.sqlite_vector_store import SQLiteBruteForceVectorStore  # type: ignore[import-not-found]

    store = SQLiteBruteForceVectorStore(tmp_path / "scope-recall" / "vector.sqlite3", table_name="memories", dimensions=2)
    store.open()
    try:
        store.upsert_records(
            [
                {
                    "id": "memory-1",
                    "scope_id": "scope-a",
                    "source": "tool-store",
                    "target": "memory",
                    "content": "non native vector backend",
                    "summary": "sqlite vector",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                    "vector": [1.0, 0.0],
                }
            ]
        )
    finally:
        store.close()

    expected_embedder = {"source": "embedder", "provider": "local-debug", "model": "debug", "dimensions": 2}
    payload, check, recommendations = doctor.vector_report(tmp_path, expected_embedder=expected_embedder, backend="sqlite-bruteforce")

    assert payload["backend"] == "sqlite-bruteforce"
    assert payload["status"] == "ready"
    assert payload["row_count"] == 1
    assert payload["dimensions"] == 2
    assert payload["search_smoke"] == "ok"
    assert check["ok"] is True
    assert recommendations == []



def test_repair_vector_index_rebuilds_sqlite_bruteforce_backend(tmp_path):
    from scope_recall.journal import ensure_journal_schema  # type: ignore[import-not-found]
    from scope_recall.sql_store import ensure_schema, store_row  # type: ignore[import-not-found]

    storage_dir = tmp_path / "scope-recall"
    storage_dir.mkdir(parents=True)
    (storage_dir / "config.json").write_text(
        json.dumps(
            {
                "vector": {
                    "enabled": True,
                    "backend": "sqlite-bruteforce",
                    "table_name": "memories",
                    "index_general": "false",
                    "embedder": {"provider": "local-debug", "dimensions": 16, "model": "debug-hash-v1"},
                },
                "retrieval": {"metric": "cosine"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    conn = sqlite3.connect(storage_dir / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        ensure_journal_schema(conn)
        store_row(
            conn,
            memory_id="memory-1",
            scope_id="scope-a",
            platform="cli",
            user_id="joy",
            chat_id="",
            thread_id="",
            gateway_session_key="",
            agent_identity="yuheng",
            agent_workspace="hermes",
            session_id="session",
            source="tool-store",
            target="memory",
            content="SQLite brute force repair rebuilds from SQLite truth.",
        )
        store_row(
            conn,
            memory_id="general-1",
            scope_id="scope-a",
            platform="cli",
            user_id="joy",
            chat_id="",
            thread_id="",
            gateway_session_key="",
            agent_identity="yuheng",
            agent_workspace="hermes",
            session_id="session",
            source="turn-user",
            target="general",
            content="General scratch rows stay out of vector repair when index_general is string false.",
        )
    finally:
        conn.close()

    result = subprocess.run(
        [sys.executable, str(REPAIR_PATH), "--hermes-home", str(tmp_path), "--apply", "--no-backup"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is False
    assert payload["vector_backend"] == "sqlite-bruteforce"
    assert payload["vector_path"].endswith("scope-recall/vector.sqlite3")
    assert payload["rows"] == 1
    assert payload["audit"] == {"physical_rows": 1, "unique_ids": 1, "duplicate_rows": 0, "duplicate_ids": 0}

    result = subprocess.run(
        [sys.executable, str(DOCTOR_PATH), "--source-root", str(PLUGIN_ROOT), "--hermes-home", str(tmp_path)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    doctor_payload = json.loads(result.stdout)
    assert doctor_payload["runtime"]["vector_backend"] == "sqlite-bruteforce"
    assert doctor_payload["runtime"]["vector"]["status"] == "ready"



def test_doctor_expected_embedder_prefers_available_primary(monkeypatch):
    spec = importlib.util.spec_from_file_location("scope_recall_doctor", DOCTOR_PATH)
    assert spec is not None
    assert spec.loader is not None
    doctor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doctor)

    monkeypatch.setenv("SCOPE_RECALL_TEST_EMBEDDING_KEY", "present")
    config = {
        "vector": {
            "embedder": {
                "provider": "openai-compatible",
                "model": "gemini-embedding-001",
                "dimensions": 3072,
                "api_key_env": ["SCOPE_RECALL_TEST_EMBEDDING_KEY"],
            },
            "fallback_embedder": {"provider": "local-hash", "model": "hash-v1", "dimensions": 256},
        }
    }

    payload = doctor.expected_embedder_from_config(config)

    assert payload["source"] == "embedder"
    assert payload["provider"] == "openai-compatible"
    assert payload["model"] == "gemini-embedding-001"
    assert payload["dimensions"] == 3072



def test_doctor_expected_embedder_uses_fallback_when_primary_unavailable(monkeypatch):
    spec = importlib.util.spec_from_file_location("scope_recall_doctor", DOCTOR_PATH)
    assert spec is not None
    assert spec.loader is not None
    doctor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doctor)

    monkeypatch.delenv("SCOPE_RECALL_TEST_EMBEDDING_KEY", raising=False)
    config = {
        "vector": {
            "embedder": {
                "provider": "openai-compatible",
                "model": "gemini-embedding-001",
                "dimensions": 3072,
                "api_key_env": ["SCOPE_RECALL_TEST_EMBEDDING_KEY"],
            },
            "fallback_embedder": {"provider": "local-hash", "model": "hash-v1", "dimensions": 256},
        }
    }

    payload = doctor.expected_embedder_from_config(config)

    assert payload["source"] == "fallback_embedder"
    assert payload["provider"] == "local-hash"
    assert payload["model"] == "hash-v1"
    assert payload["dimensions"] == 256



def test_plugin_manifest_contract_matches_provider_lifecycle_hooks():
    release_check = _load_release_check_module("scope_recall_check_release_manifest_contract")

    manifest_hooks = set(release_check.parse_plugin_manifest_hooks((PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8")))
    provider_hooks = set(release_check.provider_lifecycle_hook_methods())

    assert manifest_hooks == provider_hooks
    assert {"on_pre_compress", "on_session_switch"} <= manifest_hooks



def test_release_gate_stable_tool_names_cover_schema_surfaces_and_dispatch():
    release_check = _load_release_check_module("scope_recall_check_release_tool_contract")
    surfaces = release_check.provider_tool_schema_names_by_surface()

    for surface in ("compact", "standard", "experience", "maintenance", "all_referenced"):
        assert set(surfaces[surface]) <= release_check.STABLE_TOOL_NAMES
    assert {"scope_recall_memory", "scope_recall_entity"} <= set(surfaces["compact"])
    assert release_check.STABLE_TOOL_NAMES <= set(release_check.tool_dispatcher_names())



def test_release_gate_product_contract_is_clean():
    release_check = _load_release_check_module("scope_recall_check_release_product_contract")

    product_contract = release_check.product_contract_check()

    assert product_contract["ok"] is True, product_contract["failures"]



def test_pypi_workflow_runs_release_gate_before_publish():
    pypi_workflow = (PLUGIN_ROOT / ".github" / "workflows" / "pypi.yml").read_text(encoding="utf-8")
    release_workflow = (PLUGIN_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "scripts/check.release.py" in pypi_workflow
    assert "python -m pip install --upgrade pip build \".[lancedb,dev]\"" in pypi_workflow
    assert pypi_workflow.index("scripts/check.release.py") < pypi_workflow.index("pypa/gh-action-pypi-publish")
    assert "  release:" not in pypi_workflow
    assert "Manual fallback PyPI publish" in pypi_workflow
    assert "Invalid release tag" in pypi_workflow
    assert "Verify tag matches package version" in pypi_workflow
    assert "echo \"ref=${{ inputs.tag }}\"" not in pypi_workflow

    assert "scripts/check.release.py" in release_workflow
    assert "pypa/gh-action-pypi-publish" in release_workflow
    assert "Upload release distributions" in release_workflow
    assert "Invalid release tag" in release_workflow
    assert "Verify tag matches package version" in release_workflow
    assert "echo \"tag=${{ github.event.inputs.tag }}\"" not in release_workflow
    assert release_workflow.index("scripts/check.release.py") < release_workflow.index("pypa/gh-action-pypi-publish")



def test_release_gate_requires_doctor_script():
    release_script = (PLUGIN_ROOT / "scripts" / "check.release.py").read_text(encoding="utf-8")

    assert '"scripts/doctor.py"' in release_script



def test_release_gate_runs_ruff_and_pyright_checks():
    release_script = (PLUGIN_ROOT / "scripts" / "check.release.py").read_text(encoding="utf-8")

    assert '[sys.executable, "-m", "ruff", "check", "."]' in release_script
    assert '[sys.executable, "-m", "pyright"]' in release_script


def test_release_cleanup_preserves_repo_local_venv(monkeypatch, tmp_path):
    release_check = _load_release_check_module("scope_recall_check_release_cleanup")
    monkeypatch.setattr(release_check, "ROOT", tmp_path)
    sentinel = tmp_path / ".venv" / "sentinel.txt"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("keep developer virtualenv\n", encoding="utf-8")
    pycache = tmp_path / "pkg" / "__pycache__"
    pycache.mkdir(parents=True)
    (pycache / "mod.pyc").write_bytes(b"pyc")

    release_check.cleanup_generated()

    assert sentinel.is_file()
    assert not pycache.exists()


def test_ci_installs_release_gate_lint_dependency():
    import tomllib

    pyproject = tomllib.loads((PLUGIN_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev_deps = pyproject["project"]["optional-dependencies"]["dev"]
    workflow = (PLUGIN_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "ruff" in dev_deps
    assert '".[lancedb,dev]"' in workflow



def test_default_embedder_targets_gemini_openai_compatible_api():
    embedder = build_embedder(
        {
            "provider": "openai-compatible",
            "model": "gemini-embedding-001",
            "dimensions": 3072,
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
            "api_key_env": ["SCOPE_RECALL_GEMINI_EMBEDDING_API_KEY"],
        }
    )
    info = embedder.describe()
    assert info["provider"] == "openai-compatible"
    assert info["model"] == "gemini-embedding-001"
    assert info["dimensions"] == 3072
    assert info["base_url"] == "https://generativelanguage.googleapis.com/v1beta/openai"



def test_sentence_transformers_embedder_builds_local_interface_without_loading_weights():
    sentence_transformers_available = bool(importlib.util.find_spec("sentence_transformers"))
    embedder = build_embedder(
        {
            "provider": "sentence-transformers",
            "model": "sentence-transformers/all-MiniLM-L6-v2",
        }
    )
    info = embedder.describe()
    assert info["provider"] == "sentence-transformers"
    assert info["model"] == "sentence-transformers/all-MiniLM-L6-v2"
    assert info["dimensions"] >= 384
    assert embedder.is_available() is sentence_transformers_available


def test_sentence_transformers_embedder_can_encode_locally_when_requested(monkeypatch):
    _install_fake_sentence_transformer(monkeypatch)
    embedder = build_embedder(
        {
            "provider": "sentence-transformers",
            "model": "sentence-transformers/all-MiniLM-L6-v2",
        }
    )
    vectors = embedder.embed_texts(["scope recall local embedder smoke test"])
    assert len(vectors) == 1
    assert len(vectors[0]) == 384


@pytest.mark.skipif(
    os.getenv("SCOPE_RECALL_RUN_SENTENCE_TRANSFORMERS_INTEGRATION") != "1"
    or not bool(importlib.util.find_spec("sentence_transformers")),
    reason="real sentence-transformers integration is opt-in because it may download/load HF model weights",
)
def test_sentence_transformers_embedder_real_model_integration():
    embedder = build_embedder(
        {
            "provider": "sentence-transformers",
            "model": "sentence-transformers/all-MiniLM-L6-v2",
            "device": "cpu",
        }
    )
    vectors = embedder.embed_texts(["scope recall local embedder smoke test"])
    assert len(vectors) == 1
    assert len(vectors[0]) >= 384


def test_sentence_transformers_provider_path_uses_local_vector_dimensions(tmp_path, monkeypatch):
    _install_fake_sentence_transformer(monkeypatch)
    config_path = tmp_path / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "vector": {
                    "embedder": {
                        "provider": "sentence-transformers",
                        "model": "sentence-transformers/all-MiniLM-L6-v2",
                    },
                    "fallback_embedder": {
                        "provider": "local-hash",
                        "dimensions": 256,
                        "model": "hash-v1",
                    },
                }
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-local-model",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        payload = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {"content": "Local sentence-transformers provider smoke test.", "target": "memory"},
            )
        )
        assert payload["stored"] is True
        stats = json.loads(plugin.handle_tool_call("scope_recall_stats", {}))
        assert stats["vector"]["ready"] is True
        assert stats["vector"]["embedder"]["provider"] == "sentence-transformers"
        assert stats["vector"]["embedder"]["model"] == "sentence-transformers/all-MiniLM-L6-v2"
        assert stats["vector"]["embedder"]["dimensions"] == 384
        assert stats["vector"]["row_count"] == 1
    finally:
        plugin.shutdown()



def test_incremental_vector_sync_removes_stale_rows(tmp_path):
    _write_local_debug_vector_config(tmp_path)
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-a",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        payload = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {"content": "Deploy services with uv run app.", "target": "memory"},
            )
        )
        assert payload["stored"] is True
        plugin.flush(timeout=5.0)
        assert plugin._vector_store is not None
        plugin._vector_store.upsert_records(
            [
                {
                    "id": "stale-row",
                    "scope_id": plugin._scope_id,
                    "source": "test",
                    "target": "memory",
                    "content": "obsolete row",
                    "summary": "obsolete row",
                    "updated_at": "1970-01-01T00:00:00+00:00",
                    "vector": [0.0] * plugin._embedder.dimensions,
                }
            ]
        )
        assert plugin._vector_store.count_rows() == 2
    finally:
        plugin.shutdown()

    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-b",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        assert plugin._vector_store is not None
        assert plugin._vector_store.count_rows() == 1
        assert "stale-row" not in plugin._vector_store.list_ids()
    finally:
        plugin.shutdown()



def test_incremental_vector_sync_deduplicates_duplicate_ids(tmp_path):
    _write_local_debug_vector_config(tmp_path)
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-a",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        payload = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {"content": "Duplicate vector rows should be repaired by id.", "target": "memory"},
            )
        )
        assert payload["stored"] is True
        memory_id = payload["id"]
        plugin.flush(timeout=5.0)
        assert plugin._vector_store is not None
        assert plugin._embedder is not None
        plugin._vector_store._require_table().add(
            [
                {
                    "id": memory_id,
                    "scope_id": plugin._scope_id,
                    "source": "test-duplicate",
                    "target": "memory",
                    "content": "obsolete duplicate row",
                    "summary": "obsolete duplicate row",
                    "updated_at": "1970-01-01T00:00:00+00:00",
                    "vector": [0.0] * plugin._embedder.dimensions,
                }
            ]
        )
        assert plugin._vector_store.count_rows() == 2
        assert plugin._vector_store.audit_counts()["duplicate_rows"] == 1
    finally:
        plugin.shutdown()

    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-b",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        assert plugin._vector_store is not None
        assert plugin._vector_store.count_rows() == 1
        assert plugin._vector_store.audit_counts()["duplicate_rows"] == 0
        assert plugin._vector_store.list_ids().count(memory_id) == 1
        stats = json.loads(plugin.handle_tool_call("scope_recall_stats", {}))
        assert stats["vector"]["row_count"] == 1
        assert stats["vector"]["unique_id_count"] == 1
        assert stats["vector"]["duplicate_row_count"] == 0
    finally:
        plugin.shutdown()



def test_vector_upsert_failure_marks_needs_repair_without_losing_sqlite_row(tmp_path, monkeypatch):
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-vector-failure",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        assert plugin._vector_store is not None

        def fail_upsert(rows):
            raise RuntimeError("simulated LanceDB delete failure")

        monkeypatch.setattr(plugin._vector_store, "upsert_records", fail_upsert)
        payload = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {"content": "SQLite truth survives vector upsert failure.", "target": "memory"},
            )
        )
        assert payload["stored"] is True
        assert plugin._conn is not None
        count = plugin._conn.execute("SELECT COUNT(*) FROM memories WHERE id = ?", (payload["id"],)).fetchone()[0]
        assert count == 1
        stats = json.loads(plugin.handle_tool_call("scope_recall_stats", {}))
        assert stats["vector"]["ready"] is False
        assert stats["vector"]["status"] == "needs_repair"
        assert "simulated LanceDB delete failure" in stats["vector"]["message"]
    finally:
        plugin.shutdown()



def test_default_runtime_falls_back_to_local_hash_when_api_embedder_is_unavailable(tmp_path, monkeypatch):
    for name in ("SCOPE_RECALL_GEMINI_EMBEDDING_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "GEMINI_BASE_URL", "OPENAI_BASE_URL"):
        monkeypatch.delenv(name, raising=False)

    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-fallback",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        plugin.flush(timeout=5.0)
        assert plugin._vector_store is not None
        assert plugin._embedder is not None
        assert plugin._embedder.provider == "local-hash"
        assert plugin._vector_store.dimensions == 256
        assert "using fallback local-hash" in plugin._vector_message
        schema_field = plugin._vector_store._require_table().schema.field("vector")
        assert int(schema_field.type.list_size) == 256
    finally:
        plugin.shutdown()



def test_vector_store_rebuilds_when_embedder_dimensions_change(tmp_path):
    config_path = tmp_path / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "vector": {
                    "embedder": {
                        "provider": "local-hash",
                        "dimensions": 3072,
                        "model": "hash-v1",
                    },
                    "fallback_embedder": {
                        "provider": "local-hash",
                        "dimensions": 256,
                        "model": "hash-v1",
                    },
                }
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-a",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        plugin.flush(timeout=5.0)
        assert plugin._vector_store is not None
        assert plugin._vector_store.dimensions == 3072
        schema_field = plugin._vector_store._require_table().schema.field("vector")
        assert int(schema_field.type.list_size) == 3072
    finally:
        plugin.shutdown()

    config_path.write_text(
        json.dumps(
            {
                "vector": {
                    "embedder": {
                        "provider": "local-hash",
                        "dimensions": 256,
                        "model": "hash-v1",
                    }
                }
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-b",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        assert plugin._vector_store is not None
        assert plugin._vector_store.dimensions == 256
        schema_field = plugin._vector_store._require_table().schema.field("vector")
        assert int(schema_field.type.list_size) == 256
    finally:
        plugin.shutdown()



def test_scope_recall_package_import_is_light_without_hermes_runtime(monkeypatch):
    monkeypatch.delitem(sys.modules, "scope_recall", raising=False)
    monkeypatch.delitem(sys.modules, "agent.memory_provider", raising=False)
    plugin_root = str(PLUGIN_ROOT)
    monkeypatch.syspath_prepend(str(PLUGIN_ROOT.parent))

    class _BlockHermesRuntimeImport:
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "agent.memory_provider":
                raise ModuleNotFoundError("Hermes runtime intentionally unavailable")
            return None

    blocker = _BlockHermesRuntimeImport()
    sys.meta_path.insert(0, blocker)
    try:
        module = importlib.import_module("scope_recall")
    finally:
        sys.meta_path.remove(blocker)
        restored_package = types.ModuleType(PACKAGE_NAME)
        restored_package.__path__ = [str(PLUGIN_ROOT)]
        monkeypatch.setitem(sys.modules, PACKAGE_NAME, restored_package)

    assert list(getattr(module, "__path__", [])) == [plugin_root]
    assert module.__all__ == ["register"]
    assert callable(module.register)



def test_openclaw_import_script_is_idempotent(tmp_path):
    source_dir = tmp_path / "openclaw-memory"
    source_dir.mkdir(parents=True, exist_ok=True)
    db = lancedb.connect(str(source_dir))
    schema = pa.schema(
        [
            pa.field("id", pa.string()),
            pa.field("text", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), 4)),
            pa.field("category", pa.string()),
            pa.field("scope", pa.string()),
            pa.field("importance", pa.float32()),
            pa.field("timestamp", pa.int64()),
            pa.field("metadata", pa.string()),
        ]
    )
    table = pa.Table.from_pylist(
        [
            {
                "id": "legacy-1",
                "text": "Use uv run app for deploys.",
                "vector": [0.1, 0.2, 0.3, 0.4],
                "category": "memory",
                "scope": "joy",
                "importance": 0.8,
                "timestamp": 1715472000000,
                "metadata": json.dumps({"source": "test"}, ensure_ascii=False),
            }
        ],
        schema=schema,
    )
    db.create_table("memories", data=table)

    hermes_home = tmp_path / "hermes-home"
    cmd = [
        sys.executable,
        str(SCRIPT_PATH),
        "--source",
        str(source_dir),
        "--hermes-home",
        str(hermes_home),
    ]
    dry_run = json.loads(subprocess.run(cmd, check=True, capture_output=True, text=True).stdout)
    assert dry_run["ok"] is True
    assert dry_run["dry_run"] is True
    assert dry_run["rows_inserted"] == 0
    assert not (hermes_home / "scope-recall" / "memory.sqlite3").exists()

    apply_cmd = [*cmd, "--apply"]
    first = json.loads(subprocess.run(apply_cmd, check=True, capture_output=True, text=True).stdout)
    second = json.loads(subprocess.run(apply_cmd, check=True, capture_output=True, text=True).stdout)

    assert first["ok"] is True
    assert first["rows_inserted"] == 1
    assert first["rows_skipped"] == 0
    assert second["ok"] is True
    assert second["rows_inserted"] == 0
    assert second["rows_skipped"] == 1
    assert second["idempotent"] is True

    conn = sqlite3.connect(hermes_home / "scope-recall" / "memory.sqlite3")
    try:
        memory_count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        ledger_count = conn.execute("SELECT COUNT(*) FROM import_ledger").fetchone()[0]
        fts_count = conn.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0]
    finally:
        conn.close()

    assert memory_count == 1
    assert ledger_count == 1
    assert fts_count == 1


def test_lexical_and_combined_scores_are_capped_at_one():
    from scope_recall.scoring import combine_scores, lexical_score

    lexical = lexical_score(
        query="Joy prefers concise answers",
        content="Joy prefers concise answers with direct problem-first reporting.",
        summary="Joy prefers concise answers",
        source="builtin-curated",
        target="user",
    )
    assert 0.0 <= lexical <= 1.0

    combined = combine_scores(
        {"lexical_score": 1.3, "vector_score": 1.2},
        lexical_weight=0.45,
        vector_weight=0.55,
    )
    assert combined == 1.0


def test_recall_merge_preserves_incoming_recency_metadata(tmp_path):
    from scope_recall.models import RecallItem

    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-recency-merge",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        plugin._retrieval_config = {"mode": "hybrid", "min_score": 0.0, "candidate_pool": 3, "fusion_strategy": "linear", "entity_distance_weight": 0.0}
        duplicate_content = "Joy prefers concise answers with direct problem-first reporting."
        older = RecallItem(
            id="older",
            source="tool",
            target="user",
            content=duplicate_content,
            summary=duplicate_content,
            updated_at="2026-01-01T00:00:00+00:00",
            score=0.4,
            metadata={"lexical_score": 0.4, "base_score": 0.4, "recency_bonus": 0.05},
        )
        newer = RecallItem(
            id="newer",
            source="tool",
            target="user",
            content=duplicate_content,
            summary=duplicate_content,
            updated_at="2026-01-02T00:00:00+00:00",
            score=0.7,
            metadata={"vector_score": 0.7, "base_score": 0.7, "recency_bonus": 0.25},
        )

        plugin._search_db_memories = lambda query, limit: [older]
        plugin._search_vector_memories = lambda query, limit: [newer]
        plugin._search_curated_memories = lambda query: []

        results = plugin._recall_service.search_memories("Joy concise answers", limit=1)
    finally:
        plugin.shutdown()

    assert len(results) == 1
    assert results[0].id == "newer"
    assert results[0].metadata["lexical_score"] == 0.4
    assert results[0].metadata["vector_score"] == 0.7
    assert results[0].metadata["base_score"] == pytest.approx(0.565)
    assert results[0].metadata["recency_bonus"] == 0.25


def test_openai_compatible_embedder_rotates_to_next_key_after_failure(monkeypatch):
    from scope_recall.embedders import OpenAICompatibleEmbedder

    attempts: list[str] = []
    encoding_formats: list[str | None] = []

    class _FakeEmbeddings:
        def __init__(self, key: str) -> None:
            self.key = key

        def create(self, *, model: str, input: list[str], encoding_format: str | None = None):
            attempts.append(self.key)
            encoding_formats.append(encoding_format)
            if self.key == "public-test-key-1":
                raise RuntimeError("simulated exhausted key")

            class _Item:
                embedding = [0.1, 0.2, 0.3]

            class _Response:
                data = [_Item() for _ in input]

            return _Response()

    class _FakeOpenAI:
        def __init__(self, *, api_key: str, base_url: str | None = None) -> None:
            self.embeddings = _FakeEmbeddings(api_key)

    monkeypatch.setattr("scope_recall.embedders.OpenAI", _FakeOpenAI)
    embedder = OpenAICompatibleEmbedder(
        model="gemini-embedding-001",
        api_key=["public-test-key-1", "public-test-key-2"],
        base_url="https://example.invalid/v1",
        dimensions=3,
    )

    vectors = embedder.embed_texts(["memory row"])

    assert vectors == [[0.1, 0.2, 0.3]]
    assert attempts == ["public-test-key-1", "public-test-key-2"]
    assert encoding_formats == ["float", "float"]
