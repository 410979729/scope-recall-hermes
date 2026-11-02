"""Entry state through the Hermes provider on a synthetic installation."""

from scope_recall.contracts import TrustedContext
from scope_recall.core.management import MemoryManagement
from scope_recall.core.restore import InstallationMaintenance

from tests.host.hermes.test_two_entries_one_store import entries, root


def test_provider_entry_state_lifecycle(adapter):
    provider, _clock = adapter
    core = provider._require_core()
    binding = core.storage.binding
    context = TrustedContext(binding, "TEST-owner", binding.scope_ids, "host_generated")
    owner = MemoryManagement(core, InstallationMaintenance(context))

    def capture(number):
        provider.on_turn_start(number, f"TEST observatory amber {number}", turn_id=f"TEST-{number}")
        provider.observe_pre_llm(
            session_id="TEST-session-1", turn_id=f"TEST-{number}", user_message=f"TEST observatory amber {number}"
        )

    def count():
        with core.storage.read(context) as tx:
            return tx._check().execute("SELECT count(*) FROM source_events").fetchone()[0]

    capture(1)
    assert count() == 1
    owner.set_entry_state("local", "read-only", actor="TEST-owner")
    capture(2)
    assert count() == 1
    assert not provider._retry.captures
    owner.set_entry_state("local", "paused", actor="TEST-owner")
    capture(3)
    assert count() == 1
    assert "TEST observatory amber 1" not in provider.prefetch("TEST observatory amber")
    owner.set_entry_state("local", "normal", actor="TEST-owner")
    capture(4)
    assert count() == 2


def test_shared_worker_holds_the_owning_paused_entry(root, entries):
    from scope_recall.core.management import MemoryManagement
    from scope_recall.core.restore import InstallationMaintenance

    from tests.contract.test_worker import FakeConsolidation, consolidation_payload
    from tests.host.hermes.test_two_entries_one_store import _provider

    agent3, _agent2 = entries
    provider = _provider(agent3)
    try:
        provider.on_turn_start(1, "TEST shared worker source", turn_id="TEST-worker")
        provider.observe_pre_llm(
            session_id="TEST-session-1", turn_id="TEST-worker", user_message="TEST shared worker source"
        )
        core = provider._require_core()
        binding = core.storage.binding
        worker = TrustedContext(binding, "TEST-shared-worker", binding.scope_ids, "host_generated")
        owner = MemoryManagement(core, InstallationMaintenance(worker))
        owner.set_entry_state("agent3", "paused", actor="TEST-owner")
        with core.storage.write(worker) as tx:
            tx._check(write=True).execute("UPDATE work_items SET state='done' WHERE work_type='embed'")
        model = FakeConsolidation(lambda sources, **_: consolidation_payload(*sources))
        core.drain_worker(worker, consolidation=model, max_items=1, remaining_seconds=10)
        assert model.calls == 0
        with core.storage.read(worker) as tx:
            from scope_recall.core.entry_control import state_for

            assert state_for(tx, worker) == ("normal", 0)
            assert (
                tx._check().execute("SELECT state FROM work_items WHERE work_type='consolidate'").fetchone()[0]
                == "pending"
            )
    finally:
        provider.shutdown()


