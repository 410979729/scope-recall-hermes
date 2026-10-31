"""Real owned-pipe regressions; synthetic model work never makes HTTP calls."""

import sys
import threading
import time

import pytest
from scope_recall.core.deadline import RequestDeadline, using_request_deadline
from scope_recall.vector import process_store as native


def store(tmp_path, monkeypatch, *, error=False, delay=0.1):
    program = f"""import json,sys,time
for line in sys.stdin:
 r=json.loads(line)
 time.sleep({delay!r})
 print(json.dumps(dict(id=r['id'],ok={not error!r},result=r['method'],error='PUBLIC open failed')),flush=True)
"""
    monkeypatch.setattr(native, "_worker_command", lambda: [sys.executable, "-I", "-B", "-c", program])
    result = native.ProcessLanceVectorStore(tmp_path / "lancedb", table_name="PUBLIC", dimensions=2)
    (result.db_path / "PUBLIC.lance").mkdir(parents=True)
    return result


def test_open_overlaps_work_and_next_rpc_does_not_read_open_response(tmp_path, monkeypatch):
    s = store(tmp_path, monkeypatch, delay=0.2)
    started_marker, release_marker = tmp_path / "native-started", tmp_path / "model-finished"
    program = f"""import json,sys,time
from pathlib import Path
for line in sys.stdin:
 r=json.loads(line)
 if r['method']=='open_existing':
  Path({str(started_marker)!r}).touch()
  while not Path({str(release_marker)!r}).exists():time.sleep(.005)
 print(json.dumps(dict(id=r['id'],ok=True,result=r['method'])),flush=True)
"""
    monkeypatch.setattr(native, "_worker_command", lambda: [sys.executable, "-I", "-B", "-c", program])
    try:
        calls = []

        def work():
            assert s._process is not None
            calls.append("embedding")
            deadline = time.monotonic() + 3
            while not started_marker.exists() and time.monotonic() < deadline:
                time.sleep(0.005)
            assert started_marker.exists()
            release_marker.touch()

        with using_request_deadline(RequestDeadline.from_budget(5)):
            s.open_existing_with_work(work)
        assert calls == ["embedding"]
        assert s._call("search", [], scope_id="PUBLIC", limit=1) == "search"
    finally:
        s.close()


def test_expired_open_wait_defers_response_without_poisoning_worker(tmp_path, monkeypatch):
    s = store(tmp_path, monkeypatch, delay=0.2)
    workers = []
    failure = None

    def work():
        workers.append(s._process)
        time.sleep(0.08)

    try:
        try:
            with using_request_deadline(RequestDeadline.from_budget(0.06)):
                s.open_existing_with_work(work)
        except RuntimeError as exc:
            failure = exc
        assert failure is None, f"request deadline poisoned healthy worker: {failure}"
        assert len(workers) == 1 and workers[0] is not None
        assert s._process is workers[0] and workers[0].poll() is None
        assert not s.requires_reopen and not s._closed
        with using_request_deadline(RequestDeadline.from_budget(2)):
            assert s._call("search", [], scope_id="PUBLIC", limit=1) == "search"
        assert s._process is workers[0] and workers[0].poll() is None
        assert not s.requires_reopen and not s._closed
    finally:
        s.close()


def test_missing_companion_never_calls_model_work(tmp_path, monkeypatch):
    s = store(tmp_path, monkeypatch)
    (s.db_path / "PUBLIC.lance").rmdir()
    try:
        with pytest.raises(FileNotFoundError):
            s.open_existing_with_work(lambda: pytest.fail("model called"))
        assert s._process is None
    finally:
        s.close()


