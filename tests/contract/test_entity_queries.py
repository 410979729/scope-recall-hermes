"""Name mapping preserves longer known names and trusted principal identities."""

import json
from dataclasses import replace

import pytest
from scope_recall.contracts import TrustedSourcePrincipal
from scope_recall.core.entity_queries import resolved_query
from scope_recall.core.entity_resolution import publish_entity_merge
from scope_recall.core.evidence_support import SupportVerdict, using_support
from scope_recall.core.semantic_proposals import prepare_support

from tests.contract.test_aliases import app
from tests.contract.test_claims import accept, capture, draft
from tests.contract.test_entity_resolution import fact
from tests.contract.test_normalized_evidence import envelope


def normalized(core, ctx, text, **fields):
    """Admit a recorded supported assertion through the normalized evidence boundary."""
    source = capture(core, ctx, text)
    proposal = draft(source, **fields)
    proposal["evidence_spans"][0].update(start=0, end=len(text))

    def recording(messages, **kwargs):
        return json.dumps(dict(support="supported", action="new", target_ref=None, target_revision=None))

    with using_support(prepare_support(envelope(proposal), (source,), recording)):
        result = accept(core, ctx, proposal)
    assert result.items[0].state == "active"
    return core.current_claim(ctx, result.items[0].ref)


@pytest.mark.parametrize(
    "query,expected",
    [
        ("TEST-北京大学在哪里", "TEST-北京大学在哪里"),
        ("TEST-北京在哪里", "TEST-北京市在哪里"),
        ("TEST-北京在哪里，TEST-北京大学在哪里", "TEST-北京市在哪里，TEST-北京大学在哪里"),
        ("TEST-北京市在哪里", "TEST-北京市在哪里"),
    ],
)
def test_query_uses_the_longest_known_entity_name(app, query, expected):
    core, ctx = app
    city = normalized(
        core,
        ctx,
        "TEST-北京市 category city. TEST-北京市 is also called TEST-北京.",
        value="city",
        kind="fact",
        subject="TEST-北京市",
        predicate="category",
        statement_kind="assertion",
        entity_type="place",
        entity_names=["TEST-北京"],
    )
    fact(core, ctx, "TEST-北京大学", "category", "organization")
    with core.storage.write(ctx) as tx:
        publish_entity_merge(
            tx,
            city,
            city,
            SupportVerdict("", "supported", city.ref, city.revision, "duplicate"),
            now=core.clock.utc_now(),
            name="TEST-北京",
        )
    with core.storage.read(ctx) as tx:
        assert resolved_query(tx, query, core.clock.utc_now()) == expected


def test_verified_principal_profile_preserves_project_evidence_boundaries(app):
    core, ctx = app
    principal = TrustedSourcePrincipal("human", "verified", "principal:TEST-profile-owner")
    global_ctx = replace(ctx, project_id=None, branch_id=None, source_principal=principal)
    local_ctx = replace(ctx, source_principal=principal)
    for owner, text, value, predicate in (
        (global_ctx, "我喜欢 TEST-blue。", "TEST-blue", "喜欢"),
        (local_ctx, "我使用 TEST-vim。", "TEST-vim", "使用"),
    ):
        source = capture(core, owner, text)
        result = accept(core, owner, draft(source, value, kind="preference", subject="user", predicate=predicate))
        assert result.items[0].state == "active", result
    for owner, expected in ((global_ctx, {"TEST-blue"}), (local_ctx, {"TEST-vim"})):
        view = core.profile(owner, dict(protocol_version="1.1", request_id="TEST-owner", subject="user"))
        assert {item["value_text"] for item in view["sections"]["preferences"]} == expected


def test_literal_decision_prevents_an_unrelated_legacy_alias(app):
    from scope_recall.core.read_views import resolve_subject

    from tests.contract.test_aliases import _alias, _identity

    core, ctx = app
    target = _identity(core, ctx)
    source = capture(core, ctx, "TEST-mist 项目改名为 TEST暮光，保留旧称。")
    assert accept(core, ctx, _alias(source, target, "TEST暮光")).items[0].state == "active"
    normalized(
        core,
        ctx,
        "TEST暮光 editor TEST-vim.",
        value="TEST-vim",
        kind="decision",
        subject="TEST暮光",
        predicate="editor",
        statement_kind="assertion",
    )
    with core.storage.read(ctx) as tx:
        assert resolve_subject(tx, "TEST暮光", core.clock.utc_now()).status == "ambiguous"
