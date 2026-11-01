"""Recorded bilingual extraction and entity decisions through the production worker."""

import itertools
import json
from copy import deepcopy
from pathlib import Path

import pytest
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.entity_resolution import entity_progress
from scope_recall.core.worker import WorkerConfig, drain_worker
from scope_recall.core.worker_consolidation import build_consolidation_model

from tests.contract.test_claims import Clock, capture
from tests.v11_support import context

CORPUS = json.loads((Path(__file__).parents[2] / "prompts/entity.examples.json").read_text(encoding="utf-8"))


class RecordingPort:
    """Replay extraction and identity answers with the current store's source references."""

    def __init__(self, case):
        self.case = case
        self.entity_calls = 0

    def propose(self, messages, **kwargs):
        body = json.loads(messages[-1]["content"])
        if "proposal" in body:
            if messages[0]["content"].startswith("Determine whether"):
                self.entity_calls += 1
                target = next(
                    (
                        v
                        for v in body["candidates"]
                        if v["payload"]["subject"] == self.case["input"]["candidates"][0]["payload"]["subject"]
                    ),
                    None,
                )
                same = self.case["same_entity"] and target is not None
                return json.dumps(
                    dict(
                        support="supported" if same else "uncertain",
                        action="duplicate" if same else "new",
                        target_ref=target["ref"] if same else None,
                        target_revision=target["revision"] if same else None,
                    )
                )
            return json.dumps(dict(support="supported", action="new", target_ref=None, target_revision=None))
        proposals = []
        for source in body["sources"]:
            options = [self.case["input"]["proposal"], self.case["input"]["candidates"][0]["payload"]]
            proposal = deepcopy(next(p for p in options if p["evidence_spans"][0]["quote"] == source["content"]))
            proposal["evidence_spans"][0].update(
                source_ref=source["source_ref"], source_revision=source["source_revision"]
            )
            proposals.append(proposal)
        return json.dumps(
            dict(
                protocol_version="1.1",
                source_refs=body["source_refs"],
                claim_proposals=proposals,
                resume_proposals=[],
                reference_proposals=[],
            )
        )


@pytest.mark.parametrize("case", CORPUS, ids=lambda item: item["id"])
def test_recorded_bilingual_entity_flow(tmp_path, case):
    ctx = context(tmp_path / "TEST-entity-flow")
    core = MemoryCore(CoreConfig(ctx.binding), clock=Clock())
    core.initialize()
    core.test_sequence = itertools.count(1)
    capture(core, ctx, case["input"]["candidates"][0]["payload"]["evidence_spans"][0]["quote"])
    capture(core, ctx, case["input"]["proposal"]["evidence_spans"][0]["quote"])
    port = RecordingPort(case)
    receipt = drain_worker(
        core.storage,
        core.clock,
        ctx,
        config=WorkerConfig("TEST-entity-flow", max_items=32),
        consolidation=build_consolidation_model(port),
        remaining_seconds=30,
    )
    assert receipt.failed == 0, [(i.work_type, i.error_code, i.error_detail) for i in receipt.items]
    with core.storage.read(ctx) as tx:
        assert entity_progress(tx) == dict(total=2, completed=2, pending=0)
        count = tx._check().execute("SELECT count(*) FROM entity_alias_proofs").fetchone()[0]
    assert count == int(case["same_entity"])
