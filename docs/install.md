# Install Scope Recall

This guide covers three supported installation paths for `scope-recall`, the Hermes memory provider distributed as the Python package `hermes-scope-recall`.

## 1. Standard Hermes installation

Install the package into the same Python environment that runs Hermes, then install and activate the provider in one command:

```bash
python -m pip install "hermes-scope-recall[lancedb]"
hermes-scope-recall install --activate --hermes-home "${HERMES_HOME:-$HOME/.hermes}" --json
hermes-scope-recall verify --runtime --hermes-home "${HERMES_HOME:-$HOME/.hermes}"
```

`install --activate` performs these operations:

- copies the plugin into `$HERMES_HOME/plugins/scope-recall`;
- sets `memory.provider: scope-recall` in `$HERMES_HOME/config.yaml`;
- bootstraps `$HERMES_HOME/scope-recall/memory.sqlite3` with the SQLite truth and journal schema;
- creates sqlite-bruteforce vector metadata only for a provably fresh, empty bootstrap when that companion is configured directly or as the fallback;
- returns JSON with install, config, schema, verification, backup, and rollback evidence.

Before creating a backup or replacing any target, install/upgrade runs a read-only N-1 compatibility preflight. It validates the existing runtime config against the candidate schema and physically checks every existing READY vector generation with a bound preflight receipt. Unknown legacy keys, malformed config, missing receipts, identity drift, physical vector mismatch, or manifestless non-empty vector state fail before the target or backup tree is changed. The command reports a rebuild/migration next step; it never fabricates a receipt for an unverified legacy generation.

The command captures activation pre-state **before** replacing the plugin or applying schema work:

- an existing plugin copy is copied into the installer backup tree;
- `config.yaml` and the provider `config.json` are captured with their prior existence state; when either path is a symlink, both link identity and dereferenced target bytes/mode are snapshotted and independently verified during rollback;
- an existing SQLite truth DB is captured with SQLite's online backup API and verified with `PRAGMA quick_check`;
- LanceDB and sqlite-bruteforce companion generations are fingerprinted as rebuildable state.

Activation against an existing `memory.sqlite3` is refused unless the operator passes `--maintenance-mode`. This flag confirms that the gateway and every Scope Recall writer have already been stopped. Before the online backup starts, the installer atomically creates `$HERMES_HOME/scope-recall/.activation-maintenance.json`, acquires a SQLite writer lock, invalidates cached write statements, and installs temporary INSERT/UPDATE/DELETE guard triggers on every ordinary truth table. The matching token is passed explicitly only to the installer-owned bootstrap connection; sibling threads, same-context ordinary connections, ordinary provider connections, raw connections, and older SQLite writers fail at the guard. The offline backup copy is stripped of those temporary triggers before it becomes rollback material. The live lease and guards remain active through commit or compensation. A successful commit removes the guards before releasing the lease; a failed drift preflight retains both for manual recovery.

The copy stage normalizes directory owner permissions in the staging tree before atomic replacement. This allows installation from immutable or read-only package/source directories without making copied source files more permissive.

`config.yaml` is parsed as a single, duplicate-free YAML mapping. Block and inline `memory` mappings, quoted keys, comments, and unrelated settings are preserved. Malformed YAML and constructs that cannot be rewritten losslessly (including duplicate keys, multi-document input, anchors, and aliases) fail closed. The updated config is written to a same-directory temporary file, flushed with `fsync`, and atomically replaced. Parent-directory `fsync` remains a durability barrier where the platform supports it; on Windows or filesystems that reject directory `fsync`, a completed replacement is reported as success rather than as a contradictory failure.

If config writing, schema bootstrap/migration, provider loading, or runtime verification fails after confirmed maintenance entry, the installer first runs a no-write compensation preflight. It compares the live logical SQLite fingerprint with the latest explicitly registered activation-owned write epoch. With no drift, it restores the plugin, both config files, and SQLite state. Any unregistered post-snapshot write stops compensation before vector, plugin, config, or database state is changed, returns `rollback_failed` with `manual_recovery_required=true`, preserves the current cross-surface generation, and retains the maintenance lease. When compensation is allowed, a vector companion changed during the failed activation is discarded rather than presented as the same generation; the `activation_transaction.vector_companions` receipt marks `rebuild_required` and emits a repair command. The command returns `ok=false` rather than claiming activation, and `activation_transaction` records the failure, snapshot paths, per-surface restoration result, and manual recovery commands. A failure while creating the pre-state snapshot aborts before plugin replacement.

The standalone `rollback --backup-dir ...` command restores a plugin copy only. For activation-state recovery, use the `activation_transaction` receipt. Automatic SQLite compensation is permitted only for snapshots captured after confirmed writer quiescence; an unconfirmed snapshot never overwrites post-snapshot truth. Manual SQLite restore commands are offline recovery evidence and must not be run against an active writer.

## 2. Verify, upgrade, and rollback

Run structural verification:

```bash
hermes-scope-recall verify --hermes-home "${HERMES_HOME:-$HOME/.hermes}" --json
```

Run runtime verification:

```bash
hermes-scope-recall verify --runtime --hermes-home "${HERMES_HOME:-$HOME/.hermes}" --json
```

Runtime verification reports layered diagnostics:

