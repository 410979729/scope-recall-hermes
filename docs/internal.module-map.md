# Scope Recall Maintainer Module Map

This maintainer-facing map documents where Scope Recall functionality lives and where future work should land. It exists to reduce context load during long development sessions: read this file before changing a module, then open only the relevant implementation and tests.

This document is intentionally about source-tree boundaries, not deployment status. Live runtime state still requires `scripts/doctor.py` and direct evidence from the target Hermes home. It is safe to ship as maintainer documentation: do not add local machine paths, private validation transcripts, credentials, or one-off task status here.

## Quick rules

- Keep SQLite truth authoritative. Vector, FTS, graph, and dashboard state must remain rebuildable companions.
- Keep `provider.py`, `tooling.py`, `sql_store.py`, and `experience_store.py` thin at their boundaries. Add new algorithms in focused modules.
- Do not duplicate existing capabilities. Extend the owning module or add a thin orchestration module that calls it.
- New tools must respect the existing compact / standard / maintenance schema layers.
- Destructive or high-risk operations must be dry-run by default, audited, and gated by explicit operator approval.
- Public docs must use customer/operator language. Keep local validation notes and implementation plans in `.hermes/plans/`; maintainer docs in `docs/` must stay source-boundary focused and free of local runtime facts.

## Repository scan baseline

- Top-level Python modules: 68.
- Test files: 75.
- Naive top-level relative import cycle check: 0 cycle(s) found.
- Planned Phase -1+ module names currently absent: `task_boundary.py`, `task_boundary_llm.py`, `experience_evidence.py`, `experience_synthesis.py`, `experience_quality.py`, `skill_bridge.py`, `governance_scheduler.py`, `experience_replay_generation.py`.
- Planned module names already present: none.

## Large-file guardrails

These modules are already large enough that future changes should prefer extraction or thin adapters:

- `memory_ops.py` — 1559 lines. Boundary: High-level memory operations behind Scope Recall tools: store, search, update, merge, forget, govern, and explain.
- `nightly_digest.py` — 1297 lines. Boundary: Nightly session digest pipeline for turning session history into candidate durable memories.
- `experience_store.py` — 1126 lines. Boundary: SQLite storage layer for Experience playbooks, feedback, reviews, and promotion ledgers.
- `provider.py` — 989 lines. Boundary: Hermes MemoryProvider implementation for Scope Recall.
- `journal.py` — 983 lines. Boundary: Compatibility facade and orchestration layer for journal capture, digest, and recovery helpers.
- `sql_store.py` — 970 lines. Boundary: SQLite truth-store schema, migration, and row-level helper functions.
- `tooling.py` — 882 lines. Boundary: Dispatcher for Hermes tool calls exposed by Scope Recall.
- `recall.py` — 784 lines. Boundary: Recall service that combines curated files, SQLite rows, vector hits, graph evidence, and ranking policy.
- `experience_promotion.py` — 768 lines. Boundary: Promotion planner for moving reviewed Experience playbooks into active procedural memory.
- `installer.py` — 559 lines. Boundary: Install, upgrade, verify, and rollback helpers for copying Scope Recall into a Hermes home.
- `schemas.py` — 558 lines. Boundary: SQLite schema definitions and migration SQL for Scope Recall truth and companion tables.
- `embedders.py` — 548 lines. Boundary: Embedding provider adapters for local, OpenAI-compatible, and hosted vector backends.

Allowed changes in large files:

- small adapter calls into a new focused module;
- schema registration / dispatch wiring;
- bug fixes that cannot be isolated safely;
- tests that prove the extracted module preserves behavior.

Avoid:

- adding new multi-step algorithms;
- adding new LLM prompt construction directly inside storage or provider modules;
- mixing read-only report code with mutation/apply code;
- embedding one-off local runtime facts in source or packaged docs.

## Existing capabilities that must not be reimplemented

