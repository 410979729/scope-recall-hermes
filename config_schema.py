"""Configuration schema metadata used by tools, docs, and release checks.

The schema is descriptive rather than a runtime authority; keep defaults synchronized with config loading and plugin metadata."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_DESCRIPTION_OVERRIDES = {
    "auto_recall": "Enable automatic recall injection at turn start.",
    "auto_capture": "Capture eligible conversation turns into Scope Recall.",
    "capture_queue_capacity": "Maximum sanitized capture jobs held in the bounded process-local writer queue. Excess enqueue attempts receive an explicit rejected or deferred status instead of silent loss; queued payloads are not persisted.",
    "automatic_digest_default_lifecycle": "Lifecycle for non-time-sensitive journal/nightly automatic digest outputs. Candidate is the review-first default; promoted explicitly opts into immediate recall visibility. Time-sensitive snapshots remain candidates that need a live check.",
    "memory_isolated_chat_ids": "Runtime-only chat identifiers excluded from prompt recall, tools, capture, journal, and digest surfaces.",
    "journal.max_entries_per_digest": "Maximum journal entries a digest run may review before dynamic backlog expansion.",
    "journal.max_entries_per_session_per_run": "Maximum unprocessed entries one session may contribute to a single digest load, so one high-volume session cannot starve every other session's backlog.",
    "journal.extraction_attempts_quarantine": "Deterministic unresolved-extraction attempts (empty/filtered parses, non-retryable LLM errors) before an entry moves to the replayable rejection ledger instead of reloading forever. Transient provider failures never consume attempts.",
    "journal.retryable_failures_quarantine": "Durable cross-run retryable LLM failures (timeouts and other retryable provider errors) before an entry leaves the FIFO head for journal-recovery replay. One transient failure stays pending and does not spend the ordinary extraction-quality budget.",
    "auto_adjudication.enabled": "Run the scheduled no-human candidate adjudication pass: deterministic promote/archive lanes plus a budgeted LLM grounded-review lane. Operators read run summaries, never per-item queues.",
    "auto_adjudication.interval_hours": "Minimum hours between scheduled adjudication passes on the truth-writer process.",
    "auto_adjudication.claim_timeout_hours": "Hours before an abandoned cross-process schedule claim may be recovered.",
    "auto_adjudication.l4_budget_per_run": "Maximum held candidates re-examined against their journal evidence per pass.",
    "auto_adjudication.l4_enabled": "Enable the budgeted LLM grounded-review lane for held/needs-review candidates. Uses the journal digest LLM provider; unavailable config degrades to lanes-only.",
    "auto_adjudication.l4_max_evidence_chars": "Maximum sanitized journal-evidence characters shown to the grounded reviewer per candidate.",
    "auto_adjudication.l4_max_uncertain_rounds": "Deprecated compatibility key. L4 is advisory-only; this value never archives or changes candidate lifecycle.",
    "auto_adjudication.max_archives_per_run": "Cap on deterministic-lane archives per adjudication pass.",
    "auto_adjudication.max_promotions_per_run": "Cap on deterministic-lane promotions per adjudication pass.",
    "auto_adjudication.promote_min_age_hours": "Minimum candidate age before the deterministic promote lane may auto-promote, so fresh extractions can still be corrected by newer evidence first.",
    "auto_adjudication.retry_backoff_minutes": "Minutes before retrying failed advisory L4 work or a failed deterministic pass.",
    "journal.retention_profile": "Semantic digest detail: light keeps only minimal durable facts, balanced preserves useful rationale and steps, and full preserves detailed durable context while raw transcript evidence remains in the journal.",
    "journal.backlog_fail_entries": "Doctor failure threshold for unprocessed journal backlog.",
    "journal.no_insert_fail_streak": "Doctor failure threshold for recent digest runs that processed entries but produced no durable writes for provider/schema/quality-risk reasons.",
    "relation_extraction_enabled": "Enable bounded extraction and maintenance of relation edges during memory mutations and background repair.",
    "relation_extraction_max_pairs": "Maximum comparison budget for one relation extraction operation (1 to 5000 pairs).",
    "relation_sync_neighbor_limit": "Maximum local peers synchronously compared during a foreground memory mutation (1 to 256 peers).",
    "relation_maintenance_backoff_base_seconds": "Initial retry delay for failed bounded relation maintenance work (0.1 to 3600 seconds).",
    "relation_maintenance_backoff_max_seconds": "Maximum retry delay for failed bounded relation maintenance work (1 to 86400 seconds).",
    "relation_maintenance_interval_seconds": "Minimum interval between bounded relation maintenance ticks (1 to 3600 seconds).",
    "relation_maintenance_max_attempts": "Maximum attempts before failed relation maintenance work becomes terminal poison (1 to 20 attempts).",
    "relation_maintenance_wall_clock_seconds": "Wall-clock budget for one bounded relation maintenance tick (0.05 to 10 seconds).",
    "relation_rebuild_chunk_pairs": "Maximum items processed by one bounded relation maintenance lane (1 to 1000 items); retained as the compatibility name for finite change, focus, backfill, and reclassification work.",
    "relation_reclassification_candidate_cap": "Maximum affected candidates inspected before reclassification refuses the entire mutation without partial work (1 to 5000 candidates).",
    "relation_policy_generation_enabled": "Enable Program 2 finite, leased relation policy generations. Default false preserves Program 0 containment execution.",
    "retrieval.mode": "Recall mode: lexical, vector, or hybrid.",
    "retrieval.relation_rerank_enabled": "Enable small relation-graph rerank bonuses after primary recall scoring.",
    "retrieval.vector_only_min_score": "Minimum score for vector-only candidates to survive recall filtering.",
    "retrieval.fact_freshness_untracked_penalty": "Score penalty for factual memories without tracked freshness evidence.",
    "retrieval.fact_freshness_needs_live_check_penalty": "Score penalty for factual memories that require a live check.",
    "retrieval.fact_freshness_stale_penalty": "Score penalty for factual memories marked stale.",
    "retrieval.fact_freshness_expired_penalty": "Score penalty for factual memories whose validity window expired.",
    "vector.enabled": "Enable the rebuildable vector companion index.",
    "vector.backend": "Vector companion backend used for semantic recall.",
    "vector.startup_reconcile_enabled": "Run bounded startup/background vector outbox and truth reconciliation. Default true; set false to leave vector search available without automatic outbox/truth reconciliation ticks.",
    "vector.startup_reconcile_page_size": "Maximum truth rows planned into durable vector outbox events by one startup or background maintenance tick.",
    "vector.startup_outbox_limit": "Maximum durable vector outbox events replayed in one startup or background maintenance phase.",
    "vector.write_outbox_replay_limit": "Maximum durable vector outbox events replayed after one committed memory write so transient backlog converges during normal traffic.",
    "vector.outbox_completed_retention_days": "Delete completed vector outbox events older than this many days after a clean startup reconciliation pass; 0 disables pruning. Nonterminal events are never pruned.",
    "vector.outbox_completed_keep_per_generation": "Minimum number of the newest completed vector outbox events retained for each generation even after the age cutoff.",
    "vector.outbox_retention_interval_seconds": "Minimum seconds between completed-outbox retention passes. Retention is low-priority housekeeping that skips quietly under SQLite contention instead of colliding with live writers every idle tick.",
    "vector.startup_reconcile_interval_seconds": "Minimum delay between completed vector reconciliation cycles; interrupted cycles resume immediately from their durable watermark.",
    "vector.embedder.api_key_env": "Environment variable names that may hold the embedding API key.",
    "vector.embedder.base_url_env": "Optional environment variable name that supplies the primary hosted embedding base URL.",
    "vector.fallback_embedder.base_url_env": "Optional environment variable name that supplies the fallback hosted embedding base URL.",
    "vector.embedder.allow_insecure_endpoint": "Allow an explicitly trusted non-loopback HTTP embedding endpoint. Credential-bearing headers are always stripped on HTTP.",
    "vector.fallback_embedder.allow_insecure_endpoint": "Allow an explicitly trusted non-loopback HTTP fallback embedding endpoint. Credential-bearing headers are always stripped on HTTP.",
    "capture_llm.allow_insecure_endpoint": "Allow an explicitly trusted non-loopback HTTP capture endpoint. Credential-bearing headers are always stripped on HTTP.",
    "journal.allow_insecure_endpoint": "Allow an explicitly trusted non-loopback HTTP journal endpoint. Credential-bearing headers are always stripped on HTTP.",
    "reflection.allow_insecure_endpoint": "Allow an explicitly trusted non-loopback HTTP reflection endpoint. Credential-bearing headers are always stripped on HTTP.",
    "vector.embedder.request_dimensions": "Send the configured output dimension to providers that support explicit dimensionality.",
    "vector.embedder.document_prefix": "Optional instruction prefix applied only when embedding indexed documents.",
    "vector.embedder.query_prefix": "Optional instruction prefix applied only when embedding retrieval queries.",
    "vector.embedder.prompt_profile": "Versioned identifier for the query/document instruction profile; changing it requires a new vector generation.",
    "vector.embedder.connection_retry_delays": "Optional bounded delays in seconds for retrying transport-level embedding connection failures (maximum 8 entries, each 0 to 300 seconds). HTTP/API errors are not retried by this schedule; set an explicit empty array to disable retries. Hosted SDK retries stay at 0 so this schedule is the only retry budget.",
    "vector.embedder.connect_timeout_seconds": "Maximum seconds allowed to establish one hosted embedding TCP/TLS connection. Values are clamped to 0.05–300 seconds.",
    "vector.embedder.read_timeout_seconds": "Maximum seconds allowed to read one hosted embedding response. Each attempt is also capped by the remaining query/writer/maintenance budget.",
    "vector.embedder.write_timeout_seconds": "Maximum seconds allowed to write one hosted embedding request body. Each attempt is also capped by the remaining operation budget.",
    "vector.embedder.pool_timeout_seconds": "Maximum seconds a hosted embedding request may wait for a free HTTP connection from the pool.",
    "vector.embedder.query_timeout_seconds": "Total wall-clock budget for one retrieval-query embedding operation, including plugin retries. Exhaustion fails closed so recall can fall back lexically.",
    "vector.embedder.writer_timeout_seconds": "Total wall-clock budget for one ordinary writer/outbox embedding operation, including plugin retries.",
    "vector.embedder.maintenance_timeout_seconds": "Total wall-clock budget for one maintenance or full-sync embedding operation, including plugin retries.",
    "vector.pgvector.connect_timeout_seconds": "Maximum time allowed to establish a PGVector connection. Values are clamped to 1–300 seconds.",
    "vector.pgvector.lock_timeout_ms": "Maximum PostgreSQL lock wait for PGVector statements. Values are clamped to 100–600000 milliseconds.",
    "vector.pgvector.statement_timeout_ms": "Maximum execution time for each PGVector SQL statement. Values are clamped to 100–600000 milliseconds.",
    "experience.enabled": "Enable reusable Experience playbook surfaces.",
    "reflection.enabled": "Expose bounded citation-grounded reflection tooling.",
    "reflection.write_candidates": "Allow explicit maintenance-mode reflection calls to store hidden needs_review mental-model candidates.",
    "fact_evolution.enabled": "Enable structured Fact Evolution. This switch is high risk because an already configured apply mode can persist durable memory immediately; resident providers require reload.",
    "fact_evolution.mode": "Fallback Fact Evolution mode. preview is medium risk; auto_apply/reviewed_apply persist durable memory and are high risk. Resident providers require reload.",
    "fact_evolution.nightly_mode": "Nightly lane mode loaded by each scheduled invocation. preview is medium risk; auto_apply is high risk and may persist durable memory.",
    "fact_evolution.journal_mode": "Journal lane mode loaded by each scheduled invocation. preview is medium risk; auto_apply is high risk and may persist durable memory.",
    "fact_evolution.tool_mode": "Resident public tool-lane mode. Caller evidence remains non-authoritative until a runtime-owned evidence registry is available; provider reload is required.",
    "fact_evolution.maintenance_mode": "Explicit maintenance-lane mode. reviewed_apply permits maintenance-gated operator corrections and is high risk; provider reload is required.",
    "fact_backfill.shadow_enabled": "Enable read-only historical SplitPlan shadow generation. Shadow artifacts remain non-authoritative; applying one exact plan still requires an explicit plan-bound approval, source CAS, and the atomic Fact Executor boundary.",
    "forgetting.archive_assistant_scratch": "Classify general-target assistant prose scratch as soft-archive candidates.",
    "forgetting.archive_duplicates": "Classify older duplicate memories as soft-archive candidates.",
    "forgetting.archive_very_short": "Classify very short non-preference memories as soft-archive candidates.",
    "identity.desktop_principal": "Optional explicit Desktop principal override. Empty keeps the profile-local opaque auto-minted principal used when Hermes Desktop omits user_id. Changing it changes durable scope identity and requires provider reload.",
    "forgetting.enabled": "Enable forgetting report and apply tools; disabled tools fail closed.",
    "forgetting.hard_delete_sensitive": "Second safety gate for sensitive-data hard deletion; apply also requires an explicit hard_delete request.",
    "forgetting.soft_archive_default": "Default whether forgetting apply archives soft candidates; each call may explicitly override it.",
    "retrieval.relation_contradiction_mode": "Contradiction handling: surface keeps and warns; suppress excludes exactly one deterministic loser when both sides reach the bounded candidate set and preserves a one-sided candidate; penalize applies relation_contradicts_penalty.",
}

_GROUP_NOTES = {
    "identity": [
        "`identity.user_aliases` and `identity.chat_aliases` are optional open maps that are intentionally absent from packaged defaults.",
        "Account aliases map an exact `platform:user_id` to a canonical user.",
        "Chat aliases map an exact `platform:chat_id` to a canonical user and therefore grant every participant in that chat the same durable identity.",
        "They take precedence over account aliases and are ignored unless `identity.cross_platform_shared_scope` is enabled.",
        "Treat chat aliases as explicit operator access-control grants.",
    ]
}


_HIGH_RISK_PREFIXES = (
    "automatic_digest_default_lifecycle",
    "identity.desktop_principal",
    "vector.embedder.api_key_env",
    "capture_llm.api_key_env",
    "capture_llm.allow_insecure_endpoint",
    "journal.allow_insecure_endpoint",
    "reflection.api_key_env",
    "reflection.allow_insecure_endpoint",
    "vector.embedder.allow_insecure_endpoint",
    "vector.fallback_embedder.allow_insecure_endpoint",
    "reflection.write_candidates",
    "forgetting.hard_delete_sensitive",
    "secret_index_tools_enabled",
    "memory_isolated_chat_ids",
)
_MEDIUM_RISK_PREFIXES = (
    "journal.",
    "retrieval.",
    "vector.",
    "experience.",
    "reflection.",
    "forgetting.",
    "shared_pool.",
    "identity.",
    "relation_",
    "fact_backfill.",
)
_RESTART_PREFIXES = (
    "identity.desktop_principal",
    "vector.",
    "journal.",
    "tool_schema_",
    "maintenance_tools_enabled",
    "secret_index_tools_enabled",
    "memory_isolated_chat_ids",
    "experience.",
    "reflection.",
    "relation_",
)
_FACT_EVOLUTION_RISKS = {
    "fact_evolution.enabled": "high",
    "fact_evolution.mode": "high",
    "fact_evolution.nightly_mode": "high",
    "fact_evolution.journal_mode": "high",
    "fact_evolution.tool_mode": "high",
    "fact_evolution.maintenance_mode": "high",
}
_RESTART_OVERRIDES = {
    "fact_evolution.enabled": True,
    "fact_evolution.mode": True,
    "fact_evolution.nightly_mode": False,
    "fact_evolution.journal_mode": False,
    "fact_evolution.tool_mode": True,
    "fact_evolution.maintenance_mode": True,
    "fact_backfill.shadow_enabled": False,
}
_CHOICES = {
    "automatic_digest_default_lifecycle": ["candidate", "promoted"],
    "tool_schema_profile": ["compact", "standard"],
    "retrieval.mode": ["lexical", "vector", "hybrid"],
    "retrieval.include_general": ["never", "same-scope", "always"],
    "retrieval.metric": ["cosine", "dot", "l2"],
    "retrieval.fusion_strategy": ["rrf", "weighted"],
    "retrieval.relation_contradiction_mode": ["surface", "suppress", "penalize"],
    "vector.backend": ["lancedb", "sqlite-bruteforce", "pgvector"],
    "vector.fallback_backend": ["sqlite-bruteforce", "disabled"],
    "vector.embedder.provider": ["openai-compatible", "openai", "sentence-transformers", "local-hash"],
    "vector.sync_mode": ["incremental", "rebuild"],
    "journal.extractor": ["llm", "heuristic"],
    "journal.retention_profile": ["light", "balanced", "full"],
    "reflection.api_mode": [
        "chat_completions",
        "codex_responses",
        "anthropic_messages",
    ],
    "curated_memory.mode": ["single-user", "explicit-users", "profile-global", "disabled"],
    "fact_evolution.mode": ["preview", "auto_apply", "reviewed_apply"],
    "fact_evolution.nightly_mode": ["preview", "auto_apply"],
    "fact_evolution.journal_mode": ["preview", "auto_apply"],
    "fact_evolution.tool_mode": ["preview", "auto_apply", "reviewed_apply"],
    "fact_evolution.maintenance_mode": ["preview", "reviewed_apply"],
}
_CHOICE_RISKS = {
    "automatic_digest_default_lifecycle": {
        "candidate": "medium",
        "promoted": "high",
    },
    "fact_evolution.mode": {
        "preview": "medium",
        "auto_apply": "high",
        "reviewed_apply": "high",
    },
    "fact_evolution.nightly_mode": {
        "preview": "medium",
        "auto_apply": "high",
    },
    "fact_evolution.journal_mode": {
        "preview": "medium",
        "auto_apply": "high",
    },
    "fact_evolution.tool_mode": {
        "preview": "medium",
        "auto_apply": "high",
        "reviewed_apply": "high",
    },
    "fact_evolution.maintenance_mode": {
        "preview": "medium",
        "reviewed_apply": "high",
    },
}


def packaged_config_path() -> Path:
    return Path(__file__).resolve().parent / "config.json"


def load_packaged_config() -> dict[str, Any]:
    path = packaged_config_path()
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _value_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int) and not isinstance(value, bool):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    return "string"


def _description(key: str) -> str:
    if key in _DESCRIPTION_OVERRIDES:
        return _DESCRIPTION_OVERRIDES[key]
    group = key.split(".", 1)[0]
    return f"Scope Recall configuration key `{key}` in the `{group}` group."


def _risk(key: str) -> str:
    if key in _FACT_EVOLUTION_RISKS:
        return _FACT_EVOLUTION_RISKS[key]
    if any(key == prefix or key.startswith(f"{prefix}.") for prefix in _HIGH_RISK_PREFIXES):
        return "high"
    if any(key == prefix.rstrip(".") or key.startswith(prefix) for prefix in _MEDIUM_RISK_PREFIXES):
        return "medium"
    return "low"


def _restart_required(key: str) -> bool:
    if key in _RESTART_OVERRIDES:
        return _RESTART_OVERRIDES[key]
    return any(key == prefix.rstrip(".") or key.startswith(prefix) for prefix in _RESTART_PREFIXES)


def _flatten(value: Any, prefix: str = "") -> list[dict[str, Any]]:
    if isinstance(value, dict):
        rows: list[dict[str, Any]] = []
        for key in sorted(value):
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            rows.extend(_flatten(value[key], child_prefix))
        return rows
    row: dict[str, Any] = {
        "key": prefix,
        "type": _value_type(value),
        "default": value,
        "description": _description(prefix),
        "risk": _risk(prefix),
        "restart_required": _restart_required(prefix),
    }
    if prefix in _CHOICES:
        row["choices"] = list(_CHOICES[prefix])
    if prefix in _CHOICE_RISKS:
        row["choice_risks"] = dict(_CHOICE_RISKS[prefix])
    return [row]


def build_config_registry(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return _flatten(config if config is not None else load_packaged_config())


def render_configuration_markdown(registry: list[dict[str, Any]] | None = None) -> str:
    rows = registry if registry is not None else build_config_registry()
    lines = [
        "# Scope Recall Configuration Reference",
        "",
        "This file is generated from the packaged `config.json` registry. It lists every supported leaf key, its default value, risk level, and whether a Hermes restart/reload is normally required.",
        "",
    ]
    current_group = ""
    for entry in rows:
        key = str(entry["key"])
        group = key.split(".", 1)[0]
        if group != current_group:
            if current_group in _GROUP_NOTES:
                lines.extend(["", *_GROUP_NOTES[current_group]])
            current_group = group
            lines.extend(["", f"## `{group}`", ""])
        default = json.dumps(entry.get("default"), ensure_ascii=False, sort_keys=True)
        choices = entry.get("choices")
        choices_text = f"; choices: `{', '.join(map(str, choices))}`" if choices else ""
        choice_risks = entry.get("choice_risks")
        choice_risks_text = (
            "; choice_risks: `"
            + ", ".join(
                f"{choice}={risk}"
                for choice, risk in choice_risks.items()
            )
            + "`"
            if isinstance(choice_risks, dict) and choice_risks
            else ""
        )
        restart = "yes" if entry.get("restart_required") else "no"
        lines.append(f"- `{key}` ({entry.get('type')}; risk: `{entry.get('risk')}`; restart_required: `{restart}`{choices_text}{choice_risks_text}) — {entry.get('description')} Default: `{default}`")
    if current_group in _GROUP_NOTES:
        lines.extend(["", *_GROUP_NOTES[current_group]])
    lines.append("")
    return "\n".join(lines)
