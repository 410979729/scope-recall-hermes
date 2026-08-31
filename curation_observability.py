"""Content-free ownership projection for digest and external curation lanes."""

from __future__ import annotations

from collections.abc import Mapping
import sqlite3
from typing import Any


CURATION_OBSERVABILITY_SCHEMA_VERSION = "scope-recall.curation-observability.v1"
CURATION_OWNERS = frozenset({"internal", "external", "manual"})


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def curation_owner(runtime_config: Mapping[str, Any] | None) -> str:
    root = _mapping(runtime_config)
    curation = _mapping(root.get("curation"))
    owner = str(curation.get("owner") or "internal").strip().lower()
    return owner if owner in CURATION_OWNERS else "internal"


def curation_observability_config(
    runtime_config: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return the non-secret owner and journal enablement inputs for Stats."""

    root = _mapping(runtime_config)
    journal = _mapping(root.get("journal"))
    enabled = journal.get("enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() in {"1", "true", "yes", "on"}
    return {
        "curation": {"owner": curation_owner(root)},
        "journal": {"enabled": bool(enabled)},
    }


def disabled_nightly_digest_payload(owner: str) -> dict[str, Any]:
    """Return a no-write legacy-nightly status for a non-internal owner."""

    normalized = owner if owner in CURATION_OWNERS else "internal"
    return {
        "enabled": False,
        "status": "disabled_by_owner",
        "owner": normalized,
        "latest_run": {},
        "runs": {"total": 0, "by_status": {}},
        "reason_code": "curation_owner_is_not_internal",
    }


def latest_nightly_digest_observation(
    conn: sqlite3.Connection,
) -> dict[str, Any]:
    """Read one bounded legacy-nightly observation without returning errors."""

    present = conn.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type = 'table' AND name = 'nightly_digest_runs'
        """
    ).fetchone()
    if present is None:
        return {
            "enabled": True,
            "status": "not_initialized",
            "latest_run": {},
        }
    row = conn.execute(
        """
        SELECT started_at, finished_at, status
        FROM nightly_digest_runs
        ORDER BY started_at DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return {"enabled": True, "status": "never_run", "latest_run": {}}
    status = str(row[2] or "unknown")
    return {
        "enabled": True,
        "status": "ready" if status not in {"error", "failed"} else "degraded",
        "latest_run": {
            "started_at": str(row[0] or ""),
            "finished_at": str(row[1] or ""),
            "status": status,
        },
    }


def _journal_lane(payload: Mapping[str, Any], *, owner: str) -> dict[str, Any]:
    last_run = _mapping(payload.get("last_digest_run"))
    if last_run:
        last_started = last_run.get("started_at") or ""
        last_finished = last_run.get("finished_at") or ""
        last_status = str(last_run.get("status") or payload.get("status") or "never_run")
    else:
        last_started = payload.get("last_started") or ""
        last_finished = payload.get("last_finished") or ""
        last_status = str(payload.get("last_status") or payload.get("status") or "never_run")
    digest_health = _mapping(payload.get("digest_health"))
    reasons = digest_health.get("reasons")
    reason_codes = [str(item) for item in reasons] if isinstance(reasons, list) else []
    last_error_code = reason_codes[0] if reason_codes else ""
    if not last_error_code and last_status in {"error", "failed"}:
        # Legacy journal payloads may expose only ``last_error`` and a failure
        # count.  Never project that free-form error text, but do preserve an
        # actionable, content-free degraded signal.
        last_error_code = "journal_digest_error"
    return {
        "enabled": bool(payload.get("enabled", True)),
        "owner": "scope_recall_journal_digest",
        "last_started": last_started,
        "last_finished": last_finished,
        "last_status": last_status,
        "last_error_code": last_error_code,
        "authoritative_for_curation": owner == "internal",
    }


def _nightly_lane(payload: Mapping[str, Any], *, owner: str) -> dict[str, Any]:
    if owner != "internal":
        return {
            "enabled": False,
            "owner": "scope_recall_legacy_nightly",
            "last_started": "",
            "last_finished": "",
            "last_status": "disabled_by_owner",
            "last_error_code": "",
            "authoritative_for_curation": False,
        }
    latest = _mapping(payload.get("latest_run"))
    last_status = str(latest.get("status") or payload.get("status") or "never_run")
    return {
        "enabled": bool(payload.get("enabled", True)),
        "owner": "scope_recall_legacy_nightly",
        "last_started": latest.get("started_at") or "",
        "last_finished": latest.get("finished_at") or "",
        "last_status": last_status,
        "last_error_code": (
            "nightly_digest_error" if last_status in {"error", "failed"} else ""
        ),
        "authoritative_for_curation": True,
    }


def _external_lane(*, owner: str) -> dict[str, Any]:
    enabled = owner == "external"
    return {
        "enabled": enabled,
        "owner": "hermes_nightly_memory_curation",
        "last_started": None,
        "last_finished": None,
        "last_status": "unobserved" if enabled else "disabled_by_owner",
        "last_error_code": "",
        "authoritative_for_curation": enabled,
        "status_observed": False,
    }


def curation_status_projection(
    runtime_config: Mapping[str, Any] | None,
    *,
    journal_digest: Mapping[str, Any] | None,
    nightly_digest: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Project three distinct lanes without claiming external Cron health."""

    owner = curation_owner(runtime_config)
    config = curation_observability_config(runtime_config)
    journal_config = _mapping(config.get("journal"))
    journal_payload = dict(_mapping(journal_digest))
    journal_payload.setdefault("enabled", bool(journal_config.get("enabled", True)))
    return {
        "schema_version": CURATION_OBSERVABILITY_SCHEMA_VERSION,
        "authoritative_owner": owner,
        "journal_digest": _journal_lane(journal_payload, owner=owner),
        "nightly_digest_legacy": _nightly_lane(
            _mapping(nightly_digest), owner=owner
        ),
        "external_curation": _external_lane(owner=owner),
    }


__all__ = [
    "CURATION_OBSERVABILITY_SCHEMA_VERSION",
    "CURATION_OWNERS",
    "curation_observability_config",
    "curation_owner",
    "curation_status_projection",
    "disabled_nightly_digest_payload",
    "latest_nightly_digest_observation",
]
