"""OpenClaw memory import planner and sanitizer.

Imports must redact risky metadata, reject transcript-shaped noise, and write through an import ledger for idempotence."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .gating import compact_text, dedup_key
from .governance import classify_memory
from .graph import sync_memory_entities
from .models import ImportedMemoryRow, build_import_fingerprint, json_dumps_stable, normalize_import_fingerprint_timestamp, normalize_import_timestamp
from .sql_store import ensure_schema

DEFAULT_ALLOWED_TARGETS = {"memory", "ops", "project", "user"}
IMPORT_LEDGER_SOURCE_KIND = "openclaw-memory-lancedb-pro"

_SECRET_LIKE_RE = re.compile(r"\b(?:api[_ -]?key|secret|password|passwd|token)\b\s*(?:=|:)", re.I)
_BEARER_RE = re.compile(r"\bbearer\s+[A-Za-z0-9._\-~+/=]{8,}", re.I)
_OPENAI_STYLE_RE = re.compile(r"\bsk-[A-Za-z0-9]{8,}", re.I)
_PATH_LIKE_RE = re.compile(r"(?:^|\s)(?:/[A-Za-z0-9._\-/]{3,}|[A-Za-z]:\\\\[A-Za-z0-9._\\\\-]{3,})")
_TEMPLATE_LIKE_RE = re.compile(r"(?:\{\{[^{}]{1,120}\}\}|\$\{[^{}]{1,120}\})")
_REDACT_ASSIGNMENT_RE = re.compile(r"(\b(?:api[_ -]?key|secret|password|passwd|token)\b\s*(?:=|:)\s*)\S+", re.I)
_SECRET_KEY_RE = re.compile(r"\b(?:api[_ -]?key|secret|password|passwd|token)\b", re.I)
_ROLE_PREFIX_TRANSCRIPT_RE = re.compile(r"(?im)^\s*(?:user|assistant|system|tool)\s*:")
_TOOL_TRACE_RE = re.compile(r"\b(?:tool execution trace|stdout|stderr|traceback)\b", re.I)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_targets(allowed_targets: set[str] | list[str] | tuple[str, ...] | None) -> set[str]:
    if allowed_targets is None:
        return set(DEFAULT_ALLOWED_TARGETS)
    return {str(item).strip().lower() for item in allowed_targets if str(item).strip()}


def sanitize_snippet(text: str, limit: int = 180) -> str:
    redacted = _REDACT_ASSIGNMENT_RE.sub(r"\1[REDACTED]", str(text or ""))
    redacted = _BEARER_RE.sub("Bearer [REDACTED]", redacted)
    redacted = _OPENAI_STYLE_RE.sub("sk-[REDACTED]", redacted)
    return compact_text(redacted, limit)


def lint_openclaw_content(row_id: str, content: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    text = str(content or "")
    if _SECRET_LIKE_RE.search(text) or _BEARER_RE.search(text) or _OPENAI_STYLE_RE.search(text):
        findings.append({"row_id": row_id, "kind": "secret_like", "severity": "high", "snippet": sanitize_snippet(text)})
    if _PATH_LIKE_RE.search(text):
        findings.append({"row_id": row_id, "kind": "path_like", "severity": "medium", "snippet": sanitize_snippet(text)})
    if _TEMPLATE_LIKE_RE.search(text):
        findings.append({"row_id": row_id, "kind": "template_like", "severity": "medium", "snippet": sanitize_snippet(text)})
    return findings


def looks_like_raw_transcript(content: str) -> bool:
    text = str(content or "")
    role_prefix_count = len(_ROLE_PREFIX_TRANSCRIPT_RE.findall(text))
    if role_prefix_count >= 2:
        return True
    return role_prefix_count >= 1 and bool(_TOOL_TRACE_RE.search(text))


def _metadata_path(prefix: str, key: str) -> str:
    return f"{prefix}.{key}" if prefix else key


def lint_openclaw_metadata(row_id: str, value: Any, *, prefix: str = "metadata") -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            path = _metadata_path(prefix, key_text)
            if _SECRET_KEY_RE.search(key_text):
                findings.append({"row_id": row_id, "kind": "secret_like", "severity": "high", "field": path, "snippet": "[REDACTED]"})
            findings.extend(lint_openclaw_metadata(row_id, item, prefix=path))
        return findings
    if isinstance(value, (list, tuple, set)):
        for index, item in enumerate(value):
            findings.extend(lint_openclaw_metadata(row_id, item, prefix=f"{prefix}[{index}]"))
        return findings
    text = str(value or "")
    if not text:
        return findings
    for item in lint_openclaw_content(row_id, text):
        finding = dict(item)
        finding["field"] = prefix
        findings.append(finding)
    return findings


def redact_openclaw_metadata(value: Any, *, key_hint: str = "") -> Any:
    if isinstance(value, dict):
        return {str(key): redact_openclaw_metadata(item, key_hint=str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_openclaw_metadata(item, key_hint=key_hint) for item in value]
    if isinstance(value, tuple):
        return [redact_openclaw_metadata(item, key_hint=key_hint) for item in value]
    if isinstance(value, set):
        return [redact_openclaw_metadata(item, key_hint=key_hint) for item in sorted(value, key=str)]
    if _SECRET_KEY_RE.search(str(key_hint or "")):
        return "[REDACTED]"
    if isinstance(value, str):
        return sanitize_snippet(value, 500)
    return value


def map_openclaw_row(row: dict[str, Any], scope_prefix: str) -> ImportedMemoryRow | None:
    content = str(row.get("text") or "").strip()
    if not content:
        return None
    raw_scope = str(row.get("scope") or "unknown").strip() or "unknown"
    category = str(row.get("category") or "memory").strip().lower() or "memory"
    raw_timestamp = row.get("timestamp")
    updated_at = normalize_import_timestamp(raw_timestamp)
    fingerprint_timestamp = normalize_import_fingerprint_timestamp(raw_timestamp)
    metadata = row.get("metadata")
    metadata_text = metadata if isinstance(metadata, str) else json_dumps_stable(metadata or {})
    fingerprint = build_import_fingerprint(
        source_id=str(row.get("id") or ""),
        raw_scope=raw_scope,
        category=category,
        text=content,
        timestamp=fingerprint_timestamp,
        metadata_text=metadata_text,
    )
    return ImportedMemoryRow(
        id=f"openclaw:{fingerprint}",
        scope_id=f"{scope_prefix}|{raw_scope}",
        platform="imported-openclaw",
        user_id="",
        chat_id="",
        thread_id="",
        gateway_session_key="",
        agent_identity="openclaw-import",
        agent_workspace="scope-recall",
        session_id="openclaw-import",
        source="openclaw-import",
        target=category,
        content=content,
        summary=compact_text(content, 220),
        created_at=updated_at,
        updated_at=updated_at,
        import_metadata=metadata_text,
        import_fingerprint=fingerprint,
    )


def build_import_plan(
    rows: list[dict[str, Any]],
    *,
    source_path: Path,
    target_db: Path,
    scope_prefix: str = "imported.openclaw",
    allowed_targets: set[str] | list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Build a sanitized, idempotent OpenClaw import plan.

    The plan separates accepted rows, rejected rows, and already-imported fingerprints before any write happens."""
    allowed = _clean_targets(allowed_targets)
    mapped: list[ImportedMemoryRow] = []
    rejections: list[dict[str, Any]] = []
    lint_findings: list[dict[str, Any]] = []
    empty_rows = 0
    for raw in rows:
        row = map_openclaw_row(raw, scope_prefix)
        if row is None:
            empty_rows += 1
            continue
        row_id = str(raw.get("id") or row.id)
        if row.target not in allowed:
            rejections.append({
                "row_id": row_id,
                "target": row.target,
                "reason": "target_not_allowed",
                "snippet": sanitize_snippet(row.content),
            })
        elif looks_like_raw_transcript(row.content):
            rejections.append({
                "row_id": row_id,
                "target": row.target,
                "reason": "raw_transcript",
                "snippet": sanitize_snippet(row.content),
            })
        else:
            mapped.append(row)
        lint_findings.extend(lint_openclaw_content(row_id, row.content))
        lint_findings.extend(lint_openclaw_metadata(row_id, raw.get("metadata")))
    high_risk_count = sum(1 for item in lint_findings if item.get("severity") == "high")
    blocking_lint_count = len(lint_findings)
    safe_to_apply = not rejections and blocking_lint_count == 0
    return {
        "source": str(source_path),
        "target_db": str(target_db),
        "scope_prefix": scope_prefix,
        "allowed_targets": sorted(allowed),
        "rows_seen": len(rows),
        "rows_empty": empty_rows,
        "rows_mappable": len(mapped) + len(rejections),
        "rows_rejected": len(rejections),
        "rejections": rejections,
        "lint": {
            "finding_count": len(lint_findings),
            "blocking_count": blocking_lint_count,
            "high_risk_count": high_risk_count,
            "findings": lint_findings,
        },
        "safe_to_apply": safe_to_apply,
        "importable_rows": mapped,
    }


