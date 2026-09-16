"""Legacy rows become sanitized ``source_events`` rows; nothing is written here.

Every legacy record that carries content or history is archived as an imported
source event so later authority (claims, episodes, deletions) can cite it by a
stable Core id instead of a legacy row identity.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import re
from typing import Any, Mapping, cast

from scope_recall.maintenance.legacy_tianshu_compat import resolve_memory_scope

from .legacy_catalog import _DIGEST_TABLES, _HISTORY
from .legacy_plan import Conversion, Row
from .migration_records import (
    LEGACY_BASELINE, _canon, _digest, _json, _recorded, _safe, _safe_text, _stable, _time,
)

SOURCE_EVENT_FIELDS = (
    "event_id",
    "source_event_key",
    "source_revision",
    "source_group_key",
    "segment_index",
    "segment_total",
    "scope_id",
    "session_id",
    "project_id",
    "branch_id",
    "origin",
    "role",
    "content",
    "content_sha256",
    "event_sha256",
    "occurred_at",
    "recorded_at",
    "persisted_at",
    "time_precision",
    "capture_state",
    "source_original_origin",
    "dataset_id",
    "import_provenance_sha256",
    "extra_json",
    "capture_gaps_json",
    "read_blocked",
    "suppressed",
)

_ROLE_ORIGINS = {
    "user": ("user", "human_direct"),
    "human": ("user", "human_direct"),
    "assistant": ("assistant", "assistant_visible"),
    "model": ("assistant", "assistant_visible"),
    "tool": ("tool", "tool_observation"),
    "function": ("tool", "tool_observation"),
    "document": ("document", "external_document"),
    "doc": ("document", "external_document"),
    "system": ("system", "host_generated"),
}
_SCOPE_MODES = ("local", "shared", "shared_pool")
_JOURNAL_EXTRA = (
    "turn_number", "platform", "user_id", "chat_id", "thread_id", "agent_identity", "agent_workspace",
)
_DIGEST_EXTRA = {
    "memory_digest_sources": {
        "message_ids_namespace": "hermes_external_session_messages",
        "source_hash_meaning": "sha1_of_candidate_content",
        "evidence_resolution": "unresolved",
    },
    "nightly_digest_quarantine": {"retention_kind": "rejected_candidate_hash"},
    "nightly_digest_runs": {
        "retention_kind": "run_metadata_path",
        "deleted_counter_is_aggregate_only": True,
    },
}


# --- row readers -----------------------------------------------------------

def _role_origin(role: object) -> tuple[str, str]:
    return _ROLE_ORIGINS.get(str(role or "").lower(), ("unknown", "origin_unknown"))


def _metadata(row: Row) -> dict[str, Any]:
    value = _json(row.get("metadata"), {})
    return value if isinstance(value, dict) else {}


def _text_or_none(value: object) -> str | None:
    return str(value) if value is not None else None


def _scope_value(row: Row) -> str | None:
    """The row's own scope, or None when the column is absent or empty."""
    value = row.get("scope_id")
    return None if value is None or value == "" else str(value)


def _by_id(rows: list[Row], key: str) -> dict[str, Row]:
    return {str(row.get(key)): row for row in rows}


def _json_list(value: object) -> list[object]:
    parsed = _json(value, [])
    return parsed if isinstance(parsed, list) else []


def _evidence_items(value: object) -> list[dict[str, Any]]:
    return [item for item in _json_list(value) if isinstance(item, dict)]


def _safe_list(value: object) -> list[str]:
    return [
        _safe_text(item)[0] if isinstance(item, str) else _canon(_safe(item))
        for item in _json_list(value)
    ]


def _anchor_ref(anchor: dict[str, Any]) -> object:
    return anchor.get("source_ref") or anchor.get("source_id") or anchor.get("ref")


def journal_links(rows: dict[str, list[Row]]) -> dict[str, list[str]]:
    """memory id -> journal ids from the legacy M:N link table, in row order."""
    links: dict[str, list[str]] = defaultdict(list)
    for row in rows["memory_journal_sources"]:
        links[str(row.get("memory_id"))].append(str(row.get("journal_entry_id")))
    return links


