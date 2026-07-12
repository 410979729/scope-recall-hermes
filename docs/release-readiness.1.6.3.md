# Scope Recall 1.6.3 Release Readiness

Date: 2026-07-07

This historical public maintainer note records product-level release requirements for the `1.6.3` source tree. It excludes deployment-specific runtime counters, local paths, credentials, and private validation context; customer-facing change details live in `CHANGELOG.md`, GitHub Releases, and PyPI metadata.

## Code gate status

- Package/plugin version: `1.6.3`.
- Code-level release blockers: none known after the 1.6.3 issue #25 recovery patch and local verification cycle.
- Release artifacts are expected to pass the strict `python3 scripts/check.release.py` gate on a clean tree before publication.
- Dirty productization work after this baseline is tracked under `CHANGELOG.md` `[Unreleased]`; do not tag the dirty tree as `1.6.3` unless maintainers intentionally promote those changes into the release scope and refresh this evidence note.

## Covered release areas

The release verification covers these public product areas for the SQLite lock-recovery update prepared as v1.6.3:

- provider-owned SQLite rollback, write-probe, and connection reopen behavior for recoverable store lock/transaction failures;
- conservative `scope_recall_store` retry behavior with identical arguments and explicit `recovered=true` / `retry_count=1` receipts;
- non-SQLite business-error pass-through so retry does not mask validation or operator failures;
- forgetting, governance, journal recovery, dashboard reporting, experience replay, installer rollback, fact freshness, relation extraction, and golden benchmark coverage carried forward from the stable v1.6 release line.

## Runtime evidence policy

Owner: maintainers.

Deployment-specific runtime health is not embedded in this public historical document. Operators validate each environment independently with doctor and dashboard tooling; those local results remain outside tagged package documentation.

Clearance condition:

- The strict clean-tree release gate passes for the exact `1.6.3` source tree.
- CI completes successfully for the release commit and tag.
- The release commit, tag, GitHub Release assets, and PyPI artifacts identify the same `1.6.3` version.
