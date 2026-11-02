# WorkBuddy

1. Install the package in the intended Python environment: `python -m pip install "hermes-scope-recall[codex]"`.
2. Run `scope-recall setup --host workbuddy`, review its plan and confirm.
3. Start a new host session and complete any approval shown by the host.

Discovery uses `CODEBUDDY_CONFIG_DIR`, otherwise `~/.workbuddy`. A host-local venv is preferred; setup shows the exact interpreter
and whether it fell back to its own Python. Use `--home` or `--python` only to override that choice.

Close WorkBuddy before changing its settings, then reopen it. Approve Scope Recall in its MCP settings. Setup merges hooks and MCP configuration while preserving other keys and installs skills/scope-recall-memory/SKILL.md.

[Common installation options, models, worker modes and verification](../install.md) apply to this host.
