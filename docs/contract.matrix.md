# Scope Recall Contract Matrix

This matrix is the project-level memory for maintainers and agents working on
`scope-recall`. The plugin is larger than a single model context window, so
changes must be bounded by executable contracts instead of relying on the
current agent remembering every feature.

Use this document before changing code:

1. Identify the contract rows touched by the change.
2. Read the listed source files and nearby tests.
3. Add or update regression coverage before or with the fix.
4. Run the required targeted gates, then the repository-wide gates.
5. For stateful behavior, prefer dynamic probes that inspect durable outputs
   such as SQLite rows, doctor payloads, run metadata, or packaged wheel
   contents.

## Global rules

- SQLite is the truth source. Vector stores, summaries, and derived indexes are
  rebuildable companions.
- Scope filters must run before recall results are trusted or injected.
- Public tool names and compatible schemas must not disappear silently.
- Runtime prompts must stay budgeted. Candidate explosions must not become
  prompt explosions.
- Secrets, local attachment cache paths, raw tool dumps, and private filesystem
  paths must not leak into durable recall or public reports.
- Live runtime verification is distinct from source-tree verification. A running
  Hermes gateway that predates a code change has not loaded that change until it
  is explicitly restarted/reloaded and smoke-tested.
- Peer or autonomous-agent review is advisory until reproduced against source,
  tests, and state outputs.

## Contract rows

### 1. Public provider and tool surface

Contract:

- Stable `scope_recall_*` tool names remain registered.
- Tool schemas remain compatible unless a versioned breaking change is planned.
- Public report JSON includes stable top-level `schema_version` identifiers documented in `docs/response-contracts.md`.
- Disabled features fail closed with clear messages rather than disappearing.

Primary files:

- `provider.py`
- `tooling.py`
- `schemas.py`
- `config.py`
- `plugin.yaml`

Primary tests and gates:

- `pytest -q tests/test_provider.py tests/test_experience_tools.py tests/test_release.py`
- `python scripts/check.release.py`

Dynamic probes:

- Load the provider in a temporary Hermes-like plugin context.
- Assert every stable tool listed in `scripts/check.release.py` appears in both
  the source schema and release metadata.

### 2. Scope isolation and accessible-memory policy

Contract:

- `user`, `memory`, `project`, and `ops` rows may be durable across authorized
  windows/chats for the same user and agent identity.
- `general` rows are local scratch unless explicitly requested.
- Cross-user, cross-agent, cross-workspace, and unrelated chat/thread/session
  bleed is forbidden.

Primary files:

- `scope.py`
- `storage_views.py`
- `recall.py`
- `provider.py`
- `memory_ops.py`

Primary tests and gates:

- `pytest -q tests/test_retrieval_policy.py tests/test_storage_views.py tests/test_provider.py`
- `pytest -q tests/test_audit_regressions.py tests/test_v1015_audit_regressions.py`

Dynamic probes:

- Create memories with overlapping text across multiple users/agents/scopes.
- Query from one scope and assert foreign rows are not returned or injected.

### 3. SQLite truth store, schema, and migrations

Contract:

- SQLite remains the canonical truth store.
- Schema migrations are idempotent and preserve existing rows.
- FTS rows and supporting indexes remain consistent with truth rows.

Primary files:

- `sql_store.py`
- `models.py`
- `migration.py`
- `storage_views.py`
- `scripts/doctor.py`

Primary tests and gates:

- `pytest -q tests/test_sql_store_fts.py tests/test_storage_views.py tests/test_doctor_experience.py`
- `python scripts/check.release.py`

Dynamic probes:

- Run `ensure_schema()` against an empty DB and an already migrated DB.
- Insert/update/delete representative rows and verify FTS/search visibility.

### 4. Lexical, vector, graph, and RRF retrieval

Contract:

- Lexical/BM25, vector, alias, curated, and graph signals may combine, but scope
  policy remains authoritative.
- Reciprocal-rank fusion and score filters must not suppress all high-confidence
  exact matches without an explainable reason.
