---
name: scope-recall-setup
description: Install or upgrade Scope Recall when the user asks the agent to set it up, update it, or migrate old Scope Recall memories. Automatically distinguish first-time installation from legacy migration.
---

The user asks for the result; perform the technical work as their agent.

1. Find the actual host's instance home and Python/plugin environment. Inspect
   with `scope-recall setup --host <hermes-or-codex> --home <instance-home>`.
   Check active host configuration first; pass `--database <actual-db>` for a
   custom path. Absence at default paths alone must not hide an old database.
   If the old package lacks this entry point, inspect the new release in an
   isolated helper environment before touching the running installation.
2. Follow its route. `fresh_install` goes straight to normal installation and
   host checks. `current_upgrade` preserves the current database format.
   `legacy_migration` uses the bundled workflow printed by
   `scope-recall setup --workflow`.
3. Resolve paths, backups, exact audience mapping and restart commands yourself.
   Do not ask the user to run commands or review individual memories. Do not
   replace uncertainty about identity/permissions with an owner-private catchall.
4. Preserve the user's model choices. Report successful live use separately from
   package installation, database conversion and pending embedding indexing.

Use the existing migration job and worker rather than generating a second
conversion script or repeatedly re-extracting old journals. Preserve failed
evidence and stop retrying an unchanged failure.
