"""Map a test fake's intended private fields onto public truth-port methods.

Production MemoryCommandPort / MemoryQueryPort require query_connection,
query_lock, query_scope_view, and the explicit command/query views. This helper
only exposes methods the fake already intended through _require_conn, _lock,
scope ids, config, and vector fields. It does not teach production to fall
back to those private names.
"""

from __future__ import annotations

from typing import Any


def attach_public_truth_ports(fake: Any) -> Any:
    """Attach public truth-port methods onto ``fake`` and return the same object."""

    def query_connection() -> Any:
        require = getattr(fake, "_require_conn", None)
        if callable(require):
            return require()
        conn = getattr(fake, "_conn", None)
        if conn is None:
            raise TypeError("test fake has no intended query connection")
        return conn

    def query_lock() -> Any:
        lock = getattr(fake, "_lock", None)
        if lock is None:
            raise TypeError("test fake has no intended query lock")
        return lock

    def query_scope_view() -> dict[str, Any]:
        writable = list(getattr(fake, "_writable_scope_ids", []) or [])
        accessible = list(getattr(fake, "_accessible_scope_ids", []) or [])
        return {
            "scope_id": str(getattr(fake, "_scope_id", "") or ""),
            "shared_scope_id": str(getattr(fake, "_shared_scope_id", "") or ""),
            "shared_pool_scope_id": str(getattr(fake, "_shared_pool_scope_id", "") or ""),
            "writable_scope_ids": [str(item) for item in writable if str(item)],
            "accessible_scope_ids": [str(item) for item in accessible if str(item)],
        }

    def vector_status_view() -> dict[str, Any]:
        state = str(getattr(fake, "_vector_status", "") or "disabled")
        return {
            "enabled": bool(getattr(fake, "_vector_enabled", False)),
            "ready": bool(getattr(fake, "_vector_ready", False)),
            "state": state,
            "status": state,
            "reason_code": str(
                getattr(fake, "_vector_reason_code", "") or "test_fixture"
            ),
            "auto_recoverable": state in {"ready", "degraded"},
            "repair_required": state == "needs_repair",
            "usable_for_query": bool(
                getattr(fake, "_vector_usable_for_query", state == "ready")
            ),
            "message": str(getattr(fake, "_vector_message", "") or ""),
            "debt_counts": dict(getattr(fake, "_vector_debt_counts", None) or {}),
            "backend": str(getattr(fake, "_vector_backend", "") or ""),
            "path": str(getattr(fake, "_vector_path", "") or ""),
            "table": str(getattr(fake, "_vector_table", "") or ""),
            "row_count": int(getattr(fake, "_vector_row_count", 0) or 0),
            "unique_id_count": int(getattr(fake, "_vector_unique_id_count", 0) or 0),
            "duplicate_row_count": int(getattr(fake, "_vector_duplicate_row_count", 0) or 0),
        }

    def retrieval_status_view() -> dict[str, Any]:
        config = dict(getattr(fake, "_retrieval_config", None) or {})
        return {
            "config": config,
            "mode": str(config.get("mode") or "lexical"),
            "lexical_weight": float(config.get("lexical_weight") or 1.0),
            "vector_weight": float(config.get("vector_weight") or 0.0),
        }

    def runtime_status_view() -> dict[str, Any]:
        db_path = getattr(fake, "_db_path", None)
        return {
            "status": str(getattr(fake, "_runtime_status", "") or ""),
            "name": str(getattr(fake, "name", "") or ""),
            "hermes_home": getattr(fake, "_hermes_home", None),
            "db_path": str(db_path) if db_path else "",
            "truth_writer_role": str(getattr(fake, "_truth_writer_role", "unknown") or "unknown"),
            "truth_writer_owner": dict(getattr(fake, "_truth_writer_owner", {}) or {}),
            "last_adjudication_report": dict(
                getattr(fake, "_last_adjudication_report", {}) or {}
            ),
            "shared_pool_enabled": bool(getattr(fake, "_shared_pool_enabled", False)),
            "shared_pool_write_enabled": bool(
                getattr(fake, "_shared_pool_write_enabled", False)
            ),
            "shared_pool_id": str(getattr(fake, "_shared_pool_id", "") or ""),
            "migration_info": dict(getattr(fake, "_migration_info", {}) or {}),
            "writer_thread_alive": False,
            "writer_failed_writes": int(getattr(fake, "_writer_failed_writes", 0) or 0),
            "writer_reported_failures": int(
                getattr(fake, "_writer_reported_failures", 0) or 0
            ),
            "writer_last_error_type": str(
                getattr(fake, "_writer_last_error_type", "") or ""
            ),
            "freshness_backfill": dict(getattr(fake, "_freshness_backfill", {}) or {}),
            "journal_digest_thread_alive": False,
            "journal_digest_last_started": 0.0,
            "journal_digest_last_finished": 0.0,
            "journal_digest_last_status": "never_run",
            "journal_digest_last_error": "",
            "journal_digest_consecutive_failures": 0,
        }

    def config_view() -> dict[str, Any]:
        raw = getattr(fake, "_config", None)
        return dict(raw) if isinstance(raw, dict) else {}

    def config_value(key: str, default: Any = None) -> Any:
        existing = getattr(fake, "_config_value", None)
        if callable(existing):
            return existing(key, default)
        return config_view().get(key, default)

    def clean_text(text: Any) -> str:
        existing = getattr(fake, "_clean_text", None)
        if callable(existing):
            return str(existing(text) or "")
        return str(text or "").strip()

    def writable_scope_ids() -> list[str]:
        return [
            str(item)
            for item in (query_scope_view().get("writable_scope_ids") or [])
            if str(item)
        ]

    ports = {
        "query_connection": query_connection,
        "query_lock": query_lock,
        "query_scope_view": query_scope_view,
        "vector_status_view": vector_status_view,
        "retrieval_status_view": retrieval_status_view,
        "runtime_status_view": runtime_status_view,
        "config_view": config_view,
        "config_value": config_value,
        "clean_text": clean_text,
        "writable_scope_ids": writable_scope_ids,
    }
    for name, method in ports.items():
        if not callable(getattr(fake, name, None)):
            setattr(fake, name, method)
    rollback = getattr(fake, "_rollback_conn_after_error", None)
    if callable(rollback) and not callable(getattr(fake, "rollback_conn_after_error", None)):
        setattr(fake, "rollback_conn_after_error", rollback)
    return fake
