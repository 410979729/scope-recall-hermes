---
name: scope-recall-setup
description: Install or upgrade Scope Recall when the user asks the agent to set it up, update it, or migrate old Scope Recall memories. Automatically distinguish first-time installation from legacy migration.
---

# Install or upgrade Scope Recall

Run `scope-recall setup --dry-run --plan-json` and review the detected hosts, store, scopes, model budget,
worker mode and project-copy suggestions with the owner. Apply the approved plan with `scope-recall setup`.
Use `--yes` only for an already approved scripted invocation. Existing installations retain their store and grants.

Use the migration workflow when setup detects legacy data or when a live host needs package replacement or
process coordination. Preserve model choices and distinguish the isolated installation check from actual use
inside a host after it has reloaded and approved the hooks.
