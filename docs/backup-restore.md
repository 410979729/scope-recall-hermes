# Backup and disaster recovery

Keep the backup archive and its current `deletion-mirror.jsonl` together. The archive recovers saved memories and settings. The mirror records later deletions so forgotten memories stay deleted. Recovery requires both files and writes into a new or empty directory.

## Choose a backup location

Use a disk that will survive losing the original computer. Keep another copy of the archives and the latest mirror on separate storage. Copying an archive alone loses its protection against later deletions. If the archive and mirror live in different directories, supply the mirror's location when restoring.

Archives contain private memories and attachments and are not encrypted. Use an encrypted disk, such as a BitLocker or LUKS volume, and restrict access. Credential values, including passwords and access keys in HTTP addresses, are redacted from configuration; credential reference names remain. A backup refuses credential files that overlap a copied resource, including aliases to the same file. Move those credential files outside the copied directories before creating a backup, and reconnect the references on the recovery computer.

A bundle contains the database, installation settings, registered entries and grants, retained attachments, pending database captures, saved retry files, and auxiliary budget ledgers, including committed ledger writes still waiting in SQLite's write-ahead log. It also saves each included worker's installation-bound UTC day and count of daily queue items already used or reserved for a pass, so a same-day recovery keeps the remaining daily work allowance. Later spending and work performed after the snapshot are unavailable. Vectors are optional. A retry held only in a running process cannot be backed up: let the host finish saving before a planned move.

## Create a backup

For a shared store:

```sh
scope-recall backup create --root /path/to/shared-store --output /path/to/backups/memory.zip
```

For a local installation, select its host and home:

```sh
scope-recall backup create --host hermes --instance-root /path/to/agent1 --output /path/to/backups/memory.zip
```

Use `--host codex` for a local client installation. Choose a new output filename for each manual backup; existing archives are never overwritten, including files created while a backup is being assembled. The destination filesystem must support hard links for atomic publication. Backups require exclusive access to the store. If the command reports that it is busy, let the current write finish and try again.

Add `--with-vectors` to copy the vector files too. Without it, recovery queues their rebuilding; source text remains searchable while semantic search catches up.

For unsent uploads from a remote client on this computer, add `--client-config /path/to/client.json`. Repeat this option for each client. Each upload is checked with capture admission's secret policy: refused or unreadable entries are omitted individually, and transport payloads are removed from the included entries. The receipt counts redacted and omitted spool entries. Originals stay on the client. Its credential file must be outside every copied resource. A server cannot back up unsent uploads still on another computer. Save those files there before retiring it.

## Daily backups and retention

The first manual backup sets its output directory as the backup directory if none is configured. The receipt reports the deletion mirror's location. Keep that mirror even if later manual archives are written elsewhere.

The existing background worker wakes for a backup once per UTC day and for deletion mirror catch-up, including when no model work is queued. A scheduled backup has its own 30-minute job budget, separate from model work and daily queue allowances; the operator's background pause also cancels it. A published archive is recorded before retention begins, and concurrent mirror upkeep preserves that record, so interrupted retention does not publish another archive that day. Verification progress for retention is saved and reused while each archive is unchanged. It retains the latest seven daily snapshots and one snapshot from each of the latest four weeks; these selections can overlap. Only verified, automatically named archives belonging to this writable copy take part in rotation. A recovered copy has its own retention and background-task identity. Manual names such as `memory.zip`, other copies' archives and unreadable archives are left alone.

To choose a directory explicitly, edit `backup-config.json` beside `memory.sqlite3`:

```json
{
  "backup_dir": "/path/to/backups",
  "snapshots_enabled": true,
  "client_configs": ["/path/to/client.json"]
}
```

Use absolute paths. Omit `client_configs` when this computer has no remote-client uploads to save. A local Hermes store keeps this file in `scope-recall/` under its home; a local client store keeps it in `data/` under its home. A shared store keeps it directly under the store root. Set `snapshots_enabled` to `false` to disable automatic snapshots while keeping `backup_dir` configured for deletion protection. Removing that directory or setting it to `null` after a backup leaves later deletions unprotected, and `doctor` reports a warning. A store that has never been backed up has no archive that could bring a deletion back; `doctor` only notes that its deletions are not mirrored.

A deletion succeeds even if the backup disk is unavailable, and other writes never wait for the mirror: right after the deletion one short attempt, of about a tenth of a second, copies it, and the background worker copies whatever is left every five minutes while it runs. Rolling back to an older snapshot is mirrored too, so a later recovery keeps what the rollback withdrew. The mirror and its checkpoint must be ordinary files with a single name: a hard link, symbolic link or junction in their place stops mirroring with an error instead of writing through it. `doctor` reports records that have not reached the mirror yet. Resolve that warning before relying on a backup for a planned move.