- `plugin_files` — required plugin files, manifest, discovery marker;
- `provider_load` — provider import and schema loading;
- `config_load_errors` — candidate-schema compatibility diagnostics for the existing runtime config;
- `hermes_config` — whether `memory.provider` is set to `scope-recall`;
- `sqlite_truth` — SQLite DB path and schema migration status;
- `tool_schemas` — compact public tool schema coverage;
- `vector_companion` — configured vector backend and initialization status.

Upgrade with a dry-run first:

```bash
hermes-scope-recall upgrade --hermes-home "${HERMES_HOME:-$HOME/.hermes}" --dry-run --json
# Stop the gateway and all Scope Recall writers before the next command.
hermes-scope-recall upgrade --hermes-home "${HERMES_HOME:-$HOME/.hermes}" --activate --maintenance-mode --json
```

If the dry-run reports a missing active vector manifest while SQLite truth or a companion already contains state, keep the existing companion untouched and use the candidate's migration script to build and validate a shadow generation before retrying the upgrade:

```bash
python scripts/migrate.vector_generation.py --hermes-home "${HERMES_HOME:-$HOME/.hermes}" --dry-run --json
python scripts/migrate.vector_generation.py --hermes-home "${HERMES_HOME:-$HOME/.hermes}" --apply --activate --json
```

The apply step must run under the operator's maintenance boundary. It CAS-activates the new generation from an empty current pointer and retains any manifestless legacy companion for audit instead of adopting or deleting it.

If the upgrade replaced an existing plugin copy, use the emitted rollback command after a dry-run check:

```bash
hermes-scope-recall rollback --hermes-home "${HERMES_HOME:-$HOME/.hermes}" --backup-dir /path/to/backup/scope-recall --dry-run --json
hermes-scope-recall rollback --hermes-home "${HERMES_HOME:-$HOME/.hermes}" --backup-dir /path/to/backup/scope-recall --json
```

## 3. Multi-profile rollout

For Hermes profile homes under `~/.hermes/profiles/*`, use rollout planning first:

```bash
hermes-scope-recall rollout profiles \
  --profiles-root "$HOME/.hermes/profiles" \
  --plan \
  --json
```

Apply to a canary profile:

```bash
hermes-scope-recall rollout profiles \
  --profiles-root "$HOME/.hermes/profiles" \
  --profile default \
  --apply \
  --receipt /tmp/scope-recall-rollout-default.json
```

Rollback from the receipt:

```bash
hermes-scope-recall rollout profiles \
  --profiles-root "$HOME/.hermes/profiles" \
  --rollback \
  --receipt /tmp/scope-recall-rollout-default.json \
  --apply
```

See [`cross-profile-rollout.md`](cross-profile-rollout.md) for the full safety model.

## 4. Native-free vector fallback

If LanceDB or PyArrow native wheels are not suitable for the host, install without the LanceDB extra and configure the SQLite brute-force companion:

```bash
python -m pip install hermes-scope-recall
hermes-scope-recall install --activate --hermes-home "${HERMES_HOME:-$HOME/.hermes}" --json
```

Then set the runtime config in `$HERMES_HOME/scope-recall/config.json`:

```json
{
  "vector": {
    "backend": "sqlite-bruteforce"
  }
}
```

Run verification again:

```bash
hermes-scope-recall verify --runtime --hermes-home "${HERMES_HOME:-$HOME/.hermes}" --json
```

## 5. Optional PGVector companion

Use PGVector only when the deployment already operates PostgreSQL with the pgvector extension. SQLite remains the source of truth; PGVector is a rebuildable semantic-search companion.

```bash
python -m pip install "hermes-scope-recall[pgvector]"
# Authenticate with .pgpass, a PostgreSQL service, or your secret manager.
export SCOPE_RECALL_PGVECTOR_DSN='postgresql://user@host:5432/database'
hermes-scope-recall install --activate --hermes-home "${HERMES_HOME:-$HOME/.hermes}" --json
```

Runtime config:

```json
{
  "vector": {
    "backend": "pgvector",
    "fallback_backend": "sqlite-bruteforce",
    "pgvector": {
      "dsn_env": "SCOPE_RECALL_PGVECTOR_DSN",
      "table_name": "scope_recall_vectors"
    }
  }
}
```

See [`vector-backends.md`](vector-backends.md) for backend behavior and repair notes.

## 6. Development checkout

For local development, install editable mode and then copy/activate the plugin into a throwaway Hermes home:

```bash
git clone https://github.com/410979729/scope-recall-hermes.git
cd scope-recall-hermes
python -m pip install -e ".[dev,lancedb]"
hermes-scope-recall install --activate --hermes-home /tmp/scope-recall-hermes-home --json
hermes-scope-recall verify --runtime --hermes-home /tmp/scope-recall-hermes-home --json
python -m pytest -q tests/test_installer.py tests/test_rollout_profiles.py
```

Do not use a production Hermes home for development smoke tests unless you have a backup and an explicit rollback plan.

## Naming reference

- Python distribution: `hermes-scope-recall`
- Python import/package: `scope_recall`
- Hermes plugin/provider ID: `scope-recall`
- Runtime storage: `$HERMES_HOME/scope-recall/`
- Installed plugin copy: `$HERMES_HOME/plugins/scope-recall/`
