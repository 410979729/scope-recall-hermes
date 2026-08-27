#!/usr/bin/env python3
"""Deterministic Fact Evolution and bitemporal query benchmark."""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "benchmarks" / "memory_evolution_cases.json"


def _bootstrap_source_package() -> None:
    """Prefer this checkout even when an older scope_recall is installed."""

    if str(ROOT.parent) not in sys.path:
        sys.path.insert(0, str(ROOT.parent))
    try:
        importlib.import_module("scope_recall.fact_repository")
        return
    except ImportError:
        for name in list(sys.modules):
            if name == "scope_recall" or name.startswith("scope_recall."):
                sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        "scope_recall",
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to bootstrap scope_recall source package")
    module = importlib.util.module_from_spec(spec)
    sys.modules["scope_recall"] = module
    spec.loader.exec_module(module)


_bootstrap_source_package()

from scope_recall.evolution_policy import evaluate_evolution_policy  # noqa: E402
from scope_recall.fact_actions import (  # noqa: E402
    ClaimDraft,
    EvidenceReference,
    EvolutionAction,
    EvolutionProposal,
)
from scope_recall.fact_evolution import execute_pipeline_proposal  # noqa: E402
from scope_recall.journal import apply_journal_candidates  # noqa: E402
from scope_recall.journal_candidates import JournalDigestCandidate  # noqa: E402
from scope_recall.journal_store import (  # noqa: E402
    append_journal_entry,
    ensure_journal_schema,
)
from scope_recall.models import RuntimeScope, recall_scope_id_for_target  # noqa: E402
from scope_recall.nightly_digest import (  # noqa: E402
    MessageRecord,
    SessionBundle,
    _parse_llm_candidates_with_status,
    session_chunks,
)
from scope_recall.scope import build_scope_id, build_shared_scope_id  # noqa: E402
from scope_recall.fact_repository import (  # noqa: E402
    FACT_EXECUTOR_PENDING_SUCCESSOR_AUTHORITY,
    close_claim_interval,
    insert_claim,
    retract_claim,
)
from scope_recall.fact_identity import canonical_fact_key  # noqa: E402
from scope_recall.sql_store import ensure_schema  # noqa: E402
from scope_recall.temporal_query import query_fact_views  # noqa: E402


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_schema(conn)
    return conn


def _insert_memory(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    scope_id: str,
    content: str,
    updated_at: str,
) -> None:
    metadata = json.dumps(
        {"lifecycle": "active", "memory_type": "factual"},
        sort_keys=True,
        separators=(",", ":"),
    )
    conn.execute(
        """
        INSERT INTO memories(
            id, scope_id, source, target, content, summary,
            created_at, updated_at, metadata
        ) VALUES (?, ?, 'benchmark', 'memory', ?, ?, ?, ?, ?)
        """,
        (memory_id, scope_id, content, content, updated_at, updated_at, metadata),
    )
    conn.execute(
        "INSERT INTO memories_fts(memory_id, content, summary) VALUES (?, ?, ?)",
        (memory_id, content, content),
    )


def _insert_current_claim(
    conn: sqlite3.Connection,
    *,
    case_id: str,
    suffix: str,
    scope_id: str,
    subject: str,
    predicate: str,
    value: str,
    cardinality: str,
    valid_from: str,
    recorded_at: str,
) -> tuple[str, str]:
    memory_id = f"memory-{case_id}-{suffix}"
    claim_id = f"claim-{case_id}-{suffix}"
    _insert_memory(
        conn,
        memory_id=memory_id,
        scope_id=scope_id,
        content=f"{subject} {predicate}: {value}",
        updated_at=recorded_at,
    )
    insert_claim(
        conn,
        claim_id=claim_id,
        memory_id=memory_id,
        scope_id=scope_id,
        subject=subject,
        predicate=predicate,
        value=value,
        cardinality=cardinality,
        valid_from=valid_from,
        recorded_at=recorded_at,
        confidence=0.99,
        source_type="benchmark_fixture",
        source_ref=case_id,
    )
    return memory_id, claim_id


