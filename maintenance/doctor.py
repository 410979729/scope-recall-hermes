"""Read-only installation diagnostics for v1.1 host wrappers.

``run_doctor`` drives a sequence of named checks. Each check records its own
``checks`` row and capability gaps on the report; the driver only decides how
far into the instance the checks can get. Nothing here writes to the instance.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path
from typing import Any

import scope_recall
from scope_recall._version import __version__
from scope_recall.adapters.clients.config import load_codex_config, load_shared_client
from scope_recall.adapters.hermes.installation import read_attachment
from scope_recall.runtime.instance import RuntimeInstanceConfig
from scope_recall.runtime.model_budget import pre_request_refusals, provider_refusals
from scope_recall.runtime.running_code import live_records, stale_records

from . import package_health
from .doctor_report import DoctorReport, HostChoice, record_check
from .doctor_store import (
    check_backlog,
    check_candidates,
    check_embedding_health,
    check_embedding_respace,
    check_footprint,
    check_index,
    check_ledger,
    check_model_output,
    check_runtime_config_present,
    check_schema,
    check_storage,
    check_unreached,
)
from .install_common import RUNTIME_CONFIG_LIMIT

#: Run as a file by the target interpreter, so an installed package that
#: predates these diagnostics is still measured. It imports no optional library.
_PACKAGE_PROBE = Path(__file__).with_name("package_health.py")
#: Largest JSON control file the doctor will read from beside the store.
_CONTROL_FILE_LIMIT = 65536
#: A runtime config may weigh what the shared commands allow it to.
_RUNTIME_CONFIG_LIMIT = RUNTIME_CONFIG_LIMIT


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


def load_binding(host: HostChoice, instance_root: Path):
    if host == "hermes":
        from scope_recall.adapters.hermes.installation import load_binding_for_home

        manifest = load_binding_for_home(instance_root)
        return manifest.to_binding(), manifest.data_directory

    path = _codex_config_path(instance_root)
    config = load_shared_client(instance_root, host) if path.name == "attachment.json" else load_codex_config(path)
    return config.to_binding(), config.data_directory


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
        record_check(report, "running_code", "unreadable", type(exc).__name__)
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
        record_check(report, "running_code", "stale", ",".join(str(item["pid"]) for item in stale))
        return
    # An empty list is not a clean bill of health: a host registers when it binds
    # an identity for a session, so one that has started and had no conversation
    # yet is simply not here. Saying "ok" would invite the opposite reading.
    record_check(report, "running_code", "ok" if records else "no_records", str(len(records)))


def check_host_registration(host: str, instance: Path, python_executable: Path | None = None) -> str:
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
    report.host_registration_status = check_host_registration(report.host, instance, python_executable)
    report.hook_trust_status = "pending" if report.host == "codex" else "unknown"
    record_check(report, "host_registration", report.host_registration_status)
    if report.host_registration_status not in {"registered", "pending"}:
        report.capability_gaps.append("host_registration_incomplete")
    if report.host == "codex":
        record_check(report, "hook_trust", "pending")


def _check_python_package(report: DoctorReport, python: Path) -> dict[str, Any]:
    """Measure the package the target interpreter loads; returns its probe.

    The interpreter is probed as given: resolving a venv's ``bin/python``
    symlink would probe the base interpreter, which has no venv on its path
    and would report the package missing from an install that is fine.
    """
    python = python.expanduser()
    if not python.is_absolute():
        raise ValueError("python_executable must be absolute")
    if not python.is_file():
        report.capability_gaps.append("python_executable_missing")
        record_check(report, "python_executable", "missing")
        return {}
    report.python_executable = str(python)
    probe = _probe_python_package(python)
    if not probe:
        report.capability_gaps.append("python_package_missing")
        record_check(report, "python_package", "missing")
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
    record_check(report, "python_package", "ok" if report.package_ok else "mismatch", report.package_version)
    return probe


def _check_current_package(report: DoctorReport) -> dict[str, Any]:
    """Without a target interpreter the checker's own package is the one under test."""
    report.package_ok = True
    report.package_version = __version__
    report.package_path = str(Path(__file__).resolve())
    location = os.path.normcase(getattr(scope_recall, "__file__", "") or "").replace("\\", "/")
    report.package_source = "installed" if "site-packages" in location or "dist-packages" in location else "development"
    record_check(report, "package", "ok", report.package_source)
    return package_health.package_probe()


