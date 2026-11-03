"""Host ingress and portable/imported truth preserve summary ownership and store identity."""

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from scope_recall.adapters.clients import CodexHookHandler
from scope_recall.adapters.clients.identity import resolve_runtime_audience, trusted_context
from scope_recall.core.claims import Qualification
from scope_recall.core.management import MemoryManagement
from scope_recall.core.management_query import MemoryFilter
from scope_recall.core.memory_import import ImportRecord, import_record
from scope_recall.core.restore import InstallationMaintenance
from scope_recall.core.summary_storage import close_session, next_summary_due, queue_summaries
from scope_recall.maintenance import memory_export
from scope_recall.maintenance.shared_import import import_entry

from tests.contract.test_claims import Clock, initial
from tests.contract.test_importance_summaries import app, build
from tests.host.codex.test_merged_host_boundaries import client
from tests.host.codex.test_shared_client import _hermes, store
from tests.host.hermes.test_two_entries_one_store import _old_store, _scope, entries, root
from tests.v11_support import source_event


def _idle_jobs(core, context):
    """Observe real ingress first, then advance only this isolated core's clock."""
    with core.storage.read(context) as tx:
        row = (
            tx._check()
            .execute("SELECT * FROM summary_groups WHERE kind='session' ORDER BY rowid DESC LIMIT 1")
            .fetchone()
        )
        assert row is not None
        row = dict(row)
    context = replace(
        context,
        session_id=row["subject_ref"],
        project_id=row["project_id"],
        branch_id=row["branch_id"],
        entry_id=row["entry_id"],
    )
    last = datetime.fromisoformat(row["last_activity"].replace("Z", "+00:00"))
    core.clock = Clock()
    core.clock.now = (last + timedelta(seconds=1799)).isoformat().replace("+00:00", "Z")
    with core.storage.write(context) as tx:
        queue_summaries(tx, now=core.clock.now)
        queue_summaries(tx, now=core.clock.now)
        assert not tx._check().execute("SELECT 1 FROM summary_parts").fetchone()
        assert datetime.fromisoformat(
            next_summary_due(tx, now=core.clock.now).replace("Z", "+00:00")
        ) == last + timedelta(seconds=1800)
    core.clock.now = (last + timedelta(seconds=1800)).isoformat().replace("+00:00", "Z")
    with core.storage.write(context) as tx:
        queue_summaries(tx, now=core.clock.now)
        queue_summaries(tx, now=core.clock.now)
        assert tx._check().execute("SELECT 1 FROM summary_parts").fetchone()
        # Automatic unknown task anchors never become completed task summaries.
        assert (
            not tx._check()
            .execute("SELECT 1 FROM summary_parts p JOIN summary_groups g USING(group_key) WHERE g.kind='task'")
            .fetchone()
        )


def test_four_client_hosts_reach_the_same_thirty_minute_summary_boundary(client, tmp_path, monkeypatch):
    _shared, home, host = client
    hook = CodexHookHandler.from_home(str(home), host)
    calls = []
    monkeypatch.setattr(hook, "ensure_runtime", lambda *a, **kw: calls.append(True))
    try:
        hook.handle_payload(
            dict(
                hook_event_name="UserPromptSubmit",
                session_id="TEST-idle-session",
                prompt_id="TEST-idle-turn",
                generation_id="TEST-idle-turn",
                turn_id="TEST-idle-turn",
                cwd=str(tmp_path),
                prompt="TEST-summary host captured a safe result.",
            )
        )
        ctx = trusted_context(hook.config, resolve_runtime_audience(hook.config, None), session_id="TEST-idle-session")
        _idle_jobs(hook.core, ctx)
        assert calls
    finally:
        hook.close()


def test_hermes_reaches_the_same_thirty_minute_summary_boundary(store, monkeypatch):
    _shared, homes, _client, _scope_id = store
    provider = _hermes(homes["agent3"])
    calls = []
    monkeypatch.setattr(provider._binding, "wake_worker", lambda **kw: calls.append(True))
    try:
        provider.observe_pre_llm(
            session_id="TEST-session-1",
            turn_id="TEST-summary-idle",
            user_message="TEST-summary Hermes captured a safe result.",
        )
        _idle_jobs(provider._core, provider._require_identity().trusted_context())
        assert calls
    finally:
        provider.shutdown()


