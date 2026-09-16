"""Bounded, sanitized recall diagnostics on an independent local channel.

Diagnostic records never store raw query text or item content.  They exist only
for correlating capture, retrieval, and delivery phases within one installation.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import hashlib
import json
import threading
from typing import Deque

_MAX_RECORDS = 64
_MAX_FIELD_LEN = 240


@dataclass(frozen=True)
class RecallDiagnosticRecord:
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

    def to_public(self) -> dict[str, object]:
        return {
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
    """Process-local diagnostics keyed by installation and session."""

    def __init__(self) -> None:
        self._records: Deque[RecallDiagnosticRecord] = deque()
        self._by_ref: dict[str, RecallDiagnosticRecord] = {}
        self._counter = 0
        self._lock = threading.RLock()

    def record(
        self,
        *,
        installation_id: str,
        session_id: str,
        request_id: str,
        memory_epoch: int | None,
        phase: str,
        status: str,
        retrieval_gaps: tuple[str, ...],
        compile_gaps: tuple[str, ...],
        items_delivered: int,
        items_dropped_stale: int,
        items_dropped_budget: int,
        elapsed_ms: int,
        deadline_remaining_ms: int | None,
        rendered_bytes: int = 0,
        estimated_tokens: int = 0,
        budget_tokens: int = 0,
    ) -> str:
        with self._lock:
            self._counter += 1
            digest = hashlib.sha256(
                json.dumps(
                    {
                        "installation_id": installation_id,
                        "session_id": session_id,
                        "request_id": request_id[:100],
                        "memory_epoch": memory_epoch,
                        "phase": phase[:80],
                        "status": status[:40],
                        "counter": self._counter,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()[:16]
            ref = f"recall-diag:{digest}"
            entry = RecallDiagnosticRecord(
                ref[:_MAX_FIELD_LEN],
                installation_id[:_MAX_FIELD_LEN],
                session_id[:_MAX_FIELD_LEN],
                request_id[:100],
                memory_epoch,
                phase[:80],
                status[:40],
                tuple(gap[:1000] for gap in retrieval_gaps[:16]),
                tuple(gap[:1000] for gap in compile_gaps[:16]),
                max(0, items_delivered),
                max(0, items_dropped_stale),
                max(0, items_dropped_budget),
                max(0, elapsed_ms),
                deadline_remaining_ms,
                rendered_bytes,
                estimated_tokens,
                budget_tokens,
            )
            self._records.append(entry)
            self._by_ref[entry.ref] = entry
            while len(self._records) > _MAX_RECORDS:
                evicted = self._records.popleft()
                self._by_ref.pop(evicted.ref, None)
            return entry.ref

    def get(self, ref: str | None) -> RecallDiagnosticRecord | None:
        if type(ref) is not str or not ref:
            return None
        with self._lock:
            return self._by_ref.get(ref[:_MAX_FIELD_LEN])

    @property
    def record_count(self) -> int:
        with self._lock:
            return len(self._records)

    @property
    def index_count(self) -> int:
        with self._lock:
            return len(self._by_ref)
