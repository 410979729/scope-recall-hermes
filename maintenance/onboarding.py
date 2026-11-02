"""Read-only agent entry point. Classify first; never migrate a fresh install."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import yaml

from ..adapters.installation_receipt import RECEIPT_FILENAME
from .backup import safe_path

WORKFLOW = Path(__file__).with_name("AGENT_WORKFLOW.md")


def _selected_provider_without_binding(root: Path, host: str) -> bool:
    """Keep orphaned configuration and receipts from silently selecting a fresh empty store."""
    if (root / RECEIPT_FILENAME).exists():
        return True
    if host != "hermes":
        return False
    path = root / "config.yaml"
    if not path.is_file():
        return False
    try:
        if path.stat().st_size > 1024 * 1024:
            return True
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        return True
    if value is None:
        return False
    memory = value.get("memory", {}) if isinstance(value, dict) else None
    return not isinstance(memory, dict) or memory.get("provider") in {"scope-recall", "lancepro"}


def _empty_home_route(root: Path, host: str, result: dict) -> dict:
    """A missing database is fresh only when no previous installation marker remains."""
    manifests = [root / "scope-recall" / "installation.json", root / "codex-installation.json"]
    if any(path.exists() for path in manifests):
        return dict(
            result, route="repair_required", reason="manifest_without_database", safe_action="restore_verified_backup"
        )
    if _selected_provider_without_binding(root, host):
        return dict(
            result,
            route="repair_required",
            reason="selected_provider_without_binding",
            safe_action="preserve_original_and_inspect_configured_database",
        )
    return result


def planning_database_state(path: Path) -> str:
    """Describe the read-only planning snapshot without creating SQLite coordination files."""
    if not path.is_file():
        return "not_created"
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-journal")):
        return "unknown"
    return "main-file snapshot"


def inspect_installation(home: str | Path, *, host: str = "hermes", database: str | Path | None = None) -> dict:
    """Classify a selected home read-only; damaged or custom legacy state never authorizes a new store."""
    if host not in {"hermes", "codex"}:
        raise ValueError("unsupported host")
    root = safe_path(home)
    candidates = (
        [root / "scope-recall" / "memory.sqlite3", root / "lancepro" / "memory.sqlite3"]
        if host == "hermes"
        else [root / "data" / "memory.sqlite3"]
    )
    databases = [p for p in candidates if p.exists()]
    if database is not None:
        # Host configuration can select a nonstandard legacy location. An
        # explicit missing database is damage, never a fresh installation.
        databases = [safe_path(database, must_exist=True)]
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
        return _empty_home_route(root, host, result)
    database = safe_path(databases[0], must_exist=True)
    result["database_consistency"] = planning_database_state(database)
    active = result["database_consistency"] == "unknown"
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
            conn.execute("PRAGMA query_only=ON")
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
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
        manifest = root / ("scope-recall/installation.json" if host == "hermes" else "codex-installation.json")
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
    if active:
        return dict(
            result,
            route="needs_agent_inspection",
            reason="active_database_state_unknown",
            safe_action="quiesce_writers_and_rerun_setup",
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