def _values(views: list[Any]) -> list[str]:
    return [str(item.claim.value) for item in views]


def _query(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    action: str,
    subject: str,
    predicate: str,
    at: str | None = None,
    known_at: str | None = None,
) -> list[Any]:
    return query_fact_views(
        conn,
        scope_ids=[scope_id],
        action=action,
        subject=subject,
        predicate=predicate,
        at=at,
        known_at=known_at,
        limit=20,
    )


def _transition_case(case: dict[str, Any]) -> dict[str, Any]:
    conn = _connection()
    scope_id = "scope-a"
    try:
        _, old_claim = _insert_current_claim(
            conn,
            case_id=case["id"],
            suffix="old",
            scope_id=scope_id,
            subject=case["subject"],
            predicate=case["predicate"],
            value=case["before"],
            cardinality="single",
            valid_from=case["valid_before"],
            recorded_at=case["recorded_before"],
        )
        successor_claim = f"claim-{case['id']}-new"
        close_claim_interval(
            conn,
            claim_id=old_claim,
            valid_to=case["valid_after"],
            retired_at=case["recorded_after"],
            status="superseded",
            superseded_by_claim_id=successor_claim,
            pending_successor_scope_id=scope_id,
            pending_successor_fact_key=canonical_fact_key(
                case["subject"], case["predicate"]
            ),
            pending_successor_authority=FACT_EXECUTOR_PENDING_SUCCESSOR_AUTHORITY,
        )
        _insert_current_claim(
            conn,
            case_id=case["id"],
            suffix="new",
            scope_id=scope_id,
            subject=case["subject"],
            predicate=case["predicate"],
            value=case["after"],
            cardinality="single",
            valid_from=case["valid_after"],
            recorded_at=case["recorded_after"],
        )
        conn.commit()
        current = _values(
            _query(
                conn,
                scope_id=scope_id,
                action="current",
                subject=case["subject"],
                predicate=case["predicate"],
            )
        )
        as_of_old = _values(
            _query(
                conn,
                scope_id=scope_id,
                action="as_of",
                subject=case["subject"],
                predicate=case["predicate"],
                at=case["valid_before"],
            )
        )
        history = _values(
            _query(
                conn,
                scope_id=scope_id,
                action="history",
                subject=case["subject"],
                predicate=case["predicate"],
            )
        )
        stale = int(case["before"] in current)
        passed = (
            current == [case["after"]]
            and as_of_old == [case["before"]]
            and set(history) == {case["before"], case["after"]}
        )
        return {"passed": passed, "stale_current_leakage": stale}
    finally:
        conn.close()


def _multi_case(case: dict[str, Any]) -> dict[str, Any]:
    conn = _connection()
    try:
        for index, value in enumerate(case["values"]):
            _insert_current_claim(
                conn,
                case_id=case["id"],
                suffix=str(index),
                scope_id="scope-a",
                subject=case["subject"],
                predicate=case["predicate"],
                value=value,
                cardinality="multi",
                valid_from=case["valid_from"],
                recorded_at=case["recorded_at"],
            )
        conn.commit()
        current = _values(
            _query(
                conn,
                scope_id="scope-a",
                action="current",
                subject=case["subject"],
                predicate=case["predicate"],
            )
        )
        return {
            "passed": set(current) == set(case["values"]),
            "stale_current_leakage": 0,
        }
    finally:
        conn.close()


