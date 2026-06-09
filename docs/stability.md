# Scope Recall V1 stability contract

`scope-recall` 1.0.9 is the native-free vector backend and standalone-provider polish patch release for the Hermes Scope Recall memory provider V1 line.

This document defines the stable V1 compatibility surface and the areas that may evolve in patch or minor releases.

## Stable V1 identity

The stable public provider name is:

- `scope-recall`

The legacy `lancepro` naming exists only as a transition compatibility path. New installs and documentation should use `scope-recall`.

## Stable V1 install shape

The supported Hermes install shape for V1 is an unpacked local plugin directory:

```text
$HERMES_HOME/plugins/scope-recall/
```

Wheel builds are release-artifact sanity checks and provide an importable Python package named `scope_recall`, but Hermes plugin discovery is still directory-based unless Hermes itself adds a wheel/entry-point plugin loader later.

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
- nightly digest writes are still SQLite truth rows; the digest run/source ledger is audit metadata, not a separate memory authority

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
- durable `user`/`memory`/`project`/`ops` rows are shared across windows/chats for the same platform + agent workspace + agent identity + user id
- `general` scratch rows remain local to the current chat/thread or gateway session key
- scoped tool actions operate on the current accessible scope set: local runtime scope plus shared durable scope
- `scripts/nightly-digest.py` may add or update durable rows, but it must not store raw `system` rows or raw `tool` output; task workflows are stored only as sanitized summaries with optional tool-name and verification metadata

## Stable V1 tool surface

The following tool names are stable for V1:

- `scope_recall_store`
- `scope_recall_search`
- `scope_recall_context`
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

Patch/minor releases may add fields to JSON responses. Existing documented fields should not be removed in the V1 line unless they are unsafe or clearly erroneous, in which case the changelog must call out the compatibility impact. V1 rejects ordinary `scope_recall_update` / `scope_recall_merge` attempts that would move a row between shared durable and local scratch modes; such migrations require an explicit future maintenance path.

## Stable V1 scope contract

V1 uses a two-scope model:

- shared durable scope: `platform + agent_workspace + agent_identity + user_id`
- local runtime scope: shared durable scope plus `gateway_session_key`, or `chat_id` / `thread_id`

Targets `user`, `memory`, `project`, and `ops` are shared durable memories. Target `general` is local scratch memory. Search/retrieval uses the deduped accessible set of current local scope plus shared durable scope. Global maintenance across all scopes is outside normal chat use and requires operator mode.

## Stable V1 retrieval contract

V1 supports these retrieval modes:

- `lexical`
- `vector`
- `hybrid`

The default config uses hybrid retrieval with SQLite lexical recall plus a LanceDB vector companion. Operators can set `vector.backend=sqlite-bruteforce` for a native-free/non-AVX companion.

Embedder policy:

- the configured default targets an OpenAI-compatible Gemini embedding endpoint
- if the configured API embedder is unavailable, V1 may degrade to the `local-hash` fallback
- `local-hash` is an availability fallback, not a semantic-quality promise

## Stable V1 migration contract

V1 includes two separate migration paths:

1. legacy local `lancepro` storage migration on first initialization when applicable
2. explicit OpenClaw `memory-lancedb-pro` import through `scripts/import.openclaw.memory_lancedb_pro.py`

Old `.lance` tables enter V1 through import/cache paths: convert OpenClaw data into SQLite truth rows, then rebuild the configured vector companion from that truth store.

## V1 compatibility scope

The V1 compatibility promise is scoped to the local Hermes provider behavior described above: SQLite truth storage, configured vector companion retrieval, current-turn recall, scoped durable memory, local scratch isolation, explicit migration tools, and operator-visible diagnostics.

Compatibility with legacy OpenClaw or LanceDB-only data flows through the documented importer and migration paths. Hosted semantic quality depends on configured embedding providers; `local-hash` is an availability fallback for bootstrap and degraded offline use.

## Release gate expectations

A V1 source tree should pass:

```bash
python -m pytest -q
python scripts/check.release.py
python scripts/repair.vector_index.py --hermes-home <profile> --dry-run
```

The release check enforces V1 metadata, required public docs, wheel contents, test pass status, bytecode compilation, source-tree hygiene, and absence of obvious literal secrets/private paths.

## Live-runtime freshness boundary

Passing V1 release gates proves the source tree and release artifact are ready. It does not prove a currently running Hermes gateway has loaded this exact code.

To claim live runtime freshness, restart or reload the Hermes process and compare the live process start time against plugin source modification times, or run an equivalent runtime smoke test against the intended service instance.

`scope_recall_hygiene` is a read-only report surface. It never performs cleanup; operators must explicitly run a separate delete/merge/dedupe action after reviewing its output.
