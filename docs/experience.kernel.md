# Experience Kernel MVP

`scope-recall` now includes an enabled-by-default but kill-switchable Experience Kernel MVP for reusable procedural playbooks. In user-facing language, a playbook is a **可复用经验手册**: a checkable procedure distilled from a successful task. SQLite remains the truth store, vector/FTS surfaces are rebuildable indexes, and bounded runtime prompt injection is enabled by default through `experience.prefetch_enabled=true`; set it to `false` to keep runtime injection silent while retaining read-only tooling.

## What it does

The MVP supports this loop:

1. `scope_recall_experience_promote` can scan evidence-backed journal task traces and create a `procedural_playbook.v1` reusable experience handbook.
2. The playbook is stored in SQLite with scope isolation and FTS search fields.
3. Agents/operators can search and inspect accessible playbooks.
4. `scope_recall_experience_preflight` renders a bounded execution packet for a matching task.
5. `scope_recall_playbook_feedback` records whether reuse helped, failed, went stale, or misled the agent.
6. `scope_recall_forgetting_report` and `scope_recall_forgetting_run` keep ordinary memories from growing without bound by soft-archiving duplicates, scratch rows, tiny low-value rows, and wrapper noise.
7. Doctor reports Experience Kernel table health and playbook/run counts.

The automatic promotion path is conservative. It does **not** trust raw transcripts directly: it requires final successful closure plus verification signals, writes a task episode first, and defaults to creating candidate playbooks instead of silently promoting them. Low-risk verified handbooks are auto-promoted only when `experience.auto_promote_low_risk=true` is explicitly enabled; high-risk handbooks remain gated by `needs_review` for later agent/operator review. End users do not need to manually inspect raw memory rows.

## Safety defaults

Default config in the current source candidate:

```json
{
  "experience": {
    "enabled": true,
    "prefetch_enabled": true,
    "direct_reuse_min_confidence": 0.82,
    "allow_risky_direct_reuse": false,
    "packet_max_chars": 1400,
    "auto_promotion_enabled": false,
    "auto_promote_low_risk": false,
    "promotion_min_entries": 3,
    "promotion_min_tool_entries": 1,
    "promotion_require_verification": true
  },
  "forgetting": {
    "enabled": true,
    "soft_archive_default": true,
    "archive_very_short": true,
    "archive_assistant_scratch": true,
    "archive_duplicates": true,
    "hard_delete_sensitive": true
  }
}
```

Important defaults:

- `experience.enabled=true`: Experience Kernel tools are available. Set `false` as a global kill switch; runtime preflight returns `no_reuse`, runtime injection stays silent, and non-preflight Experience tools are hidden/blocked.
- `experience.prefetch_enabled=true`: matching promoted playbooks may append a bounded advisory runtime packet to normal recall. Set `false` to keep runtime injection silent while retaining read-only tools.
- `experience.auto_promotion_enabled=false`: successful background/session-end journal digest runs do not launch automatic promotion unless explicitly enabled. Set `true` to allow conservative evidence-gated automatic promotion; manual `scope_recall_experience_promote` remains available.
- `experience.auto_promote_low_risk=false`: even when automatic promotion scans are enabled, low-risk verified handbooks are created as candidates by default. Set `true` only when the deployment is ready to promote low-risk verified playbooks automatically; high-risk, under-verified, or final-failure handbooks remain gated by review/status.
- `allow_risky_direct_reuse=false`: playbooks with `service_control`, `network_or_remote`, `cross_instance`, `credential_adjacent`, or `destructive_or_irreversible` steps are downgraded to guided reuse unless explicitly allowed.
- `forgetting.soft_archive_default=true`: forgetting defaults to metadata archive/hide rather than physical deletion.
- `scope_recall_playbook_create` and `scope_recall_playbook_review` require `maintenance_tools_enabled=true`.
- `scope_recall_playbook_create` only writes `candidate`; `review/promote` is a separate maintenance action.
- Playbook content, review reasons, and feedback evidence reject secret-like text before persistence; legacy rows are redacted before search/inspect/preflight JSON and packet output.
- Core playbook JSON columns (`steps`, `preconditions`, `verification`, `reuse_policy`, and `environment_constraints`) fail closed if a legacy/corrupt row cannot be parsed; preflight returns `no_reuse` instead of defaulting to empty safe-looking structures.
- Per-playbook `reuse_policy` is enforced before direct reuse: `default_decision=no_reuse` suppresses packets, `default_decision=guided_reuse` or `allow_direct_reuse=false` prevents `direct_reuse`, and capability/staleness policy violations fail closed.
- Search/inspect/preflight are always scope-filtered before ranking.

## Playbook schema

Each playbook uses `procedural_playbook.v1` and must include:

- `task_class`
- `title`
- `trigger`
- `goal`
- `preconditions`
- ordered `steps`
- `verification`
- optional `pitfalls`, `cleanup`, and `reuse_policy`

Each step must include a conservative `capability_class`:

```text
read_only
local_write
service_control
network_or_remote
cross_instance
credential_adjacent
destructive_or_irreversible
```

Every step must also carry `evidence_required`; a playbook is a checkable procedure, not a vague summary.

## Tools