def _retract_case(case: dict[str, Any]) -> dict[str, Any]:
    conn = _connection()
    try:
        _, claim_id = _insert_current_claim(
            conn,
            case_id=case["id"],
            suffix="old",
            scope_id="scope-a",
            subject=case["subject"],
            predicate=case["predicate"],
            value=case["value"],
            cardinality="single",
            valid_from=case["valid_from"],
            recorded_at=case["recorded_at"],
        )
        retract_claim(
            conn,
            claim_id=claim_id,
            valid_to=case["valid_to"],
            retired_at=case["retired_at"],
        )
        conn.commit()
        current = _values(
            _query(
                conn,
                scope_id="scope-a",
                action="current",
                subject=case["subject"],
                predicate=case["predicate"],
            )
        )
        history = _values(
            _query(
                conn,
                scope_id="scope-a",
                action="history",
                subject=case["subject"],
                predicate=case["predicate"],
            )
        )
        return {
            "passed": current == [] and history == [case["value"]],
            "stale_current_leakage": int(case["value"] in current),
        }
    finally:
        conn.close()


def _delayed_case(case: dict[str, Any]) -> dict[str, Any]:
    conn = _connection()
    try:
        _, old_claim = _insert_current_claim(
            conn,
            case_id=case["id"],
            suffix="old",
            scope_id="scope-a",
            subject=case["subject"],
            predicate=case["predicate"],
            value=case["before"],
            cardinality="single",
            valid_from=case["valid_before"],
            recorded_at=case["recorded_before"],
        )
        successor_claim = f"claim-{case['id']}-new"
        close_claim_interval(
            conn,
            claim_id=old_claim,
            valid_to=case["valid_after"],
            retired_at=case["recorded_after"],
            status="superseded",
            superseded_by_claim_id=successor_claim,
            pending_successor_scope_id="scope-a",
            pending_successor_fact_key=canonical_fact_key(
                case["subject"], case["predicate"]
            ),
            pending_successor_authority=FACT_EXECUTOR_PENDING_SUCCESSOR_AUTHORITY,
        )
        _insert_current_claim(
            conn,
            case_id=case["id"],
            suffix="new",
            scope_id="scope-a",
            subject=case["subject"],
            predicate=case["predicate"],
            value=case["after"],
            cardinality="single",
            valid_from=case["valid_after"],
            recorded_at=case["recorded_after"],
        )
        conn.commit()
        before_knowledge = _values(
            _query(
                conn,
                scope_id="scope-a",
                action="as_of",
                subject=case["subject"],
                predicate=case["predicate"],
                at=case["query_at"],
                known_at=case["known_before"],
            )
        )
        after_knowledge = _values(
            _query(
                conn,
                scope_id="scope-a",
                action="as_of",
                subject=case["subject"],
                predicate=case["predicate"],
                at=case["query_at"],
                known_at=case["known_after"],
            )
        )
        return {
            "passed": before_knowledge == [case["before"]]
            and after_knowledge == [case["after"]],
            "stale_current_leakage": 0,
        }
    finally:
        conn.close()


def _policy_review_case(case: dict[str, Any]) -> dict[str, Any]:
    proposal = EvolutionProposal(
        action=EvolutionAction.REVIEW,
        raw_action="review",
        claim=ClaimDraft.from_parts(
            subject=case["subject"],
            predicate=case["predicate"],
            value=case["value"],
            scope_id="scope-a",
            cardinality="single",
        ),
        evidence_refs=(
            EvidenceReference(
                source_type="user_message",
                source_id=f"benchmark-{case['id']}",
                quote=f"Benchmark fixture for {case['id']}.",
            ),
        ),
        confidence=0.99,
        parser_reasons=(case["reason"],),
        reason=case["reason"],
        source="benchmark",
    )
    decision = evaluate_evolution_policy(proposal, allowed_target_ids=set())
    auto_applied = decision.allowed and decision.effective_action is not EvolutionAction.REVIEW
    return {
        "passed": not auto_applied and decision.effective_action is EvolutionAction.REVIEW,
        "stale_current_leakage": 0,
        "ambiguous_auto_apply": int(auto_applied),
    }


