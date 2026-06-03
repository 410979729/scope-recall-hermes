import importlib
import importlib.util
import json
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
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "scope_recall"
if PACKAGE_NAME not in sys.modules:
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(PLUGIN_ROOT)]
    sys.modules[PACKAGE_NAME] = package

build_embedder = importlib.import_module(f"{PACKAGE_NAME}.embedders").build_embedder



def _package_version() -> str:
    import tomllib

    return tomllib.loads((PLUGIN_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]



def test_readme_public_version_matches_package_metadata():
    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
    version = _package_version()

    assert f"Version `{version}`" in readme
    assert "Version `1.0.6`" not in readme



def test_readme_documents_hermes_venv_test_command():
    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")

    assert "PYTHONPATH=/path/to/hermes-agent:" in readme
    assert "venv/bin/python -m pytest -q" in readme
    assert "Plain `pytest` from an unrelated Python environment" in readme



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
    assert payload["source"]["pyproject_version"] == version
    assert payload["source"]["plugin_version"] == version
    assert payload["source"]["readme_public_versions"] == [version]
    assert payload["checks"]["source_metadata"]["ok"] is True



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



def test_release_gate_requires_doctor_script():
    release_script = (PLUGIN_ROOT / "scripts" / "check.release.py").read_text(encoding="utf-8")

    assert '"scripts/doctor.py"' in release_script



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


@pytest.mark.skipif(not bool(__import__('importlib').util.find_spec('sentence_transformers')), reason='sentence-transformers not installed')
def test_sentence_transformers_embedder_can_encode_locally_when_requested():
    embedder = build_embedder(
        {
            "provider": "sentence-transformers",
            "model": "sentence-transformers/all-MiniLM-L6-v2",
        }
    )
    vectors = embedder.embed_texts(["scope recall local embedder smoke test"])
    assert len(vectors) == 1
    assert len(vectors[0]) >= 384


@pytest.mark.skipif(not bool(__import__('importlib').util.find_spec('sentence_transformers')), reason='sentence-transformers not installed')
def test_sentence_transformers_provider_path_uses_local_vector_dimensions(tmp_path):
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
    first = json.loads(subprocess.run(cmd, check=True, capture_output=True, text=True).stdout)
    second = json.loads(subprocess.run(cmd, check=True, capture_output=True, text=True).stdout)

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
        plugin._retrieval_config = {"mode": "hybrid", "min_score": 0.0, "candidate_pool": 3}
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

    class _FakeEmbeddings:
        def __init__(self, key: str) -> None:
            self.key = key

        def create(self, *, model: str, input: list[str]):
            attempts.append(self.key)
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