- Evidence anchors already exist in `experience_store.py`, `experience_promotion.py`, `experience_bootstrap.py`, `sql_store.py`, and `tooling.py`.
- Skill anchors and skill conflict tracking already exist in `experience_store.py`, `sql_store.py`, `doctor_experience.py`, and related tests.
- Replay case loading and evaluation already exist in `experience_replay.py`; replay generation should be separate.
- Forgetting report/run already exist in `forgetting.py`; scheduled governance should call it rather than duplicating its archive/delete logic.
- Legacy archive/audit cleanup already exists in `governance_cleanup.py`; new governance scheduling should not absorb it.
- Experience health reporting already exists in `doctor_experience.py`; new maturity fields should extend the report without making doctor mutate state.
- Tool exposure gating already exists in `provider_schemas.py`; new tools must be added to the appropriate compact / standard / maintenance layer.

## Planned new modules and landing zones

- `task_boundary.py` — deterministic task-boundary detection from journal entries into task segments. It should not call LLMs or write SQLite.
- `task_boundary_llm.py` — optional ambiguous-boundary adjudication. Default off; input must be redacted summaries.
- `experience_evidence.py` — builds redacted evidence packs from task episodes and journal rows.
- `experience_synthesis.py` — strict-schema LLM synthesis from evidence packs into candidate reusable experience handbooks.
- `experience_quality.py` — quality gates for evidence anchors, verification, one-time fact removal, risk class, and promotion eligibility.
- `skill_bridge.py` — creates skill proposals only; it must not call `skill_manage` or modify real skills.
- `governance_scheduler.py` — orchestrates quality lint, forgetting report/run, replay report, and candidate promotion; it must call existing modules.
- `experience_replay_generation.py` — proposes replay cases; evaluation stays in `experience_replay.py`.

## Module inventory

### Runtime, configuration, and tool surface

- `__init__.py` (19 lines) — scope-recall current-turn memory provider plugin. Tests: no direct test by name/import; verify through integration tests before changing.
- `provider.py` (989 lines) — Hermes MemoryProvider implementation for Scope Recall. Classes: ScopeRecallMemoryProvider. Tests: `test_config_schema.py`, `test_provider.py`, `test_provider_schemas.py`.
- `provider_schemas.py` (150 lines) — Tool and configuration schema builders exposed to Hermes. Tests: `test_config_schema.py`, `test_provider_schemas.py`.
- `schemas.py` (558 lines) — SQLite schema definitions and migration SQL for Scope Recall truth and companion tables. Tests: `test_experience_config_defaults.py`, `test_provider_schemas.py`.
- `response_schemas.py` (34 lines) — Public response-schema version constants for operator-facing JSON reports. Tests: `test_release.py`.
- `config.py` (252 lines) — Runtime configuration loading and persistence for Scope Recall. Tests: `test_config_schema.py`, `test_experience_config_defaults.py`, `test_optional_vector_deps.py`.
- `config_schema.py` (157 lines) — Configuration schema metadata used by tools, docs, and release checks. Tests: `test_config_schema.py`.
- `installer.py` (559 lines) — Install, upgrade, verify, and rollback helpers for copying Scope Recall into a Hermes home. Classes: InstallError. Tests: `test_installer.py`.
- `models.py` (131 lines) — Core dataclasses and normalization helpers shared by provider, recall, migration, and vector code. Classes: RecallItem, RuntimeScope, ImportedMemoryRow, VectorIndexRecord. Tests: `test_doctor_experience.py`, `test_experience_promotion.py`, `test_fact_freshness.py`, `test_governance_contract_regressions.py`, `test_journal_digest.py`, `test_journal_extractors.py`.
- `scope.py` (251 lines) — Scope identity and access helpers for local, shared, and shared-pool memory visibility. Tests: `test_doctor_experience.py`, `test_experience_promotion.py`, `test_governance_contract_regressions.py`, `test_journal_digest.py`, `test_journal_store.py`, `test_provider.py`.
- `aliases.py` (55 lines) — Alias normalization helpers for compatibility with older Scope Recall tool and provider names. Tests: `test_scoring.py`.

### Capture, journal, and digest pipeline

