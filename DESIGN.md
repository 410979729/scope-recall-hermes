# scope-recall design

## Positioning

`scope-recall` is a Hermes local memory provider focused on **current-turn recall** plus **durable shared semantic memory**.

It inherits the useful policy ideas from OpenClaw `memory-lancedb-pro`:

- no stale previous-turn injection
- permanent shared memory for durable facts
- local scratch isolation for temporary chat/thread/session context
- conservative gating
- bounded recall budget

But its implementation is intentionally split into two clear layers:

1. **SQLite truth layer**
2. **Configured vector companion layer** (LanceDB by default, `sqlite-bruteforce` for native-free hosts)

That split is deliberate. SQLite is the durable source of truth; the configured vector backend is only the retrieval accelerator/semantic companion.

## Goals

1. Fix cross-turn topic bleed caused by queued previous-turn recall.
2. Keep durable local truth in a simple audit-friendly store.
3. Add semantic/hybrid retrieval without making the vector store the only source of truth.
4. Respect Hermes built-in curated memory as the source of truth for `memory` tool writes.
5. Share durable `user`/`memory`/`project`/`ops` facts across windows/chats for the same user + agent identity.
6. Isolate `general` scratch captures strongly enough for gateway multi-chat / multi-topic use.
7. Preserve an offline-capable default path for local operation and open-source onboarding.

## V1 scope

V1 focuses on the local recall layer that every Hermes runtime can inspect and operate directly:

- current-turn recall over the active query
- SQLite truth storage for provider-owned memory records
- a configured vector backend as a rebuildable semantic companion
- durable scoped recall for `user`/`memory`/`project`/`ops` facts
- local isolation for `general` scratch captures
- conservative governance primitives such as dedupe, trust metadata, decay review, relations, and explicit operator tools
- offline-capable bootstrap through deterministic local embeddings
- bridge-friendly export/import surfaces for deployments with a central shared backend

Procedural knowledge packaging stays with Hermes native skills. Deployment-wide authority, tenancy, and synchronization policies stay with the external systems that already own those concerns. Operational dashboards can be built on top of the CLI/JSON health outputs when a deployment needs them.

## Layer split

### Layer A — built-in curated memory

Authoritative files:

- `$HERMES_HOME/memories/USER.md`
- `$HERMES_HOME/memories/MEMORY.md`

These are live-read during recall when curated-memory policy allows it. `on_memory_write` is intentionally an observational no-op: Hermes can notify the provider, but this provider must not mirror curated writes into SQLite.

Default policy is conservative for gateways: global curated files are read in single-user/no-`user_id` contexts, but explicit `user_id` contexts must opt in via `curated_memory.mode: profile-global` or `curated_memory.mode: explicit-users` plus `allowed_user_ids`.

Reason:

If provider code mirrors built-in `memory` writes into a second database, replace/remove operations drift and stale entries survive. Live-reading the curated files keeps provider recall aligned with Hermes native memory behavior.

### Layer B — SQLite truth layer

Stored in:

- `$HERMES_HOME/scope-recall/memory.sqlite3`

Used for:

- turn captures
- provider tool writes
- lexical lookup
- runtime audit trail
- future migration source of truth

SQLite schema includes:

- `scope_id`
- `platform`
- `user_id`
- `chat_id`
- `thread_id`
- `gateway_session_key`
- `agent_identity`
- `agent_workspace`
- `session_id`
- `source`
- `target`
- `content`
- `summary`
- timestamps

An FTS5 side table provides fast lexical retrieval.

### Layer C — vector companion

Stored in one configured companion path:

- `$HERMES_HOME/scope-recall/lancedb/` when `vector.backend=lancedb`
- `$HERMES_HOME/scope-recall/vector.sqlite3` when `vector.backend=sqlite-bruteforce`

Used for:

- semantic nearest-neighbor retrieval
- hybrid ranking with lexical candidates
- future pluggable embedder upgrades

It duplicates retrieval-ready fields from SQLite plus a `vector` column, but it is **not** the truth layer.

### Layer D — SQLite graph and feedback indexes

Stored in the same SQLite truth database:

- `memory_entities`
- `memory_feedback`

Used for:

- deterministic entity probe and related-entity lookup
- compact task context construction
- local trust feedback that adjusts future ranking without rewriting memory text

This borrows the useful graph/trust ideas from Hermes memory providers such as Hindsight, Holographic, Honcho, RetainDB, and Supermemory while keeping the implementation local and auditable. Entity rows and feedback rows are indexes over SQLite truth rows, not a separate authority.

