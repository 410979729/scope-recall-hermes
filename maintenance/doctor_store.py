"""The doctor's store checks.

Schema, journal and backlog, the vector index, the store's footprint, embeddings, candidates, work that waited a
day, model output and the budget ledger; ``doctor.run_doctor`` runs them beside the host checks.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from scope_recall.contracts import TrustedContext
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.failure_retry import NEEDS_REVIEW_COUNT
from scope_recall.core.inbox_rules import given_up, replayable
from scope_recall.core.index_rebuild import IMPORT_EMBED_QUEUE_CEILING
from scope_recall.core.schema import SCHEMA_VERSION, UPGRADE_CHAIN, stale_header_schema
from scope_recall.core.storage import SQLiteStorage
from scope_recall.runtime.model_budget import embedding_calls, provider_holds
from scope_recall.vector.compaction import instance_vector_footprints

from .doctor_report import DoctorReport, record_check


def _seconds_since(stamp: Any) -> float | None:
    """Age of an ISO-8601 timestamp; None when it is missing or unparseable."""
    if not stamp:
        return None
    try:
        seen = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - seen).total_seconds()
    except (ValueError, TypeError):
        return None


def read_journal_mode(db_path: Path) -> str | None:
    with suppress(sqlite3.Error, OSError, ValueError):
        with closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5)) as db:
            return str(db.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    return None


def schema_on_disk(db_path: Path) -> int | None:
    """The store's own schema stamp, read without opening it as a store."""
    with suppress(sqlite3.Error, OSError, ValueError):
        with closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5)) as db:
            return int(db.execute("PRAGMA user_version").fetchone()[0])
    return None


def recorded_schema_under_stale_header(db_path: Path) -> int | None:
    """The schema the store records when its header was overwritten (``core.schema.stale_header_schema``)."""
    with suppress(sqlite3.Error, OSError, ValueError):
        with closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5)) as db:
            return stale_header_schema(db)
    return None


def _embedded_objects(db_path: Path) -> int | None:
    """Finished embed work, read through a separate read-only connection."""
    with suppress(sqlite3.Error, OSError, ValueError):
        with closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5)) as db:
            return int(
                db.execute("SELECT COUNT(*) FROM work_items WHERE work_type='embed' AND state='done'").fetchone()[0]
                or 0
            )
    return None


def _expired_vectors(db_path: Path) -> dict[str, int] | None:
    """Tool-output vectors the retention pass expired, by reason (``runtime/vector_retention.py``)."""
    with suppress(sqlite3.Error, OSError, ValueError):
        with closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5)) as db:
            return {
                str(reason): int(count)
                for reason, count in db.execute(
                    "SELECT reason,COUNT(*) FROM expired_vectors GROUP BY reason ORDER BY reason"
                )
            }
    return None


def check_index(report: DoctorReport, data_directory: Path, *, store_readable: bool, config: Any = None) -> None:
    """Optional vector-index facts. Reported, never acted on."""
    metadata: dict[str, Any] = {"vectors_dir_present": (data_directory / "vectors").is_dir()}
    embedded = _embedded_objects(data_directory / "memory.sqlite3") if store_readable else None
    by_reason = _expired_vectors(data_directory / "memory.sqlite3") if store_readable else None
    expired = None if by_reason is None else sum(by_reason.values())
    if embedded is not None:
        metadata["embedded_objects"] = embedded
    if expired is not None:
        metadata["expired_vectors"] = expired
        metadata["expired_vectors_by_reason"] = by_reason
    vector = getattr(config, "vector", None)
    if vector is not None:
        metadata["tool_output_retention_days"] = vector.tool_output_retention_days
    # Fragment count is what a missed compaction shows up as first, and the one
    # cost an operator can verify with a plain file listing.  Each store also
    # says whether its nearest-neighbour index was built (``index_outcome``).
    try:
        metadata["vector_stores"] = instance_vector_footprints(data_directory)
    except Exception:  # noqa: BLE001 - reporting must not fail the report.
        metadata["vector_stores"] = []
    report.index_metadata = metadata


#: Fraction of any auxiliary ledger cap above which the instance is warned.
#: The ledger's caps are lifetime totals, not a rolling window, so headroom only
#: ever shrinks: once a cap is reached the derived layer stops for good and the
#: only visible symptom is work quietly pausing with ``budget_exhausted``.
#: Reporting the ratio turns "it stopped working one day" into something an
#: operator can see coming.
_LEDGER_PRESSURE_WARN = 0.90


