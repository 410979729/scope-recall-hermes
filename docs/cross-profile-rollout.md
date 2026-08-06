# Cross-profile rollout

Use this guide when updating `scope-recall` across multiple Hermes profile homes under `~/.hermes/profiles/*` or an explicit profiles root.

## Safety model

- Default mode is dry-run: inventory only, no filesystem mutation.
- `--apply` is required to install into profile plugin directories.
- Canary first with `--canary <profile>` before batch rollout.
- Existing `plugins/scope-recall/` directories are copied to a unique per-profile backup path before replacement.
- Rollback uses the rollout receipt and also backs up the current plugin copy before restoring the previous one.
- Rollback validates every receipt action before mutation: target `hermes_home` must stay under `--profiles-root`, new backup paths must stay under the short `backups/sr/o/` root, historical receipts under `backups/scope-recall-rollout/` remain accepted, and backups must contain a valid `scope-recall` plugin manifest.
- If any rollback receipt action fails validation, rollback returns `ok=false` and does not mutate any profile.
- This tool changes plugin files only; it does not mutate SQLite memory truth or vector companion state.
- On Windows, copy/remove/move operations use an internal extended-length path adapter and preflight every destination component before mutation. Receipts always contain normal public paths; the internal `\\?\` prefix is never emitted.

## Inventory / dry-run

```bash
hermes-scope-recall rollout profiles \
  --profiles-root "$HOME/.hermes/profiles" \
  --plan \
  --json
```

`--plan` is an explicit dry-run flag for operators who want the command to read like a deployment plan; dry-run remains the default. `--json` is accepted for consistency with other `hermes-scope-recall` commands, and rollout output is always JSON.

The report includes each profile's home path, installed plugin version, basic config summary, and non-runtime installer verify result.

## Canary rollout

```bash
hermes-scope-recall rollout profiles \
  --profiles-root "$HOME/.hermes/profiles" \
  --canary default \
  --apply \
  --receipt /tmp/scope-recall-rollout-default.json
```

Verify the canary profile after install:

```bash
hermes-scope-recall verify --hermes-home "$HOME/.hermes/profiles/default" --runtime
hermes-scope-recall doctor --hermes-home "$HOME/.hermes/profiles/default" --json
```

## Batch rollout

After the canary is healthy, omit `--canary` or use repeated `--profile` to target a reviewed subset:

```bash
hermes-scope-recall rollout profiles \
  --profiles-root "$HOME/.hermes/profiles" \
  --profile default \
  --profile work \
  --apply \
  --receipt /tmp/scope-recall-rollout-batch.json
```

## Rollback

Dry-run rollback from a receipt:

```bash
hermes-scope-recall rollout profiles \
  --rollback \
  --receipt /tmp/scope-recall-rollout-batch.json
```

Apply rollback:

```bash
hermes-scope-recall rollout profiles \
  --rollback \
  --apply \
  --receipt /tmp/scope-recall-rollout-batch.json
```

After rollback, run verify/doctor for each restored profile.

## Receipt fields

The receipt contains:

- `profiles[]`: inventory for each discovered target profile
- `actions[]`: per-profile planned/applied status
- `backup_path`: pre-rollout plugin backup path when a previous plugin existed
- `previous_version` and `target_version`
- `verify`: non-runtime installer verification after apply

Retain rollout receipts and backup paths as `rollback-needed` until the rollout has been verified healthy across all target profiles.
