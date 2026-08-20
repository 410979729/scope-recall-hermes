# Scope Recall 1.10.0 Release Readiness

Date: 2026-08-19

Owner: maintainers.

This public maintainer note records the source-candidate requirements for the `1.10.0` public tree since the last public release, `1.9.2`. The `1.9.3` writer-lease and digest-transaction interval reached `main` without a tag, GitHub Release, or PyPI artifact and is incorporated here. This document excludes environment-specific health counters, filesystem locations, credentials, and private validation context. No commit, tag, GitHub Release, PyPI publication, deployment, or live migration is implied by this document.

## Candidate scope

- Journal source restore can plan and apply a trusted snapshot window with dry-run, epoch fencing, prewrite backup, operator ledger, and idempotent replay.
- Unresolved journal entries use bounded retry/quarantine and fair per-session budget deferral (issues #45/#48/#46).
- Doctor exposes a structured non-activatable inactive READY vector inventory without treating healthy inactive READY rows as active health (#44). Outward generation identifiers stay secret-free and distinct; digest health fails closed when either budget column is unmeasurable.
- Provider and tooling share one assembled production command port; internal modules stay behind thin entrypoints.
- Shutdown fails closed when a journal or capture worker does not acknowledge the deadline, keeping writer-lease and connection resources for a later retry.
- The unpublished `1.9.3` source interval remains in effect: one writer per truth database, read-only followers, digest model calls outside write transactions, and idle same-process peer recovery.
- Host Hermes `user_id` issue #41 remains open; this plugin candidate does not claim to fix host identity.

## Release identity

- Package/plugin version: `1.10.0`.
- Public release baseline: `1.9.2`.
- Expected annotated release tag: `v1.10.0` (not created by this source-candidate task).
- Published `1.9.2` source, tag, and package artifacts remain immutable.
- `1.9.3` remains an untagged source interval, not a packaged public release.

## Runtime evidence policy

Runtime health is environment-specific and is not embedded in this public source document. Candidate verification may use isolated package smoke tests; those results remain separate from tagged package documentation.

## Clearance condition

Clearance condition: every mandatory source, artifact, clean-tree, CI, tagged-identity, and publication check must pass on the exact release commit before any public delivery. This worktree is an uncommitted source candidate only.

## Required source and artifact gates

- Ruff, Pyright, the focused issue/safety/architecture/1.9.3 preservation suites, source/artifact scans, and doctor smoke must pass on one frozen candidate.
- Journal source-restore tests prove dry-run, epoch, backup, ledger, and idempotent apply/refuse paths.
- Journal backlog tests prove bounded unresolved retry/quarantine and fair session deferral.
- Vector doctor tests prove the inactive READY inventory contract.
- Architecture tests prove one production command-port object for provider and tooling.
- Cross-process and same-process writer-lease, digest-transaction, and peer-recovery suites from the `1.9.3` interval remain green.
- Built wheel and sdist must omit private Beidou/shared-bridge modules, `tests/phase0/`, and `docs/internal.module-map.md`, expose version `1.10.0`, and install without relying on the source checkout.

## Parent-run evidence (pending)

Fill these only from the exact parent verification run. Do not invent hashes or counts.

- Frozen candidate SHA: `PENDING`
- `git diff --check`: `PENDING`
- Focused issue/safety/architecture/1.9.3 suites: `PENDING`
- Ruff: `PENDING`
- Pyright: `PENDING`
- Wheel + sdist member inspection: `PENDING`
- Development release gate: `PENDING`

## Compatibility

The candidate preserves the stable V1 provider identity, public tool names, package/install shape, SQLite truth-source contract, rebuildable vector/graph companions, Fact Evolution, temporal current/as-of/history queries, bounded citation-grounded Reflection, scope routing, evidence authority, provenance-root validation, deterministic idempotency, release-identity checks, and existing journal checkpoint semantics.

## Publication and deployment boundary

Pushing source does not authorize a tag, GitHub Release, PyPI publication, deployment, plugin reload, or live migration. Each remains a separate operator action after exact-epoch review.
