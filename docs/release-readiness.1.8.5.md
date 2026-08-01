# Scope Recall 1.8.5 Release Readiness

Date: 2026-08-01

This public maintainer note records the product-level release requirements for the `1.8.5` source tree. It excludes environment-specific health counters, filesystem locations, credentials, and private validation context. No tag, GitHub Release, or PyPI publication is implied by this document.

## Code gate status

- Package/plugin version: `1.8.5`.
- Release class: compatibility-preserving reliability patch on the stable V1 line.
- SQLite remains the authoritative truth source; vector, graph, journal, and receipt files remain derived, staged, or mirrored state as documented.
- Publication requires the strict release gate against the exact clean candidate tree.

## Patch scope

### Windows activation-lease liveness

- Windows PID liveness checks use a read-only process handle and exit-code query.
- Operator doctor subprocesses never call `os.kill(pid, 0)` on Windows, where signal zero is `CTRL_C_EVENT` and can interrupt a process-group owner.
- Access-denied and unknown process states remain active/non-recoverable so stale-lease recovery stays fail closed.

### Deterministic fact freshness

- Every authoritative memory insert initializes the matching freshness companion in the same SQLite transaction.
- Memory-type defaults distinguish factual, operational, preference, procedural, decision, episodic, and other durable rows without inventing a verified current state.
- Legacy untracked rows are explicitly penalized, inventoried, reported by doctor, and recoverable through a bounded dry-run-first backfill.
- Advisory recall emits explicit stale/expired warnings; strict recall excludes unsafe freshness states.
- One durable memory is documented and described as one atomic fact or cohesive topic.

### Validator and truth-store safety

- Command validators accept only registered command identifiers; arbitrary shell text is not a validator contract.
- HTTP validators require credential-free public HTTPS targets and reject local, private, link-local, and internal destinations.
- Mutable truth paths reject symlinks and apply owner-only POSIX directory/database modes where the platform supports them.
- Read-only doctor paths remain query-only and fail closed on unsafe permissions without mutating the store.

### Audited operator recovery

- Stale activation leases, legacy freshness debt, and vector dead-letter events have explicit dry-run-first operator commands.
- Apply paths require bounded selectors, verified SQLite online backups, operation identifiers, specific reasons, authoritative ledger rows, and mirrored receipts.
- Recovery and doctor outputs sanitize secrets and private paths before operator-visible egress.

### Release and cross-platform gates

- Capture, doctor, source-tree, wheel, and sdist scans share canonical credential-value patterns and never echo matched secret values.
- PyPI publication is fail-closed, newly added Python sources are automatically included in package/type-check coverage, and Windows full-suite coverage is blocking.
- Operator JSON remains valid under Windows legacy console encodings by emitting ASCII-safe JSON escapes.
- POSIX doctor fixtures create truth stores through the same owner-only connection boundary used by production writers.

## Preserved release contracts

- Fact Evolution remains opt-in with a closed action contract and evidence-gated reviewed mutation.
- Temporal current/as-of/history and bounded citation-grounded Reflection contracts remain compatible.
- Durable `user`, `memory`, `project`, and `ops` targets remain shared only through existing scope policy; `general` remains local scratch.
- The stable provider ID, tool names, V1 storage authority, and rebuildable companion boundaries do not change.

## Runtime evidence policy

Owner: maintainers.

Runtime health is environment-specific and is not embedded in this public source document. Operators may supply an explicit dashboard payload to local release tooling when they need an environment-specific check; those results remain outside tagged package documentation.

## Clearance condition

Clearance condition: every item below is mandatory before public artifact publication:

- Ruff, Pyright, the complete pytest suite, release invariants, benchmarks, and source/artifact scans pass.
- Wheel and sdist contents import successfully and install into a clean environment outside the source tree.
- Installed-plugin activation and canonical doctor smoke checks pass on supported Windows and POSIX CI lanes.
- The release commit, tag, GitHub Release assets, and PyPI artifacts all identify version `1.8.5`.
- CI completes successfully for the exact release commit and tag.