def test_shared_worker_processes_unpaused_sources_in_a_mixed_batch(root, entries, monkeypatch):
    from scope_recall.core import worker_consolidation

    from tests.contract.test_worker import FakeConsolidation, consolidation_payload
    from tests.host.hermes.test_two_entries_one_store import _provider

    agent3, agent2 = entries
    paused, active = _provider(agent3), _provider(agent2)
    try:
        for provider, turn in ((paused, "TEST-paused"), (active, "TEST-active")):
            provider.on_turn_start(1, turn + " source text", turn_id=turn)
            provider.observe_pre_llm(session_id="TEST-session-1", turn_id=turn, user_message=turn + " source text")
        core = active._require_core()
        binding = core.storage.binding
        worker = TrustedContext(binding, "TEST-shared-worker", binding.scope_ids, "host_generated")
        owner = MemoryManagement(core, InstallationMaintenance(worker))
        with core.storage.write(worker) as tx:
            conn = tx._check(write=True)
            conn.execute("UPDATE work_items SET state='done' WHERE work_type='embed'")
            rows = conn.execute(
                "SELECT event_id,source_revision,entry_id FROM source_events ORDER BY source_id"
            ).fetchall()
            sources = {entry: tx.source(ref, revision) for ref, revision, entry in rows}
            conn.execute(
                "UPDATE work_items SET available_at='9999-01-01T00:00:00Z' WHERE subject_ref=?",
                (sources["agent3"].ref,),
            )
        owner.set_entry_state("agent3", "paused", actor="TEST-owner")
        monkeypatch.setattr(
            worker_consolidation,
            "_episode_batch",
            lambda tx, source, item, now: (None, (sources["agent3"], source), ()),
        )
        seen = []

        def build(batch, **_):
            seen.extend(item.entry_id for item in batch)
            return consolidation_payload(*batch)

        core.drain_worker(worker, consolidation=FakeConsolidation(build), max_items=1, remaining_seconds=10)
        assert seen == ["agent2"]
    finally:
        paused.shutdown()
        active.shutdown()


def test_batched_embedding_excludes_only_the_paused_entry(root, entries):
    from types import SimpleNamespace

    from scope_recall.core.worker_projection import prepare_embed_group

    from tests.host.hermes.test_two_entries_one_store import _provider

    agent3, agent2 = entries
    paused, active = _provider(agent3), _provider(agent2)
    try:
        for provider, turn in ((paused, "TEST-p"), (active, "TEST-a"), (active, "TEST-b")):
            provider.on_turn_start(1, turn + " synthetic source", turn_id=turn)
            provider.observe_pre_llm(session_id="TEST-session-1", turn_id=turn, user_message=turn + " synthetic source")
        core = active._require_core()
        binding = core.storage.binding
        worker = TrustedContext(binding, "TEST-shared-worker", binding.scope_ids, "host_generated")
        MemoryManagement(core, InstallationMaintenance(worker)).set_entry_state("agent3", "paused", actor="TEST-owner")
        with core.storage.read(worker) as tx:
            rows = (
                tx._check().execute("SELECT event_id,source_revision FROM source_events ORDER BY source_id").fetchall()
            )
        items = [SimpleNamespace(subject_ref=ref, subject_revision=revision) for ref, revision in rows]

        class BatchEmbed:
            seen = []

            def prepare_sources(self, sources, *, remaining_seconds):
                self.seen.extend(source.entry_id for source in sources)
                return [source.ref for source in sources]

        embed = BatchEmbed()
        prepared = prepare_embed_group(
            core.storage, core.clock, worker, items, embed=embed, started=core.clock.monotonic(), budget=10
        )
        assert embed.seen == ["agent2", "agent2"] and len(prepared) == 2
    finally:
        paused.shutdown()
        active.shutdown()


def test_entry_cannot_change_its_state_through_provider_tool(adapter):
    provider, _clock = adapter
    core = provider._require_core()
    binding = core.storage.binding
    worker = TrustedContext(binding, "TEST-owner", binding.scope_ids, "host_generated")
    owner = MemoryManagement(core, InstallationMaintenance(worker))
    answer = provider.handle_tool_call("entry-state", {"entry_id": "local", "state": "paused"})
    assert owner.entry("local")["state"] == "normal"
    assert "error" in str(answer).lower()


def test_doctor_reports_each_shared_entry_state(root, entries):
    from scope_recall.maintenance.doctor_report import DoctorReport
    from scope_recall.maintenance.doctor_store import check_storage

    from tests.host.hermes.test_two_entries_one_store import _provider

    agent3, _agent2 = entries
    provider = _provider(agent3)
    try:
        core = provider._require_core()
        binding = core.storage.binding
        worker = TrustedContext(binding, "TEST-owner", binding.scope_ids, "host_generated")
        MemoryManagement(core, InstallationMaintenance(worker)).set_entry_state("agent3", "paused", actor="TEST-owner")
        report = DoctorReport(host="hermes", status="ok")
        assert check_storage(report, binding, root)
        states = {entry["id"]: entry["state"] for entry in report.entries}
        assert states == {"agent2": "normal", "agent3": "paused"}
    finally:
        provider.shutdown()