- Vector-only candidates must meet vector policy thresholds.
- Search/explain/benchmark paths must expose enough Recall Funnel evidence to
  diagnose candidate-pool, filtering, and prompt-budget regressions without raw
  prompt dumps.

Primary files:

- `recall.py`
- `scoring.py`
- `storage_views.py`
- `graph.py`
- `memory_ops.py`
- `tooling.py`
- `schemas.py`
- `vector_store.py`
- `sqlite_vector_store.py`
- `vector_runtime.py`
- `embedders.py`

Primary tests and gates:

- `pytest -q tests/test_retrieval_rrf_graph.py tests/test_retrieval_policy.py tests/test_scoring.py`
- `pytest -q tests/test_vector_policy.py tests/test_sqlite_vector_store.py tests/test_optional_vector_deps.py`
- `pytest -q tests/test_benchmark_regression_cases.py`
- `PYTHONPATH=/path/to/hermes-agent:/path/to/scope-recall python scripts/benchmark.retrieval_regression.py`

Dynamic probes:

- Build a small known-answer corpus with exact, semantic, and misleading near
  matches.
- Verify top results, excluded results, explain output, benchmark aggregate
  metrics, and Recall Funnel stage/filter counts for each query.

### 5. Prompt rendering and context budgets

Contract:

- Automatic recall remains bounded by item and character budgets.
- Large candidate pools must be reduced before prompt injection.
- Rendered context should preserve evidence and target labels without leaking raw
  private paths or tool dumps.

Primary files:

- `prompting.py`
- `recall.py`
- `provider.py`
- `experience_preflight.py`

Primary tests and gates:

- `pytest -q tests/test_provider.py tests/test_experience_preflight.py tests/test_roadmap_retrieval.py`

Dynamic probes:

- Generate more eligible memories than the prompt budget allows and verify the
  final rendered packet respects max items, per-item chars, and total chars.

### 6. Journal capture and compression-boundary staging

Contract:

- Eligible conversation turns are staged as journal/provenance, not directly as
  durable memory.
- Compression-boundary capture strips attachment markers, local image cache
  paths, wrappers, low-value acknowledgements, secrets, and noisy tool dumps.
- Capture filters must preserve the user's meaningful surrounding text.

Primary files:

- `journal.py`
- `capture.py`
- `capture_filters.py`
- `capture_llm.py`
- `provider.py`

Primary tests and gates:

- `pytest -q tests/test_capture_filters.py tests/test_capture_llm_manual.py tests/test_journal_digest.py`
- `pytest -q tests/test_tool_hygiene.py tests/test_audit_regressions.py`

Dynamic probes:

- Stage turns containing inline image markers, tool JSON, and secret-like text.
- Inspect journal rows and rejection metadata instead of only return values.

### 7. Nightly digest and background extraction

Contract:

- LLM extraction failures, empty arrays, parse errors, and filtered outputs are
  observable as degraded or error states instead of plain `ok`.
- Explicit LLM `action=skip` is chunk-scoped and must not erase valid candidates
  from other chunks.
- Heuristic fallback must record fallback metadata.

Primary files:

- `nightly_digest.py`
- `scripts/nightly-digest.py`
- `scripts/doctor.py`
- `journal.py`

Primary tests and gates:

- `pytest -q tests/test_nightly_digest.py tests/test_journal_digest.py tests/test_doctor_experience.py`

Dynamic probes:

- Use temp journal entries and fake LLM outputs for candidate, explicit-skip,
  empty, invalid JSON, and filtered cases.
- Verify run metadata, fallback rows, status fields, and inserted candidates.

### 8. Experience Kernel playbooks and promotion

Contract:

- Playbooks are scope-filtered procedural memory, not unscoped global advice.
- High-risk playbooks require review.
- Final failed, blocked, or incomplete task traces must not create or promote a
  reusable playbook, even if earlier logs contain success tokens.
- Feedback updates counters and confidence without mutating terminal statuses
  unsafely.

Primary files:

- `experience_models.py`
- `experience_store.py`
- `experience_preflight.py`
- `experience_promotion.py`
- `experience_replay.py`
- `gating.py`

Primary tests and gates:

