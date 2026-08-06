# Scope Recall 1.9.0 Release Readiness

Date: 2026-08-06

This public maintainer note records the product-level release requirements for the cumulative `1.9.0` source tree since the last public release, `1.8.7`. It excludes environment-specific health counters, filesystem locations, credentials, and private validation context. No tag, GitHub Release, or PyPI publication is implied by this document.

## Code gate status

- Package/plugin version: `1.9.0`.
- Release class: compatibility-preserving minor release on the stable V1 provider and tool contract.
- SQLite remains the authoritative truth source; lexical/vector/graph companions and filesystem receipts remain rebuildable, staged, or mirrored state as documented.
- Publication requires the strict release gate against the exact clean candidate tree and the exact annotated release tag.

## Minor-release scope

### CJK lexical shadow generation

- Schema migration `0012_lexical_shadow_index_v1_9_0` adds only generation metadata and an activation pointer during ordinary initialization.
- The supplemental trigram index is created only by an explicit backup-first maintenance command. Backfill commits bounded rowid pages and resumes from a durable watermark.
- Truth-table triggers keep visible insert, update, lifecycle, and delete changes synchronized while the shadow is building, ready, or active.
- Quality review requires the fixed high-interference Chinese fixture, bounded live CJK samples, exact integrity counts, and zero removal of legacy English candidates.
- Activation uses an explicit current-generation compare-and-swap. Rollback changes only the pointer and retains both legacy and shadow index storage.
- Two-character Chinese concepts use one bounded ranked fallback scan because SQLite trigram FTS cannot represent them. A single generic bigram is insufficient final relevance evidence.
- The release gate runs a fixed 2,000-row, 20-round benchmark and enforces CJK 3/3 discovery, zero English candidate removal, requested-limit compliance, bounded p95 latency, and bounded SQLite page growth.

### Windows install, rollout, and rollback

- Copy operations preflight the complete destination tree before mutation, including component and extended-path budgets.
- Windows I/O uses an internal extended-length path representation while operator receipts retain ordinary public paths.
- Installer and cross-profile rollout share one filesystem primitive layer and short, collision-resistant backup/staging roots.
- Partial copies are removed on failure. A failed final rollback replacement automatically restores the just-created current-plugin backup or reports an explicit compensation failure.
- Historical rollout receipts under the previous backup root remain valid.

### Endpoint and SQLite boundary safety

- Capture, journal/nightly, reflection, OpenAI-compatible embedding, and MiniMax embedding share one endpoint and redirect policy.
- Credential-bearing URL components, unsafe encodings, fragments, cross-origin redirects, HTTPS downgrade, and non-boolean plaintext opt-ins fail closed.
- Loopback HTTP strips authorization, API-key, cookie, proxy, and registered credential-like headers while preserving reviewed non-credential controls.
- Doctor and transport errors expose only an origin or recognized public API suffix, not private path/query material.
- Public memory-ID collections remain bounded and validated before database access; large accepted operations are chunked under the live SQLite parameter limit without partial transaction commits.

## Preserved release contracts

- The stable `scope-recall` provider ID, public tool names, V1 SQLite authority, and existing ordinary-memory defaults do not change.
- Legacy lexical FTS/LIKE/alias candidates remain in the active read union and are never replaced in place by the CJK companion.
- Fact Evolution remains opt-in with a closed action contract and evidence-gated reviewed mutation.
- Temporal current/as-of/history queries and bounded citation-grounded Reflection remain compatible.
- Durable `user`, `memory`, `project`, and `ops` targets remain governed shared scope; `general` remains local scratch.
- Optional vector backends, PGVector, and legacy import paths remain optional rather than runtime dependencies.

## Runtime evidence policy

Owner: maintainers.

Runtime health is environment-specific and is not embedded in this public source document. Operators may supply explicit dashboard and migration receipts to local release tooling when they need an environment-specific check; those results remain outside tagged package documentation.

## Clearance condition

Clearance condition: every mandatory source, artifact, clean-tree, tagged-identity, and publication check below must pass on the exact release commit.

Every item below is mandatory before public artifact publication:

- Ruff, Pyright, the complete pytest suite, strict release invariants, benchmarks, and source/artifact scans pass.
- Wheel and sdist contents import successfully and install into a clean environment outside the source tree.
- Windows extended-path install/rollback, lexical build/activate/doctor/rollback, and installed-plugin smoke checks pass against the release artifact.
- The exact release commit passes all required main-branch CI jobs.
- A local annotated `v1.9.0` tag passes the strict tagged-release gate before the tag is pushed.
- The release commit, tag, GitHub Release assets, installed metadata, and PyPI artifacts all identify version `1.9.0`.
- Public API readback and clean-environment installation confirm cross-channel version and artifact identity after publication.
