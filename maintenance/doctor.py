"""Read-only installation diagnostics for v1.1 host wrappers."""
from __future__ import annotations

from contextlib import closing, suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
import sqlite3
import time
from collections import Counter
import subprocess
from pathlib import Path
from typing import Any, Literal

from scope_recall.contracts import TrustedContext
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.schema import SCHEMA_VERSION
from scope_recall.core.failure_retry import NEEDS_REVIEW_COUNT
from scope_recall.core.work_storage import AUTO_RECOVERABLE_ERRORS
from scope_recall.runtime.model_budget import pre_request_refusals, provider_refusals
from scope_recall.runtime.running_code import live_records, stale_records
from scope_recall.vector.compaction import instance_vector_footprints
from scope_recall._version import __version__

HostChoice = Literal["hermes", "codex"]
# Run the new checker file with the target interpreter even if its installed
# package predates these diagnostics. Optional libraries are never imported.
_PACKAGE_PROBE = Path(__file__).with_name("package_health.py")



@dataclass
class DoctorReport:
    host: HostChoice
    status: str
    python_executable: str | None = None
    package_ok: bool = False
    package_source: str | None = None
    package_version: str | None = None
    package_path: str | None = None
    expected_package_version: str = __version__
    binding_ok: bool = False
    database_present: bool = False
    schema_version: int | None = None
    memory_epoch: int | None = None
    pending_work: int | None = None
    failed_work: int | None = None
    terminal_failed_work: int | None = None
    needs_review_work: int = 0
    leased_work: int | None = None
    oldest_pending_at: str | None = None
    oldest_pending_age_seconds: float | None = None
    work_error_counts: dict[str, int] = field(default_factory=dict)
    recent_work_errors: list[dict[str, Any]] = field(default_factory=list)
    capture_inbox: int = 0
    capture_inbox_blocked: int = 0
    extraction_outcomes: dict[str, int] = field(default_factory=dict)
    autostart_status: str = "not_registered"
    worker_status: dict[str, Any] = field(default_factory=dict)
    sources: int | None = None
    source_only_sources: int | None = None
    deferred_sources: int | None = None
    oldest_deferred_at: str | None = None
    candidate_pending_evaluation: int = 0
    candidate_waiting_evidence: int = 0
    candidate_dormant: int = 0
    candidate_blocked: int = 0
    candidate_resolved: int = 0
    candidate_archived_other: int = 0
    candidate_failed: int = 0
    candidate_budget_paused: int = 0
    candidate_capability_unavailable: int = 0
    candidate_oldest_waiting_at: str | None = None
    host_registration_status: str = "pending"
    hook_trust_status: str = "unknown"
    index_metadata: dict[str, Any] = field(default_factory=dict)
    ledger_headroom: dict[str, Any] = field(default_factory=dict)
    running_code: dict[str, Any] = field(default_factory=dict)
    package_health: dict[str, Any] = field(default_factory=dict)
    candidate_settling: dict[str, int] = field(default_factory=dict)
    capability_gaps: list[str] = field(default_factory=list)
    checks: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "status": self.status,
            "python_executable": self.python_executable,
            "package_ok": self.package_ok,
            "package_source": self.package_source,
            "package_version": self.package_version,
            "package_path": self.package_path,
            "expected_package_version": self.expected_package_version,
            "binding_ok": self.binding_ok,
            "database_present": self.database_present,
            "schema_version": self.schema_version,
            "memory_epoch": self.memory_epoch,
            "pending_work": self.pending_work,
            "failed_work": self.failed_work,
            "needs_review_work": self.needs_review_work,
            "terminal_failed_work": self.terminal_failed_work,
            "leased_work": self.leased_work,
            "oldest_pending_at": self.oldest_pending_at,
            "oldest_pending_age_seconds": self.oldest_pending_age_seconds,
            "work_error_counts": dict(self.work_error_counts),
            "recent_work_errors": list(self.recent_work_errors),
            "capture_inbox": self.capture_inbox,
            "capture_inbox_blocked": self.capture_inbox_blocked,
            "extraction_outcomes": dict(self.extraction_outcomes),
            "autostart_status": self.autostart_status,
            "worker_status": dict(self.worker_status),
            "sources": self.sources,
            "source_only_sources": self.source_only_sources,
            "deferred_sources": self.deferred_sources,
            "oldest_deferred_at": self.oldest_deferred_at,
            "candidate_pending_evaluation": self.candidate_pending_evaluation,
            "candidate_waiting_evidence": self.candidate_waiting_evidence,
            "candidate_dormant": self.candidate_dormant,
            "candidate_blocked": self.candidate_blocked,
            "candidate_resolved": self.candidate_resolved,
            "candidate_archived_other": self.candidate_archived_other,
            "candidate_failed": self.candidate_failed,
            "candidate_budget_paused": self.candidate_budget_paused,
            "candidate_capability_unavailable": self.candidate_capability_unavailable,
            "candidate_oldest_waiting_at": self.candidate_oldest_waiting_at,
            "host_registration_status": self.host_registration_status,
            "hook_trust_status": self.hook_trust_status,
            "index_metadata": dict(self.index_metadata),
            "ledger_headroom": dict(self.ledger_headroom),
            "running_code": dict(self.running_code),
            "package_health": dict(self.package_health),
            "candidate_settling": dict(self.candidate_settling),
            "capability_gaps": list(self.capability_gaps),
            "checks": list(self.checks),
        }


