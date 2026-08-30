"""Tests for deterministic relation extraction and graph synchronization.

They protect contradiction, dependency, supersession, and same-topic edge semantics."""

from __future__ import annotations

import json
import sqlite3

import pytest

from plugins.memory import load_memory_provider

from scope_recall.relation_extraction import extract_relation_candidates, rebuild_extracted_relations, sync_extracted_relations_for_memory
from scope_recall.relation_frequency_index import sync_relation_frequency_memory
from scope_recall.sql_store import ensure_schema, store_row


def _disable_unrelated_vector_runtime(hermes_home) -> None:
    config_dir = hermes_home / "scope-recall"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.json").write_text(
        json.dumps({"vector": {"enabled": False}}), encoding="utf-8"
    )


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _store(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    content: str,
    updated_at: str = "2026-01-01T00:00:00+00:00",
    entities: list[str] | None = None,
    scope_id: str = "shared-scope",
) -> None:
    store_row(
        conn,
        memory_id=memory_id,
        scope_id=scope_id,
        platform="telegram",
        user_id="joy",
        chat_id="dm",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="hermes",
        session_id="relation-fixture",
        source="tool-store",
        target="project",
        content=content,
        metadata=json.dumps(
            {
                "memory_type": "factual",
                "entities": entities or ["Project Atlas"],
                "importance": 0.8,
            },
            ensure_ascii=False,
        ),
        allow_duplicate=True,
    )
    conn.execute("UPDATE memories SET updated_at = ? WHERE id = ?", (updated_at, memory_id))
    sync_relation_frequency_memory(conn, memory_id)
    conn.commit()


