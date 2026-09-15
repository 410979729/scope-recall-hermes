#!/usr/bin/env python3
"""Operator CLI for listing, inspecting, reviewing, and promoting Experience playbooks.

This script is a human review surface; it should not hide duplicate/superseded status behind terse output."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from scope_recall.capture_filters import sanitize_report_text
    from scope_recall.experience_models import ExperienceValidationError
    from scope_recall.experience_store import find_duplicate_playbooks, review_playbook, search_playbooks
    from scope_recall.operator_ledger import (
        mirror_operator_receipt,
        operator_ledger_report,
        record_committed_operator_operation,
        recover_operator_receipts,
    )
    from scope_recall.sql_store import ensure_schema
    from scope_recall.truth_connection import connect_truth_database
except Exception:  # pragma: no cover - source-tree execution fallback
    from capture_filters import sanitize_report_text  # type: ignore
    from experience_models import ExperienceValidationError  # type: ignore
    from experience_store import find_duplicate_playbooks, review_playbook, search_playbooks  # type: ignore
    from operator_ledger import (  # type: ignore
        mirror_operator_receipt,
        operator_ledger_report,
        record_committed_operator_operation,
        recover_operator_receipts,
    )
    from sql_store import ensure_schema  # type: ignore
    from truth_connection import connect_truth_database  # type: ignore


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Operate Scope Recall Experience playbooks")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("list", "dedupe", "review", "promote", "quarantine", "supersede", "receipts"):
        cmd = sub.add_parser(name, help=f"playbook {name}")
        cmd.add_argument("--hermes-home", help="Hermes home/profile path")
        cmd.add_argument("--db", help="Explicit memory.sqlite3 path; overrides --hermes-home")
        cmd.add_argument("--scope-id", action="append", default=[], help="Restrict to a scope id; repeatable. Defaults to all playbook scopes.")
        cmd.add_argument("--limit", type=int, default=20)
        cmd.add_argument("--status", default="")
        cmd.add_argument("--json", action="store_true", help="Emit JSON output (default; accepted for operator consistency)")
        if name == "list":
            cmd.add_argument("--query", default="")
        if name == "receipts":
            cmd.add_argument(
                "--apply",
                action="store_true",
                help="Retry pending receipt mirrors. Without this, report debt read-only.",
            )
            cmd.add_argument(
                "--include-failed",
                action="store_true",
                help="Also retry rows that reached failed state.",
            )
        if name in {"review", "promote", "quarantine", "supersede"}:
            cmd.add_argument("--id", required=True, help="Playbook id")
            cmd.add_argument("--reason", default="", help="Operator review reason")
            cmd.add_argument("--apply", action="store_true", help="Apply the lifecycle mutation. Without this, write commands are validation-only dry-runs.")
            cmd.add_argument("--force-cross-class", action="store_true", help="Allow superseding/merging semantically different playbooks; requires --reason.")
        if name == "supersede":
            cmd.add_argument("--superseded-by", required=True, help="Canonical playbook id replacing --id")
    return parser.parse_args(argv)


def _db_path(args: argparse.Namespace) -> Path:
    if args.db:
        return Path(args.db).expanduser().resolve()
    if args.hermes_home:
        return Path(args.hermes_home).expanduser().resolve() / "scope-recall" / "memory.sqlite3"
    return Path.home() / ".hermes" / "scope-recall" / "memory.sqlite3"


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    return connect_truth_database(
        path,
        mode="ro" if read_only else "rw",
        timeout=30,
    )


def _unlink_artifact(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _remove_new_empty_directory(path: Path, *, existed_before: bool) -> None:
    if existed_before:
        return
    try:
        path.rmdir()
    except (FileNotFoundError, OSError):
        pass


def _backup_db(path: Path) -> str:
    backup_dir = path.parent / "backups"
    directory_existed = backup_dir.exists()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S+0000")
    backup_path = backup_dir / f"{path.stem}.playbooks.{stamp}.{uuid.uuid4().hex[:8]}.sqlite3"
    temporary_path = backup_path.with_suffix(f"{backup_path.suffix}.tmp")
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        source = connect_truth_database(path, mode="ro")
        try:
            dest = sqlite3.connect(temporary_path)
            try:
                source.backup(dest)
            finally:
                dest.close()
        finally:
            source.close()
        os.replace(temporary_path, backup_path)
        return str(backup_path)
    except BaseException:
        _unlink_artifact(temporary_path)
        _unlink_artifact(backup_path)
        _remove_new_empty_directory(
            backup_dir, existed_before=directory_existed
        )
        raise


def _accessible_scope_ids(conn: sqlite3.Connection, raw_scope_ids: list[str]) -> list[str]:
    explicit = [item for item in raw_scope_ids if str(item).strip()]
    if explicit:
        return explicit
    rows = conn.execute("SELECT DISTINCT scope_id FROM procedural_playbooks ORDER BY scope_id").fetchall()
    return [str(row["scope_id"]) for row in rows] or [""]


_WRITE_COMMANDS = frozenset({"review", "promote", "quarantine", "supersede"})


def _review_action(command: str) -> str:
    return {
        "review": "review",
        "promote": "promote",
        "quarantine": "quarantine",
        "supersede": "supersede",
    }[command]


def _review_kwargs(args: argparse.Namespace, scopes: list[str], action: str) -> dict[str, Any]:
    return {
        "playbook_id": str(args.id),
        "accessible_scope_ids": scopes,
        "action": action,
        "reason": str(args.reason or ""),
        "superseded_by": str(getattr(args, "superseded_by", "") or ""),
        "force_cross_class": bool(getattr(args, "force_cross_class", False)),
    }


def _playbook_table_exists(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='procedural_playbooks'"
        ).fetchone()
        is not None
    )


def _before_review(conn: sqlite3.Connection, playbook_id: str) -> dict[str, Any]:
    inspected = conn.execute(
        "SELECT status, superseded_by, updated_at "
        "FROM procedural_playbooks WHERE id = ?",
        (playbook_id,),
    ).fetchone()
    if inspected is None:
        return {}
    return {
        "status": str(inspected["status"]),
        "superseded_by": str(inspected["superseded_by"] or ""),
        "updated_at": str(inspected["updated_at"] or ""),
    }


def _decorate_review_payload(
    payload: dict[str, Any],
    *,
    action: str,
    scopes: list[str],
) -> dict[str, Any]:
    payload.setdefault("action", action)
    payload.setdefault("scope_ids", scopes)
    payload["ok"] = bool(payload.get("reviewed")) and not payload.get("error")
    return payload


def _missing_playbook_payload(
    args: argparse.Namespace,
    *,
    action: str,
) -> dict[str, Any]:
    scopes = [item for item in list(args.scope_id or []) if str(item).strip()] or [""]
    return {
        "reviewed": False,
        "dry_run": True,
        "changed": False,
        "id": str(args.id),
        "error": "not_found",
        "action": action,
        "scope_ids": scopes,
        "ok": False,
    }


def _validation_failure(
    args: argparse.Namespace,
    *,
    action: str,
    error: BaseException,
    error_code: str,
) -> dict[str, Any]:
    scopes = [item for item in list(args.scope_id or []) if str(item).strip()] or [""]
    return {
        "reviewed": False,
        "dry_run": True,
        "changed": False,
        "id": str(args.id),
        "error": error_code,
        "detail": str(error)[:300],
        "action": action,
        "scope_ids": scopes,
        "ok": False,
    }


class _ApplyAborted(RuntimeError):
    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(str(payload.get("error") or "apply_aborted"))
        self.payload = payload


def _abort_payload(
    payload: dict[str, Any],
    *,
    action: str,
    scopes: list[str],
) -> _ApplyAborted:
    payload["dry_run"] = False
    return _ApplyAborted(
        _decorate_review_payload(payload, action=action, scopes=scopes)
    )


def _cleanup_apply_artifacts(
    *,
    db_path: Path,
    backup_path: str,
    backup_dir_existed: bool,
) -> None:
    """Remove only pre-commit artifacts; committed evidence is never deleted."""

    _unlink_artifact(Path(backup_path) if backup_path else None)
    _remove_new_empty_directory(
        db_path.parent / "backups", existed_before=backup_dir_existed
    )


def _validated_apply_kwargs(
    args: argparse.Namespace,
    *,
    scopes: list[str],
    validation: dict[str, Any],
) -> dict[str, Any]:
    """Drive mutation from the exact payload validated under the writer lock."""

    return {
        "playbook_id": str(validation["id"]),
        "accessible_scope_ids": scopes,
        "action": str(validation["status"]),
        "reason": str(args.reason or ""),
        "superseded_by": str(validation.get("superseded_by") or ""),
        "force_cross_class": bool(getattr(args, "force_cross_class", False)),
        "validated_payload": validation,
        "commit": False,
    }


def _apply_review_command(
    args: argparse.Namespace,
    *,
    db_path: Path,
) -> dict[str, Any]:
    """Commit mutation plus ledger atomically, then mirror its receipt."""

    action = _review_action(str(args.command))
    try:
        with _connect(db_path, read_only=True) as reader:
            if not _playbook_table_exists(reader):
                return _missing_playbook_payload(args, action=action)
            scopes = _accessible_scope_ids(reader, list(args.scope_id or []))
            review_kwargs = _review_kwargs(args, scopes, action)
            before = _before_review(reader, str(args.id))
            validation = review_playbook(reader, dry_run=True, **review_kwargs)
    except sqlite3.OperationalError as exc:
        return _validation_failure(
            args,
            action=action,
            error=exc,
            error_code="schema_validation_failed",
        )
    except ExperienceValidationError as exc:
        return _validation_failure(
            args,
            action=action,
            error=exc,
            error_code="semantic_validation_failed",
        )

    if validation.get("error"):
        return _decorate_review_payload(validation, action=action, scopes=scopes)

    backup_dir = db_path.parent / "backups"
    backup_dir_existed = backup_dir.exists()
    backup_path = ""
    committed = False
    writer = _connect(db_path, read_only=False)
    try:
        # Freeze the authority and row epoch before backup or migration.  A
        # RESERVED writer lock prevents another process from invalidating the
        # validated payload between these phases.
        writer.execute("BEGIN IMMEDIATE")
        try:
            locked_validation = review_playbook(
                writer, dry_run=True, **review_kwargs
            )
        except ExperienceValidationError as exc:
            raise _abort_payload(
                _validation_failure(
                    args,
                    action=action,
                    error=exc,
                    error_code="semantic_validation_failed",
                ),
                action=action,
                scopes=scopes,
            ) from exc
        if locked_validation.get("error"):
            raise _abort_payload(
                locked_validation, action=action, scopes=scopes
            )
        if locked_validation.get("validation_token") != validation.get(
            "validation_token"
        ):
            raise _abort_payload(
                {
                    "reviewed": False,
                    "changed": False,
                    "error": "validation_changed_before_lock",
                    "id": str(args.id),
                },
                action=action,
                scopes=scopes,
            )

        apply_validation = locked_validation
        if locked_validation.get("changed"):
            # This backup is from the exact locked epoch and remains pre-migration.
            backup_path = _backup_db(db_path)
            ensure_schema(writer, commit=False)
            try:
                migrated_validation = review_playbook(
                    writer, dry_run=True, **review_kwargs
                )
            except ExperienceValidationError as exc:
                raise _abort_payload(
                    _validation_failure(
                        args,
                        action=action,
                        error=exc,
                        error_code="semantic_validation_failed",
                    ),
                    action=action,
                    scopes=scopes,
                ) from exc
            if migrated_validation.get("error"):
                raise _abort_payload(
                    migrated_validation, action=action, scopes=scopes
                )
            if migrated_validation.get("validation_token") != locked_validation.get(
                "validation_token"
            ):
                raise _abort_payload(
                    {
                        "reviewed": False,
                        "changed": False,
                        "error": "validation_changed_during_apply",
                        "id": str(args.id),
                    },
                    action=action,
                    scopes=scopes,
                )
            apply_validation = migrated_validation

        payload = review_playbook(
            writer,
            dry_run=False,
            **_validated_apply_kwargs(
                args, scopes=scopes, validation=apply_validation
            ),
        )
        if payload.get("error") or not payload.get("reviewed"):
            raise _abort_payload(payload, action=action, scopes=scopes)
        payload["dry_run"] = False
        _decorate_review_payload(payload, action=action, scopes=scopes)
        payload["backup_path"] = backup_path
        if not payload.get("changed"):
            writer.rollback()
            payload["operation_id"] = ""
            payload["receipt_state"] = "not_required"
            payload["receipt_path"] = ""
            return payload

        operation_id = f"op_{uuid.uuid4().hex}"
        payload["operation_id"] = operation_id
        payload["receipt_state"] = "pending"
        payload["receipt_path"] = ""
        ledger = record_committed_operator_operation(
            writer,
            operation_id=operation_id,
            operation_kind=f"playbook.{action}",
            target_ref=str(args.id),
            before=before,
            result=payload,
            backup_path=backup_path,
            request_fingerprint=str(apply_validation.get("validation_token") or ""),
            commit=False,
        )
        payload["committed_at"] = str(ledger["committed_at"])
        writer.commit()
        committed = True

        try:
            mirror = mirror_operator_receipt(
                writer,
                db_path=db_path,
                operation_id=operation_id,
            )
        except Exception as exc:
            try:
                debt = writer.execute(
                    """
                    SELECT receipt_state, receipt_path, receipt_attempts,
                           receipt_last_error
                    FROM operator_operations WHERE operation_id=?
                    """,
                    (operation_id,),
                ).fetchone()
            except Exception:
                debt = None
            payload["receipt_state"] = str(debt[0]) if debt is not None else "pending"
            payload["receipt_path"] = str(debt[1] or "") if debt is not None else ""
            payload["receipt_attempts"] = int(debt[2] or 0) if debt is not None else 0
            payload["receipt_error"] = sanitize_report_text(str(exc))[:300]
            payload["receipt_repair_required"] = True
            return payload
        payload.update(mirror)
        payload["receipt_repair_required"] = False
        return payload
    except _ApplyAborted as exc:
        if writer.in_transaction:
            writer.rollback()
        _cleanup_apply_artifacts(
            db_path=db_path,
            backup_path=backup_path,
            backup_dir_existed=backup_dir_existed,
        )
        return exc.payload
    except BaseException:
        if writer.in_transaction:
            writer.rollback()
        if not committed:
            _cleanup_apply_artifacts(
                db_path=db_path,
                backup_path=backup_path,
                backup_dir_existed=backup_dir_existed,
            )
        raise
    finally:
        writer.close()


def _receipt_debt_command(
    args: argparse.Namespace,
    *,
    db_path: Path,
) -> dict[str, Any]:
    apply = bool(getattr(args, "apply", False))
    with _connect(db_path, read_only=not apply) as conn:
        report = operator_ledger_report(conn)
        if report["status"] == "schema_missing":
            return {
                "ok": not apply,
                "action": "receipt_repair" if apply else "receipt_report",
                "error": "operator_ledger_schema_missing" if apply else "",
                "report": report,
                "dry_run": not apply,
            }
        repair = {
            "attempted": 0,
            "mirrored": 0,
            "failed": 0,
            "errors": [],
        }
        if apply:
            repair = recover_operator_receipts(
                conn,
                db_path=db_path,
                limit=max(1, min(500, int(args.limit or 20))),
                include_failed=bool(getattr(args, "include_failed", False)),
            )
            report = operator_ledger_report(conn)
        return {
            "ok": int(repair["failed"]) == 0,
            "action": "receipt_repair" if apply else "receipt_report",
            "dry_run": not apply,
            "repair": repair,
            "report": report,
        }


def build_payload(args: argparse.Namespace) -> dict[str, Any]:
    db_path = _db_path(args)
    if not db_path.exists():
        return {"ok": False, "error": "db_missing", "path": str(db_path)}
    if args.command == "receipts":
        return _receipt_debt_command(args, db_path=db_path)
    if args.command in _WRITE_COMMANDS and bool(getattr(args, "apply", False)):
        return _apply_review_command(args, db_path=db_path)
    with _connect(db_path, read_only=True) as conn:
        scopes = _accessible_scope_ids(conn, list(args.scope_id or []))
        limit = max(1, min(100, int(args.limit or 20)))
        if args.command == "list":
            rows = search_playbooks(conn, query=str(args.query or ""), accessible_scope_ids=scopes, limit=limit, status=str(args.status or ""))
            return {"ok": True, "action": "list", "count": len(rows), "playbooks": rows, "scope_ids": scopes}
        if args.command == "dedupe":
            groups = find_duplicate_playbooks(conn, accessible_scope_ids=scopes, status=str(args.status or ""), limit=limit)
            return {"ok": True, "action": "dedupe", "count": len(groups), "groups": groups, "scope_ids": scopes}
        if args.command in _WRITE_COMMANDS:
            action = _review_action(str(args.command))
            payload = review_playbook(
                conn,
                dry_run=True,
                **_review_kwargs(args, scopes, action),
            )
            payload.setdefault("action", action)
            payload.setdefault("scope_ids", scopes)
            payload["ok"] = bool(
                payload.get("dry_run") or payload.get("reviewed")
            ) and not payload.get("error")
            return payload
    return {"ok": False, "error": "unknown_command", "command": args.command}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = build_payload(args)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
