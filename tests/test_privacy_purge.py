from __future__ import annotations

import hashlib
import json

import pytest

from plugins.memory import load_memory_provider
from scope_recall.candidate_extraction import ExtractedCandidate
from scope_recall.candidate_review import review_candidate
from scope_recall.candidate_store import store_event_candidates
from scope_recall.fact_repository import insert_claim, link_claim_evidence
from scope_recall.journal_store import load_unprocessed_journal_entries
from scope_recall.privacy_purge import replay_privacy_purge_receipts, run_privacy_purge
from scope_recall.sql_store import record_governance_audit_event
from scope_recall.windows_filesystem import atomic_write_text, read_text


@pytest.fixture
def provider(tmp_path):
    config_path = tmp_path / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "maintenance_tools_enabled": True,
                "purge": {"enabled": True},
                "vector": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "purge-session",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="private-a",
    )
    yield plugin
    plugin.shutdown()


def _tool(provider, name: str, args: dict) -> dict:
    return json.loads(provider.handle_tool_call(name, args))


def _store(provider, content: str) -> str:
    payload = _tool(
        provider,
        "scope_recall_store",
        {"content": content, "target": "memory"},
    )
    assert payload["stored"] is True
    return str(payload["id"])


def test_purge_plan_reports_zero_write_semantics(provider):
    memory_id = _store(provider, "The private value must be removed completely.")
    conn = provider._require_conn()
    before = conn.total_changes

    plan = _tool(provider, "scope_recall_purge", {"action": "plan", "id": memory_id})

    assert conn.total_changes == before
    assert plan["read_only"] is True
    assert plan["target_count"] == 1
    assert memory_id not in json.dumps(plan)
    assert plan["mode"] == "privacy_purge"
    assert plan["data_retained"] is True
    assert plan["reversible"] is False
    assert plan["privacy_purge"] is True
    assert plan["mutation_applied"] is False

    wrong = _tool(
        provider,
        "scope_recall_purge",
        {
            "action": "deny",
            "id": memory_id,
            "operation_id": plan["operation_id"],
            "confirmation": "DENY-wrong",
        },
    )
    assert "confirmation" in wrong["error"]
    assert conn.execute("SELECT COUNT(*) FROM privacy_purge_tombstones").fetchone()[0] == 0

    denied = _tool(
        provider,
        "scope_recall_purge",
        {
            "action": "deny",
            "id": memory_id,
            "operation_id": plan["operation_id"],
            "confirmation": plan["confirmation"],
        },
    )
    assert denied["status"] == "denied"
    assert denied["data_retained"] is True
    assert denied["reversible"] is False
    assert denied["privacy_purge"] is True
    assert denied["mutation_applied"] is True
    assert denied["erase_confirmation"].startswith("ERASE-")
    assert denied["erase_confirmation"] != plan["confirmation"]
    audit = conn.execute(
        "SELECT target_id, before_json, after_json FROM governance_audit_events "
        "WHERE event_type='privacy_purge' AND action='deny'"
    ).fetchone()
    audit_json = json.dumps(dict(audit))
    assert memory_id not in audit_json
    assert "The private value" not in audit_json


def test_purge_deny_reports_retained_but_irreversible(provider):
    memory_id = _store(provider, "Purge deny response contract fixture.")
    plan = _tool(provider, "scope_recall_purge", {"action": "plan", "id": memory_id})
    denied = _tool(
        provider,
        "scope_recall_purge",
        {
            "action": "deny",
            "id": memory_id,
            "operation_id": plan["operation_id"],
            "confirmation": plan["confirmation"],
        },
    )

    assert denied["status"] == "denied"
    assert denied["mode"] == "privacy_purge"
    assert denied["data_retained"] is True
    assert denied["reversible"] is False
    assert denied["privacy_purge"] is True
    assert denied["mutation_applied"] is True


