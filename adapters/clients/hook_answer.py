"""What a hook says back: the answer on stdout and its diagnostics on stderr (``emit_result``), the diagnostics it
gathers on the way (``HookDiagnostics``), and how it reads the outcome of a recall (``recall_incomplete``,
``recall_without_vectors``, ``server_own_vector_fault``)."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any

#: An embedding call's failures that its connection or its worker made, not the provider (``server_own_vector_fault``).
_CONNECTION_FAULTS = frozenset({"network_error", "http_protocol"})
#: Capture refusals a second attempt meets again.
_SETTLED_CAPTURE_CODES = frozenset({"SECRET_DETECTED", "INPUT_INVALID", "VERSION_CONFLICT"})
#: How a capture says it refused a message as holding a credential: as a code, or as the rejection it returns.
_SECRET_REFUSALS = frozenset({"SECRET_DETECTED", "plaintext_secret_rejected"})
_CAPTURE_ERROR_CODES = frozenset(
    {
        "ACCESS_DENIED",
        "IDENTITY_UNBOUND",
        "INPUT_INVALID",
        "VERSION_CONFLICT",
        "DEADLINE_EXCEEDED",
        "STORAGE_UNAVAILABLE",
        "SOURCE_MISSING",
        "SECRET_DETECTED",
    }
)


@dataclass
class HookDiagnostics:
    last_event: str | None = None
    last_reason: str | None = None
    capability_gaps: tuple[str, ...] = ()
    capture_stage: str | None = None
    capture_disposition: str | None = None
    capture_durability: str | None = None
    capture_error_type: str | None = None
    capture_error_code: str | None = None
    #: The code before the frozen allowlist collapsed it to CAPTURE_ERROR.
    #: ``capture_error_code`` is a contract the host reads and may only carry
    #: one of ``_CAPTURE_ERROR_CODES``; this keeps the original for the local
    #: stderr diagnostic line so a collapsed code is still diagnosable.  It
    #: never reaches the host.
    capture_error_detail: str | None = None
    capture_elapsed_ms: int | None = None
    #: What stopped an automatic recall (``recall_exception``): the exception's class and, for a contract error,
    #: its code.  Without it the server's log said only that a recall had failed.
    recall_error_detail: str | None = None
    #: Why the recall ran without its vector search (``recall_without_vectors``), when it did.  The packet carried
    #: that to the model and nowhere else: Claude Code and Codex recalled without their vector search for as long as
    #: anyone could tell, and no log showed it.
    recall_vector_gap: str | None = None
    #: Whether the recall ran its vector search: what a hook asks of its server's answer (``_resident_answer``).
    recall_vectors: bool | None = None
    #: How long attaching the runtime (vector store, embedding worker) took, when this hook attached it.
    runtime_attach_ms: int | None = None
    #: How long all of this hook's captures took: a Stop writes each session-record line (``capture_elapsed_ms`` is
    #: the last one's).
    capture_total_ms: int | None = None

    @property
    def capture_settled(self) -> bool:
        """No capture, or one stored, queued, or refused in a way no retry changes (a secret, an invalid message,
        its id already taken, its id's message deleted, or taken from the inbox by a pass that stored it).
        Otherwise the store was busy or away, and the same hook sent again may store it."""
        return (
            self.capture_stage is None
            or self.capture_durability in ("persisted", "queued")
            or self.capture_disposition in ("rejected", "conflict", "cancelled")
            or self.capture_error_code in _SETTLED_CAPTURE_CODES
        )

    def note_capture_error(self, code: object) -> None:
        """A capture's error: as the host may read it (one of ``_CAPTURE_ERROR_CODES``, else ``CAPTURE_ERROR``), and
        as it was, for the local diagnostic line."""
        self.capture_error_code = code if code in _CAPTURE_ERROR_CODES else "CAPTURE_ERROR"
        self.capture_error_detail = error_detail(code)

    @property
    def capture_refused(self) -> bool:
        """The capture refused its message (a credential, or nothing left to store), rather than failing to write it."""
        return self.capture_disposition == "rejected" or self.capture_error_detail in _SECRET_REFUSALS


def error_detail(code: object) -> str | None:
    """Keep an error code verbatim, bounded and free of anything but a code.

    Codes are enum-like by construction, so the guard is cheap insurance
    rather than sanitisation: whatever ends up on the diagnostic line must be
    recognisable as a code and cannot become a channel for payload text.
    """
    text = str(code or "").strip()
    if not text or len(text) > 64:
        return None
    return text if all(char.isalnum() or char in "_.:-" for char in text) else None


#: Gaps by which a recall packet says its vector search did not run or did not finish, in the order one is named:
#: the search failed, was unavailable, or had no time, or the whole search failed before it.  A search that ran and
#: had candidates refused (``vector_rejected:*``, ``vector_old_or_mismatched_space``) is not among them.
_WITHOUT_VECTORS = (
    lambda gap: gap.startswith("vector_error:"),
    lambda gap: gap == "vector_unavailable",
    lambda gap: gap in ("deadline_exceeded", "deadline_exceeded_collect", "deadline_exceeded_vector"),
    lambda gap: gap.startswith("sqlite_unavailable"),
)


def recall_incomplete(packet) -> str | None:
    """What says a recall came back empty because its read did not finish (the store could not be read, or its time
    ran out at any step: ``status: unavailable``), or None.  Such a packet reads like one that found nothing, and a
    server's was taken over the hook's own (reviews of rc11)."""
    if not isinstance(packet, dict) or packet.get("status") != "unavailable":
        return None
    gaps = [gap for gap in packet.get("gaps") or () if isinstance(gap, str)]
    cause = next((gap for gap in gaps if gap.startswith(("deadline_exceeded", "sqlite_unavailable"))), None)
    return cause or (gaps[0] if gaps else "unavailable")


def server_own_vector_fault(gap: object) -> bool:
    """Whether a server's recall went without its vector search for a reason of its own, which the hook's own recall
    may not share: no vector search at all, its key (``credential_*``), its LanceDB helper or another fault of its own
    process that is not an embedding call's (``core.vector_failure``), or its embedding connection and worker
    (``network_error``, ``http_protocol``, ``transport_*``), which the server keeps between prompts (rc12) while the
    hook's are new.  Otherwise an embedding call's failure (what the provider answered, the time it took, a spent
    budget) the hook meets as well: a second recall only cost the prompt its time and a second metered call (reviews
    of rc11).  The spend ledger's lock held by another writer is not one (an ``OperationalError``), and costs one
    recall more.  Nor is the search running out of time here."""
    if gap == "vector_unavailable":
        return True
    if type(gap) is not str or not gap.startswith("vector_error:"):
        return False
    parts = gap.split(":")
    if len(parts) < 2 or not parts[1]:
        return False
    if parts[1] != "AuxiliaryModelError":
        return True
    return len(parts) >= 3 and (parts[2].startswith(("credential_", "transport_")) or parts[2] in _CONNECTION_FAULTS)


def recall_without_vectors(gaps) -> str | None:
    """The gap that says a recall ran without its vector search, or None when the search ran."""
    listed = [gap for gap in gaps if isinstance(gap, str)]
    for matches in _WITHOUT_VECTORS:
        found = next((gap for gap in listed if matches(gap)), None)
        if found is not None:
            return found
    return None


def emit_result(result: dict[str, Any], *, diagnostics: HookDiagnostics | None = None, empty: str = "{}") -> None:
    # Codex decodes hook stdout as UTF-8, while a Windows child process may
    # inherit a legacy code-page TextIOWrapper.  ASCII JSON is safe on both
    # sides and json.loads restores the original Unicode values.  An empty
    # answer is written as ``empty`` (``boundary.EMPTY_ANSWER``).
    sys.stdout.write(json.dumps(result, ensure_ascii=True) if result else empty)
    if diagnostics is not None and diagnostics.last_reason:
        sys.stderr.write(f"CODEX_HOOK:{diagnostics.last_reason}\n")
    if diagnostics is not None and diagnostics.capture_stage:
        detail = {
            "stage": diagnostics.capture_stage,
            "disposition": diagnostics.capture_disposition,
            "durability": diagnostics.capture_durability,
            "error_type": diagnostics.capture_error_type,
            "error_code": diagnostics.capture_error_code,
            "elapsed_ms": diagnostics.capture_elapsed_ms,
        }
        # Local operator channel only.  stdout carries the host contract; this
        # line is what a person reads when the contract code is not specific
        # enough to act on.
        if diagnostics.capture_error_detail and diagnostics.capture_error_detail != diagnostics.capture_error_code:
            detail["error_detail"] = diagnostics.capture_error_detail
        sys.stderr.write("CODEX_CAPTURE:" + json.dumps(detail, ensure_ascii=True, separators=(",", ":")) + "\n")
    if diagnostics is not None and diagnostics.recall_error_detail:
        sys.stderr.write(f"CODEX_RECALL:{diagnostics.recall_error_detail}\n")
    if diagnostics is not None and diagnostics.recall_vector_gap:
        sys.stderr.write(f"CODEX_RECALL_VECTOR:{diagnostics.recall_vector_gap}\n")
