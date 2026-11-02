"""Bounded, sanitized recall diagnostics on an independent local channel.

Diagnostic records never store raw query text or item content.  They exist only
for correlating capture, retrieval, and delivery phases within one installation.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .recall_log_storage import LogPolicy, RecallLogStore
from .recall_log_writer import RecallLogWriter
from .recall_trace import CURRENT_TRACE, RECALL_DIAGNOSTIC_PREFIX
from .retrieval import RetrievalResult


@dataclass
class RecallTimings:
    """Content-free milliseconds accumulated per request phase for the diagnostic snapshot."""

    monotonic: Callable[[], float]
    stages: dict[str, float] = field(default_factory=dict)

    @contextmanager
    def measure(self, phase: str):
        """Accumulate repeated rounds of one phase, including interrupted work."""
        started = self.monotonic()
        try:
            yield
        finally:
            self.stages[phase] = self.stages.get(phase, 0.0) + max(0.0, (self.monotonic() - started) * 1000)

    def snapshot(self) -> tuple[tuple[str, str], ...]:
        """Return the bounded timing vocabulary carried by retrieval diagnostics."""
        return tuple((name, f"{elapsed:.3f}") for name, elapsed in self.stages.items())


@dataclass(frozen=True)
class RecallDiagnosticRecord:
    """Content-free compile metrics with structured attempt metadata."""

    ref: str
    installation_id: str
    session_id: str
    request_id: str
    memory_epoch: int | None
    phase: str
    status: str
    retrieval_gaps: tuple[str, ...]
    compile_gaps: tuple[str, ...]
    items_delivered: int
    items_dropped_stale: int
    items_dropped_budget: int
    elapsed_ms: int
    deadline_remaining_ms: int | None
    rendered_bytes: int = 0
    estimated_tokens: int = 0
    budget_tokens: int = 0
    stage_timings: tuple[tuple[str, str], ...] = ()

    details: dict = field(default_factory=dict)

    def to_public(self) -> dict[str, object]:
        return {
            **self.details,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "stage_elapsed_ms": dict(self.stage_timings),
            "phase": self.phase,
            "status": self.status,
            "retrieval_gaps": list(self.retrieval_gaps[:16]),
            "compile_gaps": list(self.compile_gaps[:16]),
            "items_delivered": self.items_delivered,
            "items_dropped_stale": self.items_dropped_stale,
            "items_dropped_budget": self.items_dropped_budget,
            "elapsed_ms": self.elapsed_ms,
            "deadline_remaining_ms": self.deadline_remaining_ms,
            "rendered_bytes": self.rendered_bytes,
            "estimated_tokens": self.estimated_tokens,
            "budget_tokens": self.budget_tokens,
        }


class RecallDiagnostics:
    """One persistent diagnostic stream; only viewing may wait for queued writes."""

    def __init__(self, path: Path, policy: LogPolicy | None = None) -> None:
        self.path = path
        self.store = RecallLogStore(path, policy)
        self.writer = RecallLogWriter(self.store)

    def record(self, **metrics) -> str:
        """Stage compile metrics in the current attempt, or enqueue a standalone compile."""
        trace = CURRENT_TRACE.get()
        if trace is not None:
            trace.metrics = metrics
            return trace.ref
        from uuid import uuid4

        ref = RECALL_DIAGNOSTIC_PREFIX + uuid4().hex[:16]
        self.writer.submit(dict(ref=ref, timestamp=datetime.now(UTC).isoformat(), **metrics))
        return ref

    def finish(self, trace, result) -> None:
        """Enqueue final references after overflow and privacy fences, never packet text."""
        try:
            record = attempt_record(trace, result)
            self.writer.submit(record)
        except Exception:  # noqa: BLE001 - logging cannot change a recall result.
            self.writer.write_failures += 1

    def expire(self, now: str) -> None:
        """Schedule physical expiry independently of whether another recall succeeds."""
        if self.path.is_file():
            self.writer.submit(dict(prune_at=now))

    def flush(self, timeout: float = 2.0) -> bool:
        """Drain for inspection or orderly shutdown; never called by recall."""
        return self.writer.flush(timeout)

    def get(self, ref: str | None) -> RecallDiagnosticRecord | None:
        """Read a diagnostic ref, including compiler metrics staged in this attempt."""
        trace = CURRENT_TRACE.get()
        if trace is not None and trace.ref == ref and trace.metrics:
            return diagnostic(dict(ref=ref, **trace.metrics))
        if type(ref) is not str or not ref:
            return None
        self.flush()
        rows = self.store.list(ref=ref, limit=1)
        return diagnostic(rows[0]) if rows else None


def diagnostic(value: dict) -> RecallDiagnosticRecord:
    """Retain the existing metric contract while exposing richer attempt metadata."""
    names = RecallDiagnosticRecord.__dataclass_fields__
    fields = {key: item for key, item in value.items() if key in names and key != "details"}
    fields["details"] = {key: item for key, item in value.items() if key not in names}
    return RecallDiagnosticRecord(**fields)


def attempt_record(trace, result: dict | RetrievalResult) -> dict:
    """Normalize packet and raw retrieval returns to the same content-free history."""
    context = trace.context
    trusted = context.trusted_context
    packet = result if isinstance(result, dict) else None
    delivered = result["items"] if isinstance(result, dict) else [asdict(item) for item in result.items]
    selected = {}
    for rank, item in enumerate(delivered, 1):
        key = next(
            (key for key in trace.candidates if key[1:] == (item["ref"], item["revision"])),
            ("claim" if item["kind"] == "procedure" else item["kind"], item["ref"], item["revision"]),
        )
        selected[key] = (rank, "delivered")
        if item.get("qualification_reason") == "Other-session matches":
            for ref in item["evidence_refs"]:
                name, revision = ref.rsplit("@", 1)
                selected[("event", name, int(revision))] = (rank, "overflow")
    items = []
    candidates = dict(trace.candidates)
    for key in selected:
        candidates.setdefault(key, dict(kind=key[0], ref=key[1], revision=key[2], channels={}))
    scores = (
        {candidate.key: candidate.fusion_score for candidate in result.candidates}
        if not isinstance(result, dict)
        else {}
    )
    for key, candidate in candidates.items():
        if key in scores:
            candidate = dict(candidate, fusion_score=scores[key])
        rank, reason = selected.get(key, (None, candidate.get("reason", "not_selected")))
        items.append(dict(candidate, rank=rank, reason=reason))
    gaps = tuple(result["gaps"] if isinstance(result, dict) else result.gaps)
    reasons = attempt_reasons(trace, items, gaps)
    metrics = dict(
        installation_id=trusted.binding.installation_id,
        session_id=trusted.session_id,
        request_id=context.request_id,
        memory_epoch=result.get("memory_epoch") if isinstance(result, dict) else result.memory_epoch,
        phase="compile" if packet is not None else "retrieval",
        status=result["status"] if isinstance(result, dict) else result.coverage,
        retrieval_gaps=(),
        compile_gaps=(),
        items_delivered=len(delivered),
        items_dropped_stale=0,
        items_dropped_budget=0,
        elapsed_ms=0,
        deadline_remaining_ms=None,
    )
    metrics.update(trace.metrics)
    # Candidate identities live only in the visibility-checked item list. Gap
    # payloads can also contain references, so only their fixed names persist.
    for name in ("retrieval_gaps", "compile_gaps"):
        metrics[name] = tuple(dict.fromkeys(gap.split(":", 1)[0] for gap in metrics.get(name, ())))
    query = (
        dict(refs=list(trace.capture["refs"]))
        if trace.capture["refs"]
        else dict(length=len(context.query), sha256=hashlib.sha256(context.query.encode("utf-8")).hexdigest())
    )
    return dict(
        metrics,
        ref=trace.ref,
        timestamp=context.now,
        entry_id=trusted.entry_id or trusted.binding.agent_id,
        scope_ids=sorted(trusted.allowed_scope_ids),
        project_id=trusted.project_id,
        branch_id=trusted.branch_id,
        scope_bound=bool(trusted.allowed_scope_ids),
        mode=context.mode,
        query=query,
        capture=trace.capture,
        background=trace.background,
        items=items,
        reasons=reasons,
    )


def attempt_reasons(trace, items, gaps) -> list[str]:
    """Stable explanation vocabulary; a background delay never claims it caused a miss."""
    reasons = set()
    if not trace.context.trusted_context.allowed_scope_ids:
        reasons.add("scope_unbound")
    if trace.capture["state"] == "not_captured":
        reasons.add("capture_missing")
    if trace.background.get("pending", 0):
        reasons.add("background_backlog")
    if trace.background.get("budget_paused"):
        reasons.add("budget_paused")
    if not any(item["reason"] in {"delivered", "overflow"} for item in items):
        reasons.add("retrieval_empty" if not items else "all_candidates_dropped")
        reasons.update(item["reason"] for item in items)
    for gap in gaps:
        if gap == "blank_prompt":
            reasons.add("prompt_skipped")
        elif gap == "source_ref_limit":
            reasons.add("source_ref_limit")
        elif gap.startswith("auto_relevance_below_floor"):
            reasons.add("threshold")
        elif gap.startswith("auto_recall_skipped"):
            reasons.add("prompt_skipped")
        elif gap.startswith("deadline_exceeded"):
            reasons.add("deadline")
        elif gap.startswith("sqlite_unavailable"):
            reasons.add("store_unavailable")
    return sorted(reasons)
