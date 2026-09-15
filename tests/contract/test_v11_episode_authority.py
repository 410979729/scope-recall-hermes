"""P07 authority regressions for episode state and reference versions."""
from dataclasses import replace

import pytest

from scope_recall.contracts import ContractError, DisplaySnapshot, ArtifactVersion
from scope_recall.core.episodes import source_watermark, state_from_sources
from scope_recall.core.artifact_storage import artifact_identity
from test_v11_claims import app, capture
from test_v11_episodes import apply, artifact, ref, resume


def test_question_and_negated_failure_do_not_close_or_fail_episode(app):
    core, ctx = app
    capture(core, ctx, "TEST 整个任务完成了吗？")
    assert core.episodes(ctx)[0].state == "unknown"
    capture(core, ctx, "TEST 任务没有失败，仍在运行。")
    assert core.episodes(ctx)[0].state != "failed"


@pytest.mark.parametrize('text,expected',[
    ('TEST 整个任务已经完成了吗','open'),
    ('TEST 是否任务已经完成','open'),
    ('TEST 并非任务已经完成','open'),
    ('TEST 不要再取消整个任务','open'),
    ('TEST 没有任何失败','open'),
    ('TEST 整个任务已经完成','completed'),
])
def test_terminal_state_requires_affirmative_assertion_in_actual_core(app,text,expected):
    core,ctx=app
    capture(core,ctx,'TEST 继续处理海报。')
    assert core.episodes(ctx)[0].state=='open'
    capture(core,ctx,text)
    assert core.episodes(ctx)[0].state==expected


@pytest.mark.parametrize('origin,text',[
    # The exact shape behind tianshu's 32 failed episode versions: the word is
    # an adjective on something else, deep inside a long tool transcript.
    ('tool_observation','TEST 巡检报告：' + 'x' * 400 + ' legacy recall is verified; two prior failed questions remain in evidence.'),
    # An English conditional. 如果/假如/假设 were covered; if / in case were not.
    ('human_direct','TEST 记住 If a tool failed because of setup state, capture the fix.'),
    ('human_direct','TEST 注意 in case the upload failed, retry once.'),
    # One sub-step reporting failure is not the episode reporting failure.
    ('tool_observation','TEST 日志 sync_turn failed: adapter is not initialized'),
])
def test_incidental_mention_of_failure_does_not_close_a_live_episode(app,origin,text):
    core,ctx=app
    capture(core,ctx,'TEST 继续处理海报。')
    assert core.episodes(ctx)[0].state=='open'
    capture(core,ctx,text,origin=origin)
    # Still open, and so still eligible for resume injection, which admits only
    # open and interrupted.  A wrong 'failed' drops live work silently.
    assert core.episodes(ctx)[0].state=='open'


@pytest.mark.parametrize('text,expected',[
    ('TEST 任务做完了。','completed'),
    ('TEST 全部搞定。','completed'),
    ('TEST the task is done.','completed'),
    ('TEST 任务失败了。','failed'),
    ('TEST the task has failed.','failed'),
])
def test_terminal_markers_are_symmetric_and_each_names_what_reached_the_state(app,text,expected):
    core,ctx=app
    capture(core,ctx,'TEST 继续处理海报。')
    capture(core,ctx,text)
    assert core.episodes(ctx)[0].state==expected


def test_machine_exit_code_still_ends_the_episode_without_prose(app):
    core,ctx=app
    capture(core,ctx,'TEST 继续处理海报。')
    capture(core,ctx,'TEST build exit_code: 1',origin='tool_observation')
    assert core.episodes(ctx)[0].state=='failed'


def test_question_source_cannot_be_relabelled_verified_progress(app):
    core, ctx = app
    goal = capture(core, ctx, "TEST 请完成海报任务。")
    source = capture(core, ctx, "TEST 整个任务完成了吗？")
    refs = [ref(goal), ref(source)]
    proposal = resume(goal, evidence_refs=refs, source_watermark=source_watermark(refs),
                      verified_progress=[{"text": "整个任务完成", "evidence_refs": [ref(source)]}])
    with pytest.raises(ContractError, match="resume_authority"):
        apply(core, ctx, resumes=[proposal])


def test_trusted_task_anchor_does_not_turn_question_into_goal(app):
    core, ctx = app
    ctx = replace(ctx, task_anchor="TEST-existing-task")
    source = capture(core, ctx, "TEST 请确认是否改成蓝色？")
    with pytest.raises(ContractError, match="goal_authority"):
        apply(core, ctx, resumes=[resume(source)])


def test_question_source_cannot_be_relabelled_as_decision(app):
    core, ctx = app
    goal = capture(core, ctx, "TEST 请完成海报任务。")
    source = capture(core, ctx, "TEST 改成蓝色吗？")
    refs = [ref(goal), ref(source)]
    proposal = resume(goal, evidence_refs=refs, source_watermark=source_watermark(refs),
                      decisions=[{"text": "改成蓝色", "evidence_refs": [ref(source)]}])
    with pytest.raises(ContractError, match="resume_authority"):
        apply(core, ctx, resumes=[proposal])