@pytest.mark.parametrize("failure", ["embedding", "native"])
def test_failed_overlap_reaps_owned_process_and_fresh_request_isolated(tmp_path, monkeypatch, failure):
    s = store(tmp_path, monkeypatch, error=failure == "native", delay=0.03)
    worker = []

    def work():
        worker.append(s._process)
        if failure == "embedding":
            raise ValueError("PUBLIC embedding failed")

    try:
        with pytest.raises((ValueError, RuntimeError)):
            with using_request_deadline(RequestDeadline.from_budget(2)):
                s.open_existing_with_work(work)
    finally:
        s.close()
    assert len(worker) == 1 and worker[0].poll() is not None
    fresh = store(tmp_path / "fresh", monkeypatch, delay=0.01)
    try:
        with using_request_deadline(RequestDeadline.from_budget(2)):
            fresh.open_existing_with_work(lambda: None)
            assert fresh._call("search", [], scope_id="PUBLIC", limit=1) == "search"
    finally:
        fresh.close()
    try:
        with using_request_deadline(RequestDeadline.from_budget(2)):
            s.open_existing_with_work(lambda: None)
            assert s._call("search", [], scope_id="PUBLIC", limit=1) == "search"
    finally:
        s.close()


def test_an_open_that_failed_after_its_caller_stopped_waiting_is_reopened(tmp_path, monkeypatch):
    """A table open that ran out of its caller's time and then failed in the helper left no open table, and the
    parked answer was taken without a look: a long-running host said "not open" on every search until restarted."""
    s = store(tmp_path, monkeypatch, error=True, delay=0.2)
    try:
        with using_request_deadline(RequestDeadline.from_budget(0.05)):
            s.open_existing_with_work(lambda: None)
        assert s._pending_response_id is not None and not s.requires_reopen
        time.sleep(0.3)
        with pytest.raises(RuntimeError):
            with using_request_deadline(RequestDeadline.from_budget(2)):
                s._call("search", [], scope_id="PUBLIC", limit=1)
        assert s.requires_reopen, "the next request opens the table again"
    finally:
        s.close()


def test_work_that_failed_takes_the_open_only_if_it_has_come(tmp_path, monkeypatch):
    """A query embedding that fails at once does not wait for a slow table open.
    With nothing to search with, the open is taken if it has come and parked for the next request if not."""
    s = store(tmp_path, monkeypatch, delay=3.0)
    try:
        started = time.monotonic()
        with using_request_deadline(RequestDeadline.from_budget(8)):
            s.open_existing_with_work(lambda: False)
        assert time.monotonic() - started < 2.0, "returned without waiting for the open"
        worker = s._process
        assert s._pending_response_id is not None and worker.poll() is None and not s.requires_reopen
        with using_request_deadline(RequestDeadline.from_budget(10)):
            assert s._call("search", [], scope_id="PUBLIC", limit=1) == "search"
        assert s._process is worker
    finally:
        s.close()


def test_a_parked_answer_that_came_in_long_ago_is_taken_not_called_a_wedge(tmp_path, monkeypatch):
    """A kept recall handler parked its first, cold search and was asked again minutes later: the helper had answered
    long before, but the age alone called it wedged, closed it, and the next recall opened cold (worker_unresponsive)."""
    s = store(tmp_path, monkeypatch, delay=0.2)
    try:
        with using_request_deadline(RequestDeadline.from_budget(2)):
            s.open_existing_with_work(lambda: None)
        worker = s._process
        with pytest.raises(Exception):
            with using_request_deadline(RequestDeadline.from_budget(0.05)):
                s._call("search", [], scope_id="PUBLIC", limit=1)
        assert s._pending_response_id is not None
        time.sleep(0.3)  # the helper answers the parked search
        s._pending_response_since -= s._pending_response_timeout + 1  # and it was parked longer ago than a wedge
        with using_request_deadline(RequestDeadline.from_budget(2)):
            assert s._call("search", [], scope_id="PUBLIC", limit=1) == "search"
        assert s._process is worker and worker.poll() is None and not s.requires_reopen
    finally:
        s.close()


