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
- creates sqlite-bruteforce vector metadata when that companion is configured directly or as the fallback;
- returns JSON with install, config, schema, verification, backup, and rollback evidence.

The command backs up an existing `config.yaml` before changing it. If an existing plugin copy is replaced, the result also includes a plugin rollback command.

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
- `hermes_config` — whether `memory.provider` is set to `scope-recall`;
- `sqlite_truth` — SQLite DB path and schema migration status;
- `tool_schemas` — compact public tool schema coverage;
- `vector_companion` — configured vector backend and initialization status.

Upgrade with a dry-run first:

```bash
hermes-scope-recall upgrade --hermes-home "${HERMES_HOME:-$HOME/.hermes}" --dry-run --json
hermes-scope-recall upgrade --hermes-home "${HERMES_HOME:-$HOME/.hermes}" --activate --json
```

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
export SCOPE_RECALL_PGVECTOR_DSN='postgresql://user:[password]@host:5432/database'
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
