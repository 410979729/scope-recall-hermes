"""Read-only owner inventory of secret-bearing stored versions.

SQL filters candidate text inside bounded primary-key windows. Each window's
transaction ends before Python scans or provenance reads, so a large inventory
neither retains a long SQLite snapshot nor holds up writers and checkpoints.
"""

from __future__ import annotations

import json

from ..contracts import ContractError
from .management_query import partition, witnesses
from .model_content import stored_item_pattern_names
from .secret_patterns import secret_prefilter_sql

BATCH_SIZE = 256
# Each body is the structured text an inspection can expose, including historical versions.
_TABLES = (
    (
        "event",
        "source_events",
        "event_id",
        "source_revision",
        "recorded_at",
        None,
        "json_object('content',v.content,'extra',json(v.extra_json))",
    ),
    ("claim", "claim_versions", "claim_id", "revision", "recorded_from", "claims", "v.payload_json"),
    ("episode", "episode_versions", "episode_id", "revision", "recorded_at", "episodes", "v.resume_json"),
    (
        "artifact",
        "artifact_versions",
        "artifact_id",
        "revision",
        "recorded_at",
        "artifacts",
        "json_object('label',v.label,'descriptions',json(v.description_json))",
    ),
    (
        "reference",
        "reference_versions",
        "reference_id",
        "revision",
        "recorded_at",
        "reference_bindings",
        "v.payload_json",
    ),
)


def _batch(storage, context, spec, after):
    """Read one indexed key window, transferring content only for SQL candidates."""
    _kind, table, key, revision, stamp, head, body = spec
    alias = "h" if head else "v"
    join = f" CROSS JOIN {head} h ON h.{key}=v.{key}" if head else ""
    scopes = sorted(context.allowed_scope_ids)
    predicate, words = secret_prefilter_sql("scan_text")
    with storage.read(context) as tx:
        connection = tx._check()
        # Scope indexes can sort the entire audience for every window. Pin the primary-key walk and keep
        # versions before their heads, so seeking and ordering remain bounded by the cursor's progress.
        primary = connection.execute("SELECT name FROM pragma_index_list(?) WHERE origin='pk'", (table,)).fetchone()
        if primary is None:
            raise ContractError("SCHEMA_UNSUPPORTED", "secret_inventory_index")
        index = primary[0].replace('"', '""')
        return connection.execute(
            f"WITH key_window AS MATERIALIZED ("
            f'SELECT * FROM {table} v INDEXED BY "{index}" '
            f"WHERE (v.{key},v.{revision})>(?,?) ORDER BY v.{key},v.{revision} LIMIT ?),"
            f"batch AS MATERIALIZED ("
            f"SELECT v.{key} AS ref,v.{revision} AS revision,v.{stamp} AS time,"
            f"{alias}.project_id,{alias}.branch_id,"
            f"{body} AS body,lower({body}) AS scan_text,"
            f"({alias}.read_blocked=0 AND {alias}.scope_id IN ({','.join('?' for _ in scopes)})) AS visible "
            f"FROM key_window v{join}) "
            f"SELECT ref,revision,time,project_id,branch_id,CASE WHEN visible AND {predicate} "
            "THEN body END AS scan_content "
            "FROM batch ORDER BY ref,revision",
            (*after, BATCH_SIZE, *scopes, *words),
        ).fetchall()


def _finding(storage, context, kind, row, patterns):
    """Attach source locations without returning source content or match text."""
    with storage.read(context) as tx:
        view = partition(tx, context, row["project_id"], row["branch_id"])
        sources = witnesses(view, dict(kind=kind, ref=row["ref"], revision=row["revision"]))
        first = sources[0] if sources else None
        return dict(
            ref=row["ref"],
            kind=kind,
            revision=row["revision"],
            origin=first.event["origin"] if first else "origin_unknown",
            entry=first.entry_id if first else None,
            session=first.session_id if first else None,
            time=first.event["occurred_at"] or first.event["recorded_at"] if first else row["time"],
            patterns=list(patterns),
        )


def find_secrets(storage, context):
    """Scan authorized readable versions in short reads, without changing stored data.

    Muted objects and historical versions are included. Each batch sees committed
    data at its own read; run again to include concurrent captures or revisions
    inserted behind the primary-key cursor. ``rows_read`` includes SQL rejects;
    ``candidates_scanned`` counts versions passed to the canonical Python scan.
    """
    items, rows_read, candidates_scanned = [], 0, 0
    for spec in _TABLES:
        after = ("", 0)
        while rows := _batch(storage, context, spec, after):
            rows_read += len(rows)
            after = (rows[-1]["ref"], rows[-1]["revision"])
            for row in rows:
                if row["scan_content"] is None:
                    continue
                candidates_scanned += 1
                patterns = stored_item_pattern_names(json.loads(row["scan_content"]))
                if patterns:
                    items.append(_finding(storage, context, spec[0], row, patterns))
    return dict(items=items, rows_read=rows_read, candidates_scanned=candidates_scanned)
