# Memory benchmarks

The benchmark programs use synthetic data and explicitly downloaded public
datasets. They never discover an installed host, read a user's store, or start
a host program. Data, stores and results belong outside the checkout.

## Offline checks

The retrieval tier includes the synthetic recall benchmark and the public
runner's adapter, cache, statistics and worker-completion contracts:

```text
python -X utf8 scripts/check.py --tier retrieval
python -X utf8 tests/recall_bench/recall_bench.py --no-latency
python -X utf8 tests/recall_bench/recall_bench.py --split heldout --no-latency
```

The synthetic benchmark includes two- and three-hop questions with aliases
across sessions, and questions about an assistant's unconfirmed words. A join
earns credit for each supporting edge delivered. Assistant words remain source
text; retrieving them does not promote them to facts. Its vectors hash words
and do not measure semantic embedding quality.

## Public datasets

Download explicitly, then run without a downloader:

```text
python -X utf8 tests/public_bench/download_data.py --data /task/data
python -X utf8 tests/public_bench/run.py --data /task/data --output /task/lme-smoke --fake --limit 4
python -X utf8 tests/public_bench/run.py --dataset locomo --data /task/data --output /task/locomo-smoke --fake
python -X utf8 tests/public_bench/run.py --dataset factconsolidation --data /task/data --output /task/facts-smoke --fake
```

The downloader pins LongMemEval-S cleaned, LoCoMo and MemoryAgentBench revisions
and records SHA-256 digests. LongMemEval and MemoryAgentBench are MIT licensed.
LoCoMo is CC BY-NC 4.0 and is restricted to local, noncommercial smoke tests.
Upstream evaluator files are retained as reference text outside the repository;
the runner parses only the LongMemEval judge's literal prompts and never
executes downloaded source.

LongMemEval's fixed partition contains 50 development, 150 regression and 300
sealed question ids. A seeded hash fixes their order independently of JSON
ordering. The generated manifest is immutable: a differing partition fails.
Sealed questions are reserved for milestone and release confirmation.
Abstention ids ending in `_abs` form a separate category. LoCoMo's original
numbering is 1 multi-hop, 2 temporal, 3 open-domain, 4 single-hop, 5 adversarial.
Adversarial distractor answers never become supported answers.

The fake port is a sparse mechanical extractor and a 3072-dimensional word
hash embedder. It exercises capture, consolidation, candidate evaluation,
embedding, automatic recall, answer and judge interfaces. Fake runs report no
accuracy or model-quality confidence interval. Public passages rejected by the
product's admission rules remain rejected and appear in the result.

## Explicit model routes and cached runs

A live run requires `--config`, `--allow-model-calls` and an exact
`--approved-endpoint` for each configured endpoint. The route file contains
`weights`, `chat` and `embedding`. `chat` uses the shipped
`ConsolidationRouteConfig` fields; `embedding` uses `EmbeddingRouteConfig`.
Freeze the weight/deployment identity, temperature, thinking setting and
embedding dimension. Credentials are environment-variable names in the routes,
never values in that file. There is no CLI-model fallback.

Each question has an independent truth and vector store. Messages enter in
session-time order using an injected business clock; the query uses its
published question date. The answerer receives only the selected context,
question and its date. Gold answers and evidence labels belong only to scoring. The same
answer route serves the scope-recall, `full-context` and `naive-rag` methods.

The request cache keys the normalized complete inputs, formatted prompt
messages, source roles/times/versions/chunks, candidate state, model settings
and output-contract version. Corpus refs replace local database refs in keys
and are remapped on replay. Query and document embeddings have separate
namespaces. `--retrieval-only --cache-only` refuses every miss and cannot make
a model request. Ranking and threshold changes reuse cached extraction and
embedding when their complete inputs are unchanged.

Use `--cache` to reuse the same request database while keeping each result in
its own output directory. Populate that cache in an explicitly authorized live
run before using cache-only mode. Replace the example endpoints with the exact
endpoints in the route file:

```text
python -X utf8 tests/public_bench/run.py --data ../benchmark-data --output ../benchmark-runs/regression --cache ../benchmark-cache.sqlite3 --split regression --config ../benchmark-routes.json --approved-endpoint https://chat.example.invalid/v1 --approved-endpoint https://embedding.example.invalid/v1 --allow-model-calls
python -X utf8 tests/public_bench/run.py --data ../benchmark-data --output ../benchmark-runs/sealed --cache ../benchmark-cache.sqlite3 --split sealed --config ../benchmark-routes.json --approved-endpoint https://chat.example.invalid/v1 --approved-endpoint https://embedding.example.invalid/v1 --allow-model-calls
python -X utf8 tests/public_bench/run.py --data ../benchmark-data --output ../benchmark-runs/cached-retrieval --cache ../benchmark-cache.sqlite3 --split regression --config ../benchmark-routes.json --approved-endpoint https://chat.example.invalid/v1 --approved-endpoint https://embedding.example.invalid/v1 --cache-only --retrieval-only
```

The first two commands select all 150 and 300 questions respectively. No
`--limit` is used. Sealed questions are used only for final confirmation.
Cache-only mode has no authorization flag for model calls. Any missing response,
including one caught inside a worker or retrieval channel, fails the run.
Source data and external judge prompts must match the frozen SHA-256 manifest
before an adapter reads them; an existing unverified download is rejected.

Completion is checked from durable queue and candidate states. Work is terminal
only at `done` or `obsolete`; failures and unavailable capabilities are reported
separately. Candidates may finish the current evidence set at resolved,
archived or waiting for evidence. Pending evaluations and future/leased work
remain unfinished. The runner advances the business clock when waiting for
settling windows, and enforces round, business-time and real wall-clock limits.
A pass processing zero items is insufficient evidence of completion.
Each worker batch receives the remaining wall-clock budget, and the runner
checks the deadline again after the batch. Incomplete questions appear under
`incomplete_results`, never call the answerer or judge, and make the command
exit unsuccessfully. Their scores cannot become a complete baseline.

