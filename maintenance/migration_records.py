"""Shared offline migration values, safe reports and input primitives.

No host activation, target writes or background work.
"""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable
from .backup import _safe_path
from scope_recall.core.capture_filters import sanitize_report_text, sanitize_structured_value
from scope_recall.core.schema import SCHEMA_VERSION

LEGACY_BASELINE = "578b955802df753f2e2208e26eab6f71971285a0"
REPORT_FORMAT = "scope-recall-p15-migration-report/3"
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$")


class MigrationError(RuntimeError):
    pass


def _write_report(report: dict[str, Any], report_path: str | Path | None) -> None:
    if report_path is None:
        return
    report_file = _safe_path(report_path, error_type=MigrationError)
    if report_file.exists() or report_file.is_symlink():
        raise MigrationError("refusing to overwrite migration report")
    report_file.parent.mkdir(parents=True, exist_ok=True)
    with report_file.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def _materialize_explicit_scope_selection(
    scope_ids: Iterable[str] | None,
) -> list[str] | None:
    """Keep omitted selection distinct from an explicit empty or subset list."""
    if scope_ids is None:
        return None
    seen: list[str] = []
    for item in scope_ids:
        if type(item) is not str:
            raise MigrationError("explicit scope selection items must be strings")
        if item in seen:
            raise MigrationError("explicit scope selection contains duplicates")
        seen.append(item)
    return seen


def _blocked_report(
    *,
    batch_key: str,
    unmapped: list[dict[str, Any]],
    reasons: list[str],
    **sections: Any,
) -> dict[str, Any]:
    """A report for a conversion that stopped before writing anything.

    ``sections`` are appended verbatim; the two blocked shapes differ only there.
    """
    return {
        "format": REPORT_FORMAT,
        "baseline": LEGACY_BASELINE,
        "target_schema": SCHEMA_VERSION,
        "batch_key": batch_key,
        "completion_status": "blocked",
        "source_path_excluded": True,
        "credentials_exported": False,
        "target_content_written": False,
        "counts": {
            "source_events": 0,
            "deletion_operations": 0,
            "episodes": 0,
            "claims": 0,
            "claim_versions": 0,
            "fact_claims_mapped": 0,
            "procedure_claims_mapped": 0,
            "procedure_versions": 0,
            "history_archived": 0,
            "unmapped": len(unmapped),
        },
        "unmapped": unmapped,
        "cutover_block": {"blocked": True, "reasons": reasons},
        **sections,
    }


def _blocked_prewrite_report(
    *,
    batch_key: str,
    unmapped: list[dict[str, Any]],
    reasons: list[str],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _blocked_report(
        batch_key=batch_key,
        unmapped=unmapped,
        reasons=reasons,
        source_status=[],
        deletion_receipts=[],
        permission_classification={
            "rows_with_explicit_scope_gap": 0,
            "schema_permission_unknown": True,
        },
        candidate_auto_promoted=False,
        **(extra or {}),
    )


def _canon(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canon(value).encode()).hexdigest()


def _stable(kind: str, value: object) -> str:
    return (
        f"{kind}-legacy-{hashlib.sha256(f'{kind}:{value}'.encode()).hexdigest()[:32]}"
    )


def _safe(value: object) -> object:
    return sanitize_structured_value(value)[0]


def _safe_text(value: object) -> tuple[str, bool]:
    raw = str(value or "")
    clean = sanitize_report_text(raw)
    return clean, clean != raw


def _json(value: object, default: object) -> object:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value or "")
        except (TypeError, ValueError, json.JSONDecodeError):
            return default
    return default


def _open_immutable(path: Path) -> sqlite3.Connection:
    """Open a frozen offline snapshot; ``immutable=1`` never touches its journals."""
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info([{table}])")]


def _rows(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    if table not in _tables(conn):
        return []
    return [
        {str(k): row[k] for k in row.keys()}
        for row in conn.execute(f"SELECT * FROM [{table}]")
    ]


def _time(value: object) -> tuple[str | None, str]:
    text = str(value or "")
    if not _ISO.fullmatch(text):
        return None, "unknown"
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return stamp.astimezone(timezone.utc).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z"), "instant"
    except ValueError:
        return None, "unknown"


def _recorded(value: object) -> str:
    return _time(value)[0] or "1970-01-01T00:00:00.000000Z"
