"""Autonomous recovery remains finite, scoped, and honest about failures."""
from dataclasses import replace
from io import StringIO
import json
import sqlite3

import pytest

from scope_recall.core.worker import WorkerConfig, drain_worker
from scope_recall.runtime.instance import RuntimeInstanceConfig
from scope_recall.runtime.worker_entry import (_reserve_daily_work, persist_worker_status,
                                              _now, run_worker)
from test_v11_worker import worker_app, app, capture, work_rows, _mark_embed_done
from test_runtime_worker_entry import _binding, _config_payload, _write_config
from scope_recall.core import CoreConfig, MemoryCore


def test_explicit_retry_preserves_automatic_retry_ceiling():
    from scope_recall.core.work_storage import _auto_count
    assert _auto_count('operator_retry:manual|prior:auto_retry:2|model_unavailable') == 2


def test_worker_metadata_refuses_nonregular_target(tmp_path):
    binding = _binding(tmp_path/'data')
    binding.data_directory.mkdir()
    target = binding.data_directory/'runtime-worker-status.json'
    target.mkdir()
    cfg = RuntimeInstanceConfig.from_mapping(_config_payload(binding))
    with pytest.raises(ValueError, match='worker_metadata_not_regular'):
        persist_worker_status(cfg, {'status': 'idle'}, started_at=_now(), exit_code=0)
    assert target.is_dir()


class Offline:
    def __init__(self):
        self.calls = 0

    def propose(self, *args, **kwargs):
        self.calls += 1
        raise ConnectionError("TEST transient network failure")


def test_transient_auto_recovery_is_cooled_and_has_lifetime_ceiling(worker_app):
    core, ctx, clock = worker_app
    capture(core, ctx, "TEST 需要可靠记住的长期决定。")
    _mark_embed_done(core)
    model = Offline()
    for iso in ("12:00:00", "12:00:03", "12:00:10"):
        clock.advance(iso=f"2026-09-06T{iso}Z")
        core.drain_worker(ctx, consolidation=model, max_items=1, remaining_seconds=5)
    assert model.calls == 3 and work_rows(core)[0][3] == "failed"
    clock.advance(iso="2026-09-06T12:59:59Z")
    assert core.drain_worker(ctx, consolidation=model, max_items=1, remaining_seconds=5).processed == 0
    for iso in ("13:01:00", "14:02:00"):
        clock.advance(iso=f"2026-09-06T{iso}Z")
        receipt = core.drain_worker(ctx, consolidation=model, max_items=1, remaining_seconds=5)
        assert receipt.recovered == 1 and receipt.failed == 1
    clock.advance(iso="2026-09-08T00:00:00Z")
    assert core.drain_worker(ctx, consolidation=model, max_items=1, remaining_seconds=5).processed == 0
    assert model.calls == 5 and work_rows(core)[0][4] == 5
    assert "auto_retry:2|" in work_rows(core)[0][6]


def test_permanent_failure_does_not_hide_later_recoverable_work(worker_app):
    core, ctx, clock = worker_app
    for n in range(4):
        capture(core, ctx, f"TEST durable evidence {n}")
    _mark_embed_done(core)
    with sqlite3.connect(core.storage.path) as db:
        db.execute("UPDATE work_items SET state='failed',attempt=3,last_error_code='derivation_invalid' WHERE work_type='consolidate'")
        last = db.execute("SELECT MAX(work_id) FROM work_items WHERE work_type='consolidate'").fetchone()[0]
        db.execute("UPDATE work_items SET last_error_code='model_unavailable' WHERE work_id=?", (last,))
    clock.advance(iso="2026-09-07T12:00:00Z")
    with core.storage.write(ctx) as tx:
        assert tx.work.recover_transient_failures(now=clock.utc_now(), allowed_work_types=frozenset({'consolidate'}), limit=1) == 1
        assert tx.work.read_state(last) == 'pending'


def test_purge_claim_has_priority_over_old_enrichment(worker_app):
    core, ctx, clock = worker_app
    capture(core, ctx, "TEST queued enrichment")
    with core.storage.write(ctx) as tx:
        tx.work.enqueue('purge', 'delete-test:TEST-scope', 1, available_at=clock.utc_now())
        claimed = tx.work.claim_next('test', clock.utc_now(), lease_seconds=60)
    assert claimed[0].work_type == 'purge'


def test_recovery_rejects_revoked_source_and_wrong_project(worker_app):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST scoped durable decision")
    _mark_embed_done(core)
    with sqlite3.connect(core.storage.path) as db:
        db.execute("UPDATE work_items SET state='failed',attempt=3,last_error_code='model_unavailable' WHERE work_type='consolidate'")
    clock.advance(iso="2026-09-07T12:00:00Z")
    with core.storage.write(replace(ctx, project_id='another-project')) as tx:
        assert tx.work.recover_transient_failures(now=clock.utc_now(), allowed_work_types=frozenset({'consolidate'})) == 0
    with sqlite3.connect(core.storage.path) as db:
        db.execute("UPDATE source_events SET read_blocked=1 WHERE event_id=?", (source.ref,))
    with core.storage.write(ctx) as tx:
        assert tx.work.recover_transient_failures(now=clock.utc_now(), allowed_work_types=frozenset({'consolidate'})) == 0
    assert work_rows(core)[0][3] == 'obsolete'


