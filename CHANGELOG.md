# Changelog

All notable changes to `scope-recall` will be documented in this file.

## [1.0.6] - 2026-06-01

### Added
- Added `capture_llm` module: LLM-powered semantic extraction of user+assistant turns into classified durable memory (preference, workflow, pitfall, decision, etc.) with user-configurable model and endpoint.
- Added `capture_llm` configuration block (`capture_llm.enabled`, `capture_llm.model`, `capture_llm.base_url`, etc.) with safe defaults (disabled by default, requires API key).
- LLM extraction runs in `sync_turn` before legacy regex extraction; if LLM succeeds, regex and raw-user fallback are skipped to avoid noise.
- LLM extraction preserves entity and tag metadata on stored candidates for better recall targeting.

### Changed
- `sync_turn` now has a four-tier capture pipeline: LLM semantic extraction → regex extraction → raw user capture → raw assistant capture (legacy).
- Bumped package, plugin, and release-check metadata to `1.0.6`.
- Synced public README/stability/OpenClaw comparison wording with the v1.0.4/v1.0.5 entity, feedback, and nightly digest features.
- Extended the public `scope_recall_store` tool schema `memory_type` enum to include workflow-oriented digest types already accepted by the governance layer.

## [Unreleased]

## [1.0.5] - 2026-06-01

### Added
- Added `scripts/nightly-digest.py`, a profile-scoped daily conversation digest that reads Hermes `state.db`/legacy `lcm.db`, extracts durable memories, writes through the SQLite truth store, syncs the LanceDB companion when enabled, and records digest run/source ledgers.
- Added task-session workflow extraction so successful tool-heavy work can be retained as reusable `workflow`/tool-chain memory without storing raw tool or system output.
- Added digest safeguards for secret redaction, task-vs-normal session classification, dry-run planning, exact duplicate cleanup, and semantic skip/update/insert decisions against existing scope-recall rows.
- Added regression coverage for nightly digest session loading, sensitive-value redaction, workflow memory writes, digest ledgers, duplicate skips, and dry-run no-write behavior.

### Changed
- Bumped package and plugin metadata to `1.0.5`.
- Extended accepted `memory_type` values with workflow-oriented digest types such as `workflow`, `tool_trace`, `summary`, `pitfall`, and `decision`.

## [1.0.4] - 2026-05-31

### Added
- Added a local SQLite graph layer with `memory_entities` and `memory_feedback` tables.
- Added deterministic entity extraction and backfill for existing SQLite truth rows.
- Added `scope_recall_context`, `scope_recall_probe`, `scope_recall_related`, and `scope_recall_feedback` tools.
- Added memory type, importance, trust, entity, and tag metadata support for explicit `scope_recall_store` calls.
- Added recall ranking support for metadata quality and entity overlap while preserving lexical/vector gates.
- Added BM25 ordering for SQLite FTS5 candidates before recency tie-breaking, so older exact lexical matches are not cut from the candidate pool by newer weak hits.
- Added regression coverage for entity probe, related lookup, compact context rendering, feedback trust updates, and stats.

### Changed
- Bumped package and plugin metadata to `1.0.4`.
- Extended stats with scoped entity and feedback counts.
- Made `retrieval.candidate_pool` apply inside SQLite lexical candidate selection.

### Fixed
- Reject generic `[System note: ...]` gateway/runtime wrappers, interrupted-turn recovery prompts, and preserved task-list wrappers before they can enter automatic capture or manual write surfaces.
- Added regression coverage for the stale restored-message failure mode where an interrupted-turn system note could preserve an older user request and contaminate recall.
- Tightened hybrid vector-only automatic recall so mid-confidence semantic-neighbor drift does not inject unrelated durable memories when there is no lexical evidence.
- Added regression coverage for length-framed scope identifiers so delimiter-bearing `user_id` values cannot collide with split `user_id` + `chat_id` scope components.
- Added regression coverage for operator `scope_recall_dedupe(scope_only=false)` to ensure cross-scope duplicate cleanup matches the documented maintenance-tool semantics.

### Changed
- Refined the operator dedupe regression so it creates duplicate fixture rows through the provider write path while keeping vector sync disabled for deterministic storage-only setup.
- Reworded DESIGN operational follow-up from reviewer-specific cleanup into public deployment guidance.

## [1.0.3] - 2026-05-20

