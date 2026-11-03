# Installing Scope Recall

Install Python 3.11 through 3.14 and `python -m pip install -U "hermes-scope-recall[codex]"`, then run
`scope-recall setup`. The plan detects host homes and their local Python environments, creates a store or reuses
an attachment, preserves existing scope grants, configures model routes and their budget ledger, installs host
integration and skills, and sets up bounded worker commands. Confirm once to apply it.

The package-index command installs a published release. To use this development checkout, run
`python -m pip install -U ".[codex]"` from its root; to use an unpublished wheel, install that exact wheel with
the `codex` extra. Updating a package in use requires stopping its hosts and workers first.

Choose your host: [Claude Code](install/claude-code.md), [Codex](install/codex.md), [Hermes](install/hermes.md),
[WorkBuddy](install/workbuddy.md), [DeepSeek Harness](install/dsh.md).

For a shared store on a Linux Docker host, see [Docker deployment](docker.md).

## Plan and apply

`scope-recall setup --dry-run` prints a plan without changing user files, host configuration or stores,
opening a writable database or starting workers. Python may still write its own bytecode cache beside the
installed package.
Add `--plan-json` for machine-readable output. `--yes` skips confirmation for an already approved plan.
Use `--host` to select one detected host, or repeat it to choose several. Only override `--home` or `--python`
when discovery cannot find your installation. All options are in the generated [configuration reference](configuration.md).

The plan and doctor report known host memory settings and file sizes without changing them. A configured switch
does not establish what a particular session loaded: host policies, session options and frozen snapshots can differ.
Unknown settings and unsupported file locations remain unknown. Keep project rules and host habits in host memory;
keep preferences and decisions in Scope Recall. Existing host memory can be reviewed and imported using the
[memory import workflow](import.md). Automatic recall skips an exact statement already in an enabled host memory file
only when the file predates the session and can be read within the bounded check. Otherwise recall proceeds normally.
Headings, parent list items and paragraph continuations remain part of the compared host statement. A claim with
validity dates or other qualifiers outside its comparison text is not skipped.
For Claude Code and Codex, the check also needs a recorded SessionStart; rerun setup after upgrading to register the
Claude Code SessionStart hook, then start a new session. Hermes uses the session store start time. Explicit recall
continues to return matching Scope Recall records.

`scope-recall setup --store <absolute-directory>` joins an existing shared store or creates a new one there.
The default new store is `~/.scope-recall/shared`. Existing attached homes keep their store; moving a home to
another store remains an explicit [shared-store operation](shared-store.md). Existing local Hermes or Codex
installations keep their local database and identity. Rerunning setup preserves original conversations and
existing model choices, and restores missing installation artifacts.

A second checkout with the same origin is only a suggestion. For a local Codex installation, setup and doctor
show it; approve registration separately in the interactive setup, or pass `--register-project-copy` after
reviewing the plan. `--yes` alone never grants a new directory. Git worktrees use the existing verified common
repository identity. Shared entries bind their registered identity independently of the current directory.

## Model routes and budgets

New installations have no approved model routes and all cumulative model budgets are zero. Capture and
original-text search work without any model call. Run `scope-recall setup --configure-models` to enter endpoint,
model, dimensions, credential environment names, pricing and spending limits before reviewing the plan.
For scripts, `--models` reads a JSON object containing the existing `auxiliary` and `vector` route definitions
and an optional `vector_threshold`. Unknown input fields are never copied to generated configuration.
Set credentials in the process environment before starting the host; setup does not collect or log their values.

Setup and doctor share one workload catalog measured with synthetic conversations through the real worker,
including ended-session and completed-task summaries and adoption processing.
Request token figures are estimates of message contents; chat planning also applies a floor from recorded
server usage. The wizard asks for daily turns and a budget period, scales the expected usage to that horizon,
then shows a variation factor and a separate allowance for simultaneous reservations. A lifetime horizon
never renews itself. Tasks without measurements have no invented allowance. The amount limit remains zero
until explicitly changed. The existing ledger enforces approved models, prices, tokens, calls and periods.

Changed configuration is backed up under a unique name beside the original file. These byte-exact copies
can contain existing service settings and keep the source's Windows access control; POSIX copies are private
to their owner. Receipts record backup paths and hashes, never configuration contents.

## Background work

