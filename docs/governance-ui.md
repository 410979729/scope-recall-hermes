# Governance browser and candidate review

Scope Recall exposes governance inspection through read-only browser commands and dry-run-first candidate review commands. These surfaces are intended for operators who need to inspect SQLite truth rows, review candidate memories, and understand recall behavior without mutating durable state by default.

## Read-only browser

The browser opens SQLite with `mode=ro` and `PRAGMA query_only=ON` when a database path is provided through the CLI. Listing commands do not return full memory content unless an explicit inspect command is used.

Common commands:

```bash
hermes-scope-recall memories list --target memory --limit 20 --json
hermes-scope-recall memories inspect --id <memory-id> --json
hermes-scope-recall candidates list --limit 20 --json
hermes-scope-recall recall explain --query "deployment preferences" --json
```

Behavior:

- `memories list` returns summaries, targets, metadata, lifecycle, and content length.
- `memories inspect` returns one full row for explicit inspection, with content and metadata redacted for secret-like values and private paths by default. Use `--raw` only for local operator debugging when raw row material is intentionally needed.
- `candidates list` surfaces candidate-like rows, including `event-digest` and `memory-candidate` sources.
- `recall explain` in the browser is a read-only SQLite lexical preview. It is not the full live provider retrieval pipeline.

## Candidate review commands

Candidate review commands default to dry-run and return before/after metadata without writing:

```bash
hermes-scope-recall candidates promote --id <memory-id> --json
hermes-scope-recall candidates archive --id <memory-id> --json
hermes-scope-recall candidates supersede --id <memory-id> --superseded-by <replacement-id> --json
```

To apply a reviewed decision, pass `--apply` explicitly:

```bash
hermes-scope-recall candidates promote --id <memory-id> --apply --json
```

Apply behavior:

- `promote` changes lifecycle and candidate status to promoted.
- `archive` marks the row archived and removes graph companion entities/relations for that row.
- `supersede` marks the row superseded, records the replacement id, and removes graph companion entities/relations for that row.
- every applied review writes a `memory_candidate_review` governance audit event.

## Online review through the running gateway

When the gateway owns the writer lease, use the existing `scope_recall_memory` tool to review one candidate without stopping Hermes. The CLI remains subject to the exclusive maintenance lease.

First request a plan (omitting `dry_run` is equivalent to `true`):

```json
{"action": "promote", "id": "<candidate-id>"}
```

The result includes `before`, `after`, `expected_updated_at`, and `expected_lifecycle`. Apply the reviewed plan with its returned revision values:

```json
{"action": "promote", "id": "<candidate-id>", "dry_run": false,
 "expected_updated_at": "<value-from-plan>", "expected_lifecycle": "candidate"}
```

Use `action="archive"` for the corresponding archive plan/apply flow. Each call handles one ID and requires an existing `candidate` in the current writable scope. Already processed event-digest rows are rejected; Fact-owned projections must use Fact actions. A stale revision returns a conflict. An applied review uses the live writer and commits the lifecycle, governance audit and vector outbox intent atomically. The outbox performs physical vector work after truth commits. Ordinary recall continues to exclude unreviewed candidates.

Store receipts expose the actual persisted `lifecycle`, `origin_kind`, and review/admission identity. A temporary-marker admission downgrade therefore reports `candidate`, even when a store call succeeds.

## Safety boundaries

- Candidate review does not delete SQLite truth rows.
- Hard-delete cleanup remains a separate, explicit maintenance flow.
- Full TUI experiences can be built on top of these stable read-only and dry-run-first commands; the CLI/browser contract is the supported base surface.
- Secret-like content and `secret_reference` rows remain governed by the memory quality and external vault rules documented in [`external-shared-memory.md`](external-shared-memory.md).
