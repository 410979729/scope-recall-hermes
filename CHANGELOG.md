# Changelog

All notable changes to `scope-recall` will be documented in this file.

## [1.1.2] - 2026-06-14

### Fixed
- Sanitized gateway image attachment markers before capture/journal storage, removing local `image_cache/img_*` paths and inline image placeholders while preserving the user's surrounding text.
- Added regression coverage so screenshot-only payloads are rejected as empty and screenshot questions are journaled without local image paths.

## [1.1.1] - 2026-06-14

### Fixed
- Treated short assistant acknowledgement messages such as `Understood.`, `Noted.`, and common Chinese ACKs as trivial capture input so they cannot enter the journal.
- Prevented assistant-only journal rows from being promoted by heuristic or LLM journal digest, including legacy rows created before the ACK filter.
- Added memory-quality regression tests proving assistant-only acknowledgements are skipped rather than becoming durable memories.

## [1.1.0] - 2026-06-14

### Added
- Added the `hermes-scope-recall` standalone distribution shape with a `hermes-scope-recall` console script.
- Added `hermes-scope-recall install` to copy the provider into `$HERMES_HOME/plugins/scope-recall/` without touching provider-owned data under `$HERMES_HOME/scope-recall/`.
- Added `hermes-scope-recall verify` plus installer tests covering dry-run, forced replacement safety, Hermes memory-provider discovery, and CLI round trips.

### Changed
- Renamed the Python distribution package from `scope-recall` to `hermes-scope-recall` while preserving the Hermes provider ID `scope-recall` and Python import package `scope_recall`.
- Packaged plugin metadata, docs, and operator scripts inside the wheel package so the installer can register a complete unpacked Hermes provider from site-packages.
- Updated README install guidance for the supported standalone-provider path proposed for Hermes upstream documentation.

## [1.0.16] - 2026-06-14

### Fixed
- Probed LanceDB/PyArrow native imports in a child process before importing them inside Hermes, so no-AVX/AVX2 hosts that hit `Illegal instruction` are treated as unsupported instead of crashing the agent process.
- Added automatic `sqlite-bruteforce` vector fallback when the configured LanceDB companion is absent or unsafe and `vector.fallback_backend=sqlite-bruteforce` is set.

### Changed
- Added `vector.fallback_backend` to the default config and setup schema.
- Documented the native-safe vector path for non-AVX hosts and bumped package, plugin, release-check metadata, README, and stability docs to `1.0.16`.

## [1.0.15] - 2026-06-13

### Fixed
- Reused one chat-completions endpoint builder across capture, journal, and nightly digest paths so provider-specific endpoints and `append_v1=false` are honored consistently.
- Redacted sensitive HTTP/SSE error bodies before provider exceptions surface from Codex responses or streaming response parsing.
- Kept pure `role=tool` journal traces in provenance only; heuristic digest no longer promotes raw tool output into durable memory.
- Changed empty-store nightly scope inference to use an explicit or CLI fallback instead of silently defaulting to Telegram.
- Split readable aliases from writable scopes so legacy cross-platform platform scopes remain read-only unless an explicit migration writes them.
- Preserved the updated row's real `scope_id` when nightly digest updates vectors for legacy rows.
- Redacted secret scanner findings in the release gate while still reporting file, line, and rule evidence.

### Changed
- Added regression coverage for the v1.0.15 audit findings and updated the provider tool-trace test to assert journal-only provenance behavior.
- Bumped package, plugin, release-check metadata, README, and stability docs to `1.0.15`.

## [1.0.14] - 2026-06-13

