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
| `adapters/` | Hermes and Codex host adapters (`tool_common` holds the shared tool boundary), model transport, the LanceDB port |
| `runtime/` | Background worker entry points, budgets and ledgers, scheduling, the HTTP helper subprocess |
| `maintenance/` | Install (`install*`), doctor, upgrade, legacy migration (`legacy_*`, `migration_*`) and the operator CLI |
| `tests/` | The gated test suite; `scripts/check.py --tier <tier>` selects it |
| `scripts/` | The gate runner, the manifest stamper and the dead-code scan |
| `probes/hermes/` | The P11 real-host A2A test kit (see `docs/implementation-history/p11-a2a-test.zh-CN.md`) |
| `verification/` | Byte-exact evidence bundles cited by receipts; never edit by hand |

## Before a change is merged

1. Run the tiers that own the files you changed, then `unit` + `contract` + `packaging`:
   `python -X utf8 scripts/check.py --tier contract`
2. If you changed the version, run `python scripts/build.package_manifest.py --write`
   so the plugin manifests and the wheel allowlist follow `_version.py`.
3. Update `CHANGELOG.md`, and `README.md` or `docs/` when behaviour visible to an
   operator or a host changed.

A new module enters the wheel only by being imported from an entry point named
in `packaging_hooks/module_inventory.py`; the packaging tier fails when the
committed allowlist and that computation disagree.