def _scope(row: Row) -> dict[str, Any]:
    """Use resolved old scope_id; shared_scope_id alone is not a block."""
    metadata = _metadata(row)
    source_scope = str(row.get("__legacy_source_scope_id") or row.get("scope_id") or "legacy-scope")
    explicit = str(metadata.get("scope_mode") or "").strip().lower()
    descriptors = {
        "local": str(metadata.get("runtime_scope_id") or source_scope),
        "shared": str(metadata.get("shared_scope_id") or row.get("shared_scope_id") or ""),
        "shared_pool": str(metadata.get("shared_pool_scope_id") or row.get("shared_pool_scope_id") or ""),
    }
    gap = ""
    if explicit in _SCOPE_MODES:
        if descriptors[explicit] != source_scope:
            gap = f"explicit_{explicit}_scope_mismatch"
    elif explicit:
        gap = "unsupported_scope_mode"
    return {
        "row_scope_id": str(row.get("scope_id") or "legacy-scope"),
        "source_scope_id": source_scope,
        "runtime_scope_id": descriptors["local"],
        "shared_scope_id": descriptors["shared"],
        "shared_pool_scope_id": descriptors["shared_pool"],
        "scope_mode": explicit if explicit in _SCOPE_MODES else "legacy_row_scope",
        "gap": gap,
    }


def _map_scope_rows(
    rows: list[Row],
    mapping: Mapping[str, str],
    *,
    default_source_scope: str | None = None,
) -> list[Row]:
    """Rewrite scope_id through the handoff map, remembering the source scope."""
    mapped: list[Row] = []
    for row in rows:
        source = _scope_value(row)
        if source is None and default_source_scope is not None:
            row = {**row, "scope_id": default_source_scope, "__legacy_default_scope": default_source_scope}
            source = default_source_scope
        if source is None:
            mapped.append(row)
            continue
        copy = {**row, "__legacy_source_scope_id": source}
        target = mapping.get(source)
        if target is None:
            copy["__scope_mapping_gap"] = source
        else:
            copy["scope_id"] = target
        mapped.append(copy)
    return mapped


def _resolve(
    raw: object,
    source_type: object,
    journals: dict[str, str],
    memories: dict[str, str],
    archives: dict[tuple[str, str], str],
) -> str | None:
    """Resolve a legacy evidence reference to an archived Core event id."""
    value, kind = str(raw or "").strip(), str(source_type or "").lower().strip()
    if (kind, value) in archives:
        return archives[(kind, value)]
    if value in journals:
        return journals[value]
    if value in memories:
        return memories[value]
    tail = re.split(r"[:/]+", value)[-1]
    if kind in {"memory", "memory_id", "fact_memory"}:
        return memories.get(tail)
    if kind in {"journal", "journal_entry", "event", "source"}:
        return journals.get(tail)
    return journals.get(tail) or memories.get(tail)


# --- source events ---------------------------------------------------------

def _source_event(
    table: str,
    identity: str,
    row: Row,
    scope: dict[str, Any],
    content: str,
    kind: str,
    original: str,
    extra: dict[str, Any],
    redacted: bool,
) -> Row:
    key = f"legacy:{table}:{identity}"
    stamp = row.get("created_at") or row.get("updated_at") or row.get("started_at")
    occurred, precision = _time(stamp)
    recorded = _recorded(row.get("updated_at") or row.get("created_at") or row.get("started_at"))
    role = _role_origin(row.get("role"))[0] if table == "journal_entries" else "unknown"
    body = {
        "protocol_version": "1.1",
        "source_event_key": key,
        "source_revision": 1,
        "origin": "imported",
        "role": role,
        "content": content,
        "occurred_at": occurred,
        "recorded_at": recorded,
        "time_precision": precision,
        "capture_state": "complete",
        "evidence_refs": [],
    }
    extra_payload = {
        "legacy_table": table,
        "legacy_id": identity,
        "source_kind": kind,
        "redaction_applied": redacted,
        "scope_authorization": scope,
        **cast(dict[str, Any], _safe(extra)),
    }
    return {
        "event_id": _stable("event", f"{table}:{identity}"),
        "source_event_key": key,
        "source_revision": 1,
        "source_group_key": key,
        "segment_index": 0,
        "segment_total": None,
        "scope_id": scope["row_scope_id"],
        "session_id": str(row.get("session_id") or f"legacy-session-{scope['row_scope_id']}"),
        "project_id": _text_or_none(row.get("project_id")),
        "branch_id": _text_or_none(row.get("branch_id")),
        "origin": "imported",
        "role": role,
        "content": content,
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "event_sha256": _digest(body),
        "occurred_at": occurred,
        "recorded_at": recorded,
        "persisted_at": recorded,
        "time_precision": precision,
        "capture_state": "complete",
        "source_original_origin": original,
        "dataset_id": "legacy-578b",
        "import_provenance_sha256": _digest({"baseline": LEGACY_BASELINE, "key": key}),
        "extra_json": _canon(extra_payload),
        "capture_gaps_json": _canon([scope["gap"]] if scope["gap"] else []),
        "read_blocked": int(bool(scope["gap"])),
        "suppressed": 0,
        "scope_gap": scope["gap"],
    }


