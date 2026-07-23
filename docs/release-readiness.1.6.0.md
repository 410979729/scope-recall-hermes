# Scope Recall 1.6.0 Release Readiness

Date: 2026-06-30

This historical public maintainer note records product-level release requirements for the `1.6.0` source tree. It excludes deployment-specific runtime counters, local paths, credentials, and private validation context; customer-facing change details live in `CHANGELOG.md`, GitHub Releases, and PyPI metadata.

## Code gate status

- Package/plugin version: `1.6.0`.
- Code-level release blockers: none known after the final pre-release audit cycle.
- Release artifacts are expected to pass the strict `python3 scripts/check.release.py` gate on a clean tree before publication.

## Covered release areas

The release verification covers these public product areas for the v1.6.0 source tree and artifacts.:

- installer rollback and dry-run/apply packaging flows;
- governance cleanup, rollback, and audit transaction atomicity;
- journal recovery for retry-exhausted/dead-letter entries;
- operator dashboard and doctor health reporting;
- Experience replay, playbook promotion/review, and duplicate reporting;
- fact freshness scaffolding and dashboard coverage reporting;
- relation extraction, conflict-safe relation edges, and graph hygiene;
- forgetting default soft archive, rollback receipts, and hard-delete guardrails;
- golden benchmark release gate and curated recall-regression fixtures.

## Runtime evidence policy

Owner: maintainers.

Deployment-specific runtime health is not embedded in this public historical document. Operators validate each environment independently with doctor and dashboard tooling; those local results remain outside tagged package documentation.

Clearance condition:

- The strict clean-tree release gate passes for the exact `1.6.0` source tree.
- CI completes successfully for the release commit and tag.
- The release commit, tag, GitHub Release assets, and PyPI artifacts identify the same `1.6.0` version.
