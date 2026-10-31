"""Synthetic subject, occurrence and compatibility checks at publication boundaries."""

import json
from dataclasses import replace

import pytest
from scope_recall.contracts import TrustedSourcePrincipal
from scope_recall.core.claims import Qualification
from scope_recall.core.evidence_support import using_support
from scope_recall.core.semantic_proposals import prepare_support, support_messages

from tests.contract.test_claims import accept, app, capture, draft
from tests.contract.test_normalized_evidence import envelope


def _positioned(source, quote, *, at=None, **changes):
    proposal = draft(source, quote_override=quote, **changes)
    start = source.event["content"].index(quote) if at is None else at
    proposal["evidence_spans"][0].update(start=start, end=start + len(quote))
    return proposal


def _supported(proposal, source):
    def judge(*args, **kwargs):
        return json.dumps(dict(support="supported", action="new", target_ref=None, target_revision=None))

    return using_support(prepare_support(envelope(proposal), (source,), judge))


@pytest.mark.parametrize("kind", ["preference", "decision", "constraint", "fact"])
@pytest.mark.parametrize("subject", ["Alice", "principal:TEST-forged"])
def test_supported_normalization_cannot_assign_an_unproved_subject(app, kind, subject):
    core, ctx = app
    owner = replace(ctx, source_principal=TrustedSourcePrincipal("human", "verified", "principal:TEST-owner"))
    source = capture(core, owner, "I prefer blue.")
    proposal = _positioned(source, "I prefer blue.", kind=kind, subject=subject, value="blue")
    with _supported(proposal, source):
        saved = accept(core, owner, proposal).items[0]
    assert saved.state == "proposed"


@pytest.mark.parametrize(
    "parts",
    [
        ("```text\nI prefer blue.\n```\n", "I prefer blue.\n", "> I prefer blue.\n"),
        ("I prefer blue.\n", "> I prefer blue.\n", "```text\nI prefer blue.\n```\n"),
        ("> I prefer blue.\n", "```text\nI prefer blue.\n```\n", "I prefer blue.\n"),
    ],
)
@pytest.mark.parametrize("selected", ["owner", "code", "reported"])
def test_subject_binding_uses_the_cited_occurrence(app, parts, selected):
    core, ctx = app
    owner = replace(ctx, source_principal=TrustedSourcePrincipal("human", "verified", "principal:TEST-owner"))
    text = "".join(parts)
    part = next(
        p
        for p in parts
        if (
            p.startswith("```")
            if selected == "code"
            else p.startswith(">")
            if selected == "reported"
            else p.startswith("I")
        )
    )
    at = sum(len(p) for p in parts[: parts.index(part)]) + part.index("I prefer blue.")
    source = capture(core, owner, text)
    proposal = _positioned(source, "I prefer blue.", at=at, kind="preference", subject="user", value="blue")
    with _supported(proposal, source):
        saved = accept(core, owner, proposal).items[0]
    assert saved.state == ("active" if selected == "owner" else "proposed")


@pytest.mark.parametrize("positioned", [False, True])
def test_adjacent_subject_context_preserves_legacy_and_positioned_inputs(app, positioned):
    core, ctx = app
    source = capture(core, ctx, "TEST-project。配色蓝色。")
    proposal = draft(source, quote_override="配色蓝色。")
    if positioned:
        proposal["evidence_spans"][0].update(start=13, end=len(source.event["content"]))
    saved = accept(core, ctx, proposal).items[0]
    assert saved.state == "active"


def test_requalification_preserves_a_legacy_adjacent_subject_fact(app):
    from scope_recall.core.requalify import requalify_claims

    core, ctx = app
    source = capture(core, ctx, "TEST-project。配色蓝色。")
    proposal = draft(source, quote_override="配色蓝色。")
    with core.storage.write(ctx) as tx:
        saved = tx.claims.append(
            "TEST-scope",
            proposal,
            Qualification("active", "direct_report", "explicit_scoped_source"),
            recorded_at=core.clock.utc_now(),
        )
        report = requalify_claims(tx, now=core.clock.utc_now(), dry_run=False)
    assert not report.changed
    assert core.current_claim(ctx, saved.ref).state == "active"


def test_merge_request_masks_historical_principals_without_collapsing_speakers(app):
    core, ctx = app
    owner = replace(ctx, source_principal=TrustedSourcePrincipal("human", "verified", "principal:TEST-A"))
    source = capture(core, owner, "I prefer blue.")
    proposal = _positioned(source, "I prefer blue.", kind="preference", subject="user", value="blue")
    candidates = [
        dict(ref=f"clm:TEST-{name}", revision=1, payload=dict(proposal, subject=f"principal:TEST-{name}"))
        for name in ("A", "B", "C")
    ]
    messages = support_messages(proposal, (source,), candidates)
    assert "principal:" not in json.dumps(messages)
    subjects = [c["payload"]["subject"] for c in json.loads(messages[-1]["content"])["candidates"]]
    assert subjects[0] == "current_user"
    assert len(set(subjects)) == 3


@pytest.mark.parametrize("rejected", ["proposed", "expired", "future"])
def test_alias_lookup_filters_active_relations_before_the_limit(app, rejected):
    from scope_recall.core.semantic_proposals import merge_candidates

    from tests.contract.test_normalized_evidence import normalized

    core, ctx = app
    source = capture(core, ctx, "TEST-project database postgres.")
    target = accept(core, ctx, draft(source, predicate="database", value="postgres")).items[0]
    relation = draft(
        source,
        kind="alias",
        predicate="alias",
        value="Orchard",
        alias=dict(name="Orchard", target_ref=target.ref, scope_description="TEST-project"),
    )
    with core.storage.write(ctx) as tx:
        conn = tx._check(write=True)
        refs = []
        for index in range(17):
            saved = tx.claims.append(
                "TEST-scope",
                dict(relation, predicate=f"alias-{index}"),
                Qualification(
                    "proposed" if rejected == "proposed" else "active", "direct_report", "synthetic_relation"
                ),
                recorded_at=core.clock.utc_now(),
            )
            refs.append(saved.ref)
        conn.execute("UPDATE claim_versions SET state='active' WHERE claim_id=?", (max(refs),))
        if rejected != "proposed":
            field = "valid_to" if rejected == "expired" else "valid_from"
            instant = "2026-09-06T12:00:00Z" if rejected == "expired" else "2026-09-07T00:00:00Z"
            conn.execute(
                f"UPDATE claim_versions SET {field}=?,payload_json=json_set(payload_json,'$.{field}',?) "
                "WHERE claim_id<>? AND json_extract(payload_json,'$.kind')='alias'",
                (instant, instant, max(refs)),
            )
        choices = merge_candidates(
            tx, normalized(source, subject="orchard", value_text="postgres"), "TEST-scope", now=core.clock.utc_now()
        )
    assert [item["ref"] for item in choices] == [target.ref]