#: Default supervisor wake interval (``RuntimeInstanceConfig.supervisor_seconds``),
#: used when the runtime config cannot be read. A quiet instance legitimately
#: records no progress for one whole wake interval, so the stall window is a
#: multiple of it rather than a constant: a fixed six hours would equal the
#: default interval exactly and flap on a perfectly healthy instance.
_DEFAULT_SUPERVISOR_SECONDS = 21600.0


_STALL_WAKE_MULTIPLE = 2


def _backlog_is_stalled(worker_status: dict[str, Any], *, wake_seconds: float | None) -> bool:
    """Decide whether pending work is actually stuck rather than merely large.

    Stalled means the worker has stopped making progress, so this reads the
    worker's own last success and nothing else. It is deliberately not derived
    from ``oldest_pending_age_seconds``: a migration carries the original
    timestamps, so its freshly enqueued items can be months old on the day they
    are created, and judging by item age reports every healthy migration as
    stalled for as long as it takes to drain.

    A worker that has never recorded a success is judged by its last finished
    run. One that has never run at all is not accused here; registration and
    autostart checks own that case.
    """
    status = worker_status or {}
    window = _STALL_WAKE_MULTIPLE * float(wake_seconds or _DEFAULT_SUPERVISOR_SECONDS)
    age = _seconds_since(str(status.get("last_success_at") or status.get("finished_at") or "").strip())
    return age is not None and age > window


