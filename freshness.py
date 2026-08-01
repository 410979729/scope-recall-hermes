"""Freshness metadata helpers for durable factual memories.

Freshness is advisory evidence for ranking and dashboards; it should not overwrite the original memory truth."""

from __future__ import annotations

import ipaddress
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Sequence
from urllib.parse import urlsplit

from .capture_filters import sanitize_report_text, sanitize_structured_value
from .lifecycle_policy import (
    durable_lifecycle_visible_sql,
    ordinary_recall_lifecycle_visible_sql,
)

CURRENT_STATUSES = {"current", "fresh", "valid", "verified", "ok"}
NEEDS_CHECK_STATUSES = {
    "needs_live_check",
    "needs-live-check",
    "unknown",
    "unchecked",
    "expired",
}
STALE_STATUSES = {"stale", "invalid", "superseded", "outdated"}
VALIDATOR_KINDS = {"file_exists", "command", "http", "manual", "none"}
FACTUAL_MEMORY_TYPES = {"environment_fact", "fact", "factual", "project_fact"}
_MEMORY_TYPE_ALIASES = {
    "environment_fact": "factual",
    "fact": "factual",
    "project_fact": "factual",
}
_DEFAULT_FRESHNESS_POLICY = {
    "status": "needs_live_check",
    "ttl_days": 30,
    "validator_kind": "manual",
}
MEMORY_TYPE_FRESHNESS_POLICIES: dict[str, dict[str, Any]] = {
    "factual": dict(_DEFAULT_FRESHNESS_POLICY),
    "preference": {"status": "current", "ttl_days": 365, "validator_kind": "none"},
    "procedure": {"status": "current", "ttl_days": 180, "validator_kind": "none"},
    "workflow": {"status": "current", "ttl_days": 180, "validator_kind": "none"},
    "tool_trace": {"status": "current", "ttl_days": 30, "validator_kind": "none"},
    "project": dict(_DEFAULT_FRESHNESS_POLICY),
    "summary": dict(_DEFAULT_FRESHNESS_POLICY),
    "pitfall": {"status": "current", "ttl_days": 180, "validator_kind": "none"},
    "decision": {"status": "current", "ttl_days": 0, "validator_kind": "none"},
    "episodic": {"status": "current", "ttl_days": 0, "validator_kind": "none"},
    "resource": {"status": "current", "ttl_days": 365, "validator_kind": "none"},
    "constraint": {"status": "current", "ttl_days": 365, "validator_kind": "none"},
}
_OPERATIONAL_STATUS_MEMORY_TYPES = {"decision", "episodic", "project", "summary"}
_CURRENT_STATE_MARKER_RE = re.compile(
    r"(?:当前|目前|现在|现状|截至|状态快照|current(?:ly)?|as\s+of|live[-\s]?check)",
    re.IGNORECASE,
)
_OPERATIONAL_STATUS_SUBJECT_RE = re.compile(
    r"(?:版本|version|journal\s+backlog|backlog|积压|vector(?:\s+status)?|向量(?:状态)?|运行状态|runtime\s+status|service\s+status|状态)",
    re.IGNORECASE,
)
_OPERATIONAL_STATUS_VALUE_RE = re.compile(
    r"(?:"
    r"(?:版本|version)[^。；;\n]{0,24}?v?\d+\.\d+(?:\.\d+)?"
    r"|(?:journal\s+backlog|backlog|积压)[^。；;\n]{0,24}?\d+"
    r"|(?:vector(?:\s+status)?|向量(?:状态)?|运行状态|runtime\s+status|service\s+status|状态)"
    r"[^。；;\n]{0,32}?(?:ready|needs_repair|degraded|running|active|inactive|failed|error|ok|正常|异常|故障)"
    r")",
    re.IGNORECASE,
)
_OPERATIONAL_STATUS_MIN_PENALTY = 0.55
_VALIDATOR_KIND_ALIASES = {
    "": "none",
    "none": "none",
    "no_validation": "none",
    "static": "none",
    "permanent": "none",
    "file": "file_exists",
    "path": "file_exists",
    "exists": "file_exists",
    "file_exists": "file_exists",
    "shell": "command",
    "cmd": "command",
    "command": "command",
    "http": "http",
    "https": "http",
    "url": "http",
    "http_get": "http",
    "http_head": "http",
    "manual": "manual",
    "manual_live_check": "manual",
    "human": "manual",
}

