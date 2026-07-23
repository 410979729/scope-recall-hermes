# Scope Recall 1.8.1 Release Readiness

Date: 2026-07-23

This public maintainer note records the product-level release requirements for the `1.8.1` source tree. It deliberately excludes deployment-specific runtime counters, local paths, credentials, and private validation context. Customer-facing change details live in `CHANGELOG.md`, the GitHub Release, and PyPI metadata.

## Code gate status

- Package/plugin version: `1.8.1`.
- Release class: compatibility-preserving patch on the stable V1 line.
- SQLite remains the truth source; vector generations, relation indexes, reflection output, and other companions remain derived or reviewable state.
- Publication requires the strict `python3 scripts/check.release.py` gate on a clean source tree.

## Patch scope

The patch preserves the 1.8.0 public provider, tool, configuration, schema, migration, and storage contracts while correcting cross-platform release behavior:

- The dependency-free `sqlite-bruteforce` companion applies descriptor-based POSIX mode hardening only on platforms that expose `os.fchmod`. Windows continues to use its inherited profile-directory ACL boundary instead of calling an unavailable Unix-only API.
- Activation compensation tests close raw SQLite connections before replacing database files, matching Windows' refusal to unlink or replace an open database while preserving the same fail-closed rollback behavior.
- Explicit CJK entity regression coverage no longer depends on the optional `jieba` package.
- Development environments declare the `setuptools` backend needed by clean no-isolation package-build verification.

## Preserved release contracts

- Fact Evolution remains opt-in with a closed action contract, deterministic evidence policy, reviewed mutation modes, and idempotent receipts.
- Temporal facts preserve current, as-of, and history views with the existing additive SQLite schema and valid-time/recorded-time semantics.
- Reflection remains opt-in, bounded, citation-grounded, provenance-root aware, and review-only unless every candidate-write gate passes.
- Durable `user`, `memory`, `project`, and `ops` facts resolve to shared durable scope, while `general` remains local scratch.
- Existing V1 provider identity, stable tool names, ordinary-memory defaults, SQLite authority, and rebuildable companion boundaries remain compatible.
- Release identity, package contents, public-document hygiene, source/wheel parity, Windows installer coverage, and clean-environment install/import checks remain blocking gates.

## Runtime evidence policy

Owner: maintainers.

Runtime health is deployment-specific and is not embedded in this public source document. Operators may supply an explicit dashboard payload to local release tooling when they need an environment-specific check; those results remain outside tagged package documentation.

## Clearance condition

Clearance condition: every item below is mandatory.

- The strict clean-tree release gate passes for the exact candidate source tree.
- The release commit, tag, GitHub Release assets, and PyPI artifacts all identify version `1.8.1`.
- CI completes successfully for the release commit and tag.
- A clean environment installs the published wheel and verifies package version, provider import, operator CLI, Windows dependency-free fallback, and packaged Fact Evolution, temporal-query, and Reflection modules.
