# Scope Recall 1.9.3 Release Readiness

Date: 2026-08-14

Owner: maintainers.

This public maintainer note records the release requirements for the `1.9.3` compatibility-preserving P0 patch candidate since the public `1.9.2` release. It excludes environment-specific health counters, filesystem locations, credentials, and private validation context. No commit, tag, GitHub Release, PyPI publication, deployment, or live migration is implied by this document.

## Patch scope

- One Scope Recall process may own write authority for a SQLite truth database across gateway, CLI, and other runtimes. Provider instances in that process share the lease; providers in another process open as read-only followers, refuse mutation tools, and may take over only after the operating system releases the previous writer lease.
- Same-process provider instances share one reference-counted lease across threads and import aliases. Lease registry keys normalize resolved paths, Windows case variants, and junction targets, and acquisition plus registration is atomic under one process-local registry lock.
- Journal and nightly model/network calls run without an authoritative SQLite write transaction. Results, rejection receipts, and checkpoints are applied later in short bounded transactions, including between multiple digest scopes.
- Initialization may recover one idle same-process peer transaction only after a real SQLite lock error and then retry once. Non-lock errors, active work, cross-process ownership, and read-only followers remain fail-closed.
- Provider, journal, and nightly cleanup release vector resources, SQLite connections, and writer leases on success and exception paths. Writer-owner sidecars and busy diagnostics are sanitized before operator-visible output.

## Release identity

- Package/plugin version: `1.9.3`.
- Public release baseline: `1.9.2`.
- Expected annotated release tag: `v1.9.3`.
- Published `1.9.2` source, tag, and package artifacts remain immutable.

## Runtime evidence policy

Runtime health is environment-specific and is not embedded in this public source document. Candidate verification may use isolated package smoke tests and receipt-bound profile canaries; those results remain separate from tagged package documentation.

## Clearance condition

Clearance condition: every mandatory source, artifact, clean-tree, CI, tagged-identity, and publication check must pass on the exact release commit before any public delivery.

## Required source and artifact gates

- Ruff, Pyright, the complete pytest suite, strict release invariants, benchmarks, source/artifact scans, clean installation, import smoke, and doctor smoke pass on one frozen candidate.
- Cross-process tests prove that a second process becomes a read-only follower, cannot mutate truth, and can acquire writer ownership only after the previous writer exits.
- Same-process tests prove one shared lease across provider instances, threads, import aliases, Windows case variants, and junction paths without duplicate handles or registry leaks.
- Exception-boundary probes prove journal and nightly configuration failures return lease and handle counts to their baseline and leave the database replaceable on Windows.
- Digest tests prove no model/network call occurs inside an authoritative write transaction and that every previous scope result is committed before the next network call.
- Initialization tests prove exactly one lock-only recovery attempt against an idle same-process peer and no recovery for active, cross-process, read-only, or non-lock failures.
- Diagnostic tests prove owner sidecars, status payloads, and unknown tool names cannot expose path- or credential-like values.
- Built wheel and sdist contain the writer-lease and transaction-guard modules, expose version `1.9.3`, and install without relying on the source checkout.
- Production WAL clearance records the linked SQLite version and requires `3.51.3+` or a documented fixed backport (`3.50.7` or `3.44.6`); source/package checks cannot substitute for host runtime qualification.

## Compatibility

The patch preserves the stable V1 provider identity, public tool names, package/install shape, SQLite truth-source contract, rebuildable vector/graph companions, Fact Evolution, temporal current/as-of/history queries, bounded citation-grounded Reflection, scope routing, evidence authority, provenance-root validation, deterministic idempotency, release-identity checks, and existing journal checkpoint semantics.

## Publication and deployment boundary

Pushing source does not authorize a tag, GitHub Release, PyPI publication, deployment, plugin reload, or live migration. Each remains a separate operator action after exact-epoch review. The first live profile is a canary; any later profile rollout must use the identical verified artifact and its own backup, restart, verification, and rollback receipt.
