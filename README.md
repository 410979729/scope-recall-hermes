# scope-recall

<div align="center">

**Hermes current-turn memory provider with permanent semantic recall, SQLite truth storage, and a LanceDB vector companion**

*Give Hermes durable memory that can follow the same user across windows/chats while keeping local scratch context from bleeding into the wrong place.*

Current-turn recall · Permanent shared memory · Local scratch scopes · SQLite truth · LanceDB companion · Hybrid retrieval

[![CI](https://github.com/410979729/scope-recall/actions/workflows/ci.yml/badge.svg)](https://github.com/410979729/scope-recall/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Hermes Plugin](https://img.shields.io/badge/Hermes-Memory%20Provider-blue)](https://hermes-agent.nousresearch.com/docs)
[![Python](https://img.shields.io/badge/Python-3.11%2B-blue)](pyproject.toml)
[![Storage](https://img.shields.io/badge/Storage-SQLite%20%2B%20LanceDB-orange)](DESIGN.md)

</div>

`scope-recall` is a Hermes local memory provider built for **current-turn recall** and **permanent semantic memory**. Durable user/project/ops/memory facts are shared across windows/chats for the same user + agent identity; raw general turn captures stay local to the current chat/thread/session.

Version `1.0.1` is the first stable V1 release line for the documented interfaces, packaged as a public release candidate for broader field testing. The V1 compatibility contract is documented in [`docs/stability.md`](docs/stability.md).

It uses a **two-layer design**:

- **SQLite truth store** for durable local records and deterministic auditing
- **LanceDB vector companion** for semantic retrieval and hybrid ranking

This replaces the old `lancepro` naming, which was misleading because the earlier implementation was SQLite-only.

---

## Why scope-recall?

Most agent memory pain is not just "wrong memory was recalled". The bigger user-facing failure is often "the agent forgot everything when I opened a new window." `scope-recall` therefore separates **durable facts** from **local scratch context**:

- user preferences, project facts, ops notes, and explicitly stored memories follow the same user + agent identity across chats/windows
- raw/general turn captures remain local to the current chat/thread/session so one group's temporary chatter does not contaminate another group
- current-turn recall searches only for memories relevant to the active query, avoiding stale previous-turn injection
- the SQLite truth store remains auditable, and LanceDB is only a rebuildable semantic companion

`scope-recall` is built around a simple rule:

> Recall the relevant durable memory for the **current query**, while keeping local scratch context inside the **current runtime scope**.

### Without scoped durable recall

> **You:** "For this memory-provider project, SQLite is the source of truth."
>
> *(later, in another window/chat)*
>
> **Agent:** "I don't have that context here." ❌

### With scope-recall

> **You:** "What did we decide for this Hermes memory provider?"
>
> **Agent:** recalls the durable project memory from SQLite truth/LanceDB companion and answers from the relevant context. ✅

### Without local scratch boundaries

> **Group A:** "Temporary note: restart this group's test bot only."
>
> *(later, in Group B)*
>
> **Agent:** applies Group A's temporary note in Group B. ❌

`scope-recall` keeps that temporary `general` scratch row local while still sharing durable `user`/`memory`/`project`/`ops` facts.

### What you get

| Area | What `scope-recall` V1 provides |
| --- | --- |
| Current-turn recall | `prefetch(query)` retrieves against the active user query; `queue_prefetch()` is intentionally a no-op |
| Storage authority | SQLite is the durable truth; LanceDB is rebuildable companion state |
| Hybrid retrieval | SQLite lexical/FTS candidates + LanceDB semantic candidates + bounded prompt rendering |
| Memory scope model | shared durable scope for user/project/ops/memory facts; local scope for general scratch captures |
| Built-in memory integration | Hermes curated `USER.md` / `MEMORY.md` are live-read, not mirrored into SQLite |
| Governance | deterministic exact dedupe, conservative near-duplicate merge, filtering, metadata, decay review |
| Migration | local `lancepro` auto-migration; OpenClaw `memory-lancedb-pro` import is explicit |
| Offline bootstrap | deterministic `local-hash` fallback when hosted embeddings are unavailable |

---

## Quick start

### Option A: Clone into a Hermes plugin directory

```bash
cd "$HERMES_HOME/plugins"
git clone https://github.com/410979729/scope-recall.git scope-recall
cd scope-recall
python -m pip install -e .
```

Then configure Hermes to use the provider name:

```yaml
memory:
  provider: scope-recall
```

For a local smoke check after installation:

```bash
hermes memory status
```

### Option B: Manual download / unpacked plugin install

Current Hermes plugin discovery expects an **unpacked plugin directory**. `scope-recall` V1 targets the current Hermes runtime line, which requires Python 3.11 or newer. If you download a release archive instead of cloning:

1. unpack it as `$HERMES_HOME/plugins/scope-recall/`
2. run `python -m pip install -e "$HERMES_HOME/plugins/scope-recall"`
3. set `memory.provider: scope-recall`
4. restart/reload the Hermes process that should use the provider
5. verify with `hermes memory status`

Important boundary:

- the wheel build is verified for packaging sanity and importability
- the primary Hermes install shape for V1 is still an unpacked local plugin directory
- do not read wheel build success as proof that Hermes can discover or install this plugin directly from a wheel alone

---

## Configuration

The shipped `config.json` defaults to hybrid retrieval with a hosted OpenAI-compatible Gemini embedding path and a deterministic offline fallback.

Minimal default shape:

```json
{
  "auto_recall": true,
  "auto_capture": true,
  "enable_tools": true,
  "maintenance_tools_enabled": false,
  "retrieval": {
    "mode": "hybrid",
    "lexical_weight": 0.45,
    "vector_weight": 0.55,
    "candidate_pool": 12
  },
  "vector": {
    "enabled": true,
    "backend": "lancedb",
    "sync_mode": "incremental",
    "embedder": {
      "provider": "openai-compatible",
      "model": "gemini-embedding-001",
      "dimensions": 3072,
      "api_key_env": ["OPENAI_API_KEY", "GOOGLE_API_KEY"],
      "base_url": "https://generativelanguage.googleapis.com/v1beta/openai"
    },
    "fallback_embedder": {
      "provider": "local-hash",
      "dimensions": 256,
      "model": "hash-v1"
    }
  }
}
```

Credential rule:

- put real API keys in your private environment, not in `config.json`
- if no configured key is available, `scope-recall` falls back to `local-hash`

### Embedding providers

Currently implemented:

| Provider | Use case | Notes |
| --- | --- | --- |
| `openai-compatible` | Gemini/OpenAI-compatible embedding APIs | Default configured path; supports env-based API key lookup |
| `openai` | Direct OpenAI embeddings | Useful when you do not need a custom compatible endpoint |
| `sentence-transformers` | Local Hugging Face / SentenceTransformers models | Good for local semantic embeddings when installed |
| `local-hash` | Offline fallback | Deterministic degraded fallback, not a true semantic model |
| `local-debug` | Tests/debugging | Tiny deterministic test embedder |

Provider aliases `local-model`, `local-embedding`, and `huggingface` resolve to the `sentence-transformers` backend.

---

## Durable memory vs local scratch scope

`scope-recall` does **not** split all memory by every group or tiny window. It uses two provider-owned scopes:

- **Shared durable scope**: `platform + agent_workspace + agent_identity + user_id`. Rows with targets `user`, `memory`, `project`, and `ops` are stored here, so they can be recalled across chats/windows for the same user and agent.
- **Local runtime scope**: shared durable scope plus `gateway_session_key`, or `chat_id` / `thread_id`. Rows with target `general` stay here, so temporary group/topic/session chatter does not bleed elsewhere.
- **Accessible scope set**: normal recall and scoped tool actions can see the current local scope plus the shared durable scope; they cannot see another user, sibling agent identity, or another local chat/thread/session scratch scope.

This aims at the common expectation: "if I gave the agent durable information before, it should remember it later," without making every scratch line globally visible forever.

---

## Dual-memory architecture: important

When `scope-recall` is active, Hermes memory has **two intentional authority zones**:

| Layer | Storage | Purpose | How recall sees it |
| --- | --- | --- | --- |
| Hermes curated memory | `$HERMES_HOME/memories/USER.md`, `$HERMES_HOME/memories/MEMORY.md` | User profile and durable hand-curated notes managed by Hermes built-in memory | Live-read during recall; not mirrored into SQLite |
| Scope Recall provider memory | `$HERMES_HOME/scope-recall/memory.sqlite3` + `$HERMES_HOME/scope-recall/lancedb/` | Provider-owned shared durable memories plus local scratch captures, scope metadata, lexical/vector retrieval | SQLite truth + optional LanceDB companion ranking |

Key principle:

> SQLite is the truth source for provider-owned rows. Hermes curated memory files remain their own truth source. LanceDB is a rebuildable retrieval companion, not the authority.

This is deliberate. Mirroring curated memory writes into SQLite can leave stale duplicates after replace/remove operations. Live-reading curated memory keeps Scope Recall aligned with Hermes native memory behavior.

---

## Storage layout

Under the active Hermes profile:

- `$HERMES_HOME/scope-recall/memory.sqlite3`
- `$HERMES_HOME/scope-recall/config.json`
- `$HERMES_HOME/scope-recall/lancedb/`

Legacy `lancepro` storage is migrated forward on first initialization when present.

---

## Architecture

```text
Hermes turn
   |
   | current query
   v
prefetch(query)
   |
   +--> live curated memory read
   |       - $HERMES_HOME/memories/USER.md
   |       - $HERMES_HOME/memories/MEMORY.md
   |
   +--> SQLite truth lookup / FTS
   |       - provider-owned memory rows
   |       - scope metadata
   |       - timestamps and governance metadata
   |
   +--> LanceDB vector companion
   |       - semantic candidate retrieval
   |       - rebuildable from SQLite truth
   |
   v
hybrid scoring + recency-aware ranking + bounded prompt block
```

<details>
<summary><strong>File reference</strong></summary>

| File | Purpose |
| --- | --- |
| `__init__.py` | Hermes plugin entrypoint; exposes `register()` lazily |
| `provider.py` | Provider lifecycle and Hermes hook integration |
| `config.py` | Runtime config loading/defaults |
| `scope.py` | Runtime scope construction and isolation keys |
| `sql_store.py` | SQLite schema, migrations, truth-row CRUD, FTS |
| `vector_store.py` | LanceDB companion table sync/search/repair primitives |
| `vector_runtime.py` | Vector runtime status and degradation handling |
| `recall.py` | Lexical/vector/hybrid recall orchestration |
| `scoring.py` | Score fusion, freshness boosts, capping logic |
| `gating.py` | Recall/capture gating and noise filtering |
| `capture.py` | Auto-capture pipeline |
| `governance.py` | Deterministic dedupe, metadata, decay/governance review |
| `memory_ops.py` | Store/search/forget/update/dedupe/merge/export/govern operations |
| `tooling.py` | Provider tool dispatch |
| `schemas.py` | Hermes tool schemas |
| `migration.py` | Legacy `lancepro` migration helpers |
| `scripts/import.openclaw.memory_lancedb_pro.py` | Explicit OpenClaw history importer |
| `scripts/repair.vector_index.py` | Rebuild/repair LanceDB from SQLite truth |
| `scripts/check.release.py` | Full V1 release gate used locally and by CI |

</details>

### 1. SQLite truth layer

SQLite is the authoritative provider-owned store.

It keeps:

- raw memory rows
- scope metadata
- lexical FTS index
- timestamps for auditing and migration

Why SQLite stays authoritative:

- deterministic local persistence
- easy schema inspection
- simple migration/backup story
- safer open-source baseline than tying truth directly to a vector backend

### 2. LanceDB vector companion

LanceDB is a **companion retrieval index**, not the truth source.

It stores retrieval-ready fields copied from SQLite plus a vector column:

- `id`
- `scope_id`
- `source`
- `target`
- `content`
- `summary`
- `updated_at`
- `vector`

Configured default embedder targets the Gemini OpenAI-compatible embeddings API:

- `provider: openai-compatible`
- `model: gemini-embedding-001`
- `dimensions: 3072`

Runtime fallback remains available:

- if the configured API embedder is unavailable, the plugin falls back to `local-hash` (`256` dims)
- this keeps first-boot/local operation working even without external API keys, while preserving a higher-quality default config for instances that do provide credentials

---

## Core features

### Current-turn recall

- `prefetch(query)` retrieves against the **current** user query
- `queue_prefetch()` is intentionally a no-op
- this avoids stale next-turn injection from the previous topic

### Permanent shared recall

- `user`, `memory`, `project`, and `ops` rows are durable shared memories for the same user + agent identity
- they can be recalled from another chat/window when the new query is semantically relevant
- `general` rows remain local scratch context for the current chat/thread/session
- ID-based updates/deletes/merges are restricted to the current accessible scope set, not global row ids

### Hybrid retrieval

```text
current query
   ├─> SQLite lexical / FTS candidates
   └─> LanceDB vector candidates
        ↓
score fusion + freshness hints + prompt budget
```

Supported retrieval modes:

- `lexical`
- `vector`
- `hybrid` *(default)*

Default hybrid weights:

- lexical: `0.45`
- vector: `0.55`

Guardrail: if only one side has a score, that side is used directly instead of being unfairly damped by a missing partner score.

### Scope isolation

Scope is built from:

- `platform`
- `agent_workspace`
- `agent_identity`
- `user_id`
- `gateway_session_key` when available
- otherwise `chat_id`
- plus `thread_id` when present

This prevents the same user from leaking memories across different groups, chats, or topics.

### Vector repair and stats

SQLite is the cardinality authority. During vector sync, the provider compares SQLite ids with LanceDB ids, deletes stale vector rows, collapses duplicate physical rows by id, and embeds missing/changed rows. If LanceDB delete/upsert fails, the SQLite write is preserved and vector state becomes `needs_repair` instead of surfacing the truth-row write as failed.

`scope_recall_stats` reports:

- `vector.row_count` — physical LanceDB row count
- `vector.unique_id_count` — distinct vector ids
- `vector.duplicate_row_count` — extra physical rows beyond one row per id
- `vector.status` — `ready`, `degraded`, `needs_repair`, `disabled`, or `error`

A healthy synced companion should have `total_memories == vector.unique_id_count == vector.row_count` and `vector.duplicate_row_count == 0` for provider-owned rows.

For deeper maintenance:

```bash
python scripts/repair.vector_index.py --hermes-home "$HERMES_HOME" --dry-run
python scripts/repair.vector_index.py --hermes-home "$HERMES_HOME"
```

### Write-time governance

Provider-owned captures apply a deterministic first line of governance before SQLite writes:

- exact normalized-content dedupe within `(scope_id, target)`
- conservative semantic near-duplicate merge for `user`, `ops`, and `project` memories
- conflict preservation when a near-duplicate contains negation / supersession language
- rules-based smart extraction from user turns into preference / ops / project fact candidates
- metadata classification for category, tier, confidence, sensitivity, and expiry review
- noisy maintenance/system prompt filtering
- trivial reply filtering
- obvious secret-bearing text filtering
- overlong prompt-block filtering through `capture_hard_max_chars`
- governance review through `scope_recall_govern`, including core/working/archive tier counts and decay candidates

This is a local deterministic governance layer, not a remote LLM extraction pipeline. It intentionally stays conservative so SQLite remains auditable truth and conflicting memories are preserved rather than silently overwritten.

---

## Provider tools

Primary-agent default tools:

```text
scope_recall_store
scope_recall_search
scope_recall_forget
scope_recall_update
scope_recall_merge
scope_recall_export
scope_recall_stats
```

Operator-only maintenance tools are hidden from the default schema and require `maintenance_tools_enabled=true`:

```text
scope_recall_dedupe
scope_recall_govern
scope_recall_repair
```

`scope_recall_export` defaults to the current accessible scope set: local scratch scope plus shared durable scope. Passing `scope_only=false` is an operator maintenance action and fails closed unless `maintenance_tools_enabled=true`.

Backward-compatible aliases are still accepted internally for old `lancepro_*` tool names during transition.

### Tool quick reference

Example primary-agent tool calls:

```python
# Store provider-owned memory. ops/user/memory/project become shared durable rows; general stays local scratch.
store = scope_recall_store(
    content="This project deploys with uv run app.",
    target="ops",
)

# Search the current accessible scope set: local scratch plus shared durable memory.
results = scope_recall_search(
    query="How does this project deploy?",
    limit=3,
)

# Inspect truth/vector health.
stats = scope_recall_stats()
```

Example `scope_recall_stats` shape:

```json
{
  "provider": "scope-recall",
  "total_memories": 42,
  "scope_memories": 7,
  "local_scope_memories": 3,
  "shared_scope_memories": 4,
  "vector": {
    "enabled": true,
    "ready": true,
    "status": "ready",
    "row_count": 42,
    "unique_id_count": 42,
    "duplicate_row_count": 0
  }
}
```

| Tool | Purpose |
| --- | --- |
| `scope_recall_store` | Store a provider-owned memory row after deterministic governance checks |
| `scope_recall_search` | Search the current local scratch scope plus shared durable scope with lexical/vector/hybrid retrieval |
| `scope_recall_forget` | Delete memories matching a query within the current accessible scope set |
| `scope_recall_update` | Replace content/category within the current accessible scope set; shared/local target-mode changes are rejected |
| `scope_recall_dedupe` | Operator-only: inspect or collapse exact duplicate rows |
| `scope_recall_merge` | Merge same-scope memories into a target row; shared/local mixing is rejected |
| `scope_recall_export` | Export SQLite truth rows as JSON or JSONL; defaults to current accessible scope set |
| `scope_recall_govern` | Operator-only: review tier distribution and decay/archive candidates |
| `scope_recall_repair` | Operator-only: repair/rebuild the LanceDB companion from SQLite truth |
| `scope_recall_stats` | Inspect storage, retrieval, scope, and vector health |

---

## Migration behavior

### Local `lancepro` rename migration

On first boot, if `$HERMES_HOME/lancepro/` exists and `$HERMES_HOME/scope-recall/` does not yet contain the new DB/config, the provider:

- copies the legacy SQLite database into the new location
- copies `config.json` forward
- records migration info in `scope_recall_stats`

### OpenClaw `memory-lancedb-pro` imports

OpenClaw `memory-lancedb-pro` history is handled separately as an explicit import problem, not automatic compatibility.

See:

- [`docs/migration.md`](docs/migration.md)
- [`docs/differences-from-memory-lancedb-pro.md`](docs/differences-from-memory-lancedb-pro.md)
- [`scripts/import.openclaw.memory_lancedb_pro.py`](scripts/import.openclaw.memory_lancedb_pro.py)

Do **not** point `scope-recall` directly at an OpenClaw `.lance` directory and call it done. Old vector stores must be transformed into SQLite truth rows before the companion vector index is rebuilt.

---

## Compared with OpenClaw memory-lancedb-pro

`scope-recall` was inspired by good public ideas in OpenClaw `memory-lancedb-pro`, especially current-turn recall, scoped memory boundaries, hybrid retrieval, and memory hygiene. It is not a line-for-line port and it does not claim feature parity.

| Area | OpenClaw `memory-lancedb-pro` | `scope-recall` V1 |
| --- | --- | --- |
| Host agent | OpenClaw | Hermes |
| Truth model | LanceDB-centric OpenClaw memory pipeline | SQLite truth + LanceDB companion index |
| Recall timing | OpenClaw auto-recall hook model | Hermes `prefetch(query)` current-turn recall; no queued next-turn recall |
| Curated memory | Separate OpenClaw markdown/journal behavior | Hermes `USER.md` / `MEMORY.md` live-read and kept authoritative |
| Smart extraction | LLM-backed created/merged/skipped style in upstream beta line | deterministic/rules-based extraction and conservative merge |
| Lifecycle | Weibull decay / tier promotion concepts upstream | deterministic metadata classification and decay/governance review; no full LLM summarization tier pipeline |
| Migration | OpenClaw-native data path | explicit importer from OpenClaw LanceDB shape into SQLite truth |

Honest claim boundary:

> `scope-recall` is a Hermes local memory provider for current-turn recall with SQLite truth storage, LanceDB vector companion retrieval, strong runtime scope isolation, deterministic write-time governance, and explicit migration boundaries.

It should **not** be described as:

- a drop-in replacement for OpenClaw `memory-lancedb-pro`
- a direct reuse wrapper around old `.lance` stores
- full feature parity with upstream LLM-backed governance and lifecycle orchestration

---

## Troubleshooting

### Recall returns stale or irrelevant context

Check that the running provider is `scope-recall`, not the deprecated `lancepro` name, and remember that live Hermes runtime freshness requires a process restart/reload after code changes.

```bash
hermes memory status
```

### Vector stats show duplicate rows or missing rows

Run the repair script. SQLite remains truth; the vector layer is rebuildable companion state.

```bash
python scripts/repair.vector_index.py --hermes-home "$HERMES_HOME" --dry-run
python scripts/repair.vector_index.py --hermes-home "$HERMES_HOME"
```

### Hosted embeddings are unavailable

The provider should degrade to `local-hash`. That keeps the system usable but lowers semantic quality. Set `GOOGLE_API_KEY` or `OPENAI_API_KEY` in your private environment to use the configured hosted path.

### OpenClaw `.lance` data does not appear automatically

That is expected. OpenClaw history must be explicitly imported into SQLite truth rows before the companion vector index is rebuilt.

### Live gateway still behaves like the old code

Release checks prove the source tree and artifact. They do not prove a running Hermes gateway has loaded the new plugin. Restart/reload the target Hermes process and verify with a real runtime smoke test before claiming live-runtime freshness.

---

## Current V1 limitations

- vector sync is incremental by stable row id / `updated_at`, with duplicate-id/stale-row repair during normal sync; `scripts/repair.vector_index.py` can rebuild the LanceDB companion from SQLite truth when deeper storage hygiene is needed
- semantic merge is intentionally conservative and rules/scoring-based; it is not a general-purpose contradiction resolver or LLM reasoning layer
- smart extraction is rules-based for common preference / ops / project-fact sentences; it is not full OpenClaw-style LLM created/merged/skipped extraction parity
- fallback `local-hash` is only a degraded offline path, not a true semantic model
- old `lancepro` directory still exists as a compatibility shim during the V1 transition window
- the supported Hermes install shape is still an unpacked plugin directory; the wheel is verified as a package artifact, not as a Hermes discovery mechanism

See [`docs/stability.md`](docs/stability.md) for the exact V1 compatibility and non-goal boundaries.

---

## Documentation

| Document | Description |
| --- | --- |
| [`DESIGN.md`](DESIGN.md) | Architecture, layer split, retrieval model, migration plan, and release expectations |
| [`docs/stability.md`](docs/stability.md) | Stable V1 compatibility contract and non-goals |
| [`docs/migration.md`](docs/migration.md) | Local `lancepro` migration and explicit OpenClaw import guidance |
| [`docs/differences-from-memory-lancedb-pro.md`](docs/differences-from-memory-lancedb-pro.md) | Honest comparison with OpenClaw `memory-lancedb-pro` |
| [`CHANGELOG.md`](CHANGELOG.md) | Release history |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Contribution and development notes |

---

## Release and verification

The public release gate is intentionally the same script used by GitHub Actions:

```bash
python -m pytest -q
python scripts/check.release.py
python scripts/repair.vector_index.py --hermes-home "$HERMES_HOME" --dry-run
```

`scripts/check.release.py` verifies:

- V1 metadata and stable public docs
- required source files
- full pytest suite
- bytecode compilation
- wheel build
- wheel content inspection
- temp install/import smoke
- obvious literal secret/private-path scan
- generated artifact cleanup

Current focused regression coverage includes:

- plugin loading from `$HERMES_HOME/plugins`
- hybrid recall returning semantically matched content
- built-in curated memory reflection
- vector state visible in stats
- runtime fallback from unavailable API embeddings to `local-hash`
- vector table rebuild when embedder dimensions change
- vector duplicate physical rows are repaired back to one row per id
- vector delete/upsert failure preserves SQLite truth and marks vector status `needs_repair`
- vector search failure degrades to lexical recall and marks vector status `needs_repair`
- write-time exact dedupe prevents repeat SQLite rows for the same normalized content in the same scope/target
- capture filtering blocks known maintenance prompts, trivial replies, obvious secret-bearing text, and overlong prompt blocks
- semantic near-duplicate merge and conflict preservation
- rules-based smart extraction from user turns into preference / ops / project fact memories
- merge / export / govern provider tools
- governance metadata classification and decay review candidates
- provider tools cover store/search/forget/update/dedupe/merge/export/govern/repair/stats
- explicit vector companion rebuild from SQLite truth via `scripts/repair.vector_index.py`
- `scope_recall_stats` exposes physical rows, unique ids, and duplicate-row count
- top-level `import scope_recall` stays light without Hermes runtime modules
- `on_memory_write` remains an intentional observational no-op

---

## Dependencies

| Package | Purpose |
| --- | --- |
| `lancedb>=0.30.2` | LanceDB companion vector index |
| `pyarrow>=24,<25` | Arrow data interchange used by LanceDB |
| `sentence-transformers` *(optional)* | Local semantic embedding models when using the `sentence-transformers` backend |
| Hermes Agent | Host runtime and memory-provider/plugin loading |

---

## License

MIT. See [`LICENSE`](LICENSE).
