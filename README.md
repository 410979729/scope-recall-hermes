# Scope Recall for Hermes

<div align="center">

**Hermes current-turn memory provider with journal-first semantic capture, permanent recall, SQLite truth storage, and optional vector companions**

*Give Hermes durable memory that can follow the same user across windows/chats while keeping local scratch context from bleeding into the wrong place.*

Current-turn recall · Journal-first capture · Permanent shared memory · Background digest · Local scratch scopes · SQLite truth · LanceDB/SQLite companion · Hybrid RRF retrieval

[![CI](https://github.com/410979729/scope-recall-hermes/actions/workflows/ci.yml/badge.svg)](https://github.com/410979729/scope-recall-hermes/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Hermes Plugin](https://img.shields.io/badge/Hermes-Memory%20Provider-blue)](https://hermes-agent.nousresearch.com/docs)
[![Python](https://img.shields.io/badge/Python-3.11%2B-blue)](pyproject.toml)
[![Storage](https://img.shields.io/badge/Storage-SQLite%20%2B%20Vector-orange)](DESIGN.md)

</div>

`scope-recall` is a Hermes local memory provider built for **current-turn recall** and **permanent semantic memory**. Durable user/project/ops/memory facts are shared across windows/chats for the same user + agent identity; raw general turn captures stay local to the current chat/thread/session.

This repository, `scope-recall-hermes`, is the Hermes implementation. The Python package name and Hermes plugin ID intentionally remain `scope-recall` for runtime compatibility. The OpenClaw sibling implementation lives at [`scope-recall-openclaw`](https://github.com/410979729/scope-recall-openclaw).

Version `1.0.16` continues the stable V1 release line for the documented interfaces, packaged as a public release candidate for broader field testing. It keeps the V1 compatibility contract in [`docs/stability.md`](docs/stability.md) while adding native-safe LanceDB probing and automatic SQLite vector fallback for non-AVX hosts on top of the v1.0.15 audit fixes.

It uses a **three-layer design**:

- **Journal/provenance layer** for eligible raw conversation turns and evidence links that are not directly recalled as durable memory
- **SQLite truth store** for high-density durable local records and deterministic auditing
- **Vector companion** for semantic retrieval and hybrid ranking: LanceDB by default, or `sqlite-bruteforce` for native-free/non-AVX hosts

This replaces the old `lancepro` naming, which was misleading because the earlier implementation was SQLite-only.

### Design promises

- **Truth stays inspectable**: SQLite remains the authoritative store; vectors are rebuildable.
- **Recall is current-turn scoped**: retrieval is based on the active query, not stale queued context from the previous topic.
- **Durable memory travels deliberately**: `user`, `memory`, `project`, and `ops` facts can follow the same user + agent identity across windows/chats, and can cross platforms only when explicit canonical identity mapping is configured.
- **Raw turns are provenance, not durable memory**: eligible conversation turns are written to a journal/staging layer; only digest-produced high-density `user`/`memory`/`project`/`ops` rows enter durable recall and vector sync.
- **Operator actions fail closed**: cross-scope export/dedupe/govern/repair paths require explicit maintenance mode.
- **Install remains practical**: hosted embeddings are used when configured, while deterministic `local-hash` keeps no-key bootstrap available.

### Deployment boundaries

`scope-recall` is the local per-Hermes recall layer. In multi-agent deployments that already run a central shared backend such as PostgreSQL, keep that backend as the cross-agent source of truth and connect it to `scope-recall` through explicit import/export/tool boundaries.

The V1 shape is intentionally simple:

- SQLite remains the local truth store for provider-owned memory records.
- the configured vector backend remains a rebuildable semantic retrieval companion.
- Durable `user`/`memory`/`project`/`ops` facts can be bridged deliberately across systems.
- Local `general` scratch, raw system/tool output, and plaintext secret values stay outside durable recall; explicit `scope_recall_store_secret_index` rows may store only searchable credential indexes such as service/account/purpose/vault references and non-reversible fingerprints.
- Hermes native skills remain the place for procedural knowledge packaging.
- Operational visibility is exposed through doctor, repair, inspect, explain, and benchmark utilities; deployment-specific dashboards can consume those outputs when needed.

For external shared-memory bridge guidance, see [`docs/external-shared-memory.md`](docs/external-shared-memory.md).

### Optional cross-platform identity mapping

By default, durable shared scope remains platform-isolated: `platform + agent_workspace + agent_identity + user_id`. To let the same human recall durable rows across Telegram, CLI, Feishu, or another gateway, configure an explicit canonical identity map in `$HERMES_HOME/scope-recall/config.json`:

```json
{
  "identity": {
    "cross_platform_shared_scope": true,
    "cli_user_id_fallback": "local",
    "user_aliases": {
      "telegram:8176453077": "joy",
      "cli:local": "joy",
      "feishu:ou_xxx": "joy"
    }
  }
}
```

Only durable targets (`user`, `memory`, `project`, `ops`) use the canonical shared scope. `general` scratch, raw journal evidence, chat/thread/session context, and tool traces remain local. Existing platform-specific durable rows stay readable through query-time aliases before any explicit migration. Newly written rows keep `raw_platform`, `raw_user_id`, and mapped `canonical_user` metadata for auditability.

### Provider-specific LLM endpoints

For capture, journal, or nightly digest LLM providers whose chat-completions endpoint is not `base_url + /v1/chat/completions`, set either a full endpoint or disable `/v1` appending:

```json
{
  "journal": {
    "endpoint": "https://ark.cn-beijing.volces.com/api/coding/v3/chat/completions",
    "append_v1": false
  }
}
```

For `scripts/nightly-digest.py`, the same behavior is available through `--endpoint` or `--no-append-v1`.

---

## Why scope-recall?

Most agent memory pain is not just "wrong memory was recalled". The bigger user-facing failure is often "the agent forgot everything when I opened a new window." `scope-recall` therefore separates **durable facts** from **local scratch context**:

- user preferences, project facts, ops notes, and explicitly stored memories follow the same user + agent identity across chats/windows
- raw/general turn captures remain local to the current chat/thread/session so one group's temporary chatter does not contaminate another group
- current-turn recall searches only for memories relevant to the active query, avoiding stale previous-turn injection
- the SQLite truth store remains auditable, and vector stores are only rebuildable semantic companions

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
> **Agent:** recalls the durable project memory from SQLite truth/vector companion and answers from the relevant context. ✅

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
| Storage authority | SQLite is the durable truth; vector backends are rebuildable companion state |
| Hybrid retrieval | SQLite lexical/FTS/BM25 candidates + configured vector companion candidates + RRF reranking + bounded prompt rendering |
| Entity/context layer | SQLite entity index, entity probe/related tools, compact query context, trust feedback |
| Background digest | Profile-scoped journal/nightly consolidation for durable facts, workflow summaries, and sanitized tool-chain evidence |
| Memory scope model | shared durable scope for user/project/ops/memory facts; local scope for general scratch captures |
| Built-in memory integration | Hermes curated `USER.md` / `MEMORY.md` are live-read, not mirrored into SQLite. In gateway contexts with an explicit `user_id`, curated-file recall is opt-in/allowlisted to avoid cross-user leakage from global profile files. |
| Governance | deterministic exact dedupe, conservative near-duplicate merge, filtering, metadata, decay review |
| Migration | local `lancepro` auto-migration; OpenClaw `memory-lancedb-pro` import is explicit |
| Offline bootstrap | deterministic `local-hash` fallback when hosted embeddings are unavailable |

---

## Optional companion: turn-closure-audit

`scope-recall` works as a standalone Hermes memory provider. You can install only this plugin and get scoped current-turn recall, SQLite truth storage, configured vector companion retrieval, and local scratch isolation.

For stricter post-turn knowledge governance, pair it with [`turn-closure-audit`](https://github.com/410979729/turn-closure-audit).

The two plugins solve adjacent problems:

| Plugin | Role |
| --- | --- |
| `scope-recall` | decides what memory should be recalled for the current turn |
| `turn-closure-audit` | audits a completed turn and writes redacted review candidates when important knowledge may not have been retained |

This pairing is useful for long-lived Hermes agents where you want both scoped recall during a conversation and conservative review after the turn ends. It is optional, not a runtime dependency.

---

## Quick start

### Option A: Clone into a Hermes plugin directory

```bash
cd "$HERMES_HOME/plugins"
git clone https://github.com/410979729/scope-recall-hermes.git scope-recall
cd scope-recall
python -m pip install -e ".[lancedb]"
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

If LanceDB/PyArrow native wheels are unsafe on the target CPU, install without extras and select the native-free backend instead:

```bash
python -m pip install -e .
python scripts/repair.vector_index.py --hermes-home "$HERMES_HOME" --backend sqlite-bruteforce --dry-run
```

```json
{
  "vector": {
    "backend": "sqlite-bruteforce"
  }
}
```

### Option B: Manual download / unpacked plugin install

Current Hermes plugin discovery expects an **unpacked plugin directory** named with the public provider spelling: `$HERMES_HOME/plugins/scope-recall/`. The Python import/package spelling remains `scope_recall`; see [`docs/naming.md`](docs/naming.md) for the naming contract.

`scope-recall` V1 targets the current Hermes runtime line, which requires Python 3.11 or newer. If you download a release archive instead of cloning:

1. unpack it as `$HERMES_HOME/plugins/scope-recall/`
2. run `python -m pip install -e "$HERMES_HOME/plugins/scope-recall[lancedb]"` for the default LanceDB path, or install without extras and set `vector.backend: sqlite-bruteforce` on native-sensitive hosts
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
    "candidate_pool": 12,
    "fusion_strategy": "rrf",
    "bm25_weight": 0.15,
    "rrf_weight": 0.18,
    "rrf_min_signals": 2
  },
  "journal": {
    "enabled": true,
    "digest_on_session_end": false,
    "background_digest_enabled": true,
    "extractor": "llm",
    "digest_interval_hours": 2,
    "retention_days": 0,
    "max_entries_per_digest": 500
  },
  "per_turn_extraction": {
    "enabled": false
  },
  "vector": {
    "enabled": true,
    "backend": "lancedb",
    "fallback_backend": "sqlite-bruteforce",
    "sync_mode": "incremental",
    "embedder": {
      "provider": "openai-compatible",
      "model": "gemini-embedding-001",
      "dimensions": 3072,
      "api_key_env": ["SCOPE_RECALL_GEMINI_EMBEDDING_API_KEY"],
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

Vector backend choices:

- `lancedb` — default ANN companion, best for normal hosts; install with `python -m pip install -e ".[lancedb]"`. Scope Recall probes LanceDB/PyArrow in a child process before importing them in the Hermes process, so SIGILL/illegal-instruction wheels are treated as unavailable instead of crashing the agent.
- `sqlite-bruteforce` — pure-Python/SQLite companion for non-AVX CPUs or hosts where importing LanceDB/PyArrow is unsafe; install with `python -m pip install -e .` and set `vector.backend` accordingly, or keep the default `vector.fallback_backend: sqlite-bruteforce` to fall back automatically when LanceDB is absent or unsafe.

Both backends are rebuildable caches. `$HERMES_HOME/scope-recall/memory.sqlite3` remains the truth source.

Credential rule:

- put real API keys in your private environment, not in `config.json`
- if no configured key is available, `scope-recall` falls back to `local-hash`

### Embedding providers

Currently implemented:

| Provider | Use case | Notes |
| --- | --- | --- |
| `openai-compatible` | Gemini/OpenAI-compatible embedding APIs | Default configured path; supports env-based API key lookup |
| `openai` | Direct OpenAI embeddings | Useful when you do not need a custom compatible endpoint |
| `minimax` | MiniMax `embo-01` embeddings | Uses MiniMax's non-OpenAI-compatible `/v1/embeddings` shape with `texts` and `type`; indexing uses `db`, search queries use `query` |
| `sentence-transformers` | Local Hugging Face / SentenceTransformers models | Good for local semantic embeddings when installed |
| `local-hash` | Offline fallback | Deterministic degraded fallback, not a true semantic model |
| `local-debug` | Tests/debugging | Tiny deterministic test embedder |

Provider aliases `local-model`, `local-embedding`, and `huggingface` resolve to the `sentence-transformers` backend.

MiniMax example:

```json
{
  "vector": {
    "embedder": {
      "provider": "minimax",
      "model": "embo-01",
      "dimensions": 1536,
      "api_key_env": ["MINIMAX_API_KEY"],
      "base_url": "https://api.minimaxi.com",
      "document_type": "db",
      "query_type": "query",
      "group_id_env": ["MINIMAX_GROUP_ID"],
      "timeout": 30.0
    }
  }
}
```

MiniMax notes:

- `api_key_env` should point at private environment variables; do not put real keys in `config.json`.
- `document_type` controls vector-indexing/upsert calls and defaults to `db`.
- `query_type` controls vector-search query calls and defaults to `query`.
- `group_id` / `group_id_env` is optional. When configured, Scope Recall sends it as the legacy-compatible `GroupId` query parameter for MiniMax accounts that still require a group id; leave it unset for accounts/endpoints that only require the bearer token.
- `base_url` defaults to `https://api.minimaxi.com`. Override it if your account, proxy, or regional deployment uses another MiniMax embedding endpoint.

---

## Durable memory vs local scratch scope

`scope-recall` does **not** split all memory by every group or tiny window. It uses two provider-owned scopes:

- **Shared durable scope**: `platform + agent_workspace + agent_identity + user_id`. Rows with targets `user`, `memory`, `project`, and `ops` are stored here, so they can be recalled across chats/windows for the same user and agent.
- **Local runtime scope**: shared durable scope plus `gateway_session_key`, or `chat_id` / `thread_id`. Rows with target `general` stay here, so temporary group/topic/session chatter does not bleed elsewhere.
- **Accessible scope set**: normal recall and scoped tool actions can see the current local scope plus the shared durable scope; they cannot see another user, sibling agent identity, or another local chat/thread/session scratch scope.

This aims at the common expectation: "if I gave the agent durable information before, it should remember it later," without making every scratch line globally visible forever.

---

## Detailed recall anchors and secret indexes

### External artifact anchors

Ordinary durable writes preserve stable external lookup handles. When a memory contains a GitHub issue, PR, commit, release, repository URL, or other URL, `scope-recall` appends a deterministic anchor block and stores structured artifact metadata.

Example stored text:

```text
Hermes upstream recommendation request is tracked in the linked issue.

Artifact anchors: GitHub issue NousResearch/hermes-agent#42864 (https://github.com/NousResearch/hermes-agent/issues/42864)
```

The same row also carries `artifacts` metadata with fields such as `kind`, `repo`, `number`, `commit`, `tag`, and `url`. This keeps future recall from relying on vague summaries such as "submitted the RFC" when the useful retrieval key is the exact issue/PR/release/commit handle.

Nightly digest uses the same deterministic artifact extraction in addition to its LLM/heuristic summary logic, so source conversations with external handles retain those anchors even when the human-readable summary is compact.

### Secret indexes, not plaintext secret storage

`scope_recall_store_secret_index` stores a searchable credential index without putting plaintext secret values into the ordinary recall surface. Store the real password/token/API key/private key in an external vault or keyring, then store only the locator and safe metadata in `scope-recall`.

Example tool payload shape:

```json
{
  "label": "production deploy credential",
  "secret_type": "password",
  "service": "example-service",
  "account": "deploy-user",
  "vault_ref": "vault://ops/example-service/deploy-user",
  "rotation_due": "quarterly",
  "notes": "Use only for authorized deployment maintenance."
}
```

Returned/stored metadata includes `secret_value_stored: false`. If a caller supplies `secret_value`, it is used only to compute a short non-reversible fingerprint prefix; the plaintext secret is not written to SQL/FTS/vector text, metadata, exports, logs, or chat replies.

---

## Dual-memory architecture: important

When `scope-recall` is active, Hermes memory has **two intentional authority zones**:

| Layer | Storage | Purpose | How recall sees it |
| --- | --- | --- | --- |
| Hermes curated memory | `$HERMES_HOME/memories/USER.md`, `$HERMES_HOME/memories/MEMORY.md` | User profile and durable hand-curated notes managed by Hermes built-in memory | Live-read during recall; not mirrored into SQLite; gateway `user_id` contexts require curated-memory opt-in/allowlist |
| Scope Recall journal/provenance | `$HERMES_HOME/scope-recall/memory.sqlite3` (`journal_entries`, `memory_journal_sources`) | Eligible raw turns and digest evidence links; not ordinary recall memory | Background digest reads it, but recall does not inject raw journal rows |
| Scope Recall provider memory | `$HERMES_HOME/scope-recall/memory.sqlite3` + configured vector companion (`lancedb/` or `vector.sqlite3`) | Provider-owned shared durable memories plus local scratch rows, scope metadata, lexical/vector/RRF retrieval | SQLite truth + rebuildable companion ranking |

Key principles:

> SQLite is the truth source for provider-owned rows. Hermes curated memory files remain their own truth source. The configured vector backend is a rebuildable retrieval companion, not the authority.

> Raw conversation turns are provenance first. They become durable recall only after journal/nightly digest turns them into high-density, merge-upserted memory rows.

This is deliberate. Mirroring curated memory writes into SQLite can leave stale duplicates after replace/remove operations. Live-reading curated memory keeps Scope Recall aligned with Hermes native memory behavior. Because those curated files are profile-global, live-read recall defaults to `single-user`: it is active for single-user/no-`user_id` runtimes and disabled for explicit gateway `user_id` contexts unless `curated_memory.mode` is set to `profile-global` or `explicit-users` with matching `allowed_user_ids`.

---

## Storage layout

Under the active Hermes profile:

- `$HERMES_HOME/scope-recall/memory.sqlite3`
- `$HERMES_HOME/scope-recall/config.json`
- `$HERMES_HOME/scope-recall/lancedb/` when `vector.backend=lancedb`
- `$HERMES_HOME/scope-recall/vector.sqlite3` when `vector.backend=sqlite-bruteforce`

Inside `memory.sqlite3`, `journal_entries` and `memory_journal_sources` preserve provenance for background digest without turning raw turns into ordinary recall rows.

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
   +--> SQLite truth lookup / FTS / BM25 / entity graph indexes
   |       - provider-owned memory rows
   |       - scope metadata
   |       - timestamps and governance metadata
   |
   +--> configured vector companion
   |       - semantic candidate retrieval
   |       - rebuildable from SQLite truth
   |
   v
hybrid scoring + RRF/BM25/entity-aware ranking + bounded prompt block
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
| `sqlite_vector_store.py` | Pure-SQLite brute-force vector companion for native-free hosts |
| `vector_runtime.py` | Vector runtime status and degradation handling |
| `recall.py` | Lexical/vector/hybrid recall orchestration |
| `scoring.py` | Score fusion, reciprocal-rank fusion, freshness boosts, capping logic |
| `gating.py` | Recall/capture gating and noise filtering |
| `capture.py` | Auto-capture pipeline |
| `journal.py` | Journal/provenance schema, journal digest, merge-upsert and evidence links |
| `governance.py` | Deterministic dedupe, metadata, decay/governance review |
| `memory_ops.py` | Store/search/forget/update/dedupe/merge/export/govern operations |
| `tooling.py` | Provider tool dispatch |
| `schemas.py` | Hermes tool schemas |
| `migration.py` | Legacy `lancepro` migration helpers |
| `nightly_digest.py` | Daily conversation digest pipeline, LLM/heuristic extraction, semantic write decisions |
| `scripts/import.openclaw.memory_lancedb_pro.py` | Explicit OpenClaw history importer |
| `scripts/nightly-digest.py` | CLI wrapper for the profile-scoped daily digest |
| `scripts/journal-digest.py` | CLI wrapper for journal-first background digest |
| `scripts/repair.vector_index.py` | Rebuild/repair the configured vector companion from SQLite truth |
| `scripts/check.release.py` | Full V1 release gate used locally and by CI |

</details>

### 1. SQLite truth layer

SQLite is the authoritative provider-owned store.

It keeps:

- durable memory rows
- scope metadata
- lexical FTS index
- journal provenance tables and digest evidence links
- timestamps for auditing and migration

Why SQLite stays authoritative:

- deterministic local persistence
- easy schema inspection
- simple migration/backup story
- safer open-source baseline than tying truth directly to a vector backend

### 2. Vector companion

The configured vector backend is a **companion retrieval index**, not the truth source. LanceDB is the default ANN backend; `sqlite-bruteforce` is a pure-Python/SQLite fallback for non-AVX or native-dependency-sensitive hosts.

Both backends store retrieval-ready fields copied from SQLite plus a vector column:

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

### Nightly conversation digest

`scripts/nightly-digest.py` is a plugin-owned batch path for daily memory consolidation. It reads the profile's active Hermes `state.db` first, falls back to legacy `lcm.db`, and scans the selected local date by message timestamps. Raw `system` rows and raw `tool` outputs are not stored. For task sessions, the digest keeps a sanitized tool-chain summary so repeated engineering workflows can be recalled later as `workflow` memory.

Typical smoke run:

```bash
python scripts/nightly-digest.py --hermes-home "$HERMES_HOME" --date 2026-06-01 --dry-run --extractor heuristic --verbose
```

Production runs default to the LLM extractor. The provider stages eligible turns into `journal_entries`, then schedules a non-blocking background digest according to `journal.digest_interval_hours` when `journal.background_digest_enabled=true`. `journal.digest_on_session_end=false` keeps slow LLM promotion out of normal session closeout by default; session-end LLM promotion requires explicit `journal.allow_session_end_llm=true`. The script reads model/base URL/API key information from the Hermes profile config and `.env`, with `SCOPE_RECALL_DIGEST_API_KEY` available as an explicit override. If the LLM path fails or returns no usable candidates, journal digest does **not** silently fall back and consume journal evidence; operators must explicitly request `--extractor heuristic` or set `journal.allow_heuristic_fallback=true` for a degraded fallback run. Actual writes use SQLite truth rows, FTS/entity sync, digest run/source ledgers, semantic skip/update/insert decisions, exact duplicate cleanup, and configured vector companion upsert when vector indexing is enabled.

### Hybrid retrieval

```text
current query
   ├─> SQLite lexical / FTS candidates
   └─> vector companion candidates
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

This prevents raw identifiers containing delimiters from colliding with split scope fields, and preserves the intended split: durable facts can move with the same user + agent identity, while local scratch rows do not leak across different groups, chats, sessions, or topics.

### Vector repair and stats

SQLite is the cardinality authority. During vector sync, the provider compares SQLite ids with vector companion ids, deletes stale vector rows, collapses duplicate physical rows by id where the backend can expose them, and embeds missing/changed rows. If vector delete/upsert fails, the SQLite write is preserved and vector state becomes `needs_repair` instead of surfacing the truth-row write as failed.

`scope_recall_stats` reports:

- `vector.row_count` — physical vector companion row count
- `vector.unique_id_count` — distinct vector ids
- `vector.duplicate_row_count` — extra physical rows beyond one row per id
- `vector.status` — `ready`, `degraded`, `needs_repair`, `disabled`, or `error`

When `vector.index_general=false` (the default), local `general` scratch rows are not expected in the vector companion. A healthy synced companion should have `vector.unique_id_count == vector.row_count`, `vector.duplicate_row_count == 0`, and vector ids matching the configured vector-indexed provider rows.

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
scope_recall_context
scope_recall_probe
scope_recall_related
scope_recall_feedback
scope_recall_forget
scope_recall_update
scope_recall_merge
scope_recall_export
scope_recall_stats
scope_recall_inspect
scope_recall_explain
scope_recall_benchmark
```

Operator-only maintenance tools are hidden from the default schema and require `maintenance_tools_enabled=true`:

```text
scope_recall_dedupe
scope_recall_govern
scope_recall_hygiene
scope_recall_repair
```

`scope_recall_hygiene` is read-only. It reports runtime-wrapper noise, assistant scratch prose, duplicate dedupe keys, very short/long rows, `general` rows present in the vector companion, likely promotion candidates, and likely delete candidates. It does not delete, merge, promote, or rewrite rows.

For an offline SQLite report without exposing the maintenance tool to agents:

```bash
python scripts/report.hygiene.py --db "$HERMES_HOME/scope-recall/memory.sqlite3" --format markdown
```

Destructive cleanup is intentionally out-of-band: use the hygiene report first, then require an explicit operator decision before running any separate delete/merge/dedupe action. The shipped hygiene path is dry-run/report-only.

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
| `scope_recall_context` | Render a compact task-relevant memory context block plus structured evidence for a query |
| `scope_recall_probe` | Inspect accessible memories attached to a specific entity |
| `scope_recall_related` | List entities that co-occur with a given entity in accessible memories |
| `scope_recall_feedback` | Mark a memory as helpful or unhelpful so trust scoring can adjust future recall |
| `scope_recall_forget` | Delete memories by exact id/ids within the current accessible scope set; search or inspect first, then pass explicit ids |
| `scope_recall_update` | Replace content/category within the current accessible scope set; shared/local target-mode changes are rejected |
| `scope_recall_dedupe` | Operator-only: inspect or collapse exact duplicate rows |
| `scope_recall_merge` | Merge same-scope memories into a target row; shared/local mixing is rejected |
| `scope_recall_export` | Export SQLite truth rows as JSON or JSONL; defaults to current accessible scope set |
| `scope_recall_govern` | Operator-only: review tier distribution and decay/archive candidates |
| `scope_recall_hygiene` | Operator-only, read-only: report memory-quality cleanup/promotion candidates without modifying rows |
| `scope_recall_repair` | Operator-only: repair/rebuild the configured vector companion from SQLite truth |
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

`scope-recall` was inspired by good public ideas in OpenClaw `memory-lancedb-pro`, especially current-turn recall, scoped memory boundaries, hybrid retrieval, and memory hygiene. It keeps those ideas in a Hermes-native implementation with SQLite truth storage and an explicit OpenClaw import path.

| Area | OpenClaw `memory-lancedb-pro` | `scope-recall` V1 |
| --- | --- | --- |
| Host agent | OpenClaw | Hermes |
| Truth model | LanceDB-centric OpenClaw memory pipeline | SQLite truth + rebuildable vector companion index |
| Recall timing | OpenClaw auto-recall hook model | Hermes `prefetch(query)` current-turn recall with `queue_prefetch()` kept as a deliberate no-op |
| Curated memory | Separate OpenClaw markdown/journal behavior | Hermes `USER.md` / `MEMORY.md` live-read and kept authoritative |
| Smart extraction | LLM-backed created/merged/skipped style in upstream beta line | deterministic/rules-based extraction and conservative merge |
| Lifecycle | Weibull decay / tier promotion concepts upstream | deterministic metadata classification plus decay/governance review; summarization and promotion stay in explicit digest/operator workflows |
| Migration | OpenClaw-native data path | explicit importer from OpenClaw LanceDB shape into SQLite truth |

Recommended public description:

> `scope-recall` is a Hermes local memory provider for current-turn recall with SQLite truth storage, configurable vector companion retrieval, strong runtime scope isolation, deterministic write-time governance, and explicit migration boundaries.

When describing OpenClaw migration, use the explicit importer path rather than drop-in replacement, direct `.lance` reuse, or broad feature-parity wording.

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

The provider should degrade to `local-hash`. That keeps the system usable but lowers semantic quality. Set `SCOPE_RECALL_GEMINI_EMBEDDING_API_KEY` in your private environment to use the configured hosted path.

### OpenClaw `.lance` data does not appear automatically

That is expected. OpenClaw history must be explicitly imported into SQLite truth rows before the companion vector index is rebuilt.

### Live gateway still behaves like the old code

Release checks prove the source tree and artifact. They do not prove a running Hermes gateway has loaded the new plugin. Restart/reload the target Hermes process and verify with a real runtime smoke test before claiming live-runtime freshness.

---

## Current V1 limitations

- vector sync is incremental by stable row id / `updated_at`, with duplicate-id/stale-row repair during normal sync; `scripts/repair.vector_index.py` can rebuild the configured vector companion from SQLite truth when deeper storage hygiene is needed
- semantic merge is intentionally conservative and rules/scoring-based; contradiction handling stays evidence-oriented rather than open-ended LLM reasoning
- write-time smart extraction is rules-based for common preference / ops / project-fact sentences; nightly digest adds a separate LLM/heuristic batch consolidation path for reviewed workflow summaries
- fallback `local-hash` is a degraded offline availability path; configure a hosted or local semantic embedder for better semantic quality
- old `lancepro` directory still exists as a compatibility shim during the V1 transition window
- the supported Hermes install shape is still an unpacked plugin directory; the wheel is verified as a package artifact while Hermes discovery remains directory-based

See [`docs/stability.md`](docs/stability.md) for the exact V1 compatibility scope.

---

## Documentation

| Document | Description |
| --- | --- |
| [`DESIGN.md`](DESIGN.md) | Architecture, layer split, retrieval model, migration plan, and release expectations |
| [`docs/stability.md`](docs/stability.md) | Stable V1 compatibility contract and scope |
| [`docs/naming.md`](docs/naming.md) | Public `scope-recall` vs Python/tool `scope_recall` naming contract |
| [`docs/hermes-upstream-recommendation-plan.md`](docs/hermes-upstream-recommendation-plan.md) | Roadmap for official Hermes standalone-provider recommendation |
| [`docs/migration.md`](docs/migration.md) | Local `lancepro` migration and explicit OpenClaw import guidance |
| [`docs/differences-from-memory-lancedb-pro.md`](docs/differences-from-memory-lancedb-pro.md) | Honest comparison with OpenClaw `memory-lancedb-pro` |
| [`CHANGELOG.md`](CHANGELOG.md) | Release history |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Contribution and development notes |

---

## Release and verification

The public release gate is intentionally the same script used by GitHub Actions. Run it from a Hermes-compatible Python environment so plugin-loader imports and LanceDB/pyarrow dependencies match the runtime:

```bash
cd /path/to/scope-recall
PYTHONPATH=/path/to/hermes-agent:/path/to/scope-recall \
  /path/to/hermes-agent/venv/bin/python -m pytest -q
PYTHONPATH=/path/to/hermes-agent:/path/to/scope-recall \
  /path/to/hermes-agent/venv/bin/python scripts/check.release.py
/path/to/hermes-agent/venv/bin/python scripts/doctor.py --source-root /path/to/scope-recall --hermes-home "$HERMES_HOME"
/path/to/hermes-agent/venv/bin/python scripts/repair.vector_index.py --hermes-home "$HERMES_HOME" --dry-run
```

Plain `pytest` from an unrelated Python environment is not a valid release signal: it can miss Hermes' `plugins.memory` loader path or the vector dependencies (`lancedb`, `pyarrow`) even when the checked-in plugin is healthy.

Release publishing is tag-driven: `.github/workflows/release.yml` can create a GitHub Release for a `v*` tag and populate notes from the matching `CHANGELOG.md` entry.

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
- length-framed scope identifiers prevent delimiter-collision between user/chat/thread/session components
- operator `scope_recall_dedupe(scope_only=false)` covers duplicate groups across all scopes while ordinary scoped actions remain bounded to the current accessible scope set
- capture filtering blocks known maintenance prompts, trivial replies, obvious secret-bearing text, and overlong prompt blocks
- semantic near-duplicate merge and conflict preservation
- rules-based smart extraction from user turns into preference / ops / project fact memories
- nightly digest session loading, redaction, workflow memory writes, ledgers, duplicate skips, and dry-run behavior
- merge / export / govern provider tools
- governance metadata classification and decay review candidates
- provider tools cover store/search/context/probe/related/feedback/forget/update/dedupe/merge/export/govern/repair/stats
- explicit vector companion rebuild from SQLite truth via `scripts/repair.vector_index.py`
- `scope_recall_stats` exposes physical rows, unique ids, and duplicate-row count
- top-level `import scope_recall` stays light without Hermes runtime modules
- `on_memory_write` remains an intentional observational no-op

---

## Dependencies

| Package | Purpose |
| --- | --- |
| `lancedb>=0.30.2` *(optional extra `lancedb`)* | Default LanceDB companion vector index |
| `pyarrow>=24,<25` *(optional extra `lancedb`)* | Arrow data interchange used by LanceDB |
| Python stdlib `sqlite3` | Native-free `sqlite-bruteforce` companion backend |
| `sentence-transformers` *(optional)* | Local semantic embedding models when using the `sentence-transformers` backend |
| Hermes Agent | Host runtime and memory-provider/plugin loading |

---

## License

MIT. See [`LICENSE`](LICENSE).
