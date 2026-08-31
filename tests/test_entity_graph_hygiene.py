"""Tests for entity graph lifecycle filtering and orphan companion cleanup.

They ensure graph reads follow memory lifecycle visibility and remain rebuildable companion state."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from plugins.memory import load_memory_provider

from scope_recall.graph import backfill_memory_entities, extract_entities, sync_memory_entities
from scope_recall.sql_store import ensure_schema, store_row

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
GRAPH_REPAIR_SCRIPT = PLUGIN_ROOT / "scripts" / "repair.graph_hygiene.py"


@pytest.fixture
def provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config_dir = tmp_path / "scope-recall"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.json").write_text(
        json.dumps({"vector": {"enabled": False}}), encoding="utf-8"
    )
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    monkeypatch.setitem(
        plugin.initialize.__func__.__globals__, "start_writer", lambda _provider: None
    )
    plugin.initialize(
        "session-entity-hygiene",
        hermes_home=str(tmp_path),
        platform="cli",
        user_id="joy",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        yield plugin
    finally:
        plugin.shutdown()


def _store(provider, *, content: str, target: str = "project", entities: list[str], lifecycle: str = "active") -> str:
    memory_id, inserted, outcome = provider._store_now(
        content=content,
        source="tool-store",
        target=target,
        session_id="session-entity-hygiene",
        metadata={"entities": entities, "lifecycle": lifecycle, "memory_type": "project"},
        allow_duplicate=True,
        semantic_merge=False,
        scope_mode="shared",
    )
    assert inserted, outcome
    if lifecycle != "active":
        _set_metadata_values(provider, memory_id, lifecycle=lifecycle)
    return memory_id


def _set_metadata_values(provider, memory_id: str, **values) -> None:
    with provider._lock:
        conn = provider._require_conn()
        row = conn.execute("SELECT metadata FROM memories WHERE id = ?", (memory_id,)).fetchone()
        metadata = json.loads(str(row["metadata"] or "{}"))
        metadata.update(values)
        conn.execute(
            "UPDATE memories SET metadata = ? WHERE id = ?",
            (json.dumps(metadata, ensure_ascii=False, sort_keys=True), memory_id),
        )
        conn.commit()


def _memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _store_sqlite(conn: sqlite3.Connection, *, memory_id: str, content: str, lifecycle: str = "active") -> None:
    store_row(
        conn,
        memory_id=memory_id,
        scope_id="shared-scope",
        platform="cli",
        user_id="joy",
        chat_id="",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="hermes",
        session_id="session-entity-hygiene",
        source="tool-store",
        target="project",
        content=content,
        metadata=json.dumps({"entities": ["project-atlas"], "lifecycle": "active"}, ensure_ascii=False),
    )
    if lifecycle != "active":
        row = conn.execute("SELECT metadata FROM memories WHERE id = ?", (memory_id,)).fetchone()
        metadata = json.loads(str(row["metadata"] or "{}"))
        metadata["lifecycle"] = lifecycle
        conn.execute("UPDATE memories SET metadata = ? WHERE id = ?", (json.dumps(metadata, ensure_ascii=False, sort_keys=True), memory_id))
        conn.commit()


def test_graph_sync_and_backfill_skip_lifecycle_hidden_memories():
    conn = _memory_conn()
    _store_sqlite(conn, memory_id="active-memory", content="Project Atlas visible graph memory.")
    _store_sqlite(conn, memory_id="archived-memory", content="Project Atlas archived graph memory.", lifecycle="archived")

    conn.execute("DELETE FROM memory_entities")
    backfill_memory_entities(conn)
    assert conn.execute("SELECT COUNT(*) FROM memory_entities WHERE memory_id = 'active-memory'").fetchone()[0] >= 1
    assert conn.execute("SELECT COUNT(*) FROM memory_entities WHERE memory_id = 'archived-memory'").fetchone()[0] == 0

    sync_memory_entities(
        conn,
        memory_id="active-memory",
        content="Project Atlas visible graph memory.",
        target="project",
        metadata={"entities": ["project-atlas"], "lifecycle": "archived"},
    )
    assert conn.execute("SELECT COUNT(*) FROM memory_entities WHERE memory_id = 'active-memory'").fetchone()[0] == 0


def test_entity_probe_related_and_profile_hide_lifecycle_removed_memories(provider):
    active_id = _store(
        provider,
        content="Project Atlas current decision should remain visible in entity graph probes and profile lookup.",
        entities=["project-atlas", "visible-neighbor"],
    )
    _set_metadata_values(provider, active_id, entities=["project-atlas", "visible-neighbor", "read_file", "search_files"])
    with provider._lock:
        provider._require_conn().executemany(
            "INSERT OR REPLACE INTO memory_entities(memory_id, entity, weight, source) VALUES (?, ?, 1.0, 'legacy-fixture')",
            [(active_id, "read_file"), (active_id, "search_files")],
        )
        provider._require_conn().commit()
    archived_id = _store(
        provider,
        content="Project Atlas archived digest should not appear through entity graph probes or related entities.",
        entities=["project-atlas", "archived-neighbor"],
        lifecycle="archived",
    )
    rejected_id = _store(
        provider,
        content="Project Atlas rejected candidate should not appear through entity graph probes or profile lookup.",
        entities=["project-atlas", "rejected-neighbor"],
        lifecycle="rejected",
    )
    superseded_id = _store(
        provider,
        content="Project Atlas superseded candidate should not appear through entity graph probes or profile lookup.",
        entities=["project-atlas", "superseded-neighbor"],
        lifecycle="superseded",
    )
    obsolete_id = _store(
        provider,
        content="Project Atlas obsolete candidate should not appear through entity graph probes or profile lookup.",
        entities=["project-atlas", "obsolete-neighbor"],
        lifecycle="obsolete",
    )
    candidate_id = _store(
        provider,
        content="Project Atlas pending candidate should remain review-only until explicit promotion.",
        entities=["project-atlas", "candidate-neighbor"],
        lifecycle="candidate",
    )
    # Simulate stale companion rows from an older build.  Ordinary entity graph
    # surfaces must still enforce the memory row lifecycle at read time.
    with provider._lock:
        provider._require_conn().executemany(
            "INSERT OR REPLACE INTO memory_entities(memory_id, entity, weight, source) VALUES (?, ?, 1.0, 'legacy-fixture')",
            [(candidate_id, "project-atlas"), (candidate_id, "candidate-neighbor")],
        )
        provider._require_conn().commit()

    probe = provider._probe_entity(entity="project-atlas", limit=10)
    probe_ids = {item["id"] for item in probe["results"]}
    assert active_id in probe_ids
    assert {archived_id, rejected_id, superseded_id, obsolete_id, candidate_id}.isdisjoint(probe_ids)
    active_probe = next(item for item in probe["results"] if item["id"] == active_id)
    assert {"read_file", "search_files"}.isdisjoint(set(active_probe["entities"]))

    related = provider._related_entities(entity="project-atlas", limit=20)
    related_names = {item["entity"] for item in related["related"]}
    assert "visible-neighbor" in related_names
    assert {"read_file", "search_files"}.isdisjoint(related_names)
    assert {
        "archived-neighbor",
        "rejected-neighbor",
        "superseded-neighbor",
        "obsolete-neighbor",
        "candidate-neighbor",
    }.isdisjoint(related_names)

    profile = provider._profile_payload(entity="project-atlas", targets=["project"], include_curated=False, limit=10)
    profile_ids = {item["id"] for item in profile["sections"]["project"]["items"]}
    assert active_id in profile_ids
    assert {archived_id, rejected_id, superseded_id, obsolete_id, candidate_id}.isdisjoint(profile_ids)
    active_profile = next(item for item in profile["sections"]["project"]["items"] if item["id"] == active_id)
    assert {"read_file", "search_files"}.isdisjoint(set(active_profile["entities"]))

    review_profile = provider._profile_payload(
        entity="project-atlas",
        targets=["project"],
        include_candidates=True,
        include_curated=False,
        limit=10,
    )
    review_profile_ids = {
        item["id"] for item in review_profile["sections"]["project"]["items"]
    }
    assert {active_id, candidate_id}.issubset(review_profile_ids)
    assert {archived_id, rejected_id, superseded_id, obsolete_id}.isdisjoint(
        review_profile_ids
    )


def test_extract_entities_filters_tool_trace_and_api_noise_tokens():
    text = """
    Tool execution summary: read_file returned path=/tmp/x; search_files and execute_code were called.
    The assistant then used skill_view, session_search, browser_console, terminal, patch, write_file, and todo.
    Durable fact: Project Atlas uses Scope Recall for operator memory.
    """

    entities = set(extract_entities(text))

    assert "project" in entities or "project atlas" in entities or "atlas" in entities
    assert not {
        "read_file",
        "search_files",
        "execute_code",
        "skill_view",
        "session_search",
        "browser_console",
        "terminal",
        "patch",
        "write_file",
        "todo",
        "tool",
        "path",
    } & entities


def test_extract_entities_rejects_cjk_fragments_but_keeps_explicit_named_entities(
    monkeypatch,
):
    monkeypatch.setattr("scope_recall.graph._jieba_entities", lambda _text: [])
    text = (
        "长上下文并不等于有效注意力：文档中段的决定性词出现在尾部时，会让模型被大量旧测试与审计材料淹没。"
        "`星河`只负责第一轮广覆盖审查，`云舟`负责架构决策，Scope Recall 使用 Gemini embedding-001。"
        "所有高风险状态机最终必须交给 Codex。"
    )

    entities = set(extract_entities(text))

    assert {"scope", "gemini", "embedding-001", "codex"} & entities
    assert {"星河", "云舟"} <= entities
    assert not {
        "文档中段的决定性词出",
        "现在尾部时",
        "会让模型被大量旧测试与",
        "审计材料淹没",
        "星河只负责第一轮广覆盖审",
        "所有高风险状态机",
        "最终必须交给",
    } & entities
    assert all(not (len(entity) > 8 and all("\u4e00" <= char <= "\u9fff" for char in entity)) for entity in entities)


def test_candidate_lifecycle_is_removed_from_graph_companions():
    conn = _memory_conn()
    _store_sqlite(conn, memory_id="candidate-memory", content="Project Atlas provisional candidate.")

    sync_memory_entities(
        conn,
        memory_id="candidate-memory",
        content="Project Atlas provisional candidate.",
        target="project",
        metadata={"entities": ["project-atlas"], "lifecycle": "candidate"},
    )

    assert conn.execute(
        "SELECT COUNT(*) FROM memory_entities WHERE memory_id='candidate-memory'"
    ).fetchone()[0] == 0


def test_graph_hygiene_cli_accepts_explicit_dry_run_over_apply(tmp_path):
    hermes_home = tmp_path / "hermes"
    db_dir = hermes_home / "scope-recall"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        ensure_schema(conn)
        _store_sqlite(conn, memory_id="archived-memory", content="Archived graph fixture.", lifecycle="archived")
        conn.execute(
            "INSERT OR REPLACE INTO memory_entities(memory_id, entity, weight, source) VALUES (?, ?, 1.0, 'fixture')",
            ("archived-memory", "archived-entity"),
        )
        conn.commit()
        before_count = conn.execute("SELECT COUNT(*) FROM memory_entities WHERE memory_id='archived-memory'").fetchone()[0]
    finally:
        conn.close()

    result = subprocess.run(
        [sys.executable, str(GRAPH_REPAIR_SCRIPT), "--hermes-home", str(hermes_home), "--apply", "--dry-run", "--json"],
        text=True,
        capture_output=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["status"] == "needs_repair"

    conn = sqlite3.connect(db_path)
    try:
        after_count = conn.execute("SELECT COUNT(*) FROM memory_entities WHERE memory_id='archived-memory'").fetchone()[0]
        assert after_count == before_count
        assert after_count > 0
    finally:
        conn.close()
