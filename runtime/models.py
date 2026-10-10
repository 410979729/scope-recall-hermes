"""The auxiliary models' transport: the error type, the HTTPS worker, credentials, the secret guard and the metered
post every embedding and consolidation request goes through.  The adapters and their routes are in
``embedding_models`` and ``consolidation_models``."""

from __future__ import annotations

import base64
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from ..contracts import ContractError
from ..core.endpoint_policy import endpoint_scheme_allowed
from ..core.secret_patterns import contains_secret_like_text
from .model_budget import AuxiliaryBudgetLedger

MAX_CHAT_RESPONSE_BYTES = 1_048_576
RESERVE_ENVELOPE_MARGIN = 256
MAX_CREDENTIAL_BYTES = 8192
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


class AuxiliaryModelError(RuntimeError):
    def __init__(self, error_type: str, *, detail: str | None = None) -> None:
        self.error_type = error_type
        self.detail = detail if type(detail) is str and detail.isdigit() and len(detail) == 3 else None
        message = error_type if detail is None else f"{error_type}:{detail}"
        if contains_secret_like_text(message):
            message = error_type
        super().__init__(message)


#: A provider's own name for why it refused, e.g. ``GoUsageLimitError``.  Short,
#: symbolic, drawn from the provider's vocabulary -- unlike the message beside
#: it, which is free text and may carry account identifiers or URLs.
_REFUSAL_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


def provider_refusal_code(raw: object) -> str | None:
    """The bounded symbol naming why a provider refused, or ``None``.

    The body of a failed call is free text and is discarded, but the *type*
    inside it is the difference between "the model is failing" and "the
    monthly quota is exhausted until the 27th".
    """
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_CHAT_RESPONSE_BYTES:
        return None
    try:
        payload = json.loads(bytes(raw).decode("utf-8", "replace"))
    except (ValueError, UnicodeError):
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    for candidate in (
        error.get("type") if isinstance(error, dict) else None,
        error.get("code") if isinstance(error, dict) else None,
        payload.get("type"),
    ):
        if type(candidate) is str and _REFUSAL_CODE.match(candidate) and not contains_secret_like_text(candidate):
            return candidate
    return None