def test_relation_extraction_dry_run_is_query_only_on_readonly_db(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    writer = sqlite3.connect(db_path)
    writer.row_factory = sqlite3.Row
    try:
        ensure_schema(writer)
        _store(writer, memory_id="atlas-old", content="Project Atlas v1 deploy command uses old atlasctl deploy.")
        _store(writer, memory_id="atlas-new", content="Project Atlas v2 supersedes v1 deploy command and uses uv run atlas deploy.")
    finally:
        writer.close()

    readonly = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    readonly.row_factory = sqlite3.Row
    readonly.execute("PRAGMA query_only=ON")
    try:
        before = readonly.total_changes
        payload = rebuild_extracted_relations(readonly, scope_ids=["shared-scope"], dry_run=True)
    finally:
        readonly.close()

    assert payload["dry_run"] is True
    assert payload["candidate_count"] >= 1
    assert payload["inserted"] == 0
    assert before == 0


def test_relation_extraction_builds_typed_relation_edges():
    conn = _conn()
    _store(conn, memory_id="atlas-old", content="Project Atlas v1 deploy command uses old atlasctl deploy.", updated_at="2026-01-01T00:00:00+00:00")
    _store(
        conn,
        memory_id="atlas-new",
        content="Project Atlas v2 supersedes v1 deploy command and uses uv run atlas deploy.",
        updated_at="2026-02-01T00:00:00+00:00",
    )
    _store(conn, memory_id="redis-runbook", content="Redis service runbook: check redis-cli ping before Atlas deploy.")
    _store(conn, memory_id="atlas-redis", content="Project Atlas deploy depends on Redis service availability.")
    _store(conn, memory_id="platform-team", content="Platform Team owns Redis service operations.")
    _store(conn, memory_id="redis-owner", content="Redis service is owned by Platform Team.")
    _store(conn, memory_id="zephyr-runbook", content="Project Zephyr worker queue drain metrics must be green.")
    _store(conn, memory_id="atlas-affects-zephyr", content="Project Atlas deploy affects Project Zephyr worker queue drain metrics.")
    _store(conn, memory_id="old-port-fact", content="Project Atlas old port fact says Atlas API listens on port 8443.")
    _store(conn, memory_id="new-port-fact", content="Project Atlas new port fact invalidates old port fact; Atlas API now listens on port 9443.")

    candidates = extract_relation_candidates(conn, scope_ids=["shared-scope"])
    pair_types = {(item["source_memory_id"], item["target_memory_id"], item["relation_type"]) for item in candidates}

    assert ("atlas-new", "atlas-old", "supersedes") in pair_types
    assert ("atlas-redis", "redis-runbook", "depends_on") in pair_types
    assert ("redis-owner", "platform-team", "owned_by") in pair_types
    assert ("atlas-affects-zephyr", "zephyr-runbook", "affects") in pair_types
    assert ("new-port-fact", "old-port-fact", "invalidates") in pair_types
    assert any(item[2] == "same_topic" for item in pair_types)

    result = rebuild_extracted_relations(conn, scope_ids=["shared-scope"], dry_run=False, batch_id="test-relations")

    assert result["inserted"] >= 6
    rows = conn.execute(
        "SELECT source_memory_id, target_memory_id, relation_type, note FROM memory_relations ORDER BY relation_type, source_memory_id"
    ).fetchall()
    relation_types = {row["relation_type"] for row in rows}
    assert {"same_topic", "supersedes", "depends_on", "owned_by", "affects", "invalidates"} <= relation_types
    assert all(str(row["note"]).startswith("relation-extraction:test-relations") for row in rows)


def test_relation_extraction_matches_chinese_invalidates_when_entity_precedes_trigger():
    conn = _conn()
    _store(conn, memory_id="atlas-old-yaml", content="Project Atlas 旧YAML模式仍记录在旧版部署说明中。")
    _store(conn, memory_id="atlas-new-config", content="新配置格式使 Project Atlas 旧YAML模式失效，部署应改用 TOML。")

    candidates = extract_relation_candidates(conn, scope_ids=["shared-scope"])
    invalidates = {
        (item["source_memory_id"], item["target_memory_id"], item["relation_type"], item["confidence"])
        for item in candidates
        if item["relation_type"] == "invalidates"
    }

    assert ("atlas-new-config", "atlas-old-yaml", "invalidates", 0.72) in invalidates


def test_typed_relation_requires_predicate_object_not_shared_branding():
    conn = _conn()
    _store(
        conn,
        memory_id="atlas-deploy",
        content="Project Atlas deployment depends on Redis service availability.",
        entities=["Project Atlas"],
    )
    _store(
        conn,
        memory_id="atlas-branding",
        content="Project Atlas branding guide defines approved logos and colors.",
        entities=["Project Atlas"],
    )
    _store(
        conn,
        memory_id="redis-runbook-object",
        content="Redis service availability runbook and recovery checklist.",
        entities=["Redis service"],
    )

    candidates = extract_relation_candidates(conn, scope_ids=["shared-scope"])
    depends_on = {
        (item["source_memory_id"], item["target_memory_id"])
        for item in candidates
        if item["relation_type"] == "depends_on"
    }

    assert ("atlas-deploy", "redis-runbook-object") in depends_on
    assert ("atlas-deploy", "atlas-branding") not in depends_on


@pytest.mark.parametrize(
    ("relation_type", "source", "target_entity", "unqualified_target"),
    [
        (
            "depends_on",
            "Project Atlas deployment depends on Redis service availability.",
            "Redis",
            "Redis brand color is blue and its approved logo uses a square mark.",
        ),
        (
            "owned_by",
            "Redis service is owned by Platform Team.",
            "Platform Team",
            "Platform Team brand color is blue and its approved logo uses a square mark.",
        ),
        (
            "affects",
            "Project Atlas rollout affects Project Zephyr queue latency.",
            "Project Zephyr",
            "Project Zephyr brand color is blue and its approved logo uses a square mark.",
        ),
        (
            "invalidates",
            "The new deployment policy invalidates Legacy Config v1.",
            "Legacy Config",
            "Legacy Config brand color is blue and its approved logo uses a square mark.",
        ),
    ],
)
def test_typed_relation_requires_target_memory_to_ground_the_object_semantic_slot(
    relation_type: str,
    source: str,
    target_entity: str,
    unqualified_target: str,
):
    conn = _conn()
    _store(
        conn,
        memory_id="relation-source",
        content=source,
        entities=["Project Atlas", target_entity],
    )
    _store(
        conn,
        memory_id="same-entity-wrong-slot",
        content=unqualified_target,
        entities=[target_entity],
    )

    candidates = extract_relation_candidates(conn, scope_ids=["shared-scope"])

    assert not [
        item
        for item in candidates
        if item["source_memory_id"] == "relation-source"
        and item["target_memory_id"] == "same-entity-wrong-slot"
        and item["relation_type"] == relation_type
    ]


def test_depends_on_target_eligibility_rejects_branding_dashboard_word_overlap():
    """Shared deployment words do not make a branding UI an operational dependency."""

    conn = _conn()
    _store(
        conn,
        memory_id="checkout-dependency",
        content="Checkout deployment depends on Redis service availability.",
        entities=["Checkout deployment", "Redis"],
    )
    _store(
        conn,
        memory_id="redis-branding-dashboard",
        content=(
            "The Redis availability icon is green in the branding dashboard "
            "for checkout deployment."
        ),
        entities=["Redis"],
    )
    _store(
        conn,
        memory_id="redis-operations-runbook",
        content="Redis service availability runbook with health and recovery checks.",
        entities=["Redis"],
    )

    candidates = extract_relation_candidates(conn, scope_ids=["shared-scope"])
    depends_on = {
        (item["source_memory_id"], item["target_memory_id"])
        for item in candidates
        if item["relation_type"] == "depends_on"
    }

    assert ("checkout-dependency", "redis-operations-runbook") in depends_on
    assert ("checkout-dependency", "redis-branding-dashboard") not in depends_on


@pytest.mark.parametrize(
    ("relation_type", "source", "target_entity", "camouflaged_target"),
    [
        (
            "depends_on",
            "Checkout deployment depends on Redis service availability.",
            "Redis",
            "The Redis availability icon is green in the branding dashboard for checkout deployment.",
        ),
        (
            "owned_by",
            "Redis service is owned by Platform Team during checkout deployment.",
            "Platform Team",
            "The Platform Team owner badge is green in the branding dashboard for Redis operations.",
        ),
        (
            "affects",
            "Checkout rollout affects Project Zephyr queue latency.",
            "Project Zephyr",
            "The Project Zephyr queue latency chart icon is green in the branding dashboard for checkout rollout.",
        ),
        (
            "invalidates",
            "The new checkout policy invalidates Legacy Config v1 deployment setting.",
            "Legacy Config",
            "The Legacy Config v1 deprecated badge is gray in the branding dashboard for checkout deployment.",
        ),
    ],
)
def test_typed_relation_rejects_presentation_camouflage_even_with_role_words(
    relation_type: str,
    source: str,
    target_entity: str,
    camouflaged_target: str,
) -> None:
    conn = _conn()
    _store(
        conn,
        memory_id="relation-source",
        content=source,
        entities=["Checkout deployment", target_entity],
    )
    _store(
        conn,
        memory_id="presentation-camouflage",
        content=camouflaged_target,
        entities=[target_entity],
    )

    candidates = extract_relation_candidates(conn, scope_ids=["shared-scope"])

    assert not [
        item
        for item in candidates
        if item["source_memory_id"] == "relation-source"
        and item["target_memory_id"] == "presentation-camouflage"
        and item["relation_type"] == relation_type
    ]


@pytest.mark.parametrize(
    ("relation_type", "source", "target_entity", "non_operational_target"),
    [
        (
            "depends_on",
            "Checkout deployment depends on Redis service availability.",
            "Redis",
            "Redis service received the annual community award for its mascot.",
        ),
        (
            "affects",
            "Checkout rollout affects Project Zephyr queue latency.",
            "Project Zephyr",
            "Project Zephyr queue is listed in the architecture glossary.",
        ),
    ],
)
def test_typed_relation_requires_operational_role_not_only_a_resource_noun(
    relation_type: str,
    source: str,
    target_entity: str,
    non_operational_target: str,
) -> None:
    conn = _conn()
    _store(
        conn,
        memory_id="relation-source",
        content=source,
        entities=["Checkout deployment", target_entity],
    )
    _store(
        conn,
        memory_id="resource-name-only",
        content=non_operational_target,
        entities=[target_entity],
    )

    candidates = extract_relation_candidates(conn, scope_ids=["shared-scope"])

    assert not [
        item
        for item in candidates
        if item["source_memory_id"] == "relation-source"
        and item["target_memory_id"] == "resource-name-only"
        and item["relation_type"] == relation_type
    ]


def test_target_eligibility_accepts_separate_operational_clause_after_branding_clause():
    conn = _conn()
    _store(
        conn,
        memory_id="checkout-dependency",
        content="Checkout deployment depends on Redis service availability.",
        entities=["Checkout deployment", "Redis"],
    )
    _store(
        conn,
        memory_id="mixed-redis-note",
        content=(
            "The Redis icon is green in the branding guide. "
            "Redis service health and recovery runbook defines the operational checks."
        ),
        entities=["Redis"],
    )

    candidates = extract_relation_candidates(conn, scope_ids=["shared-scope"])

    assert any(
        item["source_memory_id"] == "checkout-dependency"
        and item["target_memory_id"] == "mixed-redis-note"
        and item["relation_type"] == "depends_on"
        for item in candidates
    )


def test_typed_relation_source_trigger_cannot_cross_a_sentence_boundary():
    """A predicate for PostgreSQL must not bind a later Redis mention."""

    conn = _conn()
    _store(
        conn,
        memory_id="checkout-source",
        content=(
            "Checkout depends on PostgreSQL for transactions. "
            "Redis is mentioned only in the migration appendix."
        ),
        entities=["Checkout", "PostgreSQL", "Redis"],
    )
    _store(
        conn,
        memory_id="redis-runbook",
        content="Redis service health and recovery runbook.",
        entities=["Redis"],
    )

    candidates = extract_relation_candidates(conn, scope_ids=["shared-scope"])

    assert not [
        item
        for item in candidates
        if item["source_memory_id"] == "checkout-source"
        and item["target_memory_id"] == "redis-runbook"
        and item["relation_type"] == "depends_on"
    ]


@pytest.mark.parametrize(
    ("relation_type", "source", "target_entity", "target"),
    [
        (
            "depends_on",
            "Checkout does not depend on Redis service availability.",
            "Redis",
            "Redis service health and recovery runbook.",
        ),
        (
            "owned_by",
            "Redis service is not owned by Platform Team.",
            "Platform Team",
            "Platform Team maintains production services and owns on-call operations.",
        ),
        (
            "affects",
            "Checkout rollout does not affect Project Zephyr queue latency.",
            "Project Zephyr",
            "Project Zephyr queue latency and throughput metrics.",
        ),
        (
            "invalidates",
            "The new policy does not invalidate Legacy Config v1.",
            "Legacy Config",
            "Legacy Config v1 is the previous deployment configuration.",
        ),
        (
            "depends_on",
            "Checkout no longer depends on Redis service availability.",
            "Redis",
            "Redis service availability and runtime health checks.",
        ),
        (
            "owned_by",
            "Redis service is no longer owned by Platform Team.",
            "Platform Team",
            "Platform Team maintains production services and owns on-call operations.",
        ),
        (
            "affects",
            "Checkout rollout no longer affects Project Zephyr queue latency.",
            "Project Zephyr",
            "Project Zephyr queue latency and throughput metrics.",
        ),
        (
            "invalidates",
            "The new policy no longer invalidates Legacy Config v1.",
            "Legacy Config",
            "Legacy Config v1 is the previous deployment configuration.",
        ),
    ],
)
def test_typed_relation_rejects_explicitly_negated_source_claims(
    relation_type: str,
    source: str,
    target_entity: str,
    target: str,
) -> None:
    conn = _conn()
    _store(
        conn,
        memory_id="negated-source",
        content=source,
        entities=["Checkout", target_entity],
    )
    _store(
        conn,
        memory_id="relation-target",
        content=target,
        entities=[target_entity],
    )

    candidates = extract_relation_candidates(conn, scope_ids=["shared-scope"])

    assert not [
        item
        for item in candidates
        if item["source_memory_id"] == "negated-source"
        and item["target_memory_id"] == "relation-target"
        and item["relation_type"] == relation_type
    ]


@pytest.mark.parametrize(
    ("relation_type", "source", "target_entity", "denying_target"),
    [
        (
            "depends_on",
            "Checkout depends on Redis service availability.",
            "Redis",
            "Redis is not used by Checkout and provides no runtime service for it.",
        ),
        (
            "owned_by",
            "Redis service is owned by Platform Team.",
            "Platform Team",
            "Platform Team is not responsible for Redis operations or on-call work.",
        ),
        (
            "affects",
            "Checkout rollout affects Project Zephyr queue latency.",
            "Project Zephyr",
            "Project Zephyr metrics are unaffected by Checkout traffic and load.",
        ),
        (
            "invalidates",
            "The new policy invalidates Legacy Config v1.",
            "Legacy Config",
            "Legacy Config v1 remains valid and is not obsolete under the new policy.",
        ),
    ],
)
def test_typed_relation_rejects_target_clauses_that_deny_the_relation_role(
    relation_type: str,
    source: str,
    target_entity: str,
    denying_target: str,
) -> None:
    conn = _conn()
    _store(
        conn,
        memory_id="positive-source",
        content=source,
        entities=["Checkout", target_entity],
    )
    _store(
        conn,
        memory_id="denying-target",
        content=denying_target,
        entities=[target_entity],
    )

    candidates = extract_relation_candidates(conn, scope_ids=["shared-scope"])

    assert not [
        item
        for item in candidates
        if item["source_memory_id"] == "positive-source"
        and item["target_memory_id"] == "denying-target"
        and item["relation_type"] == relation_type
    ]


@pytest.mark.parametrize(
    ("relation_type", "source_id", "source_content", "positive", "excluded", "target_content"),
    [
        (
            "depends_on",
            "checkout-dependency-list",
            "Checkout depends on PostgreSQL, not Redis.",
            "PostgreSQL",
            "Redis",
            "{entity} is an operational service with runtime health checks.",
        ),
        (
            "owned_by",
            "checkout-owner-list",
            "Checkout is owned by SRE Team, not Platform Team.",
            "SRE Team",
            "Platform Team",
            "{entity} is an operations team with an on-call responsibility role.",
        ),
        (
            "affects",
            "rollout-impact-list",
            "The rollout affects Project Alpha, not Project Zephyr.",
            "Project Alpha",
            "Project Zephyr",
            "{entity} tracks queue latency metrics and runtime impact.",
        ),
        (
            "invalidates",
            "policy-invalidates-list",
            "The policy invalidates Legacy Config A, not Legacy Config B.",
            "Legacy Config A",
            "Legacy Config B",
            "{entity} is a deprecated old configuration superseded by the new policy.",
        ),
    ],
)
def test_typed_relation_rejects_explicitly_excluded_entity_in_positive_clause(
    relation_type,
    source_id,
    source_content,
    positive,
    excluded,
    target_content,
):
    conn = _conn()
    _store(
        conn,
        memory_id=source_id,
        content=source_content,
        entities=[positive, excluded],
    )
    _store(
        conn,
        memory_id="positive-target",
        content=target_content.format(entity=positive),
        entities=[positive],
    )
    _store(
        conn,
        memory_id="excluded-target",
        content=target_content.format(entity=excluded),
        entities=[excluded],
    )

    relations = extract_relation_candidates(conn, scope_ids=["shared-scope"])

    assert any(
        item["source_memory_id"] == source_id
        and item["target_memory_id"] == "positive-target"
        and item["relation_type"] == relation_type
        for item in relations
    )
    assert not any(
        item["source_memory_id"] == source_id
        and item["target_memory_id"] == "excluded-target"
        and item["relation_type"] == relation_type
        for item in relations
    )
    conn.close()


def test_relation_extraction_preserves_manual_same_key_relation_when_refreshing_generated_edges():
    conn = _conn()
    _store(conn, memory_id="atlas-a", content="Project Atlas deploy runbook validates Redis service before rollout.")
    _store(conn, memory_id="atlas-b", content="Project Atlas deploy checklist validates Redis service before rollout.")
    conn.execute(
        """
        INSERT INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at)
        VALUES('atlas-a', 'atlas-b', 'same_topic', 1.0, 'manual-governance:operator-reviewed', '2026-01-01T00:00:00+00:00')
        """
    )
    conn.execute(
        """
        INSERT INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at)
        VALUES('atlas-b', 'atlas-a', 'same_topic', 0.7, 'relation-extraction:old; stale generated edge', '2026-01-01T00:00:00+00:00')
        """
    )
    conn.commit()

    result = rebuild_extracted_relations(conn, scope_ids=["shared-scope"], dry_run=False, batch_id="manual-preserve")

    assert result["deleted"] == 1
    rows = conn.execute(
        """
        SELECT source_memory_id, target_memory_id, relation_type, confidence, note
        FROM memory_relations
        WHERE source_memory_id IN ('atlas-a','atlas-b')
          AND target_memory_id IN ('atlas-a','atlas-b')
        ORDER BY source_memory_id, target_memory_id
        """
    ).fetchall()
    notes = {(row["source_memory_id"], row["target_memory_id"]): row["note"] for row in rows}
    assert notes[("atlas-a", "atlas-b")] == "manual-governance:operator-reviewed"
    assert notes[("atlas-b", "atlas-a")].startswith("relation-extraction:manual-preserve")


def test_relation_extraction_does_not_treat_current_or_latest_as_supersession():
    conn = _conn()
    _store(
        conn,
        memory_id="atlas-old-url",
        content="Project Atlas base URL used old endpoint https://old-atlas.invalid/v1.",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    _store(
        conn,
        memory_id="atlas-current-owner",
        content="Project Atlas current owner is Platform Team for rollout reviews.",
        updated_at="2026-02-01T00:00:00+00:00",
    )

    candidates = extract_relation_candidates(conn, scope_ids=["shared-scope"])
    supersedes_pairs = {
        (item["source_memory_id"], item["target_memory_id"])
        for item in candidates
        if item["relation_type"] == "supersedes"
    }

    assert ("atlas-current-owner", "atlas-old-url") not in supersedes_pairs


def test_sync_relation_extraction_preserves_generated_edges_outside_pair_budget():
    conn = _conn()
    _store(
        conn,
        memory_id="old-a",
        content="Project Atlas deploy runbook validates stable health checks before rollout.",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    _store(
        conn,
        memory_id="old-b",
        content="Project Atlas deploy checklist validates stable health checks before rollout.",
        updated_at="2026-01-02T00:00:00+00:00",
    )
    conn.execute(
        """
        INSERT INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at)
        VALUES('old-a', 'old-b', 'same_topic', 0.82, 'relation-extraction:previous; fixture', '2026-01-02T00:00:00+00:00')
        """
    )
    _store(
        conn,
        memory_id="new-focus",
        content="Project Atlas deploy depends on Redis service availability.",
        updated_at="2026-03-01T00:00:00+00:00",
    )
    _store(
        conn,
        memory_id="new-peer",
        content="Redis service runbook: check redis-cli ping before Atlas deploy.",
        updated_at="2026-02-15T00:00:00+00:00",
    )

    result = sync_extracted_relations_for_memory(
        conn,
        memory_id="new-focus",
        scope_ids=["shared-scope"],
        batch_id="budgeted-sync",
        max_pairs=1,
    )

    assert result["deleted"] == 0
    preserved = conn.execute(
        """
        SELECT relation_type, note
        FROM memory_relations
        WHERE source_memory_id = 'old-a' AND target_memory_id = 'old-b' AND relation_type = 'same_topic'
        """
    ).fetchone()
    assert preserved is not None
    assert preserved["note"] == "relation-extraction:previous; fixture"


def test_focus_relation_budget_defers_unscanned_pairs_without_partial_delete():
    conn = _conn()
    _store(
        conn,
        memory_id="focus-newest",
        content="Project Atlas deploy depends on Redis service availability.",
        updated_at="2026-03-01T00:00:00+00:00",
    )
    _store(
        conn,
        memory_id="peer-first",
        content="Redis service runbook: check redis-cli ping before Atlas deploy.",
        updated_at="2026-02-01T00:00:00+00:00",
    )
    _store(
        conn,
        memory_id="peer-truncated",
        content="Unrelated archived newsletter text with no Atlas relationship.",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    conn.execute(
        """
        INSERT INTO memory_relations(
            source_memory_id, target_memory_id, relation_type,
            confidence, note, created_at
        ) VALUES(
            'focus-newest', 'peer-truncated', 'same_topic',
            0.70, 'relation-extraction:stale; must disappear only after full scan',
            '2026-01-01T00:00:00+00:00'
        )
        """
    )
    conn.commit()
    before = [
        tuple(row)
        for row in conn.execute(
            """
            SELECT source_memory_id, target_memory_id, relation_type, confidence, note
            FROM memory_relations
            ORDER BY source_memory_id, target_memory_id, relation_type
            """
        )
    ]

    result = sync_extracted_relations_for_memory(
        conn,
        memory_id="focus-newest",
        scope_ids=["shared-scope"],
        batch_id="fifth-audit-budget",
        max_pairs=1,
    )

    after = [
        tuple(row)
        for row in conn.execute(
            """
            SELECT source_memory_id, target_memory_id, relation_type, confidence, note
            FROM memory_relations
            ORDER BY source_memory_id, target_memory_id, relation_type
            """
        )
    ]
    assert result["ok"] is True
    assert result["blocked"] is True
    assert result["deferred"] is False
    assert result["immediate_status"] == "candidate_cap_exceeded"
    assert result["selected_peer_count"] == 0
    assert result["compared_pairs"] == 0
    stale = conn.execute(
        """
        SELECT note FROM memory_relations
        WHERE source_memory_id='focus-newest'
          AND target_memory_id='peer-truncated'
          AND relation_type='same_topic'
        """
    ).fetchone()
    assert stale is not None
    assert after == before
    queued = conn.execute(
        "SELECT status FROM relation_rebuild_queue WHERE focus_memory_id='focus-newest'"
    ).fetchone()
    assert queued is None


def test_relation_extraction_does_not_add_non_conflict_edges_for_contradicting_pair():
    conn = _conn()
    _store(conn, memory_id="atlas-redis", content="Project Atlas deploy depends on Redis service availability.")
    _store(conn, memory_id="redis-runbook", content="Redis service runbook: check redis-cli ping before Atlas deploy.")
    conn.execute(
        """
        INSERT INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at)
        VALUES('atlas-redis', 'redis-runbook', 'contradicts', 1.0, 'fixture-conflict', '2026-01-01T00:00:00+00:00')
        """
    )
    conn.commit()

    candidates = extract_relation_candidates(conn, scope_ids=["shared-scope"])
    blocked_types = {"same_topic", "supersedes", "depends_on", "owned_by", "affects"}

    assert not [
        item
        for item in candidates
        if {item["source_memory_id"], item["target_memory_id"]} == {"atlas-redis", "redis-runbook"}
        and item["relation_type"] in blocked_types
    ]

    rebuild_extracted_relations(conn, scope_ids=["shared-scope"], dry_run=False, batch_id="conflict-skip")
    rows = conn.execute(
        """
        SELECT source_memory_id, target_memory_id, relation_type, note
        FROM memory_relations
        WHERE source_memory_id IN ('atlas-redis', 'redis-runbook')
           OR target_memory_id IN ('atlas-redis', 'redis-runbook')
        """
    ).fetchall()
    pair_rows = [row for row in rows if {row["source_memory_id"], row["target_memory_id"]} == {"atlas-redis", "redis-runbook"}]
    assert [(row["relation_type"], row["note"]) for row in pair_rows] == [("contradicts", "fixture-conflict")]


def test_update_memory_rebuilds_extracted_relations_for_updated_content(tmp_path):
    _disable_unrelated_vector_runtime(tmp_path)
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-relation-update",
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id="joy",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        source = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "Project Atlas deploy depends on Redis service availability.",
                    "target": "project",
                    "memory_type": "procedure",
                    "entities": ["Project Atlas"],
                    "allow_duplicate": True,
                },
            )
        )
        target = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "Redis service runbook: check redis-cli ping before Atlas deploy.",
                    "target": "project",
                    "memory_type": "procedure",
                    "entities": ["Redis service"],
                    "allow_duplicate": True,
                },
            )
        )
        source_id = source["id"]
        target_id = target["id"]
        conn = plugin._require_conn()
        before = conn.execute(
            """
            SELECT relation_type
            FROM memory_relations
            WHERE source_memory_id = ? AND target_memory_id = ? AND relation_type = 'depends_on'
            """,
            (source_id, target_id),
        ).fetchall()
        assert before

        updated, _, _ = plugin._update_memory(source_id, "Project Atlas release notes mention documentation cleanup only.", "project")
        assert updated is True

        after = conn.execute(
            """
            SELECT relation_type, note
            FROM memory_relations
            WHERE source_memory_id = ? AND target_memory_id = ? AND relation_type = 'depends_on'
            """,
            (source_id, target_id),
        ).fetchall()
    finally:
        plugin.shutdown()

    assert after == []


