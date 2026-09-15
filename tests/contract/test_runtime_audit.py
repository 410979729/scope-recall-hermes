"""Focused regressions for the bounded host/cost audit; no network calls."""
from dataclasses import replace
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import sqlite3
import threading

import pytest

from scope_recall.adapters.hermes.runtime_wiring import HermesHostRuntime
from scope_recall.adapters.codex.runtime_wiring import CodexHostRuntime
from scope_recall.adapters.runtime_wiring import launch_audience_worker
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.runtime.instance import RuntimeInstanceConfig, build_runtime_instance
from scope_recall.runtime import worker_entry
from scope_recall.file_lock import advisory_file_lock
from scope_recall.adapters.models import AuxiliaryModelError
from scope_recall.core.worker import _model_exception_outcome
from test_runtime_worker_entry import _binding, _config_payload, _write_config
from test_runtime_auxiliary import _runtime_config, FakeTransport
from scope_recall.runtime.auxiliary import build_auxiliary_runtime
from test_v11_worker import worker_app, app, capture, work_rows, _mark_embed_done


def _host(tmp_path, host_type=HermesHostRuntime):
    binding = replace(_binding(tmp_path/'data'), scope_ids=frozenset({'TEST-a','TEST-b','TEST-c'}))
    cfg = RuntimeInstanceConfig.from_mapping(_config_payload(binding))
    path = _write_config(tmp_path/'worker.json', _config_payload(binding))
    core = MemoryCore(CoreConfig(binding)); core.initialize()
    host = host_type(core=core, _runtime=SimpleNamespace(config=cfg, close=Mock()), _config_path=path)
    calls = []
    def launch(path, **options):
        calls.append((json.loads(path.read_text()), options, path))
        worker = Mock(pid=100+len(calls)); worker.poll.return_value = None
        worker.communicate.return_value = ('','')
        return worker
    return host, calls, launch


@pytest.mark.parametrize('host_type',[HermesHostRuntime, CodexHostRuntime])
def test_audience_wake_is_not_swallowed_by_another_scope_follower(tmp_path, host_type):
    host, calls, launch = _host(tmp_path, host_type)
    for scope in ('TEST-a','TEST-a','TEST-a','TEST-b','TEST-c'):
        launch_audience_worker(host,session_id=scope,allowed_scope_ids=frozenset({scope}),launcher=launch)
    assert [row[0]['allowed_scope_ids'] for row in calls] == [['TEST-a'],['TEST-a'],['TEST-b'],['TEST-c']]
    assert len(host._worker_lanes) == 3
    assert calls[1][1]['after_pid'] == 101
    assert all(row[1]['detach_output'] for row in calls)
    host.close(detach_worker=True)


def test_wakeup_revalidates_changed_binding_before_launch(tmp_path):
    host, calls, launch = _host(tmp_path)
    raw = json.loads(host._config_path.read_text()); raw['binding']['installation_id']='TEST-other'
    host._config_path.write_text(json.dumps(raw))
    with pytest.raises(ValueError,match='runtime_binding_changed'):
        launch_audience_worker(host,session_id='TEST-s',allowed_scope_ids=frozenset({'TEST-a'}),launcher=launch)
    assert calls == []


def test_worker_waits_for_other_owner_within_original_budget(tmp_path, monkeypatch):
    binding = _binding(tmp_path/'data')
    core=MemoryCore(CoreConfig(binding)); core.initialize()
    cfg = _write_config(tmp_path/'worker.json',_config_payload(binding,drain_seconds=1.0))
    entered=threading.Event()
    real_lock=worker_entry.advisory_file_lock
    @contextmanager
    def observed_lock(path, *, timeout_seconds=None):
        if path.name == 'runtime-worker.lock':
            assert 0 < timeout_seconds <= 1
            entered.set()
        with real_lock(path,timeout_seconds=timeout_seconds):
            yield
    monkeypatch.setattr(worker_entry,'advisory_file_lock',observed_lock)
    output=StringIO()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with advisory_file_lock(binding.data_directory/'runtime-worker.lock'):
            future=pool.submit(worker_entry.run_worker,cfg,output=output)
            assert entered.wait(2)
            assert not future.done()
        assert future.result(timeout=2) == 0
    assert json.loads(output.getvalue())['status'] == 'idle'


