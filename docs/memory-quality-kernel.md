# Scope Recall Memory Quality Kernel

This maintainer-facing document defines the shared quality contract used before Scope Recall promotes, archives, reviews, or reports durable memory rows. It is source-boundary documentation, not a live deployment status report.

## Purpose

The memory quality kernel gives every candidate or active memory row a consistent read-only decision before any mutation path runs. Candidate promotion, lint reports, dashboards, future governance schedulers, and forgetting tools should share this contract instead of each carrying a separate copy of risk and value rules.

## Source of truth

- SQLite `memories` rows remain authoritative.
- Metadata is advisory evidence, not a replacement for row content.
- Vector, graph, dashboard, and replay state are companion surfaces and must remain rebuildable.
- The quality decision functions are read-only. Apply paths must still use governance audit events and their own transaction boundaries.

## Shared decision fields

`memory_quality.MemoryQualityDecision` exposes:

- `action`: `promote`, `keep_candidate`, `archive`, `needs_review`, or `skip`.
- `reason`: short stable explanation for the decision.
- `confidence`: numeric confidence from metadata, default `0.0`.
- `importance`: numeric importance from metadata, default `0.0`.
- `memory_type`: stable category such as `preference`, `factual`, `workflow`, `pitfall`, `constraint`, or `tool_trace`.
- `risk`: `low`, `medium`, or `high`.
- `target`: profile target such as `user`, `memory`, `project`, or `ops`.
- `lifecycle`: current row lifecycle, for example `candidate`, `promoted`, or `archived`.
- `evidence_refs`: bounded evidence references from metadata (`evidence_refs`, `evidence_anchors`, `journal_entry_ids`, or `source_ids`).
- `freshness`: expiration or freshness marker when present.
- `validator_kind`: optional freshness validator kind.
- `redaction_status`: `clean` or `secret_like`.

## Current policy

Candidate rows can be automatically promoted only when they are stable, low-risk, and sufficiently confident/important. High-risk rows stay as candidates for review. Low-value summaries, episodic notes, and tool traces can be archived by controlled apply paths.

Active rows are not promoted by this contract. If an active row trips lint rules, the decision is `needs_review`; destructive cleanup remains a separate audited operation.

## Stable memory types

Stable memory types:

- `factual`
- `preference`
- `procedure`
- `workflow`
- `pitfall`
- `decision`
- `constraint`
- `project`
- `resource`

Low-value or noisy memory types:

- `summary`
- `episodic`
- `tool_trace`

## High-risk terms

Rows mentioning credentials, release actions, push/tag/commit, sudo/systemctl, deletion, restart, or production-like changes are high risk unless a future review workflow explicitly approves them. High-risk means “do not auto-promote,” not “delete.”

## Evidence stance

Phase 1 records evidence fields but does not yet require evidence for every promotion. Phase 3 will add evidence-anchor extraction and can tighten promotion thresholds once evidence coverage exists.

## APIs

Use these functions:

- `load_quality_metadata(raw)` — parse metadata safely.
- `quality_decision_for_memory(row)` — classify a single row without mutation.
- `quality_decision_summary(conn, limit=1000)` — summarize decisions without mutation.
- `memory_quality_report(conn)` — active lint report for dashboards and doctor.

Candidate promotion should call `quality_decision_for_memory(row)` and adapt the returned decision into its existing lane/action surface. This keeps CLI output stable while avoiding duplicate rule stacks.

## Non-goals

- No automatic skill writes.
- No hard delete.
- No cross-scope mutation.
- No live credential validation.
- No claim that evidence references are complete until the evidence-anchor phase lands.

## Acceptance checks

Run:

```bash
python3 -m pytest tests/test_memory_quality_lint.py tests/test_memory_candidate_promotion.py -q
```

For broader dashboard evidence after this phase:

```bash
python3 -m pytest tests/test_dashboard.py tests/test_doctor_journal_health.py tests/test_doctor_experience.py -q
python3 scripts/report.dashboard.py --hermes-home <profile> --format json --output /tmp/scope-recall-dashboard.json
```
