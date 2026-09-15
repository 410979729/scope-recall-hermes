#!/usr/bin/env python3
"""Generate final local validation evidence for one built release candidate."""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from types import ModuleType
from typing import Callable, Mapping, NamedTuple, Sequence

try:
    from scripts.execution_boundary import (  # pyright: ignore[reportMissingImports]
        validate_execution_boundary,
    )
except ModuleNotFoundError as exc:
    if exc.name not in {"scripts", "scripts.execution_boundary"}:
        raise
    from execution_boundary import (  # pyright: ignore[reportMissingImports]
        validate_execution_boundary,
    )


SCHEMA_VERSION = "scope-recall.release-validation.v1"
RECEIPT_SCHEMA_VERSION = "scope-recall.validation-receipt.v1"
INSTALL_RECEIPT_SCHEMA_VERSION = "scope-recall.artifact-install-receipt.v1"
FULL_TEST_TIMEOUT_SECONDS = 900
STAGE_TIMEOUT_SECONDS = 300
INSTALL_TIMEOUT_SECONDS = 1800
# Native Windows ``venv`` creation can spend several minutes in ensurepip and
# endpoint-security scanning even when subsequent installs are fast.  Keep a
# separately tunable, genuinely enforced process-tree bound for that stage.
VENV_TIMEOUT_SECONDS = 1800
PROCESS_TREE_TERMINATION_TIMEOUT_SECONDS = 30
N_MINUS_ONE_VERSION = "1.10.3"
N_MINUS_ONE_WINDOW_SCHEMA_VERSION = "scope-recall.n-minus-one-window.v1"
ISSUE_51_DETAILS_SCHEMA_VERSION = "scope-recall.issue-51-regression-details.v1"
ISSUE_51_DETAILS_OUTPUT_ENV = "SCOPE_RECALL_ISSUE_51_DETAILS_OUTPUT"
ISSUE_60_SCHEMA_VERSION = "scope-recall.issue-60-regression.v1"
ISSUE_60_DETAILS_OUTPUT_ENV = "SCOPE_RECALL_ISSUE_60_DETAILS_OUTPUT"
ISSUE_61_SCHEMA_VERSION = "scope-recall.issue-61-applicability.v1"
WRITER_HANDOFF_SCHEMA_VERSION = "scope-recall.writer-lease-handoff.v1"
WRITER_HANDOFF_DETAILS_SCHEMA_VERSION = (
    "scope-recall.writer-lease-handoff-details.v1"
)
WRITER_HANDOFF_DETAILS_OUTPUT_ENV = "SCOPE_RECALL_WRITER_HANDOFF_DETAILS_OUTPUT"
WRITER_HANDOFF_STAGES = (
    "initial_owner",
    "peer_reader",
    "idle_fence",
    "all_work_quiescent",
    "os_lease_released",
    "peer_promoted",
    "peer_write_committed",
    "former_owner_remains_reader",
)
REHEARSAL_RECEIPTS: dict[str, tuple[str, ...]] = {
    "MIGRATION_N_MINUS_ONE.json": (
        "tests/test_release_candidate_rehearsals.py::test_activity_snapshot_upgrade_preserves_source_and_payload",
        "tests/test_sqlite_backup.py::test_verified_online_backup_healthy_path_records_health_and_logical_equivalence",
        "tests/test_upgrade_compatibility.py::test_n_minus_one_isolation_key_passes_read_only_upgrade_preflight",
    ),
    "MIGRATION_N.json": (
        "tests/test_schema_migrations.py::test_relation_policy_generation_migration_upgrades_pre_0014_in_place",
        "tests/test_fact_executor.py::test_add_applies_all_mandatory_surfaces_in_one_committed_transaction",
    ),
    "DOWNGRADE_N_MINUS_ONE.json": (
        "tests/test_fact_authority_router.py::test_missing_unknown_legacy_aliases_and_projection_marker_never_authorize_claim",
    ),
    "PURGE_RESTORE_REPLAY.json": (
        "tests/test_privacy_purge.py::test_restore_replay_reinstates_deny_before_writer_use",
    ),
    "READONLY_CANARY.json": (
        "tests/test_readonly_follower_tools.py::test_readonly_follower_default_denies_writes_and_unknown_tools",
    ),
    "WRITER_CANARY.json": (
        "tests/test_installer.py::test_installer_runtime_verify_loads_provider_tools_and_schema",
        "tests/test_installer.py::test_installed_plugin_loads_through_hermes_memory_discovery",
    ),
    "ROLLBACK_REHEARSAL.json": (
        "tests/test_installer.py::test_installer_rollback_restores_backup_and_backs_up_current_plugin",
    ),
    "ISSUE_51_REGRESSION.json": (
        "tests/test_issue_51_regression.py::test_issue_51_regression",
        "tests/test_relation_rebuild_retirement.py::test_every_legacy_execution_surface_is_a_zero_write_refusal",
        "tests/test_relation_policy_generation.py::test_cap_plus_one_blocks_whole_generation_without_partial_items",
        "tests/test_relation_cleanup.py::test_cleanup_apply_is_backup_first_committed_and_idempotently_replayed",
    ),
    "ISSUE_60_REGRESSION.json": (
        "tests/test_issue_60_regression.py::test_issue_60_retry_due_time_is_bounded_and_healthy_work_is_not_starved",
        "tests/test_issue_60_regression.py::test_issue_60_sixty_one_idle_ticks_do_not_restore_one_second_hammer",
        "tests/test_issue_60_regression.py::test_issue_60_maintenance_is_nonblocking_and_prefetch_is_bounded_zero_write",
    ),
    "WRITER_LEASE_HANDOFF_REHEARSAL.json": (
        "tests/test_writer_idle_handoff.py::test_process_wide_idle_handoff_allows_real_second_process_commit",
        "tests/test_writer_idle_handoff.py::test_extra_connection_pin_vetoes_process_handoff",
        "tests/test_writer_idle_handoff.py::test_active_same_process_peer_vetoes_process_handoff",
        "tests/test_writer_idle_handoff.py::test_writer_loop_automatically_schedules_idle_handoff",
        "tests/test_writer_idle_handoff.py::test_wall_clock_rollback_cannot_extend_process_cooldown",
        "tests/test_writer_idle_handoff.py::test_activity_generation_change_aborts_and_resumes_all_writers",
        "tests/test_writer_idle_handoff.py::test_capture_enqueue_racing_idle_fence_aborts_without_losing_work",
        "tests/test_writer_idle_handoff.py::test_direct_tool_write_started_before_fence_commits_and_vetoes_handoff",
        "tests/test_writer_idle_handoff.py::test_activity_after_final_check_linearizes_after_release_and_promotes",
        "tests/test_writer_idle_handoff.py::test_resource_close_failure_retains_authority_and_never_reports_reader",
        "tests/test_writer_idle_handoff.py::test_writer_connection_close_failure_restores_healthy_owner",
        "tests/test_writer_idle_handoff.py::test_connection_pin_close_failure_is_retried_before_owner_restore",
        "tests/test_writer_idle_handoff.py::test_os_lease_release_failure_fences_process_and_requires_restart",
        "tests/test_writer_idle_handoff.py::test_writer_restore_failure_stays_owner_degraded_and_fenced",
        "tests/test_writer_idle_handoff.py::test_process_fence_refuses_new_same_process_join",
        "tests/test_writer_idle_handoff.py::test_truth_work_started_after_process_fence_cannot_inherit_old_authority",
        "tests/test_writer_idle_handoff.py::test_handoff_thread_cannot_join_a_new_named_holder",
        "tests/test_writer_idle_handoff.py::test_handoff_recovery_pin_cannot_create_missing_authority",
        "tests/test_writer_idle_handoff.py::test_only_handoff_thread_may_join_existing_recovery_pin",
        "tests/test_writer_idle_handoff.py::test_every_busy_surface_vetoes_idle_handoff",
        "tests/test_writer_idle_handoff.py::test_quiesce_failure_restores_owner_without_releasing_authority",
        "tests/test_writer_idle_handoff.py::test_abort_resume_failure_is_owner_degraded_and_logs_only_fixed_codes",
        "tests/test_writer_idle_handoff.py::test_resume_waits_for_prior_sentinel_consumer_before_starting_replacement",
        "tests/test_writer_idle_handoff.py::test_read_only_reopen_failure_never_resurrects_released_writer",
        "tests/test_writer_idle_handoff.py::test_failure_telemetry_never_exposes_exception_text_or_local_path",
        "tests/test_writer_idle_handoff.py::test_successful_promotion_clears_recoverable_reader_degradation",
        "tests/test_writer_idle_handoff.py::test_twenty_reader_writer_round_trips_do_not_leak_process_authority",
        "tests/test_writer_idle_handoff.py::test_stats_exposes_content_free_writer_handoff_observability",
        "tests/test_writer_idle_handoff.py::test_idle_release_config_accepts_disabled_or_bounded_values",
        "tests/test_writer_idle_handoff.py::test_idle_release_config_rejects_ambiguous_or_unbounded_values",
        "tests/test_writer_idle_handoff.py::test_user_activity_generation_veto_is_content_free",
        "tests/test_direct_command_writer_admission.py::test_direct_command_update_started_before_fence_commits_and_vetoes_handoff",
        "tests/test_direct_command_writer_admission.py::test_direct_command_update_started_after_fence_is_rejected",
        "tests/test_direct_command_writer_admission.py::test_direct_command_merge_preserves_capture_barrier",
        "tests/test_direct_command_writer_admission.py::test_direct_command_archive_is_accounted_as_truth_work",
        "tests/test_direct_command_writer_admission.py::test_direct_command_delete_is_accounted_as_truth_work",
        "tests/test_direct_command_writer_admission.py::test_direct_command_feedback_is_accounted_as_truth_work",
        "tests/test_direct_command_writer_admission.py::test_direct_command_govern_apply_is_accounted_as_truth_work",
        "tests/test_direct_command_writer_admission.py::test_direct_command_dedupe_apply_is_accounted_as_truth_work",
        "tests/test_direct_command_writer_admission.py::test_direct_command_repair_is_accounted_as_truth_work",
        "tests/test_direct_command_writer_admission.py::test_direct_command_purge_deny_is_accounted_as_truth_work",
        "tests/test_direct_command_writer_admission.py::test_direct_command_purge_erase_is_accounted_as_truth_work",
        "tests/test_direct_command_writer_admission.py::test_direct_command_read_only_modes_do_not_require_write_authority",
        "tests/test_direct_command_writer_admission.py::test_memory_mutation_service_rejects_fenced_unadmitted_mutation",
        "tests/test_direct_command_writer_admission.py::test_memory_mutation_service_rejects_unclassified_non_owner",
        "tests/test_direct_command_writer_admission.py::test_private_admission_surface_rejects_public_bool_and_wrong_token",
        "tests/test_direct_command_writer_admission.py::test_accepted_capture_commits_after_shutdown_and_handoff_fences",
        "tests/test_direct_command_writer_admission.py::test_admitted_truth_mutation_token_is_cleared_after_exception",
        "tests/test_direct_command_writer_admission.py::test_public_store_reuses_one_active_truth_unit_and_generation",
        "tests/test_direct_command_writer_admission.py::test_govern_acquires_query_connection_under_query_lock",
        "tests/test_direct_command_writer_admission.py::test_tool_service_dry_run_governance_does_not_request_write_access",
        "tests/test_direct_command_writer_admission.py::test_fact_proposal_dry_run_is_read_only_on_reader",
        "tests/test_direct_command_writer_admission.py::test_fact_proposal_apply_reenters_command_gate_and_rejects_reader_or_fence",
        "tests/test_direct_command_writer_admission.py::test_nested_command_gate_preserves_barrier_and_propagates_exceptions",
        "tests/test_direct_command_writer_admission.py::test_public_provider_command_surface_uses_unified_admission",
        "tests/test_writer_handoff_telemetry.py::test_new_authority_epoch_fences_delayed_old_reader_update",
        "tests/test_writer_handoff_telemetry.py::test_initial_epoch_claim_linearizes_with_final_lease_release",
        "tests/test_writer_handoff_telemetry.py::test_epoch_claim_never_takes_activity_lock_under_process_state_lock",
        "tests/test_writer_handoff_telemetry.py::test_delayed_activity_cannot_overwrite_final_shutdown_with_owner_snapshot",
        "tests/test_writer_handoff_telemetry.py::test_missing_invalid_and_stale_snapshots_are_explicitly_unobserved",
        "tests/test_writer_handoff_telemetry.py::test_fresh_snapshot_activity_ages_advance_at_read_time",
        "tests/test_writer_handoff_telemetry.py::test_runtime_writes_only_real_activity_or_state_events",
        "tests/test_writer_handoff_telemetry.py::test_telemetry_failure_never_changes_writer_authority",
        "tests/test_writer_handoff_telemetry.py::test_same_process_holders_share_epoch_until_final_release",
        "tests/test_writer_handoff_telemetry.py::test_doctor_and_dashboard_expose_all_fresh_persisted_fields",
        "tests/test_writer_handoff_activity.py::test_journal_append_cannot_cross_handoff_fence_after_lifecycle_precheck",
        "tests/test_writer_handoff_activity.py::test_relation_maintenance_real_sqlite_mutation_refreshes_truth_activity",
        "tests/test_writer_handoff_activity.py::test_independent_digest_sqlite_mutation_refreshes_truth_activity",
        "tests/test_writer_handoff_activity.py::test_noop_digest_does_not_refresh_truth_activity",
    ),
}
ISOLATION_NODES = (
    "tests/test_execution_boundary.py",
    "tests/test_home_cleanup_receipt.py",
)
ARTIFACT_HARNESS_FILES = tuple(
    sorted(
        {
            Path(node_id.split("::", 1)[0]).as_posix()
            for node_ids in REHEARSAL_RECEIPTS.values()
            for node_id in node_ids
        }
        | {"tests/plugin_source.py"}
    )
)


