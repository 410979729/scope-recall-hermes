# Scope Recall: agent-operated installation and upgrade

Use this workflow when the user says “install Scope Recall”, “upgrade Scope Recall”,
“帮我安装记忆插件”, or “帮我升级一下 scoperecall”. The user should not execute
commands, find paths, write scope maps, review memories, or interpret database errors.
Their installation/upgrade request authorizes the normal reversible work below.

This is an executable operator workflow for the assistant. Keep implementation
details out of the user's interaction. Report only outcome, any real limitation,
and an action genuinely requiring the user's knowledge or authorization.

## 1. Discover before changing anything

Inspect the active host's interpreter, plugin import path, instance home, and its
service/process controller. Use the active runtime, not an arbitrary shell Python.
If the old version lacks these commands, install the new release into a separate
helper environment first. Do not overwrite the old running environment to inspect it.

Run `scope-recall setup --host hermes --home <actual-instance-home>` (or `--host codex`).
If host configuration selects a custom database path, pass `--database <actual-db>`.
Check the active configuration before interpreting absence at standard paths as a
fresh install. An explicit missing database is an error, not an empty new account.
The structured `route` selects exactly one branch:

- `fresh_install`: no old database. Use the standard plan-install/apply-install
  workflow, configure the user's chosen models, register the host adapter, and
  check the actual host. Do not run migration or create legacy reprocessing jobs.
- `current_upgrade`: preserve the current database and configuration, back them
  up, update the package/wrapper, run the supported Core schema upgrade and host
  probes. Do not feed an existing Core database into the legacy converter.
- `legacy_migration`: follow the procedure below.
- `repair_required`, `unsupported`, or `needs_agent_inspection`: inspect the exact
  cause and preserve the running old version. Never call this a fresh installation
  or force an unknown schema through a converter.

The common install CLI takes explicit host/instance/plugin/project/Python paths.
Resolve these yourself from the host. Never ask a nontechnical user to fill them in.

## 1.1 Current package upgrades (D14, including pip-less uv venvs)

Reuse this workflow and the installed `maintenance.cli package-upgrade` command.
The historical root `managed_upgrade.py` upgrades old plugin trees and is not in
the current wheel; do not route wheel-based installations through that engine.

1. Verify the exact interpreter/home/controller and offline wheel identity. Back
   up configuration, installation manifests/receipts/wrappers and consistent SQLite
   snapshots using `maintenance.cli backup`. Keep these restricted outside the venv.
2. Pause the existing background wake/autostart, then stop the target gateway,
   MCP and workers through their real controllers. Wait for their exit and prevent
   automatic relaunch. `--source-quiesced` is the operator's attestation of this
   boundary, not a command to kill processes. Do not stop another instance.
3. From an independent helper/candidate environment outside the target venv run:
   `python -I -m scope_recall.maintenance.cli package-upgrade --python <target-python>
   --wheel <verified-offline-wheel> --backup <new-private-backup-dir>
   --source-quiesced --uv <external-native-uv>`.
   The helper checks Windows delete sharing for installed files AND directories,
   copies and hashes RECORD files (including entrypoints), then calls external uv
   with `--no-index --no-deps --reinstall-package hermes-scope-recall`.
   It never bootstraps pip, changes dependencies, edits models or starts a host.
4. Lock/access or backup failure before uv means no package uninstall occurred.
   After `installing` is recorded, any failed/interrupted uv/import check requires
   retaining the backup and keeping hosts paused. Do not blindly retry or delete
   `~*` remnants. Inspect `package-upgrade.json`; use the previous verified wheel
   to restore the package under the same stopped boundary, then verify its
   imports/RECORD/version before restoring matching wrapper/receipt. Automatic
   database rollback is NOT permitted; reconcile any new writes first.
5. `package_verified` means only that package replacement and isolated import
   succeeded. Run existing plan-install/apply-install for the same binding and
   doctor, verify receipt/wrapper/package, then restore the original wake policy,
   restart and probe the real host. The package receipt never authorizes restart
   on its own (`host_restart_allowed=false`). Keep the backup until that succeeds.

The Windows preflight removes the known D14 held-handle path before uninstall.
It is not a guarantee against disk failure or a new process started in violation
of quiescence. Those failures remain explicit recovery, never a green upgrade.

## 2. Prepare legacy identity and permissions

Use the source catalog and the old runtime's actual audience/identity settings.
Export exact private, conversation, group, project and shared read/write grants.
Reuse already verified explicit account aliases; never infer ownership from a
display name, textual mention, chat membership, or a scope's similar spelling.

