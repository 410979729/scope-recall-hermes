"""Small, bounded Hermes A2A ``message/send`` transport for P18.

This is a transport seam, not a scorer or an arm runner.  The production
path is deliberately gated to an explicit TEST loopback endpoint and a
deferred, authoritative formal-config verifier.  Focused tests use ``exchange`` injection;
that path never opens a socket, starts a host, calls a model, or reads sealed
evaluation material.

Hermes primary budgeting belongs to the external P11 bridge.  This module
only calls the supplied ``BudgetBoundary`` and never imports or writes the
Codex token ledger.  ``usage=None`` tells that boundary to finalize using its
own unknown-usage upper bound.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler


MODEL = "hermes-primary"
MAX_RESPONSE_BYTES = 786_432
MAX_ANSWER_BYTES = 65_536
MAX_QUERY_BYTES = 131_072
MAX_ERROR_TEXT = 512
MAX_ID_TEXT = 256
_RESERVATION_FIELDS = frozenset({
    "bridge_id", "request_id", "operation_id", "batch", "model", "present",
    "body_sha256", "request_sha256", "request_bytes", "ledger_path", "table",
    "reserved_input", "reserved_output", "actual_input", "actual_output",
    "charge_micro_usd", "status", "started_ns", "finished_ns",
})


class HermesA2AError(RuntimeError):
    """A bounded transport or admission error."""


class FormalEvaluationBlocked(HermesA2AError):
    """The root has not supplied the required formal authorization."""


class BudgetBoundary(Protocol):
    """External Hermes/P11 primary budget boundary.

    The transport must reserve before card discovery or ``message/send``.
    ``usage`` is ``None`` when the bridge must use its configured unknown
    upper bound.  Implementations must not be the Codex token ledger.
    """

    def reserve(self, model: str, request: bytes) -> Any: ...

    def finish(self, reservation: Any, status: str, usage: Mapping[str, int] | None) -> str: ...


ReservationEvidence = Callable[[Any], Mapping[str, Any]]
Exchange = Callable[[str, str, bytes | None, float], "HermesHTTPResponse"]


@dataclass(frozen=True)
class HermesTransportConfig:
    endpoint: str
    expected_agent_card_identity: Mapping[str, str]
    isolation_root: Path
    formal_config_path: Path | None = None
    model: str = MODEL
    timeout_seconds: float = 50.0
    formal_evaluation: bool = False
    allow_diagnostic_fixture: bool = True

    def __post_init__(self) -> None:
        parsed = urlsplit(self.endpoint)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("explicit_http_endpoint_required")
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("loopback_endpoint_required")
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("endpoint_must_not_have_query_fragment_or_credentials")
        root = Path(self.isolation_root).expanduser().resolve()
        lowered = str(root).replace("/", "\\").lower().rstrip("\\")
        if "test" not in lowered:
            raise ValueError("isolation_root_must_be_TEST_path")
        if lowered == "f:\\agents" or lowered.startswith("f:\\agents\\"):
            raise ValueError("production_path_forbidden")
        if not self.expected_agent_card_identity or not all(
            isinstance(k, str) and isinstance(v, str) and k and v
            for k, v in self.expected_agent_card_identity.items()
        ):
            raise ValueError("expected_agent_card_identity_required")
        if not self.model or not isinstance(self.model, str):
            raise ValueError("model")
        if not 0 < float(self.timeout_seconds) <= 90.0:
            raise ValueError("timeout_seconds")
        object.__setattr__(self, "isolation_root", root)
        object.__setattr__(self, "endpoint", self.endpoint.rstrip("/"))
        if self.formal_config_path is not None:
            object.__setattr__(self, "formal_config_path", Path(self.formal_config_path).expanduser().resolve())


@dataclass(frozen=True)
class HermesHTTPResponse:
    status: int
    body: bytes
    headers: Mapping[str, str] | None = None


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _bounded_text(value: Any, limit: int = MAX_ERROR_TEXT) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:limit]


def _safe_id(value: Any) -> str | None:
    text = _bounded_text(value, MAX_ID_TEXT)
    return text if text else None


def _safe_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    """Keep reservation evidence auditable without allowing raw payloads."""

    result: dict[str, Any] = {}
    for key, item in value.items():
        if key not in _RESERVATION_FIELDS:
            continue
        if isinstance(item, (str, int, float, bool)) or item is None:
            result[key[:80]] = item if not isinstance(item, str) else item[:256]
    return result


def _text_from_message(message: Any) -> list[str]:
    if not isinstance(message, Mapping):
        return []
    parts = message.get("parts")
    if not isinstance(parts, list):
        return []
    texts: list[str] = []
    for part in parts:
        if isinstance(part, Mapping):
            text = part.get("text")
        else:
            text = None
        if isinstance(text, str) and text:
            texts.append(text)
    return texts


def _extract_task(result: Mapping[str, Any]) -> Mapping[str, Any]:
    task = result.get("task")
    return task if isinstance(task, Mapping) else result


def _extract_usage(result: Mapping[str, Any]) -> Mapping[str, int] | None:
    candidate = result.get("usage")
    if not isinstance(candidate, Mapping):
        return None
    usage: dict[str, int] = {}
    for key, value in candidate.items():
        if isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            usage[key] = value
    return usage or None


def _card_url(endpoint: str) -> str:
    return endpoint.rstrip("/") + "/.well-known/agent-card.json"


class HermesA2ATransport:
    """One-shot Hermes A2A transport with a fixture-only injection seam."""

    def __init__(
        self,
        config: HermesTransportConfig,
        budget: BudgetBoundary | None,
        *,
        exchange: Exchange | None = None,
        reservation_evidence: ReservationEvidence | None = None,
    ) -> None:
        if exchange is not None and not config.allow_diagnostic_fixture:
            raise ValueError("diagnostic_fixture_not_allowed")
        self.config = config
        self.budget = budget
        self.exchange = exchange
        self.reservation_evidence = reservation_evidence

    @property
    def fixture_mode(self) -> bool:
        return self.exchange is not None

    def execute(
        self,
        query_text: str,
        *,
        context_id: str,
        message_id: str,
        receipt_path: Path | None = None,
        raw_response_path: Path | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        base: dict[str, Any] = {
            "transport": "hermes_a2a_jsonrpc",
            "method": "message/send",
            "endpoint": self.config.endpoint,
            "fixture_mode": self.fixture_mode,
            "formal_evaluation": self.config.formal_evaluation,
            "status": "NOT_READY",
            "transport_status": "NOT_READY",
            "semantic_pass": False,
            "retry_count": 0,
            "network_calls": 0,
            "message_send_calls": 0,
            "answer_text": None,
            "answer_sha256": None,
            "task_id": None,
            "context_id": context_id,
            "latency_ms": None,
            "budget": {
                "owner": "external_hermes_primary_bridge",
                "ledger": "not_used",
                "reservation": None,
                "finalize": None,
                "usage_resolution": None,
            },
        }
        if not isinstance(query_text, str) or not query_text:
            return self._finish_receipt(base, "NOT_READY", "query_text_required", started, receipt_path)
        if len(query_text.encode("utf-8")) > MAX_QUERY_BYTES:
            return self._finish_receipt(base, "NOT_READY", "query_text_too_large", started, receipt_path)
        if not context_id or not message_id:
            return self._finish_receipt(base, "NOT_READY", "context_and_message_ids_required", started, receipt_path)
        if not self.fixture_mode and not self.config.formal_evaluation:
            return self._finish_receipt(base, "NOT_READY", "formal_config_required_for_runtime", started, receipt_path)
        if self.config.formal_evaluation:
            readiness = self._verify_formal_config()
            base["formal_readiness"] = readiness
            if readiness["status"] != "READY":
                return self._finish_receipt(base, "NOT_READY", "P18_G0_G2_candidate_freeze_required", started, receipt_path)
        if self.budget is None:
            return self._finish_receipt(base, "NOT_READY", "external_hermes_budget_boundary_required", started, receipt_path)

        request_payload = {
            "jsonrpc": "2.0",
            "id": message_id,
            "method": "message/send",
            "params": {
                "message": {
                    "messageId": message_id,
                    "role": "ROLE_USER",
                    "parts": [{"text": query_text}],
                    "contextId": context_id,
                },
                "configuration": {"returnImmediately": False},
            },
        }
        request_bytes = json.dumps(request_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        base.update(request_sha256=_sha256(request_bytes), request_bytes=len(request_bytes), request=request_payload)
        reservation: Any
        try:
            reservation = self.budget.reserve(self.config.model, request_bytes)
        except Exception as exc:  # boundary owns the detailed reason; receipt stays bounded
            return self._finish_receipt(base, "NOT_READY", f"budget_reserve_failed:{type(exc).__name__}", started, receipt_path)
        try:
            mapped_evidence = self.reservation_evidence(reservation) if self.reservation_evidence else {"present": True}
            if not isinstance(mapped_evidence, Mapping):
                raise TypeError("reservation_evidence_mapper_must_return_mapping")
            base["budget"]["reservation"] = _safe_evidence(mapped_evidence)
        except Exception as exc:
            try:
                base["budget"]["finalize"] = _bounded_text(self.budget.finish(reservation, "FAIL", None), 256)
                base["budget"]["usage_resolution"] = "UNKNOWN_UPPER_BOUND"
            except Exception as finalize_exc:
                base["budget"]["finalize"] = f"error:{type(finalize_exc).__name__}"
            return self._finish_receipt(base, "NOT_READY", f"reservation_evidence_failed:{type(exc).__name__}", started, receipt_path)

        status = "FAILED"
        transport_status = "FAILED"
        usage: Mapping[str, int] | None = None
        finish_status = "FAILED"
        try:
            card_response = self._call("GET", _card_url(self.config.endpoint), None)
            base["network_calls"] = 1
            base["agent_card_http_status"] = card_response.status
            base["agent_card_bytes"] = len(card_response.body)
            base["agent_card_sha256"] = _sha256(card_response.body)
            card = self._json_object(card_response.body)
            if card_response.status < 200 or card_response.status >= 300:
                raise HermesA2AError("agent_card_http_status")
            if any(card.get(k) != v for k, v in self.config.expected_agent_card_identity.items()):
                raise HermesA2AError("agent_card_identity_mismatch")

            response = self._call("POST", self.config.endpoint, request_bytes)
            base["network_calls"] = 2
            base["message_send_calls"] = 1
            base["http_status"] = response.status
            base["response_bytes"] = len(response.body)
            base["response_sha256"] = _sha256(response.body)
            if raw_response_path is not None:
                raw_target = Path(raw_response_path).expanduser().resolve()
                try:
                    raw_target.relative_to(self.config.isolation_root)
                except ValueError as exc:
                    raise ValueError("raw_response_must_stay_under_TEST_isolation_root") from exc
                raw_target.parent.mkdir(parents=True, exist_ok=True)
                raw_target.write_bytes(response.body)
                base["response_artifact_path"] = str(raw_target)
            parsed = self._json_object(response.body)
            result = parsed.get("result")
            if not isinstance(result, Mapping):
                error = parsed.get("error")
                if isinstance(error, Mapping):
                    base["error"] = {
                        "code": error.get("code") if isinstance(error.get("code"), int) else None,
                        "message": _bounded_text(error.get("message")),
                    }
                    raise HermesA2AError("jsonrpc_error")
                raise HermesA2AError("jsonrpc_result_required")
            if response.status < 200 or response.status >= 300:
                raise HermesA2AError("http_status_not_success")
            if parsed.get("jsonrpc") != "2.0" or parsed.get("id") != message_id:
                raise HermesA2AError("jsonrpc_envelope_mismatch")

            task = _extract_task(result)
            state_value = task.get("status")
            state = state_value.get("state") if isinstance(state_value, Mapping) else state_value
            base["task_status"] = _bounded_text(state, 64)
            base["task_id"] = _safe_id(task.get("id") or result.get("id"))
            base["context_id"] = _safe_id(task.get("contextId") or result.get("contextId") or context_id)
            messages: list[str] = []
            if isinstance(state_value, Mapping):
                messages.extend(_text_from_message(state_value.get("message")))
            messages.extend(_text_from_message(result.get("message")))
            messages.extend(_text_from_message(task.get("message")))
            artifacts = task.get("artifacts")
            if not isinstance(artifacts, list):
                artifacts = result.get("artifacts")
            if isinstance(artifacts, list):
                for artifact in artifacts:
                    messages.extend(_text_from_message(artifact))
            answer = "\n".join(item for item in messages if item).strip()
            if str(state).upper() not in {"COMPLETED", "TASK_STATE_COMPLETED"}:
                raise HermesA2AError("task_not_completed")
            if not answer:
                raise HermesA2AError("empty_completed_answer")
            answer_bytes = answer.encode("utf-8")
            base["answer_text"] = answer_bytes[:MAX_ANSWER_BYTES].decode("utf-8", errors="ignore")
            base["answer_truncated"] = len(answer_bytes) > MAX_ANSWER_BYTES
            base["answer_sha256"] = _sha256(answer_bytes)
            usage = _extract_usage(result)
            status = "DIAGNOSTIC" if self.fixture_mode else "COMPLETED"
            transport_status = "COMPLETED"
            finish_status = "COMPLETED"
        except (HermesA2AError, ValueError, UnicodeDecodeError, json.JSONDecodeError, URLError, OSError) as exc:
            base["error"] = base.get("error") or {"type": type(exc).__name__, "reason": str(exc)[:MAX_ERROR_TEXT]}
            status = "DIAGNOSTIC" if self.fixture_mode else "FAILED"
            transport_status = "FAILED"
            finish_status = "FAILED"
        finally:
            try:
                final_receipt = self.budget.finish(reservation, finish_status, usage)
                base["budget"]["finalize"] = _bounded_text(final_receipt, 256)
                base["budget"]["usage_resolution"] = "KNOWN" if usage is not None else "UNKNOWN_UPPER_BOUND"
            except Exception as exc:
                base["budget"]["finalize"] = f"error:{type(exc).__name__}"
                status = "DIAGNOSTIC" if self.fixture_mode else "FAILED"
                transport_status = "FAILED"
                base["error"] = base.get("error") or {"type": "budget_finalize_error"}
        base["transport_status"] = transport_status
        return self._finish_receipt(base, status, None, started, receipt_path)

    def _verify_formal_config(self) -> dict[str, Any]:
        """Defer the authoritative G0/G2/candidate check until execution."""

        if self.config.formal_config_path is None:
            return {"status": "NOT_READY", "reasons": ["formal_config_path_required"]}
        try:
            from tests.eval.p18_formal_evidence import verify_formal_run_config

            readiness = verify_formal_run_config(self.config.formal_config_path)
            allowed = bool(getattr(readiness, "formal_execution_allowed", False))
            reasons = getattr(readiness, "reasons", ())
            return {
                "status": "READY" if allowed else "NOT_READY",
                "reasons": [str(item)[:MAX_ERROR_TEXT] for item in reasons][:16],
                "config_path": str(self.config.formal_config_path),
            }
        except Exception as exc:
            return {
                "status": "NOT_READY",
                "reasons": [f"formal_config_verifier:{type(exc).__name__}"],
                "config_path": str(self.config.formal_config_path),
            }

    def _call(self, method: str, url: str, body: bytes | None) -> HermesHTTPResponse:
        if self.exchange is not None:
            response = self.exchange(method, url, body, float(self.config.timeout_seconds))
            if not isinstance(response, HermesHTTPResponse):
                raise HermesA2AError("exchange_must_return_HermesHTTPResponse")
            return response
        request = Request(url, data=body, headers={"Content-Type": "application/json", "Accept": "application/json"}, method=method)
        opener = build_opener(ProxyHandler({}))
        try:
            with opener.open(request, timeout=float(self.config.timeout_seconds)) as response:
                return HermesHTTPResponse(int(response.status), response.read(MAX_RESPONSE_BYTES), dict(response.headers.items()))
        except HTTPError as exc:
            return HermesHTTPResponse(int(exc.code), exc.read(MAX_RESPONSE_BYTES), dict(exc.headers.items()) if exc.headers else {})

    @staticmethod
    def _json_object(body: bytes) -> dict[str, Any]:
        value = json.loads(body.decode("utf-8"))
        if not isinstance(value, dict):
            raise HermesA2AError("json_object_required")
        return value

    def _finish_receipt(
        self,
        receipt: dict[str, Any],
        status: str,
        reason: str | None,
        started: float,
        receipt_path: Path | None,
    ) -> dict[str, Any]:
        receipt["status"] = status
        if reason:
            receipt.setdefault("error", {"type": "admission", "reason": reason})
        receipt["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
        if receipt_path is not None:
            destination = Path(receipt_path).expanduser().resolve()
            try:
                destination.relative_to(self.config.isolation_root)
            except ValueError as exc:
                raise ValueError("receipt_must_stay_under_TEST_isolation_root") from exc
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            receipt["receipt_path"] = str(destination)
        return receipt
