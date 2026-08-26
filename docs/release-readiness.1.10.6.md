# Scope Recall 1.10.6 Release Readiness

Date: 2026-08-26

Owner: maintainers.

## Runtime evidence policy

Release evidence must be generated from the exact candidate bytes. Mutable runtime counters, database contents, machine-specific paths, credentials, and deployment overlays are not embedded in this public readiness note; deployment acceptance remains a separate operator gate.

Clearance condition: the exact public candidate must pass the Program 0 focused regressions, full repository CI, release checker, distribution inspection, clean provenance checks, and independent source-manifest review before publication.

This public maintainer note records the `1.10.6` Program 0 patch requirements since the last tagged and packaged public release, `1.10.3`. It supersedes the unpublished `1.10.4` and `1.10.5` source checkpoints.

## Candidate scope

- Preserve the cumulative governance, rollback, Experience, scheduling, shutdown, release-provenance, merge/capture, contradiction, and distribution-scanner fixes from the unpublished checkpoints.
- Publish one stable `ci-required` aggregate and require its successful exact-SHA result in release provenance.
- Keep release resolution deterministic through explicit dependency pins and regenerated hashed constraints.
- Publish one four-state Vector status contract across runtime, Doctor, stats, and dashboard.
- Retire executable full-scope relation enqueue, claim, and drain paths; retain legacy tables only for read-only debt evidence and exact cleanup.
- Enforce relation containment with cap+1 affected-work planning, no partial mutation, fail-closed stale generated signals, finite focus work, bounded maintenance, terminal poison handling, and query zero-write observability.
- Provide content-free relation health and backup-first, selector-bound, compare-and-swap, idempotent operator cleanup receipts.
- Add only migration `0013_relation_containment_v1_10_6`; preserve SQLite truth authority, stable provider/tool identities, and ordinary recall behavior.

## Release identity

- Package/plugin version: `1.10.6`.
- Public release baseline: `1.10.3`.
- Expected annotated release tag: `v1.10.6`.
- Published `1.10.3` source, tag, GitHub Release, and PyPI artifacts remain immutable.

## Required gates

- Source/AST inspection must prove that full-scope force fan-out is absent and cannot be hidden behind a renamed worker.
- Focused tests must cover cap+1, no partial mutation, backoff, wall-clock bounds, generation fencing, terminal non-resurrection, poison isolation, query zero-write, and cleanup dry-run/apply/replay.
- Scale evidence must include bounded 2k and 10k cases plus a 100k analytical upper-bound proof.
- The CJK release benchmark must collect 20 rounds (100 timed query observations) while retaining the 100 ms target, 4x paired latency guard, and 2.5x page-growth guard.
- Full pytest, Ruff, Pyright, `git diff --check`, compile checks, benchmark invariants, wheel/sdist inspection, fresh-environment import/CLI smoke, and the repository release checker must pass on the same source epoch.
- Public wheel and sdist must omit deployment overlays and expose version `1.10.6`.
- Independent review must bind its verdict to the final source manifest and SHA-256 before any publication decision.
- Exact-SHA CI, protected-main policy, annotated-tag identity, GitHub Release assets, PyPI publication, and deployment readback are separate gates and are not claimed by this candidate note.

## Compatibility and rollback

The patch preserves the stable V1 provider/tool identities, package/install shape, scope routing, SQLite truth authority, rebuildable companions, existing rollback event identities, and public memory semantics. Rollback keeps the additive containment and terminal evidence tables in place; older code may ignore them, while maintainers use the exact cleanup receipt to resolve retired work before any downgrade.

## G0 boundary

This document describes a source candidate only. It does not authorize merge, tag, GitHub Release, PyPI publication, deployment, live migration, live repair, or work beyond G0.
