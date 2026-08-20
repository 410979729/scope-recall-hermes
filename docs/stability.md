# Scope Recall V1 stability contract

`scope-recall` 1.10.0 keeps the V1 compatibility contract as a public source candidate on the last packaged `1.9.2` line. It adds journal source restore, bounded unresolved-journal retry/quarantine with fair budget deferral, a structured non-activatable inactive READY vector inventory, and one assembled production command port, while incorporating the unpublished `1.9.3` source interval: one cross-process writer per SQLite truth store, read-only follower mode, digest model calls outside write transactions, idle same-process dirty-peer recovery, and hardened lease cleanup. Stable provider/tool identities and SQLite authority are unchanged. This document does not imply a GitHub Release or PyPI artifact. It retains the explicit CJK lexical shadow generation with backup-first build, quality-gated compare-and-swap activation, and pointer-only rollback; Windows extended-length install/rollout primitives; and fail-closed endpoint transport policy. It retains the 1.8.7 fail-closed runtime-principal isolation, authoritative fact-freshness projection, canonical secret scanning, and pinned cross-platform release validation while preserving the 1.8.6 maintenance, operator, relation-restoration, and semantic-deduplication hardening. It preserves opt-in evidence-gated Fact Evolution, bitemporal current/as-of/history queries, bounded citation-grounded Reflection, observable local-embedder readiness/fallback, configurable semantic retention detail, cross-session first-turn recall coverage, and llama.cpp-compatible compact tool schemas without removing structured fact capabilities. SQLite remains authoritative; existing ordinary-memory behavior remains the default; durable fact targets resolve to shared scope while `general` remains local; journal action/checkpoint writes are atomic; and the existing public provider identity and stable tool names remain unchanged. It builds on the 1.7.2 storage, lifecycle, vector-generation, sanitization, and governance hardening patch. It builds on the 1.7.1 runtime-config, candidate-browser, external-bridge, and release-gate patch and the 1.7.0 productization release, which added event-digest evidence packets, reviewable candidate extraction, read-only memory browsing, candidate governance commands, Experience-to-skill bridge helpers, optional PGVector companion support, external shared-memory bridge contracts, explicit sensitivity governance, release-gate progress output, and same-process peer-provider SQLite lock recovery for recoverable `scope_recall_store` writes. It also builds on the 1.6.3 issue #25 SQLite recovery patch, the 1.6.2 graph-relation, maintenance-tool, playbook-review, and journal filtered-candidate hardening patch, and the 1.6.0 compatibility-preserving refactor of the doctor, graph-hygiene, maintenance, digest-result, recall-pipeline, and provider-schema internals. It retains the 1.5 line's promoted-only profile lifecycle safety, candidate-memory promotion planning, graph-hygiene repair, fail-closed vector-repair fallback handling, Recall Funnel observability, synthetic retrieval-regression benchmarks, and release-audit hardening on top of the 1.5.0 commercial governance and release-safety tooling: governance cleanup, journal recovery, operator dashboard reporting, packaged golden benchmarks, stricter release gates, fail-closed hard-delete safety, and stronger audit/release packaging checks. It preserves `scope_recall_profile`, compression-boundary journal staging through Hermes' `on_pre_compress()` memory-provider hook, the `hermes-scope-recall` standalone distribution and installer path, attachment-marker sanitization, journal ACK quality gates, native-safe LanceDB probing, and automatic SQLite vector fallback for non-AVX hosts.

This document defines the stable V1 compatibility surface and the areas that may evolve in patch or minor releases.

## Stable V1 identity

The stable public provider name is:

- `scope-recall`

The legacy `lancepro` naming exists only as a transition compatibility path. New installs and documentation should use `scope-recall`.

## Stable V1 install shape

The supported Hermes install shape for V1 is an unpacked local plugin directory. The `hermes-scope-recall` distribution installs the package into Python and then copies a complete provider directory into:

```text
$HERMES_HOME/plugins/scope-recall/
```

The distribution package name is `hermes-scope-recall`, the Python import package is `scope_recall`, and the Hermes provider/plugin ID is `scope-recall`.

V1 targets the current Hermes runtime line and therefore requires Python 3.11 or newer.

