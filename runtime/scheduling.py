"""Finite, exact-audience supervision of the existing durable worker.

This is scheduling metadata, not another memory or work queue. It never calls
a model, changes work state, widens authority, or renews a model budget.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..core.failure_retry import AUTO_RECOVERABLE_ERRORS
from ..core.file_lock import advisory_file_lock
from ..core.inbox_rules import REPLAY_CANDIDATES, RETRIED, deferred_until, replayable
from ..core.storage import SQLiteStorage
from ..core.work_storage import AUTO_RECOVERABLE_WORK_TYPES
from .model_budget import provider_holds
from .worker_entry import (
    DAILY_COUNTER_MAX,
    load_config,
    read_worker_metadata,
    worker_metadata_path,
    write_worker_metadata,
)

#: Seconds a supervisor waits after a busy pass, when the worker lock or the
#: truth writer was held, before it tries again.  At the plain interval a long
#: maintenance write was met with a pass every few seconds; failing instead
#: stopped the supervisor until the next autostart wake.
BUSY_BACKOFF_SECONDS = 30.0
#: Hard worker failures in a row before a supervisor stops accepting wakes.
#: One is not a broken worker: a pass can raise on one item, lose a lease to a
#: maintenance command, or meet a bound nobody had met before.  Standing down on
#: the first one would stop the processing loop until the next five-minute wake
#: and say so nowhere, with the queue left waiting.  Three in a row, each after
#: a backoff, is a worker that
#: is not going to work, and then standing down is right.
MAX_CONSECUTIVE_WORKER_FAILURES = 3
#: Seconds between two reads of the operator pause while a supervisor sleeps its start delay.
PAUSE_POLL_SECONDS = 5.0
#: Seconds before a worker is woken for settled candidates again after a pass that did not sweep them: a provider
#: hold, the evaluation queue full, or a pass kept out, cut short or failed.
SETTLE_RETRY_SECONDS = 900


def _daily_budget_spent(config, used: int) -> bool:
    """Whether the per-day item cap is reached.  A limit of 0 means uncapped,
    and without the zero check every wake on an uncapped instance would be
    pushed to the next day."""
    limit = getattr(config, "daily_work_limit", 0)
    return bool(limit) and used >= limit


def _utc(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("supervisor_timestamp")
    return result.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class WakePlan:
    due_at: str | None
    reason: str
    pending: int = 0
    blocked: int = 0
    failed: int = 0


def capable_work_types(config) -> set[str]:
    aux = config.auxiliary
    capable = {"purge", "rebuild_projection"}
    # A pass uses a route only with its budget ledger (``runtime/auxiliary``).
    ledger = aux is not None and aux.ledger_path is not None
    if ledger and aux.external_consolidation and aux.consolidation is not None:
        capable.update(("consolidate", "evaluate_candidate"))
    if ledger and aux.external_embedding and aux.embedding is not None and config.vector is not None:
        capable.add("embed")
    return capable


def _daily_items_used(config, now: datetime) -> int:
    day = read_worker_metadata(worker_metadata_path(config, "runtime-worker-day.json"))
    if day.get("installation_id") not in (None, config.binding.installation_id):
        raise ValueError("supervisor_budget_binding")
    used = day.get("used", 0) if day.get("day") == _stamp(now)[:10] else 0
    if type(used) is not int or not 0 <= used <= DAILY_COUNTER_MAX:
        raise ValueError("supervisor_budget_invalid")
    return used


def _auto_recovery_rows(conn, base: str, params: tuple, config) -> list:
    """Failed items still inside their automatic recovery allowance."""
    errors = sorted(AUTO_RECOVERABLE_ERRORS)
    error_filter = " OR ".join("(last_error_code=? OR last_error_code LIKE ?)" for _ in errors)
    error_params = tuple(value for error in errors for value in (error, f"%|{error}"))
    exhausted = " AND ".join("last_error_code NOT LIKE ?" for _ in range(config.max_auto_recoveries, 5))
    exhausted_params = tuple(f"%auto_retry:{i}|%" for i in range(config.max_auto_recoveries, 5))
    return conn.execute(
        f"""SELECT work_type,min(available_at) AS due FROM work_items
        WHERE {base} AND state='failed' AND ({error_filter}) AND {exhausted}
        GROUP BY work_type""",
        (*params, *error_params, *exhausted_params),
    ).fetchall()


def _partition(config) -> tuple:
    """The audience a supervisor's control file belongs to (``SupervisorControl``)."""
    return config.project_id, config.branch_id, frozenset(config.allowed_scope_ids)


