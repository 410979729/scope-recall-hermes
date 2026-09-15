"""P07 source-to-structure contracts; real-model/host slices have separate receipts."""
from dataclasses import replace
import hashlib
import json
import sqlite3

import pytest

from scope_recall.contracts import ArtifactVersion,ContractError,DisplaySnapshot
from scope_recall.core.artifact_storage import artifact_identity
from scope_recall.core.episodes import source_watermark
from scope_recall.core.retained_artifacts import ArtifactGrant
from scope_recall.core.visibility import ObjectRef
from test_v11_claims import app,capture
from test_v11_deletion import authorize,request


def ref(source):return f'{source.ref}@{source.revision}'


def resume(source,**changes):
    refs=[ref(source)]
    value=dict(episode_ref=None,goal=dict(text=source.event['content'],evidence_refs=refs),
               decisions=[],verified_progress=[],open_items=[],blockers=[],next_step=None,next_step_basis='unknown',
               artifact_refs=[],source_watermark=source_watermark(refs),evidence_refs=refs)
    value.update(changes)
    return value


def apply(core,ctx,*,resumes=(),references=(),source_refs=None):
    refs=source_refs or list(dict.fromkeys(s for p in (*resumes,*references) for s in p['evidence_refs']))
    return core.accept_consolidation(ctx,dict(protocol_version='1.1',source_refs=refs,claim_proposals=[],
        resume_proposals=list(resumes),reference_proposals=list(references)),scope_id='TEST-scope',remaining_seconds=10)


def artifact(core,ctx,tmp_path,*,version=1,key='TEST-design',label='TEST-design',body=None):
    target=artifact_identity(ctx,'TEST-scope',key)
    display=DisplaySnapshot('observed',(ArtifactVersion(target,version),))
    source=capture(core,replace(ctx,display_snapshot=display),f'TEST 上传 {label} v{version}。',artifact_refs=[target],display_snapshot=display.to_payload())
    path=tmp_path/'TEST-design.svg'
    body=body or f'<svg xmlns="http://www.w3.org/2000/svg"><rect width="{version}" height="2"/></svg>'
    path.write_text(body,encoding='utf-8')
    grant=ArtifactGrant(path,hashlib.sha256(path.read_bytes()).hexdigest(),'image/svg+xml',4096)
    item=core.register_artifact(ctx,key=key,revision=version,scope_id='TEST-scope',source_ref=ref(source),label=label,
                                media_type='image/svg+xml',grant=grant,remaining_seconds=10)
    return item,source,path


def test_C37_plain_chat_has_provisional_episode_without_manufactured_goal(app):
    core,ctx=app
    source=capture(core,ctx,'TEST 今天看到晚霞，想起小时候。')
    item,=core.episodes(ctx)
    assert item.state=='unknown' and item.resume is None and item.unprocessed_events==1
    rows,cursor=core.episode_sources(ctx,item.ref)
    assert rows[0][1].event['content']==source.event['content'] and cursor is None
    with pytest.raises(ContractError,match='work_goal_unconfirmed'):
        apply(core,ctx,resumes=[resume(source)])
    assert core.episodes(ctx)[0].resume is None


def test_C25_one_session_changes_topic_without_cancelling_previous(app):
    core,ctx=app
    capture(core,ctx,'TEST-A先停在配色阶段。')
    capture(core,ctx,'换个话题，转到TEST-B报价。')
    episodes=core.episodes(ctx)
    assert len(episodes)==2 and all(e.state!='cancelled' for e in episodes)
    assert sorted(len(core.episode_sources(ctx,e.ref)[0]) for e in episodes)==[1,1]


def test_C26_M06_same_trusted_task_continues_across_sessions_and_branches_do_not(app):
    core,ctx=app
    first=replace(ctx,task_anchor='TEST-poster')
    capture(core,first,'TEST 结构已经确认，颜色还没调，下一步只调整配色。')
    capture(core,replace(first,session_id='TEST-next'),'继续做TEST配色。')
    assert len(core.episodes(first))==1
    other=replace(first,branch_id='TEST-experiment')
    capture(core,other,'TEST 整个任务已经完成。')
    assert core.episodes(other)[0].ref!=core.episodes(first)[0].ref
    assert core.episodes(other)[0].state=='completed' and core.episodes(first)[0].state=='open'


