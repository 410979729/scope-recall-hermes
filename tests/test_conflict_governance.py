"""Tests for contradiction governance and forget/delete documentation semantics.

The goal is to avoid hiding memories or declaring conflicts from weak same-topic evidence."""

from __future__ import annotations

import json
from pathlib import Path

from plugins.memory import load_memory_provider
from scope_recall.governance import is_conflicting, semantic_similarity


def _write_scope_recall_config(hermes_home: Path, values: dict) -> None:
    config_path = hermes_home / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(values, ensure_ascii=False) + "\n", encoding="utf-8")


def _provider(tmp_path: Path, config: dict):
    _write_scope_recall_config(tmp_path, config)
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "conflict-regression-session",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    return plugin


def test_negated_same_topic_different_attribute_is_not_a_conflict():
    existing = "Project Phoenix deploy command is uv run deploy."
    candidate = "Project Phoenix deploy command is not documented in README."

    assert semantic_similarity(existing, candidate) >= 0.35
    assert is_conflicting(existing, candidate) is False


def test_auto_conflict_candidates_do_not_supersede_or_hide_existing_memories(tmp_path):
    provider = _provider(tmp_path, {"vector": {"enabled": False}})
    try:
        first = json.loads(
            provider.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "Project Phoenix deploy command is uv run deploy.",
                    "target": "project",
                },
            )
        )
        second = json.loads(
            provider.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "Project Phoenix deploy command is not documented in README.",
                    "target": "project",
                },
            )
        )
        assert first["stored"] is True
        assert second["stored"] is True

        with provider._lock:
            row = provider._require_conn().execute(
                "SELECT metadata FROM memories WHERE id = ?",
                (first["id"],),
            ).fetchone()
            relation_rows = provider._require_conn().execute(
                """
                SELECT relation_type
                FROM memory_relations
                WHERE source_memory_id = ? OR target_memory_id = ?
                ORDER BY relation_type
                """,
                (first["id"], first["id"]),
            ).fetchall()
        metadata = json.loads(row["metadata"] or "{}")
        relation_types = [str(row["relation_type"]) for row in relation_rows]

        assert metadata.get("lifecycle") != "superseded"
        assert "superseded_by" not in metadata
        assert "supersedes" not in relation_types
        assert "superseded_by" not in relation_types

        search = json.loads(provider.handle_tool_call("scope_recall_search", {"query": "Project Phoenix deploy command"}))
        result_ids = {item["id"] for item in search["results"]}
        assert first["id"] in result_ids
        assert second["id"] in result_ids
    finally:
        provider.shutdown()


def test_true_auto_conflict_is_review_relation_not_lifecycle_supersession(tmp_path):
    provider = _provider(tmp_path, {"vector": {"enabled": False}})
    try:
        first = json.loads(
            provider.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "Project Phoenix deploy command is uv run deploy.",
                    "target": "project",
                },
            )
        )
        second = json.loads(
            provider.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "Project Phoenix deploy command is not uv run deploy.",
                    "target": "project",
                },
            )
        )
        assert first["stored"] is True
        assert second["stored"] is True

        with provider._lock:
            old_row = provider._require_conn().execute(
                "SELECT metadata FROM memories WHERE id = ?",
                (first["id"],),
            ).fetchone()
            relation_rows = provider._require_conn().execute(
                """
                SELECT source_memory_id, target_memory_id, relation_type
                FROM memory_relations
                WHERE source_memory_id IN (?, ?) OR target_memory_id IN (?, ?)
                ORDER BY relation_type, source_memory_id, target_memory_id
                """,
                (first["id"], second["id"], first["id"], second["id"]),
            ).fetchall()
        old_metadata = json.loads(old_row["metadata"] or "{}")
        relation_types = [str(row["relation_type"]) for row in relation_rows]

        assert relation_types == ["contradicts", "contradicts"]
        assert old_metadata.get("lifecycle") != "superseded"
        assert "superseded_by" not in old_metadata
    finally:
        provider.shutdown()


def test_readme_documents_forget_as_exact_id_delete():
    readme = Path(__file__).resolve().parents[1] / "README.md"
    line = next(line for line in readme.read_text(encoding="utf-8").splitlines() if "`scope_recall_forget`" in line)

    assert "exact id" in line.lower() or "exact `id`" in line.lower()
    assert "matching a query" not in line.lower()
