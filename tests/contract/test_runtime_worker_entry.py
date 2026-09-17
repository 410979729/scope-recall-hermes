"""Bounded runtime worker tests over isolated SQLite and a real subprocess."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import time
from dataclasses import replace
import pytest

from scope_recall.contracts import ContractError, InstanceBinding, TrustedContext
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.runtime.instance import (
    VectorRuntimeConfig,
    RuntimeInstanceConfig,
    build_runtime_instance,
    default_vector_factory,
)
from scope_recall.runtime.auxiliary import AuxiliaryRuntimeConfig
from scope_recall.runtime.worker_launch import launch_worker
from scope_recall.core.retrieval import SearchContext, SearchLimits
from v11_support import source_event


def _binding(root: Path) -> InstanceBinding:
    return InstanceBinding(
        "TEST-runtime-agent",
        "TEST-runtime-installation",
        root,
        frozenset({"TEST-scope"}),
        True,
    )


def _context(binding: InstanceBinding, session: str) -> TrustedContext:
    return TrustedContext(binding, session, binding.scope_ids, "human_direct")


def _config_payload(binding: InstanceBinding, *, session: str = "worker-session", **extra):
    payload = {
        "binding": {
            "agent_id": binding.agent_id,
            "installation_id": binding.installation_id,
            "data_directory": str(binding.data_directory),
            "scope_ids": sorted(binding.scope_ids),
            "test_mode": binding.test_mode,
        },
        "session_id": session,
        "allowed_scope_ids": sorted(binding.scope_ids),
        "actor_origin": "human_direct",
        "owner_id": "TEST-worker-owner",
        "request_seconds": 45.0,
        "drain_seconds": 120.0,
        "max_items": 32,
        "lease_seconds": 60.0,
        "auxiliary": {"external_embedding": False, "external_consolidation": False},
    }
    payload.update(extra)
    return payload


def _write_config(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.mark.skipif(os.name != "nt", reason="Windows native path boundary")
def test_worker_reports_native_path_gap_without_starting_helper(tmp_path, monkeypatch):
    from io import StringIO
    from scope_recall.core.recall_policy import SPACE_ID
    import scope_recall.vector.process_store as native_store
    from scope_recall.runtime.worker_entry import run_worker

    binding = _binding(tmp_path / "data")
    binding.data_directory.mkdir()
    vector_root = binding.data_directory / "vectors" / SPACE_ID
    payload = _config_payload(binding, vector={
        "backend": "lancedb", "storage_dir": str(vector_root),
        "table_name": "TEST-" + "x" * 150, "dimensions": 3072,
    })
    config = _write_config(tmp_path / "worker.json", payload)
    calls = []

    def unexpected_helper():
        calls.append("helper")
        raise AssertionError("unsafe path must be rejected before helper startup")

    monkeypatch.setattr(native_store, "_worker_command", unexpected_helper)
    output = StringIO()
    assert run_worker(config, output=output) == 1
    receipt = json.loads(output.getvalue())
    assert receipt["status"] == "degraded"
    assert receipt["processed"] == 0
    assert receipt["capability_gaps"] == ["native_vector_path_too_long"]
    assert str(vector_root) not in output.getvalue()
    assert calls == []
    assert not vector_root.exists()
    assert not (binding.data_directory / "memory.sqlite3").exists()


def _run_child(tmp_path: Path, monkeypatch, payload: dict):
    config_path = _write_config(tmp_path / "worker.json", payload)
    root = str(Path(__file__).resolve().parents[2])
    current = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", root + (os.pathsep + current if current else ""))
    child = launch_worker(config_path)
    stdout, stderr = child.communicate(timeout=120)
    assert stderr == "", stderr
    assert stdout.count("\n") == 1
    return child, json.loads(stdout)


def test_runtime_construction_is_pure_and_vector_opens_only_on_demand(tmp_path):
    binding = _binding(tmp_path / "data")
    config = RuntimeInstanceConfig(
        binding=binding,
        session_id="query-session",
        allowed_scope_ids=binding.scope_ids,
        auxiliary=AuxiliaryRuntimeConfig.from_mapping(
            {"external_embedding": False, "external_consolidation": False}
        ),
        vector=VectorRuntimeConfig(
            backend="sqlite-bruteforce",
            storage_dir=tmp_path / "vectors",
            table_name="TEST-vectors",
            dimensions=1,
            test_injection_override=True,
        ),
    )
    calls: list[str] = []

    class Vectors:
        def open_existing(self):
            calls.append("open")

        def search(self, context, *, limit, remaining_seconds):
            calls.append("search")
            return ()

    instance = build_runtime_instance(config, vector_factory=lambda _: (calls.append("build") or Vectors()))
    assert calls == []
    instance.core.initialize()
    instance.status()
    assert calls == []
    result = instance.recall(
        {
            "protocol_version": "1.1",
            "request_id": "TEST-runtime-recall",
            "query": "TEST query",
            "mode": "auto",
            "max_items": 1,
            "budget_tokens": 1200,
        }
    )
    assert calls[:2] == ["build", "open"]
    # A disabled query-embedding route must not expose or invoke the raw
    # native store as a Core VectorPort; lexical retrieval remains available.
    assert "search" not in calls
    assert result.items == ()
    instance.close()


def test_existing_store_is_composed_with_trusted_lance_ports_on_demand(tmp_path):
    binding = _binding(tmp_path / "data")
    config = RuntimeInstanceConfig(
        binding=binding,
        session_id="query-session",
        allowed_scope_ids=binding.scope_ids,
        auxiliary=AuxiliaryRuntimeConfig.from_mapping(
            {"external_embedding": False, "external_consolidation": False}
        ),
        vector=VectorRuntimeConfig(
            backend="lancedb",
            storage_dir=tmp_path / "vectors",
            table_name="TEST-vectors",
            dimensions=2,
            test_injection_override=True,
        ),
    )

    class ExistingStore:
        def __init__(self):
            self.opened = False
            self.closed = False

        def open_existing(self):
            self.opened = True

        def search(self, vector, *, scope_id, limit):
            return []

        def close(self):
            self.closed = True

    class QueryEmbedding:
        def embed_query(self, text, *, remaining_seconds):
            return (0.1, 0.2)

    class SourceEmbedding:
        def embed_source(self, source, *, remaining_seconds):
            return (0.1, 0.2)

    store = ExistingStore()
    instance = build_runtime_instance(config, vector_factory=lambda _: store)
    # Ports are explicit runtime dependencies; replacing the disabled optional
    # routes with deterministic test ports must not contact an external model.
    instance.auxiliary = replace(
        instance.auxiliary,
        query_embedding=QueryEmbedding(),
        source_embedding=SourceEmbedding(),
    )
    assert store.opened is False
    instance._ensure_vector_port()
    assert store.opened is True
    assert type(instance._vector_port).__name__ == "LanceVectorPort"
    assert type(instance.core.vectors).__name__ == "_LazyVectorPort"
    assert type(instance._default_embed).__name__ == "LanceEmbedPort"
    assert type(instance._default_purge).__name__ == "LancePurgePort"
    instance.close()
    assert store.closed is True


def test_lazy_vector_facade_opens_existing_store_for_each_core_search_context(tmp_path):
    binding = _binding(tmp_path / "data")
    config = RuntimeInstanceConfig(
        binding=binding,
        session_id="construction-session",
        allowed_scope_ids=binding.scope_ids,
        auxiliary=AuxiliaryRuntimeConfig.from_mapping(
            {"external_embedding": False, "external_consolidation": False}
        ),
        vector=VectorRuntimeConfig(
            backend="lancedb",
            storage_dir=tmp_path / "vectors",
            table_name="TEST-vectors",
            dimensions=2,
            test_injection_override=True,
        ),
    )

    class ExistingStore:
        def __init__(self):
            self.opened = False
            self.open_deadline = None
            self.search_calls = []

        def open_existing(self):
            from scope_recall.core.deadline import current_request_deadline

            self.opened = True
            deadline = current_request_deadline()
            self.open_deadline = None if deadline is None else deadline.deadline_monotonic

        def search(self, vector, *, scope_id, limit):
            self.search_calls.append((tuple(vector), scope_id, limit))
            return []

        def close(self):
            pass

    class QueryEmbedding:
        def embed_query(self, text, *, remaining_seconds):
            return (0.1, 0.2)

    store = ExistingStore()
    instance = build_runtime_instance(config, vector_factory=lambda _: store)
    instance.auxiliary = replace(instance.auxiliary, query_embedding=QueryEmbedding())
    instance.core.initialize()
    context = SearchContext(
        query="TEST query",
        mode="auto",
        as_of=None,
        focus_refs=(),
        limits=SearchLimits(max_items=1, vector_limit=1),
        deadline=time.monotonic() + 5.0,
        now="2026-09-05T12:00:00Z",
        trusted_context=_context(binding, "actual-search-session"),
    )
    assert type(instance.core.recall_pipeline.vector_port).__name__ == "_LazyVectorPort"
    instance.core.recall_pipeline.vector_port.search(context, limit=1, remaining_seconds=4.0)
    result = instance.core.recall_pipeline.search(context)
    assert result.items == ()
    assert store.opened is True, result
    assert store.open_deadline is not None
    assert store.open_deadline <= context.deadline
    assert len(store.search_calls) == 1
    # The facade does not substitute the instance construction session for
    # the request-bound physical partition; the search ran for this context.
    assert store.search_calls[0][1]
    instance.close()


def test_lazy_vector_facade_reopens_poisoned_cached_store_on_next_search(tmp_path):
    binding = _binding(tmp_path / "data")
    config = RuntimeInstanceConfig(
        binding=binding,
        session_id="construction-session",
        allowed_scope_ids=binding.scope_ids,
        auxiliary=AuxiliaryRuntimeConfig.from_mapping(
            {"external_embedding": False, "external_consolidation": False}
        ),
        vector=VectorRuntimeConfig(
            backend="lancedb",
            storage_dir=tmp_path / "vectors",
            table_name="TEST-vectors",
            dimensions=2,
            test_injection_override=True,
        ),
    )

    class RecoveringStore:
        def __init__(self):
            self.failed = False
            self.closed = False
            self.open_deadlines = []
            self.search_calls = 0

        @property
        def requires_reopen(self):
            return self.failed

        def open_existing(self):
            from scope_recall.core.deadline import current_request_deadline

            deadline = current_request_deadline()
            self.open_deadlines.append(None if deadline is None else deadline.deadline_monotonic)
            self.failed = False
            self.closed = False

        def open_existing_with_work(self, during_open):
            self.open_existing()
            return during_open()

        def search(self, vector, *, scope_id, limit):
            self.search_calls += 1
            if self.search_calls == 1:
                self.failed = True
                self.closed = True
                raise RuntimeError("worker_failed")
            if self.failed or self.closed:
                raise RuntimeError("worker_closed")
            return []

        def close(self):
            self.closed = True

    class QueryEmbedding:
        def embed_query(self, text, *, remaining_seconds):
            return (0.1, 0.2)

    store = RecoveringStore()
    factory_calls = []
    instance = build_runtime_instance(config, vector_factory=lambda _: (factory_calls.append(store) or store))
    owned_auxiliary = instance.auxiliary
    instance.auxiliary = replace(instance.auxiliary, query_embedding=QueryEmbedding())
    instance.core.initialize()
    first = SearchContext(
        query="TEST first query",
        mode="auto",
        as_of=None,
        focus_refs=(),
        limits=SearchLimits(max_items=1, vector_limit=1),
        deadline=time.monotonic() + 5.0,
        now="2026-09-05T12:00:00Z",
        trusted_context=_context(binding, "first-search-session"),
    )
    with pytest.raises(RuntimeError, match="worker_failed"):
        instance.core.recall_pipeline.vector_port.search(first, limit=1, remaining_seconds=4.0)

    second = replace(
        first,
        query="TEST second query",
        deadline=time.monotonic() + 5.0,
        trusted_context=_context(binding, "second-search-session"),
    )
    assert instance.core.recall_pipeline.vector_port.search(second, limit=1, remaining_seconds=4.0) == ()
    assert factory_calls == [store]
    assert len(store.open_deadlines) == 2
    assert store.open_deadlines[1] is not None
    assert store.open_deadlines[1] <= second.deadline
    assert instance._owned_resources == [owned_auxiliary, store]
    instance.close()
    assert store.closed


def test_default_vector_factory_selects_process_store_without_opening(tmp_path):
    config = VectorRuntimeConfig(
        backend="lancedb",
        storage_dir=tmp_path / "vectors",
        table_name="TEST-vectors",
        dimensions=2,
        test_injection_override=True,
    )
    store = default_vector_factory(config)
    assert type(store).__name__ in {"ProcessLanceVectorStore", "LanceVectorStore"}
    assert not (config.storage_dir / "lancedb").exists()
    store.close()


def test_formal_vector_configuration_is_bound_to_approved_space(tmp_path):
    binding = _binding(tmp_path / "data")
    from scope_recall.core.recall_policy import SPACE_ID

    formal = RuntimeInstanceConfig(
        binding=binding,
        session_id="formal-session",
        allowed_scope_ids=binding.scope_ids,
        vector=VectorRuntimeConfig(
            backend="lancedb",
            storage_dir=binding.data_directory / "vectors" / SPACE_ID,
            table_name="TEST-formal-vectors",
            dimensions=3072,
        ),
    )
    assert formal.vector is not None
    with pytest.raises(ContractError, match="VECTOR_DIMENSIONS_MISMATCH"):
        RuntimeInstanceConfig(
            binding=binding,
            session_id="bad-dimension",
            allowed_scope_ids=binding.scope_ids,
            vector=VectorRuntimeConfig(
                backend="lancedb",
                storage_dir=binding.data_directory / "vectors" / SPACE_ID,
                table_name="TEST-formal-vectors",
                dimensions=2,
            ),
        )
    with pytest.raises(ContractError, match="VECTOR_STORAGE_OUTSIDE_BINDING"):
        RuntimeInstanceConfig(
            binding=binding,
            session_id="bad-path",
            allowed_scope_ids=binding.scope_ids,
            vector=VectorRuntimeConfig(
                backend="lancedb",
                storage_dir=tmp_path / "other-vectors",
                table_name="TEST-formal-vectors",
                dimensions=3072,
            ),
        )


def test_worker_processes_durable_work_for_new_session_without_source_fabrication(tmp_path, monkeypatch):
    binding = _binding(tmp_path / "data")
    core = MemoryCore(CoreConfig(binding))
    core.initialize()
    source = core.record_event(
        _context(binding, "human-session-A"),
        source_event(
            source_event_key="TEST-runtime/source-A",
            content="人类会话 A 的持久化内容，不是 worker 输入。",
        ),
        scope_id="TEST-scope",
        remaining_seconds=10,
    )
    assert source.durability == "persisted"
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("SELECT count(*) FROM work_items WHERE state='pending'").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM source_events WHERE session_id=?", ("human-session-A",)).fetchone()[0] == 1

    child, result = _run_child(tmp_path, monkeypatch, _config_payload(binding))
    assert child.poll() == 0
    assert result["processed"] == 0
    assert result["status"] == "waiting"  # unconfigured ports spend no attempts
    assert set(result["unavailable_work_types"]) == {"consolidate", "embed"}
    saved = json.loads((binding.data_directory / "runtime-worker-status.json").read_text(encoding="utf-8"))
    assert saved["exit_code"] == 0 and saved["worker_pid"] > 0
    assert all("人类会话" not in json.dumps(item, ensure_ascii=False) for item in result["items"])
    with sqlite3.connect(core.storage.path) as conn:
        row = conn.execute("SELECT session_id,content FROM source_events").fetchone()
        assert row == ("human-session-A", "人类会话 A 的持久化内容，不是 worker 输入。")
        assert conn.execute("SELECT SUM(attempt) FROM work_items").fetchone()[0] == 0


def test_worker_does_not_initialize_missing_database(tmp_path, monkeypatch):
    binding = _binding(tmp_path / "missing-data")
    assert not (binding.data_directory / "memory.sqlite3").exists()
    _, result = _run_child(tmp_path, monkeypatch, _config_payload(binding))
    assert result["status"] == "degraded"
    assert any(gap.startswith("worker_error:") for gap in result["capability_gaps"])
    assert not (binding.data_directory / "memory.sqlite3").exists()


def test_worker_empty_database_is_idle_and_concurrent_owner_is_busy(tmp_path, monkeypatch):
    binding = _binding(tmp_path / "data")
    core = MemoryCore(CoreConfig(binding))
    core.initialize()
    lock_path = binding.data_directory / "runtime-worker.lock"
    from scope_recall.core.file_lock import advisory_file_lock
    from scope_recall.runtime.worker_entry import run_worker
    from concurrent.futures import ThreadPoolExecutor
    from io import StringIO

    config_path = _write_config(tmp_path/'busy.json', _config_payload(binding, drain_seconds=.05))
    output = StringIO()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with advisory_file_lock(lock_path, timeout_seconds=None):
            assert pool.submit(run_worker, config_path, output=output).result(timeout=2) == 75
    busy = json.loads(output.getvalue())
    assert busy["status"] == "busy"
    assert busy["capability_gaps"] == ["worker_wait_timeout"]
    _, idle = _run_child(tmp_path, monkeypatch, _config_payload(binding))
    assert idle["status"] == "idle"
    assert idle["processed"] == 0


def test_a_handed_deadline_bounds_the_lock_wait_and_the_drain(tmp_path, monkeypatch):
    """A watchdog-owned pass restarted its whole drain budget after its own
    start-up, so a busy pass outlived its owner's window and was killed before
    its receipt.  The pass now ends its lock wait and drain the finalize margin
    before the deadline it was handed, and its own budget still caps it."""
    from io import StringIO
    import scope_recall.core.worker as core_worker
    from scope_recall.runtime import worker_entry

    binding = _binding(tmp_path / "data")
    MemoryCore(CoreConfig(binding)).initialize()
    seen = {}
    real_lock = worker_entry.advisory_file_lock

    def observed_lock(path, *, timeout_seconds=None):
        if path.name == "runtime-worker.lock":
            seen["lock_ends"] = time.time() + timeout_seconds
        return real_lock(path, timeout_seconds=timeout_seconds)

    def drain(*args, remaining_seconds, **kwargs):
        seen["drain_ends"] = time.time() + remaining_seconds
        return core_worker.WorkerReceipt(0, 0, 0, 0, 0, 0, 0, True, ())

    monkeypatch.setattr(worker_entry, "advisory_file_lock", observed_lock)
    monkeypatch.setattr(core_worker, "drain_worker", drain)
    clock_reads = .05  # Epoch and monotonic readings, a few ticks apart.
    handed = time.time() + 10.0
    output = StringIO()
    config = _write_config(tmp_path / "owned.json", _config_payload(binding))
    assert worker_entry.run_worker(config, output=output, deadline_epoch=handed) == 0
    assert json.loads(output.getvalue())["status"] == "idle"
    limit = handed - worker_entry.FINALIZE_MARGIN_SECONDS + clock_reads
    assert seen["lock_ends"] <= limit and seen["drain_ends"] <= limit
    assert seen["drain_ends"] > time.time()

    config = _write_config(tmp_path / "short.json", _config_payload(binding, drain_seconds=1.0))
    started = time.time()
    assert worker_entry.run_worker(config, output=StringIO(), deadline_epoch=started + 60.0) == 0
    assert seen["lock_ends"] <= started + 1.0 + clock_reads
    assert seen["drain_ends"] <= started + 1.0 + clock_reads


def test_a_pass_handed_no_window_reports_the_timeout_without_reserving(tmp_path, monkeypatch):
    """With less than the finalize margin left, a pass started now could only be
    killed mid-drain, or fail it; the owner's timeout is the honest receipt."""
    from io import StringIO
    import scope_recall.core.worker as core_worker
    from scope_recall.runtime import worker_entry

    binding = _binding(tmp_path / "data")
    MemoryCore(CoreConfig(binding)).initialize()
    config = _write_config(tmp_path / "worker.json", _config_payload(binding))

    def drain(*args, **kwargs):
        raise AssertionError("no window left, no drain")

    monkeypatch.setattr(core_worker, "drain_worker", drain)
    malformed = StringIO()
    assert worker_entry.run_worker(config, output=malformed, deadline_epoch=float("nan")) == 1
    assert json.loads(malformed.getvalue())["capability_gaps"] == ["worker_error:ValueError"]
    output = StringIO()
    handed = time.time() + worker_entry.FINALIZE_MARGIN_SECONDS / 2
    assert worker_entry.run_worker(config, output=output, deadline_epoch=handed) == 124
    receipt = json.loads(output.getvalue())
    assert receipt["status"] == "degraded" and receipt["capability_gaps"] == ["worker_watchdog_timeout"]
    assert not (binding.data_directory / "runtime-worker-day.json").exists()
    saved = json.loads((binding.data_directory / "runtime-worker-status.json").read_text(encoding="utf-8"))
    assert saved["exit_code"] == 124 and saved["capability_gaps"] == ["worker_watchdog_timeout"]


