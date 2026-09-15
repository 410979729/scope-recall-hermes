"""Bounded, explicit entry point for one durable work-item drain.

The entry point deliberately accepts only a trusted configuration file.  It
does not initialize a database, infer identity from a request, or keep a
daemon alive after the bounded drain finishes.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
import hashlib
import json
import os
import stat
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any, TextIO

from ..file_lock import advisory_file_lock
from ..lance_process_store import NativeVectorPathError
from ..contracts import TrustedContext
from .instance import RuntimeInstanceConfig, build_runtime_instance
from .model_budget import pre_request_refusals, provider_refusals


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


def _json_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _json_value(item) for key, item in asdict(value).items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value



def _is_actionable(error_code: object) -> bool:
    """Whether this failure is something an operator could still clear.

    Shares ``core/failure_retry``'s classification rather than restating it, so
    the worker's own status and the doctor's cannot drift into disagreeing
    about which failures are by design.
    """
    if not error_code:
        return False
    from ..core.failure_retry import retry_class

    return retry_class(error_code) != "terminal"


def _receipt_payload(config: RuntimeInstanceConfig, receipt: Any, capability_gaps: list[str]) -> dict[str, Any]:
    items = []
    for item in getattr(receipt, "items", ()):
        items.append(
            {
                "work_id": int(item.work_id),
                "work_type": str(item.work_type),
                "disposition": str(item.disposition),
                "state": str(item.state),
                "error_code": item.error_code,
                "error_detail": getattr(item, "error_detail", None),
            }
        )
    idle = bool(getattr(receipt, "idle", False))
    # A run that met only by-design terminal outcomes did its job. Counting
    # them as degraded made the worker report degraded whenever it touched a
    # candidate whose payload the model cannot derive -- while the doctor,
    # using the same classification, correctly called the instance "attention".
    # Two components disagreeing about the same fact is how an operator learns
    # to ignore the noisier one.
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _metadata_path(config: RuntimeInstanceConfig, name: str) -> Path:
    root = config.binding.data_directory
    for path in (root, *root.parents):
        if path.is_symlink() or (path.exists() and getattr(path.stat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError("worker_metadata_reparse_path")
    target = root / name
    if target.is_symlink() or (target.exists() and (not target.is_file() or getattr(target.stat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)):
        raise ValueError("worker_metadata_not_regular")
    return target


def _read_metadata(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    if path.stat().st_size > 65536:
        raise ValueError("worker_metadata_oversized")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("worker_metadata_invalid")
    return value


def _atomic_metadata(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(payload.encode("utf-8")) > 65536:
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
            "pending_work", "failed_work", "oldest_pending_at") if key in payload}
    safe["capability_gaps"] = [str(value)[:120] for value in payload.get("capability_gaps", ())][:16]
    safe["unavailable_work_types"] = [str(value)[:32] for value in payload.get("unavailable_work_types", ())][:4]
    for key in ("ingress_replayed", "ingress_cancelled", "source_only"):
        if type(payload.get(key)) is int:
            safe[key] = payload[key]
    safe["items"] = [{key: item[key] for key in ("work_id", "work_type", "disposition", "state", "error_code", "error_detail") if key in item}
                     for item in payload.get("items", ())[:32]]
    finished = _now()
    safe.update(installation_id=config.binding.installation_id, started_at=started_at, finished_at=finished,
                exit_code=exit_code, worker_pid=worker_pid if worker_pid is not None else os.getpid(),
                last_success_at=finished if int(payload.get("completed", 0)) > 0 else previous.get("last_success_at"))
    _atomic_metadata(path, safe)


def _reserve_daily_work(config: RuntimeInstanceConfig) -> tuple[Path, dict[str, Any], int]:
    # This queue-processing cap complements, and never resets or expands, the
    # existing auxiliary model ledger's call/token/currency budget. Reserve
    # before drain so a crashed worker cannot get free repeated attempts.
    path = _metadata_path(config, "runtime-worker-day.json")
    prior = _read_metadata(path)
    if prior.get("installation_id") not in (None, config.binding.installation_id):
        raise ValueError("worker_budget_binding_mismatch")
    day = _now()[:10]
    used = prior.get("used", 0) if prior.get("day") == day else 0
    if type(used) is not int or not 0 <= used <= 100_000_000:
        raise ValueError("worker_budget_invalid")
    # 0 means uncapped: take a full page every pass and let the auxiliary ledger
    # — the only layer that knows what a request actually costs — be the limit.
    # The counter still accumulates, so the day's volume stays observable.
    count = config.max_items if config.daily_work_limit == 0 else \
        min(config.max_items, max(0, config.daily_work_limit - used))
    state = dict(installation_id=config.binding.installation_id, day=day, used=used + count)
    _atomic_metadata(path, state)
    return path, state, count




#: Items a drain may attempt while a provider is refusing everything.
#:
#: There is deliberately no instance-wide page cut here, and the reason is worth
#: keeping: rc-era code cut the whole drain to one item whenever
#: ``provider_refusals`` was non-empty, and measurement showed that was wrong
#: three times over.
#:
#: It removed nothing.  ``core/worker.py`` already stands a work type down for
#: the rest of the pass the moment one of its items sees a rate-limited code
#: (``_RATE_LIMITED_ERRORS``), so a refusing provider was already being asked
#: about once per work type per pass.  Measured on the live ledger during a real
#: outage: the refused model appears in 65 passes at a median of two calls each
#: -- one for ``evaluate_candidate``, one for ``consolidate`` -- while the
#: healthy embedding model ran up to 39 times in a single pass.
#:
#: It throttled the wrong work.  Cutting the page to one would have taken that
#: healthy model from 39 to 1, because the cut looked only at whether *any*
#: model was refusing, never at which one, nor at whether the work being drained
#: routes to it.
#:
#: And it braked long after the wall came down.  ``provider_refusals`` reads a
#: one-hour lookback window, so it keeps reporting for an hour after the last
#: refusal -- including a model that has since been replaced and is no longer
#: routed to at all.  In the hour after one such switch this instance cleared
#: 168 pending items to 15; a page of one would have prevented exactly that
#: recovery.
#:
#: The refusal is still reported, which was always the part that mattered.  What
#: a backward-looking, instance-wide, model-blind statement must not do is steer
#: a forward-looking, per-item decision.


def _failure_label(exc: BaseException) -> str:
    """The exception class, plus the one code that says *which* failure it was.

    ``worker_error:OperationalError`` names a family, not a fault: "database is
    locked" and "no such column" arrive as the same string, and the first is
    contention worth fixing while the second is a query bug.  A live instance
    reported this gap for days with no way to tell them apart.  SQLite's own
    symbolic error name is a bounded enum -- no paths, no model data, no
    message -- so it is safe where the message is not.
    """
    name = type(exc).__name__
    code = getattr(exc, "sqlite_errorname", None)
    if type(code) is str and code.isascii() and code.replace("_", "").isalnum():
        return f"{name}:{code}"
    return name

def _write(output: TextIO, payload: dict[str, Any]) -> None:
    # One compact line is the process protocol.  Never include source content,
    # model output, paths outside the explicit installation identity, or a
    # traceback in worker stdout.
    output.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    output.flush()


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


def run_worker(config_path: str | Path, *, output: TextIO | None = None) -> int:
    sink: TextIO = sys.stdout if output is None else output
    config: RuntimeInstanceConfig | None = None
    instance = None
    started_at = _now()
    preflight_gap = None
    try:
        config = load_config(config_path)
        deadline = time.monotonic() + config.drain_seconds
        if config.vector is not None and config.vector.backend == "lancedb":
            from ..lance_process_store import ProcessLanceVectorStore
            vector = config.vector
            check = ProcessLanceVectorStore(vector.storage_dir, table_name=vector.table_name,
                                            dimensions=vector.dimensions, metric=vector.metric)
            error = check.native_path_error()
            if error:
                preflight_gap = NativeVectorPathError.code
        instance = build_runtime_instance(config)
        instance.status()  # Validate the bound database before writing metadata.
        lock_path = _metadata_path(config, "runtime-worker.lock")
        try:
            with advisory_file_lock(lock_path, timeout_seconds=max(0, deadline - time.monotonic())):
                budget_path, budget_state, reserved = _reserve_daily_work(config)
                # Read before draining so the status file describes the pass it
                # is reporting on, not the one after it.
                refusals = provider_refusals(getattr(instance.auxiliary, "ledger_path", None))
                receipt = instance.drain(max_items=reserved or config.max_items,
                                         purge_only=reserved == 0,
                                         remaining_seconds=max(.001, deadline - time.monotonic()))
                # Purge never spends the optional enrichment budget.
                used = sum(item.work_type != "purge" for item in receipt.items)
                budget_state["used"] -= max(0, reserved - used)
                _atomic_metadata(budget_path, budget_state)
                gaps: list[str] = list(getattr(instance.auxiliary, "capability_gaps", ()) or ())
                # The host agent watching this instance polls the status file, not the
                # doctor; a provider refusing every call has to be visible in both or
                # the watcher learns nothing.
                gaps.extend(refusals)
                background_gaps = tuple(getattr(instance, "background_gaps", ()) or ())
                gaps.extend(background_gaps)
                if reserved == 0:
                    gaps.append("daily_queue_budget")
                payload = _receipt_payload(config, receipt, gaps)
                ingress = getattr(instance, "ingress_receipts", ())
                payload["ingress_replayed"] = sum(r.durability == "persisted" for r in ingress)
                payload["ingress_cancelled"] = sum(r.disposition == "cancelled" for r in ingress)
                payload["source_only"] = sum(item.disposition == "source_only" for item in receipt.items)
                payload["daily_queue_used"] = budget_state["used"]
                queue = instance.status()
                payload.update(pending_work=queue.pending_work, failed_work=queue.failed_work,
                               oldest_pending_at=queue.oldest_pending_at)
                if queue.failed_work or background_gaps or payload["source_only"]:
                    payload["status"] = "degraded"
                elif receipt.idle and queue.pending_work:
                    payload["status"] = "waiting"
                persist_worker_status(config, payload, started_at=started_at, exit_code=0)
        except TimeoutError:
            payload = {
                    "status": "busy",
                    "owner_id": config.owner_id,
                    "installation_id": config.binding.installation_id,
                    "processed": 0,
                    "items": [],
                    "capability_gaps": ["worker_wait_timeout"],
                }
            persist_worker_status(config, payload, started_at=started_at, exit_code=75)
            _write(sink, payload)
            return 75
        _write(sink, payload)
        return 0
    except Exception as exc:
        # Error classes/codes are useful to a supervisor while details may
        # contain paths or model data.  Keep the protocol bounded and safe.
        owner = config.owner_id if config is not None else None
        installation = config.binding.installation_id if config is not None else None
        gap = preflight_gap or (NativeVectorPathError.code if isinstance(exc, NativeVectorPathError)
                                else f"worker_error:{_failure_label(exc)}")
        payload = {
                "status": "degraded",
                "owner_id": owner,
                "installation_id": installation,
                "processed": 0,
                "items": [],
                "capability_gaps": [gap],
            }
        if config is not None:
            try:
                persist_worker_status(config, payload, started_at=started_at, exit_code=1)
            except (OSError, ValueError):
                pass
        _write(sink, payload)
        return 1
    finally:
        if instance is not None:
            instance.close()


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
        if not work_ids or len(work_ids) > 8 or len(set(work_ids)) != len(work_ids):
            raise ValueError("retry_work_ids")
        if any(type(work_id) is not int or work_id < 1 for work_id in work_ids):
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
        gaps: list[str] = list(getattr(instance.auxiliary, "capability_gaps", ()) or ())
        gaps.extend(provider_refusals(getattr(instance.auxiliary, "ledger_path", None)))
        gaps.extend(pre_request_refusals(instance.auxiliary))
        _write(sink, _retry_payload(config, retry_results, receipt, gaps))
        return 0
    except TimeoutError:
        _write(sink, {"status": "busy", "processed": 0, "items": [], "operator_retry": [], "capability_gaps": ["worker_already_running"]})
        return 0
    except Exception as exc:
        owner = config.owner_id if config is not None else None
        installation = config.binding.installation_id if config is not None else None
        gap = (NativeVectorPathError.code if isinstance(exc, NativeVectorPathError)
               else f"worker_error:{_failure_label(exc)}")
        _write(sink, {"status": "degraded", "owner_id": owner, "installation_id": installation, "processed": 0, "items": [], "operator_retry": [], "capability_gaps": [gap]})
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
