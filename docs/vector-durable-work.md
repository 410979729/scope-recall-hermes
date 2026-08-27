# Vector durable-work adapter

Scope Recall keeps the vector causal outbox as its physical persistence and its
sole ordinary vector-write executor. Program 2 adds a read-only adapter over
that existing boundary; it does not add a generic jobs table and does not move
or duplicate event payloads.

## Contract mapping

- One existing outbox event is one finite durable-work descriptor and item.
- `event_key` remains the idempotency key. The descriptor freezes the event,
  generation, memory identifier, and operation identities; its upper bound is
  exactly one item.
- Native `pending`, `processing`, `retry`, `completed`, and `dead_letter` map to
  shared `pending`, `processing`, `retry`, `completed`, and `poisoned` states.
- Payload and `last_error` text are never read into the shared descriptor, item,
  receipt, or Doctor envelope. Shared errors use stable content-free codes.
- Health is scoped to the current generation. Debt retained for an inactive
  generation remains visible in the existing inactive inventory but cannot
  poison the active generation's shared health.

## Preserved authorities

`enqueue_vector_event()` remains part of the caller-owned SQLite truth
transaction, so rollback of the truth mutation also rolls back its replay
intent. `vector_outbox_replay` remains the bounded crash-safe executor, and its
newer-event-wins checks, claim/complete/fail CAS boundaries, retry limit, and
explicit dead-letter requeue process remain unchanged.

Completed and dead-letter events are terminal in the shared projection. The
existing explicit operator requeue is a repair action on the native outbox, not
an automatic shared-state revival. No adapter API claims, completes, fails,
requeues, prunes, or otherwise writes an outbox row.
