"""Journal digest integration with shared fact evolution execution."""

from __future__ import annotations

from dataclasses import replace
import sqlite3

import pytest

import scope_recall.journal as journal_module
from scope_recall.fact_actions import (
    ClaimDraft,
    EvidenceReference,
    EvolutionAction,
    EvolutionProposal,
)
from scope_recall.fact_evolution import pipeline_idempotency_key
from scope_recall.fact_repository import insert_claim
from scope_recall.journal import (
    JournalDigestCandidate,
    append_journal_entry,
    apply_journal_candidates,
    ensure_journal_schema,
)
from scope_recall.models import RuntimeScope
from scope_recall.scope import build_scope_id, build_shared_scope_id
from scope_recall.sql_store import ensure_schema, store_row


AT = "2026-04-01T00:00:00+00:00"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_schema(conn)
    ensure_journal_schema(conn)
    conn.commit()
    return conn


def _scope() -> RuntimeScope:
    return RuntimeScope(
        platform="telegram",
        user_id="fixture-user",
        chat_id="dm",
        thread_id="",
        gateway_session_key="",
        agent_identity="default",
        agent_workspace="hermes",
        agent_context="primary",
    )


def _entry(conn: sqlite3.Connection, scope: RuntimeScope, *, content: str) -> int:
    return append_journal_entry(
        conn,
        scope=scope,
        scope_id=build_scope_id(scope),
        shared_scope_id=build_shared_scope_id(scope),
        session_id="session-fact",
        turn_number=1,
        role="user",
        content=content,
    )


def _proposal(
    scope_id: str,
    action: EvolutionAction,
    *,
    entry_id: int,
    target_ids: tuple[str, ...] = (),
) -> EvolutionProposal:
    claim = None
    if action in {EvolutionAction.ADD, EvolutionAction.ENRICH, EvolutionAction.SUPERSEDE}:
        claim = ClaimDraft.from_parts(
            subject="Asha",
            predicate="lives in",
            value="Bangalore",
            scope_id=scope_id,
            valid_from="2026-03-01T00:00:00+00:00",
        )
    return EvolutionProposal(
        action=action,
        raw_action=action.value,
        claim=claim,
        target_ids=target_ids,
        evidence_refs=(
            EvidenceReference(
                source_type="user_message",
                source_id=f"journal-message:{entry_id}",
                quote="I live in Bangalore; please correct the old city.",
                speaker_subject="Asha",
            ),
        ),
        confidence=0.96,
        reason="direct user correction",
        source="journal_digest",
    )


def _candidate(
    scope_id: str,
    entry_id: int,
    action: EvolutionAction = EvolutionAction.ADD,
    *,
    target_ids: tuple[str, ...] = (),
    target: str = "user",
) -> JournalDigestCandidate:
    return JournalDigestCandidate(
        content=(
            "Asha currently lives in Bangalore; this is a stable user fact that "
            "should be remembered for future conversations."
        ),
        target=target,
        memory_type="factual",
        importance=0.9,
        confidence=0.96,
        reason="direct user correction",
        entry_ids=[entry_id],
        session_ids=["session-fact"],
        evolution=_proposal(
            scope_id,
            action,
            entry_id=entry_id,
            target_ids=target_ids,
        ),
    )


def _employment_candidate(
    scope_id: str,
    entry_id: int,
) -> JournalDigestCandidate:
    return JournalDigestCandidate(
        content=(
            "Asha currently works at OldCo; this is a stable user fact that "
            "should be remembered for future conversations."
        ),
        target="user",
        memory_type="factual",
        importance=0.9,
        confidence=0.96,
        reason="direct user statement",
        entry_ids=[entry_id],
        session_ids=["session-fact"],
        evolution=EvolutionProposal(
            action=EvolutionAction.ADD,
            raw_action="add",
            claim=ClaimDraft.from_parts(
                subject="Asha",
                predicate="works at",
                value="OldCo",
                scope_id=scope_id,
                valid_from="2026-03-01T00:00:00+00:00",
            ),
            evidence_refs=(
                EvidenceReference(
                    source_type="user_message",
                    source_id=f"journal-message:{entry_id}",
                    quote="I work at OldCo.",
                    speaker_subject="Asha",
                ),
            ),
            confidence=0.96,
            reason="direct user statement",
            source="journal_digest",
        ),
    )