_SEVERITY = {
    "current": 0,
    "fresh": 0,
    "valid": 0,
    "verified": 0,
    "ok": 0,
    "unknown": 1,
    "unchecked": 1,
    "needs_live_check": 2,
    "needs-live-check": 2,
    "expired": 2,
    "stale": 3,
    "invalid": 3,
    "superseded": 3,
    "outdated": 3,
}


def normalize_validator_kind(kind: Any) -> str:
    """Normalize freshness validator kinds to the public contract.

    Unknown non-empty validators fail closed to `manual` so stale operational
    facts are not accidentally treated as timeless preferences.
    """
    normalized = str(kind or "").strip().lower().replace(" ", "_").replace("-", "_")
    if normalized in VALIDATOR_KINDS:
        return normalized
    return _VALIDATOR_KIND_ALIASES.get(normalized, "manual" if normalized else "none")


def _parse_iso(raw: Any) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_freshness_status(
    status: Any, *, valid_until: Any = None, now: datetime | None = None
) -> str:
    normalized = str(status or "unknown").strip().lower().replace(" ", "_")
    if normalized == "needs-live-check":
        normalized = "needs_live_check"
    if normalized not in CURRENT_STATUSES | NEEDS_CHECK_STATUSES | STALE_STATUSES:
        normalized = "unknown"
    deadline = _parse_iso(valid_until)
    now_dt = now or datetime.now(timezone.utc)
    if deadline is not None and deadline < now_dt and normalized in CURRENT_STATUSES:
        return "expired"
    return normalized


def _metadata_mapping(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _normalized_fact_key(value: Any) -> str:
    text = str(value or "memory_fact").strip().lower().replace(" ", "_")
    cleaned = re.sub(r"[^a-z0-9_.:-]+", "_", text).strip("_.:-")
    return (cleaned or "memory_fact")[:120]


def _safe_validator_spec(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}

    sanitized, _ = sanitize_structured_value(value)
    if not isinstance(sanitized, dict):
        return {}

    def clean(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                str(key)[:80]: clean(child) for key, child in list(item.items())[:50]
            }
        if isinstance(item, list):
            return [clean(child) for child in item[:50]]
        if isinstance(item, (bool, int, float)) or item is None:
            return item
        return sanitize_report_text(str(item))[:1000]

    return clean(sanitized)


_COMMAND_VALIDATOR_ID_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,79}")
REGISTERED_COMMAND_VALIDATOR_IDS = frozenset(
    {"git-head", "git-status", "hermes-doctor"}
)


def _validated_validator_spec(kind: str, value: Any) -> dict[str, Any]:
    """Return one bounded declarative validator target or fail closed.

    This module does not execute validators. These restrictions ensure stored
    metadata cannot silently become arbitrary shell, filesystem, or SSRF input
    when an executor is added. Executors must still re-check resolved paths and
    DNS answers at use time.
    """

    spec = _safe_validator_spec(value)
    if kind in {"manual", "none"}:
        return spec
    if kind == "command":
        command_id = str(spec.get("command_id") or "").strip().lower()
        if (
            set(spec) != {"command_id"}
            or not _COMMAND_VALIDATOR_ID_RE.fullmatch(command_id)
            or command_id not in REGISTERED_COMMAND_VALIDATOR_IDS
        ):
            raise ValueError(
                "validator_spec for command must contain one registered command_id"
            )
        return {"command_id": command_id}
    if kind == "file_exists":
        raw_path = str(spec.get("path") or "").strip()
        normalized_path = raw_path.replace("\\", "/")
        posix_path = PurePosixPath(normalized_path)
        windows_path = PureWindowsPath(raw_path)
        if (
            set(spec) != {"path"}
            or not normalized_path
            or len(normalized_path) > 512
            or "\x00" in normalized_path
            or ":" in normalized_path
            or any(character in normalized_path for character in "*?")
            or posix_path.is_absolute()
            or windows_path.is_absolute()
            or bool(windows_path.drive)
            or ".." in posix_path.parts
            or ".." in windows_path.parts
        ):
            raise ValueError(
                "validator_spec for file_exists must contain one safe relative path"
            )
        return {"path": posix_path.as_posix()}
    if kind == "http":
        raw_url = str(spec.get("url") or "").strip()
        try:
            parsed = urlsplit(raw_url)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("validator_spec contains an invalid HTTP URL") from exc
        hostname = str(parsed.hostname or "").strip().lower()
        if (
            set(spec) != {"url"}
            or len(raw_url) > 2048
            or parsed.scheme.lower() != "https"
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or bool(parsed.query)
            or bool(parsed.fragment)
            or (port is not None and not 1 <= port <= 65535)
            or hostname == "localhost"
            or hostname.endswith((".localhost", ".local", ".internal"))
        ):
            raise ValueError(
                "validator_spec for http must contain one public credential-free HTTPS URL"
            )
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            if not address.is_global:
                raise ValueError(
                    "validator_spec for http cannot target a non-public IP address"
                )
        return {"url": raw_url}
    raise ValueError(f"unsupported validator_spec kind: {kind}")


