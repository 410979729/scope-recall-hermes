#!/usr/bin/env python3
"""Run one neutral stage of the real N-1/N/N-1 release rehearsal.

The release orchestrator copies this file to an isolated harness and invokes it
with the interpreter whose installed distribution is under test.  Stage output
is content-free and intentionally excludes local filesystem paths.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Mapping, Sequence


SCHEMA_VERSION = "scope-recall.n-minus-one-stage.v1"
N_MINUS_ONE_VERSION = "1.10.3"
CANDIDATE_VERSION = "2.0.0"
STAGES = (
    "n_minus_one_create",
    "candidate_upgrade_write",
    "n_minus_one_read_after_n",
    "candidate_final_verify",
)
SCOPE_ID = "release-window-scope"
N_MINUS_ONE_MEMORY_ID = "n-minus-one-ordinary"
N_MINUS_ONE_PROJECTION_ID = "n-minus-one-legacy-projection"
CANDIDATE_PROJECTION_ID = "candidate-canonical-projection"
CANDIDATE_CLAIM_ID = "candidate-canonical-claim"


class RehearsalStageError(RuntimeError):
    """Raised when a stage cannot prove the intended cross-version contract."""


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _distribution_probe(*, expected_version: str, source_root: Path) -> dict[str, object]:
    import scope_recall

    distribution = importlib.metadata.distribution("hermes-scope-recall")
    actual_version = str(distribution.version)
    if actual_version != expected_version:
        raise RehearsalStageError(
            f"installed distribution mismatch: expected {expected_version}, got {actual_version}"
        )
    module_file = getattr(scope_recall, "__file__", None)
    if not module_file:
        raise RehearsalStageError("installed scope_recall module has no filesystem origin")
    module_path = Path(module_file).resolve(strict=True)
    resolved_source = source_root.resolve(strict=True)
    source_on_path = any(
        Path(item or os.curdir).resolve(strict=False) == resolved_source for item in sys.path
    )
    source_imported = module_path.is_relative_to(resolved_source)
    if source_on_path or source_imported:
        raise RehearsalStageError("candidate source worktree shadowed installed distribution")
    return {
        "installed_distribution": f"hermes-scope-recall=={actual_version}",
        "python_version": sys.version.split()[0],
        "python_executable_sha256": _sha256_file(Path(sys.executable).resolve(strict=True)),
        "source_worktree_on_sys_path": source_on_path,
        "source_worktree_imported": source_imported,
        "module_origin_class": "isolated-site-packages",
    }


def _schema_fingerprint(conn: sqlite3.Connection) -> str:
    tables = [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]
    schema = [
        {
            "table": table,
            "columns": [
                {
                    "name": str(row[1]),
                    "type": str(row[2]),
                    "notnull": int(row[3]),
                    "pk": int(row[5]),
                }
                for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            ],
        }
        for table in tables
    ]
    return _canonical_sha256(schema)


def _row_identity_fingerprint(conn: sqlite3.Connection) -> str:
    identities: dict[str, object] = {
        "memories": [
            [str(row[0]), str(row[1]), str(row[2]), str(row[3])]
            for row in conn.execute(
                "SELECT id, scope_id, source, target FROM memories ORDER BY id"
            ).fetchall()
        ]
    }
    for table, query in (
        (
            "fact_claims",
            "SELECT claim_id, memory_id, scope_id, fact_key, status FROM fact_claims ORDER BY claim_id",
        ),
        (
            "fact_claim_evidence",
            "SELECT evidence_id, claim_id, source_type, source_ref FROM fact_claim_evidence ORDER BY evidence_id",
        ),
    ):
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone():
            identities[table] = [list(map(str, row)) for row in conn.execute(query).fetchall()]
    return _canonical_sha256(identities)


def _checkpoint_for_handoff(conn: sqlite3.Connection) -> None:
    """Materialize committed WAL frames before another interpreter gets a copy."""

    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()


def _write_n_minus_one_truth(database: Path, hermes_home: Path) -> dict[str, object]:
    from scope_recall.sql_store import ensure_schema, store_row
    from scope_recall.truth_connection import connect_truth_database

    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True, exist_ok=True)
    config = {
        "memory_isolated_chat_ids": ["release-window-isolated-chat"],
        "vector": {"enabled": False},
    }
    config_path = storage / "config.json"
    config_path.write_text(
        json.dumps(config, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    conn = connect_truth_database(database, mode="rwc")
    try:
        ensure_schema(conn)
        common = {
            "scope_id": SCOPE_ID,
            "platform": "release-rehearsal",
            "user_id": "release-window-user",
            "chat_id": "release-window-chat",
            "thread_id": "",
            "gateway_session_key": "release-window-session",
            "agent_identity": "release-window-agent",
            "agent_workspace": "isolated-release-window",
            "session_id": "n-minus-one-window",
            "source": "release-rehearsal",
            "target": "memory",
            "allow_duplicate": True,
            "enqueue_vector_intent": False,
        }
        store_row(
            conn,
            memory_id=N_MINUS_ONE_MEMORY_ID,
            content="Ordinary N-1 compatibility memory.",
            metadata=json.dumps({"memory_type": "factual", "lifecycle": "promoted"}),
            **common,
        )
        store_row(
            conn,
            memory_id=N_MINUS_ONE_PROJECTION_ID,
            content="The release-window user prefers stable memory projections.",
            metadata=json.dumps(
                {
                    "memory_type": "factual",
                    "lifecycle": "promoted",
                    "legacy_projection": True,
                }
            ),
            **common,
        )
        _checkpoint_for_handoff(conn)
        rows = conn.execute(
            "SELECT id FROM memories WHERE id IN (?, ?) ORDER BY id",
            (N_MINUS_ONE_MEMORY_ID, N_MINUS_ONE_PROJECTION_ID),
        ).fetchall()
        if {str(row[0]) for row in rows} != {
            N_MINUS_ONE_MEMORY_ID,
            N_MINUS_ONE_PROJECTION_ID,
        }:
            raise RehearsalStageError("N-1 public writer did not persist both fixtures")
        return {
            "user_version": int(conn.execute("PRAGMA user_version").fetchone()[0]),
            "memory_count": len(rows),
            "schema_fingerprint": _schema_fingerprint(conn),
            "row_identity_fingerprint": _row_identity_fingerprint(conn),
            "config_sha256": _sha256_file(config_path),
            "config_isolation_key_present": True,
            "vector_enabled": False,
        }
    finally:
        conn.close()


def _candidate_upgrade_write(database: Path, hermes_home: Path) -> dict[str, object]:
    from scope_recall.evolution_policy import evaluate_evolution_policy
    from scope_recall.fact_actions import (
        ClaimDraft,
        EvidenceReference,
        EvolutionAction,
        EvolutionPlan,
        EvolutionProposal,
    )
    from scope_recall.fact_executor import FactExecutionContext, execute_fact_plan
    from scope_recall.fact_repository import assert_canonical_projection_pair
    from scope_recall.installer import _upgrade_compatibility_preflight, source_root
    from scope_recall.sql_store import ensure_schema
    from scope_recall.truth_connection import connect_truth_database

    preflight = _upgrade_compatibility_preflight(hermes_home, source_root())
    if preflight.get("ok") is not True or preflight.get("read_only") is not True:
        failures = preflight.get("failures")
        detail = "; ".join(str(item) for item in failures) if isinstance(failures, list) else ""
        raise RehearsalStageError(
            "candidate upgrade compatibility preflight failed"
            + (f": {detail[:500]}" if detail else "")
        )
    conn = connect_truth_database(database, mode="rwc")
    try:
        ensure_schema(conn)
        preserved = {
            str(row[0])
            for row in conn.execute(
                "SELECT id FROM memories WHERE id IN (?, ?)",
                (N_MINUS_ONE_MEMORY_ID, N_MINUS_ONE_PROJECTION_ID),
            ).fetchall()
        }
        if preserved != {N_MINUS_ONE_MEMORY_ID, N_MINUS_ONE_PROJECTION_ID}:
            raise RehearsalStageError("candidate migration lost N-1 truth")
        claim = ClaimDraft.from_parts(
            subject="Release Window User",
            predicate="lives in",
            value="Bangalore",
            scope_id=SCOPE_ID,
        )
        proposal = EvolutionProposal(
            action=EvolutionAction.ADD,
            raw_action="add",
            claim=claim,
            target_ids=(),
            evidence_refs=(
                EvidenceReference(
                    "user_message",
                    "release-window-evidence",
                    "I live in Bangalore; please record this current fact.",
                    speaker_subject="Release Window User",
                ),
            ),
            confidence=0.99,
            reason="cross-version release rehearsal",
            source="release_rehearsal",
        )
        plan = EvolutionPlan(
            proposal=proposal,
            action_id="release-window-add",
            idempotency_key="release-window-add-v1",
            policy_mode="reviewed_apply",
            expected_versions={},
        )
        policy = evaluate_evolution_policy(proposal, allowed_target_ids=set())
        context = FactExecutionContext(
            scope_id=SCOPE_ID,
            writable_scope_ids=(SCOPE_ID,),
            actor="scope-recall:release-rehearsal",
            timestamp="2026-08-28T00:00:00+00:00",
            source="release_rehearsal",
            target="memory",
            session_id="release-window-candidate",
            platform="release-rehearsal",
            user_id="release-window-user",
            new_memory_id=CANDIDATE_PROJECTION_ID,
            new_claim_id=CANDIDATE_CLAIM_ID,
            metadata={"memory_type": "factual"},
        )
        result = execute_fact_plan(conn, plan, policy, context)
        if result.applied is not True or result.status != "applied":
            raise RehearsalStageError("candidate Fact execution did not apply")
        pair = assert_canonical_projection_pair(
            conn,
            memory_id=CANDIDATE_PROJECTION_ID,
            claim_id=CANDIDATE_CLAIM_ID,
            scope_id=SCOPE_ID,
            fact_key=claim.fact_key,
            memory_type="factual",
        )
        _checkpoint_for_handoff(conn)
        return {
            "user_version": int(conn.execute("PRAGMA user_version").fetchone()[0]),
            "n_minus_one_rows_preserved": len(preserved),
            "claim_count": int(
                conn.execute(
                    "SELECT COUNT(*) FROM fact_claims WHERE claim_id=?",
                    (CANDIDATE_CLAIM_ID,),
                ).fetchone()[0]
            ),
            "evidence_count": int(
                conn.execute(
                    "SELECT COUNT(*) FROM fact_claim_evidence WHERE claim_id=?",
                    (CANDIDATE_CLAIM_ID,),
                ).fetchone()[0]
            ),
            "canonical_projection_pair": pair,
            "upgrade_preflight_read_only": True,
            "schema_fingerprint": _schema_fingerprint(conn),
            "row_identity_fingerprint": _row_identity_fingerprint(conn),
        }
    finally:
        conn.close()


def _n_minus_one_read_after_n(database: Path) -> dict[str, object]:
    from scope_recall.truth_connection import connect_truth_database

    conn = connect_truth_database(database, mode="ro")
    try:
        query_only = int(conn.execute("PRAGMA query_only").fetchone()[0])
        if query_only != 1:
            raise RehearsalStageError("N-1 follower did not open query-only")
        projection = conn.execute(
            "SELECT id, scope_id, source, target FROM memories WHERE id=?",
            (CANDIDATE_PROJECTION_ID,),
        ).fetchone()
        if projection is None or str(projection[1]) != SCOPE_ID:
            raise RehearsalStageError("N-1 could not read candidate legacy Projection")
        additive_counts = {
            table: int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in ("fact_claims", "fact_claim_evidence")
        }
        return {
            "query_only": query_only,
            "user_version": int(conn.execute("PRAGMA user_version").fetchone()[0]),
            "candidate_projection_readable": True,
            "candidate_projection_identity_sha256": _canonical_sha256(
                [str(projection[index]) for index in range(4)]
            ),
            "additive_tables_observed_without_import": additive_counts,
            "row_identity_fingerprint": _row_identity_fingerprint(conn),
        }
    finally:
        conn.close()


def _candidate_final_verify(database: Path) -> dict[str, object]:
    from scope_recall.fact_repository import assert_canonical_projection_pair
    from scope_recall.truth_connection import connect_truth_database

    conn = connect_truth_database(database, mode="ro")
    try:
        claim = conn.execute(
            "SELECT fact_key FROM fact_claims WHERE claim_id=?",
            (CANDIDATE_CLAIM_ID,),
        ).fetchone()
        if claim is None:
            raise RehearsalStageError("candidate Claim is missing after N-1 read")
        pair = assert_canonical_projection_pair(
            conn,
            memory_id=CANDIDATE_PROJECTION_ID,
            claim_id=CANDIDATE_CLAIM_ID,
            scope_id=SCOPE_ID,
            fact_key=str(claim[0]),
            memory_type="factual",
        )
        evidence_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM fact_claim_evidence WHERE claim_id=?",
                (CANDIDATE_CLAIM_ID,),
            ).fetchone()[0]
        )
        claim_only_count = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM fact_claims AS claim
                LEFT JOIN memories AS projection ON projection.id = claim.memory_id
                WHERE projection.id IS NULL
                """
            ).fetchone()[0]
        )
        legacy_projection_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM memories WHERE id IN (?, ?)",
                (N_MINUS_ONE_PROJECTION_ID, CANDIDATE_PROJECTION_ID),
            ).fetchone()[0]
        )
        if evidence_count != 1 or claim_only_count != 0 or legacy_projection_count != 2:
            raise RehearsalStageError("candidate final cross-surface verification failed")
        return {
            "query_only": int(conn.execute("PRAGMA query_only").fetchone()[0]),
            "user_version": int(conn.execute("PRAGMA user_version").fetchone()[0]),
            "canonical_projection_pair": pair,
            "evidence_count": evidence_count,
            "claim_only_count": claim_only_count,
            "legacy_projection_count": legacy_projection_count,
            "schema_fingerprint": _schema_fingerprint(conn),
            "row_identity_fingerprint": _row_identity_fingerprint(conn),
        }
    finally:
        conn.close()