def _enabled(mode: str = "auto_apply") -> dict[str, object]:
    return {
        "fact_evolution": {
            "enabled": True,
            "journal_mode": mode,
        }
    }


def test_journal_fact_add_uses_executor_and_links_source_entry_provenance():
    conn = _conn()
    scope = _scope()
    scope_id = build_scope_id(scope)
    entry_id = _entry(conn, scope, content="I live in Bangalore; please correct Mumbai.")

    result = apply_journal_candidates(
        conn,
        None,
        scope,
        run_id="journal-add",
        candidates=[_candidate(scope_id, entry_id)],
        runtime_config=_enabled(),
    )

    assert result["counts"] == {"inserted": 1}
    assert result["processed_entry_ids"] == [entry_id]
    assert result["actions"][0]["action"] == "evolve"
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0] == 1
    evidence = conn.execute(
        "SELECT source_type, source_ref FROM fact_claim_evidence ORDER BY source_type, source_ref"
    ).fetchall()
    assert {(row["source_type"], row["source_ref"]) for row in evidence} == {
        ("journal_entry", str(entry_id)),
        ("user_message", f"journal-message:{entry_id}"),
    }
    assert conn.execute("SELECT COUNT(*) FROM fact_action_receipts").fetchone()[0] == 1


def test_journal_gate_off_keeps_legacy_memory_and_consumes_only_after_store():
    conn = _conn()
    scope = _scope()
    entry_id = _entry(conn, scope, content="I live in Bangalore.")

    result = apply_journal_candidates(
        conn,
        None,
        scope,
        run_id="journal-gate-off",
        candidates=[_candidate(build_scope_id(scope), entry_id)],
        runtime_config={},
    )

    assert result["counts"] == {"inserted": 1}
    assert result["processed_entry_ids"] == [entry_id]
    assert result["actions"][0]["action"] == "insert"
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    metadata = str(conn.execute("SELECT metadata FROM memories").fetchone()[0])
    assert '"fact_evolution"' not in metadata
    assert "user_message" not in metadata
    assert conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fact_action_receipts").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM journal_rejections WHERE journal_entry_id = ?",
        (entry_id,),
    ).fetchone()[0] == 0


def test_journal_gate_off_general_routes_local_and_ignores_shared_match():
    conn = _conn()
    scope = _scope()
    local_scope_id = build_scope_id(scope)
    shared_scope_id = build_shared_scope_id(scope)
    entry_id = _entry(conn, scope, content="Remember this local scratch note.")
    candidate = _candidate(
        local_scope_id,
        entry_id,
        target="general",
    )
    store_row(
        conn,
        memory_id="shared-general-existing",
        scope_id=shared_scope_id,
        platform=scope.platform,
        user_id=scope.user_id,
        chat_id=scope.chat_id,
        thread_id=scope.thread_id,
        gateway_session_key=scope.gateway_session_key,
        agent_identity=scope.agent_identity,
        agent_workspace=scope.agent_workspace,
        session_id="seed",
        source="journal-digest",
        target="general",
        content=candidate.content,
        metadata="{}",
    )
    conn.commit()

    result = apply_journal_candidates(
        conn,
        None,
        scope,
        run_id="journal-gate-off-general",
        candidates=[candidate],
        runtime_config={},
    )

    assert result["counts"] == {"inserted": 1}
    assert result["processed_entry_ids"] == [entry_id]
    rows = conn.execute(
        "SELECT scope_id, target FROM memories ORDER BY scope_id"
    ).fetchall()
    assert {(row["scope_id"], row["target"]) for row in rows} == {
        (local_scope_id, "general"),
        (shared_scope_id, "general"),
    }