def test_provider_store_adds_rebuildable_relation_edges(tmp_path):
    _disable_unrelated_vector_runtime(tmp_path)
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-relation-extraction",
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id="joy",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        first = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "Project Atlas v1 deploy command uses old atlasctl deploy.",
                    "target": "project",
                    "memory_type": "factual",
                    "entities": ["Project Atlas"],
                    "allow_duplicate": True,
                },
            )
        )
        second = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "Project Atlas v2 supersedes v1 deploy command and uses uv run atlas deploy.",
                    "target": "project",
                    "memory_type": "factual",
                    "entities": ["Project Atlas"],
                    "allow_duplicate": True,
                },
            )
        )
        with plugin._lock:
            rows = plugin._require_conn().execute(
                """
                SELECT relation_type, source_memory_id, target_memory_id
                FROM memory_relations
                WHERE source_memory_id IN (?, ?) OR target_memory_id IN (?, ?)
                """,
                (first["id"], second["id"], first["id"], second["id"]),
            ).fetchall()
    finally:
        plugin.shutdown()

    assert any(row["relation_type"] == "same_topic" for row in rows)
    assert any(row["relation_type"] == "supersedes" and row["source_memory_id"] == second["id"] for row in rows)


def test_relation_extraction_ignores_generic_runtime_entities():
    conn = _conn()
    _store(
        conn,
        memory_id="generic-contract",
        content="Hermes provider selection depends on the max reasoning effort contract.",
        entities=["hermes", "provider", "model", "max"],
    )
    _store(
        conn,
        memory_id="generic-cache",
        content="Hermes release packaging records provider cache age and model inventory.",
        entities=["hermes", "provider", "model", "max"],
    )
    candidates = extract_relation_candidates(conn, scope_ids=["shared-scope"])
    assert not [
        item
        for item in candidates
        if {item["source_memory_id"], item["target_memory_id"]} == {"generic-contract", "generic-cache"}
    ]