def test_provider_recalls_read_only_and_withholds_paused_entry(adapter):
    provider, _clock = adapter
    core = provider._require_core()
    binding = core.storage.binding
    worker = TrustedContext(binding, "TEST-owner", binding.scope_ids, "host_generated")
    owner = MemoryManagement(core, InstallationMaintenance(worker))
    provider.on_turn_start(1, "TEST coral telescope value VZ-81.", turn_id="TEST-recall")
    provider.observe_pre_llm(
        session_id="TEST-session-1", turn_id="TEST-recall", user_message="TEST coral telescope value VZ-81."
    )
    provider.on_session_switch("TEST-session-2")
    from tests.host.hermes.test_prefetch import _explicit_recall

    query = "What is TEST coral telescope value VZ-81?"
    owner.set_entry_state("local", "read-only", actor="TEST-owner")
    assert "VZ-81" in provider.prefetch(query)
    assert "VZ-81" in str(_explicit_recall(provider, query)["items"])
    owner.set_entry_state("local", "paused", actor="TEST-owner")
    assert not provider.prefetch(query)
    packet = _explicit_recall(provider, query)
    assert not packet["items"] and "entry_state:paused" in packet["gaps"]


def test_local_hermes_entry_reports_its_grants(adapter, hermes_home):
    from types import SimpleNamespace

    from scope_recall.maintenance.memory import entry_grants

    _provider, _clock = adapter
    grants = entry_grants(SimpleNamespace(root=None, host="hermes", instance_root=str(hermes_home)))
    assert grants["local"]["read_scopes"] and grants["local"]["write_scopes"]


def test_provider_discards_memory_when_brief_confirmation_finds_paused_entry(adapter, monkeypatch):
    provider, _clock = adapter
    core = provider._require_core()
    context = provider._require_identity().trusted_context()
    turn = provider._active_turn_id
    monkeypatch.setattr(core, "confirm_session_brief", lambda *_args, **_kwargs: "entry_state:paused")
    result = provider._prefetch._confirm(
        core, context, context.session_id, turn, {"_epoch": 1, "text": "TEST brief"}, "TEST brief\nTEST recalled memory"
    )
    assert result == ""
    assert "entry_state:paused" in provider._diagnostics.capability_gaps


def test_provider_review_gate_withholds_fact_from_prefetch_tools_and_brief(adapter):
    from dataclasses import replace

    from tests.contract.test_claims import draft
    from tests.v11_support import recall_request

    provider, _clock = adapter
    core = provider._require_core()
    binding = core.storage.binding
    owner_context = TrustedContext(binding, "TEST-owner", binding.scope_ids, "host_generated")
    owner = MemoryManagement(core, InstallationMaintenance(owner_context))
    for scope in binding.scope_ids:
        owner.set_review_policy(scope, True, expected_version=0)
    provider.on_turn_start(1, "TEST-project palette ultramarine.", turn_id="TEST-review")
    provider.observe_pre_llm(
        session_id="TEST-session-1", turn_id="TEST-review", user_message="TEST-project palette ultramarine."
    )
    with core.storage.read(owner_context) as tx:
        row = (
            tx._check()
            .execute(
                "SELECT event_id,source_revision,scope_id,project_id,branch_id FROM source_events "
                "ORDER BY source_id DESC LIMIT 1"
            )
            .fetchone()
        )
    ctx = replace(owner_context, project_id=row["project_id"], branch_id=row["branch_id"])
    source = core.source(ctx, row[0], row[1])
    pending = core.records.accept_claim_proposals(
        ctx,
        dict(
            protocol_version="1.1",
            source_refs=[f"{source.ref}@{source.revision}"],
            claim_proposals=[
                draft(source, "ultramarine", predicate="palette", kind="fact", statement_kind="assertion")
            ],
            resume_proposals=[],
            reference_proposals=[],
        ),
        scope_id=row["scope_id"],
        remaining_seconds=10,
    ).items[0]
    assert pending.state == "proposed"
    assert pending.ref not in provider.prefetch("TEST-project palette")
    assert pending.ref not in str(
        core.recall_packet(ctx, recall_request(query="TEST-project palette", mode="current"), deadline_seconds=10)
    )
    assert pending.ref not in str(core.session_brief(ctx))