def test_same_title_in_new_session_without_anchor_does_not_merge(app):
    core,ctx=app
    capture(core,ctx,'TEST 海报排版没完。')
    capture(core,replace(ctx,session_id='TEST-other'),'TEST 海报排版没完。')
    assert len(core.episodes(ctx))==2


@pytest.mark.parametrize('origin,text,state',[
    ('tool_observation','TEST build exit_code: 1; 编译失败。','failed'),
    ('human_direct','取消这个任务，先校对。','cancelled'),
    ('host_generated','{"lifecycle":"interrupted"}','interrupted'),
    ('assistant_visible','整个任务已经完成。','unknown'),
    ('human_direct','先暂停，稍后继续。','open'),
])
def test_C02_C03_episode_state_tracks_actual_source_authority(app,origin,text,state):
    core,ctx=app
    capture(core,ctx,text,origin=origin)
    assert core.episodes(ctx)[0].state==state


def test_resume_is_extractive_versioned_and_stale_environment_is_labelled(app):
    core,ctx=app
    ctx=replace(ctx,environment_revision='TEST-head-h1')
    source=capture(core,ctx,'TEST 结构已经确认，颜色还没调，下一步只调整配色。')
    proposal=resume(source,verified_progress=[dict(text='结构已经确认',evidence_refs=[ref(source)])],
                    open_items=[dict(text='颜色还没调',evidence_refs=[ref(source)])],next_step='只调整配色',next_step_basis='user_requested')
    apply(core,ctx,resumes=[proposal])
    item,=core.episodes(ctx)
    assert item.resume['verified_progress'][0]['text']=='结构已经确认' and item.unprocessed_events==0
    before=core.storage.path.read_bytes()
    apply(core,ctx,resumes=[proposal])
    assert core.storage.path.read_bytes()==before
    fresh=replace(ctx,session_id='TEST-new',environment_revision='TEST-head-h2')
    historical,=core.episodes(fresh)
    assert historical.needs_revalidation and 'environment_needs_revalidation' in historical.gaps


def test_new_event_or_rebuilt_summary_does_not_revalidate_old_environment(app):
    core,ctx=app
    ctx=replace(ctx,environment_revision='TEST-h1')
    source=capture(core,ctx,'TEST 海报结构已经确认，下一步检查颜色。')
    proposal=resume(source,verified_progress=[dict(text='结构已经确认',evidence_refs=[ref(source)])])
    apply(core,ctx,resumes=[proposal])
    changed=replace(ctx,environment_revision='TEST-h2',session_id=ctx.session_id)
    latest=capture(core,changed,'TEST 新工作区已经打开。',origin='tool_observation',when='2026-09-02T12:00:00Z')
    assert core.episodes(changed)[0].needs_revalidation
    refs=[ref(source),ref(latest)]
    rebuilt=dict(proposal,evidence_refs=refs,source_watermark=source_watermark(refs))
    apply(core,changed,resumes=[rebuilt])
    assert core.episodes(changed)[0].needs_revalidation
    assert core.episodes(changed)[0].unprocessed_events==0


def test_occurrence_order_respects_fractional_utc_seconds(app):
    core,ctx=app
    capture(core,ctx,'TEST 整个任务已经完成。',when='2026-09-02T12:00:00.900Z')
    capture(core,ctx,'TEST 新日志到达。',when='2026-09-02T12:00:00Z')
    capture(core,ctx,'TEST 编译失败。',origin='tool_observation',when='2026-09-02T12:00:00.500Z')
    assert core.episodes(ctx)[0].state=='completed'


@pytest.mark.parametrize('text,origin,progress',[
    ('TEST 我将完成配色。','assistant_visible','完成配色'),
    ('TEST 配色还没完成。','human_direct','完成'),
    ('TEST 假设配色已完成。','human_direct','配色已完成'),
])
def test_model_cannot_promote_promise_negative_or_hypothesis_to_verified_progress(app,text,origin,progress):
    core,ctx=app
    source=capture(core,ctx,text,origin=origin)
    proposal=resume(source,verified_progress=[dict(text=progress,evidence_refs=[ref(source)])])
    before=core.storage.path.read_bytes()
    with pytest.raises(ContractError):apply(core,ctx,resumes=[proposal])
    assert core.storage.path.read_bytes()==before and core.episodes(ctx)[0].resume is None


