"""Real SQLite diagnostic and durable replay boundaries; no models or production data."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import subprocess
import sys

import pytest

from scope_recall._version import __version__
from scope_recall.contracts import ContractError
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.admission import AdmissionPolicy
from scope_recall.core.recall_policy import SPACE_ID
from scope_recall.maintenance import doctor
from test_autonomous_admission import app_at, capture


def test_admission_counts_show_current_visible_sources_and_clear_after_activation(tmp_path):
    app, ctx = app_at(tmp_path, AdmissionPolicy(max_pending_work=2, important_reserve=0))
    capture(app, ctx, 'TEST-full', 'TEST substantive first source')
    capture(app, ctx, 'TEST-wait', 'TEST substantive deferred source')
    capture(app, ctx, 'TEST-ack', '好的')
    capture(app, ctx, 'TEST-ack', '收到', source_revision=2)
    hidden = capture(app, ctx, 'TEST-suppressed', '谢谢')
    blocked = capture(app, ctx, 'TEST-blocked', '谢谢')
    with app.storage.write(ctx) as tx:
        tx._check(write=True).execute('UPDATE source_events SET suppressed=1 WHERE event_id=?', (hidden.event_refs[0].ref,))
        tx._check(write=True).execute('UPDATE source_events SET read_blocked=1 WHERE event_id=?', (blocked.event_refs[0].ref,))
    before = app.storage.path.read_bytes()
    status = app.status(ctx)
    assert status.source_only_sources == 1 and status.deferred_sources == 1
    assert status.oldest_deferred_at is not None
    assert app.storage.path.read_bytes() == before
    with app.storage.write(ctx) as tx:
        while work := tx.work.claim_next('TEST-worker', app.clock.utc_now(), lease_seconds=10):
            for item in work:
                tx.work.complete(item.work_id, item.lease_token, item.lease_owner, now=app.clock.utc_now())
    assert len(app.resume_deferred(ctx)) == 1
    assert app.status(ctx).deferred_sources == 0
    assert app.status(ctx).oldest_deferred_at is None


def test_admission_diagnostics_preserve_scope_project_and_branch_boundaries(tmp_path):
    app, ctx = app_at(tmp_path)
    binding = replace(ctx.binding, data_directory=tmp_path/'TEST-scopes', scope_ids=frozenset({'TEST-scope', 'TEST-other'}))
    app = MemoryCore(CoreConfig(binding)); app.initialize()
    ctx = replace(ctx, binding=binding)
    capture(app, ctx, 'TEST-common', '好的')
    project = replace(ctx, project_id='TEST-project', branch_id='TEST-branch')
    capture(app, project, 'TEST-private', '收到')
    other = replace(ctx, allowed_scope_ids=frozenset({'TEST-other'}))
    from v11_support import source_event
    app.record_event(other, source_event(source_event_key='TEST-other-source', content='谢谢'), scope_id='TEST-other')
    assert app.status(ctx).source_only_sources == 1
    assert app.status(project).source_only_sources == 2
    assert app.status(replace(project, branch_id='TEST-other-branch')).source_only_sources == 1
    with app.storage.read(ctx) as tx:
        assert tx.status(include_all_projects=True, include_admission=True).source_only_sources == 2
        statements = []
        tx._check().set_trace_callback(statements.append)
        assert tx.status().source_only_sources is None
        assert not any('json_extract' in statement for statement in statements)


def test_backlog_age_does_not_reset_when_retry_available_at_moves(tmp_path):
    app, ctx = app_at(tmp_path)
    capture(app, ctx, 'TEST-old', 'TEST pending durable fact')
    with sqlite3.connect(app.storage.path) as db:
        original = db.execute('SELECT persisted_at FROM source_events').fetchone()[0]
        db.execute("UPDATE work_items SET available_at='2099-01-01T00:00:00Z'")
    assert app.status(ctx).oldest_pending_at == original


@pytest.mark.parametrize('length', [30, 66000])
def test_host_replay_reads_first_capture_without_writes_and_respects_visibility(tmp_path, length):
    app, ctx = app_at(tmp_path)
    ctx = replace(ctx, project_id='TEST-project', branch_id='TEST-branch')
    saved = capture(app, ctx, 'TEST-replay', '中文内容' * (length//4))
    before = app.storage.path.read_bytes()
    source = app.source_by_event_key(ctx, 'TEST-replay', remaining_seconds=1)
    assert source is not None and source.ref == saved.event_refs[0].ref
    assert source.event['recorded_at'] == '2026-09-05T12:00:00Z'
    assert source.session_id == ctx.session_id and source.project_id == ctx.project_id
    assert app.source_by_event_key(replace(ctx, project_id='TEST-other'), 'TEST-replay') is None
    assert app.source_by_event_key(replace(ctx, branch_id='TEST-other'), 'TEST-replay') is None
    assert app.source_by_event_key(ctx, 'TEST-absent') is None
    assert app.storage.path.read_bytes() == before
    with pytest.raises(ContractError, match='DEADLINE_EXCEEDED'):
        app.source_by_event_key(ctx, 'TEST-replay', remaining_seconds=0)
    with app.storage.write(ctx) as tx:
        tx._check(write=True).execute('UPDATE source_events SET read_blocked=1')
    assert app.source_by_event_key(ctx, 'TEST-replay') is None


@pytest.mark.parametrize('key', ['', ' ', '\x00', 'x'*513, 3])
def test_host_replay_rejects_invalid_identity_keys(tmp_path, key):
    app, ctx = app_at(tmp_path)
    with pytest.raises(ContractError, match='INPUT_INVALID'):
        app.source_by_event_key(ctx, key)


def _doctor_app(tmp_path, monkeypatch):
    app, ctx = app_at(tmp_path, AdmissionPolicy(max_pending_work=2, important_reserve=0))
    (ctx.binding.data_directory/'installation.json').write_text('{}', encoding='utf-8')
    monkeypatch.setattr(doctor, '_load_binding', lambda *args: (ctx.binding, ctx.binding.data_directory))
    monkeypatch.setattr(doctor, '_hermes_data_dir', lambda root: ctx.binding.data_directory)
    return app, ctx


@pytest.mark.parametrize('version,metadata,expected_gap', [
    ('3.0.0', '3.0.0', 'python_package_version_mismatch'),
    (__version__, '3.0.0', 'python_package_metadata_mismatch'),
    (__version__, __version__, None),
])
def test_doctor_checks_actual_target_version_not_import_success(tmp_path, monkeypatch, version, metadata, expected_gap):
    app, ctx = _doctor_app(tmp_path, monkeypatch)
    def run(command, **kwargs):
        assert command[1:3] == ['-I', '-B']
        return subprocess.CompletedProcess(command, 0, json.dumps(dict(source='installed', version=version,
                    path=str(tmp_path/'site-packages'/'scope_recall'/'_version.py'), distribution_version=metadata)), '')
    monkeypatch.setattr(doctor.subprocess, 'run', run)
    before = app.storage.path.read_bytes()
    result = doctor.run_doctor(host='hermes', instance_root=ctx.binding.data_directory, python_executable=sys.executable)
    assert result.package_version == version and result.expected_package_version == __version__
    assert result.package_ok is (expected_gap is None)
    if expected_gap:
        assert expected_gap in result.capability_gaps
    assert app.storage.path.read_bytes() == before


def _write_runtime_config(ctx, **changes):
    binding = ctx.binding
    raw = {
        'binding': {'agent_id': binding.agent_id, 'installation_id': binding.installation_id,
                    'data_directory': str(binding.data_directory), 'scope_ids': sorted(binding.scope_ids),
                    'test_mode': binding.test_mode},
        'session_id': 'TEST-doctor-session',
        'allowed_scope_ids': sorted(binding.scope_ids),
        'auxiliary': {'external_embedding': True, 'external_consolidation': False,
                      'embedding': {'credential_env': 'TEST_EMBED_KEY'}},
        'vector': {'backend': 'lancedb', 'storage_dir': str(binding.data_directory/'vectors'/SPACE_ID),
                   'table_name': 'TEST_vectors', 'dimensions': 3072},
        **changes,
    }
    (binding.data_directory/'runtime-config.json').write_text(json.dumps(raw), encoding='utf-8')


def test_doctor_names_vector_recall_configured_without_a_threshold(tmp_path, monkeypatch):
    """No installer writes vector_threshold, and without it recall refuses every
    vector hit while sources are still embedded; the doctor has to say so."""
    app, ctx = _doctor_app(tmp_path, monkeypatch)
    _write_runtime_config(ctx)
    before = app.storage.path.read_bytes()
    result = doctor.run_doctor(host='hermes', instance_root=ctx.binding.data_directory)
    assert 'vector_threshold_unconfigured' in result.capability_gaps
    [check] = [item for item in result.checks if item['name'] == 'vector_threshold']
    assert check['result'] == 'unconfigured'
    for fragment in ('runtime-config.json', 'gemini-embedding-2', 'no vector_threshold', 'lexical only'):
        assert fragment in check['detail']
    assert app.storage.path.read_bytes() == before
    # Worth a look, not a fault: recall still answers lexically.
    alone = doctor.DoctorReport(host='hermes', status='degraded', capability_gaps=['vector_threshold_unconfigured'])
    doctor._classify_status(alone)
    assert alone.status == 'attention'

    _write_runtime_config(ctx, vector_threshold=0.653189984350642)
    result = doctor.run_doctor(host='hermes', instance_root=ctx.binding.data_directory)
    assert 'vector_threshold_unconfigured' not in result.capability_gaps
    assert {'name': 'vector_threshold', 'result': 'configured', 'detail': '0.653189984350642'} in result.checks

    # Without an approved embedding route no vector hit exists to refuse.
    _write_runtime_config(ctx, auxiliary={'external_embedding': False, 'external_consolidation': False,
                                          'embedding': {'credential_env': 'TEST_EMBED_KEY'}})
    result = doctor.run_doctor(host='hermes', instance_root=ctx.binding.data_directory)
    assert 'vector_threshold_unconfigured' not in result.capability_gaps
    assert not [item for item in result.checks if item['name'] == 'vector_threshold']

    # A config the hosts would refuse cannot answer the question; the doctor still finishes.
    (ctx.binding.data_directory/'runtime-config.json').write_text('{}', encoding='utf-8')
    result = doctor.run_doctor(host='hermes', instance_root=ctx.binding.data_directory)
    assert {'name': 'vector_threshold', 'result': 'invalid', 'detail': 'ValueError'} in result.checks
    assert 'vector_threshold_unconfigured' not in result.capability_gaps


def test_doctor_names_answers_cut_off_at_the_output_limit(tmp_path, monkeypatch):
    """beta's consolidation model reasoned into its max_tokens and most answers
    were cut off; only recent_work_errors showed it, sixteen rows at a time."""
    from datetime import datetime, timedelta, timezone

    app, ctx = _doctor_app(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    with sqlite3.connect(app.storage.path) as conn:
        for n in range(doctor.OUTPUT_TRUNCATION_ALERT):
            conn.execute("INSERT INTO work_error_details(work_id,lease_token,stage,error_code,error_field,recorded_at) VALUES (?,?,?,?,?,?)",
                         (n + 1, 1, 'prepare_or_model', 'DERIVATION_INVALID', 'model_output_truncated', (now - timedelta(minutes=n)).isoformat()))
    before = app.storage.path.read_bytes()
    result = doctor.run_doctor(host='hermes', instance_root=ctx.binding.data_directory)
    assert result.recent_output_truncations == doctor.OUTPUT_TRUNCATION_ALERT
    assert 'model_output_truncated' in result.capability_gaps and result.status == 'degraded'
    [check] = [item for item in result.checks if item['name'] == 'model_output']
    assert 'max_output_tokens' in check['detail'] and 'thinking' in check['detail']
    assert app.storage.path.read_bytes() == before

    with sqlite3.connect(app.storage.path) as conn:
        conn.execute("UPDATE work_error_details SET recorded_at=?", ((now - timedelta(hours=2)).isoformat(),))
    result = doctor.run_doctor(host='hermes', instance_root=ctx.binding.data_directory)
    assert result.recent_output_truncations == 0 and 'model_output_truncated' not in result.capability_gaps


def test_doctor_exposes_deferred_work_even_when_no_job_was_enqueued(tmp_path, monkeypatch):
    app, ctx = _doctor_app(tmp_path, monkeypatch)
    capture(app, ctx, 'TEST-full', 'TEST substantive first source')
    capture(app, ctx, 'TEST-wait', 'TEST substantive second source')
    capture(app, ctx, 'TEST-cheap', '好的')
    before = app.storage.path.read_bytes()
    result = doctor.run_doctor(host='hermes', instance_root=ctx.binding.data_directory)
    assert result.source_only_sources == result.deferred_sources == 1
    assert result.oldest_deferred_at is not None
    assert 'source_processing_deferred' in result.capability_gaps
    assert result.to_dict()['deferred_sources'] == 1
    assert app.storage.path.read_bytes() == before


def test_doctor_reports_the_footprint_the_growth_and_a_budget(tmp_path, monkeypatch):
    """Bytes on disk and the week's growth are what an operator needs to see a
    store outgrow its disk before it does; a budget makes that a gap."""
    app, ctx = _doctor_app(tmp_path, monkeypatch)
    for index in range(3):
        capture(app, ctx, f'TEST-growth/{index}', f'TEST growth {index}')
    moment = datetime.now(timezone.utc)
    with sqlite3.connect(app.storage.path) as conn:
        refs = [row[0] for row in conn.execute('SELECT event_id FROM source_events ORDER BY event_id')]
        for ref, age in zip(refs, (timedelta(hours=2), timedelta(days=3), timedelta(days=30))):
            conn.execute('UPDATE source_events SET persisted_at=? WHERE event_id=?', ((moment - age).isoformat(), ref))
        conn.commit()
    (ctx.binding.data_directory / 'vectors').mkdir()
    (ctx.binding.data_directory / 'vectors' / 'TEST.bin').write_bytes(b'x' * 1024)
    _write_runtime_config(ctx, storage_budget_bytes=512)
    result = doctor.run_doctor(host='hermes', instance_root=ctx.binding.data_directory)
    assert (result.sources_last_24h, result.sources_last_7d) == (1, 2)
    assert result.store_bytes == app.storage.path.stat().st_size and result.vector_bytes == 1024
    assert result.storage_budget_bytes == 512 and 'storage_budget_exceeded' in result.capability_gaps
    footprint = next(item for item in result.checks if item['name'] == 'storage_footprint')
    assert footprint['result'] == 'over_budget' and 'sources +1 in 24h, +2 in 7d' in footprint['detail']
    _write_runtime_config(ctx)
    result = doctor.run_doctor(host='hermes', instance_root=ctx.binding.data_directory)
    assert result.storage_budget_bytes == 0 and 'storage_budget_exceeded' not in result.capability_gaps
    assert result.to_dict()['index_metadata']['tool_output_retention_days'] == 180