def check_storage(report: DoctorReport, binding, data_directory: Path) -> bool:
    """Copy the store's queue, source and candidate status onto the report.

    False when there is no database or it cannot be read; the report then keeps
    whatever was learned before.
    """
    report.database_present = (data_directory / "memory.sqlite3").is_file()
    if not report.database_present:
        report.capability_gaps.append("database_missing")
        record_check(report, "database", "missing")
        return False
    report.journal_mode = read_journal_mode(data_directory / "memory.sqlite3")
    record_check(report, "database", "ok", f"journal_mode={report.journal_mode}")
    found = schema_on_disk(data_directory / "memory.sqlite3")
    if found in UPGRADE_CHAIN:
        # Reported, never applied here: the doctor is read-only.
        report.schema_version = found
        report.capability_gaps.append("schema_upgrade_pending")
        record_check(
            report,
            "schema",
            "upgrade_pending",
            f"{found} -> {SCHEMA_VERSION}; the next capture, recall or worker pass applies it in one transaction "
            "(on a store above 100 MB, a caller with a minute of budget: the worker pass, apply-install, "
            "upgrade-store or a Hermes session start)",
        )
        return False
    recorded = recorded_schema_under_stale_header(data_directory / "memory.sqlite3")
    if recorded is not None:
        # Every open fails closed on the header, so say why and what repairs it.
        report.schema_version = found
        report.capability_gaps.append("schema_header_stale")
        record_check(
            report,
            "schema",
            "header_stale",
            f"header {found}, store records {recorded}: another process (a 2.0 one, after the migration) "
            "stamped the header; stop it, then run upgrade-store with --backup-dir",
        )
        return False
    context = TrustedContext(binding, "doctor-readonly", binding.scope_ids, "origin_unknown")
    try:
        core = MemoryCore(CoreConfig(binding), storage=SQLiteStorage(binding, upgrade_on_open=False))
        with core.storage.read(context) as transaction:
            status = transaction.status(include_all_projects=True, include_admission=True)
            conn = transaction._check()
            report.capture_inbox = conn.execute("SELECT count(*) FROM capture_inbox").fetchone()[0]
            moment = datetime.now(timezone.utc)
            codes = [code for (code,) in conn.execute("SELECT last_error_code FROM capture_inbox")]
            report.capture_inbox_blocked = sum(not replayable(code, moment) for code in codes)
            report.capture_inbox_given_up = sum(given_up(code) for code in codes)
            report.recent_work_errors = [
                dict(r)
                for r in conn.execute(
                    "SELECT work_id,lease_token,stage,error_code,error_field,recorded_at FROM work_error_details ORDER BY detail_id DESC LIMIT 16"
                )
            ]
            moment = datetime.now(timezone.utc)
            hour_ago = (moment - timedelta(hours=1)).isoformat()
            growth = conn.execute(
                "SELECT sum(persisted_at>=?),sum(persisted_at>=?) FROM source_events",
                ((moment - timedelta(days=1)).isoformat(), (moment - timedelta(days=7)).isoformat()),
            ).fetchone()
            report.recent_output_truncations = conn.execute(
                "SELECT count(*) FROM work_error_details WHERE error_field='model_output_truncated' AND recorded_at>=?",
                (hour_ago,),
            ).fetchone()[0]
            report.extraction_outcomes = dict(
                conn.execute("SELECT disposition,count(*) FROM consolidation_outcomes GROUP BY disposition").fetchall()
            )
            report.needs_review_work = conn.execute(NEEDS_REVIEW_COUNT).fetchone()[0]
            candidates = transaction.candidates.summary(include_all_projects=True)
            # Debouncing raises pending_evaluation on purpose, so split that
            # number: waiting inside the quiet window is health, waiting past it
            # with no sweep having run is not.
            report.candidate_settling = transaction.candidates.settling_summary(
                now=datetime.now(timezone.utc).isoformat()
            )
            report.embedding_respace = transaction.work.respace_run()
            report.embedding_health = transaction.work.embed_queue()
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=UNREACHED_HOURS)).isoformat()
            report.unreached = [
                *transaction.work.due_unreached(before=cutoff),
                *transaction.candidates.settled_unreached(before=cutoff),
            ]
    except Exception as exc:  # noqa: BLE001 - an unreadable store is a finding, not a crash.
        report.capability_gaps.append(f"storage_read:{type(exc).__name__}")
        record_check(report, "storage_status", "unavailable", type(exc).__name__)
        return False

    report.schema_version = status.schema_version
    report.memory_epoch = status.memory_epoch
    report.sources = status.sources
    report.sources_last_24h = int(growth[0] or 0)
    report.sources_last_7d = int(growth[1] or 0)
    report.pending_work = status.pending_work
    report.failed_work = status.failed_work
    # Only meaningful next to a failure count; stays None on a clean queue.
    report.terminal_failed_work = status.terminal_failed_work if status.failed_work else None
    report.leased_work = status.leased_work
    report.oldest_pending_at = status.oldest_pending_at
    report.source_only_sources = status.source_only_sources
    report.deferred_sources = status.deferred_sources
    report.oldest_deferred_at = status.oldest_deferred_at
    report.work_error_counts = dict(status.work_error_counts)
    report.pending_error_counts = dict(status.pending_error_counts)
    if status.oldest_pending_at:
        age = _seconds_since(status.oldest_pending_at)
        if age is None:
            report.capability_gaps.append("work_timestamp_invalid")
        else:
            report.oldest_pending_age_seconds = max(0, age)
    report.candidate_pending_evaluation = candidates.pending_evaluation
    report.candidate_waiting_evidence = candidates.waiting_evidence
    report.candidate_dormant = candidates.dormant
    report.candidate_blocked = candidates.blocked
    report.candidate_resolved = candidates.resolved
    report.candidate_archived_other = candidates.archived_other
    report.candidate_failed = candidates.failed
    report.candidate_budget_paused = candidates.budget_paused
    report.candidate_capability_unavailable = candidates.capability_unavailable
    report.candidate_oldest_waiting_at = candidates.oldest_waiting_at
    return True


def _directory_bytes(path: Path) -> int:
    total = 0
    with suppress(OSError):
        for item in path.rglob("*"):
            with suppress(OSError):
                if item.is_file():
                    total += item.stat().st_size
    return total


def check_footprint(report: DoctorReport, data_directory: Path, config) -> None:
    """Bytes on disk and the week's growth: what an operator needs to see a
    store outgrow its disk before it does.  A configured budget turns the
    comparison into a gap; nothing is deleted for it."""
    store = 0
    for name in ("memory.sqlite3", "memory.sqlite3-journal", "memory.sqlite3-wal", "memory.sqlite3-shm"):
        with suppress(OSError):
            store += (data_directory / name).stat().st_size
    report.store_bytes = store
    report.vector_bytes = _directory_bytes(data_directory / "vectors")
    detail = f"store {store / 1e6:.0f} MB, vectors {report.vector_bytes / 1e6:.0f} MB"
    if report.sources_last_24h is not None:
        detail += f", sources +{report.sources_last_24h} in 24h, +{report.sources_last_7d} in 7d"
    budget = getattr(config, "storage_budget_bytes", 0) if config is not None else 0
    if budget:
        report.storage_budget_bytes = budget
        used = store + report.vector_bytes
        detail += f", budget {budget / 1e6:.0f} MB ({used / budget:.0%} used)"
        if used > budget:
            report.capability_gaps.append("storage_budget_exceeded")
            record_check(report, "storage_footprint", "over_budget", detail)
            return
    record_check(report, "storage_footprint", "ok", detail)


