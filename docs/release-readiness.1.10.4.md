# Scope Recall 1.10.4 Release Readiness

Date: 2026-08-23

Owner: maintainers.

## Runtime evidence policy

Release evidence must be generated from the exact candidate bytes. Mutable live counters, local database contents, machine-specific paths, and private overlays are not embedded in this public readiness note; live deploy acceptance is a separate operator gate.

Clearance condition: the exact public candidate must pass the focused post-release audit regressions, the repository release gate, clean provenance checks, and tag-to-package identity verification before publication.

This public maintainer note records the patch requirements for `1.10.4` since the last tagged and packaged public release, `1.10.3`.

## Candidate scope

- Restore cleanup rollback metadata from a validated before-snapshot, with replacement rather than merge semantics, and fail closed on missing or malformed rollback evidence.
- Count archive governance coverage only for explicit recognized event/action pairs whose latest receipt still binds the current archived row.
- Keep Experience preflight runs pending until feedback, pass the optional `run_id` through the public feedback tool, and allow a pending run to close after its playbook becomes terminal without changing terminal playbook counters.
- Persist the successful `memory_auto_adjudication` throttle marker so provider recreation cannot bypass the configured interval; failed runs remain retryable.
- Add no database schema migration and preserve SQLite truth authority.

## Release identity

- Package/plugin version: `1.10.4`.
- Public release baseline: `1.10.3`.
- Expected annotated release tag: `v1.10.4`.
- Published `1.10.3` source, tag, GitHub Release, and PyPI artifacts remain immutable.

## Required gates

- Focused lifecycle, governance cleanup, Experience, provider scheduling, and public tool-schema regressions must pass.
- Full pytest, Ruff, Pyright, `git diff --check`, wheel/sdist inspection, fresh-environment import/CLI smoke, and the repository release checker must pass on the same source epoch.
- Public wheel and sdist must omit private overlays and expose version `1.10.4`.
- Independent review must bind its verdict to the final source manifest and SHA-256 before any publication decision.
- Exact-SHA CI, annotated-tag identity, GitHub Release assets, PyPI publication, and clean-install readback are separate publication gates and are not claimed by this candidate note.

## Compatibility

The patch preserves stable provider/tool identities, package/install shape, SQLite truth authority, rebuildable companion stores, existing soft-archive rollback event types, and public V1 memory semantics. The feedback `run_id` field is optional.

## Publication and deployment boundary

The public release candidate and a deployment-private integration are separate trees. Public publication must contain only this generic patch; private relation/curated-recall overlays and local configuration are not release inputs.