def run_stage(
    *,
    stage: str,
    database: Path,
    hermes_home: Path,
    source_root: Path,
    expected_version: str,
) -> dict[str, object]:
    if stage not in STAGES:
        raise RehearsalStageError(f"unsupported rehearsal stage: {stage}")
    expected_for_stage = (
        N_MINUS_ONE_VERSION
        if stage in {"n_minus_one_create", "n_minus_one_read_after_n"}
        else CANDIDATE_VERSION
    )
    if expected_version != expected_for_stage:
        raise RehearsalStageError("stage expected-version label is inconsistent")
    probe = _distribution_probe(
        expected_version=expected_version,
        source_root=source_root,
    )
    if stage == "n_minus_one_create":
        details = _write_n_minus_one_truth(database, hermes_home)
    elif stage == "candidate_upgrade_write":
        details = _candidate_upgrade_write(database, hermes_home)
    elif stage == "n_minus_one_read_after_n":
        details = _n_minus_one_read_after_n(database)
    else:
        details = _candidate_final_verify(database)
    return {
        "schema_version": SCHEMA_VERSION,
        "stage": stage,
        "result": "passed",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        **probe,
        "details": details,
    }


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, choices=STAGES)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--hermes-home", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    payload = run_stage(
        stage=args.stage,
        database=args.database.resolve(strict=False),
        hermes_home=args.hermes_home.resolve(strict=False),
        source_root=args.source_root.resolve(strict=True),
        expected_version=str(args.expected_version),
    )
    _write_json(args.output, payload)
    print(json.dumps({"result": "passed", "stage": args.stage}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