def test_bad_watermark_or_old_source_revision_cannot_mark_processing_complete(app):
    core,ctx=app
    source=capture(core,ctx,'TEST 结构已经确认。',key='TEST-versioned')
    bad=resume(source,source_watermark='invented')
    with pytest.raises(ContractError,match='source_watermark'):apply(core,ctx,resumes=[bad])
    capture(core,ctx,'TEST 结构尚未确认。',key='TEST-versioned',revision=2)
    with pytest.raises(ContractError):apply(core,ctx,resumes=[resume(source)])
    assert all(e.resume is None for e in core.episodes(ctx))


def test_resume_cannot_merge_two_topics_or_invent_local_artifact_access(app):
    core,ctx=app
    one=capture(core,ctx,'TEST-A设计图。')
    two=capture(core,ctx,'换个话题，TEST-B报价。')
    refs=[ref(one),ref(two)]
    with pytest.raises(ContractError,match='episode_membership_ambiguous'):
        apply(core,ctx,resumes=[resume(one,evidence_refs=refs,source_watermark=source_watermark(refs))])
    with pytest.raises(ContractError):apply(core,ctx,resumes=[resume(one,artifact_refs=['artifact-'+('f'*64)+'@1'])])


def test_C16_M47_same_path_overwrite_preserves_exact_prior_artifact(app,tmp_path):
    core,ctx=app
    one,source,path=artifact(core,ctx,tmp_path)
    first=path.read_bytes()
    two,_,_=artifact(core,ctx,tmp_path,version=2)
    assert one.ref==two.ref and one.sha256!=two.sha256
    reopened,data=core.open_artifact(replace(ctx,session_id='TEST-fresh'),one.ref,1)
    assert data==first and reopened.sha256==hashlib.sha256(first).hexdigest()
    assert core.open_artifact(ctx,two.ref,2)[1]==path.read_bytes()
    assert core.artifact(replace(ctx,project_id='TEST-other'),one.ref,1) is None


def test_C17_M49_described_only_artifact_has_no_fabricated_bytes(app):
    core,ctx=app
    target=artifact_identity(ctx,'TEST-scope','TEST-lost')
    source=capture(core,ctx,'TEST 当时左边是蓝色方块。',artifact_refs=[target])
    item=core.register_artifact(ctx,key='TEST-lost',revision=1,scope_id='TEST-scope',source_ref=ref(source),
         label='TEST-lost',media_type='image/png',description='左边是蓝色方块',remaining_seconds=10)
    assert item.retention_state=='described_artifact' and item.sha256 is None
    with pytest.raises(ContractError,match='artifact_not_retained'):core.open_artifact(ctx,target,1)


def test_M13_M14_display_order_binds_version_only_when_actually_observed(app,tmp_path):
    core,ctx=app
    one,_,_=artifact(core,ctx,tmp_path,key='TEST-one',label='TEST-one')
    two,_,_=artifact(core,ctx,tmp_path,key='TEST-two',label='TEST-two')
    for order,expected in [('unknown','ambiguous'),('observed','resolved')]:
        display=DisplaySnapshot(order,(ArtifactVersion(two.ref,1),ArtifactVersion(one.ref,1)))
        source=capture(core,replace(ctx,display_snapshot=display),'TEST 第二张的颜色需要改。',artifact_refs=[one.ref,two.ref],display_snapshot=display.to_payload())
        proposal=dict(mention='第二张',candidate_refs=[f'{one.ref}@1',f'{two.ref}@1'],resolved_ref=f'{two.ref}@1',resolution='resolved',evidence_refs=[ref(source)])
        result=apply(core,ctx,references=[proposal])
        saved=core.reference(ctx,result.items[0].ref)
        assert saved.payload['resolution']==expected
        assert saved.payload['resolved_ref']==(f'{one.ref}@1' if expected=='resolved' else None)