def ensure_import_ledger_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS import_ledger (
            import_fingerprint TEXT PRIMARY KEY,
            source_kind TEXT NOT NULL,
            source_scope TEXT NOT NULL,
            source_path TEXT NOT NULL,
            memory_id TEXT NOT NULL,
            imported_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_import_ledger_memory_id ON import_ledger(memory_id)")
    conn.commit()


def backup_sqlite(conn: sqlite3.Connection, db_path: Path) -> str:
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    for _ in range(5):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d.%H%M%S.%f")
        backup_path = backup_dir / f"memory.sqlite3.pre-openclaw-import.{stamp}.{uuid.uuid4().hex[:8]}.sqlite3"
        try:
            fd = backup_path.open("xb")
        except FileExistsError:
            continue
        fd.close()
        dest = sqlite3.connect(backup_path)
        try:
            conn.backup(dest)
        finally:
            dest.close()
        return str(backup_path)
    raise RuntimeError("failed to create a unique OpenClaw import backup path")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup_receipt(path: str) -> dict[str, Any]:
    if not path:
        return {"path": "", "sha256": "", "size_bytes": 0, "created_at": ""}
    backup_path = Path(path)
    stat = backup_path.stat()
    return {
        "path": str(backup_path),
        "sha256": file_sha256(backup_path),
        "size_bytes": stat.st_size,
        "created_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    }


def graph_repair_receipt(target_db: Path) -> dict[str, str]:
    hermes_home = target_db.parent.parent if target_db.parent.name == "scope-recall" else target_db.parent
    return {
        "status": "not_run",
        "command": f"python scripts/repair.graph_hygiene.py --hermes-home {hermes_home} --apply",
    }


def _metadata_for_import(row: ImportedMemoryRow, source_path: Path) -> str:
    try:
        source_metadata = json.loads(row.import_metadata or "{}")
    except Exception:
        source_metadata = {"raw_metadata": row.import_metadata}
    metadata: dict[str, Any] = dict(classify_memory(row.content, row.target, row.source))
    metadata.update({
        "source_import": {
            "kind": IMPORT_LEDGER_SOURCE_KIND,
            "source_path": str(source_path),
            "fingerprint": row.import_fingerprint,
            "source_metadata": redact_openclaw_metadata(source_metadata),
        }
    })
    return json.dumps(metadata, ensure_ascii=False, sort_keys=True)


def _row_receipt(row: ImportedMemoryRow) -> dict[str, str]:
    return {"memory_id": row.id, "fingerprint": row.import_fingerprint, "scope_id": row.scope_id, "target": row.target}


def import_mapped_rows(conn: sqlite3.Connection, rows: list[ImportedMemoryRow], source_path: Path) -> dict[str, Any]:
    """Import sanitized OpenClaw rows into Scope Recall SQLite truth.

    The importer records ledger entries and skips already-seen fingerprints so repeated runs are idempotent."""
    inserted_rows: list[dict[str, str]] = []
    skipped_rows: list[dict[str, str]] = []
    graph_failures: list[dict[str, str]] = []
    ledger_fingerprints: list[str] = []
    for row in rows:
        receipt_row = _row_receipt(row)
        ledger_hit = conn.execute("SELECT memory_id FROM import_ledger WHERE import_fingerprint = ?", (row.import_fingerprint,)).fetchone()
        if ledger_hit:
            receipt_row["memory_id"] = str(ledger_hit["memory_id"])
            skipped_rows.append(receipt_row)
            ledger_fingerprints.append(row.import_fingerprint)
            continue
        metadata_json = _metadata_for_import(row, source_path)
        key = dedup_key(row.content)
        before_memory = conn.total_changes
        conn.execute(
            """
            INSERT OR IGNORE INTO memories (
                id, scope_id, platform, user_id, chat_id, thread_id, gateway_session_key,
                agent_identity, agent_workspace, session_id, source, target, content, summary,
                created_at, updated_at, last_recalled_turn, dedup_key, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                row.id,
                row.scope_id,
                row.platform,
                row.user_id,
                row.chat_id,
                row.thread_id,
                row.gateway_session_key,
                row.agent_identity,
                row.agent_workspace,
                row.session_id,
                row.source,
                row.target,
                row.content,
                row.summary,
                row.created_at,
                row.updated_at,
                key,
                metadata_json,
            ),
        )
        inserted_memory = conn.total_changes > before_memory
        if conn.execute("SELECT 1 FROM memories_fts WHERE memory_id = ? LIMIT 1", (row.id,)).fetchone() is None:
            conn.execute("INSERT INTO memories_fts(memory_id, content, summary) VALUES (?, ?, ?)", (row.id, row.content, row.summary))
        conn.execute(
            """
            INSERT OR IGNORE INTO import_ledger (
                import_fingerprint, source_kind, source_scope, source_path, memory_id, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (row.import_fingerprint, IMPORT_LEDGER_SOURCE_KIND, row.scope_id, str(source_path), row.id, now_iso()),
        )
        ledger_fingerprints.append(row.import_fingerprint)
        try:
            sync_memory_entities(conn, memory_id=row.id, content=row.content, target=row.target, metadata=json.loads(metadata_json))
        except Exception as exc:
            graph_failures.append({"memory_id": row.id, "fingerprint": row.import_fingerprint, "error": compact_text(str(exc), 160)})
        if inserted_memory:
            inserted_rows.append(receipt_row)
        else:
            skipped_rows.append(receipt_row)
    conn.commit()
    return {
        "rows_inserted": len(inserted_rows),
        "rows_skipped": len(skipped_rows),
        "inserted": inserted_rows,
        "skipped": skipped_rows,
        "ledger_fingerprints": ledger_fingerprints,
        "graph_sync_failures": graph_failures,
        "graph_sync_failed_count": len(graph_failures),
    }


def vector_repair_receipt(vector_repair: str, target_db: Path) -> dict[str, str]:
    mode = str(vector_repair or "recommend").strip().lower()
    if mode in {"", "recommend", "recommended"}:
        mode = "recommend"
    hermes_home = target_db.parent.parent if target_db.parent.name == "scope-recall" else target_db.parent
    command = f"hermes-scope-recall vector repair --hermes-home {hermes_home}"
    if mode in {"dry-run", "dry_run"}:
        command += " --dry-run"
        mode = "dry-run"
    elif mode == "none":
        command = ""
    elif mode == "apply":
        pass
    else:
        mode = "recommend"
    return {"mode": mode, "command": command, "status": "not_run"}


def _public_report(plan: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in plan.items() if key != "importable_rows"}


def run_openclaw_import_rows(
    rows: list[dict[str, Any]],
    *,
    source_path: Path,
    target_db: Path,
    scope_prefix: str = "imported.openclaw",
    allowed_targets: set[str] | list[str] | tuple[str, ...] | None = None,
    apply: bool = False,
    receipt_path: Path | None = None,
    vector_repair: str = "recommend",
) -> dict[str, Any]:
    """Run the OpenClaw import workflow against already-mapped rows.

    The function applies lint, ledger checks, and optional writes so dry-run output matches the eventual import plan."""
    source_path = Path(source_path).expanduser()
    target_db = Path(target_db).expanduser()
    plan = build_import_plan(rows, source_path=source_path, target_db=target_db, scope_prefix=scope_prefix, allowed_targets=allowed_targets)
    report = {
        "ok": True,
        "dry_run": not apply,
        **_public_report(plan),
        "rows_inserted": 0,
        "rows_skipped": 0,
        "inserted": [],
        "skipped": [],
        "ledger_fingerprints": [],
        "graph_sync_failures": [],
        "graph_sync_failed_count": 0,
        "graph_repair": graph_repair_receipt(target_db),
        "backup": "",
        "backup_info": backup_receipt(""),
        "receipt_path": "",
        "vector_repair": vector_repair_receipt(vector_repair, target_db),
        "idempotent": True,
    }
    if not apply:
        return report
    if not bool(plan["safe_to_apply"]):
        report["ok"] = False
        report["error"] = "safety findings block OpenClaw import apply"
        return report
    importable_rows = list(plan["importable_rows"])
    target_existed = target_db.exists()
    target_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target_db, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma busy_timeout=30000")
    try:
        backup = backup_sqlite(conn, target_db) if target_existed else ""
        ensure_schema(conn)
        ensure_import_ledger_schema(conn)
        import_result = import_mapped_rows(conn, importable_rows, source_path)
    finally:
        conn.close()
    report["backup"] = backup
    report["backup_info"] = backup_receipt(backup)
    report.update(import_result)
    if receipt_path is not None:
        receipt_path = Path(receipt_path).expanduser()
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        report["receipt_path"] = str(receipt_path)
        receipt_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return report
