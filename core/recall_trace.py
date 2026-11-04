"""Request-local decisions shared by collection, release and packet compilation.

Candidate decisions contain references, numeric scores and fixed reason codes.
The request context stays local until the final fence and is never enqueued.
The ContextVar isolates concurrent hooks without carrying authority.
"""

from __future__ import annotations

import json
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from typing import TYPE_CHECKING
from uuid import uuid4

from ..contracts import ContractError, TrustedContext, utc_timestamp
from .deadline import RequestDeadline, using_request_deadline

if TYPE_CHECKING:
    from .retrieval import SearchContext


RECALL_DIAGNOSTIC_PREFIX = "recall-diag:"
CURRENT_TRACE: ContextVar[RecallTrace | None] = ContextVar("recall_trace", default=None)


@dataclass(frozen=True)
class RefusalContext:
    """Local request context for attempts refused before a search request exists."""

    query: str
    request_id: str
    now: str
    trusted_context: TrustedContext
    mode: str = "auto"


@dataclass
class RecallTrace:
    """One attempted recall, published only after its final private-mode fence."""

    ref: str = field(default_factory=lambda: RECALL_DIAGNOSTIC_PREFIX + uuid4().hex[:16])
    enabled: bool = False
    private_marker: int | None = None
    context: SearchContext | RefusalContext | None = None
    metrics: dict = field(default_factory=dict)
    candidates: dict = field(default_factory=dict)
    background: dict = field(default_factory=dict)
    capture: dict = field(default_factory=lambda: {"state": "not_captured", "refs": []})


def note_candidate(candidate, reason=None, *, obj=None, source_type=None):
    """Retain decisions, channel ranks and the category from scoped metadata or a hydrated object."""
    trace = CURRENT_TRACE.get()
    if trace is None or not trace.enabled:
        return
    key = candidate.key
    if key not in trace.candidates and len(trace.candidates) >= 1024:
        return
    item = trace.candidates.setdefault(
        key,
        dict(kind=candidate.kind, ref=candidate.ref, revision=candidate.revision, channels={}, reason="not_selected"),
    )
    item["channels"][candidate.source] = dict(
        rank=candidate.rank,
        lexical=candidate.lexical_score,
        vector=candidate.vector_score,
    )
    item["fusion_score"] = candidate.fusion_score
    item.setdefault("source_type", candidate.kind)
    if source_type is not None:
        item["source_type"] = source_type
    if obj is not None:
        item["source_type"] = {
            "grounded_session_summary": "session_summary",
            "grounded_task_summary": "task_summary",
            "owner_adopted_assistant_proposal": "adopted_decision",
        }.get(dict(obj.metadata).get("qualification_reason", ""), candidate.kind)
        if obj.origin == "inductive_observation":
            item["source_type"] = "observation"
    if reason is not None:
        item["reason"] = reason


def note_removed(before, after, reason):
    """Record an actual filter decision without recomputing admission rules."""
    trace = CURRENT_TRACE.get()
    if trace is None or not trace.enabled:
        return
    retained = {candidate.key for candidate, _obj in after}
    for candidate, _obj in before:
        if candidate.key not in retained:
            note_candidate(candidate, reason)


def snapshot(_tx, context):
    """Read bounded published counters; queue scans and liveness probes stay off recall."""
    trace = CURRENT_TRACE.get()
    if trace is None or not trace.enabled:
        return
    trusted = context.trusted_context
    trace.background = {"worker_running": None, "snapshot_state": "unavailable"}
    try:
        path = trusted.binding.data_directory / "runtime-worker-status.json"
        with path.open("rb") as stream:
            raw = stream.read(65537)
        value = json.loads(raw) if len(raw) <= 65536 else {}
        state = value.get("recall_state", {})
        groups = state.get("groups")
        if value.get("installation_id") == trusted.binding.installation_id and isinstance(groups, list):
            if len(groups) > 256 or state.get("truncated") is not False:
                raise ValueError("unbounded recall snapshot")
            matched = [
                row
                for row in groups
                if row["scope_id"] in trusted.allowed_scope_ids
                and row["project_id"] in (None, trusted.project_id)
                and row["branch_id"] in (None, trusted.branch_id)
            ]
            if any(
                type(row[key]) is not int or not 0 <= row[key] < 2**63
                for row in matched
                for key in ("pending", "leased", "paused")
            ):
                raise ValueError("invalid recall snapshot")
            trace.background.update(
                pending=sum(row["pending"] for row in matched),
                leased=sum(row["leased"] for row in matched),
                budget_paused=any(row["paused"] for row in matched),
                observed_at=utc_timestamp(state["observed_at"]),
                snapshot_state="published",
            )
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ContractError):
        pass
    refs = list(context.current_source_refs)
    trace.capture = dict(state="persisted" if refs else "not_captured", refs=refs)


def logged_recall(method):
    """Submit after the public result is known; log failures never alter recall."""

    @wraps(method)
    def run(core, *args, **kwargs):
        trace = RecallTrace()
        token = CURRENT_TRACE.set(trace)
        try:
            try:
                result = method(core, *args, **kwargs)
            except ContractError:
                if trace.enabled and trace.context is not None and _error_visible(core, trace):
                    gap = "scope_unbound" if not trace.context.trusted_context.allowed_scope_ids else "recall_error"
                    core.recall_diagnostics.finish(
                        trace, dict(items=[], gaps=[gap], status="unavailable", memory_epoch=None)
                    )
                raise
            if trace.enabled:
                core.recall_diagnostics.finish(trace, result)
            return result
        finally:
            CURRENT_TRACE.reset(token)

    return run


