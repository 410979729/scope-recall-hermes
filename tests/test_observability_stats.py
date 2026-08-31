"""Focused Stats integration coverage for Fact and curation projections."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

from scope_recall.memory_queries import stats_payload
from scope_recall.sql_store import ensure_schema


class _StatsProvider:
    name = "scope-recall"

    def __init__(self, conn: sqlite3.Connection, hermes_home: Path) -> None:
        self._conn = conn
        self._lock = threading.RLock()
        self._scope_id = "scope-a"
        self._shared_scope_id = "scope-shared"
        self._shared_pool_scope_id = ""
        self._accessible_scope_ids = [self._scope_id, self._shared_scope_id]
        self._writable_scope_ids = list(self._accessible_scope_ids)
        self._hermes_home = hermes_home
        self._config = {"capture_queue": {"maxsize": 100}}

    def query_connection(self) -> sqlite3.Connection:
        return self._conn

    def query_lock(self) -> threading.RLock:
        return self._lock

    def query_scope_view(self) -> dict[str, Any]:
        return {
            "scope_id": self._scope_id,
            "shared_scope_id": self._shared_scope_id,
            "shared_pool_scope_id": self._shared_pool_scope_id,
            "accessible_scope_ids": list(self._accessible_scope_ids),
            "writable_scope_ids": list(self._writable_scope_ids),
        }

    def vector_status_view(self) -> dict[str, Any]:
        return {"enabled": False, "state": "disabled", "status": "disabled"}

    def retrieval_status_view(self) -> dict[str, Any]:
        return {"mode": "lexical", "lexical_weight": 1.0, "vector_weight": 0.0}

    def runtime_status_view(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "hermes_home": self._hermes_home,
            "truth_writer_role": "test",
            "observability_config": {
                "fact_evolution": {
                    "enabled": True,
                    "mode": "preview",
                    "nightly_mode": "preview",
                    "journal_mode": "preview",
                    "tool_mode": "preview",
                    "maintenance_mode": "preview",
                },
                "fact_backfill": {"shadow_enabled": False},
                "curation": {"owner": "external"},
                "journal": {"enabled": False},
            },
            "journal_digest_last_status": "never_run",
        }


def test_stats_exposes_fact_adoption_and_separate_curation_lanes(
    tmp_path: Path,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    provider = _StatsProvider(conn, tmp_path)
    before = conn.total_changes

    payload = stats_payload(provider)

    assert conn.total_changes == before
    assert payload["fact_evolution"]["feature_enabled"] is True
    assert payload["fact_evolution"]["state"] == "preview_no_claims"
    assert payload["fact_evolution"]["claim_count"] == 0
    assert payload["curation"]["authoritative_owner"] == "external"
    assert payload["curation"]["journal_digest"]["enabled"] is False
    assert (
        payload["curation"]["nightly_digest_legacy"]["last_status"]
        == "disabled_by_owner"
    )
    assert payload["curation"]["external_curation"]["last_status"] == "unobserved"
    assert "journal_digest" in payload
    conn.close()