def test_purge_phase_b_failure_keeps_deny_and_reports_current_semantics(
    provider, monkeypatch
):
    memory_id = _store(provider, "Phase B failure must never undo deny state.")
    plan = _tool(provider, "scope_recall_purge", {"action": "plan", "id": memory_id})
    denied = _tool(
        provider,
        "scope_recall_purge",
        {
            "action": "deny",
            "id": memory_id,
            "operation_id": plan["operation_id"],
            "confirmation": plan["confirmation"],
        },
    )
    import scope_recall.privacy_purge as purge_module

    monkeypatch.setattr(
        purge_module,
        "delete_rows",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("synthetic erase failure")),
    )
    with pytest.raises(RuntimeError, match="synthetic erase failure"):
        purge_module.erase_privacy_purge(
            provider,
            operation_id=plan["operation_id"],
            confirmation=denied["erase_confirmation"],
        )
    failed = run_privacy_purge(
        provider,
        action="erase",
        operation_id=plan["operation_id"],
        confirmation=denied["erase_confirmation"],
    )
    assert failed["ok"] is False
    assert failed["status"] == "denied"
    assert failed["data_retained"] is True
    assert failed["reversible"] is False
    assert failed["privacy_purge"] is True
    assert failed["mutation_applied"] is False
    conn = provider._require_conn()
    row = conn.execute(
        "SELECT metadata FROM memories WHERE id=?", (memory_id,)
    ).fetchone()
    assert json.loads(row["metadata"])["purge_denied"] is True
    assert conn.execute(
        "SELECT status FROM privacy_purge_operations WHERE operation_id=?",
        (plan["operation_id"],),
    ).fetchone()[0] == "denied"


def test_plan_confirmation_refuses_current_state_drift(provider):
    memory_id = _store(provider, "Original purge plan state.")
    plan = _tool(provider, "scope_recall_purge", {"action": "plan", "id": memory_id})
    updated = _tool(
        provider,
        "scope_recall_update",
        {"id": memory_id, "content": "Changed after the purge plan."},
    )
    assert updated["updated"] is True

    denied = _tool(
        provider,
        "scope_recall_purge",
        {
            "action": "deny",
            "id": memory_id,
            "operation_id": plan["operation_id"],
            "confirmation": plan["confirmation"],
        },
    )

    assert "confirmation" in denied["error"]
    assert provider._require_conn().execute(
        "SELECT COUNT(*) FROM privacy_purge_tombstones"
    ).fetchone()[0] == 0


def test_purge_erase_reports_not_retained(provider):
    memory_id = _store(provider, "Purge visibility fixture unique-771.")
    plan = _tool(provider, "scope_recall_purge", {"action": "plan", "id": memory_id})
    denied = _tool(
        provider,
        "scope_recall_purge",
        {
            "action": "deny",
            "id": memory_id,
            "operation_id": plan["operation_id"],
            "confirmation": plan["confirmation"],
        },
    )

    search = _tool(provider, "scope_recall_search", {"query": "unique-771"})
    export = _tool(provider, "scope_recall_export", {"format": "json"})
    assert all(item["id"] != memory_id for item in search["results"])
    assert all(item["id"] != memory_id for item in export["data"])
    conn = provider._require_conn()
    metadata = json.loads(
        conn.execute("SELECT metadata FROM memories WHERE id=?", (memory_id,)).fetchone()[0]
    )
    assert metadata["lifecycle"] == "archived"
    assert metadata["purge_denied"] is True

    erased = _tool(
        provider,
        "scope_recall_purge",
        {
            "action": "erase",
            "operation_id": plan["operation_id"],
            "confirmation": denied["erase_confirmation"],
        },
    )
    assert erased["status"] == "completed"
    assert erased["mode"] == "privacy_purge"
    assert erased["data_retained"] is False
    assert erased["reversible"] is False
    assert erased["privacy_purge"] is True
    assert erased["mutation_applied"] is True
    assert erased["companion_erasure_pending"] is False
    assert conn.execute("SELECT COUNT(*) FROM memories WHERE id=?", (memory_id,)).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM privacy_purge_tombstones WHERE operation_id=?",
        (plan["operation_id"],),
    ).fetchone()[0] == 1

    repeated = _tool(
        provider,
        "scope_recall_purge",
        {
            "action": "erase",
            "operation_id": plan["operation_id"],
            "confirmation": denied["erase_confirmation"],
        },
    )
    assert repeated["status"] == "completed"
    assert repeated["data_retained"] is False
    assert repeated["mutation_applied"] is False


