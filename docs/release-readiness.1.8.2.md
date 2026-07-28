# Scope Recall 1.8.2 Release Readiness

Date: 2026-07-28

This public maintainer note records the product-level release requirements for the `1.8.2` source tree. It excludes deployment-specific runtime counters, local paths, credentials, and private validation context. Customer-facing change details live in `CHANGELOG.md`, the GitHub Release, and PyPI metadata.

## Code gate status

- Package/plugin version: `1.8.2`.
- Release class: compatibility-preserving reliability patch on the stable V1 line.
- SQLite remains the truth source; vector generations, relation indexes, journal summaries, and other companions remain derived or reviewable state.
- Publication requires the strict `python3 scripts/check.release.py` gate on a clean source tree.

## Patch scope

### Embedding readiness and fallback

- Local SentenceTransformers models are loaded before a fresh vector-generation identity is committed.
- Model-load failures are sanitized and surfaced through runtime vector status instead of being mistaken for availability.
- A fresh, empty deployment may select an explicitly configured fallback after the primary fails readiness.
- An active generation remains bound to its backend, provider, model, dimensions, metric, prompt profile, and prefixes. A different embedding space cannot open it as a fallback.
- Concurrent model loads share one success or sanitized failure per cohort, while later cohorts may retry after a failed load.

### Retention and first-turn recall

- `journal.retention_profile` provides `light`, `balanced`, and `full` semantic detail levels for immediate and journal LLM extraction.
- Raw journal evidence remains independently governed by `journal.retention_days`; `full` does not duplicate whole transcripts into ordinary recall rows or the vector companion.
- Per-turn, journal, and nightly candidate admission rejects long exact or near-verbatim source copies while allowing short necessary quotations.
- A newly initialized session can retrieve durable rows extracted in an earlier session on its first turn, while local `general` scratch remains session/chat scoped.

### LM Studio and llama.cpp schema compatibility

- Unsafe nested long-string `maxLength` repetitions are omitted from LLM-facing fact schemas because affected llama.cpp versions expand them into unparseable grammar.
- Runtime fact tooling still enforces the corresponding content and fact-value length boundaries.
- Structured `freshness`, `claim`, and `evolution` capabilities remain exposed; this preserves the compatibility direction explored in pull request #30 without narrowing the public tool contract.
- Free-form object nodes explicitly declare `additionalProperties: true` for compatibility with older converter variants.
- Deterministic schema tests cover every public Scope Recall schema, and maintainer validation uses the upstream C++ JSON-schema converter and grammar parser for the default compact tool set and its combined dispatch shape.

### Safety and packaging hardening

- Optional capture-LLM network payloads are sanitized again at the outbound boundary, including fail-closed handling of truncated PEM/PGP private-key blocks and private filesystem paths.
- Fresh vector bootstrap serializes physical creation with manifest publication and compensates only newly created, proven-empty local companions.
- SQLite main, WAL, SHM, and rollback-journal files share one ownership/presence boundary during bootstrap compensation.
- Wheel and source-distribution gates require every newly added runtime module and reject missing, secret-bearing, private-path, cache, or forbidden members.

## Preserved release contracts

- Fact Evolution remains opt-in with its closed action contract, deterministic evidence policy, reviewed mutation modes, and idempotent receipts.
- Temporal facts preserve current, as-of, and history views with the existing additive SQLite schema and valid-time/recorded-time semantics.
- Reflection remains opt-in, bounded, citation-grounded, provenance-root aware, and review-only unless every candidate-write gate passes.
- Durable `user`, `memory`, `project`, and `ops` facts resolve to shared durable scope, while `general` remains local scratch.
- Existing V1 provider identity, stable tool names, ordinary-memory defaults, SQLite authority, and rebuildable companion boundaries remain compatible.

## Runtime evidence policy

Owner: maintainers.

Runtime health is deployment-specific and is not embedded in this public source document. Operators may supply an explicit dashboard payload to local release tooling when they need an environment-specific check; those results remain outside tagged package documentation.

## Clearance condition

Clearance condition: every item below is mandatory before public artifact publication:

- The strict clean-tree release gate passes for the exact candidate source tree.
- The release commit, tag, GitHub Release assets, and PyPI artifacts all identify version `1.8.2`.
- CI completes successfully for the release commit and tag.
- A clean environment installs the published wheel and verifies package version, provider import, operator CLI, dependency-free fallback, and packaged Fact Evolution, temporal-query, Reflection, retention-profile, transcript-overlap, and schema-compatibility modules.