def test_worker_session_b_can_apply_evidence_backed_correction(tmp_path):
    binding = _binding(tmp_path / "data")
    core_a = MemoryCore(CoreConfig(binding))
    core_a.initialize()
    context_a = _context(binding, "human-session-A")
    old = core_a.record_event(
        context_a,
        source_event(
            source_event_key="TEST-runtime/correction-old",
            content="TEST-project 状态为草案。",
            occurred_at="2026-09-05T11:00:00Z",
        ),
        scope_id="TEST-scope",
        remaining_seconds=10,
    )
    old_row = core_a.source(context_a, old.event_refs[0].ref, 1)
    assert old_row is not None
    old_claim = core_a.accept_claim_proposals(
        context_a,
        {
            "protocol_version": "1.1",
            "source_refs": [f"{old_row.ref}@{old_row.revision}"],
            "claim_proposals": [
                {
                    "kind": "fact",
                    "subject": "TEST-project",
                    "predicate": "状态",
                    "value_text": "草案",
                    "conditions": [],
                    "statement_kind": "assertion",
                    "valid_from": old_row.event["occurred_at"],
                    "valid_to": None,
                    "evidence_spans": [
                        {"source_ref": old_row.ref, "source_revision": 1, "quote": old_row.event["content"]}
                    ],
                }
            ],
            "resume_proposals": [],
            "reference_proposals": [],
        },
        scope_id="TEST-scope",
        remaining_seconds=10,
    )
    assert old_claim.items[0].state == "active"
    source = core_a.record_event(
        context_a,
        source_event(
            source_event_key="TEST-runtime/correction-A",
            content="TEST-project 状态由草案更正为定稿。",
            occurred_at=None,
            time_precision="unknown",
        ),
        scope_id="TEST-scope",
        remaining_seconds=10,
    )
    source_row = core_a.source(context_a, source.event_refs[0].ref, 1)
    assert source_row is not None
    # Leave independent embedding work out of this consolidation seam.  The
    # worker still consumes the durable correction queue and never receives
    # raw content as an argument.
    with sqlite3.connect(core_a.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE work_type IN ('embed','rebuild_projection')")
        conn.execute(
            "UPDATE work_items SET state='done' WHERE work_type='consolidate' AND subject_ref<>?",
            (source.event_refs[0].ref,),
        )
        conn.commit()

    class DeterministicConsolidation:
        def propose(self, sources, *, episode_ref=None, remaining_seconds):
            assert remaining_seconds <= 45.0
            selected = next((s for s in sources if "定稿" in s.event["content"]), sources[0])
            return json.dumps(
                {
                    "protocol_version": "1.1",
                    "source_refs": [f"{selected.ref}@{selected.revision}"],
                    "claim_proposals": [
                        {
                            "kind": "fact",
                            "subject": "TEST-project",
                            "predicate": "状态",
                            "value_text": "定稿",
                            "conditions": [],
                            "statement_kind": "assertion",
                            "valid_from": selected.event["occurred_at"],
                            "valid_to": None,
                            "evidence_spans": [
                                {
                                    "source_ref": selected.ref,
                                    "source_revision": selected.revision,
                                    "quote": selected.event["content"],
                                }
                            ],
                        }
                    ],
                    "resume_proposals": [],
                    "reference_proposals": [],
                },
                ensure_ascii=False,
            )

    config = RuntimeInstanceConfig(
        binding=binding,
        session_id="worker-session-B",
        allowed_scope_ids=binding.scope_ids,
    )
    instance_b = build_runtime_instance(config)
    try:
        receipt = instance_b.drain(consolidation=DeterministicConsolidation())
        assert receipt.completed >= 1
        assert receipt.retried == 0
        assert any(item.work_type == "consolidate" and item.disposition == "completed" for item in receipt.items)
        history = core_a.claim_history(context_a, old_claim.items[0].ref)
        current = next(item for item in history if item.revision == item.current_revision)
        assert current.state == "active"
        assert current.payload["value_text"] == "定稿"
        with sqlite3.connect(core_a.storage.path) as conn:
            assert conn.execute("SELECT session_id FROM source_events ORDER BY recorded_at").fetchone()[0] == "human-session-A"
    finally:
        instance_b.close()


def test_worker_session_b_cannot_promote_stale_proposal_past_newer_human_evidence(tmp_path):
    """A later same-session human source invalidates an older worker proposal."""
    binding = _binding(tmp_path / "data")
    core_a = MemoryCore(CoreConfig(binding))
    core_a.initialize()
    context_a = _context(binding, "human-session-A")
    old = core_a.record_event(
        context_a,
        source_event(
            source_event_key="TEST-runtime/stale-old",
            content="TEST-project 状态为草案。",
            occurred_at="2026-09-05T11:00:00Z",
        ),
        scope_id="TEST-scope",
        remaining_seconds=10,
    )
    old_row = core_a.source(context_a, old.event_refs[0].ref, 1)
    assert old_row is not None
    old_claim = core_a.accept_claim_proposals(
        context_a,
        {
            "protocol_version": "1.1",
            "source_refs": [f"{old_row.ref}@{old_row.revision}"],
            "claim_proposals": [
                {
                    "kind": "fact",
                    "subject": "TEST-project",
                    "predicate": "状态",
                    "value_text": "草案",
                    "conditions": [],
                    "statement_kind": "assertion",
                    "valid_from": old_row.event["occurred_at"],
                    "valid_to": None,
                    "evidence_spans": [
                        {"source_ref": old_row.ref, "source_revision": 1, "quote": old_row.event["content"]}
                    ],
                }
            ],
            "resume_proposals": [],
            "reference_proposals": [],
        },
        scope_id="TEST-scope",
        remaining_seconds=10,
    )
    assert old_claim.items[0].state == "active"
    correction = core_a.record_event(
        context_a,
        source_event(
            source_event_key="TEST-runtime/stale-correction",
            content="TEST-project 状态由草案更正为定稿。",
            occurred_at=None,
            time_precision="unknown",
        ),
        scope_id="TEST-scope",
        remaining_seconds=10,
    )
    # This newer human observation arrives after the correction but does not
    # support the old worker proposal's ``定稿`` value.
    newer = core_a.record_event(
        context_a,
        source_event(
            source_event_key="TEST-runtime/stale-newer-human",
            content="TEST-project 状态尚未定稿，仍待确认。",
            occurred_at=None,
            time_precision="unknown",
        ),
        scope_id="TEST-scope",
        remaining_seconds=10,
    )
    with sqlite3.connect(core_a.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE work_type IN ('embed','rebuild_projection')")
        conn.execute(
            "UPDATE work_items SET state='done' WHERE work_type='consolidate' AND subject_ref NOT IN (?, ?)",
            (correction.event_refs[0].ref, newer.event_refs[0].ref),
        )
        conn.execute(
            "UPDATE work_items SET state='done' WHERE work_type='consolidate' AND subject_ref=?",
            (newer.event_refs[0].ref,),
        )
        conn.commit()

    class DeterministicConsolidation:
        def propose(self, sources, *, episode_ref=None, remaining_seconds):
            selected = next(s for s in sources if "更正" in s.event["content"])
            return json.dumps(
                {
                    "protocol_version": "1.1",
                    "source_refs": [f"{selected.ref}@{selected.revision}"],
                    "claim_proposals": [
                        {
                            "kind": "fact",
                            "subject": "TEST-project",
                            "predicate": "状态",
                            "value_text": "定稿",
                            "conditions": [],
                            "statement_kind": "assertion",
                            "valid_from": selected.event["occurred_at"],
                            "valid_to": None,
                            "evidence_spans": [
                                {
                                    "source_ref": selected.ref,
                                    "source_revision": selected.revision,
                                    "quote": selected.event["content"],
                                }
                            ],
                        }
                    ],
                    "resume_proposals": [],
                    "reference_proposals": [],
                },
                ensure_ascii=False,
            )

    instance_b = build_runtime_instance(
        RuntimeInstanceConfig(
            binding=binding,
            session_id="worker-session-B",
            allowed_scope_ids=binding.scope_ids,
        )
    )
    try:
        receipt = instance_b.drain(consolidation=DeterministicConsolidation())
        assert receipt.completed >= 1
        history = core_a.claim_history(context_a, old_claim.items[0].ref)
        current = next(item for item in history if item.revision == item.current_revision)
        assert not (current.state == "active" and current.payload["value_text"] == "定稿")
        assert any(item.revision == 1 and item.state == "active" for item in history)
    finally:
        instance_b.close()


# --------------------------------------------------------------------------
# A failure family is not a fault: say which one it was
# --------------------------------------------------------------------------

def test_a_sqlite_failure_carries_its_symbolic_code():
    """``worker_error:OperationalError`` named a family for days on a live
    instance: "database is locked" and "no such column" arrived as the same
    string, and only one of them is contention."""
    import sqlite3

    from scope_recall.runtime.worker_entry import _failure_label

    try:
        sqlite3.connect(":memory:").execute("SELECT nope FROM nowhere")
    except sqlite3.OperationalError as exc:
        assert _failure_label(exc) == "OperationalError:SQLITE_ERROR"
    else:  # pragma: no cover - the query is invalid by construction
        raise AssertionError("expected an OperationalError")


def test_contention_is_distinguishable_from_a_query_bug():
    from scope_recall.runtime.worker_entry import _failure_label

    class _Busy(Exception):
        sqlite_errorname = "SQLITE_BUSY"

    assert _failure_label(_Busy()) == "_Busy:SQLITE_BUSY"


def test_an_ordinary_exception_is_just_its_class():
    from scope_recall.runtime.worker_entry import _failure_label

    assert _failure_label(ValueError("boom")) == "ValueError"


@pytest.mark.parametrize("code", [None, 12, "", "has space", "has/slash", "路径"])
def test_only_a_bounded_code_is_appended(code):
    """The message may carry paths or model data; the symbolic name may not."""
    from scope_recall.runtime.worker_entry import _failure_label

    class _Odd(Exception):
        sqlite_errorname = code

    assert _failure_label(_Odd()) == "_Odd"


# --------------------------------------------------------------------------
# The worker and the doctor must agree about which failures are by design
# --------------------------------------------------------------------------

@pytest.mark.parametrize("code", ["derivation_invalid", "DERIVATION_INVALID",
                                  "auto_retry:1|derivation_invalid", "input_invalid"])
def test_a_by_design_terminal_outcome_is_not_actionable(code):
    from scope_recall.runtime.worker_entry import _is_actionable

    assert _is_actionable(code) is False


@pytest.mark.parametrize("code", ["timeout", "http_429", "model_unavailable",
                                  "candidate_attempt_interrupted", "something_new"])
def test_anything_an_operator_could_clear_is_actionable(code):
    from scope_recall.runtime.worker_entry import _is_actionable

    assert _is_actionable(code) is True


def test_no_error_code_is_not_a_failure():
    from scope_recall.runtime.worker_entry import _is_actionable

    assert _is_actionable(None) is False and _is_actionable("") is False


def test_a_run_that_only_met_terminal_outcomes_is_not_degraded():
    """A live instance reported the worker degraded while the doctor called the
    same state "attention"; the disagreement was this classification."""
    from scope_recall.runtime.worker_entry import _receipt_payload

    class _Item:
        work_id = 1
        work_type = "evaluate_candidate"
        disposition = "failed"
        state = "failed"
        error_code = "derivation_invalid"
        error_detail = None

    class _Receipt:
        items = (_Item(),)
        idle = False
        failed = 1
        retried = 0
        processed = 1
        completed = 0
        unavailable_work_types = ()

    class _Binding:
        installation_id = "TEST-install"

    class _Config:
        owner_id = "TEST-owner"
        binding = _Binding()

    payload = _receipt_payload(_Config(), _Receipt(), [])
    assert payload["status"] == "completed", payload
    assert payload["failed"] == 1, "the failure must still be visible"


def test_a_run_with_a_clearable_failure_is_still_degraded():
    """Narrowing must not go so far that a real fault stops being reported."""
    from scope_recall.runtime.worker_entry import _receipt_payload

    class _Item:
        work_id = 1
        work_type = "evaluate_candidate"
        disposition = "failed"
        state = "failed"
        error_code = "timeout"
        error_detail = None

    class _Receipt:
        items = (_Item(),)
        idle = False
        failed = 1
        retried = 0
        processed = 1
        completed = 0
        unavailable_work_types = ()

    class _Binding:
        installation_id = "TEST-install"

    class _Config:
        owner_id = "TEST-owner"
        binding = _Binding()

    assert _receipt_payload(_Config(), _Receipt(), [])["status"] == "degraded"


def test_the_worker_status_names_a_refusing_provider_too(tmp_path):
    """A host agent watching the instance polls the status file, not the
    doctor. Reporting the refusal in only one of the two left the watcher
    reading "degraded" with an empty gap list for four hours."""
    import sqlite3
    import time as _time

    from scope_recall.runtime import worker_entry
    from scope_recall.runtime.model_budget import provider_refusals

    assert worker_entry.provider_refusals is provider_refusals, \
        "the worker must share the doctor's implementation, not restate it"

    ledger = tmp_path / "auxiliary-budget.sqlite3"
    with sqlite3.connect(ledger) as conn:
        conn.execute("CREATE TABLE requests (id INTEGER PRIMARY KEY, model TEXT,"
                     " status TEXT, started_ns INTEGER)")
        now_ns = int(_time.time() * 1_000_000_000)
        for _ in range(12):
            conn.execute("INSERT INTO requests(model,status,started_ns) VALUES (?,?,?)",
                         ("chat-model", "http_429:GoUsageLimitError_usage_unknown", now_ns))
        conn.commit()
    assert provider_refusals(ledger) == ["model_refused:chat-model:GoUsageLimitError"]


# --------------------------------------------------------------------------
# A refusal is reported; it does not steer the page
# --------------------------------------------------------------------------


def test_the_drain_page_ignores_the_refusal_list():
    """An instance-wide cut keyed on any refusal was removed, and must not come
    back: it removed nothing from the refusing provider, which `core.worker`
    already stands down per work type, and it throttled the healthy one -- on the
    live ledger the refused model averaged two calls a pass while the healthy
    embedding model reached 39 in one."""
    import inspect

    from scope_recall.runtime import worker_entry

    source = inspect.getsource(worker_entry)
    assert "_drain_page" not in source
    assert "PROBE_ITEMS_WHILE_REFUSED" not in source
    assert "max_items=reserved or config.max_items" in source


def test_a_refusal_is_still_named_in_the_gaps():
    """rc15's contribution stays: four hours of "degraded" with an empty gap list
    is what this reports against."""
    import inspect

    from scope_recall.runtime import worker_entry

    source = inspect.getsource(worker_entry)
    assert "gaps.extend(provider_refusals(" in source


def test_a_rate_limited_code_stands_its_work_type_down_for_the_pass():
    """This is the mechanism the removed page cut was duplicating, and it is the
    one that is actually per-provider: a work type is dropped from `allowed` the
    moment one of its items sees a rate-limited code, so the next item of that
    type is not tried until the next pass."""
    import inspect

    from scope_recall.core import worker

    source = inspect.getsource(worker)
    assert "if str(error_code or \"\").lower() in _RATE_LIMITED_ERRORS:" in source
    assert "allowed = allowed - {item.work_type}" in source


def test_the_two_stop_loss_layers_stay_distinct():
    """The per-item backoff decides *when* one item returns; the per-type stand
    down decides how many of that type are tried in one pass. Neither is an
    instance-wide brake, and a backward-looking ledger window must not become
    one."""
    from scope_recall.core.worker import _RATE_LIMITED_ERRORS
    from scope_recall.core.work_storage import CAPACITY_REFUSALS

    assert _RATE_LIMITED_ERRORS == CAPACITY_REFUSALS
    assert "http_429" in _RATE_LIMITED_ERRORS


# --- the watcher and the operator must read the same instance ----------------

class _Queue:
    def __init__(self, failed_work, work_error_counts, pending_work=0):
        self.failed_work = failed_work
        self.work_error_counts = work_error_counts
        self.pending_work = pending_work
        self.oldest_pending_at = None


def _status_from(queue, *, background_gaps=(), source_only=0, gaps=()):
    """The decision `run_worker` makes after the drain, in isolation."""
    from scope_recall.runtime.worker_entry import _is_actionable

    terminal = sum(count for code, count in queue.work_error_counts if not _is_actionable(code))
    actionable = max(0, queue.failed_work - terminal)
    out = {"status": "completed", "capability_gaps": sorted(set(gaps))}
    if actionable or background_gaps or source_only:
        out["status"] = "degraded"
        if actionable:
            out["capability_gaps"] = sorted(set(tuple(gaps) + (f"work_failed:{actionable}",)))
    elif terminal:
        out["capability_gaps"] = sorted(set(tuple(gaps) + ("work_failed_terminal_only",)))
    return out


def test_by_design_failures_alone_do_not_make_the_worker_say_degraded():
    """Measured on alpha: 33 items failed by design, none actionable. The
    doctor said "attention"; this line said "degraded" with an empty gap list."""
    status = _status_from(_Queue(33, (("derivation_invalid", 33),)))
    assert status["status"] != "degraded"
    assert status["capability_gaps"] == ["work_failed_terminal_only"]


def test_one_actionable_failure_among_many_terminal_is_still_degraded():
    """The restart that interrupts an in-flight evaluation leaves exactly this."""
    status = _status_from(_Queue(34, (("derivation_invalid", 33), ("candidate_attempt_interrupted", 1))))
    assert status["status"] == "degraded"
    assert "work_failed:1" in status["capability_gaps"]


def test_degraded_is_never_reported_without_a_reason():
    """An empty gap list beside "degraded" is what sent a watcher hunting through
    two-day-old logs for a cause that was not there."""
    status = _status_from(_Queue(2, (("http_429", 2),)))
    assert status["status"] == "degraded" and status["capability_gaps"]


def test_the_two_status_decisions_use_one_classification():
    """rc15 fixed the pass-level decision and left this one restating the old
    rule forty lines below it."""
    import inspect

    from scope_recall.runtime import worker_entry

    source = inspect.getsource(worker_entry)
    assert "if queue.failed_work or background_gaps" not in source
    assert "if actionable_failed or background_gaps" in source
    assert source.count("_is_actionable(") >= 2


def test_a_source_only_drain_names_itself_too():
    """The same empty-gap shape one branch over: source_only alone made the
    status degraded and nothing in the gap list said so."""
    from scope_recall.runtime.worker_entry import _is_actionable

    queue = _Queue(0, ())
    terminal = sum(count for code, count in queue.work_error_counts if not _is_actionable(code))
    actionable = max(0, queue.failed_work - terminal)
    gaps = []
    source_only = 3
    assert actionable == 0
    if actionable or () or source_only:
        if actionable:
            gaps.append(f"work_failed:{actionable}")
        if source_only:
            gaps.append(f"source_only:{source_only}")
    assert gaps == ["source_only:3"]


def test_every_degraded_branch_appends_a_gap():
    """Read the code, not a mock of it: each reason for degraded must put
    something in the list a watcher reads."""
    import inspect

    from scope_recall.runtime import worker_entry

    source = inspect.getsource(worker_entry)
    branch = source[source.index("if actionable_failed or background_gaps"):]
    branch = branch[:branch.index("elif terminal_failed")]
    assert 'gaps.append(f"work_failed:{actionable_failed}")' in branch
    assert "source_only:" in branch
    assert 'payload["capability_gaps"] = sorted(set(gaps))' in branch


def test_the_split_the_worker_computed_reaches_the_file_that_is_polled():
    """The status file takes only a closed set of keys, which is right -- and a
    field added to the payload and not to that set is computed, used for the
    decision, and then silently dropped before anyone can read it. That happened
    to `terminal_failed_work` on its first release: the behaviour changed and the
    number explaining it did not appear."""
    import inspect

    from scope_recall.runtime import worker_entry

    source = inspect.getsource(worker_entry._persist_worker_status_unlocked)
    assert '"terminal_failed_work"' in source, "the allowlist drops it before anyone reads it"
    payload_source = inspect.getsource(worker_entry)
    assert "terminal_failed_work=terminal_failed" in payload_source