def _looks_like_operational_status_snapshot(content: str, memory_type: str) -> bool:
    """Conservatively detect volatile status assertions outside factual rows."""

    if memory_type not in _OPERATIONAL_STATUS_MEMORY_TYPES:
        return False
    for sentence in re.split(r"[。；;\n]+", str(content or "")):
        if not _CURRENT_STATE_MARKER_RE.search(sentence):
            continue
        if not _OPERATIONAL_STATUS_SUBJECT_RE.search(sentence):
            continue
        if _OPERATIONAL_STATUS_VALUE_RE.search(sentence):
            return True
    return False


def _freshness_spec_from_metadata(
    metadata: dict[str, Any] | None,
    *,
    content: str = "",
    now: datetime | None = None,
) -> dict[str, Any] | None:
    payload = dict(metadata or {})
    raw_memory_type = str(
        payload.get("memory_type") or payload.get("category") or ""
    ).strip().lower()
    memory_type = _MEMORY_TYPE_ALIASES.get(raw_memory_type, raw_memory_type)
    raw_freshness = payload.get("freshness")
    explicit = dict(raw_freshness) if isinstance(raw_freshness, dict) else {}
    operational_status = not explicit and _looks_like_operational_status_snapshot(
        content, memory_type
    )
    if not memory_type and not explicit:
        return None

    policy = dict(
        MEMORY_TYPE_FRESHNESS_POLICIES.get(
            memory_type,
            _DEFAULT_FRESHNESS_POLICY,
        )
    )
    if operational_status:
        policy.update(
            status="needs_live_check",
            ttl_days=1,
            validator_kind="manual",
        )

    now_dt = now or datetime.now(timezone.utc)
    valid_until_dt = _parse_iso(
        explicit.get("valid_until") or payload.get("valid_until")
    )
    requested_status = (
        explicit.get("status")
        or payload.get("freshness_status")
        or payload.get("fact_freshness_status")
        or policy["status"]
    )
    status = normalize_freshness_status(
        requested_status, valid_until=valid_until_dt, now=now_dt
    )
    validator_kind = normalize_validator_kind(
        explicit.get("validator_kind")
        or payload.get("validator_kind")
        or policy["validator_kind"]
    )
    raw_ttl_days: Any = policy["ttl_days"]
    if "ttl_days" in explicit:
        raw_ttl_days = explicit.get("ttl_days")
    elif payload.get("ttl_days") is not None:
        raw_ttl_days = payload.get("ttl_days")
    try:
        ttl_days = max(0, int(raw_ttl_days))
    except (TypeError, ValueError):
        ttl_days = int(policy["ttl_days"])
    checked_dt = _parse_iso(
        explicit.get("last_checked_at") or payload.get("last_checked_at")
    )
    if status in CURRENT_STATUSES and checked_dt is None:
        checked_dt = now_dt
    if status in CURRENT_STATUSES and ttl_days > 0 and valid_until_dt is None:
        valid_until_dt = (checked_dt or now_dt) + timedelta(days=ttl_days)

    default_fact_key = (
        "operational_status_snapshot" if operational_status else "memory_fact"
    )
    default_truth_type = (
        "operational_status" if operational_status else memory_type or "factual"
    )
    return {
        "fact_key": _normalized_fact_key(
            explicit.get("fact_key") or payload.get("fact_key") or default_fact_key
        ),
        "truth_type": str(
            explicit.get("truth_type")
            or payload.get("truth_type")
            or default_truth_type
        )[:80],
        "validator_kind": validator_kind,
        "validator_spec": _validated_validator_spec(
            validator_kind,
            explicit.get("validator_spec") or payload.get("validator_spec"),
        ),
        "ttl_days": ttl_days,
        "last_checked_at": checked_dt.isoformat() if checked_dt is not None else "",
        "valid_until": valid_until_dt.isoformat() if valid_until_dt is not None else "",
        "status": status,
        "stale_reason": sanitize_report_text(
            explicit.get("stale_reason") or payload.get("stale_reason") or ""
        )[:500],
        "superseded_by": str(
            explicit.get("superseded_by") or payload.get("superseded_by") or ""
        )[:120],
    }


