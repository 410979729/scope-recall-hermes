"""SQLite runtime doctor checks for schema version, migration ledger, row quality, and truth-store accessibility.

Open live databases read-only here; doctor must never become a hidden migration or repair path."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

try:
    from .doctor_common import contains_secret_like_text, sanitize_report_text
    from .governance_cleanup import governance_audit_coverage_report
    from .graph_hygiene import graph_hygiene_counts, remaining_graph_hygiene_rows
    from .memory_quality import memory_quality_report
    from .sql_store import schema_migration_status
except ImportError:  # pragma: no cover - direct source-script execution fallback
    from doctor_common import contains_secret_like_text, sanitize_report_text
    from governance_cleanup import governance_audit_coverage_report
    from graph_hygiene import graph_hygiene_counts, remaining_graph_hygiene_rows
    from memory_quality import memory_quality_report
    from sql_store import schema_migration_status

def sqlite_report(hermes_home: Path) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Inspect SQLite truth-store health, schema, and migration status in read-only mode.

    The check must distinguish source-code schema readiness from the live database migration state."""
    recommendations: list[str] = []
    storage_dir = hermes_home / "scope-recall"
    db_path = storage_dir / "memory.sqlite3"
    if not db_path.exists():
        recommendations.append(
            "SQLite truth DB is missing; initialize scope-recall or restore memory.sqlite3 before running scripts/repair.vector_index.py."
        )
        sqlite_payload = {"path": str(db_path), "status": "missing", "memory_count": 0, "tables": []}
        return sqlite_payload, {"ok": False, "failures": [f"SQLite truth DB not found: {db_path}"]}, recommendations

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only=ON")
            tables = sorted(row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"))
            memory_count = 0
            if "memories" in tables:
                memory_count = int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
            graph_hygiene = graph_hygiene_counts(conn)
            schema_migrations = schema_migration_status(conn)
            governance_audit_coverage = governance_audit_coverage_report(conn)
        finally:
            conn.close()
    except Exception as exc:
        recommendations.append("Repair or restore the SQLite truth DB before rebuilding the vector companion.")
        sqlite_payload = {"path": str(db_path), "status": "error", "error": str(exc), "memory_count": 0, "tables": []}
        return sqlite_payload, {"ok": False, "failures": [f"SQLite truth DB error: {exc}"]}, recommendations

    orphan_graph_rows = remaining_graph_hygiene_rows(graph_hygiene)
    status = "needs_repair" if orphan_graph_rows else "ready"
    failures: list[str] = []
    if orphan_graph_rows:
        failures.append(
            "SQLite graph hygiene has orphan/hidden lifecycle rows: "
            f"orphan_entities={graph_hygiene['orphan_entities']}, "
            f"orphan_relations={graph_hygiene['orphan_relations']}, "
            f"hidden_lifecycle_entities={graph_hygiene['hidden_lifecycle_entities']}, "
            f"hidden_lifecycle_relations={graph_hygiene['hidden_lifecycle_relations']}"
        )
        recommendations.append(
            "Graph hygiene orphan or hidden-lifecycle rows found; run scripts/repair.graph_hygiene.py --apply after reviewing the dry-run counts."
        )
    if not bool(schema_migrations.get("current")):
        recommendations.append(
            "SQLite schema migration ledger is not current; run the current scope-recall provider or installer doctor to apply baseline schema metadata before release rollout."
        )
    raw_new_coverage = governance_audit_coverage.get("new_mutation_coverage")
    raw_legacy_coverage = governance_audit_coverage.get("legacy_coverage")
    new_coverage: dict[str, Any] = raw_new_coverage if isinstance(raw_new_coverage, dict) else {}
    legacy_coverage: dict[str, Any] = raw_legacy_coverage if isinstance(raw_legacy_coverage, dict) else {}
    new_missing_audit = int(new_coverage.get("missing_audit") or 0)
    legacy_missing_audit = int(legacy_coverage.get("missing_audit") or 0)
    if new_missing_audit:
        failures.append(f"governance audit coverage missing for {new_missing_audit} new archived memory mutation(s)")
        recommendations.append("Governance audit coverage for new archive mutations is incomplete; inspect archived_by/rollback_batch_id rows before release.")
    if legacy_missing_audit:
        recommendations.append(
            f"Legacy archived memories without governance audit coverage: {legacy_missing_audit}; run scripts/governance.audit_coverage.py --dry-run and optionally --apply to backfill lineage evidence."
        )
    sqlite_payload = {
        "path": str(db_path),
        "status": status,
        "memory_count": memory_count,
        "tables": tables,
        "graph_hygiene": graph_hygiene,
        "schema_migrations": schema_migrations,
        "governance_audit_coverage": governance_audit_coverage,
    }
    return sqlite_payload, {"ok": not failures, "failures": failures}, recommendations


def memory_candidate_debt_report(hermes_home: Path) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Report candidate-memory debt from SQLite without changing lifecycle state.

    Operators use this to decide promotion/archive work before enabling promoted-only behavior in profiles."""
    recommendations: list[str] = []
    db_path = hermes_home / "scope-recall" / "memory.sqlite3"
    if not db_path.exists():
        return {"status": "missing", "path": str(db_path), "candidate_count": 0}, {"ok": True, "failures": []}, recommendations
    source_root = Path(__file__).resolve().parents[1]
    source_parent = source_root.parent
    for candidate_path in (str(source_parent), str(source_root)):
        if candidate_path not in sys.path:
            sys.path.insert(0, candidate_path)
    try:
        from .candidate_promotion import candidate_debt_report
    except ImportError:  # pragma: no cover - direct source-script execution fallback
        from candidate_promotion import candidate_debt_report
    except Exception as exc:  # pragma: no cover - defensive standalone reporting
        return {"status": "error", "path": str(db_path), "candidate_count": 0, "error": str(exc)}, {"ok": False, "failures": [f"candidate debt classifier import failed: {exc}"]}, [
            "Repair the source checkout or installed package before relying on candidate-memory debt reporting."
        ]
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "memories" not in tables:
                return {"status": "schema_missing", "path": str(db_path), "candidate_count": 0}, {"ok": True, "failures": []}, recommendations
            payload = candidate_debt_report(conn, limit=1000, sample_limit=8)
        finally:
            conn.close()
    except Exception as exc:
        return {"status": "error", "path": str(db_path), "candidate_count": 0, "error": str(exc)}, {"ok": False, "failures": [f"candidate debt report failed: {exc}"]}, [
            "Repair or restore the SQLite truth DB before running candidate-memory promotion."
        ]

    payload["path"] = str(db_path)
    candidate_count = int(payload.get("candidate_count") or 0)
    raw_by_action = payload.get("by_action")
    by_action: dict[str, Any] = raw_by_action if isinstance(raw_by_action, dict) else {}
    promotable = int(by_action.get("promote", 0) or 0)
    archival = int(by_action.get("archive", 0) or 0)
    oldest_age_hours = float(payload.get("oldest_age_hours") or 0.0)
    if candidate_count:
        recommendations.append(
            "Candidate memory debt exists; run scripts/promote.memory_candidates.py --dry-run, then --apply after reviewing the plan."
        )
    if promotable or archival:
        recommendations.append(
            f"Candidate promotion plan has promotable={promotable}, archive_candidates={archival}; apply promotions before switching profile behavior across releases."
        )
    if candidate_count >= 25 or oldest_age_hours >= 168:
        recommendations.append(
            f"Candidate memory backlog is aging/counting up (count={candidate_count}, oldest_age_hours={oldest_age_hours}); keep promotion/review drains scheduled."
        )
    failures: list[str] = []
    # Candidate debt is a yellow operational signal, not a hard failure unless it
    # grows far beyond normal review capacity. This keeps doctor usable on live
    # systems while still surfacing the bottleneck that would starve promoted-only profile.
    if candidate_count >= 500 or oldest_age_hours >= 720:
        failures.append(f"candidate memory debt exceeds fail threshold: count={candidate_count}, oldest_age_hours={oldest_age_hours}")
    return payload, {"ok": not failures, "failures": failures}, recommendations


def memory_quality_lint_report(hermes_home: Path, *, sample_limit: int = 8) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    recommendations: list[str] = []
    db_path = hermes_home / "scope-recall" / "memory.sqlite3"
    if not db_path.exists():
        return {"status": "missing", "path": str(db_path), "active_lint_hits": 0, "samples": []}, {"ok": True, "failures": []}, recommendations
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only=ON")
            payload = memory_quality_report(conn, sample_limit=sample_limit)
        finally:
            conn.close()
    except Exception as exc:
        recommendations.append("Repair or restore the SQLite truth DB before trusting active memory quality lint status.")
        return {"status": "error", "path": str(db_path), "error": str(exc), "active_lint_hits": 0, "samples": []}, {"ok": False, "failures": [f"memory quality lint error: {exc}"]}, recommendations
    payload["path"] = str(db_path)
    active_lint_hits = int(payload.get("active_lint_hits") or 0)
    status = str(payload.get("status") or "ready")
    if active_lint_hits:
        recommendations.append(
            f"Active memory quality lint found {active_lint_hits} rule hits; review runtime.memory_quality_lint samples before promoting or exporting memory."
        )
    failures: list[str] = []
    if status == "needs_repair":
        failures.append(f"active memory quality lint exceeds repair threshold: hits={active_lint_hits}")
    return payload, {"ok": not failures, "failures": failures}, recommendations


def memory_secret_report(hermes_home: Path, *, sample_limit: int = 10) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    recommendations: list[str] = []
    db_path = hermes_home / "scope-recall" / "memory.sqlite3"
    if not db_path.exists():
        return {"status": "missing", "path": str(db_path), "active_secret_like_count": 0, "samples": []}, {"ok": True, "failures": []}, recommendations
    samples: list[dict[str, Any]] = []
    active_secret_like_count = 0
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "memories" not in tables:
                return {"status": "schema_missing", "path": str(db_path), "active_secret_like_count": 0, "samples": []}, {"ok": True, "failures": []}, recommendations
            for row in conn.execute("SELECT id, scope_id, source, target, content, summary, updated_at, metadata FROM memories"):
                try:
                    metadata = json.loads(str(row["metadata"] or "{}"))
                except Exception:
                    metadata = {}
                if str(metadata.get("lifecycle") or "").strip().lower() == "archived":
                    continue
                content = str(row["content"] or "")
                if not contains_secret_like_text(content):
                    continue
                active_secret_like_count += 1
                if len(samples) < max(0, int(sample_limit)):
                    samples.append(
                        {
                            "id": str(row["id"]),
                            "scope_id": str(row["scope_id"] or ""),
                            "source": str(row["source"] or ""),
                            "target": str(row["target"] or ""),
                            "updated_at": str(row["updated_at"] or ""),
                            "preview": sanitize_report_text(content)[:220],
                        }
                    )
        finally:
            conn.close()
    except Exception as exc:
        recommendations.append("Repair or restore the SQLite truth DB before trusting memory secret-scan status.")
        return {"status": "error", "path": str(db_path), "error": str(exc), "active_secret_like_count": 0, "samples": []}, {"ok": False, "failures": [f"memory secret scan error: {exc}"]}, recommendations

    payload = {"status": "ready", "path": str(db_path), "active_secret_like_count": active_secret_like_count, "samples": samples}
    if active_secret_like_count:
        recommendations.append("Active memory rows contain plaintext secret-like content; archive or hard-delete them and store only secret indexes/vault refs.")
    return payload, {"ok": active_secret_like_count == 0, "failures": [f"active plaintext secret-like memory rows: {active_secret_like_count}"] if active_secret_like_count else []}, recommendations
