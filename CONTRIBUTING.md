# Contributing to scope-recall

Read `AGENTS.md` first: it holds the maintenance rules, the release and
deployment procedure, and the rule that every `.py` file is either shipped
(listed in `packaging/v11-module-allowlist.json`) or a test selected by a tier
in `scripts/check.py`.

## Layout

| Path | What lives there |
|---|---|
| `core/` | Host-independent memory core: SQLite truth, capture, claims, recall, worker |
| `adapters/` | Hermes and Codex host adapters, model transport, LanceDB port |
| `runtime/` | Background worker, budgets, scheduling, HTTP helper subprocess |
| `maintenance/` | Install, doctor, upgrade, migration and the operator CLI |
| `tests/` | The gated test suite; `scripts/check.py --tier <tier>` selects it |
| `scripts/` | The gate runner and the manifest stamper only |
| `probes/hermes/` | The P11 real-host A2A test kit (see `docs/p11-a2a-test.zh-CN.md`) |
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
