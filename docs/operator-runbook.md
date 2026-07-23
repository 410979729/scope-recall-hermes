# Scope Recall Operator Runbook

This runbook is the day-to-day operating guide for the `scope-recall` Hermes memory provider. It assumes SQLite is the truth source, vector storage is rebuildable companion state, and destructive operations require an explicit operator decision.

## Operating principles

- SQLite truth lives at `$HERMES_HOME/scope-recall/memory.sqlite3`.
- Vector state is rebuildable; do not treat LanceDB or `vector.sqlite3` as the authority.
- Prefer dry-run before apply.
- Make a SQLite online backup before live data mutation.
- If SQLite reports `database is locked`, identify the holder and wait for a safe stop/restart window; do not spin on blind retries.
- Retained rollback material should be classified, for example `rollback-needed` or `audit-evidence`.
- Sensitive data rule: do not hard-delete or expose secrets unless the operator explicitly approved the exact cleanup path; ordinary governance cleanup must not hard-delete.

## Environment

```bash
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
export SCOPE_RECALL_ROOT="$HERMES_HOME/plugins/scope-recall"
cd "$SCOPE_RECALL_ROOT"
```

When operating a source checkout instead of an installed plugin, set `SCOPE_RECALL_ROOT` to that checkout.

## 安装/升级/回滚

Install or upgrade the plugin copy in a Hermes home. `upgrade` is the explicit operator verb for replacing an existing plugin copy; it backs up the current `$HERMES_HOME/plugins/scope-recall/` directory before overwrite and reports a rollback command.

```bash
hermes-scope-recall upgrade --hermes-home "$HERMES_HOME" --dry-run
hermes-scope-recall upgrade --hermes-home "$HERMES_HOME" --json
hermes-scope-recall verify --hermes-home "$HERMES_HOME" --runtime
```

For a first install, `install` uses the same safe copy path:

```bash
hermes-scope-recall install --hermes-home "$HERMES_HOME" --dry-run
hermes-scope-recall install --hermes-home "$HERMES_HOME" --json
```

Activation against an existing truth DB is a maintenance-window operation:

1. Run `upgrade --dry-run` and retain the proposed backup/rollback paths.
2. Stop the Hermes gateway and every process that can write `$HERMES_HOME/scope-recall/memory.sqlite3`.
3. Run the activation with explicit confirmation:

```bash
hermes-scope-recall upgrade \
  --hermes-home "$HERMES_HOME" \
  --activate \
  --maintenance-mode \
  --json
```

4. Require `activation_transaction.status=committed`, `maintenance_lease.released=true`, `sqlite.guards_removed=true`, `sqlite.writer_lock_verified=true`, and runtime verification `ok=true`.
5. Start the gateway, then rerun `verify --runtime` and `doctor` before reopening writes.

Do not use `--maintenance-mode` to bypass an active writer. The installer creates `.activation-maintenance.json`, probes `BEGIN IMMEDIATE`, invalidates cached statements, and installs temporary DML guard triggers before snapshotting; lease-aware and raw/legacy writers then remain blocked through commit or compensation. If a failure reports `sqlite.manual_recovery_required=true`, do **not** run the offline SQLite restore command or delete the retained lease blindly. First identify and stop every writer, preserve the live DB/WAL/SHM and snapshot, compare the receipt fingerprints, and decide whether to keep the newer truth or restore the snapshot. Remove a stale lease only after confirming no activation process owns it and recording that manual decision. If a failed activation receipt marks a vector companion `rebuild_required`, run the emitted vector repair command only after SQLite truth is stable.

For source-tree validation before a copy/install upgrade, pin the interpreter first. A release gate run is only reproducible when the active interpreter has the dev and vector extras (`pytest`, `ruff`, `pyright`, `wheel`, `lancedb`, and `pyarrow`). Do not compare results from a stale sibling venv such as `.venv` with the Hermes runtime venv unless both have been installed the same way.

```bash
python -m pip install -e '.[dev,all]'
python - <<'PY'
import importlib.util, sys
mods = ['pytest', 'ruff', 'pyright', 'wheel', 'lancedb', 'pyarrow']
print(sys.executable)
print({name: importlib.util.find_spec(name) is not None for name in mods})
PY
python -m pytest -q
python scripts/check.release.py --allow-dirty
```

Rollback strategy:

