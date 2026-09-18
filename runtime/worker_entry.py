"""Bounded, explicit entry point for one durable work-item drain.

The entry point deliberately accepts only a trusted configuration file.  It
does not initialize a database, infer identity from a request, or keep a
daemon alive after the bounded drain finishes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import stat
import tempfile
import time
from pathlib import Path
import sys
from typing import Any, TextIO

from ..core.file_lock import advisory_file_lock
from ..core.writer_lease import TruthWriterBusyError
from ..vector.process_store import NativeVectorPathError
from ..contracts import ContractError, TrustedContext
from .instance import RuntimeInstanceConfig, build_runtime_instance
from .model_budget import pre_request_refusals, provider_refusals
from .validation import strict_float, utc_now

#: Worker metadata files are small JSON documents; anything larger is not one.
METADATA_LIMIT_BYTES = 65536
#: Seconds a watchdog-owned pass stops short of the deadline it was handed, so
#: its refund, status file and receipt land before the owner kills the tree.
FINALIZE_MARGIN_SECONDS = 2.0
#: The largest ``used`` a valid day counter holds, for the worker that reserves
#: against it and the supervisor that plans from it.  It rejects a corrupt file;
#: it is not a daily cap: ``daily_work_limit`` goes to 1,000,000 and the uncapped
#: default still counts what every pass keeps.  A smaller planner-only bound
#: stopped supervision and autostart for the rest of a busy UTC day.
DAILY_COUNTER_MAX = 100_000_000


def load_config(path: str | Path) -> RuntimeInstanceConfig:
    config_path = Path(path)
    if not config_path.is_absolute():
        raise ValueError("config_must_be_absolute")
    if not config_path.is_file():
        raise ValueError("config_not_found")
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("config_unreadable") from exc
    if not isinstance(raw, dict):
        raise ValueError("config_mapping_required")
    return RuntimeInstanceConfig.from_mapping(raw)


def _is_actionable(error_code: object) -> bool:
    """Whether an operator could still clear this failure.

    Shares ``core/failure_retry``'s classification so the worker's status and
    the doctor's cannot disagree about which failures are by design.
    """
    if not error_code:
        return False
    from ..core.failure_retry import retry_class

    return retry_class(error_code) != "terminal"


def _receipt_payload(config: RuntimeInstanceConfig, receipt: Any, capability_gaps: list[str]) -> dict[str, Any]:
    items = [
        {
            "work_id": int(item.work_id),
            "work_type": str(item.work_type),
            "disposition": str(item.disposition),
            "state": str(item.state),
            "error_code": item.error_code,
            "error_detail": getattr(item, "error_detail", None),
        }
        for item in getattr(receipt, "items", ())
    ]
    idle = bool(getattr(receipt, "idle", False))
    # A run that met only by-design terminal outcomes did its job; counting
    # them as degraded made the worker disagree with the doctor, which uses
    # the same classification.
    degraded = (bool(capability_gaps)
                or int(getattr(receipt, "retried", 0)) > 0
                or any(_is_actionable(item.get("error_code")) for item in items))
    # An empty queue is a successful idle outcome even when optional external
    # routes are unavailable; the gaps remain explicit for the supervisor.
    unavailable = tuple(getattr(receipt, "unavailable_work_types", ()))
    status = "waiting" if idle and unavailable else ("idle" if idle else ("degraded" if degraded else "completed"))
    return {
        "status": status,
        "owner_id": config.owner_id,
        "installation_id": config.binding.installation_id,
        "processed": int(getattr(receipt, "processed", 0)),
        "completed": int(getattr(receipt, "completed", 0)),
        "failed": int(getattr(receipt, "failed", 0)),
        "retried": int(getattr(receipt, "retried", 0)),
        "skipped": int(getattr(receipt, "skipped", 0)),
        "stale": int(getattr(receipt, "stale", 0)),
        "obsolete": int(getattr(receipt, "obsolete", 0)),
        "items": items,
        "capability_gaps": sorted(set(capability_gaps)),
        "deferred": int(getattr(receipt, "deferred", 0)),
        "recovered": int(getattr(receipt, "recovered", 0)),
        "unavailable_work_types": list(unavailable),
    }


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    return path.exists() and bool(
        getattr(path.stat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _metadata_path(config: RuntimeInstanceConfig, name: str) -> Path:
    root = config.binding.data_directory
    if any(_is_reparse_point(path) for path in (root, *root.parents)):
        raise ValueError("worker_metadata_reparse_path")
    target = root / name
    if _is_reparse_point(target) or (target.exists() and not target.is_file()):
        raise ValueError("worker_metadata_not_regular")
    return target


def _read_metadata(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    if path.stat().st_size > METADATA_LIMIT_BYTES:
        raise ValueError("worker_metadata_oversized")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("worker_metadata_invalid")
    return value


def _atomic_metadata(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(payload.encode("utf-8")) > METADATA_LIMIT_BYTES:
        raise ValueError("worker_metadata_oversized")
    fd, raw = tempfile.mkstemp(prefix=".worker-status-", suffix=".tmp", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if path.is_symlink():
            raise ValueError("worker_metadata_not_regular")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def persist_worker_status(config: RuntimeInstanceConfig, payload: dict[str, Any], *,
                          started_at: str, exit_code: int, worker_pid: int | None = None) -> None:
    lock = _metadata_path(config, "runtime-worker-status.lock")
    with advisory_file_lock(lock, timeout_seconds=.2):
        _persist_worker_status_unlocked(config, payload, started_at=started_at,
                                       exit_code=exit_code, worker_pid=worker_pid)


def _persist_worker_status_unlocked(config: RuntimeInstanceConfig, payload: dict[str, Any], *,
                                   started_at: str, exit_code: int, worker_pid: int | None = None) -> None:
    """Bounded metadata only. Never retain model text, stderr, or credentials."""
    path = _metadata_path(config, "runtime-worker-status.json")
    previous = _read_metadata(path)
    if previous.get("installation_id") not in (None, config.binding.installation_id):
        raise ValueError("worker_status_binding_mismatch")
    if str(previous.get("started_at", "")) > started_at:
        return
    # Accept only the closed worker protocol, excluding arbitrary stderr/text.
    safe = {key: payload[key] for key in ("status", "processed", "completed", "failed", "retried",
            "skipped", "stale", "obsolete", "deferred", "recovered", "daily_queue_used",
            "pending_work", "failed_work", "terminal_failed_work",
            "oldest_pending_at") if key in payload}
    safe["capability_gaps"] = [str(value)[:120] for value in payload.get("capability_gaps", ())][:16]
    safe["unavailable_work_types"] = [str(value)[:32] for value in payload.get("unavailable_work_types", ())][:4]
    for key in ("ingress_replayed", "ingress_cancelled", "source_only"):
        if type(payload.get(key)) is int:
            safe[key] = payload[key]
    safe["items"] = [{key: item[key] for key in ("work_id", "work_type", "disposition", "state", "error_code", "error_detail") if key in item}
                     for item in payload.get("items", ())[:32]]
    finished = utc_now()
    safe.update(installation_id=config.binding.installation_id, started_at=started_at, finished_at=finished,
                exit_code=exit_code, worker_pid=worker_pid if worker_pid is not None else os.getpid(),
                last_success_at=finished if int(payload.get("completed", 0)) > 0 else previous.get("last_success_at"))
    _atomic_metadata(path, safe)


def _reserve_daily_work(config: RuntimeInstanceConfig) -> tuple[Path, dict[str, Any], int]:
    """Reserve this pass's queue items before draining, so a crashed worker gets no free retries.

    This cap complements, and never resets or expands, the auxiliary ledger's
    call/token/currency budget.
    """
    path = _metadata_path(config, "runtime-worker-day.json")
    prior = _read_metadata(path)
    if prior.get("installation_id") not in (None, config.binding.installation_id):
        raise ValueError("worker_budget_binding_mismatch")
    day = utc_now()[:10]
    used = prior.get("used", 0) if prior.get("day") == day else 0
    if type(used) is not int or not 0 <= used <= DAILY_COUNTER_MAX:
        raise ValueError("worker_budget_invalid")
    # 0 means uncapped: take a full page every pass and let the auxiliary ledger,
    # the only layer that knows what a request costs, be the limit.  The counter
    # still accumulates, so the day's volume stays observable.
    count = config.max_items if config.daily_work_limit == 0 else \
        min(config.max_items, max(0, config.daily_work_limit - used))
    state = dict(installation_id=config.binding.installation_id, day=day, used=used + count)
    _atomic_metadata(path, state)
    return path, state, count


def _failure_label(exc: BaseException) -> str:
    """The exception class plus, for SQLite, its symbolic error name.

    ``OperationalError`` alone names a family: "database is locked" and "no
    such column" arrive as the same string.  SQLite's error name is a bounded
    enum -- no paths, no model data -- so it is safe where the message is not.
    """
    name = type(exc).__name__
    code = getattr(exc, "sqlite_errorname", None)
    if type(code) is str and code.isascii() and code.replace("_", "").isalnum():
        return f"{name}:{code}"
    return name


def _writer_busy(exc: BaseException) -> bool:
    """Another writer held the truth database: a capture, a maintenance command, another tool."""
    if isinstance(exc, TruthWriterBusyError):
        return True
    return isinstance(exc, sqlite3.OperationalError) and any(word in str(exc).lower() for word in ("locked", "busy"))


def _exception_gap(exc: BaseException) -> str:
    if isinstance(exc, NativeVectorPathError):
        return NativeVectorPathError.code
    return f"worker_error:{_failure_label(exc)}"


def _write(output: TextIO, payload: dict[str, Any]) -> None:
    # One compact line is the process protocol.  Never include source content,
    # model output, paths outside the explicit installation identity, or a
    # traceback in worker stdout.
    output.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    output.flush()


def _minimal_payload(config: RuntimeInstanceConfig | None, status: str, gap: str) -> dict[str, Any]:
    return {
        "status": status,
        "owner_id": config.owner_id if config is not None else None,
        "installation_id": config.binding.installation_id if config is not None else None,
        "processed": 0,
        "items": [],
        "capability_gaps": [gap],
    }


def _report(sink: TextIO, config: RuntimeInstanceConfig | None, payload: dict[str, Any], *,
            started_at: str, exit_code: int, best_effort: bool = False) -> int:
    """Persist the status file, then write the one-line receipt.

    ``best_effort`` is for the failure path: a status file that cannot be
    written must not hide the receipt that names the failure.
    """
    if config is not None:
        try:
            persist_worker_status(config, payload, started_at=started_at, exit_code=exit_code)
        except (OSError, ValueError):
            if not best_effort:
                raise
    _write(sink, payload)
    return exit_code


def _vector_preflight_gap(config: RuntimeInstanceConfig) -> str | None:
    """A native path Lance cannot open is reported by name, whatever fails later."""
    if config.vector is None or config.vector.backend != "lancedb":
        return None
    from ..vector.process_store import ProcessLanceVectorStore

    vector = config.vector
    check = ProcessLanceVectorStore(vector.storage_dir, table_name=vector.table_name,
                                    dimensions=vector.dimensions, metric=vector.metric)
    return NativeVectorPathError.code if check.native_path_error() else None


def _drain_once(config: RuntimeInstanceConfig, instance: Any, deadline: float) -> dict[str, Any]:
    budget_path, budget_state, reserved = _reserve_daily_work(config)
    # Read before draining so the status file describes the pass it reports
    # on.  A refusing provider is reported, never used to cut the page:
    # core/worker already stands a rate-limited work type down for the rest of
    # the pass, and this one-hour lookback would throttle healthy models for an
    # hour after a provider switch.
    refusals = provider_refusals(getattr(instance.auxiliary, "ledger_path", None))
    receipt = instance.drain(max_items=reserved or config.max_items,
                             purge_only=reserved == 0,
                             remaining_seconds=max(.001, deadline - time.monotonic()))
    # Purge never spends the optional enrichment budget.
    used = sum(item.work_type != "purge" for item in receipt.items)
    budget_state["used"] -= max(0, reserved - used)
    _atomic_metadata(budget_path, budget_state)
    background_gaps = tuple(instance.background_gaps)
    # A held provider is the one refusal that does steer the pass: its work
    # types were not claimed at all (runtime/model_budget.py provider_holds).
    holds = sorted({f"provider_hold:{model[:64]}" for model, _until in (getattr(instance, "provider_holds", None) or {}).values()})
    gaps = [*(getattr(instance.auxiliary, "capability_gaps", ()) or ()), *refusals, *holds, *background_gaps]
    if reserved == 0:
        gaps.append("daily_queue_budget")
    payload = _receipt_payload(config, receipt, gaps)
    ingress = instance.ingress_receipts
    payload["ingress_replayed"] = sum(r.durability == "persisted" for r in ingress)
    payload["ingress_cancelled"] = sum(r.disposition == "cancelled" for r in ingress)
    payload["source_only"] = sum(item.disposition == "source_only" for item in receipt.items)
    payload["daily_queue_used"] = budget_state["used"]
    # The admission counts scan every source's JSON and the queue age walks every
    # queued row; a pass reports ``source_only`` from its own items and its depth
    # from a count, and the doctor is where the age is read.
    _apply_queue_status(payload, gaps, instance.status(include_admission=False, include_queue_age=False),
                        receipt, background_gaps)
    return payload


def _apply_queue_status(payload: dict[str, Any], gaps: list[str], queue: Any, receipt: Any,
                        background_gaps: tuple[str, ...]) -> None:
    """Fold the queue's standing into the pass status, with the same
    classification the doctor uses.  Never degraded without saying why: an
    empty gap list is what sent a watcher hunting through two-day-old logs."""
    terminal_failed = sum(count for code, count in queue.work_error_counts
                          if not _is_actionable(code))
    actionable_failed = max(0, queue.failed_work - terminal_failed)
    payload.update(pending_work=queue.pending_work, failed_work=queue.failed_work,
                   terminal_failed_work=terminal_failed,
                   oldest_pending_at=queue.oldest_pending_at)
    if actionable_failed or background_gaps or payload["source_only"]:
        payload["status"] = "degraded"
        if actionable_failed:
            gaps.append(f"work_failed:{actionable_failed}")
        if payload["source_only"]:
            gaps.append(f"source_only:{payload['source_only']}")
        payload["capability_gaps"] = sorted(set(gaps))
    elif terminal_failed:
        gaps.append("work_failed_terminal_only")
        payload["capability_gaps"] = sorted(set(gaps))
    elif receipt.idle and queue.pending_work:
        payload["status"] = "waiting"


def _pass_deadline(config: RuntimeInstanceConfig, deadline_epoch: float | None) -> float:
    """This pass's monotonic deadline: its own drain budget, cut to its owner's.

    A watchdog hands its deadline as wall-clock epoch seconds, because a
    monotonic reading means nothing in another process, and this pass keeps
    ``FINALIZE_MARGIN_SECONDS`` of it back for everything after the drain.
    """
    now = time.monotonic()
    deadline = now + config.drain_seconds
    if deadline_epoch is not None:
        handed = strict_float("worker_deadline_epoch", deadline_epoch, minimum=0.0, maximum=math.inf)
        deadline = min(deadline, now + (handed - time.time()) - FINALIZE_MARGIN_SECONDS)
    return deadline


def _owner_timeout(sink: TextIO, config: RuntimeInstanceConfig | None, *, started_at: str) -> int:
    """The receipt a watchdog writes when it kills a pass that overran its
    window, written instead by a pass that ran out of the window it was handed:
    the same exit code and gap, which a supervisor survives, with nothing killed."""
    payload = _minimal_payload(config, "degraded", "worker_watchdog_timeout")
    return _report(sink, config, payload, started_at=started_at, exit_code=124, best_effort=True)


def run_worker(config_path: str | Path, *, output: TextIO | None = None,
               deadline_epoch: float | None = None) -> int:
    sink: TextIO = sys.stdout if output is None else output
    config: RuntimeInstanceConfig | None = None
    instance = None
    started_at = utc_now()
    preflight_gap = None
    try:
        config = load_config(config_path)
        deadline = _pass_deadline(config, deadline_epoch)
        preflight_gap = _vector_preflight_gap(config)
        instance = build_runtime_instance(config)
        instance.memory_epoch()  # Validate the bound database before writing metadata.
        lock_path = _metadata_path(config, "runtime-worker.lock")
        try:
            with advisory_file_lock(lock_path, timeout_seconds=max(0, deadline - time.monotonic())):
                if deadline_epoch is not None and time.monotonic() >= deadline:
                    # No window left to start in; nothing is reserved yet.
                    return _owner_timeout(sink, config, started_at=started_at)
                payload = _drain_once(config, instance, deadline)
                return _report(sink, config, payload, started_at=started_at, exit_code=0)
        except TimeoutError:
            payload = _minimal_payload(config, "busy", "worker_wait_timeout")
            return _report(sink, config, payload, started_at=started_at, exit_code=75)
    except Exception as exc:
        if (deadline_epoch is not None and preflight_gap is None
                and isinstance(exc, ContractError) and exc.code == "DEADLINE_EXCEEDED"):
            # The window ran out inside the drain, as it does for a pass that
            # starts with milliseconds left.  Like a kill, it keeps its page.
            return _owner_timeout(sink, config, started_at=started_at)
        if _writer_busy(exc):
            # Nothing failed: another writer held the truth database.  Busy,
            # like a held worker lock, so the supervisor tries again after a
            # pause instead of stopping until the next autostart wake.
            payload = _minimal_payload(config, "busy", "worker_writer_busy")
            return _report(sink, config, payload, started_at=started_at, exit_code=75)
        # Error classes/codes are useful to a supervisor while details may
        # contain paths or model data.  Keep the protocol bounded and safe.
        payload = _minimal_payload(config, "degraded", preflight_gap or _exception_gap(exc))
        return _report(sink, config, payload, started_at=started_at, exit_code=1, best_effort=True)
    finally:
        if instance is not None:
            instance.close()


def _default_retry_operation(config: RuntimeInstanceConfig, work_ids: tuple[int, ...], epoch: int | None) -> str:
    material = json.dumps(
        [config.binding.installation_id, config.owner_id, tuple(sorted(work_ids)), epoch],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "operator-retry-" + hashlib.sha256(material).hexdigest()[:32]


def _retry_payload(config: RuntimeInstanceConfig, retry_results: tuple[Any, ...], receipt: Any, gaps: list[str]) -> dict[str, Any]:
    payload = _receipt_payload(config, receipt, gaps)
    payload["operator_retry"] = [
        {
            "work_id": int(item.work_id),
            "disposition": str(item.disposition),
            "state": str(item.state),
            "reason": item.reason,
            "operation_id": item.operation_id,
        }
        for item in retry_results
    ]
    payload["operator_retry_limit"] = 8
    return payload


def run_retry_failed(
    config_path: str | Path,
    work_ids: tuple[int, ...],
    *,
    operation_id: str | None = None,
    expected_memory_epoch: int | None = None,
    output: TextIO | None = None,
) -> int:
    """Explicitly requeue named failed work, then perform one bounded drain."""
    sink: TextIO = sys.stdout if output is None else output
    config: RuntimeInstanceConfig | None = None
    instance = None
    try:
        config = load_config(config_path)
        if (not work_ids or len(work_ids) > 8 or len(set(work_ids)) != len(work_ids)
                or any(type(work_id) is not int or work_id < 1 for work_id in work_ids)):
            raise ValueError("retry_work_ids")
        operation_id = operation_id or _default_retry_operation(config, work_ids, expected_memory_epoch)
        instance = build_runtime_instance(config)
        lock_path = config.binding.data_directory / "runtime-worker.lock"
        with advisory_file_lock(lock_path, timeout_seconds=0):
            # Only this explicit operator command may mint the maintenance
            # origin.  Normal worker/query contexts remain request-derived.
            maintenance_context = TrustedContext(
                binding=config.binding,
                session_id=config.session_id,
                allowed_scope_ids=config.allowed_scope_ids,
                actor_origin="host_generated",
                project_id=config.project_id,
                branch_id=config.branch_id,
            )
            retry_results = instance.retry_failed(
                work_ids,
                operation_id=operation_id,
                expected_memory_epoch=expected_memory_epoch,
                operator_context=maintenance_context,
            )
            # Reuse the ordinary worker's lease/fence path.  No retry command
            # can execute a work item outside the existing bounded drain.
            receipt = instance.drain()
        gaps = list(getattr(instance.auxiliary, "capability_gaps", ()) or ())
        gaps.extend(provider_refusals(getattr(instance.auxiliary, "ledger_path", None)))
        gaps.extend(pre_request_refusals(instance.auxiliary))
        _write(sink, _retry_payload(config, retry_results, receipt, gaps))
        return 0
    except TimeoutError:
        _write(sink, {"status": "busy", "processed": 0, "items": [], "operator_retry": [], "capability_gaps": ["worker_already_running"]})
        return 0
    except Exception as exc:
        payload = _minimal_payload(config, "degraded", _exception_gap(exc))
        payload["operator_retry"] = []
        _write(sink, payload)
        return 1
    finally:
        if instance is not None:
            instance.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scope_recall.runtime.worker_entry")
    parser.add_argument("--config", required=True)
    parser.add_argument("--retry-failed", nargs="+", type=int, metavar="WORK_ID")
    parser.add_argument("--retry-operation-id")
    parser.add_argument("--memory-epoch", type=int)
    args = parser.parse_args(argv)
    if args.retry_failed:
        return run_retry_failed(
            args.config,
            tuple(args.retry_failed),
            operation_id=args.retry_operation_id,
            expected_memory_epoch=args.memory_epoch,
        )
    return run_worker(args.config)


if __name__ == "__main__":
    raise SystemExit(main())
