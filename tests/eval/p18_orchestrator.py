"""Small P18 TEST-only orchestration preflight.

The orchestrator prepares isolated host/arm roots and exercises only the
public synthetic C-arm Core import path.  It does not open sealed raw/gold,
call a model, or make a network request unless a caller explicitly supplies an
actual Hermes endpoint.  Injected worker/transport functions are diagnostic
only and are never formal evidence.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from scope_recall.contracts import InstanceBinding, TrustedContext
from scope_recall.core.composition import CoreConfig, MemoryCore

from p18_history_loader import (
    HistoryLoaderError,
    build_public_manifest,
    history_event_dtos,
    load_raw_history,
    validate_history_manifest,
)


HOSTS: tuple[str, ...] = ("hermes_a2a", "codex_windows_desktop")
ARMS: tuple[str, ...] = ("A", "B", "C", "D")
LAYER_NAMES: tuple[str, ...] = ("L1_record", "L2_organization", "L3_recall", "L4_behavior")


class OrchestratorError(ValueError):
    """Invalid preparation configuration or trust-boundary violation."""


@dataclass(frozen=True)
class Stage:
    layer: str
    status: str
    reason: str | None = None
    details: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class _FixedClock:
    tick: int = 0

    def utc_now(self) -> str:
        return "2026-09-06T14:00:00Z"

    def monotonic(self) -> float:
        return 0.0


def _safe_test_root(value: str | Path) -> Path:
    root = Path(value).expanduser().resolve()
    lowered = str(root).replace("/", "\\").lower().rstrip("\\")
    if lowered == "f:\\agents" or lowered.startswith("f:\\agents\\"):
        raise OrchestratorError("formal F:\\Agents roots are forbidden")
    return root


def _stage(layer: str, status: str, reason: str | None = None, details: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return asdict(Stage(layer, status, reason, dict(details or {})))


def _counts(core: MemoryCore, context: TrustedContext) -> dict[str, int]:
    with core.storage.read(context) as tx:
        conn = tx._check()
        return {
            table: int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for table in ("source_events", "claims", "claim_versions", "work_items")
        }


def _history_model_input(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Project only public history/query fields accepted by the host driver."""

    events = history_event_dtos(manifest)
    history = [
        {
            "source_type": item.event["source_original_origin"],
            "speaker_role": item.event["role"],
            "text": item.event["content"],
            "occurred_at": item.event["occurred_at"],
        }
        for item in events
    ]
    return {"history": history, "query": {"text": "TEST public raw-history query"}}


def _worker_stage(core: MemoryCore, context: TrustedContext, *, diagnostic_worker: Callable[[MemoryCore, TrustedContext], Mapping[str, Any]] | None) -> dict[str, Any]:
    if diagnostic_worker is not None:
        details = diagnostic_worker(core, context)
        if not isinstance(details, Mapping):
            raise OrchestratorError("diagnostic worker must return an object")
        return _stage("L2_organization", "DIAGNOSTIC", "injected_worker_is_not_formal_evidence", details)
    # Use the existing bounded worker interface without a model or embed port.
    # It may leave work pending/retry, but it cannot create claims or answers.
    receipt = core.drain_worker(context, owner_id="P18-preflight-worker", max_items=1, remaining_seconds=5)
    return _stage(
        "L2_organization",
        "DIAGNOSTIC",
        "normal_worker_interface_without_model_or_embed_port",
        {"processed": receipt.processed, "completed": receipt.completed, "retried": receipt.retried, "failed": receipt.failed},
    )


def _query_stage(
    host_id: str,
    root: Path,
    model_input: Mapping[str, Any],
    *,
    hermes_endpoint: str | None,
    diagnostic_hermes_transport: Callable[..., Any] | None,
) -> dict[str, Any]:
    if host_id == "codex_windows_desktop":
        return _stage("L3_recall", "UNSUPPORTED", "actual_codex_desktop_driver_not_configured", {"cli_substitution": False})
    if host_id != "hermes_a2a":
        raise OrchestratorError(f"unknown host: {host_id}")
    from p18_test_runner import HOSTS, HermesA2AHostAdapter

    if diagnostic_hermes_transport is not None:
        adapter = HermesA2AHostAdapter(
            HOSTS[0],
            endpoint="http://127.0.0.1:19921",
            isolation_root=root,
            transport=diagnostic_hermes_transport,
            test_transport=True,
        )
        result = adapter.execute_query({"ordinal": 1, "kind": "diagnostic_query", "model_input": model_input})
        return _stage("L3_recall", "DIAGNOSTIC", "injected_test_transport_is_not_formal_evidence", result)
    if hermes_endpoint is None:
        return _stage("L3_recall", "UNSUPPORTED", "actual_hermes_endpoint_not_configured", {"network_calls": 0, "model_calls": 0})
    adapter = HermesA2AHostAdapter(HOSTS[0], endpoint=hermes_endpoint, isolation_root=root)
    result = adapter.execute_query({"ordinal": 1, "kind": "actual_query", "model_input": model_input})
    return _stage("L3_recall", str(result.get("status", "FAIL")), result.get("error_type"), result)


