# Installing Scope Recall

Install Python 3.11 through 3.14 and `python -m pip install "hermes-scope-recall[codex]"`, then run
`scope-recall setup`. The plan detects host homes and their local Python environments, creates a store or reuses
an attachment, preserves existing scope grants, configures model routes and their budget ledger, installs host
integration and skills, and sets up bounded worker commands. Confirm once to apply it.

Choose your host: [Claude Code](install/claude-code.md), [Codex](install/codex.md), [Hermes](install/hermes.md),
[WorkBuddy](install/workbuddy.md), [DeepSeek Harness](install/dsh.md).

## Plan and apply

`scope-recall setup --dry-run` prints a plan without writing files, opening a writable database or starting workers.
Add `--plan-json` for machine-readable output. `--yes` skips confirmation for an already approved plan.
Use `--host` to select one detected host, or repeat it to choose several. Only override `--home` or `--python`
when discovery cannot find your installation. All options are in the generated [configuration reference](configuration.md).

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
Set credentials in the process environment before starting the host; setup does not print or save their values.

Setup and doctor share one workload catalog. Per-hundred-turn figures separate measured ratios, planning
assumptions and reserved token estimates. Tasks without a measurement carry no invented allowance. The catalog
is an estimate for planning, not an automatic authorization to spend. The existing budget ledger enforces
approved models, prices, tokens, calls and calendar periods.

## Background work

The default `on-demand` mode uses the existing bounded workers triggered by host activity. It registers no
system service, login item or Windows scheduled task. Setup writes `worker-command.json` beside the runtime
configuration with the exact Python command. `--worker manual` disables the continuing supervisor and prints
a command for explicit worker passes. Host capture maintenance still uses its existing bounded processing path.
For unattended scheduled wakes, use the existing explicit [background operation commands](background-work.md).

## Verification and readiness

Setup runs doctor against the selected installation, then captures a synthetic statement, runs a real worker
pass and recalls it in a second session in an independent disposable store. Its routes match the selected
configuration. No self-test conversation is added to the owner's memory. A successful zero-budget check reports
**原文检索可用**. The model-enabled state **完整记忆功能可用** additionally requires working extraction,
embedding and model-backed retrieval through the configured ledger. Failures list the step, completed stages,
receipts, configuration backups and retry instructions, without raw exception text or credentials.

The installation check exercises host adapters and installed commands. It cannot approve a host's trust dialog
or prove a running desktop host has reloaded its settings. Start a new host session and perform the two-session
check in the host after approval. Windows isolated checks are covered by the test suite; clean Linux/macOS
machines and running host processes require platform acceptance.

## Going back to a backup

Keep the backup outside the installation and stop its writers before restoring. Run `scope-recall restore --help`
and follow the [management migration and rollback guide](manage.md#migration-and-rollback) with the exact
snapshot and destination. A setup failure does not delete the database. Receipt-backed `apply-uninstall` removes
installed wrappers while retaining memory by default; it is separate from restoring host configuration backups.

## Legacy installations

When setup detects a legacy or unreadable database, it stops before installation and preserves it.
Use the [migration workflow](../maintenance/AGENT_WORKFLOW.md) to resolve original audiences, back up,
quiesce writers and run the existing durable conversion. Setup does not guess legacy scope permissions.
