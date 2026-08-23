# Scope Recall for Hermes

<div align="center">

**Hermes current-turn memory provider with journal-first semantic capture, durable recall, SQLite truth storage, and optional vector companions**

*Give Hermes durable memory that can follow the same user across windows/chats while keeping local scratch context from bleeding into the wrong place.*

Current-turn recall · Journal-first capture · Durable shared memory · Background digest · Local scratch scopes · SQLite truth · LanceDB/SQLite companion · Hybrid RRF retrieval

[![CI](https://github.com/410979729/scope-recall-hermes/actions/workflows/ci.yml/badge.svg)](https://github.com/410979729/scope-recall-hermes/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Hermes Plugin](https://img.shields.io/badge/Hermes-Memory%20Provider-blue)](https://hermes-agent.nousresearch.com/docs)
[![Python](https://img.shields.io/badge/Python-3.11%2B-blue)](pyproject.toml)
[![Storage](https://img.shields.io/badge/Storage-SQLite%20%2B%20Vector-orange)](DESIGN.md)

</div>

`scope-recall` is a Hermes local memory provider built for **current-turn recall** and **durable semantic memory**. Durable user/project/ops/memory facts are shared across windows/chats for the same user + agent identity; raw general turn captures stay local to the current chat/thread/session.

This repository, `scope-recall-hermes`, is the Hermes implementation. The Python distribution package is `hermes-scope-recall`, the Python import/package spelling is `scope_recall`, and the Hermes plugin ID/provider name remains `scope-recall` for runtime compatibility. The OpenClaw sibling implementation lives at [`scope-recall-openclaw`](https://github.com/410979729/scope-recall-openclaw).

## Benchmark: LoCoMo long-conversation memory

In August 2026 we ran Scope Recall's `1.9.2` development line end-to-end on the [LoCoMo](https://github.com/snap-research/locomo) long-conversation benchmark: all **1,540 questions** across the four non-adversarial categories, with **100% coverage and zero invalid results** — a full run, not a sample.

- **Overall accuracy: 70.58%** (1,087 / 1,540)
- **Single-hop factual memory: 84.78%**
- Temporal reasoning: 54.83%
- Multi-hop reasoning: 54.61%
- Open-domain inference: 45.83%
- Retrieval side: Top-50 evidence any-hit recall **97.66%**, all-hit recall **90.63%**

The run used `gemini-embedding-001` for retrieval and `gpt-5.4-mini` as answerer, judge, and query planner. Every per-question result and the run manifest are hash-pinned, and the final score was recomputed independently from the raw rows; the public, path-free receipt is [`docs/benchmarks/locomo-2026-08.md`](docs/benchmarks/locomo-2026-08.md). The benchmark harness (`scripts/benchmark.locomo.py`) is part of the `1.9.2` development line so the evaluation stays reproducible.

The August run predates the harness's secret-free model-route receipt and stricter official-comparability checklist. It is therefore **legacy local evidence**, not a run for which the current `official_comparable_categories_1_to_4` flag may be asserted.

The runner has no machine-specific dataset, source-tree, or auth defaults. Supply the external paths explicitly, keep the run directory outside the source checkout, and add `--auth-path` only for model-backed phases:

```bash
python scripts/benchmark.locomo.py \
  --dataset /path/to/locomo.json \
  --run-dir /path/outside/the/checkout/locomo-run \
  --hermes-agent-root /path/to/hermes-agent \
  --phase retrieve
```

The three categories near 50% remain the clearest future improvement areas: multi-hop evidence completeness, temporal evidence presentation, and open-domain synthesis. We do not claim a cross-vendor ranking, because public LoCoMo evaluations use materially different models, judges, prompts, and dataset variants.

Version `1.10.3` is the public patch candidate on the last packaged `1.10.2` line. It fixes issue #50: governance coverage and cleanup rollback now recognize the official `memory_auto_adjudication` + `archive` receipt, while unknown archive writers remain fail-closed. Rollback still refuses state changed after the recorded receipt. SQLite authority, stable provider/tool identities, schema, and automatic-adjudication policy are unchanged.

The `1.8.6` patch hardened legacy fact-freshness maintenance, Unicode secret filtering, operator portability, truth-connection ownership, lifecycle relation restore, and semantic deduplication. It preserves the 1.8.5 Windows activation-lease PID fix and includes the 1.8.4 deep-audit closure for deterministic freshness, validator, truth-store permission, operator recovery, secret scanning, and cross-platform release gates. Its 1.8.3 predecessor hardened current-state recall, canonical chat identity boundaries, Windows repair and rollback, bounded vector-outbox history, and model-calibrated vector-only filtering. The `1.8.2` release made memory startup honest and local-model tool use reliable. If a configured local embedding model cannot actually load, Scope Recall now reports the degradation and uses a compatible fallback only for a fresh generation; it never opens an existing generation with a different embedding space. It also adds `light`, `balanced`, and `full` semantic retention profiles and fixes LM Studio/llama.cpp grammar initialization without deleting structured claim, freshness, or evolution capabilities. Durable `user`, `memory`, `project`, and `ops` fact actions resolve to shared durable scope, while `general` remains local scratch on every integration path. Existing ordinary-memory behavior remains the default because the new evolution, temporal-query, and Reflection surfaces are opt-in. The `1.7.2` release published a compatibility-preserving storage and governance hardening patch: ordinary recall uses one lifecycle policy across journal, nightly, deduplication, and vector paths; vector rebuilds support immutable generations and explicit compare-and-swap activation; metadata and import provenance are sanitized before durable or operator-visible sinks; candidate, freshness, and config mutations fail closed; and folded inline data URLs are removed without losing surrounding prose. It builds on version 1.7.1's runtime-config, candidate-browser, external-bridge, and release-gate fixes. Version 1.7.0 published the productization feature set on the stable V1 release line: event-digest evidence packets, reviewable candidate extraction, read-only memory browsing, candidate governance commands, Experience-to-skill bridge helpers, optional PGVector companion support, external shared-memory bridge contracts, explicit sensitivity governance, release-gate progress output, and same-process peer-provider SQLite lock recovery for `scope_recall_store`. Version 1.6.3 closed issue #25 with conservative SQLite lock recovery and a single safe retry for `scope_recall_store` while keeping non-SQLite business errors non-retryable. Version 1.6.2 added graph-relation backfill/benchmark visibility and hardened Experience review and journal-digest bookkeeping without changing the stable V1 runtime contract. Version 1.6.1 published documentation, packaging, and release-provenance updates without changing the stable V1 runtime contract. The 1.6.0 release packages a compatibility-preserving refactor of the doctor, graph-hygiene, maintenance, digest-result, recall-pipeline, and provider-schema internals while keeping the stable V1 commercial-governance line introduced in 1.5.0. The 1.5 line includes promoted-only profile lifecycle safety, candidate-memory promotion planning, graph-hygiene repair, fail-closed vector-repair fallback handling, governance cleanup, journal recovery, an operator dashboard, repository-owned golden benchmarks, stricter release gates, fail-closed hard-delete safety, packaged benchmark fixtures, Recall Funnel observability, synthetic retrieval-regression benchmarking, and default-safe vector fallback behavior. Runtime Experience packet injection is enabled by default through `experience.prefetch_enabled=true` and can be disabled with `experience.prefetch_enabled=false`; background automatic promotion remains an explicit operator opt-in through `experience.auto_promotion_enabled=true`, and low-risk auto-promotion remains a second explicit opt-in through `experience.auto_promote_low_risk=true`. By default, successful low-risk scans create candidate playbooks, high-risk playbooks stay review-gated, and final-failure or low-signal traces are not promoted. It keeps the `scope_recall_profile` surface added in v1.3.0, compression-boundary journal staging through Hermes' `on_pre_compress()` memory-provider hook, inline attachment-marker sanitization, the supported standalone install shape added in v1.1.0, and native-safe LanceDB probing with fresh-bootstrap SQLite vector fallback for non-AVX hosts.

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

**SQLite runtime safety:** WAL deployments should run SQLite `3.51.3+` or a fixed backport (`3.50.7` or `3.44.6`). SQLite documents a rare WAL-reset corruption race in the otherwise affected `3.7.0`–`3.51.2` range when separate connections overlap writes/checkpoints; see [SQLite's WAL-reset documentation](https://sqlite.org/wal.html#walresetbug). Scope Recall keeps live probes on pager connections, but the plugin cannot replace the SQLite library linked into Python.

The V1 shape is intentionally simple:

- SQLite remains the local truth store for provider-owned memory records.
- the configured vector backend remains a rebuildable semantic retrieval companion.
- Durable `user`/`memory`/`project`/`ops` facts can be bridged deliberately across systems.
- Local `general` scratch, raw system/tool output, and plaintext secret values stay outside durable recall; explicit `scope_recall_store_secret_index` rows may store only searchable credential indexes such as service/account/purpose/vault references and non-reversible fingerprints.
- Hermes native skills remain the place for procedural knowledge packaging.
- Operational visibility is exposed through doctor, repair, inspect, explain, and benchmark utilities; deployment-specific dashboards can consume those outputs when needed.

For external shared-memory bridge guidance, see [`docs/external-shared-memory.md`](docs/external-shared-memory.md). For public JSON report contracts used by doctor, dashboard, golden benchmark, replay, and forgetting outputs, see [`docs/response-contracts.md`](docs/response-contracts.md). For reviewed procedural playbooks and preflight packets, see [`docs/experience.kernel.md`](docs/experience.kernel.md).

### Optional cross-platform identity mapping

By default, durable shared scope remains platform-isolated: `platform + agent_workspace + agent_identity + user_id`. To let the same human recall durable rows across Telegram, CLI, Feishu, or another gateway, configure an explicit canonical identity map in `$HERMES_HOME/scope-recall/config.json`:

```json
{
  "identity": {
    "cross_platform_shared_scope": true,
    "cli_user_id_fallback": "local",
    "user_aliases": {
      "telegram:user_123": "canonical_user_123",
      "cli:local": "canonical_user_123",
      "feishu:ou_xxx": "canonical_user_123"
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

All capture, journal/nightly, reflection, OpenAI-compatible embedding, and MiniMax embedding requests share one endpoint policy:

- HTTPS is the default for hosted endpoints. Non-HTTP(S) schemes, embedded URL credentials, credential-like query parameters, fragments, cross-origin redirects, and HTTPS-to-HTTP redirect downgrades are rejected before request content reaches the destination.
- Loopback HTTP (`localhost`, `*.localhost`, `127.0.0.1`, or `::1`) remains available for local Ollama, LM Studio, and similar services, but Scope Recall strips credential-bearing headers from every HTTP request through the same normalized registry used for URL queries, including authorization, API-key, provider token, cookie, proxy credential, and signed-request aliases.
- A non-loopback HTTP endpoint requires a literal boolean `allow_insecure_endpoint: true` setting (for example under `journal`, `capture_llm`, `reflection`, or `vector.embedder`) or the explicit `--allow-insecure-endpoint` CLI flag. This opt-in permits plaintext request content but **does not** permit plaintext credentials; endpoints that require authentication must use HTTPS. Quoted strings such as `"true"` or `"false"`, numbers, arrays, objects, and other non-boolean values fail closed at config/public-option resolvers, while the final transport boundary independently rejects non-boolean direct inputs.
- urllib transports follow only same-origin redirects. The OpenAI SDK embedding transport disables automatic redirects entirely.
- Endpoint-policy failures are non-retryable configuration errors and never trigger heuristic memory extraction fallback.

`python scripts/doctor.py --json --hermes-home "$HERMES_HOME"` validates the enabled capture, LLM journal, reflection, and hosted primary/fallback embedding endpoints without performing network requests or resolving API keys. Journal/reflection checks use the same inherited Hermes provider route as runtime, and hosted embedding checks honor `base_url_env`. Automation should inspect `checks.endpoint_policy`; sanitized per-surface details under `runtime.endpoint_policy` contain only the origin and recognized public API suffix, never arbitrary configured path segments.

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
| Entity/context layer | SQLite entity index, entity probe/related tools, compact query context, compact profile/context surface, trust feedback |
| Background digest | Profile-scoped journal/nightly consolidation for durable facts, workflow summaries, and sanitized tool-chain evidence |
| Memory scope model | shared durable scope for user/project/ops/memory facts; local scope for general scratch captures |
| Built-in memory integration | Hermes curated `USER.md` / `MEMORY.md` are live-read, not mirrored into SQLite. In gateway contexts with an explicit `user_id`, curated-file recall is opt-in/allowlisted to avoid cross-user leakage from global profile files. |
| Governance | deterministic exact dedupe, conservative near-duplicate merge, filtering, metadata, decay review |
| Migration | local `lancepro` auto-migration; OpenClaw `memory-lancedb-pro` import is explicit |
| Offline bootstrap | deterministic `local-hash` fallback when hosted embeddings are unavailable |
| Maintainer contracts | [`docs/contract.matrix.md`](docs/contract.matrix.md) maps major feature contracts to source files, targeted tests, release gates, and dynamic probes so large-context changes stay evidence-backed |

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

### Standard install: three commands

Install the provider package in the same Python environment that runs Hermes, then install and activate it in one command:

```bash
python -m pip install "hermes-scope-recall[lancedb]"
hermes-scope-recall install --activate --hermes-home "${HERMES_HOME:-$HOME/.hermes}" --json
hermes-scope-recall verify --runtime --hermes-home "${HERMES_HOME:-$HOME/.hermes}"
```

`install --activate` copies the plugin into `$HERMES_HOME/plugins/scope-recall`, sets `memory.provider: scope-recall` in `$HERMES_HOME/config.yaml`, bootstraps `$HERMES_HOME/scope-recall/memory.sqlite3`, and returns JSON verification plus rollback evidence. Before replacing or migrating anything, it captures the plugin/config/provider-config pre-state and uses SQLite's online backup API for an existing truth DB. A config, migration, provider-load, or runtime-verification failure automatically restores every captured surface and returns `ok=false` with an `activation_transaction` receipt. See [`docs/install.md`](docs/install.md) for the complete install, verify, upgrade, and rollback guide.

### Upgrade and rollback

When replacing an existing plugin copy, use the explicit upgrade verb so the JSON output records `previous_version`, `new_version`, backup paths, verification details, and rollback commands:

```bash
hermes-scope-recall upgrade --hermes-home "${HERMES_HOME:-$HOME/.hermes}" --dry-run --json
# Stop the gateway and all Scope Recall writers before this apply step.
hermes-scope-recall upgrade --activate --maintenance-mode --hermes-home "${HERMES_HOME:-$HOME/.hermes}" --json
```

If the dry-run reports that the active vector manifest is missing while SQLite truth or a configured companion already contains state, leave that companion untouched. From a `1.8.1` or newer source checkout, run `python scripts/migrate.vector_generation.py --hermes-home "${HERMES_HOME:-$HOME/.hermes}" --dry-run --json`, inspect the intended embedding identity, then run the same command with `--apply --activate` under the maintenance boundary before retrying the upgrade. This builds a validated shadow generation; it does not guess the identity of, adopt, or delete a manifestless legacy companion.

If an upgrade needs to be reverted, run the emitted rollback command after a dry-run check:

```bash
hermes-scope-recall rollback --hermes-home "${HERMES_HOME:-$HOME/.hermes}" --backup-dir /path/to/backup/scope-recall --dry-run --json
hermes-scope-recall rollback --hermes-home "${HERMES_HOME:-$HOME/.hermes}" --backup-dir /path/to/backup/scope-recall --json
```

This standalone rollback command restores the plugin directory only. If `--activate` itself fails, use the structured `activation_transaction` result: automatic compensation has already attempted to restore plugin, Hermes config, provider config, and SQLite pre-state, with per-surface verification recorded in the receipt.

`verify --runtime` is read-only against `$HERMES_HOME/scope-recall/memory.sqlite3`: it loads the installed provider, checks layered install diagnostics, checks the compact tool schemas, and verifies the SQLite schema-migration ledger.

### Multi-profile rollout

For profile homes under `~/.hermes/profiles/*`, plan first and apply to an explicit subset or canary profile:

```bash
hermes-scope-recall rollout profiles --profiles-root "$HOME/.hermes/profiles" --plan --json
hermes-scope-recall rollout profiles --profiles-root "$HOME/.hermes/profiles" --profile default --apply --receipt /tmp/scope-recall-rollout-default.json
```

See [`docs/cross-profile-rollout.md`](docs/cross-profile-rollout.md) for the multi-profile safety model.

### Native-free vector fallback

If LanceDB/PyArrow native wheels are unsafe on the target CPU, install without extras and select the native-free backend instead:

```bash
python -m pip install hermes-scope-recall
hermes-scope-recall install --activate --hermes-home "${HERMES_HOME:-$HOME/.hermes}" --json
```

Then set `$HERMES_HOME/scope-recall/config.json`:

```json
{
  "vector": {
    "backend": "sqlite-bruteforce"
  }
}
```

### Development checkout

Use this editable install shape when developing the provider itself:

```bash
git clone https://github.com/410979729/scope-recall-hermes.git
cd scope-recall-hermes
python -m pip install -e ".[dev,lancedb]"
hermes-scope-recall install --activate --hermes-home /tmp/scope-recall-hermes-home --json
hermes-scope-recall verify --runtime --hermes-home /tmp/scope-recall-hermes-home --json
python -m pytest -q tests/test_installer.py tests/test_rollout_profiles.py
```

Plain `pytest` from an unrelated Python environment is not a valid compatibility check; use the Hermes venv and include the Hermes source on `PYTHONPATH` when you need to exercise Hermes discovery directly:

```bash
PYTHONPATH=/path/to/hermes-agent:$(pwd) /path/to/hermes-agent/venv/bin/python -m pytest -q
```

### Manual download / unpacked plugin install

Hermes plugin discovery expects an **unpacked plugin directory** named with the public provider spelling: `$HERMES_HOME/plugins/scope-recall/`. The Python distribution package is `hermes-scope-recall`, the Python import/package spelling remains `scope_recall`, and the Hermes provider name remains `scope-recall`; see [`docs/naming.md`](docs/naming.md) for the naming contract.

`scope-recall` V1 targets the current Hermes runtime line, which requires Python 3.11 or newer. If you download a release archive instead of cloning:

1. unpack it as `$HERMES_HOME/plugins/scope-recall/`
2. run `python -m pip install -e "$HERMES_HOME/plugins/scope-recall[lancedb]"` for the default LanceDB path, or install without extras and set `vector.backend: sqlite-bruteforce` on native-sensitive hosts
3. run `hermes-scope-recall install --activate --hermes-home "$HERMES_HOME" --json` to copy/activate/bootstrap the provider from the installed package
4. restart/reload the Hermes process that should use the provider
5. verify with `hermes-scope-recall verify --runtime --hermes-home "$HERMES_HOME" --json` and `hermes memory status`

Important boundary:

- `hermes-scope-recall install` copies the provider into `$HERMES_HOME/plugins/scope-recall/`; provider-owned data remains in `$HERMES_HOME/scope-recall/`.
- the configured vector companion is rebuildable from SQLite truth.
- manual vector repairs should be inspected first with `python scripts/repair.vector_index.py --hermes-home "$HERMES_HOME" --dry-run`; the script fails closed if the primary configured embedder is unavailable and would otherwise fall back to `vector.fallback_embedder`. Export the configured primary embedder environment variable (for example `SCOPE_RECALL_GEMINI_EMBEDDING_API_KEY`) or intentionally pass `--allow-fallback-embedder` before rebuilding with a fallback embedder.
- wheel build/import success is not enough by itself; release validation also runs Hermes-home installer and provider-discovery smokes.

---

## Configuration

The shipped `config.json` defaults to hybrid retrieval with a hosted OpenAI-compatible Gemini embedding path and a deterministic offline fallback. For the full machine-readable and operator-readable configuration reference, see [`docs/configuration.md`](docs/configuration.md).

Minimal default shape:

```json
{
  "auto_recall": true,
  "auto_capture": true,
  "enable_tools": true,
  "tool_schema_profile": "compact",
  "tool_schema_extra_tools": [],
  "maintenance_tools_enabled": false,
  "secret_index_tools_enabled": false,
  "fact_evolution": {
    "enabled": false,
    "mode": "preview"
  },
  "temporal_queries": {
    "enabled": false,
    "timezone": "UTC",
    "current_limit": 50
  },
  "reflection": {
    "enabled": false,
    "write_candidates": false,
    "max_hops": 1,
    "max_evidence": 24,
    "max_chars": 12000
  },
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
    "retention_profile": "balanced",
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

Journal retention has two independent controls:

- `retention_days` controls sanitized raw journal evidence. `0` keeps processed rows indefinitely; a positive value prunes processed rows only after that many days.
- `retention_profile` controls how much durable detail LLM extraction preserves: `light` keeps minimal stable facts, `balanced` keeps useful rationale and reusable steps, and `full` keeps detailed durable rationale, alternatives, corrections, ordered steps, and verification context.

`full` does not copy whole transcripts into recall rows or the vector index. Per-turn, journal, and nightly LLM candidates pass a deterministic source-overlap gate before durable writes; long exact or near-verbatim copies are rejected while short necessary quotations remain allowed. The raw journal remains the evidence source, and durable memories remain self-contained searchable summaries linked to cited message IDs.

When optional per-turn `capture_llm` extraction is enabled, Scope Recall redacts secret-like text and private filesystem paths again at that provider's network boundary. Truncated private-key blocks fail closed even when their closing marker falls beyond the bounded scan window. This defense does not depend on callers having already filtered the turn.

Vector backend choices:

- `lancedb` — default ANN companion, best for normal hosts; install with `python -m pip install -e ".[lancedb]"`. Scope Recall probes LanceDB/PyArrow in a child process before importing them in the Hermes process, so SIGILL/illegal-instruction wheels are treated as unavailable instead of crashing the agent.
- `sqlite-bruteforce` — pure-Python/SQLite companion for non-AVX CPUs or hosts where importing LanceDB/PyArrow is unsafe; install with `python -m pip install -e .` and set `vector.backend` accordingly, or keep the default `vector.fallback_backend: sqlite-bruteforce` so a provably fresh bootstrap can select it when LanceDB is absent or unsafe. An active generation never switches backend during startup.
- `pgvector` — optional PostgreSQL/pgvector companion for deployments that already operate PostgreSQL; install with `python -m pip install "hermes-scope-recall[pgvector]"` and configure `vector.pgvector.dsn_env` / `vector.pgvector.table_name`. See [`docs/vector-backends.md`](docs/vector-backends.md).

All vector backends are rebuildable caches. `$HERMES_HOME/scope-recall/memory.sqlite3` remains the truth source.

Credential rule:

- put real API keys in your private environment, not in `config.json`
- on a fresh setup with no active vector generation, if no primary key is available, `scope-recall` may establish the first generation with the explicitly configured `local-hash` fallback
- after a generation is active, its backend and embedding identity are pinned by the SQLite manifest; restoring a primary key does not silently switch spaces, and a different-space fallback cannot open an existing primary generation

### Embedding providers

Currently implemented:

| Provider | Use case | Notes |
| --- | --- | --- |
| `openai-compatible` | Gemini/OpenAI-compatible embedding APIs | Default configured path; supports env-based API key lookup |
| `openai` | Direct OpenAI embeddings | Useful when you do not need a custom compatible endpoint |
| `minimax` | MiniMax `embo-01` embeddings | Uses MiniMax's non-OpenAI-compatible `/v1/embeddings` shape with `texts` and `type`; indexing uses `db`, search queries use `query` |
| `sentence-transformers` | Local Hugging Face / SentenceTransformers models | Good for local semantic embeddings when installed |
| `local-hash` | Offline bootstrap/fallback | Deterministic portable fallback, not a true semantic model; changing to a semantic embedder requires an explicit generation migration |
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

`scope_recall_store_secret_index` stores a searchable credential index without putting plaintext secret values into the ordinary recall surface. Store the real password/token/API key/private key in an external vault or keyring, then store only the locator and safe metadata in `scope-recall`. To reduce the default tool schema footprint, this low-frequency tool is hidden and direct calls fail unless `secret_index_tools_enabled=true` is set.

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

This is deliberate. Mirroring curated memory writes into SQLite can leave stale duplicates after replace/remove operations. Live-reading curated memory keeps Scope Recall aligned with Hermes native memory behavior. Because those curated files are profile-global, live-read recall defaults to `single-user`: it is active for single-user/no-`user_id` runtimes and disabled for explicit gateway `user_id` contexts unless `curated_memory.mode` is set to `profile-global` or `explicit-users` with matching `allowed_user_ids`. Legacy `curated_memory.mode=shared` is still accepted as a deprecated alias for `profile-global`, but new configurations should use the canonical modes exposed by the config schema.

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
| `scripts/benchmark.golden.py` | Isolated golden recall benchmark using repository-owned fixtures |
| `scripts/benchmark.retrieval_regression.py` | Isolated synthetic Recall Funnel / retrieval-regression benchmark with configurable distractors |

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

### Durable shared recall

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

Default result sizing:

- `retrieval.candidate_pool` controls how many lexical/vector candidates each source may feed into the ranking funnel before merge, filters, graph/entity bonuses, and final slicing.
- `retrieval.top_k` controls the default tool result limit when a caller does not pass an explicit `limit`; explicit per-call `limit` still wins up to the bounded search maximum of `50`.

Recall observability:

- `scope_recall_search(..., include_trace=true)` returns the structured Recall Funnel for that query.
- `scope_recall_search(..., query_variants=[...])` deterministically fuses the primary query plus up to seven explicit variants. It reserves round-robin evidence slots per variant before global RRF fill, so one broad rewrite cannot erase a specialist hit; no LLM is called inside the provider. `evidence_diversity_depth=1..6` controls how many specialist hits each variant may protect: the backwards-compatible default is `3`, while broad multi-hop or open-domain evidence sets may opt into `4..6`.
- `scope_recall_explain` includes the same `funnel_trace` alongside rank-aligned scoring components and rejected candidates.
- `scope_recall_benchmark(..., include_trace=true, prompt_budget_chars=N)` reports per-case traces plus aggregate latency, known-answer recall, top-k accuracy, forbidden-id violations, filter counts, and prompt-budget hit rate.

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

- `journal_digest.last_status` / `journal_digest.consecutive_failures` — background digest health for operator monitoring
- `journal_digest.thread_alive` — whether a background digest worker is currently running
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

### Candidate memory promotion

`scope_recall_profile` defaults to `lifecycle=promoted` SQLite rows, so the compact profile behaves like a stable profile surface instead of mixing in ordinary candidate rows. Pass `include_candidates=true` only when an operator or debugging workflow intentionally wants non-hidden candidate rows in the profile payload. `include_general=true` remains the explicit switch for local `general` scratch rows.

Candidate rows are not left invisible forever. Use the read-only promotion planner first:

```bash
python scripts/promote.memory_candidates.py --hermes-home "$HERMES_HOME" --dry-run
```

After reviewing the plan, apply safe promotions:

```bash
python scripts/promote.memory_candidates.py --hermes-home "$HERMES_HOME" --apply
```

Rows classified as low-value noise are only archived when the operator also passes `--archive-noise`; otherwise they remain candidate/needs-review. Applied promotions and archives write `governance_audit_events` with before/after metadata and a batch id. `scripts/doctor.py` reports `runtime.memory_candidate_debt` so candidate backlog count, age, promotable rows, and archive candidates are visible before relying on promoted-only profile behavior.

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

Primary-agent default tools use `tool_schema_profile: "compact"` to keep the base request small:

```text
scope_recall_store
scope_recall_search
scope_recall_context
scope_recall_profile
scope_recall_memory
scope_recall_entity
```

The compact profile replaces several overlapping schemas with two dispatch tools:

- `scope_recall_memory(action=...)` covers `inspect`, `feedback`, `update`, `merge`, and `forget` by exact id.
- `scope_recall_entity(action=...)` covers `probe` and `related` entity graph reads.

Legacy individual tools remain direct-call compatible. To expose the pre-compact schema surface, set:

```json
{
  "tool_schema_profile": "standard"
}
```

To keep compact mode but expose selected diagnostics, use `tool_schema_extra_tools`:

```json
{
  "tool_schema_profile": "compact",
  "tool_schema_extra_tools": ["scope_recall_stats", "scope_recall_benchmark"]
}
```

Schema-surface targets after the compact-profile change:

- default compact: 6 tools, about 4.7 KB of JSON schema in repo-local measurement
- standard profile: 20 tools, about 10.6 KB
- maintenance/secret schema surfaces still require their explicit safety flags

Release `1.10.3` is the public patch on the last packaged `1.10.2` line:

- Governance coverage recognizes the exact official `memory_auto_adjudication` + `archive` receipt, so Doctor does not report a false missing-audit mutation after automatic adjudication.
- Default cleanup rollback recognizes the same exact event/action pair, restores only when the current row still matches the recorded after-snapshot, and refuses later state drift.
- Unknown writers do not gain governance or rollback authority merely by emitting a generic `archive` action.

Release `1.10.2` is the public release that incorporated the earlier untagged `1.10.0` and `1.10.1` source candidates:

- The verified online-backup cleanup fixture writes a simulated external staging replacement through an independent stdlib SQLite connection, so it does not enter this process's truth-connection hardening cache. Descriptor hardening and production runtime behavior are unchanged.
- Windows recovery-command test diagnostics decode CP936/GBK before permissive OEM fallback. This is a test-helper correction; production recovery command generation is unchanged.
- POSIX writable truth connections raw-open and fchmod-harden each live database identity at most once per process, so a later descriptor close cannot cancel same-process SQLite advisory locks. Identity replacement or permission drift after that cached event fails closed. An incompatible or foreign process-wide hardening marker fails closed and requires a process restart. Windows keeps inherited ACL behavior.
- Journal deferred-metric doctor fixtures are isolated from the default 72-hour backlog-age failure policy; production age checks are unchanged.

The `1.10.0` public source candidate remains the prior public source candidate since `1.9.2`:

- Journal source restore can plan and apply a trusted snapshot window with dry-run, epoch fencing, prewrite backup, operator ledger, and idempotent replay.
- Unresolved journal entries retry and quarantine on a bounded attempt budget, and high-volume sessions defer fairly so other sessions keep making progress (issues #45/#48/#46).
- Doctor reports a structured inventory of non-activatable inactive READY vector generations without treating healthy inactive READY rows as active health (#44).
- Provider and tooling share one assembled production command port; internal runtime modules stay behind thin entrypoints.
- Shutdown fails closed when a journal or capture worker does not acknowledge the deadline, keeping the writer lease and connections for a later retry.
- The unpublished `1.9.3` source interval remains in effect: one writer per truth store, read-only followers, digest model calls outside write transactions, and idle same-process peer recovery.

Release `1.9.3` was a source interval pushed to `main` and is incorporated above; it was not tagged or packaged:

- One process holds write authority for each SQLite truth store; additional gateway or CLI providers open as fail-closed read-only followers until the writer exits.
- Journal and nightly model calls run outside authoritative write transactions, so long network waits do not block vector retention or other SQLite writers.
- Initialization may recover one idle same-process dirty peer after an actual SQLite lock error, without rolling back active, cross-process, or read-only work.
- Lease ownership is atomic across threads, import aliases, Windows case variants, and junction paths, and every configuration/shutdown failure path releases its handle.
- Writer ownership and busy diagnostics are sanitized before operator-visible output.

Release `1.9.2` is the compatibility-preserving patch since `1.9.1`:

- Event-digest candidates trigger bounded vector outbox replay only after the SQLite transaction commits and the provider database lock is released.
- Live reconciliation checks SQLite readability through the provider-owned pager connection; raw header reads are restricted to explicitly quiesced offline paths so same-process POSIX locks are not canceled.
- Curated source and target priors rank relevant evidence but cannot manufacture matches for pure-noise queries.
- Journal failure receipts and optional vector-outbox retention recover from transient SQLite contention through bounded, observable retry paths.
- Doctor reports ambiguous placeholder-like database URI examples for review without treating them as confirmed credentials; canonical capture and durable-store filtering remain fail-closed.
- Explicit multi-query evidence-set retrieval preserves per-query rank provenance, reserves specialist evidence before global RRF fill, and allows an explicit Top-50 search without changing the compact default.
- CJK prose prefixes and unrelated Latin proper nouns no longer become premature hard scope mismatches before structured/shared entities are considered.
- A resumable isolated LoCoMo runner records source/config/data hashes, retrieval evidence, Recall@K, model/judge failures, and provider shutdown receipts without touching a live Hermes memory home.

Release `1.9.1` is the cumulative compatibility-preserving public release since `1.8.7`:

- Chinese lexical recall can build and validate a supplemental trigram generation without replacing the legacy index; activation is an explicit compare-and-swap, and rollback only changes the pointer.
- Two-character Chinese concepts use one bounded ranked fallback scan, while legacy English FTS/LIKE/alias candidates remain present and are protected by release regressions.
- Windows install, rollout, backup, and rollback use destination preflight, short backup roots, extended-length I/O, public-path receipts, and automatic compensation after final replacement failure.
- Hosted endpoint policy rejects credential-bearing URLs, unsafe redirects, plaintext transport without literal opt-in, and secret-bearing HTTP headers before memory data can leave the process.
- Doctor, migration receipts, clean-package checks, and strict invariants expose lexical integrity, endpoint safety, SQLite parameter bounds, and rollback readiness without silently repairing live state.

Release `1.8.7` is the cumulative compatibility-preserving public release since `1.8.2`:

- Non-CLI startup now fails closed before storage opens when no trusted principal is available; current recall/profile freshness comes from the SQLite authority projection rather than stale metadata.
- Calibrated vector-only thresholds, bounded completed-outbox retention, and explicit local-embedder readiness make retrieval and fresh fallback behavior measurable without changing an existing generation's embedding space.
- Lifecycle relation restore and exact-text deduplication fail closed on mismatched semantics, while Experience review/merge paths revalidate ownership and evidence before mutation.
- Canonical Unicode-aware secret filtering is shared by capture, durable storage, recall, operator output, transport errors, and release scanning.
- Windows installer, PID liveness, long-path, console, SQLite, and LanceDB paths are validated alongside pinned macOS/Linux release lanes.
- Fact Evolution, temporal current/as-of/history, bounded Reflection, stable tool names, SQLite truth authority, and governed durable/local scope routing remain compatible.

Release `1.8.6` is a compatibility-preserving reliability patch on the 1.8 line:

- Legacy freshness backfill isolates invalid validator metadata, continues valid rows, and re-scans under an immediate owner transaction; startup defers recoverable SQLite contention explicitly.
- Unicode-compatible sensitive keys are rejected, and HTTP/transport error redaction shares the canonical secret taxonomy.
- Freshness ranking penalties are explicit runtime configuration; operator recovery emits ASCII-safe JSON and stale-lease writes use the truth-connection boundary.
- Lifecycle rollback rejects unrelated relation endpoints, exact-text deduplication preserves distinct durable memory types, and the manual capture-LLM probe no longer runs as a pytest collection side effect.

Release `1.8.5` is a compatibility-preserving reliability patch on the 1.8 line:

- Windows activation-lease owner checks use a read-only process handle instead of `os.kill(pid, 0)`, whose zero signal is `CTRL_C_EVENT` on Windows.
- Every authoritative memory insert now initializes fact freshness transactionally; untracked legacy rows are visible, penalized, inventoried, and recoverable through dry-run-first tooling.
- Command and HTTP freshness validators are declarative and bounded, operator recovery requires verified backups and receipts, and release/CI checks fail closed across Windows and POSIX.
- Operator JSON remains parseable under Windows legacy console encodings, and owner-only truth-store permissions are enforced without weakening read-only doctor behavior.
- Local SentenceTransformers models are loaded before a fresh vector identity is committed. Load failures are visible in runtime statistics, and an existing generation can be opened only by an embedder with the same provider, model, prompt profile, and actual dimensions.
- `journal.retention_profile` adds `light`, `balanced`, and `full` semantic detail levels. Raw journal evidence remains separately governed by `retention_days`, and long transcript copies are rejected before ordinary recall/vector writes.
- New sessions can recall durable rows extracted in earlier sessions on their first turn, while local scratch remains chat/session scoped.
- Default compact tools compile under llama.cpp grammar conversion without deleting structured `freshness`, `claim`, or `evolution` capabilities; runtime length bounds remain enforced after unsafe nested schema repetitions are removed.
- Private-key redaction, fresh vector bootstrap compensation, SQLite sidecar ownership checks, package-member gates, and concurrency regressions were tightened during the release audit.

Release `1.8.1` is a compatibility-preserving patch on the 1.8.0 feature set:

- The dependency-free SQLite vector fallback now uses descriptor mode hardening only where CPython exposes it; Windows keeps its inherited ACL boundary instead of calling a Unix-only API.
- Windows activation-compensation tests close raw SQLite handles before file replacement, and clean build tests declare their no-isolation backend dependency explicitly.
- Explicit CJK entity regression coverage is deterministic with or without optional jieba.

The 1.8.0 feature contracts remain unchanged:

- Fact Evolution is opt-in and uses a closed `ADD`/`ENRICH`/`SUPERSEDE`/`RETRACT` action contract, deterministic evidence authority, reviewed mutation modes, and idempotent receipts.
- Bitemporal fact storage and `scope_recall_fact` provide scoped current, as-of, and history views with additive SQLite schema evolution and explicit valid-time/recorded-time semantics.
- `scope_recall_reflect` is opt-in, bounded, citation-grounded, provenance-root aware, and review-only unless every explicit candidate-write gate passes.
- Durable fact actions route `user`, `memory`, `project`, and `ops` to shared scope while every legacy and new `general` path remains local scratch.
- Nightly and journal integrations use stable source identity for replay safety; journal action receipts and source checkpoints commit atomically per candidate.
- Deterministic memory-evolution and reflection benchmarks, release identity checks, and package-content gates cover the new modules without changing existing ordinary-memory defaults.

Release `1.7.2` publishes compatibility-preserving storage, lifecycle, and governance hardening on the stable V1 line:

- One ordinary-recall lifecycle policy now governs journal/nightly matching, deduplication, retrieval, migration, and all vector mutation or replay paths.
- Immutable vector generations support inspectable shadow builds and explicit compare-and-swap activation; repair refuses unsafe in-place mutation of an active generation.
- Metadata keys and values, freshness validators, browser output, governance audit records, and external-import provenance are sanitized before reaching durable or operator-visible sinks.
- Candidate transitions use conflict checks and compare-and-swap safeguards, companion cleanup covers hidden lifecycle rows, and config/freshness updates fail closed on invalid or incomplete state.
- Folded inline data URLs are removed at capture and journal boundaries while preserving surrounding ordinary prose.

Release `1.7.1` publishes a compatibility-preserving patch on top of the 1.7.0 productization feature set:

- Runtime config diagnostics are filtered out before persisted operator config is written, while doctor/dashboard surfaces malformed config diagnostics for operators.
- Candidate browsing no longer resurfaces processed event-digest rows as active review candidates, and event-digest metadata redaction is JSON-safe for nested and non-JSON-like values.
- External shared-memory bridge documentation distinguishes read-only preview paths from audit-writing receipt paths.
- Hybrid/vector golden benchmark smoke coverage exercises semantic/vector recall paths in the release gate without external credentials.

Release `1.7.0` publishes the productization feature set while keeping the stable V1 runtime/API contract:

- Event-digest evidence packets and reviewable candidates add dry-run-first governance before extracted memories become durable recall rows.
- Read-only memory browsing, candidate review commands, and humanized explain output improve operator inspection without mutating SQLite truth by default.
- Experience-to-skill bridge helpers, optional PGVector companion support, vector backend documentation, and external shared-memory bridge contracts expand deployment choices while preserving SQLite as the truth source.
- `scope_recall_store` can now recover same-process peer-provider SQLite write locks by rolling back dirty peer transactions before retrying a recoverable `database is locked` write.

Release `1.6.3` publishes a focused SQLite lock-recovery patch for issue #25:

- `scope_recall_store` now treats SQLite lock/transaction failures as recoverable storage errors, rolls back/probes/reopens the provider connection when needed, retries the same store once, and returns `recovered=true` with `retry_count=1` when recovery handled the write.
- Non-SQLite failures remain non-retryable, so business-logic exceptions still surface while rollback guards release dirty SQLite transactions.
- The stable v1.6 runtime/API contract is unchanged; this patch does not introduce storage-schema or tool-surface changes.

Release `1.6.2` publishes a compatibility-preserving patch for graph relations and maintenance-tool hardening:

- Graph relation backfill, benchmarking, and `scope_recall_stats` density counters make explicit `supersedes` evidence easier to audit without exposing inaccessible or lifecycle-hidden peer rows.
- `scope_recall_playbook_review` write paths are inspect-only by default; operators must pass `dry_run=false` to promote, quarantine, supersede, review, or merge playbooks.
- `merge_playbooks()` repeated apply calls are idempotent for already-superseded sources, and LLM journal outputs filtered by quality gates are reported as `filtered_or_rejected` metadata rather than dead-letter errors.
- The stable v1.6 runtime/API contract is unchanged; this patch does not introduce storage-schema or tool-surface changes.

Release `1.6.1` publishes a customer-facing patch for documentation, packaging, and release provenance:

- GitHub tag, package metadata, wheel, sdist, and PyPI artifacts identify the same `1.6.1` source tree.
- Public documentation and packaged release metadata were cleaned up so shipped artifacts contain product documentation rather than internal planning material.
- The stable v1.6 runtime/API contract is unchanged; this patch does not introduce storage-schema or tool-surface changes.

Release `1.6.0` packages the compatibility-preserving refactor and audit hardening:

- `scripts/doctor.py` remains the operator CLI while implementation moves into focused `doctor_*` modules.
- `graph_hygiene.py`, `maintenance_ops.py`, `digest_run_results.py`, `recall_pipeline.py`, and `provider_schemas.py` centralize shared helpers without changing public tool APIs or SQLite truth semantics.
- Doctor runtime checks open the SQLite truth DB read-only and the doctor import fallback now catches only `ImportError`.

Release `1.5.3` adds lifecycle-governed profile hardening and memory-companion maintenance:

- `scope_recall_profile` defaults SQLite rows to explicit or legacy-promoted lifecycle rows; pass `include_candidates=true` to intentionally include non-hidden candidate rows.
- `scripts/promote.memory_candidates.py` is a dry-run-by-default promotion planner/apply path for safe ordinary candidates, with governance audit events for applied mutations and redacted review output.
- `scripts/repair.graph_hygiene.py` reports orphan/hidden-lifecycle graph companion rows and accepts explicit `--dry-run`; `--dry-run` wins over accidental `--apply`.
- `scripts/repair.vector_index.py` fails closed when the primary configured embedder is unavailable unless the operator intentionally passes `--allow-fallback-embedder`.

Release `1.5.2` adds Recall Funnel observability and retrieval-regression benchmarking:

- `scope_recall_search(include_trace=true)`, `scope_recall_explain`, and `scope_recall_benchmark(include_trace=true)` expose structured candidate-pool, per-stage, filter, timing, and returned-character traces for the active query.
- `scope_recall_benchmark` now returns aggregate quality metrics: latency percentiles, known-answer recall, top-k accuracy, forbidden-id violations, filter counts, and optional prompt-budget hit rate.
- `scripts/benchmark.retrieval_regression.py` runs an isolated synthetic benchmark with configurable distractor rows, `candidate_pool`, and `top_k`, so retrieval regressions can be reproduced without API keys or vector dependencies.
- `retrieval.top_k` controls the default tool result limit when no per-call `limit` is supplied.
- Release-audit hardening synchronizes `retrieval.top_k` defaults, serializes vector companion mutations, exposes background journal digest health in `scope_recall_stats`, and caches configured capture skip regexes.

Release `1.5.1` fixes strict release-gate handling for CI runtime scratch directories while preserving the 1.5.0 commercial-governance feature set.

Release `1.5.0` adds commercial governance and release-safety tooling:

- `scripts/benchmark.golden.py` runs in an isolated temporary Hermes home by default, copies the current plugin source for provider discovery, and treats `--overwrite-config` as an explicit maintenance-only danger flag with backup/restore protection.
- `scripts/check.release.py` now gates release readiness on golden benchmark success, dirty/untracked worktree visibility, wheel build/install smoke, doctor smoke, and secret/private-path scans.
- `scripts/governance.cleanup.py`, `scripts/journal.recovery.py`, and `scripts/report.dashboard.py` provide operator workflows for auditable cleanup, staged journal recovery, and release-health summaries without exposing raw SQL mutation as the normal path.
- Hard-delete forgetting now fails closed when no vector companion is provided, preventing SQLite truth deletion that could leave stale vector hits.

Release `1.4.5` tightened the audit/observability tools:

- `scope_recall_update` re-runs the deterministic conflict/relation review after a row changes, while preserving accumulated feedback counts, feedback-adjusted trust, conflict-review metadata, and higher existing importance scores.
- `scope_recall_explain` reports rank-aligned retrieval evidence for lexical/BM25/vector/RRF scores, metadata quality adjustment, entity overlap/distance bonuses, relation evidence/rerank contribution, memory-type temporal policy, temporal decay, recency bonus, threshold settings, and final score.
- `scope_recall_benchmark` still accepts simple `queries`, and also accepts assertion `cases` with `expected_ids`, `forbidden_ids`, `min_rank`, `min_top_score`, and `auto_explain_on_fail` for regression checks.
- `memory_type` now informs temporal decay policy: durable facts/preferences/procedures decay less aggressively than episodic or temporary/scratch evidence, and explain exposes the applied policy class/weight.
- `memory_relations` are surfaced in explain by default; relation-aware reranking remains feature-gated through `retrieval.relation_rerank_enabled` and is conservative by default.
- `shared_pool` remains read-only unless `shared_pool.write_enabled=true`; explicit shared-pool writes require `scope_mode="shared_pool"` and are limited to configured durable targets.
- Store scope is target-derived: `general` is local scratch, while `user`, `memory`, `project`, and `ops` are durable shared targets. An explicit `scope_mode` may select the canonical mode or an enabled shared pool, but cannot violate that target contract.
- Exact duplicate suppression remains automatic. Fuzzy/semantic merge is off by default; `semantic_merge=true` permits only conservative contained-text enrichment, while paraphrases and changed values remain separate for reviewed merge.
- Tool arguments are validated against the published schemas inside the provider before handlers run. Invalid calls return redacted `invalid_arguments`, `field`, and `constraint` keys without echoing the rejected value.

Operator-only maintenance tools are hidden from the default schema and require `maintenance_tools_enabled=true`:

```text
scope_recall_dedupe
scope_recall_govern
scope_recall_hygiene
scope_recall_repair
scope_recall_playbook_create
scope_recall_playbook_review
scope_recall_experience_promote
scope_recall_forgetting_report
scope_recall_forgetting_run
scope_recall_evolve
```

Secret-index tools are also hidden by default and require `secret_index_tools_enabled=true`:

```text
scope_recall_store_secret_index
```

`scope_recall_hygiene` is read-only. It reports runtime-wrapper noise, assistant scratch prose, duplicate dedupe keys, very short/long rows, `general` rows present in the vector companion, likely promotion candidates, and likely delete candidates. It does not delete, merge, promote, or rewrite rows.

For an offline SQLite report without exposing the maintenance tool to agents:

```bash
python scripts/report.hygiene.py --db "$HERMES_HOME/scope-recall/memory.sqlite3" --format markdown
```

Graph companion hygiene is checked by `scripts/doctor.py`. Orphan graph rows are safe to remove because `memory_entities` and `memory_relations` are rebuildable companions over SQLite `memories` truth:

```bash
# read-only dry run
python scripts/repair.graph_hygiene.py --hermes-home "$HERMES_HOME" --dry-run

# apply only after reviewing the dry-run counts
python scripts/repair.graph_hygiene.py --hermes-home "$HERMES_HOME" --apply
```

Lexical FTS health is also lifecycle-aware. `doctor` reports total truth rows separately from expected ordinary-recall members and fails on missing, stale, duplicate, or hidden FTS rows. The repair command is read-only by default. Apply only after stopping normal Scope Recall writers; it creates an owner-only SQLite online backup and verifies `quick_check` before rebuilding the companion in one transaction:

```bash
# read-only report
python scripts/repair.fts_index.py --hermes-home "$HERMES_HOME" --dry-run

# reviewed maintenance window only
python scripts/repair.fts_index.py --hermes-home "$HERMES_HOME" \
  --apply --maintenance-confirmed
```

Entity graph read surfaces also hide lifecycle-removed memories (`archived`, `superseded`, `obsolete`, `rejected`) and filter common tool-trace entity noise such as `read_file`, `search_files`, `execute_code`, `skill_view`, and `session_search`.

Destructive cleanup is intentionally out-of-band: use the hygiene report first, then require an explicit operator decision before running any separate delete/merge/dedupe action. The shipped hygiene path is dry-run/report-only.

`scope_recall_export` defaults to the current accessible scope set: local scratch scope plus shared durable scope. Passing `scope_only=false` remains an operator maintenance action and fails closed unless `maintenance_tools_enabled=true`.

Temporal facts and reflection are opt-in product surfaces. `temporal_queries.enabled=true` exposes the read-only `scope_recall_fact` current/as-of/history views. `reflection.enabled=true` exposes `scope_recall_reflect`, which gathers a bounded, scope-filtered evidence pack and accepts only strict JSON synthesis whose citations resolve inside that pack. Before any durable review candidate is built, each answer and observation must also match a cited content clause's polarity, argument order, temporal/modal markers, conditionals, and quantifiers; lexical token coverage alone is insufficient. Reflection is read-only by default. A `mental_model` candidate can be stored only when the caller explicitly sets `propose_memory=true`, maintenance tools are enabled, `reflection.write_candidates=true`, and citation/source/confidence thresholds pass; the result remains hidden as `needs_review` rather than becoming active memory automatically.

```text
scope_recall_fact(action="current", subject="project-alpha", predicate="workplace")
scope_recall_fact(action="as_of", subject="project-alpha", predicate="workplace", at="2026-01-01T00:00:00Z")
scope_recall_reflect(query="How did Project Alpha's deployment assumptions change?", include_trace=true)
scope_recall_reflect(query="Summarize the reviewed architecture model", propose_memory=true)
```

Run the deterministic temporal/reflection release benchmarks and the read-only runtime dashboard with:

```bash
python scripts/benchmark.memory_evolution.py --json
python scripts/benchmark.reflection.py --json
python scripts/doctor.py --json --hermes-home "$HERMES_HOME"
```
Keep `propose_memory` omitted or `false` for ordinary reflection. Enabling candidate writeback is an operator decision and still produces a review queue item, not an active fact.

`scope_recall_stats`, `scope_recall_export`, `scope_recall_explain`, `scope_recall_benchmark`, and Experience Kernel tools still work through direct tool calls, but are no longer part of the compact default schema unless `tool_schema_profile="standard"` or `tool_schema_extra_tools` exposes them.

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

# Inspect/update by exact id through the compact memory dispatcher.
inspected = scope_recall_memory(action="inspect", id=store["id"])

# Probe entity memory through the compact entity dispatcher.
entity = scope_recall_entity(action="probe", entity="this project", limit=3)
```

Example `scope_recall_stats` shape:

```json
{
  "provider": "scope-recall",
  "total_memories": 42,
  "scope_memories": 7,
  "local_scope_memories": 3,
  "shared_scope_memories": 4,
  "shared_pool_scope_memories": 0,
  "shared_pool": {
    "enabled": false,
    "write_enabled": false,
    "pool_id": "",
    "scope_id": "",
    "memories": 0
  },
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
| `scope_recall_store` | Compact default: store a provider-owned memory row after deterministic governance checks |
| `scope_recall_search` | Compact default: search local scratch plus shared durable scope; optional `query_variants` enables deterministic multi-query evidence fusion, `evidence_diversity_depth=1..6` tunes specialist-hit protection (default `3`), and `include_trace=true` returns both funnel and evidence-set provenance |
| `scope_recall_context` | Compact default: render a task-relevant memory context block plus structured evidence |
| `scope_recall_profile` | Compact default: render a bounded high-level profile/context surface |
| `scope_recall_memory` | Compact default: dispatch exact-id `inspect`, `feedback`, `update`, `merge`, and `forget` operations |
| `scope_recall_entity` | Compact default: dispatch entity `probe` and `related` reads |
| `scope_recall_probe` | Standard profile/direct-call: inspect accessible memories attached to a specific entity |
| `scope_recall_related` | Standard profile/direct-call: list entities that co-occur with a given entity |
| `scope_recall_feedback` | Standard profile/direct-call: mark a memory helpful/unhelpful for trust scoring |
| `scope_recall_forget` | Standard profile/direct-call: delete memories by exact id/ids within the current accessible scope set |
| `scope_recall_update` | Standard profile/direct-call: replace content/category within the current accessible scope set |
| `scope_recall_merge` | Standard profile/direct-call: merge same-scope memories into a target row |
| `scope_recall_export` | Standard profile/direct-call: export SQLite truth rows as JSON/JSONL; `scope_only=false` is maintenance-gated |
| `scope_recall_explain` | Standard profile/direct-call: explain rank-aligned retrieval evidence and Recall Funnel trace |
| `scope_recall_benchmark` | Standard profile/direct-call: run latency/assertion/quality-regression checks |
| `scope_recall_stats` | Standard profile/direct-call: inspect storage, retrieval, scope, and vector health |
| `scope_recall_dedupe` | Operator-only: inspect or collapse exact duplicate rows |
| `scope_recall_govern` | Operator-only: review tier distribution and decay/archive candidates |
| `scope_recall_hygiene` | Operator-only, read-only: report memory-quality cleanup/promotion candidates without modifying rows |
| `scope_recall_repair` | Operator-only: repair/rebuild the configured vector companion from SQLite truth |
| `scope_recall_fact` | Opt-in, read-only: query scoped current, as-of, or cited fact history views |
| `scope_recall_reflect` | Opt-in, read-only by default: synthesize bounded cross-memory observations and inferences with evidence-pack citations |
| `scope_recall_evolve` | Operator-only: preview or request locally authorized fact evolution; defaults to dry-run and cannot elevate configured policy |

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

This Hermes implementation is not a feature-parity target for the OpenClaw sibling. Each runtime should evolve toward the best native memory plugin for its own host platform; the OpenClaw path here remains an explicit migration/import boundary, not a forced alignment contract.

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

### Automatic Experience promotion does not run

Background Experience promotion is opt-in. Set `experience.auto_promotion_enabled=true` in `$HERMES_HOME/scope-recall/config.json`, then restart or reload the Hermes process so the provider reads the new runtime config. Automatic promotion still requires evidence-backed task episodes and verification gates; high-risk playbooks stay in review instead of being promoted silently.

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
| [`docs/operator-runbook.md`](docs/operator-runbook.md) | Operator runbook: install/upgrade/rollback, health checks, journal drains, candidate review, vector repair, cleanup, backup/restore, release, and cross-profile rollout |
| [`docs/install.md`](docs/install.md) | Installation, verification, rollback, multi-profile rollout, native-free fallback, and optional PGVector setup |
| [`docs/configuration.md`](docs/configuration.md) | Complete configuration key registry with defaults, choices, risk, and restart guidance |
| [`docs/vector-backends.md`](docs/vector-backends.md) | LanceDB, sqlite-bruteforce, and optional PGVector companion behavior and repair notes |
| [`docs/event-digest.md`](docs/event-digest.md) | Event evidence packets, candidate write rollout, and read-only doctor visibility |
| [`docs/governance-ui.md`](docs/governance-ui.md) | Read-only memory browser and dry-run-first candidate review commands |
| [`docs/skill-bridge.md`](docs/skill-bridge.md) | Experience-to-skill candidate bridge and review boundaries |
| [`docs/external-shared-memory.md`](docs/external-shared-memory.md) | Backend-neutral shared-memory export contract and optional PostgreSQL adapter |
| [`docs/naming.md`](docs/naming.md) | Public `scope-recall` vs Python/tool `scope_recall` naming contract |
| [`docs/upstream-recommendation.md`](docs/upstream-recommendation.md) | Public upstream recommendation positioning for standalone-provider visibility |
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
- Recall Funnel traces for search/explain/benchmark, including stage counts, filter counts, timings, returned ids, and returned character budget evidence
- synthetic retrieval regression with configurable distractor rows via `scripts/benchmark.retrieval_regression.py`
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
