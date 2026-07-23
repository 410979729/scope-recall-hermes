# Scope Recall 1.8.0 Release Readiness

Date: 2026-07-15

This public maintainer note records the product-level release requirements for the `1.8.0` source tree. It deliberately excludes deployment-specific runtime counters, local paths, credentials, and private validation context. Customer-facing change details live in `CHANGELOG.md`, the GitHub Release, and PyPI metadata.

## Code gate status

- Package/plugin version: `1.8.0`.
- Release class: compatibility-preserving minor release on the stable V1 line.
- SQLite remains the truth source; vector generations, relation indexes, reflection output, and other companions remain derived or reviewable state.
- Publication requires the strict `python3 scripts/check.release.py` gate on a clean source tree.

## Covered release areas

The release verification covers these public product contracts:

- Fact Evolution is opt-in and uses a closed action contract, deterministic evidence policy, trusted first-person subject binding, polarity-aware claim support, reviewed mutation modes, and idempotent receipts.
- Temporal facts preserve current, as-of, and history views with additive SQLite schema evolution and explicit valid-time and recorded-time semantics.
- Reflection is opt-in, bounded, citation-grounded, proposition-structure and polarity checked, provenance-root aware, and review-only unless every candidate-write gate passes.
- Durable `user`, `memory`, `project`, and `ops` fact actions resolve to the shared durable scope, while `general` remains local scratch on every integration path.
- Nightly and journal prompts expose real message IDs, bind evidence to the current chunk, checkpoint exact cited entry IDs, keep failed chunks pending, enforce a global exposed-character budget, preserve stable replay identity, and commit journal action receipts with source checkpoints atomically per candidate.
- Existing V1 provider identity, stable tool names, ordinary-memory defaults, SQLite authority, and rebuildable companion boundaries remain compatible.
- Activation against an existing truth DB requires explicit maintenance confirmation plus a SQLite writer-lock preflight. The installer snapshots plugin/config/provider-config state (including symlink identity plus dereferenced target bytes/mode), performs a verified SQLite online backup before replacement, rejects ambiguous YAML, writes supported YAML atomically, and records discard/rebuild receipts for vector companions changed during compensation.
- Public tool evidence remains non-authoritative without a runtime-owned source registry; RETRACT binds to the ledger-owned target claim, and future/finite writes that the current static lifecycle cannot represent fail closed without durable writes.
- Deterministic memory-evolution and Reflection v3 benchmarks verify scope routing, evidence authority/polarity/subject binding/argument order, chunk provenance, global exposure budgets, provenance diversity, replay safety, proposition reversal rejection, unsupported-output rejection, and journal checkpoint atomicity. A named RB-1 through RB-8 stage separately blocks source forgery, unrelated retraction, temporal gaps, historical-slot pollution, missing effective-time closure, unsafe activation compensation, and lossy YAML writes. Reflection v3 requires eight valid responses plus six adversarial and six matching proposition probes. The aggregate also builds fixed 100k and 1M temporal ledgers, verifies bounded overflow/index plans/zero-write reads, and enforces p50/p95/p99 thresholds including the memory-filtered current path.
- Release identity, package contents, public-document hygiene, source/wheel parity, and clean-environment install/import checks remain part of the release gate.

## Runtime evidence policy

Owner: maintainers.

Runtime health is deployment-specific and is not embedded in this public source document. Operators may supply an explicit dashboard payload to local release tooling when they need an environment-specific check; those results remain outside the tagged package documentation.

Clearance condition:

- The strict clean-tree release gate passes for the exact candidate source tree.
- The release commit, tag, GitHub Release assets, and PyPI artifacts all identify version `1.8.0`.
- CI completes successfully for the release commit and tag.
- A clean environment installs the published wheel and verifies the package version, provider import, operator CLI, and packaged Fact Evolution, temporal-query, and Reflection modules.
