"""Bounded C-arm executor for private P18 core-condition units.

The default entry point is a no-side-effect preflight.  A real run must be
explicitly bound to a frozen plan and an isolated TEST RuntimeInstance
configuration.  It uses the normal history importer, one bounded drain, and
a fresh RuntimeInstance session for recall; it never opens gold or writes a
claim or answer itself.
"""
from __future__ import annotations

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import argparse
import re
import sqlite3
import time
from typing import Any, Mapping, Sequence

from p18_history_loader import build_public_manifest, load_raw_history


class CoreExecutionError(ValueError):
    """Invalid private unit, configuration, or formal freeze binding."""


_CONTROL_FIELDS = frozenset({
    "case_id", "case_index", "case_index_control_only", "group_id", "core_class", "condition",
    "gold", "expected", "oracle", "required_facts", "answerability", "answerability_layers",
    "l3_memory_expectation", "l4_task_expectation", "prohibited_errors", "control_only",
    "independence_rationale",
})
_CORE_UNIT_FIELDS = frozenset({"unit_id", "kind", "ordinal", "arm_id", "source_sequence", "source_records", "query_record", "model_input"})
_CORE_SOURCE_SEQUENCE = "source_capture_then_actual_arm_extraction"
_RUNTIME_FROZEN_KEYS = (
    "request_seconds", "drain_seconds", "auto_recall_seconds", "hook_processing_seconds",
    "max_items", "lease_seconds", "vector_threshold", "vector", "auxiliary",
)
_LEGITIMATE_RECALL_MODES = frozenset({"auto", "current", "history", "as_of", "method"})
_UNIT_ARTIFACT_SCHEMA = "scope-recall.p18.core-unit-artifact.v1"
_UNIT_FAILURE_SCHEMA = "scope-recall.p18.core-unit-failure.v1"
_DRAIN_POLL_SECONDS = 0.25


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise CoreExecutionError(f"json_read_failed:{path.name}") from exc


def _reject_controls(value: object) -> None:
    if isinstance(value, Mapping):
        if any(key in _CONTROL_FIELDS for key in value):
            raise CoreExecutionError("control_or_gold_field_in_core_unit")
        for child in value.values():
            _reject_controls(child)
    elif isinstance(value, list):
        for child in value:
            _reject_controls(child)


def _safe_test_root(path: str | Path, *, new: bool) -> Path:
    root = Path(path).expanduser().resolve()
    lowered = str(root).replace("/", "\\").lower().rstrip("\\")
    if lowered == "f:\\agents" or lowered.startswith("f:\\agents\\") or not root.name.lower().startswith(("test-", "test_")):
        raise CoreExecutionError("unsafe_core_execution_root")
    if new:
        if root.exists() and any(root.iterdir()):
            raise CoreExecutionError("core_execution_output_must_be_new")
        root.mkdir(parents=True, exist_ok=False)
    return root


