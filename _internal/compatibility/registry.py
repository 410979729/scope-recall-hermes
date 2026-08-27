"""Single inventory of compatibility retained by Program 1A."""

from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True, slots=True)
class CompatibilityShim:
    shim_id: str
    owner: str
    source: str
    replacement: str
    usage_evidence: tuple[str, ...]
    remove_after: str
    removal_condition: str
    tests: tuple[str, ...]


COMPATIBILITY_REGISTRY: tuple[CompatibilityShim, ...] = (
    CompatibilityShim(
        shim_id="provider-command-adapter",
        owner="program-1a-runtime-boundary",
        source="_internal/runtime/command_adapter.py:ProviderCommandAdapter",
        replacement="MemoryCommandApplication plus infrastructure UoW ports",
        usage_evidence=(
            "_internal/runtime/composition.py:RuntimeComposition",
            "provider.py:def _store_now",
        ),
        remove_after="2.1.0",
        removal_condition="provider adapter no longer owns legacy memory_ops state",
        tests=(
            "tests/test_arch_convergence_command_port.py:test_provider_and_tooling_entries_use_same_command_port_object",
        ),
    ),
    CompatibilityShim(
        shim_id="isolated-host-command-fallback",
        owner="program-1a-runtime-boundary",
        source="_internal/runtime/kernel.py:_LegacyPersistCommandPort",
        replacement="explicit MemoryCommandGateway supplied by external hosts",
        usage_evidence=("_internal/runtime/tool_port.py:def _resolve_command_port",),
        remove_after="2.1.0",
        removal_condition="external host compatibility window closes after 2.0",
        tests=(
            "tests/test_arch_convergence_command_port.py:test_isolated_host_keeps_legacy_command_port_fallback",
        ),
    ),
    CompatibilityShim(
        shim_id="provider-query-adapter",
        owner="program-1a-runtime-boundary",
        source="_internal/runtime/query_adapter.py:ProviderQueryAdapter",
        replacement="typed query repositories and read models",
        usage_evidence=("_internal/runtime/composition.py:def query_port",),
        remove_after="2.1.0",
        removal_condition="legacy memory_queries accepts typed repositories",
        tests=(
            "tests/test_arch_convergence_command_port.py:test_composition_exposes_typed_query_application_and_runtime_snapshot",
        ),
    ),
    CompatibilityShim(
        shim_id="provider-tool-runtime-adapter",
        owner="program-1a-runtime-boundary",
        source="_internal/runtime/tool_port.py:ProviderToolRuntimeAdapter",
        replacement="split query command policy and maintenance tool ports",
        usage_evidence=(
            "_internal/runtime/composition.py:self.tool_port",
            "tooling.py:ScopeRecallToolService",
        ),
        remove_after="2.1.0",
        removal_condition="all legacy maintenance handlers use semantic application ports",
        tests=(
            "tests/test_arch_convergence_command_port.py:test_tool_runtime_reuses_assembled_query_application",
            "tests/test_readonly_follower_tools.py:test_readonly_follower_default_denies_writes_and_unknown_tools",
        ),
    ),
    CompatibilityShim(
        shim_id="legacy-public-truth-port",
        owner="program-1a-runtime-boundary",
        source="_internal/runtime/ports.py:MemoryQueryPort",
        replacement="MemoryQueryApplication and typed runtime snapshots",
        usage_evidence=(
            "memory_queries.py:_require_port_method",
            "memory_ops.py:_require_command_method",
        ),
        remove_after="2.1.0",
        removal_condition="legacy query and mutation modules move behind repositories",
        tests=(
            "tests/test_public_memory_port.py:test_private_only_fake_still_raises_typeerror_for_public_ports",
        ),
    ),
    CompatibilityShim(
        shim_id="provider-capture-adapter",
        owner="program-1a-runtime-boundary",
        source="_internal/runtime/capture_service.py:ProviderCaptureAdapter",
        replacement="typed capture state and queue ports",
        usage_evidence=("_internal/runtime/composition.py:self.capture",),
        remove_after="2.1.0",
        removal_condition="capture runtime state leaves the Hermes adapter",
        tests=(
            "tests/test_digest_transaction_boundary.py:test_sync_turn_capture_llm_releases_duplicate_journal_transaction",
        ),
    ),
    CompatibilityShim(
        shim_id="provider-journal-adapter",
        owner="program-1a-runtime-boundary",
        source="_internal/runtime/journal_service.py:ProviderJournalAdapter",
        replacement="typed journal repository and digest scheduler ports",
        usage_evidence=("_internal/runtime/composition.py:self.journal",),
        remove_after="2.1.0",
        removal_condition="journal state leaves the Hermes adapter",
        tests=(
            "tests/test_provider.py:test_on_pre_compress_stages_sanitized_messages_in_journal_without_direct_memory",
        ),
    ),
    CompatibilityShim(
        shim_id="provider-vector-adapter",
        owner="program-1a-runtime-boundary",
        source="_internal/runtime/vector_service.py:ProviderVectorAdapter",
        replacement="typed vector generation state and companion ports",
        usage_evidence=("_internal/runtime/composition.py:self.vector",),
        remove_after="2.1.0",
        removal_condition="vector generation state leaves the Hermes adapter",
        tests=(
            "tests/test_vector_stats_replay_audit.py:test_vector_status_view_never_calls_list_records",
        ),
    ),
    CompatibilityShim(
        shim_id="provider-vector-runtime-state-adapter",
        owner="program-1a-runtime-boundary",
        source="_internal/runtime/vector_runtime_state.py:ProviderVectorRuntimeState",
        replacement="typed vector repositories and generation state owned outside Provider",
        usage_evidence=("vector_runtime.py:bind_provider_vector_runtime",),
        remove_after="2.1.0",
        removal_condition="legacy vector algorithms no longer require Provider-backed state",
        tests=(
            "tests/test_arch_convergence_command_port.py:test_vector_runtime_uses_explicit_compatibility_state",
        ),
    ),
    CompatibilityShim(
        shim_id="provider-module-hook-anchors",
        owner="program-1a-runtime-boundary",
        source="provider.py:_COMPOSITION_RUNTIME_HOOKS",
        replacement="constructor-injected infrastructure factories",
        usage_evidence=(
            "_internal/runtime/capture_service.py:_provider_hook",
            "_internal/runtime/vector_service.py:_provider_hook",
        ),
        remove_after="2.1.0",
        removal_condition="legacy monkeypatch and plugin-loader compatibility window closes",
        tests=(
            "tests/test_digest_transaction_boundary.py:test_sync_turn_capture_llm_releases_duplicate_journal_transaction",
            "tests/test_arch_convergence_command_port.py:test_touched_internals_import_canonical_modules_not_shims",
        ),
    ),
)