class HttpTransport(Protocol):
    def post(
        self,
        url: str,
        *,
        body: bytes,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> tuple[int, bytes]: ...


_HTTP_WORKER_PATH = (Path(__file__).resolve().parents[1] / "runtime" / "_http_worker.py").resolve()
_HTTP_WORKER_STDOUT_MARGIN = 8 * 1024
_HTTP_WORKER_MAX_REQUEST_BYTES = 3 * 1024 * 1024
_HTTP_WORKER_CLEANUP_GRACE_SECONDS = 0.2
#: Replies the worker may send; anything else is a protocol fault, not a provider answer.
_HTTP_WORKER_ERRORS = frozenset(
    {
        "endpoint_invalid",
        "http_redirect",
        "http_protocol",
        "network_error",
        "request_limit",
        "response_limit",
        "timeout",
    }
)


def _cleanup_http_worker(process: subprocess.Popen[bytes] | None) -> None:
    """Kill/reap only this helper, with a short bounded cleanup grace."""
    if process is None or process.poll() is not None:
        return
    try:
        process.kill()
    except OSError:
        return
    try:
        process.communicate(timeout=_HTTP_WORKER_CLEANUP_GRACE_SECONDS)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        # The primary transport exception must remain authoritative. The fixed
        # grace prevents a broken child pipe from extending the request bound.
        pass


def validate_timeout_seconds(timeout_seconds: object) -> float:
    if type(timeout_seconds) not in (int, float):
        raise AuxiliaryModelError("timeout")
    value = float(timeout_seconds)
    if not math.isfinite(value) or value <= 0:
        raise AuxiliaryModelError("timeout")
    return value


def seconds_left(deadline: float) -> float:
    return deadline - time.monotonic()


def validate_credential_env_name(env_name: object) -> str:
    if type(env_name) is not str or not env_name or _ENV_NAME_RE.fullmatch(env_name) is None:
        raise ValueError("credential_env")
    return env_name


def _hidden_window() -> dict[str, Any]:
    """Popen options that keep the helper from flashing a console on Windows."""
    if os.name != "nt":
        return {"startupinfo": None, "creationflags": 0}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {"startupinfo": startupinfo, "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _worker_request(
    url: str,
    *,
    body: bytes,
    headers: Mapping[str, str],
    budget: float,
    max_response_bytes: int,
    allow_insecure: bool = False,
) -> bytes:
    """The one request line the worker accepts, validated before any process starts."""
    if not endpoint_scheme_allowed(url, allow_insecure=allow_insecure):
        raise AuxiliaryModelError("endpoint_invalid")
    if not _HTTP_WORKER_PATH.is_file():
        raise AuxiliaryModelError("transport_unavailable")
    if type(max_response_bytes) is not int or max_response_bytes <= 0:
        raise AuxiliaryModelError("response_limit")
    if not isinstance(body, bytes) or not isinstance(headers, Mapping):
        raise AuxiliaryModelError("request_invalid")
    request = {
        "url": url,
        "body_b64": base64.b64encode(body).decode("ascii"),
        "headers": dict(headers),
        "timeout_seconds": budget,
        "max_response_bytes": max_response_bytes,
    }
    if allow_insecure:
        request["allow_insecure"] = True
    try:
        request_bytes = json.dumps(request, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AuxiliaryModelError("request_invalid") from exc
    if len(request_bytes) > _HTTP_WORKER_MAX_REQUEST_BYTES:
        raise AuxiliaryModelError("request_limit")
    return request_bytes


def _worker_reply(stdout: bytes, max_response_bytes: int) -> tuple[int, bytes]:
    """Decode the worker's reply: a provider answer returns, a worker fault raises."""
    try:
        result = json.loads(stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AuxiliaryModelError("transport_worker_protocol") from exc
    if not isinstance(result, dict) or type(result.get("ok")) is not bool:
        raise AuxiliaryModelError("transport_worker_protocol")
    status = result.get("status")
    if status is not None and (type(status) is not int or not 100 <= status <= 599):
        raise AuxiliaryModelError("transport_worker_protocol")
    body_b64 = result.get("body_b64", "")
    if type(body_b64) is not str:
        raise AuxiliaryModelError("transport_worker_protocol")
    try:
        response_body = base64.b64decode(body_b64.encode("ascii"), validate=True)
    except (UnicodeError, ValueError) as exc:
        raise AuxiliaryModelError("transport_worker_protocol") from exc
    if len(response_body) > max_response_bytes:
        raise AuxiliaryModelError("response_limit")
    if result["ok"]:
        if set(result) != {"ok", "status", "body_b64"} or status is None:
            raise AuxiliaryModelError("transport_worker_protocol")
        return status, response_body
    error_type = result.get("error")
    if (
        set(result) - {"ok", "error", "status", "body_b64"}
        or type(error_type) is not str
        or error_type not in _HTTP_WORKER_ERRORS
    ):
        raise AuxiliaryModelError("transport_worker_protocol")
    raise AuxiliaryModelError(error_type, detail=None if status is None else str(status))


class HttpsTransport:
    """Bounded HTTPS POST; query callers may own a persistent stdlib worker."""

    def __init__(self, *, persistent: bool = False, allow_insecure_endpoint: bool = False):
        from .http_session import HttpWorkerSession

        if type(allow_insecure_endpoint) is not bool:  # only the literal boolean is permission, here as in the config
            raise ValueError("allow_insecure_endpoint")
        self._session = HttpWorkerSession() if persistent else None
        self._post_lock = threading.Lock()
        #: Permission to send plaintext HTTP beyond this machine (loopback HTTP needs none).  Held on the transport
        #: that sends, so the permission cannot be lost between the config and the socket.
        self._allow_insecure_endpoint = allow_insecure_endpoint

    def close(self):
        if self._session is not None:
            self._session.close()

    def _discard_session(self) -> None:
        if self._session is not None:
            self._session.discard()

    def post(
        self, url: str, *, body: bytes, headers: Mapping[str, str], timeout_seconds: float, max_response_bytes: int
    ) -> tuple[int, bytes]:
        """Bound the entire exchange, including waiting for a query session."""
        if self._session is None:
            return self._post(
                url, body=body, headers=headers, timeout_seconds=timeout_seconds, max_response_bytes=max_response_bytes
            )
        deadline = time.monotonic() + validate_timeout_seconds(timeout_seconds)
        if not self._post_lock.acquire(timeout=max(0, seconds_left(deadline))):
            raise AuxiliaryModelError("timeout")
        try:
            return self._post(
                url,
                body=body,
                headers=headers,
                timeout_seconds=seconds_left(deadline),
                max_response_bytes=max_response_bytes,
            )
        except BaseException:
            self._discard_session()
            raise
        finally:
            self._post_lock.release()

    def _post(
        self,
        url: str,
        *,
        body: bytes,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> tuple[int, bytes]:
        """Build the request line, exchange it with the helper, decode its reply."""
        budget = validate_timeout_seconds(timeout_seconds)
        deadline = time.monotonic() + budget
        request_bytes = _worker_request(
            url,
            body=body,
            headers=headers,
            budget=budget,
            max_response_bytes=max_response_bytes,
            allow_insecure=self._allow_insecure_endpoint,
        )
        command = [sys.executable, "-I", "-B", str(_HTTP_WORKER_PATH)]
        max_stdout = (max_response_bytes * 4) // 3 + _HTTP_WORKER_STDOUT_MARGIN
        process: subprocess.Popen[bytes] | None = None
        try:
            if seconds_left(deadline) <= 0:
                raise AuxiliaryModelError("timeout")
            if self._session is not None:
                stdout, stderr = self._session.exchange(
                    command,
                    request_bytes,
                    deadline=deadline,
                    max_stdout=max_stdout,
                    **_hidden_window(),
                )
            else:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    **_hidden_window(),
                )
                remaining = seconds_left(deadline)
                if remaining <= 0:
                    raise AuxiliaryModelError("timeout")
                stdout, stderr = process.communicate(input=request_bytes, timeout=remaining)
            if seconds_left(deadline) <= 0:
                raise AuxiliaryModelError("timeout")
            if process is not None and process.returncode != 0:
                raise AuxiliaryModelError("transport_worker", detail=str(process.returncode))
            if len(stdout) > max_stdout or len(stderr) > _HTTP_WORKER_STDOUT_MARGIN:
                raise AuxiliaryModelError("transport_worker_protocol")
            return _worker_reply(stdout, max_response_bytes)
        except AuxiliaryModelError:
            self._discard_session()
            raise
        except (subprocess.TimeoutExpired, TimeoutError) as exc:
            self._discard_session()
            raise AuxiliaryModelError("timeout") from exc
        except OSError as exc:
            raise AuxiliaryModelError("network_error", detail=type(exc).__name__) from exc
        finally:
            _cleanup_http_worker(process)


def json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def load_credential(env_name: str) -> str:
    validate_credential_env_name(env_name)
    value = os.environ.get(env_name)
    if type(value) is not str:
        raise AuxiliaryModelError("credential_missing")
    value = value.strip()
    if not value or "\r" in value or "\n" in value:
        raise AuxiliaryModelError("credential_shape_invalid")
    if len(value.encode("utf-8")) > MAX_CREDENTIAL_BYTES:
        raise AuxiliaryModelError("credential_shape_invalid")
    return value


def reject_secrets(value: str) -> None:
    if contains_secret_like_text(value):
        raise AuxiliaryModelError("sensitive_request")


def _load_json_object(raw: bytes) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise AuxiliaryModelError("unicode_error", detail=type(exc).__name__) from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AuxiliaryModelError("invalid_json") from exc
    if not isinstance(payload, dict):
        raise AuxiliaryModelError("unsupported_response_shape")
    return payload


#: Ledger refusals that mean "not now" rather than "over budget".
_BUDGET_UNAVAILABLE = frozenset(
    {
        "ledger_not_initialized",
        "unsupported_model",
        "unsupported_model_or_size",
        "ledger_busy_timeout",
    }
)


def _ledger_error(exc: ValueError) -> AuxiliaryModelError:
    """The ledger refuses with a ValueError code; callers see a closed vocabulary."""
    code = str(exc)
    if code == "budget_exhausted_or_meter_breach":
        return AuxiliaryModelError("budget_exhausted")
    if code in _BUDGET_UNAVAILABLE:
        return AuxiliaryModelError("budget_unavailable")
    return AuxiliaryModelError("request_rejected", detail=type(exc).__name__)


def _settle(
    settle: Callable[..., str],
    request_id: int,
    status: str,
    usage: Mapping[str, int] | None,
    deadline: float,
    pending: BaseException | None,
) -> BaseException | None:
    """Close the reservation; the exception to raise afterwards, if any.

    A failure that already happened stays authoritative over anything the
    settlement finds; a clean call still fails on a metering breach, or when a
    200 came back without the usage the ledger needs.
    """
    try:
        final = settle(request_id, status, usage, timeout_seconds=max(0.001, seconds_left(deadline)))
    except Exception as settle_exc:
        return pending if pending is not None else settle_exc
    if pending is not None:
        return pending
    if "meter_breach" in final:
        return AuxiliaryModelError("meter_breach")
    if "usage_unknown" in final and status.startswith("http_200"):
        return AuxiliaryModelError("missing_usage")
    return None


def metered_post(
    *,
    ledger: AuxiliaryBudgetLedger,
    settle: Callable[..., str],
    model: str,
    body: bytes,
    reserved_input: int,
    reserved_output: int,
    deadline: float,
    transport: HttpTransport,
    endpoint: str,
    headers: Mapping[str, str],
    max_response_bytes: int,
    read_usage: Callable[[Mapping[str, Any]], dict[str, int] | None],
    read_result: Callable[[Mapping[str, Any]], Any],
) -> Any:
    """Reserve, send, parse, settle -- and settle even when sending failed.

    The reservation row is committed before the request leaves the process
    and closed after it returns, so a crash between the two retains the
    reserved charge rather than losing it.  Usage is read before the result
    so a well-formed usage block still settles the row when the answer
    beside it is malformed.
    """
    request_id: int | None = None
    status = "network_error"
    usage: dict[str, int] | None = None
    pending: BaseException | None = None
    result: Any = None
    try:
        request_id = ledger.reserve(
            model,
            body,
            reserved_input=reserved_input,
            reserved_output=reserved_output,
            timeout_seconds=seconds_left(deadline),
        )
        http_remaining = seconds_left(deadline)
        if http_remaining <= 0:
            raise AuxiliaryModelError("timeout")
        status_code, raw = transport.post(
            endpoint, body=body, headers=headers, timeout_seconds=http_remaining, max_response_bytes=max_response_bytes
        )
        status = f"http_{status_code}"
        if status_code != 200:
            refusal = provider_refusal_code(raw)
            if refusal is not None:
                status = f"{status}:{refusal}"
            raise AuxiliaryModelError("http_status", detail=str(status_code))
        payload = _load_json_object(raw)
        usage = read_usage(payload)
        result = read_result(payload)
    except (AuxiliaryModelError, ContractError) as exc:
        # ContractError subclasses ValueError; a reply's own verdict (such as a
        # truncated answer) must not be relabelled as a ledger refusal below.
        pending = exc
    except ValueError as exc:
        pending = _ledger_error(exc)
    finally:
        if request_id is not None:
            pending = _settle(settle, request_id, status, usage, deadline, pending)
    if pending is not None:
        raise pending
    return result
