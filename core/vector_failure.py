"""Label vector failures with an exception class and a stable diagnostic token.

An exception's identifier-safe ASCII ``error_type`` takes precedence over its
message, except when the kind is the generic ``RuntimeError``.  A three-digit
HTTP status accompanies ``http_status`` when the exception supplies one.

Native helper messages are matched against an ordered, fixed vocabulary so
operators can group failures without labels containing arbitrary message text.
An unrecognised message, or one that cannot be read, falls back to the bare
exception class.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar

_TRACE: ContextVar[dict | None] = ContextVar("vector_trace", default=None)


@contextmanager
def using_vector_trace(fields: dict):
    """Collect fixed codes and milliseconds for one recall, without changing its deadlines."""
    fields.update(vector_started=0, vector_finished=0, vector_packet=0)
    token = _TRACE.set(fields)
    try:
        yield
    finally:
        _TRACE.reset(token)


@contextmanager
def vector_phase(kind: str, phase: str):
    """Time a known stage; retain the stage that raised rather than its enclosing call."""
    fields = _TRACE.get()
    began = time.monotonic()
    if fields is not None and kind == "vector":
        fields["vector_started"] = 1
    try:
        yield
    except BaseException:
        if fields is not None:
            fields.setdefault(f"{kind}_failure_phase", phase)
        raise
    else:
        if fields is not None and kind == "vector":
            fields["vector_finished"] = 1
    finally:
        if fields is not None:
            key = f"{kind}_{phase}_ms"
            fields[key] = fields.get(key, 0) + round((time.monotonic() - began) * 1000)


def note_helper_deadline(phase: str) -> None:
    """Retain a helper deadline even when an overlapped open parks its answer."""
    fields = _TRACE.get()
    if fields is not None:
        fields.setdefault("helper_failure_phase", phase)


def vector_trace_fields(value: object) -> dict:
    """Accept only known execution flags, timing fields and fixed phase codes."""
    if not isinstance(value, dict):
        return {}
    numeric = {
        "vector_started",
        "vector_finished",
        "vector_packet",
        "vector_search_ms",
        "embedding_ms",
        "helper_drain_ms",
        "helper_open_ms",
        "helper_search_ms",
        "helper_other_ms",
    }
    phases = {
        "helper_failure_phase": {"drain", "open", "search", "other"},
        "embedding_failure_phase": {"prefetch", "connection_slot", "connect", "read", "transport"},
        "vector_failure_phase": {"search"},
    }
    return {
        key: field
        for key, field in value.items()
        if (key in numeric and type(field) is int and field >= 0)
        or (key in phases and type(field) is str and field in phases[key])
    }


def note_embedding_failure(exc: BaseException) -> None:
    """Keep only the transport's closed phase vocabulary and numeric elapsed time."""
    fields = _TRACE.get()
    phase = getattr(exc, "phase", None)
    if fields is not None and phase in {"prefetch", "connection_slot", "connect", "read", "transport"}:
        fields["embedding_failure_phase"] = phase
        elapsed = getattr(exc, "elapsed_ms", None)
        if type(elapsed) is int and elapsed >= 0:
            fields["embedding_ms"] = elapsed


#: Marker -> token, most specific first.  Markers are fragments of the messages
#: raised in ``vector.process_store`` and ``vector.store``; the token is what an
#: operator reads.  ``test_vector_failure`` parses both modules and fails if any
#: ``RuntimeError`` there stops being covered, so this cannot silently rot.
NATIVE_VECTOR_FAULTS: tuple[tuple[str, str], ...] = (
    ("helper lock timeout", "helper_lock_timeout"),
    ("helper request deadline exhausted", "helper_request_deadline"),
    ("helper open deadline exhausted", "helper_open_deadline"),
    ("helper teardown failed", "helper_teardown_failed"),
    ("helper teardown is still pending", "helper_teardown_pending"),
    ("worker exited or returned an invalid frame", "worker_exited_mid_frame"),
    ("worker is closed", "worker_closed"),
    ("worker is not running", "worker_not_running"),
    ("worker unresponsive", "worker_unresponsive"),
    ("worker failed", "worker_failed"),
    ("fence handshake send failed", "fence_send_failed"),
    ("fence final response mismatch", "fence_mismatch"),
    ("fence guard request mismatch", "fence_mismatch"),
    ("fence deadline exhausted", "fence_deadline"),
    ("fence failed", "fence_failed"),
    ("purge deadline exhausted", "purge_deadline"),
    ("vector table is not open", "table_not_open"),
    ("cannot prove an indexed id lookup", "lance_no_indexed_lookup"),
    ("does not support row iteration", "lance_no_row_iteration"),
    ("lancedb/pyarrow is not installed", "lance_not_installed"),
    ("pyarrow is not installed", "lance_not_installed"),
)


def native_vector_fault(message: str) -> str:
    """The token for this message, or "" when it is not one this module knows."""
    if type(message) is not str or not message:
        return ""
    folded = message.casefold()
    for marker, token in NATIVE_VECTOR_FAULTS:
        if marker in folded:
            return token
    return ""


def vector_failure_label(exc: BaseException) -> str:
    """``<class>``, plus whichever fixed name the exception can be placed under."""
    label = type(exc).__name__
    kind = getattr(exc, "error_type", None)
    # A remote RuntimeError names only the family; keep the native diagnosis.
    if type(kind) is str and kind != "RuntimeError" and kind.isascii() and kind.replace("_", "").isalnum():
        # A closed vocabulary already names the fault.  One of its names stands
        # for every answer a provider gives, so the status comes with it: a
        # provider refusing every embedding call for hours would otherwise leave
        # every recall reporting only ``http_status`` -- the same gap a malformed
        # request would have left.  The status is three digits
        # the exception already kept, and a closed set of its own.
        status = getattr(exc, "detail", None)
        if kind == "http_status" and type(status) is str and status.isdigit() and len(status) == 3:
            return f"{label}:{kind}:{status}"
        return f"{label}:{kind}"
    try:
        message = str(exc)
    except Exception:
        return label
    token = native_vector_fault(message)
    return f"{label}:{token}" if token else label


__all__ = ["NATIVE_VECTOR_FAULTS", "native_vector_fault", "vector_failure_label"]
