"""Blocking regressions from the 2026-07-17 seventh independent audit."""

from __future__ import annotations

import ast
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from scope_recall.evolution_policy import evaluate_evolution_policy
from scope_recall.fact_actions import (
    ClaimDraft,
    EvidenceReference,
    EvolutionAction,
    EvolutionPlan,
    EvolutionProposal,
)
from scope_recall.fact_executor import FactExecutionContext, execute_fact_plan
from scope_recall.fact_repository import insert_claim
from scope_recall.maintenance_ops import memory_db_path
from scope_recall.sql_store import ensure_schema
from scope_recall.temporal_query import (
    _current_recall_claim_ids,
    _fts_token_routes,
    query_current_fact_views,
)
from scope_recall.tooling import ScopeRecallToolService
from scope_recall.truth_connection import connect_truth_database
from scope_recall.vector_generation import CURRENT_GENERATION_KEY, ensure_vector_generation_schema

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_REVIEW_SCRIPT = PLUGIN_ROOT / "scripts" / "candidate.review.py"
JOURNAL_RECOVERY_SCRIPT = PLUGIN_ROOT / "scripts" / "journal.recovery.py"
TEMPORAL_SCALE_SCRIPT = PLUGIN_ROOT / "scripts" / "benchmark.temporal_scale.py"


def _add_plan(subject: str, predicate: str, value: str, quote: str) -> EvolutionPlan:
    proposal = EvolutionProposal(
        action=EvolutionAction.ADD,
        raw_action="add",
        claim=ClaimDraft.from_parts(
            subject=subject,
            predicate=predicate,
            value=value,
            scope_id="scope-a",
        ),
        evidence_refs=(
            EvidenceReference(
                source_type="user_message",
                source_id="seventh-audit",
                quote=quote,
                speaker_subject=subject,
            ),
        ),
        confidence=0.99,
        reason="seventh independent audit regression",
        source="audit",
    )
    return EvolutionPlan(
        proposal=proposal,
        action_id="seventh-audit",
        idempotency_key="seventh-audit",
        policy_mode="auto_apply",
    )


def _fact_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in (
            "memories",
            "memories_fts",
            "fact_claims",
            "fact_claims_fts",
            "fact_claim_evidence",
            "fact_freshness",
            "governance_audit_events",
            "vector_outbox",
            "fact_action_receipts",
        )
    }


@pytest.mark.parametrize(
    "quote",
    (
        "当时我住在北京。",
        "那时候我住在北京。",
        "起初我住在北京。",
        "早年我住在北京。",
        "一度我住在北京。",
        "之前我住在北京。",
        "前阵子我住在北京。",
        "小时候我住在北京。",
        "彼时我住在北京。",
    ),
)
def test_cjk_historical_adverbial_prefixes_are_review_and_zero_write(quote: str) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_schema(conn)
    ensure_vector_generation_schema(conn)
    conn.execute(
        "INSERT INTO vector_generation_state(key, value, updated_at) VALUES (?, 'gen-seventh', ?)",
        (CURRENT_GENERATION_KEY, "2026-07-17T00:00:00+00:00"),
    )
    conn.commit()
    plan = _add_plan("我", "住在", "北京", quote)
    policy = evaluate_evolution_policy(plan.proposal)
    before = _fact_counts(conn)

    result = execute_fact_plan(
        conn,
        plan,
        policy,
        FactExecutionContext(
            scope_id="scope-a",
            writable_scope_ids=("scope-a",),
            actor="scope-recall:seventh-audit",
            timestamp="2026-07-17T00:00:00+00:00",
            source="fact_evolution",
            target="memory",
            session_id="seventh-audit-session",
            platform="test",
            user_id="audit-user",
            new_memory_id="seventh-audit-memory",
            new_claim_id="seventh-audit-claim",
            metadata={"memory_type": "fact"},
        ),
    )

    assert policy.allowed is False
    assert policy.effective_action is EvolutionAction.REVIEW
    assert result.applied is False
    assert result.status == "review"
    assert _fact_counts(conn) == before
    conn.close()


@pytest.mark.parametrize(
    ("predicate", "value", "quote"),
    (
        ("住在", "无锡", "我现在住在无锡。"),
        ("喜欢", "非洲音乐", "我现在喜欢非洲音乐。"),
        ("住在", "莫斯科", "我现在住在莫斯科。"),
        ("住在", "未央区", "我目前住在未央区。"),
        ("拥有", "一套别墅", "我现在拥有一套别墅。"),
    ),
)
def test_cjk_proper_nouns_do_not_count_as_negation(
    predicate: str,
    value: str,
    quote: str,
) -> None:
    policy = evaluate_evolution_policy(_add_plan("我", predicate, value, quote).proposal)
    assert policy.allowed is True
    assert policy.effective_action is EvolutionAction.ADD