def test_assistant_goal_cannot_create_work_authority(app):
    core, ctx = app
    source = capture(core, ctx, "TEST 整个任务已经完成。", origin="assistant_visible")
    with pytest.raises(ContractError, match="goal_authority"):
        apply(core, ctx, resumes=[resume(source)])


@pytest.mark.parametrize('raw,text,field,basis',[
    ('TEST 我没有确认蓝色。','确认蓝色','decisions',None),
    ('TEST 蓝色已经确认了吗','蓝色已经确认','decisions',None),
    ('TEST 不要删除海报。','删除海报','next_step','user_requested'),
    ('TEST 可以删除海报吗？','删除海报','next_step','user_requested'),
    ('TEST 不要删除海报。','删除海报','next_step','existing_plan'),
    ('TEST Can we delete the poster?','delete the poster','next_step','existing_plan'),
    ('TEST Do not delete the poster.','delete the poster','next_step','user_requested'),
    ('TEST 只调整蓝色。','调整蓝色','next_step','user_requested'),
])
def test_trimmed_negation_or_question_cannot_authorize_resume(app,raw,text,field,basis):
    core,ctx=app
    goal=capture(core,ctx,'TEST 请完成海报任务。')
    source=capture(core,ctx,raw)
    refs=[ref(goal),ref(source)]
    kwargs={field:[dict(text=text,evidence_refs=[ref(source)])]} if field=='decisions' else dict(next_step=text,next_step_basis=basis)
    proposal=resume(goal,evidence_refs=refs,source_watermark=source_watermark(refs),**kwargs)
    before=core.storage.path.read_bytes()
    with pytest.raises(ContractError):apply(core,ctx,resumes=[proposal])
    assert core.storage.path.read_bytes()==before and core.episodes(ctx)[0].resume is None


def test_negative_decision_keeps_its_prohibition_and_separate_next_step(app):
    core,ctx=app
    goal=capture(core,ctx,'TEST 请完成海报任务。')
    source=capture(core,ctx,'TEST 不要删除海报，下一步只调整蓝色。')
    refs=[ref(goal),ref(source)]
    proposal=resume(goal,evidence_refs=refs,source_watermark=source_watermark(refs),
        decisions=[dict(text='不要删除海报',evidence_refs=[ref(source)])],next_step='只调整蓝色',next_step_basis='user_requested')
    apply(core,ctx,resumes=[proposal])
    assert core.episodes(ctx)[0].resume['decisions'][0]['text']=='不要删除海报'
    assert core.episodes(ctx)[0].resume['next_step']=='只调整蓝色'


def test_zero_ordinal_is_not_last_item():
    from scope_recall.core.reference_storage import ordinal
    assert ordinal("第0张", 3) is None


def test_resume_cannot_borrow_unmentioned_version_of_same_artifact(app,tmp_path):
    core,ctx=app
    goal=capture(core,ctx,'TEST 请调整海报。')
    one,source1,_=artifact(core,ctx,tmp_path,version=1)
    two,source2,_=artifact(core,ctx,tmp_path,version=2)
    refs=[ref(goal),ref(source1)]
    proposal=resume(goal,artifact_refs=[f'{two.ref}@2'],evidence_refs=refs,source_watermark=source_watermark(refs))
    before=core.storage.path.read_bytes()
    with pytest.raises(ContractError,match='artifact_version_evidence'):
        apply(core,ctx,resumes=[proposal])
    assert core.storage.path.read_bytes()==before and core.episodes(ctx)[0].resume is None
    refs.append(ref(source2))
    apply(core,ctx,resumes=[dict(proposal,evidence_refs=refs,source_watermark=source_watermark(refs))])
    assert core.episodes(ctx)[0].resume['artifact_refs']==[f'{one.ref}@2']


@pytest.mark.parametrize('raw,mention',[
    ('入口海报有1个标题。','入口海报'),
    ('入口海报旁边是封面 v1。','入口海报'),
    ('如果第二张要改，就把它移到前面。','第二张'),
    ('不是第二张。','第二张'),
    ('入口海报 v1 是我要的吗？','入口海报'),
    ('刚才那个是入口海报 v1 吗','那个'),
    ('第二张就是要修改的吗','第二张'),
    ('入口海报 v1.2。','入口海报'),
])
def test_non_confirmation_does_not_resolve_observed_artifact(app,tmp_path,raw,mention):
    core,ctx=app
    one,_,_=artifact(core,ctx,tmp_path,key='TEST-a',label='入口海报')
    two,_,_=artifact(core,ctx,tmp_path,key='TEST-b',label='封面')
    display=DisplaySnapshot('observed',(ArtifactVersion(one.ref,1),ArtifactVersion(two.ref,1)))
    source=capture(core,replace(ctx,display_snapshot=display),raw,display_snapshot=display.to_payload(),artifact_refs=[one.ref,two.ref])
    mutation=apply(core,ctx,references=[dict(mention=mention,candidate_refs=[f'{one.ref}@1',f'{two.ref}@1'],resolved_ref=None,
                                         resolution='ambiguous',evidence_refs=[ref(source)])]).items[0]
    assert core.reference(ctx,mutation.ref).payload['resolved_ref'] is None


