"""Compile private P18 sealed-input execution units.

This module is the independent sealed-input executor.  It never opens
``gold.jsonl`` and never contacts a host or model.  Natural source/query
fields are kept in units; control and scoring metadata stay private.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from p18_score_report import (
    IDENTITY_MAP_SHA256,
    authoritative_query_pair_indices,
    frozen_public_identity_map,
    public_pair_index,
)

EXPECTED_PROTOCOL_SHA256 = "45b70192857b5b8645dd2bf409db55f320f1cf31f297e89910f9f340e76d32d4"
EXPECTED_ALLOCATION_SHA256 = "644e26cd9efa9fe51bbf40d35e6cd39ef363bc2489383b163f6f1ade54da54dc"
EXPECTED_JOURNEY_SHA256 = "78b1465904a8cc3b121c9e297d2628ca031158e6c26c8b5598d28ccad3d3e243"
EXPECTED_PRIVATE_JOURNEY_SHA256 = "b9ee535188320d3fc588b1adf5a4a28f75c57018bb6a56257492b08d81914194"
EXPECTED_SCHEMA = "scope-recall.eval-sealed.v6"
EXPECTED_DATASET = "TEST-EVAL-SEALED-120-v6"
SOURCE_TYPES = ("human_direct", "assistant_visible", "tool_observation", "external_document")
ROLES = ("user", "assistant", "tool", "document")
CONTROL_FIELDS = frozenset({
    "case_id", "case_index", "case_index_control_only", "group_id", "core_class", "condition",
    "answerability", "answerability_layers", "required_facts", "l3_memory_expectation",
    "l4_task_expectation", "prohibited_errors", "gold", "expected", "oracle", "control_only",
    "independence_rationale",
})
RAW_FIELDS = frozenset({"dataset_id", "record_key", "pair_id", "history", "query", "simulation"})
RAW_EVENT_FIELDS = frozenset({"event_id", "occurred_at", "sequence", "source_type", "speaker_role", "text"})
RAW_QUERY_FIELDS = frozenset({"clean_session", "query_id", "session_id", "text"})
GROUPS = tuple(f"B{i:02d}" for i in range(1, 13))
CLASSES = ("positive", "negative", "ambiguous")


class SealedPlanError(ValueError):
    """Input, hash, privacy, or allocation rejection."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise SealedPlanError(f"json_read_failed:{path.name}") from exc
    if type(value) is not dict:
        raise SealedPlanError(f"json_object_required:{path.name}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        values = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, UnicodeError, ValueError) as exc:
        raise SealedPlanError(f"jsonl_read_failed:{path.name}") from exc
    if any(not isinstance(value, Mapping) for value in values):
        raise SealedPlanError(f"jsonl_object_required:{path.name}")
    return [dict(value) for value in values]


def _reject_control_fields(value: object, path: str = "input") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in CONTROL_FIELDS:
                raise SealedPlanError(f"control_field_in_raw:{path}.{key}")
            _reject_control_fields(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_control_fields(child, f"{path}[{index}]")


def _contains_key(value: object, wanted: str) -> bool:
    if isinstance(value, Mapping):
        return any(key == wanted or _contains_key(child, wanted) for key, child in value.items())
    if isinstance(value, list):
        return any(_contains_key(child, wanted) for child in value)
    return False


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SealedPlanError(f"invalid_{field}")
    return value


def _project_model_input(row: Mapping[str, Any]) -> dict[str, Any]:
    """Allowlist natural source and query fields entering a host prompt."""
    history = row.get("history")
    query = row.get("query")
    if not isinstance(history, list) or not isinstance(query, Mapping):
        raise SealedPlanError("model_input_history_query_required")
    projected_history: list[dict[str, str]] = []
    for event in history:
        if not isinstance(event, Mapping):
            raise SealedPlanError("model_input_event_required")
        source_type = event.get("source_type")
        speaker_role = event.get("speaker_role")
        text = event.get("text")
        if source_type not in SOURCE_TYPES or speaker_role not in ROLES or not isinstance(text, str) or not text.strip():
            raise SealedPlanError("model_input_event_allowlist_rejected")
        projected = {"source_type": source_type, "speaker_role": speaker_role, "text": text}
        occurred_at = event.get("occurred_at")
        if isinstance(occurred_at, str) and occurred_at:
            projected["occurred_at"] = occurred_at
        projected_history.append(projected)
    query_text = query.get("text")
    if not isinstance(query_text, str) or not query_text.strip():
        raise SealedPlanError("model_input_query_allowlist_rejected")
    return {"history": projected_history, "query": {"text": query_text}}


def _validate_raw_rows(rows: Sequence[Mapping[str, Any]], *, dataset_id: str) -> list[dict[str, Any]]:
    if not rows:
        raise SealedPlanError("raw_is_empty")
    validated: list[dict[str, Any]] = []
    seen_records: set[str] = set()
    seen_queries: set[str] = set()
    pairs: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for index, raw in enumerate(rows, start=1):
        if not isinstance(raw, Mapping) or set(raw) != RAW_FIELDS:
            raise SealedPlanError(f"raw_schema_mismatch:{index}")
        if raw.get("dataset_id") != dataset_id or raw.get("simulation") is not True:
            raise SealedPlanError(f"raw_identity_mismatch:{index}")
        record_key = _text(raw.get("record_key"), "record_key")
        pair_id = _text(raw.get("pair_id"), "pair_id")
        if record_key in seen_records:
            raise SealedPlanError("duplicate_record_key")
        seen_records.add(record_key)
        history = raw.get("history")
        query = raw.get("query")
        if not isinstance(history, list) or not history:
            raise SealedPlanError(f"history_required:{index}")
        if not isinstance(query, Mapping) or set(query) != RAW_QUERY_FIELDS:
            raise SealedPlanError(f"query_schema_mismatch:{index}")
        query_id = _text(query.get("query_id"), "query_id")
        _text(query.get("session_id"), "session_id")
        if query_id in seen_queries:
            raise SealedPlanError("duplicate_query_id")
        seen_queries.add(query_id)
        if query.get("clean_session") is not True:
            raise SealedPlanError(f"query_clean_session_invalid:{index}")
        _text(query.get("text"), "query_text")
        event_ids: set[str] = set()
        sequences: list[int] = []
        for event in history:
            if not isinstance(event, Mapping) or set(event) != RAW_EVENT_FIELDS:
                raise SealedPlanError(f"history_event_schema_mismatch:{index}")
            event_id = _text(event.get("event_id"), "event_id")
            if event_id in event_ids:
                raise SealedPlanError(f"duplicate_event_id:{index}")
            event_ids.add(event_id)
            sequence = event.get("sequence")
            if type(sequence) is not int or sequence < 1:
                raise SealedPlanError(f"history_sequence_invalid:{index}")
            sequences.append(sequence)
            _text(event.get("occurred_at"), "occurred_at")
            _text(event.get("text"), "history_text")
            if event.get("source_type") not in SOURCE_TYPES or event.get("speaker_role") not in ROLES:
                raise SealedPlanError(f"history_model_input_type_invalid:{index}")
        if sequences != list(range(1, len(sequences) + 1)):
            raise SealedPlanError(f"history_sequence_not_contiguous:{index}")
        item = dict(raw)
        validated.append(item)
        pairs.setdefault(pair_id, []).append(item)
    if any(len(group) != 2 for group in pairs.values()):
        raise SealedPlanError("each_pair_must_have_two_variants")
    return validated


def _pair_groups(rows: Sequence[Mapping[str, Any]]) -> OrderedDict[str, list[dict[str, Any]]]:
    result: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for row in rows:
        result.setdefault(str(row["pair_id"]), []).append(dict(row))
    return result


def _private_association(row: Mapping[str, Any], *, unit_id: str, host_id: str | None, arm_id: str, kind: str, **extra: Any) -> dict[str, Any]:
    history = row.get("history", [])
    query = row.get("query", {})
    return {
        "unit_id": unit_id, "host_id": host_id, "arm_id": arm_id, "kind": kind,
        "dataset_id": row.get("dataset_id"), "record_key": row.get("record_key"), "pair_id": row.get("pair_id"),
        "query_id": query.get("query_id") if isinstance(query, Mapping) else None,
        "session_id": query.get("session_id") if isinstance(query, Mapping) else None,
        "event_ids": [e.get("event_id") for e in history if isinstance(e, Mapping)],
        "event_sequences": [e.get("sequence") for e in history if isinstance(e, Mapping)], **extra,
    }


def _synthetic_public_metadata(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, str]]:
    """Public-test helper; sealed compilation requires manifest metadata."""
    result: dict[str, dict[str, str]] = {}
    for index, pair_id in enumerate(_pair_groups(rows), start=1):
        pos_in_group = (index - 1) % 10
        if pos_in_group < 4:
            core_class = "positive"
        elif pos_in_group < 7:
            core_class = "negative"
        else:
            core_class = "ambiguous"
        result[pair_id] = {"pair_id": pair_id, "group_id": GROUPS[(index - 1) // 10], "core_class": core_class}
    return result


def _select_query_pairs(metadata: Mapping[str, Mapping[str, str]]) -> list[str]:
    """Select the frozen public 40-pair subset.

    Authority is ``build_public_identity_map`` / the frozen identity digest, not
    a hash of whatever dataset pair_id string the sealed manifest happens to use.
    ``P002`` and ``TEST-pair-002`` both normalize to public index 2.
    """
    frozen_public_identity_map()
    wanted = authoritative_query_pair_indices()
    by_index: dict[int, str] = {}
    for pair_id, item in metadata.items():
        if not isinstance(item, Mapping):
            raise SealedPlanError("allocation_metadata_row_invalid")
        index = public_pair_index(str(pair_id))
        if index is None:
            raise SealedPlanError(f"pair_id_public_index_required:{pair_id}")
        if index in by_index and by_index[index] != pair_id:
            raise SealedPlanError(f"public_pair_index_collision:{index}")
        by_index[index] = str(pair_id)
    selected = []
    for index in wanted:
        pair_id = by_index.get(index)
        if pair_id is None:
            raise SealedPlanError(f"identity_map_pair_missing:TEST-pair-{index:03d}")
        selected.append(pair_id)
    if len(selected) != 40 or len(set(selected)) != 40:
        raise SealedPlanError("allocation_query_pair_count_mismatch")
    return selected


def _journey_operation_kinds(journey_id: str) -> tuple[str, ...]:
    templates = {
        "J01": ("source_capture", "authorized_state_change", "new_session", "host_turn", "deterministic_assertion", "new_session"),
        "J02": ("fault_injection", "deterministic_assertion", "authorized_state_change", "new_session", "deterministic_assertion", "host_turn"),
        "J03": ("source_capture", "new_session", "host_turn", "authorized_state_change", "new_session", "new_session"),
        "J04": ("source_capture", "host_turn", "new_session", "deterministic_assertion", "authorized_state_change", "deterministic_assertion"),
        "J05": ("source_capture", "authorized_state_change", "new_session", "host_turn", "deterministic_assertion", "host_turn"),
        "J06": ("source_capture", "host_turn", "new_session", "deterministic_assertion", "fault_injection", "deterministic_assertion"),
        "J07": ("source_capture", "new_session", "host_turn", "host_turn", "deterministic_assertion", "deterministic_assertion"),
        "J08": ("source_capture", "host_turn", "authorized_state_change", "fault_injection", "deterministic_assertion", "fault_injection"),
    }
    try:
        return templates[journey_id]
    except KeyError as exc:
        raise SealedPlanError(f"unknown_journey:{journey_id}") from exc


def _validate_journeys(journeys: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if len(journeys) != 8:
        raise SealedPlanError("journey_count_mismatch")
    expected = {f"J{i:02d}" for i in range(1, 9)}
    found: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for journey in journeys:
        journey_id = journey.get("id")
        if not isinstance(journey_id, str) or journey_id not in expected or journey_id in found:
            raise SealedPlanError("journey_id_invalid")
        found.add(journey_id)
        steps = journey.get("steps")
        if not isinstance(steps, list) or len(steps) != 6:
            raise SealedPlanError(f"journey_step_count_invalid:{journey_id}")
        ordered: list[dict[str, Any]] = []
        for expected_order, step in enumerate(steps, start=1):
            if not isinstance(step, Mapping) or step.get("order") != expected_order:
                raise SealedPlanError(f"journey_step_order_invalid:{journey_id}")
            ordered.append({"order": expected_order, "instruction": _text(step.get("instruction"), "journey_instruction")})
        normalized.append({"id": journey_id, "steps": ordered})
    if found != expected:
        raise SealedPlanError("journey_id_set_mismatch")
    return sorted(normalized, key=lambda item: item["id"])


def _journey_natural_input(journey_id: str, order: int, instruction: str, operation_kind: str) -> tuple[dict[str, Any], str]:
    """Create an unseen synthetic input from a public step, without its instruction."""
    digest = hashlib.sha256(("p18-private-journey-v1\n" + journey_id + "\n" + str(order) + "\n" + instruction).encode("utf-8")).hexdigest()
    token = digest[:12]
    if operation_kind == "source_capture":
        return {"source_events": [{"source_type": "human_direct", "speaker_role": "user", "text": f"A synthetic archive entry {token} records revision {order} with a bounded test status.", "occurred_at": f"2026-01-{order:02d}T00:00:00Z"}]}, digest
    if operation_kind == "host_turn":
        return {"query": {"text": f"What recorded state belongs to synthetic archive entry {token}?"}}, digest
    return {}, digest


def _evidence_kinds(operation_kind: str) -> list[str]:
    return {
        "source_capture": ["source_event"], "host_turn": ["query_response", "recall_packet"],
        "new_session": ["session_record"], "authorized_state_change": ["state_change_record"],
        "fault_injection": ["fault_record"], "deterministic_assertion": ["deterministic_check"],
    }[operation_kind]


def _build_journey_units(journeys: Sequence[Mapping[str, Any]], *, host_id: str, arm_id: str, associations: list[dict[str, Any]], artifact_root: Path | None = None) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for journey in journeys:
        journey_id = str(journey["id"])
        for step, operation_kind in zip(journey["steps"], _journey_operation_kinds(journey_id), strict=True):
            order = int(step["order"])
            unit_id = f"{host_id}-{arm_id}-{journey_id}-step-{order:02d}"
            natural_input, instance_fingerprint = _journey_natural_input(journey_id, order, str(step["instruction"]), operation_kind)
            input_ref: str | None = None
            if natural_input and artifact_root is not None:
                artifact_path = artifact_root / host_id / arm_id / journey_id / f"step-{order:02d}.json"
                artifact_path.parent.mkdir(parents=True, exist_ok=True)
                with artifact_path.open("x", encoding="utf-8", newline="\n") as artifact_handle:
                    artifact_handle.write(json.dumps(natural_input, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
                input_ref = str(artifact_path.relative_to(artifact_root.parent))
            units.append({
                "unit_id": unit_id, "kind": "journey_operation", "journey_id": journey_id,
                "operation_id": f"{journey_id}-op-{order:02d}", "source_step_orders": [order],
                "operation_kind": operation_kind,
                "session_boundary": "new_session" if operation_kind == "new_session" else "same_journey_session",
                "input_artifact_ref": input_ref,
                "required_evidence_artifact_kinds": _evidence_kinds(operation_kind),
                "primary_round_ordinal_or_null": order if operation_kind == "host_turn" else None,
                "round_budget_per_journey": 8, "source_sequence": "fixed_journey_operation_graph",
                "model_input": natural_input,
            })
            associations.append({"unit_id": unit_id, "host_id": host_id, "arm_id": arm_id, "kind": "journey_operation", "journey_id": journey_id, "step_order": order, "operation_kind": operation_kind, "source_step_orders": [order], "instance_fingerprint": instance_fingerprint})
    return units


def _load_journey_bundle(bundle_root: Path) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Load Astra's frozen private operation graph and verify its artifacts."""
    bundle_root = bundle_root.expanduser().resolve()
    if bundle_root.is_file():
        return _load_private_journey_bundle(bundle_root)
    lowered = str(bundle_root).replace("/", "\\").lower()
    if not bundle_root.is_dir() or lowered == "f:\\agents" or lowered.startswith("f:\\agents\\"):
        raise SealedPlanError("journey_bundle_root_invalid")
    summary = _read_json(bundle_root / "bundle-summary.json")
    if summary.get("schema") != "scope-recall.p18-journey-bundle.v1" or summary.get("journey_spec_sha256") != EXPECTED_JOURNEY_SHA256 or not isinstance(summary.get("bundle_sha256"), str) or not summary["bundle_sha256"]:
        raise SealedPlanError("journey_bundle_binding_mismatch")
    records = summary.get("journeys")
    if not isinstance(records, list) or len(records) != 8:
        raise SealedPlanError("journey_bundle_count_mismatch")
    allowed = {"source_capture", "host_turn", "new_session", "test_artifact_setup", "authorized_state_change", "fault_injection", "deterministic_assertion"}
    result: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if not isinstance(record, Mapping) or record.get("id") not in {f"J{i:02d}" for i in range(1, 9)}:
            raise SealedPlanError("journey_bundle_id_invalid")
        journey_id = str(record["id"])
        relative = record.get("operation_file")
        if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise SealedPlanError("journey_bundle_operation_path_invalid")
        path = (bundle_root / relative).resolve()
        if path.parent != (bundle_root / Path(relative).parent).resolve() or not path.is_file():
            raise SealedPlanError("journey_bundle_operation_file_missing")
        if record.get("operation_sha256") != _sha256(path):
            raise SealedPlanError("journey_bundle_operation_hash_mismatch")
        operations = _read_jsonl(path)
        if not operations or int(record.get("operation_count", -1)) != len(operations):
            raise SealedPlanError("journey_bundle_operation_count_mismatch")
        seen_steps: set[int] = set()
        seen_operation_ids: set[str] = set()
        primary_rounds = 0
        normalized: list[dict[str, Any]] = []
        for operation in operations:
            if set(operation) - {"journey_id", "operation_id", "source_step_orders", "operation_kind", "session_boundary", "input_artifact_ref", "input_artifact_sha256", "required_evidence_artifact_kinds", "primary_round_ordinal_or_null", "model_input", "round_budget_per_journey"}:
                raise SealedPlanError("journey_bundle_operation_fields_invalid")
            if operation.get("journey_id") != journey_id or operation.get("operation_kind") not in allowed:
                raise SealedPlanError("journey_bundle_operation_identity_invalid")
            operation_id = operation.get("operation_id")
            if not isinstance(operation_id, str) or not operation_id.strip() or operation_id in seen_operation_ids:
                raise SealedPlanError("journey_bundle_operation_id_invalid")
            seen_operation_ids.add(operation_id)
            steps = operation.get("source_step_orders")
            if not isinstance(steps, list) or not steps or any(type(step) is not int or not 1 <= step <= 6 for step in steps):
                raise SealedPlanError("journey_bundle_step_reference_invalid")
            seen_steps.update(steps)
            round_ordinal = operation.get("primary_round_ordinal_or_null")
            if round_ordinal is not None:
                if type(round_ordinal) is not int or round_ordinal < 1:
                    raise SealedPlanError("journey_bundle_round_invalid")
                primary_rounds += 1
            if operation.get("operation_kind") == "host_turn" and round_ordinal is None:
                raise SealedPlanError("journey_bundle_host_round_missing")
            if operation.get("operation_kind") != "host_turn" and round_ordinal is not None:
                raise SealedPlanError("journey_bundle_non_host_round_invalid")
            natural = operation.get("model_input", {})
            if not isinstance(natural, Mapping) or _contains_key(natural, "instruction") or any(key in CONTROL_FIELDS for key in natural):
                raise SealedPlanError("journey_bundle_model_input_leaks_control")
            _reject_control_fields(natural, "journey_model_input")
            evidence_kinds = operation.get("required_evidence_artifact_kinds")
            if not isinstance(evidence_kinds, list) or any(not isinstance(kind, str) or not kind.strip() for kind in evidence_kinds):
                raise SealedPlanError("journey_bundle_evidence_kinds_invalid")
            artifact_ref = operation.get("input_artifact_ref")
            if artifact_ref is not None:
                if not isinstance(artifact_ref, str) or Path(artifact_ref).is_absolute() or ".." in Path(artifact_ref).parts:
                    raise SealedPlanError("journey_bundle_artifact_path_invalid")
                artifact = (bundle_root / artifact_ref).resolve()
                if artifact.parent != (bundle_root / Path(artifact_ref).parent).resolve() or not artifact.is_file():
                    raise SealedPlanError("journey_bundle_artifact_missing")
                if operation.get("input_artifact_sha256") != _sha256(artifact):
                    raise SealedPlanError("journey_bundle_artifact_hash_mismatch")
                artifact_value = _read_json(artifact)
                _reject_control_fields(artifact_value, "journey_artifact")
                if _contains_key(artifact_value, "instruction"):
                    raise SealedPlanError("journey_bundle_artifact_leaks_instruction")
            normalized.append(dict(operation))
        if seen_steps != set(range(1, 7)) or primary_rounds > 8:
            raise SealedPlanError("journey_bundle_step_or_round_coverage_invalid")
        result[journey_id] = normalized
    if set(result) != {f"J{i:02d}" for i in range(1, 9)}:
        raise SealedPlanError("journey_bundle_id_set_mismatch")
    return result, summary


_ACTION_KIND_MAP = {
    "host_turn": "host_turn", "new_session": "new_session", "copy_assets": "test_artifact_setup",
    "prepare_git_workspace": "test_artifact_setup", "seed_scale_archive": "test_artifact_setup",
    "backup_sqlite": "test_artifact_setup", "advance_git_workspace": "authorized_state_change",
    "authorized_forget": "authorized_state_change", "release_held_consolidation": "authorized_state_change",
    "restore_snapshot": "authorized_state_change", "replay_deletion_ledger": "authorized_state_change",
    "sqlite_restore": "authorized_state_change", "reinject_observed_memory": "authorized_state_change",
    "assert_evidence": "deterministic_assertion", "snapshot_immediate_state": "deterministic_assertion",
    "bounded_enumeration": "deterministic_assertion", "drain_and_observe": "deterministic_assertion",
    "hold_real_consolidation": "fault_injection", "sqlite_unavailable": "fault_injection",
}


def _load_private_journey_bundle(bundle_file: Path) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    if _sha256(bundle_file) != EXPECTED_PRIVATE_JOURNEY_SHA256:
        raise SealedPlanError("private_journey_bundle_hash_mismatch")
    payload = _read_json(bundle_file)
    if payload.get("schema") != "scope-recall.p18-private-journey-execution.v1" or payload.get("specification", {}).get("sha256") != EXPECTED_JOURNEY_SHA256:
        raise SealedPlanError("private_journey_bundle_schema_mismatch")
    journeys = payload.get("journeys")
    if not isinstance(journeys, list) or len(journeys) != 8 or payload.get("primary_round_budget_per_journey") != 8 or payload.get("same_private_inputs_all_hosts_and_arms") is not True:
        raise SealedPlanError("private_journey_bundle_contract_mismatch")
    result: dict[str, list[dict[str, Any]]] = {}
    allowed = set(_ACTION_KIND_MAP)
    for journey in journeys:
        if not isinstance(journey, Mapping) or journey.get("journey_id") not in {f"J{i:02d}" for i in range(1, 9)}:
            raise SealedPlanError("private_journey_id_invalid")
        journey_id = str(journey["journey_id"])
        actions = journey.get("actions")
        if not isinstance(actions, list) or not actions:
            raise SealedPlanError("private_journey_actions_missing")
        seen_steps: set[int] = set()
        seen_ids: set[str] = set()
        rounds = 0
        operations: list[dict[str, Any]] = []
        for action in actions:
            if not isinstance(action, Mapping) or action.get("kind") not in allowed:
                raise SealedPlanError("private_journey_action_kind_invalid")
            operation_id = _text(action.get("operation_id"), "operation_id")
            if operation_id in seen_ids:
                raise SealedPlanError("private_journey_operation_id_duplicate")
            seen_ids.add(operation_id)
            step_orders = action.get("source_step_orders")
            if not isinstance(step_orders, list) or not step_orders or any(type(order) is not int or not 1 <= order <= 6 for order in step_orders):
                raise SealedPlanError("private_journey_step_orders_invalid")
            seen_steps.update(step_orders)
            raw_kind = str(action["kind"])
            round_ordinal = action.get("primary_round_ordinal")
            if raw_kind == "host_turn":
                if type(round_ordinal) is not int or round_ordinal < 1:
                    raise SealedPlanError("private_journey_host_round_invalid")
                rounds += 1
            elif round_ordinal is not None:
                raise SealedPlanError("private_journey_non_host_round_invalid")
            parameters = action.get("parameters", {})
            if not isinstance(parameters, Mapping):
                raise SealedPlanError("private_journey_parameters_invalid")
            natural: dict[str, Any] = {}
            input_ref: str | None = None
            input_sha: str | None = None
            input_descriptor = parameters.get("input")
            if isinstance(input_descriptor, Mapping):
                relative = input_descriptor.get("path")
                input_sha = input_descriptor.get("sha256")
                if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts or not isinstance(input_sha, str):
                    raise SealedPlanError("private_journey_input_reference_invalid")
                artifact = (bundle_file.parent / relative).resolve()
                if artifact.parent != (bundle_file.parent / Path(relative).parent).resolve() or not artifact.is_file() or _sha256(artifact) != input_sha:
                    raise SealedPlanError("private_journey_input_artifact_invalid")
                artifact_value = _read_json(artifact)
                _reject_control_fields(artifact_value, "private_journey_input")
                if _contains_key(artifact_value, "instruction"):
                    raise SealedPlanError("private_journey_input_instruction_leak")
                query = artifact_value.get("query")
                if isinstance(query, str) and query.strip():
                    natural = {"query": {"text": query}}
                elif isinstance(artifact_value.get("source_events"), list):
                    natural = {"source_events": artifact_value["source_events"]}
                else:
                    raise SealedPlanError("private_journey_input_natural_fields_missing")
                input_ref = str(artifact)
            canonical_kind = _ACTION_KIND_MAP[raw_kind]
            evidence = ["query_response", "recall_packet"] if canonical_kind == "host_turn" else [canonical_kind]
            operations.append({
                "journey_id": journey_id, "operation_id": operation_id, "operation_kind": canonical_kind,
                "action_kind": raw_kind, "source_step_orders": step_orders,
                "session_boundary": "new_session" if raw_kind == "new_session" else f"session:{action.get('session_alias', 's1')}",
                "input_artifact_ref": input_ref, "input_artifact_sha256": input_sha,
                "required_evidence_artifact_kinds": evidence,
                "primary_round_ordinal_or_null": round_ordinal,
                "round_budget_per_journey": 8, "model_input": natural,
                "execution_parameters": dict(parameters),
            })
        if seen_steps != set(range(1, 7)) or rounds > 8:
            raise SealedPlanError("private_journey_step_or_round_coverage_invalid")
        result[journey_id] = operations
    if set(result) != {f"J{i:02d}" for i in range(1, 9)}:
        raise SealedPlanError("private_journey_id_set_invalid")
    return result, {"schema": payload["schema"], "bundle_sha256": EXPECTED_PRIVATE_JOURNEY_SHA256, "journey_spec_sha256": EXPECTED_JOURNEY_SHA256}


def _build_bundle_journey_units(bundle: Mapping[str, Sequence[Mapping[str, Any]]], *, host_id: str, arm_id: str, associations: list[dict[str, Any]], bundle_root: Path, journey_operation_ids: Mapping[str, Sequence[str]] | None = None) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for journey_id in sorted(bundle):
        allowed_ids = set(journey_operation_ids.get(journey_id, ())) if journey_operation_ids is not None else None
        for operation in bundle[journey_id]:
            operation_id = _text(operation.get("operation_id"), "operation_id")
            if allowed_ids is None:
                # The formal operation map contains executable host rounds;
                # controls remain in the private journey bundle for the
                # journey executor and are never model units.
                if operation.get("operation_kind") != "host_turn":
                    continue
            elif operation_id not in allowed_ids:
                continue
            unit_id = f"{host_id}-{arm_id}-{journey_id}-{operation_id}"
            unit = dict(operation)
            unit["unit_id"] = unit_id
            unit["kind"] = "journey_operation"
            unit["host_id"] = host_id
            unit["arm_id"] = arm_id
            if unit.get("input_artifact_ref") is not None:
                unit["input_artifact_ref"] = str((bundle_root / str(unit["input_artifact_ref"])).resolve())
            units.append(unit)
            associations.append({"unit_id": unit_id, "host_id": host_id, "arm_id": arm_id, "kind": "journey_operation", "journey_id": journey_id, "operation_id": operation_id, "source_step_orders": operation.get("source_step_orders"), "operation_kind": operation.get("operation_kind")})
    return units


def _g0_journey_operation_ids(bundle_file: Path, journey_ids: Sequence[str], *, host_id: str, arm_id: str) -> dict[str, tuple[str, ...]]:
    """Project the private bundle through g0's executable host-turn map."""
    try:
        from p18_run_journey import journey_operation_map
    except ImportError as exc:
        raise SealedPlanError("journey_operation_map_unavailable") from exc
    result: dict[str, tuple[str, ...]] = {}
    for journey_id in journey_ids:
        mapped = journey_operation_map(bundle_file, journey_id, host_id=host_id, arm_id=arm_id)
        if not isinstance(mapped, Mapping) or not mapped:
            raise SealedPlanError("journey_operation_map_empty")
        result[journey_id] = tuple(str(operation_id) for operation_id in mapped)
    return result


def build_units_from_rows(rows: Sequence[Mapping[str, Any]], *, host_ids: Sequence[str] = ("hermes_a2a", "codex_windows_desktop"), arm_ids: Sequence[str] = ("A", "B", "C", "D"), dataset_id: str = "PUBLIC-TEST", metadata: Mapping[str, Mapping[str, str]] | None = None, journey_specs: Sequence[Mapping[str, Any]] | None = None, journey_bundle: Mapping[str, Sequence[Mapping[str, Any]]] | None = None, journey_bundle_root: Path | None = None, journey_operation_ids: Mapping[str, Sequence[str]] | None = None) -> dict[str, Any]:
    """Build deterministic public/test units with full core and real journey steps."""
    pairs = _pair_groups(rows)
    if len(pairs) < 40:
        raise SealedPlanError("not_enough_pairs_for_host_queries")
    metadata_map = dict(metadata or _synthetic_public_metadata(rows))
    if set(metadata_map) != set(pairs):
        raise SealedPlanError("allocation_metadata_pair_mismatch")
    query_pair_ids = _select_query_pairs(metadata_map)
    journeys = _validate_journeys(journey_specs or [{"id": f"J{i:02d}", "steps": [{"order": n, "instruction": f"public step {n}"} for n in range(1, 7)]} for i in range(1, 9)])
    associations: list[dict[str, Any]] = []
    core_by_arm: list[dict[str, Any]] = []
    for arm_id in arm_ids:
        for ordinal, row in enumerate(rows, start=1):
            unit_id = f"core-{arm_id}-{ordinal:03d}"
            core_by_arm.append({"unit_id": unit_id, "kind": "core_condition", "ordinal": ordinal, "arm_id": arm_id, "source_sequence": "source_capture_then_actual_arm_extraction", "source_records": [dict(event) for event in row["history"]], "query_record": dict(row["query"]), "model_input": _project_model_input(row)})
            associations.append(_private_association(row, unit_id=unit_id, host_id=None, arm_id=arm_id, kind="core_condition", ordinal=ordinal))
    entries: list[dict[str, Any]] = []
    for host_id in host_ids:
        for arm_id in arm_ids:
            query_units: list[dict[str, Any]] = []
            for pair_ordinal, pair_id in enumerate(query_pair_ids, start=1):
                variants = pairs[pair_id]
                if len(variants) != 2:
                    raise SealedPlanError("query_pair_variant_count_mismatch")
                for condition, row in enumerate(variants, start=1):
                    unit_id = f"{host_id}-{arm_id}-query-{pair_ordinal:02d}-c{condition}"
                    query_units.append({"unit_id": unit_id, "kind": "host_query", "ordinal": len(query_units) + 1, "source_sequence": "source_seed_then_new_session_query", "model_input": _project_model_input(row)})
                    associations.append(_private_association(row, unit_id=unit_id, host_id=host_id, arm_id=arm_id, kind="host_query", condition_ordinal=condition))
            if journey_bundle is not None:
                if journey_bundle_root is None:
                    raise SealedPlanError("journey_bundle_root_required")
                journey_units = _build_bundle_journey_units(journey_bundle, host_id=host_id, arm_id=arm_id, associations=associations, bundle_root=journey_bundle_root, journey_operation_ids=journey_operation_ids)
            else:
                journey_units = _build_journey_units(journeys, host_id=host_id, arm_id=arm_id, associations=associations)
            entries.append({"host_id": host_id, "arm_id": arm_id, "unit_count": len(query_units) + len(journey_units), "query_unit_count": len(query_units), "journey_operation_count": len(journey_units), "journeys": 8, "journey_steps_per_journey": 6, "journey_round_budget_per_journey": 8, "units": query_units + journey_units})
    journey_operations_per_host_arm = entries[0]["journey_operation_count"] if entries else 0
    return {"dataset_id": dataset_id, "core_by_arm": core_by_arm, "entries": entries, "associations": associations, "allocation": {"rule": "frozen_public_identity_map_sorted_40_pair_host_subset", "identity_map_sha256": IDENTITY_MAP_SHA256, "public_query_pair_ids": list(frozen_public_identity_map()["query_pair_ids"]), "selection_sha256": hashlib.sha256(("\n".join(query_pair_ids)).encode("utf-8")).hexdigest(), "observed_record_count": len(rows), "observed_pair_groups": len(pairs), "core_conditions_per_arm": len(rows), "core_conditions_all_arms": len(rows) * len(arm_ids), "query_pair_groups": len(query_pair_ids), "query_conditions_per_host_arm": 80, "query_class_totals": {"positive": 16, "negative": 12, "ambiguous": 12}, "journeys": 8, "journey_steps_per_journey": 6, "journey_operations_per_host_arm": journey_operations_per_host_arm, "journey_round_budget_per_journey": 8, "journey_primary_rounds_are_not_padded": True, "host_arm_entries": len(entries), "unallocated_pair_groups": len(pairs) - 40, "metadata_selection": True, "same_selection_all_hosts_and_arms": True, "status": "READY_FOR_FORMAL_FREEZE"}}


def _safe_output_root(path: Path) -> Path:
    root = path.expanduser().resolve()
    lowered = str(root).replace("/", "\\").lower().rstrip("\\")
    if root.name.lower().startswith(("gold", "sealed")) or lowered == "f:\\agents" or lowered.startswith("f:\\agents\\"):
        raise SealedPlanError("unsafe_output_root")
    if not root.name.lower().startswith(("test-", "test_")):
        raise SealedPlanError("output_root_must_be_TEST")
    if root.exists() and any(root.iterdir()):
        raise SealedPlanError("output_root_must_be_new_and_empty")
    root.mkdir(parents=True, exist_ok=False)
    return root


def _write_new(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def _load_manifest_metadata(manifest: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, str]]:
    index = manifest.get("case_index_control_only")
    if not isinstance(index, list) or len(index) != 120:
        raise SealedPlanError("manifest_control_index_missing")
    result: dict[str, dict[str, str]] = {}
    for item in index:
        if not isinstance(item, Mapping):
            raise SealedPlanError("manifest_control_index_row_invalid")
        pair_id = _text(item.get("pair_id"), "manifest_pair_id")
        group_id, core_class = item.get("group_id"), item.get("core_class")
        if group_id not in GROUPS or core_class not in CLASSES or pair_id in result:
            raise SealedPlanError("manifest_control_index_value_invalid")
        result[pair_id] = {"pair_id": pair_id, "group_id": group_id, "core_class": core_class}
    if set(result) != set(_pair_groups(rows)):
        raise SealedPlanError("manifest_control_index_does_not_cover_raw")
    counts = {(group, class_name): 0 for group in GROUPS for class_name in CLASSES}
    for item in result.values():
        counts[(item["group_id"], item["core_class"])] += 1
    if any(counts[(group, class_name)] != (4 if class_name == "positive" else 3) for group in GROUPS for class_name in CLASSES):
        raise SealedPlanError("manifest_control_index_strata_invalid")
    return result


def compile_sealed_run_plan(*, protocol_path: str | Path, manifest_path: str | Path, fixture_manifest_path: str | Path, raw_path: str | Path, output_root: str | Path, allocation_adjudication_path: str | Path, journey_spec_path: str | Path, journey_bundle_path: str | Path | None = None, host_ids: Sequence[str] = ("hermes_a2a", "codex_windows_desktop"), arm_ids: Sequence[str] = ("A", "B", "C", "D")) -> dict[str, Any]:
    """Compile private sealed units without reading gold or contacting hosts."""
    protocol = Path(protocol_path).expanduser().resolve()
    manifest_file = Path(manifest_path).expanduser().resolve()
    fixture_file = Path(fixture_manifest_path).expanduser().resolve()
    raw_file = Path(raw_path).expanduser().resolve()
    allocation_file = Path(allocation_adjudication_path).expanduser().resolve()
    journey_file = Path(journey_spec_path).expanduser().resolve()
    if journey_bundle_path is None:
        raise SealedPlanError("journey_bundle_required")
    journey_bundle_root = Path(journey_bundle_path).expanduser().resolve()
    if raw_file.name.lower() == "gold.jsonl" or "gold" in raw_file.name.lower():
        raise SealedPlanError("gold_input_forbidden")
    if _sha256(protocol) != EXPECTED_PROTOCOL_SHA256 or _sha256(allocation_file) != EXPECTED_ALLOCATION_SHA256 or _sha256(journey_file) != EXPECTED_JOURNEY_SHA256:
        raise SealedPlanError("binding_hash_mismatch")
    manifest, fixture, adjudication = _read_json(manifest_file), _read_json(fixture_file), _read_json(allocation_file)
    if manifest.get("schema_version") != EXPECTED_SCHEMA or manifest.get("dataset_id") != EXPECTED_DATASET:
        raise SealedPlanError("sealed_manifest_identity_mismatch")
    if manifest.get("raw_sha256") != _sha256(raw_file):
        raise SealedPlanError("raw_hash_mismatch")
    if fixture.get("schema_version") != "scope-recall.eval-fixture-manifest.v6" or manifest.get("fixture_manifest_sha256") != _sha256(fixture_file):
        raise SealedPlanError("fixture_manifest_binding_mismatch")
    if int(manifest.get("raw_records", -1)) != 240 or int(manifest.get("paired_variants", -1)) != 240:
        raise SealedPlanError("sealed_record_count_mismatch")
    if adjudication.get("status") != "ALLOCATION_RULE_ACCEPTED_EXECUTION_MAP_PENDING":
        raise SealedPlanError("allocation_adjudication_status_invalid")
    raw_rows = _read_jsonl(raw_file)
    for index, raw_row in enumerate(raw_rows, start=1):
        _reject_control_fields(raw_row, f"raw[{index}]")
    rows = _validate_raw_rows(raw_rows, dataset_id=EXPECTED_DATASET)
    if len(rows) != 240:
        raise SealedPlanError("raw_line_count_mismatch")
    metadata = _load_manifest_metadata(manifest, rows)
    journey_bundle, bundle_summary = _load_journey_bundle(journey_bundle_root)
    journey_operation_ids = None
    if journey_bundle_root.is_file():
        journey_operation_ids = _g0_journey_operation_ids(journey_bundle_root, tuple(sorted(journey_bundle)), host_id=host_ids[0], arm_id=arm_ids[0])
    plan = build_units_from_rows(rows, host_ids=host_ids, arm_ids=arm_ids, dataset_id=EXPECTED_DATASET, metadata=metadata, journey_bundle=journey_bundle, journey_bundle_root=journey_bundle_root, journey_operation_ids=journey_operation_ids)
    root = _safe_output_root(Path(output_root))
    core_paths: list[str] = []
    for arm_id in arm_ids:
        path = root / "core" / f"{arm_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            for unit in (item for item in plan["core_by_arm"] if item["arm_id"] == arm_id):
                handle.write(json.dumps(unit, ensure_ascii=False, sort_keys=True) + "\n")
        core_paths.append(str(path.relative_to(root)))
    unit_paths: list[str] = []
    for entry in plan["entries"]:
        path = root / "units" / str(entry["host_id"]) / f"{entry['arm_id']}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            for unit in entry["units"]:
                handle.write(json.dumps(unit, ensure_ascii=False, sort_keys=True) + "\n")
        unit_paths.append(str(path.relative_to(root)))
    _write_new(root / "private-association.json", {"visibility": "independent_executor_private", "dataset_id": EXPECTED_DATASET, "raw_path": str(raw_file), "manifest_case_metadata": metadata, "associations": plan["associations"]})
    summary = {"schema": "scope-recall.p18-sealed-run-plan.v2", "status": "READY_FOR_FORMAL_FREEZE", "freeze_ready": True, "formal_execution_started": False, "protocol_sha256": EXPECTED_PROTOCOL_SHA256, "allocation_adjudication_sha256": EXPECTED_ALLOCATION_SHA256, "journey_spec_sha256": EXPECTED_JOURNEY_SHA256, "journey_bundle_schema": bundle_summary.get("schema"), "journey_bundle_sha256": bundle_summary.get("bundle_sha256"), "manifest_sha256": _sha256(manifest_file), "fixture_manifest_sha256": _sha256(fixture_file), "raw_sha256": _sha256(raw_file), "dataset_id": EXPECTED_DATASET, "output_root": str(root), "core": {"conditions_per_arm": 240, "arms": len(arm_ids), "planned_condition_executions": 240 * len(arm_ids), "core_paths": core_paths}, "entries": [{key: value for key, value in entry.items() if key != "units"} for entry in plan["entries"]], "unit_paths": unit_paths, "allocation": plan["allocation"], "journeys": {"ids": sorted(journey_bundle), "count": 8, "steps_per_journey": 6, "round_budget_per_journey": 8, "operation_units_per_host_arm": plan["allocation"]["journey_operations_per_host_arm"], "operation_map_source": "tests/eval/p18_run_journey.py:journey_operation_map"}, "privacy": {"gold_read": False, "gold_path_opened": False, "control_fields_in_model_input": False, "source_identity_metadata_in_model_input": False, "claims_or_answers_written": False}}
    _write_new(root / "plan-summary.json", summary)
    return summary


__all__ = ["EXPECTED_PROTOCOL_SHA256", "SealedPlanError", "build_units_from_rows", "compile_sealed_run_plan"]