def run_public_orchestration(
    run_root: str | Path,
    manifest: Mapping[str, Any] | None = None,
    *,
    hermes_endpoint: str | None = None,
    diagnostic_hermes_transport: Callable[..., Any] | None = None,
    diagnostic_worker: Callable[[MemoryCore, TrustedContext], Mapping[str, Any]] | None = None,
    hosts: tuple[str, ...] = HOSTS,
    arms: tuple[str, ...] = ARMS,
) -> dict[str, Any]:
    """Prepare independent TEST roots for a bounded public synthetic dry-run."""

    root = _safe_test_root(run_root)
    if root.exists() and any(root.iterdir()):
        raise OrchestratorError("orchestrator run root must be new and empty")
    if not hosts or not arms or any(host not in HOSTS for host in hosts) or any(arm not in ARMS for arm in arms):
        raise OrchestratorError("unsupported host or arm selection")
    if diagnostic_hermes_transport is not None and hermes_endpoint is not None:
        raise OrchestratorError("diagnostic transport and actual Hermes endpoint are mutually exclusive")
    source_manifest = dict(manifest or build_public_manifest())
    validated = validate_history_manifest(source_manifest)
    source_hash = validated.manifest_sha256
    model_input = _history_model_input(source_manifest)
    root.mkdir(parents=True, exist_ok=False)
    entries: list[dict[str, Any]] = []
    for host_id in hosts:
        for arm_id in arms:
            entry_root = root / host_id / f"arm-{arm_id}"
            data_directory = entry_root / "scope-recall"
            data_directory.mkdir(parents=True, exist_ok=False)
            binding = InstanceBinding(
                f"TEST-{host_id}-{arm_id}",
                f"TEST-installation-{host_id}-{arm_id}",
                data_directory,
                frozenset({f"TEST-scope-{host_id}-{arm_id}"}),
                True,
            )
            core = MemoryCore(CoreConfig(binding), clock=_FixedClock())
            core.initialize()
            scope_id = next(iter(binding.scope_ids))
            context = TrustedContext(binding, f"TEST-session-{host_id}-{arm_id}", binding.scope_ids, "human_direct")
            l1 = _stage("L1_record", "UNSUPPORTED", "arm_path_not_implemented")
            load_result = None
            if arm_id == "C":
                load_result = load_raw_history(core, context, source_manifest, scope_id=scope_id, arm_id=arm_id)
                l1 = _stage("L1_record", load_result.status, load_result.reason, {"records_seen": load_result.records_seen, "inserted": load_result.inserted, "duplicates": load_result.duplicates, "source_refs": load_result.source_refs})
            if arm_id == "C" and load_result is not None and load_result.status == "PASS":
                l2 = _worker_stage(core, context, diagnostic_worker=diagnostic_worker)
                l3 = _query_stage(host_id, entry_root, model_input, hermes_endpoint=hermes_endpoint, diagnostic_hermes_transport=diagnostic_hermes_transport)
            else:
                l2 = _stage("L2_organization", "UNSUPPORTED", "upstream_arm_not_supported")
                l3 = _stage("L3_recall", "UNSUPPORTED", "upstream_arm_not_supported")
            l4 = _stage("L4_behavior", "NOT_RUN", "model_and_final_behavior_execution_not_started")
            entries.append({
                "host_id": host_id,
                "arm_id": arm_id,
                "root": str(entry_root.resolve()),
                "data_directory": str(data_directory.resolve()),
                "binding": {"agent_id": binding.agent_id, "installation_id": binding.installation_id, "scope_id": scope_id, "test_mode": binding.test_mode},
                "source_manifest_sha256": source_hash,
                "stages": {"L1_record": l1, "L2_organization": l2, "L3_recall": l3, "L4_behavior": l4},
                "counts": _counts(core, context),
                "formal_evidence": False,
            })
    statuses = [stage["status"] for entry in entries for stage in entry["stages"].values()]
    return {
        "schema": "scope-recall.p18.orchestrator-preparation.v1",
        "status": "PREPARATION_PASS",
        "formal_execution_started": False,
        "network_calls": 0 if diagnostic_hermes_transport is None and hermes_endpoint is None else None,
        "model_calls": 0,
        "gold_read": False,
        "control_fields_in_model_input": False,
        "source_manifest_sha256": source_hash,
        "hosts": list(hosts),
        "arms": list(arms),
        "entry_count": len(entries),
        "all_roots_distinct": len({entry["root"] for entry in entries}) == len(entries),
        "all_bindings_distinct": len({entry["binding"]["installation_id"] for entry in entries}) == len(entries),
        "unsupported_or_not_run_statuses": sorted({status for status in statuses if status in {"UNSUPPORTED", "NOT_RUN", "DIAGNOSTIC"}}),
        "entries": entries,
        "formal_dependencies": [
            "sealed raw/gold fixture remains independent and was not opened",
            "actual Hermes TEST gateway endpoint and response capture",
            "actual Codex Windows desktop UI driver",
            "declared model/extractor route and shared ledger authorization",
        ],
    }


__all__ = ["ARMS", "HOSTS", "LAYER_NAMES", "OrchestratorError", "run_public_orchestration"]
