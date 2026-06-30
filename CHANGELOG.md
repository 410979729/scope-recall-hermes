# Changelog

All notable changes to `scope-recall` will be documented in this file.

## [Unreleased]

## [1.6.1] - 2026-06-30

### Changed
- Published documentation, packaging, and release-provenance updates as a dedicated patch release after `v1.6.0` had already been tagged and published.
- Aligned public documentation and release metadata so the GitHub tag, package version, wheel, sdist, and PyPI release identify the same `1.6.1` source tree.
- Preserved the v1.6 product contract across forgetting, governance, journal recovery, dashboard, experience replay, installer rollback, fact freshness, relation extraction, and golden benchmark surfaces; this release does not introduce storage-schema or tool-surface changes.

### Fixed
- Fixed release provenance ambiguity by publishing the current release commit under a distinct `v1.6.1` tag instead of reusing `v1.6.0`.

## [1.6.0] - 2026-06-29

### Added
- Added production packaging and rollout surfaces: dry-run-by-default installer rollback/apply flows, operator runbooks, cross-profile rollout planning, response-contract documentation, and release-gate wheel/install/doctor smoke checks.
- Added governance cleanup, forgetting, and rollback tooling for soft-archive batches, including governance audit coverage reporting, default rollback support for `scope_recall_forget`, and transaction-bound audit inserts.
- Added journal recovery tooling for retry-exhausted/dead-letter entries, including replay scheduling, operator no-replay classification, dead-letter category reporting, and dashboard visibility.
- Added Experience Kernel productization: playbook bootstrap/search/inspect/feedback/review/promote tools, conservative auto-promotion quality gates, duplicate playbook reporting, supersede CLI review routing, and experience replay benchmarks.
- Added fact freshness scaffolding for durable factual memories, with dashboard coverage/staleness reporting and freshness-aware recall policy hooks.
- Added relation extraction and graph hygiene support for owned-by/affects/depends-on/supersedes/same-topic style edges, contradiction-safe edge generation, and repair/counting scripts.
- Added golden benchmark fixtures and release-gate execution for commercial recall quality, including low-value scratch exclusion, archived-old-fact exclusion, and entity/project isolation cases.

### Changed
- Changed `scope_recall_forget` to soft archive by default with governance audit receipts and explicit rollback commands; hard delete is limited to maintenance flows.
- Changed delete/dedupe/nightly cleanup semantics to vector-first fail-closed behavior so SQLite truth is preserved when rebuildable vector companion cleanup fails.
- Changed vector repair to dry-run by default; writes now require explicit `--apply` or the `vector repair apply` CLI route.
- Changed recall/profile filtering so archived, superseded, rejected, candidate, and in-progress rows do not consume ordinary recall budget unless explicitly requested.
- Changed nightly digest and journal extraction paths to report fallback/dead-letter/quarantine status through doctor/dashboard instead of hiding opaque failures.
- Changed memory quality archive/reporting paths to distinguish active secret/pollution findings from archived historical rows.
- Split the scope-recall doctor into focused `doctor_*` modules while keeping `scripts/doctor.py` as the compatible CLI wrapper and preserving direct import re-exports used by tests/operators.
- Centralized graph hygiene repair/counting, maintenance dry-run helpers, digest result payload builders, recall pipeline merge/rank helpers, and provider schema construction into dedicated modules so future governance work has smaller review surfaces.