def _scope_case(case: dict[str, Any]) -> dict[str, Any]:
    conn = _connection()
    try:
        for scope_id, value, suffix in (
            ("scope-a", case["scope_a_value"], "a"),
            ("scope-b", case["scope_b_value"], "b"),
        ):
            _insert_current_claim(
                conn,
                case_id=case["id"],
                suffix=suffix,
                scope_id=scope_id,
                subject=case["subject"],
                predicate=case["predicate"],
                value=value,
                cardinality="single",
                valid_from=case["recorded_at"],
                recorded_at=case["recorded_at"],
            )
        conn.commit()
        values = _values(
            _query(
                conn,
                scope_id="scope-a",
                action="current",
                subject=case["subject"],
                predicate=case["predicate"],
            )
        )
        leaked = int(case["scope_b_value"] in values)
        return {
            "passed": values == [case["scope_a_value"]],
            "stale_current_leakage": 0,
            "scope_leakage": leaked,
        }
    finally:
        conn.close()


def _idempotency_probe() -> bool:
    conn = _connection()
    proposal = EvolutionProposal(
        action=EvolutionAction.ADD,
        raw_action="add",
        claim=ClaimDraft.from_parts(
            subject="Idempotency Probe",
            predicate="state",
            value="stable",
            scope_id="scope-a",
            cardinality="single",
            valid_from="2025-01-01T00:00:00+00:00",
        ),
        evidence_refs=(
            EvidenceReference(
                source_type="user_message",
                source_id="benchmark-idempotency",
                quote="Idempotency Probe state is stable.",
            ),
        ),
        confidence=0.99,
        reason="benchmark",
        source="benchmark",
    )
    kwargs = {
        "proposal": proposal,
        "lane": "maintenance",
        "run_id": "benchmark-idempotency",
        "source_key": "benchmark-idempotency",
        "trusted_scope_id": "scope-a",
        "writable_scope_ids": ["scope-a"],
        "actor": "benchmark",
        "source": "benchmark",
        "target": "memory",
        "content": "Idempotency Probe state is stable.",
        "metadata": {
            "memory_type": "factual",
            "confidence": 0.99,
            "digest_run_id": "benchmark-run-a",
        },
        "runtime_config": {
            "fact_evolution": {
                "enabled": True,
                "mode": "auto_apply",
                "maintenance_mode": "reviewed_apply",
            }
        },
        "dry_run": False,
        "provenance_refs": (
            {
                "source_type": "benchmark_fixture",
                "source_ref": "benchmark-idempotency",
                "excerpt": "Idempotency benchmark fixture.",
                "metadata": {"journal_run_id": "benchmark-run-a"},
            },
        ),
    }
    try:
        first = execute_pipeline_proposal(conn, **kwargs)
        before = {
            table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in (
                "memories",
                "fact_claims",
                "fact_claim_evidence",
                "fact_freshness",
                "governance_audit_events",
                "vector_outbox",
                "fact_action_receipts",
            )
        }
        replay_kwargs = {
            **kwargs,
            "run_id": "benchmark-idempotency-new-run",
            "metadata": {
                "memory_type": "factual",
                "confidence": 0.99,
                "digest_run_id": "benchmark-run-b",
            },
            "provenance_refs": (
                {
                    "source_type": "benchmark_fixture",
                    "source_ref": "benchmark-idempotency",
                    "excerpt": "Idempotency benchmark fixture.",
                    "metadata": {"journal_run_id": "benchmark-run-b"},
                },
            ),
        }
        replay = execute_pipeline_proposal(conn, **replay_kwargs)
        after = {
            table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in before
        }
        return (
            first.applied
            and replay.applied
            and replay.status == "replayed"
            and bool(replay.receipt.get("replayed"))
            and before == after
        )
    finally:
        conn.close()


