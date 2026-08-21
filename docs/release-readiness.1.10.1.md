# Scope Recall 1.10.1 Release Readiness

Date: 2026-08-20

Owner: maintainers.

This public maintainer note records the source-candidate requirements for the `1.10.1` public tree since the last tagged and packaged public release, `1.9.2`. It incorporates and supersedes the untagged `1.10.0` public source candidate that reached `main` without a tag, GitHub Release, or PyPI artifact. This document excludes environment-specific health counters, filesystem locations, credentials, and private validation context. No commit, tag, GitHub Release, PyPI publication, deployment, or live migration is implied by this document.

## Candidate scope

- `1.10.1` carries forward the complete `1.10.0` feature and compatibility scope documented in [`release-readiness.1.10.0.md`](release-readiness.1.10.0.md).
- POSIX writable truth connections raw-open and fchmod-harden each live database identity at most once per process so a later descriptor close cannot cancel same-process SQLite advisory locks. Identity replacement or permission drift after that cached event fails closed. An incompatible or foreign process-wide hardening marker fails closed and requires a process restart. Windows inherited-ACL behavior is unchanged.
- Journal deferred-metric and pending-retryable doctor fixtures are isolated from the default 72-hour backlog-age failure policy; production age checks are unchanged.
- Runtime doctor behavior, provider/tool names, SQLite authority, and public APIs are otherwise unchanged.

## Release identity

- Package/plugin version: `1.10.1`.
- Public release baseline: `1.9.2`.
- Expected annotated release tag: `v1.10.1` (not created by this source-candidate task).
- Published `1.9.2` source, tag, and package artifacts remain immutable.
- The `1.10.0` public source candidate was pushed to `main` without a tag, GitHub Release, or PyPI artifact; `1.10.1` supersedes those source bytes.

## Runtime evidence policy

Runtime health is environment-specific and is not embedded in this public source document. Candidate verification may use isolated package smoke tests; those results remain separate from tagged package documentation.

## Clearance condition

Clearance condition: every mandatory source, artifact, clean-tree, CI, tagged-identity, and publication check must pass on the exact release commit before any public delivery. This worktree is an uncommitted source candidate only.

## Required source and artifact gates

- Ruff, Pyright, the focused journal-health, truth-connection/writer-lease permission, descriptor-hardening, and version-identity suites, plus `git diff --check`, must pass on one frozen candidate.
- Journal deferred-metric and pending-retryable tests remain deterministic without weakening production backlog-age policy.
- POSIX descriptor-hardening tests prove once-per-process raw open across real import aliases, fail-closed identity/permission drift, fail-closed incompatible shared hardening state, hardlink alias sharing, concurrent first-open serialization, and fork/PID isolation.
- Cross-process and same-process writer-lease suites from the `1.9.3`/`1.10.0` interval remain green.
- Built wheel and sdist must omit private Beidou/shared-bridge modules, `tests/phase0/`, and `docs/internal.module-map.md`, expose version `1.10.1`, and install without relying on the source checkout.

## Parent-run evidence (pending)

Fill these only from the exact parent verification run. Do not invent hashes or counts.

- Frozen candidate SHA: `PENDING`
- `git diff --check`: `PENDING`
- Focused journal/truth-connection/version suites: `PENDING`
- Ruff: `PENDING`
- Pyright: `PENDING`
- Wheel + sdist member inspection: `PENDING`
- Development release gate: `PENDING`

## Compatibility

The candidate preserves the stable V1 provider identity, public tool names, package/install shape, SQLite truth-source contract, rebuildable vector/graph companions, Fact Evolution, temporal current/as-of/history queries, bounded citation-grounded Reflection, scope routing, evidence authority, provenance-root validation, deterministic idempotency, release-identity checks, and existing journal checkpoint semantics.

## Publication and deployment boundary

Pushing source does not authorize a tag, GitHub Release, PyPI publication, deployment, plugin reload, or live migration. Each remains a separate operator action after exact-epoch review.