def _error_visible(core, trace):
    """Recheck interrupted attempts; unreadable privacy state never permits logging."""
    try:
        return (
            core._recall_private_state(trace.context.trusted_context, after=trace.private_marker, logging=True)[0]
            is None
        )
    except Exception:  # noqa: BLE001 - privacy is fail-closed even when the truth store fails.
        return False


def record_unbound_attempt(core, context: TrustedContext, query: object) -> None:
    """Record an already refused host scope through the common diagnostic fence."""
    if not context.allowed_scope_ids:
        record_refusal(core, context, query, "scope_unbound")


def record_refusal(core, context: TrustedContext, query: object, reason: str) -> None:
    """Log a fixed pre-retrieval refusal, never interpreting the query or granting access.

    A short outer deadline prevents metadata reads from adding a lock wait to
    a previously empty host exit. Unreadable privacy state leaves no record.
    """
    if type(query) is not str or reason not in {"scope_unbound", "blank_prompt", "source_ref_limit"}:
        return
    try:
        with using_request_deadline(RequestDeadline.from_budget(0.05)):
            mode, marker = core._recall_private_state(context)
            if mode is not None:
                return
            trace = RecallTrace(
                enabled=True,
                private_marker=marker,
                context=RefusalContext(query, core.identifiers.next_id(), utc_timestamp(core.clock.utc_now()), context),
            )
            trace.metrics = dict(retrieval_gaps=(reason,))
            if _error_visible(core, trace):
                core.recall_diagnostics.finish(
                    trace, dict(items=[], gaps=[reason], status="unavailable", memory_epoch=None)
                )
    except Exception:  # noqa: BLE001 - a diagnostic cannot change an already refused host result.
        return


def record_brief(core, context: TrustedContext, brief: dict, reason: str) -> None:
    """Publish prepared references after the confirmation's private-mode fence."""
    if not brief.get("_log_allowed") or not brief.get("_log_checked"):
        return
    try:
        request_id = core.identifiers.next_id()
        trace = RecallTrace(
            enabled=True,
            private_marker=brief["_privacy_marker"],
            context=RefusalContext("", request_id, utc_timestamp(core.clock.utc_now()), context),
        )
        trace.capture = {"state": "not_applicable", "refs": []}
        delivered = []
        for rank, (item, line) in enumerate(zip(brief["items"], brief["_lines"], strict=True), 1):
            if item["kind"] == "handoff":
                item = dict(item, ref=item["task_ref"], revision=item["task_revision"])
            kind = "episode" if item["kind"] in {"task", "handoff"} else "claim"
            # The first rendered column is generated from the current kind and adoption state.
            label = line.split(" | ", 1)[0]
            source_type = "adopted_decision" if label == "decision (owner adopted assistant proposal)" else label
            key = (kind, item["ref"], item["revision"])
            trace.candidates[key] = dict(
                kind=kind,
                ref=item["ref"],
                revision=item["revision"],
                source_type=source_type,
                channels={"session_brief": {"rank": rank}},
                reason=reason,
            )
            if reason == "brief_delivered":
                delivered.append(dict(kind=kind, ref=item["ref"], revision=item["revision"]))
        trace.metrics = dict(phase="session_brief", elapsed_ms=brief["elapsed_ms"], estimated_tokens=brief["tokens"])
        core.recall_diagnostics.finish(trace, dict(items=delivered, gaps=[], status="ok", memory_epoch=brief["_epoch"]))
        brief["_log_request_id"] = request_id
    except Exception:  # noqa: BLE001 - diagnostic failure cannot alter the host response.
        return


def record_injection(core, context: TrustedContext, brief: dict, reason: str) -> None:
    """Publish final usage after release checks; diagnostic history is never a delivery ledger."""
    if (
        not brief.get("_log_allowed")
        or not brief.get("_log_checked")
        or reason
        not in {
            "brief_delivered",
            "injection_delivered",
            "brief_delivery_unconfirmed",
            "injection_delivery_unconfirmed",
        }
    ):
        return
    try:
        usage = brief["_injection"]
        if not usage["tokens"] and not usage.get("request_id"):
            return
        confirmed = reason in {"brief_delivered", "injection_delivered"}
        trace = RecallTrace(
            enabled=True,
            private_marker=brief["_privacy_marker"],
            context=RefusalContext(
                "",
                usage.get("request_id") or brief.get("_log_request_id") or core.identifiers.next_id(),
                utc_timestamp(core.clock.utc_now()),
                context,
            ),
        )
        trace.capture = {"state": "not_applicable", "refs": []}
        delivered = []
        for item in [*brief["items"], *usage["items"]]:
            kind = (
                "episode"
                if item["kind"] in {"task", "handoff"}
                else "claim"
                if item["kind"] in {"procedure", "decision", "preference", "constraint"}
                else item["kind"]
            )
            ref = item["task_ref"] if item["kind"] == "handoff" else item["ref"]
            revision = item["task_revision"] if item["kind"] == "handoff" else item["revision"]
            delivered.append(dict(kind=kind, ref=ref, revision=revision))
        trace.metrics = dict(
            phase="session_injection",
            elapsed_ms=usage.get("elapsed_ms", 0) + brief["elapsed_ms"],
            estimated_tokens=usage["tokens"],
            injection=dict(
                injection_tokens=usage["tokens"],
                session_tokens=usage["session_tokens"] if confirmed else None,
                injection_token_cap=usage["injection_token_cap"],
                session_token_cap=usage["session_token_cap"],
                injection_format=usage["format"],
                accounting_confirmed=confirmed,
                delivery_reason=reason,
            ),
        )
        core.recall_diagnostics.finish(
            trace, dict(items=delivered, gaps=[], status="ok", memory_epoch=brief["_epoch"]), injection=True
        )
    except Exception:  # noqa: BLE001 - lossy diagnostic metadata cannot change a confirmed response.
        return