def _legacy_backfill_plan(
    metadata: dict[str, Any],
    *,
    content: str,
) -> tuple[dict[str, Any], dict[str, Any], bool] | None:
    """Plan one legacy row without trusting obsolete validator metadata.

    Current writes must reject unsafe validator targets atomically. Historical
    rows predate that invariant, so migration isolates an invalid target as a
    manual live check instead of executing, preserving, or echoing it. The
    returned metadata is an in-memory backfill view; the authoritative memory
    metadata is never rewritten by this helper.
    """

    try:
        spec = _freshness_spec_from_metadata(metadata, content=content)
    except ValueError:
        planned_metadata = dict(metadata)
        # Empty nested values would otherwise fall through to these legacy
        # top-level aliases via ``or`` in _freshness_spec_from_metadata().
        # Remove them only from the in-memory migration view so quarantined
        # rows cannot persist an obsolete validator target under manual mode.
        planned_metadata.pop("validator_kind", None)
        planned_metadata.pop("validator_spec", None)
        raw_freshness = planned_metadata.get("freshness")
        planned_freshness = (
            dict(raw_freshness) if isinstance(raw_freshness, dict) else {}
        )
        planned_freshness.update(
            validator_kind="manual",
            validator_spec={},
            status="needs_live_check",
            last_checked_at="",
            valid_until="",
            stale_reason="legacy_invalid_validator_spec",
        )
        planned_metadata["freshness"] = planned_freshness
        spec = _freshness_spec_from_metadata(planned_metadata, content=content)
        if spec is None:  # pragma: no cover - explicit freshness is always eligible
            return None
        return spec, planned_metadata, True
    if spec is None:
        return None
    return spec, metadata, False


def upsert_memory_freshness(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    metadata: dict[str, Any] | None,
    content: str = "",
    now: datetime | None = None,
    commit: bool = True,
) -> str:
    """Idempotently write advisory freshness for one stored memory.

    Unverified factual writes fail closed to ``needs_live_check``. The helper
    never invents a current status.
    """

    spec = _freshness_spec_from_metadata(metadata, content=content, now=now)
    if spec is None or not memory_id or not _table_exists(conn, "fact_freshness"):
        return ""
    now_iso = (now or datetime.now(timezone.utc)).isoformat()
    existing = conn.execute(
        """
        SELECT id, created_at
        FROM fact_freshness
        WHERE subject_type = 'memory' AND subject_id = ? AND fact_key = ?
        ORDER BY updated_at DESC, id ASC
        LIMIT 1
        """,
        (str(memory_id), spec["fact_key"]),
    ).fetchone()
    freshness_id = (
        str(existing["id"])
        if existing is not None
        else uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"scope-recall:memory:{memory_id}:{spec['fact_key']}",
        ).hex
    )
    created_at = (
        str(existing["created_at"] or now_iso) if existing is not None else now_iso
    )
    conn.execute(
        """
        INSERT INTO fact_freshness(
            id, subject_type, subject_id, fact_key, truth_type, validator_kind,
            validator_spec, ttl_days, last_checked_at, valid_until, status,
            stale_reason, superseded_by, created_at, updated_at
        ) VALUES (?, 'memory', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            truth_type = excluded.truth_type,
            validator_kind = excluded.validator_kind,
            validator_spec = excluded.validator_spec,
            ttl_days = excluded.ttl_days,
            last_checked_at = excluded.last_checked_at,
            valid_until = excluded.valid_until,
            status = excluded.status,
            stale_reason = excluded.stale_reason,
            superseded_by = excluded.superseded_by,
            updated_at = excluded.updated_at
        """,
        (
            freshness_id,
            str(memory_id),
            spec["fact_key"],
            spec["truth_type"],
            spec["validator_kind"],
            json.dumps(spec["validator_spec"], ensure_ascii=False, sort_keys=True),
            spec["ttl_days"],
            spec["last_checked_at"] or None,
            spec["valid_until"] or None,
            spec["status"],
            spec["stale_reason"],
            spec["superseded_by"],
            created_at,
            now_iso,
        ),
    )
    if commit:
        conn.commit()
    return freshness_id


