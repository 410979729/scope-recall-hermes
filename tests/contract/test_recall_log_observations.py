"""Observation history retains inductive identity and full-manifest rejection causes."""

import json

import pytest
from scope_recall.core.retrieval import CandidateRef, RetrievalResult, SearchContext

from tests.contract.test_importance_summaries import app
from tests.contract.test_reflection import ReflectionModel, observation, owner_api, populate, run
from tests.v11_support import recall_request


def latest(core, request_id):
    """Read only the synthetic diagnostic payload after its asynchronous writer finishes."""
    assert core.recall_diagnostics.flush()
    return core.recall_diagnostics.store.list(request_id=request_id, limit=1)[0]


def published(app, reviewed=False):
    """Publish through the real worker's reflection and independent support stages."""
    core, ctx = app
    roots = populate(core, ctx)
    model = ReflectionModel()
    run(core, ctx, model)
    item = observation(core, ctx)
    assert item is not None and len(model.calls) == 2
    if reviewed:
        owner_api(core, ctx).revise(item.ref, expected_revision=item.revision, value=item.payload["value_text"])
        item = core.current_claim(ctx, item.ref)
    return core, ctx, roots, item


@pytest.mark.parametrize("entry", ["recall", "recall_packet"])
@pytest.mark.parametrize("reviewed", [False, True])
def test_observation_is_labelled_in_both_recall_logs_without_content(app, entry, reviewed):
    from scope_recall.core.recall_overflow import _qualified, _representatives

    core, ctx, roots, item = published(app, reviewed)
    request = recall_request(
        query="TEST-project recurring connection timeouts",
        mode="current",
        request_id=f"TEST-{entry}-{reviewed}",
        focus_refs=[f"{item.ref}@{item.revision}"],
    )
    result = getattr(core, entry)(ctx, request, deadline_seconds=10)
    items = result["items"] if isinstance(result, dict) else result.items
    shown = next(i for i in items if (i["ref"] if isinstance(i, dict) else i.ref) == item.ref)
    fields = shown if isinstance(shown, dict) else vars(shown)
    assert fields["origin"] == "inductive_observation"
    assert fields["content"].startswith("Inductive observation (not the owner's words): ")
    record = latest(core, request["request_id"])
    logged = next(i for i in record["items"] if i["ref"] == item.ref and i["revision"] == item.revision)
    assert logged["source_type"] == "observation" and logged["reason"] == "delivered"
    assert "exact_ref" in logged["channels"]
    encoded = json.dumps(record, ensure_ascii=False)
    assert item.payload["value_text"] not in encoded
    assert all(s.event["content"] not in encoded for s in roots)
    reader = core.recall_pipeline.storage_reader
    search = SearchContext.from_request(request, ctx, now=core.clock.utc_now(), deadline=core.clock.monotonic() + 10)
    candidate = CandidateRef("claim", item.ref, item.revision, "exact_ref")
    with core.storage.read(ctx) as tx:
        hydrated = reader.hydrate(tx, candidate, search)
        assert hydrated is not None and not _qualified(hydrated)
        result = RetrievalResult((), (), None, overflow_candidates=(candidate,))
        assert _representatives(tx, reader, result, search, {"items": []}, core.clock) == []


@pytest.mark.parametrize("entry", ["recall", "recall_packet"])
@pytest.mark.parametrize("cited", [False, True])
def test_private_observation_dependency_has_private_log_reason(app, entry, cited):
    core, ctx, roots, item = published(app)
    root = roots[0 if cited else 2]
    owner_api(core, ctx).private_on(target="session", session=root.session_id)
    request = recall_request(
        query="TEST-project recurring connection timeouts",
        mode="current",
        request_id="TEST-private-observation",
        focus_refs=[f"{item.ref}@{item.revision}"],
    )
    result = getattr(core, entry)(ctx, request, deadline_seconds=10)
    items = result["items"] if isinstance(result, dict) else result.items
    assert all((i["ref"] if isinstance(i, dict) else i.ref) != item.ref for i in items)
    logged = next(i for i in latest(core, request["request_id"])["items"] if i["ref"] == item.ref)
    assert logged["reason"] == "private"
    assert logged["source_type"] == "observation"