def _check_binding(report: DoctorReport, instance: Path):
    """The adapter binding and its data directory; None when the instance has no usable one."""
    config_path = _hermes_config_path(instance) if report.host == "hermes" else _codex_config_path(instance)
    if not config_path.is_file():
        report.capability_gaps.append("installation_config_missing")
        record_check(report, "adapter_config", "missing")
        return None
    try:
        binding, data_directory = load_binding(report.host, instance)
    except Exception as exc:  # noqa: BLE001 - a broken binding is a finding, not a crash.
        report.capability_gaps.append(f"binding_invalid:{type(exc).__name__}")
        record_check(report, "adapter_binding", "invalid", type(exc).__name__)
        return None
    report.binding_ok = True
    record_check(report, "adapter_binding", "ok", binding.installation_id)
    if binding.installation_kind == "shared":
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
    route stays healthy.  Counted by platform, never named by user.
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
        record_check(
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

    A supervisor stands down after consecutive hard worker failures, not one, and
    stays down until the next autostart wake; nothing else says so, and a stopped
    loop leaves its queue waiting.  Either state is reported here: a loop that stood down
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
    record_check(report, "supervisor", state or "unknown", f"drains={newest.get('drains')} failures={failures}")
    if state == "failed":
        report.capability_gaps.append(f"supervisor_stood_down:{exit_code if exit_code is not None else 'unknown'}")
    elif failures:
        report.capability_gaps.append(f"worker_failures:{int(failures)}")


def _check_autostart(report: DoctorReport, binding, data_directory: Path) -> float | None:
    """Autostart registration, plus the budget state its runtime config points at.

    Returns the configured supervisor wake interval for the stall window, or
    None when autostart is absent or its config could not be read.
    """
    from ..runtime.scheduling import read_control
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
    try:
        raw = _read_control_file(data_directory / "runtime-config.json")
        return None if raw is None else RuntimeInstanceConfig.from_mapping(raw)
    except Exception:  # noqa: BLE001 - reporting must not fail the report.
        return None


def _check_vector_threshold(report: DoctorReport, binding, data_directory: Path) -> None:
    """Vector recall that is wired but admits nothing, named instead of silent.

    Reads the ``runtime-config.json`` every host loads by default.  With a
    vector store and an approved embedding route but no ``vector_threshold``,
    sources and queries are still embedded while recall refuses every vector hit
    as ``vector_threshold_unconfigured``.  No threshold is assumed here.
    """
    try:
        raw = _read_control_file(data_directory / "runtime-config.json")
        config = None if raw is None else RuntimeInstanceConfig.from_mapping(raw)
    except Exception as exc:  # noqa: BLE001 - an unusable config is a finding, not a crash.
        record_check(report, "vector_threshold", "invalid", type(exc).__name__)
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
        record_check(report, "vector_threshold", "configured", str(config.vector_threshold))
        return
    model = str(config.embedding_space()["model"])[:64]
    report.capability_gaps.append("vector_threshold_unconfigured")
    record_check(
        report,
        "vector_threshold",
        "unconfigured",
        f"runtime-config.json configures vector recall with embedding model {model} but no "
        "vector_threshold; every vector hit is refused as vector_threshold_unconfigured and recall "
        "is lexical only until a threshold calibrated for this model is set",
    )


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
    check_runtime_config_present(report, data_directory)
    _check_vector_threshold(report, binding, data_directory)

    readable = check_storage(report, binding, data_directory)
    config = _runtime_config(data_directory)
    check_footprint(report, data_directory, config)
    if readable:
        _check_worker_status(report, binding, data_directory)
        wake_seconds = _check_autostart(report, binding, data_directory)
        _check_supervisor(report, data_directory)
        check_schema(report)
        check_backlog(report, wake_seconds)
        check_embedding_health(report, config)
        check_embedding_respace(report, config)
        check_unreached(report, config)
        check_candidates(report)
        check_model_output(report)
        check_ledger(report)
        _classify_status(report)
    check_index(report, data_directory, store_readable=readable, config=config)
    return report
