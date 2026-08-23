# Scope Recall 1.10.3 Release Readiness

Date: 2026-08-23

Owner: maintainers.

## Runtime evidence policy

Release evidence must be generated from the exact candidate bytes. Mutable live counters, local database contents, machine-specific paths, and private overlays are not embedded in this public readiness note; live deploy acceptance is a separate operator gate.

Clearance condition: the exact public candidate must pass the focused issue #50 regressions, the repository release gate, clean provenance checks, and tag-to-package identity verification before publication.

This public maintainer note records the patch requirements for `1.10.3` since the last tagged and packaged public release, `1.10.2`.

## Candidate scope

- Fix issue #50 by recognizing the exact official `event_type=memory_auto_adjudication` and `action=archive` receipt in governance coverage.
- Allow default cleanup rollback to select that same exact receipt pair.
- Preserve snapshot/CAS safety: rollback refuses a row whose lifecycle or metadata changed after the receipt.
- Keep unknown event types fail-closed; a generic `archive` action does not create governance or rollback authority.
- Add no schema migration and change no automatic-adjudication policy.

## Release identity

- Package/plugin version: `1.10.3`.
- Public release baseline: `1.10.2`.
- Expected annotated release tag: `v1.10.3`.
- Published `1.10.2` source, tag, GitHub Release, and PyPI artifacts remain immutable.

## Required gates

- Focused automatic-adjudication and governance-cleanup regressions must pass, including coverage, successful rollback, post-receipt drift refusal, and unknown-writer rejection.
- Full pytest, Ruff, Pyright, `git diff --check`, wheel/sdist inspection, fresh-environment import/CLI smoke, and the repository release checker must pass on the same source epoch.
- Public wheel and sdist must omit private overlays and expose version `1.10.3`.
- Exact-SHA CI, annotated-tag identity, GitHub Release assets, PyPI publication, and clean-install readback must pass before the issue is closed.

## Compatibility

The patch preserves stable provider/tool identities, package/install shape, SQLite truth authority, rebuildable companion stores, existing soft-archive rollback event types, and all public V1 memory semantics.

## Publication and deployment boundary

The public release candidate and the local private integration are separate trees. Public publication must contain only this generic patch; local private modules and configuration are not release inputs.