def test_failed_work_is_reported_even_with_no_pending(worker_app, monkeypatch):
    from scope_recall.maintenance import doctor
    core, ctx, _ = worker_app
    capture(core, ctx, "TEST known failed work")
    with sqlite3.connect(core.storage.path) as db:
        db.execute("UPDATE work_items SET state='failed',last_error_code='derivation_invalid'")
    (ctx.binding.data_directory/'installation.json').write_text('{}', encoding='utf-8')
    monkeypatch.setattr(doctor, '_load_binding', lambda *args: (ctx.binding, ctx.binding.data_directory))
    monkeypatch.setattr(doctor, '_hermes_data_dir', lambda root: ctx.binding.data_directory)
    result = doctor.run_doctor(host='hermes', instance_root=ctx.binding.data_directory)
    assert result.pending_work == 0 and result.failed_work == 2
    # The gap says which kind of failure since terminal classification landed:
    # 'work_failed_terminal_only' is what an operator cannot act on, and it is
    # the difference between a fault and a by-design refusal.
    assert result.status == 'degraded' and 'work_failed_terminal_only' in result.capability_gaps


def test_idle_worker_receipt_still_reports_terminal_failures(worker_app, tmp_path):
    core, ctx, _ = worker_app
    capture(core, ctx, 'TEST durable failed decision')
    with sqlite3.connect(core.storage.path) as db:
        db.execute("UPDATE work_items SET state='failed',last_error_code='derivation_invalid'")
    path = _write_config(tmp_path/'worker.json', _config_payload(ctx.binding,
                         project_id=ctx.project_id, branch_id=ctx.branch_id))
    output = StringIO()
    assert run_worker(path, output=output) == 0
    result = json.loads(output.getvalue())
    assert result['processed'] == 0 and result['failed_work'] == 2
    # Still reported -- that is what this test is named for -- but no longer
    # "degraded". Both failures are by design, the doctor calls the same instance
    # "attention" for the same reason, and a worker that disagreed with it while
    # offering no gap is what sent a watcher through two-day-old logs.
    assert result['status'] != 'degraded'
    assert result['terminal_failed_work'] == 2
    assert 'work_failed_terminal_only' in result['capability_gaps']
    saved = json.loads((ctx.binding.data_directory/'runtime-worker-status.json').read_text())
    assert saved['failed_work'] == 2 and saved['status'] != 'degraded'
    assert 'work_failed_terminal_only' in saved['capability_gaps']


def test_daily_processing_cap_never_resets_model_budget_and_resets_by_day(tmp_path, monkeypatch):
    binding = _binding(tmp_path/'data')
    binding.data_directory.mkdir()
    cfg = RuntimeInstanceConfig.from_mapping(_config_payload(binding, max_items=2, daily_work_limit=3))
    assert _reserve_daily_work(cfg)[2] == 2
    assert _reserve_daily_work(cfg)[2] == 1
    assert _reserve_daily_work(cfg)[2] == 0
    # Patch the namespace the function actually reads, not a module path
    # resolved by name: under the full gate run this file's import of
    # worker_entry and the string target stopped referring to the same
    # globals, so the clock moved for the module and not for the caller and
    # the day never rolled over. Order-dependent green is not green.
    monkeypatch.setitem(_reserve_daily_work.__globals__, '_now', lambda: '2030-01-01T00:00:00Z')
    assert _reserve_daily_work(cfg)[2] == 2
    assert not (binding.data_directory/'auxiliary-budget.sqlite3').exists()


def test_worker_receipt_keeps_last_success_and_excludes_arbitrary_text(tmp_path):
    binding = _binding(tmp_path/'data')
    binding.data_directory.mkdir()
    cfg = RuntimeInstanceConfig.from_mapping(_config_payload(binding))
    start = _now()
    persist_worker_status(cfg, dict(status='completed', completed=2, stderr='SECRET', model_output='SECRET'), started_at=start, exit_code=0)
    path = binding.data_directory/'runtime-worker-status.json'
    first = json.loads(path.read_text())
    persist_worker_status(cfg, dict(status='degraded', failed=1), started_at=_now(), exit_code=1)
    second = json.loads(path.read_text())
    assert second['last_success_at'] == first['last_success_at']
    assert second['exit_code'] == 1 and 'SECRET' not in path.read_text()


def test_daily_limit_worker_leaves_enrichment_unclaimed(tmp_path):
    binding = _binding(tmp_path/'data')
    core = MemoryCore(CoreConfig(binding)); core.initialize()
    cfg = RuntimeInstanceConfig.from_mapping(_config_payload(binding, daily_work_limit=1))
    _reserve_daily_work(cfg)
    path = _write_config(tmp_path/'worker.json', _config_payload(binding, daily_work_limit=1))
    output = StringIO()
    assert run_worker(path, output=output) == 0
    result = json.loads(output.getvalue())
    assert 'daily_queue_budget' in result['capability_gaps']
    assert result['daily_queue_used'] == 1