## Stable V1 storage contract

SQLite is the truth source.

Stable V1 guarantees:

- provider-owned records are stored in `$HERMES_HOME/scope-recall/memory.sqlite3`
- the `memories` table remains the authoritative source for stored Scope Recall rows
- row ids are stable within the SQLite truth store
- the configured vector companion is rebuildable companion state, not the source of truth
- losing or rebuilding `$HERMES_HOME/scope-recall/lancedb/` or `$HERMES_HOME/scope-recall/vector.sqlite3` must not delete SQLite truth rows
- vector repair may rebuild the configured companion from SQLite truth
- journal rows are provenance/staging evidence, not ordinary recall rows; digest-produced durable memories remain SQLite truth rows
- nightly and journal digest writes are still SQLite truth rows; digest run/source ledgers are audit metadata, not separate memory authorities
- live truth-health and reconciliation probes stay on provider-owned SQLite connections; raw database-file reads are restricted to explicit offline/quiesced backup or maintenance boundaries so same-process POSIX locks remain intact
- every writable FILE-backed connection classified as live truth acquires a connection-level truth writer lease on the storage directory before the SQLite pager opens. Live truth is the ASCII-case-insensitive `memory.sqlite3` basename, plus any existing same-directory filesystem alias or hardlink that `os.path.samefile` identifies with sibling `memory.sqlite3`; missing-path or `OSError` comparisons fail safe without bypassing a canonical basename. `:memory:`, read-only mode, and backup/staging/vector files that are not same-file aliases do not take that lease
- a provider publishes its writable truth connection before authorizer/schema setup; only a successful close clears that handle. Any close exception, including `sqlite3.ProgrammingError`, retains the exact connection and process authority, demotes the writer role fail-closed, and does not open a replacement because that exception alone does not prove the SQLite pager is closed
- process writer-lease registry state is bound to the current OS PID; a fork child must close inherited lock-handle copies without unlinking the parent sidecar and must attempt a fresh OS lock instead of joining inherited same-process state
- a shutdown request immediately blocks durable tools, capture, and background writes while the owner role and OS lease remain fail-closed until cleanup completes
- initialize is serialized on the existing writer lifecycle lock. A live owner/reader role, published connection, acquired lease, or live writer thread must finish shutdown before the same provider can initialize again. Concurrent initialize on one instance publishes exactly one runtime; the loser fails closed. Disabled-missing-principal or fully cleaned failed initialization may be retried
- same-process peer recovery compares truth-database identity with `os.path.samefile`, falling back to canonical realpath/normcase only when a path is missing or the OS rejects the comparison. A peer whose shutdown event is already set is skipped before role or transaction rollback; reader, unknown, failed, and shutdown peers never authorize retry
- a new writer-lease registry entry created by `truth_connection` is pin-only (`holders=0`, `connection_pins=1`); a named provider creates `holders=1`, `connection_pins=0`. Connection-first then provider join is `holders=1`/`pins=1`; releasing the provider leaves the pin and still blocks a child process until the last pin close clears the registry
- if `connect_truth_database` setup fails after a leased pager is open and close or pre-pager lease release also fails, the error is a `TruthDatabaseCleanupError` that retains a private retryable cleanup owner. `retry_cleanup()` is serialized and idempotent after success; a failed retry stays pending. Diagnostics never include the connection or path
- WAL deployments should use SQLite `3.51.3+` or a fixed backport (`3.50.7` or `3.44.6`); versions `3.7.0` through `3.51.2` otherwise contain SQLite's documented rare WAL-reset corruption race. Scope Recall can preserve its own lock boundaries but cannot replace the SQLite library linked into Python.

Schema evolution policy:

- patch/minor releases may add nullable columns, indexes, metadata fields, or migration ledger fields
- patch/minor releases must preserve existing V1 `memories` rows
- destructive schema changes require a major version bump or an explicit migration/export path

## Stable V1 runtime behavior

V1 keeps these behavior boundaries stable:

