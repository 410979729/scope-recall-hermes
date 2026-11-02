# Claude Code

1. Install the package in the intended Python environment: `python -m pip install "hermes-scope-recall[codex]"`.
2. Run `scope-recall setup --host claude-code`, review its plan and confirm.
3. Start a new host session and complete any approval shown by the host.

Discovery uses `CLAUDE_CONFIG_DIR`, otherwise `~/.claude`. A host-local venv is preferred; setup shows the exact interpreter
and whether it fell back to its own Python. Use `--home` or `--python` only to override that choice.

The plugin is placed in the host skills directory with hooks and MCP wiring. Start a new session after installation.

[Common installation options, models, worker modes and verification](../install.md) apply to this host.
