"""Real owned-pipe regressions; synthetic model work never makes HTTP calls."""
import sys
import threading
import time

import pytest

from scope_recall.vector import process_store as native
from scope_recall.core.deadline import RequestDeadline, using_request_deadline


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


def test_an_open_that_failed_after_its_caller_stopped_waiting_is_reopened(tmp_path,monkeypatch):
    """A table open that ran out of its caller's time and then failed in the helper left no open table, and the
    parked answer was taken without a look: a long-running host said "not open" on every search until restarted."""
    s=store(tmp_path,monkeypatch,error=True,delay=.2)
    try:
        with using_request_deadline(RequestDeadline.from_budget(.05)):
            s.open_existing_with_work(lambda:None)
        assert s._pending_response_id is not None and not s.requires_reopen
        time.sleep(.3)
        with pytest.raises(RuntimeError):
            with using_request_deadline(RequestDeadline.from_budget(2)):
                s._call('search',[],scope_id='PUBLIC',limit=1)
        assert s.requires_reopen, "the next request opens the table again"
    finally:s.close()


def test_work_that_failed_takes_the_open_only_if_it_has_come(tmp_path,monkeypatch):
    """A query embedding that failed at once waited for a slow table open (1.56 s against 0.02 s, review of 3.4.1).
    With nothing to search with, the open is taken if it has come and parked for the next request if not."""
    s=store(tmp_path,monkeypatch,delay=3.0)
    try:
        started=time.monotonic()
        with using_request_deadline(RequestDeadline.from_budget(8)):
            s.open_existing_with_work(lambda:False)
        assert time.monotonic()-started<2.0, 'returned without waiting for the open'
        worker=s._process
        assert s._pending_response_id is not None and worker.poll() is None and not s.requires_reopen
        with using_request_deadline(RequestDeadline.from_budget(10)):
            assert s._call('search',[],scope_id='PUBLIC',limit=1)=='search'
        assert s._process is worker
    finally:s.close()


def test_a_parked_answer_that_came_in_long_ago_is_taken_not_called_a_wedge(tmp_path,monkeypatch):
    """A kept recall handler parked its first, cold search and was asked again minutes later: the helper had answered
    long before, but the age alone called it wedged, closed it, and the next recall opened cold (worker_unresponsive)."""
    s=store(tmp_path,monkeypatch,delay=.2)
    try:
        with using_request_deadline(RequestDeadline.from_budget(2)):
            s.open_existing_with_work(lambda:None)
        worker=s._process
        with pytest.raises(Exception):
            with using_request_deadline(RequestDeadline.from_budget(.05)):
                s._call('search',[],scope_id='PUBLIC',limit=1)
        assert s._pending_response_id is not None
        time.sleep(.3)  # the helper answers the parked search
        s._pending_response_since-=s._pending_response_timeout+1  # and it was parked longer ago than a wedge
        with using_request_deadline(RequestDeadline.from_budget(2)):
            assert s._call('search',[],scope_id='PUBLIC',limit=1)=='search'
        assert s._process is worker and worker.poll() is None and not s.requires_reopen
    finally:s.close()


def test_a_parked_answer_that_never_came_is_still_a_wedge(tmp_path,monkeypatch):
    s=store(tmp_path,monkeypatch,delay=.2)
    try:
        with using_request_deadline(RequestDeadline.from_budget(2)):
            s.open_existing_with_work(lambda:None)
        with pytest.raises(Exception):
            with using_request_deadline(RequestDeadline.from_budget(.05)):
                s._call('search',[],scope_id='PUBLIC',limit=1)
        s._pending_response_since-=s._pending_response_timeout+1  # parked longer ago than a wedge, not yet answered
        with pytest.raises(RuntimeError,match='unresponsive'):
            with using_request_deadline(RequestDeadline.from_budget(2)):
                s._call('search',[],scope_id='PUBLIC',limit=1)
        assert s.requires_reopen
    finally:s.close()


def test_a_spare_helper_is_taken_once_and_not_replaced(monkeypatch):
    """A server kept a spare as well, replaced each time one was taken: about 0.55 GB of committed memory idle once
    the store all its runtimes share holds its helper (3.4.9)."""
    spawned=[]
    class Alive:
        stdin=stdout=None
        def poll(self):return None
    monkeypatch.setattr(native,'_spawn_helper',lambda:spawned.append(Alive()) or spawned[-1])
    monkeypatch.setattr(native,'_spare',None)
    native.prestart()
    assert native._take_spare() is spawned[0]
    assert native._spare is None and len(spawned)==1