def _finished_derived_work(db_path: Path) -> int:
    """Embeddings and consolidations the worker finished, read through a separate read-only connection."""
    with suppress(sqlite3.Error, OSError, ValueError):
        with closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5)) as db:
            return int(
                db.execute(
                    "SELECT count(*) FROM work_items WHERE state='done' AND work_type IN ('embed','consolidate')"
                ).fetchone()[0]
                or 0
            )
    return 0


def check_runtime_config_present(report: DoctorReport, data_directory: Path) -> None:
    """A runtime config that was there and is gone, named instead of a silent basic mode.

    Without ``runtime-config.json`` every host runs in basic mode -- no worker, no model
    routes -- which is also how an install starts, so absence alone is not a finding.  A
    store the worker has embedded or consolidated for had one: that work only runs from the
    routes a runtime config names.
    """
    if os.path.lexists(data_directory / "runtime-config.json"):
        return
    finished = _finished_derived_work(data_directory / "memory.sqlite3")
    if finished:
        report.capability_gaps.append("runtime_config_missing")
        record_check(
            report,
            "runtime_config",
            "missing",
            f"{finished} finished embeddings and consolidations came from routes a runtime-config.json "
            "named; without it hosts run in basic mode and no worker runs",
        )


#: Hours the oldest waiting embedding may wait before the doctor says so.
EMBEDDING_BACKLOG_HOURS = 24


def check_embedding_health(report: DoctorReport, config) -> None:
    """The embedding queue beside what the provider has been answering.

    Recall goes on answering while embeddings wait, by words alone, and nothing else says so: an installation on a
    free tier can meet HTTP 429 most days and have embeddings waiting for over a week.  A backlog older than
    ``EMBEDDING_BACKLOG_HOURS`` is named, with the provider's hold and refusals when it has them.  Without a vector
    store and an external embedding route nothing embeds, by choice, and the queue only grows: that is no finding."""
    health = report.embedding_health
    auxiliary = getattr(config, "auxiliary", None) if config is not None else None
    if (
        getattr(config, "vector", None) is None
        or auxiliary is None
        or getattr(auxiliary, "external_embedding", False) is not True
        or getattr(auxiliary, "embedding", None) is None
    ):
        return
    hold = provider_holds(auxiliary).get("embed")
    if hold is not None:
        health["held_model"], until = hold
        health["held_until"] = datetime.fromtimestamp(until, timezone.utc).isoformat()
    calls = embedding_calls(auxiliary)
    if calls is not None:
        health["last_day"] = calls
    oldest = health.get("oldest_pending_at")
    age = _seconds_since(oldest) if health.get("pending") and oldest else None
    if age is None or age <= EMBEDDING_BACKLOG_HOURS * 3600:
        return
    report.capability_gaps.append("embedding_backlog_aged")
    detail = (
        f"{health['pending']} embeddings wait, the oldest for {int(age // 3600)} h; recall finds what came in "
        "since then by its words alone"
    )
    if "held_until" in health:
        detail += f"; the provider is held for {health['held_model']} until {health['held_until']}"
    calls = health.get("last_day")
    if calls is not None and calls["calls"]:
        detail += f"; in the last day the provider was asked {calls['calls']} times and answered {calls['answered']}"
        if calls["refused"]:
            detail += f", refusing {', '.join(f'{code} x{count}' for code, count in calls['refused'].items())}"
    elif calls is not None and "held_until" not in health:
        # Asked nothing for a day while embeddings waited: the provider is not what holds them.
        detail += (
            "; nothing asked the provider in the last day, so no worker has reached them: see worker_status, "
            "and on an installation with a worker per project, whether each one runs"
        )
    record_check(report, "embedding_backlog", "aged", detail)


#: Hours due work or a candidate with new evidence may wait for a pass before the doctor says so.
UNREACHED_HOURS = 24


