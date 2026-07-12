# Scope Recall 1.7.0 Release Readiness

Date: 2026-07-08

This historical public maintainer note records product-level release requirements for the `1.7.0` source tree. It excludes deployment-specific runtime counters, local paths, credentials, and private validation context; customer-facing change details live in `CHANGELOG.md`, GitHub Releases, and PyPI metadata.

## Code gate status

- Package/plugin version: `1.7.0`.
- Code-level release blockers: none known after the 1.7.0 productization verification cycle.
- Release artifacts are expected to pass the strict `python3 scripts/check.release.py` gate on a clean tree before publication.
- Release scope: event-digest evidence packets, reviewable candidate extraction, read-only memory browsing, candidate governance commands, Experience-to-skill bridge helpers, optional PGVector companion support, external shared-memory bridge contracts, explicit sensitivity governance, release-gate progress output, and same-process peer-provider SQLite lock recovery for recoverable `scope_recall_store` writes.

## Covered release areas

The release verification covers these public product areas for the 1.7.0 productization release:

- SQLite truth storage remains authoritative; vector stores and external bridge payloads remain rebuildable or derived companion state.
- Event-digest candidates remain reviewable and dry-run-first unless candidate writing is explicitly enabled.
- Generic unclassified chat packets are rejected instead of becoming durable memory candidates.
- Browser and governance inspection surfaces redact secret-like values and private paths by default unless an operator explicitly requests raw local inspection.
- External shared-memory export filters hidden lifecycle and restricted sensitivity before applying export limits.
- `scope_recall_store` retries recoverable SQLite lock/transaction failures after rolling back dirty current or same-process peer-provider transactions.
- Optional PGVector support is an extra dependency and does not make base installation require PostgreSQL client libraries.
- Release-gate coverage is maintained across forgetting, governance, journal recovery, dashboard reporting, experience replay, installer rollback, fact freshness, relation extraction, and the golden benchmark.

## Runtime evidence policy

Owner: maintainers.

Deployment-specific runtime health is not embedded in this public historical document. Operators validate each environment independently with doctor and dashboard tooling; those local results remain outside tagged package documentation.

Clearance condition:

- The strict clean-tree release gate passes for the exact `1.7.0` source tree.
- CI completes successfully for the release commit and tag.
- The release commit, tag, GitHub Release assets, and PyPI artifacts identify the same `1.7.0` version.
