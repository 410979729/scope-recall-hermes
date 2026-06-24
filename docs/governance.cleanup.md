# Scope Recall Governance Cleanup Runbook

This runbook covers historical memory pollution cleanup for scope-recall. It is intentionally conservative: SQLite remains the truth layer, cleanup defaults to dry-run, apply only soft-archives rows, and every mutation writes a governance audit event with a rollback batch id.

## What this cleanup targets

The cleanup script targets historical durable memory rows that were produced before the v1.4.5 journal digest hardening and look like template/transcript wrappers instead of high-density memory:

- `Operations workflow summary from journal digest: ...`
- `Journal digest memory ...`
- rows containing transcript role wrappers such as `user:` / `assistant:`

Archived rows are ignored. Hard delete is never performed by this script.

## Commands

Preview candidates:

```bash
python3 scripts/governance.cleanup.py \
  --hermes-home "$HERMES_HOME" \
  --dry-run \
  --limit 500 \
  --format json
```

Apply soft archive with a named batch:

```bash
BATCH="cleanup-live-$(date +%Y%m%d_%H%M%S)"
python3 scripts/governance.cleanup.py \
  --hermes-home "$HERMES_HOME" \
  --apply \
  --batch-id "$BATCH" \
  --limit 500 \
  --format json
```

Rollback a batch:

```bash
python3 scripts/governance.cleanup.py \
  --hermes-home "$HERMES_HOME" \
  --rollback-batch \
  --apply \
  --batch-id "$BATCH" \
  --format json
```

## Audit trail

Each applied soft archive writes one row to `governance_audit_events`:

- `event_type = memory_cleanup`
- `action = soft_archive`
- `target_id = memories.id`
- `batch_id = <operator batch>`
- `before_json` contains previous metadata and timestamps after report-safe redaction
- `after_json` contains archived metadata after report-safe redaction

Rollback writes `action = rollback_soft_archive` rows into the same table.

## Safety sequence

1. Run doctor and record baseline.
2. Make an online SQLite backup with `sqlite3.Connection.backup()`.
3. Run cleanup dry-run.
4. Inspect candidate counts and samples.
5. Apply soft archive.
6. Re-run doctor and active dirty-count probe.
7. Keep the backup and batch id in the final report.

## Acceptance checks

```bash
python3 -m pytest tests/test_governance_cleanup.py tests/test_forgetting.py tests/test_doctor_secret_scan.py -q
python3 scripts/doctor.py --json --hermes-home "$HERMES_HOME"
```

Expected live state after cleanup:

- `journal_unprocessed = 0`
- `vector.status = ready`
- `memory_secret_scan.active_secret_like_count = 0`
- active template/transcript cleanup counts are zero
- `governance_audit_events` has one row per archived row for the cleanup batch

## Boundaries

- This script does not hard-delete secrets. Use forgetting hard-delete only after explicit operator approval and backup.
- This script does not mutate LanceDB/vector rows. Archived rows are excluded before recall candidate limiting/fusion/dedupe, and vector search drops hits that no longer have SQLite truth rows; vector repair can rebuild the companion from SQLite truth if needed.
- This script does not enable maintenance tools in the live Hermes config.
