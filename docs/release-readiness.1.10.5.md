# Scope Recall 1.10.5 Release Readiness

Date: 2026-08-25

Owner: maintainers.

## Runtime evidence policy

Release evidence must be generated from the exact candidate bytes. Mutable live counters, local database contents, machine-specific paths, and private overlays are not embedded in this public readiness note; live deploy acceptance is a separate operator gate.

Clearance condition: the exact public candidate must pass the focused audit regressions, repository release gate, clean provenance checks, and tag-to-package identity verification before publication.

This public maintainer note records the patch requirements for `1.10.5` since the last tagged and packaged public release, `1.10.3`. It supersedes the unpublished `1.10.4` source checkpoint.

## Candidate scope

- Retain the cumulative `1.10.4` rollback metadata, governance receipt, Experience `run_id`, and persistent `memory_auto_adjudication` throttle fixes.
- Bound public shutdown and cleanup to one absolute deadline while keeping one tracked retryable cleanup worker.
- Fail closed when Windows Git timeout recovery cannot confirm process-tree termination and bounded pipe collection.
- Require the exact originating release workflow run to be completed and successful before PyPI publication.
- Serialize queued capture with merge mutations so accepted writes cannot recreate merged source rows.
- Preserve current and remaining L4 candidates when fresh-evidence lookup fails, publishing retry context rather than false completion.
- Select a deterministic independent set across contradiction chains while retaining authoritative and two-node behavior.
- Apply synthetic fixture exemptions only to source fixtures; wheel and sdist scans remain unmasked.
- Add no database schema migration and preserve SQLite truth authority.

## Release identity

- Package/plugin version: `1.10.5`.
- Public release baseline: `1.10.3`.
- Expected annotated release tag: `v1.10.5`.
- Published `1.10.3` source, tag, GitHub Release, and PyPI artifacts remain immutable.

## Required gates

- Focused shutdown, capture/merge, L4 retry, contradiction, workflow-security, provenance, and distribution-scanner regressions must pass.
- Full pytest, Ruff, Pyright, `git diff --check`, wheel/sdist inspection, fresh-environment import/CLI smoke, and the repository release checker must pass on the same source epoch before publication.
- Public wheel and sdist must omit private overlays and expose version `1.10.5`.
- Independent review must bind its verdict to the final source manifest and SHA-256 before any publication decision.
- Exact-SHA CI, protected-main policy, annotated-tag identity, GitHub Release assets, PyPI publication, and clean-install readback are separate publication gates and are not claimed by this candidate note.

## Compatibility

The patch preserves stable provider/tool identities, package/install shape, SQLite truth authority, rebuildable companion stores, existing soft-archive rollback event types, and public V1 memory semantics.

## Publication and deployment boundary

The public release candidate and a deployment-private integration are separate trees. Public publication must contain only this generic patch; private relation/curated-recall overlays and local configuration are not release inputs.
