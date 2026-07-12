# Scope Recall 1.6.2 Release Readiness

Date: 2026-07-07

This historical public maintainer note records product-level release requirements for the `1.6.2` source tree. It excludes deployment-specific runtime counters, local paths, credentials, and private validation context; customer-facing change details live in `CHANGELOG.md`, GitHub Releases, and PyPI metadata.

## Code gate status

- Package/plugin version: `1.6.2`.
- Code-level release blockers: none known after the 1.6.2 local verification and re-audit cycle.
- Release artifacts are expected to pass the strict `python3 scripts/check.release.py` gate on a clean tree before publication.

## Covered release areas

The release verification covers these public product areas for the graph-relation and maintenance hardening updates prepared as v1.6.2:

- graph relation backfill, benchmarking, density counters, and scope-filtered relation evidence;
- maintenance-tool dry-run defaults for `scope_recall_playbook_review` write paths;
- idempotent Experience playbook merge apply behavior;
- journal digest metadata classification for quality-filtered LLM outputs;
- forgetting, governance, journal recovery, dashboard reporting, experience replay, installer rollback, fact freshness, relation extraction, and golden benchmark coverage carried forward from the stable v1.6 release line.

## Runtime evidence policy

Owner: maintainers.

Deployment-specific runtime health is not embedded in this public historical document. Operators validate each environment independently with doctor and dashboard tooling; those local results remain outside tagged package documentation.

Clearance condition:

- The strict clean-tree release gate passes for the exact `1.6.2` source tree.
- CI completes successfully for the release commit and tag.
- The release commit, tag, GitHub Release assets, and PyPI artifacts identify the same `1.6.2` version.