def _evidence_authority_probe() -> bool:
    proposal = EvolutionProposal(
        action=EvolutionAction.ADD,
        raw_action="add",
        claim=ClaimDraft.from_parts(
            subject="Asha",
            predicate="lives in",
            value="Bangalore",
            scope_id="scope-shared",
            cardinality="single",
        ),
        evidence_refs=(
            EvidenceReference(
                source_type="user_message",
                source_id="benchmark-unrelated-user",
                quote="Please remember that I prefer concise weekly reports.",
            ),
            EvidenceReference(
                source_type="model_inference",
                source_id="benchmark-assistant-guess",
                quote="Asha may live in Bangalore, but the user did not state this.",
            ),
        ),
        confidence=0.99,
        reason="adversarial authority laundering probe",
        source="benchmark",
    )
    decision = evaluate_evolution_policy(proposal, allowed_target_ids=set())
    return (
        not decision.allowed
        and decision.effective_action is EvolutionAction.REVIEW
        and decision.direct_source_count == 0
        and "authoritative_evidence_not_claim_supporting" in decision.reason_codes
    )


def _adversarial_evidence_probe() -> dict[str, bool]:
    def proposal(
        *,
        subject: str,
        predicate: str,
        value: str,
        quote: str,
        speaker_subject: str,
    ) -> EvolutionProposal:
        return EvolutionProposal(
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
                    source_id="benchmark-adversarial-message",
                    quote=quote,
                    speaker_subject=speaker_subject,
                ),
            ),
            confidence=0.99,
            reason="adversarial evidence benchmark",
            source="benchmark",
        )

    negative_proposals = [
        proposal(
            subject="Asha",
            predicate="lives in",
            value="Bangalore",
            quote="I don't live in Bangalore.",
            speaker_subject="Asha",
        ),
        proposal(
            subject="Asha",
            predicate="moved to",
            value="Bangalore",
            quote="I haven't moved to Bangalore.",
            speaker_subject="Asha",
        ),
        proposal(
            subject="Asha",
            predicate="住在",
            value="北京",
            quote="我不住在北京。",
            speaker_subject="Asha",
        ),
        proposal(
            subject="Asha",
            predicate="住在",
            value="北京",
            quote="我没住在北京。",
            speaker_subject="Asha",
        ),
    ]
    polarity_gate = all(
        not evaluate_evolution_policy(item, allowed_target_ids=set()).allowed
        for item in negative_proposals
    )
    wrong_subject = proposal(
        subject="UnrelatedPerson",
        predicate="lives in",
        value="Bangalore",
        quote="I live in Bangalore.",
        speaker_subject="ActualSpeaker",
    )
    subject_binding_gate = not evaluate_evolution_policy(
        wrong_subject,
        allowed_target_ids=set(),
    ).allowed

    conn = _connection()
    try:
        tables = (
            "memories",
            "fact_claims",
            "fact_claim_evidence",
            "governance_audit_events",
            "fact_action_receipts",
        )
        before = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }
        changes_before = conn.total_changes
        result = execute_pipeline_proposal(
            conn,
            proposal=negative_proposals[0],
            lane="tool",
            run_id="benchmark-adversarial-evidence",
            source_key="benchmark-adversarial-evidence",
            trusted_scope_id="scope-a",
            writable_scope_ids=["scope-a"],
            actor="benchmark",
            source="benchmark",
            target="memory",
            content="Asha lives in Bangalore.",
            metadata={"memory_type": "factual"},
            runtime_config={
                "fact_evolution": {
                    "enabled": True,
                    "mode": "auto_apply",
                    "tool_mode": "auto_apply",
                }
            },
            dry_run=False,
        )
        after = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }
        zero_write = (
            result.status == "review"
            and not result.applied
            and before == after
            and conn.total_changes == changes_before
        )
    finally:
        conn.close()
    return {
        "evidence_polarity_gate": polarity_gate,
        "evidence_subject_binding_gate": subject_binding_gate,
        "adversarial_evidence_zero_write": zero_write,
    }


