"""Bounded source-grounded claim rules; synthetic text and no model calls."""
from dataclasses import replace
import itertools

import pytest

from scope_recall.contracts import ContractError
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.claims import RootEvidence, qualify
from scope_recall.core.source_qualification import conditions_match
from tests.contract.test_v11_claims import Clock, capture, initial, revise_request
from tests.v11_support import context


def qualification(text, *, subject='user', value='蓝色', conditions=(), principal=None):
    if principal is None:
        principal = {
            'kind': 'human',
            'resolution': 'verified',
            'principal_ref': 'principal:TEST-owner',
        }
    root = RootEvidence('TEST-root', 1, 'human_direct', None, text,
                        '2026-09-06T12:00:00Z', 'complete', 'TEST-session',
                        source_principal=principal)
    proposal = dict(kind='preference', subject=subject, predicate='颜色偏好',
                    value_text=value, conditions=list(conditions),
                    statement_kind='assertion', valid_from=root.occurred_at, valid_to=None,
                    evidence_spans=[dict(source_ref=root.ref, source_revision=1, quote=text)])
    return qualify(proposal, (root,))


def test_condition_wrappers_preserve_assertion_and_identity():
    for condition, query in (
        ('写小说时', '现在写小说'), ('当写小说时', '现在写小说'),
        ('when writing fiction', 'I am writing fiction'),
        ('when writing  fiction', 'I am writing fiction'),
        ('café', 'cafe\u0301'),
        ('TEST-sandbox', 'TEST-sandbox.'),
        ('写小说时', '现在写小说，能帮我润色吗？'),
    ):
        assert conditions_match([condition], query), (condition, query)
    for condition, query in (
        ('写小说时', '现在不写小说'), ('TEST-sandbox', 'TEST-sandbox-other'),
        ('blue', 'blue.txt'),
        ('TEST-sandbox', 'test-sandbox'), ('写小说', '如果写小说'),
        ('写小说', '客户说写小说'), ('写小说', '写小说吗？'),
        ('写小说时', '是不是写小说'),
        ('今天', '今天'), ('本次', '本次'), ('暂时', '暂时'),
    ):
        assert not conditions_match([condition], query), (condition, query)


def test_self_report_does_not_promote_third_party_or_team_to_user():
    for text, value in (('我喜欢蓝色。', '蓝色'), ('我的偏好是蓝色。', '蓝色'),
                        ('I prefer blue.', 'blue'), ('My preference is blue.', 'blue'),
                        ('我不喜欢蓝色。', '不喜欢蓝色'), ('I do not like blue.', 'do not like blue')):
        assert qualification(text, value=value).state == 'active', text
    for text, value in (('我的同事喜欢蓝色。', '蓝色'), ('我们喜欢蓝色。', '蓝色'),
                        ('我朋友喜欢蓝色。', '蓝色'), ('我同事喜欢蓝色。', '蓝色'),
                        ('我有个同事喜欢蓝色。', '蓝色'), ('I have a colleague who likes blue.', 'blue'),
                        ('My colleague likes blue.', 'blue'), ('We prefer blue.', 'blue'),
                        ('她说：“我喜欢蓝色。”', '蓝色'), ('He said "I prefer blue."', 'blue')):
        assert qualification(text, value=value).state == 'proposed', text
    assert qualification('我的同事喜欢蓝色。', subject='我').state == 'proposed'


def test_self_report_requires_a_verified_c1_principal():
    missing = qualification('我喜欢蓝色。', principal={})
    assert missing.state == 'proposed'
    assert missing.reason == 'source_identity_unresolved'
    unresolved = qualification(
        '我喜欢蓝色。',
        principal={'kind': 'human', 'resolution': 'unresolved'},
    )
    assert unresolved.state == 'proposed'
    assert unresolved.reason == 'source_identity_unresolved'


def test_unrelated_condition_cannot_erase_relative_scope():
    text = '我本次在TEST沙箱喜欢蓝色。'
    assert qualification(text, conditions=['TEST沙箱']).reason == 'relative_scope_not_preserved'
    assert qualification(text, conditions=['本次', 'TEST沙箱']).state == 'active'


@pytest.fixture
def app(tmp_path):
    ctx = replace(context(tmp_path / 'TEST-sprint-claims'), project_id='TEST-project', branch_id='TEST-main')
    core = MemoryCore(CoreConfig(ctx.binding), clock=Clock())
    core.initialize()
    core.test_sequence = itertools.count(1)
    return core, ctx


def test_attribute_correction_cannot_skip_negative_value_qualification(app):
    core, ctx = app
    item, _ = initial(core, ctx, value='H100', kind='fact')
    source = capture(core, ctx, '更正TEST-project，配色不是H200。', when='2026-09-06T12:00:00Z')
    with pytest.raises(ContractError, match='value_polarity_not_preserved'):
        core.revise(ctx, revise_request(item, source, 'H200'))
    assert core.current_claim(ctx, item.ref).payload['value_text'] == 'H100'
    assert len(core.claim_history(ctx, item.ref)) == 1


def test_explicit_replacement_forms_keep_exact_old_value(app):
    core, ctx = app
    item, _ = initial(core, ctx, value='H100', kind='fact')
    capture(core, ctx, 'TEST-project 配色替换为H200。', when='2026-09-06T12:00:00Z')
    assert core.current_claim(ctx, item.ref).payload['value_text'] == 'H200'
    capture(core, ctx, 'TEST-project 配色 replace h200 with H300.', when='2026-09-06T12:00:00Z')
    assert core.current_claim(ctx, item.ref).payload['value_text'] == 'H200'
    capture(core, ctx, 'TEST-project 配色 replace H200 with H300.', when='2026-09-06T12:00:00Z')
    assert core.current_claim(ctx, item.ref).payload['value_text'] == 'H300'
    assert len(core.claim_history(ctx, item.ref)) == 3


def test_temporary_retraction_does_not_erase_regular_rule(app):
    core, ctx = app
    item, _ = initial(core, ctx, value='H100', kind='fact')
    source = capture(core, ctx, '本次撤回TEST-project 配色H100。', when='2026-09-06T12:00:00Z')
    with pytest.raises(ContractError, match='conditional_retraction_not_authorized'):
        core.revise(ctx, revise_request(item, source, None))
    assert core.current_claim(ctx, item.ref).payload['value_text'] == 'H100'
    assert core.source(ctx, source.ref, 1).event['content'] == source.event['content']
