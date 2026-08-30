# Public response contracts

Scope Recall operator-facing JSON reports include a top-level `schema_version` so scripts, dashboards, and external automation can branch safely as fields evolve.

These versions are lightweight response-contract identifiers, not full JSON Schema documents. The stable rule is:

- add fields without changing the version when existing keys keep their meaning;
- bump to a new `.vN` when a top-level field is renamed, removed, or changes type/meaning;
- keep machine-readable status booleans (`ok`, `passed`, `dry_run`) stable across minor field additions;
- do not put plaintext secrets, tokens, cookies, or private raw paths in any public report.

## Current top-level reports

- `doctor_report.v1`
  - Producer: `scripts/doctor.py`
  - Required top-level keys: `schema_version`, `ok`, `source`, `checks`, `recommendations`, `runtime`
  - Purpose: source/runtime health checks and operator recommendations.
  - `checks.endpoint_policy` is a required additive check. It evaluates inherited Hermes provider routes and hosted embedder `base_url_env` values without reading credential sources. Sanitized per-surface results under `runtime.endpoint_policy` expose only endpoint origins and recognized public API suffixes; arbitrary configured paths, query strings, userinfo, API keys, and credential values are never reported.

- `dashboard_report.v1`
  - Producer: `scripts/report.dashboard.py`
  - Required top-level keys: `schema_version`, `ok`, `severity`, `generated_at`, `sections`
  - Purpose: compact operator dashboard for candidate debt, quality lint, schema, freshness, and Experience health.

## Vector status object

`scope_recall_stats.vector`, the combined Doctor vector section, and the dashboard vector summary use `vector_status.v1`. Required fields are `schema_version`, `state`, `reason_code`, `auto_recoverable`, `repair_required`, `usable_for_query`, `message`, and `debt_counts`. `state` is exactly one of `ready`, `degraded`, `needs_repair`, or `disabled`; runtime `status` is retained as a compatibility alias with the same value. Backend/generation-specific Doctor detail is reported separately as `diagnostic_status` and nested diagnostic objects.

`debt_counts` contains non-negative `pending`, `processing`, `retry`, `dead_letter`, and derived `replayable` counts. Consumers must branch on the structured fields rather than message prose.

- `golden_benchmark_report.v1`
  - Producer: `scripts/benchmark.golden.py`
  - Required top-level keys: `schema_version`, `passed`, `query_count`, `failures`, `results`, `golden_name`, `case_file`, `hermes_home`
  - Purpose: repository-owned recall/experience regression benchmark output.

- `experience_replay_report.v1`
  - Producer: `experience_replay.py` / `scripts/experience-replay.py`
  - Required top-level keys: `schema_version`, `ok`, `case_count`, `pass_count`, `results`
  - Purpose: replay benchmark for promoted/reviewed procedural playbooks.

- `forgetting_report.v1`
  - Producer: `build_forgetting_report()` / `scope_recall_forgetting_report`
  - Required top-level keys: `schema_version`, `total_rows`, `soft_archive_candidates`, `hard_delete_candidates`, `review_debt`, `duplicate_groups`
  - Purpose: read-only forgetting/governance candidate report.

- `forgetting_run.v1`
  - Producer: `run_forgetting()` / `scope_recall_forgetting_run`
  - Required top-level keys: `schema_version`, `dry_run`, `batch_id`, `archived`, `deleted`, `review_debt`, `archive_ids`, `delete_ids`
  - Purpose: dry-run/apply result for forgetting maintenance actions.

## Retention mutation fields

Forget, forgetting maintenance, and two-phase privacy purge responses include
`retention_response.v1`. The stable fields are
`retention_schema_version`, `mode`, `data_retained`, `reversible`,
`privacy_purge`, `mutation_applied`, and `companion_erasure_pending`.

| Operation state | `mode` | `data_retained` | `reversible` | `privacy_purge` | `mutation_applied` |
| --- | --- | --- | --- | --- | --- |
| Soft archive applied | `archive` | `true` | `true` | `false` | `true` |
| Hard delete applied | `hard_delete` | `false` | `false` | `false` | `true` |
| Purge plan | `privacy_purge` | `true` | `false` | `true` | `false` |
| Purge deny (Phase A) | `privacy_purge` | `true` | `false` | `true` | `true` |
| Purge erase (Phase B) | `privacy_purge` | `false` | `false` | `true` | `true` |

Blocked and no-op responses set `mutation_applied=false` and report
`data_retained` from current authoritative truth. `data_retained=false` after a
successful Phase B truth erasure remains false even while a rebuildable vector
companion deletion is outstanding; that debt is reported separately as
`companion_erasure_pending=true`. A privacy-purge deny tombstone is irreversible
and is not an ordinary archive that can be rolled back.

## Tool argument failures

Direct provider calls and platform-dispatched calls enforce the same declared tool parameter schemas before a handler can coerce, query, or persist input. Validation failures return:

- `error`: a stable field-oriented message that does not include the rejected value;
- `invalid_arguments: true`;
- `field`: the dotted field path (or `$` for the root object);
- `constraint`: the failed JSON Schema keyword such as `required`, `type`, `enum`, `minimum`, `maximum`, `maxLength`, or `maxItems`.

This is an additive error contract. Callers should branch on `invalid_arguments`, `field`, and `constraint` rather than parsing prose.

## Implementation anchor

The canonical constants live in `response_schemas.py`:

```python
from scope_recall.response_schemas import PUBLIC_RESPONSE_SCHEMA_VERSIONS
```

Release checks require this document and the constants module to be present in both the source tree and the built wheel.