def _digest_provenance_budget_probe() -> dict[str, bool]:
    bundle = SessionBundle(
        id="benchmark-long-session",
        title="chunk provenance and budget",
        messages=[
            MessageRecord(
                id=930000 + index,
                session_id="benchmark-long-session",
                role="user",
                content=f"Stable benchmark message {index}: " + "r" * 820,
                timestamp=float(index),
            )
            for index in range(20)
        ],
    )
    chunks = session_chunks(bundle, chunk_chars=1000, max_session_chars=5000)
    if not chunks:
        return {
            "chunk_provenance_gate": False,
            "session_char_budget_gate": False,
        }
    first = chunks[0]
    cited_id = first.message_ids[0]
    forged_id = next(
        message.id for message in bundle.messages if message.id not in first.message_ids
    )

    def response(message_id: int) -> str:
        return json.dumps(
            [
                {
                    "action": "ADD",
                    "content": (
                        "Stable extraction rule: every durable candidate keeps only the exact "
                        "message identifier cited in the currently visible chunk."
                    ),
                    "claim": {
                        "subject": "Scope Recall",
                        "predicate": "uses",
                        "value": "chunk-scoped provenance",
                    },
                    "evidence_message_ids": [message_id],
                    "target": "ops",
                    "memory_type": "workflow",
                    "importance": 0.8,
                    "confidence": 0.9,
                    "reason": "release benchmark provenance",
                }
            ]
        )

    valid, valid_status = _parse_llm_candidates_with_status(
        response(cited_id),
        bundle=bundle,
        allowed_message_ids=set(first.message_ids),
    )
    forged, forged_status = _parse_llm_candidates_with_status(
        response(forged_id),
        bundle=bundle,
        allowed_message_ids=set(first.message_ids),
    )
    provenance_gate = (
        valid_status == "parsed"
        and len(valid) == 1
        and valid[0].message_ids == [cited_id]
        and f"[message_id={cited_id} role=user]" in first.text
        and forged == []
        and forged_status == "filtered"
    )
    budget_gate = (
        sum(chunk.exposed_chars for chunk in chunks) <= 5000
        and all(chunk.exposed_chars <= 1000 for chunk in chunks)
        and chunks[0].input_chars > 5000
        and all(chunk.truncated for chunk in chunks)
    )
    return {
        "chunk_provenance_gate": provenance_gate,
        "session_char_budget_gate": budget_gate,
    }