@pytest.mark.parametrize(
    "action,reason",
    [
        ("delete", "deleted"),
        ("suppress", "muted"),
        ("quarantined", "quarantine"),
        ("pending_review", "pending_review"),
        ("stale", "stale"),
    ],
)
def test_uncited_observation_dependency_uses_existing_rejection_codes(app, action, reason):
    core, ctx, roots, item = published(app)
    root = roots[2]
    if action in {"delete", "suppress"}:
        api = owner_api(core, ctx)
        api.apply(api.preview([root.ref], mode=action))
    else:
        with core.storage.write(ctx) as tx:
            if action == "stale":
                tx._check().execute("UPDATE observations SET signature='TEST-obsolete' WHERE claim_ref=?", (item.ref,))
            else:
                tx._check().execute(
                    "INSERT INTO source_safety VALUES(?,?,?,'[]',?,NULL)",
                    (root.ref, root.revision, action, core.clock.utc_now()),
                )
    request = recall_request(
        query="TEST recurring timeouts",
        mode="current",
        request_id="TEST-held-observation",
        focus_refs=[f"{item.ref}@{item.revision}"],
    )
    packet = core.recall_packet(ctx, request, deadline_seconds=10)
    assert all(i["ref"] != item.ref for i in packet["items"])
    logged = next(i for i in latest(core, request["request_id"])["items"] if i["ref"] == item.ref)
    assert logged["reason"] == reason
    assert logged["source_type"] == "observation"


def test_observation_covered_by_its_original_logs_restatement(app):
    core, ctx, roots, item = published(app)
    owner_api(core, ctx).revise(item.ref, expected_revision=item.revision, value=roots[0].event["content"])
    item = core.current_claim(ctx, item.ref)
    request = recall_request(
        query="TEST-project connection timeouts",
        mode="current",
        request_id="TEST-covered-observation",
        focus_refs=[f"{item.ref}@{item.revision}", f"{roots[0].ref}@1"],
    )
    packet = core.recall_packet(ctx, request, deadline_seconds=10)
    assert all(i["ref"] != item.ref for i in packet["items"])
    assert any(i["ref"] == roots[0].ref for i in packet["items"])
    logged = next(i for i in latest(core, request["request_id"])["items"] if i["ref"] == item.ref)
    assert logged["source_type"] == "observation" and logged["reason"] == "restatement"


def test_doctor_keeps_observation_history_beside_daily_reflection_usage(app):
    from datetime import UTC, datetime

    from scope_recall.core.reflection_policy import reserve_call
    from scope_recall.maintenance.doctor_recall import check_recall_log
    from scope_recall.maintenance.doctor_report import DoctorReport
    from scope_recall.maintenance.doctor_store import _derived_progress

    core, ctx, _roots, item = published(app)
    core.clock.now = datetime.now(UTC).isoformat()
    request = recall_request(
        query="TEST recurring timeouts",
        mode="current",
        request_id="TEST-observation-doctor",
        focus_refs=[f"{item.ref}@{item.revision}"],
    )
    core.recall_packet(ctx, request, deadline_seconds=10)
    assert core.recall_diagnostics.flush()
    report = DoctorReport(host="hermes", status="ok")
    with core.storage.write(ctx) as tx:
        assert reserve_call(tx, "TEST-scope", now=core.clock.utc_now())
    with core.storage.read(ctx) as tx:
        _derived_progress(report, tx, ctx.binding)
    check_recall_log(report, ctx.binding, request_id=request["request_id"])
    value = report.to_dict()
    assert value["reflections"]["daily"][0]["calls"] == 1
    assert value["reflections"]["daily"][0]["limit"] == 4
    assert value["recall_log"]["present"] is True
    assert value["recall_attempt"]["request_id"] == request["request_id"]
    assert any(i.get("source_type") == "observation" for i in value["recall_attempt"]["items"])