def _sharing(tmp_path, monkeypatch, *, fail_first_open=False, open_delay=0.0):
    """A process that shares its stores (``share``), with a helper that logs each request and counts its starts."""
    log, spawned, failed = tmp_path/'requests.log', [], tmp_path/'open-failed-once'
    program = ('import json,os,sys,time\n'
               'for line in sys.stdin:\n'
               ' r=json.loads(line)\n'
               f' open({str(log)!r},"a").write(r["method"]+chr(10))\n'
               ' ok=True\n'
               ' if r["method"]=="open_existing":\n'
               f'  time.sleep({open_delay!r})\n'
               f'  if {fail_first_open!r} and not os.path.exists({str(failed)!r}):\n'
               f'   open({str(failed)!r},"w").close(); ok=False\n'
               ' print(json.dumps(dict(id=r["id"],ok=ok,error="PUBLIC table cannot be opened",error_type="OSError",'
               'result=[] if r["method"].startswith("search") else r["method"])),flush=True)\n')
    monkeypatch.setattr(native,'_worker_command',lambda:[sys.executable,'-I','-B','-c',program])
    spawn=native._spawn_helper
    monkeypatch.setattr(native,'_spawn_helper',lambda:spawned.append(1) or spawn())
    monkeypatch.setattr(native,'_spare',None)
    monkeypatch.setattr(native,'_shared',{})
    monkeypatch.setattr(native,'_sharing',False)
    native.share()
    (tmp_path/'lancedb'/'PUBLIC.lance').mkdir(parents=True)
    return log, spawned


def test_a_server_s_runtimes_search_one_store_with_one_helper(tmp_path,monkeypatch):
    """A handler made for a prompt the kept handler was busy with started a helper of its own, and lost its vector
    search to that helper's start: bursts of parallel sub-agents on the work computer (2026-09-30)."""
    log, spawned = _sharing(tmp_path, monkeypatch)
    kept=native.store_for(tmp_path/'lancedb',table_name='PUBLIC',dimensions=2)
    made=native.store_for(tmp_path/'lancedb',table_name='PUBLIC',dimensions=2)
    try:
        assert isinstance(kept,native.SharedStore) and kept._store is made._store
        with using_request_deadline(RequestDeadline.from_budget(5)):
            kept.open_existing()
            work=[]
            made.open_existing_with_work(lambda:work.append('embedding'))
            assert work==['embedding'], "the request's own work still runs"
            made.close()  # a handler closed at the end of its request
            assert kept.search_scopes([0.0,1.0],scope_ids=['PUBLIC'],limit=1)==[]
        assert log.read_text().split()==['open_existing','search_scopes'], "opened once, searched after a close"
        assert spawned==[1]
    finally:
        native._close_shared()


def test_a_shared_store_whose_helper_failed_is_opened_again(tmp_path,monkeypatch):
    log, spawned = _sharing(tmp_path, monkeypatch)
    view=native.store_for(tmp_path/'lancedb',table_name='PUBLIC',dimensions=2)
    try:
        with using_request_deadline(RequestDeadline.from_budget(5)):
            view.open_existing()
            view._store._detach_helper(failed=True)  # what a helper that died mid-request leaves
            assert view.requires_reopen
            view._store._finish_teardown(timeout=5,retry_stop=True)
            view.open_existing()
            assert not view.requires_reopen
        assert log.read_text().split()==['open_existing','open_existing'] and spawned==[1,1]
    finally:
        native._close_shared()


def test_a_process_that_does_not_share_builds_a_store_for_each(tmp_path,monkeypatch):
    monkeypatch.setattr(native,'_sharing',False)
    one=native.store_for(tmp_path/'lancedb',table_name='PUBLIC',dimensions=2)
    two=native.store_for(tmp_path/'lancedb',table_name='PUBLIC',dimensions=2)
    assert type(one) is native.ProcessLanceVectorStore and one is not two


