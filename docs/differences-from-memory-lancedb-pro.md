# Differences from OpenClaw `memory-lancedb-pro`

`scope-recall` borrows several good ideas from OpenClaw `memory-lancedb-pro`, but it is not a line-for-line port and it does not claim runtime parity.

## What was inherited

- current-turn-first retrieval mindset
- strong scope isolation as a first-class design concern
- conservative greeting/noise gating
- bounded recall budgets
- hybrid lexical + vector retrieval as the long-term direction

## What is intentionally different

### 1. Truth layer

OpenClaw `memory-lancedb-pro` is fundamentally organized around its own LanceDB-centric memory pipeline.

`scope-recall` instead uses:

- SQLite as the authoritative local truth layer
- LanceDB only as a retrieval companion index

This is deliberate so local auditing, backup, migration, and debugging remain simple.

### 2. Curated memory authority boundary

In Hermes, built-in curated memory files are already authoritative:

- `$HERMES_HOME/memories/USER.md`
- `$HERMES_HOME/memories/MEMORY.md`

`scope-recall` reads those files live during recall and does not mirror them into SQLite.

That differs from a design where every memory layer is collapsed into one backend-owned store.

### 3. Scope model

OpenClaw upstream scope fields look like a single `scope` string in the observed LanceDB table.

`scope-recall` expands runtime isolation around Hermes realities:

- `platform`
- `agent_workspace`
- `agent_identity`
- `user_id`
- `gateway_session_key` when available
- otherwise `chat_id`
- plus `thread_id`

This was necessary to avoid cross-group/topic memory bleed in gateway deployments.

### 4. Default embedding path

Some OpenClaw `memory-lancedb-pro` stores use 3072-dimensional embedding vectors.

`scope-recall` now ships with a configured default that targets a 3072-dimensional Gemini OpenAI-compatible embedding endpoint, while still retaining an offline `local-hash` fallback when that API path is unavailable.

That means the release posture is now:

- higher-quality default configuration for real deployments
- degraded offline bootstrap still available through the fallback path

### 5. Migration philosophy

OpenClaw history reuse is treated as an **explicit import problem**, not transparent compatibility.

`scope-recall` only auto-migrates its own prior local `lancepro` SQLite/config state.

## Remaining gaps vs OpenClaw `memory-lancedb-pro`

`scope-recall` now implements a practical deterministic governance layer:

- write-time exact deduplication by normalized content within `(scope_id, target)`
- conservative semantic near-duplicate merge for `user`, `ops`, and `project` memories
- conflict preservation when near-duplicate memories contain negation / supersession language
- rules-based smart extraction from user turns into preference / ops / project fact candidates
- metadata classification for category, tier, confidence, sensitivity, and expiry review
- `scope_recall_forget`
- `scope_recall_update`
- `scope_recall_dedupe`
- `scope_recall_merge`
- `scope_recall_export`
- `scope_recall_govern`
- `scope_recall_repair`
- LanceDB vector-search failure degradation to lexical recall with vector status marked `needs_repair`

It still does not claim full OpenClaw `memory-lancedb-pro` parity. The remaining differences are:

- extraction / merge is deterministic and conservative, not an LLM-backed created/merged/skipped classifier
- semantic merge is score/rule-based, not a general contradiction resolver
- tier governance currently classifies and reports decay/archive candidates, but does not run a full summarization / promotion / compression pipeline
- no automatic reuse of old OpenClaw LanceDB stores without explicit transformation

## Honest claim boundary

The correct way to describe `scope-recall` today is:

> A Hermes local memory provider for current-turn recall with SQLite truth storage, LanceDB vector companion retrieval, strong runtime scope isolation, deterministic write-time governance, and explicit migration boundaries.

It should **not** be described as:

- a drop-in replacement for OpenClaw `memory-lancedb-pro`
- a direct reuse wrapper around old `.lance` stores
- full feature parity with the upstream governance stack