def test_journal_explicit_preview_keeps_source_pending_without_rejection():
    conn = _conn()
    scope = _scope()
    entry_id = _entry(conn, scope, content="I live in Bangalore.")

    result = apply_journal_candidates(
        conn,
        None,
        scope,
        run_id="journal-preview",
        candidates=[_candidate(build_scope_id(scope), entry_id)],
        runtime_config=_enabled("preview"),
    )

    assert result["counts"] == {"previewed": 1}
    assert result["processed_entry_ids"] == []
    assert result["actions"][0]["action"] == "preview"
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM journal_rejections WHERE journal_entry_id = ?",
        (entry_id,),
    ).fetchone()[0] == 0


def test_journal_high_risk_auto_apply_becomes_review_without_text_merge():
    conn = _conn()
    scope = _scope()
    scope_id = build_scope_id(scope)
    entry_id = _entry(conn, scope, content="I live in Bangalore; correct Mumbai.")
    store_row(
        conn,
        memory_id="memory-old",
        scope_id=scope_id,
        platform=scope.platform,
        user_id=scope.user_id,
        chat_id=scope.chat_id,
        thread_id="",
        gateway_session_key="",
        agent_identity=scope.agent_identity,
        agent_workspace=scope.agent_workspace,
        session_id="old",
        source="manual",
        target="user",
        content="Asha lives in Mumbai.",
        timestamp=AT,
    )
    insert_claim(
        conn,
        claim_id="claim-old",
        memory_id="memory-old",
        scope_id=scope_id,
        subject="Asha",
        predicate="lives in",
        value="Mumbai",
        cardinality="single",
        assertion_kind="direct",
        valid_from="2026-01-01T00:00:00+00:00",
        recorded_at=AT,
        confidence=0.9,
        source_type="user_message",
        source_ref="old-message",
    )
    conn.commit()

    result = apply_journal_candidates(
        conn,
        None,
        scope,
        run_id="journal-review",
        candidates=[
            _candidate(
                scope_id,
                entry_id,
                EvolutionAction.SUPERSEDE,
                target_ids=("memory-old",),
            )
        ],
        runtime_config=_enabled("auto_apply"),
    )

    assert result["counts"] == {"review": 1}
    assert result["processed_entry_ids"] == []
    assert result["actions"][0]["action"] == "review"
    assert conn.execute("SELECT content FROM memories WHERE id = 'memory-old'").fetchone()[0] == "Asha lives in Mumbai."
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM fact_claims WHERE status = 'current'").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM journal_rejections WHERE journal_entry_id = ?",
        (entry_id,),
    ).fetchone()[0] == 0


def test_journal_executor_failure_does_not_mark_source_entry_processed(monkeypatch):
    conn = _conn()
    scope = _scope()
    entry_id = _entry(conn, scope, content="I live in Bangalore.")

    def fail(*args, **kwargs):  # noqa: ARG001
        raise RuntimeError("injected fact executor failure")

    monkeypatch.setattr(journal_module, "execute_pipeline_proposal", fail, raising=False)
    with pytest.raises(RuntimeError, match="injected fact executor failure"):
        apply_journal_candidates(
            conn,
            None,
            scope,
            run_id="journal-failure",
            candidates=[_candidate(build_scope_id(scope), entry_id)],
            runtime_config=_enabled(),
        )

    row = conn.execute(
        "SELECT processed_run_id FROM journal_entries WHERE id = ?",
        (entry_id,),
    ).fetchone()
    assert row["processed_run_id"] == ""
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fact_action_receipts").fetchone()[0] == 0


def test_pipeline_idempotency_identity_is_independent_of_scheduler_run_id():
    first = pipeline_idempotency_key(
        lane="journal",
        run_id="scheduler-run-one",
        source_key="entries:10,11:candidate-digest",
        scope_id="scope-shared",
    )
    second = pipeline_idempotency_key(
        lane="journal",
        run_id="scheduler-run-two",
        source_key="entries:10,11:candidate-digest",
        scope_id="scope-shared",
    )

    assert first == second