- `capture.py` (167 lines) — Asynchronous capture writer for current-turn memory rows. Tests: `test_audit_regressions.py`, `test_capture_filters.py`, `test_capture_llm_manual.py`, `test_doctor_experience.py`, `test_governance_contract_regressions.py`, `test_v1015_audit_regressions.py`.
- `capture_filters.py` (244 lines) — Capture hygiene filters for rejecting low-value, secret-like, or path-heavy text before it reaches durable storage. Classes: CaptureFilterResult. Tests: `test_capture_filters.py`, `test_doctor_experience.py`, `test_governance_contract_regressions.py`.
- `capture_llm.py` (371 lines) — LLM-powered semantic capture for scope-recall. Classes: Candidate. Tests: `test_audit_regressions.py`, `test_capture_llm_manual.py`, `test_v1015_audit_regressions.py`.
- `journal.py` (983 lines) — Compatibility facade and orchestration layer for journal capture, digest, and recovery helpers. Tests: `test_doctor_experience.py`, `test_doctor_journal_health.py`, `test_experience_promotion.py`, `test_journal_candidates.py`, `test_journal_digest.py`, `test_journal_extractors.py`.
- `journal_store.py` (388 lines) — SQLite journal storage primitives for capture, chunking, processed flags, and backlog loading. Classes: JournalEntry. Tests: `test_journal_candidates.py`, `test_journal_extractors.py`, `test_journal_store.py`.
- `journal_candidates.py` (287 lines) — Heuristic candidate extraction for journal digest batches. Classes: JournalDigestCandidate. Tests: `test_journal_candidates.py`.
- `journal_extractors.py` (244 lines) — Extractor selection and shared runtime helpers for heuristic and LLM journal digest paths. Tests: `test_journal_extractors.py`.
- `journal_llm.py` (119 lines) — LLM call/retry/quarantine helpers for journal digest extraction. Classes: JournalDigestLLMError. Tests: `test_journal_llm.py`.
- `journal_recovery.py` (338 lines) — Dead-letter and retry-exhausted journal recovery planning. Tests: `test_journal_recovery.py`.
- `nightly_digest.py` (1297 lines) — Nightly session digest pipeline for turning session history into candidate durable memories. Classes: MessageRecord, SessionBundle, DigestCandidate, ScopeProfile. Tests: `test_nightly_digest.py`, `test_nightly_llm.py`, `test_v1015_audit_regressions.py`.
- `nightly_llm.py` (504 lines) — Provider resolution and LLM prompt helpers for nightly digest runs. Tests: `test_journal_digest.py`, `test_journal_llm.py`, `test_nightly_llm.py`.
- `digest_quality.py` (109 lines) — Quality gates for deciding whether journal or session digest output is durable enough to promote. Classes: DigestQuality. Tests: `test_nightly_digest.py`.
- `digest_run_results.py` (152 lines) — Small constructors for journal digest result payloads. Tests: `test_digest_run_results.py`.

### SQLite truth, migrations, storage views, and imports

- `sql_store.py` (970 lines) — SQLite truth-store schema, migration, and row-level helper functions. Tests: `test_doctor_experience.py`, `test_doctor_journal_health.py`, `test_doctor_secret_scan.py`, `test_doctor_sqlite_readonly.py`, `test_entity_graph_hygiene.py`, `test_experience_bootstrap.py`.
- `storage_views.py` (333 lines) — Read views over curated files, SQLite truth rows, and vector companion hits. Tests: `test_retrieval_policy.py`, `test_storage_views.py`.
- `migration.py` (46 lines) — Legacy Scope Recall storage migration helper. Tests: `test_legacy_hygiene_migration.py`, `test_openclaw_import.py`, `test_schema_migrations.py`.
- `migration_openclaw.py` (473 lines) — OpenClaw memory import planner and sanitizer. Tests: `test_openclaw_import.py`.
- `maintenance_ops.py` (48 lines) — Shared maintenance-operation result helpers. Tests: `test_maintenance_ops.py`.

