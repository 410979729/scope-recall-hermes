"""Read-only installation diagnostics for v1.1 host wrappers.

``run_doctor`` drives a sequence of named checks. Each check records its own
``checks`` row and capability gaps on the report; the driver only decides how
far into the instance the checks can get. Nothing here writes to the instance.
"""

from __future__ import annotations

from contextlib import closing, suppress
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import importlib.metadata
import json
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Literal

import scope_recall
from scope_recall.contracts import TrustedContext
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.capture_inbox import given_up, replayable
from scope_recall.core.schema import SCHEMA_VERSION, UPGRADE_CHAIN, stale_header_schema
from scope_recall.core.storage import SQLiteStorage
from scope_recall.core.failure_retry import NEEDS_REVIEW_COUNT
from scope_recall.core.index_rebuild import IMPORT_EMBED_QUEUE_CEILING
from scope_recall.runtime.model_budget import embedding_calls, pre_request_refusals, provider_holds, provider_refusals
from scope_recall.runtime.running_code import live_records, stale_records
from scope_recall.vector.compaction import instance_vector_footprints
from scope_recall._version import __version__

from . import package_health
from .install_common import RUNTIME_CONFIG_LIMIT

HostChoice = Literal["hermes", "codex", "claude-code", "workbuddy", "dsh"]
#: Run as a file by the target interpreter, so an installed package that
#: predates these diagnostics is still measured. It imports no optional library.
_PACKAGE_PROBE = Path(__file__).with_name("package_health.py")
#: Largest JSON control file the doctor will read from beside the store.
_CONTROL_FILE_LIMIT = 65536
#: A runtime config may weigh what the shared commands allow it to.
_RUNTIME_CONFIG_LIMIT = RUNTIME_CONFIG_LIMIT


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
    #: ``wal`` on every store this release has opened; readers and the writer coexist.
    journal_mode: str | None = None
    memory_epoch: int | None = None
    pending_work: int | None = None
    failed_work: int | None = None
    needs_review_work: int = 0
    terminal_failed_work: int | None = None
    leased_work: int | None = None
    oldest_pending_at: str | None = None
    oldest_pending_age_seconds: float | None = None
    work_error_counts: dict[str, int] = field(default_factory=dict)
    recent_work_errors: list[dict[str, Any]] = field(default_factory=list)
    #: Model answers cut off at the output limit in the last hour.
    recent_output_truncations: int = 0
    capture_inbox: int = 0
    capture_inbox_blocked: int = 0
    #: Of those, rows a replay gave up after its tries (``retry-failures --apply`` returns them to it).
    capture_inbox_given_up: int = 0
    extraction_outcomes: dict[str, int] = field(default_factory=dict)
    autostart_status: str = "not_registered"
    worker_status: dict[str, Any] = field(default_factory=dict)
    sources: int | None = None
    #: Bytes of memory.sqlite3 with its journal, and of everything under vectors/.
    store_bytes: int | None = None
    vector_bytes: int | None = None
    #: Sources that entered the store in the last day and the last week.
    sources_last_24h: int | None = None
    sources_last_7d: int | None = None
    #: runtime-config.json ``storage_budget_bytes``; 0 when none is set.
    storage_budget_bytes: int = 0
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
    #: The store's re-embed run (``respace-embeddings``), or ``None`` when none was started.
    embedding_respace: dict[str, Any] | None = None
    #: The embedding queue (pending, failed, the oldest pending), and with an external route the provider's hold
    #: and its answers over the last day (``_check_embedding_health``).
    embedding_health: dict[str, Any] = field(default_factory=dict)
    #: Work and candidates of any partition that have waited more than ``UNREACHED_HOURS``, by partition
    #: (``_check_unreached``).
    unreached: list[dict[str, Any]] = field(default_factory=list)
    #: For a home attached to a shared store: the store's root, and this home's
    #: entry.  Everything else in the report is then the shared store's.
    shared_store: dict[str, str] = field(default_factory=dict)
    capability_gaps: list[str] = field(default_factory=list)
    checks: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """The public JSON shape: the fields above, in this order."""
        return asdict(self)


def _record(report: DoctorReport, name: str, result: str, detail: str = "") -> None:
    item = {"name": name, "result": result}
    if detail:
        item["detail"] = detail
    report.checks.append(item)


def _hermes_data_dir(instance_root: Path) -> Path:
    return instance_root / "scope-recall"


def _codex_config_path(instance_root: Path) -> Path:
    """What a client binds with: a shared store's pointer, or a Codex installation of its own."""
    pointer = _hermes_data_dir(instance_root) / "attachment.json"
    return pointer if pointer.is_file() else instance_root / "codex-installation.json"