- recall is current-turn based through `prefetch(query)`
- `queue_prefetch()` remains a deliberate no-op to avoid stale next-turn injection
- built-in Hermes curated memory files are read live at recall time
- built-in curated memory writes are not mirrored into SQLite
- `on_memory_write()` remains observational unless a later major release changes storage ownership
- subagent / non-primary contexts do not expose Scope Recall tools
- maintenance tools (`scope_recall_dedupe`, `scope_recall_govern`, `scope_recall_hygiene`, `scope_recall_repair`, and `scope_recall_evolve`) are hidden and fail closed unless `maintenance_tools_enabled=true`
- Fact Evolution remains opt-in through `fact_evolution.enabled=true`; when disabled, nightly and journal factual candidates retain the legacy ordinary-memory path rather than being previewed, rejected, or consumed. When enabled, `user`/`memory`/`project`/`ops` actions are rebound to the shared durable scope, `general` remains local, and every authoritative quote must itself support the proposed claim with trusted first-person subject binding and positive claim-aligned polarity.
- journal Fact Evolution commits the fact action/receipt and the exact cited source-entry checkpoint atomically per candidate; nightly/journal prompts expose real message IDs, citations are chunk-scoped, parse/filtered chunks remain pending, `max_session_chars` caps total exposed text across chunks, and scheduler run IDs remain audit metadata without changing replay identity.
- `scope_recall_fact` provides scoped, read-only `current`, `as_of`, and `history` fact views when `temporal_queries.enabled=true`; `scope_recall_evolve` defaults to dry-run and cannot elevate the execution mode configured by a trusted local operator
- `scope_recall_reflect` is exposed only when `reflection.enabled=true`, remains read-only unless all explicit candidate-write gates pass, accepts at most one follow-up retrieval, rejects synthesis citations outside its bounded evidence pack, rejects unsupported top-level answers or observations, requires cited content clauses to preserve polarity, argument order, temporal/modal markers, conditionals, and quantifiers, and counts independent sources by provenance root rather than derived memory ID
- `install --activate` snapshots plugin/config/provider-config state and a verified SQLite online backup before replacement; activation-stage failures automatically compensate every captured surface and return a structured receipt rather than leaving a partial upgrade active
- `scope_recall_hygiene` is read-only and never performs cleanup; operators must explicitly run a separate delete/merge/dedupe action after reviewing its output
- `scope_recall_export` is available for scoped exports by default; `scope_only=false` requires `maintenance_tools_enabled=true`
- Experience Kernel runtime prompt injection is enabled by default through `experience.prefetch_enabled=true`, but packets remain advisory scaffolds and live user instructions/current evidence override old experience; operators can set `experience.prefetch_enabled=false` as a runtime injection kill switch.
- Experience Kernel create/review and maintenance promotion tools are hidden and fail closed unless `maintenance_tools_enabled=true`; ordinary read-only search/inspect/preflight/stats and scoped feedback tools remain available when `experience.enabled=true`.
- automatic experience promotion is opt-in after successful journal digest through `experience.auto_promotion_enabled=false` by default; when enabled, it still requires evidence-backed task traces with final successful closure, writes task episodes, creates playbook candidates by default because `experience.auto_promote_low_risk=false`, and keeps high-risk or final-failure playbooks gated by status/review. Set `experience.auto_promote_low_risk=true` only to auto-promote low-risk verified playbooks.
- forgetting tools are hidden and fail closed unless `maintenance_tools_enabled=true`; `scope_recall_forgetting_report` is read-only, and `scope_recall_forgetting_run` defaults to dry-run/soft archive rather than physical deletion
- `scope_recall_playbook_create` only writes `candidate`; promotion requires `scope_recall_playbook_review`, and direct reuse is blocked by confidence, reuse-policy, stale-fact, and risky-capability gates
- `scope_recall_store_secret_index` may store searchable credential indexes, vault references, and non-reversible fingerprint prefixes, but plaintext secret values must not be stored in SQLite content, metadata, FTS, vector text, exports, logs, or chat replies
- durable `user`/`memory`/`project`/`ops` rows are shared across windows/chats for the same platform + agent workspace + agent identity + user id by default
- when `identity.cross_platform_shared_scope=true` and explicit aliases map platform accounts to a canonical user, durable rows use a canonical `agent_workspace + agent_identity + canonical_user` shared scope
- `general` scratch rows remain local to the current platform/account/chat/thread or gateway session key, including when canonical durable identity mapping is enabled
- scoped read actions operate on the current accessible scope set: local runtime scope, shared durable scope, and explicit read-only legacy aliases when canonical cross-platform identity mapping is enabled
- scoped mutation actions operate only on writable current scopes: local runtime scope plus the current shared/canonical durable scope; legacy platform aliases remain read-only unless an explicit migration tool writes them
- `sync_turn()` defaults to journal-first staging; legacy per-turn durable extraction must be explicitly enabled through `per_turn_extraction.enabled=true`
- `scripts/journal-digest.py` may add or update durable rows from staged journal entries, but raw journal rows themselves are not recalled or indexed into the vector companion
- session-end tool capture stores tool execution summaries by default, not raw tool output; `journal.tool_trace_include_output_preview=false` is the safe default and must only be enabled for explicit debugging with redaction still applied
- `scripts/nightly-digest.py` may add or update durable rows, but it must not store raw `system` rows or raw `tool` output; task workflows are stored only as sanitized summaries with optional tool-name and verification metadata
- recall suppresses rows whose metadata lifecycle is `superseded`, `obsolete`, `rejected`, or `archived`; `archived` is used by the legacy hygiene migrator for old scratch rows that remain auditable but should not be recalled

