"""Natural revisions preserve evidence and never require candidate review."""
from dataclasses import replace
import itertools

import pytest

from scope_recall.core import CoreConfig, MemoryCore
from tests.contract.test_v11_claims import Clock, accept, capture, draft, initial
from tests.v11_support import context


@pytest.fixture
def app(tmp_path):
    ctx = replace(context(tmp_path / 'TEST-natural-correction'), project_id='TEST-project', branch_id='TEST-main')
    core = MemoryCore(CoreConfig(ctx.binding), clock=Clock())
    core.initialize()
    core.test_sequence = itertools.count(1)
    return core, ctx


@pytest.mark.parametrize('verb', ['换成', '调整为', 'switch to'])
def test_explicit_natural_update_without_model(app, verb):
    core, ctx = app
    item, _ = initial(core, ctx, value='H100', kind='fact')
    capture(core, ctx, f'TEST-project 配色 {verb} H200。', when='2026-09-06T12:00:00Z')
    current = core.current_claim(ctx, item.ref)
    assert current.payload['value_text'] == 'H200'
    assert current.revision == 2


@pytest.mark.parametrize('text', [
    'TEST-project 配色停止使用 H100。', 'TEST-project 配色不再采用 H100。',
    'TEST-project 配色 discontinue H100。',
])
def test_explicit_cessation_retracts_current_but_preserves_history(app, text):
    core, ctx = app
    item, _ = initial(core, ctx, value='H100', kind='fact')
    capture(core, ctx, text, when='2026-09-06T12:00:00Z')
    assert core.current_claim(ctx, item.ref) is None
    assert core.claim_history(ctx, item.ref)[-1].state == 'retracted'
    assert core.claim_history(ctx, item.ref)[0].payload['value_text'] == 'H100'


def test_cessation_with_explicit_replacement_keeps_new_current(app):
    core, ctx = app
    item, _ = initial(core, ctx, value='H100', kind='fact')
    capture(core, ctx, 'TEST-project 配色停止使用 H100，换成 H200。', when='2026-09-06T12:00:00Z')
    assert core.current_claim(ctx, item.ref).payload['value_text'] == 'H200'
    assert len(core.claim_history(ctx, item.ref)) == 2


@pytest.mark.parametrize('text', [
    '不要把 TEST-project 配色换成 H200。',
    'TEST-project 配色换成 H200 吗？',
    '如果 TEST-project 配色调整为 H200。',
    '明天 TEST-project 配色停止使用 H100。',
    "do not stop using TEST-project 配色 H100.",
    '举例：TEST-project 配色停止使用 H100。',
    '客户原文：TEST-project 配色撤回 H100。',
    '不要不再使用 TEST-project 配色 H100。',
    'TEST-project 配色将停止使用 H100。',
])
def test_conditional_question_and_negation_do_not_mutate(app, text):
    core, ctx = app
    item, _ = initial(core, ctx, value='H100', kind='fact')
    capture(core, ctx, text, when='2026-09-06T12:00:00Z')
    assert core.current_claim(ctx, item.ref).payload['value_text'] == 'H100'
    assert len(core.claim_history(ctx, item.ref)) == 1


def test_target_lookup_is_not_limited_to_first_two_hundred_claims(app):
    core, ctx = app
    items = []
    for i in range(205):
        subject = f'TEST-device-{i:03}'
        predicate = f'色系{i:03}'
        source = capture(core, ctx, f'{subject} {predicate} H100。')
        item = accept(core, ctx, draft(source, 'H100', kind='fact', subject=subject, predicate=predicate)).items[0]
        items.append((item, subject, predicate))
    ordered = sorted(items, key=lambda pair: pair[0].ref)
    target, subject, predicate = ordered[-1]
    capture(core, ctx, f'{subject} {predicate}换成 H200。', when='2026-09-06T12:00:00Z')
    assert core.current_claim(ctx, target.ref).payload['value_text'] == 'H200'
    first, _, first_predicate = ordered[0]
    # More than 200 matches; naming two predicates cannot become a unique
    # action just because one candidate falls outside the fallback window.
    capture(core, ctx, f'H100 改一下：{first_predicate} 和 {predicate} 换成 H200。', when='2026-09-06T12:00:00Z')
    assert core.current_claim(ctx, first.ref).payload['value_text'] == 'H100'
    assert core.current_claim(ctx, target.ref).revision == 2
