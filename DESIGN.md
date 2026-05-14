# scope-recall design

## Positioning

`scope-recall` is a Hermes local memory provider focused on **current-turn recall**.

It inherits the useful policy ideas from OpenClaw `memory-lancedb-pro`:

- no stale previous-turn injection
- strong scope isolation
- conservative gating
- bounded recall budget

But its implementation is intentionally split into two clear layers:

1. **SQLite truth layer**
2. **LanceDB vector companion layer**

That split is deliberate. SQLite is the durable source of truth; LanceDB is the retrieval accelerator/semantic companion.

## Goals

1. Fix cross-turn topic bleed caused by queued previous-turn recall.
2. Keep durable local truth in a simple audit-friendly store.
3. Add semantic/hybrid retrieval without making the vector store the only source of truth.
4. Respect Hermes built-in curated memory as the source of truth for `memory` tool writes.
5. Isolate memory strongly enough for gateway multi-chat / multi-topic use.
6. Preserve an offline-capable default path for local operation and open-source onboarding.

## Non-goals for this iteration

- full long-term memory governance pipeline
- multi-tier summarization/decay/extraction orchestration
- cloud-only dependency requirement
- forcing external embedding APIs for basic functionality

## Layer split

### Layer A — built-in curated memory

Authoritative files:

- `$HERMES_HOME/memories/USER.md`
- `$HERMES_HOME/memories/MEMORY.md`

These are live-read during recall. `on_memory_write` is intentionally an observational no-op: Hermes can notify the provider, but this provider must not mirror curated writes into SQLite.

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

### Layer C — LanceDB vector companion

Stored in:

- `$HERMES_HOME/scope-recall/lancedb/`

Used for:

- semantic nearest-neighbor retrieval
- hybrid ranking with lexical candidates
- future pluggable embedder upgrades

It duplicates retrieval-ready fields from SQLite plus a `vector` column, but it is **not** the truth layer.

## Why SQLite truth + LanceDB companion

This architecture gives us:

- stable local truth independent of vector backend changes
- easier migrations and backups
- reproducible lexical fallback
- semantic search when available
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

## Embedders

### Configured default: Gemini OpenAI-compatible API

The shipped runtime config now targets a hosted embedder by default:

- provider: `openai-compatible`
- model: `gemini-embedding-001`
- dimensions: `3072`

That matches the current production-quality path used in this Hermes instance.

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

## Scope isolation

Scope priority:

1. `platform`
2. `agent_workspace`
3. `agent_identity`
4. `user_id`
5. `gateway_session_key` when Hermes provides it
6. otherwise `chat_id`
7. plus `thread_id` when present

This matters because user-only scoping is insufficient in Telegram/Discord multi-group or topic scenarios.

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

The companion LanceDB layer syncs incrementally from SQLite truth on init by comparing stable ids and `updated_at` values:

- missing vector rows are embedded and inserted
- stale vector rows absent from SQLite are deleted
- duplicate physical vector rows for the same id are collapsed to the newest matching row
- unchanged rows are left alone
- after sync, stats record physical row count, unique id count, and duplicate extra rows

If LanceDB delete/upsert fails, SQLite remains authoritative and the provider marks vector state as `needs_repair`; the truth-row write is not reported as lost.

Full rebuild is no longer the default init path. For deep maintenance or release-grade storage hygiene, run `scripts/repair.vector_index.py` to rebuild the LanceDB companion from SQLite truth with an automatic backup.

### Remaining cleanup after reviewer signoff

After final review:

- decide whether the old `lancepro` shim stays for one release or is archived immediately
- run human-triggered live gateway smoke verification under the real service path
- package the standalone repo layout for GitHub publish if it is split out of the local profile

## Tool exposure

Primary-agent only:

- `scope_recall_store`
- `scope_recall_search`
- `scope_recall_stats`

Subagents do not get tool schemas and cannot use them.

## Open-source packaging expectations

Before GitHub release, finish these:

1. complete rename from `lancepro` to `scope-recall` in runtime config and plugin directory usage
2. port all legacy regression tests
3. clean deprecation warnings and compatibility shims
4. add LICENSE and release notes
5. write migration notes from `lancepro`
6. verify systemd runtime with real gateway traffic
7. decide whether to keep old tool aliases for one release or remove them immediately

## Current status

What is already real now:

- plugin skeleton exists under `$HERMES_HOME/plugins/scope-recall`
- SQLite truth layer exists
- LanceDB companion layer exists
- hybrid retrieval path exists
- legacy local rename migration exists
- focused tests for loading / hybrid recall / curated memory / stats pass
- release docs now include migration notes, upstream differences, and an OpenClaw import script skeleton
- vector sync now repairs stale ids and duplicate physical rows during normal initialization
- stats distinguish vector physical rows, unique ids, and duplicate extra rows
- top-level package import is lightweight enough for clean wheel/venv checks without Hermes runtime modules

What still needs finishing before public release:

- decide the final fate/timeline of the deprecated `lancepro` shim
- after installing into a live Hermes profile, run human-triggered live gateway verification under the real service path before claiming that the running service has loaded the newest code
- create or push the standalone GitHub remote repository
