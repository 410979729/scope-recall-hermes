# Hermes Memory Stack Contract

This contract defines how `scope-recall` and `turn-closure-audit` cooperate without becoming a monolith and without adding a third plugin.

## Compatibility matrix

| Contract version | scope-recall | turn-closure-audit | Status |
|---|---:|---:|---|
| 0.1 | >= 1.0.1 | >= 1.0.2 | Planning baseline; candidate/receipt protocol documented, not fully implemented |

Rules:

- `scope-recall` must remain usable by itself as a scoped memory provider.
- `turn-closure-audit` must remain usable by itself as a post-turn audit plugin.
- When both are installed, they cooperate through this contract only: candidate records from audit, receipts from recall.
- Version bumps must preserve backward compatibility for existing review JSONL and existing `scope-recall` SQLite rows unless a migration is explicitly documented and tested.

## Final sinks

A candidate's `final_sink` is the intended terminal destination, not an intermediate evidence file.

Allowed final sinks:

```text
user
memory
project
ops
skill
knowledge
discard
ask_user
```

Not final sinks:

```text
memory/day
knowledge/review candidate
turn.closure.audit/*.json
turn.closure.audit/candidates/*.jsonl
general
```

Those are evidence, scratch, compatibility, or ledger locations. Writing a candidate to one of them does **not** count as promotion.

## Candidate record schema

`turn-closure-audit` owns candidate creation. Candidate records must be redacted, concise, and safe for review.

Required fields:

```json
{
  "candidate_id": "stable-id",
  "record_id": "turn-audit-record-id",
  "session_id": "...",
  "created_at": "...",
  "updated_at": "...",
  "status": "pending",
  "classification": "user-preference-or-boundary",
  "final_sink": "user",
  "risk": "low|medium|high",
  "confidence": 0.85,
  "candidate_content": "Redacted concise candidate memory or promotion text",
  "evidence_summary": "Redacted evidence summary, not a full transcript",
  "decision_reason": "Why this state/final sink was chosen",
  "source": {
    "plugin": "turn-closure-audit",
    "platform": "telegram",
    "tool_events": 0
  },
  "receipt": null
}
```

`candidate_id` should be based primarily on normalized `candidate_content + final_sink + classification`, with `record_id` used as supporting provenance. It must not be based mainly on a generic reason string.

## Terminal statuses

Every candidate must eventually reach a terminal state or appear in an overdue report.

```text
promoted
merged
rejected_noise
rejected_temporary
rejected_sensitive
needs_user_confirmation
expired
```

`pending` is not a terminal state.

## Receipt schema

`scope-recall` owns durable memory write receipts. A promotion is not complete without a receipt or an explicit rejection reason.

Example promotion receipt:

```json
{
  "action": "promoted",
  "provider": "scope-recall",
  "target": "project",
  "id": "memory-row-id",
  "scope_mode": "shared",
  "at": "2026-05-17T00:00:00Z"
}
```

Example merge receipt:

```json
{
  "action": "merged",
  "provider": "scope-recall",
  "target": "project",
  "target_id": "kept-memory-id",
  "source_candidate_id": "candidate-id",
  "at": "2026-05-17T00:00:00Z"
}
```

Example rejection receipt:

```json
{
  "action": "rejected_temporary",
  "reason": "runtime service status should be checked live",
  "at": "2026-05-17T00:00:00Z"
}
```

## Ledger model

`turn-closure-audit` should use an append-only event log as the source of truth for candidate state transitions.

Recommended event kinds:

```text
candidate.created
candidate.classified
candidate.promote.dry_run
candidate.promoted
candidate.merged
candidate.rejected
candidate.needs_user_confirmation
candidate.expired
```

Daily review JSONL files are compatibility outputs only, not the authoritative ledger.

## General scratch and vector policy

`general` is local scratch. It is not durable memory.

Default policy:

- automatic recall may include `general` only for same-scope continuity and with lower weight;
- `general` should not be indexed into the durable vector companion by default;
- if scratch vector indexing is later needed, it should use a separate namespace/table or a clear config flag;
- durable vector indexing should prioritize `user`, `memory`, `project`, and `ops` rows.

## Validation matrix

Before declaring the memory stack ready, validate all three modes:

1. `scope-recall` alone: storage, recall, tools, hygiene, release gate.
2. `turn-closure-audit` alone: audit trail, candidate ledger, dry-run report, release gate.
3. Both installed: candidate -> dry-run -> receipt -> recall ranking smoke.

Old config and old review files must remain readable during this rollout.