def _hermes_config_path(instance_root: Path) -> Path:
    """What a Hermes home binds with: a shared store's pointer, or its own manifest."""
    pointer = _hermes_data_dir(instance_root) / "attachment.json"
    return pointer if pointer.is_file() else _hermes_data_dir(instance_root) / "installation.json"


def _require_absolute(path: Path, name: str) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise ValueError(f"{name} must be absolute")
    return expanded.resolve()


def _read_control_file(path: Path) -> dict[str, Any] | None:
    """A small JSON object the runtime left beside the store; None when absent.

    A symlink or an oversized file is refused rather than followed: the doctor
    reads whatever sits at a well-known name inside the data directory and must
    not be steered into reading something else.
    """
    if not path.exists():
        return None
    # A shared worker's runtime config lists every scope of the store and passes 64 KB at a few hundred
    # scopes; it is bounded where the shared commands write it.
    limit = _RUNTIME_CONFIG_LIMIT if path.name == "runtime-config.json" else _CONTROL_FILE_LIMIT
    if path.is_symlink() or path.stat().st_size > limit:
        raise ValueError(f"{path.stem}_invalid")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{path.stem}_invalid")
    return loaded


def _seconds_since(stamp: Any) -> float | None:
    """Age of an ISO-8601 timestamp; None when it is missing or unparseable."""
    if not stamp:
        return None
    try:
        seen = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - seen).total_seconds()
    except (ValueError, TypeError):
        return None


def _probe_python_package(python: Path) -> dict[str, Any]:
    """What the target interpreter imports; empty when it cannot answer cleanly."""
    try:
        result = subprocess.run(
            [str(python), "-I", "-B", str(_PACKAGE_PROBE)], capture_output=True, text=True, timeout=30, check=False
        )
        found = json.loads(result.stdout) if result.returncode == 0 and len(result.stdout) <= 65536 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {}
    if not isinstance(found, dict) or found.get("source") not in {"installed", "development"}:
        return {}
    if not all(type(found.get(key)) is str and 0 < len(found[key]) <= 4096 for key in ("version", "path")):
        return {}
    return found


def _load_binding(host: HostChoice, instance_root: Path):
    if host == "hermes":
        from scope_recall.adapters.hermes.installation import load_binding_for_home

        manifest = load_binding_for_home(instance_root)
        return manifest.to_binding(), manifest.data_directory
    from scope_recall.adapters.clients.config import load_codex_config, load_shared_client

    path = _codex_config_path(instance_root)
    config = load_shared_client(instance_root, host) if path.name == "attachment.json" else load_codex_config(path)
    return config.to_binding(), config.data_directory


def _journal_mode(db_path: Path) -> str | None:
    with suppress(sqlite3.Error, OSError, ValueError):
        with closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5)) as db:
            return str(db.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    return None


def _schema_on_disk(db_path: Path) -> int | None:
    """The store's own schema stamp, read without opening it as a store."""
    with suppress(sqlite3.Error, OSError, ValueError):
        with closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5)) as db:
            return int(db.execute("PRAGMA user_version").fetchone()[0])
    return None


def _recorded_schema_under_stale_header(db_path: Path) -> int | None:
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


def _check_index(report: DoctorReport, data_directory: Path, *, store_readable: bool, config: Any = None) -> None:
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
    # operator's own contract, so a currency amount means something different
    # for every reader, while calls and tokens are the same unit for everybody.
    # The ledger still meters charges internally for ``meter_breach``, which is
    # an anomaly check rather than a usage report.
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
#: at "degraded" forever. Two kinds qualify, on every work type:
#:
#: * ``derivation_invalid`` -- the model returned a payload that did not
#:   validate; after its one extra automatic attempt it needs review.
#: * ``budget_checked:*|input_invalid`` -- evidence that genuinely does not fit,
#:   already durably marked as having had its one re-look.
#:
#: Both remain visible in ``capability_gaps`` and still raise "attention". An
#: operator can still grant them another attempt (``maintenance/cli.py
#: retry-failures``); terminal means "will not clear by itself", not "forbidden
#: to look at again".
TERMINAL_FAILURE_COUNT = """
    SELECT count(*) FROM work_items WHERE state='failed' AND (
        lower(last_error_code)='derivation_invalid'
        OR lower(last_error_code) LIKE '%|derivation_invalid'
        OR lower(last_error_code) LIKE 'budget_checked:%|input_invalid'
        OR lower(last_error_code) LIKE '%sensitive_request'
    )
"""


