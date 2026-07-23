# Vector backends

Scope Recall keeps SQLite memory rows as the source of truth. Vector stores are rebuildable companion indexes used for hybrid retrieval. If a companion becomes stale or unavailable, rebuild it from SQLite instead of treating vector rows as durable memory.

## Built-in backend choices

- `lancedb`: default vector companion. This is suitable for local deployments with compatible LanceDB/PyArrow wheels.
- `sqlite-bruteforce`: dependency-free fallback. This is slower but portable and safe for small to medium local stores.
- `pgvector`: optional PostgreSQL/pgvector companion for operators who already run PostgreSQL.

The alias `sqlite` is normalized to `sqlite-bruteforce`.

## Generation pinning and fallback

On a fresh setup with no active generation, Scope Recall may use the explicitly configured fallback backend and/or fallback embedder to create a real, empty generation. This lets credential-free installs complete with a companion that can accept the first memory instead of reporting `not_initialized`.

Once a generation is active, its backend, model, dimensions, metric, prompt profile, prefixes, and dimension-request behavior are pinned by the SQLite generation manifest. Later startup selects only a configured embedder that exactly matches that manifest. Restoring a primary credential does not silently replace an active fallback generation, and a different-space fallback cannot access an existing primary generation. Use the generation migration/activation workflow to switch embedding spaces or backends.

### Manifestless upgrade state

Automatic bootstrap is allowed only when SQLite truth and every inspectable configured companion are empty. If the active generation manifest is missing while truth, a local companion path, or a remote companion may already contain state, setup and upgrade preflight fail closed before choosing an embedding identity. Do not delete or rename the old companion to bypass this check. Build a shadow generation from SQLite truth, validate its physical receipt, and activate it with compare-and-swap:

```bash
python scripts/migrate.vector_generation.py --hermes-home "${HERMES_HOME:-$HOME/.hermes}" --dry-run --json
python scripts/migrate.vector_generation.py --hermes-home "${HERMES_HOME:-$HOME/.hermes}" --apply --activate --json
```

Run the apply step only after the dry-run identifies the intended backend/model/dimensions and the target runtime is under the operator's maintenance boundary. The old manifestless companion is retained for audit; it is never silently adopted or deleted.

## Future backend reservations

`qdrant` and `chroma` are reserved names for possible future optional backends. They are not runtime backends in this release, do not add dependencies, and should continue to fail fast as unsupported if configured directly. This keeps the VectorStore interface open without expanding the supported operational surface prematurely.

## PGVector setup

Install the optional dependency group when PGVector support is needed:

```bash
python3 -m pip install 'hermes-scope-recall[pgvector]'
```

Configure the vector backend and DSN environment variable name:

```json
{
  "vector": {
    "backend": "pgvector",
    "fallback_backend": "sqlite-bruteforce",
    "pgvector": {
      "dsn_env": "SCOPE_RECALL_PGVECTOR_DSN",
      "table_name": "scope_recall_vectors",
      "connect_timeout_seconds": 10,
      "statement_timeout_ms": 30000,
      "lock_timeout_ms": 5000
    }
  }
}
```

Then export the DSN before starting Hermes:

```bash
export SCOPE_RECALL_PGVECTOR_DSN='postgresql://user:password@host:5432/database'
```

Fresh setup or an explicit generation migration creates the PGVector `vector` extension and configured companion table. Runtime startup for an active generation opens only the exact existing table and fails closed if it is missing or incompatible. Every connection has an explicit connection timeout plus PostgreSQL statement and lock timeouts, so an unavailable database or blocked DDL/DML cannot hold the vector replay worker indefinitely. The table is a cache: run vector repair to rebuild it from SQLite truth after changing embedding dimensions, table names, or backend configuration.

## Repair behavior

Inspect first:

```bash
hermes-scope-recall vector repair --dry-run
```

Apply explicitly:

```bash
hermes-scope-recall vector repair apply
```

For local LanceDB and sqlite-bruteforce companions, repair backs up local companion files before rebuild unless `--no-backup` is passed. PGVector uses a remote database table, so local file backup is not available; use database-native backup or snapshots if an operator needs rollback beyond rebuilding from SQLite truth.

## Operational notes

- Keep `vector.embedder.dimensions` aligned with the backend table dimensions.
- Use `fallback_backend: sqlite-bruteforce` to let a provably fresh setup establish its first generation when an optional backend is unavailable. An already active generation never switches backend during startup.
- Do not delete SQLite memory rows to repair a vector index. Delete or rebuild only the companion index.