def test_shared_import_discards_foreign_body_scan_receipts(root, entries):
    home, _peer = entries
    core, _ctx, _refs, old = _old_store(home, _scope(home), "TEST-imported color amber.")
    with closing(sqlite3.connect(core.storage.path)) as conn:
        assert conn.execute("SELECT secret_scan_version FROM source_events").fetchone()[0] is not None
    result = import_entry(root=root, entry_id="agent3", source=old)
    assert result["status"] == "imported"
    with closing(sqlite3.connect(root / "memory.sqlite3")) as conn:
        assert conn.execute("SELECT secret_scan_version,secret_scan_names FROM source_events").fetchall() == [
            (None, None)
        ]


@pytest.mark.parametrize("source_schema", [1119, 1120])
def test_shared_import_preserves_fact_salience_and_identity_but_rebuilds_summaries(
    root, entries, monkeypatch, source_schema
):
    home, _peer = entries
    scope = _scope(home)
    core, ctx, refs, old = _old_store(home, scope, "TEST-imported color amber.")
    core.clock = Clock()
    source = core.source(ctx, refs[0].ref, 1)
    payload = dict(
        kind="fact",
        subject="TEST-imported",
        predicate="color",
        value_text="amber",
        conditions=[],
        statement_kind="assertion",
        valid_from=None,
        valid_to=None,
        evidence_spans=[dict(source_ref=source.ref, source_revision=1, quote=source.event["content"])],
    )
    with core.storage.write(ctx) as tx:
        fact = tx.claims.append(
            scope, payload, Qualification("active", "direct_report", "qualified_statement"), recorded_at=core.clock.now
        )
    owner = MemoryManagement(core, InstallationMaintenance(ctx))
    owner.importance(fact.ref, expected_revision=1, importance=2, pinned=True)
    build(core, ctx)
    later = replace(ctx, session_id="TEST-pending-summary")
    core.record_event(
        later,
        source_event(source_event_key="TEST-pending", content="TEST-imported pending result."),
        scope_id=scope,
        remaining_seconds=10,
    )
    with core.storage.write(later) as tx:
        queue_summaries(tx, now=core.clock.now)
        close_session(tx, later.session_id, now=core.clock.now)
        # The durable group cursor may need one wrap to revisit a just-closed group.
        queue_summaries(tx, now=core.clock.now)
        queue_summaries(tx, now=core.clock.now)
        summary_refs = {
            r[0]
            for r in tx._check().execute(
                "SELECT DISTINCT claim_id FROM claim_versions WHERE qualification_reason IN "
                "('summary_pending','grounded_session_summary','grounded_task_summary')"
            )
        }
        assert len(summary_refs) >= 2
    with closing(sqlite3.connect(root / "memory.sqlite3")) as db:
        identity = db.execute("SELECT database_id FROM instance_meta").fetchone()[0]
    from scope_recall.maintenance import shared_import

    original_settle = shared_import.settle_brought_denials

    def settle(tx, brought, *, now):
        assert not summary_refs.intersection(brought)
        return original_settle(tx, brought, now=now)

    monkeypatch.setattr(shared_import, "settle_brought_denials", settle)
    import hashlib

    from tests.v11_support import downgrade_store

    if source_schema < 1120:
        downgrade_store(core.storage.path, source_schema)
    source_hash = hashlib.sha256(core.storage.path.read_bytes()).hexdigest()
    result = import_entry(root=root, entry_id="agent3", source=old)
    assert hashlib.sha256(core.storage.path.read_bytes()).hexdigest() == source_hash
    assert result["status"] == "imported"
    with closing(sqlite3.connect(root / "memory.sqlite3")) as db:
        assert db.execute("SELECT database_id FROM instance_meta").fetchone()[0] == identity
        assert db.execute(
            "SELECT importance,pinned,importance_owner FROM claims WHERE claim_id=?", (fact.ref,)
        ).fetchone() == ((2, 1, 1) if source_schema == 1120 else (3, 0, 0))
        assert not db.execute(
            "SELECT 1 FROM claim_versions WHERE qualification_reason IN "
            "('summary_pending','grounded_session_summary','grounded_task_summary')"
        ).fetchone()
        assert not any(r[0] in summary_refs for r in db.execute("SELECT subject_ref FROM work_items"))
        assert db.execute("SELECT count(*) FROM source_events").fetchone()[0] >= 2
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_portable_export_preserves_base_importance_and_summary_without_replacing_database_identity(app, tmp_path):
    core, ctx = app
    fact, _source = initial(core, ctx)
    owner = MemoryManagement(core, InstallationMaintenance(replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)))
    owner.importance(fact.ref, expected_revision=1, importance=2, pinned=True)
    build(core, ctx)
    with core.storage.read(ctx) as tx:
        identity = tx.database_identity()
    out = tmp_path / "TEST-export"
    with owner.export_reader(MemoryFilter()) as reader:
        memory_export.write_export(reader, out)
    rows = [json.loads(line) for line in (out / "facts.jsonl").read_text(encoding="utf-8").splitlines()]
    exported = next(r for r in rows if r["ref"] == fact.ref)
    assert (exported["importance"], exported["pinned"]) == (2, True)
    assert any(r["qualification_reason"] == "grounded_session_summary" for r in rows)
    assert "database_id" not in json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    with core.storage.read(ctx) as tx:
        assert tx.database_identity() == identity