### Recall, ranking, graph, relation, freshness, and vector companions

- `recall.py` (784 lines) — Recall service that combines curated files, SQLite rows, vector hits, graph evidence, and ranking policy. Classes: RecallService. Tests: `test_fact_freshness.py`, `test_legacy_hygiene_migration.py`, `test_recall_pipeline.py`, `test_relation_aware_recall.py`, `test_retrieval_policy.py`, `test_roadmap_retrieval.py`.
- `recall_pipeline.py` (125 lines) — Composable ranking and filtering stages for Scope Recall retrieval. Classes: RecallSearchPlan. Tests: `test_recall_pipeline.py`.
- `scoring.py` (168 lines) — Recall scoring helpers for lexical, vector, temporal, metadata, and relation-aware ranking. Tests: `test_release.py`, `test_retrieval_policy.py`, `test_retrieval_rrf_graph.py`, `test_roadmap_retrieval.py`, `test_scoring.py`.
- `freshness.py` (202 lines) — Freshness metadata helpers for durable factual memories. Tests: `test_fact_freshness.py`.
- `graph.py` (466 lines) — Entity and relation companion helpers for normalizing graph evidence derived from memories. Tests: `test_doctor_journal_health.py`, `test_entity_graph_hygiene.py`, `test_fact_freshness.py`, `test_graph_hygiene.py`, `test_relation_aware_recall.py`, `test_retrieval_rrf_graph.py`.
- `relation_extraction.py` (390 lines) — Deterministic relation extraction and synchronization for memory graph edges. Tests: `test_relation_extraction.py`.
- `embedders.py` (548 lines) — Embedding provider adapters for local, OpenAI-compatible, and hosted vector backends. Classes: EmbedderInfo, BaseEmbedder, LocalHashEmbedder, LocalDebugEmbedder. Tests: `test_embedders.py`, `test_release.py`.
- `vector_runtime.py` (336 lines) — Runtime setup and mutation helpers for vector companions. Tests: `test_optional_vector_deps.py`, `test_sqlite_vector_store.py`, `test_vector_policy.py`.
- `vector_store.py` (313 lines) — LanceDB vector companion implementation. Classes: LanceVectorStore. Tests: `test_optional_vector_deps.py`, `test_sqlite_vector_store.py`.
- `sqlite_vector_store.py` (273 lines) — SQLite-backed brute-force vector companion used for lightweight or dependency-free deployments. Classes: SQLiteBruteForceVectorStore. Tests: `test_optional_vector_deps.py`, `test_release.py`, `test_report_hygiene_script.py`, `test_sqlite_vector_store.py`.

### Memory operations and governance

- `memory_ops.py` (1559 lines) — High-level memory operations behind Scope Recall tools: store, search, update, merge, forget, govern, and explain. Tests: `test_governance_cleanup.py`, `test_journal_digest.py`, `test_v1015_audit_regressions.py`.
- `memory_quality.py` (171 lines) — Memory quality lint rules for active secrets, pollution, and low-signal durable rows. Tests: `test_memory_quality_lint.py`.
- `governance.py` (457 lines) — Memory governance heuristics for classification, conflict detection, and merge decisions. Classes: ExtractionCandidate. Tests: `test_conflict_governance.py`, `test_forgetting.py`, `test_governance_cleanup.py`, `test_governance_contract_regressions.py`, `test_memory_candidate_promotion.py`, `test_readonly_dry_run_contracts.py`.
- `governance_cleanup.py` (535 lines) — Governance cleanup planners and apply helpers for legacy archive/audit hygiene. Tests: `test_forgetting.py`, `test_governance_cleanup.py`, `test_memory_candidate_promotion.py`, `test_readonly_dry_run_contracts.py`.
- `candidate_promotion.py` (241 lines) — Candidate-memory promotion planner and debt reporter. Classes: CandidateDecision. Tests: `test_memory_candidate_promotion.py`.
- `forgetting.py` (403 lines) — Forgetting and governance reporting for duplicate, stale, or low-value memories. Classes: VectorDeleteStore. Tests: `test_forgetting.py`, `test_journal_digest.py`.
- `secret_index.py` (118 lines) — Secret-indexing helper for explicitly enabled maintenance scans. Tests: no direct test by name/import; verify through integration tests before changing.

