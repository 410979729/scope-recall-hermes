# Lifecycle operation registry

Scope Recall defines each lifecycle operation once in `lifecycle_registry.py`.
Runtime producers select an `operation_id`; they do not invent governance
`event_type` or `action` strings at the call site.

The executable registry owns, for every operation:

- its domain, permitted source states, and target-state rule;
- its historical V1 event/action identity and authorization policy;
- whether Fact authority is mandatory and whether rollback is available;
- projection effects and receipt policy.

`lifecycle_service.py` resolves and validates the operation before mutation.
Governance archive coverage, default rollback eligibility, Doctor health, the
producer census, and contract tests are derived from the same definitions.
An unknown operation or V1 receipt pair fails closed before a transaction is
opened.

## V1 compatibility

The 2.0 compatibility window preserves existing governance receipt identities:
the Registry is an internal authority and does not add, rename, rewrite, or
migrate V1 receipt fields. `lifecycle_compat.py` is the only adapter that may
accept a registered raw event/action pair. Its removal is tracked in the
compatibility registry for the post-2.0 window.

Default rollback remains limited to the exact Registry-derived archive sources.
Rollback still requires a receipt bound to current SQLite truth; a current-state
drift or arbitrary third-party archive receipt is not trusted.

## Adding an operation

Add the operation to `LIFECYCLE_REGISTRY`, bind its producer in
`LIFECYCLE_PRODUCER_CENSUS`, and use its `operation_id` at the write call. Update
the focused contract tests when the operation intentionally changes governance
coverage or rollback. A new raw event/action at a producer is rejected by the
AST contract test.
