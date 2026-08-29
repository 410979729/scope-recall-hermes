"""Executable counterexamples from the sixth independent 1.8.0 audit.

These tests intentionally use temporary Hermes homes and real SQLite files.  They
protect invariants rather than line-number-specific implementations: public
mutations are serializable, merges are atomic, evidence denotes a current durable
state, activation authority is connection-scoped, and N-1 privacy config survives.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import threading
from typing import Any

import pytest

from plugins.memory import load_memory_provider

from scope_recall.config import load_runtime_config, load_runtime_config_errors
from scope_recall.evolution_policy import evaluate_evolution_policy
from scope_recall.fact_actions import (
    ClaimDraft,
    EvidenceReference,
    EvolutionAction,
    EvolutionProposal,
)
from scope_recall.fact_evidence import (
    evidence_supports_claim,
    evidence_supports_retraction,
)
from scope_recall.fact_repository import insert_claim
import scope_recall.installer as installer
from scope_recall.maintenance_lease import install_activation_lease_authorizer
from scope_recall.vector_generation import (
    CURRENT_GENERATION_KEY,
    ensure_vector_generation_schema,
)
from scope_recall.activation_transaction import (
    capture_activation_state,
    committed_activation_receipt,
    refresh_activation_sqlite_epoch,
)


@pytest.fixture
def provider(tmp_path: Path):
    config_path = tmp_path / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "relation_extraction_enabled": False,
                "vector": {"enabled": False},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "sixth-audit-session",
        hermes_home=str(tmp_path),
        platform="test",
        user_id="fixture-user",
        chat_id="fixture-chat",
        agent_context="primary",
        agent_identity="test-agent",
        agent_workspace="test-workspace",
    )
    yield plugin
    plugin.shutdown()


def _store(provider: Any, content: str) -> str:
    memory_id, inserted, _outcome = provider._store_now(
        content=content,
        source="tool-store",
        target="memory",
        session_id=provider._session_id,
        allow_duplicate=True,
    )
    assert inserted is True
    assert memory_id
    return str(memory_id)


def test_legacy_update_is_serializable_with_concurrent_fact_claim(
    provider: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A claim cannot appear between the ownership guard and legacy UPDATE."""

    memory_id = _store(provider, "Asha lives in Mumbai.")
    with provider._lock:
        row = provider._require_conn().execute(
            "SELECT scope_id FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
    assert row is not None
    scope_id = str(row["scope_id"])

    guard_seen = threading.Event()
    competing_writer_done = threading.Event()
    writer_result: dict[str, object] = {}
    # Hermes loads user plugins under a private package alias. Patch the exact
    # function-global used by this provider, not a second `scope_recall` alias.
    update_memory_fn = provider._update_memory.__func__.__globals__["update_memory"]
    update_row_fn = update_memory_fn.__globals__["update_row"]
    real_guard = update_row_fn.__globals__["require_fact_mutation_authority"]

    def barrier_guard(*args: Any, **kwargs: Any) -> None:
        real_guard(*args, **kwargs)
        guard_seen.set()
        assert competing_writer_done.wait(5), "competing claim writer did not finish"

    monkeypatch.setitem(
        update_row_fn.__globals__,
        "require_fact_mutation_authority",
        barrier_guard,
    )

    def bind_claim_after_guard() -> None:
        assert guard_seen.wait(5), "legacy ownership guard was not reached"
        second = sqlite3.connect(provider._db_path, timeout=0.2)
        second.row_factory = sqlite3.Row
        second.execute("PRAGMA foreign_keys=ON")
        try:
            insert_claim(
                second,
                claim_id="claim-race",
                memory_id=memory_id,
                scope_id=scope_id,
                subject="Asha",
                predicate="lives in",
                value="Mumbai",
                valid_from="2026-01-01T00:00:00+00:00",
                recorded_at="2026-07-16T00:00:00+00:00",
                confidence=0.99,
                source_type="user_message",
                source_ref="race-fixture",
            )
            second.commit()
            writer_result["committed"] = True
        except sqlite3.OperationalError as exc:
            second.rollback()
            writer_result["committed"] = False
            writer_result["error"] = str(exc)
        finally:
            second.close()
            competing_writer_done.set()

    competitor = threading.Thread(target=bind_claim_after_guard, daemon=True)
    competitor.start()
    updated, _summary, _updated_at = provider._update_memory(
        memory_id,
        "Asha lives in Bangalore.",
        "memory",
    )
    competitor.join(timeout=5)
    assert not competitor.is_alive()

    with provider._lock:
        truth = provider._require_conn().execute(
            "SELECT content FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        claim_count = int(
            provider._require_conn()
            .execute(
                "SELECT COUNT(*) FROM fact_claims WHERE memory_id = ? AND status = 'current'",
                (memory_id,),
            )
            .fetchone()[0]
        )
    assert truth is not None
    dual_truth = (
        bool(updated)
        and bool(writer_result.get("committed"))
        and claim_count == 1
        and "Bangalore" in str(truth["content"])
    )
    assert dual_truth is False


_TEMPORAL_EVENT_CASES = (
    ("lives in", "Paris", "Alice moved to Paris for a two-week conference."),
    ("lives in", "Paris", "Alice will move to Paris next month."),
    ("lives in", "Paris", "Alice is moving to Paris for summer."),
    ("lives in", "Paris", "Alice relocated to Paris temporarily."),
    ("lives in", "Paris", "Alice moved to Paris on vacation."),
    ("works at", "OldCo", "Alice worked at OldCo in 2019."),
    ("works at", "OldCo", "Alice worked at OldCo."),
    ("works at", "OldCo", "Alice worked at OldCo from June to August."),
    ("works at", "OldCo", "Alice worked at OldCo last summer."),
    ("works at", "OldCo", "Alice was working at OldCo over the summer."),
    ("works at", "OldCo", "Alice started working at OldCo."),
    ("works at", "OldCo", "Alice stopped working at OldCo."),
    ("works at", "OldCo", "Alice began working at OldCo."),
    ("works at", "OldCo", "Alice finished working at OldCo."),
    ("works at", "OldCo", "Alice tried working at OldCo."),
    ("works at", "OldCo", "Alice considered working at OldCo."),
    ("works at", "OldCo", "Alice returned to working at OldCo."),
    ("works at", "OldCo", "Alice is resuming working at OldCo."),
    ("works at", "OldCo", "Alice commenced working at OldCo."),
    ("works at", "OldCo", "Alice ceased working at OldCo."),
    ("works at", "OldCo", "Alice attempted working at OldCo."),
    ("works at", "OldCo", "Alice contemplated working at OldCo."),
    ("works at", "OldCo", "Alice will work at OldCo next year."),
    ("works at", "OldCo", "Alice is working at OldCo for a two-week contract."),
    ("works at", "OldCo", "Alice works at OldCo from June through August."),
    ("works at", "OldCo", "Alice works at OldCo from Jun to Aug."),
    ("works at", "OldCo", "Alice works at OldCo from Jun. through Aug."),
    ("works at", "OldCo", "Alice works at OldCo from Jan. to Mar."),
    ("works at", "OldCo", "Alice works at OldCo from Jun.-Aug."),
    ("works at", "OldCo", "Alice works at OldCo from Sep.–Nov."),
    ("works at", "OldCo", "Alice works at OldCo from June-August."),
    ("works at", "OldCo", "Alice works at OldCo between Jun and Aug."),
    ("works at", "OldCo", "Alice works at OldCo Jun-Aug."),
    ("works at", "OldCo", "Alice works at OldCo for a six-month contract."),
    ("works at", "OldCo", "Alice works at OldCo for six months."),
    ("works at", "OldCo", "Alice works at OldCo for a 6-month contract."),
    ("works at", "OldCo", "Alice works at OldCo for 6 months."),
    ("works at", "OldCo", "Alice works at OldCo for five years."),
    ("works at", "OldCo", "Alice works at OldCo on a contract through August."),
    ("works at", "OldCo", "Alice works at OldCo under a fixed-term contract."),
    ("works at", "OldCo", "Alice works at OldCo June through August."),
    ("works at", "OldCo", "Alice works at OldCo for now."),
    ("works at", "OldCo", "Alice works at OldCo for the time being."),
    ("works at", "OldCo", "Alice works at OldCo for another six months."),
    ("works at", "OldCo", "Alice works at OldCo for another year."),
    ("works at", "OldCo", "Alice works at OldCo for up-to six months."),
    ("works at", "OldCo", "Alice works at OldCo for upto six months."),
    ("works at", "OldCo", "Alice works at OldCo for no-more-than six months."),
    ("works at", "OldCo", "Alice works at OldCo for max six months."),
    ("works at", "OldCo", "Alice works at OldCo for min six months."),
    ("works at", "OldCo", "Alice works at OldCo for a max of six months."),
    ("works at", "OldCo", "Alice works at OldCo for maximum six months."),
    ("works at", "OldCo", "Alice works at OldCo for minimum six months."),
    ("works at", "OldCo", "Alice works at OldCo for up-to-six months."),
    ("works at", "OldCo", "Alice works at OldCo for upto-six months."),
    ("works at", "OldCo", "Alice works at OldCo for no-more-than-six months."),
    ("works at", "OldCo", "Alice works at OldCo for six mths."),
    ("works at", "OldCo", "Alice works at OldCo for 6 mth."),
    ("works at", "OldCo", "Alice works at OldCo for six mnths."),
    ("works at", "OldCo", "Alice works at OldCo for 6 mon."),
    ("works at", "OldCo", "Alice works at OldCo for no more than six months."),
    ("works at", "OldCo", "Alice works at OldCo for up to six months."),
    ("works at", "OldCo", "Alice works at OldCo for a half-year contract."),
    ("works at", "OldCo", "Alice works at OldCo on probation."),
    ("works at", "OldCo", "Alice works at OldCo for only six months."),
    ("works at", "OldCo", "Alice works at OldCo for just six months."),
    ("works at", "OldCo", "Alice works at OldCo for six more months."),
    ("works at", "OldCo", "Alice works at OldCo for the coming six months."),
    ("works at", "OldCo", "Alice works at OldCo for under six months."),
    ("works at", "OldCo", "Alice works at OldCo for over six months."),
    ("works at", "OldCo", "Alice works at OldCo for fewer than six months."),
    ("works at", "OldCo", "Alice works at OldCo for a maximum of six months."),
    ("works at", "OldCo", "Alice works at OldCo for a minimum of six months."),
    ("works at", "OldCo", "Alice works at OldCo for six to twelve months."),
    ("works at", "OldCo", "Alice works at OldCo for 6-12 months."),
    ("works at", "OldCo", "Alice works at OldCo for six–twelve months."),
    ("works at", "OldCo", "Alice works at OldCo for the rest of the year."),
    ("works at", "OldCo", "Alice works at OldCo for the remainder of the year."),
    ("works at", "OldCo", "Alice works at OldCo between June 1 and August 31."),
    ("works at", "OldCo", "Alice works at OldCo from 6/1 to 8/31."),
    ("works at", "OldCo", "Alice works at OldCo from 2026-06-01 to 2026-08-31."),
    ("works at", "OldCo", "Alice works at OldCo from 2026/06/01 to 2026/08/31."),
    ("works at", "OldCo", "Alice works at OldCo from 2026.06.01 through 2026.08.31."),
    ("works at", "OldCo", "Alice works at OldCo from 1 June to 31 August."),
    ("works at", "OldCo", "Alice works at OldCo from 1st Jun. through 31st Aug."),
    ("works at", "OldCo", "Alice works at OldCo from 2026 June 1 to 2026 August 31."),
    ("works at", "OldCo", "Alice works at OldCo 2026 Jun. 1–2026 Aug. 31."),
    ("works at", "OldCo", "Alice works at OldCo from 01.06 through 31.08."),
    ("works at", "OldCo", "Alice works at OldCo between 1 June and 31 Aug."),
    ("works at", "OldCo", "Alice works at OldCo from June 2026 to Aug. 2026."),
    ("works at", "OldCo", "Alice works at OldCo from 2026 June to 2026 Aug."),
    ("works at", "OldCo", "Alice works at OldCo 2026-06–2026-08."),
    ("works at", "OldCo", "Alice works at OldCo from early June through late August."),
    ("works at", "OldCo", "Alice works at OldCo 2026-06-01–2026-08-31."),
    ("works at", "OldCo", "Alice works at OldCo between 6/1 and 8/31."),
    ("works at", "OldCo", "Alice works at OldCo 6/1-8/31."),
    ("works at", "OldCo", "Alice works at OldCo from 06-01 through 08-31."),
    ("works at", "OldCo", "Alice works at OldCo，为期六个月。"),
)


def _add_decision(predicate: str, value: str, quote: str):
    claim = ClaimDraft.from_parts(
        subject="Alice",
        predicate=predicate,
        value=value,
        scope_id="scope-a",
    )
    proposal = EvolutionProposal(
        action=EvolutionAction.ADD,
        raw_action="add",
        claim=claim,
        evidence_refs=(
            EvidenceReference(
                source_type="user_message",
                source_id="temporal-fixture",
                quote=quote,
                speaker_subject="Alice",
            ),
        ),
        confidence=0.99,
        reason="sixth audit temporal semantics",
    )
    return evaluate_evolution_policy(proposal)


@pytest.mark.parametrize(("predicate", "value", "quote"), _TEMPORAL_EVENT_CASES)
def test_past_future_and_temporary_events_cannot_authorize_current_fact(
    predicate: str,
    value: str,
    quote: str,
) -> None:
    decision = _add_decision(predicate, value, quote)

    assert decision.allowed is False
    assert decision.effective_action is EvolutionAction.REVIEW
    assert "authoritative_evidence_not_claim_supporting" in decision.reason_codes


@pytest.mark.parametrize(
    ("predicate", "value", "quote"),
    (
        ("lives in", "Paris", "Alice lives in Paris."),
        ("works at", "OldCo", "Alice works at OldCo."),
        ("works at", "OldCo", "Alice currently works at OldCo."),
        ("works at", "OldCo", "Alice continues working at OldCo."),
    ),
)
def test_explicit_current_state_remains_authoritative(
    predicate: str,
    value: str,
    quote: str,
) -> None:
    decision = _add_decision(predicate, value, quote)

    assert decision.allowed is True
    assert decision.effective_action is EvolutionAction.ADD


def test_transition_event_cannot_add_current_fact_but_can_retract() -> None:
    claim = ClaimDraft.from_parts(
        subject="Alice",
        predicate="works at",
        value="OldCo",
        scope_id="scope-a",
    )
    evidence = EvidenceReference(
        source_type="user_message",
        source_id="transition-retraction",
        quote="Alice stopped working at OldCo.",
        speaker_subject="Alice",
    )

    assert evidence_supports_claim(evidence, claim) is False
    assert evidence_supports_retraction(evidence, claim) is True


_DURABLE_STATE_MATRIX = (
    # residence
    ("lives in", "Paris", "Alice lives in Paris.", "current", True),
    ("lives in", "Paris", "In 2019, Alice lived in Paris.", "past", False),
    ("lives in", "Paris", "Next year, Alice will live in Paris.", "future", False),
    ("lives in", "Paris", "Alice is living in Paris for summer.", "temporary", False),
    ("lives in", "Paris", "Alice does not live in Paris.", "negated", False),
    ("lives in", "Paris", "If hired, Alice would live in Paris.", "conditional", False),
    # employment
    ("works at", "OldCo", "Alice works at OldCo.", "current", True),
    ("works at", "OldCo", "Alice worked at OldCo in 2019.", "past", False),
    ("works at", "OldCo", "Alice will work at OldCo next year.", "future", False),
    ("works at", "OldCo", "Alice is working at OldCo for a two-week contract.", "temporary", False),
    ("works at", "OldCo", "Alice does not work at OldCo.", "negated", False),
    ("works at", "OldCo", "If selected, Alice would work at OldCo.", "conditional", False),
    # contact
    ("contact channel is", "alpha-channel", "Alice contact channel is alpha-channel.", "current", True),
    ("contact channel is", "old-channel", "In 2019, Alice contact channel is old-channel.", "past", False),
    ("contact channel is", "next-channel", "Next year, Alice contact channel is next-channel.", "future", False),
    ("contact channel is", "temp-channel", "Temporarily, Alice contact channel is temp-channel.", "temporary", False),
    ("contact channel is", "alpha-channel", "Alice contact channel is not alpha-channel.", "negated", False),
    ("contact channel is", "alpha-channel", "If needed, Alice contact channel is alpha-channel.", "conditional", False),
    # preference
    ("prefers", "tea", "Alice prefers tea.", "current", True),
    ("prefers", "tea", "In 2019, Alice preferred tea.", "past", False),
    ("prefers", "tea", "Next year, Alice will prefer tea.", "future", False),
    ("prefers", "tea", "Alice temporarily prefers tea.", "temporary", False),
    ("prefers", "tea", "Alice does not prefer tea.", "negated", False),
    ("prefers", "tea", "If available, Alice would prefer tea.", "conditional", False),
)


@pytest.mark.parametrize(
    ("predicate", "value", "quote", "semantic_class", "expected_allowed"),
    _DURABLE_STATE_MATRIX,
)
def test_durable_state_authority_matrix(
    predicate: str,
    value: str,
    quote: str,
    semantic_class: str,
    expected_allowed: bool,
) -> None:
    decision = _add_decision(predicate, value, quote)

    assert decision.allowed is expected_allowed, semantic_class
    assert decision.effective_action is (
        EvolutionAction.ADD if expected_allowed else EvolutionAction.REVIEW
    )


def test_activation_capability_is_not_inherited_by_sibling_thread(
    tmp_path: Path,
) -> None:
    storage_dir = tmp_path / "scope-recall"
    storage_dir.mkdir()
    db_path = storage_dir / "memory.sqlite3"
    with sqlite3.connect(db_path) as seed:
        seed.execute("CREATE TABLE events(value TEXT NOT NULL)")
        seed.execute("INSERT INTO events(value) VALUES ('before')")

    snapshot = capture_activation_state(tmp_path, writer_quiesced=True)
    sibling_result: dict[str, object] = {}
    same_context_result: dict[str, object] = {}
    lease_token = str(dict(snapshot["maintenance_lease"])["token"])

    assert not hasattr(installer, "_ACTIVE_ACTIVATION_SNAPSHOT")
    assert not hasattr(installer, "_active_activation_lease_token")

    owner = sqlite3.connect(db_path)
    install_activation_lease_authorizer(
        owner,
        db_path,
        lease_token=lease_token,
    )
    owner.execute("INSERT INTO events(value) VALUES ('activation-owner')")
    owner.commit()
    owner.close()

    ordinary = sqlite3.connect(db_path)
    install_activation_lease_authorizer(ordinary, db_path)
    try:
        ordinary.execute("INSERT INTO events(value) VALUES ('same-context-ordinary')")
        ordinary.commit()
        same_context_result["committed"] = True
    except sqlite3.DatabaseError as exc:
        ordinary.rollback()
        same_context_result["committed"] = False
        same_context_result["error"] = str(exc)
    finally:
        ordinary.close()

    def sibling_write() -> None:
        sibling = sqlite3.connect(db_path, timeout=0.2)
        install_activation_lease_authorizer(sibling, db_path)
        try:
            sibling.execute("INSERT INTO events(value) VALUES ('sibling')")
            sibling.commit()
            sibling_result["committed"] = True
        except sqlite3.DatabaseError as exc:
            sibling.rollback()
            sibling_result["committed"] = False
            sibling_result["error"] = str(exc)
        finally:
            sibling.close()

    thread = threading.Thread(target=sibling_write, daemon=True)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    refresh_activation_sqlite_epoch(snapshot)

    receipt = committed_activation_receipt(
        snapshot,
        plugin_dir=tmp_path / "plugins" / "scope-recall",
        previous_plugin_existed=False,
        plugin_backup_path="",
        plugin_replaced=False,
    )
    assert receipt["status"] == "committed"
    assert same_context_result.get("committed") is False
    assert sibling_result.get("committed") is False
    with sqlite3.connect(db_path) as check:
        values = [str(row[0]) for row in check.execute("SELECT value FROM events")]
    assert values == ["before", "activation-owner"]


def test_merge_rolls_back_target_when_source_delete_fails(
    provider: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_id = _store(provider, "Target original content.")
    source_id = _store(provider, "Source content to merge.")

    def fail_source_delete(
        _provider: Any,
        _ids: list[str],
        **_kwargs: Any,
    ) -> int:
        raise RuntimeError("injected source delete failure")

    merge_memories_fn = provider._merge_memories.__func__.__globals__["merge_memories"]
    monkeypatch.setitem(
        merge_memories_fn.__globals__,
        "delete_memories_result",
        fail_source_delete,
    )
    with pytest.raises(RuntimeError, match="injected source delete failure"):
        provider._merge_memories(target_id, [source_id])

    with provider._lock:
        target_row = provider._require_conn().execute(
            "SELECT content FROM memories WHERE id = ?", (target_id,)
        ).fetchone()
        source_row = provider._require_conn().execute(
            "SELECT content FROM memories WHERE id = ?", (source_id,)
        ).fetchone()
    assert target_row is not None
    assert str(target_row["content"]) == "Target original content."
    assert source_row is not None
    assert str(source_row["content"]) == "Source content to merge."


def _sqlite_mutation_snapshot(provider: Any) -> dict[str, list[tuple[object, ...]]]:
    tables = (
        "memories",
        "memories_fts",
        "memory_entities",
        "memory_feedback",
        "memory_relations",
        "governance_audit_events",
        "vector_outbox",
    )
    snapshot: dict[str, list[tuple[object, ...]]] = {}
    with provider._lock:
        conn = provider._require_conn()
        for table in tables:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
                (table,),
            ).fetchone()
            if exists is None:
                snapshot[table] = []
                continue
            rows = conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
            snapshot[table] = [tuple(row) for row in rows]
    return snapshot


def _assert_failed_merge_preserves_snapshot(
    provider: Any,
    before: dict[str, list[tuple[object, ...]]],
) -> None:
    assert _sqlite_mutation_snapshot(provider) == before
    with provider._lock:
        assert provider._require_conn().in_transaction is False


def test_merge_rolls_back_when_relation_sync_fails(
    provider: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_id = _store(provider, "Target relation-fault original.")
    source_id = _store(provider, "Source relation-fault original.")
    # The shared fixture intentionally disables relation extraction. This test
    # exercises the enabled merge path, so opt in explicitly instead of relying
    # on an unknown config key being silently discarded by the old validator.
    monkeypatch.setitem(provider._config, "relation_extraction_enabled", True)
    before = _sqlite_mutation_snapshot(provider)
    merge_fn = provider._merge_memories.__func__.__globals__["merge_memories"]

    def fail_relation_sync(_conn: sqlite3.Connection, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("injected relation sync failure")

    monkeypatch.setitem(
        merge_fn.__globals__,
        "sync_extracted_relations_for_memory",
        fail_relation_sync,
    )
    with pytest.raises(RuntimeError, match="injected relation sync failure"):
        provider._merge_memories(target_id, [source_id])

    _assert_failed_merge_preserves_snapshot(provider, before)


def test_merge_rolls_back_when_source_relation_cleanup_fails(
    provider: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_id = _store(provider, "Target cleanup-fault original.")
    source_id = _store(provider, "Source cleanup-fault original.")
    monkeypatch.setitem(provider._config, "relation_extraction_enabled", False)
    with provider._lock:
        conn = provider._require_conn()
        conn.execute(
            "INSERT INTO memory_relations("
            "source_memory_id, target_memory_id, relation_type, confidence, note, created_at"
            ") VALUES (?, ?, 'supports', 0.8, 'fixture', '2026-07-16T00:00:00+00:00')",
            (source_id, target_id),
        )
        conn.commit()
    before = _sqlite_mutation_snapshot(provider)
    merge_fn = provider._merge_memories.__func__.__globals__["merge_memories"]
    delete_fn = merge_fn.__globals__["delete_memories"]
    hard_delete_fn = delete_fn.__globals__["hard_delete_memories"]
    real_delete_rows = hard_delete_fn.__globals__["delete_rows"]

    def fail_after_relation_cleanup(
        conn: sqlite3.Connection,
        ids: list[str],
        **kwargs: Any,
    ) -> int:
        real_delete_rows(conn, ids, **kwargs)
        raise RuntimeError("injected source relation cleanup failure")

    monkeypatch.setitem(
        hard_delete_fn.__globals__,
        "delete_rows",
        fail_after_relation_cleanup,
    )
    with pytest.raises(RuntimeError, match="injected source relation cleanup failure"):
        provider._merge_memories(target_id, [source_id])

    _assert_failed_merge_preserves_snapshot(provider, before)


def test_merge_rolls_back_when_vector_outbox_insert_fails(
    provider: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_id = _store(provider, "Target outbox-fault original.")
    source_id = _store(provider, "Source outbox-fault original.")
    monkeypatch.setitem(provider._config, "relation_extraction_enabled", False)
    _set_current_vector_generation(provider, "generation-fault-fixture")
    before = _sqlite_mutation_snapshot(provider)
    merge_fn = provider._merge_memories.__func__.__globals__["merge_memories"]

    def fail_outbox(*_args: Any, **_kwargs: Any) -> bool:
        raise RuntimeError("injected vector outbox failure")

    monkeypatch.setitem(
        merge_fn.__globals__,
        "enqueue_current_vector_event",
        fail_outbox,
    )
    with pytest.raises(RuntimeError, match="injected vector outbox failure"):
        provider._merge_memories(target_id, [source_id])

    _assert_failed_merge_preserves_snapshot(provider, before)


def test_update_rolls_back_when_vector_outbox_insert_fails(
    provider: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_id = _store(provider, "Update outbox original.")
    monkeypatch.setitem(provider._config, "relation_extraction_enabled", False)
    _set_current_vector_generation(provider, "generation-update-fault")
    before = _sqlite_mutation_snapshot(provider)
    update_fn = provider._update_memory.__func__.__globals__["update_memory"]

    def fail_outbox(*_args: Any, **_kwargs: Any) -> bool:
        raise RuntimeError("injected update outbox failure")

    monkeypatch.setitem(
        update_fn.__globals__,
        "enqueue_current_vector_event",
        fail_outbox,
    )
    updated, summary, updated_at = provider._update_memory(
        memory_id,
        "Update outbox must not commit.",
        "memory",
    )

    assert updated is False
    assert "injected update outbox failure" in summary
    assert updated_at == ""
    assert _sqlite_mutation_snapshot(provider) == before


def test_update_commits_durable_outbox_before_best_effort_replay(
    provider: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_id = _store(provider, "Update replay original.")
    monkeypatch.setitem(provider._config, "relation_extraction_enabled", False)
    _set_current_vector_generation(provider, "generation-update-replay")
    update_fn = provider._update_memory.__func__.__globals__["update_memory"]

    def fail_replay(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("injected post-commit replay failure")

    monkeypatch.setitem(update_fn.__globals__, "replay_vector_outbox", fail_replay)
    updated, _summary, updated_at = provider._update_memory(
        memory_id,
        "Update replay committed content.",
        "memory",
    )

    assert updated is True
    assert updated_at
    with provider._lock:
        conn = provider._require_conn()
        row = conn.execute(
            "SELECT content FROM memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        events = conn.execute(
            "SELECT operation, status FROM vector_outbox WHERE memory_id = ? ORDER BY created_at",
            (memory_id,),
        ).fetchall()
    assert str(row["content"]) == "Update replay committed content."
    assert [(str(item["operation"]), str(item["status"])) for item in events] == [
        ("upsert", "pending")
    ]


def _set_current_vector_generation(provider: Any, generation_id: str) -> None:
    with provider._lock:
        conn = provider._require_conn()
        ensure_vector_generation_schema(conn)
        conn.execute(
            "INSERT OR REPLACE INTO vector_generation_state(key, value, updated_at) "
            "VALUES (?, ?, '2026-07-16T00:00:00+00:00')",
            (CURRENT_GENERATION_KEY, generation_id),
        )
        conn.commit()
    provider._vector_generation_id = generation_id


def _clear_vector_outbox(provider: Any) -> None:
    with provider._lock:
        conn = provider._require_conn()
        conn.execute("DELETE FROM vector_outbox")
        conn.commit()


def test_update_uses_db_current_generation_when_runtime_generation_is_stale(
    provider: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_id = _store(provider, "Stale runtime update original.")
    monkeypatch.setitem(provider._config, "relation_extraction_enabled", False)
    _set_current_vector_generation(provider, "generation-db-update")
    provider._vector_generation_id = ""
    _clear_vector_outbox(provider)

    updated, _summary, updated_at = provider._update_memory(
        memory_id,
        "Stale runtime update committed.",
        "memory",
    )

    assert updated is True
    assert updated_at
    with provider._lock:
        rows = provider._require_conn().execute(
            "SELECT generation_id, memory_id, operation, status FROM vector_outbox "
            "WHERE memory_id = ?",
            (memory_id,),
        ).fetchall()
    assert [
        (
            str(row["generation_id"]),
            str(row["memory_id"]),
            str(row["operation"]),
            str(row["status"]),
        )
        for row in rows
    ] == [("generation-db-update", memory_id, "upsert", "pending")]


def test_merge_uses_db_current_generation_when_runtime_generation_is_stale(
    provider: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_id = _store(provider, "Stale runtime merge target.")
    source_id = _store(provider, "Stale runtime merge source.")
    monkeypatch.setitem(provider._config, "relation_extraction_enabled", False)
    _set_current_vector_generation(provider, "generation-db-merge")
    provider._vector_generation_id = ""
    _clear_vector_outbox(provider)

    result = provider._merge_memories(target_id, [source_id])

    assert result["merged"] is True
    with provider._lock:
        rows = provider._require_conn().execute(
            "SELECT generation_id, memory_id, operation, status FROM vector_outbox "
            "WHERE memory_id IN (?, ?) ORDER BY memory_id, operation",
            (target_id, source_id),
        ).fetchall()
    assert {
        (
            str(row["generation_id"]),
            str(row["memory_id"]),
            str(row["operation"]),
            str(row["status"]),
        )
        for row in rows
    } == {
        ("generation-db-merge", target_id, "upsert", "pending"),
        ("generation-db-merge", source_id, "delete", "pending"),
    }


def test_merge_replays_exact_target_and_source_events_after_commit(
    provider: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_id = _store(provider, "Exact replay merge target.")
    source_id = _store(provider, "Exact replay merge source.")
    monkeypatch.setitem(provider._config, "relation_extraction_enabled", False)
    _set_current_vector_generation(provider, "generation-exact-merge")
    _clear_vector_outbox(provider)
    observed: dict[str, Any] = {}
    merge_fn = provider._merge_memories.__func__.__globals__["merge_memories"]

    def classify_exact(runtime: Any, event_keys: list[str]) -> dict[str, Any]:
        conn = runtime._require_conn()
        assert conn.in_transaction is False
        placeholders = ",".join("?" for _ in event_keys)
        rows = conn.execute(
            f"SELECT id, event_key, memory_id, operation FROM vector_outbox "
            f"WHERE event_key IN ({placeholders}) ORDER BY id",
            event_keys,
        ).fetchall()
        observed["keys"] = list(event_keys)
        observed["events"] = [
            (str(row["memory_id"]), str(row["operation"])) for row in rows
        ]
        return {
            "event_keys": list(event_keys),
            "event_ids": [int(row["id"]) for row in rows],
            "status_counts": {"completed": len(rows)},
            "replay": {
                "claimed": len(rows),
                "completed": len(rows),
                "failed": 0,
            },
            "all_completed": True,
            "retryable_pending": 0,
            "dead_letter": 0,
            "missing": 0,
            "other_pending": 0,
        }

    monkeypatch.setitem(
        merge_fn.__globals__,
        "replay_and_classify_exact_vector_intents",
        classify_exact,
    )

    result = provider._merge_memories(target_id, [source_id])

    assert result["merged"] is True
    assert result["vector_pending"] is False
    assert len(observed["keys"]) == 2
    assert set(observed["events"]) == {
        (target_id, "upsert"),
        (source_id, "delete"),
    }


class _FailingVectorStore:
    def upsert_records(self, _records: list[dict[str, Any]]) -> None:
        raise RuntimeError("injected store vector upsert failure")

    def delete_by_ids(self, _ids: list[str]) -> None:
        raise RuntimeError("injected store vector delete failure")

    def audit_counts(self) -> dict[str, int]:
        return {
            "physical_rows": 0,
            "unique_ids": 0,
            "duplicate_rows": 0,
            "duplicate_ids": 0,
        }

    def close(self) -> None:
        return


class _FixtureEmbedder:
    def embed(self, _text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


def test_store_commits_truth_with_low_level_durable_outbox_intent(
    provider: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_current_vector_generation(provider, "generation-store-replay")
    provider._vector_ready = True
    provider._vector_store = _FailingVectorStore()
    provider._embedder = _FixtureEmbedder()
    store_service = provider._store_now.__func__.__globals__["store_memory_now"]
    capture_store = store_service.__globals__["store_now"]
    replay_fn = capture_store.__globals__["replay_vector_outbox"]
    monkeypatch.setitem(
        replay_fn.__globals__,
        "enqueue_vector_repair_event",
        lambda *_args, **_kwargs: False,
    )

    memory_id, inserted, outcome = provider._store_now(
        content="Store outbox probe content long enough for deterministic capture.",
        source="tool-store",
        target="memory",
        session_id=provider._session_id,
        allow_duplicate=True,
    )

    assert inserted is True
    assert outcome.startswith("stored")
    with provider._lock:
        conn = provider._require_conn()
        memory_count = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()[0]
        events = conn.execute(
            "SELECT operation, status FROM vector_outbox WHERE memory_id = ?",
            (memory_id,),
        ).fetchall()
    assert int(memory_count) == 1
    assert [(str(row["operation"]), str(row["status"])) for row in events] == [
        ("upsert", "retry")
    ]


def test_store_rolls_back_truth_when_low_level_outbox_insert_fails(
    provider: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_current_vector_generation(provider, "generation-store-fault")
    before = _sqlite_mutation_snapshot(provider)

    def fail_low_level_outbox(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("injected low-level vector outbox failure")

    store_service = provider._store_now.__func__.__globals__["store_memory_now"]
    capture_store = store_service.__globals__["store_now"]
    store_row_fn = capture_store.__globals__["store_row"]
    monkeypatch.setitem(
        store_row_fn.__globals__,
        "enqueue_current_vector_event",
        fail_low_level_outbox,
    )
    with pytest.raises(RuntimeError, match="injected low-level vector outbox failure"):
        provider._store_now(
            content="Store must rollback when durable vector intent cannot persist.",
            source="tool-store",
            target="memory",
            session_id=provider._session_id,
            allow_duplicate=True,
        )

    assert _sqlite_mutation_snapshot(provider) == before


def test_n_minus_one_chat_isolation_config_remains_supported(tmp_path: Path) -> None:
    source_root = Path(__file__).resolve().parents[1]
    storage_dir = tmp_path / "scope-recall"
    storage_dir.mkdir()
    (storage_dir / "config.json").write_text(
        json.dumps({"memory_isolated_chat_ids": ["isolated-chat-fixture"]}) + "\n",
        encoding="utf-8",
    )

    config = load_runtime_config(source_root, storage_dir)

    assert load_runtime_config_errors(config) == []
    assert config["memory_isolated_chat_ids"] == ["isolated-chat-fixture"]