def _journal_atomic_scope_probe() -> dict[str, bool]:
    conn = _connection()
    ensure_journal_schema(conn)
    scope = RuntimeScope(
        platform="telegram",
        user_id="benchmark-user",
        chat_id="benchmark-chat",
        agent_identity="benchmark-agent",
        agent_workspace="benchmark-workspace",
    )
    config = {
        "fact_evolution": {
            "enabled": True,
            "mode": "auto_apply",
            "journal_mode": "auto_apply",
        }
    }
    local_scope_id = build_scope_id(scope, config)
    shared_scope_id = build_shared_scope_id(scope, config)
    entry_id = append_journal_entry(
        conn,
        scope=scope,
        scope_id=local_scope_id,
        shared_scope_id=shared_scope_id,
        session_id="benchmark-journal-session",
        turn_number=1,
        role="user",
        content="I now live in Bangalore; please keep this current.",
    )
    conn.commit()
    proposal = EvolutionProposal(
        action=EvolutionAction.ADD,
        raw_action="add",
        claim=ClaimDraft.from_parts(
            subject="Benchmark User",
            predicate="lives in",
            value="Bangalore",
            scope_id=local_scope_id,
            cardinality="single",
            valid_from="2026-07-01T00:00:00+00:00",
        ),
        evidence_refs=(
            EvidenceReference(
                source_type="user_message",
                source_id="benchmark-journal-message",
                quote="I now live in Bangalore; please keep this current.",
                speaker_subject="Benchmark User",
            ),
        ),
        confidence=0.99,
        reason="journal atomic scope benchmark",
        source="journal-digest",
    )
    candidate = JournalDigestCandidate(
        content="Benchmark User currently lives in Bangalore.",
        target="user",
        memory_type="factual",
        importance=0.9,
        confidence=0.99,
        reason="journal atomic scope benchmark",
        entry_ids=[entry_id],
        session_ids=["benchmark-journal-session"],
        evolution=proposal,
    )
    try:
        conn.execute("BEGIN IMMEDIATE")
        pending = apply_journal_candidates(
            conn,
            None,
            scope,
            run_id="benchmark-journal-rollback",
            candidates=[candidate],
            runtime_config=config,
        )
        inside_fact = (
            conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
            and conn.execute("SELECT COUNT(*) FROM fact_action_receipts").fetchone()[0]
            == 1
        )
        inside_checkpoint = (
            conn.execute(
                "SELECT processed_run_id FROM journal_entries WHERE id = ?",
                (entry_id,),
            ).fetchone()[0]
            == "benchmark-journal-rollback"
        )
        pending_status = pending["actions"][0]["status"] == "applied_pending_outer_commit"
        conn.rollback()
        rollback_atomic = (
            conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
            and conn.execute("SELECT COUNT(*) FROM fact_action_receipts").fetchone()[0]
            == 0
            and not conn.execute(
                "SELECT processed_run_id FROM journal_entries WHERE id = ?",
                (entry_id,),
            ).fetchone()[0]
        )

        committed = apply_journal_candidates(
            conn,
            None,
            scope,
            run_id="benchmark-journal-commit",
            candidates=[candidate],
            runtime_config=config,
        )
        row = conn.execute("SELECT scope_id FROM memories").fetchone()
        checkpoint = conn.execute(
            "SELECT processed_run_id FROM journal_entries WHERE id = ?",
            (entry_id,),
        ).fetchone()[0]
        atomic_checkpoint = (
            inside_fact
            and inside_checkpoint
            and pending_status
            and rollback_atomic
            and committed["counts"].get("inserted") == 1
            and checkpoint == "benchmark-journal-commit"
            and conn.execute("SELECT COUNT(*) FROM fact_action_receipts").fetchone()[0]
            == 1
        )
        scope_route = (
            local_scope_id != shared_scope_id
            and row is not None
            and str(row["scope_id"]) == shared_scope_id
            and recall_scope_id_for_target(
                "user",
                local_scope_id=local_scope_id,
                shared_scope_id=shared_scope_id,
            )
            == shared_scope_id
            and recall_scope_id_for_target(
                "general",
                local_scope_id=local_scope_id,
                shared_scope_id=shared_scope_id,
            )
            == local_scope_id
        )
        general_entry_id = append_journal_entry(
            conn,
            scope=scope,
            scope_id=local_scope_id,
            shared_scope_id=shared_scope_id,
            session_id="benchmark-general-session",
            turn_number=1,
            role="user",
            content="Keep this bounded scratch note local to this conversation only.",
        )
        conn.commit()
        general_candidate = JournalDigestCandidate(
            content=(
                "This bounded scratch note belongs only to the current conversation "
                "and must not become shared durable memory."
            ),
            target="general",
            memory_type="summary",
            importance=0.6,
            confidence=0.85,
            reason="legacy general scope benchmark",
            entry_ids=[general_entry_id],
            session_ids=["benchmark-general-session"],
            evolution=None,
        )
        general_result = apply_journal_candidates(
            conn,
            None,
            scope,
            run_id="benchmark-general-commit",
            candidates=[general_candidate],
            runtime_config={},
        )
        general_row = conn.execute(
            "SELECT scope_id FROM memories WHERE target = 'general'"
        ).fetchone()
        legacy_general_scope_route = (
            general_result["counts"].get("inserted") == 1
            and general_row is not None
            and str(general_row["scope_id"]) == local_scope_id
        )
        return {
            "journal_atomic_checkpoint": atomic_checkpoint,
            "durable_scope_route": scope_route,
            "legacy_general_scope_route": legacy_general_scope_route,
        }
    finally:
        conn.close()