1. Prefer the `rollback_command` emitted by `install`/`upgrade` JSON output. It points at the backup directory under `$HERMES_HOME/backups/scope-recall-installer/.../scope-recall`.
2. Validate first, then apply:

```bash
hermes-scope-recall rollback --hermes-home "$HERMES_HOME" --backup-dir /path/to/backup/scope-recall --dry-run --json
hermes-scope-recall rollback --hermes-home "$HERMES_HOME" --backup-dir /path/to/backup/scope-recall --json
```

3. Restart Hermes after restoring a plugin copy, then rerun runtime verify/doctor.
4. Restore SQLite only if the data mutation itself must be reverted; prefer targeted rollback batches when available.
5. Rebuild vector companion after any SQLite restore:

```bash
hermes-scope-recall vector repair --hermes-home "$HERMES_HOME" --dry-run
hermes-scope-recall vector repair --hermes-home "$HERMES_HOME"
```

## 日常 health check

Run doctor first, then dashboard for a compact summary:

```bash
hermes-scope-recall doctor \
  --hermes-home "$HERMES_HOME" \
  --source-root "$SCOPE_RECALL_ROOT" \
  --json

hermes-scope-recall dashboard \
  --hermes-home "$HERMES_HOME" \
  --source-root "$SCOPE_RECALL_ROOT" \
  --format json \
  --output /tmp/scope-recall-dashboard.json
```

Expected healthy state:

- `ok: true` in doctor.
- dashboard `schema_version: dashboard_report.v1`.
- no journal backlog above configured thresholds.
- vector status is ready or intentionally disabled.
- memory secret scan reports no active plaintext-like secret rows.

## journal backlog drain

Always start with dry-run and a small limit:

```bash
hermes-scope-recall journal digest \
  --hermes-home "$HERMES_HOME" \
  --extractor heuristic \
  --limit-entries 100 \
  --dry-run \
  --verbose
```

Before applying a live drain, create a SQLite online backup:

```bash
python3 - <<'PY'
import sqlite3, time
from pathlib import Path
home = Path(__import__('os').environ['HERMES_HOME'])
src = home / 'scope-recall' / 'memory.sqlite3'
dst = home / 'backups' / f'scope-recall-memory-before-journal-drain-{time.strftime("%Y%m%d-%H%M%S")}.sqlite3'
dst.parent.mkdir(parents=True, exist_ok=True)
with sqlite3.connect(src) as source, sqlite3.connect(dst) as backup:
    source.backup(backup)
print(dst)
PY
```

Apply in bounded batches:

```bash
hermes-scope-recall journal digest \
  --hermes-home "$HERMES_HOME" \
  --extractor heuristic \
  --limit-entries 500
```

LLM extraction failures are not data rejections. Timeout, authentication, quota, and malformed-output failures return `status=error`, retain the source journal rows as pending, and record a sanitized digest-run error. Fix the model/provider cause before retrying; do not manually mark those rows processed. Retention cleanup is internally batched below SQLite's variable limit.

After drain:

```bash
hermes-scope-recall doctor --hermes-home "$HERMES_HOME" --source-root "$SCOPE_RECALL_ROOT" --json
```

If `database is locked` appears:

1. Inspect holders: `fuser -v "$HERMES_HOME/scope-recall/memory.sqlite3" "$HERMES_HOME/scope-recall/memory.sqlite3-wal" "$HERMES_HOME/scope-recall/memory.sqlite3-shm"`.
2. Do not force-kill the live gateway during an active user conversation.
3. Retry during a quiet window or after an explicit service restart authorization.

## candidate review

Inspect candidate memory debt:

```bash
hermes-scope-recall candidates report \
  --hermes-home "$HERMES_HOME" \
  --limit 100
```

Apply promotion/archival decisions only after reviewing samples:

```bash
hermes-scope-recall candidates apply \
  --hermes-home "$HERMES_HOME" \
  --limit 100
```

High candidate debt means Scope Recall is collecting useful possibilities but not promoting/rejecting them fast enough. Prefer small review batches over bulk promotion.

Relation-frequency maintenance isolates each dirty memory in a savepoint. A failed row is retried twice and becomes visible as `relation_frequency_failures.status=dead_letter` on the third failure while healthy rows continue. Repair the underlying row, trigger or schedule it again, and confirm doctor/schema status before removing failure evidence.

## playbook review

可复用经验手册（playbook）review should distinguish verified reusable procedures from one-off progress logs.

