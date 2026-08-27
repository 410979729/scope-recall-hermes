"""Static tool runtime metadata. No handlers, no SQL, no network.

This is the production identity/policy source for compact/standard/maintenance
exposure. Tool-name contracts are enforced by the public tool-hygiene and
release suites, not by private cutover oracles.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...schemas import (
    SCOPE_RECALL_BENCHMARK_SCHEMA,
    SCOPE_RECALL_CONTEXT_SCHEMA,
    SCOPE_RECALL_DEDUPE_SCHEMA,
    SCOPE_RECALL_ENTITY_SCHEMA,
    SCOPE_RECALL_EXPERIENCE_PREFLIGHT_SCHEMA,
    SCOPE_RECALL_EXPERIENCE_PROMOTE_SCHEMA,
    SCOPE_RECALL_EXPERIENCE_STATS_SCHEMA,
    SCOPE_RECALL_EXPLAIN_SCHEMA,
    SCOPE_RECALL_EXPORT_SCHEMA,
    SCOPE_RECALL_EVOLVE_SCHEMA,
    SCOPE_RECALL_FACT_SCHEMA,
    SCOPE_RECALL_FEEDBACK_SCHEMA,
    SCOPE_RECALL_FORGET_SCHEMA,
    SCOPE_RECALL_FORGETTING_REPORT_SCHEMA,
    SCOPE_RECALL_FORGETTING_RUN_SCHEMA,
    SCOPE_RECALL_GOVERN_SCHEMA,
    SCOPE_RECALL_HYGIENE_SCHEMA,
    SCOPE_RECALL_INSPECT_SCHEMA,
    SCOPE_RECALL_MEMORY_SCHEMA,
    SCOPE_RECALL_MERGE_SCHEMA,
    SCOPE_RECALL_PLAYBOOK_CREATE_SCHEMA,
    SCOPE_RECALL_PLAYBOOK_FEEDBACK_SCHEMA,
    SCOPE_RECALL_PLAYBOOK_INSPECT_SCHEMA,
    SCOPE_RECALL_PLAYBOOK_REVIEW_SCHEMA,
    SCOPE_RECALL_PLAYBOOK_SEARCH_SCHEMA,
    SCOPE_RECALL_PROBE_SCHEMA,
    SCOPE_RECALL_PROFILE_SCHEMA,
    SCOPE_RECALL_PURGE_SCHEMA,
    SCOPE_RECALL_REPAIR_SCHEMA,
    SCOPE_RECALL_RELATED_SCHEMA,
    SCOPE_RECALL_REFLECT_SCHEMA,
    SCOPE_RECALL_SEARCH_SCHEMA,
    SCOPE_RECALL_STATS_SCHEMA,
    SCOPE_RECALL_STORE_SCHEMA,
    SCOPE_RECALL_STORE_SECRET_INDEX_SCHEMA,
    SCOPE_RECALL_UPDATE_SCHEMA,
)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    schema: dict[str, Any]
    surfaces: frozenset[str]
    access: str
    owner_capability: str
    feature_gate: str | None = None
    aliases: tuple[str, ...] = ()
    stable: bool = True


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec("scope_recall_store", SCOPE_RECALL_STORE_SCHEMA, frozenset({"compact", "standard"}), "write", "C03", aliases=("lancepro_store",)),
    ToolSpec("scope_recall_search", SCOPE_RECALL_SEARCH_SCHEMA, frozenset({"compact", "standard"}), "read", "C04", aliases=("lancepro_search",)),
    ToolSpec("scope_recall_context", SCOPE_RECALL_CONTEXT_SCHEMA, frozenset({"compact", "standard"}), "read", "C04"),
    ToolSpec("scope_recall_profile", SCOPE_RECALL_PROFILE_SCHEMA, frozenset({"compact", "standard"}), "read", "C04"),
    ToolSpec("scope_recall_memory", SCOPE_RECALL_MEMORY_SCHEMA, frozenset({"compact"}), "conditional", "C03"),
    ToolSpec("scope_recall_entity", SCOPE_RECALL_ENTITY_SCHEMA, frozenset({"compact"}), "read", "C04"),
    ToolSpec("scope_recall_probe", SCOPE_RECALL_PROBE_SCHEMA, frozenset({"standard"}), "read", "C04"),
    ToolSpec("scope_recall_related", SCOPE_RECALL_RELATED_SCHEMA, frozenset({"standard"}), "read", "C04"),
    ToolSpec("scope_recall_feedback", SCOPE_RECALL_FEEDBACK_SCHEMA, frozenset({"standard"}), "write", "C03"),
    ToolSpec("scope_recall_forget", SCOPE_RECALL_FORGET_SCHEMA, frozenset({"standard"}), "write", "C03"),
    ToolSpec("scope_recall_update", SCOPE_RECALL_UPDATE_SCHEMA, frozenset({"standard"}), "write", "C03"),
    ToolSpec("scope_recall_merge", SCOPE_RECALL_MERGE_SCHEMA, frozenset({"standard"}), "write", "C03"),
    ToolSpec("scope_recall_inspect", SCOPE_RECALL_INSPECT_SCHEMA, frozenset({"standard"}), "read", "C04"),
    ToolSpec("scope_recall_explain", SCOPE_RECALL_EXPLAIN_SCHEMA, frozenset({"standard"}), "read", "C05"),
    ToolSpec("scope_recall_export", SCOPE_RECALL_EXPORT_SCHEMA, frozenset({"standard"}), "read", "C04"),
    ToolSpec("scope_recall_stats", SCOPE_RECALL_STATS_SCHEMA, frozenset({"standard"}), "read", "C04", aliases=("lancepro_stats",)),
    ToolSpec("scope_recall_benchmark", SCOPE_RECALL_BENCHMARK_SCHEMA, frozenset({"standard"}), "read", "C18"),
    ToolSpec("scope_recall_fact", SCOPE_RECALL_FACT_SCHEMA, frozenset(), "read", "C12", feature_gate="temporal"),
    ToolSpec("scope_recall_reflect", SCOPE_RECALL_REFLECT_SCHEMA, frozenset(), "conditional", "C13", feature_gate="reflection"),
    ToolSpec("scope_recall_store_secret_index", SCOPE_RECALL_STORE_SECRET_INDEX_SCHEMA, frozenset(), "write", "C16", feature_gate="secret_index"),
    ToolSpec("scope_recall_playbook_search", SCOPE_RECALL_PLAYBOOK_SEARCH_SCHEMA, frozenset({"standard"}), "read", "C14", feature_gate="experience"),
    ToolSpec("scope_recall_playbook_inspect", SCOPE_RECALL_PLAYBOOK_INSPECT_SCHEMA, frozenset({"standard"}), "read", "C14", feature_gate="experience"),
    ToolSpec("scope_recall_experience_preflight", SCOPE_RECALL_EXPERIENCE_PREFLIGHT_SCHEMA, frozenset({"standard"}), "read", "C14", feature_gate="experience"),
    ToolSpec("scope_recall_playbook_feedback", SCOPE_RECALL_PLAYBOOK_FEEDBACK_SCHEMA, frozenset({"standard"}), "write", "C14", feature_gate="experience"),
    ToolSpec("scope_recall_experience_stats", SCOPE_RECALL_EXPERIENCE_STATS_SCHEMA, frozenset({"standard"}), "read", "C14", feature_gate="experience"),
    ToolSpec("scope_recall_dedupe", SCOPE_RECALL_DEDUPE_SCHEMA, frozenset({"maintenance"}), "write", "C11", feature_gate="maintenance"),
    ToolSpec("scope_recall_govern", SCOPE_RECALL_GOVERN_SCHEMA, frozenset({"maintenance"}), "write", "C11", feature_gate="maintenance"),
    ToolSpec("scope_recall_repair", SCOPE_RECALL_REPAIR_SCHEMA, frozenset({"maintenance"}), "write", "C18", feature_gate="maintenance"),
    ToolSpec("scope_recall_hygiene", SCOPE_RECALL_HYGIENE_SCHEMA, frozenset({"maintenance"}), "read", "C11", feature_gate="maintenance"),
    ToolSpec("scope_recall_forgetting_report", SCOPE_RECALL_FORGETTING_REPORT_SCHEMA, frozenset({"maintenance"}), "read", "C11", feature_gate="maintenance"),
    ToolSpec("scope_recall_forgetting_run", SCOPE_RECALL_FORGETTING_RUN_SCHEMA, frozenset({"maintenance"}), "write", "C11", feature_gate="maintenance"),
    ToolSpec("scope_recall_purge", SCOPE_RECALL_PURGE_SCHEMA, frozenset({"maintenance"}), "conditional", "C11", feature_gate="maintenance"),
    ToolSpec("scope_recall_evolve", SCOPE_RECALL_EVOLVE_SCHEMA, frozenset({"maintenance"}), "write", "C12", feature_gate="maintenance"),
    ToolSpec("scope_recall_playbook_create", SCOPE_RECALL_PLAYBOOK_CREATE_SCHEMA, frozenset({"maintenance"}), "write", "C14", feature_gate="experience+maintenance"),
    ToolSpec("scope_recall_playbook_review", SCOPE_RECALL_PLAYBOOK_REVIEW_SCHEMA, frozenset({"maintenance"}), "write", "C14", feature_gate="experience+maintenance"),
    ToolSpec("scope_recall_experience_promote", SCOPE_RECALL_EXPERIENCE_PROMOTE_SCHEMA, frozenset({"maintenance"}), "write", "C14", feature_gate="experience+maintenance"),
)


def tool_spec_by_name() -> dict[str, ToolSpec]:
    return {spec.name: spec for spec in TOOL_SPECS}


def visible_tool_specs(*, profile: str, flags: set[str]) -> list[ToolSpec]:
    """Reproduce the historical compact/standard/maintenance exposure rules."""

    visible: list[ToolSpec] = []
    for spec in TOOL_SPECS:
        gate = spec.feature_gate
        if gate == "experience+maintenance":
            if "experience" in flags and "maintenance" in flags:
                visible.append(spec)
            continue
        if gate == "experience":
            if "experience" not in flags:
                continue
            if profile == "standard" or spec.name in flags:
                visible.append(spec)
            continue
        if gate in {"temporal", "reflection", "secret_index", "maintenance"}:
            if gate in flags:
                visible.append(spec)
            continue
        if profile in spec.surfaces:
            visible.append(spec)
    return visible