def test_a_parked_answer_that_never_came_is_still_a_wedge(tmp_path, monkeypatch):
    s = store(tmp_path, monkeypatch, delay=0.2)
    try:
        with using_request_deadline(RequestDeadline.from_budget(2)):
            s.open_existing_with_work(lambda: None)
        with pytest.raises(Exception):
            with using_request_deadline(RequestDeadline.from_budget(0.05)):
                s._call("search", [], scope_id="PUBLIC", limit=1)
        s._pending_response_since -= s._pending_response_timeout + 1  # parked longer ago than a wedge, not yet answered
        with pytest.raises(RuntimeError, match="unresponsive"):
            with using_request_deadline(RequestDeadline.from_budget(2)):
                s._call("search", [], scope_id="PUBLIC", limit=1)
        assert s.requires_reopen
    finally:
        s.close()


def test_a_spare_helper_is_taken_once_and_not_replaced(monkeypatch):
    """A server's spare helper is taken once and not replaced: a replacement would sit idle, holding memory, once the
    store all its runtimes share holds its own helper."""
    spawned = []

    class Alive:
        stdin = stdout = None

        def poll(self):
            return None

    monkeypatch.setattr(native, "_spawn_helper", lambda: spawned.append(Alive()) or spawned[-1])
    monkeypatch.setattr(native, "_spare", None)
    native.prestart()
    assert native._take_spare() is spawned[0]
    assert native._spare is None and len(spawned) == 1


def _sharing(tmp_path, monkeypatch, *, fail_first_open=False, open_delay=0.0):
    """A process that shares its stores (``share``), with a helper that logs each request and counts its starts."""
    log, spawned, failed = tmp_path / "requests.log", [], tmp_path / "open-failed-once"
    program = (
        "import json,os,sys,time\n"
        "for line in sys.stdin:\n"
        " r=json.loads(line)\n"
        f' open({str(log)!r},"a").write(r["method"]+chr(10))\n'
        " ok=True\n"
        ' if r["method"]=="open_existing":\n'
        f"  time.sleep({open_delay!r})\n"
        f"  if {fail_first_open!r} and not os.path.exists({str(failed)!r}):\n"
        f'   open({str(failed)!r},"w").close(); ok=False\n'
        ' print(json.dumps(dict(id=r["id"],ok=ok,error="PUBLIC table cannot be opened",error_type="OSError",'
        'result=[] if r["method"].startswith("search") else r["method"])),flush=True)\n'
    )
    monkeypatch.setattr(native, "_worker_command", lambda: [sys.executable, "-I", "-B", "-c", program])
    spawn = native._spawn_helper
    monkeypatch.setattr(native, "_spawn_helper", lambda: spawned.append(1) or spawn())
    monkeypatch.setattr(native, "_spare", None)
    monkeypatch.setattr(native, "_shared", {})
    monkeypatch.setattr(native, "_sharing", False)
    native.share()
    (tmp_path / "lancedb" / "PUBLIC.lance").mkdir(parents=True)
    return log, spawned


def test_a_server_s_runtimes_search_one_store_with_one_helper(tmp_path, monkeypatch):
    """A handler made for a prompt while the kept handler is busy shares the store's helper and starts none of its own,
    so its vector search is not lost to a helper's start, as when parallel sub-agents prompt in bursts."""
    log, spawned = _sharing(tmp_path, monkeypatch)
    kept = native.store_for(tmp_path / "lancedb", table_name="PUBLIC", dimensions=2)
    made = native.store_for(tmp_path / "lancedb", table_name="PUBLIC", dimensions=2)
    try:
        assert isinstance(kept, native.SharedStore) and kept._store is made._store
        with using_request_deadline(RequestDeadline.from_budget(5)):
            kept.open_existing()
            work = []
            made.open_existing_with_work(lambda: work.append("embedding"))
            assert work == ["embedding"], "the request's own work still runs"
            made.close()  # a handler closed at the end of its request
            assert kept.search_scopes([0.0, 1.0], scope_ids=["PUBLIC"], limit=1) == []
        assert log.read_text().split() == ["open_existing", "search_scopes"], "opened once, searched after a close"
        assert spawned == [1]
    finally:
        native._close_shared()