def test_M50_deleted_artifact_blocks_descriptions_references_resume_and_cached_release(app,tmp_path):
    core,ctx=app
    item,source,_=artifact(core,ctx,tmp_path)
    work=capture(core,ctx,'TEST 下一步调整这张图的配色。',artifact_refs=[item.ref])
    refs=[ref(source),ref(work)]
    proposal=resume(work,artifact_refs=[f'{item.ref}@1'],evidence_refs=refs,source_watermark=source_watermark(refs))
    apply(core,ctx,resumes=[proposal])
    episode=core.episodes(ctx)[0]
    epoch=core.status(ctx).memory_epoch
    authorize(core,ctx,item)
    operation=core.forget(ctx,request(item),remaining_seconds=10)
    assert core.artifact(ctx,item.ref,1) is None and not core.episodes(ctx)
    with pytest.raises(ContractError):core.open_artifact(ctx,item.ref,1)
    with pytest.raises(ContractError):core.release_objects(ctx,(ObjectRef('episode',episode.ref,episode.revision),),expected_epoch=epoch,automatic=False,history=True)
    result=core.purge_attachments(ctx,operation['operation_id'],remaining_seconds=10)
    assert result['layers']['attachments']=='removed' and not result['declared_scope_complete']
    from scope_recall.core.retained_artifacts import read_retained
    with pytest.raises(ContractError):read_retained(ctx.binding,item.blob)


def test_resume_keeps_unprocessed_tail_and_source_paging_is_not_a_false_complete_count(app):
    core,ctx=app
    one=capture(core,ctx,'TEST 先做版式。')
    capture(core,ctx,'TEST 颜色还没做。')
    apply(core,ctx,resumes=[resume(one)])
    episode,=core.episodes(ctx)
    assert episode.unprocessed_events==1 and 'resume_requires_rebuild' in episode.gaps
    first,cursor=core.episode_sources(ctx,episode.ref,limit=1)
    second,end=core.episode_sources(ctx,episode.ref,after_sequence=cursor,limit=1)
    assert len(first)==len(second)==1 and first[0][1].ref!=second[0][1].ref and end is None


def test_long_episode_uses_bounded_segments_and_preserves_final_source(app):
    core,ctx=app
    ctx=replace(ctx,task_anchor='TEST-long-task')
    sources=[capture(core,ctx,f'TEST raw attempt {index}') for index in range(201)]
    episodes=core.episodes(ctx)
    assert len(episodes)==2
    restored=[]
    for episode in episodes:
        cursor=0
        while True:
            rows,next_page=core.episode_sources(ctx,episode.ref,after_sequence=cursor,limit=32)
            restored.extend(s.ref for _,s in rows)
            if next_page is None:break
            cursor=next_page
    assert len(restored)==201 and set(restored)=={s.ref for s in sources}
    assert sum(e.unprocessed_events for e in episodes)==201


def test_old_arriving_failure_does_not_replace_newer_episode_completion(app):
    core,ctx=app
    ctx=replace(ctx,task_anchor='TEST-ordered')
    capture(core,ctx,'TEST 整个任务已经完成。',when='2026-09-04T12:00:00Z')
    capture(core,ctx,'TEST build failed.',origin='tool_observation',when='2026-09-01T12:00:00Z')
    assert core.episodes(ctx)[0].state=='completed'


def test_M15_explicit_clarification_updates_binding_and_preserves_old_version(app,tmp_path):
    core,ctx=app
    ctx=replace(ctx,task_anchor='TEST-one-work')
    one,_,_=artifact(core,ctx,tmp_path,label='TEST-v1')
    two,_,_=artifact(core,ctx,tmp_path,version=2,label='TEST-v2')
    source=capture(core,ctx,'TEST 那个颜色不行。')
    proposal=dict(mention='那个颜色',candidate_refs=[f'{one.ref}@1',f'{two.ref}@2'],resolved_ref=None,resolution='ambiguous',evidence_refs=[ref(source)])
    original=apply(core,ctx,references=[proposal]).items[0]
    clarification=capture(core,ctx,'刚才“那个颜色”说的是TEST-v2。')
    updated=dict(proposal,evidence_refs=[ref(clarification)])
    latest=apply(core,ctx,references=[updated]).items[0]
    assert latest.ref==original.ref and latest.revision==2
    assert core.reference(ctx,latest.ref).payload['resolved_ref']==f'{two.ref}@2'
    assert core.reference(ctx,latest.ref,1).payload['resolution']=='ambiguous'
