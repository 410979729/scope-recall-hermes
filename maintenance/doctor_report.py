"""The doctor's report: one host's diagnosis, field by field, and the checks that made it."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from scope_recall._version import __version__

HostChoice = Literal["hermes", "codex", "claude-code", "workbuddy", "dsh"]


@dataclass
class DoctorReport:
    host: HostChoice
    status: str
    python_executable: str | None = None
    package_ok: bool = False
    package_source: str | None = None
    package_version: str | None = None
    package_path: str | None = None
    expected_package_version: str = __version__
    binding_ok: bool = False
    database_present: bool = False
    schema_version: int | None = None
    #: ``wal`` on every store this release has opened; readers and the writer coexist.
    journal_mode: str | None = None
    memory_epoch: int | None = None
    pending_work: int | None = None
    failed_work: int | None = None
    needs_review_work: int = 0
    terminal_failed_work: int | None = None
    leased_work: int | None = None
    oldest_pending_at: str | None = None
    oldest_pending_age_seconds: float | None = None
    work_error_counts: dict[str, int] = field(default_factory=dict)
    recent_work_errors: list[dict[str, Any]] = field(default_factory=list)
    #: Model answers cut off at the output limit in the last hour.
    recent_output_truncations: int = 0
    capture_inbox: int = 0
    capture_inbox_blocked: int = 0
    #: Of those, rows a replay gave up after its tries (``retry-failures --apply`` returns them to it).
    capture_inbox_given_up: int = 0
    extraction_outcomes: dict[str, int] = field(default_factory=dict)
    autostart_status: str = "not_registered"
    worker_status: dict[str, Any] = field(default_factory=dict)
    sources: int | None = None
    #: Bytes of memory.sqlite3 with its journal, and of everything under vectors/.
    store_bytes: int | None = None
    vector_bytes: int | None = None
    #: Sources that entered the store in the last day and the last week.
    sources_last_24h: int | None = None
    sources_last_7d: int | None = None
    #: runtime-config.json ``storage_budget_bytes``; 0 when none is set.
    storage_budget_bytes: int = 0
    source_only_sources: int | None = None
    deferred_sources: int | None = None
    oldest_deferred_at: str | None = None
    candidate_pending_evaluation: int = 0
    candidate_waiting_evidence: int = 0
    candidate_dormant: int = 0
    candidate_blocked: int = 0
    candidate_resolved: int = 0
    candidate_archived_other: int = 0
    candidate_failed: int = 0
    candidate_budget_paused: int = 0
    candidate_capability_unavailable: int = 0
    candidate_oldest_waiting_at: str | None = None
    host_registration_status: str = "pending"
    hook_trust_status: str = "unknown"
    index_metadata: dict[str, Any] = field(default_factory=dict)
    ledger_headroom: dict[str, Any] = field(default_factory=dict)
    running_code: dict[str, Any] = field(default_factory=dict)
    package_health: dict[str, Any] = field(default_factory=dict)
    candidate_settling: dict[str, int] = field(default_factory=dict)
    #: The store's re-embed run (``respace-embeddings``), or ``None`` when none was started.
    embedding_respace: dict[str, Any] | None = None
    #: The embedding queue (pending, failed, the oldest pending), and with an external route the provider's hold
    #: and its answers over the last day (``doctor_store.check_embedding_health``).
    embedding_health: dict[str, Any] = field(default_factory=dict)
    #: Work and candidates of any partition that have waited more than ``UNREACHED_HOURS``, by partition
    #: (``doctor_store.check_unreached``).
    unreached: list[dict[str, Any]] = field(default_factory=list)
    #: For a home attached to a shared store: the store's root, and this home's
    #: entry.  Everything else in the report is then the shared store's.
    shared_store: dict[str, str] = field(default_factory=dict)
    capability_gaps: list[str] = field(default_factory=list)
    checks: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """The public JSON shape: the fields above, in this order."""
        return asdict(self)


def record_check(report: DoctorReport, name: str, result: str, detail: str = "") -> None:
    item = {"name": name, "result": result}
    if detail:
        item["detail"] = detail
    report.checks.append(item)