def _record(report: DoctorReport, name: str, result: str, detail: str = "") -> None:
    item = {"name": name, "result": result}
    if detail:
        item["detail"] = detail
    report.checks.append(item)


def _hermes_data_dir(instance_root: Path) -> Path:
    return instance_root / "scope-recall"


def _codex_config_path(instance_root: Path) -> Path:
    return instance_root / "codex-installation.json"


def _require_absolute(path: Path, field: str) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise ValueError(f"{field} must be absolute")
    return expanded.resolve()


def _probe_python_package(python: Path) -> tuple[bool, dict[str, Any]]:
    try:
        result = subprocess.run(
            [str(python), "-I", "-B", str(_PACKAGE_PROBE)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, {}
    if result.returncode != 0:
        return False, {}
    try:
        if len(result.stdout) > 65536:
            return False, {}
        found = json.loads(result.stdout)
        if not isinstance(found, dict) or found.get('source') not in {'installed', 'development'}:
            return False, {}
        if not all(type(found.get(key)) is str and 0 < len(found[key]) <= 4096 for key in ('version', 'path')):
            return False, {}
    except (ValueError, TypeError):
        return False, {}
    return True, found


def _load_binding(host: HostChoice, instance_root: Path):
    if host == "hermes":
        from scope_recall.adapters.hermes.installation import load_installation_manifest

        manifest = load_installation_manifest(instance_root)
        return manifest.to_binding(), manifest.data_directory
    from scope_recall.adapters.codex.config import load_codex_config

    config = load_codex_config(_codex_config_path(instance_root))
    return config.to_binding(), config.data_directory


#: Embedded objects beyond which a brute-force vector scan stops being free and
#: an approximate index starts to earn its complexity. Below it an ANN index is
#: a net loss: it costs build time and recall for a search that already answers
#: in milliseconds. Reported rather than acted on, so the day the corpus crosses
#: it is visible instead of arriving as unexplained latency.
_VECTOR_SCAN_COMFORT_LIMIT = 100_000


def _optional_index_metadata(data_directory: Path, embedded: int | None = None) -> dict[str, Any]:
    vectors_dir = data_directory / "vectors"
    metadata: dict[str, Any] = {"vectors_dir_present": vectors_dir.is_dir()}
    if embedded is not None:
        metadata["embedded_objects"] = embedded
        metadata["vector_scan_comfort_limit"] = _VECTOR_SCAN_COMFORT_LIMIT
        metadata["vector_index_advised"] = embedded > _VECTOR_SCAN_COMFORT_LIMIT
    # Fragment count is the cost an operator can actually verify with a file
    # listing, and it is what a missed compaction shows up as first: on TianShu
    # it reached 2,243 fragments and 237.6 MB of manifest history, which cost
    # 142 ms per search against 29 ms on the same rows once compacted.
    try:
        metadata["vector_stores"] = instance_vector_footprints(data_directory)
    except Exception:  # noqa: BLE001 - reporting must not fail the report.
        metadata["vector_stores"] = []
    return metadata


#: Fraction of any auxiliary ledger cap above which the instance is warned.
#: The ledger's caps are lifetime totals, not a rolling window, so headroom only
#: ever shrinks: once a cap is reached the derived layer stops for good and the
#: only visible symptom is work quietly pausing with ``budget_exhausted``.
#: Reporting the ratio turns "it stopped working one day" into something an
#: operator can see coming.
_LEDGER_PRESSURE_WARN = 0.90




def _ledger_headroom(ledger_path: Path | None, policy: Any) -> dict[str, Any]:
    """Lifetime usage of each auxiliary-ledger cap, as used/cap plus a ratio.

    Read-only and best-effort: a missing ledger, an unreadable file or a policy
    without caps reports nothing rather than failing the whole doctor run.
    """
    if ledger_path is None or policy is None:
        return {}
    try:
        if not Path(ledger_path).is_file():
            return {}
        uri = f"file:{Path(ledger_path).as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
            row = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(charge_micro_usd),0),"
                " COALESCE(SUM(COALESCE(actual_input,reserved_input)),0),"
                " COALESCE(SUM(COALESCE(actual_output,reserved_output)),0) FROM requests"
            ).fetchone()
    except (sqlite3.Error, OSError, ValueError):
        return {}
    calls, _charge, inputs, outputs = (int(value or 0) for value in row)
    # Deliberately no money figure. What an instance spent depends on that
    # operator's own contract, so a currency amount reported here means
    # something different for every reader and nothing comparable to anyone --
    # while calls and tokens are the same unit for everybody. The ledger still
    # meters charges internally, because ``meter_breach`` uses them to catch a
    # provider billing an instance for work it did not request; that is an
    # anomaly check, not a usage report.
    caps = (
        ("calls", calls, getattr(policy, "total_call_cap", 0)),
        ("input_tokens", inputs, getattr(policy, "total_input_cap", 0)),
        ("output_tokens", outputs, getattr(policy, "total_output_cap", 0)),
    )
    headroom: dict[str, Any] = {}
    worst = 0.0
    for name, used, cap in caps:
        # None is an uncapped total; report the usage but no ratio, and never let
        # it contribute to the pressure signal. 0 is still a hard stop, and a
        # ratio against it is undefined rather than infinite.
        entry: dict[str, Any] = {"used": used, "cap": None if cap is None else int(cap)}
        if cap is not None and int(cap) > 0:
            ratio = used / int(cap)
            entry["used_ratio"] = round(ratio, 4)
            worst = max(worst, ratio)
        headroom[name] = entry
    headroom["worst_used_ratio"] = round(worst, 4)
    headroom["scope"] = "lifetime"
    return headroom