def test_the_shared_stores_are_stopped_when_the_process_ends(tmp_path,monkeypatch):
    _log, _spawned = _sharing(tmp_path, monkeypatch)
    view=native.store_for(tmp_path/'lancedb',table_name='PUBLIC',dimensions=2)
    with using_request_deadline(RequestDeadline.from_budget(5)):
        view.open_existing()
    store=view._store
    native._close_shared()
    assert store._closed and native._shared=={}


def test_a_shared_store_whose_open_failed_is_opened_again(tmp_path,monkeypatch):
    """A helper that could not open the table held none, and every search it answered said so until the server
    restarted: a store a runtime owned alone was closed and made anew, one its process shares was not (review of
    3.4.9)."""
    log, spawned = _sharing(tmp_path, monkeypatch, fail_first_open=True)
    view=native.store_for(tmp_path/'lancedb',table_name='PUBLIC',dimensions=2)
    try:
        with using_request_deadline(RequestDeadline.from_budget(5)):
            with pytest.raises(RuntimeError,match='cannot be opened'):
                view.open_existing()
            assert view.requires_reopen
            view.open_existing()
            assert view.search_scopes([0.0,1.0],scope_ids=['PUBLIC'],limit=1)==[]
        assert log.read_text().split()==['open_existing','open_existing','search_scopes'] and len(spawned)==2
    finally:
        native._close_shared()


def test_a_reopen_that_ran_out_of_time_before_its_helper_started_is_opened_by_the_next_request(tmp_path,monkeypatch):
    """The store looked open with no helper: the next search started one, which held no table, and every runtime of
    the process searched it until the process ended (review of 3.4.9)."""
    log, spawned = _sharing(tmp_path, monkeypatch)
    view=native.store_for(tmp_path/'lancedb',table_name='PUBLIC',dimensions=2)
    try:
        with using_request_deadline(RequestDeadline.from_budget(5)):
            view.open_existing()
            view._store._detach_helper(failed=True)
            view._store._finish_teardown(timeout=5,retry_stop=True)
        with using_request_deadline(RequestDeadline.from_absolute(time.monotonic()-1)):
            view.open_existing()  # out of time before a helper started
        assert view.requires_reopen and not view._store._serving() and spawned==[1]
        with using_request_deadline(RequestDeadline.from_budget(5)):
            view.open_existing()
            assert view.search_scopes([0.0,1.0],scope_ids=['PUBLIC'],limit=1)==[]
        assert log.read_text().split()==['open_existing','open_existing','search_scopes'] and spawned==[1,1]
    finally:
        native._close_shared()


def test_a_search_starts_no_helper(tmp_path,monkeypatch):
    """A helper a search started held no table and answered every search so, and the store looked open (review of
    3.4.9).  A helper is started to open the table, or to say whether LanceDB is installed."""
    log, spawned = _sharing(tmp_path, monkeypatch)
    view=native.store_for(tmp_path/'lancedb',table_name='PUBLIC',dimensions=2)
    try:
        with using_request_deadline(RequestDeadline.from_budget(5)):
            with pytest.raises(RuntimeError,match='closed'):
                view.search_scopes([0.0,1.0],scope_ids=['PUBLIC'],limit=1)
            assert spawned==[] and view.requires_reopen and not view._store._serving()
            view.open_existing()
            assert view.search_scopes([0.0,1.0],scope_ids=['PUBLIC'],limit=1)==[]
        assert log.read_text().split()==['open_existing','search_scopes'] and spawned==[1]
    finally:
        native._close_shared()


def test_a_table_not_made_yet_starts_no_helper_for_a_shared_store(tmp_path,monkeypatch):
    """Each prompt started a helper to learn the table was missing, about 2 s each with no spare (review of 3.4.9)."""
    log, spawned = _sharing(tmp_path, monkeypatch)
    (tmp_path/'lancedb'/'PUBLIC.lance').rmdir()
    view=native.store_for(tmp_path/'lancedb',table_name='PUBLIC',dimensions=2)
    try:
        with using_request_deadline(RequestDeadline.from_budget(5)):
            for _ in range(3):
                with pytest.raises(FileNotFoundError):
                    view.open_existing_with_work(lambda:pytest.fail('model called'))
        assert spawned==[] and not log.exists()
    finally:
        native._close_shared()


