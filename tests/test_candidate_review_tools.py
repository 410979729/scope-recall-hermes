"""Online candidate review keeps the gateway writer, scope, and lifecycle authority."""

import json

import pytest

from plugins.memory import load_memory_provider


@pytest.fixture
def provider(tmp_path):
    config = tmp_path / "scope-recall" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"vector": {"enabled": False},
                                  "relation_extraction_enabled": False}), encoding="utf-8")
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize("online-review", hermes_home=str(tmp_path), platform="cli",
                      user_id="review-user", chat_id="review-chat", agent_context="primary",
                      agent_identity="reviewer", agent_workspace="hermes")
    yield plugin
    plugin.shutdown()


def _candidate(provider):
    memory_id, inserted, _ = provider._store_now(
        content="The service uses blue green deployment with health validation.",
        source="event-digest", target="memory", session_id="online-review",
        metadata={"lifecycle": "candidate", "event_digest": True,
                  "candidate_status": "needs_review"},
    )
    assert inserted
    return memory_id


def _call(provider, **args):
    return json.loads(provider.handle_tool_call("scope_recall_memory", args))


@pytest.mark.parametrize("action,lifecycle", [("promote", "promoted"), ("archive", "archived")])
def test_candidate_review_plans_then_applies_inside_existing_writer(provider, action, lifecycle):
    memory_id = _candidate(provider)
    conn = provider._require_conn()
    writer = provider._writer_thread
    before = conn.execute("SELECT metadata FROM memories WHERE id=?", (memory_id,)).fetchone()[0]
    planned = _call(provider, action=action, id=memory_id)
    assert planned["ok"] and planned["dry_run"] and not planned["applied"]
    assert conn.execute("SELECT metadata FROM memories WHERE id=?", (memory_id,)).fetchone()[0] == before

    applied = _call(provider, action=action, id=memory_id, dry_run=False,
                    expected_updated_at=planned["expected_updated_at"],
                    expected_lifecycle=planned["expected_lifecycle"])
    assert applied["ok"] and applied["applied"]
    metadata = json.loads(conn.execute("SELECT metadata FROM memories WHERE id=?", (memory_id,)).fetchone()[0])
    assert metadata["lifecycle"] == lifecycle
    assert provider._truth_writer_role == "owner"
    assert provider._writer_thread is writer and writer.is_alive()
    assert not conn.in_transaction


def test_candidate_review_honors_plan_revision(provider):
    memory_id = _candidate(provider)
    planned = _call(provider, action="promote", id=memory_id)
    assert planned["ok"]
    conn = provider._require_conn()
    conn.execute("UPDATE memories SET updated_at='changed-after-plan' WHERE id=?", (memory_id,))
    conn.commit()
    result = _call(provider, action="promote", id=memory_id, dry_run=False,
                   expected_updated_at=planned["expected_updated_at"])
    assert not result["ok"] and result["status"] == "conflict"
    assert json.loads(conn.execute("SELECT metadata FROM memories WHERE id=?", (memory_id,)).fetchone()[0])["lifecycle"] == "candidate"


@pytest.mark.parametrize("dry_run", [True, False])
def test_candidate_review_cannot_read_or_mutate_foreign_scope(provider, dry_run):
    memory_id = _candidate(provider)
    conn = provider._require_conn()
    conn.execute("UPDATE memories SET scope_id='foreign-scope' WHERE id=?", (memory_id,))
    conn.commit()
    result = _call(provider, action="promote", id=memory_id, dry_run=dry_run)
    assert not result["ok"] and result["status"] == "not_found"
    assert "blue green" not in json.dumps(result)


def test_store_receipt_reports_actual_candidate_lifecycle(provider):
    stored = json.loads(provider.handle_tool_call("scope_recall_store", {
        "content": "During migration we use a temporary database replica for verification.",
        "target": "ops",
    }))
    assert stored["stored"]
    row = provider._require_conn().execute("SELECT metadata FROM memories WHERE id=?", (stored["id"],)).fetchone()
    lifecycle = json.loads(row[0])["lifecycle"]
    assert lifecycle == "candidate"
    assert stored["lifecycle"] == lifecycle
    assert stored["receipt"]["lifecycle"] == lifecycle
    assert stored["receipt"]["action"] != "promoted"


@pytest.mark.parametrize("dry_run", [True, False])
def test_online_review_cannot_mutate_fact_owned_projection(provider, dry_run):
    memory_id = _candidate(provider)
    conn = provider._require_conn()
    conn.execute("""INSERT INTO fact_claims(
        claim_id, memory_id, scope_id, subject_key, predicate_key, fact_key,
        value, normalized_value, value_fingerprint, cardinality, recorded_at,
        source_type, status)
        SELECT 'fact-claim', id, scope_id, 'service', 'deployment', 'deployment',
        'blue green', 'blue green', 'fingerprint', 'single', updated_at,
        'manual', 'current' FROM memories WHERE id=?""", (memory_id,))
    conn.commit()
    result = _call(provider, action="archive", id=memory_id, dry_run=dry_run)
    assert result["status"] == "invalid_state"
    assert result["blocked_fact_ids"] == [memory_id]
    assert json.loads(conn.execute("SELECT metadata FROM memories WHERE id=?", (memory_id,)).fetchone()[0])["lifecycle"] == "candidate"


@pytest.mark.parametrize("lifecycle", ["promoted", "archived", "superseded", "active"])
def test_online_review_does_not_reprocess_event_digest_rows(provider, lifecycle):
    memory_id = _candidate(provider)
    conn = provider._require_conn()
    metadata = json.loads(conn.execute("SELECT metadata FROM memories WHERE id=?", (memory_id,)).fetchone()[0])
    metadata["lifecycle"] = lifecycle
    conn.execute("UPDATE memories SET metadata=? WHERE id=?", (json.dumps(metadata), memory_id))
    conn.commit()
    for dry_run in (True, False):
        result = _call(provider, action="promote", id=memory_id, dry_run=dry_run)
        assert result["status"] == "invalid_state" and not result["applied"]
    assert json.loads(conn.execute("SELECT metadata FROM memories WHERE id=?", (memory_id,)).fetchone()[0]) == metadata


def test_online_review_rolls_back_truth_when_audit_write_fails(provider):
    memory_id = _candidate(provider)
    conn = provider._require_conn()
    before = tuple(conn.execute("SELECT metadata,updated_at FROM memories WHERE id=?", (memory_id,)).fetchone())
    audit_count = conn.execute("SELECT COUNT(*) FROM governance_audit_events").fetchone()[0]
    outbox_count = conn.execute("SELECT COUNT(*) FROM vector_outbox").fetchone()[0]
    conn.execute("""CREATE TEMP TRIGGER reject_review_audit
        BEFORE INSERT ON governance_audit_events
        WHEN NEW.event_type = 'memory_candidate_review'
        BEGIN SELECT RAISE(ABORT, 'injected audit failure'); END""")
    conn.commit()
    try:
        result = _call(provider, action="archive", id=memory_id, dry_run=False)
        assert result.get("error") and not result.get("applied")
        assert tuple(conn.execute("SELECT metadata,updated_at FROM memories WHERE id=?", (memory_id,)).fetchone()) == before
        assert conn.execute("SELECT COUNT(*) FROM governance_audit_events").fetchone()[0] == audit_count
        assert conn.execute("SELECT COUNT(*) FROM vector_outbox").fetchone()[0] == outbox_count
        assert not conn.in_transaction
    finally:
        conn.execute("DROP TRIGGER reject_review_audit")
        conn.commit()
    assert _call(provider, action="archive", id=memory_id, dry_run=False)["applied"]