def _control_stamp(config, field: str) -> datetime | None:
    """A time this partition's supervisor recorded in its control file (``supervise``), or ``None`` when it recorded
    none: ``last_pass_at``, when the last pass whose settle sweep saw every ready candidate began, and
    ``unswept_pass_at``, when a later pass that did not sweep them began."""
    try:
        value = SupervisorControl(config).read().get(field)
        return _utc(value) if isinstance(value, str) else None
    except (OSError, ValueError):
        return None


def next_wake(config, *, now: datetime | None = None, unavailable_until=None) -> WakePlan:
    """Read only generic work fields under the same partition as claim_next.

    Candidate timing is advisory. Core rechecks deletion, scope, epoch and
    leases before every real mutation; this function grants no permission.
    """
    now = now or datetime.now(timezone.utc)
    unavailable_until = dict(unavailable_until or {})
    # A provider on hold keeps its work types asleep until the hold ends, so a
    # refusing provider is asked once per hold instead of once per wake.

    for work_type, (_model, until) in provider_holds(config.auxiliary, now=now.timestamp()).items():
        held = datetime.fromtimestamp(until, timezone.utc)
        if work_type not in unavailable_until or unavailable_until[work_type] < held:
            unavailable_until[work_type] = held
    capable = capable_work_types(config)
    spent = _daily_budget_spent(config, _daily_items_used(config, now))
    next_day = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

    def cooled(due, reason, cooldown):
        """A capability cooldown wins when it ends later than the work is due."""
        if cooldown is not None and cooldown > due:
            return cooldown, "capability_cooldown"
        return due, reason

    def budgeted(due, reason):
        """A spent daily budget pushes the wake to the next UTC day."""
        return (max(due, next_day), "daily_queue_budget") if spent else (due, reason)

    def scheduled(work_type, due, reason):
        """Queue items: cooldown first, then the daily budget; purge is exempt from it."""
        due, reason = cooled(due, reason, unavailable_until.get(work_type))
        return budgeted(due, reason) if work_type != "purge" else (due, reason)

    scopes = sorted(config.allowed_scope_ids)
    marks = ",".join("?" for _ in scopes)
    base = f"scope_id IN ({marks}) AND (project_id IS NULL OR project_id=?) AND (branch_id IS NULL OR branch_id=?)"
    params = (*scopes, config.project_id, config.branch_id)
    candidates = []
    pending = blocked = failed = 0
    storage = SQLiteStorage(config.binding)
    with storage.read(config.context(), remaining_seconds=1.0) as tx:
        conn = tx._check()
        source_pages = tx.candidates.pending_source_pages()
        if source_pages:
            pending += source_pages
            if "evaluate_candidate" in capable:
                cooldown = unavailable_until.get("evaluate_candidate", unavailable_until.get("consolidate"))
                candidates.append(cooled(*budgeted(now, "candidate_evidence_remainder"), cooldown))
            else:
                blocked += source_pages
        if "evaluate_candidate" in capable:
            # A candidate still collecting evidence is in no queue: wake when it becomes ready.  The last pass
            # whose sweep saw every ready candidate leaves only those ready or changed since it began; with none on
            # record, every ready candidate counts.  A later pass that could not sweep holds the wake for a while.
            swept = _control_stamp(config, "last_pass_at")
            ready = tx.candidates.next_settle_at(after=_stamp(swept) if swept else None, now=_stamp(now))
            if ready is not None:
                due = max(_utc(ready), now)
                unswept = _control_stamp(config, "unswept_pass_at")
                if unswept is not None and (swept is None or unswept > swept):
                    due = max(due, unswept + timedelta(seconds=SETTLE_RETRY_SECONDS))
                cooldown = unavailable_until.get("evaluate_candidate", unavailable_until.get("consolidate"))
                candidates.append(cooled(*budgeted(due, "candidate_settle_window"), cooldown))
        inbox = 0
        # The partition a pass replays (``replay_inbox``): a row of another woke a pass that never took it.  A key
        # collision is the pass's too (``resolve_conflicted_ingress``).
        for (code,) in conn.execute(
            f"""SELECT last_error_code FROM capture_inbox WHERE scope_id IN ({marks})
                    AND project_id IS ? AND branch_id IS ?
                    AND ({REPLAY_CANDIDATES} OR last_error_code='VERSION_CONFLICT')""",
            (*scopes, config.project_id, config.branch_id, *RETRIED),
        ):
            if replayable(code, now):
                inbox += 1
            elif (until := deferred_until(code, now)) is not None:
                # A row put off wakes the worker when its hour is up.
                candidates.append((until, "durable_capture_deferred"))
        if inbox:
            pending += inbox
            candidates.append((now, "durable_capture_ingress"))
        rows = conn.execute(
            f"""SELECT work_type,state,count(*) AS n,
                    min(CASE WHEN state='leased' THEN lease_until ELSE available_at END) AS due
                FROM work_items WHERE {base} AND state IN ('pending','leased','failed')
                GROUP BY work_type,state""",
            params,
        ).fetchall()
        for row in rows:
            if row["state"] == "failed":
                failed += row["n"]
                continue
            pending += row["n"]
            if row["work_type"] not in capable or row["due"] is None:
                blocked += row["n"]
                continue
            due = _utc(row["due"])
            if row["state"] == "leased":
                due += timedelta(milliseconds=1)  # release_stale uses strict <.
            reason = "lease_expiry" if row["state"] == "leased" else "work_available"
            candidates.append(scheduled(row["work_type"], due, reason))
        # Schema 1106 gives each legacy offset-zero failure one inspection.
        # Core marks BOTH repaired and non-repairable candidates, so even a
        # page with recovered=0 makes bounded progress and cannot hide the next
        # page. Never revive a checked failure or a failed later chunk here.
        if "consolidate" in capable:
            legacy = conn.execute(
                f"""SELECT 1 FROM work_items WHERE {base}
                AND work_type='consolidate' AND state='failed'
                AND last_error_code='INPUT_INVALID' AND consolidation_offset=0 LIMIT 1""",
                params,
            ).fetchone()
            if legacy is not None:
                candidates.append(cooled(*budgeted(now, "legacy_source_repair"), unavailable_until.get("consolidate")))
        if config.max_auto_recoveries:
            for row in _auto_recovery_rows(conn, base, params, config):
                if row["work_type"] not in capable or row["work_type"] not in AUTO_RECOVERABLE_WORK_TYPES:
                    continue
                due = _utc(row["due"]) + timedelta(seconds=config.auto_retry_cooldown_seconds)
                candidates.append(scheduled(row["work_type"], due, "failure_cooldown"))
    if not candidates:
        reason = "capability_unavailable" if blocked else ("failed_terminal" if failed else "idle")
        return WakePlan(None, reason, pending, blocked, failed)
    due, reason = min(candidates)
    return WakePlan(_stamp(due), reason, pending, blocked, failed)


