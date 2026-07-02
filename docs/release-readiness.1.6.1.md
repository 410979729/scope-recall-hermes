# Scope Recall 1.6.1 Release Readiness

Date: 2026-06-30

This maintainer verification note records release-gate evidence for the `1.6.1` source tree. It is included for auditability; customer-facing release notes live in `CHANGELOG.md`, GitHub Releases, and PyPI metadata. Runtime counters below describe the maintainer validation environment used for release checks and do not describe customer deployments.

## Code gate status

- Package/plugin version: `1.6.1`.
- Code-level release blockers: none known after the 1.6.1 release verification cycle.
- Release artifacts are expected to pass the strict `python3 scripts/check.release.py` gate on a clean tree before publication.

## Covered release areas

The release verification covers these public product areas for the documentation, packaging, and release-provenance updates published as v1.6.1.:

- installer rollback and dry-run/apply packaging flows;
- governance cleanup, rollback, and audit transaction atomicity;
- journal recovery for retry-exhausted/dead-letter entries;
- operator dashboard and doctor health reporting;
- Experience replay, playbook promotion/review, and duplicate reporting;
- fact freshness scaffolding and dashboard coverage reporting;
- relation extraction, conflict-safe relation edges, and graph hygiene;
- forgetting default soft archive, rollback receipts, and hard-delete guardrails;
- golden benchmark release gate and commercial recall-quality fixtures.
- public documentation and release provenance for the `v1.6.1` patch.

## Live dashboard waiver

Status: documented maintainer-environment waiver when optional live-dashboard evidence is supplied to the release gate.

Owner: maintainers.

Current read-only snapshot from the maintainer validation environment at validation time:

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

- These counters document maintenance backlog in the validation environment. They are not source-code release blockers and are not a statement about customer installations.
- the validation environment reported `schema_migration_current=true` and `vector_status=ready`; no storage-schema or tool-surface changes are introduced by v1.6.1.
- Customer-facing release notes must describe package behavior, compatibility, and artifact provenance rather than maintainer-local runtime health.

Scope of this evidence:

- A source/tag release may proceed when maintainers decide the validation-environment backlog is outside the package-code release boundary.
- Release notes must not present maintainer-environment dashboard counters as customer deployment health.

Clearance condition:

- Preferred: rerun dashboard after journal recovery and Experience review/dedupe clear the maintainer validation environment.
- If not cleared: keep this verification note as maintainer evidence, include the latest numeric dashboard snapshot, and continue operational follow-up outside the customer-facing release notes.

## Clean-tree requirement

Before tag/PyPI release:

1. Review all tracked and untracked paths.
2. Commit only intended source/docs/tests/scripts/fixtures.
3. Run `python3 scripts/check.release.py` without `--allow-dirty`.
4. Run `python3 scripts/check.release.py --live-dashboard-json <fresh-dashboard.json>` and confirm the maintainer-environment snapshot matches this note when live evidence is used.
5. Confirm the generated wheel includes the release-readiness note and public docs.
6. Tag/release only after explicit maintainer authorization.