The default `on-demand` mode uses the existing bounded workers triggered by host activity. It registers no
system service, login item or Windows scheduled task. Setup writes `worker-command.json` beside the runtime
configuration with the exact Python command. `--worker manual` disables the continuing supervisor and prints
a command for explicit worker passes. Host capture maintenance still uses its existing bounded processing path.
For unattended scheduled wakes, use the existing explicit [background operation commands](background-work.md).

## Verification and readiness

Setup runs doctor against the selected installation, then captures a synthetic statement, runs a real worker
pass and recalls it in a second session in an independent disposable store. Its routes match the selected
configuration. The selected truth store must also pass doctor; vector and embedding faults in that store
prevent the complete state even when the isolated probe succeeds. No self-test conversation is added to the owner's memory. A successful zero-budget check reports
**原文检索可用**. The model-enabled state **完整记忆功能可用** additionally requires working extraction,
embedding and model-backed retrieval through the configured ledger. Failures list the step, completed stages,
receipts, configuration backups and retry instructions, without raw exception text or credentials.

The setup self-check directly calls Python host adapters. The separate installation test suite executes the
installed hook commands and provider wrappers. It cannot approve a host's trust dialog
or prove a running desktop host has reloaded its settings. Start a new host session and perform the two-session
check in the host after approval. Windows isolated checks are covered by the test suite; clean Linux/macOS
machines and running host processes require platform acceptance.

## Upgrading from 3.16 to 4.0

Stop every host, remote entry server, resident recall process and worker opening the store. Take a consistent
SQLite backup and retain the old package, runtime configuration, manifests and host settings. Upgrade the shared
worker's environment and every entry to the same package version, then rerun setup for each host to refresh copied
hooks and plugins. A Hermes-managed plugin environment needs its own
[environment rebuild](install/hermes.md#package-managed-plugin-environments), including its local wheel source
when the pinned version is unavailable from the package index.

The coordinated store upgrade saves a verified pre-upgrade snapshot and upgrades supported Core layouts to schema
1134. Run the foreground upgrade through an attached Codex entry before restarting hosts:

```sh
scope-recall upgrade-store --host codex --instance-root /path/to/attached-codex-home --backup-dir /path/to/new-upgrade-backups
```

This command accepts only `hermes` or `codex`
as `--host`; use the selected host's home for a local store too. Other entries upgrade through setup or ordinary
coordinated opening.
Foreground upgrade has no total deadline; wait for completion. Ordinary recall can be unavailable until index
backfill completes. Keep free space for the database, snapshots, WAL and new indexes.

After upgrade, run doctor for each entry, start new host sessions and verify capture and recall through the hosts.
A package/import check alone does not establish that a running host loaded the update. Remote clients need their
own update; see [remote compatibility](remote-entries.md).

After reconnecting a Claude Code, Codex or WorkBuddy entry, recover sessions recorded while its hooks were disabled
with `scope-recall replay-sessions`. The operator declares that `--records` belongs to the selected entry; use the
host's `projects` directory for Claude Code or WorkBuddy, or its `sessions` directory for Codex. Preview first:

```sh
scope-recall replay-sessions --host claude-code --instance-root /path/to/entry --records /path/to/claude/projects --since '<start-with-timezone>' --until '<end-with-timezone>' --json
scope-recall replay-sessions --host claude-code --instance-root /path/to/entry --records /path/to/claude/projects --since '<start-with-timezone>' --until '<end-with-timezone>' --yes --preview-id '<preview-id>' --json
```

Check the session IDs, recorded working directories, time ranges, event counts and rejection reasons in the preview.
The time bounds include both endpoints. The confirmation must use the same parameters; if records change, preview
again. Keep the selected records unchanged throughout execution: the command checks their fingerprint before it
starts writing. Current grants, entry state and private intervals still apply, and repeated recovery does not duplicate
events.

## Going back to a backup

Keep the backup outside the installation and stop its writers before restoring. Run `scope-recall restore --help`
and follow the [management migration and rollback guide](manage.md#migration-and-rollback) with the exact
snapshot and destination. A setup failure does not delete the database. Receipt-backed `apply-uninstall` removes
installed wrappers while retaining memory by default; it is separate from restoring host configuration backups.

## Legacy installations

When setup detects a legacy or unreadable database, it stops before installation and preserves it.
Use the [migration workflow](../maintenance/AGENT_WORKFLOW.md) to resolve original audiences, back up,
quiesce writers and run the existing durable conversion. Setup does not guess legacy scope permissions.
