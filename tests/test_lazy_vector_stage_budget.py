"""A vector-stage deadline may be shorter than its parent recall context."""
from types import SimpleNamespace
import time

import pytest

from scope_recall.adapters.lance import LanceVectorPort
from scope_recall.core.retrieval import SearchContext, SearchLimits
from scope_recall.runtime.instance import _LazyVectorPort
from tests.v11_support import context as trusted_context


def search_context(tmp_path,deadline):
    return SearchContext(query='PUBLIC query',mode='auto',as_of=None,focus_refs=(),limits=SearchLimits(),
        deadline=deadline,now='2026-09-08T00:00:00Z',trusted_context=trusted_context(tmp_path))


@pytest.mark.parametrize('cold',[False,True])
def test_short_stage_budget_is_one_absolute_deadline_through_open_embed_search(tmp_path,monkeypatch,cold):
    clock=[100.]
    monkeypatch.setattr(time,'monotonic',lambda:clock[0])
    seen={}
    class Embedding:
        def embed_query(self,text,*,remaining_seconds):
            seen.setdefault('embedding',[]).append(remaining_seconds)
            clock[0]+=.03
            return [1.,0.]
    class Store:
        def search(self,*args,**kwargs):
            from scope_recall._internal.recall.deadline import remaining_seconds
            seen['native_remaining']=remaining_seconds()
            return []
    embedding=Embedding()
    real_port=LanceVectorPort(Store(),embedding)
    class Port:
        def search(self,context,*,limit,remaining_seconds,**kwargs):
            seen['search_deadline']=context.deadline
            seen['search_remaining']=remaining_seconds
            return real_port.search(context,limit=limit,remaining_seconds=remaining_seconds,**kwargs)
    def ensure(*,allow_create,deadline,during_open):
        assert allow_create is False
        seen['open_deadline']=deadline
        clock[0]+=.02
        if cold:during_open()
        return Port()
    facade=_LazyVectorPort(SimpleNamespace(auxiliary=SimpleNamespace(query_embedding=embedding),_ensure_vector_port=ensure))
    facade.search(search_context(tmp_path,110.),limit=6,remaining_seconds=.1)
    assert seen['open_deadline']==pytest.approx(100.1)
    assert seen['search_deadline']==pytest.approx(100.1)
    assert seen['embedding']==pytest.approx([.08])
    assert seen['search_remaining']==pytest.approx(.05 if cold else .08)
    assert seen['native_remaining']==pytest.approx(.05)


def test_short_stage_budget_times_out_and_reaps_native_open(tmp_path,monkeypatch):
    from tests.test_cold_open_overlap import store
    native=store(tmp_path,monkeypatch,delay=.5)
    workers=[]
    original_start=native._start
    def start():
        original_start()
        workers.append(native._process)
    monkeypatch.setattr(native,'_start',start)
    calls=[]
    class Embedding:
        def embed_query(self,text,*,remaining_seconds):
            calls.append(remaining_seconds)
            return [1.,0.]
    def ensure(*,allow_create,deadline,during_open):
        native.open_existing_with_work(during_open)
        pytest.fail('expired native open accepted')
    facade=_LazyVectorPort(SimpleNamespace(auxiliary=SimpleNamespace(query_embedding=Embedding()),_ensure_vector_port=ensure))
    try:
        with pytest.raises((RuntimeError,TimeoutError)):
            facade.search(search_context(tmp_path,time.monotonic()+10),limit=6,remaining_seconds=.1)
        assert len(calls)<=1 and all(0<value<=.1 for value in calls)
    finally:native.close()
    assert workers and all(worker.poll() is not None for worker in workers)
