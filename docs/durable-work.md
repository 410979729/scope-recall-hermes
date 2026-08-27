# Durable work contract

Scope Recall uses one behavioral contract for bounded background work while each
domain keeps its own physical persistence, transactions, payload shape,
provenance, retention, and indexes. Program 2 does not introduce a universal
`durable_jobs` table.

The shared descriptor freezes work identity, idempotency key, scope and
authority snapshots, policy version, generation, upper bound, item-set hash,
and creation time. A worker claim is valid only for the exact worker, immutable
lease token, lease generation, and expiry recorded by the owning domain. A
reclaim advances the generation exactly once and uses a new token, so a stale
worker cannot complete or fail newer work.

Items use `pending`, `processing`, `retry`, `completed`, `poisoned`,
`cancelled`, or `superseded`. The final four states are terminal and cannot be
revived. Error classification is limited to `retriable`, `permanent`, `poison`,
`authority_revoked`, `epoch_mismatch`, `dependency_unavailable`, and
`contention`. Current authority is revalidated by the domain before a write;
revocation cannot inherit the authority frozen when work was created.

Every adapter exposes a content-free health envelope with item counts, oldest
age, progress, lease expiry and contention counts, recoverability, operator
action, and fairness metadata. Doctor consumes that envelope. Relation, vector,
journal/nightly, governance, and fact backfill remain independent adapters and
may be rolled back without migrating another domain's payload.
