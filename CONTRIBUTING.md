# Contributing to scope-recall

Thanks for helping with `scope-recall`.

## Release-quality rules

Before opening a PR or publishing a fork:

- keep README wording honest about the actual backend and capability boundary
- keep SQLite as the authoritative truth layer unless the design doc is updated deliberately
- treat OpenClaw reuse as explicit import/migration, not transparent drop-in compatibility
- do not remove the `lancepro` compatibility shim until downstream users and configs are cleaned up
- preserve an explicit vector repair path via `scripts/repair.vector_index.py`
- `scope-recall` uses a SQLite truth layer plus a rebuildable vector companion (LanceDB by default, sqlite-bruteforce for native-free hosts); see the architecture and stability contract for details
- keep `queue_prefetch()` as a no-op unless you also prove cross-turn topic bleed protections another way

## Local development

From a Hermes profile with the plugin installed:

```bash
./hermes-agent/venv/bin/pytest -q /path/to/plugins/scope-recall/tests/test_provider.py
python3 -m py_compile /path/to/plugins/scope-recall/*.py /path/to/plugins/scope-recall/scripts/*.py
./hermes-agent/venv/bin/hermes memory status
```

## What to test before merge

Minimum gate:

- plugin loads from `$HERMES_HOME/plugins`
- current-turn recall still uses the current query only
- `chat_id`, `thread_id`, and `gateway_session_key` isolation still hold
- curated memory add/replace/remove still reflect live file truth
- hybrid/vector stats remain sane
- legacy `lancepro_*` aliases still work during the deprecation window

## Packaging expectations

If you publish a standalone repo or wheel:

- keep `pyproject.toml`, `LICENSE`, `CHANGELOG.md`, `README.md`, and `DESIGN.md` in sync
- exclude runtime artifacts like `__pycache__/`, `lancedb/`, `vector.sqlite3`, and `*.sqlite3`
- verify `pip wheel . --no-deps` succeeds from a clean checkout
- run `python scripts/check.release.py` before publishing
- remember that current Hermes runtime discovery for user plugins is directory-based (`$HERMES_HOME/plugins/<name>/`); wheel success is build hygiene, not by itself a proof of discoverable runtime installation for this plugin layout

## Documentation expectations

If behavior changes, update these together:

- `README.md`
- `DESIGN.md`
- `docs/migration.md`
- `docs/differences-from-memory-lancedb-pro.md`
- `CHANGELOG.md`