def test_a_spare_is_not_started_once_the_shared_store_serves(tmp_path, monkeypatch):
    """A Hermes gateway asks for a helper ahead each time it binds an agent and shares its stores: once the shared
    store holds its helper, no spare is started for a later agent, since none would be taken."""
    log, spawned = _sharing(tmp_path, monkeypatch)
    native.prestart()  # the first agent's, which the shared store's open takes
    assert spawned == [1] and native._spare is not None, "nothing serves yet: the spare is started"
    view = native.store_for(tmp_path / "lancedb", table_name="PUBLIC", dimensions=2)
    try:
        with using_request_deadline(RequestDeadline.from_budget(5)):
            view.open_existing()
        assert spawned == [1] and native._spare is None
        native.prestart()  # a later agent's
        assert spawned == [1] and native._spare is None
    finally:
        native._close_shared()


def test_a_bind_after_the_shared_helper_failed_starts_a_spare_that_the_reopen_takes(tmp_path, monkeypatch):
    """The guard asks whether a shared store holds a live helper, not whether one is registered."""
    log, spawned = _sharing(tmp_path, monkeypatch)
    view = native.store_for(tmp_path / "lancedb", table_name="PUBLIC", dimensions=2)
    try:
        with using_request_deadline(RequestDeadline.from_budget(5)):
            view.open_existing()
        assert spawned == [1]
        view._store._detach_helper(failed=True)  # what a helper that died mid-request leaves
        view._store._finish_teardown(timeout=5, retry_stop=True)
        native.prestart()  # a later agent's bind
        assert spawned == [1, 1] and native._spare is not None, "nothing serves: the bind starts a spare"
        with using_request_deadline(RequestDeadline.from_budget(5)):
            view.open_existing()  # the next recall's reopen
        assert spawned == [1, 1] and native._spare is None, "the reopen took the spare instead of starting cold"
    finally:
        native._close_shared()
        native.discard_spare()


def test_a_bind_while_the_shared_store_starts_its_helper_starts_no_spare(tmp_path, monkeypatch):
    """The store takes the spare before it asks for its table: a bind in that instant (another agent made at the first
    recall) starts no spare, since nothing would take it before the process ends."""
    log, spawned = _sharing(tmp_path, monkeypatch)
    native.prestart()  # the first agent's bind
    assert spawned == [1]
    start = native.ProcessLanceVectorStore._start

    def start_then_another_bind(self):
        start(self)  # the store holds the spare now
        native.prestart()  # another agent binds in this instant

    monkeypatch.setattr(native.ProcessLanceVectorStore, "_start", start_then_another_bind)
    view = native.store_for(tmp_path / "lancedb", table_name="PUBLIC", dimensions=2)
    try:
        with using_request_deadline(RequestDeadline.from_budget(5)):
            view.open_existing()
        assert view._store._serving()
        assert spawned == [1] and native._spare is None, "a spare was started that no store will take"
    finally:
        native._close_shared()
        native.discard_spare()


def test_a_bind_after_the_shared_helper_ended_unnoticed_starts_a_spare(tmp_path, monkeypatch):
    """A helper that ended outside any request (a crash, a failed allocation near the commit limit) is not counted as
    live while it still looks open: a bind before a request meets it starts a spare, so a session's reopen need not
    start cold."""
    log, spawned = _sharing(tmp_path, monkeypatch)
    view = native.store_for(tmp_path / "lancedb", table_name="PUBLIC", dimensions=2)
    try:
        with using_request_deadline(RequestDeadline.from_budget(5)):
            view.open_existing()
        helper = view._store._process
        helper.kill()
        helper.wait(5)
        assert view._store._serving(), "nothing has noticed yet"
        native.prestart()  # a later agent's bind
        assert spawned == [1, 1] and native._spare is not None, "no live helper is held: the bind starts a spare"
    finally:
        native._close_shared()
        native.discard_spare()


def test_a_shared_store_whose_helper_failed_is_opened_again(tmp_path, monkeypatch):
    log, spawned = _sharing(tmp_path, monkeypatch)
    view = native.store_for(tmp_path / "lancedb", table_name="PUBLIC", dimensions=2)
    try:
        with using_request_deadline(RequestDeadline.from_budget(5)):
            view.open_existing()
            view._store._detach_helper(failed=True)  # what a helper that died mid-request leaves
            assert view.requires_reopen
            view._store._finish_teardown(timeout=5, retry_stop=True)
            view.open_existing()
            assert not view.requires_reopen
        assert log.read_text().split() == ["open_existing", "open_existing"] and spawned == [1, 1]
    finally:
        native._close_shared()