def _load_units(unit_path: Path) -> list[dict[str, Any]]:
    if unit_path.name.lower() in {"gold.jsonl", "gold.json"} or "gold" in unit_path.name.lower():
        raise CoreExecutionError("gold_unit_path_forbidden")
    if not unit_path.is_file():
        raise CoreExecutionError("core_unit_file_missing")
    rows: list[dict[str, Any]] = []
    for line in unit_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, Mapping):
                raise CoreExecutionError("core_unit_must_be_object")
            _reject_controls(value)
            rows.append(dict(value))
    if not rows or len(rows) > 240:
        raise CoreExecutionError("core_unit_count_out_of_bounds")
    seen: set[str] = set()
    ordinals: set[int] = set()
    for row in rows:
        if set(row) != _CORE_UNIT_FIELDS:
            raise CoreExecutionError("core_unit_schema_invalid")
        unit_id = row.get("unit_id")
        if not isinstance(unit_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", unit_id) or unit_id in seen or row.get("kind") != "core_condition":
            raise CoreExecutionError("core_unit_identity_invalid")
        ordinal = row.get("ordinal")
        if type(ordinal) is not int or not 1 <= ordinal <= 240 or ordinal in ordinals or unit_id != f"core-C-{ordinal:03d}":
            raise CoreExecutionError("core_unit_identity_invalid")
        if row.get("arm_id") != "C" or row.get("source_sequence") != _CORE_SOURCE_SEQUENCE:
            raise CoreExecutionError("core_unit_binding_invalid")
        seen.add(unit_id)
        ordinals.add(ordinal)
        model_input = row.get("model_input")
        if not isinstance(model_input, Mapping) or set(model_input) != {"history", "query"}:
            raise CoreExecutionError("core_unit_model_input_invalid")
        if not isinstance(model_input.get("history"), list) or not isinstance(model_input.get("query"), Mapping):
            raise CoreExecutionError("core_unit_model_input_invalid")
        if not isinstance(row.get("source_records"), list) or not isinstance(row.get("query_record"), Mapping):
            raise CoreExecutionError("core_unit_private_source_payload_missing")
    return rows


def _load_runtime_config(path: Path):
    from scope_recall.runtime.instance import RuntimeInstanceConfig

    raw = _json(path)
    if not isinstance(raw, Mapping):
        raise CoreExecutionError("runtime_config_must_be_object")
    provenance = raw.get("source_provenance")
    if not isinstance(provenance, Mapping) or not isinstance(provenance.get("commit"), str) or not provenance["commit"].strip():
        raise CoreExecutionError("runtime_source_provenance_missing")
    config = RuntimeInstanceConfig.from_mapping(raw)
    if not config.binding.test_mode:
        raise CoreExecutionError("core_execution_requires_test_binding")
    return config, {"commit": provenance["commit"], "source_root": provenance.get("source_root"), "package_sha256": provenance.get("package_sha256"), "wheel_sha256": provenance.get("wheel_sha256")}


def _record_manifest(unit: Mapping[str, Any]) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for event in unit["source_records"]:
        if not isinstance(event, Mapping):
            raise CoreExecutionError("core_source_record_invalid")
        occurred = event.get("occurred_at")
        recorded = event.get("recorded_at", occurred)
        precision = event.get("time_precision", "instant")
        source_key = event.get("event_id")
        role = event.get("speaker_role")
        origin = event.get("source_type")
        revision = event.get("source_revision", 1)
        if not all(isinstance(value, str) and value.strip() for value in (occurred, recorded, source_key, role, origin, event.get("text"))):
            raise CoreExecutionError("core_source_record_identity_missing")
        if precision not in {"instant", "day", "approximate", "unknown"}:
            raise CoreExecutionError("core_source_record_precision_invalid")
        if type(revision) is not int or revision < 1:
            raise CoreExecutionError("core_source_record_revision_invalid")
        records.append({
            "order": event.get("sequence"), "source_event_key": source_key, "source_revision": revision,
            "role": role, "source_original_origin": origin, "text": event["text"],
            "occurred_at": occurred, "recorded_at": recorded, "time_precision": precision, "evidence_refs": [],
        })
    return build_public_manifest(records)


def _receipt_hashes(refs: Sequence[str]) -> str:
    return hashlib.sha256("\n".join(refs).encode("utf-8")).hexdigest()


def _relative_artifact(base: Path, value: object, *, reason: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise CoreExecutionError(reason)
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise CoreExecutionError(reason)
    resolved = (base / candidate).resolve()
    if resolved.parent != (base / candidate.parent).resolve() or not resolved.is_file():
        raise CoreExecutionError(reason)
    return resolved


def _runtime_frozen_projection(raw: Mapping[str, Any]) -> dict[str, Any]:
    auxiliary = raw.get("auxiliary")
    if not isinstance(auxiliary, Mapping):
        raise CoreExecutionError("formal_runtime_auxiliary_missing")
    return {key: raw.get(key) for key in _RUNTIME_FROZEN_KEYS}


def _runtime_frozen_sha256(raw: Mapping[str, Any]) -> str:
    projected = json.dumps(_runtime_frozen_projection(raw), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256_bytes(projected.encode("utf-8"))


def _validate_formal_config(formal_config_path: Path, unit_file: Path, rows: Sequence[Mapping[str, Any]], runtime_config_path: Path | None = None) -> dict[str, Any]:
    """Require the real P18 evidence gate plus a C-arm unit binding."""

    from p18_formal_evidence import verify_formal_run_config

    readiness = verify_formal_run_config(formal_config_path)
    if not readiness.formal_execution_allowed:
        reasons = ",".join(readiness.reasons) or "formal_config_not_ready"
        raise CoreExecutionError(f"formal_config_not_ready:{reasons}")
    raw = _json(formal_config_path)
    core_inputs = raw.get("core_inputs") if isinstance(raw, Mapping) else None
    if not isinstance(core_inputs, Mapping) or core_inputs.get("arm_id") != "C" or core_inputs.get("conditions_per_arm") != 240:
        raise CoreExecutionError("formal_core_inputs_missing_or_invalid")
    dataset_id = core_inputs.get("dataset_id")
    raw_sha256 = core_inputs.get("raw_sha256")
    if not isinstance(dataset_id, str) or not dataset_id.strip() or not isinstance(raw_sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", raw_sha256):
        raise CoreExecutionError("formal_core_dataset_binding_missing")
    base = formal_config_path.parent
    unit_binding = core_inputs.get("unit")
    if not isinstance(unit_binding, Mapping):
        raise CoreExecutionError("formal_core_unit_binding_missing")
    declared_unit = _relative_artifact(base, unit_binding.get("path"), reason="formal_core_unit_path_invalid")
    declared_sha = unit_binding.get("sha256")
    if not isinstance(declared_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", declared_sha) or _sha256(declared_unit) != declared_sha or declared_unit != unit_file:
        raise CoreExecutionError("formal_core_unit_hash_mismatch")
    if unit_binding.get("record_count") != 240 or len(rows) != 240:
        raise CoreExecutionError("formal_core_unit_denominator_invalid")
    plan_binding = core_inputs.get("plan")
    if not isinstance(plan_binding, Mapping):
        raise CoreExecutionError("formal_core_plan_binding_missing")
    plan_path = _relative_artifact(base, plan_binding.get("path"), reason="formal_core_plan_path_invalid")
    plan_sha = plan_binding.get("sha256")
    if not isinstance(plan_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", plan_sha) or _sha256(plan_path) != plan_sha:
        raise CoreExecutionError("formal_core_plan_hash_mismatch")
    plan = _json(plan_path)
    if not isinstance(plan, Mapping) or plan.get("status") != "READY_FOR_FORMAL_FREEZE" or plan.get("freeze_ready") is not True:
        raise CoreExecutionError("formal_core_plan_not_frozen")
    if plan.get("dataset_id") != dataset_id or plan.get("raw_sha256") != raw_sha256:
        raise CoreExecutionError("formal_core_dataset_binding_mismatch")
    plan_core = plan.get("core")
    if not isinstance(plan_core, Mapping) or plan_core.get("conditions_per_arm") != 240 or plan_core.get("arms") != 4:
        raise CoreExecutionError("formal_core_plan_denominator_invalid")
    plan_paths = {str(item).replace("/", "\\") for item in plan_core.get("core_paths", []) if isinstance(item, str)}
    declared_relative = str(declared_unit.relative_to(base)).replace("/", "\\")
    plan_relative = str(declared_unit.relative_to(plan_path.parent)).replace("/", "\\") if declared_unit.is_relative_to(plan_path.parent) else ""
    if declared_relative not in plan_paths and plan_relative not in plan_paths:
        raise CoreExecutionError("formal_core_unit_not_in_plan")
    runtime_binding = core_inputs.get("runtime")
    result = {"config_sha256": _sha256(formal_config_path), "readiness": dict(readiness.details), "arm_id": "C", "conditions_per_arm": 240, "dataset_id": dataset_id, "raw_sha256": raw_sha256, "unit_path": str(declared_unit), "unit_sha256": declared_sha, "plan_path": str(plan_path), "plan_sha256": plan_sha}
    if runtime_config_path is not None:
        if not isinstance(runtime_binding, Mapping):
            raise CoreExecutionError("formal_core_runtime_binding_missing")
        declared_runtime = _relative_artifact(base, runtime_binding.get("path"), reason="formal_core_runtime_path_invalid")
        runtime_sha = runtime_binding.get("sha256")
        if declared_runtime != runtime_config_path or not isinstance(runtime_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", runtime_sha) or _sha256(declared_runtime) != runtime_sha:
            raise CoreExecutionError("formal_core_runtime_hash_mismatch")
        source_commit = readiness.details.get("candidate_source_commit")
        wheel_sha256 = readiness.details.get("wheel_sha256")
        if runtime_binding.get("source_commit") != source_commit or runtime_binding.get("wheel_sha256") != wheel_sha256:
            raise CoreExecutionError("formal_core_runtime_candidate_mismatch")
        frozen_sha = runtime_binding.get("frozen_fields_sha256")
        runtime_raw = _json(declared_runtime)
        if not isinstance(runtime_raw, Mapping) or not isinstance(frozen_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", frozen_sha) or _runtime_frozen_sha256(runtime_raw) != frozen_sha:
            raise CoreExecutionError("formal_core_runtime_frozen_fields_mismatch")
        result.update({"runtime_path": str(declared_runtime), "runtime_sha256": runtime_sha, "runtime_frozen_fields_sha256": frozen_sha})
    return result


def _validate_formal_runtime_binding(config_file: Path, config: Any, provenance: Mapping[str, Any], formal_binding: Mapping[str, Any]) -> None:
    readiness = formal_binding.get("readiness")
    if not isinstance(readiness, Mapping):
        raise CoreExecutionError("formal_runtime_readiness_missing")
    candidate_commit = readiness.get("candidate_source_commit")
    candidate_wheel = readiness.get("wheel_sha256")
    if provenance.get("commit") != candidate_commit or provenance.get("package_sha256") != candidate_wheel:
        raise CoreExecutionError("runtime_source_provenance_candidate_mismatch")
    if getattr(config, "vector", None) is not None and config.vector.test_injection_override:
        raise CoreExecutionError("formal_vector_test_injection_override")
    ledger = getattr(getattr(config, "auxiliary", None), "ledger_path", None)
    ledger_path = readiness.get("ledger_path")
    if not isinstance(ledger_path, str) or ledger is None or Path(ledger).resolve() != Path(ledger_path).resolve():
        raise CoreExecutionError("formal_auxiliary_ledger_mismatch")
    raw = _json(config_file)
    if not isinstance(raw, Mapping) or _runtime_frozen_sha256(raw) != formal_binding.get("runtime_frozen_fields_sha256"):
        raise CoreExecutionError("formal_runtime_frozen_fields_mismatch")


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict

        return _jsonable(asdict(value))
    return value


def _close_runtime(instance: object | None) -> None:
    if instance is None:
        return
    closer = getattr(instance, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:
            pass


def _recall_mode(unit: Mapping[str, Any]) -> str:
    model_input = unit.get("model_input")
    if not isinstance(model_input, Mapping):
        return "auto"
    query = model_input.get("query")
    if not isinstance(query, Mapping):
        return "auto"
    mode = query.get("mode")
    if isinstance(mode, str) and mode in _LEGITIMATE_RECALL_MODES:
        return mode
    return "auto"


def _explicit_budget_tokens(unit: Mapping[str, Any]) -> int:
    from scope_recall.core.retrieval import AUTOMATIC_PACKET_BUDGET_UNITS, SearchLimits

    model_input = unit.get("model_input")
    query = model_input.get("query") if isinstance(model_input, Mapping) else None
    if not isinstance(query, Mapping) or "budget_tokens" not in query:
        return AUTOMATIC_PACKET_BUDGET_UNITS
    value = query["budget_tokens"]
    try:
        SearchLimits(budget_tokens=value)
    except Exception as exc:
        raise CoreExecutionError("core_query_budget_tokens_invalid") from exc
    if value > AUTOMATIC_PACKET_BUDGET_UNITS:
        return AUTOMATIC_PACKET_BUDGET_UNITS
    return value


def _build_recall_request(unit: Mapping[str, Any], config) -> dict[str, Any]:
    query_text = unit["query_record"].get("text")
    if not isinstance(query_text, str) or not query_text.strip():
        raise CoreExecutionError("core_query_text_missing")
    mode = _recall_mode(unit)
    request: dict[str, Any] = {
        "protocol_version": "1.1",
        "request_id": unit["unit_id"],
        "query": query_text,
        "mode": mode,
        "max_items": min(6, config.max_items),
        "budget_tokens": _explicit_budget_tokens(unit),
    }
    if mode == "as_of":
        model_query = unit.get("model_input", {}).get("query", {})
        as_of = model_query.get("as_of") if isinstance(model_query, Mapping) else None
        if not isinstance(as_of, str) or not as_of.strip():
            raise CoreExecutionError("core_query_as_of_missing")
        request["as_of"] = as_of
    return request


def _rebind_vector(vector, data_directory: Path):
    if vector is None:
        return None
    from scope_recall.core.recall_policy import SPACE_ID

    if vector.test_injection_override:
        storage_dir = (data_directory / "vectors").resolve()
        return replace(vector, storage_dir=storage_dir)
    storage_dir = (data_directory / "vectors" / SPACE_ID).resolve()
    return replace(vector, storage_dir=storage_dir)


def _unit_runtime_configs(template, unit_id: str, unit_root: Path):
    from scope_recall.contracts import InstanceBinding
    from scope_recall.runtime.instance import RuntimeInstanceConfig

    data_directory = (unit_root / "data").resolve()
    data_directory.mkdir(parents=True, exist_ok=True)
    binding = InstanceBinding(
        template.binding.agent_id,
        f"TEST-installation-{unit_id}",
        data_directory,
        template.binding.scope_ids,
        True,
    )
    auxiliary = template.auxiliary
    if auxiliary is not None:
        auxiliary = replace(
            auxiliary,
            installation_dir=data_directory,
            ledger_path=auxiliary.ledger_path,
        )
        if auxiliary.consolidation is not None:
            headers = dict(auxiliary.consolidation.headers)
            if "x-opencode-session" in headers:
                headers["x-opencode-session"] = f"{headers['x-opencode-session']}:{unit_id}"
                auxiliary = replace(auxiliary, consolidation=replace(auxiliary.consolidation, headers=headers))
    vector = _rebind_vector(template.vector, data_directory)
    capture_config = RuntimeInstanceConfig(
        binding=binding,
        session_id=f"TEST-core-source-{unit_id}",
        allowed_scope_ids=template.allowed_scope_ids,
        actor_origin=template.actor_origin,
        project_id=template.project_id,
        branch_id=template.branch_id,
        owner_id=template.owner_id,
        request_seconds=template.request_seconds,
        drain_seconds=template.drain_seconds,
        auto_recall_seconds=template.auto_recall_seconds,
        hook_processing_seconds=template.hook_processing_seconds,
        max_items=template.max_items,
        lease_seconds=template.lease_seconds,
        auxiliary=auxiliary,
        vector=vector,
        vector_threshold=template.vector_threshold,
    )
    query_config = replace(capture_config, session_id=f"TEST-core-query-{unit_id}")
    return capture_config, query_config


def _parse_work_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _pending_work_schedule(instance) -> tuple[int, int, float | None]:
    """Observe durable pending/leased work without changing product retry rules.

    Returns ``(pending_or_leased, claimable_now, seconds_until_next_ready)``.
    ``pending_or_leased == -1`` means the queue could not be read.
    """
    try:
        database = Path(instance.config.binding.data_directory) / "memory.sqlite3"
    except (AttributeError, TypeError):
        return (-1, 0, None)
    if not database.is_file():
        return (-1, 0, None)
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0.25)) as db:
            rows = db.execute(
                "SELECT state, available_at, lease_until FROM work_items WHERE state IN ('pending','leased')"
            ).fetchall()
    except sqlite3.Error:
        return (-1, 0, None)
    if not rows:
        return (0, 0, None)
    now = datetime.now(timezone.utc)
    claimable = 0
    delays: list[float] = []
    for state, available_at, lease_until in rows:
        ready_at = _parse_work_timestamp(available_at if state == "pending" else lease_until)
        if ready_at is None or ready_at <= now:
            claimable += 1
        else:
            delays.append(max(0.0, (ready_at - now).total_seconds()))
    if claimable:
        return (len(rows), claimable, 0.0)
    return (len(rows), 0, min(delays) if delays else None)


def _bounded_drain(instance, *, timeout_seconds: float) -> dict[str, Any]:
    started = time.monotonic()
    drains: list[dict[str, Any]] = []
    aggregate = {"processed": 0, "completed": 0, "failed": 0, "retried": 0}
    timed_out = False
    while time.monotonic() - started < timeout_seconds:
        remaining = timeout_seconds - (time.monotonic() - started)
        if remaining < 0.001:
            timed_out = True
            break
        original_config = instance.config
        instance.config = replace(original_config, drain_seconds=min(original_config.drain_seconds, remaining))
        try:
            drain = instance.drain()
        finally:
            instance.config = original_config
        drains.append({
            "processed": drain.processed,
            "completed": drain.completed,
            "failed": drain.failed,
            "retried": drain.retried,
            "idle": drain.idle,
            "items": [_jsonable(item) for item in drain.items],
        })
        for key in aggregate:
            aggregate[key] += int(getattr(drain, key))
        status = instance.status()
        if int(status.pending_work) == 0:
            break
        if not drain.idle:
            continue
        # idle means nothing was claimable in this pass. Pending work with a
        # future available_at is scheduled backoff, not completion.
        pending, claimable, delay = _pending_work_schedule(instance)
        if pending == 0:
            break
        remaining = timeout_seconds - (time.monotonic() - started)
        if remaining < 0.001:
            timed_out = True
            break
        if claimable > 0:
            sleep_for = min(_DRAIN_POLL_SECONDS, remaining)
        elif delay is None:
            sleep_for = min(_DRAIN_POLL_SECONDS, remaining)
        else:
            sleep_for = min(float(delay), remaining)
        if sleep_for < 0.001:
            timed_out = True
            break
        time.sleep(sleep_for)
    else:
        timed_out = True
    final_status = instance.status()
    return {
        "drains": drains,
        "aggregate": aggregate,
        "timed_out": timed_out,
        "pending_work_after": final_status.pending_work,
        "sources_after_drain": final_status.sources,
        "memory_epoch_after_drain": final_status.memory_epoch,
    }


def _write_json(path: Path, payload: Mapping[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    raw = text.encode("utf-8")
    with path.open("xb") as handle:
        handle.write(raw)
    return _sha256_bytes(raw)


def _execute_unit(unit: Mapping[str, Any], template, output_root: Path, scope_id: str) -> dict[str, Any]:
    from scope_recall.runtime.instance import build_runtime_instance

    unit_id = unit["unit_id"]
    unit_root = output_root / "units" / unit_id
    unit_root.mkdir(parents=True, exist_ok=False)
    capture_config = query_config = None
    capture_instance = None
    query_instance = None
    started_at = time.monotonic()
    try:
        capture_config, query_config = _unit_runtime_configs(template, unit_id, unit_root)
        manifest = _record_manifest(unit)
        capture_instance = build_runtime_instance(capture_config)
        capture_instance.core.initialize()
        load_result = load_raw_history(
            capture_instance.core,
            capture_config.context(),
            manifest,
            scope_id=scope_id,
            arm_id="C",
        )
        if load_result.status != "PASS":
            raise CoreExecutionError("core_source_capture_failed")
        drain_evidence = _bounded_drain(capture_instance, timeout_seconds=float(capture_config.drain_seconds))
        query_instance = build_runtime_instance(query_config)
        query_instance.core.initialize()
        request = _build_recall_request(unit, query_config)
        packet = query_instance.core.recall_packet(
            query_config.context(),
            request,
            deadline_seconds=query_config.request_seconds,
        )
        if not isinstance(packet, Mapping):
            packet = dict(packet)
        query_status = query_instance.status()
        packet_json = json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        source_refs = tuple(item["ref"] for item in packet.get("items", []) if isinstance(item, Mapping) and isinstance(item.get("ref"), str))
        artifact = {
            "schema": _UNIT_ARTIFACT_SCHEMA,
            "status": "EXECUTED_WITHOUT_SEMANTIC_SCORING",
            "unit_id": unit_id,
            "installation_id": capture_config.binding.installation_id,
            "data_directory": str(capture_config.binding.data_directory),
            "source_session_id": capture_config.session_id,
            "query_session_id": query_config.session_id,
            "source_manifest_sha256": manifest["manifest_sha256"],
            "source_load": {
                "records_seen": load_result.records_seen,
                "inserted": load_result.inserted,
                "duplicates": load_result.duplicates,
                "source_refs": list(load_result.source_refs),
                "queued_work_items": load_result.queued_work_items,
            },
            "drain": drain_evidence,
            "recall_request": request,
            "recall_packet": dict(packet),
            "packet_sha256": _sha256_bytes(packet_json.encode("utf-8")),
            "source_ref_count": len(source_refs),
            "source_ref_sha256": _receipt_hashes(source_refs) if source_refs else _sha256_bytes(b""),
            "query_status": {
                "sources": query_status.sources,
                "pending_work": query_status.pending_work,
                "memory_epoch": query_status.memory_epoch,
            },
            "elapsed_seconds": round(time.monotonic() - started_at, 3),
        }
        artifact_path = unit_root / "unit-artifact.json"
        artifact_sha256 = _write_json(artifact_path, artifact)
        return {
            "unit_id": unit_id,
            "status": "EXECUTED_WITHOUT_SEMANTIC_SCORING",
            "artifact_path": str(artifact_path),
            "artifact_sha256": artifact_sha256,
            "packet_sha256": artifact["packet_sha256"],
            "installation_id": capture_config.binding.installation_id,
            "data_directory": str(capture_config.binding.data_directory),
            "source_session_id": capture_config.session_id,
            "query_session_id": query_config.session_id,
            "pending_work_after_drain": drain_evidence["pending_work_after"],
            "drain_timed_out": drain_evidence["timed_out"],
        }
    except Exception as exc:
        failure = {
            "schema": _UNIT_FAILURE_SCHEMA,
            "status": "FAILED",
            "unit_id": unit_id,
            "installation_id": capture_config.binding.installation_id if capture_config is not None else None,
            "data_directory": str(capture_config.binding.data_directory) if capture_config is not None else str(unit_root / "data"),
            "source_session_id": capture_config.session_id if capture_config is not None else f"TEST-core-source-{unit_id}",
            "query_session_id": query_config.session_id if query_config is not None else f"TEST-core-query-{unit_id}",
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "elapsed_seconds": round(time.monotonic() - started_at, 3),
        }
        failure_path = unit_root / "unit-failure.json"
        failure_sha256 = _write_json(failure_path, failure)
        return {
            "unit_id": unit_id,
            "status": "FAILED",
            "failure_path": str(failure_path),
            "failure_sha256": failure_sha256,
            "error_type": type(exc).__name__,
        }
    finally:
        _close_runtime(query_instance)
        _close_runtime(capture_instance)


def run_core_unit(unit_path: str | Path, runtime_config_path: str | Path, output_root: str | Path, *, run: bool = False, freeze_receipt_path: str | Path | None = None) -> dict[str, Any]:
    """Preflight or execute one C-arm core unit file.

    ``run=False`` performs no SQLite initialization.  ``run=True`` requires a
    frozen plan receipt and then executes the units through the normal public
    importer/runtime interfaces with an isolated TEST binding.
    """
    unit_file = Path(unit_path).expanduser().resolve()
    config_file = Path(runtime_config_path).expanduser().resolve()
    rows = _load_units(unit_file)
    config, provenance = _load_runtime_config(config_file)
    template_data_directory = str(config.binding.data_directory)
    formal_binding: dict[str, Any] | None = None
    concurrency = 1
    if run:
        if freeze_receipt_path is None:
            raise CoreExecutionError("formal_freeze_receipt_required")
        concurrency = json.loads(Path(freeze_receipt_path).read_text(encoding='utf-8')).get('core_concurrency', 1)
        if type(concurrency) is not int or not 1 <= concurrency <= 4:
            raise CoreExecutionError('core_concurrency_must_be_between_one_and_four')
        formal_binding = _validate_formal_config(Path(freeze_receipt_path).expanduser().resolve(), unit_file, rows, config_file)
        if formal_binding.get("conditions_per_arm") != 240:
            raise CoreExecutionError("formal_freeze_core_denominator_invalid")
        # Existing public offline tests replace the admission function with an
        # explicit test seam.  Real formal evidence always carries the full
        # verifier details and therefore cannot opt out of runtime binding.
        readiness = formal_binding.get("readiness")
        if not (isinstance(readiness, Mapping) and readiness.get("test_mock") is True):
            _validate_formal_runtime_binding(config_file, config, provenance, formal_binding)
    output = _safe_test_root(output_root, new=True)
    external_auxiliary = bool(config.auxiliary and (config.auxiliary.external_embedding or config.auxiliary.external_consolidation))
    receipt: dict[str, Any] = {
        "schema": "scope-recall.p18.core-execution.v1", "status": "PREFLIGHT_ONLY" if not run else "RUN_PENDING",
        "formal_execution_started": False, "unit_path": str(unit_file), "unit_sha256": _sha256(unit_file),
        "unit_count": len(rows), "runtime_config_path": str(config_file), "binding_test_mode": config.binding.test_mode,
        "template_installation_id": config.binding.installation_id,
        "template_data_directory": template_data_directory,
        "source_provenance": provenance,
        "network_calls": 0 if not external_auxiliary else None,
        "model_calls": 0 if not external_auxiliary else None,
        "usage_counts_measured": not external_auxiliary,
        "auxiliary_ledger_path": str(config.auxiliary.ledger_path) if config.auxiliary and config.auxiliary.ledger_path is not None else None,
        "gold_read": False, "claims_or_answers_written": False,
        "semantic_score": None,
        "max_parallel_units": concurrency,
        "formal_binding": formal_binding,
    }
    if not run:
        (output / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return receipt
    scope_id = next(iter(config.allowed_scope_ids))
    unit_summaries: list[dict[str, Any]] = []
    succeeded = failed = 0
    summaries_path = output / "unit-summaries.jsonl"
    with summaries_path.open("x", encoding="utf-8", newline="\n") as summary_handle, ThreadPoolExecutor(max_workers=concurrency) as pool:
        for summary in pool.map(lambda unit: _execute_unit(unit, config, output, scope_id), rows):
            unit_summaries.append(summary)
            if summary["status"] == "FAILED":
                failed += 1
            else:
                succeeded += 1
            summary_handle.write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n")
            summary_handle.flush()
    receipt.update({
        "status": "EXECUTED_WITHOUT_SEMANTIC_SCORING" if failed == 0 else "EXECUTED_WITH_UNIT_FAILURES",
        "formal_execution_started": True,
        "units_succeeded": succeeded,
        "units_failed": failed,
        "unit_summaries_path": str(summaries_path),
        "unit_summaries": [
            {key: value for key, value in item.items() if key not in {"artifact_path", "failure_path"}}
            for item in unit_summaries
        ],
        "template_data_directory_touched": Path(template_data_directory).exists(),
    })
    (output / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P18 C-arm core execution; TEST-only")
    parser.add_argument("unit_path", type=Path)
    parser.add_argument("runtime_config_path", type=Path)
    parser.add_argument("output_root", type=Path)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--freeze-receipt", type=Path)
    args = parser.parse_args(argv)
    try:
        receipt = run_core_unit(args.unit_path, args.runtime_config_path, args.output_root, run=args.run, freeze_receipt_path=args.freeze_receipt)
    except (CoreExecutionError, OSError, ValueError) as exc:
        print(json.dumps({"status": "REJECTED", "reason": type(exc).__name__}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": receipt["status"], "unit_count": receipt["unit_count"], "network_calls": receipt["network_calls"], "model_calls": receipt["model_calls"]}, ensure_ascii=False))
    return 0


__all__ = ["CoreExecutionError", "run_core_unit"]


if __name__ == "__main__":
    raise SystemExit(_main())
