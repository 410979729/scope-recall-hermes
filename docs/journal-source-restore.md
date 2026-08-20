# Journal source-restore

`journal source-restore` copies a pre-approved window of `journal_entries` and
`journal_digest_runs` from a trusted, checkpointed SQLite snapshot into an
offline target database. It is a maintenance action, not dead-letter
`journal recovery`, and it never calls `append_journal_entry`, capture
sanitizers, `now_iso`, digest execution, or raw operator SQL.

Default mode is dry-run and query-only. Planning opens source and target
read-only and acquires no activation lease. Apply requires every gate below
and otherwise performs zero target mutation.

## Canonical set digests

Half-open windows select rows: `start <= timestamp < end`. Journal rows use
`created_at`. Digest runs use `started_at`. Both windows must be non-empty
and strictly ordered (`start < end`); equal, reversed, or blank bounds fail
closed with a bounded window error. Unbound rows before a valid window are ignored
and do not by themselves cause refusal. An explicit excluded unknown-tail
window is different: every journal/digest row in that half-open range must
already exist on the target with exact logical equality, or planning and
apply refuse (`excluded_tail_missing` / `excluded_tail_conflict`). Changing
a selected window so the approved count/digest no longer matches fails
closed. Window bounds must be timezone-aware ISO/RFC3339; naive timestamps
are refused.

Serialization is UTF-8 JSON with `sort_keys=True` and compact separators
`("," , ":")`. The set digest is SHA-256 of those records joined by `\n`.

Journal set digest binds the existing unique identity plus original time, in
this field order inside each object:

- `scope_id`
- `session_id`
- `turn_number`
- `role`
- `content_hash`
- `created_at`

Records sort by `created_at`, `scope_id`, `session_id`, `turn_number`, `role`,
`content_hash`.

Digest-run set digest binds every stored logical field and excludes no
semantic column: `id`, `started_at`, `finished_at`, `status`, `extractor`,
`interval_label`, `processed_entries`, `inserted`, `updated`, `skipped`,
`error`, `metadata`. Records sort by `started_at`, `id`.

Source binding also includes the checkpointed file SHA-256, a schema digest
of `sqlite_master` user objects, and `PRAGMA user_version`. Any present `-wal`/`-shm` or rollback `-journal` sibling is refused,
including zero-byte files and symlinks. Source opens use a dedicated
`file:` URI with `mode=ro&immutable=1` after binding regular-file identity
(`st_dev`/`st_ino`/size/mtime/SHA-256). Ordinary read-only truth openers
are not used on source: they can materialize sidecars on a WAL-header
main-only snapshot. Plan and pre-lock target inspection bind regular-file
identity and a coherent logical epoch from a checkpointed main-only
artifact using the same sidecar-safe reader; they must not create
``-wal``/``-shm``/``-journal`` siblings. A dirty or non-checkpointed
target is refused before that reader is opened and is never treated as
immutable. Apply preflight operator-ledger lookup uses that same
sidecar-safe reader on a main-only target. Ordinary read-only truth open
is used for that lookup only when siblings already exist, so WAL-visible
committed rows remain readable for retry/reconciliation. The early
same-operation checkpoint exemption is WAL-only (``-wal`` and/or
``-shm``, including symlink presence). A rollback ``-journal`` never uses
that exemption and still fails ``target_wal_incoherent``. The lock-held
apply-time epoch is computed on the already-authorized writer connection
inside its ``BEGIN IMMEDIATE``, not by opening a second ordinary
read-only target connection. Writer-bound identity recapture after the
initial target-epoch capture detects unrelated committed drift that lands
before that hash is taken. A cooperative-writer window remains between
that hash and ``BEGIN IMMEDIATE``; this command does not treat every
TOCTOU interleaving as impossible.

Source health, schema/`user_version`, approved row selection, and the file
SHA are bound to one snapshot. Identity is recaptured after the immutable
read; sidecar or file-identity drift refuses `source_snapshot_changed`.

## Referential, conflict, and schema gates

Every non-empty incoming `journal_entries.processed_run_id` must resolve to
a digest-run that is either in the approved selected source set or already
present on the target with exact logical equality. An omitted, conflicting,
or otherwise unresolved referenced digest fails closed before backup or
write (`dangling_digest_reference`). Stored `content_hash` must equal
`sha256(content.encode("utf-8"))`.

Target epoch binds schema/`user_version`, file SHA-256, `sqlite_sequence`
for `journal_entries`, and logical counts/digests of the protected tables
including `memories_fts` and `operator_operations`. Drift in any of those
bindings refuses apply.

A target row with the same journal identity or digest ID but different
semantic data is a planning conflict: dry-run returns `ok=false`,
`verdict=not_ready`, and the bounded conflict counts. It is never `ready`.

The target schema digest and `PRAGMA user_version` must match the bound
source contract before classification or apply. This command does not bump
schema.

## Apply gates

Apply requires all of:

- explicit `dry_run=False` and `--maintenance-confirmed`
- an activation/maintenance lease owned by the current process and bound to
  the target path
- the dedicated truth-writer role `journal_source_restore`; foreign writer
  contention fails closed
- a caller-supplied `--operation-id` reused for retry/reconciliation
- the same source snapshot and approved selection counts/digests used for
  planning
- an exact apply-time target epoch (schema/`user_version`, relevant table
  counts/digests including FTS and operator ledger, `sqlite_sequence`, and
  the checkpointed file SHA-256)
- a verified SQLite online backup created after leases, under an empty
  `BEGIN IMMEDIATE` writer fence, after the final target-epoch recheck, and
  before any restore DML

This command does not add a restore table or bump schema. The business
inserts and one `operator_operations` row (`operation_kind=journal.source_restore`)
share the same `BEGIN IMMEDIATE` transaction. Ledger JSON stores only
hashed remap evidence (salted pairs plus mapping count/digest), never raw
integer IDs. Same operation ID plus the same request fingerprint
reconciles after commit without a second backup; the same ID with a
different fingerprint refuses. Committed ledger is consulted before
stale-prewrite epoch rejection so process death between commit and
mirror/output can recover. Local mirrors use `operator_receipt.v1` and
`operator.source_restore.<id>.json` without the remap pair list; existing
playbook receipt names and `playbook_operator_receipt.v2` stay unchanged.

The CLI owns that PID-bound activation lease: dry-run never acquires it; on
`--apply --maintenance-confirmed` the CLI process acquires it atomically,
calls the domain function in the same PID, and releases the exact lease on
every success, refusal, or exception path. A foreign or existing lease emits
one bounded JSON object and a nonzero exit.

Existing target journal/digest rows are never updated, deleted, or replaced.
Journal inserts omit source integer IDs so target AUTOINCREMENT allocates new
IDs. Digest-run TEXT IDs are preserved. Transaction order is: missing
referenced digest rows, then journal rows, then missing unreferenced error
receipts, then the operator ledger row, then one commit. Empty
`processed_run_id` is preserved. Inserts are classify-then-plain-INSERT;
exact already-present rows are idempotent, and logical mismatches fail
closed.

`journal_session_digest_state` is not an epoch, protected, or nontarget
table: restore never copies it. After journal inserts, the same apply
transaction may `DELETE` a session cursor only when restored unprocessed
rows or ID remaps make a prior high cursor unsafe. Processed-only and
unaffected sessions keep their cursors. The public receipt reports
`cursor_reset_count` and a secret-free `cursor_reset_digest` of hashed
`(scope_id, session_id)` identities; raw scope or session values never
appear. The same two fields are persisted in the private
`operator_operations.result_json` so WAL/ledger reconciliation and
same-operation replay reconstruct them byte-for-byte. Ledger rows that
predate the fields default to `0` and an empty digest without failing.
A rolled-back apply restores those cursors with the rest of the
transaction.

The authorized target writer holds the empty `BEGIN IMMEDIATE` fence through
backup verification and the restore inserts, then commits once. Backup uses
a separate reader connection; no DML occurs before the verified backup.
Backup failure rolls back the empty transaction and inserts nothing.

Machine output is one JSON receipt. Public receipts may contain only
stage/verdict/status, booleans, counts, canonical digests, redacted error
codes, epoch and backup/batch digests, `remapping_occurred`,
`mapping_count`, `mapping_digest`, `cursor_reset_count`,
`cursor_reset_digest`, `operation_id`, `request_fingerprint`,
and receipt-repair flags. Hashed remap pairs stay in the private
`operator_operations.result_json` only. Public JSON and the generic
filesystem mirror (`operator.source_restore.<id>.json`) omit the pair
list; they keep `mapping_count` and `mapping_digest`. Existing playbook
receipt bytes and `playbook_operator_receipt.v2` stay unchanged. This
command does not repair FTS; a successful apply only states that normal
aftercare verification is required.

If lease release fails after a committed apply, the receipt preserves the
committed counts/digests, sets `error_code=activation_lease_cleanup_failed`
and `status=manual_recovery_required`, returns nonzero, and must not be
treated as a clean success. It does not leak the lease token or path.

If the truth-writer lease context, writer close, or receipt finalization
fails after the restore transaction is known committed, the receipt is
`ok=false`, `stage=apply`, `status=manual_recovery_required`,
`verdict=applied_cleanup_failed`, and `error_code=committed_cleanup_failed`.
It keeps the known operation id, request fingerprint, inserted counts,
mapping digest, and receipt state. It must never be labeled
`apply_rolled_back`, and it must not restore or re-run business DML.
A pre-commit unexpected error may still be `apply_rolled_back` after an
actual rollback. A commit exception whose durable readback is
indeterminate stays `commit_outcome_unknown`. A known commit whose only
debt is the filesystem mirror stays `committed_receipt_debt`.
