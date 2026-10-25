# Contributing to scope-recall

## What a change needs

A pull request says whose problem it solves, what shows that the problem exists
(a reproduction, a failing test, numbers read from a running install, or a
user's report), and what will measurably change once it is merged. A bug fix, a
security fix, or a change that follows one made by a host (Hermes, Codex) needs
only its reproduction.

New capabilities are welcome, ideas from other memory systems included, when
they fit the design: SQLite is the only source of truth, every memory can be
traced to its source, several agents share one store and are kept apart by
scope, and everything runs locally. Code from other projects is not copied in.

A change that replaces an implementation removes the old one in the same pull
request and gives the reason. Stored memories are never lost: a change to the
stored format comes with an automatic migration, a migration test and a way
back. A change that makes a user or a host edit a configuration or a call by
hand says how, in the pull request and in `CHANGELOG.md`.

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

## Design rules

- SQLite is the only authority for facts, identity, permissions, lifecycle and
  evidence. Vector and other derived indexes are optional and can be rebuilt
  from it.
- Each behaviour lives in one module. Host adapters translate requests and bind
  the trusted identity; the policy they apply stays in `core/`. A change adds no
  import cycle.
- Migration orchestration, legacy conversion, host activation and background
  indexing stay separate.
- Multi-hop queries only join evidence: they do not infer identity, widen an
  audience, revive invalid facts or store conclusions drawn at query time.
- Long work runs in bounded stages with durable progress and retries that are
  safe to repeat, and its failures are visible. Data a migration cannot read
  stops the migration instead of counting as migrated, and no retry runs
  forever.
- A new public capability comes with a shared contract, support on each host
  surface it applies to, operator documentation and tests at its boundaries.
- A new dependency, stored state or abstraction gives its reason in the pull
  request.

## Code style

- Comments and docstrings say what the code does, why, and the constraints a
  reader would not see. The history of a change (dates, versions, issue numbers,
  where an idea came from) belongs in the commit message and `CHANGELOG.md`.
- Comments and documentation are written in the language of their file.
- Tests are named for what they check.
- A name another module uses is public (no leading underscore). Imports go at
  the top of the module, except when moving one up would load more when an
  entry point starts, when it needs an optional dependency, when tests replace
  what it imports at call time, or when it is one of the lazy upward imports
  `scripts/quality.py` names.

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

Tests use synthetic data only, never a real memory store or a running
installation.

A new module enters the wheel only by being imported from an entry point named
in `packaging_hooks/module_inventory.py`; the packaging tier fails when the
committed allowlist and that computation disagree. A new `.py` file is either
listed in `packaging/v11-module-allowlist.json` or a test under `tests/` that a
tier in `scripts/check.py` selects.

## The recall benchmark

`tests/recall_bench/` measures recall on a synthetic shared store: three owners,
five entries and six scopes, four weeks of sessions in English and Chinese, and
questions with known answers in nine tasks (single facts, preferences, time,
knowledge updates, lists said across sessions, abstention, cross-scope
isolation, automatic relevance floors and session prompts needing no memory),
each beside hard negatives. Every question is asked as the
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

## Releases

- Wheels are built from a tagged commit on `main` (`v<major>.<minor>.<patch>`),
  one wheel and one sha256 per tag. The first commit after a tag moves
  `_version.py` past it.
- Changes that belong together ship as one release once all of them are done
  and verified. A fix a user is waiting for goes out as one pull request with
  the fix, the version and its notes, and is tagged without a release candidate.
- `CHANGELOG.md` gives each release a heading and one paragraph, plus anything
  an operator must do beyond installing the package. The release workflow
  publishes that section as the release notes on GitHub.
- The `integration` and `native` tiers pass before a tag is pushed.