def test_real_cjk_negation_remains_review_only() -> None:
    policy = evaluate_evolution_policy(
        _add_plan("我", "住在", "北京", "我现在不住在北京。").proposal
    )
    assert policy.allowed is False
    assert policy.effective_action is EvolutionAction.REVIEW


@pytest.mark.parametrize("value", ("我不是药神", "《我不是药神》"))
def test_cjk_negation_inside_aligned_title_does_not_flip_polarity(value: str) -> None:
    policy = evaluate_evolution_policy(
        _add_plan("我", "喜欢", value, "我现在喜欢《我不是药神》。").proposal
    )

    assert policy.allowed is True
    assert policy.effective_action is EvolutionAction.ADD


@pytest.mark.parametrize(
    "quote",
    (
        "我现在不喜欢《我不是药神》。",
        "我现在并不喜欢《我不是药神》。",
        "我可能喜欢《我不是药神》。",
        "我以前喜欢《我不是药神》。",
        "我现在喜欢的不是《我不是药神》。",
    ),
)
def test_cjk_title_masking_does_not_hide_frame_level_non_authority(quote: str) -> None:
    policy = evaluate_evolution_policy(
        _add_plan("我", "喜欢", "《我不是药神》", quote).proposal
    )

    assert policy.allowed is False
    assert policy.effective_action is EvolutionAction.REVIEW


@pytest.mark.parametrize(
    "quote",
    (
        "我还住在北京。",
        "我确实住在北京。",
        "我主要住在北京。",
        "我就住在北京。",
        "我住在北京呀。",
    ),
)
def test_cjk_controlled_current_focus_and_final_particles_are_authoritative(
    quote: str,
) -> None:
    policy = evaluate_evolution_policy(_add_plan("我", "住在", "北京", quote).proposal)

    assert policy.allowed is True
    assert policy.effective_action is EvolutionAction.ADD


@pytest.mark.parametrize(
    "quote",
    (
        "我还没住在北京。",
        "我确实不住在北京。",
        "我主要不住在北京。",
        "我就不住在北京。",
        "我不住在北京呀。",
        "我还住在北京吗？",
        "当时我还住在北京。",
        "明年我就住在北京。",
        "我暂时住在北京呀。",
    ),
)
def test_cjk_focus_matrix_keeps_negative_noncurrent_frames_review_only(
    quote: str,
) -> None:
    policy = evaluate_evolution_policy(_add_plan("我", "住在", "北京", quote).proposal)

    assert policy.allowed is False
    assert policy.effective_action is EvolutionAction.REVIEW


def _bulk_seed_complete_fts(conn: sqlite3.Connection, count: int) -> None:
    now = "2026-07-17T00:00:00+00:00"
    conn.executemany(
        """
        INSERT INTO memories(
            id, scope_id, source, target, content, summary,
            created_at, updated_at, dedup_key, metadata
        ) VALUES (?, 'scope-a', 'audit', 'memory', ?, ?, ?, ?, ?, ?)
        """,
        (
            (
                f"memory-{index}",
                f"subject {index} owns value {index}",
                f"subject {index} owns value {index}",
                now,
                now,
                f"dedup-{index}",
                '{"lifecycle":"promoted","memory_type":"factual"}',
            )
            for index in range(count)
        ),
    )
    conn.executemany(
        """
        INSERT INTO memories_fts(memory_id, content, summary)
        VALUES (?, ?, ?)
        """,
        (
            (
                f"memory-{index}",
                f"subject {index} owns value {index}",
                f"subject {index} owns value {index}",
            )
            for index in range(count)
        ),
    )
    conn.executemany(
        """
        INSERT INTO memory_entities(memory_id, entity, weight, source)
        VALUES (?, ?, 1.0, 'audit-fixture')
        """,
        ((f"memory-{index}", f"subject-{index}") for index in range(count)),
    )
    conn.executemany(
        """
        INSERT INTO fact_claims(
            claim_id, memory_id, scope_id, subject_key, predicate_key,
            fact_key, value, normalized_value, value_fingerprint,
            cardinality, assertion_kind, recorded_at, status, confidence,
            source_type, source_ref, evidence_hash, metadata
        ) VALUES (?, ?, 'scope-a', ?, 'owns', ?, ?, ?, ?, 'single',
                  'direct', ?, 'current', 0.9, 'user_message', ?, '', '{}')
        """,
        (
            (
                f"claim-{index}",
                f"memory-{index}",
                f"subject {index}",
                f"subject-{index}:owns",
                f"value {index}",
                f"value {index}",
                f"fingerprint-{index}",
                now,
                f"message-{index}",
            )
            for index in range(count)
        ),
    )
    conn.commit()


