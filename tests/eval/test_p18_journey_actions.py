from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from scope_recall.contracts import InstanceBinding
from scope_recall.core.composition import CoreConfig, MemoryCore
from scope_recall.runtime.instance import RuntimeInstance, RuntimeInstanceConfig

from p18_journey_actions import JourneyActionError, execute_action


@dataclass
class _Clock:
    now: str = "2026-09-06T12:00:00Z"

    def utc_now(self) -> str:
        return self.now

    def monotonic(self) -> float:
        return 100.0


class _Host:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def new_session(self, alias: str, *, action):
        self.calls.append(("new_session", alias))
        return {"actual_host": True, "session_id": f"TEST-session-{alias}"}

    def host_turn(self, query: str, *, session_alias, action):
        self.calls.append(("host_turn", query))
        return {
            "actual_host": True,
            "request_id": "TEST-request-1",
            "turn_id": "TEST-turn-1",
            "session_id": "TEST-session-1",
            "answer_text": "diagnostic host answer",
            "response_bytes": b"{}",
        }


def _runtime(tmp_path: Path) -> RuntimeInstance:
    data = tmp_path / "TEST-runtime-data"
    binding = InstanceBinding("TEST-agent", "TEST-install", data, frozenset({"TEST-scope"}), True)
    clock = _Clock()
    core = MemoryCore(CoreConfig(binding), clock=clock)
    core.initialize()
    config = RuntimeInstanceConfig(binding=binding, session_id="TEST-session", allowed_scope_ids=frozenset({"TEST-scope"}))
    return RuntimeInstance(config=config, core=core, auxiliary=None)


def _action(operation_id: str, kind: str, **extra):
    return {"operation_id": operation_id, "journey_id": "J01", "step_order": 1, "operation_kind": kind, **extra}


def test_source_capture_persists_through_real_core_and_deterministic_probe(tmp_path: Path):
    runtime = _runtime(tmp_path)
    root = tmp_path / "TEST-artifacts"
    root.mkdir()
    host = _Host()
    capture = execute_action(
        _action(
            "J01-op-01",
            "source_capture",
            model_input={
                "source_events": [
                    {
                        "source_type": "human_direct",
                        "speaker_role": "user",
                        "text": "TEST archive entry has a durable revision.",
                        "occurred_at": "2026-09-06T11:59:00Z",
                    }
                ]
            },
        ),
        runtime,
        host,
        root,
    )
    assert capture["status"] == "COMPLETED"
    assert capture["formal_evaluation"] is False
    assert capture["execution_evidence"] == "TEST_DIAGNOSTIC_ONLY"
    assert len(capture["source_capture_refs"]) == 1
    ref = capture["source_capture_refs"][0]["ref"]
    revision = capture["source_capture_refs"][0]["revision"]
    with runtime.core.storage.read(runtime.config.context()) as tx:
        assert tx.source(ref, revision).event["content"] == "TEST archive entry has a durable revision."

    probe = execute_action(
        _action("J01-op-02", "deterministic_assertion", probe="source_exists", ref=ref, revision=revision),
        runtime,
        host,
        root,
    )
    assert probe["status"] == "COMPLETED"
    assert probe["result"]["observation"]["exists"] is True


def test_host_operations_require_explicit_real_identifiers_and_never_leak_controls(tmp_path: Path):
    runtime = _runtime(tmp_path)
    root = tmp_path / "TEST-artifacts"
    root.mkdir()
    host = _Host()
    session = execute_action(_action("J01-op-03", "new_session", session_alias="followup"), runtime, host, root)
    assert session["status"] == "COMPLETED"
    assert session["session_id"] == "TEST-session-followup"
    turn = execute_action(
        _action(
            "J01-op-04",
            "host_turn",
            session_alias="followup",
            model_input={"query": {"text": "What was recorded?"}},
            expected={"must": "never sent to host"},
        ),
        runtime,
        host,
        root,
    )
    assert turn["status"] == "FAILED"
    assert turn["error_code"] == "control_field_in_action_input"
    # The failed operation is durable and the host never saw the query.
    assert host.calls == [("new_session", "followup")]

    good = execute_action(
        _action("J01-op-05", "host_turn", session_alias="followup", model_input={"query": {"text": "What was recorded?"}}),
        runtime,
        host,
        root,
    )
    assert good["status"] == "COMPLETED"
    assert good["model_call"] is True
    assert good["answer_sha256"]
    assert "diagnostic host answer" not in (root / "operations" / "J01-op-05.json").read_text(encoding="utf-8")


