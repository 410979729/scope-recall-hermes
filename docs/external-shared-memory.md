# External shared-memory bridge

Scope Recall is local-first: SQLite remains the source of truth for each Hermes profile. The external shared-memory bridge is a contract for exporting reviewed durable facts to another backend without making that backend authoritative.

## Contract v1

Schema version: `external_shared_memory_export.v1`

Exported records must include:

- `id`: Scope Recall memory id.
- `target`: one of `user`, `memory`, `project`, or `ops`.
- `content` and `summary`: the reviewed durable memory text.
- `metadata`: memory type, quality signals, entities, tags, lifecycle, and sensitivity classification.
- `provenance`: original scope id, source, timestamps, source trust, and origin marker.
- `conflict_policy`: required import/export conflict behavior.

## Safety rules

- Export durable targets only: `user`, `memory`, `project`, and `ops`.
- Do not export `general` scratch by default.
- Skip hidden lifecycle rows: archived, candidate, in-progress, obsolete, rejected, or superseded.
- Skip restricted or secret-reference rows until an operator has an explicit vault/export policy.
- Reject plaintext secret-looking content with the `plaintext_secret_rejected` reason before export.
- Treat sensitivity as one of `public`, `internal`, `secret_reference`, or `restricted`.
- Require `vault_ref` for `secret_reference` metadata.
- Preserve provenance so receiving systems can audit where a fact came from.
- Require a conflict policy before producing a payload.
- Record an explicit governance audit event when `record_audit=True` is used for an export.

Supported conflict policy values:

- `manual_review`: receiving side must queue conflicts for operator review.
- `prefer_local`: receiving side keeps its local row when an imported record conflicts.
- `prefer_newer`: receiving side may prefer the row with the newest provenance timestamp.

## Python API

Read-only preview:

```python
from scope_recall.external_bridge import build_external_memory_export_preview

payload = build_external_memory_export_preview(
    conn,
    accessible_scope_ids=["scope-a"],
    conflict_policy="manual_review",
)
```

Preview helpers are strictly read-only: they do not write to SQLite, vector companions, or remote services. When an operator wants an auditable export receipt, use the explicit receipt helper instead:

```python
from scope_recall.external_bridge import build_external_memory_export_with_receipt

payload = build_external_memory_export_with_receipt(
    conn,
    accessible_scope_ids=["scope-a"],
    conflict_policy="manual_review",
    actor="operator",
    batch_id="export-batch-1",
)
```

The legacy `build_external_memory_export(..., record_audit=False)` entry point remains default read-only for compatibility. Passing `record_audit=True` records a governance audit event, commits that audit row, and returns `read_only=false` plus `audit_recorded=true`.

## JSONL exchange examples

The repository includes safe JSONL examples for bridge implementers:

- `examples/external_bridge/import.jsonl`
- `examples/external_bridge/export.jsonl`
- `examples/external_bridge/conflict_resolution.jsonl`

Example rows use `schema_version` `scope-recall.external-memory.v1` and include a `bridge_action` value of `import`, `export`, or `conflict_resolution`. Each row carries `tenant_id`, `external_user_ref`, `agent_identity`, and `workspace_id` so external backends can enforce tenant and workspace boundaries. The `external_user_ref` field is a pseudonymous external user reference, not a raw email address or platform user id.

Rows must also include metadata with an `identity_safety` block and a `redaction_policy` block. The redaction policy records whether a row is sanitized or redacted and whether secret-like values were detected. Conflict examples document policies such as `central-backend-wins`; production importers should map these examples to their own conflict-review workflow rather than overwriting local SQLite truth directly.

## Future backend adapters

The contract is backend-neutral. Specific adapters, such as PostgreSQL shared tables, should consume this payload and keep their own import audit trail. Backend adapters must not change the v1 safety rules unless they introduce a new schema version.

## PostgreSQL adapter prototype

The optional `PostgresSharedMemoryBridge` publishes contract-v1 payloads into a PostgreSQL table. It does not require `psycopg` at import time; install and configure PostgreSQL support only for deployments that need a shared backend.

```python
from scope_recall.external_bridge import build_external_memory_export_preview
from scope_recall.postgres_bridge import PostgresSharedMemoryBridge

payload = build_external_memory_export_preview(
    conn,
    accessible_scope_ids=["scope-a"],
    conflict_policy="manual_review",
)

bridge = PostgresSharedMemoryBridge()
bridge.open()
try:
    receipt = bridge.publish_export(payload)
finally:
    bridge.close()
```

Default DSN environment variable:

```text
SCOPE_RECALL_POSTGRES_BRIDGE_DSN
```

The example schema is available at `examples/external_bridge/postgres_schema.sql`.
