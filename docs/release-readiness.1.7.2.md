# Scope Recall 1.7.2 Release Readiness

Date: 2026-07-12

This public maintainer note records the product-level release requirements for the `1.7.2` source tree. It deliberately excludes deployment-specific runtime counters, local paths, credentials, and private validation context. Customer-facing change details live in `CHANGELOG.md`, the GitHub Release, and PyPI metadata.

## Code gate status

- Package/plugin version: `1.7.2`.
- Release class: compatibility-preserving patch on the stable V1 line.
- SQLite remains the truth source; vector generations, relation indexes, and other companions remain rebuildable derived state.
- Publication requires the strict `python3 scripts/check.release.py` gate on a clean source tree.

## Covered release areas

The release verification covers these public product contracts:

- One ordinary-recall lifecycle policy governs semantic matching, journal and nightly processing, deduplication, retrieval, migration, and vector mutation or replay.
- Immutable vector generations are built as inactive shadows and require explicit compare-and-swap activation; active generations are not repaired in place.
- Metadata keys and values, freshness validators, browser output, governance records, import provenance, generation manifests, migration receipts, and vector outbox errors are sanitized before durable or operator-visible sinks.
- Candidate transitions use conflict checks and compare-and-swap safeguards, with graph and vector companion cleanup for hidden lifecycle rows.
- Invalid runtime config updates fail atomically, and factual freshness readiness uses a consistent eligible cohort.
- Folded inline data URLs are removed at capture and journal boundaries while surrounding ordinary prose is preserved.
- Optional LanceDB and PostgreSQL/pgvector companions do not replace SQLite truth or become mandatory base-install dependencies.
- Release-gate coverage includes forgetting, governance, journal recovery, dashboard, experience replay, installer rollback, fact freshness, relation extraction, and the golden benchmark.

## Runtime evidence policy

Owner: maintainers.

Runtime health is deployment-specific and is not embedded in this public source document. Operators may supply an explicit dashboard payload to local release tooling when they need an environment-specific check; those results remain outside the tagged package documentation.

Clearance condition:

- The strict clean-tree release gate passes for the exact candidate source tree.
- The release commit, tag, GitHub Release assets, and PyPI artifacts all identify version `1.7.2`.
- CI completes successfully for the release commit and tag.
- A clean environment installs the published wheel and verifies the package version, provider import, and operator CLI.