### Experience Kernel

- `experience_models.py` (197 lines) — Dataclass models for Experience playbooks, feedback, promotion plans, and replay cases. Classes: ExperienceValidationError, PlaybookStep, ProceduralPlaybook. Tests: `test_experience_schema.py`, `test_experience_store.py`.
- `experience_store.py` (1126 lines) — SQLite storage layer for Experience playbooks, feedback, reviews, and promotion ledgers. Tests: `test_doctor_experience.py`, `test_experience_preflight.py`, `test_experience_replay.py`, `test_experience_store.py`, `test_experience_tools.py`, `test_skill_governance.py`.
- `experience_preflight.py` (280 lines) — Preflight checks that score Experience playbook candidates before promotion. Tests: `test_experience_preflight.py`, `test_experience_promotion.py`, `test_experience_store.py`.
- `experience_promotion.py` (768 lines) — Promotion planner for moving reviewed Experience playbooks into active procedural memory. Tests: `test_experience_promotion.py`.
- `experience_replay.py` (198 lines) — Replay benchmark runner for Experience playbooks and procedural recall behavior. Classes: ReplayCaseValidationError. Tests: `test_experience_replay.py`.
- `experience_bootstrap.py` (219 lines) — Bootstrap logic for creating Experience playbooks from vetted memories and journal evidence. Tests: `test_experience_bootstrap.py`, `test_experience_replay.py`.
- `experience_classification.py` (124 lines) — Heuristics for classifying whether text is a reusable Experience playbook candidate. Classes: ExperienceClassification. Tests: `test_experience_promotion.py`.

### Doctor and operator reports

- `doctor.py` — **missing from current source tree**.
- `doctor_common.py` (206 lines) — Shared helpers for doctor checks and operator-facing health reports. Tests: no direct test by name/import; verify through integration tests before changing.
- `doctor_experience.py` (375 lines) — Doctor checks for Experience Kernel playbooks, promotion debt, duplicate groups, and nightly digest health. Tests: `test_doctor_experience.py`, `test_fact_freshness.py`.
- `doctor_journal.py` (435 lines) — Doctor checks for journal backlog, retry/dead-letter queues, quarantine history, and digest failure patterns. Tests: `test_doctor_journal_health.py`.
- `doctor_source.py` (69 lines) — Source-tree doctor checks for release-critical files, packaged assets, and repository contract surfaces. Tests: no direct test by name/import; verify through integration tests before changing.
- `doctor_sqlite.py` (240 lines) — SQLite runtime doctor checks for schema version, migration ledger, row quality, and truth-store accessibility. Tests: `test_doctor_sqlite_readonly.py`, `test_memory_quality_lint.py`.
- `doctor_vector.py` (360 lines) — Vector companion doctor checks for LanceDB/SQLite vector readiness and consistency with SQLite truth rows. Tests: `test_optional_vector_deps.py`.

### Dashboard and reporting helpers

- `dashboard.py` — **missing from current source tree**.
- `http_utils.py` (48 lines) — HTTP utility helpers for hosted providers and operator scripts. Tests: no direct test by name/import; verify through integration tests before changing.

### Other top-level modules

