# Journal and nightly durable-work adapters

Journal and nightly digest keep their existing domain tables and evidence
pipelines. Program 2 does not introduce a generic jobs table, move journal
content, replace run receipts, or rewrite `memory_journal_sources` and
`memory_digest_sources` provenance.

## Journal mapping

Each journal entry is a finite work item whose identity is its existing entry
ID and content hash. Unprocessed rows map to `pending`; durable retryable
failure counters and active `retry-exhausted` recovery receipts map to `retry`;
active `dead-letter` recovery receipts map to `poisoned`; other processed rows
map to `completed`. Shared receipts carry identifiers and hashes, never entry
content, rejection text, or provider error text.

Health preserves the native oldest-entry/session-cursor scheduler. It reports
pending scope/session counts, oldest active debt, stable retry/poison counts,
and the latest native run receipt. Explicit journal recovery remains the only
way to reopen processed retry/dead-letter evidence.

## Nightly mapping

Each persisted nightly run remains the authoritative work receipt. Its local
`source_db` path is represented in the adapter only by SHA-256; digest date,
run ID, terminal status, and the count of existing source links remain visible.
Health is based on the latest run while older runs remain historical
provenance. Retry classification uses the already-persisted content-free
fallback classification; unknown failed runs fail closed as poison.

## Lease boundary

Both executors retain `TruthWriterLease`, the existing cross-process,
process-lifetime OS lock. The read-only adapter reports only its sanitized
owner-role hint and its crash-release semantics. The native lease does not
persist a token, generation, or expiry, and the adapter explicitly reports
those fields as unavailable instead of inventing authority. It never probes,
takes, renews, or releases the lock.