### Added
- Added opt-in canonical identity mapping for cross-platform durable recall. When `identity.cross_platform_shared_scope=true` and explicit `identity.user_aliases` map platform accounts to one canonical user, `user`/`memory`/`project`/`ops` rows share a canonical durable scope while `general` scratch remains local to the platform/account/chat/session scope.
- Added query-time compatibility for legacy platform-specific durable shared scopes so mapped identities can still read existing rows before any explicit migration.
- Added digest transport controls for provider-specific OpenAI-compatible endpoints: `endpoint` / `chat_endpoint` and `append_v1=false`, including CLI support for `scripts/nightly-digest.py --endpoint` and `--no-append-v1`.
- Added regression coverage for default isolation, unmapped-account isolation, mapped durable sharing, scratch non-sharing, legacy shared-scope aliases, endpoint construction, and redacted provider HTTP errors.

### Fixed
- Fixed journal/nightly digest chat-completions calls that incorrectly forced `/v1/chat/completions` onto provider-specific roots such as Ark Coding Plan.
- Fixed maintenance tool schema registration so `maintenance_tools_enabled=true` is visible before provider `initialize()`, matching Hermes tool registration order.
- Preserved built-in curated memory default behavior for CLI sessions without an explicit user id while still allowing configured `cli_user_id_fallback` for canonical identity mapping.

### Changed
- Newly written provider, journal digest, and nightly digest rows include audit metadata for `raw_platform`, `raw_user_id`, and, when mapped, `canonical_user` / `scope_identity_mode`.
- Bumped package, plugin, release-check metadata, README, and stability docs to `1.0.14`.

## [1.0.13] - 2026-06-12

### Added
- Added lifecycle-aware conflict review: newly inserted contradictory durable memories now record bidirectional `contradicts` relations plus `needs_conflict_review` metadata without automatically superseding or hiding older rows.
- Added governance review candidates for local scratch rows, conflict-review rows, superseded/obsolete/rejected lifecycle rows, raw turn-source rows, low-confidence rows, and archive candidates so historical dirty data can be reviewed without automatic deletion.
- Added `scripts/migrate.legacy_hygiene.py`, a dry-run-first legacy hygiene migrator that backs up SQLite truth, archives historical `general`/raw/scratch rows without deleting content, and normalizes missing durable lifecycle/category metadata.
- Added regression coverage proving automatic conflict detection does not hide older rows, exact-id forget behavior matches docs, lifecycle metadata survives governance runs, dirty-history candidates are reported for operator review, LLM digest retries transient failures before quarantine, and legacy hygiene migration is backup-backed and read-only by default.

### Changed
- Recall still suppresses explicitly `superseded`, `obsolete`, `rejected`, and now `archived` rows by default, but automatic contradiction detection no longer writes `lifecycle=superseded`; operators must use explicit update/merge/delete actions after review.
- Journal LLM digest now classifies provider failures and retries transient timeout/rate-limit/network/server errors before quarantining; auth/quota/parse failures fail closed without wasteful retry loops.
- Governance classification now preserves existing lifecycle and conflict-review metadata instead of overwriting it with a fresh generic classification.
- Bumped package, plugin, release-check metadata, README, and stability docs to `1.0.13`.

## [1.0.12] - 2026-06-12

### Added
- Added journal-first provenance capture with `journal_entries`, `journal_digest_runs`, and `memory_journal_sources` tables. Eligible turn text is staged as provenance instead of being written directly as durable recall memory.
- Added `scripts/journal-digest.py`, a background digest entrypoint that groups related journal turns, creates high-density memory candidates, merge-upserts existing rows, links source journal evidence, and syncs the configured vector companion only for durable memory rows.
- Added weighted reciprocal-rank fusion (RRF) and entity-distance scoring primitives so lexical, vector, BM25, curated-memory, and entity-neighborhood signals can be combined without trusting incompatible raw score scales.
- Added regression coverage for journal/provenance storage, provider long-turn chunking, digest evidence links, same-topic merge/upsert behavior, LLM-first extractor defaults, non-silent LLM failure handling, background digest scheduling, doctor `.env` isolation, RRF promotion of cross-signal hits, and entity-distance reranking.

