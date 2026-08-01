# Scope Recall 1.8.6 Release Readiness

Date: 2026-08-01

This public maintainer note records the product-level release requirements for the `1.8.6` source tree. It excludes environment-specific health counters, filesystem locations, credentials, and private validation context. No tag, GitHub Release, or PyPI publication is implied by this document.

## Code gate status

- Package/plugin version: `1.8.6`.
- Release class: compatibility-preserving reliability patch on the stable V1 line.
- SQLite remains the authoritative truth source; vector, graph, journal, and receipt files remain derived, staged, or mirrored state as documented.
- Publication requires the strict release gate against the exact clean candidate tree.

## Patch scope

### Resilient fact-freshness maintenance

- Legacy rows with obsolete or invalid validator metadata are isolated as manual live-check debt rather than aborting an entire backfill.
- One malformed row cannot stop valid rows in the same bounded maintenance batch.
- Apply mode re-scans authoritative candidates under an immediate owner transaction, preventing stale read-snapshot promotion.
- Startup defers recoverable lock contention without hiding database corruption or claiming work was completed.

### Governed recall penalties

- Every fact-freshness recall penalty has an explicit packaged default and runtime configuration-registry owner.
- Untracked, needs-live-check, stale, and expired states remain independently configurable without phantom runtime keys.
- Existing defaults preserve advisory ranking behavior; no verified-current state is invented.

### Input and operator-output safety

- Structured sensitive-key detection checks raw and Unicode-compatible forms so visual key obfuscation cannot bypass capture rejection.
- HTTP and transport error redaction reuse the canonical secret-pattern taxonomy instead of maintaining a divergent list.
- Freshness backfill, vector dead-letter recovery, and activation-lease recovery emit ASCII-safe JSON under legacy Windows console encodings.
- The standalone capture-LLM probe runs explicitly from `scripts/`; pytest collection no longer executes the probe as a module side effect.

### Truth, relation, and deduplication integrity

- Stale activation-lease recovery opens SQLite through the shared truth-connection boundary and inherits its path, pragma, and permission invariants.
- Lifecycle rollback may restore only relations that contain the memory being restored; unrelated endpoint pairs are rejected and audited as skipped.
- Exact-text deduplication does not collapse rows whose durable `memory_type` or legacy `category` semantics differ.

## Preserved release contracts

- The Windows read-only activation-lease PID liveness contract from 1.8.5 remains intact.
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
- The release commit, tag, GitHub Release assets, and PyPI artifacts all identify version `1.8.6`.
- CI completes successfully for the exact release commit and tag.