### Fixed
- Fixed governance audit transaction atomicity: `record_governance_audit_event()` is now a DDL-free INSERT helper, preventing sqlite `executescript()` from implicitly committing business updates before rollback/commit failure.
- Fixed soft-archive consistency when vector deletion succeeds but SQLite/entity/audit/commit later fails: SQLite is rolled back, the operation returns a failed receipt, and vector status is marked `needs_repair`.
- Fixed rollback reachability for `scope_recall_forget` archive batches by including that audit event type in default rollback candidates.
- Fixed top-level tool exception sanitization so fallback errors redact secret-like strings and local paths before returning to users.
- Fixed OpenAI-compatible hosted embeddings for OpenRouter-style backends by explicitly requesting `encoding_format="float"` from the OpenAI SDK (#24).
- Fixed SQLite provider initialization/bootstrap concurrency by opening the truth DB with a 10-second busy timeout instead of the Python sqlite default (#23).
- Hardened doctor runtime checks by opening the SQLite truth DB with URI `mode=ro` and by narrowing the doctor wrapper import fallback to `ImportError` so real import-time bugs are not hidden.
- Hardened release cleanup so the gate no longer removes repository-local `.venv` directories.

### Release verification
- Release artifacts are built only after the source tree passes the strict `scripts/check.release.py` gate in CI.
- Live-dashboard evidence in release-readiness documents is maintainer validation context, not a customer deployment health claim.

## [1.5.3] - 2026-06-26

### Added
- Added `scripts/repair.graph_hygiene.py`, a dry-run-by-default maintenance script that reports and, with `--apply`, removes orphan `memory_entities` / `memory_relations` rows from the rebuildable SQLite graph companion.
- Added `scripts/promote.memory_candidates.py`, a dry-run-by-default candidate-memory promotion planner/apply path that promotes safe ordinary `candidate` memories, optionally archives low-value noise with `--archive-noise`, and records governance audit events for applied mutations.
- Added doctor visibility for ordinary candidate-memory debt, including candidate count, age, target/source distribution, promotable rows, archive candidates, and samples so promoted-only profile behavior cannot silently starve on stale candidates.

### Changed
- `scope_recall_profile` now defaults SQLite rows to `lifecycle=promoted`; pass `include_candidates=true` to intentionally include non-hidden candidate rows while `include_general=true` remains the explicit switch for local scratch/general rows.
- Reduced the default primary-agent tool schema surface with a new `tool_schema_profile="compact"` default (6 tools, about 4.7 KB in repo-local measurement) that exposes core store/search/context/profile plus compact `scope_recall_memory` and `scope_recall_entity` dispatch tools; `tool_schema_profile="standard"` restores the legacy 20-tool read-only/diagnostic surface, and `tool_schema_extra_tools` can selectively expose diagnostics while staying compact.
- Kept the low-frequency `scope_recall_store_secret_index` schema behind `secret_index_tools_enabled=true`; direct calls also fail closed unless the operator explicitly enables it.

### Fixed
- Added lifecycle filtering to entity/profile graph read paths so `scope_recall_entity`, `probe`, `related`, and profile entity lookup hide `archived`, `superseded`, `obsolete`, and `rejected` memories consistently with the main recall path.
- Reduced deterministic entity-extraction noise from tool traces and filtered legacy noisy entity metadata/rows from graph read surfaces, including common tool tokens such as `read_file`, `search_files`, `execute_code`, `skill_view`, and `session_search`.
- Added a SQLite doctor graph-hygiene check that reports orphan graph companion rows and marks the runtime store as needing repair when they are present.
- Added a deterministic journal-digest durable-value gate so obvious webhook/notification/log/tool-summary noise is rejected before it can become durable `user`/`memory`/`project`/`ops` rows, while preserving reusable root-cause/fix/workflow candidates.
- Made `scripts/repair.vector_index.py` fail closed when the primary configured vector embedder is unavailable; operators must explicitly pass `--allow-fallback-embedder` before rebuilding with `vector.fallback_embedder`, and dry-run reports primary/fallback availability plus existing-vs-planned dimensions.
- Made maintenance dry-runs fail-safe: `scripts/repair.graph_hygiene.py` now accepts explicit `--dry-run`, `--dry-run` wins over accidental `--apply`, and candidate-promotion dry-run review output redacts secret-like text and private paths.

## [1.5.2] - 2026-06-25

### Added
- Added Recall Funnel traces for search/explain/benchmark paths, including candidate-pool sizing, per-stage candidate counts, filter counts, returned ids/chars, and retrieval timings.
- Added benchmark aggregate metrics for latency percentiles, known-answer recall, top-k accuracy, forbidden-id violations, filter counts, and optional prompt-budget hit rate.
- Added `scripts/benchmark.retrieval_regression.py`, an isolated synthetic benchmark that stress-tests lexical retrieval with distractor memories and Recall Funnel traces without requiring vector dependencies or API keys.

### Changed
- Added `retrieval.top_k` as the default tool result limit while preserving explicit per-call `limit` overrides.

### Fixed
- Made vector sync release tests use the deterministic `local-debug` embedder so release gates no longer depend on hosted embedding network availability.
- Synchronized `retrieval.top_k` across packaged `config.json` and in-code default config, exposed background journal digest health in `scope_recall_stats`, cached configured capture skip regexes to reduce per-turn filter overhead, and serialized vector companion mutations behind a provider-level lock.

## [1.5.1] - 2026-06-24

### Fixed
- Fixed strict release-gate dirty-tree checks in CI by ignoring known local/runtime scratch directories such as `.hermes-agent-src/` while still blocking real tracked or untracked source changes.

## [1.5.0] - 2026-06-24

### Added
- Added governance cleanup, journal recovery, operator dashboard, and repository-owned golden benchmark release-readiness tooling.
- Added golden benchmark cases to packaged artifacts and release metadata checks.

### Fixed
- Made `scripts/benchmark.golden.py` run in an isolated temporary Hermes home by default, copy the current plugin source for provider discovery, and keep any `--hermes-home` config read-only unless an explicit maintenance-only `--overwrite-config` flag is used with automatic backup/restore.
- Made release readiness run the golden benchmark and report dirty/untracked worktree state so new files cannot be missed before a release.
- Made hard-delete forgetting fail closed when no vector companion is provided, preventing SQL truth deletion that could leave stale vector hits.

## [1.4.5] - 2026-06-24

### Added
- Expanded `scope_recall_explain` so each returned row includes rank-aligned retrieval evidence for lexical/BM25/vector/RRF scores, metadata quality adjustment, entity overlap/distance bonuses, relation evidence/rerank contribution, memory-type temporal policy, temporal decay, recency bonus, threshold settings, and final score.
- Added rejected-candidate visibility to `scope_recall_explain`, including `rejected_count` and score-threshold rejection reasons for candidates filtered out before final ranking.
- Added assertion-case support to `scope_recall_benchmark`: cases can declare `expected_ids`, `forbidden_ids`, `min_rank`, `min_top_score`, and `auto_explain_on_fail` while preserving the legacy `queries` latency-smoke mode.
- Added benchmark regression cases and a CI/type-check matrix covering full extras, sqlite-only/native-free paths, missing optional jieba, shared-pool configuration, and pyright checks.
- Added memory-type-aware temporal policy so durable facts/preferences/procedures decay less aggressively than episodic or temporary evidence, with policy class/weight surfaced in explain.
- Added persisted `memory_relations` evidence to recall/explain and feature-gated relation-aware reranking through `retrieval.relation_rerank_enabled`.
- Added explicit `shared_pool` write policy: the pool remains read-only by default, `scope_mode="shared_pool"` writes require `shared_pool.write_enabled=true`, and writes are limited to configured durable targets.

### Fixed
- Made `scope_recall_update` re-run deterministic conflict/relation review after content or target changes so updates receive the same contradiction evidence as newly stored memories.
- Preserved accumulated feedback metadata during updates, including feedback counts, feedback-adjusted trust, conflict-review fields, and higher existing importance scores.
- Fixed journal digest skip/covered-candidate paths so filtered or already-covered candidates still advance the processed watermark instead of leaving permanent backlog.
- Fixed `scope_recall_forgetting_run` soft-archive persistence and hard-delete vector consistency, including vector record deletion and relation cleanup.
- Kept conflict-review metadata in sync on peer memories when related rows are deleted.
- Prevented heuristic journal digest from producing template/transcript-shaped durable memories such as `Operations workflow summary`, `Journal digest memory`, `user:`, or `assistant:` wrappers.
- Prevented low-signal Experience playbooks such as “继续”, “进度如何”, and fixed reply smoke tests from being auto-created as reusable procedures.
- Fixed explicit `scope_mode` handling so `local`, `shared`, and `shared_pool` writes are respected, semantic merge stays inside the selected scope, and shared-pool rows can be updated/merged when write-enabled.

## [1.4.4] - 2026-06-23

### Added
- Added `docs/contract.matrix.md`, a maintainer gate matrix that maps each major scope-recall contract to source files, targeted tests, release gates, and dynamic probes so large-context changes do not rely on an agent remembering the whole plugin.

### Fixed
- Made the SQLite brute-force vector companion safe to use from background journal/digest threads by opening the connection with `check_same_thread=False`, serializing access with a local lock, and closing/reopening the companion cleanly when `setup_vector_layer()` is rerun after a `needs_repair` state.
- Skipped `session_messages` tool dumps in session-end tool-trace journaling so current-session MCP readbacks cannot be restaged as memory-provider evidence.
- Enabled the native-safe `sqlite-bruteforce` vector fallback by default when LanceDB/PyArrow are absent or unsafe on non-AVX hosts.
- Bootstrapped the empty SQLite truth/journal schema and sqlite-bruteforce `vector_meta` records during `hermes memory setup` config saves so operators can verify installation before the first live message lazily initializes the provider.

## [1.4.3] - 2026-06-20

This is the first public release after `v1.4.0`; the GitHub release notes for `v1.4.3` include the cumulative `v1.4.1`, `v1.4.2`, and `v1.4.3` changes.

### Changed
- Defaulted `experience.auto_promote_low_risk` to `false` so automatic Experience scans create candidate playbooks unless low-risk auto-promotion is explicitly enabled.

### Fixed
- Blocked Experience auto-promotion for final-failure or incomplete task traces even when earlier logs contain `passed`/`ok` success tokens.
- Tightened final-failure detection to avoid false positives from words such as `cannot`, `no errors`, or `redacted`.
- Nightly digest now records `ok_with_fallback` and `extractor_used=heuristic-fallback` when LLM output is empty, unparsable, or filtered out before heuristic fallback writes candidates.
- Preserved already parsed LLM candidates when a later chunk explicitly returns `action=skip`, and continued parsing later chunks when an earlier chunk returns `action=skip`.
- Marked LLM fallback runs as `error` when heuristic fallback also produces no candidates.
- Made the optional legacy `memory-lancedb-pro` migration importer load LanceDB lazily. This importer is only used when importing existing OpenClaw memory stores into scope-recall; normal Hermes runtime, tests, and non-import workflows do not require OpenClaw or LanceDB.

## [1.4.2] - 2026-06-20

- Clarified Experience Kernel runtime docs so default prefetch and operator-enabled automatic promotion are described as separate controls.
- Added doctor visibility for nightly digest health, including latest status, recent fallback/error rows, and consecutive failure counts.
- Added release regression coverage for the Experience docs/schema promotion contract and nightly digest doctor reporting.

## [1.4.1] - 2026-06-19

### Changed
- Kept Experience preflight packet injection enabled by default but made background reusable-experience promotion opt-in (`experience.auto_promotion_enabled=false`) until the review queue has enough field feedback.
- Nightly digest runs that fall back from LLM extraction to heuristic extraction now record `ok_with_fallback` instead of plain `ok`, preserving success while making degraded provider health visible.

### Fixed
- Hardened report/evidence surfaces so session-end tool capture stores safe summaries by default, tool JSON errors redact local paths, journal rejections/errors, feedback notes, hygiene/forgetting previews, and Experience evidence use a shared report sanitizer for secrets, private paths, attachment markers, and raw tool traces.
- Made release-gate sentence-transformers coverage deterministic by mocking local encoder behavior in default tests and moving real HF model loading behind an explicit `SCOPE_RECALL_RUN_SENTENCE_TRANSFORMERS_INTEGRATION=1` integration test, preventing release readiness from depending on network/cache/GPU state.
- Preserved manual Skill governance anchors during Experience playbook anchor sync/backfill; source-managed related-skill anchors are now inserted only when missing instead of deleting and rebuilding all anchors for a playbook.
- Wired `experience.auto_promotion_enabled` into successful background/session-end journal digest runs so automatic reusable-experience promotion can run without manually calling `scope_recall_experience_promote`.
- Added Skill anchor/conflict enforcement for Experience Playbooks: promoted playbooks write `skill_anchors`, startup backfills anchors for existing promoted playbooks with `related_skills`, open conflicts force `no_reuse`, missing anchors degrade direct reuse to guided reuse, and stale/misleading feedback opens Skill conflict records.

## [1.4.0] - 2026-06-17

### Added
- Added the conservative Experience Kernel MVP: procedural playbook schema/tables, deterministic `procedural_playbook.v1` validation with per-step `capability_class`, scope-filtered playbook create/search/inspect/preflight/review/feedback/stats tools, feedback run counters, bounded preflight packet rendering controlled by `experience.prefetch_enabled`, doctor visibility for Experience tables, and a read-only `scripts/experience-replay.py` benchmark for comparing baseline coverage against Experience packets.
- Hardened the Experience Kernel MVP so `experience.enabled=false` is a global kill switch, create can only write `candidate`, promotion requires review, secret-like playbook/feedback text is rejected before persistence, legacy secret-like rows are redacted before tool/preflight output, corrupt core playbook JSON fails closed, `reuse_policy` is enforced before direct reuse, shared-scope feedback cannot demote global playbooks, terminal playbook statuses reject feedback, and CJK queries are not misclassified by whitespace-only low-signal checks.
- Added the first automatic reusable-experience loop: `scope_recall_experience_promote` scans evidence-backed journal task traces, writes `task_episodes`, creates reusable experience handbooks, auto-promotes low-risk verified handbooks, and keeps high-risk handbooks in `needs_review` for later agent/operator review instead of requiring end users to manually inspect raw memory rows.
- Added the first forgetting loop: `scope_recall_forgetting_report` and `scope_recall_forgetting_run` identify duplicate, scratch, tiny, wrapper-noise, and secret-like memory rows; the default action is soft archive via metadata, with hard delete reserved for explicit hard-delete candidates.
- Added journal backlog observability to `scripts/doctor.py`, including unprocessed role distribution, oldest backlog age, attachment/path contamination counts, configurable warn/fail thresholds, and operator recommendations for digest throughput and tool-trace hygiene.

### Changed
- Experience runtime injection is now enabled by default in the current source candidate through `experience.prefetch_enabled=true`; set `experience.prefetch_enabled=false` to keep runtime injection silent while exposing read-only playbook search/inspect/preflight/stats and scoped feedback tools for operator-guided reuse.
- Journal digest now dynamically raises the per-run entry limit when backlog exceeds the configured threshold, capped by `journal.max_entries_per_digest_ceiling`, so old queues can drain without permanently over-provisioning normal runs.

### Fixed
- Sanitized session-end tool traces with the same `sanitize_capture_text()` / `should_capture_text()` path used for user and assistant capture, preventing image attachment markers, `image_cache/img_*` paths, secret-like text, and low-value tool dumps from entering new journal rows.
- Classified failed LLM journal digest batches as `retry-exhausted:<kind>` or `dead-letter:<kind>` in journal rejections and run metadata, preserving retry/dead-letter evidence instead of leaving opaque quarantine rows.
- Redacted raw and partially masked provider key strings from journal digest quarantine error messages before storing rejection snippets or run metadata.

## [1.3.0] - 2026-06-14

### Added
- Added `scope_recall_profile`, a compact high-level profile/context surface over accessible durable `user`/`memory`/`project`/`ops` rows, optional local `general` scratch, and live Hermes curated `USER.md`/`MEMORY.md` entries.
- Added regression coverage proving the profile surface is registered as a provider tool, live-reads curated memory without copying it into SQLite, preserves gateway user isolation, recalls durable rows across sessions for the same user, and excludes local `general` scratch unless requested.

### Changed
- Documented why this is a minor release: it adds a new public tool/API surface without breaking the V1 storage or runtime compatibility contract.

## [1.2.1] - 2026-06-14

### Fixed
- Preserved surrounding user text when gateway image attachment markers or local `image_cache/img_*` paths appear inline rather than on their own line, while still stripping the attachment metadata before journal/capture storage.
- Added regression coverage for inline attachment marker sanitization so pre-compression journal staging cannot silently drop the user's actual sentence.

## [1.2.0] - 2026-06-14

### Added
- Added `ScopeRecallMemoryProvider.on_pre_compress()` so Hermes context-compression boundaries stage sanitized user/assistant messages into the journal before old turns are summarized/discarded.
- Added regression coverage proving pre-compression staging strips image attachment metadata, filters wrappers/tool output/secret-like text/trivial acknowledgements, and never writes raw compression-boundary content directly into durable memory.

### Changed
- Relaxed vector stats regression coverage to accept the designed `sqlite-bruteforce` fallback when LanceDB/PyArrow is unavailable or unsafe, while still requiring a ready vector companion and fallback evidence.

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
- Added `docs/upstream-recommendation.md` with the standalone-provider checklist and Hermes upstream recommendation route.
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
