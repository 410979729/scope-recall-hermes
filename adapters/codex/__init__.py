"""The entry modules installed clients run, under the names their configurations hold.

Installers write ``python -m scope_recall.adapters.codex.<entry>`` into Codex's, Claude Code's, WorkBuddy's and
dsh's configurations and into scheduled tasks, and Codex trusts a hook, as WorkBuddy approves an MCP server, by its
command: a new module name would leave every hook skipped until the owner approved it again.  So these names stay,
and each module here only runs its namesake in ``adapters/clients``, where the code for all of those clients lives.
This package imports nothing of its own, so a hook's start pays for nothing more.
"""
