# Scope Recall 1.8.7 Release Readiness

Date: 2026-08-03

This public maintainer note records the product-level release requirements for the cumulative `1.8.7` source tree since the last public release, `1.8.2`. It excludes environment-specific health counters, filesystem locations, credentials, and private validation context. No tag, GitHub Release, or PyPI publication is implied by this document.

## Code gate status

- Package/plugin version: `1.8.7`.
- Release class: cumulative compatibility-preserving reliability patch on the stable V1 line.
- SQLite remains the authoritative truth source; vector, graph, journal, and receipt files remain derived, staged, or mirrored state as documented.
- Publication requires the strict release gate against the exact clean candidate tree and the exact annotated release tag.

## Cumulative patch scope

### Runtime identity and scope isolation

- Non-CLI provider initialization requires a trusted runtime principal and fails closed before opening SQLite, registering tools, starting background workers, or injecting recall when that principal is absent.
- Durable `user`, `memory`, `project`, and `ops` targets remain governed shared scope; `general` remains local scratch.
- Canonical chat identity mapping, scope visibility, and principal ownership remain explicit rather than inferred from display names or transport-local labels.

### Freshness and current-state recall

- Runtime recall and profile output project fact freshness from the authoritative SQLite companion row rather than stale metadata declarations.
- Legacy rows with obsolete or invalid validator metadata are isolated as manual live-check debt instead of aborting a bounded backfill.
- Apply mode re-scans authoritative candidates under an immediate owner transaction, and recoverable startup contention is deferred without hiding corruption.
- Current-state and temporal ranking avoid treating historical or merely normative facts as verified present state.

### Vector, relation, and Experience integrity

- Existing vector generations can be opened only by an embedder with the same provider, model, prompt profile, actual dimensions, backend, and generation identity.
- Fresh fallback and native-free SQLite vector bootstrap remain explicit and observable; incomplete generations are never activated as current state.
- Relation restore, lifecycle cleanup, graph-frequency companions, and rebuild queues preserve transaction ownership and reject unrelated endpoints.
- Experience promotion, replay, merge, and lifecycle mutation remain evidence-gated, provenance-aware, idempotent, and reviewable.

### Input, secret, and operator-output safety

- Capture, durable writes, recall, doctor output, transport errors, structured mapping keys, and release scans share one Unicode-aware secret taxonomy.
- Private-key blocks, cookies, tokens, database credentials, and secret-like structured fields are rejected or redacted before durable and operator-visible sinks.
- Operator recovery remains dry-run-first, bounded, receipt-backed, and safe under legacy Windows console encodings.

### Cross-platform and release integrity

- Windows PID liveness, installer replacement and rollback, long paths, SQLite replacement, FTS recovery, LanceDB backup, and console behavior avoid Unix-only assumptions.
- Linux, Windows, macOS, installer, optional-native-dependency, and strict release lanes use the reviewed pinned Hermes compatibility source.
- Third-party GitHub Actions are pinned to reviewed immutable commits.
- Build jobs have no publish credential; publish jobs download reviewed artifacts without checking out, installing, or executing repository source.
- The release tag, GitHub Release, wheel, sdist, installed metadata, and PyPI project version must agree exactly.

## Preserved release contracts

- The stable `scope-recall` provider ID, public tool names, V1 SQLite authority, and rebuildable companion boundaries do not change.
- Fact Evolution remains opt-in with a closed action contract and evidence-gated reviewed mutation.
- Temporal current/as-of/history queries and bounded citation-grounded Reflection remain compatible.
- Scope routing, evidence authority, provenance-root validation, idempotency, journal checkpoint ownership, and release-identity checks remain fail-closed.
- Existing ordinary-memory behavior remains the default; optional PGVector and legacy-import integrations remain optional.

## Runtime evidence policy

Owner: maintainers.

Runtime health is environment-specific and is not embedded in this public source document. Operators may supply an explicit dashboard payload to local release tooling when they need an environment-specific check; those results remain outside tagged package documentation.

## Clearance condition

Clearance condition: every item below is mandatory before public artifact publication:

- Ruff, Pyright, the complete pytest suite, release invariants, benchmarks, and source/artifact scans pass.
- Wheel and sdist contents import successfully and install into a clean environment outside the source tree.
- Installed-plugin activation and canonical doctor smoke checks pass on supported Windows and POSIX CI lanes.
- The exact release commit passes all required main-branch CI jobs.
- A local annotated `v1.8.7` tag passes the strict tagged-release gate before the tag is pushed.
- The release commit, tag, GitHub Release assets, and PyPI artifacts all identify version `1.8.7`.
- Public API readback and clean-environment installation confirm cross-channel version and artifact identity after publication.
