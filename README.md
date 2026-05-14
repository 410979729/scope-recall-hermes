# scope-recall

`scope-recall` is a Hermes local memory provider built for **current-turn recall** with strong runtime scope isolation.

It now uses a **two-layer design**:

- **SQLite truth store** for durable local records and deterministic auditing
- **LanceDB vector companion** for semantic retrieval and hybrid ranking

This replaces the old `lancepro` naming, which was misleading because the earlier implementation was SQLite-only.

## What it does

- recalls against the **current user query** inside `prefetch()`
- keeps `queue_prefetch()` as a no-op to avoid stale next-turn injection
- reads Hermes built-in curated memory files live at recall time
- stores provider-owned captures in a local SQLite database
- optionally ranks SQLite candidates with a LanceDB companion vector index
- audits and repairs the vector companion by stable SQLite row id during normal sync

## Installation assumption for Hermes users

This project is published for people who want to **download it and use it with Hermes**.

The intended install shape today is:

1. download or clone this plugin directory
2. place the unpacked directory at `$HERMES_HOME/plugins/scope-recall/`
3. enable it in Hermes as a local plugin / memory provider

Important boundary:

- current Hermes plugin discovery expects an **unpacked plugin directory**
- a wheel build is useful for packaging/release verification, but it is **not** the primary install path for Hermes users yet
- do not read wheel build success as proof that Hermes can install or discover the plugin directly from the wheel alone

## Storage layout

Under the active Hermes profile:

- `$HERMES_HOME/scope-recall/memory.sqlite3`
- `$HERMES_HOME/scope-recall/config.json`
- `$HERMES_HOME/scope-recall/lancedb/`

Legacy `lancepro` storage is migrated forward on first initialization when present.

## Architecture

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

It stores:

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

## Vector repair and stats

SQLite is the cardinality authority. During vector sync, the provider compares SQLite ids with LanceDB ids, deletes stale vector rows, collapses duplicate physical rows by id, and embeds missing/changed rows. If LanceDB delete/upsert fails, the SQLite write is preserved and vector state becomes `needs_repair` instead of surfacing the truth-row write as failed.

`scope_recall_stats` reports:

- `vector.row_count` — physical LanceDB row count
- `vector.unique_id_count` — distinct vector ids
- `vector.duplicate_row_count` — extra physical rows beyond one row per id
- `vector.status` — `ready`, `degraded`, `needs_repair`, `disabled`, or `error`

A healthy synced companion should have `total_memories == vector.unique_id_count == vector.row_count` and `vector.duplicate_row_count == 0` for provider-owned rows.

## Retrieval modes

Configured in `config.json`:

- `lexical`
- `vector`
- `hybrid` *(default)*

Default hybrid weights:

- lexical: `0.45`
- vector: `0.55`

Freshness / recency knobs are also configurable in `config.json`:

- `freshness_hints`
- `freshness_base_weight`
- `freshness_step_weight`
- `freshness_max_weight`

Freshness detection is token-based rather than substring-based, so unrelated words like `know` / `day` / `date` do not accidentally trigger recency bonuses.

Guardrail: if only one side has a score, that side is used directly instead of being unfairly damped by a missing partner score.

## Scope isolation

Scope is built from:

- `platform`
- `agent_workspace`
- `agent_identity`
- `user_id`
- `gateway_session_key` when available
- otherwise `chat_id`
- plus `thread_id` when present

This prevents the same user from leaking memories across different groups, chats, or topics.

## Authority boundary

Hermes built-in curated memory remains authoritative in:

- `$HERMES_HOME/memories/USER.md`
- `$HERMES_HOME/memories/MEMORY.md`

`scope-recall` reads those files live during recall. It does **not** mirror built-in `memory` tool writes into SQLite, which avoids stale duplicates after replace/remove operations. The `on_memory_write` hook is intentionally retained as an observational no-op so Hermes may notify the provider without changing storage ownership.

## Provider tools

Primary-agent only:

- `scope_recall_store`
- `scope_recall_search`
- `scope_recall_stats`

Backward-compatible aliases are still accepted internally for old `lancepro_*` tool names during transition.

## Embedders

Currently implemented:

- `local-hash` — offline hashed fallback embedder
- `local-debug` — tiny deterministic test embedder
- `openai-compatible` — configured default path for Gemini/OpenAI-compatible embedding APIs
- `openai` — direct OpenAI embedding endpoint support
- `sentence-transformers` — local embedding model path for SentenceTransformers / Hugging Face checkpoints

Provider aliases `local-model`, `local-embedding`, and `huggingface` also resolve to the `sentence-transformers` backend.

This means `scope-recall` already supports both:

- hosted API embeddings (for example Gemini/OpenAI-compatible or direct OpenAI)
- local embedding models loaded in-process through `sentence-transformers`

## Migration behavior

On first boot, if `$HERMES_HOME/lancepro/` exists and `$HERMES_HOME/scope-recall/` does not yet contain the new DB/config, the provider:

- copies the legacy SQLite database into the new location
- copies `config.json` forward
- records migration info in `scope_recall_stats`

OpenClaw `memory-lancedb-pro` history is handled separately as an explicit import problem, not automatic compatibility. See:

- `docs/migration.md`
- `docs/differences-from-memory-lancedb-pro.md`
- `scripts/import.openclaw.memory_lancedb_pro.py`

## Current limitations

- vector sync is incremental by stable row id / `updated_at`, with duplicate-id/stale-row repair during normal sync; `scripts/repair.vector_index.py` can rebuild the LanceDB companion from SQLite truth when deeper storage hygiene is needed
- fallback `local-hash` is only a degraded offline path, not a true semantic model
- old `lancepro` directory still exists as a compatibility shim until final cleanup is approved
- standalone GitHub repo packaging is bootstrapped with `pyproject.toml` and GitHub Actions CI; a future repo split may still want a conventional `src/` layout

## Packaging and release bootstrap

This directory now includes:

- `pyproject.toml`
- `.gitignore`
- `CONTRIBUTING.md`
- `.github/workflows/ci.yml`
- `scripts/check.release.py`
- `.env.example`
- `scripts/repair.vector_index.py`

Basic packaging verification target:

```bash
python3 -m pip wheel . --no-deps -w /tmp/scope-recall-dist
```

Important boundary:

- Hermes runtime discovery for this plugin still expects an unpacked plugin directory under `$HERMES_HOME/plugins/scope-recall/`
- a successful wheel build is a packaging sanity check for the Python module, not proof that Hermes can discover or install the plugin directly from that wheel
- do not treat wheel success alone as live-plugin installation verification unless Hermes later gains an explicit wheel/entry-point install path for this plugin shape

## Test status

Current focused regression coverage includes:

- plugin loading from `$HERMES_HOME/plugins`
- hybrid recall returning semantically matched content
- built-in curated memory reflection
- vector state visible in stats
- runtime fallback from unavailable API embeddings to `local-hash`
- vector table rebuild when embedder dimensions change
- vector duplicate physical rows are repaired back to one row per id
- vector delete/upsert failure preserves SQLite truth and marks vector status `needs_repair`
- explicit vector companion rebuild from SQLite truth via `scripts/repair.vector_index.py`
- release gate automation via `scripts/check.release.py`
- `scope_recall_stats` exposes physical rows, unique ids, and duplicate-row count
- top-level `import scope_recall` stays light without Hermes runtime modules
- `on_memory_write` remains an intentional observational no-op

The repository is structured for GitHub publication as a beta plugin. Legacy `lancepro` compatibility remains intentionally covered by focused migration and alias tests during the deprecation window.