class ReleaseValidationError(RuntimeError):
    """Raised when final local validation cannot produce honest evidence."""


class ValidationContext(NamedTuple):
    source_commit: str
    source_tree: str
    wheel_sha256: str
    sdist_sha256: str
    wheel_name: str = "candidate.whl"
    wheel_relative_path: str = "artifacts/candidate.whl"


def _load_script(path: Path, name: str) -> ModuleType:
    project_root = path.resolve(strict=True).parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ReleaseValidationError(f"cannot load validation helper: {path.name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseValidationError(
            f"cannot read validation input {path.name}: {type(exc).__name__}"
        ) from exc
    if not isinstance(payload, dict):
        raise ReleaseValidationError(f"validation input is not an object: {path.name}")
    return payload


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_sha256(value: object) -> str:
    return _sha256_bytes(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _artifact_sha(provenance: Mapping[str, object], kind: str) -> str:
    raw = provenance.get(kind)
    if not isinstance(raw, dict):
        raise ReleaseValidationError(f"build provenance {kind} is missing")
    value = str(raw.get("sha256") or "")
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ReleaseValidationError(f"build provenance {kind} digest is invalid")
    return value


def _artifact_descriptor(
    provenance: Mapping[str, object],
    kind: str,
) -> tuple[str, str, str]:
    raw = provenance.get(kind)
    if not isinstance(raw, dict):
        raise ReleaseValidationError(f"build provenance {kind} is missing")
    name = str(raw.get("name") or "")
    relative_path = str(raw.get("relative_path") or "").replace("\\", "/")
    pure = Path(relative_path)
    if (
        not name
        or not relative_path
        or pure.is_absolute()
        or ".." in pure.parts
        or pure.name != name
    ):
        raise ReleaseValidationError(f"build provenance {kind} path is invalid")
    return name, relative_path, _artifact_sha(provenance, kind)


def _validation_context(
    root: Path,
    evidence_dir: Path,
    expected_sha: str,
) -> ValidationContext:
    if evidence_dir.name != expected_sha:
        raise ReleaseValidationError("evidence directory is not the exact SHA directory")
    candidate = _load_script(
        root / "scripts" / "report.candidate_manifest.py",
        "scope_recall_validation_candidate_manifest",
    )
    identity = candidate._repository_identity(root, require_clean=True)
    if not isinstance(identity, dict):
        raise ReleaseValidationError("candidate source identity is invalid")
    commit = str(identity.get("commit") or "")
    tree = str(identity.get("tree") or "")
    if commit != expected_sha:
        raise ReleaseValidationError("candidate source does not match expected SHA")
    source = _load_json(evidence_dir / "SOURCE_IDENTITY.json")
    provenance = _load_json(evidence_dir / "BUILD_PROVENANCE.json")
    if source.get("source_commit") != commit or source.get("source_tree") != tree:
        raise ReleaseValidationError("source identity evidence differs from current source")
    if provenance.get("source_commit") != commit or provenance.get("source_tree") != tree:
        raise ReleaseValidationError("build provenance differs from current source")
    if source.get("source_dirty") is not False or provenance.get("source_dirty") is not False:
        raise ReleaseValidationError("source evidence is not clean")
    wheel_name, wheel_relative_path, wheel_sha256 = _artifact_descriptor(
        provenance,
        "wheel",
    )
    return ValidationContext(
        source_commit=commit,
        source_tree=tree,
        wheel_sha256=wheel_sha256,
        sdist_sha256=_artifact_sha(provenance, "sdist"),
        wheel_name=wheel_name,
        wheel_relative_path=wheel_relative_path,
    )


def _isolated_environment(
    boundary: Path,
    *,
    active_hermes_home: Path,
    real_home: Path,
) -> dict[str, str]:
    targets = {
        "HOME": boundary / "user-home",
        "USERPROFILE": boundary / "user-home",
        "APPDATA": boundary / "appdata",
        "LOCALAPPDATA": boundary / "local-appdata",
        "TEMP": boundary / "temp",
        "TMP": boundary / "temp",
        "XDG_CONFIG_HOME": boundary / "xdg-config",
        "XDG_CACHE_HOME": boundary / "xdg-cache",
        "PIP_CACHE_DIR": boundary / "pip-cache",
        "SCOPE_RECALL_TEST_BOUNDARY_PARENT": boundary / "p",
        "HERMES_HOME": boundary / "hermes-home",
        "SCOPE_RECALL_DB": boundary / "truth" / "memory.sqlite3",
        "SCOPE_RECALL_LOG_DIR": boundary / "logs",
        "SCOPE_RECALL_LEASE_DIR": boundary / "leases",
        "SCOPE_RECALL_PLUGIN_DIR": boundary
        / "hermes-home"
        / "plugins"
        / "scope-recall",
    }
    validate_execution_boundary(
        isolated_root=boundary,
        targets=targets,
        active_hermes_home=active_hermes_home,
        real_home=real_home,
    )
    for name, path in targets.items():
        if name in {"SCOPE_RECALL_DB", "SCOPE_RECALL_PLUGIN_DIR"}:
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            path.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    for inherited in (
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTEST_ADDOPTS",
        "PYTEST_PLUGINS",
    ):
        environment.pop(inherited, None)
    environment.update({name: str(path) for name, path in targets.items()})
    environment.update(
        {
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "SCOPE_RECALL_ACTIVE_HERMES_HOME": str(active_hermes_home),
            "SCOPE_RECALL_REAL_HOME": str(real_home),
        }
    )
    return environment


def _pytest_basetemp(environment: Mapping[str, str], name: str) -> Path:
    parent_text = str(environment.get("SCOPE_RECALL_TEST_BOUNDARY_PARENT") or "").strip()
    if not parent_text:
        raise ReleaseValidationError("pytest boundary parent is not declared")
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ReleaseValidationError("pytest basetemp name is not a single path segment")
    return Path(parent_text).resolve(strict=False) / name


def _validation_cleanup_path(boundary: Path) -> Path:
    """Return the repository filesystem helper's long-path-safe I/O path."""

    if os.name != "nt":
        return boundary
    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    from windows_filesystem import io_path  # pyright: ignore[reportMissingImports]

    return Path(io_path(boundary))


def _retry_readonly_cleanup(
    boundary: Path,
    function: Callable[[str], object],
    path: str,
    error: BaseException,
) -> None:
    """Repair owner permissions only inside the declared temp boundary."""

    if not isinstance(error, PermissionError) and getattr(error, "winerror", None) != 5:
        raise error
    candidate = Path(os.path.abspath(path))
    try:
        candidate.relative_to(boundary)
    except ValueError as exc:
        raise ReleaseValidationError(
            "refusing to repair permissions outside the validation boundary"
        ) from exc
    is_junction = getattr(os.path, "isjunction", None)
    lineage: list[Path] = []
    current = candidate
    while True:
        lineage.append(current)
        if current == boundary:
            break
        current = current.parent
    # Walk from the accessible boundary toward the failed leaf.  On POSIX
    # 3.11, even lstat/is_symlink on the leaf fails while an ancestor lacks
    # execute permission, so every ancestor must be inspected and repaired in
    # this order.  lstat keeps the symlink check non-following.
    for item in reversed(lineage):
        item_stat = os.lstat(item)
        if stat.S_ISLNK(item_stat.st_mode) or (
            callable(is_junction) and bool(is_junction(item))
        ):
            raise error
        if os.name != "nt":
            owner_bits = stat.S_IRUSR | stat.S_IWUSR
            if stat.S_ISDIR(item_stat.st_mode):
                owner_bits |= stat.S_IXUSR
            os.chmod(
                item,
                stat.S_IMODE(item_stat.st_mode) | owner_bits,
                follow_symlinks=False,
            )
    if os.name == "nt":
        os.chmod(candidate, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    function(str(candidate))


def _cleanup_validation_boundary(boundary: Path, *, attempts: int = 8) -> None:
    resolved = boundary.resolve(strict=False)
    temp_root = Path(tempfile.gettempdir()).resolve(strict=False)
    if resolved.parent != temp_root or not resolved.name.startswith("srv."):
        raise ReleaseValidationError("refusing to clean an undeclared validation boundary")
    cleanup_target = _validation_cleanup_path(resolved)

    def handle_cleanup_error(function, path, exc_info):  # noqa: ANN001
        _retry_readonly_cleanup(cleanup_target, function, path, exc_info[1])

    for attempt in range(attempts):
        try:
            shutil.rmtree(
                cleanup_target,
                onerror=handle_cleanup_error,
            )
            if os.path.lexists(cleanup_target):
                raise OSError("validation boundary still exists after cleanup")
            return
        except FileNotFoundError:
            if not os.path.lexists(cleanup_target):
                return
            if attempt + 1 >= attempts:
                raise
            time.sleep(min(0.1 * (attempt + 1), 0.5))
        except OSError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(min(0.1 * (attempt + 1), 0.5))


@contextmanager
def _temporary_validation_boundary() -> Iterator[Path]:
    boundary = Path(tempfile.mkdtemp(prefix="srv."))
    try:
        yield boundary
    finally:
        pending = sys.exception()
        try:
            _cleanup_validation_boundary(boundary)
        except OSError as cleanup_error:
            if pending is None:
                raise
            pending.add_note(f"validation boundary cleanup also failed: {cleanup_error}")


def _text_output(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _terminate_process_tree(
    process: subprocess.Popen[str],
    *,
    platform: str | None = None,
) -> None:
    """Boundedly terminate an exact validation subprocess and its descendants."""

    if process.poll() is not None:
        return
    current_platform = os.name if platform is None else platform
    if current_platform == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=PROCESS_TREE_TERMINATION_TIMEOUT_SECONDS,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        kill_group = getattr(os, "killpg", None)
        kill_signal = getattr(signal, "SIGKILL", None)
        if callable(kill_group) and isinstance(kill_signal, int):
            try:
                kill_group(process.pid, kill_signal)
            except OSError:
                pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass


def _run(
    command: Sequence[str],
    *,
    display_command: Sequence[str],
    cwd: Path,
    environment: Mapping[str, str],
    timeout_seconds: int,
    log_path: Path,
    ledger: list[dict[str, object]],
) -> dict[str, object]:
    started_at = _utc_now()
    started = time.monotonic()
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            env=dict(environment),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )
        try:
            output, _ = process.communicate(timeout=timeout_seconds)
            exit_code = int(process.returncode or 0)
        except subprocess.TimeoutExpired as exc:
            partial_output = _text_output(exc.stdout)
            _terminate_process_tree(process)
            try:
                final_output, _ = process.communicate(
                    timeout=PROCESS_TREE_TERMINATION_TIMEOUT_SECONDS
                )
            except subprocess.TimeoutExpired:
                _terminate_process_tree(process)
                final_output = ""
                if process.stdout is not None:
                    process.stdout.close()
            output = _text_output(final_output) or partial_output
            exit_code = 124
    except OSError as exc:
        output = f"{type(exc).__name__}\n"
        exit_code = 125
    finished_at = _utc_now()
    duration = round(time.monotonic() - started, 3)
    log_path.write_text(output, encoding="utf-8", newline="\n")
    stage = {
        "command": list(display_command),
        "timeout_seconds": timeout_seconds,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": duration,
        "exit_code": exit_code,
        "log": log_path.name,
        "log_sha256": _sha256(log_path),
    }
    ledger.append(stage)
    if exit_code != 0:
        raise ReleaseValidationError(
            f"validation command failed with exit code {exit_code}: {display_command[0]}"
        )
    return stage


_INSTALL_PROBE = r"""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import scope_recall

def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def canonical(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

source_root = Path(os.environ["SCOPE_RECALL_ARTIFACT_SOURCE_ROOT"]).resolve()
module_file = Path(scope_recall.__file__).resolve()
dist = importlib.metadata.distribution("hermes-scope-recall")
package_root = module_file.parent
source_on_path = any(Path(item or os.curdir).resolve() == source_root for item in sys.path)
source_imported = module_file.is_relative_to(source_root)
entries = []
for path in sorted(package_root.rglob("*")):
    if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
        continue
    relative = path.relative_to(package_root).as_posix()
    entries.append({"path": relative, "sha256": sha(path), "size_bytes": path.stat().st_size})
record_candidates = [item for item in (dist.files or ()) if item.as_posix().endswith(".dist-info/RECORD")]
if len(record_candidates) != 1:
    raise RuntimeError("installed distribution RECORD is ambiguous")
record_path = Path(dist.locate_file(record_candidates[0])).resolve()
direct_text = dist.read_text("direct_url.json") or ""
distributions = sorted(
    {
        str(item.metadata.get("Name") or "").casefold(): item.version
        for item in importlib.metadata.distributions()
        if str(item.metadata.get("Name") or "").strip()
    }.items()
)
payload = {
    "distribution": "hermes-scope-recall",
    "version": dist.version,
    "python_version": sys.version.split()[0],
    "python_executable_sha256": sha(Path(sys.executable)),
    "installed_file_count": len(entries),
    "installed_package_manifest_sha256": canonical(entries),
    "environment_distribution_count": len(distributions),
    "environment_distribution_manifest_sha256": canonical(distributions),
    "record_sha256": sha(record_path),
    "direct_url_sha256": hashlib.sha256(direct_text.encode("utf-8")).hexdigest(),
    "source_worktree_on_sys_path": source_on_path,
    "source_worktree_imported": source_imported,
    "imported_module_path_class": "isolated-site-packages",
}
if source_on_path or source_imported:
    raise RuntimeError("candidate source shadowed installed distribution")
print(json.dumps(payload, sort_keys=True))
"""


def _venv_python(venv_root: Path) -> Path:
    return (
        venv_root / "Scripts" / "python.exe"
        if os.name == "nt"
        else venv_root / "bin" / "python"
    )


def _artifact_install_environment(
    *,
    root: Path,
    artifact: Path,
    artifact_sha256: str,
    expected_version: str,
    label: str,
    workspace: Path,
    staging: Path,
    active_hermes_home: Path,
    include_dev: bool,
    ledger: list[dict[str, object]],
) -> tuple[Path, dict[str, object], str]:
    if not artifact.is_file() or _sha256(artifact) != artifact_sha256:
        raise ReleaseValidationError(f"{label} artifact digest mismatch")
    venv_root = workspace / f"venv-{label}"
    install_boundary = workspace / f"install-{label}"
    environment = _isolated_environment(
        install_boundary,
        active_hermes_home=active_hermes_home,
        real_home=Path.home().resolve(strict=False),
    )
    create_stage = _run(
        [sys.executable, "-B", "-m", "venv", str(venv_root)],
        display_command=["python", "-B", "-m", "venv", f"<isolated-{label}-venv>"],
        cwd=workspace,
        environment=environment,
        timeout_seconds=VENV_TIMEOUT_SECONDS,
        log_path=staging / f"INSTALL_{label.upper()}_VENV.log",
        ledger=ledger,
    )
    python = _venv_python(venv_root)
    install_target = str(artifact) + ("[dev]" if include_dev else "")
    install_stage = _run(
        [
            str(python),
            "-B",
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            install_target,
        ],
        display_command=[
            "python",
            "-B",
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            f"<exact-{label}-wheel>" + ("[dev]" if include_dev else ""),
        ],
        cwd=workspace,
        environment=environment,
        timeout_seconds=INSTALL_TIMEOUT_SECONDS,
        log_path=staging / f"INSTALL_{label.upper()}.log",
        ledger=ledger,
    )
    probe_environment = dict(environment)
    probe_environment["SCOPE_RECALL_ARTIFACT_SOURCE_ROOT"] = str(root)
    probe_log = staging / f"INSTALL_{label.upper()}_PROBE.log"
    probe_stage = _run(
        [str(python), "-B", "-c", _INSTALL_PROBE],
        display_command=["python", "-B", "-c", "<installed-distribution-probe>"],
        cwd=workspace,
        environment=probe_environment,
        timeout_seconds=STAGE_TIMEOUT_SECONDS,
        log_path=probe_log,
        ledger=ledger,
    )
    try:
        probe = json.loads(probe_log.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseValidationError(f"{label} install probe is invalid") from exc
    if not isinstance(probe, dict):
        raise ReleaseValidationError(f"{label} install probe is not an object")
    if probe.get("version") != expected_version:
        raise ReleaseValidationError(f"{label} installed version mismatch")
    if (
        probe.get("source_worktree_imported") is not False
        or probe.get("source_worktree_on_sys_path") is not False
        or probe.get("imported_module_path_class") != "isolated-site-packages"
    ):
        raise ReleaseValidationError(f"{label} install imported the source worktree")
    environment_id = _canonical_sha256(
        {
            "artifact_sha256": artifact_sha256,
            "python_executable_sha256": probe.get("python_executable_sha256"),
            "installed_package_manifest_sha256": probe.get(
                "installed_package_manifest_sha256"
            ),
            "environment_distribution_manifest_sha256": probe.get(
                "environment_distribution_manifest_sha256"
            ),
            "label": label,
        }
    )
    receipt: dict[str, object] = {
        "schema_version": INSTALL_RECEIPT_SCHEMA_VERSION,
        "artifact_kind": "wheel",
        "artifact_name": artifact.name,
        "artifact_sha256": artifact_sha256,
        "installed_distribution": f"hermes-scope-recall=={expected_version}",
        "environment_id": environment_id,
        "python_version": probe.get("python_version"),
        "python_executable_sha256": probe.get("python_executable_sha256"),
        "installed_file_count": probe.get("installed_file_count"),
        "installed_package_manifest_sha256": probe.get(
            "installed_package_manifest_sha256"
        ),
        "environment_distribution_count": probe.get(
            "environment_distribution_count"
        ),
        "environment_distribution_manifest_sha256": probe.get(
            "environment_distribution_manifest_sha256"
        ),
        "record_sha256": probe.get("record_sha256"),
        "direct_url_sha256": probe.get("direct_url_sha256"),
        "imported_module_path_class": "isolated-site-packages",
        "source_worktree_imported": False,
        "source_worktree_on_sys_path": False,
        "venv_stage_sha256": create_stage["log_sha256"],
        "install_stage_sha256": install_stage["log_sha256"],
        "probe_stage_sha256": probe_stage["log_sha256"],
        "started_at": create_stage["started_at"],
        "finished_at": probe_stage["finished_at"],
        "result": "passed",
    }
    receipt_path = staging / f"INSTALL_{label.upper()}_RECEIPT.json"
    _write_json(receipt_path, receipt)
    return python, receipt, _sha256(receipt_path)


def _prepare_artifact_harness(
    *,
    root: Path,
    python: Path,
    workspace: Path,
) -> Path:
    harness = workspace / "artifact-harness"
    tests_dir = harness / "tests"
    tests_dir.mkdir(parents=True)
    for relative in ARTIFACT_HARNESS_FILES:
        source = root.joinpath(*Path(relative).parts)
        if relative == "tests/plugin_source.py":
            target = harness / "plugin_source.py"
        else:
            target = harness.joinpath(*Path(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    package_root = (
        python.parents[1] / "Lib" / "site-packages" / "scope_recall"
        if os.name == "nt"
        else python.parents[1]
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
        / "scope_recall"
    )
    shutil.copy2(package_root / "writer_lease.py", harness / "writer_lease.py")
    (harness / "pytest.ini").write_text(
        "[pytest]\naddopts =\n",
        encoding="utf-8",
        newline="\n",
    )
    (tests_dir / "conftest.py").write_text(
        """from __future__ import annotations

import os
from pathlib import Path

from scope_recall import installer


def pytest_configure(config):
    del config
    home = Path(os.environ["HERMES_HOME"])
    result = installer.install(home)
    if result.get("ok") is not True:
        raise RuntimeError("installed-wheel Hermes discovery setup failed")
""",
        encoding="utf-8",
        newline="\n",
    )
    return harness


def _optional_file_sha256(path: Path) -> str:
    return _sha256(path) if path.is_file() else hashlib.sha256(b"").hexdigest()


def _run_n_minus_one_window_stage(
    *,
    python: Path,
    stage: str,
    expected_version: str,
    install_receipt: Mapping[str, object],
    artifact_sha256: str,
    runner: Path,
    database: Path,
    hermes_home: Path,
    source_root: Path,
    workspace: Path,
    staging: Path,
    environment: Mapping[str, str],
    ledger: list[dict[str, object]],
) -> dict[str, object]:
    """Execute one installed-distribution stage with separate output hashes."""

    output_path = workspace / f"{stage}.json"
    stdout_path = staging / f"N_MINUS_ONE_WINDOW_{stage.upper()}.stdout.log"
    stderr_path = staging / f"N_MINUS_ONE_WINDOW_{stage.upper()}.stderr.log"
    before_sha256 = _optional_file_sha256(database)
    command = [
        str(python),
        "-B",
        str(runner),
        "--stage",
        stage,
        "--database",
        str(database),
        "--hermes-home",
        str(hermes_home),
        "--source-root",
        str(source_root),
        "--expected-version",
        expected_version,
        "--output",
        str(output_path),
    ]
    display_command = [
        "python",
        "-B",
        "<neutral-n-minus-one-window-runner>",
        "--stage",
        stage,
        "--database",
        "<isolated-window-database>",
        "--hermes-home",
        "<isolated-hermes-home>",
        "--source-root",
        "<candidate-source-root>",
        "--expected-version",
        expected_version,
        "--output",
        "<isolated-stage-result>",
    ]
    started_at = _utc_now()
    started = time.monotonic()
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        process = subprocess.Popen(
            command,
            cwd=workspace,
            env=dict(environment),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )
        try:
            captured_stdout, captured_stderr = process.communicate(
                timeout=STAGE_TIMEOUT_SECONDS
            )
            stdout = _text_output(captured_stdout)
            stderr = _text_output(captured_stderr)
            returncode = int(process.returncode or 0)
        except subprocess.TimeoutExpired as exc:
            partial_stdout = _text_output(exc.stdout)
            partial_stderr = _text_output(exc.stderr)
            _terminate_process_tree(process)
            try:
                final_stdout, final_stderr = process.communicate(
                    timeout=PROCESS_TREE_TERMINATION_TIMEOUT_SECONDS
                )
            except subprocess.TimeoutExpired as final_exc:
                _terminate_process_tree(process)
                final_stdout = _text_output(final_exc.stdout)
                final_stderr = _text_output(final_exc.stderr)
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()
            stdout = _text_output(final_stdout) or partial_stdout
            stderr = _text_output(final_stderr) or partial_stderr
            returncode = 124
    except OSError as exc:
        stdout = ""
        stderr = f"{type(exc).__name__}\n"
        returncode = 125
    stdout_path.write_text(stdout, encoding="utf-8", newline="\n")
    stderr_path.write_text(stderr, encoding="utf-8", newline="\n")
    finished_at = _utc_now()
    duration_seconds = round(time.monotonic() - started, 3)
    ledger.append(
        {
            "command": display_command,
            "timeout_seconds": STAGE_TIMEOUT_SECONDS,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": duration_seconds,
            "exit_code": returncode,
            "stdout_log": stdout_path.name,
            "stdout_sha256": _sha256(stdout_path),
            "stderr_log": stderr_path.name,
            "stderr_sha256": _sha256(stderr_path),
        }
    )
    if returncode != 0 or not output_path.is_file():
        raise ReleaseValidationError(f"N-1 window stage failed: {stage}")
    result = _load_json(output_path)
    expected_distribution = f"hermes-scope-recall=={expected_version}"
    if (
        result.get("schema_version") != "scope-recall.n-minus-one-stage.v1"
        or result.get("stage") != stage
        or result.get("result") != "passed"
        or result.get("installed_distribution") != expected_distribution
        or result.get("source_worktree_on_sys_path") is not False
        or result.get("source_worktree_imported") is not False
        or result.get("module_origin_class") != "isolated-site-packages"
    ):
        raise ReleaseValidationError(f"N-1 window stage identity mismatch: {stage}")
    if install_receipt.get("installed_distribution") != expected_distribution:
        raise ReleaseValidationError(f"N-1 window install receipt mismatch: {stage}")
    if result.get("python_executable_sha256") != install_receipt.get(
        "python_executable_sha256"
    ):
        raise ReleaseValidationError(f"N-1 window interpreter mismatch: {stage}")
    if not database.is_file():
        raise ReleaseValidationError(f"N-1 window database missing after stage: {stage}")
    return {
        "stage": stage,
        "python_environment_id": str(install_receipt.get("environment_id") or ""),
        "installed_distribution": expected_distribution,
        "artifact_sha256": artifact_sha256,
        "python_executable_sha256": result["python_executable_sha256"],
        "command_sha256": _canonical_sha256(display_command),
        "stdout_sha256": _sha256(stdout_path),
        "stderr_sha256": _sha256(stderr_path),
        "database_before_sha256": before_sha256,
        "database_after_sha256": _sha256(database),
        "source_worktree_on_sys_path": False,
        "source_worktree_imported": False,
        "returncode": returncode,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": duration_seconds,
        "details": result.get("details"),
    }


def _run_n_minus_one_window(
    *,
    root: Path,
    context: ValidationContext,
    candidate_python: Path,
    candidate_install: Mapping[str, object],
    candidate_install_sha256: str,
    n_minus_one_python: Path,
    n_minus_one_install: Mapping[str, object],
    n_minus_one_install_sha256: str,
    n_minus_one_artifact_sha256: str,
    workspace: Path,
    staging: Path,
    active_hermes_home: Path,
    real_home: Path,
    ledger: list[dict[str, object]],
) -> tuple[dict[str, object], str]:
    """Exercise a single database through real N-1/N/N-1 interpreters."""

    candidate_environment_id = str(candidate_install.get("environment_id") or "")
    n_minus_one_environment_id = str(n_minus_one_install.get("environment_id") or "")
    if not candidate_environment_id or not n_minus_one_environment_id:
        raise ReleaseValidationError("N-1 window install environment identity is missing")
    if candidate_environment_id == n_minus_one_environment_id:
        raise ReleaseValidationError("candidate and N-1 window environments are not distinct")
    window_root = workspace / "n-minus-one-window"
    harness = window_root / "neutral-harness"
    harness.mkdir(parents=True)
    runner = harness / "rehearsal.py"
    shutil.copy2(root / "scripts" / "rehearse_n_minus_one_window.py", runner)
    hermes_home = window_root / "hermes-home"
    hermes_home.mkdir(parents=True)
    n_minus_one_database = hermes_home / "scope-recall" / "memory.sqlite3"
    window_database = window_root / "window.sqlite3"
    base_environment = _isolated_environment(
        window_root / "environment",
        active_hermes_home=active_hermes_home,
        real_home=real_home,
    )
    base_environment["HERMES_HOME"] = str(hermes_home)
    base_environment["SCOPE_RECALL_DB"] = str(window_database)
    base_environment["PYTHONPATH"] = ""

    stages: list[dict[str, object]] = []
    stages.append(
        _run_n_minus_one_window_stage(
            python=n_minus_one_python,
            stage="n_minus_one_create",
            expected_version=N_MINUS_ONE_VERSION,
            install_receipt=n_minus_one_install,
            artifact_sha256=n_minus_one_artifact_sha256,
            runner=runner,
            database=n_minus_one_database,
            hermes_home=hermes_home,
            source_root=root,
            workspace=harness,
            staging=staging,
            environment=base_environment,
            ledger=ledger,
        )
    )
    shutil.copy2(n_minus_one_database, window_database)
    if _sha256(n_minus_one_database) != _sha256(window_database):
        raise ReleaseValidationError("N-1 window verified database copy mismatch")
    stage_specs = (
        (
            candidate_python,
            "candidate_upgrade_write",
            "2.0.1",
            candidate_install,
            context.wheel_sha256,
        ),
        (
            n_minus_one_python,
            "n_minus_one_read_after_n",
            N_MINUS_ONE_VERSION,
            n_minus_one_install,
            n_minus_one_artifact_sha256,
        ),
        (
            candidate_python,
            "candidate_final_verify",
            "2.0.1",
            candidate_install,
            context.wheel_sha256,
        ),
    )
    for python, stage, version, install_receipt, artifact_sha256 in stage_specs:
        stages.append(
            _run_n_minus_one_window_stage(
                python=python,
                stage=stage,
                expected_version=version,
                install_receipt=install_receipt,
                artifact_sha256=artifact_sha256,
                runner=runner,
                database=window_database,
                hermes_home=hermes_home,
                source_root=root,
                workspace=harness,
                staging=staging,
                environment=base_environment,
                ledger=ledger,
            )
        )
    if stages[1]["database_before_sha256"] != stages[0]["database_after_sha256"]:
        raise ReleaseValidationError("N-1 window migration lineage mismatch")
    if stages[2]["database_before_sha256"] != stages[2]["database_after_sha256"]:
        raise ReleaseValidationError("N-1 read-after-N mutated candidate truth")
    if stages[3]["database_before_sha256"] != stages[3]["database_after_sha256"]:
        raise ReleaseValidationError("candidate final verification mutated truth")
    receipt: dict[str, object] = {
        "schema_version": N_MINUS_ONE_WINDOW_SCHEMA_VERSION,
        "candidate_source_commit": context.source_commit,
        "candidate_source_tree": context.source_tree,
        "candidate_install_receipt_sha256": candidate_install_sha256,
        "n_minus_one_install_receipt_sha256": n_minus_one_install_sha256,
        "neutral_runner_sha256": _sha256(runner),
        "database_lineage_id": _canonical_sha256(
            {
                "n_minus_one_created": stages[0]["database_after_sha256"],
                "candidate_upgraded": stages[1]["database_after_sha256"],
            }
        ),
        "stages": stages,
        "candidate_n_minus_one_environment_mixed": False,
        "active_instance_touched": False,
        "result": "passed",
    }
    receipt_path = staging / "N_MINUS_ONE_WINDOW.json"
    _write_json(receipt_path, receipt)
    return receipt, _sha256(receipt_path)


def _copy_hermes_install_source(source: Path, target: Path) -> None:
    """Copy only files declared by Hermes' Python packaging configuration.

    The pinned Hermes checkout also contains large website, application, and test
    trees.  They are irrelevant to an editable Python install and some historical
    translated website paths exceed the default Windows path limit when copied
    below the release-validation boundary.
    """

    source = source.resolve(strict=True)
    pyproject = source / "pyproject.toml"
    try:
        configuration = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseValidationError("pinned Hermes pyproject.toml is invalid") from exc

    project = configuration.get("project")
    tool = configuration.get("tool")
    setuptools = tool.get("setuptools") if isinstance(tool, dict) else None
    if not isinstance(project, dict) or not isinstance(setuptools, dict):
        raise ReleaseValidationError("pinned Hermes setuptools metadata is missing")

    raw_modules = setuptools.get("py-modules")
    packages = setuptools.get("packages")
    find = packages.get("find") if isinstance(packages, dict) else None
    raw_includes = find.get("include") if isinstance(find, dict) else None
    if not isinstance(raw_modules, list) or not raw_modules:
        raise ReleaseValidationError("pinned Hermes py-modules declaration is missing")
    if not isinstance(raw_includes, list) or not raw_includes:
        raise ReleaseValidationError("pinned Hermes package include declaration is missing")

    module_names = sorted({str(item) for item in raw_modules})
    package_roots = sorted({str(item).split(".", 1)[0] for item in raw_includes})
    if any(not name.isidentifier() for name in module_names + package_roots):
        raise ReleaseValidationError("pinned Hermes packaging declaration is unsafe")

    referenced_files = {"pyproject.toml", "setup.py"}
    readme = project.get("readme")
    if isinstance(readme, str):
        referenced_files.add(readme)
    elif isinstance(readme, dict) and isinstance(readme.get("file"), str):
        referenced_files.add(str(readme["file"]))
    license_files = project.get("license-files", [])
    if not isinstance(license_files, list):
        raise ReleaseValidationError("pinned Hermes license-files declaration is invalid")
    referenced_files.update(str(item) for item in license_files)

    target.mkdir(parents=True, exist_ok=False)
    for relative in sorted(referenced_files):
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or len(path.parts) != 1:
            raise ReleaseValidationError("pinned Hermes build file declaration is unsafe")
        source_file = source / path
        if not source_file.is_file():
            raise ReleaseValidationError(f"pinned Hermes build file is missing: {relative}")
        shutil.copy2(source_file, target / path)

    for name in module_names:
        source_file = source / f"{name}.py"
        if not source_file.is_file():
            raise ReleaseValidationError(f"pinned Hermes Python module is missing: {name}")
        shutil.copy2(source_file, target / source_file.name)

    for name in package_roots:
        source_package = source / name
        if not source_package.is_dir():
            raise ReleaseValidationError(f"pinned Hermes package is missing: {name}")
        shutil.copytree(
            source_package,
            target / name,
            ignore=shutil.ignore_patterns(
                ".pytest_cache",
                ".ruff_cache",
                "__pycache__",
                "*.egg-info",
                "*.pyc",
            ),
        )


def _prepare_full_suite_environment(
    *,
    root: Path,
    candidate_wheel: Path,
    hermes_source: Path,
    workspace: Path,
    staging: Path,
    active_hermes_home: Path,
    ledger: list[dict[str, object]],
) -> Path:
    """Create the CI-equivalent interpreter used by source-level full pytest."""

    environment = _isolated_environment(
        workspace / "install-boundary",
        active_hermes_home=active_hermes_home,
        real_home=Path.home().resolve(strict=False),
    )
    environment["PIP_CONSTRAINT"] = str(
        (root / "constraints" / "release.txt").resolve(strict=True)
    )
    venv_root = workspace / "venv"
    _run(
        [sys.executable, "-B", "-m", "venv", str(venv_root)],
        display_command=[
            "python",
            "-B",
            "-m",
            "venv",
            "<isolated-full-suite-venv>",
        ],
        cwd=workspace,
        environment=environment,
        timeout_seconds=VENV_TIMEOUT_SECONDS,
        log_path=staging / "INSTALL_FULL_SUITE_VENV.log",
        ledger=ledger,
    )
    python = _venv_python(venv_root)
    _run(
        [
            str(python),
            "-B",
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            str(candidate_wheel) + "[lancedb,dev]",
        ],
        display_command=[
            "python",
            "-B",
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "<exact-candidate-wheel>[lancedb,dev]",
        ],
        cwd=workspace,
        environment=environment,
        timeout_seconds=INSTALL_TIMEOUT_SECONDS,
        log_path=staging / "INSTALL_FULL_SUITE_PLUGIN.log",
        ledger=ledger,
    )
    hermes_copy = workspace / "hermes-source-copy"
    _copy_hermes_install_source(hermes_source, hermes_copy)
    _run(
        [
            str(python),
            "-B",
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "-e",
            str(hermes_copy),
        ],
        display_command=[
            "python",
            "-B",
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "-e",
            "<pinned-hermes-0.19.1-source>",
        ],
        cwd=workspace,
        environment=environment,
        timeout_seconds=INSTALL_TIMEOUT_SECONDS,
        log_path=staging / "INSTALL_FULL_SUITE_HERMES.log",
        ledger=ledger,
    )
    return python


def _receipt(
    context: ValidationContext,
    *,
    stage: Mapping[str, object],
    command: Sequence[str],
    database_kind: str,
    details: Mapping[str, object] | None = None,
    artifact_contract: Mapping[str, object] | None = None,
    environment_identity: Mapping[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "source_commit": context.source_commit,
        "source_tree": context.source_tree,
        "artifact_sha256": context.wheel_sha256,
        "started_at": stage["started_at"],
        "finished_at": stage["finished_at"],
        "command": list(command),
        "exit_code": stage["exit_code"],
        "environment_boundary": {
            "hermes_home_kind": "isolated",
            "database_kind": database_kind,
            "active_instance_touched": False,
        },
        "result": "passed",
        "raw_log_sha256": stage.get("log_sha256", "not-applicable"),
    }
    if artifact_contract:
        payload.update(dict(artifact_contract))
    if environment_identity:
        boundary = payload["environment_boundary"]
        assert isinstance(boundary, dict)
        boundary.update(dict(environment_identity))
    if details:
        payload["details"] = dict(details)
    return payload


def _run_pytest_receipt(
    *,
    root: Path,
    harness: Path,
    python: Path,
    staging: Path,
    environment: Mapping[str, str],
    context: ValidationContext,
    install_receipt: Mapping[str, object],
    install_receipt_sha256: str,
    hermes_source: Path,
    hermes_source_identity: Mapping[str, object],
    receipt_name: str,
    node_ids: Sequence[str],
    ledger: list[dict[str, object]],
) -> None:
    stem = Path(receipt_name).stem
    log_path = staging / f"{stem}.log"
    guard_output = Path(environment["TEMP"]) / f"{stem}.import-guard.json"
    rehearsal_environment = dict(environment)
    rehearsal_environment.update(
        {
            "PYTHONPATH": os.pathsep.join((str(harness), str(hermes_source))),
            "SCOPE_RECALL_ARTIFACT_GUARD_OUTPUT": str(guard_output),
            "SCOPE_RECALL_ARTIFACT_SOURCE_ROOT": str(root),
            "SCOPE_RECALL_ARTIFACT_SHA256": context.wheel_sha256,
            "SCOPE_RECALL_INSTALL_RECEIPT_SHA256": install_receipt_sha256,
        }
    )
    issue_51_details_path: Path | None = None
    if receipt_name == "ISSUE_51_REGRESSION.json":
        issue_51_details_path = (
            Path(environment["TEMP"]) / "issue-51-regression-details.json"
        )
        rehearsal_environment[ISSUE_51_DETAILS_OUTPUT_ENV] = str(
            issue_51_details_path
        )
    issue_60_details_path: Path | None = None
    if receipt_name == "ISSUE_60_REGRESSION.json":
        issue_60_details_path = (
            Path(environment["TEMP"]) / "issue-60-regression-details.json"
        )
        rehearsal_environment[ISSUE_60_DETAILS_OUTPUT_ENV] = str(
            issue_60_details_path
        )
    writer_handoff_details_path: Path | None = None
    if receipt_name == "WRITER_LEASE_HANDOFF_REHEARSAL.json":
        writer_handoff_details_path = (
            Path(environment["TEMP"]) / "writer-lease-handoff-details.json"
        )
        rehearsal_environment[WRITER_HANDOFF_DETAILS_OUTPUT_ENV] = str(
            writer_handoff_details_path
        )
    actual = [
        str(python),
        "-B",
        "-m",
        "pytest",
        "-c",
        str(harness / "pytest.ini"),
        "--confcutdir",
        str(harness),
        "-p",
        "no:cacheprovider",
        "-p",
        "scope_recall.scripts.release_artifact_test_guard",
        "-q",
        "--basetemp",
        str(
            _pytest_basetemp(
                environment,
                f"r-{hashlib.sha256(stem.encode('utf-8')).hexdigest()[:8]}",
            )
        ),
        *node_ids,
    ]
    display = [
        "python",
        "-B",
        "-m",
        "pytest",
        "-c",
        "<artifact-harness>/pytest.ini",
        "--confcutdir",
        "<artifact-harness>",
        "-p",
        "no:cacheprovider",
        "-p",
        "scope_recall.scripts.release_artifact_test_guard",
        "-q",
        "--basetemp",
        f"<isolated>/{stem.lower()}",
        *node_ids,
    ]
    stage = _run(
        actual,
        display_command=display,
        cwd=harness,
        environment=rehearsal_environment,
        timeout_seconds=STAGE_TIMEOUT_SECONDS,
        log_path=log_path,
        ledger=ledger,
    )
    if not guard_output.is_file():
        raise ReleaseValidationError(f"{receipt_name} import guard did not run")
    guard = _load_json(guard_output)
    if (
        guard.get("result") != "passed"
        or guard.get("artifact_sha256") != context.wheel_sha256
        or guard.get("install_receipt_sha256") != install_receipt_sha256
        or guard.get("source_worktree_imported") is not False
        or guard.get("source_worktree_on_sys_path") is not False
    ):
        raise ReleaseValidationError(f"{receipt_name} import guard failed")
    environment_id = _canonical_sha256(
        {
            "install_environment_id": install_receipt.get("environment_id"),
            "receipt_name": receipt_name,
            "hermes_home": _canonical_sha256(environment["HERMES_HOME"]),
            "database": _canonical_sha256(environment["SCOPE_RECALL_DB"]),
            "basetemp": _canonical_sha256(
                str(
                    _pytest_basetemp(
                        environment,
                        f"r-{hashlib.sha256(stem.encode('utf-8')).hexdigest()[:8]}",
                    )
                )
            ),
        }
    )
    receipt_details: dict[str, object] = {
        "node_ids": list(node_ids),
        "import_guard": guard,
    }
    if issue_51_details_path is not None:
        if not issue_51_details_path.is_file():
            raise ReleaseValidationError(
                "ISSUE_51_REGRESSION.json details output is missing"
            )
        issue_51_details = _load_json(issue_51_details_path)
        if issue_51_details.get("schema_version") != (
            ISSUE_51_DETAILS_SCHEMA_VERSION
        ):
            raise ReleaseValidationError(
                "ISSUE_51_REGRESSION.json details schema mismatch"
            )
        receipt_details["issue_51_regression"] = issue_51_details
    issue_60_details: dict[str, object] | None = None
    if issue_60_details_path is not None:
        if not issue_60_details_path.is_file():
            raise ReleaseValidationError(
                "ISSUE_60_REGRESSION.json details output is missing"
            )
        issue_60_details = _load_json(issue_60_details_path)
        if issue_60_details.get("schema_version") != ISSUE_60_SCHEMA_VERSION:
            raise ReleaseValidationError(
                "ISSUE_60_REGRESSION.json details schema mismatch"
            )
        exact_issue_60: dict[str, object] = {
            "poison_initial_attempts": 1_667,
            "early_retry_count": 0,
            "terminal_revive_count": 0,
            "healthy_item_completed": True,
            "legacy_queue_mutation_count": 0,
            "simulated_seconds": 61,
            "maintenance_transactions": 2,
            "prefetch_timeout_observed": False,
            "active_instance_touched": False,
            "result": "passed",
        }
        for field, expected in exact_issue_60.items():
            if issue_60_details.get(field) != expected:
                raise ReleaseValidationError(
                    f"ISSUE_60_REGRESSION.json {field} mismatch"
                )
        max_wait = issue_60_details.get("prefetch_max_wait_ms")
        if (
            isinstance(max_wait, bool)
            or not isinstance(max_wait, int)
            or not 0 <= max_wait <= 550
        ):
            raise ReleaseValidationError(
                "ISSUE_60_REGRESSION.json prefetch_max_wait_ms mismatch"
            )
        receipt_details["issue_60_regression"] = issue_60_details
    writer_handoff_details: dict[str, object] | None = None
    if writer_handoff_details_path is not None:
        if not writer_handoff_details_path.is_file():
            raise ReleaseValidationError(
                "WRITER_LEASE_HANDOFF_REHEARSAL.json details output is missing"
            )
        writer_handoff_details = _load_json(writer_handoff_details_path)
        if writer_handoff_details.get("schema_version") != (
            WRITER_HANDOFF_DETAILS_SCHEMA_VERSION
        ):
            raise ReleaseValidationError(
                "WRITER_LEASE_HANDOFF_REHEARSAL.json details schema mismatch"
            )
        expected_handoff = {
            "writer_artifact_sha256": context.wheel_sha256,
            "idle_release_seconds": 1800.0,
            "process_count": 2,
            "same_process_provider_count": 2,
            "stages": list(WRITER_HANDOFF_STAGES),
            "simultaneous_writer_observed": False,
            "accepted_work_lost": False,
            "holder_count_after_release": 0,
            "connection_pin_count_after_release": 0,
            "result": "passed",
        }
        for field, expected in expected_handoff.items():
            if writer_handoff_details.get(field) != expected:
                raise ReleaseValidationError(
                    "WRITER_LEASE_HANDOFF_REHEARSAL.json "
                    f"{field} mismatch"
                )
        receipt_details["writer_lease_handoff"] = writer_handoff_details
    receipt_payload = _receipt(
        context,
        stage=stage,
        command=display,
        database_kind="fixture-copy",
        artifact_contract={
            "artifact_consumed": True,
            "artifact_kind": "wheel",
            "installed_distribution": install_receipt["installed_distribution"],
            "imported_module_path_class": "isolated-site-packages",
            "source_worktree_imported": False,
            "source_worktree_on_sys_path": False,
            "install_receipt_sha256": install_receipt_sha256,
            "direct_url_sha256": install_receipt["direct_url_sha256"],
            "record_sha256": install_receipt["record_sha256"],
            "environment_id": environment_id,
            "hermes_source_identity": dict(hermes_source_identity),
        },
        environment_identity={
            "identity_scheme": "sha256-local-path-v1",
            "hermes_home_id": _canonical_sha256(environment["HERMES_HOME"]),
            "database_id": _canonical_sha256(environment["SCOPE_RECALL_DB"]),
            "pytest_basetemp_id": _canonical_sha256(
                str(
                    _pytest_basetemp(
                        environment,
                        f"r-{hashlib.sha256(stem.encode('utf-8')).hexdigest()[:8]}",
                    )
                )
            ),
        },
        details=receipt_details,
    )
    if writer_handoff_details is not None:
        receipt_payload["schema_version"] = WRITER_HANDOFF_SCHEMA_VERSION
        receipt_payload.update(
            {
                field: value
                for field, value in writer_handoff_details.items()
                if field != "schema_version"
            }
        )
    if issue_60_details is not None:
        receipt_payload["schema_version"] = ISSUE_60_SCHEMA_VERSION
        receipt_payload.update(
            {
                field: value
                for field, value in issue_60_details.items()
                if field != "schema_version"
            }
        )
    _write_json(
        staging / receipt_name,
        receipt_payload,
    )


def _run_full_suite(
    *,
    root: Path,
    python: Path,
    staging: Path,
    environment: dict[str, str],
    hermes_source: Path,
    context: ValidationContext,
    ledger: list[dict[str, object]],
) -> None:
    junit = staging / "PYTEST_JUNIT.xml"
    honesty_raw = staging / "PYTEST_SKIP_REPORT.raw.json"
    honesty = staging / "PYTEST_SKIP_REPORT.json"
    log = staging / "PYTEST_STDOUT.log"
    environment.update(
        {
            "PYTHONPATH": str(hermes_source.resolve(strict=True)),
            "SCOPE_RECALL_TEST_HONESTY_OUTPUT": str(honesty_raw),
            "SCOPE_RECALL_SOURCE_COMMIT": context.source_commit,
            "SCOPE_RECALL_SOURCE_TREE": context.source_tree,
            "SCOPE_RECALL_TEST_TIMEOUTS_JSON": json.dumps(
                [
                    {
                        "stage": "full_pytest",
                        "seconds": FULL_TEST_TIMEOUT_SECONDS,
                        "reason": "bounded final local Windows suite ceiling",
                    }
                ]
            ),
        }
    )
    actual = [
        str(python),
        "-B",
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-p",
        "scripts.release_test_honesty",
        "-ra",
        f"--junitxml={junit}",
        "--basetemp",
        str(_pytest_basetemp(environment, "f")),
    ]
    display = [
        "python",
        "-B",
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-p",
        "scripts.release_test_honesty",
        "-ra",
        "--junitxml=<evidence>/PYTEST_JUNIT.xml",
        "--basetemp=<isolated>/pytest-full",
    ]
    _run(
        actual,
        display_command=display,
        cwd=root,
        environment=environment,
        timeout_seconds=FULL_TEST_TIMEOUT_SECONDS,
        log_path=log,
        ledger=ledger,
    )
    if not junit.is_file() or not honesty_raw.is_file():
        raise ReleaseValidationError("full pytest did not produce JUnit and raw honesty evidence")
    honesty_module = _load_script(
        root / "scripts" / "release_test_honesty.py",
        "scope_recall_validation_test_honesty_redactor",
    )
    honesty_module.write_shareable_report(honesty_raw, honesty)
    evidence = _load_script(
        root / "scripts" / "report.evidence_package.py",
        "scope_recall_validation_evidence_contract",
    )
    honesty_payload = _load_json(honesty)
    evidence.validate_test_honesty(honesty_payload)
    evidence.validate_test_honesty_pair(
        _load_json(honesty_raw),
        honesty_payload,
    )
    if honesty_payload.get("source_commit") != context.source_commit:
        raise ReleaseValidationError("pytest honesty source commit mismatch")
    if honesty_payload.get("source_tree") != context.source_tree:
        raise ReleaseValidationError("pytest honesty source tree mismatch")


def _run_static_validation(
    *,
    root: Path,
    staging: Path,
    environment: Mapping[str, str],
    ledger: list[dict[str, object]],
) -> None:
    commands = (
        (
            [sys.executable, "-B", "-m", "ruff", "check", "--no-cache", "."],
            ["python", "-B", "-m", "ruff", "check", "--no-cache", "."],
            staging / "RUFF.log",
        ),
        (
            [sys.executable, "-B", "-m", "pyright"],
            ["python", "-B", "-m", "pyright"],
            staging / "PYRIGHT.log",
        ),
        (
            ["git", "diff", "--check"],
            ["git", "diff", "--check"],
            staging / "GIT_DIFF_CHECK.log",
        ),
    )
    for actual, display, log in commands:
        _run(
            actual,
            display_command=display,
            cwd=root,
            environment=environment,
            timeout_seconds=STAGE_TIMEOUT_SECONDS,
            log_path=log,
            ledger=ledger,
        )


def _active_isolation_evidence(
    *,
    root: Path,
    staging: Path,
    environment: Mapping[str, str],
    context: ValidationContext,
    active_hermes_home: Path,
    hermes_0191_source: Path,
    hermes_0206_source: Path,
    accidental_home_path: Path,
    quarantine_path: Path,
    ledger: list[dict[str, object]],
) -> None:
    log = staging / "ACTIVE_ISOLATION.log"
    display = [
        "python",
        "-B",
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-q",
        *ISOLATION_NODES,
    ]
    stage = _run(
        [
            sys.executable,
            "-B",
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-q",
            "--basetemp",
            str(_pytest_basetemp(environment, "a")),
            *ISOLATION_NODES,
        ],
        display_command=display,
        cwd=root,
        environment=environment,
        timeout_seconds=STAGE_TIMEOUT_SECONDS,
        log_path=log,
        ledger=ledger,
    )
    probe = _load_script(
        root / "scripts" / "probe.hermes_compatibility.py",
        "scope_recall_validation_hermes_probe",
    )
    probe_0191 = probe.build_probe_receipt(
        candidate_source=root,
        hermes_source=hermes_0191_source,
        expected_hermes_version="0.19.1",
        active_hermes_home=active_hermes_home,
    )
    probe_0206 = probe.build_probe_receipt(
        candidate_source=root,
        hermes_source=hermes_0206_source,
        expected_hermes_version="0.20.6",
        active_hermes_home=active_hermes_home,
    )
    if probe_0191.get("result") != "compatible":
        raise ReleaseValidationError("pinned Hermes 0.19.1 probe is not compatible")
    if probe_0206.get("result") not in {"compatible", "incompatible"}:
        raise ReleaseValidationError("Hermes 0.20.6 probe is not conclusively classified")
    if probe_0206.get("support_matrix_changed") is not False:
        raise ReleaseValidationError("Hermes 0.20.6 probe changed support policy")
    _write_json(staging / "HERMES_COMPATIBILITY_PROBE.0.19.1.json", probe_0191)
    _write_json(staging / "HERMES_COMPATIBILITY_PROBE.0.20.6.json", probe_0206)
    cleanup = _load_script(
        root / "scripts" / "report.home_cleanup.py",
        "scope_recall_validation_home_cleanup",
    )
    cleanup_receipt = cleanup.build_cleanup_receipt(
        accidental_path=accidental_home_path,
        active_plugin_path=active_hermes_home / "plugins" / "scope-recall",
        quarantine_path=quarantine_path,
    )
    _write_json(staging / "ACCIDENTAL_HOME_CLEANUP_RECEIPT.json", cleanup_receipt)
    combined_stage = dict(stage)
    combined_stage["finished_at"] = _utc_now()
    _write_json(
        staging / "ACTIVE_ISOLATION.json",
        _receipt(
            context,
            stage=combined_stage,
            command=[
                "release-validation",
                "active-isolation",
                "pytest-boundary",
                "hermes-0.19.1-probe",
                "hermes-0.20.6-probe",
                "home-cleanup-inventory",
            ],
            database_kind="fixture-copy",
            details={
                "hermes_0_19_1": probe_0191["result"],
                "hermes_0_20_6": probe_0206["result"],
                "support_matrix_changed": False,
                "home_cleanup_deletion_performed": False,
            },
        ),
    )


def _repository_evidence(
    *,
    root: Path,
    staging: Path,
    context: ValidationContext,
) -> None:
    census_module = _load_script(
        root / "scripts" / "report.repository_census.py",
        "scope_recall_validation_repository_census",
    )
    started = _utc_now()
    census = census_module.build_census(root, tracked_only=True)
    delta = census_module.repository_delta(root)
    finished = _utc_now()
    stage = {
        "started_at": started,
        "finished_at": finished,
        "exit_code": 0,
        "log_sha256": "not-applicable",
    }
    _write_json(
        staging / "REPOSITORY_CENSUS.json",
        _receipt(
            context,
            stage=stage,
            command=[
                "python",
                "scripts/report.repository_census.py",
                "--tracked-only",
            ],
            database_kind="not-used",
            details={"census": census},
        ),
    )
    _write_json(
        staging / "REPOSITORY_DELETE_RENAME_EVIDENCE.json",
        _receipt(
            context,
            stage=stage,
            command=[
                "git",
                "diff",
                "--name-status",
                "-z",
                "-M",
                f"{census_module.PUBLIC_BASE_COMMIT}...HEAD",
            ],
            database_kind="not-used",
            details={"delta": delta},
        ),
    )


def _issue_61_applicability_evidence(
    *,
    root: Path,
    staging: Path,
    context: ValidationContext,
    wheel: Path,
    sdist: Path,
) -> None:
    """Prove the retired 1.10.3 visual writer is absent from 2.0 artifacts."""

    artifacts = _load_script(
        root / "scripts" / "release_candidate_artifacts.py",
        "scope_recall_validation_issue_61_artifacts",
    )
    candidate = _load_script(
        root / "scripts" / "report.candidate_manifest.py",
        "scope_recall_validation_issue_61_source",
    )
    started = _utc_now()
    source_manifest = candidate.source_manifest(root)
    wheel_members = artifacts.read_archive_members(wheel)
    sdist_members = artifacts.read_archive_members(sdist)
    sdist_roots = {
        PurePosixPath(name).parts[0]
        for name in sdist_members
        if PurePosixPath(name).parts
    }
    if len(sdist_roots) != 1:
        raise ReleaseValidationError("Issue #61 sdist root is ambiguous")
    sdist_root = next(iter(sdist_roots))
    findings = {
        "source": artifacts.legacy_visual_console_source_findings(
            root, source_manifest
        ),
        "wheel": artifacts.legacy_visual_console_artifact_findings(
            wheel_members, kind="wheel"
        ),
        "sdist": artifacts.legacy_visual_console_artifact_findings(
            sdist_members,
            kind="sdist",
            sdist_root=sdist_root,
        ),
    }
    if any(findings.values()):
        raise ReleaseValidationError(
            "Issue #61 retired visual-console writer is present in candidate"
        )
    _write_json(
        staging / "ISSUE_61_APPLICABILITY.json",
        {
            "schema_version": ISSUE_61_SCHEMA_VERSION,
            "source_commit": context.source_commit,
            "source_tree": context.source_tree,
            "artifact_sha256": context.wheel_sha256,
            "wheel_sha256": context.wheel_sha256,
            "sdist_sha256": context.sdist_sha256,
            "started_at": started,
            "finished_at": _utc_now(),
            "command": [
                "release-validation",
                "issue-61",
                "source-wheel-sdist-absence",
            ],
            "exit_code": 0,
            "environment_boundary": {
                "hermes_home_kind": "isolated",
                "database_kind": "not-used",
                "active_instance_touched": False,
            },
            "affected_version": "1.10.3",
            "legacy_server_present_in_source": False,
            "legacy_server_present_in_wheel": False,
            "legacy_server_present_in_sdist": False,
            "raw_truth_write_endpoint_present": False,
            "unsafe_console_entrypoint_present": False,
            "unsafe_console_documentation_present": False,
            "two_point_zero_code_change_required": False,
            "one_ten_backport_required": True,
            "active_instance_touched": False,
            "result": "not-applicable-to-2.0",
        },
    )


def run_release_validation(
    *,
    root: Path,
    expected_sha: str,
    evidence_dir: Path,
    active_hermes_home: Path,
    hermes_0191_source: Path,
    hermes_0206_source: Path,
    accidental_home_path: Path,
    quarantine_path: Path,
    n_minus_one_wheel: Path,
) -> Path:
    resolved = root.resolve(strict=True)
    evidence = evidence_dir.resolve(strict=True)
    active = active_hermes_home.resolve(strict=False)
    real_home = Path.home().resolve(strict=False)
    context = _validation_context(resolved, evidence, expected_sha)
    candidate_wheel = (
        evidence.joinpath(*Path(context.wheel_relative_path).parts).resolve(strict=True)
    )
    try:
        candidate_wheel.relative_to(evidence)
    except ValueError as exc:
        raise ReleaseValidationError("candidate wheel escapes evidence directory") from exc
    if candidate_wheel.name != context.wheel_name:
        raise ReleaseValidationError("candidate wheel name differs from provenance")
    if _sha256(candidate_wheel) != context.wheel_sha256:
        raise ReleaseValidationError("candidate wheel differs from provenance")
    provenance = _load_json(evidence / "BUILD_PROVENANCE.json")
    sdist_name, sdist_relative_path, sdist_sha256 = _artifact_descriptor(
        provenance, "sdist"
    )
    candidate_sdist = evidence.joinpath(
        *Path(sdist_relative_path).parts
    ).resolve(strict=True)
    try:
        candidate_sdist.relative_to(evidence)
    except ValueError as exc:
        raise ReleaseValidationError("candidate sdist escapes evidence directory") from exc
    if candidate_sdist.name != sdist_name or sdist_sha256 != context.sdist_sha256:
        raise ReleaseValidationError("candidate sdist identity differs from provenance")
    if _sha256(candidate_sdist) != context.sdist_sha256:
        raise ReleaseValidationError("candidate sdist differs from provenance")
    previous_wheel = n_minus_one_wheel.resolve(strict=True)
    known_quarantine = quarantine_path.resolve(strict=True)
    hermes_probe = _load_script(
        resolved / "scripts" / "probe.hermes_compatibility.py",
        "scope_recall_validation_rehearsal_hermes_identity",
    )
    hermes_rehearsal_identity = hermes_probe._git_identity(
        hermes_0191_source.resolve(strict=True)
    )
    if not hermes_probe._is_clean_bound_git_identity(hermes_rehearsal_identity):
        raise ReleaseValidationError(
            "artifact rehearsals require a clean, Git-bound Hermes 0.19.1 source"
        )
    validation_targets = {
        "TEST_COMMANDS.json",
        "PYTEST_JUNIT.xml",
        "PYTEST_STDOUT.log",
        "PYTEST_SKIP_REPORT.raw.json",
        "PYTEST_SKIP_REPORT.json",
        "RUFF.log",
        "PYRIGHT.log",
        "N_MINUS_ONE_WINDOW.json",
        "MIGRATION_N_MINUS_ONE.json",
        "MIGRATION_N.json",
        "DOWNGRADE_N_MINUS_ONE.json",
        "PURGE_RESTORE_REPLAY.json",
        "READONLY_CANARY.json",
        "WRITER_CANARY.json",
        "ROLLBACK_REHEARSAL.json",
        "ISSUE_51_REGRESSION.json",
        "ISSUE_60_REGRESSION.json",
        "ISSUE_61_APPLICABILITY.json",
        "WRITER_LEASE_HANDOFF_REHEARSAL.json",
        "INSTALL_CANDIDATE_RECEIPT.json",
        "INSTALL_N_MINUS_ONE_RECEIPT.json",
        "ACTIVE_ISOLATION.json",
        "REPOSITORY_CENSUS.json",
        "REPOSITORY_DELETE_RENAME_EVIDENCE.json",
    }
    existing = sorted(name for name in validation_targets if (evidence / name).exists())
    if existing:
        raise ReleaseValidationError(
            f"refusing to overwrite existing final validation evidence: {', '.join(existing)}"
        )
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".validation.{expected_sha}.",
            dir=evidence.parent,
        )
    )
    try:
        with _temporary_validation_boundary() as boundary:
            environment = _isolated_environment(
                boundary,
                active_hermes_home=active,
                real_home=real_home,
            )
            ledger: list[dict[str, object]] = []
            full_suite_workspace = boundary / "full-suite-environment"
            full_suite_workspace.mkdir(parents=True)
            full_suite_python = _prepare_full_suite_environment(
                root=resolved,
                candidate_wheel=candidate_wheel,
                hermes_source=hermes_0191_source,
                workspace=full_suite_workspace,
                staging=staging,
                active_hermes_home=active,
                ledger=ledger,
            )
            _run_full_suite(
                root=resolved,
                python=full_suite_python,
                staging=staging,
                environment=environment,
                hermes_source=hermes_0191_source,
                context=context,
                ledger=ledger,
            )
            _run_static_validation(
                root=resolved,
                staging=staging,
                environment=environment,
                ledger=ledger,
            )
            _issue_61_applicability_evidence(
                root=resolved,
                staging=staging,
                context=context,
                wheel=candidate_wheel,
                sdist=candidate_sdist,
            )
            artifact_workspace = boundary / "artifact-environments"
            artifact_workspace.mkdir(parents=True)
            candidate_python, candidate_install, candidate_install_sha = (
                _artifact_install_environment(
                    root=resolved,
                    artifact=candidate_wheel,
                    artifact_sha256=context.wheel_sha256,
                    expected_version="2.0.1",
                    label="candidate",
                    workspace=artifact_workspace,
                    staging=staging,
                    active_hermes_home=active,
                    include_dev=True,
                    ledger=ledger,
                )
            )
            candidate_install["source_commit"] = context.source_commit
            candidate_install["source_tree"] = context.source_tree
            _write_json(
                staging / "INSTALL_CANDIDATE_RECEIPT.json",
                candidate_install,
            )
            candidate_install_sha = _sha256(
                staging / "INSTALL_CANDIDATE_RECEIPT.json"
            )
            n_minus_one_artifact_sha256 = _sha256(previous_wheel)
            n_minus_one_python, n_minus_one_install, n_minus_one_install_sha = (
                _artifact_install_environment(
                    root=resolved,
                    artifact=previous_wheel,
                    artifact_sha256=n_minus_one_artifact_sha256,
                    expected_version=N_MINUS_ONE_VERSION,
                    label="n_minus_one",
                    workspace=artifact_workspace,
                    staging=staging,
                    active_hermes_home=active,
                    include_dev=False,
                    ledger=ledger,
                )
            )
            n_minus_one_install["source_commit"] = "published-v1.10.3-artifact"
            n_minus_one_install["candidate_source_mixed"] = False
            _write_json(
                staging / "INSTALL_N_MINUS_ONE_RECEIPT.json",
                n_minus_one_install,
            )
            n_minus_one_install_sha = _sha256(
                staging / "INSTALL_N_MINUS_ONE_RECEIPT.json"
            )
            _n_minus_one_window_receipt, n_minus_one_window_sha = (
                _run_n_minus_one_window(
                    root=resolved,
                    context=context,
                    candidate_python=candidate_python,
                    candidate_install=candidate_install,
                    candidate_install_sha256=candidate_install_sha,
                    n_minus_one_python=n_minus_one_python,
                    n_minus_one_install=n_minus_one_install,
                    n_minus_one_install_sha256=n_minus_one_install_sha,
                    n_minus_one_artifact_sha256=n_minus_one_artifact_sha256,
                    workspace=artifact_workspace,
                    staging=staging,
                    active_hermes_home=active,
                    real_home=real_home,
                    ledger=ledger,
                )
            )
            harness = _prepare_artifact_harness(
                root=resolved,
                python=candidate_python,
                workspace=artifact_workspace,
            )
            for receipt_name, node_ids in REHEARSAL_RECEIPTS.items():
                rehearsal_boundary = (
                    boundary
                    / "rehearsals"
                    / hashlib.sha256(receipt_name.encode("utf-8")).hexdigest()[:12]
                )
                rehearsal_environment = _isolated_environment(
                    rehearsal_boundary,
                    active_hermes_home=active,
                    real_home=real_home,
                )
                _run_pytest_receipt(
                    root=resolved,
                    harness=harness,
                    python=candidate_python,
                    staging=staging,
                    environment=rehearsal_environment,
                    context=context,
                    install_receipt=candidate_install,
                    install_receipt_sha256=candidate_install_sha,
                    hermes_source=hermes_0191_source.resolve(strict=True),
                    hermes_source_identity=hermes_rehearsal_identity,
                    receipt_name=receipt_name,
                    node_ids=node_ids,
                    ledger=ledger,
                )
                if receipt_name in {
                    "MIGRATION_N_MINUS_ONE.json",
                    "DOWNGRADE_N_MINUS_ONE.json",
                }:
                    receipt_path = staging / receipt_name
                    receipt_payload = _load_json(receipt_path)
                    receipt_payload["n_minus_one_install_receipt_sha256"] = (
                        n_minus_one_install_sha
                    )
                    receipt_payload["n_minus_one_distribution"] = (
                        n_minus_one_install["installed_distribution"]
                    )
                    receipt_payload["candidate_n_minus_one_environment_mixed"] = False
                    receipt_payload["n_minus_one_window_receipt_sha256"] = (
                        n_minus_one_window_sha
                    )
                    receipt_payload["n_minus_one_window_evidence"] = (
                        "real-cross-interpreter"
                    )
                    _write_json(receipt_path, receipt_payload)
            immutable_environment = _isolated_environment(
                artifact_workspace / "immutability-probe",
                active_hermes_home=active,
                real_home=real_home,
            )
            immutable_environment["SCOPE_RECALL_ARTIFACT_SOURCE_ROOT"] = str(resolved)
            immutable_log = staging / "INSTALL_CANDIDATE_IMMUTABILITY_PROBE.log"
            _run(
                [str(candidate_python), "-B", "-c", _INSTALL_PROBE],
                display_command=[
                    "python",
                    "-B",
                    "-c",
                    "<installed-distribution-immutability-probe>",
                ],
                cwd=artifact_workspace,
                environment=immutable_environment,
                timeout_seconds=STAGE_TIMEOUT_SECONDS,
                log_path=immutable_log,
                ledger=ledger,
            )
            immutable_probe = _load_json(immutable_log)
            if immutable_probe.get("installed_package_manifest_sha256") != (
                candidate_install.get("installed_package_manifest_sha256")
            ) or immutable_probe.get("record_sha256") != candidate_install.get(
                "record_sha256"
            ) or immutable_probe.get(
                "environment_distribution_manifest_sha256"
            ) != candidate_install.get("environment_distribution_manifest_sha256"):
                raise ReleaseValidationError(
                    "candidate installed environment changed during rehearsals"
                )
            _active_isolation_evidence(
                root=resolved,
                staging=staging,
                environment=environment,
                context=context,
                active_hermes_home=active,
                hermes_0191_source=hermes_0191_source,
                hermes_0206_source=hermes_0206_source,
                accidental_home_path=accidental_home_path,
                quarantine_path=known_quarantine,
                ledger=ledger,
            )
            _repository_evidence(root=resolved, staging=staging, context=context)
            _write_json(
                staging / "TEST_COMMANDS.json",
                {
                    "schema_version": SCHEMA_VERSION,
                    "source_commit": context.source_commit,
                    "source_tree": context.source_tree,
                    "active_instance_touched": False,
                    "commands": ledger,
                },
            )
    except Exception as exc:
        raise ReleaseValidationError(
            f"{exc}; raw validation logs retained in {staging.name}"
        ) from exc
    for path in sorted(staging.iterdir(), key=lambda item: item.name):
        if not path.is_file():
            raise ReleaseValidationError(
                f"unexpected non-file validation output: {path.name}"
            )
        target = evidence / path.name
        if target.exists():
            raise ReleaseValidationError(
                f"refusing to overwrite evidence output: {path.name}"
            )
        path.replace(target)
    staging.rmdir()
    evidence_module = _load_script(
        resolved / "scripts" / "report.evidence_package.py",
        "scope_recall_validation_evidence_index",
    )
    index = evidence_module.build_evidence_index(evidence, expected_sha=expected_sha)
    return evidence_module.write_evidence_index(evidence, index)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--active-hermes-home", type=Path, required=True)
    parser.add_argument("--hermes-0-19-1-source", type=Path, required=True)
    parser.add_argument("--hermes-0-20-6-source", type=Path, required=True)
    parser.add_argument("--accidental-home-path", type=Path)
    parser.add_argument("--quarantine-path", type=Path, required=True)
    parser.add_argument("--n-minus-one-wheel", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.resolve(strict=True)
    expected_sha = str(args.expected_sha)
    evidence = (
        args.evidence_dir
        if args.evidence_dir is not None
        else root / ".execution" / "evidence" / expected_sha
    )
    active = args.active_hermes_home.resolve(strict=False)
    index = run_release_validation(
        root=root,
        expected_sha=expected_sha,
        evidence_dir=evidence,
        active_hermes_home=active,
        hermes_0191_source=args.hermes_0_19_1_source,
        hermes_0206_source=args.hermes_0_20_6_source,
        accidental_home_path=args.accidental_home_path
        or Path.home() / "plugins" / "scope-recall",
        quarantine_path=args.quarantine_path,
        n_minus_one_wheel=args.n_minus_one_wheel,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "source_commit": expected_sha,
                "evidence_index_sha256": _sha256(index),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReleaseValidationError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        raise SystemExit(1) from exc