## Why SQLite truth + rebuildable vector companion

This architecture gives us:

- stable local truth independent of vector backend changes
- easier migrations and backups
- reproducible lexical fallback
- semantic search when available through LanceDB or a native-free SQLite fallback
- a cleaner open-source story than a provider whose name and reality drift apart

## Retrieval model

### Current-turn only

- `prefetch(query)` retrieves against the *current* user query
- `queue_prefetch()` is intentionally a no-op

This is the core anti-topic-bleed decision.

### Conservative gating

Skip recall when query is:

- empty
- too short
- greeting/noise/trivial text

### Hybrid ranking

Current config supports:

- `lexical`
- `vector`
- `hybrid`

Default is `hybrid`.

Important rule:

- if only lexical is available, use lexical score directly
- if only vector is available, use vector score directly
- only blend when both sides exist

That prevents good curated lexical hits from being suppressed merely because there is no vector twin.

SQLite FTS5 candidate discovery uses `bm25(memories_fts)` before recency tie-breaking. Final ranking still uses the provider's lexical/vector/freshness/entity/trust scoring, but BM25 prevents old high-relevance lexical rows from being cut out of the candidate pool by newer weak matches.

## Embedders

### Configured default: Gemini OpenAI-compatible API

The shipped runtime config now targets a hosted embedder by default:

- provider: `openai-compatible`
- model: `gemini-embedding-001`
- dimensions: `3072`

That is the recommended high-quality hosted path for deployments that provide credentials.

### Runtime fallback: local-hash

When the configured API embedder is unavailable, runtime falls back to the offline deterministic `local-hash` embedder.

That fallback is *not* a true semantic embedding model, but it gives us:

- no hard dependency on external APIs for basic bootstrap
- deterministic tests when we explicitly select it
- workable paraphrase tolerance for practical operations language
- a safe degraded path when credentials are absent

### Local model path: sentence-transformers

When you want a real local embedding model instead of a hosted API, set `vector.embedder.provider` to `sentence-transformers`.

The provider aliases `local-model`, `local-embedding`, and `huggingface` also resolve to the same backend.

Typical local model choices include:

- `sentence-transformers/all-MiniLM-L6-v2`
- `sentence-transformers/all-mpnet-base-v2`

This keeps the retrieval pipeline unchanged while swapping only the embedder implementation.

## Scope model: durable shared memory + local scratch

`scope-recall` deliberately avoids both extremes: it does not make every row global, and it does not lock every useful fact into one chat/window.

### Shared durable scope

Shared durable scope is built from:

1. `platform`
2. `agent_workspace`
3. `agent_identity`
4. `user_id`

Rows with targets `user`, `memory`, `project`, and `ops` are stored in this shared scope. They can be recalled across chat/thread/session boundaries for the same user and agent identity when the current query is relevant.

### Local runtime scope

Local runtime scope starts with the shared durable scope and adds:

1. `gateway_session_key` when Hermes provides it; otherwise
2. `chat_id`
3. `thread_id` when present

Rows with target `general` stay in this local scope. Raw turn captures and temporary scratch notes therefore do not leak from one group/topic/session into another.

### Accessible scope set

At runtime, normal recall and scoped tool actions use the deduped set:

```text
[current local runtime scope, shared durable scope]
```

This lets one user recall durable memory from another window while still preventing cross-user, cross-agent, and cross-local-scratch access. ID-based update/merge/delete paths must use this accessible set, never raw global ids alone. Ordinary update/merge paths also reject shared/local mode changes so a durable row cannot accidentally become globally visible `general` scratch, and a local scratch merge cannot swallow a shared durable memory.

## Migration plan

### Implemented now

On first init:

- if legacy `$HERMES_HOME/lancepro/memory.sqlite3` exists and new DB is absent, copy it into `$HERMES_HOME/scope-recall/memory.sqlite3`
- if legacy config exists and new config is absent, copy it forward
- expose migration status in stats

### OpenClaw historical imports

OpenClaw `memory-lancedb-pro` history is **not** auto-attached.

Instead, release docs and a one-shot importer now define the supported path:

- `docs/migration.md`
- `docs/differences-from-memory-lancedb-pro.md`
- `scripts/import.openclaw.memory_lancedb_pro.py`

The importer now uses a stable source fingerprint + import ledger so rerunning the same source is idempotent instead of duplicating rows.

