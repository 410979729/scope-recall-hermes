#!/usr/bin/env python3
"""Rehearse candidate isolation against a disposable in-memory truth store."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from threading import RLock
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "scope_recall_candidate_isolation_runtime"
if PACKAGE_NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load scope-recall package from {PLUGIN_ROOT}")
    package = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = package
    spec.loader.exec_module(package)

from scope_recall_candidate_isolation_runtime.candidate_extraction import (  # noqa: E402
    ExtractedCandidate,
)
from scope_recall_candidate_isolation_runtime.candidate_promotion import (  # noqa: E402
    classify_candidate_row,
)
from scope_recall_candidate_isolation_runtime.candidate_review import (  # noqa: E402
    review_candidate,
)
from scope_recall_candidate_isolation_runtime.candidate_store import (  # noqa: E402
    store_event_candidates,
)
from scope_recall_candidate_isolation_runtime.memory_queries import (  # noqa: E402
    inspect_memory,
    profile_payload,
)
from scope_recall_candidate_isolation_runtime.models import RuntimeScope  # noqa: E402
from scope_recall_candidate_isolation_runtime.sql_store import ensure_schema  # noqa: E402
from scope_recall_candidate_isolation_runtime.storage_views import (  # noqa: E402
    search_db_memories,
)
from scope_recall_candidate_isolation_runtime.truth_connection import (  # noqa: E402
    connect_truth_database,
)


class _CandidateProvider:
    name = "candidate-isolation-rehearsal"

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = RLock()
        self._scope_id = "scope-a"
        self._shared_scope_id = "scope-a"
        self._accessible_scope_ids = ["scope-a"]
        self._writable_scope_ids = ["scope-a"]
        self._retrieval_config = {"candidate_pool": 5, "min_score": 0.18}

    @staticmethod
    def _config_value(key: str, default: Any) -> Any:
        del key
        return default

    def _require_conn(self) -> sqlite3.Connection:
        return self._conn

    def query_connection(self) -> sqlite3.Connection:
        return self._conn

    def query_lock(self) -> RLock:
        return self._lock

    @staticmethod
    def query_scope_view() -> dict[str, Any]:
        return {
            "scope_id": "scope-a",
            "shared_scope_id": "scope-a",
            "accessible_scope_ids": ["scope-a"],
        }

    def retrieval_status_view(self) -> dict[str, Any]:
        return {"config": dict(self._retrieval_config)}

    @staticmethod
    def runtime_status_view() -> dict[str, Any]:
        return {}


def _section_ids(payload: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    sections = payload.get("sections")
    if not isinstance(sections, dict):
        return ids
    for section in sections.values():
        if not isinstance(section, dict):
            continue
        items = section.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and str(item.get("id") or ""):
                ids.add(str(item["id"]))
    return ids


def build_candidate_isolation_evidence() -> dict[str, Any]:
    """Execute write-boundary and read-boundary checks on disposable SQLite."""

    conn = connect_truth_database(":memory:", mode="rwc")
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        conn.commit()
        scope = RuntimeScope(
            platform="cli",
            user_id="fixture-user",
            agent_identity="fixture-agent",
            agent_workspace="fixture-workspace",
        )

        before_wrapper = int(conn.total_changes)
        wrapper_result = store_event_candidates(
            conn,
            candidates=[
                ExtractedCandidate(
                    target="memory",
                    content="[CONTEXT COMPACTION] Historical transport wrapper.",
                    memory_type="summary",
                    confidence=0.99,
                    evidence_refs=["journal:wrapper"],
                )
            ],
            scope=scope,
            scope_id="scope-a",
            session_id="wrapper-session",
            dry_run=False,
        )
        wrapper_total_changes_delta = int(conn.total_changes) - before_wrapper

        candidate_result = store_event_candidates(
            conn,
            candidates=[
                ExtractedCandidate(
                    target="memory",
                    content="Stable durable workflow requires verified evidence before reuse.",
                    memory_type="workflow",
                    confidence=0.92,
                    evidence_refs=["journal:stable"],
                )
            ],
            scope=scope,
            scope_id="scope-a",
            session_id="stable-session",
            dry_run=False,
        )
        candidate_id = str((candidate_result.get("ids") or [""])[0])
        row = conn.execute(
            "SELECT * FROM memories WHERE id = ?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("candidate rehearsal failed to persist safe candidate")
        decision = classify_candidate_row(row, conn)
        provider = _CandidateProvider(conn)

        before_read_boundary = int(conn.total_changes)
        ordinary_search_ids = {
            item.id
            for item in search_db_memories(
                provider,
                "stable durable workflow",
                limit=5,
            )
        }
        ordinary_profile_ids = _section_ids(
            profile_payload(
                provider,
                include_candidates=False,
                include_curated=False,
                limit=5,
            )
        )
        explicit_profile_ids = _section_ids(
            profile_payload(
                provider,
                include_candidates=True,
                include_curated=False,
                limit=5,
            )
        )
        inspected = inspect_memory(provider, memory_id=candidate_id)
        review_plan = review_candidate(
            conn,
            memory_id=candidate_id,
            action="promote",
            dry_run=True,
            actor="candidate-isolation-rehearsal",
        )
        read_total_changes_delta = int(conn.total_changes) - before_read_boundary

        candidate_ordinary_leak_count = int(candidate_id in ordinary_search_ids) + int(
            candidate_id in ordinary_profile_ids
        )
        unreviewed_auto_promote_count = int(decision.action == "promote")
        explicit_profile_visible_count = int(candidate_id in explicit_profile_ids)
        explicit_review_visible_count = int(
            bool(inspected.get("found"))
            and bool(review_plan.get("ok"))
            and bool(review_plan.get("dry_run"))
        )
        wrapper_insert_count = int(wrapper_result.get("inserted") or 0)
        passed = all(
            (
                wrapper_insert_count == 0,
                wrapper_total_changes_delta == 0,
                candidate_ordinary_leak_count == 0,
                unreviewed_auto_promote_count == 0,
                explicit_profile_visible_count == 1,
                explicit_review_visible_count == 1,
                read_total_changes_delta == 0,
            )
        )
        return {
            "schema_version": 1,
            "wrapper_insert_count": wrapper_insert_count,
            "wrapper_total_changes_delta": wrapper_total_changes_delta,
            "candidate_ordinary_leak_count": candidate_ordinary_leak_count,
            "unreviewed_auto_promote_count": unreviewed_auto_promote_count,
            "explicit_profile_visible_count": explicit_profile_visible_count,
            "explicit_review_visible_count": explicit_review_visible_count,
            "read_total_changes_delta": read_total_changes_delta,
            "passed": passed,
        }
    finally:
        conn.close()


def main() -> int:
    payload = build_candidate_isolation_evidence()
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
