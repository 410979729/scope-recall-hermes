# Scope Recall 1.8.3 Release Readiness

Date: 2026-07-31

This public maintainer note records the product-level release requirements for the `1.8.3` source tree. It excludes environment-specific health counters, filesystem locations, credentials, and private validation context. Customer-facing changes belong in `CHANGELOG.md`, the GitHub Release, and PyPI metadata.

## Code gate status

- Package/plugin version: `1.8.3`.
- Release class: compatibility-preserving reliability patch on the stable V1 line.
- SQLite remains the truth source; vector generations, relation indexes, journal summaries, and other companions remain derived or reviewable state.
- Publication requires the strict release gate against the exact clean candidate tree.

## Patch scope

### Current-state recall and identity boundaries

- Current operating-system answers require answer-shaped state evidence; platform words in manuals or reference material do not receive current-state authority by themselves.
- Linux distributions, Windows, macOS, WSL, and concrete timezone values share deterministic answer-evidence coverage.
- Direct Chinese location questions support multi-character subjects and optional current-state predicates without swallowing the subject into the query suffix.
- Empty or malformed platform/chat aliases fail closed at both configuration validation and runtime resolution boundaries.
- Canonical identity regression tests exercise the enabled cross-platform sharing gate rather than raw-ID allowlisting alone.
- The vector-only default threshold is backed by a checked-in, model-specific labeled calibration fixture and an explicit false-positive cost.

### Transaction and health contracts

- Playbook merges with `commit=False` require a caller-owned transaction and cannot return a successful merge while leaving an implicit transaction open.
- Journal provenance health distinguishes a recent-window risk count from the consecutive latest-run streak used by the failure threshold.
- Completed vector outbox history has bounded, configurable retention; nonterminal work and a per-generation recent-history floor are never pruned by that policy.
- Raw journal evidence retention remains independently configured and is not changed by vector outbox retention.

### Windows repair and rollback

- FTS online backups use platform-aware descriptor hardening and remove incomplete backup artifacts on failure.
- Fresh-install compensation detaches local exception tracebacks before cleanup and retries only bounded Windows sharing violations.
- Existing SQLite truth is restored through SQLite's online backup API instead of delete-and-copy replacement.
- A rollback blocked by an external file lock fails closed and reports a physically present maintenance lease.
- LanceDB companion backups support Windows extended paths, shorter backup names, and verified source/destination manifests before mutation.
- Manual recovery receipts emit PowerShell commands on Windows and POSIX commands on POSIX hosts.

### Optional embedding and packaging

- SentenceTransformers is resolved lazily so an optional installation or test double added after module import is visible without restarting the process.
- Newly added runtime modules, configuration keys, documentation, and benchmark fixtures are included in package and type-check manifests.
- Release source scans reject secret-bearing, private-path, cache, generated, and forbidden artifacts.

## Preserved release contracts

- Fact Evolution remains opt-in with its closed action contract, deterministic evidence policy, reviewed mutation modes, and idempotent receipts.
- Temporal facts preserve current, as-of, and history views with valid-time and recorded-time semantics.
- Reflection remains opt-in, bounded, citation-grounded, provenance-root aware, and review-only unless every candidate-write gate passes.
- Durable `user`, `memory`, `project`, and `ops` facts resolve to shared durable scope, while `general` remains local scratch.
- Existing V1 provider identity, stable tool names, ordinary-memory defaults, SQLite authority, and rebuildable companion boundaries remain compatible.

## Runtime evidence policy

Owner: maintainers.

Runtime health is environment-specific and is not embedded in this public source document. Operators may supply an explicit dashboard payload to local release tooling when they need an environment-specific check; those results remain outside tagged package documentation.

## Clearance condition

Clearance condition: every item below is mandatory before public artifact publication:

- The strict clean-tree release gate passes for the exact candidate source tree.
- The release commit, tag, GitHub Release assets, and PyPI artifacts all identify version `1.8.3`.
- CI completes successfully for the release commit and tag.
- A clean environment installs the wheel and verifies package version, provider import, operator CLI, dependency-free fallback, Windows recovery commands, and packaged Fact Evolution, temporal-query, Reflection, retention, retrieval-calibration, and schema-compatibility modules.
