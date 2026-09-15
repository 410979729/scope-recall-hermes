"""Real owned-pipe regressions; synthetic model work never makes HTTP calls."""
import sys
import time

import pytest

from scope_recall import lance_process_store as native
from scope_recall._internal.recall.deadline import RequestDeadline, using_request_deadline


def store(tmp_path, monkeypatch, *, error=False, delay=.1):
    program = f'''import json,sys,time
for line in sys.stdin:
 r=json.loads(line)
 time.sleep({delay!r})
 print(json.dumps(dict(id=r['id'],ok={not error!r},result=r['method'],error='PUBLIC open failed')),flush=True)
'''
    monkeypatch.setattr(native,'_worker_command',lambda:[sys.executable,'-I','-B','-c',program])
    result=native.ProcessLanceVectorStore(tmp_path/'lancedb',table_name='PUBLIC',dimensions=2)
    (result.db_path/'PUBLIC.lance').mkdir(parents=True)
    return result


def test_open_overlaps_work_and_next_rpc_does_not_read_open_response(tmp_path,monkeypatch):
    s=store(tmp_path,monkeypatch,delay=.2)
    started_marker, release_marker = tmp_path/'native-started', tmp_path/'model-finished'
    program=f'''import json,sys,time
from pathlib import Path
for line in sys.stdin:
 r=json.loads(line)
 if r['method']=='open_existing':
  Path({str(started_marker)!r}).touch()
  while not Path({str(release_marker)!r}).exists():time.sleep(.005)
 print(json.dumps(dict(id=r['id'],ok=True,result=r['method'])),flush=True)
'''
    monkeypatch.setattr(native,'_worker_command',lambda:[sys.executable,'-I','-B','-c',program])
    try:
        calls=[]
        def work():
            assert s._process is not None
            calls.append('embedding')
            deadline=time.monotonic()+3
            while not started_marker.exists() and time.monotonic()<deadline:time.sleep(.005)
            assert started_marker.exists()
            release_marker.touch()
        with using_request_deadline(RequestDeadline.from_budget(5)):
            s.open_existing_with_work(work)
        assert calls==['embedding']
        assert s._call('search',[],scope_id='PUBLIC',limit=1)=='search'
    finally:s.close()


def test_expired_open_wait_defers_response_without_poisoning_worker(tmp_path,monkeypatch):
    s=store(tmp_path,monkeypatch,delay=.2)
    workers=[]
    failure=None
    def work():
        workers.append(s._process)
        time.sleep(.08)
    try:
        try:
            with using_request_deadline(RequestDeadline.from_budget(.06)):
                s.open_existing_with_work(work)
        except RuntimeError as exc:
            failure=exc
        assert failure is None, f'request deadline poisoned healthy worker: {failure}'
        assert len(workers)==1 and workers[0] is not None
        assert s._process is workers[0] and workers[0].poll() is None
        assert not s.requires_reopen and not s._closed
        with using_request_deadline(RequestDeadline.from_budget(2)):
            assert s._call('search',[],scope_id='PUBLIC',limit=1)=='search'
        assert s._process is workers[0] and workers[0].poll() is None
        assert not s.requires_reopen and not s._closed
    finally:s.close()


def test_missing_companion_never_calls_model_work(tmp_path,monkeypatch):
    s=store(tmp_path,monkeypatch)
    (s.db_path/'PUBLIC.lance').rmdir()
    try:
        with pytest.raises(FileNotFoundError):s.open_existing_with_work(lambda:pytest.fail('model called'))
        assert s._process is None
    finally:s.close()


@pytest.mark.parametrize('failure',['embedding','native'])
def test_failed_overlap_reaps_owned_process_and_fresh_request_isolated(tmp_path,monkeypatch,failure):
    s=store(tmp_path,monkeypatch,error=failure=='native',delay=.03)
    worker=[]
    def work():
        worker.append(s._process)
        if failure=='embedding':raise ValueError('PUBLIC embedding failed')
    try:
        with pytest.raises((ValueError,RuntimeError)):
            with using_request_deadline(RequestDeadline.from_budget(2)):
                s.open_existing_with_work(work)
    finally:s.close()
    assert len(worker)==1 and worker[0].poll() is not None
    fresh=store(tmp_path/'fresh',monkeypatch,delay=.01)
    try:
        with using_request_deadline(RequestDeadline.from_budget(2)):
            fresh.open_existing_with_work(lambda:None)
            assert fresh._call('search',[],scope_id='PUBLIC',limit=1)=='search'
    finally:fresh.close()
    try:
        with using_request_deadline(RequestDeadline.from_budget(2)):
            s.open_existing_with_work(lambda:None)
            assert s._call('search',[],scope_id='PUBLIC',limit=1)=='search'
    finally:s.close()
