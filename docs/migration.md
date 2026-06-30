# Migration guide

This document describes how to move into `scope-recall` from earlier local experiments.

## Supported migration paths

### Schema migration status

Before mutating older profile data, inspect the SQLite schema ledger without repairing it implicitly:

```bash
python scripts/migrate.status.py --hermes-home /path/to/hermes-profile
# or, after installing the package:
hermes-scope-recall migrate status --hermes-home /path/to/hermes-profile
```

The status command opens the SQLite truth DB read-only (`mode=ro` + `PRAGMA query_only=ON`) and reports `schema_migrations`, `user_version`, and missing baseline metadata. Use `scripts/doctor.py` or installer runtime verify for the broader health view.

### 0. Legacy raw/general/scratch hygiene inside an existing `scope-recall` SQLite store

Use this when an older pre-journal or experimental `scope-recall` database already contains raw `general` scratch rows or durable rows with incomplete governance metadata.

A conservative migrator is provided at:

- `scripts/migrate.legacy_hygiene.py`

Default mode is read-only:

```bash
python scripts/migrate.legacy_hygiene.py --hermes-home /path/to/hermes-profile
```

Apply mode creates a SQLite backup first, then updates metadata only:

```bash
python scripts/migrate.legacy_hygiene.py --hermes-home /path/to/hermes-profile --apply
python scripts/repair.vector_index.py --hermes-home /path/to/hermes-profile
```

The migrator does not delete memory content. It marks legacy `general`/raw/scratch rows as `lifecycle=archived, category=legacy-scratch`, fills missing `lifecycle`/`category` metadata for durable rows, and records a `legacy_hygiene` receipt in row metadata. Rebuild the configured vector companion afterwards so archived/general scratch rows do not remain in semantic retrieval state.

### 1. Legacy local `lancepro` profile storage

Implemented automatically on first initialization.

If these exist:

- `$HERMES_HOME/lancepro/memory.sqlite3`
- `$HERMES_HOME/lancepro/config.json`

and the new target files do not yet exist, `scope-recall` will:

- copy `memory.sqlite3` into `$HERMES_HOME/scope-recall/memory.sqlite3`
- copy `config.json` into `$HERMES_HOME/scope-recall/config.json`
- expose the migration result in `scope_recall_stats`

This path preserves local provider-owned SQLite history from the older name.

### 2. OpenClaw `memory-lancedb-pro` historical vector stores

This is **not automatic**.

OpenClaw historical memory stores are not drop-in compatible with `scope-recall` because they use a different schema and different truth assumptions.

Known OpenClaw LanceDB source rows commonly include fields such as:

- `id`
- `text`
- `vector`
- `category`
- `scope`
- `importance`
- `timestamp`
- `metadata` (JSON string)

`scope-recall` expects:

- SQLite truth rows as the durable authority
- a configured companion vector index rebuilt from the SQLite truth layer
- Hermes-style runtime scope fields such as `platform`, `user_id`, `chat_id`, `thread_id`, `gateway_session_key`, `agent_identity`, and `agent_workspace`

So OpenClaw import must be an explicit transform, not a folder copy.

### 3. Canonical cross-platform durable identity mapping

This is **opt-in** and does not require rewriting existing rows. Configure explicit aliases only after deciding which platform accounts belong to the same human:

```json
{
  "identity": {
    "cross_platform_shared_scope": true,
    "cli_user_id_fallback": "local",
    "user_aliases": {
      "telegram:user_123": "canonical_user_123",
      "cli:local": "canonical_user_123"
    }
  }
}
```

After enabling it, new durable `user`/`memory`/`project`/`ops` rows use the canonical durable scope. Existing platform-specific durable rows remain readable through query-time aliases, so operators can verify behavior before planning any explicit repair/migration. `general` scratch and raw journal evidence remain platform/account/chat/session local.

## Recommended migration order

1. Switch runtime provider to `scope-recall`
2. Let local `lancepro` storage auto-migrate if present
3. Verify the new provider works on live traffic first
4. Import OpenClaw historical records only after deciding scope remapping rules
5. Rebuild the companion LanceDB index from imported SQLite truth

## Verifying a local rename migration

Use:

```bash
hermes memory status
```

And, from the active Hermes home:

```bash
HERMES_HOME=/path/to/profile venv/bin/python - <<'PY'
import json
from plugins.memory import load_memory_provider
p = load_memory_provider('scope-recall')
p.initialize('migration-check', hermes_home='/path/to/profile')
print(p.handle_tool_call('scope_recall_stats', {}))
p.shutdown()
PY
```

Look for:

- `provider: scope-recall`
- `migration.migrated: true` when old local storage was copied
- expected SQLite/vector companion paths under `$HERMES_HOME/scope-recall/`

## Importing OpenClaw history

A safe import guide is provided at:

- CLI: `hermes-scope-recall migrate openclaw-import`
- Script: `scripts/import.openclaw.memory_lancedb_pro.py`

The importer is intentionally conservative:

- defaults to dry-run/inspection; pass `--apply` explicitly to write
- reads from an explicit OpenClaw LanceDB directory
- transforms rows into `scope-recall` SQLite truth rows
- records import provenance in an `import_ledger`
- uses stable source fingerprints so repeated runs are idempotent
- enforces a target/category allowlist (`memory`, `ops`, `project`, `user` by default)
- rejects raw role-prefix transcripts even when they claim a durable target
- blocks apply when secret-like, path-like, or template-like content/metadata is detected
- stores source metadata only after recursive redaction
- creates a unique online SQLite backup before applying to an existing target DB
- can write an import receipt JSON via `--receipt <path>` containing inserted/skipped row ids, fingerprints, backup checksum, lint/rejection details, and graph/vector repair guidance
- records the recommended post-import vector repair command in the receipt

Dry-run first:

```bash
hermes-scope-recall migrate openclaw-import \
  --source /path/to/openclaw/memory-lancedb-pro \
  --hermes-home /path/to/hermes-profile
```

Apply only after reviewing rejections/lint findings and scope mapping:

```bash
hermes-scope-recall migrate openclaw-import \
  --source /path/to/openclaw/memory-lancedb-pro \
  --hermes-home /path/to/hermes-profile \
  --scope-prefix imported.openclaw \
  --allow-target memory \
  --allow-target ops \
  --apply \
  --receipt /path/to/openclaw-import-receipt.json \
  --vector-repair dry-run
```

After a successful apply, run the receipt's vector repair command (normally a dry-run first, then apply when reviewed):

```bash
hermes-scope-recall vector repair --hermes-home /path/to/hermes-profile --dry-run
```

Before using it for real data, decide:

- which OpenClaw instance(s) are authoritative
- how upstream `scope` values map to Hermes `scope_id` via `--scope-prefix`
- which categories are safe to import via repeated `--allow-target`
- whether any secret/path/template lint findings require source cleanup or manual redaction
- whether imported records should use their original timestamps or be treated as historical context

## Important warning

Do **not** point `scope-recall` directly at an OpenClaw `.lance` directory and call it done.

That would bypass:

- SQLite truth/audit expectations
- Hermes scope isolation fields
- curated-memory authority boundaries
- future debugging and migration guarantees

Use explicit import, then verify the resulting `scope-recall` store.
