# Context Compiler shadow

Scope Recall has one production retrieval orchestrator. Program 4 does not add
a second search path: lexical, vector, curated, temporal, graph, relation, and
freshness work is still collected once by `_internal/recall/orchestrator.py`.
The ranked result becomes one typed `CandidateSet`; both the V1 compatibility
view and the `RecallPacket` candidate are compiled from that exact snapshot.

The pure compiler stages are:

1. canonical current-truth filtering;
2. conflict exposure;
3. exact duplicate removal;
4. evidence coverage ordering;
5. deterministic diversity ordering;
6. prompt token budgeting;
7. `scope_recall.recall_packet.v1` assembly.

The compiler owns no provider, SQLite, vector, network, job, repair, or
telemetry port. Query execution therefore remains truth-zero-write. A legacy
freshness `fact_key` cannot acquire Claim authority: hard current-truth grouping
requires `temporal_fact_key` or the canonical projection's `fact_claim_key`,
and always includes `scope_id`. Two current conflicting candidates remain in
the packet and are marked as a conflict; the read side does not invent a
winner.

The product switches remain independent. Program 5 makes canonical
current-truth filtering and the Recall Packet renderer default-on in separate
changes; the token budgeter stays default-off until its own product decision:

- `recall_compiler.current_truth_enabled` defaults on and activates only
  canonical stale/current filtering; setting it to `false` restores the V1
  ordering without changing stored truth;
- `recall_compiler.budgeter_enabled` activates packet token limits;
- `recall_compiler.renderer_enabled` defaults on and activates
  evidence/diversity packet order and the new prompt renderer; setting it to
  `false` restores the V1 prompt renderer independently.

With all switches explicitly off, ordinary results and the V1 prompt renderer
remain the compatibility authority. The compiler still calculates a bounded aggregate
shadow record from the same `CandidateSet`. That record contains counts,
estimated tokens, booleans, and elapsed time only—never query text, candidate
IDs, summaries, Evidence text, or the CandidateSet fingerprint. Complete
paired item diffs require the explicit `isolated=True` API and are reserved for
offline tests and `scripts/benchmark.recall_compiler.py`.

The historical `current_truth_removed` trace field remains the decontented
shadow count. `active_current_truth_removed` is the active-product count, so an
operator can prove that the explicit rollback switch stopped filtering without
disabling shadow comparison.

The token estimator is deterministic and intentionally conservative: each
non-ASCII code point costs one token, while ASCII costs one token per four code
points. It is a local budget guard, not a claim about any hosted tokenizer.