def test_complete_fts_second_start_is_bounded_and_write_free() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _bulk_seed_complete_fts(conn, 5_000)
    assert conn.execute("SELECT COUNT(*) FROM fact_claims_fts").fetchone()[0] == 5_000
    before_changes = conn.total_changes

    started = time.perf_counter()
    ensure_schema(conn, commit=False)
    elapsed = time.perf_counter() - started

    assert elapsed < 1.5, f"second ensure_schema took {elapsed:.3f}s for 5K complete rows"
    assert conn.total_changes == before_changes
    membership = conn.execute(
        "SELECT COUNT(*) FROM fact_claims_fts_membership"
    ).fetchone()[0]
    assert membership == 5_000
    conn.close()


def _seed_recall_fact(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    claim_id: str,
    value: str,
    content: str,
) -> None:
    now = "2026-07-17T00:00:00+00:00"
    conn.execute(
        """
        INSERT INTO memories(
            id, scope_id, source, target, content, summary,
            created_at, updated_at, metadata
        ) VALUES (?, 'scope-a', 'audit', 'memory', ?, ?, ?, ?, ?)
        """,
        (
            memory_id,
            content,
            content,
            now,
            now,
            '{"lifecycle":"promoted","memory_type":"factual"}',
        ),
    )
    insert_claim(
        conn,
        claim_id=claim_id,
        memory_id=memory_id,
        scope_id="scope-a",
        # Keep every distractor in a distinct single-value fact slot while all
        # rows still share the same first twelve FTS query terms.
        subject=f"alpha beta {memory_id}",
        predicate="gamma delta",
        value=value,
        valid_from="2026-01-01T00:00:00+00:00",
        recorded_at=now,
        source_type="user_message",
        source_ref=claim_id,
        confidence=0.9,
    )


def test_long_query_tail_terms_survive_real_candidate_limit() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    prefix = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu"
    for index in range(1_001):
        _seed_recall_fact(
            conn,
            memory_id=f"distractor-{index:04d}",
            claim_id=f"distractor-claim-{index:04d}",
            value="epsilon zeta eta theta iota kappa lambda mu",
            content=prefix,
        )
    _seed_recall_fact(
        conn,
        memory_id="correct",
        claim_id="zzzz-correct-claim",
        value="epsilon zeta eta theta iota kappa lambda mu phone number",
        content=f"{prefix} phone number",
    )
    conn.commit()
    diagnostics: dict[str, object] = {}

    result = query_current_fact_views(
        conn,
        scope_ids=["scope-a"],
        query=f"{prefix} phone number",
        valid_at="2026-07-17T00:00:00+00:00",
        limit=10,
        diagnostics=diagnostics,
    )

    assert diagnostics["candidate_limit"] == 1_000
    assert diagnostics["truncated"] is True
    assert diagnostics["complete"] is False
    assert result and result[0].memory_id == "correct"
    conn.close()


@pytest.mark.parametrize("token_count", [13, 24, 60, 120, 200])
def test_ninth_audit_routes_cover_every_token_through_two_hundred(
    token_count: int,
) -> None:
    tokens = [f"term{index}" for index in range(token_count)]

    routes = _fts_token_routes(tokens)
    covered = {token for route in routes for token in route}

    assert covered == set(tokens)
    assert all(1 <= len(route) <= 12 for route in routes)


def test_ninth_audit_middle_decisive_token_enters_candidate_pool() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    tokens = [f"term{index}" for index in range(60)]
    distractor_text = " ".join(tokens[:12])
    for index in range(10):
        _seed_recall_fact(
            conn,
            memory_id=f"middle-distractor-{index:02d}",
            claim_id=f"middle-distractor-claim-{index:02d}",
            value=distractor_text,
            content=distractor_text,
        )
    _seed_recall_fact(
        conn,
        memory_id="middle-correct",
        claim_id="middle-correct-claim",
        value="term12",
        content="term12",
    )
    conn.commit()

    claim_ids, diagnostics = _current_recall_claim_ids(
        conn,
        scope_ids=["scope-a"],
        semantic_at="2026-07-17T00:00:00+00:00",
        query=" ".join(tokens),
    )

    assert "middle-correct-claim" in claim_ids
    assert diagnostics["token_count"] == 60
    assert diagnostics["covered_token_count"] == 60
    assert diagnostics["token_coverage_complete"] is True
    assert diagnostics["complete"] is True
    conn.close()


