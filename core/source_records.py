"""A source as the store keeps it: what a read returns (``StoredSource``, built from a ``source_events`` row by
``stored_source``) and what a write did (``SourceWrite``)."""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..contracts import SourceEvent


@dataclass(frozen=True)
class StoredSource:
    ref: str
    revision: int
    scope_id: str
    session_id: str
    project_id: str | None
    branch_id: str | None
    event: SourceEvent
    content_sha256: str
    suppressed: bool
    capture_gaps: tuple[str, ...] = ()
    import_provenance_sha256: str | None = None
    entry_id: str = "local"


#: The columns a loaded source is built from (``stored_source``).
SOURCE_COLUMNS = (
    "event_id",
    "source_event_key",
    "source_revision",
    "source_group_key",
    "segment_total",
    "scope_id",
    "session_id",
    "project_id",
    "branch_id",
    "origin",
    "role",
    "content",
    "content_sha256",
    "occurred_at",
    "recorded_at",
    "time_precision",
    "capture_state",
    "source_original_origin",
    "dataset_id",
    "extra_json",
    "capture_gaps_json",
    "suppressed",
    "import_provenance_sha256",
    "entry_id",
)


def source_size(loaded) -> int:
    """The text a remembered source holds (``Transaction.remember``)."""
    return 0 if loaded is None else len(loaded[0]["content"]) + len(loaded[0]["extra_json"])


def stored_source(row, segment_count: int | None) -> StoredSource:
    """A source built afresh from its row, so that no reader shares another's ``event``."""
    event = json.loads(row["extra_json"])
    event.pop("_scope_recall_admission", None)  # Internal scheduling never enters source evidence or model input.
    event.update(
        protocol_version="1.1",
        source_event_key=row["source_event_key"],
        source_revision=row["source_revision"],
        origin=row["origin"],
        role=row["role"],
        content=row["content"],
        occurred_at=row["occurred_at"],
        recorded_at=row["recorded_at"],
        time_precision=row["time_precision"],
        capture_state=row["capture_state"],
    )
    for name in ("source_original_origin", "dataset_id"):
        if row[name] is not None:
            event[name] = row[name]
    gaps = list(json.loads(row["capture_gaps_json"]))
    if "segment" in event:
        total = row["segment_total"]
        if total is None or segment_count != total or event["segment"]["truncated"]:
            gaps.append("source_segments_incomplete")
    return StoredSource(
        row["event_id"],
        row["source_revision"],
        row["scope_id"],
        row["session_id"],
        row["project_id"],
        row["branch_id"],
        event,
        row["content_sha256"],
        bool(row["suppressed"]),
        tuple(dict.fromkeys(gaps)),
        row["import_provenance_sha256"],
        row["entry_id"],
    )


@dataclass(frozen=True)
class SourceWrite:
    disposition: str
    ref: str
    revision: int