def _row_payload(row: sqlite3.Row, *, now: datetime | None = None) -> dict[str, Any]:
    status = normalize_freshness_status(
        row["status"], valid_until=row["valid_until"], now=now
    )
    needs_live_check = status in NEEDS_CHECK_STATUSES or status in STALE_STATUSES
    return {
        "id": str(row["id"]),
        "subject_type": str(row["subject_type"]),
        "subject_id": str(row["subject_id"]),
        "fact_key": str(row["fact_key"]),
        "truth_type": str(row["truth_type"]),
        "validator_kind": normalize_validator_kind(row["validator_kind"]),
        "last_checked_at": str(row["last_checked_at"] or ""),
        "valid_until": str(row["valid_until"] or ""),
        "status": status,
        "stale_reason": str(row["stale_reason"] or ""),
        "superseded_by": str(row["superseded_by"] or ""),
        "needs_live_check": needs_live_check,
        "severity": _SEVERITY.get(status, 1),
    }


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def memory_freshness_map(
    conn: sqlite3.Connection, memory_ids: Iterable[str], *, now: datetime | None = None
) -> dict[str, dict[str, Any]]:
    ids = sorted({str(memory_id) for memory_id in memory_ids if str(memory_id)})
    if not ids or not _table_exists(conn, "fact_freshness"):
        return {}
    placeholders = ",".join("?" for _ in ids)
    try:
        rows = conn.execute(
            f"""
            SELECT id, subject_type, subject_id, fact_key, truth_type, validator_kind,
                   last_checked_at, valid_until, status, stale_reason, superseded_by
            FROM fact_freshness
            WHERE subject_type = 'memory'
              AND subject_id IN ({placeholders})
            ORDER BY updated_at DESC, id ASC
            """,
            ids,
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        payload = _row_payload(row, now=now)
        subject_id = str(payload["subject_id"])
        existing = result.get(subject_id)
        if existing is None or int(payload.get("severity") or 0) > int(
            existing.get("severity") or 0
        ):
            result[subject_id] = payload
    return result


def freshness_penalty(
    freshness: dict[str, Any] | None, config: dict[str, Any] | None = None
) -> float:
    cfg = config or {}

    def configured_penalty(key: str, default: float) -> float:
        raw_value = cfg[key] if key in cfg else default
        if raw_value is None or raw_value == "":
            raw_value = default
        return float(raw_value)

    try:
        needs_live_check_penalty = configured_penalty(
            "fact_freshness_needs_live_check_penalty", 0.18
        )
        stale_penalty = configured_penalty(
            "fact_freshness_stale_penalty", 0.35
        )
    except (TypeError, ValueError):
        needs_live_check_penalty = 0.18
        stale_penalty = 0.35
    if not freshness:
        try:
            configured_untracked = configured_penalty(
                "fact_freshness_untracked_penalty", 0.10
            )
        except (TypeError, ValueError):
            configured_untracked = 0.10
        penalty = min(
            configured_untracked,
            max(0.0, needs_live_check_penalty - 0.01),
        )
        return max(0.0, min(0.9, penalty))
    status = str(freshness.get("status") or "").strip().lower()
    truth_type = str(freshness.get("truth_type") or "").strip().lower()
    try:
        if status in STALE_STATUSES:
            penalty = stale_penalty
        elif status == "expired":
            configured_expired = configured_penalty(
                "fact_freshness_expired_penalty", 0.45
            )
            penalty = max(stale_penalty, configured_expired)
        elif status in NEEDS_CHECK_STATUSES:
            penalty = needs_live_check_penalty
        else:
            return 0.0
    except (TypeError, ValueError):
        return 0.0
    if truth_type == "operational_status":
        # Unverified snapshots must not outrank stable policy merely because an
        # old version number or status token is a strong lexical match.
        penalty = max(penalty, _OPERATIONAL_STATUS_MIN_PENALTY)
    return max(0.0, min(0.9, penalty))


def attach_freshness_metadata(
    metadata: dict[str, Any],
    freshness: dict[str, Any] | None,
    *,
    config: dict[str, Any] | None = None,
) -> float:
    penalty = freshness_penalty(freshness, config)
    if not freshness:
        metadata.setdefault("fact_freshness_status", "untracked")
        metadata.setdefault("needs_live_check", True)
        metadata.setdefault("fact_freshness_penalty", penalty)
        metadata.setdefault(
            "freshness_warning",
            "⚠ UNTRACKED — freshness unknown; verify before use",
        )
        return penalty
    status = str(freshness.get("status") or "unknown")
    metadata["fact_freshness_status"] = status
    metadata["fact_key"] = str(freshness.get("fact_key") or "")
    metadata["truth_type"] = str(freshness.get("truth_type") or "")
    metadata["validator_kind"] = str(freshness.get("validator_kind") or "")
    metadata["last_checked_at"] = str(freshness.get("last_checked_at") or "")
    metadata["valid_until"] = str(freshness.get("valid_until") or "")
    metadata["stale_reason"] = str(freshness.get("stale_reason") or "")
    metadata["needs_live_check"] = bool(freshness.get("needs_live_check"))
    metadata["fact_freshness_penalty"] = penalty
    last_checked_at = str(freshness.get("last_checked_at") or "unknown")
    if status in STALE_STATUSES:
        metadata["freshness_warning"] = (
            f"⚠ STALE — last checked {last_checked_at}; verify before use"
        )
    elif status == "expired":
        metadata["freshness_warning"] = (
            f"⚠ EXPIRED — last checked {last_checked_at}; verify before use"
        )
    elif status in NEEDS_CHECK_STATUSES:
        metadata["freshness_warning"] = "⚠ NEEDS LIVE CHECK — verify before use"
    else:
        metadata.pop("freshness_warning", None)
    return penalty


def _metadata_memory_type_sql(alias: str) -> str:
    """Return normalized memory type with blank values falling back to category."""

    return f"""
        LOWER(COALESCE(
            NULLIF(TRIM(CASE WHEN json_valid({alias}.metadata) THEN json_extract({alias}.metadata, '$.memory_type') ELSE '' END), ''),
            NULLIF(TRIM(CASE WHEN json_valid({alias}.metadata) THEN json_extract({alias}.metadata, '$.category') ELSE '' END), ''),
            ''
        ))
    """.strip()


def freshness_backfill_inventory(
    conn: sqlite3.Connection,
    *,
    page_size: int = 1000,
    max_rows: int = 1_000_000,
) -> dict[str, Any]:
    """Inventory all active rows that still lack freshness metadata.

    The scan is read-only and keyset paginated so large legacy stores do not
    materialize the full corpus at once. Content is inspected only to select a
    deterministic policy; report samples contain ids, never memory text.
    """

    bounded_page = max(1, min(int(page_size), 5000))
    bounded_max = max(bounded_page, min(int(max_rows), 5_000_000))
    scanned = 0
    eligible = 0
    ineligible = 0
    quarantined = 0
    last_id = ""
    memory_types: dict[str, int] = {}
    statuses: dict[str, int] = {}
    sample_ids: list[str] = []
    quarantined_sample_ids: list[str] = []
    truncated = False
    while scanned < bounded_max:
        rows = conn.execute(
            """
            SELECT m.id, m.content, m.metadata
            FROM memories AS m
            LEFT JOIN fact_freshness AS f
              ON f.subject_type = 'memory' AND f.subject_id = m.id
            WHERE f.id IS NULL
              AND m.id > ?
              AND COALESCE(LOWER(json_extract(m.metadata, '$.lifecycle')), '')
                  NOT IN ('archived', 'deleted', 'expired', 'forgotten', 'invalidated', 'merged', 'superseded')
            ORDER BY m.id ASC
            LIMIT ?
            """,
            (last_id, min(bounded_page, bounded_max - scanned)),
        ).fetchall()
        if not rows:
            break
        for row in rows:
            memory_id = str(row["id"])
            last_id = memory_id
            scanned += 1
            metadata = _metadata_mapping(row["metadata"])
            plan = _legacy_backfill_plan(
                metadata,
                content=str(row["content"] or ""),
            )
            if plan is None:
                ineligible += 1
                continue
            spec, _planned_metadata, is_quarantined = plan
            eligible += 1
            if is_quarantined:
                quarantined += 1
                if len(quarantined_sample_ids) < 50:
                    quarantined_sample_ids.append(memory_id)
            memory_type = str(
                metadata.get("memory_type") or metadata.get("category") or "unknown"
            ).strip().lower()
            memory_types[memory_type] = memory_types.get(memory_type, 0) + 1
            status = str(spec.get("status") or "needs_live_check")
            statuses[status] = statuses.get(status, 0) + 1
            if len(sample_ids) < 50:
                sample_ids.append(memory_id)
        if len(rows) < bounded_page:
            break
    else:
        truncated = True
    if scanned >= bounded_max:
        more = conn.execute(
            """
            SELECT 1
            FROM memories AS m
            LEFT JOIN fact_freshness AS f
              ON f.subject_type = 'memory' AND f.subject_id = m.id
            WHERE f.id IS NULL AND m.id > ?
            LIMIT 1
            """,
            (last_id,),
        ).fetchone()
        truncated = more is not None
    return {
        "status": (
            "scan_truncated"
            if truncated
            else ("needs_backfill" if eligible else "ready")
        ),
        "scanned": scanned,
        "eligible": eligible,
        "ineligible": ineligible,
        "quarantined": quarantined,
        "truncated": truncated,
        "max_rows": bounded_max,
        "memory_types": dict(sorted(memory_types.items())),
        "statuses": dict(sorted(statuses.items())),
        "sample_ids": sample_ids,
        "quarantined_sample_ids": quarantined_sample_ids,
    }


def _scan_untracked_memory_freshness_candidates(
    conn: sqlite3.Connection,
    *,
    clauses: Sequence[str],
    params: Sequence[Any],
    limit: int,
) -> tuple[list[tuple[sqlite3.Row, dict[str, Any], bool]], bool]:
    """Return a bounded, validated candidate set without mutating truth state."""

    rows: list[tuple[sqlite3.Row, dict[str, Any], bool]] = []
    truncated = False
    offset = 0
    scan_page = min(1000, max(100, limit))
    while not truncated and offset < 1_000_000:
        candidate_rows = conn.execute(
            f"SELECT m.id, m.content, m.metadata FROM memories m WHERE {' AND '.join(clauses)} "
            "ORDER BY m.updated_at DESC, m.id ASC LIMIT ? OFFSET ?",
            [*params, scan_page, offset],
        ).fetchall()
        if not candidate_rows:
            break
        offset += len(candidate_rows)
        for row in candidate_rows:
            metadata = _metadata_mapping(row["metadata"])
            plan = _legacy_backfill_plan(
                metadata,
                content=str(row["content"] or ""),
            )
            if plan is None:
                continue
            _spec, planned_metadata, is_quarantined = plan
            if len(rows) >= limit:
                truncated = True
                break
            rows.append((row, planned_metadata, is_quarantined))
        if len(candidate_rows) < scan_page:
            break
    return rows, truncated


def _freshness_backfill_result(
    *,
    apply: bool,
    rows: Sequence[tuple[sqlite3.Row, dict[str, Any], bool]],
    inserted: int,
    truncated: bool,
) -> dict[str, Any]:
    """Build the stable, content-free operator/startup result."""

    return {
        "apply": bool(apply),
        "eligible": len(rows),
        "inserted": inserted,
        "quarantined": sum(1 for _row, _metadata, flag in rows if flag),
        "truncated": truncated,
        "ids": [str(row["id"]) for row, _metadata, _flag in rows],
        "quarantined_ids": [
            str(row["id"]) for row, _metadata, flag in rows if flag
        ],
    }


def backfill_untracked_memory_freshness(
    conn: sqlite3.Connection,
    *,
    scope_ids: Sequence[str] | None = None,
    apply: bool = False,
    limit: int = 500,
) -> dict[str, Any]:
    """Plan or atomically apply a bounded freshness backfill.

    Apply owns its transaction. It first performs a read-only eligibility probe
    to avoid taking a write reservation on already-current databases, then
    re-scans authoritative rows under ``BEGIN IMMEDIATE`` before writing. This
    prevents a WAL read snapshot from being invalidated by a competing writer.
    """

    empty_rows: list[tuple[sqlite3.Row, dict[str, Any], bool]] = []
    if not _table_exists(conn, "fact_freshness") or not _table_exists(conn, "memories"):
        return _freshness_backfill_result(
            apply=apply,
            rows=empty_rows,
            inserted=0,
            truncated=False,
        )
    clauses = [
        ordinary_recall_lifecycle_visible_sql("m"),
        "NOT EXISTS (SELECT 1 FROM fact_freshness f WHERE f.subject_type = 'memory' AND f.subject_id = m.id)",
    ]
    params: list[Any] = []
    scopes = [str(scope_id) for scope_id in (scope_ids or []) if str(scope_id)]
    if scope_ids is not None:
        if not scopes:
            return _freshness_backfill_result(
                apply=apply,
                rows=empty_rows,
                inserted=0,
                truncated=False,
            )
        clauses.append(f"m.scope_id IN ({','.join('?' for _ in scopes)})")
        params.extend(scopes)
    bounded_limit = max(1, min(int(limit or 500), 5000))
    rows, truncated = _scan_untracked_memory_freshness_candidates(
        conn,
        clauses=clauses,
        params=params,
        limit=bounded_limit,
    )
    if not apply or not rows:
        return _freshness_backfill_result(
            apply=apply,
            rows=rows,
            inserted=0,
            truncated=truncated,
        )
    if conn.in_transaction:
        raise RuntimeError("freshness backfill apply requires transaction ownership")

    inserted = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows, truncated = _scan_untracked_memory_freshness_candidates(
            conn,
            clauses=clauses,
            params=params,
            limit=bounded_limit,
        )
        for row, metadata, _is_quarantined in rows:
            if upsert_memory_freshness(
                conn,
                memory_id=str(row["id"]),
                metadata=metadata,
                content=str(row["content"] or ""),
                commit=False,
            ):
                inserted += 1
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    return _freshness_backfill_result(
        apply=apply,
        rows=rows,
        inserted=inserted,
        truncated=truncated,
    )


def _scope_filter_sql(scope_ids: Sequence[str] | None) -> tuple[str, list[str]] | None:
    if scope_ids is None:
        return "", []
    scopes = [str(scope_id) for scope_id in scope_ids if str(scope_id)]
    if not scopes:
        return None
    placeholders = ",".join("?" for _ in scopes)
    return f" AND m.scope_id IN ({placeholders})", scopes


def fact_freshness_report(
    conn: sqlite3.Connection, *, scope_ids: Sequence[str] | None = None
) -> dict[str, Any]:
    """Summarize freshness coverage and stale durable facts.

    Freshness reports guide review and ranking policy without rewriting the underlying factual memory. When a scope allowlist is provided, only memory-scoped freshness rows for those scopes are counted.
    """
    if not _table_exists(conn, "fact_freshness"):
        return {
            "status": "schema_missing",
            "tracked_facts": 0,
            "by_status": {},
            "needs_live_check": 0,
            "stale_facts": 0,
            "by_validator_kind": {},
            "coverage": {
                "factual_memories": 0,
                "tracked_memory_facts": 0,
                "coverage_percent": 0.0,
            },
        }
    scope_filter = _scope_filter_sql(scope_ids)
    lifecycle_sql = ordinary_recall_lifecycle_visible_sql("m")
    advisory_lifecycle_sql = durable_lifecycle_visible_sql("m")
    factual_type_sql = f"{_metadata_memory_type_sql('m')} IN ('factual', 'fact', 'project_fact', 'environment_fact')"
    if scope_filter is None:
        rows = []
    else:
        scope_sql, scope_params = scope_filter
        rows = conn.execute(
            f"""
            SELECT f.id, f.subject_type, f.subject_id, f.fact_key, f.truth_type, f.validator_kind,
                   f.last_checked_at, f.valid_until, f.status, f.stale_reason, f.superseded_by,
                   CASE WHEN ({lifecycle_sql}) AND ({factual_type_sql}) THEN 1 ELSE 0 END AS cohort_factual
            FROM fact_freshness AS f
            JOIN memories AS m ON m.id = f.subject_id
            WHERE f.subject_type = 'memory'
              AND {advisory_lifecycle_sql}
              {scope_sql}
            """,
            scope_params,
        ).fetchall()
    by_status: dict[str, int] = {}
    by_validator_kind: dict[str, int] = {}
    needs_live_check = 0
    stale_facts = 0
    for row in rows:
        payload = _row_payload(row)
        status = str(payload["status"])
        validator_kind = str(payload["validator_kind"] or "none")
        by_status[status] = by_status.get(status, 0) + 1
        by_validator_kind[validator_kind] = by_validator_kind.get(validator_kind, 0) + 1
        if bool(payload.get("needs_live_check")):
            needs_live_check += 1
        if status in STALE_STATUSES:
            stale_facts += 1
    factual_memories = 0
    tracked_memory_facts = len(
        {
            str(row["subject_id"])
            for row in rows
            if str(row["subject_type"] or "") == "memory"
            and bool(row["cohort_factual"])
        }
    )
    if _table_exists(conn, "memories"):
        if scope_filter is None:
            factual_memories = 0
        else:
            scope_sql, scope_params = scope_filter
            factual_memories = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM memories AS m
                    WHERE {lifecycle_sql}
                      AND {factual_type_sql}
                      {scope_sql}
                    """,
                    scope_params,
                ).fetchone()[0]
            )
    coverage_percent = (
        round((tracked_memory_facts / factual_memories) * 100.0, 3)
        if factual_memories
        else 0.0
    )
    coverage_incomplete = tracked_memory_facts < factual_memories
    status = "needs_review" if needs_live_check or coverage_incomplete else "ready"
    return {
        "status": status,
        "tracked_facts": len(rows),
        "by_status": dict(sorted(by_status.items())),
        "by_validator_kind": dict(sorted(by_validator_kind.items())),
        "needs_live_check": needs_live_check,
        "stale_facts": stale_facts,
        "coverage": {
            "factual_memories": factual_memories,
            "tracked_memory_facts": tracked_memory_facts,
            "coverage_percent": coverage_percent,
        },
    }