#: Failures that no amount of waiting clears, so they must not pin the instance
#: at "degraded" forever.  Two kinds qualify, and only two:
#:
#: * ``derivation_invalid`` -- the model returned a payload that did not
#:   validate. After its one extra automatic attempt it needs review.
#:   This used to be counted for
#:   ``consolidate`` only, which left 213 identical failures on the candidate
#:   path driving "degraded" on tianshu with nobody able to act on them; the
#:   reasoning in the comment below never distinguished the two work types.
#: * ``budget_checked:*|input_invalid`` -- evidence that genuinely does not fit,
#:   already durably marked as having had its one re-look.
#:
#: Both remain visible in ``capability_gaps`` and still raise "attention".  An
#: operator can still grant them another attempt (``maintenance/cli.py
#: retry-failures``); terminal means "will not clear by itself", not "forbidden
#: to look at again".
TERMINAL_FAILURE_COUNT = """
    SELECT count(*) FROM work_items WHERE state='failed' AND (
        lower(last_error_code)='derivation_invalid'
        OR lower(last_error_code) LIKE '%|derivation_invalid'
        OR lower(last_error_code) LIKE 'budget_checked:%|input_invalid'
    )
"""


#: Gaps that report a standing configuration choice or a by-design terminal
#: state. They stay visible in ``capability_gaps`` and still raise "attention",
#: but they must not drive "degraded": a status that is permanently degraded
#: carries no signal when something actually breaks.
_NON_ACTIONABLE_GAPS = frozenset({
    "work_failed_terminal_only",
    "work_needs_review",
    "worker_capability_unavailable",
})

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
    worker's own last success and nothing else.

    Deliberately not derived from ``oldest_pending_age_seconds``: a migration
    carries the original timestamps, so its freshly enqueued items can be months
    old on the day they are created. Judging by item age reported every healthy
    migration as stalled for as long as it took to drain.

    A worker that has never recorded a success is judged by its last finished
    run. One that has never run at all is not accused here — registration and
    autostart checks own that case.
    """
    status = worker_status or {}
    window = _STALL_WAKE_MULTIPLE * float(wake_seconds or _DEFAULT_SUPERVISOR_SECONDS)
    stamp = str(status.get("last_success_at") or status.get("finished_at") or "").strip()
    if not stamp:
        return False
    try:
        seen = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    return (datetime.now(timezone.utc) - seen).total_seconds() > window


def _loaded_package_root(report: DoctorReport) -> Path | None:
    """Directory a restart would load the package from.

    With a target interpreter the probe reports ``_version.py``'s path, whose
    parent is the package root.  Without one the report carries this module's
    own path instead, which is a level too deep, so the root comes from the
    imported package.
    """
    if report.python_executable and report.package_path:
        return Path(report.package_path).parent
    import scope_recall

    module_file = getattr(scope_recall, "__file__", None)
    return Path(module_file).resolve().parent if module_file else None


def _check_running_code(report: DoctorReport, data_directory: Path) -> None:
    """Report live processes that are not running the package now on disk.

    The reference version is whatever the *target* interpreter resolves, which
    is the code a restart would actually load; falling back to this checker's
    own version only when no target interpreter was given.

    Failures here are swallowed on purpose.  This is an advisory breadcrumb
    reader, and a doctor that cannot finish because a breadcrumb was malformed
    would hide every other finding in the report.
    """
    reference = report.package_version or __version__
    try:
        records = live_records(data_directory)
        stale = stale_records(
            data_directory,
            disk_version=reference,
            package_path=_loaded_package_root(report),
        )
    except Exception as exc:  # noqa: BLE001 - advisory only; see docstring.
        _record(report, "running_code", "unreadable", type(exc).__name__)
        return
    report.running_code = {
        "reference_version": reference,
        "live_processes": [
            {
                "pid": record.pid,
                "version": record.version,
                "host_adapter": record.host_adapter,
                "first_record_at": record.first_record_at,
            }
            for record in records
        ],
        "stale_processes": stale,
    }
    if stale:
        report.capability_gaps.append("stale_process")
        _record(report, "running_code", "stale", ",".join(str(item["pid"]) for item in stale))
        return
    # An empty list is not a clean bill of health: a host registers when it binds
    # an identity for a session, so one that has started and had no conversation
    # yet is simply not here. Saying "ok" would invite the opposite reading.
    _record(report, "running_code", "ok" if records else "no_records", str(len(records)))


def _host_registration_status(host: str, instance: Path, python_executable: Path | None = None) -> str:
    """Report whether the host can actually reach this provider.

    The previous value was the constant "pending": it could never become
    anything else, so the check carried no information and an operator could not
    tell a working install from a broken one. For Hermes, registration means the
    package exposes its memory-provider entry point and the instance selects
    that provider in config.yaml.
    """
    if host != "hermes":
        return "pending"
    if python_executable is not None:
        script = "import importlib.metadata as m,json; print(json.dumps(any(e.name=='scope-recall' for e in m.entry_points(group='hermes_agent.memory_providers'))))"
        try:
            probe = subprocess.run([str(python_executable), "-I", "-B", "-c", script],
                                   capture_output=True, text=True, timeout=15, check=False)
            if probe.returncode or json.loads(probe.stdout) is not True:
                return "entry_point_missing"
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return "unknown"
    if python_executable is None:
        try:
            import importlib.metadata as metadata
            entries = metadata.entry_points(group="hermes_agent.memory_providers")
            if not any(entry.name == "scope-recall" for entry in entries):
                return "entry_point_missing"
        except Exception:
            return "unknown"
    config = instance / "config.yaml"
    if not config.is_file():
        return "host_config_missing"
    try:
        import yaml
        value = yaml.safe_load(config.read_text(encoding="utf-8"))
        memory = value.get("memory", {}) if isinstance(value, dict) else {}
        selected = memory.get("provider") if isinstance(memory, dict) else None
    except (OSError, ValueError, yaml.YAMLError):
        return "unknown"
    return "registered" if selected == "scope-recall" else "not_selected"


def run_doctor(
    *,
    host: str,
    instance_root: Path | str,
    python_executable: Path | str | None = None,
) -> DoctorReport:
    if host == "hermes":
        host_choice: HostChoice = "hermes"
    elif host == "codex":
        host_choice = "codex"
    else:
        raise ValueError("host must be 'hermes' or 'codex'")
    instance = _require_absolute(Path(instance_root), "instance_root")

    from .package_health import apply_package_health, package_probe as current_package_probe

    package_probe: dict[str, Any] = {}
    report = DoctorReport(host=host_choice, status="degraded")
    report.host_registration_status = _host_registration_status(host_choice, instance, Path(python_executable) if python_executable else None)
    report.hook_trust_status = "pending" if host_choice == "codex" else "unknown"
    _record(report, "host_registration", report.host_registration_status)
    if report.host_registration_status not in {"registered", "pending"}:
        report.capability_gaps.append("host_registration_incomplete")
    if host_choice == "codex":
        _record(report, "hook_trust", "pending")

    if python_executable is not None:
        python_path = _require_absolute(Path(python_executable), "python_executable")
        if not python_path.is_file():
            report.capability_gaps.append("python_executable_missing")
            _record(report, "python_executable", "missing")
        else:
            report.python_executable = str(python_path)
            package_ok, package_probe = _probe_python_package(python_path)
            if package_ok:
                report.package_source = package_probe['source']
                report.package_version = package_probe['version']
                report.package_path = package_probe['path']
                report.package_ok = report.package_version == __version__
                if not report.package_ok:
                    report.capability_gaps.append('python_package_version_mismatch')
                if package_probe.get('distribution_version') not in (None, report.package_version):
                    report.package_ok = False
                    report.capability_gaps.append('python_package_metadata_mismatch')
                _record(report, "python_package", "ok" if report.package_ok else "mismatch", report.package_version)
            else:
                report.capability_gaps.append("python_package_missing")
                _record(report, "python_package", "missing")

    try:
        import scope_recall.core  # noqa: F401
    except ImportError as exc:
        report.capability_gaps.append(f"package_import:{type(exc).__name__}")
        _record(report, "package", "missing", type(exc).__name__)
        return report

    if python_executable is None:
        report.package_ok = True
        report.package_version = __version__
        report.package_path = str(Path(__file__).resolve())
        module_file = Path(getattr(__import__("scope_recall"), "__file__", "") or "")
        normalized = os.path.normcase(str(module_file)).replace("\\", "/")
        report.package_source = (
            "installed"
            if "site-packages" in normalized or "dist-packages" in normalized
            else "development"
        )
        _record(report, "package", "ok", report.package_source)
        package_probe = current_package_probe()
    # An unavailable target interpreter is one diagnostic result. The current
    # installed checker can still inspect the explicit binding and SQLite store.

    config_path = (
        _codex_config_path(instance)
        if host_choice == "codex"
        else _hermes_data_dir(instance) / "installation.json"
    )
    apply_package_health(report, instance, package_probe)
    if not config_path.is_file():
        report.capability_gaps.append("installation_config_missing")
        _record(report, "adapter_config", "missing")
        return report

    try:
        binding, data_directory = _load_binding(host_choice, instance)
    except Exception as exc:
        report.capability_gaps.append(f"binding_invalid:{type(exc).__name__}")
        _record(report, "adapter_binding", "invalid", type(exc).__name__)
        return report

    report.binding_ok = True
    _record(report, "adapter_binding", "ok", binding.installation_id)

    _check_running_code(report, data_directory)
    report.checks = [item for item in report.checks if item["name"] not in report.package_health]
    apply_package_health(report, instance, package_probe)

    db_path = data_directory / "memory.sqlite3"
    report.database_present = db_path.is_file()
    if not report.database_present:
        report.capability_gaps.append("database_missing")
        _record(report, "database", "missing")
        report.index_metadata = _optional_index_metadata(data_directory)
        return report

    _record(report, "database", "ok")
    context = TrustedContext(binding, "doctor-readonly", binding.scope_ids, "origin_unknown")
    try:
        core = MemoryCore(CoreConfig(binding))
        with core.storage.read(context) as transaction:
            status = transaction.status(include_all_projects=True, include_admission=True)
            conn = transaction._check()
            report.capture_inbox = conn.execute("SELECT count(*) FROM capture_inbox").fetchone()[0]
            report.capture_inbox_blocked = conn.execute("SELECT count(*) FROM capture_inbox WHERE last_error_code IS NOT NULL AND last_error_code NOT IN ('STORAGE_UNAVAILABLE','DEADLINE_EXCEEDED')").fetchone()[0]
            report.recent_work_errors = [dict(r) for r in conn.execute("SELECT work_id,lease_token,stage,error_code,error_field,recorded_at FROM work_error_details ORDER BY detail_id DESC LIMIT 16")]
            report.extraction_outcomes = dict(conn.execute("SELECT disposition,count(*) FROM consolidation_outcomes GROUP BY disposition").fetchall())
            terminal_extraction_failures = conn.execute(TERMINAL_FAILURE_COUNT).fetchone()[0]
            report.needs_review_work = conn.execute(NEEDS_REVIEW_COUNT).fetchone()[0]
            candidate_summary = transaction.candidates.summary(include_all_projects=True)
            # Debouncing raises pending_evaluation on purpose, so split that
            # number: waiting inside the quiet window is health, waiting past it
            # with no sweep having run is not.
            candidate_settling = transaction.candidates.settling_summary(
                now=datetime.now(timezone.utc).isoformat())
    except Exception as exc:
        report.capability_gaps.append(f"storage_read:{type(exc).__name__}")
        _record(report, "storage_status", "unavailable", type(exc).__name__)
        report.index_metadata = _optional_index_metadata(data_directory)
        return report

    report.schema_version = status.schema_version
    report.memory_epoch = status.memory_epoch
    report.pending_work = status.pending_work
    report.failed_work = status.failed_work
    report.leased_work = status.leased_work
    report.source_only_sources = status.source_only_sources
    report.deferred_sources = status.deferred_sources
    report.oldest_deferred_at = status.oldest_deferred_at
    report.oldest_pending_at = status.oldest_pending_at
    report.candidate_pending_evaluation = candidate_summary.pending_evaluation
    report.candidate_waiting_evidence = candidate_summary.waiting_evidence
    report.candidate_dormant = candidate_summary.dormant
    report.candidate_blocked = candidate_summary.blocked
    report.candidate_resolved = candidate_summary.resolved
    report.candidate_archived_other = candidate_summary.archived_other
    report.candidate_failed = candidate_summary.failed
    report.candidate_budget_paused = candidate_summary.budget_paused
    report.candidate_capability_unavailable = candidate_summary.capability_unavailable
    report.candidate_settling = candidate_settling
    report.candidate_oldest_waiting_at = candidate_summary.oldest_waiting_at
    for error, count in status.work_error_counts:
        report.work_error_counts[error] = report.work_error_counts.get(error, 0) + count
    if status.oldest_pending_at:
        try:
            oldest = datetime.fromisoformat(status.oldest_pending_at.replace("Z", "+00:00"))
            report.oldest_pending_age_seconds = max(0, (datetime.now(timezone.utc) - oldest).total_seconds())
        except (ValueError, TypeError):
            report.capability_gaps.append("work_timestamp_invalid")
    worker_path = data_directory / "runtime-worker-status.json"
    if worker_path.exists():
        try:
            if worker_path.is_symlink() or worker_path.stat().st_size > 65536:
                raise ValueError("worker_status_invalid")
            worker_status = json.loads(worker_path.read_text(encoding="utf-8"))
            if worker_status.get("installation_id") != binding.installation_id:
                raise ValueError("worker_status_binding_mismatch")
            allowed = {"status", "installation_id", "started_at", "finished_at", "exit_code", "worker_pid",
                       "last_success_at", "completed", "failed", "retried", "deferred", "recovered",
                       "daily_queue_used", "capability_gaps", "unavailable_work_types",
                       "pending_work", "failed_work", "oldest_pending_at"}
            report.worker_status = {key: value for key, value in worker_status.items() if key in allowed}
            if worker_status.get("exit_code", 0) != 0:
                report.capability_gaps.append("worker_last_exit_failed")
            if status.pending_work and worker_status.get("unavailable_work_types"):
                report.capability_gaps.append("worker_capability_unavailable")
        except (OSError, ValueError, TypeError, AttributeError):
            report.capability_gaps.append("worker_status_unreadable")
    report.sources = status.sources
    from ..runtime.resume_entry import read_control
    from ..runtime.worker_entry import load_config
    # The stall window is a multiple of the configured wake interval, so capture
    # it here where the runtime config is already being read. Stays None when
    # autostart is absent or unreadable; the helper falls back to the default.
    wake_seconds: float | None = None
    autostart_path = data_directory / "runtime-autostart.json"
    if autostart_path.exists():
        try:
            if autostart_path.is_symlink() or autostart_path.stat().st_size > 65536:
                raise ValueError("autostart_control_invalid")
            entry = json.loads(autostart_path.read_text(encoding="utf-8"))
            runtime_config = load_config(entry["config_path"])
            if runtime_config.binding != binding:
                raise ValueError("autostart_binding")
            wake_seconds = float(getattr(runtime_config, "supervisor_seconds", 0) or 0) or None
            aux = getattr(runtime_config, "auxiliary", None)
            if aux is not None:
                ledger = getattr(aux, "ledger_path", None)
                report.ledger_headroom = _ledger_headroom(ledger, getattr(aux, "budget", None))
                report.capability_gaps.extend(provider_refusals(ledger))
                report.capability_gaps.extend(pre_request_refusals(aux))
            control = read_control(runtime_config)
            if not control["enabled"]:
                report.autostart_status = "paused"
            elif os.name == "nt":
                query = subprocess.run(["schtasks.exe", "/Query", "/TN", control["task_name"], "/XML"], capture_output=True, timeout=15)
                report.autostart_status = "registered" if query.returncode == 0 else "registration_missing"
                if query.returncode:
                    report.capability_gaps.append("autostart_registration_missing")
            else:
                report.autostart_status = "unsupported_platform"
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
            report.autostart_status = "invalid"
            report.capability_gaps.append("autostart_configuration_invalid")
    if status.schema_version != SCHEMA_VERSION:
        report.capability_gaps.append("schema_version_mismatch")
        _record(report, "schema", "mismatch", str(status.schema_version))
    else:
        _record(report, "schema", "ok", str(status.schema_version))

    if status.pending_work:
        _record(report, "work_backlog", "present", str(status.pending_work))
        # Age alone is not a fault. A fresh migration enqueues tens of thousands
        # of items that legitimately take days to drain, and the old
        # `oldest_pending_age_seconds > 3600` rule reported every one of those
        # days as "stalled" — so the first thing a new install saw was
        # "degraded". Stalled means the worker has stopped completing work, so
        # measure the last completion instead.
        if _backlog_is_stalled(report.worker_status, wake_seconds=wake_seconds):
            report.capability_gaps.append("work_backlog_stalled")
    else:
        _record(report, "work_backlog", "idle")
    if report.needs_review_work:
        report.capability_gaps.append("work_needs_review")
        _record(report, "needs_review_work", "present", str(report.needs_review_work))
    if status.failed_work:
        # A DERIVATION_INVALID is terminal by design and never clears, so
        # counting it as an ordinary gap pins the instance at "degraded" forever
        # and destroys the signal for the next real problem. Report it
        # separately; only recoverable failures still drive degraded.
        terminal = terminal_extraction_failures
        report.terminal_failed_work = terminal
        if max(0, status.failed_work - terminal):
            report.capability_gaps.append("work_failed")
        elif terminal:
            report.capability_gaps.append("work_failed_terminal_only")
        _record(report, "failed_work", "present", f"{status.failed_work} (terminal={terminal})")
    if status.deferred_sources:
        _record(report, 'source_processing', 'deferred', str(status.deferred_sources))
        report.capability_gaps.append('source_processing_deferred')
    if report.candidate_capability_unavailable:
        _record(report, "candidate_processing", "capability_unavailable", str(report.candidate_capability_unavailable))
        report.capability_gaps.append("candidate_capability_unavailable")
    elif report.candidate_budget_paused:
        _record(report, "candidate_processing", "budget_paused", str(report.candidate_budget_paused))
        report.capability_gaps.append("candidate_budget_paused")
    elif report.candidate_pending_evaluation:
        _record(report, "candidate_processing", "pending", str(report.candidate_pending_evaluation))
    elif report.candidate_waiting_evidence or report.candidate_dormant:
        _record(report, "candidate_processing", "waiting_evidence",
                f"waiting={report.candidate_waiting_evidence},dormant={report.candidate_dormant}")
    else:
        _record(report, "candidate_processing", "idle")
    if report.candidate_failed:
        _record(report, "candidate_failures", "retained", str(report.candidate_failed))

    embedded = None
    with suppress(sqlite3.Error, OSError, ValueError):
        with closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5)) as db:
            embedded = int(db.execute(
                "SELECT COUNT(*) FROM work_items WHERE work_type='embed' AND state='done'"
            ).fetchone()[0] or 0)
    report.index_metadata = _optional_index_metadata(data_directory, embedded)
    # The ledger's caps are lifetime totals: headroom only ever shrinks, and
    # reaching one stops the derived layer for good with no symptom beyond work
    # quietly pausing. Report the ratio always so it is visible long before it
    # binds, and raise a gap only when it is close enough to need action — a gap
    # that fires at half-full would pin the instance at degraded for months.
    if (report.ledger_headroom.get("worst_used_ratio") or 0) >= _LEDGER_PRESSURE_WARN:
        _record(report, "auxiliary_ledger", "pressure",
                f"{report.ledger_headroom['worst_used_ratio']:.0%} of a lifetime cap")
        report.capability_gaps.append("auxiliary_budget_pressure")
    # Gaps that describe a standing choice rather than a fault. Counting them as
    # actionable pins the instance at "degraded" for as long as the choice holds
    # and destroys the signal for the next real problem — the same reasoning
    # already applied to terminal extraction failures above.
    # `worker_capability_unavailable` is exactly that: it fires because the
    # operator declined external consolidation, so the work type is unavailable
    # by configuration, not because anything broke. It stays in capability_gaps
    # so it remains visible and still raises "attention".
    actionable = [
        gap for gap in report.capability_gaps
        if gap not in _NON_ACTIONABLE_GAPS
    ]
    if report.capture_inbox_blocked:
        actionable.append("capture_ingress_blocked")
        report.capability_gaps.append("capture_ingress_blocked")
    attention = (report.failed_work or report.capture_inbox
                 or any(report.extraction_outcomes.get(k) for k in ("partial", "source_only"))
                 or any(gap in _NON_ACTIONABLE_GAPS for gap in report.capability_gaps))
    report.status = "degraded" if actionable else ("attention" if attention else "ok")
    return report