def test_a_process_that_does_not_share_builds_a_store_for_each(tmp_path, monkeypatch):
    monkeypatch.setattr(native, "_sharing", False)
    one = native.store_for(tmp_path / "lancedb", table_name="PUBLIC", dimensions=2)
    two = native.store_for(tmp_path / "lancedb", table_name="PUBLIC", dimensions=2)
    assert type(one) is native.ProcessLanceVectorStore and one is not two


def test_the_shared_stores_are_stopped_when_the_process_ends(tmp_path, monkeypatch):
    _log, _spawned = _sharing(tmp_path, monkeypatch)
    view = native.store_for(tmp_path / "lancedb", table_name="PUBLIC", dimensions=2)
    with using_request_deadline(RequestDeadline.from_budget(5)):
        view.open_existing()
    store = view._store
    native._close_shared()
    assert store._closed and native._shared == {}


def test_a_shared_store_whose_open_failed_is_opened_again(tmp_path, monkeypatch):
    """A shared store whose helper could not open the table is opened again, as a store a runtime owns alone is closed
    and made anew; its searches do not keep saying the table is not open until the server restarts."""
    log, spawned = _sharing(tmp_path, monkeypatch, fail_first_open=True)
    view = native.store_for(tmp_path / "lancedb", table_name="PUBLIC", dimensions=2)
    try:
        with using_request_deadline(RequestDeadline.from_budget(5)):
            with pytest.raises(RuntimeError, match="cannot be opened"):
                view.open_existing()
            assert view.requires_reopen
            view.open_existing()
            assert view.search_scopes([0.0, 1.0], scope_ids=["PUBLIC"], limit=1) == []
        assert log.read_text().split() == ["open_existing", "open_existing", "search_scopes"] and len(spawned) == 2
    finally:
        native._close_shared()


def test_a_reopen_that_ran_out_of_time_before_its_helper_started_is_opened_by_the_next_request(tmp_path, monkeypatch):
    """A reopen that runs out of time before its helper starts leaves the store needing a reopen, not looking open
    with no helper: the next request opens it, so no runtime of the process searches a helper that holds no table."""
    log, spawned = _sharing(tmp_path, monkeypatch)
    view = native.store_for(tmp_path / "lancedb", table_name="PUBLIC", dimensions=2)
    try:
        with using_request_deadline(RequestDeadline.from_budget(5)):
            view.open_existing()
            view._store._detach_helper(failed=True)
            view._store._finish_teardown(timeout=5, retry_stop=True)
        with using_request_deadline(RequestDeadline.from_absolute(time.monotonic() - 1)):
            view.open_existing()  # out of time before a helper started
        assert view.requires_reopen and not view._store._serving() and spawned == [1]
        with using_request_deadline(RequestDeadline.from_budget(5)):
            view.open_existing()
            assert view.search_scopes([0.0, 1.0], scope_ids=["PUBLIC"], limit=1) == []
        assert log.read_text().split() == ["open_existing", "open_existing", "search_scopes"] and spawned == [1, 1]
    finally:
        native._close_shared()


def test_a_search_starts_no_helper(tmp_path, monkeypatch):
    """A search starts no helper: one started that way would hold no table, answer every search so, and leave the
    store looking open.  A helper is started to open the table, or to say whether LanceDB is installed."""
    log, spawned = _sharing(tmp_path, monkeypatch)
    view = native.store_for(tmp_path / "lancedb", table_name="PUBLIC", dimensions=2)
    try:
        with using_request_deadline(RequestDeadline.from_budget(5)):
            with pytest.raises(RuntimeError, match="closed"):
                view.search_scopes([0.0, 1.0], scope_ids=["PUBLIC"], limit=1)
            assert spawned == [] and view.requires_reopen and not view._store._serving()
            view.open_existing()
            assert view.search_scopes([0.0, 1.0], scope_ids=["PUBLIC"], limit=1) == []
        assert log.read_text().split() == ["open_existing", "search_scopes"] and spawned == [1]
    finally:
        native._close_shared()


