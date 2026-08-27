# Scope Recall 2.0.0 Release Readiness

Date: 2026-08-27

Owner: maintainers.

## Runtime evidence policy

Release evidence must be generated from the exact candidate bytes. Mutable
runtime counters, database contents, machine-specific paths, credentials, and
deployment overlays are not embedded in this public readiness note. Active
deployment remains a later operator gate.

This maintainer note describes the `2.0.0` candidate cumulative since the last
tagged and packaged public release, `1.10.3`. The unpublished `1.10.4`,
`1.10.5`, and `1.10.6` source candidates remain historical evidence.

## Candidate scope

- Keep SQLite as truth and preserve stable V1 provider/tool identities.
- Route structured writes through strict Fact authority while atomically
  maintaining the legacy projection required by N-1 readers.
- Use bounded relation generation and shared DurableWork contracts; never
  restore full-scope all-pairs work or introduce a second scheduler.
- Compile one Recall Packet from the production candidate set with current
  truth, conflict, provenance, diversity, and token-budget visibility.
- Enforce deny-first two-phase Purge and immutable content-free restore replay.
- Govern core, compatibility, maintenance, developer, and extension tool
  profiles without expanding the default core schema budget.
- Keep optional extension boundaries non-authoritative and disableable.
- Expose Recall Inspector only as a read-only application use case over the
  exact production packet, without private-table access.

## Release identity

- Package/plugin version: `2.0.0`.
- Public release baseline: `1.10.3`.
- Expected annotated release tag: `v2.0.0`.
- Published `1.10.3` source, tag, GitHub Release, and PyPI artifacts remain
  immutable.

## Required gates

- Full pytest, Ruff, Pyright, source diff checks, release invariants, and
  repository release checker pass on one exact source epoch.
- Windows, Linux, and macOS remote CI bind to the exact candidate commit.
- Wheel and sdist scans, clean install, import, CLI, and Doctor smoke pass from
  the same source manifest; artifacts contain no private overlay or secret.
- Isolated activity-snapshot migration, N-1/N/N-1, purge restore replay,
  read-only canary, writer canary, and rollback rehearsals pass without
  touching an active Hermes instance.
- Independent review binds its verdict to the final candidate manifest before
  any merge, tag, release, publication, or deployment decision.

## Compatibility and rollback

The 2.0.x line preserves legacy projection dual-write and does not create
claim-only durable user data. Normal rollback disables product switches and
reverts candidate code while leaving additive schema in place. Whole-database
restore is reserved for an invariant failure, and Purge deny receipts must be
replayed before writer use after any restore.

## Authorization boundary

This document describes a source candidate only. It does not authorize merge,
tag, GitHub Release, PyPI publication, deployment, live migration, live repair,
or changes to an active Hermes instance.

Clearance condition: an independent review must bind an approval verdict to
the exact candidate manifest and exact successful remote CI before any later
merge, tag, release, publication, or deployment authorization is considered.