This keeps the boundary explicit: old OpenClaw LanceDB stores must be transformed into `scope-recall` SQLite truth rows before the companion index is rebuilt.

### Vector companion sync and repair

The configured vector companion layer syncs incrementally from SQLite truth on init by comparing stable ids and `updated_at` values:

- missing vector rows are embedded and inserted
- stale vector rows absent from SQLite are deleted
- duplicate physical vector rows for the same id are collapsed to the newest matching row
- unchanged rows are left alone
- after sync, stats record physical row count, unique id count, and duplicate extra rows

If vector companion delete/upsert fails, SQLite remains authoritative and the provider marks vector state as `needs_repair`; the truth-row write is not reported as lost.

Full rebuild is no longer the default init path. For deep maintenance or release-grade storage hygiene, run `scripts/repair.vector_index.py` to rebuild the configured vector companion from SQLite truth with an automatic backup.

### Nightly digest consolidation

`scripts/nightly-digest.py` is the plugin-owned batch path for daily consolidation. It reads the active Hermes `state.db` first and falls back to legacy `lcm.db`, groups user/assistant/tool metadata by session, and scans the selected local day by timestamps instead of trusting only session-id date prefixes.

The digest does not store raw `system` rows or raw `tool` output. Task-like sessions keep only sanitized tool-chain evidence: tool names, safe command hints, verification hints, and a compact workflow summary. Candidate memories are checked against existing accessible durable rows before writing:

- exact or strongly covered candidate: skip
- semantically related but less complete existing row: update/merge
- new durable fact/workflow/summary: insert
- exact duplicate groups found after the run: delete duplicates through the same scoped delete path

Actual writes use SQLite truth, FTS/entity sync, digest run/source ledger tables, and configured vector upsert when the vector companion is enabled. Dry-run mode plans the same decisions without writing provider memory.

### Operational follow-up outside source readiness

Source-tree readiness and public documentation are separate from live deployment. Operators still need to:

- decide when to remove the deprecated `lancepro` shim in a later, explicitly announced release
- restart or reload the target Hermes service after installing a new checkout
- run a live runtime smoke test under the intended service profile before claiming gateway freshness

## Tool exposure

Primary-agent default tools:

- `scope_recall_store`
- `scope_recall_search`
- `scope_recall_context`
- `scope_recall_probe`
- `scope_recall_related`
- `scope_recall_feedback`
- `scope_recall_forget`
- `scope_recall_update`
- `scope_recall_merge`
- `scope_recall_export` with `scope_only=true`
- `scope_recall_stats`

Operator-only maintenance tools require `maintenance_tools_enabled=true` and fail closed otherwise:

- `scope_recall_dedupe`
- `scope_recall_govern`
- `scope_recall_hygiene`
- `scope_recall_repair`
- `scope_recall_export(scope_only=false)`

Subagents do not get tool schemas and cannot use them.

## Open-source packaging expectations

For V1 release/publish, keep these gates green:

1. package and plugin metadata stay on `1.0.5` until the next patch release
2. public maturity wording remains beta / release-candidate until broader field testing justifies a production-stable classifier
3. README, DESIGN, CHANGELOG, stability contract, migration docs, and upstream-difference docs stay in sync
4. local release gate passes with `python scripts/check.release.py`
5. live gateway freshness is verified separately after installing or restarting a real Hermes service
6. the deprecated `lancepro` shim remains covered by tests until its removal is explicitly announced in a later release

## Current status

What is already real now:

- plugin source is packaged as an unpacked Hermes plugin under `$HERMES_HOME/plugins/scope-recall`
- SQLite truth layer exists
- configured vector companion layer exists
- hybrid retrieval path exists
- legacy local rename migration exists
- focused tests for loading / hybrid recall / curated memory / stats pass
- release docs include migration notes, upstream differences, a V1 stability contract, and an OpenClaw import script
- vector sync repairs stale ids and duplicate physical rows during normal initialization
- stats distinguish vector physical rows, unique ids, and duplicate extra rows
- SQLite graph and feedback indexes exist for entity probe/related lookup, compact context rendering, and trust feedback
- top-level package import is lightweight enough for clean wheel/venv checks without Hermes runtime modules
- CI runs the full `scripts/check.release.py` gate

What remains outside source-tree readiness:

- live Hermes gateway freshness still requires a restart/reload plus runtime smoke verification after deployment
- publishing a new release requires pushing a clean commit, waiting for remote CI, and creating or updating the appropriate next version tag; tag pushes and manual release workflow dispatches create the GitHub Release from the matching changelog entry