- `pytest -q tests/test_experience_schema.py tests/test_experience_store.py tests/test_experience_preflight.py`
- `pytest -q tests/test_experience_promotion.py tests/test_experience_replay.py tests/test_experience_tools.py`

Dynamic probes:

- Insert successful, failed, blocked, stale, and misleading traces into a temp DB.
- Inspect `task_episodes`, `procedural_playbooks`, feedback counters, and
  preflight packets.

### 9. Governance, forgetting, hygiene, and conflicts

Contract:

- Governance tools must be auditable and scope-filtered.
- Forgetting defaults to safe soft archive unless hard deletion is explicitly
  requested and justified.
- Hygiene reports should classify noise without deleting unrelated data.
- Conflict records must affect reuse decisions deterministically.

Primary files:

- `governance.py`
- `forgetting.py`
- `hygiene.py`
- `memory_ops.py`
- `experience_store.py`

Primary tests and gates:

- `pytest -q tests/test_forgetting.py tests/test_hygiene.py tests/test_skill_governance.py`
- `pytest -q tests/test_conflict_governance.py tests/test_governance_contract_regressions.py`

Dynamic probes:

- Run governance/forgetting/hygiene against a temp DB with duplicate, stale,
  archived, secret-like, and conflict rows.
- Verify metadata transitions and retained audit evidence.

### 10. Secret index and artifact anchors

Contract:

- Plaintext secrets are never stored in SQL, FTS, vector stores, chat output, or
  logs by secret-index workflows.
- Secret indexes store safe metadata, vault references, and fingerprints only.
- Artifact anchors must refer to evidence without copying private local payloads
  into durable recall.

Primary files:

- `secret_index.py`
- `artifacts.py`
- `memory_ops.py`
- `http_utils.py`
- `capture_filters.py`

Primary tests and gates:

- `pytest -q tests/test_provider.py tests/test_release_scanner.py tests/test_tool_hygiene.py`
- `python scripts/check.release.py`

Dynamic probes:

- Store a secret index with `secret_value`, then inspect SQL/FTS/vector outputs
  for absence of the plaintext value.
- Render tool responses and reports to confirm redaction.

### 11. Installer, doctor, packaging, and release integrity

Contract:

- Standalone install verifies source metadata and copied plugin files.
- Wheels contain the expected runtime files, docs, and scripts.
- Release gates must not depend on network-only model downloads or local private
  runtime state.
- Version metadata is consistent across `pyproject.toml`, `plugin.yaml`, docs,
  wheel name, and doctor output.

Primary files:

- `installer.py`
- `scripts/doctor.py`
- `scripts/check.release.py`
- `pyproject.toml`
- `plugin.yaml`
- `MANIFEST.in`
- `README.md`
- `CHANGELOG.md`

Primary tests and gates:

- `pytest -q tests/test_installer.py tests/test_release.py tests/test_release_scanner.py`
- `ruff check .`
- `python scripts/check.release.py`

Dynamic probes:

- Build a wheel in a temp directory.
- Install it into a temp target.
- Run installer verify and doctor from the installed copy.

## Gate selection by change type

Small documentation-only change:

- `pytest -q tests/test_release.py`
- `python scripts/check.release.py` if packaging, release, or source-file lists
  changed.

Single module behavior fix:

- Relevant row's targeted tests.
- Neighboring row tests for shared interfaces.
- `ruff check .`
- Full `pytest -q` before declaring release readiness.

Stateful or safety-sensitive fix:

- Targeted tests.
- Dynamic probe that inspects durable state or rendered output.
- Full `pytest -q`.
- `python scripts/check.release.py`.

Public API, packaging, release, or tool-surface change:

- `pytest -q tests/test_provider.py tests/test_release.py tests/test_installer.py`
- `ruff check .`
- `python scripts/check.release.py`
- Wheel/install/doctor read-back.

## Known next hardening targets

- Add a retrieval golden corpus for known-answer recall regression tests.
- Add a structured retrieval trace/funnel packet for production observability.
- Add a unified context budget manager for facts, constraints, workflows, and
  Experience packets.
- Split oversized modules only behind behavior-preserving tests and gates.