Failed daily backups are recorded in `backup-state.json` beside the database. Ordinary failures retry after a delay. A backup that exceeds its job budget pauses automatic snapshots, and `doctor` reports the budget problem; mirror catch-up continues. Check the disk's availability, speed and free space and create a manual backup. To allow another scheduled attempt after resolving the cause, set `backup_blocked` to `false` in `backup-state.json`. Retention failures keep the publication checkpoint and retry retention separately. The worker must remain running for scheduled backups to occur.

## Verify and rehearse recovery

```sh
scope-recall backup verify --bundle /path/to/backups/memory.zip
```

Verification checks the manifest, the exact archive contents, every file's size and SHA-256 digest, and the database's integrity, schema and installation identity. Unexpected, missing, duplicated or unsafe archive paths are rejected. Creation and verification limit a bundle to 100,000 members, 256 GiB of expanded data and an 8 MiB manifest.

Rehearse the complete recovery in a temporary directory:

```sh
scope-recall backup verify --bundle /path/to/backups/memory.zip --temp-restore-check
```

The rehearsal uses the same path relocation, deletion replay, vector rebuilding queue and diagnostic checks as a real recovery. It reports its results without registering or starting a host. It requires the current mirror beside the archive, or `--mirror /path/to/deletion-mirror.jsonl` when it is elsewhere.

## Restore on another computer

Install Scope Recall on the recovery computer and make the archive and latest mirror available there. Recovery requires the same operating-system family as the source: restore Windows bundles on Windows, Linux bundles on Linux and macOS bundles on macOS. The source system is recorded in the manifest; a different family refuses recovery before extraction. First check the recovery plan:

```sh
scope-recall restore --bundle /path/to/backups/memory.zip --to /path/to/recovered
```

Without `--apply`, this checks the bundle, mirror and destination without extracting anything. A missing mirror or one belonging to another installation refuses recovery. Use `--mirror /path/to/deletion-mirror.jsonl` if the mirror is elsewhere. Keep that exact filename when copying it; other mirror filenames are refused before extraction.

For a shared store, `--to` names its new store root. For a local installation, it names the new host home: Hermes recovers its store under `scope-recall/`, and a local client under `data/`. The receipt's `data_directory` gives the actual store location.

Apply the recovery:

```sh
scope-recall restore --bundle /path/to/backups/memory.zip --to /path/to/recovered --apply
```

Recovery relocates bundled resource paths, preserves the installation identity, replays the mirror's deletions and runs store diagnostics. Vector paths come from the embedding-space identity. If vectors were omitted, recovery queues their rebuilding. Host bindings, diagnostic checks and the recovery receipt are prepared while ordinary admission remains closed. The receipt is saved as `recovery-receipt.json` beside the database before admission reopens. A failure before that point keeps the destination fenced against ordinary use. Automatic snapshots are disabled in the recovered settings; deletion protection continues at the supplied mirror. The relocated client configurations are retained in `backup-config.json`, so subsequent backups also include their spools without repeating `--client-config`. Select a new backup directory before enabling automatic snapshots.

The receipt includes commands to reconnect each entry. Replace bracketed placeholders with the new computer's paths. Local installations print `plan-install` and `apply-install` commands. Shared stores print one `attach` command per active entry, including its identifier and display name. Reattachment keeps recovered grants and budget history; entries stay inactive until attached.

Reconnect the credential references named in the receipt. Review remote addresses and project mappings when those locations also moved. Local client project mappings are in `codex-installation.json` under the recovered home; recovered remote-client settings are listed in the receipt. Recovery does not restart hosts or services. After reinstallation, run the normal host `doctor` and check a real recall before enabling ordinary use.

## What recovery loses

`recovery_point_utc` is the snapshot time. Captures and updates after it cannot be reconstructed from this archive, even when a newer deletion mirror is available. The receipt reports that lost interval separately from `deletion_protection`, which describes the supplied mirror and the deletions replayed from it.

Only deletions that reached the supplied mirror can be replayed. Its latest deletion time cannot prove that a lost computer had no newer deletion. Unsent uploads and retries outside the files saved in the bundle are unavailable too. Keep the mirror current and check `doctor` before a planned move.

For a rollback while the original store still exists, use the existing snapshot `restore` command. It reads the current store's deletion ledger before returning to the older snapshot. Disaster recovery uses `restore --bundle ... --to ...` because the original store is unavailable.