def test_a_table_not_made_yet_starts_no_helper_for_a_shared_store(tmp_path, monkeypatch):
    """A prompt starts no helper just to learn that the table is not made yet."""
    log, spawned = _sharing(tmp_path, monkeypatch)
    (tmp_path / "lancedb" / "PUBLIC.lance").rmdir()
    view = native.store_for(tmp_path / "lancedb", table_name="PUBLIC", dimensions=2)
    try:
        with using_request_deadline(RequestDeadline.from_budget(5)):
            for _ in range(3):
                with pytest.raises(FileNotFoundError):
                    view.open_existing_with_work(lambda: pytest.fail("model called"))
        assert spawned == [] and not log.exists()
    finally:
        native._close_shared()


def test_a_shared_store_is_made_once_by_the_runtimes_that_may_make_it(tmp_path, monkeypatch):
    log, spawned = _sharing(tmp_path, monkeypatch)
    views = [native.store_for(tmp_path / "lancedb", table_name="PUBLIC", dimensions=2) for _ in range(2)]
    try:
        with using_request_deadline(RequestDeadline.from_budget(5)):
            for view in views:
                view.open()
        assert log.read_text().split() == ["open"] and spawned == [1]
    finally:
        native._close_shared()


def test_the_drain_s_open_of_a_shared_store_says_it_ran_out_of_time(tmp_path, monkeypatch):
    """A store of its own raises it, and the drain reports the gap; the shared store does the same and does not return
    as if it had opened."""
    _log, spawned = _sharing(tmp_path, monkeypatch)
    view = native.store_for(tmp_path / "lancedb", table_name="PUBLIC", dimensions=2)
    try:
        with using_request_deadline(RequestDeadline.from_absolute(time.monotonic() - 1)):
            with pytest.raises(native._RequestBudgetExpired):
                view.open()
        assert spawned == [] and view.requires_reopen
    finally:
        native._close_shared()


def test_a_helper_asked_only_whether_lancedb_is_installed_holds_no_table(tmp_path, monkeypatch):
    """A helper asked only whether LanceDB is installed does not count as serving: the open is not skipped, and
    searches do not say the table is not open."""
    log, spawned = _sharing(tmp_path, monkeypatch)
    view = native.store_for(tmp_path / "lancedb", table_name="PUBLIC", dimensions=2)
    try:
        with using_request_deadline(RequestDeadline.from_budget(5)):
            assert view.is_available()
            view.open_existing()
            assert view.search_scopes([0.0, 1.0], scope_ids=["PUBLIC"], limit=1) == []
        assert log.read_text().split() == ["is_available", "open_existing", "search_scopes"] and spawned == [1]
    finally:
        native._close_shared()


def test_two_runtimes_that_open_the_shared_store_together_open_it_once(tmp_path, monkeypatch):
    """Two runtimes that both find the store closed open it once, and no search waits on a second open."""
    log, spawned = _sharing(tmp_path, monkeypatch, open_delay=0.3)
    views = [native.store_for(tmp_path / "lancedb", table_name="PUBLIC", dimensions=2) for _ in range(2)]
    work = []

    def opening(view):
        with using_request_deadline(RequestDeadline.from_budget(5)):
            view.open_existing_with_work(lambda: work.append(1))

    threads = [threading.Thread(target=opening, args=(view,)) for view in views]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        assert work == [1, 1] and log.read_text().split() == ["open_existing"] and spawned == [1]
    finally:
        native._close_shared()