def test_relation_extraction_allows_distinctive_two_character_cjk_entity():
    conn = _conn()
    _store(
        conn,
        memory_id="deployment-checklist",
        content="部署任务明确依赖于青岚完成最终验收。",
        entities=["部署任务"],
    )
    _store(
        conn,
        memory_id="qinglan-owner",
        content="青岚负责最终验收规则与结果签收。",
        entities=["青岚"],
    )
    candidates = extract_relation_candidates(conn, scope_ids=["shared-scope"])
    pair_types = {
        (item["source_memory_id"], item["target_memory_id"], item["relation_type"])
        for item in candidates
    }
    assert ("deployment-checklist", "qinglan-owner", "depends_on") in pair_types


def test_relation_extraction_ignores_corpus_high_frequency_entity_without_static_stoplist_entry():
    conn = _conn()
    _store(
        conn,
        memory_id="novel-alpha",
        content="Service deployment current status alpha orchid.",
        entities=["Novel Hub"],
    )
    _store(
        conn,
        memory_id="novel-beta",
        content="Service deployment current status beta quartz.",
        entities=["Novel Hub"],
    )
    for index in range(23):
        _store(
            conn,
            memory_id=f"novel-noise-{index}",
            content=f"Service deployment current status noise{index}.",
            entities=["Novel Hub"],
        )
    candidates = extract_relation_candidates(conn, scope_ids=["shared-scope"])
    assert not [
        item
        for item in candidates
        if {item["source_memory_id"], item["target_memory_id"]} == {"novel-alpha", "novel-beta"}
    ]
    for peer_count in (0, 1, 3, 11):
        chunk_ids = [
            "novel-alpha",
            "novel-beta",
            *(f"novel-noise-{index}" for index in range(peer_count)),
        ]
        chunk_candidates = extract_relation_candidates(
            conn,
            scope_ids=["shared-scope"],
            memory_ids=chunk_ids,
        )
        assert not [
            item
            for item in chunk_candidates
            if {item["source_memory_id"], item["target_memory_id"]}
            == {"novel-alpha", "novel-beta"}
        ]