def check_unreached(report: DoctorReport, config) -> None:
    """Work and candidates of any partition, this audience's or another's, that have waited more than a day.

    A partition's queue is drained only by a worker of its own audience, started by a session of that audience or by
    a scheduled wake, and the work-queue figures above cover this binding's audience only.  Work this installation's
    routes cannot do, and work a provider holds (reported by ``embedding_backlog_aged`` and ``model_refused``), is
    left out.  The store records no time a pass looked at an item, so a queue longer than its passes reach in a day
    is named too, and the finding asks for attention rather than degrading the report.  The detail line counts; the
    scope ids, which carry chat and account ids, are only in ``unreached``.
    """
    from ..runtime.scheduling import capable_work_types

    capable = capable_work_types(config) if config is not None else {"purge", "rebuild_projection"}
    if config is not None:
        capable -= set(provider_holds(config.auxiliary, now=datetime.now(timezone.utc).timestamp()))
    partitions: dict[tuple, dict[str, Any]] = {}
    for row in report.unreached:
        if "work_type" in row and row["work_type"] not in capable:
            continue
        if "candidates" in row and "evaluate_candidate" not in capable:
            continue
        key = (row["scope_id"], row["project_id"], row["branch_id"])
        found = partitions.setdefault(
            key,
            {
                "scope_id": key[0],
                "project_id": key[1],
                "branch_id": key[2],
                "work": 0,
                "candidates": 0,
                "oldest": row["oldest"],
            },
        )
        found["work"] += row.get("work", 0)
        found["candidates"] += row.get("candidates", 0)
        found["oldest"] = min(str(found["oldest"] or row["oldest"]), str(row["oldest"] or found["oldest"]))
    report.unreached = sorted(partitions.values(), key=lambda found: str(found["oldest"]))
    if not report.unreached:
        return
    report.capability_gaps.append("due_work_unreached")
    work = sum(found["work"] for found in report.unreached)
    candidates = sum(found["candidates"] for found in report.unreached)
    record_check(
        report,
        "due_work_unreached",
        "present",
        f"{work} work items and {candidates} candidates with new evidence have waited more than "
        f"{UNREACHED_HOURS} h, the oldest since {report.unreached[0]['oldest']}, in "
        f"{len(report.unreached)} partition(s) listed in unreached: no worker of that audience has run, or the "
        "queue is longer than its passes reach (docs/install.md, section 7)",
    )


def check_embedding_respace(report: DoctorReport, config) -> None:
    """A re-embed run's progress, and a run no worker will go on with because the config embeds into another space.

    A worker reopens a page of the run at each drain only in the run's own space (``respace_if_due``); after a
    second change of model the run would wait for good, so it is named here with what to do."""
    run = report.embedding_respace
    if run is None:
        return
    if run["completed"]:
        record_check(
            report, "embedding_respace", "complete", f"{run['reopened']} reopened, last at {run['updated_at']}"
        )
        return
    space = config.embedding_space_id() if config is not None else None
    if space is not None and space != run["embedding_space"]:
        report.capability_gaps.append("embedding_respace_space_mismatch")
        record_check(
            report,
            "embedding_respace",
            "space_mismatch",
            f"the run embeds into {run['embedding_space'][:12]} but runtime-config.json into {space[:12]}; "
            "run respace-embeddings --restart --apply for the new space, or --cancel --apply",
        )
        return
    # A held pass writes nothing, so the run's time alone does not say it waits.
    waiting = report.embedding_health.get("pending")
    record_check(
        report,
        "embedding_respace",
        "running",
        f"{run['reopened']} reopened, next work id {run['next_work_id']}, last page at {run['updated_at']}"
        + (
            f"; {waiting} embeddings wait in the store, and the run goes on while fewer than "
            f"{IMPORT_EMBED_QUEUE_CEILING} do"
            if waiting is not None
            else ""
        ),
    )


def check_schema(report: DoctorReport) -> None:
    if report.schema_version != SCHEMA_VERSION:
        report.capability_gaps.append("schema_version_mismatch")
        record_check(report, "schema", "mismatch", str(report.schema_version))
    else:
        record_check(report, "schema", "ok", str(report.schema_version))