def test_exact_artifact_identity_disambiguates_same_label(app,tmp_path):
    core,ctx=app
    one,_,_=artifact(core,ctx,tmp_path,key='TEST-a',label='入口海报')
    two,_,_=artifact(core,ctx,tmp_path,key='TEST-b',label='入口海报')
    source=capture(core,ctx,f'刚才那个是 {two.ref}@1。')
    mutation=apply(core,ctx,references=[dict(mention='刚才那个',candidate_refs=[f'{one.ref}@1',f'{two.ref}@1'],resolved_ref=None,
                                         resolution='ambiguous',evidence_refs=[ref(source)])]).items[0]
    assert core.reference(ctx,mutation.ref).payload['resolved_ref']==f'{two.ref}@1'


def test_stale_reference_head_can_be_clarified_as_new_version(app, tmp_path):
    core, ctx = app
    first, _, _ = artifact(core, ctx, tmp_path, key="TEST-ref-a", label="TEST-v1")
    second, _, _ = artifact(core, ctx, tmp_path, version=1, key="TEST-ref-b", label="TEST-v2")
    display = DisplaySnapshot("unknown", (ArtifactVersion(first.ref, 1), ArtifactVersion(second.ref, 1)))
    source = capture(core, replace(ctx, display_snapshot=display), "TEST 那个颜色不行。", key="TEST-ref-mention", display_snapshot=display.to_payload(), artifact_refs=[first.ref, second.ref])
    proposal = dict(mention="那个颜色", candidate_refs=[f"{first.ref}@1", f"{second.ref}@1"], resolved_ref=None, resolution="ambiguous", evidence_refs=[ref(source)])
    original = apply(core, ctx, references=[proposal]).items[0]
    clarification = capture(core, replace(ctx, display_snapshot=display), f"刚才那个颜色说的是{second.ref}@1。", key="TEST-ref-mention", revision=2, display_snapshot=display.to_payload(), artifact_refs=[first.ref, second.ref])
    latest = apply(core, ctx, references=[dict(proposal, evidence_refs=[ref(clarification)]) ]).items[0]
    assert latest.ref == original.ref and latest.revision == 2
    assert core.reference(ctx, original.ref, 1).payload["resolution"] == "ambiguous"
    assert core.reference(ctx, original.ref).payload["resolution"] == "resolved"


def test_hypothesis_and_negative_reference_texts_stay_unresolved(app, tmp_path):
    core, ctx = app
    one, _, _ = artifact(core, ctx, tmp_path, key="TEST-entrance-a", label="入口海报")
    two, _, _ = artifact(core, ctx, tmp_path, version=2, key="TEST-entrance-a", label="入口海报")
    display = DisplaySnapshot("observed", (ArtifactVersion(one.ref, 1), ArtifactVersion(two.ref, 2)))
    candidates = [f"{one.ref}@1", f"{two.ref}@2"]
    hypothetical = capture(core, replace(ctx, display_snapshot=display), "如果刚才那个是入口海报 v1，就继续。", key="TEST-hypothesis", display_snapshot=display.to_payload(), artifact_refs=[one.ref])
    proposal = dict(mention="入口海报", candidate_refs=candidates, resolved_ref=None, resolution="ambiguous", evidence_refs=[ref(hypothetical)])
    result = apply(core, ctx, references=[proposal]).items[0]
    assert result.state == "ambiguous"
    negative = capture(core, replace(ctx, display_snapshot=display), "刚才那个不是入口海报 v1，是入口海报 v2。", key="TEST-negative", display_snapshot=display.to_payload(), artifact_refs=[one.ref])
    corrected = apply(core, ctx, references=[dict(proposal, evidence_refs=[ref(negative)])]).items[0]
    assert corrected.state == "resolved"


def test_omitted_same_label_version_prevents_candidate_uniqueness(app, tmp_path):
    core, ctx = app
    one, _, _ = artifact(core, ctx, tmp_path, key="TEST-same-label-a", label="入口海报")
    two, _, _ = artifact(core, ctx, tmp_path, key="TEST-same-label-b", label="入口海报")
    three, _, _ = artifact(core, ctx, tmp_path, key="TEST-same-label-c", label="入口海报")
    display = DisplaySnapshot("observed", (ArtifactVersion(one.ref, 1), ArtifactVersion(two.ref, 1), ArtifactVersion(three.ref, 1)))
    source = capture(core, replace(ctx, display_snapshot=display), "入口海报 v1。", key="TEST-omitted", display_snapshot=display.to_payload(), artifact_refs=[one.ref, two.ref, three.ref])
    result = apply(core, ctx, references=[dict(mention="入口海报", candidate_refs=[f"{one.ref}@1", f"{two.ref}@1"], resolved_ref=None, resolution="ambiguous", evidence_refs=[ref(source)])]).items[0]
    assert result.state == "ambiguous"
