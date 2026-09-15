# P15 Legacy Migration Bridge — worker edit report

**tests NOT RUN (file-only worker); parent review/TEST execution pending**

This is the same unfinished incident as the prior unsuccessful repairs. This worker did not execute tests, migrations, or a production switch and cannot claim the defects are verified closed.

## Exact changed files
- `adapters/hermes/installation.py`
- `tests/migration/test_p15_bounded_archival.py`
- `docs/implementation-history/p15-bounded-legacy-bridge.md`

## Parent-run after checkpoint contract (same incident)
Parent reviewed and ran the affected tests: they passed. Prior 10-node result after the segment fix remains **3 pass, 7 blocked at receipt**; those seven later passed once `(0,-1,-1)` was accepted only for a non-`wal` journal. This note records that parent-run only. This worker did not rerun them.

## Real immutable snapshot (parent-executed; same incident)
The installed wheel then reached the real historical snapshot (not available to this worker). Catalog: 53 source scopes. Installer failed at `InstallationManifest.to_binding` → `InstanceBinding.__post_init__` (`contracts.py:450`) because 24 UTF-8 hex archive IDs exceeded 240 characters (max 403). Registered ID count was 56, below the 128-count limit. Only a TEST `installation.json` was written; no target database or migration occurred. Count was not the defect. No private snapshot, evidence, live memory, or credentials were read here.

## Parent-run 10-node after segment fix (same incident)
Parent: **3 pass, 7 blocked at receipt**. Catalog provenance, Core forget/suppress, and both-parent-bridge allocation passed. The seven CLI nodes failed only in `_checkpoint_existing_memory_db` on SQLite `(0,-1,-1)` for a rollback-journal target (15 persisted sources, zero WAL bytes). That tuple is the non-WAL checkpoint result, not a failed TRUNCATE.

## Parent-run after fixture correction (same incident)
Parent then ran the focused files: **17 affected nodes, 8 pass, 9 fail**. One sentinel provenance mismatch (`procedural_playbooks.shared_scope_id` empty string). Eight executions hit `UNIQUE(source_group_key,source_revision,segment_index)` at `migrate_v2.py` insert because attached digest bridges reused the parent memory's `(group, revision, segment_index=0)`.

## Parent-run fixture failure (same incident)
Earlier parent run: **32 tests, 17 failed, 0 errors, 0 skipped**. All 17 failed in setup with `sqlite3.IntegrityError: NOT NULL constraint failed: governance_audit_events.event_type` at the simplified governance insert. Official 578b already has that table (`event_type`/`action`/`created_at` NOT NULL, no default) and nullable `relation_scope_statistics.scope_id`.

## Parent-review corrections (unverified by this worker; same incident)
- Identical-run reuse fails closed on a nonempty `memory.sqlite3-wal` without checkpointing or writing. Receipt production checkpoints then binds only the main `memory.sqlite3` file; a completed WAL TRUNCATE is `(0,0,0)`, and `(0,-1,-1)` is accepted only for a non-`wal` journal.
- Focused test file: blob `typeof(scope_id)` for the malformed-scope case; `row_factory` on the second forget connection; every SQLite connection is closed explicitly; one held-open WAL CLI refusal case.

## Edits attempted (unverified)
- `main(--archive-install-test)` no longer accepts a preexisting report after caller hash/target equality alone. It now requires 64-hex source/catalog formats, recomputes both, requires an absolute TEST target, then binds reuse to source/catalog, installed manifest bytes, batch key, report bytes, and `memory.sqlite3` after a write-side checkpoint. Missing/changed evidence, a planted report without a receipt, or a stale source fail closed without creating or overwriting the target. A receipt is written only after `completion_status=complete`, exclusively. Omitted `--report` uses `scope-recall/p15-archive-migration-report.json` under the TEST target.
- `migrate_legacy()` treats catalog `malformed_non_string_identity` as a pre-write block in ordinary mode before `str()` scope coercion. Archive `scope_ids` now distinguish omitted vs explicit empty/subset; items are materialized once and reject non-strings/duplicates. Catalog `total_nonempty_raw_values` excludes the empty-string scope without dropping sentinel/occurrence maps. Digest retention remains an early block; blocked `counts.unmapped` matches the per-table `unmapped` list. Attached digest bridges copy parent `source_group_key`/scope/project/branch/`scope_authorization`; orphans still create no memory; quarantine/runs stay `read_blocked`.
- Installer builder/loader now reject raw `allowed_scope_ids` duplicates/non-strings before normalization, non-Mapping retention input, and explicit null archive fields as distinct from absent keys.

## Not done by this worker
- No tests, CLI, or SQLite migration were executed here.
- No production install, promotion, or automatic cutover.
- No private snapshot or live profile was read.

## Pending parent verification
- Run `tests/migration/test_p15_bounded_archival.py`, including `test_long_archive_scopes_fit_binding_without_runtime_grants`.
- Do not treat this edit as green until that parent run.

## This correction
Files: `adapters/hermes/installation.py`, `tests/migration/test_p15_bounded_archival.py`, `docs/implementation-history/p15-bounded-legacy-bridge.md`.

`build_archive_scope_id` still emits the existing length-prefixed UTF-8 hex ID when that string fits 240 characters. Longer originals keep the exact source string as the `archive_source_map` key and receive `archive|sha256:` plus the full SHA-256 of those UTF-8 bytes. No strip/normalize, no collapse to `owner_private`, no widened global 240/128 contracts. Manifest value-collision rejection remains mandatory. One focused regression covers long ASCII, multibyte, exact leading/trailing keys, reload/`to_binding`, distinct bounded outputs, and no runtime archive grants.
