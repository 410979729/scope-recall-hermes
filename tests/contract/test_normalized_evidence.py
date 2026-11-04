"""Normalized evidence contracts over synthetic sources and deterministic verdicts."""

import json
from copy import deepcopy

import pytest
from scope_recall.contracts import ContractError
from scope_recall.core.evidence_support import using_support
from scope_recall.core.semantic_proposals import prepare_support

from tests.contract.test_claims import accept, app, capture, draft


def normalized(source, **changes):
    proposal = draft(source, subject="TEST-project", predicate="database", value="PostgreSQL")
    proposal["evidence_spans"][0].update(start=0, end=len(source.event["content"]))
    proposal.update(changes)
    return proposal


def envelope(proposal):
    span = proposal["evidence_spans"][0]
    return dict(
        protocol_version="1.1",
        source_refs=[f"{span['source_ref']}@{span['source_revision']}"],
        claim_proposals=[proposal],
        resume_proposals=[],
        reference_proposals=[],
    )


def judge(messages, **kwargs):
    assert json.loads(messages[-1]["content"])["proposal"]["value_text"] == "PostgreSQL"
    return json.dumps(dict(support="supported", action="new", target_ref=None, target_revision=None))


def test_normalized_fields_need_a_source_bound_support_verdict(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-project 的数据库采用 postgres。")
    proposal = normalized(source)
    verdicts = prepare_support(envelope(proposal), (source,), judge)
    with using_support(verdicts):
        receipt = accept(core, ctx, proposal)
    assert receipt.items[0].state == "active"
    stored = core.current_claim(ctx, receipt.items[0].ref)
    assert stored.payload["value_text"] == "PostgreSQL"
    assert stored.payload["evidence_spans"][0]["start"] == 0


def test_unavailable_support_keeps_normalized_proposal_as_candidate(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-project 的数据库采用 postgres。")
    receipt = accept(core, ctx, normalized(source))
    assert receipt.items[0].state == "proposed"


def test_offsets_cannot_be_repaired_to_another_occurrence(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-project 的数据库采用 postgres。")
    proposal = normalized(source)
    proposal["evidence_spans"][0]["start"] = 1
    with pytest.raises(ContractError) as error:
        accept(core, ctx, proposal)
    assert error.value.field == "evidence_position"


@pytest.mark.parametrize(
    "text",
    [
        "TEST-project 的数据库不采用 postgres。",
        "如果迁移成功，TEST-project 的数据库采用 postgres。",
        "同事说 TEST-project 的数据库采用 postgres。",
    ],
)
def test_semantic_support_cannot_override_core_authority(app, text):
    core, ctx = app
    source = capture(core, ctx, text)
    proposal = normalized(source)
    with using_support(prepare_support(envelope(proposal), (source,), judge)):
        receipt = accept(core, ctx, proposal)
    assert receipt.items[0].state == "proposed"


def test_changed_proposal_cannot_reuse_a_support_receipt(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-project 的数据库采用 postgres。")
    proposal = normalized(source)
    verdicts = prepare_support(envelope(proposal), (source,), judge)
    changed = deepcopy(proposal)
    changed["value_text"] = "SQLite"
    with using_support(verdicts):
        receipt = accept(core, ctx, changed)
    assert receipt.items[0].state == "proposed"


def test_one_verified_owner_statement_promotes_without_a_second_witness(app):
    from dataclasses import replace

    from scope_recall.contracts import TrustedSourcePrincipal

    core, ctx = app
    owner = replace(ctx, source_principal=TrustedSourcePrincipal("human", "verified", "principal:TEST-owner"))
    source = capture(core, owner, "我决定采用 postgres。")
    proposal = normalized(source, subject="user")
    with using_support(prepare_support(envelope(proposal), (source,), judge)):
        receipt = accept(core, owner, proposal)
    assert receipt.items[0].state == "active"
    assert core.current_claim(owner, receipt.items[0].ref).payload["subject"] == "principal:TEST-owner"


def test_support_failure_preserves_candidates_and_stops_later_requests(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-project 的数据库采用 postgres。")
    proposal = normalized(source)
    value = envelope(proposal)
    value["claim_proposals"].append(dict(proposal, predicate="storage"))
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(True)
        raise RuntimeError("synthetic unavailable route")

    verdicts = prepare_support(value, (source,), unavailable)
    assert len(calls) == 1
    assert all(v.state.startswith("unavailable") for v in verdicts)
    with using_support(verdicts):
        receipt = accept(core, ctx, *value["claim_proposals"])
    assert all(item.state == "proposed" for item in receipt.items)


def test_normalized_slot_lookup_and_model_update_preserve_history(app):
    from scope_recall.core.semantic_proposals import prepare_publication

    core, ctx = app
    first = capture(core, ctx, "TEST-project database PostgreSQL。", when="2026-09-05T12:00:00Z")
    old = accept(core, ctx, draft(first, subject="TEST-project", predicate="database", value="PostgreSQL"))
    source = capture(core, ctx, "TEST-project 的数据库采用 sqlite。", when="2026-09-06T12:00:00Z")
    proposal = normalized(source, subject="test-project", value_text="SQLite")

    def update(messages, **kwargs):
        candidates = json.loads(messages[-1]["content"])["candidates"]
        target = next(c for c in candidates if c["ref"] == old.items[0].ref)
        return json.dumps(
            dict(support="supported", action="update", target_ref=target["ref"], target_revision=target["revision"])
        )

    verdicts = prepare_publication(core.storage, ctx, envelope(proposal), (source,), update)
    with using_support(verdicts):
        receipt = accept(core, ctx, proposal)
    assert receipt.items[0].ref == old.items[0].ref
    assert receipt.items[0].revision == 2
    assert core.current_claim(ctx, old.items[0].ref).payload["value_text"] == "SQLite"
    assert core.claim_history(ctx, old.items[0].ref)[0].payload["value_text"] == "PostgreSQL"


def test_normalized_event_time_is_canonical_utc_and_keeps_semantic_evidence(app):
    from scope_recall.core.worker_consolidation import decode_consolidation_result

    core, ctx = app
    source = capture(core, ctx, "TEST-project chose postgres at 2026-09-05 23:00 in New York.")
    proposal = normalized(source, valid_from="2026-09-05T23:00:00-04:00", occurred_at="2026-09-05T23:00:00-04:00")
    value = decode_consolidation_result(json.dumps(envelope(proposal)), (source,))
    proposal = value["claim_proposals"][0]
    with using_support(prepare_support(value, (source,), judge)):
        receipt = accept(core, ctx, proposal)
    saved = core.current_claim(ctx, receipt.items[0].ref)
    assert saved.payload["occurred_at"] == "2026-09-06T03:00:00Z"
    assert saved.valid_from.startswith("2026-09-06T03:00:00")


def test_each_support_request_rechecks_the_outbound_guard(app):
    from scope_recall.core import private

    core, ctx = app
    source = capture(core, ctx, "TEST-project 的数据库采用 postgres。")
    proposal = normalized(source)
    checks, calls = [], []

    def blocked():
        checks.append(True)
        return len(checks) > 1

    def never_send(*args, **kwargs):
        calls.append(True)
        return judge(*args, **kwargs)

    with private.using_outbound_check(blocked), pytest.raises(ContractError) as error:
        prepare_support(envelope(proposal), (source,), never_send)
    assert error.value.field == "private_mode"
    assert not calls


@pytest.mark.parametrize("error_type,detail", [("http_status", "401"), ("model_unavailable", None)])
def test_support_account_refusal_stops_the_worker_batch(app, error_type, detail):
    from dataclasses import replace

    from scope_recall.core.worker_consolidation import build_consolidation_model

    core, ctx = app
    capture(core, ctx, "TEST-project selected postgres.", key="TEST-support/first")
    capture(
        core,
        replace(ctx, session_id="TEST-other-session"),
        "TEST-project selected postgres again.",
        key="TEST-support/second",
    )

    class Refusal(RuntimeError):
        pass

    Refusal.error_type = error_type
    Refusal.detail = detail

    class Port:
        def __init__(self):
            self.extractions, self.judgements = 0, 0

        def propose(self, messages, **kwargs):
            body = json.loads(messages[-1]["content"])
            if "proposal" in body:
                self.judgements += 1
                raise Refusal()
            self.extractions += 1
            source = body["sources"][0]
            claim = dict(
                kind="decision",
                subject="TEST-project",
                predicate="database",
                value_text="PostgreSQL",
                conditions=[],
                statement_kind="assertion",
                valid_from=None,
                valid_to=None,
                evidence_spans=[
                    dict(
                        source_ref=source["source_ref"],
                        source_revision=source["source_revision"],
                        quote=source["content"],
                        start=0,
                        end=len(source["content"]),
                    )
                ],
            )
            return json.dumps(
                dict(
                    protocol_version="1.1",
                    source_refs=body["source_refs"],
                    claim_proposals=[claim],
                    resume_proposals=[],
                    reference_proposals=[],
                )
            )

    port = Port()
    receipt = core.drain_worker(ctx, max_items=8, remaining_seconds=30, consolidation=build_consolidation_model(port))
    assert port.extractions == port.judgements == 1
    assert "consolidate" in receipt.unavailable_work_types
    with core.storage.read(ctx) as tx:
        conn = tx._check()
        assert conn.execute("SELECT count(*) FROM claims").fetchone()[0] == 1
        pending = conn.execute(
            "SELECT state,attempt FROM work_items WHERE work_type='consolidate' AND state='pending'"
        ).fetchall()
        assert len(pending) == 1
        assert pending[0][1] == 0


def test_merge_lookup_filters_before_limiting_unrelated_candidates(app):
    from scope_recall.core.semantic_proposals import merge_candidates

    core, ctx = app
    source = capture(core, ctx, "TEST-project 的数据库采用 postgres。")
    target = accept(core, ctx, draft(source, predicate="database", value="postgres"))
    with core.storage.write(ctx, remaining_seconds=30) as tx:
        conn = tx._check(write=True)
        original = {
            table: dict(conn.execute(f"SELECT * FROM {table} WHERE claim_id=?", (target.items[0].ref,)).fetchone())
            for table in ("claims", "claim_versions")
        }
        for index in range(205):
            ref = target.items[0].ref[:-1] + "!" + str(index)
            for table, template in original.items():
                row = dict(template, claim_id=ref)
                if table == "claims":
                    row.update(subject=f"OTHER-{index}", slot_key=f"TEST-unrelated-{index}")
                else:
                    payload = json.loads(row["payload_json"])
                    payload["subject"] = f"OTHER-{index}"
                    row.update(payload_json=json.dumps(payload), state="proposed")
                conn.execute(
                    f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", tuple(row.values())
                )
        assert target.items[0].ref not in tx.claims.list_refs(kind="decision", limit=200)
    with core.storage.read(ctx) as tx:
        choices = merge_candidates(
            tx, normalized(source, subject="test-project"), "TEST-scope", now=core.clock.utc_now()
        )
    assert [item["ref"] for item in choices] == [target.items[0].ref]


def test_semantic_duplicate_keeps_the_stored_wording_and_revision(app):
    from dataclasses import replace

    from scope_recall.contracts import TrustedSourcePrincipal
    from scope_recall.core.semantic_proposals import prepare_publication

    core, ctx = app
    ctx = replace(ctx, source_principal=TrustedSourcePrincipal("human", "verified", "principal:TEST-owner"))
    first = capture(core, ctx, "TEST-project database postgres.", when="2026-09-05T12:00:00Z")
    old = accept(core, ctx, draft(first, predicate="database", value="postgres"))
    source = capture(core, ctx, "TEST-project selected PostgreSQL.", when="2026-09-06T12:00:00Z")
    proposal = normalized(source)

    def duplicate(messages, **kwargs):
        target = json.loads(messages[-1]["content"])["candidates"][0]
        return json.dumps(
            dict(support="supported", action="duplicate", target_ref=target["ref"], target_revision=target["revision"])
        )

    verdicts = prepare_publication(core.storage, ctx, envelope(proposal), (source,), duplicate)
    with using_support(verdicts):
        receipt = accept(core, ctx, proposal)
    assert receipt.items[0].disposition == "duplicate"
    assert receipt.items[0].revision == 1
    assert core.current_claim(ctx, old.items[0].ref).payload["value_text"] == "postgres"
    assert len(core.claim_history(ctx, old.items[0].ref)) == 1


def test_merge_lookup_resolves_a_normalized_verified_alias(app):
    from scope_recall.core.semantic_proposals import merge_candidates

    core, ctx = app
    first = capture(core, ctx, "TEST-project database postgres.")
    old = accept(core, ctx, draft(first, predicate="database", value="postgres"))
    identity_source = capture(core, ctx, "TEST-project project name is TEST-oldname.")
    identity = accept(
        core,
        ctx,
        draft(identity_source, kind="fact", statement_kind="assertion", predicate="project name", value="TEST-oldname"),
    )
    rename = capture(core, ctx, "TEST-project is now called Orchard.")
    alias = draft(
        rename,
        kind="alias",
        subject="TEST-project",
        predicate="alias",
        value="Orchard",
        alias=dict(name="Orchard", target_ref=identity.items[0].ref, scope_description="TEST-project"),
    )
    assert accept(core, ctx, alias).items[0].state == "active"
    with core.storage.read(ctx) as tx:
        choices = merge_candidates(tx, normalized(first, subject="orchard"), "TEST-scope", now=core.clock.utc_now())
    assert [item["ref"] for item in choices] == [old.items[0].ref]


def test_support_and_merge_inputs_keep_principal_authority_keys_local(app):
    from dataclasses import replace

    from scope_recall.contracts import TrustedSourcePrincipal
    from scope_recall.core.semantic_proposals import prepare_publication

    core, ctx = app
    principal = "principal:TEST-owner"
    owner = replace(ctx, source_principal=TrustedSourcePrincipal("human", "verified", principal))
    first = capture(core, owner, "我决定采用 postgres。")
    original = normalized(first, subject="user")
    with using_support(prepare_support(envelope(original), (first,), judge)):
        saved = accept(core, owner, original)
    second = capture(core, owner, "我决定继续采用 postgres。")
    proposal = normalized(second, subject="user")

    def safe_judge(messages, **kwargs):
        assert principal not in json.dumps(messages)
        body = json.loads(messages[-1]["content"])
        assert body["candidates"][0]["payload"]["subject"] == "current_user"
        return json.dumps(
            dict(
                support="supported",
                action="duplicate",
                target_ref=saved.items[0].ref,
                target_revision=saved.items[0].revision,
            )
        )

    verdicts = prepare_publication(core.storage, owner, envelope(proposal), (second,), safe_judge)
    with using_support(verdicts):
        receipt = accept(core, owner, proposal)
    assert receipt.items[0].disposition == "duplicate"


@pytest.mark.parametrize("period", ["expired", "future", "microsecond"])
def test_merge_alias_must_apply_at_publication_time(app, period):
    from scope_recall.core.claims import Qualification
    from scope_recall.core.semantic_proposals import merge_candidates

    core, ctx = app
    source = capture(core, ctx, "TEST-project database postgres.")
    target = accept(core, ctx, draft(source, predicate="database", value="postgres")).items[0]
    relation = draft(
        source,
        kind="alias",
        predicate="alias",
        value="Orchard",
        alias=dict(name="Orchard", target_ref=target.ref, scope_description="TEST-project"),
        valid_from="2026-09-07T00:00:00Z" if period == "future" else "2026-09-01T00:00:00Z",
        valid_to={"expired": "2026-09-06T12:00:00Z", "microsecond": "2026-09-06T12:00:00.000001Z"}.get(period),
    )
    with core.storage.write(ctx) as tx:
        tx.claims.append(
            "TEST-scope",
            relation,
            Qualification("active", "direct_report", "synthetic_relation"),
            recorded_at=core.clock.utc_now(),
        )
        choices = merge_candidates(tx, normalized(source, subject="orchard"), "TEST-scope", now=core.clock.utc_now())
    assert [c["ref"] for c in choices] == ([target.ref] if period == "microsecond" else [])


def test_merge_alias_is_rechecked_if_it_expires_during_judgement(app):
    from scope_recall.core.claims import Qualification
    from scope_recall.core.semantic_proposals import prepare_publication

    core, ctx = app
    original = capture(core, ctx, "TEST-project database postgres.")
    target = accept(core, ctx, draft(original, predicate="database", value="postgres")).items[0]
    relation = draft(
        original,
        kind="alias",
        predicate="alias",
        value="Orchard",
        alias=dict(name="Orchard", target_ref=target.ref, scope_description="TEST-project"),
        valid_to="2026-09-07T00:00:00Z",
    )
    with core.storage.write(ctx) as tx:
        tx.claims.append(
            "TEST-scope",
            relation,
            Qualification("active", "direct_report", "synthetic_relation"),
            recorded_at=core.clock.utc_now(),
        )
    source = capture(core, ctx, "Orchard database postgres.")
    proposal = normalized(source, subject="Orchard", value_text="postgres")

    def expires(messages, **kwargs):
        assert json.loads(messages[-1]["content"])["candidates"][0]["ref"] == target.ref
        core.clock.now = "2026-09-07T00:00:00Z"
        return json.dumps(dict(support="supported", action="update", target_ref=target.ref, target_revision=1))

    verdicts = prepare_publication(core.storage, ctx, envelope(proposal), (source,), expires, clock=core.clock)
    with using_support(verdicts), pytest.raises(ContractError) as error:
        accept(core, ctx, proposal)
    assert (error.value.code, error.value.field) == ("VERSION_CONFLICT", "merge_target")
    assert len(core.claim_history(ctx, target.ref)) == 1