Current CLI surfaces bootstrap, list, dedupe, review, promote, quarantine, and supersede operations:

```bash
hermes-scope-recall playbooks bootstrap --dry-run
hermes-scope-recall playbooks list --hermes-home "$HERMES_HOME" --status candidate
hermes-scope-recall playbooks dedupe --hermes-home "$HERMES_HOME" --limit 20
hermes-scope-recall playbooks review --hermes-home "$HERMES_HOME" --id <playbook-id> --reason "reviewed by operator"
hermes-scope-recall playbooks promote --hermes-home "$HERMES_HOME" --id <playbook-id> --reason "verified successful reuse"
hermes-scope-recall playbooks quarantine --hermes-home "$HERMES_HOME" --id <playbook-id> --reason "stale or misleading"
hermes-scope-recall playbooks supersede --hermes-home "$HERMES_HOME" --id <duplicate-id> --superseded-by <canonical-id> --reason "duplicate group closeout"
hermes-scope-recall playbooks receipts --hermes-home "$HERMES_HOME"
hermes-scope-recall benchmark experience --case-file /path/to/experience-cases.json
```

When using provider tools, keep these lifecycle boundaries:

- create writes `candidate` by default.
- review/promotion/supersede requires explicit operator action.
- risky or stale-fact procedures should remain gated until verified.

For duplicate groups, prefer `playbooks dedupe` first, then `playbooks supersede` for the weaker/newer duplicate when a promoted canonical playbook already exists. Supersede keeps the row and writes `playbook_versions`; it does not delete evidence.

The raw command names are also useful for automation receipts: `playbooks list`, `playbooks review`, `playbooks dedupe`, `playbooks promote`, `playbooks quarantine`, `playbooks supersede`, and `playbooks receipts`.

Applied playbook changes commit an authoritative SQLite operator-ledger row with the business mutation. The filesystem JSON is a post-commit audit mirror, not the source of truth. `playbooks receipts` reports pending/failed mirror debt read-only; use `playbooks receipts --apply` to retry pending rows, and add `--include-failed` only after reviewing failed samples. A mirror failure never means the playbook mutation rolled back—check `operation_id`, `receipt_state`, doctor, and SQLite before retrying or compensating.

## migration and imports

Legacy hygiene migration and OpenClaw imports should be dry-run first; schema status is read-only:

```bash
hermes-scope-recall migrate status --hermes-home "$HERMES_HOME"
hermes-scope-recall migrate legacy --hermes-home "$HERMES_HOME"
hermes-scope-recall migrate apply --hermes-home "$HERMES_HOME"
hermes-scope-recall migrate openclaw-import \
  --source /path/to/openclaw/memory-lancedb-pro \
  --hermes-home "$HERMES_HOME" \
  --dry-run
```

`migrate status` reports the SQLite `schema_migrations` ledger through a read-only connection (`mode=ro` + `PRAGMA query_only=ON`) and does not repair missing metadata implicitly. `migrate legacy` runs the legacy-hygiene dry-run. `migrate apply` applies that legacy hygiene migration and creates its own backup unless `--no-backup` is explicitly passed. `migrate openclaw-import` maps to the OpenClaw importer; use dry-run first, then run vector repair after apply.

## vector repair

Vector repair must fail closed when the configured primary embedder is unavailable. Inspect first:

```bash
hermes-scope-recall vector repair \
  --hermes-home "$HERMES_HOME" \
  --dry-run
```

Apply only when the configured embedder is available or the fallback embedder is intentionally accepted:

```bash
hermes-scope-recall vector repair --hermes-home "$HERMES_HOME"
```

If fallback is intentional, use the script's explicit fallback flag rather than silently degrading semantic quality.

## governance cleanup

Governance cleanup targets historical pollution patterns such as transcript wrappers and old journal summary templates. It defaults to dry-run and soft archive.

```bash
hermes-scope-recall governance cleanup \
  --hermes-home "$HERMES_HOME" \
  --dry-run \
  --limit 500 \
  --format json
```

Apply with a named batch:

```bash
BATCH="cleanup-live-$(date +%Y%m%d_%H%M%S)"
hermes-scope-recall governance cleanup \
  --hermes-home "$HERMES_HOME" \
  --apply \
  --batch-id "$BATCH" \
  --limit 500 \
  --format json
```

Rollback that batch if needed:

```bash
hermes-scope-recall governance rollback \
  --hermes-home "$HERMES_HOME" \
  --apply \
  --batch-id "$BATCH" \
  --format json
```