### Added
- Added structured memory classification metadata for new writes, including category, tier, kind, lifecycle, authority, confidence, sensitivity, expiry, entity, tag, and scope-mode fields.
- Added FTS hygiene repair coverage so missing, stale, or duplicate SQLite FTS rows are detected and repaired deterministically.
- Added hygiene-report coverage for structured metadata presence and release-time regression coverage for the expanded governance layer.

### Changed
- Isolated the default Gemini embedding credential to `SCOPE_RECALL_GEMINI_EMBEDDING_API_KEY`, avoiding accidental reuse of general OpenAI or Google API keys.
- Kept the OpenAI-compatible Gemini endpoint as the hosted default while retaining `local-hash` as the no-credential fallback.

## [1.0.2] - 2026-05-18

### Added
- Added `capture_filters.py` to centralize automatic capture hygiene and block runtime-wrapper text such as recent Telegram context, context-compaction handoffs, skill-review meta prompts, and secret-like literals before they enter SQLite or vector storage.
- Added regression coverage for capture filtering, structured content capture, context-wrapper rejection, and default assistant-response non-capture.
- Added storage receipts to `scope_recall_store`, `scope_recall_update`, and successful `scope_recall_merge` responses so governance companions can close promotion/merge/rejection loops against concrete write evidence.
- Added conservative curated-memory policy controls: global `USER.md` / `MEMORY.md` recall now requires opt-in for explicit gateway `user_id` contexts unless an allowlist/profile-global mode is configured.
- Added stable OpenClaw import fingerprint material for missing/invalid legacy timestamps so dry-run/import reruns remain idempotent.

### Changed
- Changed default automatic capture posture to reduce raw `general` noise: `capture_assistant=false`, `min_capture_length=40`, and `capture_hard_max_chars=2500`.
- Kept short extracted durable candidates eligible for capture even when raw-turn capture uses a higher minimum length, so concise user preferences and ops facts are not lost.
- Treat exact semantic-merge matches as duplicates rather than no-op merges, preserving existing memory ids without rewriting content.

## [1.0.1] - 2026-05-16

### Security
- Scoped all ID-based write paths (`scope_recall_update`, `scope_recall_merge`, query-driven delete plumbing, and dedupe deletes) to the current accessible scope set so a caller that learns an inaccessible memory id cannot update, merge, or delete that row from a different user, sibling agent, or local chat/thread/session scratch scope. Ordinary merge calls now fail if any requested source id is missing or inaccessible, including explicit-content merges that would otherwise silently overwrite the target. Ordinary update/merge calls now also reject shared/local mode changes, preventing durable rows from becoming cross-window `general` scratch or local merges from swallowing shared durable memory.
- Restricted maintenance tools behind explicit `maintenance_tools_enabled=true`. `scope_recall_dedupe`, `scope_recall_govern`, and `scope_recall_repair` are hidden from the default tool schema and fail closed unless operator mode is enabled; `scope_recall_export(scope_only=false)` also requires operator mode.
- Changed `scope_recall_dedupe` default behavior to current-scope-only. Cross-scope dedupe remains available only as an operator maintenance action.

### Changed
- Reframed the scope model as permanent shared memory plus local scratch scope: durable `user`/`memory`/`project`/`ops` rows follow the same user + agent identity across windows/chats, while `general` rows stay local.
- Aligned package metadata, plugin metadata, release checker, README, stability contract, and design docs with the public `v1.0.1` tag.
- Added `CONTRIBUTING.md` to verified wheel data files so installed release docs match the README documentation table.

## [1.0.0] - 2026-05-15

### Added
- Declared the first stable V1 release line with explicit provider identity, storage, tool, retrieval, migration, and runtime-freshness contracts in `docs/stability.md`.
- Added V1-grade release checks for stable metadata, required documentation, wheel contents, and public-facing version consistency.
- Kept release-tree scanning focused on `scope-recall` sources when CI clones Hermes into `.hermes-agent-src` for runtime compatibility tests.
- Added a public README structure with badges, quick start, architecture diagram, tool quick reference, troubleshooting notes, and release-gate guidance.

