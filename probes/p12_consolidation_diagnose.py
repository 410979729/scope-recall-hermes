#!/usr/bin/env python3
"""One-shot, read-only-source diagnosis of a TEST consolidation rejection."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = Path(r"F:\SCOPERECALL更新项目\TEST-P12-AUTO-v5")
SOURCE_DB = SOURCE_ROOT / "data" / "memory.sqlite3"
DIAG_ROOT = Path(os.environ.get(
    "P12_DIAG_ROOT",
    r"F:\SCOPERECALL更新项目\TEST-P12-AUTO-v5-DIAG-FC8D481",
))
ADMISSION = DIAG_ROOT / "admission.json"
DIAG_DATA = DIAG_ROOT / "data"
DIAG_DB = DIAG_DATA / "memory.sqlite3"
LEDGER = Path(r"F:\SCOPERECALL更新项目\worktrees\scope-recall-v1.1\.execution\TEST-MODEL-BUDGET-V1\call-budget.sqlite3")
RUNTIME_CONFIG = SOURCE_ROOT / "data" / "runtime-config.json"
CANDIDATE_RECEIPT = ROOT / ".execution" / "TEST-CANDIDATE-fc8d481" / "build-receipt.json"
G2_DRIVER = ROOT / ".execution" / "TEST-CURSOR-APP-PROTOCOL-v1" / "run_g2_echo_fix.py"
P11_STARTER = ROOT / "probes" / "hermes" / "p11_start_a2a_test.py"
SOURCE_REF = "event-07985cac9d3541c9e4d26d89f56758db67688249113132402d7fbb46a5baf8d8"
SOURCE_REVISION = 1
BATCH = "P12_AUTO_V4"
MODEL = "mimo-v2.5"
MAX_CALLS = 1


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"module_loader:{name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"json_object_required:{path.name}")
    return value


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def ledger_snapshot() -> dict[str, Any]:
    uri = f"file:{LEDGER.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=2.0) as db:
        db.row_factory = sqlite3.Row
        requests = [dict(row) for row in db.execute(
            "SELECT id,batch,model,body_sha256,request_bytes,reserved_input,reserved_output,"
            "actual_input,actual_output,status,started_ns FROM requests ORDER BY id"
        )]
        codex = [dict(row) for row in db.execute(
            "SELECT operation_id,batch,model,request_sha256,request_bytes,reserved_input,"
            "reserved_output,actual_input,actual_output,status,dispatch_status,usage_quality,"
            "started_ns,finished_ns FROM codex_submissions ORDER BY operation_id"
        )]
    return {"requests": requests, "codexSubmissions": codex}


class CaptureTransport:
    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.request_body: bytes | None = None
        self.request_meta: dict[str, Any] | None = None
        self.response_status: int | None = None
        self.response_body: bytes | None = None

    def post(self, endpoint: str, *, body: bytes, headers: Mapping[str, str], timeout_seconds: float, max_response_bytes: int):
        self.request_body = bytes(body)
        self.request_meta = {
            "endpoint": endpoint,
            "method": "POST",
            "bodySha256": sha256(body),
            "bodyBytes": len(body),
            "headerNames": sorted(headers),
            "credentialValuePersisted": False,
        }
        status, response = self.delegate.post(
            endpoint,
            body=body,
            headers=headers,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
        )
        self.response_status = int(status)
        self.response_body = bytes(response)
        return status, response


def copy_source_database() -> None:
    DIAG_DATA.mkdir(parents=True, exist_ok=True)
    expected_directory = os.path.normcase(os.path.abspath(os.fspath(DIAG_DATA)))
    if DIAG_DB.exists():
        with sqlite3.connect(DIAG_DB) as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("diagnostic_database_invalid")
            meta = db.execute(
                "SELECT data_directory FROM instance_meta WHERE singleton=1"
            ).fetchone()
            if meta is None or Path(meta[0]).resolve() != DIAG_DATA.resolve():
                raise RuntimeError("diagnostic_database_binding_mismatch")
            if meta[0] != expected_directory:
                db.execute("UPDATE instance_meta SET data_directory=? WHERE singleton=1", (expected_directory,))
        return
    source_uri = f"file:{SOURCE_DB.as_posix()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True, timeout=2.0) as source, sqlite3.connect(DIAG_DB) as target:
        source.backup(target)
    with sqlite3.connect(DIAG_DB) as db, db:
        db.execute("UPDATE instance_meta SET data_directory=? WHERE singleton=1", (expected_directory,))


def source_and_roots(core: Any, context: Any, worker: Any) -> tuple[Any, tuple[Any, ...], str | None, list[dict[str, Any]]]:
    with core.storage.read(context) as tx:
        source = tx.source(SOURCE_REF, SOURCE_REVISION)
        if source is None:
            raise RuntimeError("diagnostic_source_missing")
        episode_ref, batch, needs_model = worker._episode_batch(tx, source)
        roots = worker._root_only_sources(tx, batch)
        messages = worker.consolidation_messages(roots, episode_ref=episode_ref)
    return source, roots, episode_ref, messages


def main() -> int:
    if not ADMISSION.is_file() or not SOURCE_DB.is_file() or not RUNTIME_CONFIG.is_file():
        raise RuntimeError("diagnostic_prerequisite_missing")
    if any((DIAG_ROOT / name).exists() for name in ("result.json", "failure.json", "model-response.raw")):
        raise RuntimeError("diagnostic_receipt_exists_no_retry")
    admission = read_json(ADMISSION)
    if admission.get("status") not in {
        "AUTHORIZED_SINGLE_MIMO_CALL",
        "AUTHORIZED_SINGLE_MIMO_CALL_FOLLOWUP",
    } or admission.get("model", {}).get("maxCalls") != MAX_CALLS:
        raise RuntimeError("diagnostic_admission_invalid")
    before_source_sha = sha256(SOURCE_DB.read_bytes())
    before_ledger = ledger_snapshot()
    prior_auxiliary_ids = admission["priorLedger"]["auxiliaryRequestIds"]
    prior_auxiliary_id_set = set(prior_auxiliary_ids)
    observed_batch_rows = [row for row in before_ledger["requests"] if row["batch"] == BATCH]
    observed_batch_ids = {row["id"] for row in observed_batch_rows}
    if not prior_auxiliary_id_set.issubset(observed_batch_ids):
        raise RuntimeError("prior_auxiliary_ledger_rows_missing")
    concurrent_batch_rows = [row for row in observed_batch_rows if row["id"] not in prior_auxiliary_id_set]
    candidate = read_json(CANDIDATE_RECEIPT)
    if candidate.get("source_commit") != admission["candidateSourceCommit"]:
        raise RuntimeError("candidate_commit_mismatch")
    if candidate.get("wheel_sha256") != admission["candidateWheelSha256"]:
        raise RuntimeError("candidate_wheel_mismatch")
    g2 = load_module("p12_diagnostic_g2", G2_DRIVER)
    installed = g2.verify_candidate(CANDIDATE_RECEIPT)
    if installed.get("mismatches") or installed.get("package_files_checked") != 109:
        raise RuntimeError("candidate_installed_files_mismatch")
    copy_source_database()

    # Keep the package import pinned to the installed candidate.  Root is added
    # only afterwards so the TEST key loader can be imported; it cannot replace
    # the already loaded installed package.
    import scope_recall
    installed_root = Path(scope_recall.__file__).resolve().parent
    expected_root = (Path(sys.prefix) / "Lib" / "site-packages" / "scope_recall").resolve()
    if installed_root != expected_root:
        raise RuntimeError("installed_runtime_root_mismatch")
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from scope_recall.adapters.models import HttpsTransport
    from scope_recall.runtime.auxiliary import build_auxiliary_runtime
    from scope_recall.contracts import ContractError
    from scope_recall.core import CoreConfig, MemoryCore
    from scope_recall.core.consolidate import ConsolidationWorkFence, accept_consolidation, consolidation_messages
    from scope_recall.core.mutate import validate_claims
    from scope_recall.runtime.instance import RuntimeInstanceConfig
    from scope_recall.core import worker as worker_module

    raw_config = read_json(RUNTIME_CONFIG)
    raw_config["binding"]["data_directory"] = str(DIAG_DATA)
    raw_config["auxiliary"]["installation_dir"] = str(DIAG_DATA)
    raw_config.pop("vector", None)
    config = RuntimeInstanceConfig.from_mapping(raw_config)
    core = MemoryCore(CoreConfig(config.binding))
    context = config.context()
    source, roots, episode_ref, messages = source_and_roots(core, context, worker_module)
    if not roots:
        raise RuntimeError("diagnostic_roots_empty")
    route = config.auxiliary.consolidation
    if route is None:
        raise RuntimeError("consolidation_route_unavailable")
    request_payload = {
        "model": route.model,
        "messages": messages,
        "stream": route.stream,
        "n": route.n,
        route.output_limit_field: route.max_output_tokens,
    }
    if route.thinking is not None:
        request_payload["thinking"] = dict(route.thinking)
    if route.response_format is not None:
        request_payload["response_format"] = dict(route.response_format)
    if route.reasoning_effort is not None:
        request_payload["reasoning_effort"] = route.reasoning_effort
    planned_body = json.dumps(request_payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    planned_body_sha = sha256(planned_body)
    if any(
        row["model"] == MODEL and row["body_sha256"] == planned_body_sha
        for row in concurrent_batch_rows
    ):
        raise RuntimeError("diagnostic_operation_already_reserved")
    DIAG_ROOT.mkdir(parents=True, exist_ok=True)
    write_json(DIAG_ROOT / "request.json", request_payload)
    write_json(DIAG_ROOT / "roots.json", {
        "subjectRef": SOURCE_REF,
        "subjectRevision": SOURCE_REVISION,
        "episodeRef": episode_ref,
        "rootRefs": [f"{item.ref}@{item.revision}" for item in roots],
        "sourceCount": len(roots),
    })
    loader = load_module("p12_diagnostic_p11_loader", P11_STARTER)
    key = loader._load_authorized_test_key()
    previous_key = __import__("os").environ.get("SCOPE_RECALL_TEST_OPENCODE_GO_API_KEY")
    __import__("os").environ["SCOPE_RECALL_TEST_OPENCODE_GO_API_KEY"] = key
    transport = CaptureTransport(HttpsTransport())
    auxiliary = build_auxiliary_runtime(config.auxiliary, transport=transport)
    if auxiliary.consolidation is None:
        raise RuntimeError("consolidation_route_unavailable")
    status = "unknown"
    acceptance: dict[str, Any] = {}
    content: str | None = None
    try:
        try:
            content = auxiliary.consolidation.propose(messages, remaining_seconds=45.0)
        except Exception as exc:
            acceptance.update({"status": "model_call_failed", "errorType": type(exc).__name__})
            status = "model_call_failed"
            return 0
        (DIAG_ROOT / "model-response.raw").write_text(content, encoding="utf-8")
        try:
            value = json.loads(content)
            from scope_recall.contracts import validate_payload
            value = validate_payload("consolidation_result", value)
            acceptance["schema"] = "accepted"
            with core.storage.read(context) as tx:
                validate_claims(tx, value, source.scope_id)
            acceptance["validateClaims"] = "accepted"
            with sqlite3.connect(DIAG_DB) as db, db:
                db.execute(
                    "UPDATE work_items SET state='leased',lease_owner='diagnostic',lease_until=?,lease_token=2 WHERE work_id=1",
                    ((datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),),
                )
            with core.storage.read(context) as tx:
                epoch = tx.status().memory_epoch
            fence = ConsolidationWorkFence(
                1, 2, "diagnostic", SOURCE_REF, SOURCE_REVISION, epoch,
                frozenset(f"{item.ref}@{item.revision}" for item in roots),
            )
            accept_consolidation(core.storage, core.clock, context, value, scope_id=source.scope_id, remaining_seconds=45.0, work_fence=fence)
            acceptance["acceptConsolidation"] = "accepted"
            status = "accepted_no_rejection_reproduced"
        except ContractError as exc:
            acceptance.update({"status": "rejected", "code": exc.code, "field": exc.field})
            status = "rejection_reproduced"
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            acceptance.update({"status": "parse_or_schema_rejected", "errorType": type(exc).__name__})
            status = "parse_or_schema_rejected"
    finally:
        key = ""
        if previous_key is None:
            __import__("os").environ.pop("SCOPE_RECALL_TEST_OPENCODE_GO_API_KEY", None)
        else:
            __import__("os").environ["SCOPE_RECALL_TEST_OPENCODE_GO_API_KEY"] = previous_key
        if transport.response_body is not None and not (DIAG_ROOT / "model-response.raw").exists():
            (DIAG_ROOT / "model-response.raw").write_bytes(transport.response_body)
        if transport.response_body is not None:
            (DIAG_ROOT / "http-response.raw").write_bytes(transport.response_body)
        after_ledger = ledger_snapshot()
        after_source_sha = sha256(SOURCE_DB.read_bytes())
        if transport.request_body is not None:
            write_json(DIAG_ROOT / "request-capture.json", {
                **(transport.request_meta or {}),
                "body": json.loads(transport.request_body.decode("utf-8")),
            })
        write_json(DIAG_ROOT / "ledger-before-after.json", {"before": before_ledger, "after": after_ledger})
        receipt = {
            "schema": "p12-auto-v5-consolidation-diagnosis.result.v1",
            "status": status,
            "admission": str(ADMISSION),
            "candidate": installed,
            "source": {
                "eventId": SOURCE_REF,
                "revision": SOURCE_REVISION,
                "databaseSha256Before": before_source_sha,
                "databaseSha256After": after_source_sha,
                "originalDatabaseUnchanged": before_source_sha == after_source_sha,
            },
            "ledgerPrecondition": {
                "priorAuxiliaryRequestIds": prior_auxiliary_ids,
                "observedConcurrentBatchRows": concurrent_batch_rows,
                "concurrentModelReservationAbsent": not any(
                    row["model"] == MODEL and row["body_sha256"] == planned_body_sha
                    for row in concurrent_batch_rows
                ),
            },
            "modelCall": {
                "batch": BATCH,
                "model": MODEL,
                "maxCalls": MAX_CALLS,
                "plannedRequestBodySha256": planned_body_sha,
                "requestBodySha256": sha256(transport.request_body) if transport.request_body is not None else None,
                "responseSha256": sha256(transport.response_body) if transport.response_body is not None else sha256((content or "").encode("utf-8")),
                "responseBytes": len(transport.response_body or (content or "").encode("utf-8")),
                "httpStatus": transport.response_status,
            },
            "acceptance": acceptance,
            "rawArtifacts": {
                "request": "request.json",
                "requestCapture": "request-capture.json",
                "response": "model-response.raw",
                "httpResponse": "http-response.raw",
                "roots": "roots.json",
                "ledger": "ledger-before-after.json",
            },
            "constraints": {
                "originalSourceWorkChanged": False,
                "originalClaimsChanged": False,
                "newCodexRequests": 0,
                "automaticRetry": False,
                "credentialsPersisted": False,
            },
        }
        write_json(DIAG_ROOT / "result.json", receipt)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "FAILED", "errorType": type(exc).__name__}, ensure_ascii=True))
        raise