### Changed
- `sync_turn()` now defaults to journal-first staging and routes long eligible turns into the journal chunking path instead of dropping them at the outer capture-length gate. Legacy per-turn regex durable extraction is explicitly gated behind `per_turn_extraction.enabled=false` by default, and raw user fallback remains disabled by default.
- `on_session_end()` now captures compact tool execution traces into journal provenance; synchronous durable promotion is not the default, and LLM session-end digest requires explicit `journal.allow_session_end_llm=true`.
- Journal digest now defaults to LLM-first extraction, groups by conversation session/topic, runs from a non-blocking background scheduler controlled by `journal.digest_interval_hours`, honors `journal.max_entries_per_digest`, records skipped candidates in `journal_rejections`, preserves provenance by default (`retention_days=0`), and requires explicit `journal.allow_heuristic_fallback=true` or `--extractor heuristic` before degraded heuristic fallback can consume journal evidence.
- Hybrid retrieval now includes bounded BM25 final-score contribution and RRF metadata blending while preserving current-turn recall, scope isolation, and lexical/vector fallback behavior.
- Bumped package, plugin, release-check metadata, README, DESIGN, and stability docs to `1.0.12`.

### Fixed
- Fixed unrelated journal tasks over-merging through a global `scope-recall` bucket, while preserving same-session merge/upsert behavior for continuing work.
- Fixed `scope_recall_forget`/dedupe deletion leaving orphan `memory_journal_sources` provenance rows.
- Extended `scripts/doctor.py` to validate journal/provenance schema, backlog, digest run, rejection, and orphan-link health without leaking profile `.env` values into process-global `os.environ`.

## [1.0.11] - 2026-06-11

### Added
- Added a `MiniMaxEmbedder` (provider: `minimax`) and a `build_embedder` route for the MiniMax `embo-01` embedding endpoint. The endpoint is non-OpenAI-compatible (`texts` plural, `type: "db" | "query"`, `vectors` reply), so the embedder talks to it directly via `urllib`.
- Added MiniMax document/query request-type separation: vector indexing/upserts use `db`, while vector search uses `query` through the embedder query path.
- Added optional MiniMax `GroupId` support for accounts that still require it, with `group_id` / `group_id_env` configuration.

### Changed
- Bumped package, plugin, release-check metadata, README, and stability docs to `1.0.11`.

## [1.0.10] - 2026-06-10

### Added
- Added deterministic external-artifact enrichment for direct memory writes and nightly digest candidates. GitHub issues, PRs, commits, releases, repositories, and URLs now get a human-readable `Artifact anchors:` block plus structured `artifacts` metadata, derived entities, and tags.
- Added `scope_recall_store_secret_index`, an explicit credential-index tool that stores searchable service/account/purpose/vault-reference metadata without storing plaintext secret values in SQLite, FTS, vector text, exports, logs, or chat replies.
- Added regression coverage for direct GitHub issue anchors, nightly digest artifact preservation, and secret-index export hygiene.

### Changed
- Bumped package, plugin, README, stability contract, and release-check metadata to `1.0.10`.
- Updated project URLs to the Hermes-specific repository slug `410979729/scope-recall-hermes` while keeping the runtime package and plugin ID as `scope-recall`.
- Strengthened nightly digest extraction instructions so external artifacts retain repo/name, issue/PR/release/commit identifiers, exact URLs, and available status/date/author/next-step anchors.

### Fixed
- Fixed vague memory records that mentioned external work without durable lookup anchors, forcing later sessions to rediscover issue/PR/release URLs from scratch.
- Fixed a secret-index false positive where multiline credential metadata such as a label ending in `credential` followed by `Kind: api_key` could be rejected as `secret-like-content` even though no plaintext secret was stored.

## [1.0.9] - 2026-06-09

