# Follow-up: Peer Provider Transaction Recovery

> Reference: Issue [#25](https://github.com/410979729/scope-recall-hermes/issues/25)
> Date: 2026-07-07

## Background

Issue #25 was fixed with a single-connection rollback guard. After close, a
recurrence was observed in a long-running dashboard session that prompted
further local hardening.

## Diagnosis

The original rollback fix covers the single-connection dirty transaction case.
However, another recurrence appeared in a long-running dashboard session:

- The same dashboard process had **many open `memory.sqlite3`/WAL file descriptors**
  (11+ FDs via `lsof`)
- External `BEGIN IMMEDIATE` still failed with `database is locked`
- `lslocks` showed the dashboard process holding a write lock via
  `memory.sqlite3-shm`

Interpretation: when another Scope Recall provider instance or background writer
connection **inside the same dashboard process** leaves a dirty transaction open,
the current `scope_recall_store` connection can rollback itself, but it **cannot
release the peer connection's write lock**.

## Hardening

A process-local provider registry was added:

1. **Registry**: a `WeakSet` of live Scope Recall providers (`provider.py:72`)
2. **Register**: on provider initialize (`provider.py:264`)
3. **Unregister**: on shutdown (`provider.py:854`)
4. **Recovery** (`provider.py:905`): when `scope_recall_store` hits a SQLite
   lock/transaction error:
   - Traverse all registered providers sharing the same `memory.sqlite3` path
   - Rollback dirty transactions on peer providers (`provider.py:942`)
   - Probe current connection and retry the store once

## Regression Test

`test_provider.py:172` —
- Provider A opens a write transaction and leaves it uncommitted
- Provider B calls `scope_recall_store`
- **Old behavior**: B returns `database is locked` (test failed)
- **New behavior**: recovery rolls back A's dirty transaction → B retries → succeeds

## Verification

| Check | Result |
|-------|--------|
| `tests/test_provider.py` | 104 passed |
| `tests/test_tool_hygiene.py` + `tests/test_provider_schemas.py` | 15 passed |
| Plugin source in sync with venv runtime copy | `provider_same: True, tooling_same: True` |
| Live dashboard restart (new PID) | WAL = 0, only read locks |
| External `BEGIN IMMEDIATE` | Immediate success |
| `PRAGMA wal_checkpoint` | `(0, 0, 0)` |

## Key Files

- `scope_recall/provider.py` — provider registry + peer rollback logic
- `scope_recall/tests/test_provider.py` — regression test