- `artifacts.py` (182 lines) — Artifact extraction helpers for turning issue, PR, release, commit, and URL mentions into stable recall anchors. Tests: no direct test by name/import; verify through integration tests before changing.
- `cli.py` (129 lines) — Installed command-line entry point for operating Scope Recall outside the Hermes plugin loader. Tests: `test_installer.py`, `test_repair_vector_index_cli.py`, `test_rollout_profiles.py`, `test_schema_migrations.py`.
- `gating.py` (190 lines) — General gating, normalization, and compact-text helpers used across capture, recall, and reporting. Tests: no direct test by name/import; verify through integration tests before changing.
- `graph_hygiene.py` (180 lines) — Graph companion hygiene checks and repair helpers. Tests: `test_entity_graph_hygiene.py`, `test_graph_hygiene.py`.
- `hygiene.py` (154 lines) — Legacy and runtime hygiene reporting for noisy memories, secret-like text, and low-value captures. Tests: `test_entity_graph_hygiene.py`, `test_graph_hygiene.py`, `test_hygiene.py`, `test_journal_digest.py`, `test_legacy_hygiene_migration.py`, `test_report_hygiene_script.py`.
- `prompting.py` (92 lines) — Prompt rendering helpers for injecting current-turn recall/profile context. Tests: no direct test by name/import; verify through integration tests before changing.
- `scope_recall.py` (29 lines) — Backward-compatible import shim for environments that import the package as scope_recall.py. Tests: no direct test by name/import; verify through integration tests before changing.
- `tooling.py` (882 lines) — Dispatcher for Hermes tool calls exposed by Scope Recall. Classes: ScopeRecallToolService. Tests: `test_tool_hygiene.py`.

## Tool surface ownership

Tool schema exposure is controlled by `provider_schemas.py`; dispatch is controlled by `tooling.py`.

- Compact default tools: store/search/context/profile/memory/entity. Keep this layer small.
- Standard tools: read-only or low-risk inspection/search/explain/stat surfaces.
- Maintenance tools: dedupe, govern, repair, playbook create/review/promote, forgetting report/run, and any future apply-capable governance tools.
- Secret index tools remain separately gated by `secret_index_tools_enabled`.

When adding a new tool:

1. Add the response schema in `response_schemas.py` if it returns structured JSON.
2. Add the tool schema in `schemas.py`.
3. Register exposure in `provider_schemas.py` under the narrowest safe layer.
4. Add dispatch in `tooling.py` as a thin call into the owning module.
5. Add tests in `tests/test_provider_schemas.py` and the owning feature test file.

## Pre-change duplicate/conflict check

Run this before adding a new feature module or tool:

```bash
git status --short --branch
python scripts/doctor.py --hermes-home "$HERMES_HOME" --json > /tmp/scope-doctor-before.json
python -m json.tool /tmp/scope-doctor-before.json >/dev/null
python - <<'PY'
from pathlib import Path
root = Path('.')
terms = [
    'task_boundary', 'TaskSegment', 'experience_evidence', 'experience_synthesis',
    'experience_quality', 'skill_bridge', 'governance_scheduler',
    'experience_replay_generation', 'evidence_anchors', 'skill_anchors',
]
for term in terms:
    hits = []
    for path in list(root.glob('*.py')) + list((root / 'tests').glob('test_*.py')):
        text = path.read_text(encoding='utf-8', errors='ignore')
        if term in text:
            hits.append(str(path))
    print(term, hits)
PY
```

Interpretation:

- If a planned name already exists, read it first and update the plan before coding.
- If a near capability exists, prefer extending the owner or adding a thin adapter.
- If current `git status` is dirty, inspect the diff and avoid touching those files unless the current task explicitly owns them.
- If doctor is not valid JSON or key checks are red, stop and report the blocker before feature work.

## Context-recovery reading order

After context compaction or when resuming this work:

1. Read `.hermes/plans/2026-07-02_214416-scope-recall-memory-quality-kernel-plan.md`.
2. Read this file.
3. Run `git status --short --branch`.
4. Read only the module family relevant to the current task.
5. Read the direct tests listed for that module.
6. Run the narrow test first, then doctor/release gates as needed.

## Phase -1 acceptance checklist

- [ ] This module map exists and is current enough for the planned slice.
- [ ] Planned new modules are confirmed absent or deliberately redirected to existing owners.
- [ ] Large-file guardrails are followed.
- [ ] Dirty working-tree files are inspected before touching them.
- [ ] New tool surfaces have a defined schema layer before implementation.
- [ ] Any new maintenance/apply path has dry-run, audit, rollback, and test coverage.