def test_purge_redacts_event_candidate_governance_payload_copies(provider):
    marker = "PURGE-EVENT-CANDIDATE-PRIVATE-9f81"
    evidence_marker = "journal:PRIVATE-EVIDENCE-9f81"
    conn = provider._require_conn()
    stored = store_event_candidates(
        conn,
        candidates=[
            ExtractedCandidate(
                target="memory",
                content=f"Stable workflow detail {marker} requires review.",
                memory_type="workflow",
                confidence=0.92,
                evidence_refs=[evidence_marker],
                metadata={"private_marker": f"{marker}-METADATA"},
            )
        ],
        scope=provider._scope,
        scope_id=provider._scope_id,
        session_id=provider._session_id,
        dry_run=False,
    )
    memory_id = str(stored["ids"][0])
    reviewed = review_candidate(
        conn,
        memory_id=memory_id,
        action="promote",
        dry_run=False,
        actor="purge-regression",
    )
    assert reviewed["applied"] is True
    audit_before = conn.execute(
        "SELECT * FROM governance_audit_events "
        "WHERE event_type='event_candidate' AND target_id=?",
        (memory_id,),
    ).fetchone()
    assert audit_before is not None
    # New events are content-free at creation; emulate the 2.0 legacy shape
    # so the patch also proves old payload copies are erased during Purge.
    conn.execute(
        "UPDATE governance_audit_events SET after_json=? WHERE id=?",
        (
            json.dumps({"summary": marker, "evidence_refs": [evidence_marker]}),
            audit_before["id"],
        ),
    )
    record_governance_audit_event(
        conn,
        event_id="purge-nested-cursor-fixture",
        event_type="memory_auto_adjudication",
        action="candidate_scan_cursor",
        target_id="queue-safe-identity",
        after={"last_scanned_id": memory_id},
        reason="nested cursor purge fixture",
        actor="test",
        dry_run=False,
    )
    record_governance_audit_event(
        conn,
        event_id="purge-nested-export-fixture",
        event_type="external_memory_export",
        action="export",
        target_id="export-safe-identity",
        after={"record_ids": ["unrelated-memory", memory_id]},
        reason="nested export purge fixture",
        actor="test",
        dry_run=False,
    )
    conn.commit()

    plan = _tool(
        provider, "scope_recall_purge", {"action": "plan", "id": memory_id}
    )
    denied = _tool(
        provider,
        "scope_recall_purge",
        {
            "action": "deny",
            "id": memory_id,
            "operation_id": plan["operation_id"],
            "confirmation": plan["confirmation"],
        },
    )
    erased = _tool(
        provider,
        "scope_recall_purge",
        {
            "action": "erase",
            "operation_id": plan["operation_id"],
            "confirmation": denied["erase_confirmation"],
        },
    )

    assert erased["status"] == "completed"
    assert conn.execute(
        "SELECT COUNT(*) FROM memories WHERE id=?", (memory_id,)
    ).fetchone()[0] == 0
    audit_after = conn.execute(
        "SELECT target_id, before_json, after_json, reason "
        "FROM governance_audit_events WHERE id=?",
        (audit_before["id"],),
    ).fetchone()
    all_governance = conn.execute(
        "SELECT * FROM governance_audit_events ORDER BY id"
    ).fetchall()
    rendered = json.dumps(
        [dict(row) for row in all_governance], ensure_ascii=False
    )
    assert memory_id not in rendered
    assert marker not in rendered
    assert evidence_marker not in rendered
    assert json.loads(audit_after["after_json"]) == {"privacy_purged": True}
    for event_id in (
        "purge-nested-cursor-fixture",
        "purge-nested-export-fixture",
    ):
        nested = conn.execute(
            "SELECT target_id, before_json, after_json "
            "FROM governance_audit_events WHERE id=?",
            (event_id,),
        ).fetchone()
        assert nested["target_id"] in {
            "queue-safe-identity",
            "export-safe-identity",
        }
        assert json.loads(nested["before_json"]) == {"privacy_purged": True}
        assert json.loads(nested["after_json"]) == {"privacy_purged": True}