Create a destination installation manifest with the standard installer/host
installation API. Preserve original scope IDs where possible, including retained
history scopes. Exact existing IDs map automatically. Otherwise produce an
injective old-to-new map from verified grants yourself; this is agent work, not
user memory review. The target must remain inactive and separate from the source.

The migration preparer reports all missing scopes together. Resolve their runtime
bindings from the old host's configuration/session routing records, not by dumping
all old scopes into owner_private. Unsupported routes keep the old host available.
An identity declaration must preserve both who can read and who can write.
For a Hermes legacy database with digest audit tables, pass
`legacy_audit_retention=True` to the existing installation API. This registers
two fixed, non-searchable audit namespaces; it does not archive the user's
runtime memories or enable the TEST-only archive migration mode.

## 3. Quiesce and migrate through the durable job

Choose a private job directory outside both source and destination namespaces.
Back up host configuration, old plugin/package identity and required attachment
files as well as the automatic database snapshot. Keep original backups restricted
and out of automatic memory/skill discovery.

1. Quiesce the host's memory writers through its real controller. When upgrading
   the agent's own host, launch a bounded detached helper before stopping it;
   record restart/rollback commands first. On Windows helpers must be hidden.
2. Create/finalize the destination installation manifest using the verified grants.
3. Run `scope-recall migrate prepare --source <old-db> --job <job-dir>
   --installation-manifest <new-manifest-or-home> [--scope-map <agent-generated-json>]`.
4. Inspect the structured blockers. `prepared` means the snapshot/catalog/target
   binding are fixed, not that migration is complete.
5. Run `scope-recall migrate run --job <job-dir> --source-quiesced`.
   Supply `--legacy-reader-contract tianshu-2.0.1/memories-physical-scope-in-accessible-scopes`
   only after verifying the matching old runtime/schema. Do not apply it to other
   reader semantics. Completed bridge records are archived without replay.
   Known OpenClaw import ledgers are preserved as verified, non-searchable audit
   sidecars, including receipts whose original memory was deleted. They grant no
   access and do not recreate imported or deleted memories.
6. On interruption, run `migrate status`, then `migrate run` on the same job after
   diagnosing the recorded error. No blind retry loop. A changed source requires
   a fresh snapshot before cutover; never discard changes made after preparation.

The converter preserves original status, evidence/history and deletion closure.
Do not re-extract the entire old journal or promote every old candidate. The
snapshot retains originals when a type cannot be represented as an active fact.
Unknown/incomplete conversions block activation rather than silently succeeding.

## 4. Restore retrieval and activate the real host

Run `scope-recall migrate verify --job <job-dir>`. It checks the bound report,
SQLite integrity and source/claim/version counts. Verification is a migration
gate, not proof that the live host has loaded the new plugin.

Configure the destination's approved embedding route and own budget ledger using
the existing runtime configuration. Preserve the user's main model choice. Never
silently add another extraction model. Current Codex host-model consolidation is
a separate capability boundary; installing/upgrading cannot claim to create it.

Run `scope-recall migrate queue-index --job <job-dir>` until `index_queue_complete`
is true; each call schedules at most 128 source rows through the existing worker.
Enable the normal runtime autostart/resume mechanism once, so network/restart
interruptions resume without a second migration worker. Rebuild only eligible
current sources. Do not run legacy journal consolidation as an upgrade step.

Activate through the host's actual configuration/controller. Keep the source
namespace and backup intact. If the host requires a different final data path,
create its final installer binding and rerun conversion there BEFORE activation;
do not rename a bound database into a different installation path. Verify the
actual host interpreter/import path/version and a real memory status/recall call.

Test a few representative old questions (including changed/deleted facts and
different audiences). Use offline deterministic checks for the rest. Do not
substitute a successful source count or a synthetic import for real-host recall.
Read-only keyword availability is `lexical_ready`; say indexing is still running
until the actual vector coverage is checked. Do not declare full readiness while
the auxiliary route is unavailable or a required identity mapping is unresolved.

## 5. Failure and rollback

If conversion or host checks fail, leave/restart the old host using its verified
original namespace/configuration. Preserve the failed job and exact error. If the
new host has accepted writes, first freeze it and preserve its database and new
writes. Reconcile them before claiming a lossless rollback. Never overwrite the
new database with an old snapshot. `scope-recall rollback` supplies the existing
snapshot checks; it deliberately refuses unsafe replacement when new writes exist.

## Completion report

Tell the user whether this was a fresh install or an upgrade, the actual loaded
version, whether old memories are usable, and whether background indexing is
still running. Do not expose commands, schema numbers or scope maps unless asked.
No human memory-governance queue is part of this workflow.
