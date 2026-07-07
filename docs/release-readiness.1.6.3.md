# Scope Recall 1.6.3 Release Readiness

Date: 2026-07-07

This maintainer verification note records release-gate evidence for the `1.6.3` source tree. It is included for auditability; customer-facing release notes live in `CHANGELOG.md`, GitHub Releases, and PyPI metadata. Runtime counters below describe the maintainer validation environment used for release checks and do not describe customer deployments.

## Code gate status

- Package/plugin version: `1.6.3`.
- Code-level release blockers: none known after the 1.6.3 issue #25 recovery patch and local verification cycle.
- Release artifacts are expected to pass the strict `python3 scripts/check.release.py` gate on a clean tree before publication.

## Covered release areas

The release verification covers these public product areas for the SQLite lock-recovery update prepared as v1.6.3:

- provider-owned SQLite rollback, write-probe, and connection reopen behavior for recoverable store lock/transaction failures;
- conservative `scope_recall_store` retry behavior with identical arguments and explicit `recovered=true` / `retry_count=1` receipts;
- non-SQLite business-error pass-through so retry does not mask validation or operator failures;
- forgetting, governance, journal recovery, dashboard reporting, experience replay, installer rollback, fact freshness, relation extraction, and golden benchmark coverage carried forward from the stable v1.6 release line.

## Live dashboard waiver

Status: documented maintainer-environment waiver when optional live-dashboard evidence is supplied to the release gate.

Owner: maintainers.

Current read-only snapshot from the maintainer validation environment at validation time:

- `ok=true`
- `severity=DEGRADED`
- `journal_unprocessed=227`
- `journal_dead_letter_replay_candidates=0`
- `journal_llm_quarantine_runs=0`
- `journal_digest_status=ready`
- `experience_duplicate_groups=0`
- `experience_needs_review=6`
- `memory_quality_active_hits=2`
- `memory_secret_active=0`
- `vector_status=ready`
- `schema_migration_current=true`
- `dead-letter:auth=0`
- `candidate_debt_count=4`
- `nightly_status=degraded`

Reason:

- These counters document maintenance backlog in the validation environment. They are not source-code release blockers and are not a statement about customer installations.
- The validation environment reported `schema_migration_current=true` and `vector_status=ready`; no storage-schema or stable tool-surface changes are introduced by v1.6.3.
- Customer-facing release notes describe package behavior, compatibility, and artifact provenance rather than maintainer-local runtime health.
- This v1.6.3 snapshot records the current `severity=DEGRADED` maintainer backlog separately from package-code release readiness.

Scope of this evidence:

- A source/tag release may proceed when maintainers decide the validation-environment backlog is outside the package-code release boundary.
- Release notes must not present maintainer-environment dashboard counters as customer deployment health.

Clearance condition:

- Preferred: rerun dashboard after candidate-debt review, memory-quality review, journal digest, and nightly fallback closeout clear the maintainer validation environment.
- If not cleared: keep this verification note as maintainer evidence, include the latest numeric dashboard snapshot, and continue operational follow-up outside the customer-facing release notes.

## Clean-tree requirement

Before tag/PyPI release:

1. Review all tracked and untracked paths.
2. Commit only intended source/docs/tests/scripts/fixtures.
3. Run `python3 scripts/check.release.py` without `--allow-dirty`.
4. Run `python3 scripts/check.release.py --live-dashboard-json <fresh-dashboard.json>` and confirm the maintainer-environment snapshot matches this note when live evidence is used.
5. Confirm the generated wheel includes the release-readiness note and public docs.
6. Tag/release only after explicit maintainer authorization.