### Changed
- Promoted package and plugin metadata from `0.2.0` to `1.0.0`, while keeping the public package classifier at beta/release-candidate maturity until broader field use.
- Aligned the public Python support floor and CI matrix with the current Hermes runtime requirement of Python 3.11+.
- Tightened V1 documentation around SQLite truth ownership, LanceDB companion-cache rebuildability, and non-goals versus OpenClaw `memory-lancedb-pro` parity.
- Changed GitHub Actions to run `scripts/check.release.py` as the remote CI gate so CI matches the local V1 release audit.
- Replaced agent-specific author/copyright wording with project contributor wording and added `SECURITY.md` plus a `py.typed` marker for public-release hygiene.
- Fixed scope id serialization to avoid delimiter-collision between user/chat/thread/session components and aligned `scope_recall_dedupe(scope_only=false)` with its documented cross-scope semantics.

## [0.2.0] - 2026-05-12

### Added
- Added vector audit stats for physical LanceDB row count, unique id count, and duplicate extra row count.
- Added regression coverage for duplicate vector row repair, stale vector row cleanup, vector upsert failure degradation, light top-level package import, and the intentional `on_memory_write` no-op boundary.
- Renamed public provider from `lancepro` to `scope-recall` with a deprecated compatibility shim left in place for the old plugin directory.
- Added SQLite truth store + LanceDB vector companion architecture for hybrid current-turn recall.
- Added scope isolation coverage for `chat_id`, `thread_id`, and `gateway_session_key`.
- Added focused release docs: migration notes, upstream differences, and OpenClaw import guidance.
- Added idempotent OpenClaw import tooling with stable source fingerprints and an `import_ledger`.
- Added release bootstrap files: `pyproject.toml`, `.gitignore`, and `CONTRIBUTING.md`.
- Added GitHub Actions CI and a local `scripts/check.release.py` gate for test/build/secret/path/artifact verification.
- Added `scripts/repair.vector_index.py` to rebuild the LanceDB companion from SQLite truth with backup support.

### Changed
- Switched active Hermes memory provider to `scope-recall`.
- Refactored provider internals by splitting migration logic, recall fusion, capture flow, storage views, and tool handling into dedicated modules.
- Changed vector maintenance from init-time full rebuild toward incremental sync by stable row id and `updated_at`, including stale-row cleanup and duplicate physical-row repair.
- Clarified README and DESIGN documentation to describe the real runtime architecture, configured Gemini OpenAI-compatible default embedder, and local fallback boundary.
- Updated release regression coverage so the default runtime path explicitly verifies fallback to `local-hash` when API embeddings are unavailable, while dimension-rebuild coverage uses an explicit local-hash config override.
- Fixed wheel packaging so the published artifact installs as an importable `scope_recall` package instead of scattering provider modules at site-packages top level.
- Restored Python 3.10/3.11 compatibility in `vector_store.py` by removing 3.12-only f-string quoting syntax.
- Included the OpenClaw import script in wheel data files for public release completeness.
- Preserved SQLite truth writes when LanceDB delete/upsert fails and marked the vector layer `needs_repair` for later repair.
- Kept top-level `import scope_recall` free of Hermes runtime imports; `register()` lazy-loads provider code.
- Documented `on_memory_write` as an intentional observational no-op because curated memory files are live-read instead of mirrored.
- Replaced dynamic `ALTER TABLE` f-string construction with an explicit allowlisted migration mapping and changed test placeholder keys to obvious non-secrets.

### Compatibility
- Legacy `lancepro_store`, `lancepro_search`, and `lancepro_stats` aliases remain accepted during transition.
- Legacy `$HERMES_HOME/lancepro/` SQLite/config storage is migrated forward on first initialization.

### Known limitations
- Vector repair/rebuild is available through `scripts/repair.vector_index.py`, but live gateway runtime freshness still requires an explicit service restart / human-triggered verification after deployment.
- OpenClaw historical imports still require an explicit one-shot import step; they are not automatically reused.
## 2026-05-20 — Retrieval hygiene regression

- Removed arbitrary recent-memory backfill from lexical SQLite retrieval. This prevents unrelated ordinary turns from recalling fresh durable ops rows (for example OpenClaw / 凌晨 task context) solely because of source/target bonus.
- Added a `vector_only_min_score` gate so weak vector-only matches cannot auto-recall unrelated durable ops rows without lexical evidence.
- Added alias-expanded SQL discovery so lexical-only recall still finds intended alias matches such as `response style` → `replies` without broad recency scans.
- Added regression coverage for unrelated-query suppression, high-confidence semantic hits, relevant lexical hits, and alias-expanded discovery.
