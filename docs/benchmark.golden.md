# Scope Recall Golden Recall Benchmark

This benchmark is the repository-owned recall quality gate for commercial memory readiness. It uses an isolated temporary Hermes home by default, stores labeled fixture memories through the public `scope_recall_store` tool, resolves labels to real runtime memory ids, and runs `scope_recall_benchmark` assertions.

## What it covers

Current fixture: `benchmarks/golden_recall_cases.json`

- Durable procedure retrieval beats low-value same-scope `general` scratch.
- Archived old facts are excluded when a newer current fact exists.
- Explicit project/entity scope avoids cross-topic/project bleed.

## Run

```bash
python3 scripts/benchmark.golden.py --auto-explain-on-fail
```

Expected result:

```json
{"passed": true, "failures": []}
```

Use an existing Hermes home only as a read-only reference label; the benchmark still creates an isolated temporary home and does not write that live config:

```bash
python3 scripts/benchmark.golden.py \
  --hermes-home /path/to/hermes-home \
  --auto-explain-on-fail
```

Maintenance-only live/profile mode requires the explicit dangerous flag and automatically backs up/restores the config even on failure:

```bash
python3 scripts/benchmark.golden.py \
  --hermes-home /path/to/hermes-home \
  --overwrite-config \
  --auto-explain-on-fail
```

Do not use `--overwrite-config` during normal release checks; the release gate runs the isolated default mode.

## Fixture schema

- `config`: scope-recall runtime config for the isolated benchmark.
- `setup`: list of memories to store. Each item needs a stable `label`; the runner stores it through `scope_recall_store` and records the returned id.
- `lifecycle: archived`: optional setup marker. The runner updates fixture metadata after storage so recall lifecycle filtering can be tested.
- `cases`: benchmark cases with `expected_labels` / `forbidden_labels`; runner resolves them to real ids before calling `scope_recall_benchmark`.

## Acceptance

The golden benchmark should be run before release when retrieval ranking/filtering/scoring changes. A failure is a real regression unless the fixture itself is intentionally updated with a new commercial-quality invariant.
