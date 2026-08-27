"""Typed query boundaries preserve the frozen public payloads."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from plugins.memory import load_memory_provider

from scope_recall import memory_queries


def _write_config(hermes_home: Path) -> None:
    config_path = hermes_home / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "auto_capture": False,
                "vector": {"enabled": False},
            }
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.fixture
def differential_provider(tmp_path: Path):
    _write_config(tmp_path)
    provider = load_memory_provider("scope-recall")
    assert provider is not None
    provider.initialize(
        "query-boundary-differential",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        receipt = json.loads(
            provider.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "Typed query boundaries preserve the public query contract exactly.",
                    "target": "project",
                },
            )
        )
        assert receipt.get("id")
        yield provider, str(receipt["id"])
    finally:
        provider.shutdown()


def _fingerprint(conn: sqlite3.Connection) -> tuple[int, int, int]:
    return (
        int(conn.total_changes),
        int(conn.execute("PRAGMA data_version").fetchone()[0]),
        int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]),
    )


def test_typed_query_application_matches_legacy_payloads_without_writes(
    differential_provider,
) -> None:
    provider, memory_id = differential_provider
    conn = provider._require_conn()
    cases: tuple[
        tuple[str, Callable[[], dict], Callable[[], dict]], ...
    ] = (
        (
            "context",
            lambda: provider._context_payload(
                query="public query contract", limit=5, max_chars=900
            ),
            lambda: memory_queries.context_payload(
                provider, query="public query contract", limit=5, max_chars=900
            ),
        ),
        (
            "profile",
            lambda: provider._profile_payload(
                query="public query contract",
                targets=["project"],
                include_curated=False,
                limit=5,
                max_chars=1200,
            ),
            lambda: memory_queries.profile_payload(
                provider,
                query="public query contract",
                targets=["project"],
                include_curated=False,
                limit=5,
                max_chars=1200,
            ),
        ),
        (
            "inspect",
            lambda: provider._inspect_memory(memory_id=memory_id),
            lambda: memory_queries.inspect_memory(provider, memory_id=memory_id),
        ),
        (
            "export",
            lambda: provider._export_memories(fmt="json", scope_only=True),
            lambda: memory_queries.export_memories(
                provider, fmt="json", scope_only=True
            ),
        ),
    )

    before = _fingerprint(conn)
    for name, typed_call, legacy_call in cases:
        assert typed_call() == legacy_call(), name
    after = _fingerprint(conn)
    assert after == before


def test_typed_stats_matches_legacy_stable_contract(differential_provider) -> None:
    provider, _memory_id = differential_provider
    typed = provider._stats_payload()
    legacy = memory_queries.stats_payload(provider)
    volatile = {
        "journal_digest_last_started",
        "journal_digest_last_finished",
        "write_transactions",
    }
    for key in sorted(set(typed) | set(legacy)):
        if key not in volatile:
            assert typed.get(key) == legacy.get(key), key