def test_purge_removes_claim_evidence_and_degrades_shared_journal_provenance(provider):
    purge_id = _store(provider, "Shared journal contains private removal target.")
    peer_id = _store(provider, "Peer memory has shared but non-authoritative provenance.")
    conn = provider._require_conn()
    purge_scope_id = str(
        conn.execute("SELECT scope_id FROM memories WHERE id=?", (purge_id,)).fetchone()[0]
    )
    now = "2026-08-27T00:00:00+00:00"
    content = "Private removal target and harmless peer context."
    conn.execute(
        """
        INSERT INTO journal_entries(
            scope_id, shared_scope_id, session_id, turn_number, role, content,
            content_hash, created_at, metadata
        ) VALUES (?, ?, 'journal-purge', 1, 'user', ?, ?, ?, '{}')
        """,
        (
            purge_scope_id,
            provider._shared_scope_id,
            content,
            hashlib.sha256(content.encode()).hexdigest(),
            now,
        ),
    )
    entry_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.executemany(
        "INSERT INTO memory_journal_sources(memory_id,journal_entry_id,run_id,created_at) "
        "VALUES(?,?,'run-purge',?)",
        [(purge_id, entry_id, now), (peer_id, entry_id, now)],
    )
    insert_claim(
        conn,
        claim_id="claim-purge",
        memory_id=purge_id,
        scope_id=purge_scope_id,
        subject="User",
        predicate="private value",
        value="target",
        source_type="message",
        source_ref=str(entry_id),
        recorded_at=now,
    )
    link_claim_evidence(
        conn,
        claim_id="claim-purge",
        source_type="message",
        source_ref=str(entry_id),
        excerpt="Private removal target",
        recorded_at=now,
    )
    conn.commit()

    plan = _tool(provider, "scope_recall_purge", {"action": "plan", "id": purge_id})
    denied = _tool(
        provider,
        "scope_recall_purge",
        {
            "action": "deny",
            "id": purge_id,
            "operation_id": plan["operation_id"],
            "confirmation": plan["confirmation"],
        },
    )
    assert load_unprocessed_journal_entries(
        conn, scope_ids=[purge_scope_id], limit=10
    ) == []

    erased = _tool(
        provider,
        "scope_recall_purge",
        {
            "action": "erase",
            "operation_id": plan["operation_id"],
            "confirmation": denied["erase_confirmation"],
        },
    )
    assert erased["status"] == "completed"
    assert conn.execute("SELECT COUNT(*) FROM fact_claims WHERE claim_id='claim-purge'").fetchone()[0] == 0
    journal = conn.execute(
        "SELECT content, content_hash, metadata FROM journal_entries WHERE id=?", (entry_id,)
    ).fetchone()
    assert journal["content"] == ""
    assert journal["content_hash"]
    assert json.loads(journal["metadata"])["privacy_purge_redaction"] == "body_removed"
    peer_metadata = json.loads(
        conn.execute("SELECT metadata FROM memories WHERE id=?", (peer_id,)).fetchone()[0]
    )
    assert peer_metadata["provenance_degraded"] is True


def test_restore_replay_reinstates_deny_before_writer_use(provider):
    memory_id = _store(provider, "Backup replay must not resurrect this target.")
    conn = provider._require_conn()
    row = conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
    plan = _tool(provider, "scope_recall_purge", {"action": "plan", "id": memory_id})
    denied = _tool(
        provider,
        "scope_recall_purge",
        {
            "action": "deny",
            "id": memory_id,
            "operation_id": plan["operation_id"],
            "confirmation": plan["confirmation"],
        },
    )
    assert denied["receipt"]["receipt_state"] == "mirrored"
    receipt_dir = provider._db_path.parent / "receipts"
    receipt_text = read_text(
        next(receipt_dir.glob("operator.deny.*.json")), encoding="utf-8"
    )
    assert memory_id not in receipt_text
    assert "Backup replay must not resurrect" not in receipt_text
    tampered = json.loads(receipt_text)
    tampered["result"]["targets"][0]["content_hash"] = "0" * 64
    atomic_write_text(
        receipt_dir / "operator.deny.forged.deny.json",
        json.dumps(tampered),
        encoding="utf-8",
    )

    restored = conn.__class__(":memory:")
    restored.row_factory = conn.row_factory
    from scope_recall.sql_store import ensure_schema

    ensure_schema(restored)
    columns = [item[1] for item in restored.execute("PRAGMA table_info(memories)")]
    values = [row[column] for column in columns]
    restored.execute(
        f"INSERT INTO memories({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
        values,
    )
    restored.commit()

    replay = replay_privacy_purge_receipts(restored, receipt_dir=receipt_dir)

    assert replay["targets_denied"] == 1
    assert replay["receipt_count"] == 1
    assert replay["invalid_receipt_count"] == 1
    replayed = json.loads(
        restored.execute("SELECT metadata FROM memories WHERE id=?", (memory_id,)).fetchone()[0]
    )
    assert replayed["purge_denied"] is True
    assert replayed["lifecycle"] == "archived"
