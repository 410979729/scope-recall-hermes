"""State carried through the legacy conversion pipeline.

One ``Conversion`` is built per ``migrate_legacy`` call and handed to every
stage in order; each stage reads what earlier stages filled in and adds its
own. ``Blocked`` carries a finished blocked report out of any pre-write stage.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .legacy_tianshu_compat import MemoryStorageAuthority

Row = dict[str, Any]


class Blocked(Exception):
    """A pre-write stage produced its blocked report; nothing was written."""

    def __init__(self, report: dict[str, Any]) -> None:
        super().__init__(report["completion_status"])
        self.report = report


@dataclass
class Conversion:
    """Everything one conversion decides and produces, grouped by stage."""

    # The validated request.
    batch_key: str
    agent_id: str
    installation_id: str
    source_path: Path
    target_dir: Path
    catalog: dict[str, Any]
    explicit_scopes: list[str] | None
    handoff: dict[str, Any] | None
    audience_scopes: dict[str, str]
    source_scope_map: Mapping[str, str] | None
    single_scope_to: str | None
    memory_reader_contract: str | None
    bridge_archive_path: str | Path | None
    import_ledger_archive_path: str | Path | None
    project_memberships: bool

    # The frozen legacy snapshot, as dict rows keyed by legacy table name.
    tables: set[str] = field(default_factory=set)
    rows: dict[str, list[Row]] = field(default_factory=dict)
    bridge_receipt: dict[str, Any] | None = None
    import_ledger_receipt: dict[str, Any] | None = None

    # The scope plan: which legacy scopes are converted and where they land.
    requested_source: frozenset[str] = frozenset()
    requested: frozenset[str] = frozenset()
    scope_mapping: dict[str, str] = field(default_factory=dict)
    applied_scope_map: Mapping[str, str] | None = None
    memory_authority: MemoryStorageAuthority | None = None
    orphan_bridge_scope: str | None = None
    digest_audit_scope: str | None = None

    # Findings that reach the report.
    report_rows: list[Row] = field(default_factory=list)
    unknown_tables: list[str] = field(default_factory=list)
    derived_tables: list[str] = field(default_factory=list)
    schema_gaps: list[Row] = field(default_factory=list)
    redactions: int = 0
    permission_gaps: int = 0

    # Archived source events and the Core ids legacy identities resolve to.
    sources: list[Row] = field(default_factory=list)
    journal_refs: dict[str, str] = field(default_factory=dict)
    memory_refs: dict[str, str] = field(default_factory=dict)
    memory_items: dict[str, Row] = field(default_factory=dict)
    archives: dict[tuple[str, str], str] = field(default_factory=dict)
    episode_refs: dict[str, str] = field(default_factory=dict)
    deletion_specs: list[Row] = field(default_factory=list)

    # Write receipts.
    inserted: defaultdict[str, int] = field(default_factory=lambda: defaultdict(int))
    mapped_facts: set[str] = field(default_factory=set)
    mapped_procedures: set[str] = field(default_factory=set)
    fact_claim_refs: dict[str, str] = field(default_factory=dict)
    procedure_collisions: dict[str, tuple[str, ...]] = field(default_factory=dict)
    membership_audit: dict[str, Any] | None = None
    lifecycle_suppression: dict[str, Any] | None = None

    def unmapped(self, table: str, key: str, reason: str, **fields: Any) -> Row:
        """Record one row the conversion could not map losslessly."""
        item = {"table": table, "key": key, "reason": reason, **fields, "auto_promoted": False}
        self.report_rows.append(item)
        return item

    def installation_handoff(self) -> dict[str, Any]:
        return {
            **(self.handoff or {}),
            "source_scope_mapping": dict(self.applied_scope_map or {}),
            "resolved_scope_mapping": self.scope_mapping,
        }