def run_benchmark(cases_path: Path = DEFAULT_CASES) -> dict[str, Any]:
    payload = json.loads(cases_path.read_text(encoding="utf-8"))
    cases = payload.get("cases")
    thresholds = payload.get("thresholds")
    if not isinstance(cases, list) or len(cases) < 10:
        raise ValueError("benchmark requires at least ten cases")
    if not isinstance(thresholds, dict):
        raise ValueError("benchmark thresholds are required")

    runners = {
        "transition": _transition_case,
        "multi": _multi_case,
        "retract": _retract_case,
        "delayed": _delayed_case,
        "policy-review": _policy_review_case,
        "scope-isolation": _scope_case,
    }
    results: list[dict[str, Any]] = []
    for case in cases:
        if not isinstance(case, dict) or str(case.get("kind") or "") not in runners:
            raise ValueError("unknown benchmark case kind")
        details = runners[case["kind"]](case)
        results.append(
            {
                "id": case["id"],
                "kind": case["kind"],
                **details,
            }
        )

    passed_count = sum(1 for item in results if item["passed"])
    stale = sum(int(item.get("stale_current_leakage") or 0) for item in results)
    ambiguous = sum(int(item.get("ambiguous_auto_apply") or 0) for item in results)
    scope_leakage = sum(int(item.get("scope_leakage") or 0) for item in results)
    replay_ok = _idempotency_probe()
    evidence_authority_gate = _evidence_authority_probe()
    adversarial_evidence = _adversarial_evidence_probe()
    digest_probes = _digest_provenance_budget_probe()
    semantic_probes = _journal_atomic_scope_probe()
    metrics = {
        "case_count": len(results),
        "passed_count": passed_count,
        "case_pass_rate": passed_count / len(results),
        "stale_current_leakage": stale,
        "ambiguous_auto_apply": ambiguous,
        "scope_leakage": scope_leakage,
        "idempotent_replay": replay_ok,
        "evidence_authority_gate": evidence_authority_gate,
        **adversarial_evidence,
        **digest_probes,
        **semantic_probes,
    }
    passed = (
        metrics["case_pass_rate"] >= float(thresholds["case_pass_rate"])
        and stale <= int(thresholds["max_stale_current_leakage"])
        and ambiguous <= int(thresholds["max_ambiguous_auto_apply"])
        and scope_leakage <= int(thresholds["max_scope_leakage"])
        and (not thresholds.get("require_idempotent_replay") or replay_ok)
        and (
            not thresholds.get("require_evidence_authority_gate")
            or evidence_authority_gate
        )
        and (
            not thresholds.get("require_evidence_polarity_gate")
            or adversarial_evidence["evidence_polarity_gate"]
        )
        and (
            not thresholds.get("require_evidence_subject_binding_gate")
            or adversarial_evidence["evidence_subject_binding_gate"]
        )
        and (
            not thresholds.get("require_adversarial_evidence_zero_write")
            or adversarial_evidence["adversarial_evidence_zero_write"]
        )
        and (
            not thresholds.get("require_chunk_provenance_gate")
            or digest_probes["chunk_provenance_gate"]
        )
        and (
            not thresholds.get("require_session_char_budget_gate")
            or digest_probes["session_char_budget_gate"]
        )
        and (
            not thresholds.get("require_journal_atomic_checkpoint")
            or semantic_probes["journal_atomic_checkpoint"]
        )
        and (
            not thresholds.get("require_durable_scope_route")
            or semantic_probes["durable_scope_route"]
        )
        and (
            not thresholds.get("require_legacy_general_scope_route")
            or semantic_probes["legacy_general_scope_route"]
        )
    )
    return {
        "schema_version": payload.get("schema_version"),
        "passed": passed,
        "thresholds": thresholds,
        "metrics": metrics,
        "cases": results,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run deterministic Scope Recall memory evolution benchmark"
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--json", action="store_true", help="Output JSON (default)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run_benchmark(args.cases)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
