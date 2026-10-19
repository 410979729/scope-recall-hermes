# Backup and Disaster Recovery

Keep both the backup archive and its current `deletion-mirror.jsonl`. The archive restores memories and configuration; the mirror keeps later deletions from returning. Restore always writes into a new or empty location.

## Architecture and Scope

A ScopeRecall backup bundle is a single archive holding:
- A consistent SQLite snapshot of the truth database (`memory.sqlite3`), taken via SQLite's online backup interface while holding truth writer and background worker locks.
- Instance configuration documents, with embedded secret bodies refused or redacted to reference names.
- Retained attachments (`retained/`).
- Pending captures already in SQLite and the Hermes retry buffer's durable `kept-captures/` files. A retry still held only in a running process is outside a disk backup; let the host finish saving before a planned move.
- Registered shared-entry configuration, authorization and independent budget ledgers, plus the shared worker's budget. SQLite ledgers are snapshotted with their committed WAL contents.
- Remote-client spools explicitly selected with `--client-config`. A server cannot recover unsent captures from another computer's disk.
- Optional vector companion directories (`vectors/`).
- A cryptographic manifest with SHA-256 digests and file sizes for every archive member.

The archive is not encrypted. Use an encrypted disk such as a BitLocker or LUKS volume, restrict access to backups, and protect the mirror alongside them. Configuration credentials are redacted; credential reference names remain, and the referenced credential files are never copied. Memories and retained attachments remain private content in the archive.

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

To include the pending uploads of a remote client accessible on this computer, add `--client-config /path/to/client.json`. Repeat the option for each client. Its configured `state_dir/spool` is included; its `token_file` contents are not read. Run a backup where those files are accessible before retiring a remote computer. Do not assume a server-only archive contains offline clients' unsent data.

For local stores attached to a single agent (such as Hermes or Codex):

```bash
scope-recall backup create --host hermes --instance-root ~/.hermes --output /var/backups/hermes-backup.zip
```

### Automatic Daily Upkeep and Rotation

The first manual bundle records its output directory in `backup-config.json` when no backup directory is configured. Keep the reported mirror at that configured location even when later archives are written elsewhere. The worker uses this directory for daily backups. You can also set it explicitly inside the instance data directory:

```json
{
  "backup_dir": "/var/backups/scope-recall",
  "client_configs": ["/path/to/client.json"]
}
```

The optional `client_configs` list includes those local clients' spools in daily backups too. Leave it empty when this computer holds no remote-client spool.

The existing background worker wakes for backup upkeep even when no model work is queued:
1. Deletion Ledger Mirroring: Appends any recent deletion operations to `deletion-mirror.jsonl` in the configured backup directory.
2. Daily Bundle Creation: Generates a timestamped full backup bundle once per UTC day without vector weight.
3. Retention Rotation: Retains the newest 7 daily and 4 weekly snapshots of this installation. Foreign, unreadable and manually named files are left alone.

A failed daily backup is recorded in `backup-state.json` and retried after a delay. Snapshot copying and hashing stop at the worker pass's deadline; a manual backup has no worker-pass limit. A deletion succeeds even if its mirror cannot be written. Doctor reports the missing mirror records, and later upkeep attempts to copy them again. Resolve that warning before relying on the mirror for a planned move.

## Verifying Backup Bundles

To inspect and cryptographically verify a backup bundle without modifying any store:

```bash
scope-recall backup verify --bundle /var/backups/scope-recall-backup-20261019T140000Z.zip
```

Verification validates:
- Manifest schema, canonical document checksum and format compatibility.
- Presence, size, and SHA-256 digest of every member file.
- SQLite structural integrity (`PRAGMA quick_check`), schema and installation identity on a temporary read-only snapshot.

An optional `--temp-restore-check` flag unpacks all members to an isolated temporary sandbox to verify archive decompression.

## Disaster Recovery and Migration

To restore an instance to a fresh target directory on the same or a new machine:

### 1. Plan and Validate First

```bash
scope-recall restore --bundle /var/backups/scope-recall-backup-20261019T140000Z.zip --to /srv/scoperecall-fresh
```

Without `--apply`, the command validates the bundle and deletion mirror, checks that the target is new or empty, and reports the plan. The mirror is required. It is found beside the archive, or supplied explicitly with `--mirror /path/to/deletion-mirror.jsonl`. A missing or unrelated mirror refuses recovery.

For a shared store, `--to` is the new store root. For a local installation, it is the new host home: Hermes stores its recovered files in `scope-recall/`, and Codex stores them in `data/` with its installation configuration beside that directory. The receipt's `data_directory` names the actual store.

### 2. Apply Restoration

```bash
scope-recall restore --bundle /var/backups/scope-recall-backup-20261019T140000Z.zip --to /srv/scoperecall-fresh --apply
```

Restoration performs the following deterministic sequence:
1. Validates bundle checksums and database integrity.
2. Extracts files exclusively into the fresh target directory.
3. Rewrites SQLite and installation paths to the new store, and points runtime budgets and client spools at their recovered files. The stable installation identifier remains the same so source references and deletion groups still match.
4. If vectors were omitted from the bundle, enqueues embedding work items so the background worker rebuilds vector indices cleanly.
5. Replays post-backup deletion ledger records from `deletion-mirror.jsonl` so that any items forgotten after the snapshot was created remain forgotten.
6. Runs instance diagnostic checks.
7. Emits operator re-registration commands for attached agents and outputs recovery gap details.

### 3. Re-register Client Entries

The receipt includes `reregistration_argv` and shell-quoted `reregistration_commands`. Substitute the bracketed home, interpreter, plugin and project paths with the intended destinations. Local installations use `plan-install` followed by `apply-install`; shared stores print one `attach` command for every active entry, including its original identifier and name:

```bash
scope-recall attach --root /srv/scoperecall-fresh --host hermes --entry agent1 --display-name agent1 --instance-root /srv/agent1 --runtime-config-from /srv/scoperecall-fresh/entries/agent1/runtime-config.json
```

Shared-entry grants stay inactive until that explicit attachment. Reattachment preserves the recovered grants and spend history. Review external project mappings in a local client's `codex-installation.json` when project directories also moved. Restore cannot infer where projects now live.

Restore does not restart hosts or services. Reconnect the credential references reported in the receipt before enabling remote clients or model services. Check remote-server configuration and addresses for the new machine. Run the normal host doctor and a real recall after reinstallation. Automatic backups are disabled in the recovered configuration until you choose a new backup directory, so the restored copy cannot write into the old machine's backup location.

## Understanding the Recovery Gap

A restored instance represents the exact state captured at `recovery_point_utc`.

Use the latest mirror: only deletions that reached that mirror can be replayed. A stale copy cannot prove that no newer deletion occurred on a lost machine. The receipt reports the latest mirrored time. Normal captures and updates after the recovery point cannot be reconstructed from this archive; the receipt reports that later interval with its start, end and duration. Remote uploads and retries still held only outside the saved files are also unavailable.

The existing snapshot rollback command remains separate: use it when the original store still exists and you want to return to an older database snapshot while preserving the current deletion ledger.
