"""Read-only agent entry point. Classify first; never migrate a fresh install."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

from .backup import _safe_path

WORKFLOW = Path(__file__).with_name("AGENT_WORKFLOW.md")


def inspect_installation(
    home: str | Path, *, host: str = "hermes", database: str | Path | None = None
) -> dict:
    if host not in {"hermes", "codex"}:
        raise ValueError("unsupported host")
    root = _safe_path(home)
    candidates = (
        [root / "scope-recall" / "memory.sqlite3", root / "lancepro" / "memory.sqlite3"]
        if host == "hermes"
        else [root / "data" / "memory.sqlite3"]
    )
    databases = [p for p in candidates if p.exists()]
    if database is not None:
        # Host configuration can select a nonstandard legacy location. An
        # explicit missing database is damage, never a fresh installation.
        databases = [_safe_path(database, must_exist=True)]
    result = dict(
        format="scope-recall.agent-setup/1",
        host=host,
        home=str(root),
        workflow_path=str(WORKFLOW),
        operator="agent",
        memory_review_required=False,
        route="fresh_install",
        reason="no_previous_database",
        databases=[str(p) for p in databases],
    )
    if len(databases) > 1:
        return dict(
            result,
            route="needs_agent_inspection",
            reason="multiple_legacy_databases",
            safe_action="keep_existing_installation",
        )
    if not databases:
        # A manifest without its truth DB is damage, not a fresh installation.
        manifests = [
            root / "scope-recall" / "installation.json",
            root / "codex-installation.json",
        ]
        if any(p.exists() for p in manifests):
            return dict(
                result,
                route="repair_required",
                reason="manifest_without_database",
                safe_action="restore_verified_backup",
            )
        return result
    database = _safe_path(databases[0], must_exist=True)
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.execute("PRAGMA query_only=ON")
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            schema = conn.execute("PRAGMA user_version").fetchone()[0]
            result["database_schema"] = schema
    except sqlite3.DatabaseError:
        return dict(
            result,
            route="repair_required",
            reason="database_unreadable",
            safe_action="keep_original_and_restore_backup",
        )
    if {"source_events", "instance_meta"} <= tables:
        manifest = root / (
            "scope-recall/installation.json"
            if host == "hermes"
            else "codex-installation.json"
        )
        if not manifest.is_file():
            return dict(
                result,
                route="repair_required",
                reason="current_database_without_manifest",
                safe_action="keep_original",
            )
        return dict(
            result,
            route="current_upgrade",
            reason="current_core_detected",
            migration_required=False,
        )
    if "memories" in tables or "journal_entries" in tables:
        return dict(
            result,
            route="legacy_migration",
            reason="legacy_truth_detected",
            source_database=str(database),
            migration_required=True,
        )
    return dict(
        result,
        route="unsupported",
        reason="unknown_database_format",
        safe_action="keep_original",
    )


def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")