def check_backlog(report: DoctorReport, wake_seconds: float | None) -> None:
    """Queue health: a backlog is only a fault once the worker stops clearing it."""
    if report.pending_work:
        record_check(report, "work_backlog", "present", str(report.pending_work))
        if _backlog_is_stalled(report.worker_status, wake_seconds=wake_seconds):
            report.capability_gaps.append("work_backlog_stalled")
    else:
        record_check(report, "work_backlog", "idle")
    if report.needs_review_work:
        report.capability_gaps.append("work_needs_review")
        record_check(report, "needs_review_work", "present", str(report.needs_review_work))
    if report.failed_work:
        # Terminal failures never clear, so only the recoverable remainder may
        # drive "degraded"; the terminal count is still reported beside it.
        terminal = report.terminal_failed_work or 0
        if report.failed_work > terminal:
            report.capability_gaps.append("work_failed")
        elif terminal:
            report.capability_gaps.append("work_failed_terminal_only")
        record_check(report, "failed_work", "present", f"{report.failed_work} (terminal={terminal})")
    if report.deferred_sources:
        record_check(report, "source_processing", "deferred", str(report.deferred_sources))
        report.capability_gaps.append("source_processing_deferred")


def check_candidates(report: DoctorReport) -> None:
    """One line for the candidate pipeline, naming the most pressing state first."""
    if report.candidate_capability_unavailable:
        record_check(
            report, "candidate_processing", "capability_unavailable", str(report.candidate_capability_unavailable)
        )
        report.capability_gaps.append("candidate_capability_unavailable")
    elif report.candidate_budget_paused:
        record_check(report, "candidate_processing", "budget_paused", str(report.candidate_budget_paused))
        report.capability_gaps.append("candidate_budget_paused")
    elif report.candidate_pending_evaluation:
        # ``pending_evaluation`` is a lifecycle state, not a queue.  What will be
        # evaluated is what is queued, still collecting, or settled with a new
        # question; a candidate whose evidence was already put to the evaluator
        # waits for new evidence however long it keeps the state.  On one live
        # store 1,031 of 1,032 were of that kind, and the bare "pending 1032"
        # read as a backlog that never drains.  The state's count stays in the
        # line; the settling figures are this context's and may cover less.
        settling = report.candidate_settling
        due = sum(int(settling.get(key, 0)) for key in ("queued", "collecting", "settled_waiting_sweep"))
        record_check(
            report,
            "candidate_processing",
            "pending",
            f"due={due},nothing_new_to_ask={int(settling.get('settled_nothing_to_ask', 0))},"
            f"pending_evaluation={report.candidate_pending_evaluation}",
        )
    elif report.candidate_waiting_evidence or report.candidate_dormant:
        record_check(
            report,
            "candidate_processing",
            "waiting_evidence",
            f"waiting={report.candidate_waiting_evidence},dormant={report.candidate_dormant}",
        )
    else:
        record_check(report, "candidate_processing", "idle")
    if report.candidate_failed:
        record_check(report, "candidate_failures", "retained", str(report.candidate_failed))


#: Cut-off answers in an hour that mean the route's output limit is wrong, not
#: that one source was long: a model that reasons by default (DeepSeek V4 Flash)
#: counts its reasoning against max_tokens, and most consolidation answers are cut
#: off while the backlog stands still, visible only in recent_work_errors.
OUTPUT_TRUNCATION_ALERT = 5


def check_model_output(report: DoctorReport) -> None:
    if report.recent_output_truncations < OUTPUT_TRUNCATION_ALERT:
        return
    report.capability_gaps.append("model_output_truncated")
    record_check(
        report,
        "model_output",
        "truncated",
        f"{report.recent_output_truncations} model answers were cut off at the output limit in the last "
        "hour and failed as model_output_truncated; raise the route's max_output_tokens, or turn the "
        "provider's thinking off when its reasoning counts against that limit "
        '(DeepSeek: "thinking": {"type": "disabled"})',
    )


def check_ledger(report: DoctorReport) -> None:
    """A gap only once a lifetime cap is close enough to need action; the ratio
    itself is always in ``ledger_headroom`` so it is visible long before that."""
    ratio = report.ledger_headroom.get("worst_used_ratio") or 0
    if ratio >= _LEDGER_PRESSURE_WARN:
        record_check(report, "auxiliary_ledger", "pressure", f"{ratio:.0%} of a lifetime cap")
        report.capability_gaps.append("auxiliary_budget_pressure")
