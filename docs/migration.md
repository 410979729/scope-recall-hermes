# Migration guide

This document describes how to move into `scope-recall` from earlier local experiments.

## Supported migration paths

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
- a companion LanceDB index rebuilt from the SQLite truth layer
- Hermes-style runtime scope fields such as `platform`, `user_id`, `chat_id`, `thread_id`, `gateway_session_key`, `agent_identity`, and `agent_workspace`

So OpenClaw import must be an explicit transform, not a folder copy.

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
- expected SQLite/LanceDB paths under `$HERMES_HOME/scope-recall/`

## Importing OpenClaw history

A one-shot importer is provided at:

- `scripts/import.openclaw.memory_lancedb_pro.py`

It is intentionally conservative:

- reads from an explicit OpenClaw LanceDB directory
- transforms rows into `scope-recall` SQLite truth rows
- records import provenance in an `import_ledger`
- uses stable source fingerprints so repeated runs are idempotent
- leaves vector sync to `scope-recall` initialization after import

Before using it for real data, decide:

- which OpenClaw instance(s) are authoritative
- how upstream `scope` values map to Hermes `scope_id`
- whether to preserve all categories or filter to a subset
- whether imported records should use their original timestamps or import-time timestamps

## Important warning

Do **not** point `scope-recall` directly at an OpenClaw `.lance` directory and call it done.

That would bypass:

- SQLite truth/audit expectations
- Hermes scope isolation fields
- curated-memory authority boundaries
- future debugging and migration guarantees

Use explicit import, then verify the resulting `scope-recall` store.
