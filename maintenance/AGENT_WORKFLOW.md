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

Find out which Hermes surface the user actually talks through. The CLI is the
owner's by default. Hermes Desktop's chat panel (`desktop`) and `hermes --tui`
(`tui`) name no user unless a dashboard login exists, and such a session is
refused (`user principal required for non-cli platform`) until the installer
approves the surface: add `--local-platform desktop` and/or `--local-platform tui`
to plan-install and apply-install, on a fresh install or later on the same
instance. Approve one only where everyone who can reach that surface without
logging in is the owner; if you cannot tell, ask the user that one question.
On a host that serves its dashboard to other machines this is every dashboard
login: the dashboard's Chat tab runs there as a session that names no user.
A Hermes whose gateway runs these conversations, and the CLI's turns, over its
local session transport names the gateway's own OS account instead (`sid:<SID>`,
`uid:<uid>`): that binds as the owner on an approved surface, and on another
binds nothing while the host log names the approval, so approve the same way.
A session that carries a dashboard login (`hermes serve` with
`dashboard.basic_auth`, or the Desktop app connected to such a host) is that
login, `basic:<name>` (over the transport `auth:v1:["basic","","<name>"]`), never
the local owner, and binds nothing until approved: add
`--owner-login desktop=<login>` (or `tui=`) with the login the host log names,
only for a login that is the owner's own, and ask which one is if you cannot tell.
On an entry of a shared store `apply-install` approves neither: add the owner
principal and row with `scope-recall grants` (docs/shared-store.md).
A Desktop installation of 1.9 to 2.x wrote under a minted `srdesk_*` principal. When
the old runtime's own settings show that principal is the owner, keep its private
scope as it is: list that scope ID in `allowed_scope_ids` of the owner's rows (the
CLI row, and a desktop row on the route `--local-platform desktop` writes: chat
type `private`, chat `local`, thread `main`) when you create the destination
(section 2), so it maps to itself and the same person reads the same memory on
both surfaces. Mapping it onto
`owner_private` instead works only when no other old scope maps there, since the
scope map is one-to-one. The 3.x host never sends that principal, so it needs no
owner principal of its own. Never approve anything for a scheduled (`cron`) run;
the installer refuses it.

## 1.1 Current package upgrades (including pip-less uv venvs)

Reuse this workflow and the installed `maintenance.cli package-upgrade` command.
The root `managed_upgrade.py` of old plugin trees is not in the wheel; do not
route wheel-based installations through it.

1. Verify the exact interpreter/home/controller and offline wheel identity. Back
   up configuration, installation manifests/receipts/wrappers and consistent SQLite
   snapshots using `maintenance.cli backup`. Keep these restricted outside the venv.
2. Pause the existing background wake/autostart, then stop the target gateway,
   MCP and workers through their real controllers. Wait for their exit and prevent
   automatic relaunch. `--source-quiesced` is the operator's attestation of this
   boundary, not a command to kill processes. Do not stop another instance.
   A host that is shutting down launches one last detached wake. Under the pause
   it leaves at once (a package up to 3.1.1 reads the pause only after its start
   delay, up to `worker_min_interval_seconds`); a supervisor in the middle of a pass leaves when
   that pass ends (`drain_seconds`). A host that runs elevated hides these children
   from a normal shell's process listing, so an empty listing is not proof.
   A client attached to a shared store may keep a resident recall server apart from its own
   processes (WorkBuddy by default): after quitting the client, stop it with
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
   new package has opened the store, the previous one may refuse it: 3.2's first
   open moves a 3.1 store to schema 1110, which 3.1 cannot open. Going back then
   needs the external pre-upgrade backup, put back with `scope-recall restore`
   (never by copying the file, which brings back what was forgotten since); other
   writes made since are lost. Across a schema step, `restore` with the newer
   package prepares and `restore` with the older one finishes (docs/install.md,
   section 9); a snapshot below schema 1110 is refused.
5. `package_verified` means only that package replacement and isolated import
   succeeded. Run existing plan-install/apply-install for the same binding and
   doctor, verify receipt/wrapper/package, then restore the original wake policy,
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
build. A declaration written there by hand without the vector extra leaves a LanceDB
store's recall without vectors; `doctor` reads that environment and reports it
under `hermes_runtime`.

1. Once the release is on PyPI, run plan-install/apply-install with
   `--target-plugin-dir <home>\plugins\scope-recall`. A receipt that names another
   plugin directory is moved (`move` in the plan); a wrapper someone put there by
   hand is backed up and replaced (`replace`). The wrapper's `pyproject.toml`
   declares `hermes-scope-recall[lancedb]==<version>`.
2. Rebuild the environment with Hermes's own sync, `HERMES_HOME` set to the home:
   `<hermes python> -m hermes_cli.venv_sync --project-root <home>\hermes-agent --json`.
   A running gateway keeps the environment it started with. `"state": "synced"` or
   `"current"` is success. A failed sync disables the plugin: Hermes adds
   `scope-recall` to `plugins.disabled` in `config.yaml` and empties
   `memory.provider`. Copy `config.yaml` before the sync and put the copy back after
   a failed one; removing only the `plugins.disabled` entry leaves the gateway
   without memory (`doctor`: `host_registration_status: "not_selected"`). Then fix
   the cause and sync again before the gateway restarts.
3. Restart the gateway through its controller and run `doctor`: `hermes_runtime`
   must name the new version with `vector_dependencies: true`, and no
   `hermes_runtime_*` gap may remain. A Hermes that runs elevated may keep that
   environment's interpreter readable only to an elevated process; then run step 2
   from an elevated shell.

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

Write the manifest whole before anything creates the destination's store:
`install_hermes_scope_recall(home, agent_id=..., agent_workspace=...,
owner_principals=[...], audiences=[...], retained_scope_ids=[...])` writes it and
creates the store together, and `plan-install`/`apply-install` afterwards keep its
rows (`--local-platform` leaves a route declared there as it is). A store keeps the
scopes it was created with: a manifest naming other scopes, written after
`apply-install` or anything else made the store, no longer opens it, which
`migrate prepare` reports. The destination holds nothing before `migrate run`
(`doctor` reports `sources: 0`), so start again: remove the destination's
`scope-recall` folder, never the source's, and create it whole. The installer's command line declares only local surfaces and
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

## Completion report

Tell the user whether this was a fresh install or an upgrade, the actual loaded
version, whether old memories are usable, and whether background indexing is
still running. Do not expose commands, schema numbers or scope maps unless asked.
No human memory-governance queue is part of this workflow.