class SupervisorControl:
    """A generation handshake closes the idle-exit/new-wakeup race.

    Writers announce a wake before attempting ownership. An accepting owner
    must recheck that generation while closing. Once it marks non-accepting,
    the new caller waits briefly for ownership instead of discarding its wake.
    """

    def __init__(self, config):
        self.config = config
        if not config.binding.data_directory.is_dir():
            raise ValueError("supervisor_data_missing")
        material = [
            config.binding.installation_id,
            config.project_id,
            config.branch_id,
            sorted(config.allowed_scope_ids),
        ]
        key = hashlib.sha256(json.dumps(material, separators=(",", ":")).encode()).hexdigest()[:24]
        self.path = worker_metadata_path(config, f"runtime-supervisor-{key}.json")
        self.control_lock = worker_metadata_path(config, f"runtime-supervisor-{key}.control.lock")
        self.owner_lock = worker_metadata_path(config, f"runtime-supervisor-{key}.owner.lock")

    def read(self):
        value = read_worker_metadata(self.path)
        if value.get("installation_id") not in (None, self.config.binding.installation_id):
            raise ValueError("supervisor_binding_mismatch")
        revision = value.get("wake_revision", 0)
        if type(revision) is not int or not 0 <= revision < 2**53:
            raise ValueError("supervisor_revision")
        return value

    def request(self):
        with advisory_file_lock(self.control_lock, timeout_seconds=1):
            value = self.read()
            value.update(
                installation_id=self.config.binding.installation_id, wake_revision=value.get("wake_revision", 0) + 1
            )
            write_worker_metadata(self.path, value)

    def update(self, **fields):
        with advisory_file_lock(self.control_lock, timeout_seconds=1):
            value = self.read()
            value.update(fields)
            write_worker_metadata(self.path, value)
            return value

    def close_if_unchanged(self, revision, **fields):
        with advisory_file_lock(self.control_lock, timeout_seconds=1):
            value = self.read()
            if value.get("wake_revision", 0) != revision:
                return False
            value.update(accepting=False, **fields)
            write_worker_metadata(self.path, value)
            return True


