"""Independent Python/SQL lifecycle visibility contract, including legacy rows."""

from __future__ import annotations

import json
import sqlite3
import threading

import pytest

from scope_recall.gating import compact_text, dedup_key
from scope_recall.lifecycle_policy import (
    DURABLE_HIDDEN_LIFECYCLES,
    LIFECYCLE_STRIP_CHARACTERS,
    ORDINARY_RECALL_HIDDEN_LIFECYCLES,
    durable_lifecycle_visible,
    durable_lifecycle_visible_sql,
    ordinary_recall_lifecycle_visible,
    ordinary_recall_lifecycle_visible_sql,
)
from scope_recall.sql_store import (
    ensure_schema,
    exact_duplicate_groups,
    fts_integrity_report,
)
from scope_recall.storage_views import search_db_memories


LEGAL_LIFECYCLES = (
    "candidate",
    "scratch",
    "in_progress",
    "promoted",
    "active",
    "archived",
    "obsolete",
    "rejected",
    "superseded",
)
TARGETS = ("user", "general", "memory")


class _ViewProvider:
    def __init__(self, conn: sqlite3.Connection, scope_id: str = "scope-a") -> None:
        self._conn = conn
        self._lock = threading.RLock()
        self._scope_id = scope_id
        self._shared_scope_id = scope_id
        self._accessible_scope_ids = [scope_id]
        self._retrieval_config = {"candidate_pool": 12, "min_score": 0.0}

    def _require_conn(self) -> sqlite3.Connection:
        return self._conn

    def _config_value(self, key: str, default):
        return default


def _expected_ordinary(lifecycle: object, target: object) -> bool:
    token = "" if lifecycle is None else str(lifecycle).strip().lower()
    target_token = "" if target is None else str(target).strip().lower()
    hidden = {
        "archived",
        "obsolete",
        "rejected",
        "superseded",
        "candidate",
        "scratch",
        "in_progress",
    }
    return token not in hidden or (target_token == "general" and token == "scratch")