Boundary: governance cleanup does not hard-delete; it soft-archives and writes audit events.

## backup/restore

Use SQLite online backup for live DB snapshots. Keep backup paths in final reports and classify them as `rollback-needed` when they are required for undo.

Restore sequence:

1. Stop or pause the Hermes process that owns the profile.
2. Copy the selected backup over `$HERMES_HOME/scope-recall/memory.sqlite3`.
3. Remove stale WAL/SHM files only while the DB is closed.
4. Run `hermes-scope-recall vector repair --hermes-home "$HERMES_HOME" --dry-run`.
5. Apply vector repair if the dry-run reports drift.
6. Run doctor and dashboard.

## orphan fact repair (explicit, never automatic)

`doctor.temporal.foreign_key_integrity` reports the current connection's FK state, the exact number of `PRAGMA foreign_key_check` violations, and a bounded sample. Any violation makes doctor fail closed. Scope Recall never deletes an orphan merely because doctor found it.

Repair procedure:

1. Pause the profile writer and acquire the normal maintenance/activation lease.
2. Create an SQLite online backup and record its absolute path, SHA-256, size, and source DB fingerprint.
3. Export the complete `PRAGMA foreign_key_check` result to an operator-owned evidence file. Do not rely only on the bounded doctor sample.
4. Classify every row by table, rowid, missing parent, and intended data owner.
5. Choose one explicit action per row:
   - restore/reconstruct the missing parent from a trusted backup or source record; or
   - quarantine the complete orphan row and its evidence/receipt graph, then delete it in one reviewed maintenance transaction.
6. Record an audit receipt containing the backup hash, pre-repair violation, chosen action, affected identifiers, actor/reason, transaction outcome, and post-repair checks. Do not put secrets or raw private chat text in the receipt.
7. Re-run doctor and require both `foreign_keys.enabled=true` and `violation_count=0`; then run the focused FK/cascade tests before resuming the writer.
8. On any mismatch or partial failure, rollback the transaction and restore the recorded backup. Never continue with a partially repaired graph.

## release checklist

Before release or handoff:

```bash
python3 -m ruff check .
python3 -m pyright
python3 -m pytest -q
python3 scripts/benchmark.golden.py --auto-explain-on-fail
python3 scripts/check.release.py --allow-dirty
hermes-scope-recall doctor --hermes-home "$HERMES_HOME" --source-root "$SCOPE_RECALL_ROOT" --json
```

Also verify:

- no `build/`, `dist/`, or stale temp files remain unless intentionally retained.
- package metadata version matches `plugin.yaml` and README release line.
- live doctor status is separated from source-tree release readiness.
- rollback/audit artifacts are reported with paths and reasons.

## cross-profile rollout

Use the rollout helper for inventory, canary install, backup receipts, and rollback:

```bash
hermes-scope-recall rollout profiles \
  --profiles-root "$HOME/.hermes/profiles"

hermes-scope-recall rollout profiles \
  --profiles-root "$HOME/.hermes/profiles" \
  --canary default \
  --apply \
  --receipt /tmp/scope-recall-rollout-default.json
```

For every target profile:

1. Identify profile home, plugin version, and configured provider from the dry-run report.
2. Canary one profile before batch rollout.
3. Keep the receipt; it records each previous plugin backup path. Rollback validates receipt target homes against `--profiles-root` and validates backup paths before deleting/replacing any current plugin.
4. Verify the canary/profile after install:

```bash
hermes-scope-recall verify --hermes-home /path/to/profile --runtime
hermes-scope-recall dashboard \
  --hermes-home /path/to/profile \
  --source-root "$SCOPE_RECALL_ROOT" \
  --format json
```

5. Roll back from the receipt if needed:

```bash
hermes-scope-recall rollout profiles \
  --rollback \
  --apply \
  --receipt /tmp/scope-recall-rollout-default.json
```

6. Run doctor and vector repair dry-run after each profile.

Do not assume another Hermes profile uses the same config or embedding credentials as the current profile. See `docs/cross-profile-rollout.md` for the full rollout/rollback procedure.

## Final closeout

After any operator task:

- run doctor or the focused relevant smoke.
- delete task-scoped temporary JSON files.
- retain online backups only when classified as `rollback-needed` or `audit-evidence`.
- report remaining risks, especially live service freshness or unrestarted processes.