#: Gaps that report a standing configuration choice or a by-design terminal
#: state. They stay visible in ``capability_gaps`` and still raise "attention",
#: but they must not drive "degraded": a status that is permanently degraded
#: carries no signal when something actually breaks.
#: ``worker_capability_unavailable`` fires because the operator declined
#: external consolidation, so the work type is unavailable by configuration.
#: ``vector_threshold_unconfigured`` is vector recall wired without a threshold:
#: recall still answers lexically, and no value can be supplied for the operator
#: because a threshold is calibrated for one embedding model.
#: ``audience_owner_unverified`` is an owner grant whose user is no owner
#: principal: it grants nothing until the owner approves that user, and only
#: the owner knows whether to.
_NON_ACTIONABLE_GAPS = frozenset(
    {
        "audience_owner_unverified",
        "due_work_unreached",
        "vector_threshold_unconfigured",
        "work_failed_terminal_only",
        "work_needs_review",
        "worker_capability_unavailable",
    }
)

#: Default supervisor wake interval (``RuntimeInstanceConfig.supervisor_seconds``),
#: used when the runtime config cannot be read. A quiet instance legitimately
#: records no progress for one whole wake interval, so the stall window is a
#: multiple of it rather than a constant: a fixed six hours would equal the
#: default interval exactly and flap on a perfectly healthy instance.
_DEFAULT_SUPERVISOR_SECONDS = 21600.0
_STALL_WAKE_MULTIPLE = 2

#: The receipt fields the report carries. Anything else in the file (stderr,
#: model output) is arbitrary text and must not reach the doctor's JSON.
_WORKER_STATUS_KEYS = frozenset(
    {
        "status",
        "installation_id",
        "started_at",
        "finished_at",
        "exit_code",
        "worker_pid",
        "last_success_at",
        "completed",
        "failed",
        "retried",
        "deferred",
        "recovered",
        "daily_queue_used",
        "capability_gaps",
        "unavailable_work_types",
        "pending_work",
        "failed_work",
        "oldest_pending_at",
        "worker_error",
        "ingress_deferred",
        "ingress_given_up",
    }
)


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


def _loaded_package_root(report: DoctorReport) -> Path | None:
    """Directory a restart would load the package from.

    With a target interpreter the probe reports ``_version.py``'s path, whose
    parent is the package root. Without one the report carries this module's
    own path instead, which is a level too deep, so the root comes from the
    imported package.
    """
    if report.python_executable and report.package_path:
        return Path(report.package_path).parent
    module_file = getattr(scope_recall, "__file__", None)
    return Path(module_file).resolve().parent if module_file else None


