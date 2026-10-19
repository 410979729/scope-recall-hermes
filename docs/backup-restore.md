# Backup and Disaster Recovery

ScopeRecall provides consistent, self-contained backup bundles and disaster recovery restoration to fresh destinations.

## Architecture and Scope

A ScopeRecall backup bundle is a single archive holding:
- A consistent SQLite snapshot of the truth database (`memory.sqlite3`), taken via SQLite's online backup interface while holding truth writer and background worker locks.
- Instance configuration documents, with embedded secret bodies refused or redacted to reference names.
- Retained attachments (`retained/`).
- Pending capture queues (`inbox/`), remote outboxes (`outbox/`), and retry buffers (`retry/`).
- Model budget ledgers (`budget/`).
- Optional vector companion directories (`vectors/`).
- A cryptographic manifest with SHA-256 digests and file sizes for every archive member.

Application-level payload encryption is deliberately outside ScopeRecall's process boundary. Operators should store backup archives on encrypted filesystems, BitLocker/LUKS volumes, or hardware-encrypted external storage.

## Creating Backup Bundles

### Manual Creation

To create a full backup bundle from a shared store root:

```bash
scope-recall backup create --root /path/to/shared-store --output /var/backups/scope-recall-backup-20261019T140000Z.zip
```

To include vector companion directories directly in the archive:

```bash
scope-recall backup create --root /path/to/shared-store --output /var/backups/scope-recall-backup-20261019T140000Z.zip --with-vectors
```

When `--with-vectors` is omitted, the backup remains compact and portable. Restoring without vector files automatically enqueues background embedding rebuilds for all eligible sources.

For local stores attached to a single agent (such as Hermes or Codex):

```bash
scope-recall backup create --host hermes --instance-root ~/.hermes --output /var/backups/hermes-backup.zip
```

### Automatic Daily Upkeep and Rotation

When a backup directory is declared in `backup-config.json` inside the instance data directory:

```json
{
  "backup_dir": "/var/backups/scope-recall"
}
```

The background worker automatically performs daily maintenance during upkeep passes:
1. Deletion Ledger Mirroring: Appends any recent deletion operations to `deletion-mirror.jsonl` in the configured backup directory.
2. Daily Bundle Creation: Generates a timestamped full backup bundle once per UTC day without vector weight.
3. Retention Rotation: Automatically rotates verified owned bundles in the backup directory, retaining 7 daily and 4 weekly archives. Pruning uses individual file unlinks; no recursive or bulk cleanup commands are ever issued.

## Verifying Backup Bundles

To inspect and cryptographically verify a backup bundle without modifying any store:

```bash
scope-recall backup verify --bundle /var/backups/scope-recall-backup-20261019T140000Z.zip
```

Verification validates:
- Manifest syntax and format compatibility.
- Presence, size, and SHA-256 digest of every member file.
- SQLite structural integrity (`PRAGMA quick_check`) on a temporary read-only snapshot.

An optional `--temp-restore-check` flag unpacks all members to an isolated temporary sandbox to verify archive decompression.

## Disaster Recovery and Migration

To restore an instance to a fresh target directory on the same or a new machine:

### 1. Plan and Validate First

```bash
scope-recall restore --bundle /var/backups/scope-recall-backup-20261019T140000Z.zip --to /srv/scoperecall-fresh
```

Without `--apply`, the command only validates the bundle, checks that the target destination is fresh and empty, verifies the deletion mirror if present, and reports the restoration plan.

### 2. Apply Restoration

```bash
scope-recall restore --bundle /var/backups/scope-recall-backup-20261019T140000Z.zip --to /srv/scoperecall-fresh --apply
```

Restoration performs the following deterministic sequence:
1. Validates bundle checksums and database integrity.
2. Extracts files exclusively into the fresh target directory.
3. Rewrites internal paths in `instance_meta` (SQLite database) and configuration files (`runtime-config.json`, `shared-manifest.json`, etc.) to point to the new destination.
4. If vectors were omitted from the bundle, enqueues embedding work items so the background worker rebuilds vector indices cleanly.
5. Replays post-backup deletion ledger records from `deletion-mirror.jsonl` so that any items forgotten after the snapshot was created remain forgotten.
6. Runs instance diagnostic checks.
7. Emits operator re-registration commands for attached agents and outputs recovery gap details.

### 3. Re-register Client Entries

Disaster recovery does not execute live registration, host restarts, or service modifications. The operator executes the printed re-registration command for each host:

```bash
scope-recall attach --root /srv/scoperecall-fresh --host codex --instance-root ~/.codex
scope-recall attach --root /srv/scoperecall-fresh --host hermes --instance-root ~/.hermes
```

## Understanding the Recovery Gap

A restored instance represents the exact state captured at `recovery_point_utc`.

When a deletion mirror is present, deletions committed after the backup are replayed so deleted content is never resurrected. However, normal source captures, claim updates, and session turns created between the backup timestamp and the restore moment cannot be reconstructed from backup alone. The restore output explicitly details this `unrecoverable_later_interval` with start, end, and duration.
