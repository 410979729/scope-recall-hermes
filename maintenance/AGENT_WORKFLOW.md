# Migration and live-host coordination

Normal installation and configuration use `scope-recall setup`. This document covers operations setup cannot
infer safely: choosing an active host's package controller, stopping live writers, legacy audience mapping,
restoring backups and proving use inside a running host. Package replacement and process restarts remain
separate operator actions. Preserve the existing host until its replacement is verified.

## 1.1 Current package upgrades (including pip-less uv venvs)

Use `scope-recall package-upgrade` from an independent helper environment for offline wheel replacement.

1. Verify the exact interpreter/home/controller and offline wheel identity. Back
   up configuration, installation manifests/receipts/wrappers and consistent SQLite
   snapshots using `scope-recall backup`. Keep these restricted outside the venv.
2. Pause the existing background wake/autostart, then stop the target gateway,
   MCP and workers through their real controllers. Wait for their exit and prevent
   automatic relaunch. `--source-quiesced` is the operator's attestation of this
   boundary, not a command to kill processes. Do not stop another instance.
   A host that is shutting down launches one last detached wake. Under the pause
   it leaves at once; a supervisor in the middle of a pass leaves when
   that pass ends (`drain_seconds`). A host that runs elevated hides these children
   from a normal shell's process listing, so an empty listing is not proof.
   A client attached to a shared store may keep a resident recall server apart from its own
   processes: after quitting the client, stop it with
   `python -I -X utf8 -m scope_recall.maintenance.cli resident stop --home <entry home> --host <client>`
   (`resident status` shows it). It writes nothing; left running, it ends itself within 30 s of the
   package being replaced, and until then it holds the store's files open. If `stop` says
   `still_running` (exit 1), a client process is still up (quit it and stop again), or the server
   cannot be proven or ended by this account (`verified: false` in `status`): check that process
   and end it by hand, or wait for its end.
3. From an independent helper/candidate environment outside the target venv run:
   `python -I -m scope_recall.maintenance.cli package-upgrade --python <target-python>
   --wheel <verified-offline-wheel> --backup <new-private-backup-dir>
   --source-quiesced --uv <external-native-uv>`.
   The helper checks Windows delete sharing for installed files AND directories,
   copies and hashes RECORD files (including entrypoints), then calls external uv
   with `--no-index --no-deps --reinstall-package hermes-scope-recall`.
   It never bootstraps pip, changes dependencies, edits models or starts a host.