def test_unknown_fault_requires_explicit_finite_handler_and_duplicate_receipt_is_rejected(tmp_path: Path):
    runtime = _runtime(tmp_path)
    root = tmp_path / "TEST-artifacts"
    root.mkdir()
    host = _Host()
    unsupported = execute_action(_action("J02-op-01", "fault_injection", fault_mode="sqlite_unavailable"), runtime, host, root)
    assert unsupported["status"] == "UNSUPPORTED"
    assert unsupported["model_call"] is False
    with pytest.raises(JourneyActionError, match="operation_already_recorded"):
        execute_action(_action("J02-op-01", "fault_injection", fault_mode="sqlite_unavailable"), runtime, host, root)
    invalid = execute_action(_action("J02-op-02", "fault_injection", fault_mode="arbitrary"), runtime, host, root)
    assert invalid["status"] == "FAILED"
    assert invalid["error_code"] == "fault_mode_invalid"


def test_authorized_revision_uses_existing_core_mutation_contract(tmp_path: Path):
    runtime = _runtime(tmp_path)
    root = tmp_path / "TEST-artifacts"
    root.mkdir()
    host = _Host()
    first = execute_action(
        _action(
            "J04-op-01",
            "source_capture",
            model_input={
                "source_events": [
                    {
                        "source_type": "human_direct",
                        "speaker_role": "user",
                        "text": "TEST-project palette is white.",
                        "occurred_at": "2026-09-06T11:00:00Z",
                    }
                ]
            },
        ),
        runtime,
        host,
        root,
    )
    source_ref = first["source_capture_refs"][0]["ref"]
    proposal = {
        "protocol_version": "1.1",
        "source_refs": [f"{source_ref}@1"],
        "claim_proposals": [
            {
                "kind": "decision",
                "subject": "TEST-project",
                "predicate": "palette",
                "value_text": "white",
                "conditions": [],
                "statement_kind": "decision",
                "valid_from": "2026-09-06T11:00:00Z",
                "valid_to": None,
                "evidence_spans": [{"source_ref": source_ref, "source_revision": 1, "quote": "TEST-project palette is white."}],
            }
        ],
        "resume_proposals": [],
        "reference_proposals": [],
    }
    accepted = runtime.core.accept_claim_proposals(runtime.config.context(), proposal, scope_id="TEST-scope", remaining_seconds=10)
    claim = accepted.items[0]
    second = execute_action(
        _action(
            "J04-op-02",
            "source_capture",
            model_input={
                "source_events": [
                    {
                        "source_type": "human_direct",
                        "speaker_role": "user",
                        "text": "correct: TEST-project palette is silver.",
                        "occurred_at": "2026-09-06T11:30:00Z",
                    }
                ]
            },
        ),
        runtime,
        host,
        root,
    )
    second_ref = second["source_capture_refs"][0]["ref"]
    changed = execute_action(
        _action(
            "J04-op-03",
            "authorized_state_change",
            state_change={
                "method": "revise",
                "request": {
                    "protocol_version": "1.1",
                    "target_ref": claim.ref,
                    "expected_revision": claim.revision,
                    "new_value": "silver",
                    "conditions": [],
                    "source_evidence_refs": [f"{second_ref}@1"],
                    "valid_from": "2026-09-06T11:30:00Z",
                },
            },
        ),
        runtime,
        host,
        root,
    )
    assert changed["status"] == "COMPLETED", (changed.get("error_code"), changed.get("result"))
    current = runtime.core.current_claim(runtime.config.context(), claim.ref)
    assert current is not None and current.payload["value_text"] == "silver"


def test_artifact_hash_and_path_are_verified_before_core_work(tmp_path: Path):
    runtime = _runtime(tmp_path)
    root = tmp_path / "TEST-artifacts"
    root.mkdir()
    artifact = root / "source.json"
    artifact.write_text('{"source_events": [{"source_type": "human_direct", "speaker_role": "user", "text": "TEST artifact source", "occurred_at": "2026-09-06T12:00:00Z"}]}', encoding="utf-8")
    import hashlib

    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    result = execute_action(
        _action("J03-op-01", "source_capture", input_artifact_ref="source.json", input_artifact_sha256=digest),
        runtime,
        _Host(),
        root,
    )
    assert result["status"] == "COMPLETED"
    outside = execute_action(
        _action("J03-op-02", "source_capture", input_artifact_ref="../source.json", input_artifact_sha256=digest),
        runtime,
        _Host(),
        root,
    )
    assert outside["status"] == "FAILED"
    assert outside["error_code"] == "input_artifact_outside_root"
