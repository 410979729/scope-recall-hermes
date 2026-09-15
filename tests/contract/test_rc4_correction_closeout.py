"""Frozen failed model outputs: no resampling or manual promotion."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from scope_recall.contracts import TrustedSourcePrincipal
from scope_recall.core.claim_normalization import normalize_frame, repair_frames
from test_v11_claims import app as app, capture, accept


def fixture(core, ctx, *, other_owner=False, unknown_time=False, filename='rc3-correction-failure.json'):
    records=json.loads((Path(__file__).parents[1]/'fixtures'/filename).read_text(encoding='utf-8'))
    sources=[]
    for index, record in enumerate(records):
        actor=replace(ctx,session_id=f'TEST-session-{index}',source_principal=TrustedSourcePrincipal(
            'human','verified',principal_ref='principal:TEST-other' if other_owner and index else 'principal:TEST-owner'))
        core.clock.now=record['recorded_at']
        source=capture(core,actor,record['content'],key=f'TEST-record-{index}',when=None if unknown_time else record['occurred_at'])
        sources.append(source)
    for index,record in enumerate(records):
        for proposal in record['proposals']:
            if unknown_time:
                proposal['valid_from']=None
            for span in proposal['evidence_spans']:
                span['source_ref']=sources[int(span['source_ref'].rsplit('-',1)[-1])-1].ref
            if 'intention' in proposal:
                proposal['intention']['state_evidence_refs']=[f'{sources[index].ref}@1']
    core.clock.now='2026-09-14T07:00:00Z'
    return records,sources


def heads(core,ctx):
    with core.storage.read(ctx) as tx:
        return [v for ref in tx.claims.list_refs() for v in tx.claims.versions(ref)
                if v.revision==v.current_revision and v.state=='active']


def assert_correct(core,ctx):
    active=heads(core,ctx)
    silver=[v for v in active if v.payload['value_text']=='银色']
    green=[v for v in active if v.payload['value_text']=='绿色']
    assert len(silver)==len(green)==1,[(v.payload,v.reason) for v in active]
    assert silver[0].payload['subject']=='内部技术总结'
    assert silver[0].payload['conditions']==['项目【RC2SOL-青岚】']
    assert set(green[0].payload['conditions'])=={'项目【RC2SOL-青岚】','对外版'}
    assert not any(any(x in v.payload['value_text'] for x in ('蓝色','红色','橙色','临时尾标')) for v in active)
    view = core.profile(ctx, {'protocol_version':'1.1','request_id':'TEST-current-profile',
                            'subject':'内部技术总结','max_items':16,'budget_tokens':4096})
    assert '银色' in json.dumps(view,ensure_ascii=False)
    assert '蓝色' not in json.dumps(view,ensure_ascii=False)


@pytest.mark.parametrize('order',[(0,1),(1,0)])
@pytest.mark.parametrize('unknown_time',[False,True])
def test_frozen_correction_survives_delayed_and_reversed_consolidation(app,order,unknown_time):
    core,ctx=app
    records,_=fixture(core,ctx,unknown_time=unknown_time)
    for index in order:
        accept(core,ctx,*records[index]['proposals'])
    assert_correct(core,ctx)
    # Re-extraction of the earlier source must not resurrect the old value.
    accept(core,ctx,*records[0]['proposals'])
    assert_correct(core,ctx)


def test_legacy_failure_is_repaired_without_model_or_rewriting_history(app,monkeypatch):
    core,ctx=app
    records,_=fixture(core,ctx)
    with monkeypatch.context() as patch:
        patch.setattr('scope_recall.core.claim_normalization.normalize_frame',lambda p,r:p)
        for record in records:
            accept(core,ctx,*record['proposals'])
    assert any('蓝色' in v.payload['value_text'] for v in heads(core,ctx))
    with core.storage.read(ctx) as tx:
        original={(v.ref,v.revision):deepcopy(v.payload) for ref in tx.claims.list_refs() for v in tx.claims.versions(ref)}
    cursor=''
    for _ in range(12):
        with core.storage.write(ctx) as tx:
            receipt=repair_frames(tx,now=core.clock.utc_now(),after_ref=cursor,limit=2)
        assert not receipt['errors'],receipt
        cursor=receipt['cursor']
        if receipt['done']:
            break
    else:
        pytest.fail('repair did not finish')
    assert_correct(core,ctx)
    with core.storage.read(ctx) as tx:
        current={(v.ref,v.revision):v.payload for ref in tx.claims.list_refs() for v in tx.claims.versions(ref)}
    assert all(current[key]==value for key,value in original.items())
    with core.storage.write(ctx) as tx:
        second = repair_frames(tx,now=core.clock.utc_now(),limit=32)
    assert not second['errors']
    with core.storage.read(ctx) as tx:
        repeated={(v.ref,v.revision):v.payload for ref in tx.claims.list_refs() for v in tx.claims.versions(ref)}
    assert repeated==current


def test_candidate_worker_uses_the_same_literal_frame_normalization(app,monkeypatch):
    from test_r1_candidate_lifecycle import Evaluator, _finish_source_work
    core,ctx=app
    records,_=fixture(core,ctx)
    proposal=records[1]['proposals'][1]
    with monkeypatch.context() as patch:
        patch.setattr('scope_recall.core.claim_normalization.normalize_frame',lambda p,r:p)
        assert accept(core,ctx,proposal).items[0].state=='proposed'
    _finish_source_work(core)
    model=Evaluator(proposal)
    receipt=core.drain_worker(ctx,remaining_seconds=10,max_items=1,consolidation=model)
    assert receipt.completed==1 and model.calls==1,receipt
    assert heads(core,ctx)[0].payload['value_text']=='银色'


def test_literal_condition_wrappers_align_even_when_model_already_split_the_subject(app):
    core,ctx=app
    records,_=fixture(core,ctx)
    old=records[0]['proposals'][1]
    old.update(subject='内部技术总结',value_text='蓝色标题')
    accept(core,ctx,old)
    accept(core,ctx,*records[1]['proposals'])
    assert_correct(core,ctx)


def test_different_speaker_cannot_replace_a_preference(app):
    core,ctx=app
    records,_=fixture(core,ctx,other_owner=True)
    for record in records:
        accept(core,ctx,*record['proposals'])
    active=heads(core,ctx)
    assert any('蓝色' in v.payload['value_text'] for v in active)
    assert not any(v.payload['value_text']=='银色' for v in active)


@pytest.mark.parametrize('replacement',[
    '项目【RC2SOL-另一项目】内部技术总结',
    '项目【RC2SOL-青岚】不存在的主体',
])
def test_normalization_does_not_repair_invented_subjects(app,replacement):
    core,ctx=app
    records,sources=fixture(core,ctx)
    proposal=deepcopy(records[1]['proposals'][1])
    proposal['subject']=replacement
    with core.storage.read(ctx) as tx:
        roots=tx.claims.roots((f'{sources[1].ref}@1',))
    assert normalize_frame(proposal,roots)==proposal
    result=accept(core,ctx,proposal)
    assert result.items[0].state=='proposed'


@pytest.mark.parametrize('tail',['不是事实','不是确认','不是认真的','这不是真的'])
def test_separate_statement_denial_is_not_treated_as_rejected_old_value(app,tail):
    core,ctx=app
    actor=replace(ctx,source_principal=TrustedSourcePrincipal('human','verified',principal_ref='principal:TEST-owner'))
    text=f'项目【TEST-project】内部技术总结应使用银色，{tail}。'
    source=capture(core,actor,text,when=None)
    proposal={'kind':'preference','subject':'项目【TEST-project】内部技术总结','predicate':'应使用','value_text':'银色',
        'conditions':[],'statement_kind':'assertion','valid_from':None,'valid_to':None,
        'evidence_spans':[{'source_ref':source.ref,'source_revision':1,'quote':text}]}
    assert accept(core,ctx,proposal).items[0].state=='proposed'


@pytest.mark.parametrize('order',[(0,1),(1,0)])
def test_fresh_model_composite_correction_and_misplaced_sibling_condition(app,order):
    core,ctx=app
    records,_=fixture(core,ctx,filename='rc4-fresh-correction-failure.json')
    for index in order:
        accept(core,ctx,*records[index]['proposals'])
    active=heads(core,ctx)
    assert [(v.payload['subject'],v.payload['value_text']) for v in active if v.payload['subject']=='会议纪要']==[('会议纪要','楷体')]
    external=[v for v in active if v.payload['subject']=='对外版']
    with core.storage.read(ctx) as tx:
        diagnostic=[(v.payload['subject'],v.payload['value_text'],v.state,v.reason) for ref in tx.claims.list_refs() for v in tx.claims.versions(ref)]
    assert len(external)==1 and external[0].payload['value_text']=='黑体',diagnostic
    assert set(external[0].payload['conditions'])=={'项目【RC4-松石】','对外版'}
    assert not any('宋体' in v.payload['value_text'] for v in active)
    view=core.profile(ctx,dict(protocol_version='1.1',request_id='TEST-fresh-read',subject='会议纪要',max_items=16,budget_tokens=4096))
    assert '楷体' in json.dumps(view,ensure_ascii=False) and '宋体' not in json.dumps(view,ensure_ascii=False)


def test_repair_of_preexisting_composite_frame_retires_duplicate_without_rewriting_payload(app,monkeypatch):
    core,ctx=app
    records,_=fixture(core,ctx,filename='rc4-fresh-correction-failure.json')
    with monkeypatch.context() as patch:
        patch.setattr('scope_recall.core.claim_normalization.normalize_frame',lambda p,r:p)
        for record in records:
            accept(core,ctx,*record['proposals'])
    with core.storage.read(ctx) as tx:
        original={(v.ref,v.revision):v.payload for ref in tx.claims.list_refs() for v in tx.claims.versions(ref)}
    result=core.repair_claim_frames(ctx,limit=32,remaining_seconds=10)
    assert not result['errors'],result
    active=heads(core,ctx)
    assert any(v.payload['value_text']=='楷体' for v in active)
    assert any(v.payload['value_text']=='黑体' and v.payload['subject']=='对外版' for v in active)
    assert not any('宋体' in v.payload['value_text'] for v in active)
    with core.storage.read(ctx) as tx:
        current={(v.ref,v.revision):v.payload for ref in tx.claims.list_refs() for v in tx.claims.versions(ref)}
    assert all(current[key]==value for key,value in original.items())
    core.repair_claim_frames(ctx,limit=32,remaining_seconds=10)
    with core.storage.read(ctx) as tx:
        repeated={(v.ref,v.revision):v.payload for ref in tx.claims.list_refs() for v in tx.claims.versions(ref)}
    assert repeated==current
