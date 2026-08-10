# Scope Recall 1.9.2 Release Readiness

Date: 2026-08-09

Owner: maintainers.

This public maintainer note records the release requirements for the `1.9.2` patch candidate since the public `1.9.1` release. It excludes environment-specific health counters, filesystem locations, credentials, and private validation context. No commit, tag, GitHub Release, PyPI publication, or deployment is implied by this document.

## Patch scope

- Event-digest candidate writes trigger bounded replay of their causal vector outbox event IDs only after the authoritative SQLite transaction commits and the provider database lock is released; unrelated older backlog cannot consume that bound, and incomplete companion work remains explicit in the receipt.
- Live vector reconciliation probes page-1 readability through the provider-owned SQLite connection, never by raw-opening the active database file. Offline raw-header inspection requires an explicit quiesced-connection declaration so POSIX advisory locks cannot be canceled by a same-process `close()`.
- Curated source and target priors rank evidence only after lexical, phrase, or intent relevance exists; independently qualified vector-only recall remains governed by its existing threshold.
- Journal failure handling rolls back failed transactions before best-effort receipt storage, sanitizes full exception text before applying the receipt cap, quarantines connections whose rollback fails, skips active peer providers instead of waiting on or rolling back their work, preserves the triggering error when receipt storage remains contended, and performs one bounded background retry only after successful SQLite recovery. Completed-vector-outbox retention receives one isolated transient-lock retry.
- Doctor downgrades a database URI to placeholder review only when username, password, and host are all explicit examples; production-like hosts remain actionable, and canonical URI scanning/capture/store filtering remains fail-closed.
- The shared SQLite contention/recovery module is required in source, wheel, sdist, and type-check coverage.
- Explicit evidence-set search remains opt-in, bounds query variants, result count, and diversity depth at both schema and direct-call runtime boundaries, restores indexed batch embeddings to input order, keeps the ordinary funnel trace attached to the primary query, and stores all per-request diagnostics in context-local state.
- The LoCoMo runner requires explicit external dataset/source/auth paths and keeps output outside the checkout. Its path-free epoch binds the HEAD tree, index entries, raw tracked worktree bytes/modes/symlinks, and untracked bytes independently of Git diff rendering. It validates retrieval, query-plan, and result checkpoint identity/schema before resume, scoring, or reporting; binds workers, model rounds, timeout, and secret-free route identity; rechecks that route at every model call while allowing same-route token refresh; rejects undeclared or duplicate judge fields; and grants official comparability only after every canonical/artifact/route check passes.
- GitHub Release publication triggers the separately permissioned PyPI workflow through explicit repository dispatch; the PyPI job revalidates tag and package identity, uses OIDC/environment protection, and fails loudly on duplicate versions.

## Release identity

- Package/plugin version: `1.9.2`.
- Public release baseline: `1.9.1`.
- Expected annotated release tag: `v1.9.2`.
- The published `1.9.1` artifacts remain immutable and must not be replaced with modified bytes.

## Runtime evidence policy

Runtime health is environment-specific and is not embedded in this public source document. Candidate verification may use isolated package smoke tests and receipt-bound profile canaries; those results remain separate from tagged package documentation.

## Clearance condition

Clearance condition: every mandatory source, artifact, clean-tree, CI, tagged-identity, and publication check must pass on the exact release commit before any public delivery.

## Required source and artifact gates

- Ruff, Pyright, the complete pytest suite, strict release invariants, benchmarks, and source/artifact scans pass on one frozen candidate.
- The new SQLite recovery module is present and importable in both wheel and sdist artifacts.
- Live reconciliation tests forbid raw file opens while a pager connection exists, require an explicit quiesced declaration for offline header reads, and preserve fail-closed receipts for pager-level `DatabaseError`.
- Isolated installation exposes the `hermes-scope-recall` console command and the command routes `verify --runtime --json` through the production installer verification path.
- Negative retrieval coverage proves pure-noise curated queries return no prior-only matches while relevant curated evidence remains available.
- Public evidence-set validation rejects out-of-range limits, variant counts/lengths, and diversity depth before handler coercion; indexed batch ordering, primary-trace ownership, explicit-name separation, and CJK polite-prefix scope are regression tested.
- The LoCoMo harness proves path-free artifacts, HEAD/index/raw-worktree/untracked source binding that is insensitive to local Git diff rendering, explicit external path requirements, deterministic checkpoint behavior, fail-closed ingestion-home plus retrieval/query-plan/result identity and schema validation, exact judge parsing, preflight and call-time model-route drift rejection, same-route token refresh, and oracle/incomplete/unexpected-row denial at the official-comparability gate.
- Deterministic contention tests prove full-text-before-truncation receipt sanitization, rollback-failure quarantine, nonblocking peer ownership, lock-only background retry, causal event-ID vector replay, and isolated vector-retention retry behavior.
- Workflow tests prove the GitHub Release-to-PyPI repository-dispatch handoff, tag/version revalidation, OIDC environment isolation, and absence of `skip-existing`.
- Profile canaries require a verified pre-mutation backup, exact artifact fingerprint, runtime verification, canonical doctor output, provider registration, real recall behavior, and a receipt-bound rollback path.
- Production WAL clearance records the linked SQLite version and requires `3.51.3+` or a documented fixed backport (`3.50.7` or `3.44.6`); source/package checks cannot substitute for the host runtime qualification.

## Compatibility

The patch preserves the stable V1 provider identity, public tool names, SQLite truth-source contract, rebuildable vector/graph companions, Fact Evolution, temporal current/as-of/history queries, bounded citation-grounded Reflection, scope routing, evidence authority, provenance-root validation, deterministic idempotency, atomic journal checkpoint ownership, and release-identity checks.

## Publication and deployment boundary

Pushing source does not authorize a tag, GitHub Release, PyPI publication, deployment, plugin reload, or live migration. Each remains a separate operator action after exact-epoch review. The first live profile is a canary; any later profile rollout must use the identical verified artifact and its own backup, restart, verification, and rollback receipt.