## Stable V1 tool surface

The following tool names are stable for V1. `tool_schema_profile="compact"` exposes only the compact default subset in ordinary prompts, while legacy individual tools remain direct-call compatible and can be re-exposed with `tool_schema_profile="standard"` or `tool_schema_extra_tools`.

- `scope_recall_store`
- `scope_recall_store_secret_index`
- `scope_recall_search`
- `scope_recall_context`
- `scope_recall_profile`
- `scope_recall_memory`
- `scope_recall_entity`
- `scope_recall_probe`
- `scope_recall_related`
- `scope_recall_feedback`
- `scope_recall_forget`
- `scope_recall_update`
- `scope_recall_dedupe`
- `scope_recall_merge`
- `scope_recall_export`
- `scope_recall_govern`
- `scope_recall_hygiene`
- `scope_recall_repair`
- `scope_recall_stats`
- `scope_recall_inspect`
- `scope_recall_explain`
- `scope_recall_benchmark`
- `scope_recall_playbook_create`
- `scope_recall_playbook_search`
- `scope_recall_playbook_inspect`
- `scope_recall_experience_preflight`
- `scope_recall_playbook_feedback`
- `scope_recall_playbook_review`
- `scope_recall_experience_stats`
- `scope_recall_experience_promote`
- `scope_recall_forgetting_report`
- `scope_recall_forgetting_run`
- `scope_recall_fact`
- `scope_recall_evolve`
- `scope_recall_reflect`

Patch/minor releases may add fields to JSON responses. Existing documented fields should not be removed in the V1 line unless they are unsafe or clearly erroneous, in which case the changelog must call out the compatibility impact. V1 rejects ordinary `scope_recall_update` / `scope_recall_merge` attempts that would move a row between shared durable and local scratch modes; such migrations require an explicit future maintenance path.

## Stable V1 scope contract

V1 uses a two-scope model:

- shared durable scope by default: `platform + agent_workspace + agent_identity + user_id`
- optional canonical durable scope: `agent_workspace + agent_identity + canonical_user`, enabled only by explicit identity aliases
- local runtime scope: durable scope plus the raw platform/account and `gateway_session_key`, or `chat_id` / `thread_id`

Targets `user`, `memory`, `project`, and `ops` are shared durable memories. Target `general` is local scratch memory. Search/retrieval uses the deduped accessible set of current local scope plus shared durable scope, plus legacy platform shared-scope aliases for mapped identities. Global maintenance across all scopes is outside normal chat use and requires operator mode.

## Stable V1 retrieval contract

V1 supports these retrieval modes:

- `lexical`
- `vector`
- `hybrid`

