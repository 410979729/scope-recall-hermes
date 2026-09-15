"""Small, append-only evidence boundary for the P18 formal runner.

This module executes no host and scores no answers. A run config binds the
candidate, source fingerprint, G2 report, original ledger path and an operation
map (host_id/arm_id/unit/request_id). G2 is checked before P18, without a P18
aggregate. Retained artifact paths stay within that config's directory. The
mutable original ledger may be external when its canonical path and filesystem
identity are explicitly bound; its contents are never copied into the bundle.

The writer requires the retained transport receipt and a runner-observed
association artifact: operation_id, source, ids, source_capture_refs,
request_sha256s, ledger_entries (route/id/request_sha256), and context_id.
Hermes additionally retains host_request (the original A2A request artifact);
its actual session id is observed from Hermes state, never inferred from the
A2A context id. Go model request bytes are separate from that A2A envelope.

Usage entries reference existing requests.id or codex_submissions.operation_id
and original model request artifacts; this module never creates a ledger.
The append-only output contains run_binding and accounted_usage. COMPLETED is
execution evidence, never semantic PASS. Provenance of the runner's retained
observations remains subject to independent G3 review, not a boolean signature.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from p18_ledger_reference import LedgerReferenceError, resolve_ledger_reference


PROTOCOL_SHA256 = "45b70192857b5b8645dd2bf409db55f320f1cf31f297e89910f9f340e76d32d4"
METHOD_ID = "codex_windows_appserver_native_hooks_v2"
METHOD_SHA256 = "029f8a2eeecf47ddc7d2a4be62457993a9667d58277f5158a4e745bda17653a4"
HERMES_METHOD_ID = "hermes_cli_local_input_v1"
HERMES_METHOD_SHA256 = "119f596efd98476a87401775b692bf0dd7718e72949e0f1813571720c85c57af"
CONFIG_SCHEMA = "scope-recall.p18-formal-run-config.v1"
RECEIPT_SCHEMA = "scope-recall.p18-formal-operation-evidence.v1"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT40 = re.compile(r"^[0-9a-fA-F]{40}$")
_CONTROL_KEYS = frozenset(
    {"case_id", "case_index", "group_id", "gold", "expected", "oracle", "required_facts", "prohibited_errors", "control_only"}
)


class EvidenceSchemaError(ValueError):
    """The host supplied an incomplete or unsafe evidence record."""


class FormalNotReady(EvidenceSchemaError):
    """A formal operation was requested before all frozen gates passed."""


def _strict_json(raw: bytes | str) -> Any:
    def reject_constant(value: str) -> None:
        raise EvidenceSchemaError("nonfinite_json_number")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise EvidenceSchemaError("duplicate_json_key")
            result[key] = value
        return result

    def finite_float(number: str) -> float:
        value = float(number)
        if not math.isfinite(value):
            raise EvidenceSchemaError("nonfinite_json_number")
        return value

    value = json.loads(
        raw,
        parse_constant=reject_constant,
        parse_float=finite_float,
        object_pairs_hook=unique_object,
    )
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_path(value: Any, base: Path, *, must_exist: bool = True) -> Path:
    if type(value) is not str or not value.strip():
        raise EvidenceSchemaError("artifact_path_invalid")
    candidate = Path(value)
    if candidate.is_absolute():
        raise EvidenceSchemaError("artifact_path_must_be_relative")
    target = (base / candidate).resolve()
    if not target.is_relative_to(base.resolve()):
        raise EvidenceSchemaError("artifact_outside_evidence_root")
    if must_exist and not target.is_file():
        raise EvidenceSchemaError("artifact_missing")
    return target


def _artifact(reference: Any, base: Path) -> tuple[Path, bytes]:
    if type(reference) is not dict or set(reference) != {"path", "sha256"}:
        raise EvidenceSchemaError("artifact_reference_invalid")
    digest = reference["sha256"]
    if type(digest) is not str or not _HEX64.fullmatch(digest):
        raise EvidenceSchemaError("artifact_digest_invalid")
    target = _relative_path(reference["path"], base)
    raw = target.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise EvidenceSchemaError("artifact_hash_mismatch")
    return target, raw


def _verify_candidate_build(candidate: Mapping[str, Any], base: Path) -> tuple[Path, Path, str, str]:
    """Verify the retained build receipt against its source and wheel bytes."""
    source_commit = candidate.get("source_commit")
    if type(source_commit) is not str or not _COMMIT40.fullmatch(source_commit):
        raise EvidenceSchemaError("candidate_source_commit_missing")
    receipt_path, receipt_raw = _artifact(candidate.get("receipt"), base)
    receipt = _strict_json(receipt_raw)
    if type(receipt) is not dict:
        raise EvidenceSchemaError("candidate_build_receipt_invalid")
    if receipt.get("source_commit") != source_commit:
        raise EvidenceSchemaError("candidate_source_commit_mismatch")
    count = receipt.get("package_files_checked")
    if type(count) is not int or count < 1 or type(receipt.get("package_files")) is not list or len(receipt["package_files"]) != count:
        raise EvidenceSchemaError("candidate_package_file_count_invalid")
    if receipt.get("mismatches") != []:
        raise EvidenceSchemaError("candidate_build_mismatches")
    wheel_path, wheel_raw = _artifact(candidate.get("wheel"), base)
    wheel_digest = hashlib.sha256(wheel_raw).hexdigest()
    if receipt.get("wheel_sha256") != wheel_digest:
        raise EvidenceSchemaError("candidate_wheel_hash_mismatch")
    seen: set[str] = set()
    expected: dict[str, str] = {}
    for item in receipt["package_files"]:
        if type(item) is not dict or set(item) != {"path", "sha256"}:
            raise EvidenceSchemaError("candidate_package_file_entry_invalid")
        relative = item["path"]
        digest = item["sha256"]
        if type(relative) is not str or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise EvidenceSchemaError("candidate_package_path_invalid")
        if type(digest) is not str or not _HEX64.fullmatch(digest) or relative in seen:
            raise EvidenceSchemaError("candidate_package_file_digest_invalid")
        seen.add(relative)
        expected[relative] = digest
    try:
        with zipfile.ZipFile(wheel_path) as archive:
            members = {
                name.removeprefix("scope_recall/")
                for name in archive.namelist()
                if name.startswith("scope_recall/") and not name.endswith("/")
            }
            if members != set(expected):
                raise EvidenceSchemaError("candidate_wheel_package_set_mismatch")
            for relative, digest in expected.items():
                if hashlib.sha256(archive.read("scope_recall/" + relative)).hexdigest() != digest:
                    raise EvidenceSchemaError("candidate_wheel_package_hash_mismatch")
    except zipfile.BadZipFile as exc:
        raise EvidenceSchemaError("candidate_wheel_invalid") from exc
    source_root = _relative_path(candidate.get("source_root"), base, must_exist=False)
    if not source_root.is_dir():
        raise EvidenceSchemaError("candidate_source_root_missing")
    for relative, digest in expected.items():
        source = (source_root / relative).resolve()
        if not source.is_file() or not source.is_relative_to(source_root) or _sha256(source) != digest:
            raise EvidenceSchemaError("candidate_source_package_hash_mismatch")
    git_root = candidate.get("git_root")
    if git_root is not None:
        git_path = _relative_path(git_root, base, must_exist=False)
        if not git_path.is_dir():
            raise EvidenceSchemaError("candidate_git_root_missing")
        try:
            actual_commit = subprocess.check_output(
                ["git", "-C", str(git_path), "rev-parse", "--verify", source_commit + "^{commit}"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise EvidenceSchemaError("candidate_git_commit_unverifiable") from exc
        if actual_commit != source_commit:
            raise EvidenceSchemaError("candidate_git_commit_mismatch")
    return receipt_path, wheel_path, source_commit, wheel_digest


@dataclass(frozen=True)
class FormalReadiness:
    status: str
    reasons: tuple[str, ...]
    details: Mapping[str, Any]

    @property
    def formal_execution_allowed(self) -> bool:
        return self.status == "READY"


def verify_formal_run_config(config_path: str | Path) -> FormalReadiness:
    """Validate a frozen run config without starting a host or reading a DB."""

    path = Path(config_path).expanduser().resolve()
    reasons: list[str] = []
    details: dict[str, Any] = {"config_path": str(path)}
    try:
        config_raw = path.read_bytes()
        details["config_sha256"] = hashlib.sha256(config_raw).hexdigest()
        config_value = _strict_json(config_raw)
        if type(config_value) is not dict or config_value.get("schema") != CONFIG_SCHEMA:
            raise EvidenceSchemaError("formal_config_schema_invalid")
        base = path.parent

        protocol = config_value.get("protocol")
        protocol_path, protocol_raw = _artifact(protocol, base)
        if hashlib.sha256(protocol_raw).hexdigest() != PROTOCOL_SHA256:
            raise EvidenceSchemaError("protocol_hash_not_frozen")
        _strict_json(protocol_raw)

        method = config_value.get("method")
        if type(method) is not dict or method.get("id") != METHOD_ID:
            raise EvidenceSchemaError("method_id_not_frozen")
        method_path, method_raw = _artifact(method.get("artifact"), base)
        if hashlib.sha256(method_raw).hexdigest() != METHOD_SHA256:
            raise EvidenceSchemaError("method_hash_not_frozen")
        method_payload = _strict_json(method_raw)
        if type(method_payload) is not dict or method_payload.get("method", {}).get("id") != METHOD_ID:
            raise EvidenceSchemaError("method_artifact_id_mismatch")
        original = method_payload.get("original_protocol")
        if type(original) is not dict or original.get("sha256") != PROTOCOL_SHA256:
            raise EvidenceSchemaError("method_protocol_binding_mismatch")

        candidate = config_value.get("candidate")
        if type(candidate) is not dict:
            raise EvidenceSchemaError("candidate_freeze_missing")
        receipt_path, wheel_path, source_commit, wheel_digest = _verify_candidate_build(candidate, base)

        gate = config_value.get("g2_review")
        if type(gate) is not dict:
            raise EvidenceSchemaError("g2_review_missing")
        gate_path, gate_raw = _artifact(gate.get("artifact"), base)
        report = _strict_json(gate_raw)
        if type(report) is not dict or report.get("status") != "PASS" or report.get("kind") != "independent_gate_review" or report.get("evidence_kind") != "real":
            raise EvidenceSchemaError("g2_not_passed")
        source = config_value.get("source")
        if (type(source) is not dict or source.get("commit") != source_commit
                or type(source.get("source_inputs_sha256")) is not str
                or not _HEX64.fullmatch(source["source_inputs_sha256"])):
            raise EvidenceSchemaError("source_binding_invalid")
        expected_gate = {
            "gate": "G2", "status": "PASS", "kind": "independent_gate_review",
            "evidence_kind": "real", "source": source,
            "protocol": config_value.get("protocol"), "method_id": METHOD_ID,
        }
        if any(report.get(key) != value for key, value in expected_gate.items()):
            raise EvidenceSchemaError("g2_report_binding_mismatch")
        if report.get("unresolved_p0_p1") != []:
            raise EvidenceSchemaError("g2_p0_p1_unresolved")
        evidence = report.get("evidence")
        if type(evidence) is not list or not evidence:
            raise EvidenceSchemaError("g2_evidence_missing")
        for reference in evidence:
            _artifact(reference, base)
        # G2 precedes formal P18. Its own report has the release validator's
        # G2 contract; the later aggregate/scorer is deliberately not required.
        operations = _operation_map(config_value.get("operations"))
        if any(op["host_id"] == HERMES_METHOD_ID for op in operations.values()):
            if any(op["host_id"] == "hermes_a2a" for op in operations.values()):
                raise EvidenceSchemaError("Hermes_arms_cannot_mix_CLI_and_A2A_methods")
            hermes_method = config_value.get("hermes_method")
            if not isinstance(hermes_method, dict) or hermes_method.get("id") != HERMES_METHOD_ID:
                raise EvidenceSchemaError("hermes_CLI_method_binding_required")
            hermes_path, hermes_raw = _artifact(hermes_method.get("artifact"), base)
            if hashlib.sha256(hermes_raw).hexdigest() != HERMES_METHOD_SHA256:
                raise EvidenceSchemaError("hermes_CLI_method_bytes_mismatch")
            hermes_payload = _strict_json(hermes_raw)
            if (hermes_payload.get("method", {}).get("id") != HERMES_METHOD_ID
                    or hermes_payload.get("original_protocol", {}).get("sha256") != PROTOCOL_SHA256
                    or report.get("hermes_method") != hermes_method):
                raise EvidenceSchemaError("hermes_CLI_G2_method_binding_mismatch")
            details.update(hermes_method_path=str(hermes_path), hermes_method_sha256=HERMES_METHOD_SHA256)
        ledger_binding = config_value.get("ledger_binding")
        ledger_path = resolve_ledger_reference(config_value.get("ledger_path"), base, ledger_binding)
        details.update(
            {
                "protocol_path": str(protocol_path),
                "method_path": str(method_path),
                "candidate_receipt_path": str(receipt_path),
                "candidate_wheel_path": str(wheel_path),
                "candidate_source_commit": source_commit,
                "g2_path": str(gate_path),
                "wheel_sha256": wheel_digest,
                "source": source,
                "operations": operations,
                "ledger_path": str(ledger_path),
                "ledger_binding": ledger_binding,
            }
        )
    except (OSError, ValueError, TypeError, KeyError, AttributeError, json.JSONDecodeError) as exc:
        reasons.append(str(exc) or type(exc).__name__)
    status = "READY" if not reasons else "NOT_READY"
    return FormalReadiness(status, tuple(reasons), details)


def _walk_control_keys(value: Any, *, path: str = "") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).casefold() in _CONTROL_KEYS:
                raise EvidenceSchemaError("control_field_in_host_response")
            _walk_control_keys(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_control_keys(child, path=f"{path}[{index}]")


def _hash_and_artifact(value: Mapping[str, Any], base: Path, *, prefix: str) -> None:
    allowed = {"status", f"{prefix}_sha256", "artifact_path"}
    if set(value) != allowed:
        raise EvidenceSchemaError(f"{prefix}_evidence_schema_invalid")
    status = value["status"]
    if type(status) is not str or status not in {"available", "delivered", "not_attempted", "failed", "unknown"}:
        raise EvidenceSchemaError(f"{prefix}_status_invalid")
    digest = value[f"{prefix}_sha256"]
    artifact = value["artifact_path"]
    if digest is not None and (type(digest) is not str or not _HEX64.fullmatch(digest)):
        raise EvidenceSchemaError(f"{prefix}_digest_invalid")
    if artifact is not None:
        target = _relative_path(artifact, base)
        if digest is None or _sha256(target) != digest:
            raise EvidenceSchemaError(f"{prefix}_artifact_hash_mismatch")
    if status in {"available", "delivered"} and (digest is None or artifact is None):
        raise EvidenceSchemaError(f"{prefix}_evidence_missing")


def _operation_map(value: Any) -> dict[str, Any]:
    if type(value) is not dict or not value:
        raise EvidenceSchemaError("operations_missing")
    units = set()
    requests = set()
    for operation_id, operation in value.items():
        if type(operation_id) is not str or not _SAFE_NAME.fullmatch(operation_id):
            raise EvidenceSchemaError("operation_id_invalid")
        if type(operation) is not dict or set(operation) not in ({"host_id", "arm_id", "unit", "request_id"}, {"host_id", "arm_id", "unit", "request_id", "fault"}):
            raise EvidenceSchemaError("frozen_operation_invalid")
        if operation["host_id"] not in {"hermes_a2a", HERMES_METHOD_ID, METHOD_ID} or operation["arm_id"] not in {"A", "B", "C", "D"}:
            raise EvidenceSchemaError("frozen_host_or_arm_invalid")
        unit = operation["unit"]
        if "fault" in operation and (operation["fault"] != "sqlite_unavailable" or not isinstance(unit, dict) or unit.get("journey_id") != "J08"):
            raise EvidenceSchemaError("frozen_fault_not_approved_journey")
        if type(unit) is not dict or set(unit) != {"kind", "query_id", "journey_id", "round_id"}:
            raise EvidenceSchemaError("frozen_unit_invalid")
        key = {"query": "query_id", "journey": "journey_id", "round": "round_id"}.get(unit["kind"])
        if key is None or type(unit[key]) is not str or not unit[key].strip():
            raise EvidenceSchemaError("frozen_unit_invalid")
        if type(operation["request_id"]) is not str or not operation["request_id"].strip():
            raise EvidenceSchemaError("frozen_request_id_invalid")
        # A query_id denotes a paired variant (e.g. q01-answerable/q01-insufficient),
        # never a new independent sample. Round ids are scoped by journey_id.
        unit_key = (operation["host_id"], operation["arm_id"], json.dumps(unit, sort_keys=True))
        request_key = (operation["host_id"], operation["request_id"])
        if unit_key in units or request_key in requests:
            raise EvidenceSchemaError("duplicate_frozen_operation")
        units.add(unit_key)
        requests.add(request_key)
    return value


def _validate_usage(usage: Any, base: Path, record: Mapping[str, Any], readiness: FormalReadiness) -> dict[str, Any]:
    """Read existing Go/Codex rows; never reserve, finalize or create a ledger.

    entries = [{"id": <Go integer id or Codex operation_id>,
                "request": {"path": <original model request bytes>, "sha256": ...}}]
    A Hermes turn may contain several real model calls. These are Go request
    bodies, not the different outer A2A message/send body.
    """
    if type(usage) is not dict or set(usage) != {"status", "input_tokens", "output_tokens", "ledger_path", "entries"}:
        raise EvidenceSchemaError("usage_schema_invalid")
    if usage["status"] not in {"known", "unknown", "not_applicable"}:
        raise EvidenceSchemaError("usage_status_invalid")
    for key in ("input_tokens", "output_tokens"):
        if usage[key] is not None and (type(usage[key]) is not int or usage[key] < 0):
            raise EvidenceSchemaError("usage_token_invalid")
    if usage["status"] == "not_applicable":
        if (record["status"] not in {"UNSUPPORTED", "FAILED"}
                or usage["entries"] != [] or usage["ledger_path"] is not None
                or usage["input_tokens"] is not None or usage["output_tokens"] is not None):
            raise EvidenceSchemaError("model_execution_requires_ledger")
        return {"route": None, "entries": []}
    try:
        ledger = resolve_ledger_reference(usage["ledger_path"], base, readiness.details.get("ledger_binding"))
    except LedgerReferenceError as exc:
        raise EvidenceSchemaError(str(exc)) from exc
    if str(ledger) != readiness.details["ledger_path"]:
        raise EvidenceSchemaError("ledger_not_frozen_original")
    entries = usage["entries"]
    if type(entries) is not list or not entries:
        raise EvidenceSchemaError("ledger_entries_missing")
    route = "go" if record["host_id"] in {"hermes_a2a", HERMES_METHOD_ID} else "codex"
    table, key, digest_column = ("requests", "id", "body_sha256") if route == "go" else ("codex_submissions", "operation_id", "request_sha256")
    checked = []
    row_ids = set()
    try:
        # The original ledger can still have a WAL. immutable=1 would silently
        # miss its newest reservations; use a real read-only snapshot transaction.
        with sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True, timeout=2) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            for entry in entries:
                if type(entry) is not dict or set(entry) != {"id", "request"}:
                    raise EvidenceSchemaError("ledger_entry_invalid")
                row_id = entry["id"]
                if (route == "go" and (type(row_id) is not int or row_id < 1)
                        or route == "codex" and (type(row_id) is not str or not row_id)):
                    raise EvidenceSchemaError("ledger_id_invalid")
                if row_id in row_ids:
                    raise EvidenceSchemaError("duplicate_ledger_row")
                row_ids.add(row_id)
                _, request_raw = _artifact(entry["request"], base)
                request_digest = hashlib.sha256(request_raw).hexdigest()
                row = db.execute(f"SELECT * FROM {table} WHERE {key}=?", (row_id,)).fetchone()
                if row is None or row[digest_column] != request_digest or row["request_bytes"] != len(request_raw):
                    raise EvidenceSchemaError("ledger_request_binding_mismatch")
                values = {name: row[name] for name in ("reserved_input", "reserved_output", "actual_input", "actual_output")}
                if any(v is not None and (type(v) is not int or v < 0) for v in values.values()):
                    raise EvidenceSchemaError("ledger_token_invalid")
                if any(type(values[name]) is not int or values[name] < 1 for name in ("reserved_input", "reserved_output")):
                    raise EvidenceSchemaError("ledger_unknown_upper_bound")
                if (values["actual_input"] is None) != (values["actual_output"] is None):
                    raise EvidenceSchemaError("ledger_partial_usage")
                if record["status"] == "COMPLETED":
                    if row["status"] in {"meter_breach", "reserved_before_network", "reserved_before_dispatch"}:
                        raise EvidenceSchemaError("ledger_not_completed")
                    if any(values["actual_" + side] is not None and values["actual_" + side] > values["reserved_" + side] for side in ("input", "output")):
                        raise EvidenceSchemaError("ledger_meter_breach")
                    if route == "codex" and (row["dispatch_status"] != "dispatched" or row["finished_ns"] is None or row["historical_not_pre_dispatch"] != 0):
                        raise EvidenceSchemaError("ledger_not_formal_dispatch")
                item = {"id": row_id, "request_sha256": request_digest, "status": row["status"], **values}
                if route == "go":
                    if type(row["charge_micro_usd"]) is not int or row["charge_micro_usd"] < 0:
                        raise EvidenceSchemaError("ledger_charge_invalid")
                    item["charge_micro_usd"] = row["charge_micro_usd"]
                checked.append(item)
    except sqlite3.Error as exc:
        raise EvidenceSchemaError("ledger_row_unreadable") from exc
    known = all(item["actual_input"] is not None for item in checked)
    actual_in = sum(item["actual_input"] for item in checked) if known else None
    actual_out = sum(item["actual_output"] for item in checked) if known else None
    if usage["status"] != ("known" if known else "unknown") or (usage["input_tokens"], usage["output_tokens"]) != (actual_in, actual_out):
        raise EvidenceSchemaError("ledger_usage_mismatch")
    return {"route": route, "entries": checked,
            "effective_input_tokens": sum(item["actual_input"] if item["actual_input"] is not None else item["reserved_input"] for item in checked),
            "effective_output_tokens": sum(item["actual_output"] if item["actual_output"] is not None else item["reserved_output"] for item in checked)}


def _validate_operation(record: Mapping[str, Any], base: Path, *, diagnostic: bool) -> dict[str, Any]:
    required = {
        "operation_id", "attempt", "host_id", "arm_id", "unit", "ids", "source_capture_refs",
        "delivery", "answer", "usage", "status", "latency_ms", "no_retry", "host_response", "association",
    }
    if set(record) != required:
        raise EvidenceSchemaError("operation_schema_invalid")
    operation_id = record["operation_id"]
    if type(operation_id) is not str or not _SAFE_NAME.fullmatch(operation_id):
        raise EvidenceSchemaError("operation_id_invalid")
    if record["attempt"] != 1 or type(record["attempt"]) is not int:
        raise EvidenceSchemaError("only_first_attempt_is_recordable")
    for key in ("host_id", "arm_id"):
        if type(record[key]) is not str or not record[key].strip():
            raise EvidenceSchemaError(f"{key}_invalid")
    unit = record["unit"]
    if type(unit) is not dict or set(unit) != {"kind", "query_id", "journey_id", "round_id"}:
        raise EvidenceSchemaError("unit_schema_invalid")
    if unit["kind"] not in {"query", "journey", "round"}:
        raise EvidenceSchemaError("unit_kind_invalid")
    expected_key = {"query": "query_id", "journey": "journey_id", "round": "round_id"}[unit["kind"]]
    if type(unit[expected_key]) is not str or not unit[expected_key].strip():
        raise EvidenceSchemaError("unit_id_missing")
    for key in ("query_id", "journey_id", "round_id"):
        if unit[key] is not None and (type(unit[key]) is not str or not unit[key].strip()):
            raise EvidenceSchemaError("unit_id_invalid")
    ids = record["ids"]
    if type(ids) is not dict or set(ids) != {"request_id", "turn_id", "session_id", "unknown"}:
        raise EvidenceSchemaError("host_ids_schema_invalid")
    unknown = ids["unknown"]
    if type(unknown) is not list or any(type(item) is not str or item not in {"request_id", "turn_id", "session_id"} for item in unknown):
        raise EvidenceSchemaError("unknown_host_ids_invalid")
    for key in ("request_id", "turn_id", "session_id"):
        value = ids[key]
        if value is not None and (type(value) is not str or not value.strip()):
            raise EvidenceSchemaError("host_id_value_invalid")
        if value is None and key not in unknown:
            raise EvidenceSchemaError("missing_host_id_not_declared")
        if value is not None and key in unknown:
            raise EvidenceSchemaError("known_host_id_declared_unknown")
    refs = record["source_capture_refs"]
    if type(refs) is not list or len(refs) > 64 or any(type(item) is not str or not item.strip() for item in refs):
        raise EvidenceSchemaError("source_capture_refs_invalid")
    _hash_and_artifact(record["delivery"], base, prefix="context")
    _hash_and_artifact(record["answer"], base, prefix="answer")
    status = record["status"]
    if type(status) is not str or status not in {"COMPLETED", "FAILED", "TRUNCATED", "UNSUPPORTED", "DIAGNOSTIC"}:
        raise EvidenceSchemaError("operation_status_invalid")
    if (diagnostic and status != "DIAGNOSTIC") or (not diagnostic and status == "DIAGNOSTIC"):
        raise EvidenceSchemaError("fixture_cannot_be_formal_pass")
    latency = record["latency_ms"]
    if type(latency) not in {int, float} or isinstance(latency, bool) or not math.isfinite(float(latency)) or latency < 0:
        raise EvidenceSchemaError("latency_invalid")
    if record["no_retry"] is not True:
        raise EvidenceSchemaError("retry_not_allowed")
    host_response = record["host_response"]
    if type(host_response) is not dict or set(host_response) != {"response_sha256", "artifact_path", "http_status", "transport", "real_host"}:
        raise EvidenceSchemaError("host_response_schema_invalid")
    _walk_control_keys(host_response)
    if type(host_response["real_host"]) is not bool or (not diagnostic and not host_response["real_host"]):
        raise EvidenceSchemaError("host_response_is_not_real")
    response_digest = host_response["response_sha256"]
    if response_digest is not None and (type(response_digest) is not str or not _HEX64.fullmatch(response_digest)):
        raise EvidenceSchemaError("host_response_digest_invalid")
    if host_response["artifact_path"] is not None:
        target = _relative_path(host_response["artifact_path"], base)
        if response_digest is None or _sha256(target) != response_digest:
            raise EvidenceSchemaError("host_response_artifact_hash_mismatch")
    if type(host_response["http_status"]) not in {int, type(None)}:
        raise EvidenceSchemaError("host_response_status_invalid")
    if type(host_response["transport"]) is not str or not host_response["transport"].strip():
        raise EvidenceSchemaError("host_response_transport_invalid")
    _walk_control_keys(record)
    return dict(record)


def _validate_bound_operation(record: Mapping[str, Any], base: Path, readiness: FormalReadiness) -> dict[str, Any]:
    operation = readiness.details["operations"].get(record["operation_id"])
    if operation is None or any(record[key] != operation[key] for key in ("host_id", "arm_id", "unit")) or record["ids"]["request_id"] != operation["request_id"]:
        raise EvidenceSchemaError("operation_not_frozen")
    accounted = _validate_usage(record["usage"], base, record, readiness)
    _, association_raw = _artifact(record["association"], base)
    association = _strict_json(association_raw)
    # This retained association is the runner's host/store observation, not a
    # model-authored statement. Hermes contextId is not its real session id.
    # G3 audits observation provenance; byte equality is not a human signature.
    expected = {"operation_id": record["operation_id"], "source": readiness.details["source"],
                "ids": record["ids"], "source_capture_refs": record["source_capture_refs"],
                "request_sha256s": [item["request_sha256"] for item in accounted["entries"]],
                "ledger_entries": [{"route": accounted["route"], "id": item["id"], "request_sha256": item["request_sha256"]} for item in accounted["entries"]]}
    if type(association) is not dict or any(association.get(key) != value for key, value in expected.items()):
        raise EvidenceSchemaError("host_association_mismatch")
    reference = record["host_response"]
    _, response_raw = _artifact({"path": reference["artifact_path"], "sha256": reference["response_sha256"]}, base)
    response = _strict_json(response_raw)
    if type(response) is not dict or response.get("formal_evaluation") is not True:
        raise EvidenceSchemaError("diagnostic_transport_cannot_be_formal")
    if reference["transport"] != response.get("transport"):
        raise EvidenceSchemaError("transport_mismatch")
    completed = record["status"] == "COMPLETED"
    ids = record["ids"]
    if completed and (ids["unknown"] or any(type(ids[key]) is not str or not ids[key] for key in ("request_id", "turn_id", "session_id"))):
        raise EvidenceSchemaError("completed_host_ids_missing")
    if record["host_id"] == HERMES_METHOD_ID:
        from p18_hermes_cli_transport import TRANSPORT, validate_cli_capture
        if response.get("transport") != TRANSPORT or response.get("fixture_mode") is not False:
            raise EvidenceSchemaError("hermes_CLI_transport_not_real")
        _, request_raw = _artifact(association.get("host_request"), base)
        if (hashlib.sha256(request_raw).hexdigest() != response.get("request_sha256")
                or _strict_json(request_raw) != response.get("request")
                or response.get("request_id") != ids["request_id"]):
            raise EvidenceSchemaError("hermes_CLI_request_binding_mismatch")
        process = response.get("process", {})
        if completed:
            observed = validate_cli_capture(response, base)
            if (any(observed[key] != ids[key] for key in ("session_id", "turn_id"))
                    or response.get("status") != "COMPLETED" or response.get("errors") != []
                    or type(process.get("pid")) is not int or process["pid"] < 1
                    or process.get("returncode") != 0 or process.get("error") is not None
                    or response.get("retry_count") != 0 or not accounted["entries"]):
                raise EvidenceSchemaError("hermes_CLI_completion_mismatch")
            # Upstream byte archives are separately verified against actual Go
            # rows. The first primary request must contain this exact input.
            first_entry = min(record["usage"]["entries"], key=lambda item: item["id"])
            first = _strict_json(_artifact(first_entry["request"], base)[1])
            users = [m.get("content") for m in first.get("messages", []) if m.get("role") == "user"]
            # Frozen Hermes persists raw content separately from api_content:
            # the latter includes its real native memory/plugin injection.
            if not users or users[-1] != observed["model_user_content"] or (response.get("requested_session_id") is None and len(users) != 1):
                raise EvidenceSchemaError("hermes_CLI_actual_model_input_mismatch")
            exported = observed["export_usage"]
            if exported["status"] == "known" and (exported["api_call_count"] != len(accounted["entries"])
                    or (record["usage"]["status"] == "known" and any(
                        exported[key] != record["usage"][key] for key in ("input_tokens", "output_tokens")))):
                from p18_hermes_usage_gap import validate_summary_gap
                try:
                    validate_summary_gap(record, response, observed, accounted, base)
                except (ValueError, KeyError, TypeError, OSError):
                    raise EvidenceSchemaError("hermes_CLI_export_ledger_usage_mismatch") from None
        answer = response.get("answer_text")
    elif record["host_id"] == "hermes_a2a":
        if response.get("transport") != "hermes_a2a_jsonrpc" or response.get("fixture_mode") is not False:
            raise EvidenceSchemaError("hermes_transport_not_real")
        request = response.get("request")
        if type(request) is not dict or request.get("id") != ids["request_id"]:
            raise EvidenceSchemaError("hermes_request_id_mismatch")
        _, request_raw = _artifact(association.get("host_request"), base)
        request_digest = hashlib.sha256(request_raw).hexdigest()
        if response.get("request_sha256") != request_digest or _strict_json(request_raw) != request:
            raise EvidenceSchemaError("hermes_request_hash_mismatch")
        if completed and (response.get("status") != "COMPLETED" or response.get("transport_status") != "COMPLETED"
                          or response.get("message_send_calls") != 1 or response.get("retry_count") != 0
                          or response.get("task_id") != ids["turn_id"]
                          or not association.get("context_id") or response.get("context_id") != association["context_id"]
                          or response.get("answer_truncated") is not False):
            raise EvidenceSchemaError("hermes_completion_mismatch")
        answer = response.get("answer_text")
        if not accounted["entries"] and response.get("message_send_calls") != 0:
            raise EvidenceSchemaError("dispatched_request_requires_ledger")
    else:
        process = response.get("process", {})
        if (response.get("schema") != "scope-recall.p18.codex-appserver-candidate-receipt.v1"
                or response.get("transport") != "candidate-codex-app-server-stdio-jsonrpc"
                or type(process) is not dict or type(process.get("pid")) is not int or process["pid"] < 1
                or process.get("cleanup") == "fixture" or response.get("arm", {}).get("arm_id") != record["arm_id"]):
            raise EvidenceSchemaError("codex_transport_not_real")
        host_ids = response.get("association", {})
        if completed and (host_ids.get("thread_id") != ids["session_id"] or host_ids.get("turn_id") != ids["turn_id"]
                          or response.get("turn", {}).get("completed", {}).get("status") != "completed"
                          or response.get("errors") != [] or response.get("io", {}).get("stdout_truncated") is not False
                          or response.get("io", {}).get("parse_error_count") != 0
                          or response.get("rpc_provenance", {}).get("turn_rpcs_dispatched") != 1):
            raise EvidenceSchemaError("codex_completion_mismatch")
        answer = response.get("public_output")
        if not accounted["entries"] and response.get("rpc_provenance", {}).get("turn_rpcs_dispatched") != 0:
            raise EvidenceSchemaError("dispatched_request_requires_ledger")
    if completed:
        if record["answer"]["status"] != "available" or type(answer) is not str or not answer.strip():
            raise EvidenceSchemaError("completed_answer_missing")
        if hashlib.sha256(answer.encode("utf-8")).hexdigest() != record["answer"]["answer_sha256"]:
            raise EvidenceSchemaError("host_answer_binding_mismatch")
    return accounted



class FormalEvidenceWriter:
    """Write one immutable JSON receipt per operation id under a TEST root."""

    def __init__(self, root: str | Path, config_path: str | Path | None = None) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.config_path = Path(config_path).expanduser().resolve() if config_path is not None else None
        self._bound_config_sha256 = None
        self.readiness = None
        if self.config_path is not None:
            self.readiness = verify_formal_run_config(self.config_path)
            self._bound_config_sha256 = self.readiness.details.get("config_sha256")

    def append_operation(self, record: Mapping[str, Any], *, diagnostic: bool = False) -> Path:
        if not diagnostic:
            if self.config_path is None or not isinstance(self.readiness, FormalReadiness) or not self.readiness.formal_execution_allowed:
                raise FormalNotReady("formal_execution_not_ready")
            current = verify_formal_run_config(self.config_path)
            if not current.formal_execution_allowed or current.details.get("config_sha256") != self._bound_config_sha256:
                raise FormalNotReady("formal_config_changed_or_not_ready")
        base = self.root if diagnostic else self.config_path.parent
        normalized = _validate_operation(record, base, diagnostic=diagnostic)
        accounted = None if diagnostic else _validate_bound_operation(normalized, base, current)
        normalized = {
            "schema": RECEIPT_SCHEMA,
            "evidence_class": "diagnostic" if diagnostic else "formal",
            **normalized,
        }
        if not diagnostic:
            normalized["run_binding"] = {
                "config_sha256": self._bound_config_sha256, "source": current.details["source"],
                "protocol_sha256": PROTOCOL_SHA256, "method_id": METHOD_ID,
                "method_sha256": METHOD_SHA256, "wheel_sha256": current.details["wheel_sha256"],
            }
            normalized["accounted_usage"] = accounted
            if normalized["host_id"] == HERMES_METHOD_ID:
                normalized["run_binding"]["hermes_method_id"] = HERMES_METHOD_ID
                normalized["run_binding"]["hermes_method_sha256"] = HERMES_METHOD_SHA256
        payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        final = self.root / f"{normalized['operation_id']}.json"
        temp = self.root / f".{normalized['operation_id']}.{uuid.uuid4().hex}.tmp"
        lock = self.root / ".formal-evidence.lock"
        try:
            lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise EvidenceSchemaError("evidence_writer_busy") from exc
        os.close(lock_fd)
        try:
            if not diagnostic:
                self._reject_replayed_operation(normalized)
            fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temp, final)
            except FileExistsError as exc:
                raise EvidenceSchemaError("operation_id_already_recorded") from exc
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass
            lock.unlink()
        return final

    def _reject_replayed_operation(self, record: Mapping[str, Any]) -> None:
        ids = record["ids"]
        rows = {(record["accounted_usage"]["route"], item["id"]) for item in record["accounted_usage"]["entries"]}
        for path in self.root.glob("*.json"):
            previous = _strict_json(path.read_bytes())
            if type(previous) is not dict or previous.get("schema") != RECEIPT_SCHEMA or previous.get("evidence_class") != "formal":
                continue
            previous_ids = previous.get("ids", {})
            if previous["host_id"] == record["host_id"] and (
                previous_ids.get("request_id") == ids["request_id"]
                or (ids["session_id"] and ids["turn_id"] and (previous_ids.get("session_id"), previous_ids.get("turn_id")) == (ids["session_id"], ids["turn_id"]))
            ):
                raise EvidenceSchemaError("host_execution_already_recorded")
            prior = previous.get("accounted_usage", {})
            if rows & {(prior.get("route"), item["id"]) for item in prior.get("entries", [])}:
                raise EvidenceSchemaError("ledger_execution_already_recorded")


__all__ = [
    "CONFIG_SCHEMA",
    "FormalEvidenceWriter",
    "FormalNotReady",
    "FormalReadiness",
    "EvidenceSchemaError",
    "METHOD_ID",
    "METHOD_SHA256",
    "PROTOCOL_SHA256",
    "RECEIPT_SCHEMA",
    "verify_formal_run_config",
]