def test_missing_credentials_do_not_reserve_or_send(tmp_path, monkeypatch):
    config, ledger, _ = _runtime_config(tmp_path)
    monkeypatch.delenv('SCOPE_RECALL_TEST_CHAT_KEY',raising=False)
    monkeypatch.delenv('SCOPE_RECALL_TEST_EMBED_KEY',raising=False)
    transport=Mock()
    runtime=build_auxiliary_runtime(config,transport=transport)
    for invoke in (lambda:runtime.consolidation.propose([{'role':'user','content':'TEST'}],remaining_seconds=1),
                   lambda:runtime.query_embedding.embed_query('TEST',remaining_seconds=1)):
        with pytest.raises(AuxiliaryModelError) as error:
            invoke()
        assert error.value.error_type == 'credential_missing'
    transport.post.assert_not_called()
    with sqlite3.connect(ledger) as db:
        assert db.execute('SELECT count(*) FROM requests').fetchone()[0] == 0


def test_http_failure_classification_preserves_transient_and_permanent_errors():
    assert _model_exception_outcome(AuxiliaryModelError('http_status',detail='503')) == ('retry','http_503')
    assert _model_exception_outcome(AuxiliaryModelError('http_status',detail='429')) == ('retry','http_429')
    assert _model_exception_outcome(AuxiliaryModelError('http_status',detail='401')) == ('failed','http_401')


@pytest.mark.parametrize('code,state,attempt',[('credential_missing','pending',0),('meter_breach','failed',1)])
def test_worker_counts_only_actual_model_attempts(worker_app,code,state,attempt):
    core,ctx,_=worker_app
    capture(core,ctx,'TEST durable original source for metering')
    _mark_embed_done(core)
    class Model:
        def propose(self,*args,**kwargs):
            raise AuxiliaryModelError(code)
    core.drain_worker(ctx,consolidation=Model(),max_items=1,remaining_seconds=1)
    row=work_rows(core)[0]
    assert (row[3],row[4])==(state,attempt)


def test_expired_request_never_settles_with_unbounded_timeout(tmp_path,monkeypatch):
    from scope_recall import adapters
    import scope_recall.adapters.models as models
    config, _, _ = _runtime_config(tmp_path)
    monkeypatch.setenv('SCOPE_RECALL_TEST_CHAT_KEY','test-key')
    now=[100.0]
    monkeypatch.setattr(models.time,'monotonic',lambda:now[0])
    def handler(**kwargs):
        now[0]=102.0
        raise AuxiliaryModelError('timeout')
    runtime=build_auxiliary_runtime(config,transport=FakeTransport(handler))
    finish=Mock(return_value='network_error_usage_unknown_reserved_charge_retained')
    monkeypatch.setattr(runtime.consolidation._ledger,'finish',finish)
    with pytest.raises(AuxiliaryModelError):
        runtime.consolidation.propose([{'role':'user','content':'TEST'}],remaining_seconds=1)
    assert finish.call_args.kwargs['timeout_seconds'] == .001


def test_failed_vector_open_does_not_suppress_healthy_worker_port(tmp_path,monkeypatch):
    binding=_binding(tmp_path/'data')
    cfg=RuntimeInstanceConfig.from_mapping(_config_payload(binding,drain_seconds=1))
    instance=build_runtime_instance(cfg); instance.core.initialize()
    monkeypatch.setattr(instance,'_ensure_vector_port',Mock(side_effect=OSError('TEST offline optional index')))
    called=[]
    monkeypatch.setattr('scope_recall.core.worker.drain_worker',lambda *args,**kwargs:called.append(kwargs) or 'TEST-receipt')
    model=Mock()
    assert instance.drain(consolidation=model) == 'TEST-receipt'
    assert called[0]['consolidation'] is not None
    assert instance.background_gaps == ('vector_unavailable:OSError',)
    instance.close()