def _check_running_code(report: DoctorReport, data_directory: Path) -> None:
    """Report live processes that are not running the package now on disk.

    The reference version is whatever the *target* interpreter resolves, which
    is the code a restart would actually load; this checker's own version only
    stands in when no target interpreter was given. Failures are swallowed on
    purpose: this is an advisory breadcrumb reader, and a doctor that cannot
    finish because a breadcrumb was malformed would hide every other finding.
    """
    reference = report.package_version or __version__
    try:
        records = live_records(data_directory)
        stale = stale_records(data_directory, disk_version=reference, package_path=_loaded_package_root(report))
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
    """Whether the host can actually reach this provider.

    For Hermes, registration means the package exposes its memory-provider entry
    point (in the target interpreter when one is given) and the instance selects
    that provider in config.yaml. Codex registration is not verified yet.
    """
    if host != "hermes":
        return "pending"
    if python_executable is not None:
        script = "import importlib.metadata as m,json; print(json.dumps(any(e.name=='scope-recall' for e in m.entry_points(group='hermes_agent.memory_providers'))))"
        try:
            probe = subprocess.run(
                [str(python_executable), "-I", "-B", "-c", script],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if probe.returncode or json.loads(probe.stdout) is not True:
                return "entry_point_missing"
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return "unknown"
    else:
        try:
            entries = importlib.metadata.entry_points(group="hermes_agent.memory_providers")
            if not any(entry.name == "scope-recall" for entry in entries):
                return "entry_point_missing"
        except Exception:  # noqa: BLE001 - metadata lookups fail in host-specific ways.
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


def _check_host_registration(report: DoctorReport, instance: Path, python_executable: Path | None) -> None:
    report.host_registration_status = _host_registration_status(report.host, instance, python_executable)
    report.hook_trust_status = "pending" if report.host == "codex" else "unknown"
    _record(report, "host_registration", report.host_registration_status)
    if report.host_registration_status not in {"registered", "pending"}:
        report.capability_gaps.append("host_registration_incomplete")
    if report.host == "codex":
        _record(report, "hook_trust", "pending")


def _check_python_package(report: DoctorReport, python: Path) -> dict[str, Any]:
    """Measure the package the target interpreter loads; returns its probe.

    The interpreter is probed as given: resolving a venv's ``bin/python``
    symlink would probe the base interpreter, which has no venv on its path
    and would report the package missing from an install that is fine (#87).
    """
    python = python.expanduser()
    if not python.is_absolute():
        raise ValueError("python_executable must be absolute")
    if not python.is_file():
        report.capability_gaps.append("python_executable_missing")
        _record(report, "python_executable", "missing")
        return {}
    report.python_executable = str(python)
    probe = _probe_python_package(python)
    if not probe:
        report.capability_gaps.append("python_package_missing")
        _record(report, "python_package", "missing")
        return {}
    report.package_source = probe["source"]
    report.package_version = probe["version"]
    report.package_path = probe["path"]
    report.package_ok = report.package_version == __version__
    if not report.package_ok:
        report.capability_gaps.append("python_package_version_mismatch")
    if probe.get("distribution_version") not in (None, report.package_version):
        report.package_ok = False
        report.capability_gaps.append("python_package_metadata_mismatch")
    _record(report, "python_package", "ok" if report.package_ok else "mismatch", report.package_version)
    return probe


def _check_current_package(report: DoctorReport) -> dict[str, Any]:
    """Without a target interpreter the checker's own package is the one under test."""
    report.package_ok = True
    report.package_version = __version__
    report.package_path = str(Path(__file__).resolve())
    location = os.path.normcase(getattr(scope_recall, "__file__", "") or "").replace("\\", "/")
    report.package_source = "installed" if "site-packages" in location or "dist-packages" in location else "development"
    _record(report, "package", "ok", report.package_source)
    return package_health.package_probe()


def _check_binding(report: DoctorReport, instance: Path):
    """The adapter binding and its data directory; None when the instance has no usable one."""
    config_path = _hermes_config_path(instance) if report.host == "hermes" else _codex_config_path(instance)
    if not config_path.is_file():
        report.capability_gaps.append("installation_config_missing")
        _record(report, "adapter_config", "missing")
        return None
    try:
        binding, data_directory = _load_binding(report.host, instance)
    except Exception as exc:  # noqa: BLE001 - a broken binding is a finding, not a crash.
        report.capability_gaps.append(f"binding_invalid:{type(exc).__name__}")
        _record(report, "adapter_binding", "invalid", type(exc).__name__)
        return None
    report.binding_ok = True
    _record(report, "adapter_binding", "ok", binding.installation_id)
    if binding.installation_kind == "shared":
        from scope_recall.adapters.hermes.installation import read_attachment

        attachment = read_attachment(instance)
        if attachment is not None:
            report.shared_store = {
                "root": str(attachment.root),
                "entry_id": attachment.entry_id,
                "entry_name": attachment.display_name,
            }
    return binding, data_directory


def _check_audiences(report: DoctorReport, instance: Path) -> None:
    """Owner grants no session can use: owner_private rows whose user is no owner principal.

    Such a row binds nothing, so every session on its route captures and recalls nothing while the CLI's
    route stays healthy (#175).  Counted by platform, never named by user.
    """
    if report.host != "hermes":
        return
    from scope_recall.adapters.hermes.installation import load_binding_for_home

    try:
        manifest = load_binding_for_home(instance)
    except Exception:  # noqa: BLE001 - _check_binding already reported an unusable binding.
        return
    owners = {(item["platform"], item["user_id"]) for item in manifest.owner_principals}
    unverified: dict[str, int] = {}
    for row in manifest.audiences:
        if row.get("kind") == "owner_private" and (row["platform"], row["user_id"]) not in owners:
            unverified[row["platform"]] = unverified.get(row["platform"], 0) + 1
    if unverified:
        report.capability_gaps.append("audience_owner_unverified")
        _record(
            report,
            "audiences",
            "owner_unverified",
            ",".join(f"{platform}={count}" for platform, count in sorted(unverified.items()))
            + ": owner_private rows whose user is no owner principal grant nothing; approve the owner's own "
            "desktop or tui login (apply-install --owner-login) or remove the rows",
        )


def _serves(worker, binding) -> bool:
    """Whether the autostart's worker is the one for this binding.

    A local store's worker binds exactly what the host does.  A shared store's
    one worker binds every scope of the store, so an entry's are among them.
    """
    if binding.installation_kind != "shared":
        return worker == binding
    return (
        worker.installation_kind,
        worker.installation_id,
        worker.agent_id,
        worker.test_mode,
        worker.data_directory.resolve(),
    ) == (
        binding.installation_kind,
        binding.installation_id,
        binding.agent_id,
        binding.test_mode,
        binding.data_directory.resolve(),
    ) and binding.scope_ids <= worker.scope_ids


def _check_storage(report: DoctorReport, binding, data_directory: Path) -> bool:
    """Copy the store's queue, source and candidate status onto the report.

    False when there is no database or it cannot be read; the report then keeps
    whatever was learned before.
    """
    report.database_present = (data_directory / "memory.sqlite3").is_file()
    if not report.database_present:
        report.capability_gaps.append("database_missing")
        _record(report, "database", "missing")
        return False
    report.journal_mode = _journal_mode(data_directory / "memory.sqlite3")
    _record(report, "database", "ok", f"journal_mode={report.journal_mode}")
    found = _schema_on_disk(data_directory / "memory.sqlite3")
    if found in UPGRADE_CHAIN:
        # Reported, never applied here: the doctor is read-only.
        report.schema_version = found
        report.capability_gaps.append("schema_upgrade_pending")
        _record(
            report,
            "schema",
            "upgrade_pending",
            f"{found} -> {SCHEMA_VERSION}; the next capture, recall or worker pass applies it in one transaction "
            "(on a store above 100 MB, a caller with a minute of budget: the worker pass, apply-install, "
            "upgrade-store or a Hermes session start)",
        )
        return False
    recorded = _recorded_schema_under_stale_header(data_directory / "memory.sqlite3")
    if recorded is not None:
        # Every open fails closed on the header, so say why and what repairs it (#117).
        report.schema_version = found
        report.capability_gaps.append("schema_header_stale")
        _record(
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
            terminal_failures = conn.execute(TERMINAL_FAILURE_COUNT).fetchone()[0]
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
        _record(report, "storage_status", "unavailable", type(exc).__name__)
        return False

    report.schema_version = status.schema_version
    report.memory_epoch = status.memory_epoch
    report.sources = status.sources
    report.sources_last_24h = int(growth[0] or 0)
    report.sources_last_7d = int(growth[1] or 0)
    report.pending_work = status.pending_work
    report.failed_work = status.failed_work
    # Only meaningful next to a failure count; stays None on a clean queue.
    report.terminal_failed_work = terminal_failures if status.failed_work else None
    report.leased_work = status.leased_work
    report.oldest_pending_at = status.oldest_pending_at
    report.source_only_sources = status.source_only_sources
    report.deferred_sources = status.deferred_sources
    report.oldest_deferred_at = status.oldest_deferred_at
    for error, count in status.work_error_counts:
        report.work_error_counts[error] = report.work_error_counts.get(error, 0) + count
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


def _check_worker_status(report: DoctorReport, binding, data_directory: Path) -> None:
    """The worker's last receipt, when it left one for this binding."""
    try:
        status = _read_control_file(data_directory / "runtime-worker-status.json")
        if status is None:
            return
        if status.get("installation_id") != binding.installation_id:
            raise ValueError("worker_status_binding_mismatch")
    except (OSError, ValueError):
        report.capability_gaps.append("worker_status_unreadable")
        return
    report.worker_status = {key: value for key, value in status.items() if key in _WORKER_STATUS_KEYS}
    # A pass that yielded failed nothing: another writer held the store, or another pass the worker lock, and the
    # supervisor tries again after a pause (runtime/worker_entry.py, exit 75 with status busy).  It was reported as
    # a failed exit (yuheng's audit of 3.4.2); its status says busy, and only other exits are failures.
    if status.get("exit_code", 0) != 0 and not (status.get("exit_code") == 75 and status.get("status") == "busy"):
        report.capability_gaps.append("worker_last_exit_failed")
    if report.pending_work and status.get("unavailable_work_types"):
        report.capability_gaps.append("worker_capability_unavailable")


def _check_supervisor(report: DoctorReport, data_directory: Path) -> None:
    """Whether the loop that drains the queue is still accepting wakes.

    A supervisor that met a hard worker failure used to mark itself non-accepting and
    return, leaving the processing loop stopped until the next autostart wake -- and
    saying so nowhere.  It was found by reading a control file by hand, at
    ``drains=217`` with 180 items still queued.  It is now bounded by consecutive
    failures instead of one, and either state is reported here: a loop that stood down
    is a finding, and so is one that is limping.
    """
    newest: dict[str, Any] | None = None
    try:
        for path in sorted(data_directory.glob("runtime-supervisor-*.json")):
            control = _read_control_file(path)
            if control is None or control.get("finished_at") is not None and control.get("state") == "paused":
                continue  # An operator pause is not a failure.
            if newest is None or str(control.get("started_at") or "") > str(newest.get("started_at") or ""):
                newest = control
    except (OSError, ValueError):
        report.capability_gaps.append("supervisor_state_unreadable")
        return
    if newest is None:
        return
    state = str(newest.get("state") or "")
    exit_code = newest.get("exit_code")
    failures = newest.get("worker_failures") or 0
    _record(report, "supervisor", state or "unknown", f"drains={newest.get('drains')} failures={failures}")
    if state == "failed":
        report.capability_gaps.append(f"supervisor_stood_down:{exit_code if exit_code is not None else 'unknown'}")
    elif failures:
        report.capability_gaps.append(f"worker_failures:{int(failures)}")


def _check_autostart(report: DoctorReport, binding, data_directory: Path) -> float | None:
    """Autostart registration, plus the budget state its runtime config points at.

    Returns the configured supervisor wake interval for the stall window, or
    None when autostart is absent or its config could not be read.
    """
    from ..runtime.resume_entry import read_control
    from ..runtime.worker_entry import load_config

    wake_seconds: float | None = None
    try:
        entry = _read_control_file(data_directory / "runtime-autostart.json")
        if entry is None:
            return None
        runtime_config = load_config(entry["config_path"])
        if not _serves(runtime_config.binding, binding):
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
        elif control.get("registration") == "operator_timer":
            # The operator's own timer runs the wake; nothing here can see it.
            report.autostart_status = "operator_timer"
        elif os.name != "nt":
            report.autostart_status = "unsupported_platform"
        else:
            query = subprocess.run(
                ["schtasks.exe", "/Query", "/TN", control["task_name"], "/XML"], capture_output=True, timeout=15
            )
            report.autostart_status = "registered" if query.returncode == 0 else "registration_missing"
            if query.returncode:
                report.capability_gaps.append("autostart_registration_missing")
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        report.autostart_status = "invalid"
        report.capability_gaps.append("autostart_configuration_invalid")
    return wake_seconds


def _runtime_config(data_directory: Path):
    """The instance's runtime-config.json as the worker loads it, or ``None``.

    An unusable file is ``None`` here; ``_check_vector_threshold`` names it.
    """
    from ..runtime.instance import RuntimeInstanceConfig

    try:
        raw = _read_control_file(data_directory / "runtime-config.json")
        return None if raw is None else RuntimeInstanceConfig.from_mapping(raw)
    except Exception:  # noqa: BLE001 - reporting must not fail the report.
        return None


def _directory_bytes(path: Path) -> int:
    total = 0
    with suppress(OSError):
        for item in path.rglob("*"):
            with suppress(OSError):
                if item.is_file():
                    total += item.stat().st_size
    return total


def _check_footprint(report: DoctorReport, data_directory: Path, config) -> None:
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
            _record(report, "storage_footprint", "over_budget", detail)
            return
    _record(report, "storage_footprint", "ok", detail)


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


def _check_runtime_config_present(report: DoctorReport, data_directory: Path) -> None:
    """A runtime config that was there and is gone, named instead of a silent basic mode (#118).

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
        _record(
            report,
            "runtime_config",
            "missing",
            f"{finished} finished embeddings and consolidations came from routes a runtime-config.json "
            "named; without it hosts run in basic mode and no worker runs",
        )


def _check_vector_threshold(report: DoctorReport, binding, data_directory: Path) -> None:
    """Vector recall that is wired but admits nothing, named instead of silent.

    Reads the ``runtime-config.json`` every host loads by default.  With a
    vector store and an approved embedding route but no ``vector_threshold``,
    sources and queries are still embedded while recall refuses every vector hit
    as ``vector_threshold_unconfigured``.  No threshold is assumed here.
    """
    from ..runtime.instance import RuntimeInstanceConfig

    try:
        raw = _read_control_file(data_directory / "runtime-config.json")
        config = None if raw is None else RuntimeInstanceConfig.from_mapping(raw)
    except Exception as exc:  # noqa: BLE001 - an unusable config is a finding, not a crash.
        _record(report, "vector_threshold", "invalid", type(exc).__name__)
        return
    auxiliary = getattr(config, "auxiliary", None)
    if (
        config is None
        or config.binding != binding
        or config.vector is None
        or auxiliary is None
        or auxiliary.external_embedding is not True
        or auxiliary.embedding is None
    ):
        return
    if config.vector_threshold is not None:
        _record(report, "vector_threshold", "configured", str(config.vector_threshold))
        return
    model = str(config.embedding_space()["model"])[:64]
    report.capability_gaps.append("vector_threshold_unconfigured")
    _record(
        report,
        "vector_threshold",
        "unconfigured",
        f"runtime-config.json configures vector recall with embedding model {model} but no "
        "vector_threshold; every vector hit is refused as vector_threshold_unconfigured and recall "
        "is lexical only until a threshold calibrated for this model is set",
    )


#: Hours the oldest waiting embedding may wait before the doctor says so.
EMBEDDING_BACKLOG_HOURS = 24


def _check_embedding_health(report: DoctorReport, config) -> None:
    """The embedding queue beside what the provider has been answering.

    Recall goes on answering while embeddings wait, by words alone, and nothing said so: an installation on a free
    tier met HTTP 429 most days and had embeddings waiting for over a week before a status page of its own showed it
    (reported with #200).  A backlog older than ``EMBEDDING_BACKLOG_HOURS`` is named, with the provider's hold and
    refusals when it has them.  Without a vector store and an external embedding route nothing embeds, by choice,
    and the queue only grows: that is no finding (review of 3.8.0)."""
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
    _record(report, "embedding_backlog", "aged", detail)


#: Hours due work or a candidate with new evidence may wait for a pass before the doctor says so.
UNREACHED_HOURS = 24


def _check_unreached(report: DoctorReport, config) -> None:
    """Work and candidates of any partition, this audience's or another's, that have waited more than a day.

    A partition's queue is drained only by a worker of its own audience, started by a session of that audience or by
    a scheduled wake, and the work-queue figures above cover this binding's audience only.  Work this installation's
    routes cannot do, and work a provider holds (reported by ``embedding_backlog_aged`` and ``model_refused``), is
    left out.  The store records no time a pass looked at an item, so a queue longer than its passes reach in a day
    is named too, and the finding asks for attention rather than degrading the report.  The detail line counts; the
    scope ids, which carry chat and account ids, are only in ``unreached``.
    """
    from ..runtime.scheduling import _capable_work_types

    capable = _capable_work_types(config) if config is not None else {"purge", "rebuild_projection"}
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
    _record(
        report,
        "due_work_unreached",
        "present",
        f"{work} work items and {candidates} candidates with new evidence have waited more than "
        f"{UNREACHED_HOURS} h, the oldest since {report.unreached[0]['oldest']}, in "
        f"{len(report.unreached)} partition(s) listed in unreached: no worker of that audience has run, or the "
        "queue is longer than its passes reach (docs/install.md, section 7)",
    )


def _check_embedding_respace(report: DoctorReport, config) -> None:
    """A re-embed run's progress, and a run no worker will go on with because the config embeds into another space.

    A worker reopens a page of the run at each drain only in the run's own space (``respace_if_due``); after a
    second change of model the run would wait for good, so it is named here with what to do."""
    run = report.embedding_respace
    if run is None:
        return
    if run["completed"]:
        _record(report, "embedding_respace", "complete", f"{run['reopened']} reopened, last at {run['updated_at']}")
        return
    space = config.embedding_space_id() if config is not None else None
    if space is not None and space != run["embedding_space"]:
        report.capability_gaps.append("embedding_respace_space_mismatch")
        _record(
            report,
            "embedding_respace",
            "space_mismatch",
            f"the run embeds into {run['embedding_space'][:12]} but runtime-config.json into {space[:12]}; "
            "run respace-embeddings --restart --apply for the new space, or --cancel --apply",
        )
        return
    # A held pass writes nothing, so the run's time alone does not say it waits (review of 3.8.0).
    waiting = report.embedding_health.get("pending")
    _record(
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


def _check_schema(report: DoctorReport) -> None:
    if report.schema_version != SCHEMA_VERSION:
        report.capability_gaps.append("schema_version_mismatch")
        _record(report, "schema", "mismatch", str(report.schema_version))
    else:
        _record(report, "schema", "ok", str(report.schema_version))


def _check_backlog(report: DoctorReport, wake_seconds: float | None) -> None:
    """Queue health: a backlog is only a fault once the worker stops clearing it."""
    if report.pending_work:
        _record(report, "work_backlog", "present", str(report.pending_work))
        if _backlog_is_stalled(report.worker_status, wake_seconds=wake_seconds):
            report.capability_gaps.append("work_backlog_stalled")
    else:
        _record(report, "work_backlog", "idle")
    if report.needs_review_work:
        report.capability_gaps.append("work_needs_review")
        _record(report, "needs_review_work", "present", str(report.needs_review_work))
    if report.failed_work:
        # Terminal failures never clear, so only the recoverable remainder may
        # drive "degraded"; the terminal count is still reported beside it.
        terminal = report.terminal_failed_work or 0
        if report.failed_work > terminal:
            report.capability_gaps.append("work_failed")
        elif terminal:
            report.capability_gaps.append("work_failed_terminal_only")
        _record(report, "failed_work", "present", f"{report.failed_work} (terminal={terminal})")
    if report.deferred_sources:
        _record(report, "source_processing", "deferred", str(report.deferred_sources))
        report.capability_gaps.append("source_processing_deferred")


def _check_candidates(report: DoctorReport) -> None:
    """One line for the candidate pipeline, naming the most pressing state first."""
    if report.candidate_capability_unavailable:
        _record(report, "candidate_processing", "capability_unavailable", str(report.candidate_capability_unavailable))
        report.capability_gaps.append("candidate_capability_unavailable")
    elif report.candidate_budget_paused:
        _record(report, "candidate_processing", "budget_paused", str(report.candidate_budget_paused))
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
        _record(
            report,
            "candidate_processing",
            "pending",
            f"due={due},nothing_new_to_ask={int(settling.get('settled_nothing_to_ask', 0))},"
            f"pending_evaluation={report.candidate_pending_evaluation}",
        )
    elif report.candidate_waiting_evidence or report.candidate_dormant:
        _record(
            report,
            "candidate_processing",
            "waiting_evidence",
            f"waiting={report.candidate_waiting_evidence},dormant={report.candidate_dormant}",
        )
    else:
        _record(report, "candidate_processing", "idle")
    if report.candidate_failed:
        _record(report, "candidate_failures", "retained", str(report.candidate_failed))


#: Cut-off answers in an hour that mean the route's output limit is wrong, not
#: that one source was long.  another instance's DeepSeek V4 Flash thought by default,
#: its reasoning counted against max_tokens, and most consolidation answers were
#: cut off while the backlog stood still -- visible only in recent_work_errors.
OUTPUT_TRUNCATION_ALERT = 5


def _check_model_output(report: DoctorReport) -> None:
    if report.recent_output_truncations < OUTPUT_TRUNCATION_ALERT:
        return
    report.capability_gaps.append("model_output_truncated")
    _record(
        report,
        "model_output",
        "truncated",
        f"{report.recent_output_truncations} model answers were cut off at the output limit in the last "
        "hour and failed as model_output_truncated; raise the route's max_output_tokens, or turn the "
        "provider's thinking off when its reasoning counts against that limit "
        '(DeepSeek: "thinking": {"type": "disabled"})',
    )


def _check_ledger(report: DoctorReport) -> None:
    """A gap only once a lifetime cap is close enough to need action; the ratio
    itself is always in ``ledger_headroom`` so it is visible long before that."""
    ratio = report.ledger_headroom.get("worst_used_ratio") or 0
    if ratio >= _LEDGER_PRESSURE_WARN:
        _record(report, "auxiliary_ledger", "pressure", f"{ratio:.0%} of a lifetime cap")
        report.capability_gaps.append("auxiliary_budget_pressure")


def _classify_status(report: DoctorReport) -> None:
    """degraded: something an operator must act on; attention: worth a look; ok."""
    if report.capture_inbox_blocked:
        report.capability_gaps.append("capture_ingress_blocked")
    actionable = [gap for gap in report.capability_gaps if gap not in _NON_ACTIONABLE_GAPS]
    attention = (
        report.failed_work
        or report.capture_inbox
        or any(report.extraction_outcomes.get(k) for k in ("partial", "source_only"))
        or any(gap in _NON_ACTIONABLE_GAPS for gap in report.capability_gaps)
    )
    report.status = "degraded" if actionable else ("attention" if attention else "ok")


def run_doctor(
    *,
    host: str,
    instance_root: Path | str,
    python_executable: Path | str | None = None,
) -> DoctorReport:
    if host not in ("hermes", "codex", "claude-code", "workbuddy", "dsh"):
        raise ValueError("host must be 'hermes', 'codex', 'claude-code', 'workbuddy' or 'dsh'")
    instance = _require_absolute(Path(instance_root), "instance_root")
    python = Path(python_executable) if python_executable is not None else None
    report = DoctorReport(host=host, status="degraded")

    _check_host_registration(report, instance, python)
    probe = _check_current_package(report) if python is None else _check_python_package(report, python)
    # Applied before the binding so an instance that cannot even be bound still
    # gets these, and again after the live breadcrumbs exist for version_mismatch.
    package_health.apply_package_health(report, instance, probe)
    bound = _check_binding(report, instance)
    if bound is None:
        return report
    binding, data_directory = bound
    _check_audiences(report, instance)
    _check_running_code(report, data_directory)
    package_health.apply_package_health(report, instance, probe)
    _check_runtime_config_present(report, data_directory)
    _check_vector_threshold(report, binding, data_directory)

    readable = _check_storage(report, binding, data_directory)
    config = _runtime_config(data_directory)
    _check_footprint(report, data_directory, config)
    if readable:
        _check_worker_status(report, binding, data_directory)
        wake_seconds = _check_autostart(report, binding, data_directory)
        _check_supervisor(report, data_directory)
        _check_schema(report)
        _check_backlog(report, wake_seconds)
        _check_embedding_health(report, config)
        _check_embedding_respace(report, config)
        _check_unreached(report, config)
        _check_candidates(report)
        _check_model_output(report)
        _check_ledger(report)
        _classify_status(report)
    _check_index(report, data_directory, store_readable=readable, config=config)
    return report