def test_ninth_audit_extreme_query_reports_incomplete_token_coverage() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    alphabet = "abcdefghijklmnopqrstuvwxyz"
    tokens = [left + right for left in alphabet for right in alphabet][:300]

    _, diagnostics = _current_recall_claim_ids(
        conn,
        scope_ids=["scope-a"],
        semantic_at="2026-07-17T00:00:00+00:00",
        query=" ".join(tokens),
    )

    assert diagnostics["token_count"] > diagnostics["covered_token_count"]
    assert diagnostics["token_coverage_complete"] is False
    assert diagnostics["truncated"] is False
    assert diagnostics["complete"] is False
    assert len(diagnostics["token_routes"]) == 20
    conn.close()


def test_public_recall_json_exposes_incomplete_candidate_diagnostics() -> None:
    service = object.__new__(ScopeRecallToolService)
    item = SimpleNamespace(
        id="memory-1",
        content="content",
        summary="summary",
        source="audit",
        target="memory",
        score=0.9,
        metadata={
            "temporal_candidate_diagnostics": {
                "strategy": "fts5_bm25_multi_route",
                "candidate_limit": 1_000,
                "candidate_count": 1_000,
                "raw_unique_candidate_count": 1_501,
                "truncated": True,
                "complete": False,
                "route_candidate_counts": [1_001, 500, 400],
                "token_count": 60,
                "covered_token_count": 30,
                "token_coverage_complete": False,
                "semantic_tokens": ["private-query-token"],
            }
        },
    )

    payload = service._serialize_recall_item(item)

    assert payload["temporal_candidate_diagnostics"] == {
        "strategy": "fts5_bm25_multi_route",
        "candidate_limit": 1_000,
        "candidate_count": 1_000,
        "raw_unique_candidate_count": 1_501,
        "truncated": True,
        "complete": False,
        "route_candidate_counts": [1_001, 500, 400],
        "token_count": 60,
        "covered_token_count": 30,
        "token_coverage_complete": False,
    }


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=PLUGIN_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def test_truth_read_connection_enforces_query_only(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.sqlite3"
    seed = sqlite3.connect(db_path)
    try:
        seed.execute("CREATE TABLE sentinel(value TEXT)")
        seed.commit()
    finally:
        seed.close()

    conn = connect_truth_database(db_path, mode="ro")
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO sentinel(value) VALUES ('forbidden')")
    finally:
        conn.close()


def test_candidate_review_missing_db_dry_run_creates_no_file(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.sqlite3"
    before = sorted(path.name for path in tmp_path.iterdir())
    result = _run(
        str(CANDIDATE_REVIEW_SCRIPT),
        "promote",
        "--db",
        str(db_path),
        "--id",
        "missing",
        "--json",
    )

    assert result.returncode != 0
    assert not db_path.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == before
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error_code"] == "truth_db_missing"


def test_journal_recovery_missing_db_dry_run_creates_no_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    db_path = memory_db_path(home)
    db_path.parent.mkdir(parents=True)
    before = sorted(path.name for path in db_path.parent.iterdir())
    result = _run(
        str(JOURNAL_RECOVERY_SCRIPT),
        "--hermes-home",
        str(home),
        "--format",
        "json",
    )

    assert result.returncode != 0
    assert not db_path.exists()
    assert sorted(path.name for path in db_path.parent.iterdir()) == before
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error_code"] == "truth_db_missing"


def test_temporal_scale_gate_checks_real_truncated_diagnostic_key() -> None:
    source = TEMPORAL_SCALE_SCRIPT.read_text(encoding="utf-8")
    assert 'diagnostics.get("candidate_truncated")' not in source
    assert 'diagnostics.get("truncated")' in source


def test_shipped_truth_connections_use_the_unified_boundary() -> None:
    """Only non-truth benchmark/backup/vector SQLite connections are exempt."""

    allowed_direct_connections = {
        ("benchmark.graph_relations.py", "':memory:'"),
        ("benchmark.memory_evolution.py", "':memory:'"),
        ("benchmark.reflection.py", "':memory:'"),
        ("benchmark.temporal_scale.py", "path"),
        ("migrate.legacy_hygiene.py", "backup_path"),
        ("playbooks.py", "temporary_path"),
        ("repair.vector_index.py", "f'file:{target}?mode=ro'"),
        ("report.hygiene.py", "f'file:{self.vector_path}?mode=ro'"),
    }
    actual: set[tuple[str, str]] = set()
    for path in sorted((PLUGIN_ROOT / "scripts").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if not (
                isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id == "sqlite3"
                and function.attr == "connect"
            ):
                continue
            first_argument = ast.unparse(node.args[0]) if node.args else ""
            actual.add((path.name, first_argument))
    assert actual == allowed_direct_connections