PROGRAM_1A_COMPATIBILITY_IDS = frozenset(
    {
        "provider-command-adapter",
        "isolated-host-command-fallback",
        "provider-query-adapter",
        "provider-tool-runtime-adapter",
        "legacy-public-truth-port",
        "provider-capture-adapter",
        "provider-journal-adapter",
        "provider-vector-adapter",
        "provider-vector-runtime-state-adapter",
        "provider-module-hook-anchors",
    }
)


def validate_compatibility_registry() -> tuple[str, ...]:
    errors: list[str] = []
    ids = [item.shim_id for item in COMPATIBILITY_REGISTRY]
    if len(ids) != len(set(ids)):
        errors.append("duplicate shim_id")
    if set(ids) != PROGRAM_1A_COMPATIBILITY_IDS:
        errors.append("Program 1A compatibility set mismatch")
    for item in COMPATIBILITY_REGISTRY:
        if not item.owner:
            errors.append(f"{item.shim_id}: missing owner")
        if ":" not in item.source:
            errors.append(f"{item.shim_id}: source must name file and symbol")
        if not item.replacement:
            errors.append(f"{item.shim_id}: missing replacement")
        if not item.usage_evidence:
            errors.append(f"{item.shim_id}: missing usage evidence")
        if re.fullmatch(r"\d+\.\d+\.\d+", item.remove_after) is None:
            errors.append(f"{item.shim_id}: remove-after must be a release version")
        if not item.removal_condition:
            errors.append(f"{item.shim_id}: missing removal condition")
        if not item.tests:
            errors.append(f"{item.shim_id}: missing tests")
    return tuple(errors)