4. Lock/access or backup failure before uv means no package uninstall occurred.
   `"state": "blocked"` carries a `reason`. `installed_files_locked_or_not_replaceable`
   means a process still runs from the package, usually the wake of step 2: nothing
   was changed, so wait for it to leave and run step 3 again, or put the host back
   up and come back later. Never kill a process to get past it.
   After `installing` is recorded, any failed/interrupted uv/import check requires
   retaining the backup and keeping hosts paused. Do not blindly retry or delete
   `~*` remnants. Inspect `package-upgrade.json`; use the previous verified wheel
   to restore the package under the same stopped boundary, then verify its
   imports/RECORD/version before restoring matching wrapper/receipt. Automatic
   database rollback is NOT permitted; reconcile any new writes first. Once the
   new package has opened the store, the previous one may refuse its schema.
   Across a schema step, `restore` with the newer package prepares the ledger and
   `restore` with a release compatible with the snapshot finishes it. See the
   [restore guide](../docs/manage.md#migration-and-rollback), including the
   shared-scope limitation when returning to 3.16. Snapshot restore refuses a
   schema below 1110. New captures and ordinary corrections after the snapshot
   are outside its recovery point.
5. `package_verified` means only that package replacement and isolated import
   succeeded. Run `scope-recall setup --host <host>` to reuse the same binding,
   repair its installation and run doctor. Verify receipt/wrapper/package, then restore the original wake policy,
   restart and probe the real host. The package receipt never authorizes restart
   on its own (`host_restart_allowed=false`). Keep the backup until that succeeds.

The Windows preflight rules out the known held-handle failure before uninstall.
It is not a guarantee against disk failure or a new process started in violation
of quiescence. Those failures remain explicit recovery, never a green upgrade.

A home with `scope-recall\attachment.json` is an entry of a shared store
([docs/shared-store.md](../docs/shared-store.md)); its memory and its worker live in
the store's directory. The store's worker and every entry run the same package
version: pause the store's worker and stop every attached host, upgrade the worker's
environment first, then each entry's, then resume. `plan-install`/`apply-install`
and `doctor` on an attached home work as on any other.

## 1.2 A Hermes that builds its own plugin environment

A Hermes home with `installs\<id>\facts.json` runs plugins in an environment its
package manager builds (`packages.venv.environment` in that file), not in the venv
the `hermes` command runs with, and builds it again from each plugin's declaration
in `<home>\plugins\`. Upgrading the venv with `package-upgrade` does not reach the
gateway, and pip or uv into the package manager's environment is undone by its next
build. NumPy vector retrieval is part of the base package; the vector extra remains
accepted for installation compatibility. `doctor` reads that environment and reports
it under `hermes_runtime`.

1. Run plan-install/apply-install with
   `--target-plugin-dir <home>\plugins\scope-recall`. A receipt that names another
   plugin directory is moved (`move` in the plan); a wrapper someone put there by
   hand is backed up and replaced (`replace`). The wrapper's `pyproject.toml`
   declares `hermes-scope-recall[vectors]==<version>`.
2. Rebuild the environment with Hermes's own sync, `HERMES_HOME` set to the home:
   `<hermes-python> -m hermes_cli.venv_sync --project-root <hermes-checkout> --json`.
   Use the actual Hermes source directory, which can be outside the home.
   If the pinned release is unavailable from the package index, set `UV_FIND_LINKS`
   to a directory containing that exact wheel for the sync. Repeat the sync with `UV_FIND_LINKS`
   after every `hermes update`: otherwise rebuilding fails and disables the plugin.
   A running gateway keeps the environment it started with. `"state": "synced"` or
   `"current"` reports environment sync, not that this plugin is selected. Dependency
   resolution can evict an incompatible plugin: Hermes adds
   `scope-recall` to `plugins.disabled` in `config.yaml` and empties
   `memory.provider`. Copy `config.yaml` before the sync and restore it if
   eviction changed the selection; removing only the `plugins.disabled` entry leaves the gateway
   without memory (`doctor`: `host_registration_status: "not_selected"`). Then fix
   the cause and sync again before the gateway restarts.
3. Restart the gateway through its controller and run `doctor`: `hermes_runtime`
   must name the new package version, with `vector_dependencies: true` when
   vectors are configured, and no `hermes_runtime_*` gap may remain. An
   unreadable `hermes_runtime` is unverified; use the account that owns the
   environment to inspect it. Doctor reads package metadata and does not prove
   that an already running gateway loaded the new environment. Verify that
   separately through the host.

## 2. Prepare legacy identity and permissions

Use the source catalog and the old runtime's actual audience/identity settings.
Export exact private, conversation, group, project and shared read/write grants.
Reuse already verified explicit account aliases; never infer ownership from a
display name, textual mention, chat membership, or a scope's similar spelling.

Create a destination installation manifest with the standard installer/host
installation API. Preserve original scope IDs where possible, including retained
history scopes. Exact existing IDs map automatically. Otherwise produce an
injective old-to-new map from verified grants. The target must remain inactive and separate from the source.

Write the manifest whole before anything creates the destination's store:
`install_hermes_scope_recall(home, agent_id=..., agent_workspace=...,
owner_principals=[...], audiences=[...], retained_scope_ids=[...])` writes it and
creates the store together, and `plan-install`/`apply-install` afterwards keep its
rows (`--local-platform` leaves a route declared there as it is). A store keeps the
scopes it was created with: a manifest naming other scopes, written after
`apply-install` or anything else made the store, no longer opens it, which
`migrate prepare` reports. If the target was initialized with the wrong scopes, use a new empty
destination and create its complete manifest before initializing it. The installer's command line declares only local surfaces and
their logins. A gateway route (Telegram, Feishu, ...) is an exact audience row,
and the person on it is an owner principal only when the old grants show it is
the owner: `platform`, `user_id`, `chat_type` (`dm` or `private` for a one-to-one
chat, as the old session key names it), `chat_id`, `thread_id` (empty for an
unthreaded chat), `gateway_session_key` (empty) and `agent_workspace`, with the
scopes it may read and write. Retained scopes are kept for history and readable by
no one: never use them for memory someone should still recall.

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
2. Create/finalize the destination installation manifest using the verified grants,
   before anything creates its store (section 2).
3. Run `scope-recall migrate prepare --source <old-db> --job <job-dir>
   --installation-manifest <new-manifest-or-home> [--scope-map <agent-generated-json>]`.
4. Inspect the structured blockers. `prepared` means the snapshot/catalog/target
   binding are fixed, not that migration is complete.
5. Run `scope-recall migrate run --job <job-dir> --source-quiesced`.
   Supply `--legacy-reader-contract legacy-2.0.1/memories-physical-scope-in-accessible-scopes`
   only after verifying the matching old runtime/schema. Do not apply it to other
   reader semantics. Completed bridge records are archived without replay.
   Known OpenClaw import ledgers are preserved as verified, non-searchable audit
   sidecars, including receipts whose original memory was deleted. They grant no
   access and do not recreate imported or deleted memories.
6. On interruption, run `migrate status`, then `migrate run` on the same job after
   diagnosing the recorded error. No blind retry loop. A changed source requires
   a fresh snapshot before cutover; never discard changes made after preparation.

A record longer than a source holds (65,536 characters) is stored in parts with
every character kept (`segmented_records` in the conversion report). A journal
entry several episodes name has one processing owner; the other episodes keep
its evidence link and leave automatic recall (`legacy_episode_memberships`). An
entry stored under another scope than its episode stays its own source, unlinked
(`episode_source_outside_episode_scope`).

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
silently add another extraction model. Codex host-model consolidation is
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
new host has accepted writes, first stop it and preserve its database and new
writes. Reconcile them before claiming a lossless rollback. Never overwrite the
new database with an old snapshot. The old host's own database is never written by
the migration; `scope-recall backup --database <it> --output <new file>` makes a
verified copy of it. Going back to it keeps nothing the new database received,
deletions included: what was forgotten there since the migration is still in the
old database. To go back to a snapshot of the new database itself, use
`scope-recall restore`.
After going back to an earlier release and forward again, run `scope-recall
retry-failures --config <file>` (the shared worker's config for a shared store), and
with `--apply` if it counts any `inbox_given_up`: a capture the earlier release could
not read may have been given up meanwhile, and nothing else takes it again.
`--apply` also re-opens the failed work a shipped fix may cure, which the preview
counts (`retried`, `by_kind`).
