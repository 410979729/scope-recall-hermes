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
| `probes/hermes/` | The P11 real-host A2A test kit (see `docs/implementation-history/p11-a2a-test.zh-CN.md`) |
| `verification/` | Byte-exact evidence bundles cited by receipts; never edit by hand |

## Before a change is merged

1. Run the tiers that own the files you changed, then `unit` + `contract` + `packaging`:
   `python -X utf8 scripts/check.py --tier contract`
2. Run the quality check, which CI's `lint` job runs: `ruff format` leaves every
   file as it is, no file has more ruff or pyright findings of a rule than
   `scripts/quality.baseline.json` records, and no function is bigger than
   recorded (new code meets the rules in `pyproject.toml`; older findings are
   recorded until they are fixed). Make the
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