Results retain dataset provenance, model settings, prompt hashes, per-question
packets, admission gaps, pipeline states, session/turn evidence recall, estimated
token units, latency and cache hits. Exact model token usage and query embedding
network latency require a configured live measurement. English and Chinese
results are separate.

LoCoMo reports stemmed token F1 for single-hop, temporal and open-domain answers;
the open-domain reference uses the first semicolon-delimited answer. Multi-hop
answers match comma-delimited subanswers and average their best F1 values.
Adversarial questions score the specified refusal phrases. These lexical
metrics require `nltk` in the benchmark environment (`python -m pip install
nltk==3.9.2`); no tokenizer data or model weights are downloaded. Missing NLTK
is reported as unsupported, including the affected category count.
FactConsolidation reports normalized substring exact match, maximizing over
the supplied answer variants. Scores are grouped by the questions actually
selected, with completion and scoring coverage beside each mean. Fake runs
continue to report no model-quality aggregate.

Binary answer accuracy uses a 95% Wilson interval. Version comparisons pair the
same question ids, use the exact two-sided McNemar test and a 10,000-sample paired
bootstrap with seed 42. Significance is 0.05 with Holm correction over the
predeclared experiment/category family. Preference questions are reported
without a separate promotion gate. Latency is measured with a real clock and
reported independently of the deterministic retrieval score.

Compare two complete quality result files, or paired binary evidence-coverage
results from cached retrieval:

```text
python -X utf8 tests/public_bench/paired_statistics.py --before ../benchmark-runs/before/results.json --after ../benchmark-runs/after/results.json --output ../benchmark-runs/quality-comparison.json
python -X utf8 tests/public_bench/paired_statistics.py --before ../benchmark-runs/before-retrieval/results.json --after ../benchmark-runs/after-retrieval/results.json --metric turn_recall_all --output ../benchmark-runs/retrieval-comparison.json
```

The files must contain identical question ids and categories, dataset hashes,
partition hashes, model settings and judge prompts. Incomplete runs, duplicate
ids, missing outcomes on one side and continuous F1 scores are rejected. Binary
retrieval labels absent on both sides are listed as unscored question ids.
The comparison includes overall and per-category paired statistics, with Holm
correction across that declared family. Fake answer-quality comparisons are
rejected.

The single-recall method is the baseline. Additional evaluation protocols are:
one session-start brief charged to the session budget; compact entries followed
by charged inspect calls under a cumulative session budget; and repeated visits
to the same question within one session, charging every visit. Capabilities
absent from the measured product are reported as unsupported.

## Chinese translation

`translate.py` generates one frozen translation only with explicit live-route
authorization. It preserves ids, roles, timestamps, session evidence and answer
turn labels, validates aligned source and translated quotes, and records source,
translation, model and prompt hashes. `--validate-only` runs offline. A fixed
20-question sample requires semantic review by a different model; structural
validation alone does not establish translation fidelity. Dataset files stay
outside the checkout.

## Compatibility, performance and platforms

`compatibility.py` installs every published 3.x release in its own environment,
uses that installed release to generate a synthetic library, then opens it with
the target interpreter. It compares truth/deletion hashes, fixed queries,
integrity and snapshot restoration. `historical_states.py` additionally creates
a displaced denial, retry marks, failed/pending/leased work and validated old
configurations for both vector backends.
Every verification starts from `pre-upgrade.sqlite3` in a new directory and
leaves the historical store unchanged. Actual source text, original
failed/pending/leased work, truth and deletion state must be preserved, and
integrity checks must return `ok`. New derived work may be queued by an upgrade.
Use `compatibility.py --verify-only` with a separate `--output` directory to
revalidate existing snapshots without reinstalling historical releases.

`performance.py` freezes machine/workload conditions before populating 350,000
and 1,000,000 sources. It uses 3072-dimensional synthetic float32 vectors and
the shipped LanceDB helper. Fixture bulk loading is separate from measured
capture/recall/worker operations. Four logical foreground entries are paced at
four arrivals per second with one request in flight; achieved input rate is
reported when retrieval prevents that rate. OS page-cache flushing is avoided
on a shared machine. `--channels-only` measures lexical and vector retrieval
separately on an existing task fixture.
Resuming rejects a different platform, CPU, logical CPU count, total memory or
other frozen condition. Observation timestamps and disk occupancy may change.

`hook_profile.py` measures all five adapters with synthetic hook/provider inputs,
including config, identity, write and recall API timings. Its small-store
measurements do not establish host timing at either large scale.
`scenarios.py` exercises real hook and MCP facades and reports current-version
subchecks and capability gaps for each product scenario; it does not start actual host programs.

The test command is `python -X utf8 scripts/check.py --tier <name>`:

| Level | Tiers |
| --- | --- |
| T0 | unit |
| T1 | contract, storage, capture, claims, deletion, episodes, retrieval |
| T2 | native, host, migration, integration |
| T3 | packaging, release |
| T4 | eval; requires explicit model authorization |

`release` selects unit, contract, native, host, migration and packaging.
A complete T0–T3 run also executes storage, capture, claims, deletion, episodes,
retrieval and integration; `release` alone does not replace those selections.
Ubuntu and Windows CI run the main tiers; macOS CI runs the native import probe, not the main suite. The CI
jobs record their actual Python SQLite version and extension-loading capability.
Windows measurements do not establish Linux or macOS behavior; run the same
commands and frozen workload on those machines separately.
