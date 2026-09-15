from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path

from scope_recall.contracts import InstanceBinding, TrustedContext, validate_capture
from scope_recall.core.retrieval import AUTOMATIC_PACKET_BUDGET_UNITS


ROOT = Path(__file__).resolve().parents[1]


@dataclass
class FixedInputs:
    now: datetime = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
    sequence: int = 0

    def time(self):
        return self.now.isoformat()

    def next_id(self):
        self.sequence += 1
        return f"TEST-generated-{self.sequence}"


def context(directory, origin="human_direct", test_mode=True, scope="TEST-scope"):
    binding = InstanceBinding("TEST-agent", "TEST-installation", directory, frozenset({scope}), test_mode)
    return TrustedContext(binding, "TEST-session", frozenset({scope}), origin)


def source_event(**changes):
    event = dict(protocol_version="1.1", source_event_key="TEST-S1/m1", source_revision=1,
                 origin="human_direct", role="user", content="仅 TEST 项目用白色，内部表保留价格。",
                 occurred_at="2026-09-05T12:00:00Z", recorded_at="2026-09-05T12:00:00Z",
                 time_precision="instant", capture_state="complete", evidence_refs=[])
    event.update(changes)
    return event


def recall_request(**changes):
    request = dict(protocol_version="1.1", request_id="TEST-request", query="继续 TEST 项目",
                   mode="auto", max_items=6, budget_tokens=AUTOMATIC_PACKET_BUDGET_UNITS)
    request.update(changes)
    return request


def recall_item(**changes):
    item = dict(ref="TEST-claim", revision=1, kind="claim", content="TEST 白色",
                temporal_status="current", origin="human_direct", applicability="仅 TEST 项目",
                evidence_refs=["TEST-event@1"], expandable=True, basis="direct_report")
    item.update(changes)
    return item


def recall_packet(**changes):
    packet = dict(protocol_version="1.1", request_id="TEST-request", status="ok", memory_epoch=1,
                  items=[recall_item()], gaps=[], diagnostic_ref=None, answerability="supported",
                  coverage="partial", unmet_needs=[])
    packet.update(changes)
    return packet


def proposal(**changes):
    claim = dict(kind="constraint", subject="TEST-project", predicate="palette", value_text="仅项目用白色",
                 conditions=["仅 TEST 项目"], statement_kind="assertion", valid_from=None, valid_to=None,
                 evidence_spans=[dict(source_ref="TEST-event", source_revision=1, quote="仅 TEST 项目用白色")])
    result = dict(protocol_version="1.1", source_refs=["TEST-event@1"], claim_proposals=[claim],
                  resume_proposals=[], reference_proposals=[])
    result.update(changes)
    return result


def public_cases(filename):
    return [json.loads(line) for line in (ROOT / "fixtures" / filename).read_text(encoding="utf-8").splitlines() if line]


def raw_case_inputs(case_id, directory, clock):
    route = json.loads((ROOT / "fixtures/input_routes.json").read_text(encoding="utf-8"))["cognitive"][case_id]
    if route["input_status"] != "raw_text_ready":
        raise NotImplementedError(f"{case_id}: requires actual {route['required_setup']}")
    case = next(c for c in public_cases("cognitive_cases.jsonl") if c["id"] == case_id)
    events = []
    for raw in case["source_events"]:
        event = source_event(**raw, recorded_at=clock.time(), occurred_at=None, time_precision="unknown", dataset_id="SYNTHETIC_TEST_ONLY")
        events.append(validate_capture(event, context(directory, raw["origin"])))
    return {"events": events, "query": {"session": case["query"]["session"], "text": case["query"]["text"]}}