### Added
- Added the `sqlite-bruteforce` vector backend for non-AVX or native-dependency-sensitive hosts. It stores rebuildable vector companion rows in `$HERMES_HOME/scope-recall/vector.sqlite3` while keeping `$HERMES_HOME/scope-recall/memory.sqlite3` as the truth source.
- Added `docs/naming.md` to define the public `scope-recall` spelling versus Python/tool/config identifiers that use `scope_recall`.
- Added `docs/hermes-upstream-recommendation-plan.md` with the standalone-provider checklist and Hermes upstream recommendation route.
- Added regression coverage for native-free vector imports, `sqlite-bruteforce` runtime sync/search, doctor reporting, and repair-script rebuilds.

### Changed
- Moved `lancedb`/`pyarrow` to the `lancedb` optional dependency extra. Default package import no longer requires native vector dependencies, while CI and LanceDB installs use `.[lancedb]`.
- Extended `vector.backend` configuration, runtime dispatch, doctor diagnostics, release checks, and repair tooling to cover both `lancedb` and `sqlite-bruteforce` companions.
- Updated installation docs to distinguish the recommended LanceDB path from the native-free SQLite fallback path.

### Fixed
- Fixed the no-AVX/native-import failure mode where importing vector runtime modules could fail before the operator had a chance to select a safer backend.
- Fixed the #4 naming ambiguity by documenting where each spelling is authoritative instead of performing a risky whole-repository rename.

## [1.0.8] - 2026-06-03

### Added
- Added deterministic Chinese entity fallback hints so compound input-method terms such as `自然码` and `双拼` are extracted even when Jieba is unavailable or segments differently in CI/runtime environments.
- Added `docs/external-shared-memory.md` to document safe bridge boundaries for deployments with a central shared backend such as PostgreSQL.

### Changed
- Bumped package, plugin, release-check metadata, README, and stability docs to `1.0.8`.
- Reworded the V1 scope documentation around the positive architecture: local-first recall, SQLite truth storage, LanceDB companion retrieval, explicit bridge boundaries for external shared backends, Hermes-native skill ownership for procedural knowledge, and deployment-driven observability.
- Included the external shared-memory integration document in release-gate source and wheel checks.

### Fixed
- Fixed the GitHub Actions regression where the Chinese entity test could fail because `自然码` was not extracted when Jieba was not installed or did not split the compound phrase as expected.

## [1.0.7] - 2026-06-03

### Added
- Added `scripts/doctor.py`, a read-only source/runtime health report that checks release metadata alignment, SQLite truth availability, LanceDB companion readability, and repair recommendations.
- Added BM25 as an optional final-score component for hybrid retrieval, while preserving candidate-local SQLite FTS5 `bm25()` normalization and raw-score metadata for explainability.
- Added optional Jieba-backed Chinese entity extraction and broader code-ish entity extraction for mixed Chinese/English project memory.
- Added explicit temporal-decay scoring, deterministic source-trust priors, typed `memory_relations`, and conservative contradiction marking with feedback/metadata evidence.
- Added opt-in shared-pool scope stats plus `scope_recall_inspect`, `scope_recall_explain`, and `scope_recall_benchmark` observability tools.

### Changed
- Bumped package, plugin, release-check metadata, README, and stability docs to `1.0.7`.
- Extended the release gate stable-tool check to cover the full public V1 default tool surface and new observability tools.

### Fixed
- Aligned the README public version text with package/plugin metadata and documented the Hermes venv + `PYTHONPATH` test command so plain `pytest` from an unrelated environment is not mistaken for release evidence.
- Preserved pure lexical recall in default hybrid mode when BM25 metadata exists but `bm25_weight` is still zero, avoiding accidental dampening of local/general matches.
- Reduced generic English entity noise so related-entity results keep explicit caller-provided entities such as `yuheng` visible.

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
- Tightened V1 documentation around SQLite truth ownership, LanceDB companion-cache rebuildability, and OpenClaw migration/compatibility boundaries.
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