| Tool | Mode | Purpose |
|---|---|---|
| `scope_recall_playbook_create` | maintenance-only | Create a candidate playbook from reviewed payload JSON; promotion must go through `scope_recall_playbook_review`. |
| `scope_recall_playbook_search` | read-only | Search accessible playbooks by query/task/status. |
| `scope_recall_playbook_inspect` | read-only | Inspect one playbook, versions, and recent runs. |
| `scope_recall_experience_preflight` | read-only | Render direct/guided/no-reuse decision and packet. |
| `scope_recall_playbook_feedback` | scoped write | Records per-scope reuse outcome. Owner-scope feedback may update global counters/confidence/status; shared-pool consumer feedback records a private run without demoting the shared playbook, and terminal `quarantined`/`superseded` playbooks reject feedback. |
| `scope_recall_playbook_review` | maintenance-only | Mark reviewed/promoted/needs_review/quarantined/superseded. |
| `scope_recall_experience_stats` | read-only | Count accessible playbooks and runs. |
| `scope_recall_experience_promote` | maintenance-only | 自动从 journal 任务轨迹提取可复用经验手册；默认 dry-run。 |
| `scope_recall_forgetting_report` | maintenance-only | 只读遗忘/归档报告，找出重复、草稿、极短、包装噪声和疑似敏感记忆。 |
| `scope_recall_forgetting_run` | maintenance-only | 执行遗忘；默认 dry-run，非 dry-run 默认软归档。 |

## Runtime prefetch

The provider appends a bounded Experience Kernel packet by default only when a matching promoted playbook exists and the query is non-trivial. Operators can silence runtime packet injection without disabling the read-only Experience tools:

```json
{
  "experience": {
    "prefetch_enabled": false
  }
}
```

The packet is explicitly advisory:

> use this as a scaffold only; live evidence and current user instruction override old experience.

## Replay benchmark

`scripts/experience-replay.py` is a read-only benchmark harness for proving that playbook packets add concrete execution coverage before runtime injection is enabled. It compares a historical/simulated no-experience baseline against the baseline plus the current `scope_recall_experience_preflight` packet.

Example JSONL case:

```json
{"id":"headscale-acl","query":"Need one-way Headscale ACL","baseline_text":"I will edit the ACL and test management access.","required_terms":["rollback","negative reachability","live nodes"],"expected_decision":"direct_reuse","expected_playbook_id":"pb_acl"}
```

Run it against a SQLite truth DB:

```bash
python scripts/experience-replay.py \
  --db "$HERMES_HOME/scope-recall/memory.sqlite3" \
  --case-file replay-cases.jsonl \
  --scope-id '<accessible-scope-id>'
```

The script opens SQLite read-only (`mode=ro`), does not record `experience_runs`, and returns an `experience_replay_report.v1` JSON payload with baseline coverage, with-experience coverage, coverage gain, matched playbook id, decision, and missing terms. Positive cases must provide non-empty `required_terms`, render a non-empty Experience packet, and show positive coverage gain; empty/malformed case files fail instead of passing vacuously. Use `case_type: "negative_no_reuse"` or `expect_no_reuse: true` only for explicit negative controls.

## Duplicate playbook review CLI

The operator CLI exposes a read-first duplicate review path. Use it after taking a SQLite backup and before claiming Experience auto-promotion is clean:

```bash
python scripts/playbooks.py dedupe --hermes-home "$HERMES_HOME" --json
python scripts/playbooks.py supersede \
  --hermes-home "$HERMES_HOME" \
  --id pb_auto_duplicate_candidate \
  --superseded-by pb_promoted_canonical \
  --reason "duplicate group closeout"
```

`supersede` routes through the same `review_playbook()` governance path as maintenance-tool review: it scope-checks the canonical playbook, writes a `playbook_versions` row, and removes the duplicate from doctor/dashboard duplicate-group counts without deleting the historical row.

## Doctor output

`python scripts/doctor.py --hermes-home $HERMES_HOME` now includes:

```json
{
  "runtime": {
    "experience": {
      "status": "ready",
      "playbooks": {"total": 0, "by_status": {}},
      "runs": {"total": 0, "by_outcome": {}},
      "stale_facts": 0
    }
  }
}
```

Missing Experience tables are reported as `schema_missing` with a recommendation to initialize the plugin with the current code.

For an existing profile, make a SQLite backup first, then initialize the Experience tables with the current plugin code:

```bash
DB="$HERMES_HOME/scope-recall/memory.sqlite3"
BACKUP="$HERMES_HOME/scope-recall/backups/memory.before-experience-schema.$(date -u +%Y%m%dT%H%M%SZ).sqlite3"
mkdir -p "$(dirname "$BACKUP")"
python - <<'PY' "$DB" "$BACKUP"
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()
PY
python - <<'PY' "$DB"
import sqlite3, sys
from scope_recall.sql_store import ensure_experience_schema
conn = sqlite3.connect(sys.argv[1])
ensure_experience_schema(conn)
conn.commit()
conn.close()
PY
python scripts/doctor.py --hermes-home "$HERMES_HOME" --json
```

## MVP boundary

This is the first safe product slice, not a fully autonomous experience extractor. Deliberately deferred:

- LLM transcript-to-playbook extraction.
- Automatic task-boundary detection.
- Automatic promotion to `direct_reuse`.
- Cross-user/global shared playbook pools by default.
- Destructive validators or automatic skill rewrites.

Those belong behind replay benchmarks, explicit review, and additional tests.
