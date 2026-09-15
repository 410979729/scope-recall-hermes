"""Bounded P18 transport integration for public preparation inputs.

The runner owns admission, per-operation artifact retention, and receipt
linkage.  It does not score answers and it never turns a fixture exchange into
formal evidence.  A future host owner can pass an already configured real
transport to :func:`run_transport_operations` after the formal freeze gate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from p18_arm_provision import load_public_fixture
from p18_codex_appserver_transport import ARM_HOOK_POLICIES, DEFAULT_TIMEOUT_SECONDS, EFFORT, MODEL, CodexAppServerTransport, CodexTransportConfig
from p18_formal_evidence import EvidenceSchemaError, FormalEvidenceWriter, FormalReadiness, verify_formal_run_config
from p18_hermes_a2a_transport import HermesA2ATransport, HermesHTTPResponse, HermesTransportConfig
from p18_hermes_operation_budget import HermesOperationBudget
from p18_ledger_reference import ledger_path_reference


class FormalRunnerError(ValueError):
    """Invalid or unsafe runner input."""


class DiagnosticBudget:
    """In-memory diagnostic budget; it is never the shared formal ledger."""

    def __init__(self) -> None:
        self.reservations: dict[str, dict[str, Any]] = {}
        self.finished: list[dict[str, Any]] = []

    def reserve(self, model: str, request: bytes) -> str:
        operation_id = "diagnostic-" + hashlib.sha256(request).hexdigest()[:24]
        self.reservations[operation_id] = {"model": model, "request_sha256": hashlib.sha256(request).hexdigest()}
        return operation_id

    def finish(self, reservation: str, status: str, usage: Mapping[str, int] | None) -> str:
        if reservation not in self.reservations:
            raise FormalRunnerError("diagnostic_reservation_unknown")
        self.finished.append({"reservation": reservation, "status": status, "usage_known": usage is not None})
        return status


class CodexLedgerBudgetAdapter:
    """Adapt the durable Codex ledger to the transport's narrow budget port.

    The operation id is supplied by the frozen manifest before each turn.  No
    table is created here; the caller must provide an already initialized
    ``CodexSubmissionBudget`` and the formal verifier still binds the rows.
    """

    def __init__(self, backend: Any, *, operation_root: Path, config_root: Path) -> None:
        self.backend = backend
        self.operation_root = operation_root
        self.config_root = config_root
        self.operation_id: str | None = None
        self.ledger_operation_id: str | None = None
        self.request_path: Path | None = None
        self.last_row: Mapping[str, Any] | None = None

    def begin_operation(self, operation_id: str) -> None:
        self.operation_id = operation_id
        # A new isolated attempt must reserve independently of earlier failed
        # attempts of the same frozen logical operation. Never reset old rows.
        attempt = _sha256(str(self.operation_root.resolve()).casefold().encode('utf-8'))[:24]
        self.ledger_operation_id = f'{operation_id}@{attempt}'
        self.request_path = None
        self.last_row = None

    def reserve(self, model: str, request: bytes) -> Any:
        if self.operation_id is None:
            raise FormalRunnerError("codex_budget_operation_not_bound")
        operation_dir = self.operation_root / self.operation_id
        operation_dir.mkdir(parents=True, exist_ok=True)
        request_path = operation_dir / "model-request.bin"
        _write_new(request_path, request)
        self.request_path = request_path
        self.last_row = self.backend.reserve(self.ledger_operation_id, request, model=model)
        return self.last_row

    def finish(self, reservation: Any, status: str, usage: Mapping[str, int] | None) -> str:
        if self.operation_id is None:
            raise FormalRunnerError("codex_budget_operation_not_bound")
        self.last_row = self.backend.finish(self.ledger_operation_id, status, usage)
        return str(self.last_row.get("status", status)) if isinstance(self.last_row, Mapping) else status

    def formal_usage(self) -> dict[str, Any] | None:
        if self.operation_id is None or self.request_path is None or not isinstance(self.last_row, Mapping):
            return None
        actual_input = self.last_row.get("actual_input")
        actual_output = self.last_row.get("actual_output")
        known = isinstance(actual_input, int) and isinstance(actual_output, int)
        return {
            "status": "known" if known else "unknown",
            "input_tokens": actual_input if known else None,
            "output_tokens": actual_output if known else None,
            "ledger_path": ledger_path_reference(Path(self.backend.path), self.config_root),
            "entries": [{
                "id": self.ledger_operation_id,
                "request": {
                    "path": str(self.request_path.resolve().relative_to(self.config_root)),
                    "sha256": _sha256(self.request_path.read_bytes()),
                },
            }],
        }


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _test_root(value: str | Path) -> Path:
    root = Path(value).expanduser().resolve()
    lowered = str(root).replace("/", "\\").lower()
    if not root.name.lower().startswith(("test-", "test_")) or lowered == "f:\\agents" or lowered.startswith("f:\\agents\\"):
        raise FormalRunnerError("output_root_must_be_isolated_TEST_path")
    if root.exists() and any(root.iterdir()):
        raise FormalRunnerError("output_root_must_be_new_and_empty")
    root.mkdir(parents=True, exist_ok=False)
    return root


def _write_new(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(payload)


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _fixture_exchange(path: str | Path) -> tuple[Callable[..., HermesHTTPResponse], Mapping[str, str], list[bytes]]:
    fixture_path = Path(path).expanduser().resolve()
    value = json.loads(fixture_path.read_text(encoding="utf-8"))
    if type(value) is not dict or type(value.get("agent_card")) is not dict or type(value.get("response")) is not dict:
        raise FormalRunnerError("Hermes exchange fixture requires agent_card and response objects")
    card = value["agent_card"]
    response = _json_bytes(value["response"])
    captured: list[bytes] = []
    identity = {key: card[key] for key in ("name", "version") if isinstance(card.get(key), str) and card[key]}
    if not identity:
        raise FormalRunnerError("Hermes exchange fixture has no agent-card identity")

    def exchange(method: str, url: str, body: bytes | None, timeout: float) -> HermesHTTPResponse:
        if method == "GET":
            body = _json_bytes(card)
            captured.append(body)
            return HermesHTTPResponse(200, body, {"content-type": "application/json"})
        if method == "POST":
            captured.append(response)
            return HermesHTTPResponse(200, response, {"content-type": "application/json"})
        raise FormalRunnerError("unsupported_fixture_method")

    return exchange, identity, captured


def _record_operation(
    writer: FormalEvidenceWriter,
    operation_id: str,
    arm_id: str,
    unit_id: str,
    *,
    request_id: str | None,
    turn_id: str | None,
    session_id: str | None,
    source_capture_refs: Sequence[str],
    response_bytes: bytes,
    answer_text: str | None,
    transport: str,
) -> Path:
    operation_dir = writer.root / "operations" / operation_id
    response_path = operation_dir / "response.bin"
    _write_new(response_path, response_bytes)
    response_digest = _sha256(response_bytes)
    if answer_text:
        answer_bytes = answer_text.encode("utf-8")
        answer_path = operation_dir / "answer.txt"
        _write_new(answer_path, answer_bytes)
        answer = {"status": "available", "answer_sha256": _sha256(answer_bytes), "artifact_path": str(answer_path.relative_to(writer.root))}
    else:
        answer = {"status": "not_attempted", "answer_sha256": None, "artifact_path": None}
    unknown = [key for key, value in (("request_id", request_id), ("turn_id", turn_id), ("session_id", session_id)) if value is None]
    record = {
        "operation_id": operation_id,
        "attempt": 1,
        "host_id": transport,
        "arm_id": arm_id,
        "unit": {"kind": "query", "query_id": unit_id, "journey_id": None, "round_id": None},
        "ids": {"request_id": request_id, "turn_id": turn_id, "session_id": session_id, "unknown": unknown},
        "source_capture_refs": list(source_capture_refs),
        "delivery": {"status": "not_attempted", "context_sha256": None, "artifact_path": None},
        "answer": answer,
        "usage": {"status": "not_applicable", "input_tokens": None, "output_tokens": None, "ledger_path": None, "reservation": None},
        "status": "DIAGNOSTIC",
        "latency_ms": 0.0,
        "no_retry": True,
        "host_response": {
            "response_sha256": response_digest,
            "artifact_path": str(response_path.relative_to(writer.root)),
            "http_status": 200,
            "transport": transport,
            "real_host": False,
        },
        "association": {"diagnostic": True},
    }
    return writer.append_operation(record, diagnostic=True)


def run_public_diagnostic(
    *,
    fixture_path: str | Path,
    output_root: str | Path,
    arms: Sequence[str] = ("A", "B", "C", "D"),
    hermes_exchange_fixture: str | Path | None = None,
    codex_fixture: str | Path | None = None,
) -> dict[str, Any]:
    """Run one bounded public probe per selected arm through a diagnostic seam."""
    rows, fixture_sha = load_public_fixture(Path(fixture_path).expanduser().resolve())
    selected = tuple(arms)
    if not selected or any(arm not in {"A", "B", "C", "D"} for arm in selected):
        raise FormalRunnerError("unsupported_arm")
    if (hermes_exchange_fixture is None) == (codex_fixture is None):
        raise FormalRunnerError("exactly_one_diagnostic_transport_fixture_required")
    root = _test_root(output_root)
    writer = FormalEvidenceWriter(root)
    receipts: list[dict[str, Any]] = []
    if hermes_exchange_fixture is not None:
        exchange, identity, captured = _fixture_exchange(hermes_exchange_fixture)
        budget = DiagnosticBudget()
        transport = HermesA2ATransport(
            HermesTransportConfig(
                endpoint="http://127.0.0.1:19921",
                expected_agent_card_identity=identity,
                isolation_root=root,
                allow_diagnostic_fixture=True,
            ),
            budget,
            exchange=exchange,
        )
        for index, arm_id in enumerate(selected):
            row = rows[index % len(rows)]
            operation_id = f"hermes-{arm_id}-{index + 1:03d}"
            transport_receipt = transport.execute(row["query"]["text"], context_id=f"TEST-context-{arm_id}", message_id=f"TEST-message-{operation_id}")
            response = captured[-1] if captured else _json_bytes(transport_receipt)
            # A2A contextId is retained inside the transport receipt; it is
            # not a Hermes session identifier and must not be promoted here.
            receipt_path = _record_operation(writer, operation_id, arm_id, operation_id, request_id=transport_receipt.get("message_id"), turn_id=transport_receipt.get("task_id"), session_id=None, source_capture_refs=[], response_bytes=response, answer_text=transport_receipt.get("answer_text"), transport="hermes_a2a_fixture")
            receipts.append({"operation_id": operation_id, "arm_id": arm_id, "transport_receipt": transport_receipt, "evidence_path": str(receipt_path)})
    else:
        fixture = Path(codex_fixture).expanduser().resolve()
        if not fixture.is_file():
            raise FormalRunnerError("codex_fixture_missing")
        raw_fixture = fixture.read_bytes()
        for index, arm_id in enumerate(selected):
            row = rows[index % len(rows)]
            operation_id = f"codex-{arm_id}-{index + 1:03d}"
            transport = CodexAppServerTransport.from_fixture(fixture, arm_id=arm_id)
            transport_receipt = transport.run_turn(row["query"]["text"])
            output = transport_receipt.get("public_output") if isinstance(transport_receipt.get("public_output"), str) else None
            association = transport_receipt.get("association") if isinstance(transport_receipt.get("association"), Mapping) else {}
            receipt_path = _record_operation(writer, operation_id, arm_id, operation_id, request_id=None, turn_id=association.get("turn_id"), session_id=association.get("thread_id"), source_capture_refs=[], response_bytes=raw_fixture, answer_text=output, transport="codex_appserver_fixture")
            receipts.append({"operation_id": operation_id, "arm_id": arm_id, "transport_receipt": transport_receipt, "evidence_path": str(receipt_path)})
    result = {
        "schema": "scope-recall.p18-formal-runner-diagnostic.v1",
        "status": "DIAGNOSTIC_ONLY",
        "formal_pass": False,
        "fixture_sha256": fixture_sha,
        "arms": list(selected),
        "receipts": receipts,
        "semantic_scoring": "independent_scorer_required",
        "network_calls": 0,
        "model_calls": 0,
    }
    _write_new(root / "run-receipt.json", (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode())
    return result


def run_transport_operations(
    *,
    config_path: str | Path,
    output_root: str | Path,
    transport: Any,
    queries: Sequence[Mapping[str, Any]],
    arm_id: str,
) -> dict[str, Any]:
    """Run caller-supplied real transport only after formal freeze verification.

    The caller owns the real host and budget adapter.  This function merely
    binds the config, retains bounded response metadata, and leaves scoring to
    an independent consumer.
    """
    readiness = verify_formal_run_config(config_path)
    if not readiness.formal_execution_allowed:
        return {"status": "NOT_READY", "formal_pass": False, "reasons": list(readiness.reasons)}
    root = _test_root(output_root)
    config_root = Path(readiness.details.get("config_path", config_path)).expanduser().resolve().parent
    try:
        root.relative_to(config_root)
    except ValueError as exc:
        raise FormalRunnerError("formal_output_must_stay_under_config_root") from exc
    writer = FormalEvidenceWriter(root, config_path)
    frozen_operations = readiness.details.get("operations")
    if not isinstance(frozen_operations, Mapping):
        raise FormalRunnerError("formal_operation_map_missing")
    receipts: list[dict[str, Any]] = []
    for index, item in enumerate(queries):
        if not isinstance(item, Mapping):
            raise FormalRunnerError("operation_manifest_entry_required")
        query = item.get("query")
        if not isinstance(query, str) or not query.strip():
            raise FormalRunnerError("query_text_required")
        query_digest = item.get("query_sha256")
        if not isinstance(query_digest, str) or query_digest != _sha256(query.encode("utf-8")):
            raise FormalRunnerError("query_hash_binding_required")
        source_refs = item.get("source_capture_refs")
        if not isinstance(source_refs, list) or any(not isinstance(ref, str) or not ref.strip() for ref in source_refs):
            raise FormalRunnerError("source_capture_refs_required")
        operation_id = item.get("operation_id")
        if not isinstance(operation_id, str) or operation_id not in frozen_operations:
            raise FormalRunnerError("operation_id_not_in_frozen_config")
        frozen = frozen_operations[operation_id]
        if not isinstance(frozen, Mapping) or frozen.get("arm_id") != arm_id:
            raise FormalRunnerError("operation_arm_mismatch")
        operation_dir = root / "operations" / operation_id
        operation_dir.mkdir(parents=True, exist_ok=False)
        raw_path = operation_dir / "response.bin"
        transport_raw_path = raw_path
        if isinstance(transport, HermesA2ATransport):
            try:
                raw_path.relative_to(transport.config.isolation_root)
            except ValueError:
                transport_raw_path = transport.config.isolation_root / ".p18-raw" / operation_id / "response.bin"
        started = time.perf_counter()
        budget = getattr(transport, "budget", None)
        if hasattr(budget, "begin_operation"):
            budget.begin_operation(operation_id)
        if isinstance(transport, HermesA2ATransport) and hasattr(budget, "set_operation"):
            budget.set_operation(
                operation_id=operation_id,
                request_id=str(frozen["request_id"]),
                task_id=str(item.get("task_id") or operation_id),
                context_id=str(item.get("context_id") or operation_id),
            )
        try:
            if isinstance(transport, HermesA2ATransport):
                session_id = item.get("session_id")
                if not isinstance(session_id, str) or not session_id.strip():
                    raise FormalRunnerError("hermes_actual_session_id_required")
                result = transport.execute(
                    query,
                    context_id=str(item.get("context_id") or operation_id),
                    message_id=str(frozen["request_id"]),
                    raw_response_path=transport_raw_path,
                )
            elif isinstance(transport, CodexAppServerTransport):
                result = transport.run_turn(query, thread_id=item.get("thread_id"), raw_capture_path=raw_path)
            else:
                raise FormalRunnerError("unsupported_transport_type")
        except Exception as exc:
            result = {
                "status": "FAILED",
                "formal_evaluation": False,
                "fixture_mode": False,
                "error": {"type": type(exc).__name__, "reason": str(exc)[:256]},
            }
        if hasattr(budget, "formal_usage"):
            formal_usage = budget.formal_usage()
            if formal_usage is not None:
                result = dict(result)
                result["formal_usage"] = formal_usage
        if transport_raw_path != raw_path and transport_raw_path.is_file() and not raw_path.exists():
            _write_new(raw_path, transport_raw_path.read_bytes())
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        transport_receipt_path = operation_dir / "transport-receipt.json"
        _write_new(transport_receipt_path, (json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))
        evidence_item = dict(item)
        evidence_item["request_id"] = str(frozen["request_id"])
        if isinstance(transport, HermesA2ATransport):
            try:
                observed = _observe_hermes_session(evidence_item, result, config_root=config_root)
                evidence_item["session_id"] = observed["session_id"]
                result = dict(result)
                result["session_observation"] = observed
            except FormalRunnerError as exc:
                result = dict(result)
                errors = list(result.get("errors") or [])
                errors.append({"kind": "association", "error_type": str(exc)[:128]})
                result["errors"] = errors
        evidence_path = _append_formal_transport_operation(
            writer,
            readiness,
            frozen=frozen,
            item=evidence_item,
            result=result,
            response_path=transport_receipt_path,
            latency_ms=latency_ms,
            transport=transport,
        )
        receipts.append({"operation_id": operation_id, "transport_receipt": result, "evidence_path": str(evidence_path)})
    result = {
        "schema": "scope-recall.p18-formal-runner.v1",
        "status": "EXECUTED",
        "formal_pass": False,
        "resume": {"supported": False, "reason": "operation_attempts_are_append_only_and_must_be_restarted_with_a_new_output_root"},
        "receipts": receipts,
        "semantic_scoring": "independent_scorer_required",
    }
    _write_new(root / "run-receipt.json", (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode())
    return result


def _observe_hermes_session(item: Mapping[str, Any], result: Mapping[str, Any], *, config_root: Path) -> dict[str, Any]:
    """Read the isolated Hermes routing DB using its JSON-backed schema.

    Hermes stores routing rows as ``(scope, session_key, entry_json,
    updated_at)``.  The session identifier is a field in ``entry_json``;
    ``context_id`` is used only to select the row and is never promoted to a
    session identifier.
    """
    raw_path = item.get("session_db")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise FormalRunnerError("hermes_session_db_required")
    db_path = Path(raw_path).expanduser().resolve()
    try:
        db_path.relative_to(config_root)
    except ValueError as exc:
        raise FormalRunnerError("hermes_session_db_outside_config_root") from exc
    if not db_path.is_file():
        raise FormalRunnerError("hermes_session_db_missing")
    import sqlite3

    try:
        with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=2) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(gateway_routing)").fetchall()}
            required = {"scope", "session_key", "entry_json", "updated_at"}
            if not required.issubset(columns):
                raise FormalRunnerError("hermes_gateway_routing_schema_mismatch")
            if len(columns) > 16:
                raise FormalRunnerError("hermes_gateway_routing_schema_unexpected")
            # This table is normally tiny.  Bound the read so a corrupted DB
            # cannot turn association into an unbounded formal-run operation.
            rows = db.execute(
                'SELECT "scope", "session_key", "entry_json", "updated_at" '
                'FROM gateway_routing ORDER BY "updated_at" DESC LIMIT 257'
            ).fetchall()
    except sqlite3.Error as exc:
        raise FormalRunnerError("hermes_gateway_routing_read_failed") from exc

    if len(rows) > 256:
        raise FormalRunnerError("hermes_gateway_routing_too_many_rows")
    supplied_key = item.get("session_key")
    if supplied_key is not None and (not isinstance(supplied_key, str) or not supplied_key.strip()):
        raise FormalRunnerError("hermes_gateway_session_key_invalid")
    context_id = result.get("context_id") or item.get("context_id")
    if not isinstance(context_id, str) or not context_id.strip():
        raise FormalRunnerError("hermes_gateway_routing_context_id_required")

    candidates: list[dict[str, Any]] = []
    for scope, table_key, raw_entry, updated_at in rows:
        if not isinstance(table_key, str) or not table_key.strip() or not isinstance(raw_entry, str):
            continue
        try:
            entry = json.loads(raw_entry)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(entry, Mapping):
            continue
        entry_key = entry.get("session_key")
        session_id = entry.get("session_id")
        platform = entry.get("platform")
        origin = entry.get("origin")
        origin_chat_id = origin.get("chat_id") if isinstance(origin, Mapping) else None
        if (
            not isinstance(entry_key, str) or not entry_key.strip() or entry_key != table_key
            or not isinstance(session_id, str) or not session_id.strip()
            or session_id == context_id
            or not isinstance(platform, str) or not platform.strip()
        ):
            continue
        selected = supplied_key == table_key if isinstance(supplied_key, str) else False
        if not selected and isinstance(origin_chat_id, str) and origin_chat_id == context_id:
            selected = True
        if not selected:
            # Some routing entries expose the context only in the final key
            # component.  This is selection metadata, never the session id.
            selected = table_key.rsplit(":", 1)[-1] == context_id
        if selected:
            candidates.append({"session_id": session_id, "session_key": table_key, "platform": platform, "scope": scope, "updated_at": updated_at})
    if len(candidates) != 1:
        raise FormalRunnerError("hermes_gateway_routing_session_not_unique")
    observed = candidates[0]
    if isinstance(supplied_key, str) and supplied_key != observed["session_key"]:
        raise FormalRunnerError("hermes_gateway_session_key_mismatch")
    if item.get("platform") is not None and item.get("platform") != observed["platform"]:
        raise FormalRunnerError("hermes_gateway_platform_mismatch")
    return {key: observed[key] for key in ("session_id", "session_key", "platform")} | {"db_path": str(db_path)}


def _append_formal_transport_operation(
    writer: FormalEvidenceWriter,
    readiness: FormalReadiness,
    *,
    frozen: Mapping[str, Any],
    item: Mapping[str, Any],
    result: Mapping[str, Any],
    response_path: Path | None,
    latency_ms: float,
    transport: Any,
) -> Path:
    """Convert one real transport receipt to execution evidence.

    A transport completion is deliberately downgraded to FAILED unless the
    caller supplies the immutable ledger entries required by the evidence
    contract.  This keeps the runner from turning a successful HTTP/RPC reply
    into a formal model-execution claim without ledger provenance.
    """
    operation_id = str(frozen.get("operation_id") or item.get("operation_id"))
    config_root = Path(readiness.details.get("config_path", "")).expanduser().resolve().parent
    if response_path is not None:
        try:
            response_path.relative_to(config_root)
        except ValueError as exc:
            raise FormalRunnerError("response_artifact_outside_config_root") from exc
        response_sha = _sha256(response_path.read_bytes())
        response_ref = str(response_path.relative_to(config_root))
    else:
        response_sha = None
        response_ref = None
    association = result.get("association") if isinstance(result.get("association"), Mapping) else {}
    request_id = str(frozen.get("request_id"))
    turn_id = item.get("turn_id") or association.get("turn_id")
    session_id = item.get("session_id")
    unknown = [key for key, value in (("request_id", request_id), ("turn_id", turn_id), ("session_id", session_id)) if not isinstance(value, str) or not value.strip()]
    answer_text = result.get("answer_text") or result.get("public_output")
    if not isinstance(answer_text, str) or not answer_text:
        answer_text = None
    operation_dir = writer.root / "operations" / operation_id
    answer_path = operation_dir / "answer.txt"
    if answer_text is not None and not answer_path.exists():
        _write_new(answer_path, answer_text.encode("utf-8"))
    answer = {
        "status": "available" if answer_text is not None else "not_attempted",
        "answer_sha256": _sha256(answer_path.read_bytes()) if answer_path.is_file() else None,
        "artifact_path": str(answer_path.relative_to(config_root)) if answer_path.is_file() else None,
    }
    receipt_is_real = (
        result.get("formal_evaluation") is True
        and result.get("fixture_mode") is not True
        and (result.get("process", {}).get("pid") is not None if isinstance(result.get("process"), Mapping) else item.get("owned_process") is True)
    )
    transport_status = result.get("transport_status") or result.get("status")
    requested_completed = receipt_is_real and transport_status in {"COMPLETED", "PASS", "PASS_USAGE_UNKNOWN"} and not result.get("errors")
    usage = item.get("usage") or result.get("formal_usage")
    if requested_completed and not isinstance(usage, Mapping):
        status = "FAILED"
        usage = {"status": "not_applicable", "input_tokens": None, "output_tokens": None, "ledger_path": None, "entries": []}
    elif requested_completed:
        status = "COMPLETED"
    else:
        status = "FAILED"
        if not isinstance(usage, Mapping):
            usage = {"status": "not_applicable", "input_tokens": None, "output_tokens": None, "ledger_path": None, "entries": []}
    unit = frozen.get("unit")
    if not isinstance(unit, Mapping):
        raise FormalRunnerError("frozen_unit_missing")
    accounted_entries = usage.get("entries") if isinstance(usage, Mapping) else []
    request_hashes = [entry.get("request", {}).get("sha256") for entry in accounted_entries if isinstance(entry, Mapping)]
    association_payload = {
        "operation_id": operation_id,
        "source": readiness.details.get("source"),
        "ids": {"request_id": request_id, "turn_id": turn_id if isinstance(turn_id, str) and turn_id else None, "session_id": session_id if isinstance(session_id, str) and session_id else None, "unknown": unknown},
        "source_capture_refs": list(item.get("source_capture_refs") or []),
        "request_sha256s": request_hashes,
        "ledger_entries": [
            {"route": "go" if frozen.get("host_id") in {"hermes_a2a", "hermes_cli_local_input_v1"} else "codex", "id": entry.get("id"), "request_sha256": entry.get("request", {}).get("sha256")}
            for entry in accounted_entries if isinstance(entry, Mapping)
        ],
        "context_id": result.get("context_id") if isinstance(result.get("context_id"), str) else item.get("context_id"),
    }
    if frozen.get("host_id") == "hermes_cli_local_input_v1":
        association_payload["host_request"] = result.get("request_artifact")
    elif frozen.get("host_id") == "hermes_a2a":
        request = result.get("request")
        if isinstance(request, Mapping):
            host_request_path = operation_dir / "host-request.json"
            if not host_request_path.exists():
                raw_request = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                if _sha256(raw_request) != result.get("request_sha256"):
                    raise FormalRunnerError("actual_host_request_bytes_mismatch")
                _write_new(host_request_path, raw_request)
            association_payload["host_request"] = {
                "path": str(host_request_path.relative_to(config_root)),
                "sha256": _sha256(host_request_path.read_bytes()),
            }
        else:
            association_payload["host_request"] = {"path": "", "sha256": ""}
    association_path = operation_dir / "association.json"
    if not association_path.exists():
        _write_new(association_path, (json.dumps(association_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))
    association_ref = {"path": str(association_path.relative_to(config_root)), "sha256": _sha256(association_path.read_bytes())}
    record = {
        "operation_id": operation_id,
        "attempt": 1,
        "host_id": frozen.get("host_id"),
        "arm_id": frozen.get("arm_id"),
        "unit": dict(unit),
        "ids": {"request_id": request_id, "turn_id": turn_id if isinstance(turn_id, str) and turn_id else None, "session_id": session_id if isinstance(session_id, str) and session_id else None, "unknown": unknown},
        "source_capture_refs": list(item.get("source_capture_refs") or []),
        "delivery": item.get("delivery") or {"status": "not_attempted", "context_sha256": None, "artifact_path": None},
        "answer": answer,
        "usage": dict(usage),
        "status": status,
        "latency_ms": latency_ms,
        "no_retry": True,
        "host_response": {
            "response_sha256": response_sha,
            "artifact_path": response_ref,
            "http_status": result.get("http_status") if isinstance(result.get("http_status"), int) else None,
            "transport": result.get("transport") if isinstance(result.get("transport"), str) else type(transport).__name__,
            "real_host": receipt_is_real,
        },
        "association": association_ref,
    }
    try:
        return writer.append_operation(record)
    except EvidenceSchemaError as exc:
        # A dispatched operation without an independently bound ledger row is
        # retained as a transport receipt, never upgraded to formal evidence.
        rejected = operation_dir / "formal-evidence-rejected.json"
        _write_new(
            rejected,
            (json.dumps({"status": "REJECTED", "reason": str(exc)[:256], "operation_id": operation_id}, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"),
        )
        return rejected


def _load_formal_manifest(path: str | Path, *, formal_config: Path, output_root: Path) -> tuple[Any, list[Mapping[str, Any]], str]:
    """Build one explicit real transport from a TEST-only execution manifest."""
    manifest_path = Path(path).expanduser().resolve()
    lowered = str(manifest_path).replace("/", "\\").lower()
    if "test" not in lowered or lowered == "f:\\agents" or lowered.startswith("f:\\agents\\"):
        raise FormalRunnerError("formal_manifest_must_be_TEST_path")
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FormalRunnerError("formal_manifest_invalid") from exc
    if not isinstance(value, Mapping) or value.get("schema") != "scope-recall.p18-formal-execution-manifest.v1":
        raise FormalRunnerError("formal_manifest_schema_invalid")
    host = value.get("host")
    if not isinstance(host, Mapping) or host.get("id") not in {"codex_appserver", "hermes_a2a"}:
        raise FormalRunnerError("formal_manifest_host_invalid")
    arm_id = host.get("arm_id")
    if arm_id not in ARM_HOOK_POLICIES:
        raise FormalRunnerError("formal_manifest_arm_invalid")
    queries = value.get("queries")
    if not isinstance(queries, list) or not queries or any(not isinstance(item, Mapping) for item in queries):
        raise FormalRunnerError("formal_manifest_queries_invalid")
    if host.get("id") == "hermes_a2a":
        required = ("endpoint", "expected_agent_card_identity", "isolation_root", "bridge_state", "ledger_path", "freeze_sha256")
        if any(not host.get(key) for key in required) or not isinstance(host.get("expected_agent_card_identity"), Mapping):
            raise FormalRunnerError("hermes_formal_bridge_config_required")
        expected_config_sha = _sha256(formal_config.read_bytes())
        if str(host["freeze_sha256"]).lower() != expected_config_sha:
            raise FormalRunnerError("hermes_bridge_freeze_hash_mismatch")
        budget = HermesOperationBudget(
            host["bridge_state"],
            host["ledger_path"],
            freeze_sha256=str(host["freeze_sha256"]),
            operation_root=output_root.resolve() / "operations",
            config_root=formal_config.resolve().parent,
            bridge_model=str(host.get("model") or "deepseek-v4-flash"),
        )
        config = HermesTransportConfig(
            endpoint=str(host["endpoint"]),
            expected_agent_card_identity=dict(host["expected_agent_card_identity"]),
            isolation_root=Path(str(host["isolation_root"])).expanduser().resolve(),
            formal_config_path=formal_config,
            model=str(host.get("model") or "deepseek-v4-flash"),
            timeout_seconds=float(host.get("timeout_seconds", 50.0)),
            formal_evaluation=True,
            allow_diagnostic_fixture=False,
        )
        return HermesA2ATransport(config, budget), queries, str(arm_id)
    try:
        from p18_codex_budget import CodexSubmissionBudget

        executable = Path(str(host.get("codex_exe"))).expanduser().resolve()
        cwd = Path(str(host.get("cwd"))).expanduser().resolve()
        ledger_path = Path(str(host.get("ledger_path"))).expanduser().resolve()
        backend = CodexSubmissionBudget(ledger_path)
        config = CodexTransportConfig(
            codex_exe=executable,
            cwd=cwd,
            arm_id=str(arm_id),
            expected_hooks_policy=ARM_HOOK_POLICIES[str(arm_id)],
            model=str(host.get("model") or MODEL),
            effort=str(host.get("effort") or EFFORT),
            timeout_seconds=float(host.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
            formal_evaluation=True,
            formal_config_path=formal_config,
        )
        adapter = CodexLedgerBudgetAdapter(backend, operation_root=output_root.resolve() / "operations", config_root=formal_config.resolve().parent)
        transport = CodexAppServerTransport(config, budget=adapter)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise FormalRunnerError(f"codex_formal_transport_unavailable:{type(exc).__name__}") from exc
    return transport, queries, str(arm_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="P18 bounded formal/diagnostic runner")
    parser.add_argument("--mode", choices=("formal", "diagnostic"), required=True)
    parser.add_argument("--public-fixture", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--arms", nargs="+", default=["A", "B", "C", "D"])
    parser.add_argument("--hermes-exchange-fixture", type=Path)
    parser.add_argument("--codex-fixture", type=Path)
    parser.add_argument("--formal-config", type=Path)
    parser.add_argument("--manifest", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.mode == "formal":
        if args.hermes_exchange_fixture is not None or args.codex_fixture is not None:
            print(json.dumps({"status": "NOT_READY", "reason": "fixture_transport_is_diagnostic_only"}, ensure_ascii=False))
            return 1
        if args.formal_config is None:
            print(json.dumps({"status": "NOT_READY", "reason": "formal_config_required"}, ensure_ascii=False))
            return 1
        if args.manifest is None:
            print(json.dumps({"status": "NOT_READY", "reason": "formal_manifest_required"}, ensure_ascii=False))
            return 1
        readiness = verify_formal_run_config(args.formal_config)
        if not readiness.formal_execution_allowed:
            print(json.dumps({"status": readiness.status, "formal_execution_allowed": False, "reasons": list(readiness.reasons)}, ensure_ascii=False))
            return 1
        try:
            transport, queries, arm_id = _load_formal_manifest(args.manifest, formal_config=args.formal_config, output_root=args.output)
            result = run_transport_operations(config_path=args.formal_config, output_root=args.output, transport=transport, queries=queries, arm_id=arm_id)
        except FormalRunnerError as exc:
            print(json.dumps({"status": "NOT_READY", "formal_execution_allowed": False, "reason": str(exc)}, ensure_ascii=False))
            return 1
        print(json.dumps({"status": result["status"], "formal_pass": False, "receipt_count": len(result["receipts"])}, ensure_ascii=False))
        return 0
    if args.public_fixture is None:
        print(json.dumps({"status": "NOT_READY", "reason": "public_fixture_required"}, ensure_ascii=False))
        return 1
    result = run_public_diagnostic(fixture_path=args.public_fixture, output_root=args.output, arms=args.arms, hermes_exchange_fixture=args.hermes_exchange_fixture, codex_fixture=args.codex_fixture)
    print(json.dumps({"status": result["status"], "formal_pass": result["formal_pass"], "receipt_count": len(result["receipts"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
