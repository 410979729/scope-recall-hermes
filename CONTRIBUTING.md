# Contributing to scope-recall

Read `AGENTS.md` first: it holds the maintenance rules, the release and
deployment procedure, and the rule that every `.py` file is either shipped
(listed in `packaging/v11-module-allowlist.json`) or a test selected by a tier
in `scripts/check.py`.

## Layout

| Path | What lives there |
|---|---|
| `contracts.py` | The v1.1 protocol: trusted context, payload schemas, contract errors |
| `core/` | Host-independent memory core: SQLite truth (`storage`, `truth_connection`, `writer_lease`), capture and admission, claims (`claims`, `mutate`, `fact_*`), candidates (`candidate_*`), episodes, recall (`recall*`, `retrieval*`, `read_views`), the worker (`worker*`), deletion and restore |
| `vector/` | Rebuildable vector companions: the `VectorStore` contract, the Lance store, its process-isolated driver, the SQLite brute-force fallback, compaction |
| `adapters/` | The Hermes plugin (`hermes/`); the clients that reach the memory through hooks and an MCP server, Codex, Claude Code, WorkBuddy and dsh, here or on another machine (`clients/`; `codex/` keeps the entry module names installed configurations run); the shared tool boundary (`tool_common`), model transport, the LanceDB port |
| `runtime/` | Background worker entry points, budgets and ledgers, scheduling, the HTTP helper subprocess |
| `maintenance/` | Install (`install*`), doctor, upgrade, legacy migration (`legacy_*`, `migration_*`) and the operator CLI |
| `tests/` | The gated test suite; `scripts/check.py --tier <tier>` selects it |
| `scripts/` | The gate runner, the quality check and its baseline, the manifest stamper and the dead-code scan |
| `probes/hermes/` | The real-host A2A test kit for a TEST Hermes gateway (`probes/README.md`) |
| `verification/` | Byte-exact evidence bundles cited by receipts; never edit by hand |

## Before a change is merged

1. Run the tiers that own the files you changed, then `unit` + `contract` + `packaging`:
   `python -X utf8 scripts/check.py --tier contract`
2. Run the quality check, which CI's `lint` job runs: `ruff format` leaves every
   file as it is, no file has more ruff or pyright findings of a rule than
   `scripts/quality.baseline.json` records, and no function is bigger than
   recorded (new code meets the rules in `pyproject.toml`; older findings are
   recorded until they are fixed). No comment, docstring or Markdown file may
   carry a marker of work left undone (`MARKERS` in the script names them): do
   the work or say what holds, and open an issue for the rest. Make the
   environment from the lock, so the tool versions and packages are CI's:
   `uv sync --locked --no-editable --reinstall-package hermes-scope-recall --extra lancedb --extra codex --extra dev`
   then `uv run --no-sync python scripts/quality.py`. When you fixed findings it
   asks you to record the lower numbers with `--update`.
3. If you changed the version, run `python scripts/build.package_manifest.py --write`
   so the plugin manifests and the wheel allowlist follow `_version.py`.
4. Update `CHANGELOG.md`, and `README.md` or `docs/` when behaviour visible to an
   operator or a host changed.

A new module enters the wheel only by being imported from an entry point named
in `packaging_hooks/module_inventory.py`; the packaging tier fails when the
committed allowlist and that computation disagree.

## The recall benchmark

`tests/recall_bench/` measures recall on a synthetic shared store: three owners,
four entries and five scopes, four weeks of sessions in English and Chinese, and
questions with known answers in seven tasks (single facts, preferences, time,
knowledge updates, lists said across sessions, abstention, cross-scope
isolation), each beside hard negatives. Every question is asked as the
automatic packet and as an explicit lookup. An item answers when it is the
answer's message, or a claim from it that states the same value. The report
gives recall@1/3/6/10, MRR, nDCG@10, how often a replaced value outranks the
current one, the share of answerable questions answered and of the others met
with `unknown`, items from scopes the asking entry does not read, how many hard
negatives of each kind became facts, packet size, latency, and the channel that
found the first answer. Recall runs on a clock that counts the work done, so
the numbers do not depend on the machine; latency comes from a second pass on
the real clock (`--no-latency` skips it).

    python -X utf8 tests/recall_bench/recall_bench.py                   # dev split, CI size
    python -X utf8 tests/recall_bench/recall_bench.py --split heldout   # held-out split
    python -X utf8 tests/recall_bench/recall_bench.py --scale 2 --json report.json

The retrieval tier runs the dev split (`test_recall_bench.py`, about a minute)
and fails on any item from another scope, on a metric that moved further than
the recorded tolerance to the wrong side of `tests/recall_bench/baseline.json`,
or on more active claims standing on hard negatives of a kind than it records.
A change meant to move the numbers records them in the same commit with
`--update` (a drop also needs `--allow-worse`); so does a change to the templates
or the generator, which makes another world. Judge a change on the held-out
split too: its templates were written apart from the dev ones. The vector
channel there hashes words, not meaning, so a paraphrased question measures
what the store finds without a model.