def test_journal_partial_batch_failure_never_leaves_committed_fact_pending(
    monkeypatch,
):
    conn = _conn()
    scope = _scope()
    first_entry = _entry(conn, scope, content="I live in Bangalore.")
    second_entry = append_journal_entry(
        conn,
        scope=scope,
        scope_id=build_scope_id(scope),
        shared_scope_id=build_shared_scope_id(scope),
        session_id="session-fact",
        turn_number=2,
        role="user",
        content="I confirm that I live in Bangalore.",
    )
    original = journal_module.execute_pipeline_proposal
    calls = 0

    def fail_after_first(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected crash after first fact action")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        journal_module,
        "execute_pipeline_proposal",
        fail_after_first,
        raising=False,
    )
    with pytest.raises(RuntimeError, match="injected crash after first fact action"):
        apply_journal_candidates(
            conn,
            None,
            scope,
            run_id="journal-partial-crash",
            candidates=[
                _candidate(build_scope_id(scope), first_entry),
                _candidate(build_scope_id(scope), second_entry),
            ],
            runtime_config=_enabled(),
        )

    memory_count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    receipt_count = conn.execute(
        "SELECT COUNT(*) FROM fact_action_receipts"
    ).fetchone()[0]
    processed = {
        row["id"]: row["processed_run_id"]
        for row in conn.execute(
            "SELECT id, processed_run_id FROM journal_entries WHERE id IN (?, ?)",
            (first_entry, second_entry),
        )
    }
    all_rolled_back = (
        memory_count == 0
        and receipt_count == 0
        and processed[first_entry] == ""
        and processed[second_entry] == ""
    )
    first_atomically_checkpointed = (
        memory_count == 1
        and receipt_count == 1
        and processed[first_entry] == "journal-partial-crash"
        and processed[second_entry] == ""
    )
    assert all_rolled_back or first_atomically_checkpointed


def test_journal_same_entry_fact_closure_rolls_back_if_later_candidate_crashes(
    monkeypatch,
):
    conn = _conn()
    scope = _scope()
    scope_id = build_scope_id(scope)
    entry_id = _entry(
        conn,
        scope,
        content="I live in Bangalore and I work at OldCo.",
    )
    residence = _candidate(scope_id, entry_id)
    employment = _employment_candidate(scope_id, entry_id)
    original = journal_module.execute_pipeline_proposal
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected same-entry second candidate crash")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        journal_module,
        "execute_pipeline_proposal",
        fail_second,
        raising=False,
    )

    with pytest.raises(RuntimeError, match="same-entry second candidate crash"):
        apply_journal_candidates(
            conn,
            None,
            scope,
            run_id="shared-entry-crash",
            candidates=[residence, employment],
            runtime_config=_enabled(),
        )

    row = conn.execute(
        "SELECT processed_run_id FROM journal_entries WHERE id = ?", (entry_id,)
    ).fetchone()
    assert row["processed_run_id"] == ""
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fact_action_receipts").fetchone()[0] == 0


def test_journal_overlapping_entry_sets_form_one_atomic_fact_closure(monkeypatch):
    conn = _conn()
    scope = _scope()
    scope_id = build_scope_id(scope)
    first_entry = _entry(conn, scope, content="I live in Bangalore.")
    bridge_entry = _entry(conn, scope, content="This confirms both stable facts.")
    last_entry = _entry(conn, scope, content="I work at OldCo.")
    residence = replace(
        _candidate(scope_id, first_entry),
        entry_ids=[first_entry, bridge_entry],
    )
    employment = replace(
        _employment_candidate(scope_id, last_entry),
        entry_ids=[bridge_entry, last_entry],
    )
    original = journal_module.execute_pipeline_proposal
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected overlapping-entry closure crash")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        journal_module,
        "execute_pipeline_proposal",
        fail_second,
        raising=False,
    )

    with pytest.raises(RuntimeError, match="overlapping-entry closure crash"):
        apply_journal_candidates(
            conn,
            None,
            scope,
            run_id="overlapping-entry-crash",
            candidates=[residence, employment],
            runtime_config=_enabled(),
        )

    processed = [
        str(row[0] or "")
        for row in conn.execute(
            "SELECT processed_run_id FROM journal_entries ORDER BY id"
        )
    ]
    assert processed == ["", "", ""]
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fact_action_receipts").fetchone()[0] == 0


