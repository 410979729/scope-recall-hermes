# External shared-memory integration contract

`scope-recall` is a local-first Hermes memory provider. It is designed to run inside each Hermes profile as the per-agent recall layer, not as a distributed cluster memory authority.

If your deployment already has a shared center such as PostgreSQL, Redis-backed knowledge services, or another multi-agent memory backend, keep that system as the cross-agent source of truth and bridge to `scope-recall` deliberately.

## Responsibilities

### External shared backend owns

- cross-agent source of truth
- global knowledge synchronization
- permissions and tenancy
- cluster-scale conflict policy
- PostgreSQL-scale indexing, audit, and retention
- fan-out/fan-in across many Hermes instances

### `scope-recall` owns

- current-turn recall for one Hermes runtime
- local SQLite truth rows for provider-owned memory
- LanceDB companion retrieval index
- local scratch isolation
- per-user/per-agent durable recall
- scoped import/export/tool surfaces
- doctor, repair, inspect, explain, and benchmark utilities

## Safe synchronization targets

External bridge code may choose to synchronize durable rows with these targets:

```text
user
memory
project
ops
```

Do not synchronize these by default:

```text
general
raw system output
raw tool output
secret-like records
temporary chat/thread scratch
```

`general` is local scratch. It should remain inside the current runtime scope unless an operator deliberately promotes a sanitized item into a durable target.

## Recommended modes

### Read-only bridge

Use this when a central backend should inform a Hermes instance without letting local recall mutate global truth automatically.

1. External backend selects durable facts for one user/agent/workspace.
2. Bridge writes sanitized rows with `scope_recall_store` or imports them into SQLite truth.
3. `scope-recall` recalls them locally for the current query.
4. Local updates are reviewed before being sent back to the external backend.

### Writeback bridge

Use this only when the external backend has a clear conflict policy.

1. Local agent writes durable `user`/`memory`/`project`/`ops` rows.
2. Bridge exports only durable rows, never `general` scratch.
3. External backend resolves duplicates/conflicts.
4. Accepted central facts are written back or re-imported with source/trust metadata.

## Conflict policy hooks

Bridge code should decide and document at least one policy:

- central backend wins
- curated user memory wins
- newest durable fact wins
- highest source-trust row wins
- user-confirmed rows supersede agent/tool-derived rows
- conflicts are only marked, never auto-deleted

`scope-recall` can expose evidence through `scope_recall_inspect`, `scope_recall_explain`, `memory_relations`, and `memory_feedback`, but it should not become the global conflict resolver.

## Source trust guidance

Recommended source ordering for cross-system imports:

1. explicit user-confirmed central records
2. Hermes curated memory (`USER.md` / `MEMORY.md`) when allowlisted
3. operator-reviewed `scope-recall` durable rows
4. tool-derived facts with sanitized evidence
5. raw assistant inference

Bridge metadata should preserve provenance whenever possible:

```json
{
  "source_system": "central-postgres",
  "source_trust": 0.9,
  "import_mode": "read_only",
  "external_record_id": "..."
}
```

## Minimal JSONL shape

A bridge-friendly export/import row should include:

```json
{
  "id": "local-or-external-id",
  "target": "project",
  "content": "SQLite remains the truth layer for scope-recall; LanceDB is rebuildable.",
  "summary": "scope-recall truth/vector boundary",
  "memory_type": "project",
  "entities": ["scope-recall", "SQLite", "LanceDB"],
  "tags": ["memory-architecture"],
  "source": "operator-reviewed",
  "updated_at": "2026-06-03T00:00:00Z",
  "metadata": {
    "source_system": "central-postgres",
    "source_trust": 0.9
  }
}
```

## Example JSONL bridge fixtures

Safe, synthetic bridge fixtures live under:

- `examples/external_bridge/import.jsonl`
- `examples/external_bridge/export.jsonl`
- `examples/external_bridge/conflict_resolution.jsonl`

They are documentation fixtures, not live synchronization scripts. They use demo tenant, user, agent, and workspace identifiers only.

### JSONL schema

Each line is one JSON object. Bridge code may extend the object, but the example contract keeps these fields stable:

- `schema_version`: currently `scope-recall.external-memory.v1`
- `bridge_action`: `import`, `export`, or `conflict_resolution`
- `record_id`: local bridge fixture id, not a live user identifier
- `target` / `memory_type`: one of durable `user`, `memory`, `project`, or `ops`; never `general`
- `content` and `summary`: sanitized text safe for local recall
- `tenant_id`: external tenant boundary used before any import/export
- `external_user_ref`: pseudonymous external user reference; do not use raw email, phone, chat handle, or profile path
- `agent_identity` and `workspace_id`: synthetic or deployment-scoped routing values
- `entities`, `tags`, `source`, `updated_at`: recall and provenance helpers
- `metadata.source_system`, `metadata.source_trust`, and an external/local record id for provenance
- `metadata.identity_safety`: documents tenant/user/workspace boundary checks
- `metadata.redaction_policy`: records whether text is sanitized/redacted and whether secret-like values were removed

### Import example

`examples/external_bridge/import.jsonl` shows a read-only central import. The bridge imports one sanitized project fact for one `tenant_id` and `external_user_ref`, preserving `source_system`, `source_trust`, and `external_record_id` metadata.

### Export example

`examples/external_bridge/export.jsonl` shows a writeback proposal. The bridge exports only durable `memory` content after operator review, skips `general` scratch and raw tool output, and records `export_mode` plus destination metadata for the central backend to review.

### Conflict resolution example

`examples/external_bridge/conflict_resolution.jsonl` shows a conflict marker rather than an auto-delete. It documents a `central-backend-wins` policy, the winning and losing record ids, and the action a bridge should take (`mark_local_superseded_then_reimport_winner`). Other deployments can choose different policies, but the policy must be explicit and auditable.

### Tenant/user identity safety

External bridges must resolve `tenant_id`, `external_user_ref`, and `workspace_id` before importing or exporting. Treat mismatches as hard failures. Keep raw account identifiers outside `scope-recall`; use a pseudonymous external user reference and retain the reversible mapping only in the external authority that already owns tenancy and permissions.

### Redaction policy

Run redaction before writing JSONL or calling `scope_recall_store`. The bridge should drop raw tool output, raw system output, local filesystem paths, credentials, and secret-like substrings. If evidence must be preserved, store only a sanitized summary in `content` and keep raw evidence in the external system under its access controls. Mark the result in `metadata.redaction_policy` with `state`, `contains_secret_like_values`, and the applied rule.

## Cluster deployment pattern

For clusters, keep the shared center outside `scope-recall` and use `scope-recall` as the local recall/cache/tooling layer with explicit boundaries.

Recommended split:

- central backend: global durable truth, permissions, tenancy, synchronization, and cross-agent conflict policy
- `scope-recall`: current-turn recall, local SQLite/LanceDB state, scoped tools, operator diagnostics, and bridge-friendly import/export rows
- Hermes native skills: procedural knowledge packaging and reusable workflows

This keeps the local memory provider small, inspectable, and safe to run per agent while still allowing larger deployments to integrate it with their existing shared-memory infrastructure.