def test_multi_scope_scan_does_not_leak_high_frequency_entities_between_scopes():
    """Adding a noisy scope must not change relation results in a quiet scope."""

    conn = _conn()
    _store(
        conn,
        scope_id="scope-quiet",
        memory_id="quiet-alpha",
        content="Service deployment current status alpha orchid.",
        entities=["Novel Hub"],
    )
    _store(
        conn,
        scope_id="scope-quiet",
        memory_id="quiet-beta",
        content="Service deployment current status beta quartz.",
        entities=["Novel Hub"],
    )
    for index in range(25):
        _store(
            conn,
            scope_id="scope-noisy",
            memory_id=f"noisy-{index:02d}",
            content=f"Unrelated noisy scope status marker {index}.",
            entities=["Novel Hub"],
        )

    quiet_only = extract_relation_candidates(conn, scope_ids=["scope-quiet"])
    combined = extract_relation_candidates(
        conn, scope_ids=["scope-quiet", "scope-noisy"]
    )
    quiet_pair = {"quiet-alpha", "quiet-beta"}
    quiet_only_edges = {
        (item["source_memory_id"], item["target_memory_id"], item["relation_type"])
        for item in quiet_only
        if {item["source_memory_id"], item["target_memory_id"]} == quiet_pair
    }
    combined_edges = {
        (item["source_memory_id"], item["target_memory_id"], item["relation_type"])
        for item in combined
        if {item["source_memory_id"], item["target_memory_id"]} == quiet_pair
    }

    assert quiet_only_edges
    assert combined_edges == quiet_only_edges


