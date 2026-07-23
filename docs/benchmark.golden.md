# Scope Recall Golden and Retrieval-Regression Benchmarks

This document covers two repository-owned recall quality tiers:

- `scripts/benchmark.golden.py` defaults to a curated lexical-regression fixture with 120 labeled cases.
- The lexical and hybrid fixtures retained under the historical `golden_recall_*` names are **smoke profiles**, not production or commercial evidence.
- `scripts/benchmark.retrieval_regression.py` provides synthetic Recall Funnel stress cases with configurable distractor rows, `candidate_pool`, `top_k`, and prompt-budget metrics.

The curated regression benchmark is a release regression gate, not a claim of real-world commercial quality. It uses an isolated temporary Hermes home by default, stores labeled fixture memories through the public `scope_recall_store` tool, resolves labels to real runtime memory ids, and runs `scope_recall_benchmark` assertions.

## What it covers

Current fixtures:

- `benchmarks/curated_recall_quality_cases_v2.json`: release gate, 88 setup rows and 120 curated expected/forbidden-label cases.
- `benchmarks/golden_recall_cases.json`: fast lexical smoke profile (7 queries).
- `benchmarks/golden_recall_hybrid_cases.json`: fast hybrid/vector smoke profile (5 queries) using the built-in `local-hash` embedder and `sqlite-bruteforce` companion.

The curated fixture covers:

- English procedures with same-entity candidate distractors.
- Current facts with archived stale predecessors.
- Chinese natural questions and current-fact wording.
- Cross-target low-value `general` noise versus `ops` procedures.
- Mixed Chinese/English facts with in-progress distractors.
- Positive, negative, distractor, and lifecycle-isolation assertions for every query.

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

## Run synthetic Recall Funnel regression

```bash
PYTHONPATH=/path/to/hermes-agent:/path/to/scope-recall \
  python3 scripts/benchmark.retrieval_regression.py \
  --distractors 60 \
  --candidate-pool 24 \
  --top-k 5 \
  --prompt-budget-chars 1600
```

Expected high-level metrics:

```json
{
  "passed": true,
  "metrics": {
    "known_answer_recall": 1.0,
    "top_k_accuracy": 1.0,
    "prompt_budget_hit_rate": 1.0
  }
}
```

The synthetic runner disables vectors by default so it can run on CI and contributor machines without API keys, LanceDB, or PyArrow. It is designed to catch candidate-pool, lexical/BM25, filter, and prompt-budget regressions before graph or memory-quality tuning.

## Fixture schema

- `config`: scope-recall runtime config for the isolated benchmark.
- `setup`: list of memories to store. Each item needs a stable `label`; the runner stores it through `scope_recall_store` and records the returned id.
- `lifecycle: archived`: optional setup marker. The runner updates fixture metadata after storage so recall lifecycle filtering can be tested.
- `cases`: benchmark cases with `expected_labels` / `forbidden_labels`; runner resolves them to real ids before calling `scope_recall_benchmark`.

## Acceptance

The golden benchmark should be run before release when retrieval ranking/filtering/scoring changes. A failure is a real regression unless the fixture itself is intentionally updated with a new commercial-quality invariant.
