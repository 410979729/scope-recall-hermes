# Scope Recall 1.7.0 Release Readiness

Date: 2026-07-08

This maintainer verification note records release-gate evidence for the `1.7.0` source tree. It is included for auditability; customer-facing release notes live in `CHANGELOG.md`, GitHub Releases, and PyPI metadata. Runtime counters below describe the maintainer validation environment used for release checks and do not describe customer deployments.

## Code gate status

- Package/plugin version: `1.7.0`.
- Code-level release blockers: none known after the 1.7.0 productization verification cycle.
- Release artifacts are expected to pass the strict `python3 scripts/check.release.py` gate on a clean tree before publication.
- Release scope: event-digest evidence packets, reviewable candidate extraction, read-only memory browsing, candidate governance commands, Experience-to-skill bridge helpers, optional PGVector companion support, external shared-memory bridge contracts, explicit sensitivity governance, release-gate progress output, and same-process peer-provider SQLite lock recovery for recoverable `scope_recall_store` writes.

## Covered release areas

The release verification covers these public product areas for the 1.7.0 productization release:

- SQLite truth storage remains authoritative; vector stores and external bridge payloads remain rebuildable or derived companion state.
- Event-digest candidates remain reviewable and dry-run-first unless candidate writing is explicitly enabled.
- Generic unclassified chat packets are rejected instead of becoming durable memory candidates.
- Browser and governance inspection surfaces redact secret-like values and private paths by default unless an operator explicitly requests raw local inspection.
- External shared-memory export filters hidden lifecycle and restricted sensitivity before applying export limits.
- `scope_recall_store` retries recoverable SQLite lock/transaction failures after rolling back dirty current or same-process peer-provider transactions.
- Optional PGVector support is an extra dependency and does not make base installation require PostgreSQL client libraries.
- Release-gate coverage is maintained across forgetting, governance, journal recovery, dashboard reporting, experience replay, installer rollback, fact freshness, relation extraction, and the golden benchmark.

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
- The validation environment reported `schema_migration_current=true` and `vector_status=ready`; the 1.7.0 package keeps SQLite truth as the source of authority and treats vector/external stores as derived companion state.
- Customer-facing release notes describe package behavior, compatibility, and artifact provenance rather than maintainer-local runtime health.
- This 1.7.0 snapshot records the current `severity=DEGRADED` maintainer backlog separately from package-code release readiness.

Scope of this evidence:

- A source/tag release may proceed when maintainers decide the validation-environment backlog is outside the package-code release boundary.
- Release notes must not present maintainer-environment dashboard counters as customer deployment health.

Clearance condition:

- Strict clean-tree release gate passes for the tagged source tree.
- GitHub Actions release workflow passes after the tag is pushed.
- GitHub Release assets and PyPI artifacts are read back for the same `1.7.0` version.
