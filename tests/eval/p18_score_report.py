"""Aggregate explicit independent-grader P18 annotations into per-attempt reports.

This module does **not** perform semantic grading, infer grades from transport
success, or declare G2/G3/project/formal-evaluation completion. It validates a
frozen JSONL annotation bundle against a public opaque identity map and
aggregates numerators, denominators, Wilson 95% intervals, inventory
completeness, and arm-C threshold calculations separately for each attempt.

A separate formal composer must emit ``scope-recall.p18-formal-evaluation-receipt.v1``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import NormalDist
from typing import Any, Callable, Mapping, Sequence

REPORT_SCHEMA = "scope-recall.p18-score-report.v1"
BUNDLE_SCHEMA = "scope-recall.p18-grader-bundle.v1"
IDENTITY_MAP_SCHEMA = "scope-recall.p18-public-identity-map.v1"
OPAQUE_IDENTITY_SCHEMA = "scope-recall.p18-opaque-allocation-identity.v1"
OPAQUE_IDENTITY_SHA256 = "8dad2588ba160d35b1c5e14ca70d90de37086fbd1ad35649af714d6182fe41c2"

PROTOCOL_SHA256 = "45b70192857b5b8645dd2bf409db55f320f1cf31f297e89910f9f340e76d32d4"
ALLOCATION_ADJUDICATION_SHA256 = "644e26cd9efa9fe51bbf40d35e6cd39ef363bc2489383b163f6f1ade54da54dc"
IDENTITY_MAP_SHA256 = "f838ea8b8ae03183ec3de8c61a0afc3e775df67a968e8d78429b9b4bef998393"
ALLOCATION_SEED = f"p18-allocation-v1:{PROTOCOL_SHA256}"

HERMES_METHOD_ID = "hermes_cli_local_input_v1"
CODEX_METHOD_ID = "codex_windows_appserver_native_hooks_v2"
HERMES_METHOD_ARTIFACT_SHA256 = "119f596efd98476a87401775b692bf0dd7718e72949e0f1813571720c85c57af"
CODEX_METHOD_ARTIFACT_SHA256 = "029f8a2eeecf47ddc7d2a4be62457993a9667d58277f5158a4e745bda17653a4"

ARMS = ("A", "B", "C", "D")
HOSTS = ("hermes_a2a", "codex_windows_desktop")
GROUPS = tuple(f"B{i:02d}" for i in range(1, 13))
JOURNEYS = tuple(f"J{i:02d}" for i in range(1, 9))
CONDITIONS = (1, 2)
CORE_CLASSES = ("positive", "negative", "ambiguous")
EXTRA_POSITIVE_GROUPS = frozenset({"B01", "B04", "B07", "B10"})

CORE_PAIRS = 120
CORE_CONDITIONS_PER_ARM = CORE_PAIRS * len(CONDITIONS)
QUERY_PAIRS_PER_HOST_ARM = 40
QUERY_CONDITIONS_PER_HOST_ARM = QUERY_PAIRS_PER_HOST_ARM * len(CONDITIONS)
JOURNEYS_PER_HOST_ARM = len(JOURNEYS)

L3_OVERALL_THRESHOLD = 0.90
L3_GROUP_THRESHOLD = 0.80
L4_QUERY_PAIR_MINIMUM = 36
L4_QUERY_PAIR_DENOMINATOR = QUERY_PAIRS_PER_HOST_ARM
JOURNEY_MINIMUM = JOURNEYS_PER_HOST_ARM

APPROVED_METHOD_IDS = {
    HERMES_METHOD_ID: HERMES_METHOD_ARTIFACT_SHA256,
    CODEX_METHOD_ID: CODEX_METHOD_ARTIFACT_SHA256,
}
HISTORICAL_HOST_METHOD_BINDING = {
    "hermes_a2a": HERMES_METHOD_ID,
    "codex_windows_desktop": CODEX_METHOD_ID,
}
GROUP_CLASS_COUNTS = {"positive": 4, "negative": 3, "ambiguous": 3}

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_PUBLIC_PAIR_INDEX = re.compile(r"^(?:TEST-pair-|P)(\d+)$")
_WILSON_Z = NormalDist().inv_cdf(0.975)


class AnnotationSchemaError(ValueError):
    """Annotation record or bundle failed strict validation."""


class EvidenceValidationError(ValueError):
    """Evidence path/hash validation failed."""


@dataclass(frozen=True)
class RateMetric:
    numerator: int
    denominator: int
    rate: float | None
    wilson_95: tuple[float, float] | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "numerator": self.numerator,
            "denominator": self.denominator,
            "rate": self.rate,
            "wilson_95": None if self.wilson_95 is None else list(self.wilson_95),
            "unavailable": self.denominator == 0,
        }


def build_public_identity_map() -> dict[str, Any]:
    """Deterministic public synthetic identity map (opaque pair/group/class bindings)."""
    pairs: list[dict[str, Any]] = []
    for group_index, group_id in enumerate(GROUPS):
        for pos_in_group in range(10):
            pair_index = group_index * 10 + pos_in_group + 1
            if pos_in_group < 4:
                core_class = "positive"
            elif pos_in_group < 7:
                core_class = "negative"
            else:
                core_class = "ambiguous"
            pair_id = f"TEST-pair-{pair_index:03d}"
            pairs.append(
                {
                    "pair_id": pair_id,
                    "group_id": group_id,
                    "core_class": core_class,
                    "conditions": list(CONDITIONS),
                }
            )
    metadata = {item["pair_id"]: item for item in pairs}
    selected: list[str] = []
    for group in GROUPS:
        for class_name in CORE_CLASSES:
            candidates = [item for item in metadata.values() if item["group_id"] == group and item["core_class"] == class_name]
            candidates.sort(
                key=lambda item: (
                    hashlib.sha256(f"{ALLOCATION_SEED}\n{item['pair_id']}".encode("utf-8")).hexdigest(),
                    item["pair_id"],
                )
            )
            required = 2 if class_name == "positive" and group in EXTRA_POSITIVE_GROUPS else 1
            selected.extend(item["pair_id"] for item in candidates[:required])
    if len(selected) != QUERY_PAIRS_PER_HOST_ARM or len(set(selected)) != QUERY_PAIRS_PER_HOST_ARM:
        raise ValueError("identity_map_query_selection_invalid")
    return {
        "schema": IDENTITY_MAP_SCHEMA,
        "protocol_sha256": PROTOCOL_SHA256,
        "allocation_adjudication_sha256": ALLOCATION_ADJUDICATION_SHA256,
        "allocation_seed": ALLOCATION_SEED,
        "pairs": pairs,
        "query_pair_ids": sorted(selected),
        "journeys": list(JOURNEYS),
    }


def identity_map_bytes(identity_map: Mapping[str, Any] | None = None) -> bytes:
    payload = identity_map if identity_map is not None else build_public_identity_map()
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def frozen_public_identity_map() -> dict[str, Any]:
    """Return the frozen public identity map after verifying its canonical digest."""
    identity_map = build_public_identity_map()
    if hashlib.sha256(identity_map_bytes(identity_map)).hexdigest() != IDENTITY_MAP_SHA256:
        raise ValueError("identity_map_content_mismatch")
    return identity_map


def public_pair_index(pair_id: str) -> int | None:
    """Map a dataset or public pair id onto the frozen TEST-pair index."""
    if type(pair_id) is not str:
        return None
    match = _PUBLIC_PAIR_INDEX.fullmatch(pair_id)
    if match is None:
        return None
    index = int(match.group(1))
    if not 1 <= index <= CORE_PAIRS:
        return None
    return index


def authoritative_query_pair_indices() -> tuple[int, ...]:
    """Sorted identity-map query subset, as 1-based public pair indices."""
    return tuple(int(pair_id.rsplit("-", 1)[1]) for pair_id in frozen_public_identity_map()["query_pair_ids"])


def _canonicalize_opaque_identity(payload: Mapping[str, Any], raw_sha256: str) -> dict[str, Any]:
    """Convert the permitted opaque sidecar to the aggregator's identity interface.

    The sidecar contains identities and operation grouping only; it contains no
    prompts, answers, or sealed grading material.  In particular, journey
    operations remain grouped by (host, arm, journey) instead of becoming
    independent journeys.
    """
    if payload.get("schema") != OPAQUE_IDENTITY_SCHEMA or payload.get("raw_or_gold_text_included") is not False:
        raise AnnotationSchemaError("identity_sidecar_schema_invalid")
    if payload.get("gold_read") is not False:
        raise AnnotationSchemaError("identity_sidecar_gold_read")
    sections = {"core": 960, "host_queries": 640, "journey_operations": 344}
    for section, expected_count in sections.items():
        value = payload.get(section)
        if type(value) is not dict or value.get("unit_count") != expected_count or type(value.get("units")) is not list:
            raise AnnotationSchemaError(f"identity_sidecar_{section}_count_invalid")
        if len(value["units"]) != expected_count:
            raise AnnotationSchemaError(f"identity_sidecar_{section}_units_invalid")
    core_units = payload["core"]["units"]
    query_units = payload["host_queries"]["units"]
    operation_units = payload["journey_operations"]["units"]
    pairs: dict[str, dict[str, Any]] = {}
    for item in core_units:
        required = {"arm_id", "condition", "core_class", "group_id", "host_id", "kind", "pair_id", "unit_id"}
        if type(item) is not dict or not required <= set(item) or item["kind"] != "core_condition":
            raise AnnotationSchemaError("identity_sidecar_core_unit_invalid")
        if item["host_id"] is not None or item["condition"] not in CONDITIONS:
            raise AnnotationSchemaError("identity_sidecar_core_identity_invalid")
        prior = pairs.setdefault(item["pair_id"], {"pair_id": item["pair_id"], "group_id": item["group_id"], "core_class": item["core_class"], "conditions": list(CONDITIONS)})
        if prior["group_id"] != item["group_id"] or prior["core_class"] != item["core_class"]:
            raise AnnotationSchemaError("identity_sidecar_pair_identity_conflict")
    query_pairs = []
    for item in query_units:
        required = {"arm_id", "condition", "core_class", "group_id", "host_id", "kind", "pair_id", "unit_id"}
        if type(item) is not dict or not required <= set(item) or item["kind"] != "host_query":
            raise AnnotationSchemaError("identity_sidecar_query_unit_invalid")
        if item["condition"] not in CONDITIONS or item["host_id"] not in HOSTS:
            raise AnnotationSchemaError("identity_sidecar_query_identity_invalid")
        pair = pairs.setdefault(item["pair_id"], {"pair_id": item["pair_id"], "group_id": item["group_id"], "core_class": item["core_class"], "conditions": list(CONDITIONS)})
        if pair["group_id"] != item["group_id"] or pair["core_class"] != item["core_class"]:
            raise AnnotationSchemaError("identity_sidecar_query_pair_conflict")
        if item["pair_id"] not in query_pairs:
            query_pairs.append(item["pair_id"])
    journey_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in operation_units:
        required = {"arm_id", "host_id", "journey_id", "kind", "operation_id", "operation_kind", "unit_id"}
        if type(item) is not dict or not required <= set(item) or item["kind"] != "journey_operation":
            raise AnnotationSchemaError("identity_sidecar_operation_unit_invalid")
        if item["arm_id"] not in ARMS or item["host_id"] not in HOSTS or item["journey_id"] not in JOURNEYS:
            raise AnnotationSchemaError("identity_sidecar_operation_identity_invalid")
        journey_groups[(item["host_id"], item["arm_id"], item["journey_id"])].append(dict(item))
    if len(pairs) != CORE_PAIRS or len(query_pairs) != QUERY_PAIRS_PER_HOST_ARM or len(journey_groups) != len(ARMS) * len(HOSTS) * len(JOURNEYS):
        raise AnnotationSchemaError("identity_sidecar_group_counts_invalid")
    expected_units: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in core_units:
        expected_units[("core", item["arm_id"], None, item["pair_id"], item["condition"])] = dict(item)
    for item in query_units:
        expected_units[("query", item["arm_id"], item["host_id"], item["pair_id"], item["condition"])] = dict(item)
    journey_unit_groups = {key: tuple(sorted(items, key=lambda item: item["operation_id"])) for key, items in journey_groups.items()}
    return {
        "schema": OPAQUE_IDENTITY_SCHEMA,
        "identity_map_sha256": raw_sha256,
        "pairs": sorted(pairs.values(), key=lambda item: item["pair_id"]),
        "query_pair_ids": query_pairs,
        "journeys": list(JOURNEYS),
        "expected_units": expected_units,
        "journey_operation_groups": journey_unit_groups,
        "sidecar_counts": sections,
        "sidecar_dataset_id": payload.get("dataset_id"),
    }


def load_identity_map(
    metadata: Mapping[str, Any],
    identity_map_path: Path | None = None,
    *,
    expected_sha256: str | None = None,
    sidecar_path: Path | None = None,
) -> dict[str, Any]:
    if sidecar_path is not None:
        if identity_map_path is not None:
            raise AnnotationSchemaError("identity_sidecar_path_ambiguous")
        identity_map_path = sidecar_path
    if identity_map_path is not None:
        try:
            raw = identity_map_path.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AnnotationSchemaError("identity_sidecar_read_failed") from exc
        actual = hashlib.sha256(raw).hexdigest()
        trusted = expected_sha256 or metadata.get("identity_map_sha256")
        declared = metadata.get("identity_map_sha256")
        if expected_sha256 is not None and declared != expected_sha256:
            raise AnnotationSchemaError("identity_sidecar_metadata_sha256_mismatch")
        if type(trusted) is not str or not _HEX64.fullmatch(trusted) or actual != trusted:
            raise AnnotationSchemaError("identity_sidecar_sha256_mismatch")
        if actual != OPAQUE_IDENTITY_SHA256:
            raise AnnotationSchemaError("identity_sidecar_untrusted_sha256")
        return _canonicalize_opaque_identity(payload, actual)
    declared = metadata.get("identity_map_sha256")
    if type(declared) is not str or not _HEX64.fullmatch(declared):
        raise AnnotationSchemaError("identity_map_sha256_invalid")
    if declared != IDENTITY_MAP_SHA256:
        raise AnnotationSchemaError("identity_map_sha256_mismatch")
    identity_map = build_public_identity_map()
    if hashlib.sha256(identity_map_bytes(identity_map)).hexdigest() != IDENTITY_MAP_SHA256:
        raise AnnotationSchemaError("identity_map_content_mismatch")
    return identity_map


def wilson_interval(successes: int, denominator: int, *, z: float = _WILSON_Z) -> tuple[float, float] | None:
    if denominator <= 0:
        return None
    if successes < 0 or successes > denominator:
        raise ValueError("wilson_invalid_counts")
    p = successes / denominator
    z2 = z * z
    denom = 1.0 + z2 / denominator
    center = (p + z2 / (2.0 * denominator)) / denom
    margin = z * math.sqrt((p * (1.0 - p) / denominator) + (z2 / (4.0 * denominator * denominator))) / denom
    low = max(0.0, center - margin)
    high = min(1.0, center + margin)
    return (low, high)


def rate_metric(successes: int, denominator: int) -> RateMetric:
    if denominator == 0:
        return RateMetric(0, 0, None, None)
    return RateMetric(successes, denominator, successes / denominator, wilson_interval(successes, denominator))


def _strict_bool(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise AnnotationSchemaError(f"{field}_must_be_strict_boolean")
    return value


def _optional_strict_bool(value: Any, field: str) -> bool | None:
    if value is None:
        return None
    return _strict_bool(value, field)


def _require_text(value: Any, field: str) -> str:
    if type(value) is not str or not value.strip():
        raise AnnotationSchemaError(f"{field}_invalid")
    return value


def _require_int(value: Any, field: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    if type(value) is not int or isinstance(value, bool):
        raise AnnotationSchemaError(f"{field}_invalid")
    if minimum is not None and value < minimum:
        raise AnnotationSchemaError(f"{field}_below_minimum")
    if maximum is not None and value > maximum:
        raise AnnotationSchemaError(f"{field}_above_maximum")
    return value


def _validate_evidence_refs(
    refs: Any,
    evidence_root: Path | None,
    *,
    validate_files: bool,
    record: Mapping[str, Any] | None = None,
) -> None:
    if type(refs) is not list or not refs:
        raise AnnotationSchemaError("evidence_invalid")
    root = evidence_root.resolve() if evidence_root is not None else None
    unit_id = record.get("unit_id") if record is not None else None
    for item in refs:
        if type(item) is not dict or set(item) != {"path", "sha256"}:
            raise AnnotationSchemaError("evidence_reference_invalid")
        rel = item["path"]
        digest = item["sha256"]
        if type(rel) is not str or not rel.strip() or Path(rel).is_absolute():
            raise AnnotationSchemaError("evidence_path_invalid")
        if ".." in Path(rel).parts:
            raise AnnotationSchemaError("evidence_path_traversal")
        if type(digest) is not str or not _HEX64.fullmatch(digest):
            raise AnnotationSchemaError("evidence_digest_invalid")
        # The filename is only a locator.  Unit/operation association is
        # verified from the runtime JSON below, so a renamed evidence file
        # cannot pass merely by containing a unit substring.
        if not validate_files or root is None:
            continue
        target = (root / rel).resolve()
        if not target.is_relative_to(root):
            raise EvidenceValidationError("evidence_outside_root")
        if not target.is_file():
            raise EvidenceValidationError("evidence_missing")
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual != digest:
            raise EvidenceValidationError("evidence_hash_mismatch")


def _verify_evidence_binding(record: Mapping[str, Any], evidence_root: Path) -> None:
    """Check the small runtime association contract; filenames are insufficient."""
    root = evidence_root.resolve()
    operation_ids = set(record.get("operation_ids", []))
    for item in record["evidence"]:
        target = (root / item["path"]).resolve()
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise EvidenceValidationError("evidence_association_read_failed") from exc
        if type(payload) is not dict:
            raise EvidenceValidationError("evidence_association_not_object")
        for field in ("run_id", "attempt", "operation_id", "unit_id", "host_id", "arm_id"):
            if field not in payload:
                raise EvidenceValidationError(f"evidence_association_missing:{field}")
        expected = {
            "run_id": record["run_id"], "attempt": record["attempt"],
            "unit_id": record["unit_id"], "host_id": record.get("host_id"),
            "arm_id": record["arm_id"],
        }
        if any(payload[field] != value for field, value in expected.items()):
            raise EvidenceValidationError(f"evidence_association_mismatch:{record['unit_id']}")
        if operation_ids and payload["operation_id"] not in operation_ids:
            raise EvidenceValidationError(f"evidence_operation_mismatch:{record['unit_id']}")
        if record["status"] == "unsupported" and payload.get("capability_supported") is not False:
            raise EvidenceValidationError(f"unsupported_capability_association_invalid:{record['unit_id']}")


def _validate_graded_layers(record: Mapping[str, Any], layers: Sequence[str]) -> dict[str, bool | None]:
    result: dict[str, bool | None] = {}
    for layer in layers:
        result[layer] = _optional_strict_bool(record.get(layer), layer)
    return result


def _attach_core_query_identity(normalized: dict[str, Any], source: Mapping[str, Any], *, line_no: int) -> None:
    normalized["pair_id"] = _require_text(source.get("pair_id"), "pair_id")
    condition_id = _require_int(source.get("condition_id"), "condition_id")
    if condition_id not in CONDITIONS:
        raise AnnotationSchemaError(f"line_{line_no}:condition_id_invalid")
    normalized["condition_id"] = condition_id
    group_id = source.get("group_id")
    if normalized["kind"] == "core":
        group_id = _require_text(group_id, "group_id")
        if group_id not in GROUPS:
            raise AnnotationSchemaError(f"line_{line_no}:group_id_invalid")
    elif group_id is not None:
        if type(group_id) is not str or group_id not in GROUPS:
            raise AnnotationSchemaError(f"line_{line_no}:group_id_invalid")
    normalized["group_id"] = group_id


def validate_record(record: Mapping[str, Any], *, line_no: int, expected_run_id: str) -> dict[str, Any]:
    if type(record) is not dict:
        raise AnnotationSchemaError(f"line_{line_no}:record_not_object")
    run_id = _require_text(record.get("run_id"), "run_id")
    if run_id != expected_run_id:
        raise AnnotationSchemaError(f"line_{line_no}:run_id_mismatch")
    attempt = _require_int(record.get("attempt"), "attempt", minimum=1)
    kind = _require_text(record.get("kind"), "kind")
    if kind not in {"core", "query", "journey"}:
        raise AnnotationSchemaError(f"line_{line_no}:kind_invalid")
    arm_id = _require_text(record.get("arm_id"), "arm_id")
    if arm_id not in ARMS:
        raise AnnotationSchemaError(f"line_{line_no}:arm_id_invalid")
    host_id = record.get("host_id")
    if kind == "core":
        if host_id is not None:
            raise AnnotationSchemaError(f"line_{line_no}:core_host_id_must_be_null")
    else:
        host_id = _require_text(host_id, "host_id")
        if host_id not in HOSTS:
            raise AnnotationSchemaError(f"line_{line_no}:host_id_invalid")
    unit_id = _require_text(record.get("unit_id"), "unit_id")
    status = _require_text(record.get("status"), "status")
    if status not in {"graded", "unsupported", "not_run", "failed"}:
        raise AnnotationSchemaError(f"line_{line_no}:status_invalid")
    reason = record.get("reason")
    if status == "graded":
        if reason is not None and type(reason) is not str:
            raise AnnotationSchemaError(f"line_{line_no}:reason_invalid")
    else:
        if type(reason) is not str or not reason.strip():
            raise AnnotationSchemaError(f"line_{line_no}:reason_required_for_non_graded")

    normalized: dict[str, Any] = {
        "run_id": run_id,
        "attempt": attempt,
        "kind": kind,
        "arm_id": arm_id,
        "host_id": host_id,
        "unit_id": unit_id,
        "status": status,
        "reason": reason,
        "evidence": record["evidence"],
        "line_no": line_no,
    }
    if "capability_proof" in record:
        if type(record["capability_proof"]) is not dict:
            raise AnnotationSchemaError(f"line_{line_no}:capability_proof_invalid")
        normalized["capability_proof"] = dict(record["capability_proof"])
    _validate_evidence_refs(record.get("evidence"), None, validate_files=False, record=normalized)

    if kind in {"core", "query"}:
        _attach_core_query_identity(normalized, record, line_no=line_no)
    elif status != "graded":
        journey_id = _require_text(record.get("journey_id"), "journey_id")
        if journey_id not in JOURNEYS:
            raise AnnotationSchemaError(f"line_{line_no}:journey_id_invalid")
        normalized["journey_id"] = journey_id

    if kind == "journey":
        operation_unit_ids = record.get("operation_unit_ids")
        if operation_unit_ids is not None:
            if type(operation_unit_ids) is not list or not operation_unit_ids or any(type(item) is not str or not item.strip() for item in operation_unit_ids):
                raise AnnotationSchemaError(f"line_{line_no}:operation_unit_ids_invalid")
            if len(set(operation_unit_ids)) != len(operation_unit_ids):
                raise AnnotationSchemaError(f"line_{line_no}:operation_unit_ids_duplicate")
            normalized["operation_unit_ids"] = list(operation_unit_ids)
        operation_ids = record.get("operation_ids")
        if operation_ids is not None:
            if type(operation_ids) is not list or not operation_ids or any(type(item) is not str or not item.strip() for item in operation_ids):
                raise AnnotationSchemaError(f"line_{line_no}:operation_ids_invalid")
            if len(set(operation_ids)) != len(operation_ids):
                raise AnnotationSchemaError(f"line_{line_no}:operation_ids_duplicate")
            normalized["operation_ids"] = list(operation_ids)

    if status != "graded":
        return normalized

    if kind in {"core", "query"}:
        normalized.update(_validate_graded_layers(record, ("l1_pass", "l2_pass", "l3_pass", "l4_pass")))
        normalized["l3_answerable"] = _strict_bool(record.get("l3_answerable"), "l3_answerable")
        normalized["required_slots"] = _require_int(record.get("required_slots"), "required_slots", minimum=0)
        covered = _require_int(record.get("covered_slots"), "covered_slots", minimum=0)
        if covered > normalized["required_slots"]:
            raise AnnotationSchemaError(f"line_{line_no}:covered_slots_exceed_required")
        normalized["covered_slots"] = covered
        violations = record.get("safety_violations")
        if type(violations) is not list or any(type(v) is not str for v in violations):
            raise AnnotationSchemaError(f"line_{line_no}:safety_violations_invalid")
        normalized["safety_violations"] = list(violations)
        return normalized

    normalized["journey_id"] = _require_text(record.get("journey_id"), "journey_id")
    if normalized["journey_id"] not in JOURNEYS:
        raise AnnotationSchemaError(f"line_{line_no}:journey_id_invalid")
    normalized.update(_validate_graded_layers(record, ("l1_pass", "l2_pass", "l3_pass", "l4_pass")))
    violations = record.get("safety_violations")
    if type(violations) is not list or any(type(v) is not str for v in violations):
        raise AnnotationSchemaError(f"line_{line_no}:safety_violations_invalid")
    normalized["safety_violations"] = list(violations)
    normalized["all_required_steps_pass"] = _strict_bool(record.get("all_required_steps_pass"), "all_required_steps_pass")
    normalized["primary_rounds"] = _require_int(record.get("primary_rounds"), "primary_rounds", minimum=0, maximum=8)
    return normalized


def load_bundle_metadata(
    path: Path,
    identity_map_path: Path | None = None,
    identity_map_sha256: str | None = None,
) -> dict[str, Any]:
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AnnotationSchemaError("metadata_read_failed") from exc
    if type(metadata) is not dict or metadata.get("schema") != BUNDLE_SCHEMA:
        raise AnnotationSchemaError("metadata_schema_invalid")
    grading = metadata.get("grading_module")
    if type(grading) is not dict:
        raise AnnotationSchemaError("grading_module_invalid")
    grading_id = grading.get("id")
    grading_version = grading.get("version")
    if type(grading_id) is not str or not grading_id or type(grading_version) is not str or not grading_version:
        raise AnnotationSchemaError("grading_module_identity_invalid")
    input_sha = metadata.get("annotation_input_sha256")
    if type(input_sha) is not str or not _HEX64.fullmatch(input_sha):
        raise AnnotationSchemaError("annotation_input_sha256_invalid")
    _require_text(metadata.get("run_id"), "run_id")
    protocol_sha = metadata.get("protocol_sha256")
    if type(protocol_sha) is not str or not _HEX64.fullmatch(protocol_sha):
        raise AnnotationSchemaError("protocol_sha256_invalid")
    if protocol_sha != PROTOCOL_SHA256:
        raise AnnotationSchemaError("protocol_sha256_mismatch")
    allocation_sha = metadata.get("allocation_adjudication_sha256")
    if type(allocation_sha) is not str or not _HEX64.fullmatch(allocation_sha):
        raise AnnotationSchemaError("allocation_adjudication_sha256_invalid")
    if allocation_sha != ALLOCATION_ADJUDICATION_SHA256:
        raise AnnotationSchemaError("allocation_adjudication_sha256_mismatch")
    load_identity_map(metadata, identity_map_path, expected_sha256=identity_map_sha256)
    method_ids = metadata.get("method_ids")
    if type(method_ids) is not dict:
        raise AnnotationSchemaError("method_ids_invalid")
    for host, expected_method in HISTORICAL_HOST_METHOD_BINDING.items():
        if host not in method_ids:
            raise AnnotationSchemaError("method_ids_missing_host")
        method_value = method_ids[host]
        if type(method_value) is not str or method_value != expected_method:
            raise AnnotationSchemaError("method_ids_unapproved_binding")
        if method_value not in APPROVED_METHOD_IDS:
            raise AnnotationSchemaError("method_ids_unknown_method")
    method_artifacts = metadata.get("method_artifact_sha256")
    if type(method_artifacts) is not dict:
        raise AnnotationSchemaError("method_artifact_sha256_invalid")
    for method_id, expected_digest in APPROVED_METHOD_IDS.items():
        actual = method_artifacts.get(method_id)
        if type(actual) is not str or not _HEX64.fullmatch(actual) or actual != expected_digest:
            raise AnnotationSchemaError("method_artifact_sha256_mismatch")
    return metadata


def load_annotations(path: Path, metadata: Mapping[str, Any], *, raw_bytes: bytes | None = None) -> list[dict[str, Any]]:
    run_id = metadata["run_id"]
    raw = raw_bytes if raw_bytes is not None else path.read_bytes()
    actual_sha = hashlib.sha256(raw).hexdigest()
    if actual_sha != metadata["annotation_input_sha256"]:
        raise AnnotationSchemaError("annotation_input_sha256_mismatch")
    text = raw.decode("utf-8")
    records: list[dict[str, Any]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AnnotationSchemaError(f"line_{line_no}:json_invalid") from exc
        records.append(validate_record(parsed, line_no=line_no, expected_run_id=run_id))
    return records


def _record_key(record: Mapping[str, Any]) -> tuple[Any, ...]:
    return (record["attempt"], record["kind"], record["arm_id"], record["host_id"], record["unit_id"])


def _pair_key(record: Mapping[str, Any]) -> tuple[str, int]:
    return (record["pair_id"], record["condition_id"])


def _identity_pair_lookup(identity_map: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["pair_id"]: item for item in identity_map["pairs"]}


def _expected_unit_keys(identity_map: Mapping[str, Any]) -> set[tuple[Any, ...]]:
    query_pairs = identity_map["query_pair_ids"]
    expected: set[tuple[Any, ...]] = set()
    for arm in ARMS:
        for pair in identity_map["pairs"]:
            for condition_id in CONDITIONS:
                expected.add(("core", arm, None, pair["pair_id"], condition_id))
        for host in HOSTS:
            for pair_id in query_pairs:
                for condition_id in CONDITIONS:
                    expected.add(("query", arm, host, pair_id, condition_id))
            for journey_id in identity_map["journeys"]:
                expected.add(("journey", arm, host, None, journey_id))
    return expected


def _expected_unit(identity_map: Mapping[str, Any], key: tuple[Any, ...]) -> Mapping[str, Any] | None:
    expected_units = identity_map.get("expected_units")
    if isinstance(expected_units, Mapping):
        return expected_units.get(key)
    return None


def _inventory_errors_for_attempt(
    records: Sequence[Mapping[str, Any]],
    attempt: int,
    identity_map: Mapping[str, Any],
) -> list[str]:
    errors: list[str] = []
    subset = [r for r in records if r["attempt"] == attempt]
    seen: set[tuple[Any, ...]] = set()
    seen_logical: set[tuple[Any, ...]] = set()
    for record in subset:
        key = _record_key(record)
        logical = (record["kind"], record["arm_id"], record["host_id"], record.get("pair_id"), record.get("condition_id"), record.get("journey_id"))
        if key in seen or logical in seen_logical:
            errors.append(f"duplicate_unit:{record['unit_id']}:attempt:{attempt}")
        seen.add(key)
        seen_logical.add(logical)

    pair_lookup = _identity_pair_lookup(identity_map)
    query_pairs = set(identity_map["query_pair_ids"])
    expected_keys = _expected_unit_keys(identity_map)
    present_keys: set[tuple[Any, ...]] = set()

    for record in subset:
        kind = record["kind"]
        arm = record["arm_id"]
        host = record["host_id"]
        status = record["status"]
        if kind in {"core", "query"}:
            pair_id = record["pair_id"]
            condition_id = record["condition_id"]
            if pair_id not in pair_lookup:
                errors.append(f"unknown_pair_id:{pair_id}")
                continue
            frozen = pair_lookup[pair_id]
            group_id = record.get("group_id")
            if group_id is not None and group_id != frozen["group_id"]:
                errors.append(f"group_id_mismatch:{arm}:{pair_id}:{group_id}!={frozen['group_id']}")
            logical_key = (kind, arm, host, pair_id, condition_id)
            present_keys.add(logical_key)
            expected = _expected_unit(identity_map, logical_key)
            if expected is not None and record["unit_id"] != expected["unit_id"]:
                errors.append(f"unit_id_mismatch:{record['unit_id']}!={expected['unit_id']}")
            if kind == "query":
                if pair_id not in query_pairs:
                    errors.append(f"query_pair_not_in_frozen_subset:{host}:{arm}:{pair_id}")
        elif kind == "journey":
            journey_id = record.get("journey_id")
            if journey_id not in JOURNEYS:
                errors.append(f"journey_id_invalid:{record['unit_id']}")
            else:
                logical_key = ("journey", arm, host, None, journey_id)
                present_keys.add(logical_key)
                expected_group = identity_map.get("journey_operation_groups", {}).get((host, arm, journey_id))
                if expected_group is not None:
                    expected_unit_ids = [item["unit_id"] for item in expected_group]
                    expected_operation_ids = [item["operation_id"] for item in expected_group]
                    if record.get("operation_unit_ids") != expected_unit_ids:
                        errors.append(f"journey_operation_units_mismatch:{record['unit_id']}:{journey_id}")
                    if record.get("operation_ids") != expected_operation_ids:
                        errors.append(f"journey_operation_ids_mismatch:{record['unit_id']}:{journey_id}")

        if status == "not_run":
            errors.append(f"not_run_incomplete:{record['unit_id']}")
        if arm in {"A", "D"} and status == "not_run":
            errors.append(f"supported_baseline_not_run:{arm}:{record['unit_id']}")
        if status == "unsupported":
            proof = record.get("capability_proof")
            valid_proof = (
                isinstance(proof, Mapping)
                and proof.get("schema") == "scope-recall.p18-capability-proof.v1"
                and proof.get("run_id") == record["run_id"]
                and proof.get("attempt") == record["attempt"]
                and proof.get("arm_id") == record["arm_id"]
                and proof.get("host_id") == record.get("host_id")
                and proof.get("unit_id") == record["unit_id"]
                and proof.get("capability_supported") is False
            )
            if not valid_proof:
                errors.append(f"unsupported_capability_proof_unverified:{record['unit_id']}")
            if arm == "C":
                errors.append(f"c_unsupported_not_accepted:{record['unit_id']}")
        if status == "graded":
            if kind in {"core", "query"}:
                errors.extend(_graded_consistency_errors(record, pair_lookup))
            else:
                for layer in ("l1_pass", "l2_pass", "l3_pass", "l4_pass"):
                    if record.get(layer) is None:
                        errors.append(f"unknown_layer:{record['unit_id']}:{layer}")

    for expected in expected_keys:
        if expected not in present_keys:
            errors.append(f"missing_expected_unit:{expected}")

    for arm in ARMS:
        core_graded = [
            r
            for r in subset
            if r["kind"] == "core" and r["arm_id"] == arm
        ]
        group_class_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for record in core_graded:
            pair_id = record["pair_id"]
            frozen = pair_lookup.get(pair_id)
            if frozen is None:
                errors.append(f"unknown_pair_id:{pair_id}")
                continue
            group_class_counts[frozen["group_id"]][frozen["core_class"]] += 1
        for group in GROUPS:
            counts = group_class_counts.get(group, {})
            for class_name, required in GROUP_CLASS_COUNTS.items():
                actual = counts.get(class_name, 0)
                if actual != required * len(CONDITIONS):
                    errors.append(f"group_class_count:{arm}:{group}:{class_name}:{actual}!={required * len(CONDITIONS)}")

    query_pair_sets: dict[tuple[str, str], set[str]] = defaultdict(set)
    for record in subset:
        if record["kind"] != "query":
            continue
        query_pair_sets[(record["host_id"], record["arm_id"])].add(record["pair_id"])
    if query_pair_sets:
        reference = next(iter(query_pair_sets.values()))
        for key, pair_set in query_pair_sets.items():
            if pair_set != reference:
                errors.append(f"query_pair_subset_mismatch:{key[0]}:{key[1]}")
            if pair_set != query_pairs:
                errors.append(f"query_pair_subset_not_frozen:{key[0]}:{key[1]}")

    c_core = [r for r in subset if r["arm_id"] == "C" and r["kind"] == "core"]
    if len(c_core) != CORE_CONDITIONS_PER_ARM:
        errors.append("missing_c_core_records")
    for host in HOSTS:
        c_query = [r for r in subset if r["arm_id"] == "C" and r["kind"] == "query" and r["host_id"] == host]
        if len(c_query) != QUERY_CONDITIONS_PER_HOST_ARM:
            errors.append(f"missing_c_query_records:{host}")
        c_journey = [r for r in subset if r["arm_id"] == "C" and r["kind"] == "journey" and r["host_id"] == host]
        if len(c_journey) != JOURNEYS_PER_HOST_ARM:
            errors.append(f"missing_c_journey_records:{host}")

    return errors


def _graded_consistency_errors(record: Mapping[str, Any], pair_lookup: Mapping[str, Mapping[str, Any]]) -> list[str]:
    errors: list[str] = []
    pair_id = record["pair_id"]
    frozen = pair_lookup.get(pair_id)
    if frozen is None:
        return [f"unknown_pair_id:{pair_id}"]
    core_class = frozen["core_class"]
    answerable = record.get("l3_answerable")
    required_slots = int(record.get("required_slots", 0))
    covered_slots = int(record.get("covered_slots", 0))
    l3_pass = record.get("l3_pass")
    if answerable is True and required_slots > 0 and covered_slots == 0 and l3_pass is True:
        errors.append(f"contradictory_slot_success:{record['unit_id']}")
    required_layers = ("l1_pass", "l2_pass", "l3_pass", "l4_pass") if record.get("kind") == "query" else (("l1_pass", "l2_pass", "l3_pass", "l4_pass") if record.get("kind") == "journey" else ("l1_pass", "l2_pass", "l3_pass"))
    for layer in required_layers:
        if record.get(layer) is None:
            errors.append(f"unknown_layer:{record['unit_id']}:{layer}")
    if core_class in {"negative", "ambiguous"}:
        if answerable is True and l3_pass is not True:
            errors.append(f"class_assertion_failed:{record['unit_id']}:{core_class}")
    elif core_class == "positive" and answerable is False:
        errors.append(f"incorrect_abstention_on_positive:{record['unit_id']}")
    return errors


def _condition_passes_layer(record: Mapping[str, Any], layer: str) -> bool:
    return record.get(layer) is True


def _layer_known_pass(record: Mapping[str, Any], layer: str) -> bool:
    value = record.get(layer)
    if value is None:
        return False
    return value is True


def _pair_passes_layers(conditions: Sequence[Mapping[str, Any]], layers: Sequence[str]) -> bool:
    if len(conditions) != len(CONDITIONS):
        return False
    graded = [c for c in conditions if c["status"] == "graded"]
    if len(graded) != len(CONDITIONS):
        return False
    return all(all(_layer_known_pass(item, layer) for layer in layers) for item in graded)


def _aggregate_condition_and_pair_metrics(
    conditions: Sequence[Mapping[str, Any]],
    *,
    layer: str,
    pair_filter: Callable[[Sequence[Mapping[str, Any]]], bool] | None = None,
    pair_layers: Sequence[str] | None = None,
) -> dict[str, Any]:
    # Every expected condition remains in the denominator.  ``unsupported``,
    # ``failed`` and unknown grader values are visible failures/incompleteness;
    # they are never silently removed by an answerability filter.
    cond_pass = sum(1 for c in conditions if _condition_passes_layer(c, layer))
    cond_metric = rate_metric(cond_pass, len(conditions))
    layers = pair_layers or (layer,)

    by_pair: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in conditions:
        pair_id = item.get("pair_id")
        if pair_id is None:
            continue
        by_pair[pair_id].append(item)
    pair_den = 0
    pair_pass = 0
    for pair_conds in by_pair.values():
        if len(pair_conds) != len(CONDITIONS):
            continue
        if pair_filter is not None and not pair_filter(pair_conds):
            continue
        pair_den += 1
        if _pair_passes_layers(pair_conds, layers):
            pair_pass += 1
    pair_metric = rate_metric(pair_pass, pair_den)
    return {
        "condition_level": cond_metric.as_dict(),
        "independent_pair_level": pair_metric.as_dict(),
    }


def _answerable_pair_filter(pair_conds: Sequence[Mapping[str, Any]]) -> bool:
    graded = [c for c in pair_conds if c["status"] == "graded"]
    if len(graded) != len(CONDITIONS):
        return False
    return all(c.get("l3_answerable") is True for c in graded)


def _slot_metrics(conditions: Sequence[Mapping[str, Any]], *, by_group: Mapping[str, Sequence[Mapping[str, Any]]] | None = None) -> dict[str, Any]:
    answerable = [c for c in conditions if c["status"] == "graded" and c.get("l3_answerable") is True]
    required = sum(c["required_slots"] for c in answerable)
    covered = sum(c["covered_slots"] for c in answerable)
    full_slot_pass = sum(
        1 for c in answerable if c["required_slots"] > 0 and c["covered_slots"] == c["required_slots"]
    )
    slot_den = len([c for c in answerable if c["required_slots"] > 0])
    overall_rate = None if required == 0 else covered / required
    group_rates: dict[str, Any] = {}
    if by_group is not None:
        for group, group_records in by_group.items():
            group_answerable = [c for c in group_records if c["status"] == "graded" and c.get("l3_answerable") is True]
            group_required = sum(c["required_slots"] for c in group_answerable)
            group_covered = sum(c["covered_slots"] for c in group_answerable)
            group_rates[group] = {
                "required_slots_total": group_required,
                "covered_slots_total": group_covered,
                "slot_coverage_rate": None if group_required == 0 else group_covered / group_required,
            }
    return {
        "answerable_conditions": len(answerable),
        "required_slots_total": required,
        "covered_slots_total": covered,
        "slot_coverage_rate": overall_rate,
        "all_required_slots_condition_passes": full_slot_pass,
        "all_required_slots_condition_denominator": slot_den,
        "by_group": group_rates,
        "overall_passes_threshold": overall_rate is not None and overall_rate >= L3_OVERALL_THRESHOLD,
        "group_threshold": L3_GROUP_THRESHOLD,
    }


def _status_histogram(records: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    hist = {"graded": 0, "unsupported": 0, "not_run": 0, "failed": 0}
    for record in records:
        hist[record["status"]] += 1
    return hist


def _l1_l2_gaps(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    for record in records:
        if record["status"] != "graded":
            continue
        for layer in ("l1_pass", "l2_pass"):
            value = record.get(layer)
            if value is False or value is None:
                gaps.append(
                    {
                        "unit_id": record["unit_id"],
                        "kind": record["kind"],
                        "arm_id": record["arm_id"],
                        "host_id": record["host_id"],
                        "layer": layer,
                        "value": value,
                    }
                )
    return gaps


def _safety_blocks(records: Sequence[Mapping[str, Any]]) -> tuple[bool, list[str]]:
    violations: list[str] = []
    for record in records:
        if record["status"] != "graded":
            continue
        for item in record.get("safety_violations", []):
            violations.append(f"{record['unit_id']}:{item}")
    return (len(violations) > 0, violations)


def _unsupported_arm_summary(records: Sequence[Mapping[str, Any]], arm: str) -> dict[str, Any]:
    arm_records = [r for r in records if r["arm_id"] == arm]
    unsupported = [r for r in arm_records if r["status"] == "unsupported"]
    not_run = [r for r in arm_records if r["status"] == "not_run"]
    return {
        "expected_denominators_retained": True,
        "unsupported_count": len(unsupported),
        "not_run_count": len(not_run),
        "invented_success_score": False,
        "status_histogram": _status_histogram(arm_records),
        "blocks_c_threshold": False,
    }


def _core_arm_report(records: Sequence[Mapping[str, Any]], arm: str) -> dict[str, Any]:
    core = [r for r in records if r["kind"] == "core" and r["arm_id"] == arm]
    if arm == "B" and any(r["status"] == "unsupported" for r in core):
        return {"baseline_unsupported": True, **_unsupported_arm_summary(records, arm)}
    by_group: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for record in core:
        if record.get("group_id"):
            by_group[record["group_id"]].append(record)
    graded_core = list(core)
    graded_slot_by_group = {
        group: [record for record in group_records if record["status"] == "graded"]
        for group, group_records in by_group.items()
    }
    l3_groups: dict[str, Any] = {}
    for group in GROUPS:
        group_records = by_group.get(group, [])
        l3_groups[group] = _aggregate_condition_and_pair_metrics(
            group_records,
            layer="l3_pass",
            pair_layers=("l1_pass", "l2_pass", "l3_pass"),
        )
    slot_by_group = {group: by_group.get(group, []) for group in GROUPS}
    return {
        "status_histogram": _status_histogram(core),
        "l3": {
            "overall": _aggregate_condition_and_pair_metrics(
                graded_core,
                layer="l3_pass",
                pair_layers=("l1_pass", "l2_pass", "l3_pass"),
            ),
            "by_group": l3_groups,
            "slot_coverage": _slot_metrics(
                [record for record in graded_core if record["status"] == "graded"],
                by_group=graded_slot_by_group,
            ),
        },
        "layers": {
            layer: _aggregate_condition_and_pair_metrics(graded_core, layer=layer)
            for layer in ("l1_pass", "l2_pass", "l3_pass", "l4_pass")
        },
    }


def _query_host_arm_report(records: Sequence[Mapping[str, Any]], host: str, arm: str) -> dict[str, Any]:
    query = [r for r in records if r["kind"] == "query" and r["host_id"] == host and r["arm_id"] == arm]
    if arm == "B" and any(r["status"] == "unsupported" for r in query):
        return {"baseline_unsupported": True, **_unsupported_arm_summary(records, arm)}
    by_pair: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for record in query:
        by_pair[record["pair_id"]].append(record)
    pair_pass = 0
    pair_den = 0
    for pair_conds in by_pair.values():
        if len(pair_conds) != len(CONDITIONS):
            continue
        pair_den += 1
        graded = [c for c in pair_conds if c["status"] == "graded"]
        if len(graded) != len(CONDITIONS):
            continue
        if _pair_passes_layers(graded, ("l1_pass", "l2_pass", "l3_pass", "l4_pass")):
            pair_pass += 1
    return {
        "status_histogram": _status_histogram(query),
        "l4_pair_gate": {
            **rate_metric(pair_pass, L4_QUERY_PAIR_DENOMINATOR).as_dict(),
            "expected_pair_denominator": L4_QUERY_PAIR_DENOMINATOR,
            "minimum_passing_pairs": L4_QUERY_PAIR_MINIMUM,
            "passes_threshold": pair_pass >= L4_QUERY_PAIR_MINIMUM,
            "missing_or_failed_count_in_denominator": L4_QUERY_PAIR_DENOMINATOR - pair_den,
        },
        "layers": {
            layer: _aggregate_condition_and_pair_metrics(
                query,
                layer=layer,
                pair_layers=("l1_pass", "l2_pass", "l3_pass", "l4_pass") if layer == "l4_pass" else (layer,),
            )
            for layer in ("l1_pass", "l2_pass", "l3_pass", "l4_pass")
        },
    }


def _journey_host_arm_report(records: Sequence[Mapping[str, Any]], host: str, arm: str) -> dict[str, Any]:
    journeys = [r for r in records if r["kind"] == "journey" and r["host_id"] == host and r["arm_id"] == arm]
    if arm == "B" and any(r["status"] == "unsupported" for r in journeys):
        return {"baseline_unsupported": True, **_unsupported_arm_summary(records, arm)}
    complete = 0
    for record in journeys:
        if record["status"] != "graded":
            continue
        if (
            record.get("all_required_steps_pass") is True
            and _layer_known_pass(record, "l4_pass")
            and _layer_known_pass(record, "l1_pass")
            and _layer_known_pass(record, "l2_pass")
            and _layer_known_pass(record, "l3_pass")
            and not record.get("safety_violations")
        ):
            complete += 1
    return {
        "status_histogram": _status_histogram(journeys),
        "journey_gate": {
            **rate_metric(complete, JOURNEY_MINIMUM).as_dict(),
            "expected_journey_denominator": JOURNEY_MINIMUM,
            "minimum_complete_journeys": JOURNEY_MINIMUM,
            "passes_threshold": complete >= JOURNEY_MINIMUM,
            "missing_or_failed_count_in_denominator": JOURNEY_MINIMUM - len(journeys),
        },
    }


def _evaluate_c_threshold(attempt_report: Mapping[str, Any], inventory_complete: bool) -> dict[str, Any]:
    blockers: list[str] = []
    core = attempt_report["arms"]["C"]["core"]
    if core.get("baseline_unsupported"):
        blockers.append("c_core_unsupported")
    l3_overall = core["l3"]["overall"]["independent_pair_level"]
    overall_den = l3_overall["denominator"]
    overall_rate = l3_overall["rate"]
    l3_overall_pass = overall_den > 0 and overall_rate is not None and overall_rate >= L3_OVERALL_THRESHOLD
    if overall_den == 0:
        blockers.append("l3_overall_zero_answerable_denominator")
    elif not l3_overall_pass:
        blockers.append("l3_overall_below_threshold")

    slot_coverage = core["l3"]["slot_coverage"]
    if not slot_coverage.get("overall_passes_threshold"):
        blockers.append("l3_slot_coverage_below_threshold")
    if slot_coverage.get("answerable_conditions", 0) == 0:
        blockers.append("l3_slot_coverage_zero_answerable_denominator")
    for group in GROUPS:
        group_slot = slot_coverage.get("by_group", {}).get(group, {})
        group_rate = group_slot.get("slot_coverage_rate")
        if group_rate is None or group_rate < L3_GROUP_THRESHOLD:
            blockers.append(f"l3_slot_coverage_group_below_threshold:{group}")

    group_results: dict[str, Any] = {}
    for group in GROUPS:
        metric = core["l3"]["by_group"][group]["independent_pair_level"]
        den = metric["denominator"]
        rate = metric["rate"]
        passes = den > 0 and rate is not None and rate >= L3_GROUP_THRESHOLD
        if den == 0:
            blockers.append(f"l3_group_zero_answerable_denominator:{group}")
        elif not passes:
            blockers.append(f"l3_group_below_threshold:{group}")
        group_results[group] = {**metric, "threshold": L3_GROUP_THRESHOLD, "passes_threshold": passes if den > 0 else False}

    lower_layer_gaps = attempt_report.get("l1_l2_evidence_gaps", [])
    c_lower_gaps = [gap for gap in lower_layer_gaps if gap.get("arm_id") == "C"]
    if c_lower_gaps:
        blockers.append(f"c_lower_layer_gaps:{len(c_lower_gaps)}")

    hosts_report: dict[str, Any] = {}
    for host in HOSTS:
        query_gate = attempt_report["arms"]["C"]["hosts"][host]["query"]["l4_pair_gate"]
        journey_gate = attempt_report["arms"]["C"]["hosts"][host]["journey"]["journey_gate"]
        hosts_report[host] = {"l4_query_pairs": query_gate, "journeys_complete": journey_gate}
        if not query_gate["passes_threshold"]:
            blockers.append(f"l4_query_below_threshold:{host}")
        if not journey_gate["passes_threshold"]:
            blockers.append(f"journey_below_threshold:{host}")

    safety_blocked = attempt_report["safety"]["c_arm_blocked"]
    safety_violations = attempt_report["safety"].get("c_violations", attempt_report["safety"]["violations"])
    if safety_blocked:
        blockers.append("safety_violations")
    if not inventory_complete:
        blockers.append("inventory_incomplete")

    blocked = bool(blockers)
    return {
        "applicable": True,
        "blocked": blocked,
        "blockers": blockers,
        "inventory_blocks": not inventory_complete,
        "safety_blocks": safety_blocked,
        "l3_overall": {
            **l3_overall,
            "threshold": L3_OVERALL_THRESHOLD,
            "passes_threshold": l3_overall_pass if overall_den > 0 else False,
            "zero_denominator_blocks": overall_den == 0,
        },
        "l3_by_group": group_results,
        "l3_slot_coverage": slot_coverage,
        "hosts": hosts_report,
        "safety_violations": safety_violations,
        "baseline_b_failures_block_c_threshold": False,
        "would_pass_if_unblocked": not blocked,
    }


def build_attempt_report(
    records: Sequence[Mapping[str, Any]],
    attempt: int,
    identity_map: Mapping[str, Any],
) -> dict[str, Any]:
    subset = [r for r in records if r["attempt"] == attempt]
    inventory_errors = _inventory_errors_for_attempt(records, attempt, identity_map)
    inventory_complete = len(inventory_errors) == 0
    all_safety_blocked, all_safety_violations = _safety_blocks(subset)
    c_safety_blocked, c_safety_violations = _safety_blocks([r for r in subset if r["arm_id"] == "C"])
    safety_by_arm = {
        arm: _safety_blocks([r for r in subset if r["arm_id"] == arm])[1]
        for arm in ARMS
    }
    arms: dict[str, Any] = {}
    for arm in ARMS:
        hosts = {
            host: {
                "query": _query_host_arm_report(subset, host, arm),
                "journey": _journey_host_arm_report(subset, host, arm),
            }
            for host in HOSTS
        }
        arms[arm] = {"core": _core_arm_report(subset, arm), "hosts": hosts}
    attempt_report = {
        "attempt": attempt,
        "first_run": attempt == 1,
        "inventory": {"complete": inventory_complete, "errors": inventory_errors},
        "safety": {
            "blocked": all_safety_blocked,
            "violations": all_safety_violations,
            "violations_by_arm": safety_by_arm,
            "c_arm_blocked": c_safety_blocked,
            "c_violations": c_safety_violations,
        },
        "l1_l2_evidence_gaps": _l1_l2_gaps(subset),
        "arms": arms,
        "unsupported_baselines": {"B": _unsupported_arm_summary(subset, "B")},
        "c_threshold": _evaluate_c_threshold(
            {
                "arms": arms,
                "safety": {"c_arm_blocked": c_safety_blocked, "violations": c_safety_violations},
                "l1_l2_evidence_gaps": _l1_l2_gaps(subset),
            },
            inventory_complete,
        ),
    }
    return attempt_report


def build_report(
    metadata: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    *,
    evidence_root: Path | None = None,
    validate_evidence: bool = False,
    identity_map: Mapping[str, Any] | None = None,
    identity_map_path: Path | None = None,
    identity_map_sha256: str | None = None,
) -> dict[str, Any]:
    identity = identity_map if identity_map is not None else load_identity_map(metadata, identity_map_path, expected_sha256=identity_map_sha256)
    actual_identity_sha = identity.get("identity_map_sha256")
    if actual_identity_sha is not None and metadata.get("identity_map_sha256") != actual_identity_sha:
        raise AnnotationSchemaError("identity_sidecar_metadata_sha256_mismatch")
    if validate_evidence:
        if evidence_root is None:
            raise EvidenceValidationError("evidence_root_required")
        for record in records:
            _validate_evidence_refs(record["evidence"], evidence_root, validate_files=True, record=record)
            _verify_evidence_binding(record, evidence_root)
    attempt_reports = {
        str(attempt): build_attempt_report(records, attempt, identity) for attempt in sorted({record["attempt"] for record in records})
    }
    first_run = attempt_reports.get("1", {})
    inventory_complete = bool(first_run.get("inventory", {}).get("complete"))
    # Operation receipt completeness belongs to the formal composer.  The
    # score aggregator may validate file syntax/association, but it must not
    # promote partial annotation refs to a verified runtime binding.
    binding_verified = False
    # This is the score aggregation boundary.  Formal composer binding remains
    # explicitly pending even when the inventory itself is complete.
    aggregation_complete = inventory_complete
    return {
        "schema": REPORT_SCHEMA,
        "grading_module": metadata["grading_module"],
        "annotation_input_sha256": metadata["annotation_input_sha256"],
        "protocol_sha256": metadata["protocol_sha256"],
        "allocation_adjudication_sha256": metadata["allocation_adjudication_sha256"],
        "identity_map_sha256": identity.get("identity_map_sha256", metadata.get("identity_map_sha256", IDENTITY_MAP_SHA256)),
        "run_id": metadata["run_id"],
        "method_ids": metadata.get("method_ids", {}),
        "method_artifact_sha256": metadata.get("method_artifact_sha256", {}),
        "historical_host_label_is_not_transport_proof": True,
        "attempts": attempt_reports,
        "first_run_c_threshold": first_run.get("c_threshold", {"applicable": False, "blocked": True, "blockers": ["no_first_run"]}),
        "aggregation_complete": aggregation_complete,
        "binding_verified": binding_verified,
        "binding_status": "verified" if binding_verified else "pending_formal_composer_binding",
        "binding_pending_reasons": [] if binding_verified else [
            "formal_composer_must_verify_complete_operation_receipt_coverage",
            "journey_primary_operation_set_must_be_complete",
            "core_query_operation_ids_must_match_frozen_plan",
        ],
        "declares_formal_evaluation_complete": False,
        "declares_g2_complete": False,
        "declares_g3_complete": False,
        "declares_project_complete": False,
        "integration_gap": {
            "required_consumer_schema": "scope-recall.p18-formal-evaluation-receipt.v1",
            "composer_responsibility": "Separate formal composer must bind G2, candidate/source/wheel, frozen protocol/method/allocation, operation receipts and original ledger before emitting formal receipt.",
            "missing_for_formal_receipt": [
                "status",
                "formal_execution",
                "source",
                "protocol",
                "gates",
                "method_adjudication",
                "coverage",
                "scorer_report",
                "budget",
                "original_ledger",
                "evidence",
            ],
        },
        "limitations": [
            "Aggregates explicit independent-grader annotations only.",
            "Does not infer semantic grades from transport success or counts.",
            "Does not select best attempt or hide first-run failures.",
            "Does not declare formal evaluation, G2, G3, or overall project completion.",
        ],
    }


def write_report(report: Mapping[str, Any], output_path: Path) -> dict[str, str]:
    payload = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    output_path.write_bytes(payload)
    return {"path": str(output_path), "sha256": digest, "bytes": str(len(payload))}


def _aggregate_stdout_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    first = report.get("attempts", {}).get("1", {})
    return {
        "schema": report.get("schema"),
        "run_id": report.get("run_id"),
        "aggregation_complete": report.get("aggregation_complete"),
        "binding_verified": report.get("binding_verified"),
        "first_run_inventory_complete": first.get("inventory", {}).get("complete"),
        "first_run_c_threshold_blocked": first.get("c_threshold", {}).get("blocked"),
        "first_run_c_threshold_blockers": first.get("c_threshold", {}).get("blockers", []),
        "attempts_reported": sorted(int(key) for key in report.get("attempts", {})),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate and aggregate P18 grader annotations.")
    parser.add_argument("--metadata", required=True, type=Path)
    parser.add_argument("--annotations", required=True, type=Path)
    parser.add_argument("--evidence-root", type=Path, default=None)
    parser.add_argument("--identity-map", type=Path, default=None,
                        help="Explicit frozen opaque allocation sidecar JSON.")
    parser.add_argument("--identity-map-sha256", default=None,
                        help="Trusted SHA256 for --identity-map (the approved P18 sidecar hash).")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--validate-evidence",
        action="store_true",
        help="Require --evidence-root and verify every evidence path exists with matching SHA256.",
    )
    args = parser.parse_args(argv)
    try:
        metadata = load_bundle_metadata(args.metadata, args.identity_map, args.identity_map_sha256)
        annotation_bytes = args.annotations.read_bytes()
        records = load_annotations(args.annotations, metadata, raw_bytes=annotation_bytes)
        report = build_report(
            metadata,
            records,
            evidence_root=args.evidence_root,
            validate_evidence=args.validate_evidence or args.output is not None,
            identity_map_path=args.identity_map,
            identity_map_sha256=args.identity_map_sha256,
        )
        if args.output is not None:
            hash_meta = write_report(report, args.output)
            summary = _aggregate_stdout_summary(report)
            summary["report_sha256"] = hash_meta["sha256"]
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        else:
            print(json.dumps(_aggregate_stdout_summary(report), ensure_ascii=False, sort_keys=True))
        return 0
    except (AnnotationSchemaError, EvidenceValidationError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AnnotationSchemaError, EvidenceValidationError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