def control_path(config):
    return config.binding.data_directory / "runtime-autostart.json"


def read_control(config):
    path = control_path(config)
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_size > 65536:
        raise ValueError("autostart_control_invalid")
    control = json.loads(path.read_text(encoding="utf-8"))
    if control.get("installation_id") != config.binding.installation_id or type(control.get("enabled")) is not bool:
        raise ValueError("autostart_binding_invalid")
    expected = "ScopeRecall-" + hashlib.sha256(config.binding.installation_id.encode()).hexdigest()[:20]
    if control.get("task_name") != expected:
        raise ValueError("autostart_task_identity_invalid")
    return control


def _acquire_ownership(control: SupervisorControl):
    """Take the owner lock, or return ``None`` when a live owner owes this wake a read."""
    owner = advisory_file_lock(control.owner_lock, timeout_seconds=0)
    try:
        owner.__enter__()
        return owner
    except TimeoutError:
        pass
    with advisory_file_lock(control.control_lock, timeout_seconds=1):
        if control.read().get("accepting", False):
            return None  # The owner now owes this generation a read before exit.
    owner = advisory_file_lock(control.owner_lock, timeout_seconds=2)
    try:
        owner.__enter__()  # Closing owner hands off; failure stays explicit.
        return owner
    except TimeoutError:
        with advisory_file_lock(control.control_lock, timeout_seconds=1):
            if control.read().get("accepting", False):
                return None
        raise


