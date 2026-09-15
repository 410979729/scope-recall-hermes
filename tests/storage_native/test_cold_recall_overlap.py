"""Actual Lance cold recall, real SQLite authority, public offline embeddings."""
from dataclasses import replace
import time

from scope_recall.contracts import InstanceBinding
from scope_recall.runtime.instance import RuntimeInstanceConfig, VectorRuntimeConfig, build_runtime_instance
from scope_recall.runtime.auxiliary import AuxiliaryRuntimeConfig
from scope_recall.core.recall_policy import RecallPolicy
from v11_support import source_event


class PublicEmbedding:
    def __init__(self,delay=0):self.delay=delay;self.queries=[]
    def embed_source(self,source,*,remaining_seconds):return (1.,0.)
    def embed_query(self,text,*,remaining_seconds):
        self.queries.append((text,remaining_seconds))
        time.sleep(self.delay)
        return (1.,0.)


def prepare(root):
    binding=InstanceBinding('TEST-cold-agent','TEST-cold-install',root/'truth',frozenset({'TEST-scope'}),True)
    config=RuntimeInstanceConfig(binding=binding,session_id='source',allowed_scope_ids=binding.scope_ids,
        auto_recall_seconds=4.,hook_processing_seconds=5.,request_seconds=45.,drain_seconds=45.,max_items=16,
        auxiliary=AuxiliaryRuntimeConfig.from_mapping({'external_embedding':False,'external_consolidation':False}),
        vector=VectorRuntimeConfig(backend='lancedb',storage_dir=root/'vectors',table_name='PUBLIC',dimensions=2,test_injection_override=True))
    runtime=build_runtime_instance(config)
    runtime.auxiliary=replace(runtime.auxiliary,source_embedding=PublicEmbedding(),query_embedding=PublicEmbedding())
    runtime.core.initialize()
    receipt=runtime.core.record_event(config.context(),source_event(source_event_key='PUBLIC-cold-source',
        content='海报四周压低明度，核心图案保留高光。'),scope_id='TEST-scope',remaining_seconds=10)
    runtime.drain()
    runtime.close()
    return replace(config,session_id='query'),receipt.event_refs[0].ref


def query(config,embedding):
    runtime=build_runtime_instance(config)
    runtime.auxiliary=replace(runtime.auxiliary,query_embedding=embedding)
    runtime.core.recall_pipeline.policy=RecallPolicy(vector_threshold=.5)
    return runtime


def packet(runtime,question='什么设计手法吸引注意焦点？',*,mode='auto'):
    return runtime.core.recall_packet(runtime.config.context(),{'protocol_version':'1.1','request_id':'PUBLIC-cold',
        'query':question,'mode':mode,'max_items':6,'budget_tokens':1200},deadline_seconds=45)


def test_real_cold_then_warm_embeds_once_per_request_and_checks_authority(tmp_path):
    config,ref=prepare(tmp_path)
    embedding=PublicEmbedding()
    runtime=query(config,embedding)
    try:
        assert any(item['ref']==ref for item in packet(runtime,mode='current')['items'])
        worker=runtime._vector_store._process
        assert any(item['ref']==ref for item in packet(runtime,mode='current')['items'])
        assert len(embedding.queries)==2
        assert runtime._vector_store._process is worker
        from scope_recall.contracts import TrustedContext
        # A new foreign installation cannot use the already-open native port
        # to hydrate this installation's SQLite truth.
        foreign=InstanceBinding('TEST-foreign','TEST-other',tmp_path/'foreign',config.binding.scope_ids,True)
        foreign_packet=runtime.core.recall_packet(TrustedContext(foreign,'query',foreign.scope_ids,'human_direct'),
            {'protocol_version':'1.1','request_id':'PUBLIC-foreign','query':'other',
             'mode':'auto','max_items':6,'budget_tokens':1200},deadline_seconds=45)
        assert not foreign_packet['items']
        assert len(embedding.queries)==2
    finally:runtime.close()
