# Scope Recall V1 stability contract

`scope-recall` 1.4.1 keeps the V1 compatibility contract while adding the conservative Experience Kernel MVP for reviewed procedural playbooks, read-only preflight packets, scoped feedback accounting, and replay benchmarking. It preserves `scope_recall_profile`, compression-boundary journal staging through Hermes' `on_pre_compress()` memory-provider hook, the `hermes-scope-recall` standalone distribution and installer path, attachment-marker sanitization, journal ACK quality gates, native-safe LanceDB probing, and automatic SQLite vector fallback for non-AVX hosts.

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
- maintenance tools (`scope_recall_dedupe`, `scope_recall_govern`, `scope_recall_hygiene`, and `scope_recall_repair`) are hidden and fail closed unless `maintenance_tools_enabled=true`
- `scope_recall_hygiene` is read-only and never performs cleanup; operators must explicitly run a separate delete/merge/dedupe action after reviewing its output
- `scope_recall_export` is available for scoped exports by default; `scope_only=false` requires `maintenance_tools_enabled=true`
- Experience Kernel runtime prompt injection is enabled by default through `experience.prefetch_enabled=true`, but packets remain advisory scaffolds and live user instructions/current evidence override old experience; operators can set `experience.prefetch_enabled=false` as a runtime injection kill switch.
- Experience Kernel create/review and maintenance promotion tools are hidden and fail closed unless `maintenance_tools_enabled=true`; ordinary read-only search/inspect/preflight/stats and scoped feedback tools remain available when `experience.enabled=true`.
- automatic experience promotion is opt-in after successful journal digest through `experience.auto_promotion_enabled=false` by default; when enabled, it still requires evidence-backed task traces, writes task episodes, auto-promotes only low-risk verified playbooks, and keeps high-risk playbooks gated by status/review.
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

The following tool names are stable for V1:

- `scope_recall_store`
- `scope_recall_store_secret_index`
- `scope_recall_search`
- `scope_recall_context`
- `scope_recall_profile`
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

The default config uses hybrid retrieval with SQLite lexical/BM25 recall, weighted reciprocal-rank fusion metadata, and a LanceDB vector companion. Operators can set `vector.backend=sqlite-bruteforce` for a native-free/non-AVX companion. V1 probes LanceDB/PyArrow native imports in a child process before importing them in the Hermes process; when `vector.fallback_backend=sqlite-bruteforce`, an absent or SIGILL-prone LanceDB stack automatically falls back to the pure SQLite companion instead of crashing the agent.

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
python scripts/journal-digest.py --hermes-home <profile> --dry-run
python scripts/repair.vector_index.py --hermes-home <profile> --dry-run
```

The release check enforces V1 metadata, required public docs, wheel contents, test pass status, bytecode compilation, source-tree hygiene, and absence of obvious literal secrets/private paths.

## Live-runtime freshness boundary

Passing V1 release gates proves the source tree and release artifact are ready. It does not prove a currently running Hermes gateway has loaded this exact code.

To claim live runtime freshness, restart or reload the Hermes process and compare the live process start time against plugin source modification times, or run an equivalent runtime smoke test against the intended service instance.

`scope_recall_hygiene` is a read-only report surface. It never performs cleanup; operators must explicitly run a separate delete/merge/dedupe action after reviewing its output.