def test_f5_import_is_model_free_and_defaults_new_facts_to_neutral_salience(app):
    core, ctx = app
    with core.storage.read(ctx) as tx:
        identity = tx.database_identity()
    record = ImportRecord(
        "TEST-fact", "TEST-imported color amber.", "document", project=ctx.project_id, original_origin="origin_unknown"
    )
    receipt = import_record(core, ctx, record, source="TEST-export", source_id="TEST-account", scope_id="TEST-scope")
    assert receipt.durability == "persisted"
    with core.storage.write(ctx) as tx:
        assert not tx._check().execute("SELECT 1 FROM claims").fetchone()
        assert not tx._check().execute("SELECT 1 FROM work_items WHERE work_type='consolidate'").fetchone()
        source = tx.source(receipt.event_refs[0].ref, 1)
        payload = dict(
            kind="fact",
            subject="TEST-imported",
            predicate="color",
            value_text="amber",
            conditions=[],
            statement_kind="assertion",
            valid_from=None,
            valid_to=None,
            evidence_spans=[dict(source_ref=source.ref, source_revision=1, quote=source.event["content"])],
        )
        fact = tx.claims.append(
            "TEST-scope",
            payload,
            Qualification("proposed", "derived_summary", "import_requires_confirmation"),
            recorded_at=core.clock.now,
        )
        assert (fact.base_importance, fact.pinned) == (3, False)
        assert tx.database_identity() == identity


@pytest.mark.parametrize("origin,expected", [("human_direct", True), ("origin_unknown", False)])
def test_f5_conversations_require_verified_original_origin_before_summary_generation(app, origin, expected):
    from scope_recall.core.schema import SCHEMA_VERSION
    from scope_recall.maintenance.shared_import import SOURCE_SCHEMAS

    from tests.contract.test_summary_repairs import FirstSentenceModel

    core, ctx = app
    with core.storage.read(ctx) as tx:
        identity = tx.database_identity()
    record = ImportRecord(
        "TEST-chat",
        "TEST-imported safe conversation.",
        "user",
        project=ctx.project_id,
        session="TEST-imported-chat",
        original_origin=origin,
    )
    receipt = import_record(core, ctx, record, source="TEST-export", source_id="TEST-account", scope_id="TEST-scope")
    with core.storage.read(ctx) as tx:
        assert not tx._check().execute("SELECT 1 FROM claim_versions").fetchone()
        assert tx.database_identity() == identity
    imported = core.source(ctx, receipt.event_refs[0].ref, 1)
    model = FirstSentenceModel()
    build(core, replace(ctx, session_id=imported.session_id), model)
    assert bool(model.pages) == expected
    assert SOURCE_SCHEMAS == frozenset(range(1109, SCHEMA_VERSION + 1))