def test_relation_rebuild_fanout_gate_preserves_existing_edges():
    conn = _conn()
    _store(
        conn,
        memory_id="atlas-focus",
        content="Project Atlas deploy depends on Redis service availability.",
    )
    _store(
        conn,
        memory_id="atlas-peer-a",
        content="Project Atlas Redis service deployment runbook and availability checks.",
    )
    _store(
        conn,
        memory_id="atlas-peer-b",
        content="Project Atlas Redis service deployment checklist and availability checks.",
    )
    conn.execute(
        """
        INSERT INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at)
        VALUES('atlas-focus', 'atlas-peer-a', 'same_topic', 0.91, 'relation-extraction:reviewed; keep-on-block', '2026-01-01T00:00:00+00:00')
        """
    )
    conn.commit()
    result = rebuild_extracted_relations(
        conn,
        scope_ids=["shared-scope"],
        focus_memory_ids=["atlas-focus"],
        dry_run=False,
        batch_id="fanout-block",
        max_candidates=1,
    )
    assert result["ok"] is False
    assert result["blocked"] is True
    assert result["candidate_count"] > 1
    assert result["deleted"] == 0
    assert result["inserted"] == 0
    preserved = conn.execute(
        "SELECT note FROM memory_relations WHERE source_memory_id = 'atlas-focus' AND target_memory_id = 'atlas-peer-a'"
    ).fetchone()
    assert preserved is not None
    assert preserved["note"] == "relation-extraction:reviewed; keep-on-block"