The default config uses hybrid retrieval with SQLite lexical/BM25 recall, weighted reciprocal-rank fusion metadata, conservative entity-distance graph hints, and a LanceDB vector companion. Ordinary calls remain single-query; callers may opt into bounded `query_variants` evidence-set fusion, which reserves specialist query slots before global RRF fill and exposes per-query provenance. Its `evidence_diversity_depth` remains `3` by default for compatibility and accepts `1..6`; use the wider range only when a broad multi-hop or open-domain evidence set benefits more from specialist coverage than from global-RRF concentration. The compact default result count is unchanged, while an explicit `scope_recall_search.limit` is bounded at 50. Persisted relation evidence is scope-filtered before inspect/explain/rerank surfaces expose related ids. Relation-aware reranking remains opt-in through `retrieval.relation_rerank_enabled`; when enabled, `supersedes` edges boost the superseding row and penalize the superseded peer by the configured relation weights. Deterministic relation backfill is available through `scripts/backfill.graph_relations.py`; it is dry-run by default and only creates same-scope `supersedes` edges from trusted `metadata.superseded_by` provenance unless an operator explicitly relaxes the scope boundary. Operators can set `vector.backend=sqlite-bruteforce` for a native-free/non-AVX companion. V1 probes LanceDB/PyArrow native imports in a child process before importing them in the Hermes process; when `vector.fallback_backend=sqlite-bruteforce`, an absent or SIGILL-prone LanceDB stack automatically falls back to the pure SQLite companion instead of crashing the agent.

Evidence-set retrieval may batch query embeddings, but it preserves query order, the configured query/document prompt distinction, and the ordinary per-query fallback when batching is unavailable. Variant candidates stay inside the retrieval pipeline until fusion; only the bounded fused result window crosses the egress sanitizer. Direct `search_memories` calls remain sanitized by default. Entity-normalization memoization is request-local so it cannot outlive graph or memory changes.

Embedder policy:

- the configured default targets an OpenAI-compatible Gemini embedding endpoint
- if the configured API embedder is unavailable, V1 may degrade to the `local-hash` fallback
- `local-hash` is an availability fallback, not a semantic-quality promise

## Stable V1 migration contract

V1 includes three separate migration paths:

1. legacy scratch/raw hygiene migration through `scripts/migrate.legacy_hygiene.py` for older `scope-recall` SQLite stores
2. legacy local `lancepro` storage migration on first initialization when applicable
3. explicit OpenClaw `memory-lancedb-pro` import through `scripts/import.openclaw.memory_lancedb_pro.py`

Old `.lance` tables enter V1 through import/cache paths: convert OpenClaw data into SQLite truth rows, then rebuild the configured vector companion from that truth store. Old raw `general` scratch rows inside an existing SQLite truth store are metadata-archived, not deleted, and remain recoverable from the backup created by the hygiene migrator.

## V1 compatibility scope

The V1 compatibility promise is scoped to the local Hermes provider behavior described above: SQLite truth storage, configured vector companion retrieval, current-turn recall, scoped durable memory, local scratch isolation, explicit migration tools, and operator-visible diagnostics.

Compatibility with legacy OpenClaw or LanceDB-only data flows through the documented importer and migration paths. Hosted semantic quality depends on configured embedding providers; `local-hash` is an availability fallback for bootstrap and degraded offline use.

## Release gate expectations

A V1 source tree should pass:

```bash
python -m pytest -q
python scripts/check.release.py
python scripts/benchmark.graph_relations.py
python scripts/backfill.graph_relations.py --hermes-home <profile> --dry-run
python scripts/journal-digest.py --hermes-home <profile> --dry-run
python scripts/repair.vector_index.py --hermes-home <profile> --dry-run
```

The release check enforces V1 metadata, required public docs, wheel contents, test pass status, bytecode compilation, source-tree hygiene, absence of obvious literal secrets/private paths, and deterministic golden plus graph-relation benchmark success.

## Live-runtime freshness boundary

Passing V1 release gates proves the source tree and release artifact are ready. It does not prove a currently running Hermes gateway has loaded this exact code.

To claim live runtime freshness, restart or reload the Hermes process and compare the live process start time against plugin source modification times, or run an equivalent runtime smoke test against the intended service instance.

`scope_recall_hygiene` is a read-only report surface. It never performs cleanup; operators must explicitly run a separate delete/merge/dedupe action after reviewing its output.
