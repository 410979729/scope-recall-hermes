"""Finite, exact-audience supervision of the existing durable worker.

This is scheduling metadata, not another memory or work queue. It never calls
a model, changes work state, widens authority, or renews a model budget.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import time

from ..core.storage import SQLiteStorage
from ..core.work_storage import AUTO_RECOVERABLE_ERRORS, AUTO_RECOVERABLE_WORK_TYPES
from ..core.file_lock import advisory_file_lock
from .worker_entry import DAILY_COUNTER_MAX, _atomic_metadata, _metadata_path, _read_metadata, load_config


#: Seconds a supervisor waits after a busy pass, when the worker lock or the
#: truth writer was held, before it tries again.  At the plain interval a long
#: maintenance write was met with a pass every few seconds; failing instead
#: stopped the supervisor until the next autostart wake.
BUSY_BACKOFF_SECONDS = 30.0
#: Hard worker failures in a row before a supervisor stops accepting wakes.
#: One is not a broken worker: a pass can raise on one item, lose a lease to a
#: maintenance command, or meet a bound nobody had met before.  Standing down on
#: the first one stopped the processing loop until the next five-minute wake and
#: said so nowhere -- observed on a live instance at ``drains=217`` with 180
#: items still queued.  Three in a row, each after a backoff, is a worker that
#: is not going to work, and then standing down is right.
MAX_CONSECUTIVE_WORKER_FAILURES = 3


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


def _capable_work_types(config) -> set[str]:
    aux = config.auxiliary
    capable = {"purge", "rebuild_projection"}
    if aux is not None and aux.external_consolidation and aux.consolidation is not None:
        capable.update(("consolidate", "evaluate_candidate"))
    if aux is not None and aux.external_embedding and aux.embedding is not None and config.vector is not None:
        capable.add("embed")
    return capable


def _daily_items_used(config, now: datetime) -> int:
    day = _read_metadata(_metadata_path(config, "runtime-worker-day.json"))
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
    error_params = tuple(value for error in errors for value in (error, f'%|{error}'))
    exhausted = " AND ".join("last_error_code NOT LIKE ?" for _ in range(config.max_auto_recoveries, 5))
    exhausted_params = tuple(f'%auto_retry:{i}|%' for i in range(config.max_auto_recoveries, 5))
    return conn.execute(f"""SELECT work_type,min(available_at) AS due FROM work_items
        WHERE {base} AND state='failed' AND ({error_filter}) AND {exhausted}
        GROUP BY work_type""", (*params, *error_params, *exhausted_params)).fetchall()


def next_wake(config, *, now: datetime | None = None, unavailable_until=None) -> WakePlan:
    """Read only generic work fields under the same partition as claim_next.

    Candidate timing is advisory. Core rechecks deletion, scope, epoch and
    leases before every real mutation; this function grants no permission.
    """
    now = now or datetime.now(timezone.utc)
    unavailable_until = dict(unavailable_until or {})
    # A provider on hold keeps its work types asleep until the hold ends, so a
    # refusing provider is asked once per hold instead of once per wake.
    from .model_budget import provider_holds
    for work_type, (_model, until) in provider_holds(config.auxiliary, now=now.timestamp()).items():
        held = datetime.fromtimestamp(until, timezone.utc)
        if work_type not in unavailable_until or unavailable_until[work_type] < held:
            unavailable_until[work_type] = held
    capable = _capable_work_types(config)
    spent = _daily_budget_spent(config, _daily_items_used(config, now))
    next_day = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

    def cooled(due, reason, cooldown):
        """A capability cooldown wins when it ends later than the work is due."""
        if cooldown is not None and cooldown > due:
            return cooldown, 'capability_cooldown'
        return due, reason

    def budgeted(due, reason):
        """A spent daily budget pushes the wake to the next UTC day."""
        return (max(due, next_day), 'daily_queue_budget') if spent else (due, reason)

    def scheduled(work_type, due, reason):
        """Queue items: cooldown first, then the daily budget; purge is exempt from it."""
        due, reason = cooled(due, reason, unavailable_until.get(work_type))
        return budgeted(due, reason) if work_type != 'purge' else (due, reason)

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
            if 'evaluate_candidate' in capable:
                cooldown = unavailable_until.get('evaluate_candidate', unavailable_until.get('consolidate'))
                candidates.append(cooled(*budgeted(now, 'candidate_evidence_remainder'), cooldown))
            else:
                blocked += source_pages
        inbox = conn.execute(f"""SELECT count(*) FROM capture_inbox WHERE {base}
            AND (last_error_code IS NULL OR last_error_code IN ('STORAGE_UNAVAILABLE','DEADLINE_EXCEEDED'))""", params).fetchone()[0]
        if inbox:
            pending += inbox
            candidates.append((now, 'durable_capture_ingress'))
        rows = conn.execute(f"""SELECT work_type,state,count(*) AS n,
                    min(CASE WHEN state='leased' THEN lease_until ELSE available_at END) AS due
                FROM work_items WHERE {base} AND state IN ('pending','leased','failed')
                GROUP BY work_type,state""", params).fetchall()
        for row in rows:
            if row['state'] == 'failed':
                failed += row['n']
                continue
            pending += row['n']
            if row['work_type'] not in capable or row['due'] is None:
                blocked += row['n']
                continue
            due = _utc(row['due'])
            if row['state'] == 'leased':
                due += timedelta(milliseconds=1)  # release_stale uses strict <.
            reason = 'lease_expiry' if row['state'] == 'leased' else 'work_available'
            candidates.append(scheduled(row['work_type'], due, reason))
        # Schema 1106 gives each legacy offset-zero failure one inspection.
        # Core marks BOTH repaired and non-repairable candidates, so even a
        # page with recovered=0 makes bounded progress and cannot hide the next
        # page. Never revive a checked failure or a failed later chunk here.
        if 'consolidate' in capable:
            legacy = conn.execute(f"""SELECT 1 FROM work_items WHERE {base}
                AND work_type='consolidate' AND state='failed'
                AND last_error_code='INPUT_INVALID' AND consolidation_offset=0 LIMIT 1""", params).fetchone()
            if legacy is not None:
                candidates.append(cooled(*budgeted(now, 'legacy_source_repair'), unavailable_until.get('consolidate')))
        if config.max_auto_recoveries:
            for row in _auto_recovery_rows(conn, base, params, config):
                if row['work_type'] not in capable or row['work_type'] not in AUTO_RECOVERABLE_WORK_TYPES:
                    continue
                due = _utc(row['due']) + timedelta(seconds=config.auto_retry_cooldown_seconds)
                candidates.append(scheduled(row['work_type'], due, 'failure_cooldown'))
    if not candidates:
        reason = 'capability_unavailable' if blocked else ('failed_terminal' if failed else 'idle')
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
            raise ValueError('supervisor_data_missing')
        material = [config.binding.installation_id, config.project_id, config.branch_id, sorted(config.allowed_scope_ids)]
        key = hashlib.sha256(json.dumps(material, separators=(',', ':')).encode()).hexdigest()[:24]
        self.path = _metadata_path(config, f'runtime-supervisor-{key}.json')
        self.control_lock = _metadata_path(config, f'runtime-supervisor-{key}.control.lock')
        self.owner_lock = _metadata_path(config, f'runtime-supervisor-{key}.owner.lock')

    def read(self):
        value = _read_metadata(self.path)
        if value.get('installation_id') not in (None, self.config.binding.installation_id):
            raise ValueError('supervisor_binding_mismatch')
        revision = value.get('wake_revision', 0)
        if type(revision) is not int or not 0 <= revision < 2**53:
            raise ValueError('supervisor_revision')
        return value

    def request(self):
        with advisory_file_lock(self.control_lock, timeout_seconds=1):
            value = self.read()
            value.update(installation_id=self.config.binding.installation_id,
                         wake_revision=value.get('wake_revision', 0) + 1)
            _atomic_metadata(self.path, value)

    def update(self, **fields):
        with advisory_file_lock(self.control_lock, timeout_seconds=1):
            value = self.read()
            value.update(fields)
            _atomic_metadata(self.path, value)
            return value

    def close_if_unchanged(self, revision, **fields):
        with advisory_file_lock(self.control_lock, timeout_seconds=1):
            value = self.read()
            if value.get('wake_revision', 0) != revision:
                return False
            value.update(accepting=False, **fields)
            _atomic_metadata(self.path, value)
            return True


def _acquire_ownership(control: SupervisorControl):
    """Take the owner lock, or return ``None`` when a live owner owes this wake a read."""
    owner = advisory_file_lock(control.owner_lock, timeout_seconds=0)
    try:
        owner.__enter__()
        return owner
    except TimeoutError:
        pass
    with advisory_file_lock(control.control_lock, timeout_seconds=1):
        if control.read().get('accepting', False):
            return None  # The owner now owes this generation a read before exit.
    owner = advisory_file_lock(control.owner_lock, timeout_seconds=2)
    try:
        owner.__enter__()  # Closing owner hands off; failure stays explicit.
        return owner
    except TimeoutError:
        with advisory_file_lock(control.control_lock, timeout_seconds=1):
            if control.read().get('accepting', False):
                return None
        raise


def supervise(config_path: Path, drain_once, *, delay_seconds=0.0, clock=time.monotonic,
              sleep=time.sleep, utc_now=lambda: datetime.now(timezone.utc), planner=next_wake) -> int:
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
    try:
        control.update(accepting=True, state='running', reason='initial_wake', worker_pid=os.getpid(),
                       started_at=_stamp(utc_now()), deadline_at=_stamp(wall_deadline), drains=0,
                       next_wake_at=None, finished_at=None, exit_code=None, last_exit_code=None)
        if delay_seconds:
            sleep(min(delay_seconds, max(0, deadline - clock())))
        plan = WakePlan(_stamp(utc_now()), 'initial_wake')
        while True:
            from .resume_entry import read_control
            background_control = read_control(config)
            if background_control is not None and not background_control["enabled"]:
                control.update(accepting=False, state='paused', reason='operator_pause', drains=count,
                               finished_at=_stamp(utc_now()))
                return 0
            if clock() >= deadline or count >= config.supervisor_max_drains:
                control.update(accepting=False, state='suspended', reason='supervisor_limit',
                               drains=count, next_wake_at=plan.due_at, finished_at=_stamp(utc_now()))
                return last_code
            if load_config(config_path) != config:
                raise ValueError('supervisor_config_changed')
            revision = control.read().get('wake_revision', 0)
            if count:
                plan = planner(config, now=utc_now(), unavailable_until=unavailable_until)
                # A successful batch may have freed admission capacity while
                # no work is queued yet. One extra drain lets Core refill it.
                if plan.due_at is None and last_drain and last_drain.get('progress', 0):
                    plan = WakePlan(_stamp(utc_now()), 'progress_followup', plan.pending, plan.blocked, plan.failed)
            if plan.due_at is None:
                if control.close_if_unchanged(revision, state='blocked' if plan.blocked or plan.failed else 'idle',
                        reason=plan.reason, drains=count, next_wake_at=None, pending_work=plan.pending,
                        failed_work=plan.failed, blocked_work=plan.blocked, finished_at=_stamp(utc_now())):
                    return last_code
                continue
            delay = max(0, (_utc(plan.due_at) - utc_now()).total_seconds())
            if last_drain is not None:
                delay = max(delay, config.worker_min_interval_seconds - (clock() - last_drain['monotonic']))
            if busy_until is not None:
                delay = max(delay, busy_until - clock())
            if delay > 0:
                control.update(state='waiting', reason=plan.reason, next_wake_at=plan.due_at, drains=count,
                               pending_work=plan.pending, failed_work=plan.failed, blocked_work=plan.blocked)
                sleep(min(delay, 60.0, max(0, deadline - clock())))
                continue  # Read fresh work, budget and wake generation after sleep.
            control.update(state='running', reason=plan.reason, next_wake_at=None, drains=count)
            code, payload = drain_once(max(.001, min(config.drain_seconds, deadline - clock())))
            last_code = code
            count += 1
            busy_until = clock() + BUSY_BACKOFF_SECONDS if code == 75 else None
            last_drain = {'monotonic': clock(),
                          'progress': int(payload.get('completed', 0)) + int(payload.get('recovered', 0))}
            unavailable = set(payload.get('unavailable_work_types', ())) & {'embed', 'consolidate', 'evaluate_candidate'}
            unavailable_until = {kind: utc_now() + timedelta(seconds=min(config.auto_retry_cooldown_seconds, 300))
                                 for kind in unavailable}
            if code in (0, 75, 124):
                failures = 0
            else:
                failures += 1
                if failures >= MAX_CONSECUTIVE_WORKER_FAILURES:
                    control.update(accepting=False, state='failed', reason='worker_failed', exit_code=code,
                                   drains=count, worker_failures=failures, finished_at=_stamp(utc_now()))
                    return code
                # Not a broken worker yet: wait as long as a busy pass waits and
                # try again, leaving the failure in the control file so the
                # doctor reports a loop that is limping rather than a silent one.
                busy_until = clock() + BUSY_BACKOFF_SECONDS
                control.update(state='degraded', reason='worker_failed', last_exit_code=code,
                               drains=count, worker_failures=failures)
                continue
            # Busy/timeout is bounded by the same supervisor limit. Work and
            # cost reservations remain authoritative in their own ledgers.
            control.update(drains=count, last_exit_code=code, worker_failures=0)
    except BaseException:
        control.update(accepting=False, state='failed', reason='supervisor_failed', drains=count,
                       finished_at=_stamp(utc_now()))
        raise
    finally:
        owner.__exit__(None, None, None)


__all__ = ['WakePlan', 'next_wake', 'SupervisorControl', 'supervise']