def archive(
    cv: Conversion,
    table: str,
    identity: str,
    row: Row,
    content: str,
    kind: str,
    original: str,
    extra: dict[str, Any],
    scope: dict[str, Any],
    redacted: bool = False,
) -> Row:
    """Queue one legacy record as an imported source event; scope gaps block."""
    cv.redactions += int(redacted)
    cv.permission_gaps += int(bool(scope["gap"]))
    item = _source_event(table, identity, row, scope, content, kind, original, extra, redacted)
    cv.sources.append(item)
    if scope["gap"]:
        cv.unmapped(table, item["source_event_key"], scope["gap"], scope_authorization=_safe(scope))
    return item


def _archive_record(cv: Conversion, table: str, identity: str, scope_row: Row, row: Row, kind: str, scope: dict[str, Any] | None = None, **extra: Any) -> Row:
    """Archive a row whose content is its own sanitized canonical JSON."""
    content, changed = _safe_text(_canon(_safe(row)))
    item = archive(
        cv, table, identity, {**scope_row, "id": identity}, content, kind, "origin_unknown",
        {"legacy_row": _safe(row), **extra}, scope if scope is not None else _scope(scope_row), changed,
    )
    cv.archives[(table, identity)] = item["event_id"]
    return item


def archive_journal(cv: Conversion) -> None:
    for row in cv.rows["journal_entries"]:
        role, original = _role_origin(row.get("role"))
        content, changed = _safe_text(row.get("content"))
        extra = {"role": role, **{key: row.get(key) for key in _JOURNAL_EXTRA}, "metadata": _metadata(row)}
        item = archive(
            cv, "journal_entries", str(row.get("id") or "unknown"), row, content,
            "raw_event", original, extra, _scope(row), changed,
        )
        cv.journal_refs[str(row.get("id"))] = item["event_id"]


def archive_memories(cv: Conversion) -> None:
    links = journal_links(cv.rows)
    for row in cv.rows["memories"]:
        content, changed_content = _safe_text(row.get("content") or row.get("summary"))
        summary, changed_summary = _safe_text(row.get("summary"))
        meta = _metadata(row)
        ids = list(links.get(str(row.get("id")), []))
        for ref in _json_list(meta.get("journal_entry_ids")):
            if str(ref) not in ids:
                ids.append(str(ref))
        scope = _scope(row)
        if cv.memory_authority is not None:
            scope = resolve_memory_scope(row, scope, authority=cv.memory_authority)
        extra = {
            "summary": summary,
            "source": row.get("source") or "",
            "target": row.get("target") or "",
            "legacy_journal_ids": ids,
            "legacy_metadata": meta,
        }
        item = archive(
            cv, "memories", str(row.get("id") or "unknown"), row, content,
            "durable_memory_evidence", "origin_unknown", extra, scope, changed_content or changed_summary,
        )
        cv.memory_refs[str(row.get("id"))] = item["event_id"]
        cv.memory_items[str(row.get("id"))] = item


def archive_fact_records(cv: Conversion) -> None:
    """Facts and their evidence rows are archived verbatim; claims cite them."""
    facts = _by_id(cv.rows["fact_claims"], "claim_id")
    for row in cv.rows["fact_claims"]:
        identity = str(row.get("claim_id") or row.get("id") or "unknown")
        _archive_record(cv, "fact_claims", identity, row, row, "legacy_fact_record")
    for row in cv.rows["fact_claim_evidence"]:
        identity = str(row.get("evidence_id") or row.get("claim_id") or "unknown")
        scope_row = row if row.get("scope_id") else _inherit(row, facts.get(str(row.get("claim_id")), {}))
        archive_row = {**row, "project_id": scope_row.get("project_id"), "branch_id": scope_row.get("branch_id")}
        _archive_record(cv, "fact_claim_evidence", identity, archive_row, row, "legacy_fact_record", _scope(scope_row))


def _inherit(row: Row, parent: Row) -> Row:
    return {**row, "scope_id": parent.get("scope_id"), "project_id": parent.get("project_id"), "branch_id": parent.get("branch_id")}


def report_target_tombstones(cv: Conversion) -> None:
    for row in cv.rows["privacy_purge_tombstones"]:
        identity = _digest({key: row.get(key) for key in ("operation_id", "target_hash", "content_hash", "erased_at")})
        cv.unmapped("privacy_purge_tombstones", identity, "unmapped_target_tombstone_blocks_cutover", redacted_row=_safe(row))


