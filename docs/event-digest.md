# Event digest candidates

Scope Recall can normalize task, compression, session, release, and issue closeout events into sanitized evidence packets. Event digest candidates are review artifacts: they are not durable recall rows until a review or promotion path explicitly accepts them.

## Event kinds

Supported event kinds are normalized to:

- `task_closeout`
- `pre_compress`
- `session_end`
- `release_closeout`
- `issue_closeout`

Unknown event kinds are rejected with `unknown_event_kind` so callers can inspect producer behavior before any candidate is written.

## Evidence packet safety

`build_evidence_packet()` is read-only and does not write SQLite. It performs these checks before candidate extraction:

- sanitize report text and private paths;
- redact secret-like values;
- reject packets with secret-like content using `plaintext_secret_rejected` evidence;
- reject very short, low-signal packets;
- reject unclassified generic chat with `unclassified_event_candidate` instead of falling back to durable memory proposals;
- attach stable evidence references such as `session:<id>:turn:<n>` or `session:<id>:event:<kind>`.

The packet is an input to candidate extraction, not a memory write by itself. When a reviewed event produces multiple candidates, the truth rows and governance evidence commit as one SQLite batch; any candidate or audit failure rolls the entire batch back.

## Runtime configuration

Relevant keys are documented in [`configuration.md`](configuration.md):

- `event_digest.enabled`
- `event_digest.write_candidates`
- `event_digest.dry_run_log`
- `event_digest.max_events_per_turn`

Recommended rollout posture:

1. Keep `event_digest.enabled=true` to allow packet normalization.
2. Keep `event_digest.write_candidates=false` until operators are ready to review candidate rows.
3. Keep `event_digest.dry_run_log=true` during rollout so skipped and proposed candidates remain observable.
4. Set `event_digest.max_events_per_turn` conservatively to limit per-turn candidate volume.

## Doctor visibility

The event-digest doctor path is read-only. It reports:

- whether event-digest processing is enabled;
- whether candidate writes are enabled;
- how many candidate rows are currently persisted;
- whether candidate audit events are missing;
- whether high-risk candidates need attention.

Use the standard doctor or dashboard commands to inspect the event-digest section before enabling candidate writes in production.

## Candidate lifecycle

Candidate rows should be reviewed through the governance browser/review commands:

```bash
hermes-scope-recall candidates list --json
hermes-scope-recall candidates promote --id <memory-id> --dry-run --json
hermes-scope-recall candidates archive --id <memory-id> --dry-run --json
hermes-scope-recall candidates supersede --id <memory-id> --superseded-by <replacement-id> --dry-run --json
```

All candidate review commands default to dry-run. Use `--apply` only after inspecting the before/after payload.
