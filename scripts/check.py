from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from test_directories import TestDirectory
from tier_allows import TIER_ALLOWS

ROOT = Path(__file__).resolve().parents[1]
#: No default path: with neither ``SCOPE_RECALL_UV`` nor a uv on ``PATH``, no helper root is authorized.
_DEFAULT_PACKAGING_UV = ""
DEFAULT_WATCHDOG_SECONDS = 180
RELEASE_WATCHDOG_SECONDS = 600
#: GitHub's Windows runner executes the integration baseline roughly an order
#: of magnitude slower than the Linux runners; the 118-file selection needs
#: tens of minutes of real work there. Cap the integration tier so the
#: watchdog bounds a hang, not a slow-but-healthy suite.
INTEGRATION_WATCHDOG_SECONDS = 2700
#: Headroom granted per selected test file, on top of the 180s floor.  A flat
#: 180s times out a tier of 1,048 tests across 66 files with nothing hung, only
#: a bigger suite, and a gate that reports a timeout instead of a result is not
#: a gate.  A watchdog is there to bound a *hang*, which is unbounded; sizing it
#: to the work actually selected keeps that meaning instead of turning every
#: eighty new tests into a flake that has to be rediscovered.  GitHub's Windows
#: runners also run the same healthy suite several times slower at times: the
#: retrieval tier took 39-170s across runs and once ran past a 215s watchdog,
#: the host tier 142-237s and once past 290s, with nothing hung either time.
#: Twenty seconds a file leaves such a runner room and still bounds a hang at
#: the release budget.
WATCHDOG_SECONDS_PER_FILE = 20
#: Test files that build whole stores, and the seconds each adds to its tier's
#: watchdog.  The allowance comes after the release budget has capped the
#: twenty seconds a file, so the cap that bounds a tier of many files never
#: cuts it, and a hang stays bounded by the sum.
#: The recall bench's file builds a synthetic shared store of four weeks
#: through the real capture, worker and recall paths, and a two-day one twice:
#: about 60 s under this gate on a local SSD, and up to six and a half times
#: that on GitHub's Windows runners, whose file operations are the slowest it
#: meets.  Its 600 s are that and half as much again.  The memory export file
#: captures stores of a hundred and a thousand sources to show that export
#: memory stays bounded, about 30 s on the same SSD; its 300 s follow the same
#: rule.
HEAVY_FILE_SECONDS = {
    "tests/recall_bench/test_recall_bench.py": 600,
    "tests/contract/test_memory_export.py": 300,
}


def pytest_watchdog_seconds(
    tier: str, selected_files: int = 0, selected_paths: list[str] | tuple[str, ...] = ()
) -> int:
    """Bounded suite watchdog, scaled to how much this run selected.

    Release and integration keep fixed budgets of 600s and 2700s respectively.
    Every other tier keeps 180s as a floor and adds twenty seconds per selected
    file, capped at the release budget so no run --
    hung or merely large -- waits forever; each of ``selected_paths`` that
    builds a whole store then adds its own allowance (``HEAVY_FILE_SECONDS``),
    which the cap does not cut.
    """

    if tier == "release":
        return RELEASE_WATCHDOG_SECONDS
    if type(selected_files) is not int or type(selected_files) is bool or selected_files < 0:
        selected_files = 0
    if tier == "integration":
        # The GitHub Windows runner executes the 118-file integration baseline
        # tens of times slower than Linux: a shared cap of 600s fired with the
        # suite 25% done and still printing progress.  The tier carries its own
        # fixed budget, sized to that run, the way release carries 600s.
        return INTEGRATION_WATCHDOG_SECONDS
    paths = selected_paths if isinstance(selected_paths, (list, tuple)) else ()
    heavy = sum(HEAVY_FILE_SECONDS.get(path, 0) for path in {str(path).replace("\\", "/") for path in paths})
    scaled = DEFAULT_WATCHDOG_SECONDS + WATCHDOG_SECONDS_PER_FILE * selected_files
    return min(max(DEFAULT_WATCHDOG_SECONDS, scaled), RELEASE_WATCHDOG_SECONDS) + heavy


