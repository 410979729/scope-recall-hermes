"""Name which vector fault happened, not which family it belongs to.

``_vector_candidates`` reports an optional channel's failure as
``vector_error:<class>``.  For an ``AuxiliaryModelError`` that is enough, because
it carries an ``error_type`` from a fixed vocabulary.  For the native Lance
helper it is not: that path raises twenty-three distinct ``RuntimeError``
messages -- the helper lock timed out, the worker is closed, the worker exited
mid-frame, the worker was never running, a request deadline was exhausted, a
fence handshake mismatched, the table is not open, lancedb is not installed --
and every one of them reaches the operator as the single word ``RuntimeError``.

Measured on tianshu: every recall on 2026-09-15 reported ``vector_unavailable``
together with ``vector_error:RuntimeError``, the semantic channel was lost on all
of them, and *which* of those faults it was could not be recovered from anywhere.
The exception is caught and discarded, so the message survives nowhere else --
not in the gap, not in a log, not in the database.

This is the same shape the project has already named twice: ``model_unavailable``
standing for every auxiliary failure, and ``OperationalError`` standing for every
SQLite fault until ``sqlite_errorname`` was attached to it.

The vocabulary is closed on purpose.  The first version of this module derived a
token from the message text, and the gate immediately showed why that is wrong:
a test's own wording turned into
``TimeoutError:synthetic_embedding_request_used_its_entire_allowance``.  A gap is
something a reader groups on, so it may only take values from a set someone can
enumerate -- which is the very reason ``error_type`` is preferred over a message
three lines below.  An unrecognised message contributes nothing, and the label
falls back to the bare class rather than inventing a category.
"""
from __future__ import annotations

#: Marker -> token, most specific first.  Markers are fragments of the messages
#: raised in ``lance_process_store`` and ``vector_store``; the token is what an
#: operator reads.  ``test_vector_failure`` parses both modules and fails if any
#: ``RuntimeError`` there stops being covered, so this cannot silently rot.
NATIVE_VECTOR_FAULTS: tuple[tuple[str, str], ...] = (
    ("helper lock timeout", "helper_lock_timeout"),
    ("helper request deadline exhausted", "helper_request_deadline"),
    ("helper teardown failed", "helper_teardown_failed"),
    ("helper teardown is still pending", "helper_teardown_pending"),
    ("worker exited or returned an invalid frame", "worker_exited_mid_frame"),
    ("worker is closed", "worker_closed"),
    ("worker is not running", "worker_not_running"),
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
    if type(kind) is str and kind.isascii() and kind.replace("_", "").isalnum():
        # A closed vocabulary already names the fault.
        return f"{label}:{kind}"
    try:
        message = str(exc)
    except Exception:
        return label
    token = native_vector_fault(message)
    return f"{label}:{token}" if token else label


__all__ = ["NATIVE_VECTOR_FAULTS", "native_vector_fault", "vector_failure_label"]