def test_a_shared_store_is_made_once_by_the_runtimes_that_may_make_it(tmp_path,monkeypatch):
    log, spawned = _sharing(tmp_path, monkeypatch)
    views=[native.store_for(tmp_path/'lancedb',table_name='PUBLIC',dimensions=2) for _ in range(2)]
    try:
        with using_request_deadline(RequestDeadline.from_budget(5)):
            for view in views:
                view.open()
        assert log.read_text().split()==['open'] and spawned==[1]
    finally:
        native._close_shared()


def test_the_drain_s_open_of_a_shared_store_says_it_ran_out_of_time(tmp_path,monkeypatch):
    """A store of its own raises it, and the drain reports the gap; the shared store returned as if it had opened
    (review of 3.4.9)."""
    _log, spawned = _sharing(tmp_path, monkeypatch)
    view=native.store_for(tmp_path/'lancedb',table_name='PUBLIC',dimensions=2)
    try:
        with using_request_deadline(RequestDeadline.from_absolute(time.monotonic()-1)):
            with pytest.raises(native._RequestBudgetExpired):
                view.open()
        assert spawned==[] and view.requires_reopen
    finally:
        native._close_shared()


def test_a_helper_asked_only_whether_lancedb_is_installed_holds_no_table(tmp_path,monkeypatch):
    """It counted as serving: the open was skipped, and every search said the table was not open (review of
    3.4.9)."""
    log, spawned = _sharing(tmp_path, monkeypatch)
    view=native.store_for(tmp_path/'lancedb',table_name='PUBLIC',dimensions=2)
    try:
        with using_request_deadline(RequestDeadline.from_budget(5)):
            assert view.is_available()
            view.open_existing()
            assert view.search_scopes([0.0,1.0],scope_ids=['PUBLIC'],limit=1)==[]
        assert log.read_text().split()==['is_available','open_existing','search_scopes'] and spawned==[1]
    finally:
        native._close_shared()


def test_two_runtimes_that_open_the_shared_store_together_open_it_once(tmp_path,monkeypatch):
    """Both saw it closed and both opened it, the second while every search waited (review of 3.4.9)."""
    log, spawned = _sharing(tmp_path, monkeypatch, open_delay=0.3)
    views=[native.store_for(tmp_path/'lancedb',table_name='PUBLIC',dimensions=2) for _ in range(2)]
    work=[]

    def opening(view):
        with using_request_deadline(RequestDeadline.from_budget(5)):
            view.open_existing_with_work(lambda:work.append(1))

    threads=[threading.Thread(target=opening,args=(view,)) for view in views]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        assert work==[1,1] and log.read_text().split()==['open_existing'] and spawned==[1]
    finally:
        native._close_shared()


def test_the_opening_runtime_s_embedding_does_not_hold_the_others_searches(tmp_path,monkeypatch):
    """Overlapped with the open, the opener's embedding held the store's lock, and another prompt's search with a
    second left timed out on it (review of 3.4.9).  The embedding is asked for when its recall starts anyway."""
    _log, _spawned = _sharing(tmp_path, monkeypatch)
    opener=native.store_for(tmp_path/'lancedb',table_name='PUBLIC',dimensions=2)
    other=native.store_for(tmp_path/'lancedb',table_name='PUBLIC',dimensions=2)
    embedding, release = threading.Event(), threading.Event()

    def opening():
        with using_request_deadline(RequestDeadline.from_budget(10)):
            opener.open_existing_with_work(lambda:(embedding.set(), release.wait(5)))

    thread=threading.Thread(target=opening)
    thread.start()
    try:
        assert embedding.wait(5)
        with using_request_deadline(RequestDeadline.from_budget(1)):
            assert other.search_scopes([0.0,1.0],scope_ids=['PUBLIC'],limit=1)==[]
    finally:
        release.set()
        thread.join(10)
        native._close_shared()


@pytest.mark.skipif(sys.platform!='win32',reason="the helper-process store is Windows'")
def test_a_server_s_factory_builds_views_of_one_store(tmp_path,monkeypatch):
    from scope_recall.vector.store import build_vector_store

    _sharing(tmp_path, monkeypatch)
    try:
        one=build_vector_store('lancedb',storage_dir=tmp_path,table_name='PUBLIC',dimensions=2)
        two=build_vector_store('lancedb',storage_dir=tmp_path,table_name='PUBLIC',dimensions=2)
        assert isinstance(one,native.SharedStore) and one._store is two._store
    finally:
        native._close_shared()
