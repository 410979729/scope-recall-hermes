# Scope Recall 1.6.1 Release Readiness

Date: 2026-06-30

This document is the release-readiness note for the `1.6.1` source tree. It records current gate evidence and the live dashboard waiver required before any formal tag/PyPI publication. It is not a commit, tag, push, or release authorization.

## Code gate status

- Package/plugin version: `1.6.1`.
- Code-level release blockers: none known after the 1.6.1 release-boundary audit cycle.
- Dirty-tree audit gate: required during local release-candidate verification before commit.
- Formal release gate still required: after all intended files are committed, run `python3 scripts/check.release.py` without `--allow-dirty` on a clean tree.

## Covered release areas

The 1.6.1 release notes intentionally cover the already-shipped 1.6 product contract while publishing the post-1.6.0 patch boundary:

- installer rollback and dry-run/apply packaging flows;
- governance cleanup, rollback, and audit transaction atomicity;
- journal recovery for retry-exhausted/dead-letter entries;
- operator dashboard and doctor health reporting;
- Experience replay, playbook promotion/review, and duplicate reporting;
- fact freshness scaffolding and dashboard coverage reporting;
- relation extraction, conflict-safe relation edges, and graph hygiene;
- forgetting default soft archive, rollback receipts, and hard-delete guardrails;
- golden benchmark release gate and commercial recall-quality fixtures;
- public documentation hygiene and release provenance for the post-`v1.6.0` commits.

## Live dashboard waiver

Status: active waiver required before formal release if live state is not cleared.

Owner: local operations.

Current read-only snapshot from the local Hermes home at audit time:

- `ok=true`
- `severity=DEGRADED`
- `journal_unprocessed=855`
- `journal_dead_letter_replay_candidates=409`
- `dead-letter:auth=409`
- `journal_llm_quarantine_runs=12`
- `journal_digest_status=degraded`
- `experience_duplicate_groups=2`
- `experience_needs_review=7`
- `memory_quality_active_hits=8`
- `memory_secret_active=0`
- `vector_status=ready`
- `schema_migration_current=true`
- doctor: `ok=true`, failed checks empty, recommendation count `13`

Reason:

- The remaining degraded state is runtime journal/experience/memory-quality debt, not a known source-code release blocker, but it is still live-operations debt for claiming the installed runtime is fully healthy.
- The already-published `v1.6.0` tag points at `7471574`, while current `main` contains post-release commits for source invariants and public release hygiene; `1.6.1` is the patch release boundary for those commits instead of reusing the `1.6.0` version.
- The local live DB has applied the 1.6.0 schema migration ledger (`schema_migration_current=true`) and the live vector companion is ready (`vector_status=ready`), but the local runtime still carries journal quarantine/auth debt, Experience duplicate/review debt, memory-quality hygiene, and the remaining journal backlog.

Release meaning if this waiver is accepted:

- A source/tag release may proceed only with an explicit owner decision that this runtime tail is outside the package-code release boundary.
- Release notes must not claim the live installation is fully healthy while `severity=DEGRADED` remains true.

Clearance condition:

- Preferred: rerun dashboard after journal recovery and Experience review/dedupe show the live system is no longer `severity=DEGRADED`.
- If not cleared: keep this waiver in the release artifacts, include the latest numeric dashboard snapshot, and name the operations owner for follow-up.

## Clean-tree requirement

Before tag/PyPI release:

1. Review all tracked and untracked paths.
2. Commit only intended source/docs/tests/scripts/fixtures.
3. Run `python3 scripts/check.release.py` without `--allow-dirty`.
4. Run `python3 scripts/check.release.py --live-dashboard-json <fresh-dashboard.json>` and confirm the waiver matches.
5. Confirm the generated wheel includes the release-readiness note and public docs.
6. Tag/release only after explicit user authorization.
