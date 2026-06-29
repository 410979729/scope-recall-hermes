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

- `dashboard_report.v1`
  - Producer: `scripts/report.dashboard.py`
  - Required top-level keys: `schema_version`, `ok`, `severity`, `generated_at`, `sections`
  - Purpose: compact operator dashboard for candidate debt, quality lint, schema, freshness, and Experience health.

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

## Implementation anchor

The canonical constants live in `response_schemas.py`:

```python
from scope_recall.response_schemas import PUBLIC_RESPONSE_SCHEMA_VERSIONS
```

Release checks require this document and the constants module to be present in both the source tree and the built wheel.