def test_journal_same_entry_fact_and_quarantine_outcome_roll_back_together(
    monkeypatch,
):
    conn = _conn()
    scope = _scope()
    scope_id = build_scope_id(scope)
    entry_id = _entry(
        conn,
        scope,
        content="I live in Bangalore and I work at OldCo.",
    )
    residence = _candidate(scope_id, entry_id)
    polluted = replace(
        _employment_candidate(scope_id, entry_id),
        content=(
            "Scope Recall public repo handoff: HEAD=6a1fbf0, pushed commits were "
            "verified, closed issues #24 and #23, and 678 tests passed."
        ),
    )
    assessments = journal_module.assess_digest_batch([residence, polluted])
    assert assessments[0].quarantined is False
    assert assessments[1].quarantined is True

    def fail_rejection(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("injected quarantine receipt failure")

    monkeypatch.setattr(journal_module, "_record_journal_rejection", fail_rejection)
    with pytest.raises(RuntimeError, match="quarantine receipt"):
        apply_journal_candidates(
            conn,
            None,
            scope,
            run_id="fact-quarantine-closure",
            candidates=[residence, polluted],
            runtime_config=_enabled(),
        )

    entry = conn.execute(
        "SELECT processed_run_id FROM journal_entries WHERE id = ?",
        (entry_id,),
    ).fetchone()
    assert str(entry["processed_run_id"] or "") == ""
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fact_action_receipts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM journal_rejections").fetchone()[0] == 0


def test_journal_same_entry_fact_and_quarantine_commit_as_one_terminal_closure():
    conn = _conn()
    scope = _scope()
    scope_id = build_scope_id(scope)
    entry_id = _entry(
        conn,
        scope,
        content="I live in Bangalore and I work at OldCo.",
    )
    residence = _candidate(scope_id, entry_id)
    polluted = replace(
        _employment_candidate(scope_id, entry_id),
        content=(
            "Scope Recall public repo handoff: HEAD=6a1fbf0, pushed commits were "
            "verified, closed issues #24 and #23, and 678 tests passed."
        ),
    )

    result = apply_journal_candidates(
        conn,
        None,
        scope,
        run_id="fact-quarantine-terminal",
        candidates=[residence, polluted],
        runtime_config=_enabled(),
    )

    assert result["counts"]["inserted"] == 1
    assert result["counts"]["quarantined"] == 1
    assert result["processed_entry_ids"] == [entry_id]
    entry = conn.execute(
        "SELECT processed_run_id FROM journal_entries WHERE id = ?",
        (entry_id,),
    ).fetchone()
    assert entry["processed_run_id"] == "fact-quarantine-terminal"
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM fact_action_receipts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM journal_rejections").fetchone()[0] == 1


def _legacy_workflow_candidate(entry_id: int) -> JournalDigestCandidate:
    return JournalDigestCandidate(
        content=(
            "Deployment rollback workflow: validate the backup, run the release "
            "gate, and verify recovery before restoring traffic."
        ),
        target="project",
        session_ids=["mixed-entry-session"],
        entry_ids=[entry_id],
        evolution=None,
    )


def test_journal_same_entry_fact_and_legacy_candidate_roll_back_together(
    monkeypatch,
):
    conn = _conn()
    scope = _scope()
    scope_id = build_scope_id(scope)
    entry_id = _entry(
        conn,
        scope,
        content=(
            "Asha lives in Bangalore. Deployment rollback workflow: validate "
            "the backup, run the release gate, and verify recovery."
        ),
    )
    fact_candidate = _candidate(scope_id, entry_id)
    legacy_candidate = _legacy_workflow_candidate(entry_id)
    original_store_row = journal_module.store_row

    def fail_legacy_store(*args, **kwargs):
        if kwargs.get("target") == "project":
            raise RuntimeError("injected mixed-entry legacy candidate crash")
        return original_store_row(*args, **kwargs)

    monkeypatch.setattr(journal_module, "store_row", fail_legacy_store)
    with pytest.raises(
        RuntimeError,
        match="injected mixed-entry legacy candidate crash",
    ):
        apply_journal_candidates(
            conn,
            None,
            scope,
            run_id="mixed-entry-crash",
            candidates=[fact_candidate, legacy_candidate],
            runtime_config=_enabled("auto_apply"),
        )

    assert conn.execute(
        "SELECT processed_run_id FROM journal_entries WHERE id = ?",
        (entry_id,),
    ).fetchone()[0] == ""
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fact_action_receipts").fetchone()[0] == 0


def test_journal_same_entry_fact_and_legacy_candidate_commit_as_one_closure():
    conn = _conn()
    scope = _scope()
    scope_id = build_scope_id(scope)
    entry_id = _entry(
        conn,
        scope,
        content=(
            "Asha lives in Bangalore. Deployment rollback workflow: validate "
            "the backup, run the release gate, and verify recovery."
        ),
    )
    result = apply_journal_candidates(
        conn,
        None,
        scope,
        run_id="mixed-entry-success",
        candidates=[
            _candidate(scope_id, entry_id),
            _legacy_workflow_candidate(entry_id),
        ],
        runtime_config=_enabled("auto_apply"),
    )

    assert result["counts"]["inserted"] == 2
    assert result["processed_entry_ids"] == [entry_id]
    assert conn.execute(
        "SELECT processed_run_id FROM journal_entries WHERE id = ?",
        (entry_id,),
    ).fetchone()[0] == "mixed-entry-success"
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM fact_action_receipts").fetchone()[0] == 1


def test_journal_same_entry_fact_closure_commits_all_and_replays_idempotently():
    conn = _conn()
    scope = _scope()
    scope_id = build_scope_id(scope)
    entry_id = _entry(
        conn,
        scope,
        content="I live in Bangalore and I work at OldCo.",
    )
    candidates = [
        _candidate(scope_id, entry_id),
        _employment_candidate(scope_id, entry_id),
    ]

    first = apply_journal_candidates(
        conn,
        None,
        scope,
        run_id="shared-entry-success-one",
        candidates=candidates,
        runtime_config=_enabled(),
    )
    second = apply_journal_candidates(
        conn,
        None,
        scope,
        run_id="shared-entry-success-two",
        candidates=candidates,
        runtime_config=_enabled(),
    )

    assert first["counts"] == {"inserted": 2}
    assert first["processed_entry_ids"] == [entry_id]
    assert second["counts"] == {"replayed": 2}
    assert second["processed_entry_ids"] == [entry_id]
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM fact_action_receipts").fetchone()[0] == 2
    assert conn.execute(
        "SELECT processed_run_id FROM journal_entries WHERE id = ?", (entry_id,)
    ).fetchone()[0] == "shared-entry-success-two"


def test_journal_same_source_replays_across_new_scheduler_run_id():
    conn = _conn()
    scope = _scope()
    entry_id = _entry(conn, scope, content="I live in Bangalore.")
    candidate = _candidate(build_scope_id(scope), entry_id)

    first = apply_journal_candidates(
        conn,
        None,
        scope,
        run_id="journal-run-one",
        candidates=[candidate],
        runtime_config=_enabled(),
    )
    second = apply_journal_candidates(
        conn,
        None,
        scope,
        run_id="journal-run-two",
        candidates=[candidate],
        runtime_config=_enabled(),
    )

    assert first["counts"] == {"inserted": 1}
    assert second["counts"] == {"replayed": 1}
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM fact_action_receipts").fetchone()[0] == 1


def test_journal_non_fact_workflow_keeps_legacy_storage_path():
    conn = _conn()
    scope = _scope()
    entry_id = _entry(conn, scope, content="Document the reusable rollback workflow.")
    candidate = JournalDigestCandidate(
        content=(
            "Reusable rollback workflow: validate the backup, run the release gate, "
            "and verify rollback evidence before cleanup."
        ),
        target="ops",
        memory_type="workflow",
        importance=0.85,
        confidence=0.9,
        reason="reusable verified workflow",
        entry_ids=[entry_id],
        session_ids=["session-fact"],
        evolution=_proposal(build_scope_id(scope), EvolutionAction.ADD, entry_id=entry_id),
    )

    result = apply_journal_candidates(
        conn,
        None,
        scope,
        run_id="journal-workflow",
        candidates=[candidate],
        runtime_config=_enabled(),
    )

    assert result["counts"]["inserted"] == 1
    assert result["actions"][0]["action"] == "insert"
    assert result["processed_entry_ids"] == [entry_id]
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0] == 0
