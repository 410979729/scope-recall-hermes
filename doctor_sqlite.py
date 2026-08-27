"""SQLite runtime doctor checks for schema version, migration ledger, row quality, and truth-store accessibility.

Open live databases read-only here; doctor must never become a hidden migration or repair path."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:
    from .adjudication_schedule import adjudication_schedule_status
    from .doctor_common import contains_secret_like_text, sanitize_report_text
    from .freshness import fact_freshness_report
    from .governance_cleanup import governance_audit_coverage_report
    from .graph_hygiene import graph_hygiene_counts, remaining_graph_hygiene_rows
    from .lexical_generation import lexical_generation_report
    from .lifecycle_registry import lifecycle_registry_report
    from .maintenance_lease import activation_lease_status
    from .memory_quality import memory_quality_report
    from .operator_ledger import operator_ledger_report
    from .relation_containment import relation_containment_report
    from .relation_frequency_maintenance import relation_frequency_index_report
    from .relation_policy_generation import relation_policy_generation_report
    from .relation_rebuild_queue import relation_rebuild_queue_report
    from .secret_patterns import scan_secret_like_text
    from .sql_store import fts_integrity_report, schema_migration_status
    from .truth_connection import connect_truth_database, truth_storage_permissions
except ImportError:  # pragma: no cover - direct source-script execution fallback
    from adjudication_schedule import adjudication_schedule_status
    from doctor_common import contains_secret_like_text, sanitize_report_text
    from freshness import fact_freshness_report
    from governance_cleanup import governance_audit_coverage_report
    from graph_hygiene import graph_hygiene_counts, remaining_graph_hygiene_rows
    from lexical_generation import lexical_generation_report
    from lifecycle_registry import lifecycle_registry_report
    from maintenance_lease import activation_lease_status
    from memory_quality import memory_quality_report
    from operator_ledger import operator_ledger_report
    from relation_containment import relation_containment_report
    from relation_frequency_maintenance import relation_frequency_index_report
    from relation_policy_generation import relation_policy_generation_report
    from relation_rebuild_queue import relation_rebuild_queue_report
    from secret_patterns import scan_secret_like_text
    from sql_store import fts_integrity_report, schema_migration_status
    from truth_connection import connect_truth_database, truth_storage_permissions

def sqlite_report(hermes_home: Path) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Inspect SQLite truth-store health, schema, and migration status in read-only mode.

    The check must distinguish source-code schema readiness from the live database migration state."""
    recommendations: list[str] = []
    registry_report = lifecycle_registry_report()
    storage_dir = hermes_home / "scope-recall"
    db_path = storage_dir / "memory.sqlite3"
    if not db_path.exists():
        recommendations.append(
            "SQLite truth DB is missing; initialize scope-recall or restore memory.sqlite3 before running scripts/repair.vector_index.py."
        )
        sqlite_payload = {
            "path": str(db_path),
            "status": "missing",
            "memory_count": 0,
            "tables": [],
            "lifecycle_registry": registry_report,
        }
        return sqlite_payload, {"ok": False, "failures": [f"SQLite truth DB not found: {db_path}"]}, recommendations

    activation_lease = activation_lease_status(db_path)
    storage_permissions = truth_storage_permissions(db_path)
    try:
        conn = connect_truth_database(db_path, mode="ro")
        try:
            tables = sorted(row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"))
            memory_count = 0
            if "memories" in tables:
                memory_count = int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
            fts_integrity: dict[str, Any] = {
                "status": "schema_missing",
                "healthy": False,
            }
            if "memories" in tables and "memories_fts" in tables:
                memory_columns = {
                    str(row["name"])
                    for row in conn.execute("PRAGMA table_info(memories)")
                }
                if {"metadata", "target"}.issubset(memory_columns):
                    fts_integrity = dict(fts_integrity_report(conn))
                    fts_integrity["status"] = (
                        "ready" if bool(fts_integrity.get("healthy")) else "needs_repair"
                    )
            graph_hygiene = graph_hygiene_counts(conn)
            operator_ledger = operator_ledger_report(conn)
            relation_containment = relation_containment_report(conn)
            relation_frequency = relation_frequency_index_report(conn)
            relation_generation = relation_policy_generation_report(conn)
            relation_rebuild = relation_rebuild_queue_report(conn)
            lexical_generation = lexical_generation_report(conn)
            schema_migrations = schema_migration_status(conn)
            governance_audit_coverage = governance_audit_coverage_report(conn)
            freshness_report = fact_freshness_report(conn)
        finally:
            conn.close()
    except Exception as exc:
        recommendations.append("Repair or restore the SQLite truth DB before rebuilding the vector companion.")
        sqlite_payload = {
            "path": str(db_path),
            "status": "error",
            "error": str(exc),
            "memory_count": 0,
            "tables": [],
            "lifecycle_registry": registry_report,
        }
        return sqlite_payload, {"ok": False, "failures": [f"SQLite truth DB error: {exc}"]}, recommendations

    orphan_graph_rows = remaining_graph_hygiene_rows(graph_hygiene)
    failures: list[str] = []
    if str(registry_report.get("status") or "") != "ready":
        failures.append("lifecycle registry is invalid")
        recommendations.append(
            "Repair the lifecycle registry contract before allowing lifecycle writers."
        )
    lease_status = str(activation_lease.get("status") or "absent")
    if lease_status == "stale":
        failures.append("stale activation maintenance lease blocks SQLite writers")
        recommendations.append(
            "Inspect stale lease recovery with `python "
            "scripts/recover.activation_lease.py --dry-run --hermes-home <profile>`; "
            "apply only after verifying the recorded owner process is dead."
        )
    elif lease_status == "active":
        recommendations.append(
            "Activation maintenance is currently active; wait for the owner to finish "
            "before resuming ordinary writers."
        )
    if not bool(storage_permissions.get("ok")):
        failures.append(
            "SQLite truth-store permissions are unsafe: "
            f"directory={storage_permissions.get('directory_mode') or 'unknown'}, "
            f"database={storage_permissions.get('database_mode') or 'unknown'}"
        )
        recommendations.append(
            "Harden the scope-recall storage directory to 0700 and memory.sqlite3 "
            "to 0600 before resuming normal writers."
        )
    raw_freshness_coverage = freshness_report.get("coverage")
    freshness_coverage = (
        raw_freshness_coverage
        if isinstance(raw_freshness_coverage, dict)
        else {}
    )
    factual_memories = int(freshness_coverage.get("factual_memories") or 0)
    tracked_memory_facts = int(
        freshness_coverage.get("tracked_memory_facts") or 0
    )
    if tracked_memory_facts < factual_memories:
        failures.append(
            "SQLite factual freshness coverage is incomplete: "
            f"tracked={tracked_memory_facts}, factual={factual_memories}"
        )
        recommendations.append(
            "Run `python scripts/backfill.freshness.py --hermes-home <profile> "
            "--dry-run`; apply bounded batches only after backup/maintenance confirmation, "
            "then rerun doctor before declaring the store ready."
        )
    if (
        str(fts_integrity.get("status") or "") == "needs_repair"
        and not bool(fts_integrity.get("healthy"))
    ):
        failures.append(
            "SQLite FTS lifecycle membership drift: "
            f"expected={int(fts_integrity.get('expected_fts_rows') or 0)}, "
            f"actual={int(fts_integrity.get('fts_rows') or 0)}, "
            f"missing={int(fts_integrity.get('missing_fts_rows') or 0)}, "
            f"stale={int(fts_integrity.get('stale_fts_rows') or 0)}, "
            f"hidden={int(fts_integrity.get('hidden_fts_rows') or 0)}, "
            f"duplicates={int(fts_integrity.get('duplicate_fts_extra_rows') or 0)}"
        )
        recommendations.append(
            "FTS lifecycle membership drift exists; run "
            "`python scripts/repair.fts_index.py --hermes-home <profile> --dry-run`; "
            "only add `--apply --maintenance-confirmed` after taking normal writers "
            "offline (the apply path creates and verifies an online backup first)."
        )
    lexical_current = str(lexical_generation.get("current_generation_id") or "")
    if lexical_current and not bool(lexical_generation.get("healthy")):
        raw_integrity = lexical_generation.get("integrity")
        integrity = raw_integrity if isinstance(raw_integrity, dict) else {}
        failures.append(
            "Active lexical shadow integrity failed: "
            f"missing={int(integrity.get('missing_rows') or 0)}, "
            f"stale={int(integrity.get('stale_rows') or 0)}, "
            f"hidden={int(integrity.get('hidden_rows') or 0)}, "
            f"duplicates={int(integrity.get('duplicate_rows') or 0)}, "
            f"content_drift={int(integrity.get('content_drift_rows') or 0)}"
        )
        recommendations.append(
            "Run lexical rollback under a confirmed maintenance window with "
            f"`hermes-scope-recall lexical rollback --expected-current {lexical_current} "
            "--apply --maintenance-confirmed`, then inspect/rebuild the retained shadow."
        )
    elif str(lexical_generation.get("status") or "") == "ready":
        recommendations.append(
            "A reviewed lexical shadow is READY but inactive; activate it only after "
            "reviewing the migration quality receipt and current-generation CAS value."
        )
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
    relation_frequency_dirty = int(relation_frequency.get("dirty_memories") or 0)
    relation_frequency_backfill = int(
        relation_frequency.get("backfill_pending_scopes") or 0
    )
    relation_frequency_reclassification = int(
        relation_frequency.get("reclassification_pending_scopes") or 0
    )
    relation_frequency_focus_pending = int(
        relation_frequency.get("focus_pending") or 0
    )
    relation_frequency_retry = int(relation_frequency.get("retry_failures") or 0)
    relation_frequency_focus_retry = int(
        relation_frequency.get("focus_retry_failures") or 0
    )
    relation_frequency_dead_letter = int(
        relation_frequency.get("dead_letter_failures") or 0
    )
    relation_frequency_focus_dead_letter = int(
        relation_frequency.get("focus_dead_letter_failures") or 0
    )
    if str(relation_frequency.get("status") or "") == "schema_missing":
        recommendations.append(
            "Relation frequency index schema is missing; initialize with the current provider before relying on bounded relation sync."
        )
    elif (
        relation_frequency_dirty
        or relation_frequency_backfill
        or relation_frequency_focus_pending
        or relation_frequency_retry
        or relation_frequency_focus_retry
    ):
        recommendations.append(
            "Bounded relation maintenance debt exists; keep the background writer running until dirty memories, backfill scopes, focus work, and retry work reach zero."
        )
    if relation_frequency_reclassification:
        recommendations.append(
            "Retired relation reclassification debt cannot auto-drain; review an exact scope dry-run with scripts/repair.relation_queue.py before backup-first cleanup."
        )
    if relation_frequency_dead_letter or relation_frequency_focus_dead_letter:
        failures.append(
            "relation maintenance has poisoned work requiring operator action: "
            f"frequency={relation_frequency_dead_letter}, "
            f"focus={relation_frequency_focus_dead_letter}"
        )
        recommendations.append(
            "Inspect poisoned relation frequency/focus work and its content-free receipt; repair or explicitly dispose only the exact failed item after preserving a backup."
        )
    if relation_frequency_dirty >= 5000:
        failures.append(
            "relation frequency dirty-memory debt exceeds fail threshold: "
            f"dirty_memories={relation_frequency_dirty}"
        )

    relation_unresolved = int(relation_rebuild.get("unresolved") or 0)
    relation_dead_letter = int(relation_rebuild.get("dead_letter") or 0)
    if str(relation_rebuild.get("status") or "") == "schema_missing":
        recommendations.append(
            "Relation rebuild queue schema is missing; initialize with the current provider before relying on bounded relation sync."
        )
    elif relation_unresolved:
        recommendations.append(
            "Retired relation rebuild debt cannot auto-drain; review exact scope/status selectors with scripts/repair.relation_queue.py --dry-run, then use backup-first --apply only during confirmed maintenance."
        )
    if relation_unresolved:
        failures.append(
            "retired relation rebuild work requires exact operator cleanup: "
            f"unresolved={relation_unresolved}, dead_letter={relation_dead_letter}"
        )

    containment_status = str(relation_containment.get("status") or "")
    containment_scopes = list(relation_containment.get("scopes") or [])
    containment_operator_scopes = sum(
        bool(item.get("operator_action_required"))
        for item in containment_scopes
        if isinstance(item, dict)
    )
    containment_degraded_scopes = sum(
        str(item.get("state") or "") == "degraded"
        for item in containment_scopes
        if isinstance(item, dict)
    )
    if containment_status == "schema_missing":
        recommendations.append(
            "Relation containment schema is missing; initialize with the current provider before trusting generated relation signals."
        )
    elif containment_operator_scopes:
        failures.append(
            "relation containment requires operator action: "
            f"scopes={containment_operator_scopes}"
        )
    elif containment_degraded_scopes:
        recommendations.append(
            "Relation containment is degraded but auto-recoverable; keep bounded background maintenance running and verify progress before release."
        )

    if bool(relation_generation.get("operator_action_required")):
        failures.append(
            "relation policy generation requires operator action: "
            f"reason={relation_generation.get('reason_code') or 'unknown'}"
        )
    elif str(relation_generation.get("state") or "") == "degraded":
        recommendations.append(
            "Relation policy generation has auto-recoverable durable debt; keep bounded maintenance running and verify oldest age and progress."
        )

    if operator_ledger["status"] == "schema_missing":
        recommendations.append(
            "Operator ledger schema is missing; initialize with the current provider before relying on filesystem receipts."
        )
    else:
        pending_receipts = int(operator_ledger.get("pending", 0) or 0)
        failed_receipts = int(operator_ledger.get("failed", 0) or 0)
        oldest_receipt_age = float(
            operator_ledger.get("oldest_unresolved_age_seconds", 0.0) or 0.0
        )
        if pending_receipts or failed_receipts:
            recommendations.append(
                "Operator receipt mirrors are unresolved; inspect with `python scripts/playbooks.py receipts --json` and retry explicitly with `--apply --include-failed`."
            )
        if failed_receipts:
            failures.append(
                f"Operator receipt mirror failures require repair: failed={failed_receipts}"
            )
        elif pending_receipts >= 100 or oldest_receipt_age >= 3600.0:
            failures.append(
                f"Operator receipt mirror debt is stale or excessive: pending={pending_receipts}, oldest={oldest_receipt_age}s"
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
    status = "needs_repair" if failures else "ready"
    sqlite_payload = {
        "path": str(db_path),
        "status": status,
        "activation_lease": activation_lease,
        "storage_permissions": storage_permissions,
        "memory_count": memory_count,
        "tables": tables,
        "fts_integrity": fts_integrity,
        "graph_hygiene": graph_hygiene,
        "operator_ledger": operator_ledger,
        "relation_containment": relation_containment,
        "relation_frequency_index": relation_frequency,
        "relation_policy_generation": relation_generation,
        "relation_rebuild_queue": relation_rebuild,
        "lexical_generation": lexical_generation,
        "schema_migrations": schema_migrations,
        "lifecycle_registry": registry_report,
        "governance_audit_coverage": governance_audit_coverage,
        "fact_freshness": freshness_report,
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


_PLACEHOLDER_URI_USERS = {
    "default",
    "demo",
    "example",
    "service",
    "test",
    "user",
    "username",
}
_PLACEHOLDER_URI_PASSWORDS = {
    "changeme",
    "demo",
    "example",
    "pass",
    "passwd",
    "password",
    "placeholder",
    "secret",
    "test",
}
_PLACEHOLDER_URI_HOSTS = {
    "db-host",
    "example.com",
    "host",
    "hostname",
    "redis-host",
}


def _is_placeholder_like_database_uri_only(value: str) -> bool:
    """Classify ambiguous URI examples without weakening canonical scanning.

    A row is review-only only when every secret match is a database URI and
    every URI uses explicit placeholder values for username, password, and
    host. Any production-like component remains actionable because weak or
    default credentials can still be real deployment secrets.
    """

    matches = scan_secret_like_text(value)
    if not matches or any(match.name != "database_uri_with_password" for match in matches):
        return False
    for match in matches:
        try:
            parsed = urlsplit(match.text)
            placeholder_components = (
                str(parsed.username or "").casefold() in _PLACEHOLDER_URI_USERS,
                str(parsed.password or "").casefold() in _PLACEHOLDER_URI_PASSWORDS,
                str(parsed.hostname or "").casefold() in _PLACEHOLDER_URI_HOSTS,
            )
        except ValueError:
            return False
        if not all(placeholder_components):
            return False
    return True


def memory_secret_report(hermes_home: Path, *, sample_limit: int = 10) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    recommendations: list[str] = []
    db_path = hermes_home / "scope-recall" / "memory.sqlite3"
    if not db_path.exists():
        return {"status": "missing", "path": str(db_path), "active_secret_like_count": 0, "placeholder_like_uri_count": 0, "samples": [], "placeholder_like_samples": []}, {"ok": True, "failures": []}, recommendations
    samples: list[dict[str, Any]] = []
    placeholder_like_samples: list[dict[str, Any]] = []
    active_secret_like_count = 0
    placeholder_like_uri_count = 0
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "memories" not in tables:
                return {
                    "status": "schema_missing",
                    "path": str(db_path),
                    "active_secret_like_count": 0,
                    "placeholder_like_uri_count": 0,
                    "samples": [],
                    "placeholder_like_samples": [],
                }, {"ok": True, "failures": []}, recommendations
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
                sample = {
                    "id": str(row["id"]),
                    "scope_id": str(row["scope_id"] or ""),
                    "source": str(row["source"] or ""),
                    "target": str(row["target"] or ""),
                    "updated_at": str(row["updated_at"] or ""),
                    "preview": sanitize_report_text(content)[:220],
                }
                if _is_placeholder_like_database_uri_only(content):
                    placeholder_like_uri_count += 1
                    if len(placeholder_like_samples) < max(0, int(sample_limit)):
                        placeholder_like_samples.append(sample)
                    continue
                active_secret_like_count += 1
                if len(samples) < max(0, int(sample_limit)):
                    samples.append(sample)
        finally:
            conn.close()
    except Exception as exc:
        recommendations.append("Repair or restore the SQLite truth DB before trusting memory secret-scan status.")
        return {
            "status": "error",
            "path": str(db_path),
            "error": str(exc),
            "active_secret_like_count": 0,
            "placeholder_like_uri_count": 0,
            "samples": [],
            "placeholder_like_samples": [],
        }, {"ok": False, "failures": [f"memory secret scan error: {exc}"]}, recommendations

    payload = {
        "status": "ready",
        "path": str(db_path),
        "active_secret_like_count": active_secret_like_count,
        "placeholder_like_uri_count": placeholder_like_uri_count,
        "samples": samples,
        "placeholder_like_samples": placeholder_like_samples,
    }
    if active_secret_like_count:
        recommendations.append("Active memory rows contain plaintext secret-like content; archive or hard-delete them and store only secret indexes/vault refs.")
    if placeholder_like_uri_count:
        recommendations.append(
            "Active memory contains placeholder-like database URI patterns; review them manually. Canonical capture/store secret filtering remains fail-closed."
        )
    return payload, {"ok": active_secret_like_count == 0, "failures": [f"active plaintext secret-like memory rows: {active_secret_like_count}"] if active_secret_like_count else []}, recommendations



def runtime_pipeline_report(
    hermes_home: Path,
    runtime_config: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Report bounded capture configuration and persistent adjudication health.

    Capture is deliberately process-local in this patch release: queue pressure is
    returned synchronously to callers, so Doctor verifies the bounded contract
    rather than pretending it can inspect another process's transient depth.
    Adjudication ownership is durable and therefore inspected from the read-only
    governance ledger for stale claims, retry storms, and L4 configuration errors.
    """

    recommendations: list[str] = []
    failures: list[str] = []
    try:
        capture_capacity = int(runtime_config.get("capture_queue_capacity", 256))
    except (TypeError, ValueError):
        capture_capacity = 0
    capture_ok = 8 <= capture_capacity <= 4096
    capture = {
        "mode": "bounded_process_local",
        "capacity": capture_capacity,
        "durable_backlog": False,
        "pressure_result": "rejected",
        "ok": capture_ok,
    }
    if not capture_ok:
        failures.append("capture queue capacity is outside the validated 8..4096 range")
        recommendations.append(
            "Set capture_queue_capacity to a bounded value from 8 through 4096."
        )

    db_path = hermes_home / "scope-recall" / "memory.sqlite3"
    statuses: list[dict[str, Any]] = []
    adjudication_error = ""
    if db_path.exists():
        try:
            conn = connect_truth_database(db_path, mode="ro")
            conn.row_factory = sqlite3.Row
            try:
                tables = {
                    str(row[0])
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if "governance_audit_events" in tables:
                    target_rows = conn.execute(
                        """
                        SELECT DISTINCT target_id
                        FROM governance_audit_events
                        WHERE event_type = 'memory_auto_adjudication'
                          AND target_id LIKE 'auto_adjudication_schedule:%'
                        ORDER BY target_id
                        """
                    ).fetchall()
                    for row in target_rows:
                        target_id = str(row[0] or "")
                        status = adjudication_schedule_status(
                            conn,
                            target_id=target_id,
                        )
                        statuses.append(
                            {
                                "lane": "l4" if target_id.endswith(":l4") else "primary",
                                **status,
                            }
                        )
            finally:
                conn.close()
        except Exception as exc:
            adjudication_error = sanitize_report_text(str(exc))[:160]

    stale_claims = sum(bool(item.get("stale_claim")) for item in statuses)
    l4_config_errors = sum(bool(item.get("l4_config_error")) for item in statuses)
    retry_storms = sum(
        int(item.get("consecutive_failures") or 0) >= 3 for item in statuses
    )
    if adjudication_error:
        failures.append("auto-adjudication schedule ledger could not be inspected")
        recommendations.append(
            "Repair SQLite governance-ledger readability before trusting adjudication health."
        )
    if stale_claims:
        failures.append(f"stale auto-adjudication schedule claims: {stale_claims}")
        recommendations.append(
            "Inspect the recorded adjudication owner before allowing the expired claim to be reclaimed."
        )
    if l4_config_errors:
        failures.append(f"pending L4 configuration errors: {l4_config_errors}")
        recommendations.append(
            "Repair the journal LLM configuration, then let the persisted L4 retry complete."
        )
    if retry_storms:
        failures.append(f"auto-adjudication retry storms: {retry_storms}")
        recommendations.append(
            "Inspect repeated adjudication release/retry receipts before resuming automatic review."
        )

    adjudication = {
        "status": "error" if adjudication_error else ("ready" if statuses else "never_run"),
        "schedule_count": len(statuses),
        "stale_claims": stale_claims,
        "l4_config_errors": l4_config_errors,
        "retry_storms": retry_storms,
        "schedules": statuses,
    }
    if adjudication_error:
        adjudication["error"] = adjudication_error
    payload = {"capture": capture, "adjudication": adjudication}
    return payload, {"ok": not failures, "failures": failures}, recommendations
