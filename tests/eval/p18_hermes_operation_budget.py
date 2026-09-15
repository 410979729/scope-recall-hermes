"""P18 outer-operation ownership for the existing P11 Hermes bridge.

This adapter owns no model-money ledger.  It places a short-lived, redacted
operation marker in the isolated bridge state; the P11 bridge consumes that
marker before reserving its existing ``requests`` row.  ``formal_usage`` then
binds the observed bridge archive and exact upstream request bytes to that
row for the P18 evidence writer.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Mapping

from p18_ledger_reference import ledger_path_reference


class HermesOperationBudgetError(ValueError):
    """Fail-closed outer operation or bridge archive mismatch."""


_SCHEMA = "scope-recall.p18.hermes-active-operation.v1"
DEFAULT_OPERATION_LEASE_SECONDS = 60.0


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _strict_text(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 240:
        raise HermesOperationBudgetError(name)
    return value


class HermesOperationBudget:
    """Bridge marker plus read-only linkage to the existing Go ledger."""

    def __init__(self, state: str | Path, ledger_path: str | Path, *, freeze_sha256: str, operation_root: str | Path, config_root: str | Path, bridge_model: str = "deepseek-v4-flash", operation_lease_seconds: float = DEFAULT_OPERATION_LEASE_SECONDS) -> None:
        self.state = Path(state).expanduser().resolve()
        self.archive = self.state / "archive"
        self.marker = self.state / "active-operation.json"
        self.ledger_path = Path(ledger_path).expanduser().resolve()
        self.operation_root = Path(operation_root).expanduser().resolve()
        self.config_root = Path(config_root).expanduser().resolve()
        if len(freeze_sha256) != 64 or any(char not in "0123456789abcdefABCDEF" for char in freeze_sha256):
            raise HermesOperationBudgetError("freeze_sha256")
        self.freeze_sha256 = freeze_sha256.lower()
        self.bridge_model = _strict_text("bridge_model", bridge_model)
        if type(operation_lease_seconds) not in (int, float) or not 0 < float(operation_lease_seconds) <= 120:
            raise HermesOperationBudgetError("operation_lease_seconds")
        self.operation_lease_seconds = float(operation_lease_seconds)
        self.reservation: dict[str, Any] | None = None
        self.last_archive: Path | None = None
        self._finished_reservation: dict[str, Any] | None = None
        self._finished_archives: tuple[Path, ...] = ()

    def reserve(self, model: str, request: bytes) -> dict[str, Any]:
        if model != self.bridge_model or not isinstance(request, bytes) or not request:
            raise HermesOperationBudgetError("bridge_model_or_request")
        if self.reservation is not None:
            raise HermesOperationBudgetError("operation_already_reserved")
        operation_id = _strict_text("operation_id", str(getattr(self, "operation_id", "")))
        request_id = _strict_text("request_id", str(getattr(self, "request_id", "")))
        task_id = _strict_text("task_id", str(getattr(self, "task_id", "")))
        context_id = _strict_text("context_id", str(getattr(self, "context_id", "")))
        expires_ns = time.time_ns() + int(self.operation_lease_seconds * 1_000_000_000)
        payload = {
            "schema": _SCHEMA,
            "operation_id": operation_id,
            "request_id": request_id,
            "task_id": task_id,
            "context_id": context_id,
            "formal_config_sha256": self.freeze_sha256,
            "expires_ns": expires_ns,
        }
        self.state.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise HermesOperationBudgetError("bridge_operation_busy") from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
        except Exception:
            try:
                self.marker.unlink()
            except FileNotFoundError:
                pass
            raise
        self.reservation = payload
        return dict(payload)

    def set_operation(self, *, operation_id: str, request_id: str, task_id: str, context_id: str) -> None:
        if self.reservation is not None:
            raise HermesOperationBudgetError("operation_already_reserved")
        # A new operation owns the formal_usage snapshot from this point on;
        # never let a failed/new operation report the preceding operation's
        # ledger rows.
        self._finished_reservation = None
        self._finished_archives = ()
        self.last_archive = None
        self.operation_id = _strict_text("operation_id", operation_id)
        self.request_id = _strict_text("request_id", request_id)
        self.task_id = _strict_text("task_id", task_id)
        self.context_id = _strict_text("context_id", context_id)

    def finish(self, reservation: Any, status: str, usage: Mapping[str, int] | None) -> str:
        if not isinstance(reservation, Mapping) or self.reservation is None or dict(reservation) != self.reservation:
            raise HermesOperationBudgetError("reservation_mismatch")
        if not isinstance(status, str) or not status:
            raise HermesOperationBudgetError("status")
        deadline = time.time_ns() + 5 * 1_000_000_000
        while time.time_ns() < deadline:
            rows = sorted(self.archive.glob("bridge-request-*.json"), key=lambda path: path.stat().st_mtime_ns)
            matched: list[tuple[Path, dict[str, Any]]] = []
            for path in reversed(rows):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                binding = value.get("p18_active_operation") if isinstance(value, dict) else None
                if isinstance(binding, Mapping) and binding.get("operation_id") == self.reservation["operation_id"]:
                    matched.append((path, value))
            if matched:
                # Preserve the completed operation independently of the active
                # reservation.  The next journey operation can therefore bind
                # immediately without losing this operation's evidence.
                self._finished_reservation = dict(self.reservation)
                self._finished_archives = tuple(path for path, _ in reversed(matched))
                self.last_archive = self._finished_archives[-1]
                try:
                    self.marker.unlink()
                except FileNotFoundError:
                    pass
                self.reservation = None
                return str(matched[0][1].get("ledger_status") or status)
            time.sleep(0.01)
        # No bridge archive means no model ledger row was observed. Keep the
        # marker for the operator to inspect; never claim a completed call.
        return "bridge_archive_missing"

    def formal_usage(self) -> dict[str, Any] | None:
        reservation = self._finished_reservation
        archive_paths = self._finished_archives
        if reservation is None or not archive_paths:
            return None
        entries: list[dict[str, Any]] = []
        seen_ids: dict[int, str] = {}
        with sqlite3.connect(self.ledger_path.as_uri() + "?mode=ro", uri=True, timeout=2) as db:
            input_total = 0
            output_total = 0
            all_known = True
            for archive_path in archive_paths:
                if not archive_path.is_file():
                    raise HermesOperationBudgetError("bridge_archive_missing")
                value = json.loads(archive_path.read_text(encoding="utf-8"))
                request_id = value.get("ledger_request_id")
                if type(request_id) is not int or request_id < 1:
                    raise HermesOperationBudgetError("bridge_ledger_request_id")
                raw_path_value = value.get("request_artifact_path")
                if not isinstance(raw_path_value, str):
                    raise HermesOperationBudgetError("bridge_request_artifact_path")
                raw_path = Path(raw_path_value).expanduser().resolve()
                if not raw_path.is_file():
                    raise HermesOperationBudgetError("bridge_request_artifact_missing")
                body = raw_path.read_bytes()
                body_sha = _digest(body)
                if value.get("request_artifact_sha256") != body_sha:
                    raise HermesOperationBudgetError("bridge_request_artifact_hash_mismatch")
                prior_sha = seen_ids.get(request_id)
                if prior_sha is not None:
                    if prior_sha != body_sha:
                        raise HermesOperationBudgetError("bridge_request_id_artifact_conflict")
                    continue
                seen_ids[request_id] = body_sha
                destination = self.operation_root / str(reservation["operation_id"]) / f"model-request-{request_id}.json"
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists() and destination.read_bytes() != body:
                    raise HermesOperationBudgetError("model_request_artifact_conflict")
                if not destination.exists():
                    destination.write_bytes(body)
                row = db.execute("SELECT actual_input,actual_output FROM requests WHERE id=?", (request_id,)).fetchone()
                known = row is not None and type(row[0]) is int and type(row[1]) is int and row[0] >= 0 and row[1] >= 0
                if known:
                    input_total += row[0]
                    output_total += row[1]
                else:
                    all_known = False
                entries.append({"id": request_id, "request": {"path": str(destination.relative_to(self.config_root)), "sha256": body_sha}})
        if not entries:
            return None
        return {
            "status": "known" if all_known else "unknown",
            "input_tokens": input_total if all_known else None,
            "output_tokens": output_total if all_known else None,
            "ledger_path": ledger_path_reference(self.ledger_path, self.config_root),
            "entries": entries,
        }


__all__ = ["HermesOperationBudget", "HermesOperationBudgetError"]
