"""Measure bounded native-review pagination using only synthetic records."""

import time

import pytest
from scope_recall.core.management import MemoryManagement
from scope_recall.core.restore import InstallationMaintenance

from tests.contract.test_claims import app, capture, draft


@pytest.mark.real_store_time
def test_review_list_pages_all_held_facts_without_a_mirror(app, record_property):
    core, ctx = app
    api = MemoryManagement(core, InstallationMaintenance(ctx))
    api.set_review_policy("TEST-scope", True, expected_version=0)
    for index in range(120):
        subject = f"TEST-project-{index}"
        source = capture(core, ctx, f"{subject} palette amber.")
        proposal = draft(source, "amber", subject=subject, predicate="palette", kind="fact", statement_kind="assertion")
        core.records.accept_claim_proposals(
            ctx,
            dict(
                protocol_version="1.1",
                source_refs=[f"{source.ref}@1"],
                claim_proposals=[proposal],
                resume_proposals=[],
                reference_proposals=[],
            ),
            scope_id="TEST-scope",
            remaining_seconds=10,
        )
    began = time.perf_counter()
    page = api.reviews(limit=30)
    elapsed = (time.perf_counter() - began) * 1000
    keys = [str(item["key"]) for item in page["items"]]
    assert len(keys) == 30
    while page["next_cursor"]:
        page = api.reviews(limit=30, cursor=page["next_cursor"])
        keys.extend(str(item["key"]) for item in page["items"])
    assert len(keys) == len(set(keys)) == 120
    record_property("review_sources", 120)
    record_property("review_pending_facts", 120)
    record_property("review_first_page_ms", round(elapsed, 3))
