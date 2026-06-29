# Scope Recall 1.6.0 Release Readiness

Date: 2026-06-30

This document is the release-readiness note for the `1.6.0` source tree. It records current gate evidence and the live dashboard waiver required before any formal tag/PyPI publication. It is not a commit, tag, push, or release authorization.

## Code gate status

- Package/plugin version: `1.6.0`.
- Code-level release blockers: none known after the final pre-release audit cycle.
- Dirty-tree audit gate: `scripts/check.release.py --allow-dirty` passed during local pre-release verification.
- Formal release gate still required: after all intended files are committed, run `python3 scripts/check.release.py` without `--allow-dirty` on a clean tree.

## Covered release areas

The 1.6.0 release notes intentionally cover these user-visible product areas:

- installer rollback and dry-run/apply packaging flows;
- governance cleanup, rollback, and audit transaction atomicity;
- journal recovery for retry-exhausted/dead-letter entries;
- operator dashboard and doctor health reporting;
- Experience replay, playbook promotion/review, and duplicate reporting;
- fact freshness scaffolding and dashboard coverage reporting;
- relation extraction, conflict-safe relation edges, and graph hygiene;
- forgetting default soft archive, rollback receipts, and hard-delete guardrails;
- golden benchmark release gate and commercial recall-quality fixtures.

## Live dashboard waiver

Status: active waiver required before formal release if live state is not cleared.

Owner: Joy / 玉衡 operations.

Current read-only snapshot from the local Hermes home at audit time:

- `ok=true`
- `severity=DEGRADED`
- `journal_unprocessed=335`
- `journal_dead_letter_replay_candidates=80`
- `dead-letter:auth=80`
- `journal_llm_quarantine_runs=6`
- `journal_digest_status=degraded`
- `experience_duplicate_groups=2`
- `experience_needs_review=6`
- `memory_quality_active_hits=7`
- `memory_secret_active=0`
- `vector_status=ready`
- `schema_migration_current=true`
- doctor: `ok=true`, failed checks empty, recommendation count `12`

Reason:

- The remaining degraded state is runtime journal/experience/memory-quality debt, not a known source-code release blocker, but it is still live-operations debt for claiming the installed runtime is fully healthy.
- During the 2026-06-29 stop-window closeout, historical dead-letter replay candidates were operator-classified and two bounded heuristic digest batches reduced the journal backlog from `1235` to `215`; later digest/verification activity moved the current read-only snapshot to `journal_unprocessed=335` with `dead-letter:auth=80`.
- The local live DB has applied the 1.6.0 schema migration ledger (`schema_migration_current=true`) and the live vector companion was rebuilt successfully (`vector_status=ready`), but the stop-window did not fully clear the live system: journal quarantine/auth debt, Experience duplicate/review debt, memory-quality hygiene, and the remaining journal backlog still require operations follow-up before claiming live health is fully green.

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
4. Confirm the generated wheel includes the release-readiness note and public docs.
5. Tag/release only after explicit user authorization.
