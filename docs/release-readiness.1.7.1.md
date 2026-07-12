# Scope Recall 1.7.1 Release Readiness

Date: 2026-07-08

This historical public maintainer note records product-level release requirements for the `1.7.1` source tree. It excludes deployment-specific runtime counters, local paths, credentials, and private validation context; customer-facing change details live in `CHANGELOG.md`, GitHub Releases, and PyPI metadata.

## Code gate status

- Package/plugin version: `1.7.1`.
- Code-level release blockers: none known after the 1.7.1 patch verification cycle.
- Release artifacts are expected to pass the strict `python3 scripts/check.release.py` gate on a clean tree before publication.
- Release scope: runtime config diagnostics are kept out of persisted operator config, doctor/dashboard expose malformed config diagnostics, candidate browsing excludes processed event-digest rows, event-digest metadata redaction is JSON-safe for nested and non-JSON-like values, external shared-memory preview/receipt semantics are clarified, and hybrid/vector smoke coverage is included in the release gate.

## Covered release areas

The release verification covers these public product areas for the 1.7.1 patch release:

- SQLite truth storage remains authoritative; vector stores and external bridge payloads remain rebuildable or derived companion state.
- Runtime config diagnostics are read-only operator evidence and are not persisted back into `config.json`.
- Event-digest candidates remain reviewable and dry-run-first unless candidate writing is explicitly enabled.
- Browser and governance inspection surfaces redact secret-like values and private paths by default unless an operator explicitly requests raw local inspection.
- Candidate browser queries do not resurface processed event-digest rows as active operator candidates.
- External shared-memory export filters hidden lifecycle and restricted sensitivity before applying export limits; preview helpers remain read-only while receipt helpers explicitly record audit evidence.
- Optional PGVector support remains an extra dependency and does not make base installation require PostgreSQL client libraries.
- Release-gate coverage is maintained across forgetting, governance, journal recovery, dashboard reporting, experience replay, installer rollback, fact freshness, relation extraction, and the golden benchmark.

## Runtime evidence policy

Owner: maintainers.

Deployment-specific runtime health is not embedded in this public historical document. Operators validate each environment independently with doctor and dashboard tooling; those local results remain outside tagged package documentation.

Clearance condition:

- The strict clean-tree release gate passes for the exact `1.7.1` source tree.
- CI completes successfully for the release commit and tag.
- The release commit, tag, GitHub Release assets, and PyPI artifacts identify the same `1.7.1` version.