def test_the_opening_runtime_s_embedding_does_not_hold_the_others_searches(tmp_path, monkeypatch):
    """Overlapped with the open, the opener's embedding does not hold the store's lock: another prompt's search with a
    second left does not time out on it.  The embedding is asked for when its recall starts anyway."""
    _log, _spawned = _sharing(tmp_path, monkeypatch)
    opener = native.store_for(tmp_path / "lancedb", table_name="PUBLIC", dimensions=2)
    other = native.store_for(tmp_path / "lancedb", table_name="PUBLIC", dimensions=2)
    embedding, release = threading.Event(), threading.Event()

    def opening():
        with using_request_deadline(RequestDeadline.from_budget(10)):
            opener.open_existing_with_work(lambda: (embedding.set(), release.wait(5)))

    thread = threading.Thread(target=opening)
    thread.start()
    try:
        assert embedding.wait(5)
        with using_request_deadline(RequestDeadline.from_budget(1)):
            assert other.search_scopes([0.0, 1.0], scope_ids=["PUBLIC"], limit=1) == []
    finally:
        release.set()
        thread.join(10)
        native._close_shared()


@pytest.mark.skipif(sys.platform != "win32", reason="the helper-process store is Windows'")
def test_a_server_s_factory_builds_views_of_one_store(tmp_path, monkeypatch):
    from scope_recall.vector.store import build_vector_store

    _sharing(tmp_path, monkeypatch)
    try:
        one = build_vector_store("lancedb", storage_dir=tmp_path, table_name="PUBLIC", dimensions=2)
        two = build_vector_store("lancedb", storage_dir=tmp_path, table_name="PUBLIC", dimensions=2)
        assert isinstance(one, native.SharedStore) and one._store is two._store
    finally:
        native._close_shared()


def test_helper_deadline_trace_distinguishes_drain_open_and_search(tmp_path, monkeypatch):
    from scope_recall.core.vector_failure import using_vector_trace

    s = store(tmp_path, monkeypatch, delay=0.3)
    opened, drained, searched = {}, {}, {}
    try:
        with using_vector_trace(opened), using_request_deadline(RequestDeadline.from_budget(0.04)):
            s.open_existing_with_work(lambda: None)
        assert opened["helper_failure_phase"] == "open"
        with pytest.raises(TimeoutError):
            with using_vector_trace(drained), using_request_deadline(RequestDeadline.from_budget(0.04)):
                s._call("search", [], scope_id="PUBLIC", limit=1)
        assert drained["helper_failure_phase"] == "drain"
        assert drained["helper_drain_ms"] >= 0
        assert drained["vector_started"] == drained["vector_finished"] == 0
        with using_request_deadline(RequestDeadline.from_budget(2)):
            s._drain_pending_response_locked()
        with pytest.raises(TimeoutError):
            with using_vector_trace(searched), using_request_deadline(RequestDeadline.from_budget(0.04)):
                s._call("search", [], scope_id="PUBLIC", limit=1)
        assert searched["helper_failure_phase"] == "search"
        assert searched["vector_started"] == 1 and searched["vector_finished"] == 0
    finally:
        s.close()


@pytest.mark.parametrize("failed", [False, True])
def test_native_search_trace_counts_successful_replies_only(tmp_path, monkeypatch, failed):
    from scope_recall.core.vector_failure import using_vector_trace

    s = store(tmp_path, monkeypatch)
    program = f"""import json,sys
for line in sys.stdin:
 r=json.loads(line)
 ok=not ({failed!r} and r['method']=='search')
 print(json.dumps(dict(id=r['id'],ok=ok,result=[],error='TEST search refused')),flush=True)
"""
    monkeypatch.setattr(native, "_worker_command", lambda: [sys.executable, "-I", "-B", "-c", program])
    trace = {}
    try:
        with using_request_deadline(RequestDeadline.from_budget(2)):
            s.open_existing()
            with using_vector_trace(trace):
                if failed:
                    with pytest.raises(RuntimeError):
                        s._call("search", [], scope_id="PUBLIC", limit=1)
                else:
                    assert s._call("search", [], scope_id="PUBLIC", limit=1) == []
        assert trace["vector_started"] == 1
        assert trace["vector_finished"] == int(not failed)
        assert not s.requires_reopen
    finally:
        s.close()