def supervise(
    config_path: Path,
    drain_once,
    *,
    delay_seconds=0.0,
    clock=time.monotonic,
    sleep=time.sleep,
    utc_now=lambda: datetime.now(timezone.utc),
    planner=next_wake,
) -> int:
    """Run bounded drains sequentially; no recursive supervisor process spawn.

    An OS restart needs a separate startup integration. The finite window and
    drain count are never extended by coalesced requests.
    """
    config = load_config(config_path)
    control = SupervisorControl(config)
    control.request()
    owner = _acquire_ownership(control)
    if owner is None:
        return 0
    deadline = clock() + config.supervisor_seconds
    wall_deadline = utc_now() + timedelta(seconds=config.supervisor_seconds)
    count = 0
    failures = 0
    last_drain = None
    last_code = 0
    unavailable_until = {}
    busy_until = None

    def paused() -> bool:
        background_control = read_control(config)
        if background_control is None or background_control["enabled"]:
            return False
        control.update(
            accepting=False, state="paused", reason="operator_pause", drains=count, finished_at=_stamp(utc_now())
        )
        return True

    try:
        control.update(
            accepting=True,
            state="running",
            reason="initial_wake",
            worker_pid=os.getpid(),
            started_at=_stamp(utc_now()),
            deadline_at=_stamp(wall_deadline),
            drains=0,
            next_wake_at=None,
            finished_at=None,
            exit_code=None,
            last_exit_code=None,
        )
        # The start delay is slept in steps that read the operator pause.  This process runs from
        # the package folder, and a host that is shutting down launches one last detached wake:
        # slept in one piece, that wake held the folder for worker_min_interval_seconds after the
        # operator had paused everything, and the package step of an upgrade refused.
        delay_left = min(delay_seconds, max(0, deadline - clock())) if delay_seconds else 0.0
        while delay_left > 0:
            if paused():
                return 0
            step = min(delay_left, PAUSE_POLL_SECONDS)
            sleep(step)
            delay_left -= step
        plan = WakePlan(_stamp(utc_now()), "initial_wake")
        while True:
            if paused():
                return 0
            if clock() >= deadline or count >= config.supervisor_max_drains:
                control.update(
                    accepting=False,
                    state="suspended",
                    reason="supervisor_limit",
                    drains=count,
                    next_wake_at=plan.due_at,
                    finished_at=_stamp(utc_now()),
                )
                return last_code
            fresh = load_config(config_path)
            if fresh != config:
                if fresh.binding != config.binding or _partition(fresh) != _partition(config):
                    # Another store or another audience: this owner's control files are the old one's.  It closes
                    # cleanly and the next wake starts the new one's own supervisor.
                    control.update(
                        accepting=False,
                        state="suspended",
                        reason="config_changed",
                        drains=count,
                        next_wake_at=_stamp(utc_now()),
                        finished_at=_stamp(utc_now()),
                    )
                    return last_code
                # A setting was edited (the pass size, the interval).  Every pass is its own process
                # and reads the file anyway, so the supervisor takes the same values and carries on.
                # Raising here reported each edit as a failed supervisor and left the store without
                # one until the next scheduled wake, up to five minutes later.  The window and the
                # drain count stay the ones it started with.
                config = fresh
            revision = control.read().get("wake_revision", 0)
            if count:
                plan = planner(config, now=utc_now(), unavailable_until=unavailable_until)
                # A successful batch may have freed admission capacity while
                # no work is queued yet. One extra drain lets Core refill it.
                if plan.due_at is None and last_drain and last_drain.get("progress", 0):
                    plan = WakePlan(_stamp(utc_now()), "progress_followup", plan.pending, plan.blocked, plan.failed)
            if plan.due_at is None:
                if control.close_if_unchanged(
                    revision,
                    state="blocked" if plan.blocked or plan.failed else "idle",
                    reason=plan.reason,
                    drains=count,
                    next_wake_at=None,
                    pending_work=plan.pending,
                    failed_work=plan.failed,
                    blocked_work=plan.blocked,
                    finished_at=_stamp(utc_now()),
                ):
                    return last_code
                continue
            delay = max(0, (_utc(plan.due_at) - utc_now()).total_seconds())
            if last_drain is not None:
                delay = max(delay, config.worker_min_interval_seconds - (clock() - last_drain["monotonic"]))
            if busy_until is not None:
                delay = max(delay, busy_until - clock())
            if delay > 0:
                control.update(
                    state="waiting",
                    reason=plan.reason,
                    next_wake_at=plan.due_at,
                    drains=count,
                    pending_work=plan.pending,
                    failed_work=plan.failed,
                    blocked_work=plan.blocked,
                )
                sleep(min(delay, 60.0, max(0, deadline - clock())))
                continue  # Read fresh work, budget and wake generation after sleep.
            control.update(state="running", reason=plan.reason, next_wake_at=None, drains=count)
            began = _stamp(utc_now())
            code, payload = drain_once(max(0.001, min(config.drain_seconds, deadline - clock())))
            last_code = code
            count += 1
            busy_until = clock() + BUSY_BACKOFF_SECONDS if code == 75 else None
            last_drain = {
                "monotonic": clock(),
                "progress": int(payload.get("completed", 0)) + int(payload.get("recovered", 0)),
            }
            unavailable = set(payload.get("unavailable_work_types", ())) & {
                "embed",
                "consolidate",
                "evaluate_candidate",
            }
            unavailable_until = {
                kind: utc_now() + timedelta(seconds=min(config.auto_retry_cooldown_seconds, 300))
                for kind in unavailable
            }
            if code in (0, 75, 124):
                failures = 0
            else:
                failures += 1
                if failures >= MAX_CONSECUTIVE_WORKER_FAILURES:
                    control.update(
                        accepting=False,
                        state="failed",
                        reason="worker_failed",
                        exit_code=code,
                        drains=count,
                        worker_failures=failures,
                        finished_at=_stamp(utc_now()),
                        unswept_pass_at=began,
                    )
                    return code
                # Not a broken worker yet: wait as long as a busy pass waits and
                # try again, leaving the failure in the control file so the
                # doctor reports a loop that is limping rather than a silent one.
                busy_until = clock() + BUSY_BACKOFF_SECONDS
                control.update(
                    state="degraded",
                    reason="worker_failed",
                    last_exit_code=code,
                    drains=count,
                    worker_failures=failures,
                    unswept_pass_at=began,
                )
                continue
            # Busy/timeout is bounded by the same supervisor limit. Work and
            # cost reservations remain authoritative in their own ledgers.  A pass whose sweep saw every ready
            # candidate is recorded; one that stopped at its page leaves the rest for the next pass; any other holds
            # the settle wake for ``SETTLE_RETRY_SECONDS``.
            if code == 0 and payload.get("settle_swept") is True:
                stamps = {"last_pass_at": began, "unswept_pass_at": None}
            elif payload.get("settle_partial") is True:
                stamps = {"unswept_pass_at": None}
            else:
                stamps = {"unswept_pass_at": began}
            control.update(drains=count, last_exit_code=code, worker_failures=0, **stamps)
    except BaseException:
        control.update(
            accepting=False, state="failed", reason="supervisor_failed", drains=count, finished_at=_stamp(utc_now())
        )
        raise
    finally:
        owner.__exit__(None, None, None)


__all__ = ["WakePlan", "next_wake", "SupervisorControl", "supervise"]