def _expected_durable(lifecycle: object) -> bool:
    token = "" if lifecycle is None else str(lifecycle).strip().lower()
    return token not in {"archived", "obsolete", "rejected", "superseded"}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _insert_legacy(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    target: str,
    metadata: object,
    content: str,
    scope_id: str = "scope-a",
) -> None:
    if metadata is None:
        metadata_sql: str | None = None
    elif isinstance(metadata, str):
        metadata_sql = metadata
    else:
        metadata_sql = json.dumps(metadata, ensure_ascii=False)
    now = "2026-01-01T00:00:00+00:00"
    conn.execute(
        """
        INSERT INTO memories (
            id, scope_id, platform, user_id, chat_id, thread_id, gateway_session_key,
            agent_identity, agent_workspace, session_id, source, target, content,
            summary, created_at, updated_at, last_recalled_turn, dedup_key, metadata
        ) VALUES (?, ?, 'telegram', 'user-a', 'chat-a', '', '', 'yuheng', 'hermes',
                  'legacy', 'user', ?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (
            memory_id,
            scope_id,
            target,
            content,
            compact_text(content, 220),
            now,
            now,
            dedup_key(content),
            metadata_sql,
        ),
    )


def _sql_visible(
    conn: sqlite3.Connection,
    memory_id: str,
    predicate: str,
) -> bool:
    row = conn.execute(
        f"SELECT {predicate} AS visible FROM memories AS m WHERE m.id = ?",
        (memory_id,),
    ).fetchone()
    return bool(row["visible"])


def _decoded_lifecycle(metadata: object) -> object:
    if metadata is None:
        return None
    if not isinstance(metadata, str):
        return metadata.get("lifecycle") if isinstance(metadata, dict) else None
    try:
        parsed = json.loads(metadata)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed.get("lifecycle")


@pytest.mark.parametrize("lifecycle", LEGAL_LIFECYCLES)
@pytest.mark.parametrize("target", TARGETS)
def test_legal_lifecycle_python_and_sql_match_independent_policy(lifecycle, target):
    conn = _connect()
    memory_id = f"legal-{target}-{lifecycle}"
    _insert_legacy(
        conn,
        memory_id=memory_id,
        target=target,
        metadata={"lifecycle": lifecycle},
        content=f"Legal {lifecycle} row for {target} target stays policy-stable.",
    )
    expected_ordinary = _expected_ordinary(lifecycle, target)
    expected_durable = _expected_durable(lifecycle)
    assert ordinary_recall_lifecycle_visible(
        lifecycle=lifecycle, target=target
    ) is expected_ordinary
    assert durable_lifecycle_visible(lifecycle=lifecycle) is expected_durable
    assert _sql_visible(
        conn, memory_id, ordinary_recall_lifecycle_visible_sql("m")
    ) is expected_ordinary
    assert _sql_visible(
        conn, memory_id, durable_lifecycle_visible_sql("m")
    ) is expected_durable
    assert ORDINARY_RECALL_HIDDEN_LIFECYCLES >= DURABLE_HIDDEN_LIFECYCLES


@pytest.mark.parametrize(
    ("raw_lifecycle", "target"),
    [
        (" candidate ", "user"),
        ("\tcandidate\t", "user"),
        ("\u3000candidate\u3000", "user"),
        ("\u00a0CANDIDATE\u00a0", "user"),
        ("CANDIDATE", "user"),
        ("  PROMOTED  ", "user"),
        (" scratch ", "general"),
        (" scratch ", "user"),
        ("\u3000scratch\u3000", "general"),
        ("  IN_PROGRESS  ", "memory"),
    ],
)
def test_whitespace_and_case_python_sql_match_independent_policy(raw_lifecycle, target):
    conn = _connect()
    memory_id = "padded-lifecycle"
    _insert_legacy(
        conn,
        memory_id=memory_id,
        target=target,
        metadata={"lifecycle": raw_lifecycle},
        content="Padded lifecycle tokens must use one strip-and-case contract.",
    )
    expected_ordinary = _expected_ordinary(raw_lifecycle, target)
    expected_durable = _expected_durable(raw_lifecycle)
    assert ordinary_recall_lifecycle_visible(
        lifecycle=raw_lifecycle, target=target
    ) is expected_ordinary
    assert durable_lifecycle_visible(lifecycle=raw_lifecycle) is expected_durable
    assert _sql_visible(
        conn, memory_id, ordinary_recall_lifecycle_visible_sql("m")
    ) is expected_ordinary
    assert _sql_visible(
        conn, memory_id, durable_lifecycle_visible_sql("m")
    ) is expected_durable


@pytest.mark.parametrize(
    ("metadata", "target", "lifecycle_for_python"),
    [
        (None, "user", None),
        ("{}", "user", None),
        ('{"lifecycle": null}', "user", None),
        ('{"lifecycle": ""}', "user", ""),
        ("not-json", "user", None),
        ("[]", "user", None),
        ('{"lifecycle": "mystery"}', "user", "mystery"),
        ('{"lifecycle": "Candidate-Review"}', "general", "Candidate-Review"),
    ],
)
def test_missing_invalid_unknown_lifecycle_keeps_legacy_visibility(
    metadata,
    target,
    lifecycle_for_python,
):
    conn = _connect()
    memory_id = "legacy-absent"
    _insert_legacy(
        conn,
        memory_id=memory_id,
        target=target,
        metadata=metadata,
        content="Absent or unknown lifecycle remains ordinarily visible.",
    )
    expected_ordinary = _expected_ordinary(lifecycle_for_python, target)
    decoded = _decoded_lifecycle(metadata)
    assert expected_ordinary is True
    assert _expected_durable(lifecycle_for_python) is True
    assert ordinary_recall_lifecycle_visible(
        lifecycle=decoded if decoded is not None else "",
        target=target,
    ) is True
    assert durable_lifecycle_visible(
        lifecycle=decoded if decoded is not None else ""
    ) is True
    assert _sql_visible(conn, memory_id, ordinary_recall_lifecycle_visible_sql("m")) is True
    assert _sql_visible(conn, memory_id, durable_lifecycle_visible_sql("m")) is True


def test_padded_target_general_scratch_matches_python_strip():
    conn = _connect()
    _insert_legacy(
        conn,
        memory_id="padded-target",
        target=" general ",
        metadata={"lifecycle": "scratch"},
        content="Same-scope general scratch stays visible after target strip.",
    )
    assert _expected_ordinary("scratch", " general ") is True
    assert ordinary_recall_lifecycle_visible(
        lifecycle="scratch", target=" general "
    ) is True
    assert _sql_visible(
        conn, "padded-target", ordinary_recall_lifecycle_visible_sql("m")
    ) is True


def test_strip_character_contract_matches_python_str_strip():
    observed = "".join(chr(code) for code in range(0x110000) if not chr(code).strip())
    assert LIFECYCLE_STRIP_CHARACTERS == observed


def test_fts_integrity_counts_padded_candidate_as_hidden():
    conn = _connect()
    _insert_legacy(
        conn,
        memory_id="visible-promoted",
        target="user",
        metadata={"lifecycle": "promoted"},
        content="Visible promoted memory remains an expected FTS member.",
    )
    _insert_legacy(
        conn,
        memory_id="padded-candidate",
        target="user",
        metadata={"lifecycle": " candidate "},
        content="Padded candidate must not be an expected ordinary FTS member.",
    )
    report = fts_integrity_report(conn)
    assert report["memory_rows"] == 2
    assert report["expected_fts_rows"] == 1


def test_exact_duplicate_groups_ignore_padded_candidate():
    conn = _connect()
    content = "Exact duplicate grouping must ignore padded candidate twins."
    _insert_legacy(
        conn,
        memory_id="visible-a",
        target="user",
        metadata={"lifecycle": "promoted"},
        content=content,
    )
    _insert_legacy(
        conn,
        memory_id="visible-b",
        target="user",
        metadata={"lifecycle": "active"},
        content=content,
    )
    _insert_legacy(
        conn,
        memory_id="padded-candidate",
        target="user",
        metadata={"lifecycle": " candidate "},
        content=content,
    )
    groups = exact_duplicate_groups(conn, scope_id="scope-a")
    assert len(groups) == 1
    assert groups[0]["count"] == 2
    assert set(groups[0]["delete_ids"]) <= {"visible-a", "visible-b"}
    assert groups[0]["keep_id"] in {"visible-a", "visible-b"}


def test_search_db_memories_exact_id_hides_padded_candidate():
    conn = _connect()
    _insert_legacy(
        conn,
        memory_id="PADDED-CAND-01",
        target="user",
        metadata={"lifecycle": " candidate "},
        content="Exact identifier must not bypass padded candidate concealment.",
    )
    _insert_legacy(
        conn,
        memory_id="VISIBLE-PROMO-01",
        target="user",
        metadata={"lifecycle": "promoted"},
        content="Exact identifier still returns ordinary-visible promoted memory.",
    )
    provider = _ViewProvider(conn)
    assert search_db_memories(provider, "PADDED-CAND-01", limit=5) == []
    visible = search_db_memories(provider, "VISIBLE-PROMO-01", limit=5)
    assert [item.id for item in visible] == ["VISIBLE-PROMO-01"]