def distribution_is_installed(python_executable: str | None = None) -> bool:
    """Whether this interpreter can see the package as an installed distribution.

    Importable is not the same as installed: ``check.py`` puts the source tree
    on ``PYTHONPATH``, which satisfies ``import scope_recall`` but leaves
    ``importlib.metadata`` with nothing to find, and the doctor's entry-point
    probe runs isolated precisely so that it measures the real thing.
    """

    if python_executable is not None and python_executable != sys.executable:
        probe = "import importlib.metadata as m; print(m.version('hermes-scope-recall'))"
        try:
            done = subprocess.run(
                [python_executable, "-I", "-B", "-c", probe], capture_output=True, text=True, timeout=30, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return done.returncode == 0
    try:
        importlib.metadata.version("hermes-scope-recall")
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


def packaging_helper_env(tier: str) -> dict[str, str]:
    """Authorize the same declared uv helper root for every tier whose runs may drive the packaging helpers.

    A uv named by ``SCOPE_RECALL_UV`` goes into the run too: the tests take that one before they search ``PATH``, and
    a ``PATH`` with another uv first would hand them one outside the root authorized here.
    """

    if "build" not in TIER_ALLOWS.get(tier, frozenset()):
        return {}
    declared = os.environ.get("SCOPE_RECALL_UV")
    packaging_uv = declared or shutil.which("uv") or _DEFAULT_PACKAGING_UV
    if not packaging_uv:
        return {}
    env = {"SCOPE_RECALL_TEST_PACKAGING_HELPER_ROOTS": str(Path(packaging_uv).resolve().parent)}
    if declared:
        env["SCOPE_RECALL_UV"] = declared
    return env


def node_helper_env(tier: str) -> dict[str, str]:
    """The node executable the host tier's dsh plugin tests run; none where node is not installed (they skip)."""

    if "node" not in TIER_ALLOWS.get(tier, frozenset()):
        return {}
    node = shutil.which("node")
    return {"SCOPE_RECALL_TEST_NODE": str(Path(node).resolve())} if node else {}


SCRIPT_GATE_TESTS = [
    "tests/packaging/test_source_manifest.py",
    "tests/packaging/test_check_selection.py",
    "tests/packaging/test_package_manifest.py",
    "tests/packaging/test_release_notes.py",
    "tests/packaging/test_quality_gate.py",
]
#: The tests that run git on the checkout, which only the tiers whose ``TIER_ALLOWS`` entry has ``git`` can pass.
GIT_TESTS = ["tests/integration/test_guard_git.py"]
SUITES = {
    "unit": [
        "tests/unit/test_trusted_context.py",
        "tests/unit/test_check_runner.py",
        "tests/unit/test_secret_patterns.py",
        "tests/unit/test_recall_budget.py",
        "tests/unit/test_query_time.py",
        "tests/unit/test_contract_schemas.py",
        "tests/unit/test_claude_code_record.py",
        "tests/unit/test_workbuddy_record.py",
        "tests/unit/test_dsh_record.py",
    ],
    "contract": [
        "tests/contract/test_backup_relocation.py",
        "tests/contract/test_full_backup.py",
        "tests/contract/test_memory_admin.py",
        "tests/contract/test_memory_cli.py",
        "tests/contract/test_memory_ui_api.py",
        "tests/contract/test_memory_export.py",
        "tests/contract/test_private_mode.py",
        "tests/contract/test_fragment_quote_recovery.py",
        "tests/contract/test_protocol.py",
        "tests/contract/test_synthetic_inputs.py",
        "tests/contract/test_recall_budget_config.py",
        "tests/contract/test_autostart_cli.py",
        "tests/contract/test_companion_publish.py",
        "tests/contract/test_upgrade_store_cli.py",
        "tests/contract/test_request_guard_escaping.py",
        "tests/contract/test_relation_candidates_rank.py",
        "tests/contract/test_status_file_beside_its_writer.py",
        "tests/contract/test_every_store_meets_the_runtime.py",
        "tests/contract/test_a_paused_wake_lets_go.py",
        "tests/contract/test_a_pass_that_ends_hands_back_its_group.py",
        "tests/contract/test_settle_window_wake.py",
        "tests/contract/test_monthly_auxiliary_budget.py",
    ],
    "storage": [
        "tests/contract/test_storage.py",
        "tests/contract/test_writable_store_connections.py",
        "tests/contract/test_shared_store.py",
        "tests/contract/test_embedding_respace.py",
        "tests/known_answer/test_recall_invariants_check.py",
    ],
    "capture": ["tests/contract/test_capture.py", "tests/contract/test_capture_row_crossings.py"],
    "claims": [
        "tests/test_denial_values.py",
        "tests/contract/test_claims.py",
        "tests/contract/test_claim_embed_recovery.py",
    ],
    "deletion": [
        "tests/contract/test_deletion.py",
        "tests/contract/test_purge_leaves_no_copy.py",
        "tests/contract/test_restore.py",
    ],
    "episodes": [
        "tests/contract/test_episodes.py",
        "tests/contract/test_retained_artifacts.py",
        "tests/contract/test_episode_authority.py",
        "tests/contract/test_consolidation_input.py",
        "tests/contract/test_aliases.py",
        "tests/contract/test_storage_growth.py",
    ],
    "retrieval": [
        "tests/contract/test_retrieval_pipeline.py",
        "tests/contract/test_evidence_followup.py",
        "tests/contract/test_recall_admission.py",
        "tests/contract/test_retrieval_history_state.py",
        "tests/contract/test_vector_timeout_fallback.py",
        "tests/contract/test_auto_query_echo.py",
        "tests/contract/test_vector_retention.py",
        "tests/contract/test_recall_scope.py",
        "tests/contract/test_recall_time.py",
        "tests/contract/test_withheld_outputs_unindexed.py",
        "tests/contract/test_recall_row_crossings.py",
        "tests/contract/test_owner_view.py",
        "tests/recall_bench/test_recall_bench.py",
    ],
    "native": [
        "tests/contract/test_lance_embed_fence.py",
        "tests/contract/test_retained_lock_deadline.py",
        "tests/integration/test_lance_retrieval.py",
        "tests/storage_native/test_runtime_instance_seam.py",
        "tests/storage_native/test_vector_compaction.py",
        "tests/storage_native/test_in_process_lance_store.py",
        "tests/storage_native/test_helper_runs_in_the_environment_that_owns_the_package.py",
        "tests/storage_native/test_helper_imports_what_its_host_imports.py",
        "tests/storage_native/test_probe_rehearses_with_the_roots_its_host_imports.py",
        "tests/test_cold_open_overlap.py",
        "tests/test_lance_fanout_deadline.py",
    ],
    "host": [
        "tests/host/test_memory_ui.py",
        "tests/host/test_memory_ui_http.py",
        "tests/host/test_memory_ui_assets.py",
        "tests/host/codex/test_client_private_mode.py",
        "tests/host/hermes/test_hermes_private_mode.py",
        "tests/host/test_endpoint_credentials.py",
        "tests/host/hermes/test_attachments_shutdown.py",
        "tests/host/hermes/test_audience_isolation.py",
        "tests/host/hermes/test_bounded_corrections.py",
        "tests/host/hermes/test_dedupe.py",
        "tests/host/hermes/test_steers.py",
        "tests/host/hermes/test_identity.py",
        "tests/host/hermes/test_local_surfaces.py",
        "tests/host/hermes/test_a2a_prepare.py",
        "tests/host/hermes/test_prefetch.py",
        "tests/host/hermes/test_real_host_loader.py",
        "tests/host/hermes/test_runtime_bounds.py",
        "tests/host/hermes/test_hook_budgets.py",
        "tests/host/hermes/test_runtime_wiring.py",
        "tests/host/hermes/test_operator_tools.py",
        "tests/host/hermes/test_two_entries_one_store.py",
        "tests/host/hermes/test_reinjection.py",
        "tests/host/test_runtime_config_threshold.py",
        "tests/host/codex/test_env_file_credentials.py",
        "tests/host/codex/test_hooks.py",
        "tests/host/codex/test_hook_budgets.py",
        "tests/host/codex/test_lifecycle_worker_wakeup.py",
        "tests/host/codex/test_mcp.py",
        "tests/host/codex/test_runtime_wiring.py",
        "tests/host/codex/test_shared_client.py",
        "tests/host/codex/test_clients_only_store.py",
        "tests/host/codex/test_remote.py",
        "tests/host/codex/test_dsh_remote.py",
        "tests/host/codex/test_resident_recall.py",
    ],
    "migration": ["tests/migration/test_migration_drills.py"],
    "packaging": [
        "tests/packaging/test_clean_v11_wheel.py",
        "tests/packaging/test_package_upgrade.py",
        "tests/packaging/test_install_v11.py",
        "tests/packaging/test_memory_skill.py",
        "tests/packaging/test_shared_cli.py",
        "tests/packaging/test_windows_hook_command.py",
        *SCRIPT_GATE_TESTS,
    ],
    "integration": [
        "tests/contract/test_worker.py",
        "tests/contract/test_worker_incremental_batch.py",
        *GIT_TESTS,
        "tests/contract/test_runtime_auxiliary.py",
        "tests/contract/test_monthly_auxiliary_budget.py",
        "tests/contract/test_codex_cli_consolidation.py",
        "tests/contract/test_runtime_worker_entry.py",
        "tests/contract/test_running_code.py",
        "tests/contract/test_coverage_gaps.py",
        "tests/contract/test_duplicate_collapse.py",
        "tests/contract/test_embedding_retry.py",
        "tests/contract/test_recall_probe_set.py",
        "tests/contract/test_candidate_debounce.py",
        "tests/contract/test_evidence_question.py",
        "tests/contract/test_subject_binding.py",
        "tests/contract/test_corroboration.py",
        "tests/contract/test_confirmation.py",
        "tests/contract/test_failure_retry.py",
        "tests/contract/test_embedding_budget.py",
        "tests/integration/test_lance_retrieval.py",
        "tests/integration/test_fault_probes.py",
        "tests/integration/faults/test_storage_full.py",
    ],
    "release": [],
    "eval": [],
}

# The named tiers are deliberately small and explicit.  ``SUITES`` remains a
# public compatibility surface for callers that patch or inspect it, while the
# metadata below tells the selector what a tier can and cannot prove.
TIER_METADATA = {
    "unit": {"level": "T0", "requires": [], "model_calls": False},
    "contract": {"level": "T1", "requires": [], "model_calls": False},
    "storage": {"level": "T1", "requires": [], "model_calls": False},
    "capture": {"level": "T1", "requires": [], "model_calls": False},
    "claims": {"level": "T1", "requires": [], "model_calls": False},
    "deletion": {"level": "T1", "requires": [], "model_calls": False},
    "episodes": {"level": "T1", "requires": [], "model_calls": False},
    "retrieval": {"level": "T1", "requires": [], "model_calls": False},
    "native": {"level": "T2", "requires": ["native"], "model_calls": False},
    "host": {"level": "T2", "requires": ["hermes", "codex"], "model_calls": False},
    "migration": {"level": "T2", "requires": ["migration"], "model_calls": False},
    "packaging": {"level": "T3", "requires": ["clean_wheel"], "model_calls": False},
    "integration": {"level": "T2", "requires": ["native", "hermes", "codex"], "model_calls": False},
    "release": {
        "level": "T3",
        "requires": ["native", "hermes", "codex", "migration", "clean_wheel"],
        "model_calls": False,
    },
    "eval": {"level": "T4", "requires": ["model"], "model_calls": True},
}

#: The tiers whose runs allow child processes and loopback sockets: the tiers of ``TIER_ALLOWS``.
#: ``tests/v11_guard.py`` reads the same table and refuses both to every other tier, so a test that starts a process or
#: opens a socket can only pass in one of these.
PROCESS_TIERS = frozenset(TIER_ALLOWS)

# These are the deterministic checks that protect the authority and privacy
# boundary when a change affects writes, identity, deletion, or time.  They are
# intentionally separate from semantic/model evaluation.  None of them starts a
# child process or opens a socket, so every tier can run them.
SAFETY_BASELINE = [
    "tests/unit/test_trusted_context.py",
    "tests/contract/test_synthetic_inputs.py",
    "tests/contract/test_protocol.py",
    "tests/contract/test_storage.py",
    "tests/contract/test_deletion.py",
    "tests/contract/test_recall_admission.py",
]


def _dedupe(paths: list[str]) -> list[str]:
    return list(dict.fromkeys(paths))


# The I-contract bottom line is explicit so a path-based selector cannot
# accidentally turn an authority change into a surface-only test.  These are
# existing deterministic tests, not a new scheduler or a semantic benchmark.
I_SAFETY_MATRIX = {
    "I01": [
        "tests/unit/test_trusted_context.py",
        "tests/host/hermes/test_identity.py",
        "tests/host/codex/test_hooks.py",
    ],
    "I03": ["tests/contract/test_capture.py", "tests/contract/test_worker.py"],
    "I04": [
        "tests/contract/test_capture.py",
        "tests/contract/test_episodes.py",
        "tests/host/hermes/test_dedupe.py",
        "tests/host/hermes/test_steers.py",
    ],
    "I06": [
        "tests/contract/test_claims.py",
        "tests/contract/test_episode_authority.py",
        "tests/contract/test_recall_admission.py",
    ],
    "I07": [
        "tests/contract/test_deletion.py",
        "tests/contract/test_worker.py",
        "tests/contract/test_lance_embed_fence.py",
    ],
    "I08": [
        "tests/contract/test_retrieval_pipeline.py",
        "tests/contract/test_recall_admission.py",
        "tests/contract/test_retrieval_history_state.py",
        "tests/contract/test_vector_timeout_fallback.py",
        "tests/integration/test_lance_retrieval.py",
    ],
    "I09": [
        "tests/contract/test_storage.py",
        "tests/contract/test_deletion.py",
        "tests/contract/test_worker.py",
        "tests/contract/test_worker_incremental_batch.py",
    ],
    "I10": [
        "tests/contract/test_worker.py",
        "tests/contract/test_worker_incremental_batch.py",
        "tests/contract/test_lance_embed_fence.py",
        "tests/contract/test_retained_lock_deadline.py",
    ],
    # Codex MCP requires its optional extra and belongs to the Codex host tier;
    # Core safety selection must remain runnable in the base test environment.
    "I14": ["tests/contract/test_synthetic_inputs.py", "tests/host/hermes/test_audience_isolation.py"],
}
I_CORE_BASELINE = _dedupe([path for paths in I_SAFETY_MATRIX.values() for path in paths])
# The I-contract baseline of the tiers that refuse child processes: none of these tests starts a process or opens a
# socket.  I_CORE_BASELINE also holds host and native tests that do.
CONTRACT_I_BASELINE = [
    "tests/contract/test_retrieval_history_state.py",
    "tests/unit/test_trusted_context.py",
    "tests/contract/test_synthetic_inputs.py",
    "tests/contract/test_protocol.py",
    "tests/contract/test_capture.py",
    "tests/contract/test_claims.py",
    "tests/contract/test_episodes.py",
    "tests/contract/test_episode_authority.py",
    "tests/contract/test_deletion.py",
    "tests/contract/test_storage.py",
    "tests/contract/test_retrieval_pipeline.py",
    "tests/contract/test_evidence_followup.py",
    "tests/contract/test_recall_admission.py",
    "tests/contract/test_worker.py",
    "tests/contract/test_worker_incremental_batch.py",
    "tests/contract/test_operator_retry.py",
    "tests/contract/test_recall_budget_config.py",
]
# The contracts a change to claims, episodes or time calls for.  They need no process; the migration drills that
# complete the closure run in the migration tier.
CLAIMS_TIME_CONTRACT_CLOSURE = [
    "tests/contract/test_claims.py",
    "tests/contract/test_episode_authority.py",
    "tests/contract/test_episodes.py",
    "tests/contract/test_recall_admission.py",
    "tests/contract/test_consolidation_input.py",
]
CORE_RELEASE_CONTRACTS = [
    "tests/contract/test_retrieval_history_state.py",
    "tests/contract/test_synthetic_inputs.py",
    "tests/contract/test_protocol.py",
    "tests/contract/test_capture.py",
    "tests/contract/test_claims.py",
    "tests/contract/test_episodes.py",
    "tests/contract/test_episode_authority.py",
    "tests/contract/test_consolidation_input.py",
    "tests/contract/test_storage.py",
    "tests/contract/test_deletion.py",
    "tests/contract/test_retained_artifacts.py",
    "tests/contract/test_retrieval_pipeline.py",
    "tests/contract/test_evidence_followup.py",
    "tests/contract/test_auto_query_echo.py",
    "tests/contract/test_vector_timeout_fallback.py",
    "tests/contract/test_recall_admission.py",
    "tests/contract/test_worker.py",
    "tests/contract/test_worker_incremental_batch.py",
    "tests/contract/test_operator_retry.py",
    "tests/contract/test_recall_budget_config.py",
]
# Every contract file under tests/contract must be selected by some tier
# (tests/packaging/test_check_selection.py enforces it).  These run in both
# ``integration`` and ``release`` because ``release`` is not built from
# ``SUITES["integration"]``; a file listed only there would run in CI and not in
# the gate that decides whether something ships.
CORE_RELEASE_CONTRACTS += [
    "tests/test_vector_runtime.py",
    "tests/contract/test_candidate_debounce.py",
    "tests/contract/test_candidate_evidence_window.py",
    "tests/contract/test_confirmation.py",
    "tests/contract/test_corroboration.py",
    "tests/contract/test_coverage_gaps.py",
    "tests/contract/test_duplicate_collapse.py",
    "tests/contract/test_embedding_budget.py",
    "tests/contract/test_embedding_retry.py",
    "tests/contract/test_evidence_question.py",
    "tests/contract/test_failure_retry.py",
    "tests/contract/test_recall_probe_set.py",
    "tests/contract/test_running_code.py",
    "tests/contract/test_subject_binding.py",
    "tests/contract/test_vector_failure.py",
    "tests/contract/test_audit_lifecycle.py",
    "tests/contract/test_audit_maintenance.py",
    "tests/contract/test_private_mode.py",
    "tests/contract/test_autonomous_admission.py",
    "tests/contract/test_autonomous_context.py",
    "tests/contract/test_autonomous_corrections.py",
    "tests/contract/test_autonomous_recall.py",
    "tests/contract/test_autonomous_worker.py",
    "tests/contract/test_comprehensive_storage_diagnostics.py",
    "tests/contract/test_http_proxy_boundary.py",
    "tests/contract/test_embedding_space_policy.py",
    "tests/contract/test_recall_packet_compiler.py",
    "tests/contract/test_quality_audit.py",
    "tests/contract/test_candidate_lifecycle.py",
    "tests/contract/test_host_recall_boundaries.py",
    "tests/contract/test_host_principal_injection.py",
    "tests/contract/test_runtime_cross_component.py",
    "tests/contract/test_claim_acceptance_semantics.py",
    "tests/contract/test_runtime_audit.py",
    "tests/contract/test_runtime_request_limits.py",
    "tests/contract/test_claim_semantics.py",
    "tests/contract/test_consolidation_chunks.py",
    "tests/contract/test_recall_selection.py",
    "tests/contract/test_trace.py",
    "tests/contract/test_profile_entity.py",
    "tests/contract/test_recall_dating_and_order.py",
    "tests/contract/test_provider_hold.py",
    "tests/contract/test_worker_idle_backoff.py",
    "tests/contract/test_source_page_triggers.py",
    "tests/contract/test_summary_isolation.py",
    "tests/contract/test_evidence_trigger_precision.py",
    "tests/contract/test_writer_busy_pass.py",
    "tests/contract/test_identifier_forms.py",
    "tests/contract/test_interrupted_attempts.py",
    "tests/contract/test_turn_replies.py",
    "tests/contract/test_candidate_identity.py",
    "tests/contract/test_queue_backpressure.py",
    "tests/contract/test_empty_episode.py",
    "tests/contract/test_rejection_names_the_field.py",
    "tests/contract/test_embed_batch.py",
    "tests/contract/test_pass_statistics.py",
    "tests/contract/test_pass_size.py",
    "tests/contract/test_processing_loop_survives.py",
    "tests/contract/test_embed_group_bookkeeping.py",
    "tests/contract/test_tool_output_is_not_derived.py",
    "tests/contract/test_retire_rootless_claims.py",
]

# The runtime's boundaries with the processes and sockets it uses.  Most of these tests start a child process or open a
# loopback socket, so a process tier runs all of them.
RUNTIME_BOUNDARY_TESTS = [
    "tests/contract/test_http_transport_boundary.py",
    "tests/contract/test_codex_cli_consolidation.py",
    "tests/contract/test_responses_consolidation.py",
    "tests/contract/test_runtime_auxiliary.py",
    "tests/contract/test_runtime_worker_entry.py",
    "tests/host/test_runtime_watchdog.py",
    "tests/host/test_runtime_wiring_common.py",
    "tests/contract/test_finite_supervisor.py",
    "tests/contract/test_group_publish.py",
    "tests/contract/test_receipt_larger_than_a_pipe.py",
    "tests/contract/test_restart_and_failure_recovery.py",
]
# The host adapters' tests.  Some start child processes or open loopback sockets, so a process tier runs all of them.
HERMES_HOST_TESTS = [path for path in SUITES["host"] if "/hermes/" in path]
CODEX_HOST_TESTS = [path for path in SUITES["host"] if "/codex/" in path]
INTEGRATION_STABLE_BASELINE = _dedupe(list(SUITES["native"]) + list(SUITES["host"]) + list(SUITES["migration"]))

#: The directories and files that decide an impact by where they are rather than by what their names hold.  A directory
#: ends in ``/``.  Every entry is in the checkout, which tests/packaging/test_check_selection.py holds them to.  The
#: native entries are the Lance store and its helper process and the runtime modules that start and feed them: what the
#: native suite is about.
IMPACT_PATHS = {
    "quiet": ("verification/", "docs/", "fixtures/"),
    "tests": ("tests/",),
    "selector": ("scripts/check.py", "scripts/tier_allows.py"),
    "packaging": ("pyproject.toml", "MANIFEST.in", "packaging/", "packaging_hooks/"),
    "host": ("adapters/", "runtime/"),
    "runtime": ("runtime/",),
    "hermes": ("adapters/hermes/",),
    "clients": ("adapters/clients/", "adapters/codex/"),
    "native": (
        "vector/",
        "_lance_worker.py",
        "runtime/lance_port.py",
        "runtime/vector_upkeep.py",
        "runtime/instance.py",
        "runtime/instance_config.py",
    ),
}

_IMPACT_PATTERNS = {
    "scope": ("scope", "identity", "path", "binding", "visibility"),
    "write": ("capture", "mutate", "storage", "schema", "claim", "episode", "artifact", "reference", "worker", "queue"),
    "claims": ("claim",),
    "episodes": ("episode",),
    "delete": ("delete", "purge", "forget", "restore"),
    "time": ("time", "temporal", "deadline", "freshness", "correction", "alias"),
    "read": ("recall", "retrieval", "policy"),
}


def _changed_paths(args: argparse.Namespace) -> tuple[list[str], list[str]]:
    """Return changed paths and any selector diagnostics.

    Explicit paths are useful in CI and do not require a base checkout.  The
    older ``--changed-base`` form remains supported, including untracked files.
    """
    diagnostics: list[str] = []
    explicit = [str(Path(item).as_posix()) for item in (args.changed_files or [])]
    if explicit:
        return sorted(set(explicit)), diagnostics
    if not args.changed_base:
        return [], diagnostics
    changed = subprocess.run(
        ["git", "diff", "--name-only", args.changed_base, "--"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if changed.returncode:
        diagnostics.append(f"invalid_base:{changed.returncode}")
        return [], diagnostics
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(set(changed.stdout.splitlines()) | set(untracked.stdout.splitlines())), diagnostics


def _source_manifest() -> dict[str, str]:
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout.split(b"\0")
    names: list[str] = []
    for raw_name in listed:
        if not raw_name:
            continue
        if b"\n" in raw_name or b"\r" in raw_name:
            raise RuntimeError("source manifest cannot safely hash a path containing a newline")
        try:
            path = raw_name.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError("source manifest encountered a non-UTF-8 Git path") from exc
        if not path.startswith(("verification/", "docs/")):
            names.append(path)
    names = sorted(set(names))
    existing = [path for path in names if (ROOT / path).is_file()]
    missing = [path for path in names if not (ROOT / path).is_file()]
    manifest = {path: "missing" for path in missing}
    if existing:
        hashed = subprocess.run(
            ["git", "-c", "core.autocrlf=input", "hash-object", "--stdin-paths"],
            cwd=ROOT,
            input=(b"\n".join(path.encode("utf-8") for path in existing) + b"\n"),
            capture_output=True,
            check=True,
        ).stdout.splitlines()
        if len(hashed) != len(existing):
            raise RuntimeError("Git returned an incomplete source manifest")
        manifest.update({path: f"git-blob:{oid.decode('ascii')}" for path, oid in zip(existing, hashed)})
    return {path: manifest[path] for path in names}


def _source_inputs_sha256(manifest: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()


def _at(path: str, where: str) -> bool:
    """Whether the lowercased ``path`` is an entry of ``IMPACT_PATHS[where]`` or inside one of its directories."""
    return any(
        path.startswith(entry.lower()) if entry.endswith("/") else path == entry.lower()
        for entry in IMPACT_PATHS[where]
    )


def _impact(changed: list[str]) -> tuple[set[str], list[str]]:
    impacts: set[str] = set()
    unknown: list[str] = []
    for raw in changed:
        path = raw.replace("\\", "/").lower()
        # ``.execution/`` is local scratch the repository ignores, so it is not an entry that must exist.
        if _at(path, "quiet") or path.startswith(".execution/"):
            continue
        if _at(path, "tests"):
            # Test-only edits should run that test directly when it exists;
            # their production impact is not inferred from the filename.
            continue
        if _at(path, "selector"):
            impacts.add("selector")
            continue
        if _at(path, "packaging"):
            impacts.update({"packaging", "unknown_config"})
            continue
        path_impacts: set[str] = set()
        for label, fragments in _IMPACT_PATTERNS.items():
            if any(fragment in path for fragment in fragments):
                impacts.add(label)
                path_impacts.add(label)
        if _at(path, "host"):
            impacts.update({"scope", "write", "host"})
            path_impacts.update({"scope", "write", "host"})
            if _at(path, "hermes"):
                impacts.add("hermes_host")
                path_impacts.add("hermes_host")
            elif _at(path, "clients"):
                impacts.add("codex_host")
                path_impacts.add("codex_host")
            else:
                impacts.add("host_both")
                path_impacts.add("host_both")
            if _at(path, "runtime") or path.endswith("runtime_wiring.py"):
                impacts.add("runtime_boundary")
                path_impacts.add("runtime_boundary")
        if _at(path, "native"):
            impacts.add("native")
            path_impacts.add("native")
        if not path_impacts.intersection(
            {
                "scope",
                "write",
                "delete",
                "time",
                "read",
                "claims",
                "episodes",
                "packaging",
                "unknown_config",
                "host",
                "host_both",
                "hermes_host",
                "codex_host",
                "runtime_boundary",
                "native",
            }
        ):
            unknown.append(raw)
    return impacts, unknown


def _process_groups() -> list[tuple[str, list[str], frozenset[str]]]:
    """The tests that start child processes, open loopback sockets, run git or drive the packaging helpers, as (the tier
    that owns them, the tests, what a tier's runs must allow for them to pass).  A test in none of them is taken to
    need nothing."""
    process = frozenset({"process"})
    return [
        ("native", SUITES["native"], process),
        ("host", SUITES["host"], process),
        ("migration", SUITES["migration"], process),
        ("packaging", SCRIPT_GATE_TESTS, process | {"git"}),
        ("packaging", [path for path in SUITES["packaging"] if path not in SCRIPT_GATE_TESTS], process | {"build"}),
        ("integration", RUNTIME_BOUNDARY_TESTS, process),
        ("integration", GIT_TESTS, process | {"git"}),
    ]


def needs(tests: list[str]) -> frozenset[str]:
    """What a tier's runs must allow for ``tests`` to pass: the union over the groups they belong to."""
    return frozenset().union(
        *(wanted for _owner, group, wanted in _process_groups() if not set(group).isdisjoint(tests))
    )


def _runnable(tier: str, tests: list[str]) -> list[str]:
    """``tests`` when ``tier``'s runs allow everything they need, else none: a list goes into a run whole or not at
    all."""
    return tests if needs(tests) <= TIER_ALLOWS.get(tier, frozenset()) else []


def _split_changed_tests(tier: str, changed_tests: list[str]) -> tuple[list[str], list[str]]:
    """The changed tests ``tier`` can run, and the tiers that own the others.

    A test in a group whose needs ``tier``'s runs do not allow is left to the tier that owns the group.
    """
    if not changed_tests:
        return [], []
    allowed = TIER_ALLOWS.get(tier, frozenset())
    groups = _process_groups()
    left = {
        path: owner
        for path in changed_tests
        for owner, group, wanted in groups
        if path in group and not wanted <= allowed
    }
    return [path for path in changed_tests if path not in left], _dedupe(list(left.values()))


def select_tests(tier: str, *, changed: list[str] | None = None) -> tuple[list[str], dict]:
    """Select a bounded suite and report why safety gates were added.

    A run selects only tests its tier's policy allows (``TIER_ALLOWS``): a list a change calls for goes into a run when
    the policy allows everything its tests need (``needs``), and a changed test goes into a run the same way.  What is
    left out is named by the tier that can run it in ``required_tiers``, whose names are keys of ``SUITES``.
    """
    changed = changed or []
    if tier == "release":
        selected = _dedupe(
            list(SUITES["unit"])
            + list(SUITES["contract"])
            + list(SUITES["native"])
            + list(SUITES["host"])
            + list(SUITES["migration"])
            + list(SUITES["packaging"])
        )
    else:
        selected = list(SUITES.get(tier, []))
    changed_tests = [
        path for path in changed if path.startswith("tests/") and path.endswith(".py") and (ROOT / path).is_file()
    ]
    changed_tests, left_to = _split_changed_tests(tier, changed_tests)
    if changed_tests:
        selected = _dedupe(selected + changed_tests)
    impacts, unknown = _impact(changed)
    reasons = ["changed_tests_owned_by_process_tiers"] if left_to else []
    required_tiers = [tier, *left_to]
    if tier in {"integration", "release"}:
        selected = _dedupe(selected + CORE_RELEASE_CONTRACTS + RUNTIME_BOUNDARY_TESTS + SCRIPT_GATE_TESTS)
    if tier == "integration":
        # A clean checkout has no diff to drive impact inference. Integration
        # is the CI baseline, so it retains native, host, and migration
        # coverage even when ``changed=[]``.
        selected = _dedupe(selected + INTEGRATION_STABLE_BASELINE)
        required_tiers.extend(["native", "host", "migration"])
        reasons.append("integration_stable_native_host_migration_baseline")
    if tier in {"unit", "contract", "storage", "capture", "claims", "deletion", "episodes", "retrieval"}:
        host_impacts = {"host", "host_both", "hermes_host", "codex_host", "runtime_boundary"}
        authority_impacts = impacts - host_impacts
        production_changed = [
            path.replace("\\", "/") for path in changed if not path.replace("\\", "/").startswith("tests/")
        ]
        host_only_change = bool(production_changed) and all(
            path.startswith(("adapters/", "runtime/")) for path in production_changed
        )
        if authority_impacts.intersection({"scope", "write", "delete", "time"}) and not host_only_change:
            selected = _dedupe(CONTRACT_I_BASELINE + selected)
            reasons.append("I01_I03_I04_I06_I07_I08_I09_I10_I14_safety_baseline")
        if "read" in impacts and tier != "retrieval":
            selected = _dedupe(selected + SUITES["retrieval"])
            reasons.append("retrieval_for_read_path_change")
    if impacts.intersection({"claims", "episodes", "time"}):
        selected = _dedupe(selected + CLAIMS_TIME_CONTRACT_CLOSURE)
        required_tiers.append("migration")
        reasons.append("claims_time_current_history_candidate_and_migration_closure")
    if "host" in impacts:
        host_selected = []
        if "hermes_host" in impacts or "host_both" in impacts:
            host_selected.extend(HERMES_HOST_TESTS)
        if "codex_host" in impacts or "host_both" in impacts:
            host_selected.extend(CODEX_HOST_TESTS)
        host_tests = _runnable(tier, host_selected)
        selected = _dedupe(selected + host_tests + SAFETY_BASELINE)
        required_tiers.append("host")
        reasons.append(
            "host_specific_contract_and_core_safety_baseline" if host_tests else "core_safety_baseline_for_host_change"
        )
    if "runtime_boundary" in impacts:
        selected = _dedupe(selected + _runnable(tier, RUNTIME_BOUNDARY_TESTS))
        required_tiers.append("integration")
        reasons.append("runtime_boundary_contracts")
    if "native" in impacts:
        selected = _dedupe(selected + _runnable(tier, SUITES["native"] + I_CORE_BASELINE))
        required_tiers.append("native")
        reasons.append("native_boundary_for_vector_change")
    if impacts.intersection({"packaging", "unknown_config"}):
        required_tiers.extend(["integration", "packaging"])
        selected = _dedupe(selected + _runnable(tier, SUITES["integration"]) + _runnable(tier, SUITES["packaging"]))
        reasons.append("integration_and_build_for_packaging_or_config")
    if unknown:
        required_tiers.extend(["integration", "packaging"])
        selected = _dedupe(selected + _runnable(tier, SUITES["integration"]) + _runnable(tier, SUITES["packaging"]))
        reasons.append("unknown_dependency_requires_conservative_integration_and_build")
    if changed and not unknown and not impacts and tier not in {"unit", "contract"}:
        # A changed test is still a valid direct selection; no silent empty
        # selection is allowed.
        selected = _dedupe(changed_tests) or selected
    return selected, {
        "changed_files": changed,
        "impacts": sorted(impacts),
        "unknown_files": unknown,
        "required_tiers": _dedupe(required_tiers),
        "reasons": reasons,
        "metadata": TIER_METADATA.get(tier, {}),
    }


def _selection_output(
    tier: str,
    selected: list[str],
    details: dict,
    *,
    status: str,
) -> dict:
    metadata = TIER_METADATA.get(tier, {})
    missing = []
    if tier == "release" and status in {"planned_not_executed", "listed_not_executed"}:
        # A release plan names what the run needs; listing it proves none of it.
        missing = list(metadata.get("requires", []))
    payload = {
        "status": status,
        "tier": tier,
        "level": metadata.get("level"),
        "selected": selected,
        "selection": details,
        "required_gates": list(metadata.get("requires", [])),
        "missing_gates": missing,
        "model_calls": bool(metadata.get("model_calls", False)),
    }
    return payload


def _safe_error(exc: BaseException) -> dict[str, str]:
    reason = " ".join(str(exc).split()) or "no detail"
    return {"type": type(exc).__name__, "reason": reason[:240]}


def _as_text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _test_environment(tier: str, isolated: Path) -> dict[str, str]:
    """A clean, isolated environment for one pytest run: only the OS essentials,
    every user/config/temp location redirected under ``isolated``."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper()
        in {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS"}
    }
    for key in (
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "TEMP",
        "TMP",
        "HERMES_HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
    ):
        target = isolated / key.lower()
        target.mkdir()
        env[key] = str(target)
    test_root = ROOT / "tests"
    test_import_paths = (test_root, test_root / "contract", test_root / "migration", ROOT)
    env.update(
        PYTHONUTF8="1",
        PYTHONDONTWRITEBYTECODE="1",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PYTHONPATH=os.pathsep.join(str(path) for path in test_import_paths),
        SCOPE_RECALL_TEST_BOUNDARY_PARENT=str(isolated),
        SCOPE_RECALL_TEST_PROTECTED_HOME=str(Path.home()),
        SCOPE_RECALL_ACTIVE_HERMES_HOME=str(isolated / "protected-unused"),
        SCOPE_RECALL_REAL_HOME=str(isolated / "unused-real"),
        SCOPE_RECALL_TEST_TIER=tier,
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
    )
    env.update(packaging_helper_env(tier))
    env.update(node_helper_env(tier))
    if tier in PROCESS_TIERS:
        # Process tiers enable v11_guard's allowlisted TEST child-process path;
        # network and protected-file checks remain active in the parent.
        env["SCOPE_RECALL_TEST_ALLOW_OWNED_SUBPROCESSES"] = "1"
        env["SCOPE_RECALL_TEST_ALLOW_LOOPBACK"] = "1"
    return env


def _junit_counts(junit: Path) -> dict[str, int]:
    counts = {"passed": 0, "failed": 0, "skipped": 0}
    if junit.is_file():
        cases = ET.parse(junit).findall(".//testcase")
        counts["failed"] = sum(c.find("failure") is not None or c.find("error") is not None for c in cases)
        counts["skipped"] = sum(c.find("skipped") is not None for c in cases)
        counts["passed"] = len(cases) - counts["failed"] - counts["skipped"]
    return counts


def _evidence_scope(tier: str, task: str) -> str:
    if tier in PROCESS_TIERS:
        return f"{task} selected local contracts and bounded TEST process boundaries; no model/API semantic evaluation"
    return f"{task} selected local in-process contracts only; no model/API semantic evaluation"


def _early_exit(tier, selected, selection, *, status, missing_gates=None, **extra) -> int:
    payload = _selection_output(tier, selected, selection, status=status)
    if missing_gates is not None:
        payload["missing_gates"] = missing_gates
    payload.update(extra)
    print(json.dumps(payload, ensure_ascii=False))
    return 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tier", required=True, choices=SUITES)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--plan", action="store_true", help="show selection and required gates without executing")
    parser.add_argument("--changed-base")
    parser.add_argument("--changed-files", nargs="*", default=None)
    parser.add_argument("--allow-model-calls", action="store_true")
    parser.add_argument("--task", choices=[f"P{i:02d}" for i in range(20)], default="P01")
    args = parser.parse_args()
    changed, change_diagnostics = _changed_paths(args)
    if change_diagnostics:
        print(json.dumps({"status": "invalid_base", "diagnostics": change_diagnostics}, ensure_ascii=False))
        return 2
    selected, selection = select_tests(args.tier, changed=changed)
    selection["diagnostics"] = change_diagnostics
    if TIER_METADATA.get(args.tier, {}).get("model_calls") and not args.allow_model_calls:
        return _early_exit(
            args.tier,
            selected,
            selection,
            status="model_calls_disabled",
            missing_gates=["explicit_model_authorization"],
        )
    if args.tier == "release" and not distribution_is_installed():
        # Release asks the doctor whether a host can actually reach this
        # provider, and the doctor probes the interpreter for the
        # ``hermes_agent.memory_providers`` entry point in isolated mode, which
        # sees installed distributions only.  Say so here instead of failing
        # later on an assertion that names neither the cause nor the cure.
        return _early_exit(
            args.tier,
            selected,
            selection,
            status="distribution_not_installed",
            missing_gates=["installed_distribution"],
            remedy=f"uv pip install -e . --no-deps --python {sys.executable}",
        )
    if not selected:
        print(
            json.dumps(_selection_output(args.tier, selected, selection, status="not_implemented"), ensure_ascii=False)
        )
        return 2
    missing = [p for p in selected if not (ROOT / p).is_file()]
    if missing:
        return _early_exit(args.tier, selected, selection, status="missing_tests", missing_tests=missing)
    if args.list or args.plan:
        status = "planned_not_executed" if args.plan else "listed_not_executed"
        print(json.dumps(_selection_output(args.tier, selected, selection, status=status), ensure_ascii=False))
        return 0
    return _execute(args, selected, selection)


def pytest_command(selected: list[str], junit: Path) -> list[str]:
    """Keep each directory's files together so pytest's conftest fixtures and their users share a collector.

    Re-entering a directory after selecting a file in its parent can create another collector for it. Normalize
    separators before sorting so Windows selections form the same contiguous subtrees as POSIX selections.
    """
    return [
        sys.executable,
        "-B",
        "-m",
        "pytest",
        *sorted({path.replace("\\", "/") for path in selected}),
        "-q",
        "--import-mode=importlib",
        "-p",
        "v11_guard",
        "-p",
        "no:cacheprovider",
        "--durations=10",
        f"--junitxml={junit}",
    ]


def _execute(args, selected: list[str], selection: dict) -> int:
    """Run the selected files under the watchdog, always clean up, always write a receipt.

    The receipt records pytest, wrapper and cleanup outcomes separately with
    the transition history that led to them; a failed cleanup or a missing
    required gate fails the run even when every test passed.
    """
    evidence = ROOT / "verification" / args.task
    evidence.mkdir(parents=True, exist_ok=True)
    # Keep the owned TEST root short on Windows.  Packaging install probes use
    # atomic files and otherwise cross the Win32 path-length boundary before
    # v11_guard can make its normal allow/deny decision.
    temp_root = Path(tempfile.gettempdir())
    if os.name == "nt":
        temp_root = Path(temp_root.anchor) / "Temp"
    parent = temp_root / "sr"
    parent.mkdir(parents=True, exist_ok=True)
    manifest = _source_manifest()
    stem = f"{args.tier}-{time.time_ns()}"
    log = evidence / f"{stem}.log"
    command: list[str] = []
    output_parts: list[str] = []
    counts = {"passed": 0, "failed": 0, "skipped": 0}
    elapsed = 0.0
    pytest_exit_code = 2
    wrapper_exit_code = 0
    pytest_state = "not_started"
    wrapper_error = None
    cleanup_exit_code = 0
    cleanup_state = "not_started"
    cleanup_error = None
    transitions = ["prepared"]
    directory = None
    watchdog_seconds = pytest_watchdog_seconds(args.tier, len(selected), selected)
    try:
        directory = TestDirectory(prefix="TEST-v11-", dir=parent)
        isolated = Path(directory.name if hasattr(directory, "name") else directory)
        env = _test_environment(args.tier, isolated)
        junit = isolated / "junit.xml"
        command = pytest_command(selected, junit)
        transitions.append("pytest_started")
        start = time.perf_counter()
        try:
            result = subprocess.run(
                command,
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=watchdog_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            result = subprocess.CompletedProcess(
                command, 124, _as_text(exc.stdout), f"TEST_TIMEOUT after {watchdog_seconds} seconds\n"
            )
        elapsed = time.perf_counter() - start
        output_parts.extend((_as_text(result.stdout), _as_text(result.stderr)))
        counts = _junit_counts(junit)
        pytest_exit_code = (
            result.returncode if result.returncode else (0 if counts["passed"] and not counts["failed"] else 2)
        )
        pytest_state = "completed"
        transitions.append("pytest_completed_waiting_cleanup")
    except Exception as exc:
        pytest_exit_code = wrapper_exit_code = 2
        pytest_state = "wrapper_failed"
        wrapper_error = _safe_error(exc)
        output_parts.append(json.dumps({"transition": "pytest_wrapper_failed", "error": wrapper_error}, sort_keys=True))
        transitions.append("pytest_wrapper_failed_waiting_cleanup")
    finally:
        if directory is None:
            cleanup_exit_code = 3
            cleanup_error = {"type": "CleanupNotStarted", "reason": "test directory was not created"}
            transitions.append("cleanup_not_started")
        else:
            try:
                directory.cleanup()
                cleanup_state = "succeeded"
                transitions.append("cleanup_succeeded")
            except Exception as exc:
                cleanup_exit_code = 3
                cleanup_state = "failed"
                cleanup_error = _safe_error(exc)
                output_parts.append(
                    json.dumps({"transition": "cleanup_failed", "error": cleanup_error}, sort_keys=True)
                )
                transitions.append("cleanup_failed")

    overall_code = cleanup_exit_code or wrapper_exit_code or pytest_exit_code
    transitions.append("receipt_written")
    log_lines = [part for part in output_parts if part]
    log_lines.append(
        json.dumps(
            {
                "transition_history": transitions,
                "pytest_exit_code": pytest_exit_code,
                "wrapper_exit_code": wrapper_exit_code,
                "cleanup_exit_code": cleanup_exit_code,
                "overall_exit_code": overall_code,
            },
            sort_keys=True,
        )
    )
    log.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    receipt = {
        "command": command,
        "exit_code": overall_code,
        "overall_exit_code": overall_code,
        "pytest_exit_code": pytest_exit_code,
        "wrapper_exit_code": wrapper_exit_code,
        "cleanup_exit_code": cleanup_exit_code,
        "pytest": {"state": pytest_state, "exit_code": pytest_exit_code, "error": None},
        "wrapper": {
            "state": "failed" if wrapper_exit_code else "succeeded",
            "exit_code": wrapper_exit_code,
            "error": wrapper_error,
        },
        "cleanup": {"state": cleanup_state, "exit_code": cleanup_exit_code, "error": cleanup_error},
        "transition_history": transitions,
        "duration_seconds": elapsed,
        "watchdog_seconds": watchdog_seconds,
        **counts,
        "selected": selected,
        "selection": selection,
        "required_gates": list(TIER_METADATA.get(args.tier, {}).get("requires", [])),
        "missing_gates": [],
        "log": log.relative_to(ROOT).as_posix(),
        "model_calls": bool(args.allow_model_calls and TIER_METADATA.get(args.tier, {}).get("model_calls", False)),
        "process_policy": "v11_guard_with_allowlisted_test_subprocesses"
        if args.tier in PROCESS_TIERS
        else "v11_guard_no_child_processes",
        "test_data": "synthetic_only",
        "source_base": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "source_inputs_sha256": _source_inputs_sha256(manifest),
        "source_manifest": f"verification/{args.task}/{stem}-inputs.json",
        "evidence_scope": _evidence_scope(args.tier, args.task),
    }
    (evidence / f"{stem}-inputs.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (evidence / f"{stem}.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print("".join(output_parts), end="")
    print(json.dumps(receipt))
    return overall_code


if __name__ == "__main__":
    raise SystemExit(main())