def _history_parent(table: str, row: Row, parents: dict[str, dict[str, Row]]) -> Row:
    if table in {"skill_anchors", "skill_conflicts", "playbook_versions"}:
        return parents["procedural_playbooks"].get(str(row.get("playbook_id")), {})
    if table == "fact_freshness":
        subject_type = str(row.get("subject_type") or "").lower()
        if subject_type in {"memory", "memories"}:
            return parents["memories"].get(str(row.get("subject_id")), {})
        if subject_type in {"fact", "fact_claim", "claim"}:
            return parents["fact_claims"].get(str(row.get("subject_id")), {})
    return {}


def archive_history(cv: Conversion) -> None:
    """History rows without a scope inherit their parent's; unresolved ones block."""
    parents = {
        "procedural_playbooks": _by_id(cv.rows["procedural_playbooks"], "id"),
        "memories": _by_id(cv.rows["memories"], "id"),
        "fact_claims": _by_id(cv.rows["fact_claims"], "claim_id"),
    }
    for table in sorted(_HISTORY | {"procedural_playbooks", "playbook_versions"}):
        for row in cv.rows[table]:
            identity = str(
                row.get("id") or row.get("action_id") or row.get("evidence_id")
                or row.get("playbook_id") or row.get("version") or "unknown"
            )
            scope_row = row
            if not row.get("scope_id"):
                parent = _history_parent(table, row, parents)
                if not parent.get("scope_id"):
                    cv.unmapped(table, identity, "history_scope_unresolved_blocks_cutover", redacted_row=_safe(row))
                    continue
                scope_row = _inherit(row, parent)
            _archive_record(cv, table, identity, scope_row, row, "legacy_history")


def archive_digests(cv: Conversion) -> None:
    """Digest audit rows are read-blocked archives under the manifest's retention scopes.

    A digest bridge attached to a converted memory shares that memory's source
    group, so Core's sibling closure fences it when the memory is deleted; no
    event-to-event evidence link is needed (and 'event' is not a durable object).
    event_sha256 hashes the protocol body only, so source_group_key and
    segment_index stay writable after the event is built.
    """
    memories = _by_id(cv.rows["memories"], "id")
    next_segment: dict[str, int] = {}
    for table in sorted(_DIGEST_TABLES):
        for row in cv.rows[table]:
            parent_item = None
            attached = False
            if table == "memory_digest_sources":
                mid, rid, sid = (str(row.get(key) or "") for key in ("memory_id", "run_id", "session_id"))
                identity = f"{len(mid)}:{mid}-{len(rid)}:{rid}-{len(sid)}:{sid}"
                parent = memories.get(mid, {})
                parent_item = cv.memory_items.get(mid)
                attached = parent_item is not None and bool(parent.get("scope_id"))
                if attached:
                    scope_row = {
                        **parent,
                        "session_id": sid,
                        "created_at": row.get("created_at"),
                        "run_id": rid,
                        "message_ids": row.get("message_ids"),
                        "source_hash": row.get("source_hash"),
                    }
                else:
                    scope_row = {**row, "scope_id": cv.orphan_bridge_scope}
            else:
                identity = str(row.get("id") or "unknown")
                scope_row = {**row, "scope_id": cv.digest_audit_scope}
            extra: dict[str, Any] = {
                "source_kind": "legacy_audit",
                "retention_namespace": "hermes_digest_archive",
                "composite_id": identity,
                "read_blocked": True,
                **_DIGEST_EXTRA[table],
            }
            if table == "memory_digest_sources":
                extra["attachment_resolution"] = "attached" if parent_item else "absent_memory"
                if attached:
                    extra["legacy_parent_event_id"] = parent_item["event_id"]
            scope = json.loads(parent_item["extra_json"])["scope_authorization"] if attached else None
            item = _archive_record(cv, table, identity, scope_row, row, "legacy_audit", scope, **extra)
            item["read_blocked"] = 1
            if attached:
                for key in ("source_group_key", "scope_id", "project_id", "branch_id"):
                    item[key] = parent_item[key]
                group = str(parent_item["source_group_key"])
                item["segment_index"] = next_segment.get(group, 1)
                next_segment[group] = item["segment_index"] + 1


def archive_sources(cv: Conversion) -> None:
    """Archive every convertible legacy record, in the order the report expects."""
    archive_journal(cv)
    archive_memories(cv)
    archive_fact_records(cv)
    report_target_tombstones(cv)
    archive_history(cv)
    archive_digests(cv)
    cv.episode_refs = {
        str(row.get("id")): _stable("episode", row.get("id")) for row in cv.rows["task_episodes"]
    }
